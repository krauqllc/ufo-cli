#!/usr/bin/env python3
"""Verify packaged `ufo edit` outputs against pinned bytes, results, and reopen.

An argument may name a corpus file as ``{corpus}/<relative path>`` (an archive
addition, for example). The evaluator stages a copy of that file inside the run
directory with a fixed modification time before the edit runs, so an added
entry's stored timestamp, and therefore the pinned output hash, is the same on
every machine and on both packaged surfaces.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import download_corpus
    import download_realworld
    import evaluate_cli_corpus
    import evaluate_desktop_corpus
except ModuleNotFoundError:
    from tools.corpus import (
        download_corpus,
        download_realworld,
        evaluate_cli_corpus,
        evaluate_desktop_corpus,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.cli.artifact_identity import container_artifact, launcher_artifact
from tools.cli.bounded_process import docker_image_id, run_bounded_command

DEFAULT_EXPECTATIONS = Path(__file__).with_name("edit_expectations.tsv")
DEFAULT_RECEIPTS = REPO_ROOT / "local-corpus" / "reports" / "cli-edit-receipts.jsonl"
DEFAULT_SUMMARY = REPO_ROOT / "local-corpus" / "reports" / "cli-edit-summary.json"
EXPECTATION_HEADER = [
    "case_id", "source_id", "operation", "arguments_json", "status", "code",
    "output_size_bytes", "output_sha256", "result_json",
]
OPERATIONS = (
    "rotate", "pages", "metadata", "accept-changes", "reject-changes",
    "replace-text", "cells", "archive", "stamp", "form",
)
CORPUS_TOKEN = "{corpus}"
ADDED_FILES_DIRECTORY = "added"
# 2001-09-09T01:46:40Z: a whole even second, so ZIP's two-second clock keeps it.
STAGED_FILE_MTIME_SECONDS = 1_000_000_000
CASE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{2,63}")
RECEIPT_KEYS = {
    "schema", "schemaVersion", "engineVersion", "sequence", "action", "operation",
    "status", "source", "output", "result", "plan", "code", "message", "limits",
    # Every receipt records the licence the run was under, free tier included.
    "durationMillis", "license", "network",
}
SOURCE_KEYS = {
    "path", "name", "sizeBytes", "lastModifiedMillis", "sha256",
    "stableDuringProcessing",
}
OUTPUT_KEYS = {"path", "sizeBytes", "sha256"}
RESULT_KEYS = {"engine", "applied", "skipped", "outputExtension"}
APPLIED_KEYS = {"kind", "count", "detail"}
# A Word, Excel or PowerPoint output carries UFO's provenance stamp, and its receipt says so in
# `result.stamp`: the UFO properties written and every member the stamp wrote. A pinned result
# may carry one; the receipt must then report exactly the pinned stamp, as it reports the rest.
# The shape is the edit receipt schema's `result.stamp`.
STAMP_KEYS = {"properties", "parts"}
STAMP_PROPERTIES = {"UFO.Version", "UFO.SourceSha256", "UFO.LastModified"}


def valid_stamp(value: object) -> bool:
    return value is None or (
        isinstance(value, dict) and set(value) == STAMP_KEYS
        and isinstance(value["properties"], dict) and 1 <= len(value["properties"]) <= 3
        and all(key in STAMP_PROPERTIES and isinstance(text, str) and 1 <= len(text) <= 128
                for key, text in value["properties"].items())
        and isinstance(value["parts"], list) and len(value["parts"]) <= 3
        and all(isinstance(part, str) and 1 <= len(part) <= 256 for part in value["parts"])
    )
EXPECTED_LIMITS = {
    "maxSourceBytes": 67_108_864,
    "workerTimeoutMillis": 30_000,
    "workerMaxHeapMiB": 512,
}
# The edited copy must reopen exactly like its source: same intake status,
# same resolved kind and format, same probe outcome.
REOPEN_DETECTION_FIELDS = ("resolvedKind", "resolvedFormat", "contentFormat")


def _hex_digest(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_arguments(arguments: object, line_number: int) -> list[str]:
    if not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
        raise ValueError(f"line {line_number}: arguments_json is not a list of strings")
    for argument in arguments:
        if not argument or any(c in argument for c in ("\0", "\r", "\n")):
            raise ValueError(f"line {line_number}: empty or control-character argument")
        if argument in ("-o", "--output", "--") or argument.startswith("--output="):
            raise ValueError(f"line {line_number}: the evaluator owns the output option")
        for piece in argument.split("="):
            if CORPUS_TOKEN in piece:
                if not piece.startswith(CORPUS_TOKEN + "/"):
                    raise ValueError(f"line {line_number}: {CORPUS_TOKEN} must start a path")
                relative = PurePosixPath(piece[len(CORPUS_TOKEN) + 1:])
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ValueError(f"line {line_number}: unsafe corpus-relative path")
    return list(arguments)


def read_expectations(path: Path = DEFAULT_EXPECTATIONS) -> OrderedDict[str, dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != EXPECTATION_HEADER:
            raise ValueError(f"unexpected edit expectation header: {reader.fieldnames}")
        cases: OrderedDict[str, dict] = OrderedDict()
        for line_number, row in enumerate(reader, start=2):
            case_id = row["case_id"]
            if not CASE_ID_PATTERN.fullmatch(case_id) or case_id in cases:
                raise ValueError(f"line {line_number}: invalid or duplicate case id {case_id!r}")
            if not row["source_id"]:
                raise ValueError(f"line {line_number}: empty source id")
            if row["operation"] not in OPERATIONS:
                raise ValueError(f"line {line_number}: unknown operation {row['operation']!r}")
            try:
                arguments = validate_arguments(json.loads(row["arguments_json"]), line_number)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {line_number}: arguments_json is not JSON") from error
            case = {
                "caseId": case_id,
                "sourceId": row["source_id"],
                "operation": row["operation"],
                "arguments": arguments,
                "status": row["status"],
            }
            if row["status"] == "completed":
                if row["code"] or not row["output_size_bytes"].isdigit():
                    raise ValueError(f"line {line_number}: a completed case pins size, not a code")
                if not _hex_digest(row["output_sha256"]):
                    raise ValueError(f"line {line_number}: invalid output SHA-256")
                try:
                    result = json.loads(row["result_json"])
                except json.JSONDecodeError as error:
                    raise ValueError(f"line {line_number}: result_json is not JSON") from error
                if (
                    not isinstance(result, dict)
                    or set(result) - {"stamp"} != RESULT_KEYS
                    or not valid_stamp(result.get("stamp"))
                    or not isinstance(result["applied"], list)
                    or not result["applied"]
                    or any(
                        not isinstance(item, dict) or set(item) != APPLIED_KEYS
                        or type(item["count"]) is not int or item["count"] < 1
                        for item in result["applied"]
                    )
                    or not isinstance(result["skipped"], list)
                ):
                    raise ValueError(f"line {line_number}: result_json is not an edit result")
                case.update(
                    outputSizeBytes=int(row["output_size_bytes"]),
                    outputSha256=row["output_sha256"],
                    result=result,
                )
            elif row["status"] == "refused":
                if not CODE_PATTERN.fullmatch(row["code"]):
                    raise ValueError(f"line {line_number}: a refused case pins a refusal code")
                if row["output_size_bytes"] or row["output_sha256"] or row["result_json"]:
                    raise ValueError(f"line {line_number}: a refused case has no output")
                case["code"] = row["code"]
            else:
                raise ValueError(f"line {line_number}: status must be completed or refused")
            cases[case_id] = case
    if not cases:
        raise ValueError("edit expectations are empty")
    return cases


def referenced_corpus_paths(arguments: list[str]) -> list[str]:
    """Corpus-relative paths named through the {corpus} token, in argument order."""
    paths: list[str] = []
    for argument in arguments:
        for piece in argument.split("="):
            if piece.startswith(CORPUS_TOKEN + "/"):
                relative = piece[len(CORPUS_TOKEN) + 1:]
                if relative not in paths:
                    paths.append(relative)
    return paths


def stage_referenced_files(cases: dict[str, dict], corpus: Path, output_root: Path) -> list[str]:
    """Copy every {corpus} file into the run directory with the fixed timestamp."""
    problems: list[str] = []
    staged: set[str] = set()
    for case in cases.values():
        for relative in referenced_corpus_paths(case["arguments"]):
            if relative in staged:
                continue
            source = corpus / relative
            target = output_root / ADDED_FILES_DIRECTORY / relative
            if not source.is_file() or source.is_symlink():
                problems.append(f"{case['caseId']}: {relative} is not a corpus file")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.utime(target, (STAGED_FILE_MTIME_SECONDS, STAGED_FILE_MTIME_SECONDS))
            staged.add(relative)
    return problems


def resolve_arguments(arguments: list[str], staged_root: PurePosixPath | Path) -> list[str]:
    """Replace the {corpus} token with the staged copy's location on the tested surface."""
    return [argument.replace(CORPUS_TOKEN, str(staged_root)) for argument in arguments]


def output_name(case: dict, source_local_path: str) -> str:
    return case["caseId"] + PurePosixPath(source_local_path).suffix.lower()


def restricted_docker_prefix(cidfile: Path, corpus: Path, output_root: Path, output_writable: bool) -> list[str]:
    for path in (corpus, output_root):
        if "," in str(path):
            raise ValueError("Docker bind paths cannot contain commas")
    user_arguments = (
        [f"--user={os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    )
    output_mount = f"type=bind,src={output_root},dst=/output"
    if not output_writable:
        output_mount += ",readonly"
    return [
        "docker", "run", "--rm", "--interactive", f"--cidfile={cidfile}", *user_arguments,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=128", "--cpus=2",
        "--memory=1024m", "--tmpfs=/tmp:rw,nosuid,nodev,size=256m",
        "--mount", f"type=bind,src={corpus},dst=/corpus,readonly",
        "--mount", output_mount,
    ]


def container_edit_command(
    image: str,
    corpus: Path,
    output_root: Path,
    case: dict,
    source_local_path: str,
    cidfile: Path,
) -> list[str]:
    return [
        *restricted_docker_prefix(cidfile, corpus, output_root, output_writable=True),
        image, "edit", case["operation"], "-o", f"/output/{output_name(case, source_local_path)}",
        *resolve_arguments(case["arguments"], PurePosixPath("/output") / ADDED_FILES_DIRECTORY),
        "--", f"/corpus/{source_local_path}",
    ]


def container_reopen_command(image: str, corpus: Path, output_root: Path, cidfile: Path) -> list[str]:
    return [
        *restricted_docker_prefix(cidfile, corpus, output_root, output_writable=False),
        image, "intake", "--files0-from", "-",
    ]


def launcher_edit_command(
    launcher: Path, corpus: Path, output_root: Path, case: dict, source_local_path: str
) -> list[str]:
    return [
        str(launcher), "edit", case["operation"], "-o", str(output_root / output_name(case, source_local_path)),
        *resolve_arguments(case["arguments"], output_root / ADDED_FILES_DIRECTORY),
        "--", str(corpus / source_local_path),
    ]


def validate_edit_receipt(
    receipt: object,
    case: dict,
    pin: dict[str, str],
    expected_source: Path,
    expected_output: Path,
    host_output: Path,
    expected_version: str | None,
) -> list[str]:
    problems: list[str] = []
    label = case["caseId"]
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        return [f"{label}: receipt fields differ from edit schema v1"]
    constants = {
        "schema": "com.krauq.ufo.edit-receipt",
        "schemaVersion": 1,
        "sequence": 0,
        "action": "edit",
        "operation": case["operation"],
        "status": case["status"],
        "network": "not_used",
        "limits": EXPECTED_LIMITS,
    }
    for field, expected in constants.items():
        if receipt.get(field) != expected:
            problems.append(f"{label}: expected {field}={expected!r}, got {receipt.get(field)!r}")
    if expected_version is not None and receipt.get("engineVersion") != expected_version:
        problems.append(f"{label}: unexpected engineVersion {receipt.get('engineVersion')!r}")
    if type(receipt.get("durationMillis")) is not int or receipt["durationMillis"] < 0:
        problems.append(f"{label}: invalid durationMillis")

    source = receipt.get("source")
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        problems.append(f"{label}: source fields differ from edit schema v1")
    else:
        expected = {
            "path": str(expected_source),
            "name": expected_source.name,
            "sizeBytes": int(pin["size_bytes"]),
            "sha256": pin["sha256"],
            "stableDuringProcessing": True,
        }
        for field, value in expected.items():
            if source.get(field) != value:
                problems.append(f"{label}: unexpected source.{field}={source.get(field)!r}")
        if type(source.get("lastModifiedMillis")) is not int or source["lastModifiedMillis"] < 0:
            problems.append(f"{label}: invalid source.lastModifiedMillis")

    if case["status"] == "refused":
        if receipt.get("code") != case["code"]:
            problems.append(f"{label}: expected refusal {case['code']!r}, got {receipt.get('code')!r}")
        if not isinstance(receipt.get("message"), str) or not receipt["message"].strip():
            problems.append(f"{label}: a refusal must carry a message")
        if receipt.get("output") is not None or receipt.get("result") is not None:
            problems.append(f"{label}: a refusal must not report an output or a result")
        if host_output.exists() or host_output.is_symlink():
            problems.append(f"{label}: a refused edit published a file")
        return problems

    if receipt.get("code") is not None or receipt.get("message") is not None:
        problems.append(f"{label}: a completed edit must not carry a code or message")
    expected_output_receipt = {
        "path": str(expected_output),
        "sizeBytes": case["outputSizeBytes"],
        "sha256": case["outputSha256"],
    }
    if receipt.get("output") != expected_output_receipt:
        problems.append(f"{label}: output receipt differs from reviewed expectations")
    if receipt.get("result") != case["result"]:
        problems.append(f"{label}: result differs from reviewed expectations")
    if host_output.is_symlink() or not host_output.is_file():
        problems.append(f"{label}: published output is missing or not a regular file")
    else:
        size = host_output.stat().st_size
        digest = download_corpus.sha256(host_output)
        if size != case["outputSizeBytes"] or digest != case["outputSha256"]:
            problems.append(f"{label}: output bytes differ from reviewed expectations")
    return problems


def reopen_problems(
    case_ids: list[str], receipts: list[dict], expected_version: str | None
) -> list[str]:
    """Pair intake receipts (source, output) per case and require identical reopen."""
    problems: list[str] = []
    if len(receipts) != 2 * len(case_ids):
        return [f"reopen: expected {2 * len(case_ids)} intake receipts, got {len(receipts)}"]
    for index, case_id in enumerate(case_ids):
        original, edited = receipts[2 * index], receipts[2 * index + 1]
        shape_problems: list[str] = []
        for label, receipt in (("source", original), ("output", edited)):
            if not isinstance(receipt, dict) or receipt.get("schema") != "com.krauq.ufo.intake-receipt":
                shape_problems.append(f"{case_id}: reopen {label} receipt is not an intake receipt")
                continue
            if expected_version is not None and receipt.get("engineVersion") != expected_version:
                shape_problems.append(f"{case_id}: reopen {label} engineVersion differs")
        if shape_problems:
            # Only this case's receipts are unusable; every other case still reports.
            problems.extend(shape_problems)
            continue
        if original.get("status") != "accepted":
            problems.append(f"{case_id}: the source itself does not reopen as accepted")
        if edited.get("status") != original.get("status"):
            problems.append(
                f"{case_id}: edited copy reopens as {edited.get('status')!r}, "
                f"source as {original.get('status')!r}"
            )
        detection_original = original.get("detection") or {}
        detection_edited = edited.get("detection") or {}
        for field in REOPEN_DETECTION_FIELDS:
            if detection_edited.get(field) != detection_original.get(field):
                problems.append(f"{case_id}: edited copy resolves {field} differently")
        probe_original = original.get("probe") or {}
        probe_edited = edited.get("probe") or {}
        if probe_edited.get("outcome") != probe_original.get("outcome"):
            problems.append(f"{case_id}: edited copy probes differently")
        if edited.get("warnings") != original.get("warnings"):
            problems.append(f"{case_id}: edited copy carries different intake warnings")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    surface = parser.add_mutually_exclusive_group()
    surface.add_argument("--ufo", type=Path)
    surface.add_argument("--container-image")
    parser.add_argument("--manifest", type=Path, default=download_realworld.DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=download_realworld.DEFAULT_PINS)
    parser.add_argument("--expectations", type=Path, default=DEFAULT_EXPECTATIONS)
    parser.add_argument("--corpus", type=Path, default=download_realworld.DEFAULT_OUTPUT)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--expect-version")
    parser.add_argument("--command-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    for field in ("manifest", "pins", "expectations", "corpus", "receipts", "summary"):
        setattr(args, field, getattr(args, field).resolve())
    launcher = args.ufo.resolve() if args.ufo else None
    if launcher is None and args.container_image is None:
        launcher = evaluate_cli_corpus.discover_launcher()
    if launcher is None and args.container_image is None:
        print("FAIL: no unique packaged ufo launcher found; supply --ufo", file=sys.stderr)
        return 2
    if launcher is not None and (
        not launcher.is_file() or (os.name != "nt" and not os.access(launcher, os.X_OK))
    ):
        print(f"FAIL: ufo launcher is missing or not executable: {launcher}", file=sys.stderr)
        return 2
    if args.command_timeout_seconds < 1:
        parser.error("--command-timeout-seconds must be positive")
    if args.container_image and shutil.which("docker") is None:
        print("FAIL: docker is required for --container-image", file=sys.stderr)
        return 2

    try:
        rows = download_realworld.read_manifest(args.manifest)
        pins = download_corpus.validate_manifest_pins(rows, download_corpus.read_pins(args.pins))
        cases = read_expectations(args.expectations)
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid edit corpus authority: {error}", file=sys.stderr)
        return 2
    rows_by_id = {row["id"]: row for row in rows}
    missing = sorted({case["sourceId"] for case in cases.values()} - set(rows_by_id))
    if missing:
        print(f"FAIL: edit sources are absent from real-world manifest: {missing}", file=sys.stderr)
        return 2

    if args.download:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools/corpus/download_realworld.py"),
             "--manifest", str(args.manifest), "--pins", str(args.pins),
             "--output", str(args.corpus)],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode
    corpus_problems = evaluate_desktop_corpus.local_corpus_problems(rows, pins, args.corpus)
    if corpus_problems:
        print("FAIL: edit corpus cache is not the reviewed set:", file=sys.stderr)
        for problem in corpus_problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    container_image_id: str | None = None
    if args.container_image:
        try:
            container_image_id = docker_image_id(args.container_image, cwd=REPO_ROOT)
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
    try:
        artifact = (
            container_artifact(args.container_image, container_image_id)
            if args.container_image
            else launcher_artifact(launcher)
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: could not bind the tested artifact: {error}", file=sys.stderr)
        return 2

    def new_cidfile() -> Path | None:
        if not args.container_image:
            return None
        descriptor, raw_cidfile = tempfile.mkstemp(
            prefix="ufo-edit-", suffix=".cid", dir=REPO_ROOT / "local-corpus",
        )
        os.close(descriptor)
        cidfile = Path(raw_cidfile)
        cidfile.unlink()
        return cidfile

    (REPO_ROOT / "local-corpus").mkdir(exist_ok=True)
    receipts: list[dict] = []
    problems: list[str] = []
    reopen_pairs: list[tuple[str, Path, Path]] = []
    with tempfile.TemporaryDirectory(prefix="edit-gate-", dir=REPO_ROOT / "local-corpus") as raw:
        output_root = Path(raw).resolve()
        problems.extend(stage_referenced_files(cases, args.corpus, output_root))
        for case_id, case in cases.items():
            if problems:
                break
            row = rows_by_id[case["sourceId"]]
            host_output = output_root / output_name(case, row["local_path"])
            cidfile = new_cidfile()
            if args.container_image:
                command = container_edit_command(
                    container_image_id, args.corpus, output_root, case, row["local_path"], cidfile,
                )
                expected_source = Path("/corpus") / row["local_path"]
                expected_output = Path("/output") / host_output.name
            else:
                command = launcher_edit_command(launcher, args.corpus, output_root, case, row["local_path"])
                expected_source = (args.corpus / row["local_path"]).resolve()
                expected_output = host_output
            try:
                result = run_bounded_command(
                    command,
                    cwd=REPO_ROOT,
                    input_bytes=b"",
                    timeout_seconds=args.command_timeout_seconds,
                    label=f"ufo edit corpus ({case_id})",
                    max_stdout_bytes=4 * 1024 * 1024,
                )
            except (OSError, ValueError) as error:
                problems.append(f"{case_id}: {error}")
                continue
            finally:
                evaluate_cli_corpus.remove_container_from_cidfile(cidfile)
            expected_exit = 0 if case["status"] == "completed" else 1
            if result.returncode != expected_exit:
                problems.append(f"{case_id}: expected exit {expected_exit}, got {result.returncode}")
            if result.stderr:
                problems.append(f"{case_id}: stderr was not empty: {result.stderr.strip()!r}")
            parsed_receipts, framing_problems = evaluate_cli_corpus.parse_json_lines(result.stdout)
            problems.extend(f"{case_id}: {problem}" for problem in framing_problems)
            if len(parsed_receipts) != 1:
                problems.append(f"{case_id}: expected one JSON receipt line, got {len(parsed_receipts)}")
                continue
            receipt = parsed_receipts[0]
            receipts.append(receipt)
            case_problems = validate_edit_receipt(
                receipt, case, pins[case["sourceId"]], expected_source, expected_output,
                host_output, args.expect_version,
            )
            problems.extend(case_problems)
            if case["status"] == "completed" and not case_problems:
                reopen_pairs.append((case_id, expected_source, expected_output))

        reopen_receipts: list[dict] = []
        if reopen_pairs and not problems:
            cidfile = new_cidfile()
            if args.container_image:
                command = container_reopen_command(container_image_id, args.corpus, output_root, cidfile)
            else:
                command = [str(launcher), "intake", "--files0-from", "-"]
            files0 = "".join(f"{source}\0{output}\0" for _, source, output in reopen_pairs)
            try:
                result = run_bounded_command(
                    command,
                    cwd=REPO_ROOT,
                    input_bytes=files0.encode("utf-8"),
                    timeout_seconds=args.command_timeout_seconds * len(reopen_pairs),
                    label="ufo edit corpus (reopen)",
                    max_stdout_bytes=16 * 1024 * 1024,
                )
            except (OSError, ValueError) as error:
                problems.append(f"reopen: {error}")
            else:
                if result.returncode != 0:
                    problems.append(f"reopen: expected exit 0, got {result.returncode}")
                if result.stderr:
                    problems.append(f"reopen: stderr was not empty: {result.stderr.strip()!r}")
                reopen_receipts, framing_problems = evaluate_cli_corpus.parse_json_lines(result.stdout)
                problems.extend(f"reopen: {problem}" for problem in framing_problems)
                problems.extend(
                    reopen_problems([pair[0] for pair in reopen_pairs], reopen_receipts, args.expect_version)
                )
            finally:
                evaluate_cli_corpus.remove_container_from_cidfile(cidfile)

    if problems:
        print("FAIL: packaged edit corpus mismatches:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if launcher is not None:
        try:
            if launcher_artifact(launcher) != artifact:
                print("FAIL: packaged app image changed during the edit corpus run", file=sys.stderr)
                return 1
        except (OSError, ValueError) as error:
            print(f"FAIL: could not recheck the tested artifact: {error}", file=sys.stderr)
            return 1
    receipt_text = "".join(
        json.dumps(receipt, separators=(",", ":")) + "\n"
        for receipt in [*receipts, *reopen_receipts]
    )
    evaluate_cli_corpus.atomic_write(args.receipts, receipt_text)
    completed = [case_id for case_id, case in cases.items() if case["status"] == "completed"]
    summary = {
        "schema": "com.krauq.ufo.edit-corpus-summary",
        "schemaVersion": 1,
        "caseCount": len(cases),
        "completedCount": len(completed),
        "refusedCount": len(cases) - len(completed),
        "reopenedCount": len(reopen_pairs),
        "operations": sorted({case["operation"] for case in cases.values()}),
        "engines": sorted({case["result"]["engine"] for case in cases.values() if case["status"] == "completed"}),
        "surface": "container" if args.container_image else "launcher",
        "artifact": artifact,
        "caseIds": list(cases),
        "receiptsSha256": hashlib.sha256(receipt_text.encode("utf-8")).hexdigest(),
    }
    evaluate_cli_corpus.atomic_write(args.summary, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"Packaged edit corpus: {len(cases)}/{len(cases)} cases; "
        f"{len(completed)} exact outputs reopened, {len(cases) - len(completed)} pinned refusals; "
        f"{len(summary['engines'])} engines"
    )
    print(f"Receipts: {args.receipts}")
    print(f"Summary: {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
