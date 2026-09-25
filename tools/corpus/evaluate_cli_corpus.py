#!/usr/bin/env python3
"""Verify the packaged UFO intake contract over the pinned desktop corpus."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

try:
    import download_corpus
    import download_realworld
    import evaluate_desktop_corpus
except ModuleNotFoundError:  # Loaded as tools.corpus.evaluate_cli_corpus in tests.
    from tools.corpus import download_corpus, download_realworld, evaluate_desktop_corpus


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.cli.artifact_identity import container_artifact, launcher_artifact
from tools.cli.bounded_process import docker_image_id, run_bounded_command

DEFAULT_CORPUS = download_realworld.DEFAULT_OUTPUT
DEFAULT_RECEIPTS = REPO_ROOT / "local-corpus" / "reports" / "cli-intake-receipts.jsonl"
DEFAULT_SUMMARY = REPO_ROOT / "local-corpus" / "reports" / "cli-intake-summary.json"
RECEIPT_KEYS = {
    "schema",
    "schemaVersion",
    "engineVersion",
    "sequence",
    "action",
    "status",
    "source",
    "detection",
    "probe",
    "warnings",
    "limits",
    "durationMillis",
    # Every receipt records the licence the run was under, free tier included.
    "license",
    "network",
}
WARNING_CODES = ("name_content_mismatch", "executable_content", "truncated_or_damaged")
SOURCE_KEYS = {
    "path",
    "name",
    "sizeBytes",
    "lastModifiedMillis",
    "sha256",
    "stableDuringProcessing",
}
DETECTION_KEYS = {
    "resolvedKind",
    "resolvedFormat",
    "claimedExtension",
    "claimedFormat",
    "contentExtension",
    "contentFormat",
    "nameAndBytes",
}
PROBE_KEYS = {"outcome", "level", "durationMillis", "code", "message"}
LIMIT_KEYS = {"maxSourceBytes", "workerTimeoutMillis", "workerMaxHeapMiB"}
KIND_WIRE_NAMES = {
    "Pdf": "pdf",
    "Image": "image",
    "Audio": "audio",
    "Video": "video",
    "Archive": "archive",
    "Web": "web",
    "OfficeText": "office",
    "Markdown": "markdown",
    "Sqlite": "sqlite",
    "Font": "font",
    "Certificate": "certificate",
    "Calendar": "calendar",
    "Contact": "contact",
    "Email": "email",
    "Spreadsheet": "spreadsheet",
    "Text": "text",
    "Hex": "binary",
}
PROBE_WIRE_NAMES = {
    "WorkerParsed": "worker_parsed",
    "Parsed": "in_process_parsed",
    "DetectedOnly": "detected_only",
}


def expected_result(row: dict[str, str]) -> tuple[str, str]:
    if row["expected_outcome"] == "refused":
        return "refused", "refused"
    if row["expected_probe"] == "DetectedOnly":
        return "limited", "detected_only"
    return "accepted", "parsed"


def is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def warnings_problem(warnings: object) -> str | None:
    """Return why ``warnings`` is not a canonical intake v1 warning list, or None."""
    if not isinstance(warnings, list):
        return "warnings is not a list"
    if any(code not in WARNING_CODES for code in warnings):
        return f"warnings contains an unknown code: {warnings!r}"
    if len(set(warnings)) != len(warnings):
        return f"warnings repeats a code: {warnings!r}"
    if warnings != sorted(warnings, key=WARNING_CODES.index):
        return f"warnings are not in canonical order: {warnings!r}"
    return None


def expected_warnings_for(row: dict[str, str]) -> list[str]:
    """Warnings the corpus manifest declares for a fixture; absent column means none."""
    declared = [code for code in (row.get("expected_warnings") or "").split(",") if code]
    unknown = [code for code in declared if code not in WARNING_CODES]
    if unknown:
        raise ValueError(f"{row['id']}: manifest declares unknown warning codes {unknown}")
    return sorted(set(declared), key=WARNING_CODES.index)


def validate_receipts(
    rows: list[dict[str, str]],
    pins: dict[str, dict[str, str]],
    receipts: list[dict],
    corpus: Path,
    worker_timeout_millis: int,
    worker_max_heap_mib: int,
    expected_version: str | None = None,
    source_root: Path | None = None,
) -> list[str]:
    problems: list[str] = []
    expected_source_root = source_root or corpus
    if len(receipts) != len(rows):
        problems.append(f"expected {len(rows)} receipts, got {len(receipts)}")

    for index, row in enumerate(rows):
        if index >= len(receipts):
            problems.append(f"{row['id']}: receipt is missing")
            continue
        receipt = receipts[index]
        label = row["id"]
        if not isinstance(receipt, dict):
            problems.append(f"{label}: receipt is not an object")
            continue
        if set(receipt) != RECEIPT_KEYS:
            problems.append(f"{label}: receipt fields differ from schema v1")
            continue

        constants = {
            "schema": "com.krauq.ufo.intake-receipt",
            "schemaVersion": 1,
            "sequence": index,
            "action": "intake",
            "network": "not_used",
        }
        for field, expected in constants.items():
            if receipt.get(field) != expected:
                problems.append(
                    f"{label}: expected {field}={expected!r}, got {receipt.get(field)!r}"
                )
        version = receipt.get("engineVersion")
        if not isinstance(version, str) or not version:
            problems.append(f"{label}: engineVersion is empty or not a string")
        elif expected_version is not None and version != expected_version:
            problems.append(
                f"{label}: expected engineVersion={expected_version!r}, got {version!r}"
            )
        if not is_nonnegative_int(receipt.get("durationMillis")):
            problems.append(f"{label}: durationMillis is not a non-negative integer")

        expected_status, expected_outcome = expected_result(row)
        if receipt.get("status") != expected_status:
            problems.append(
                f"{label}: expected status={expected_status}, got {receipt.get('status')!r}"
            )

        source = receipt.get("source")
        if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
            problems.append(f"{label}: source fields differ from schema v1")
        else:
            expected_path = (expected_source_root / row["local_path"]).resolve()
            pin = pins[row["id"]]
            source_checks = {
                "path": str(expected_path),
                "name": expected_path.name,
                "sizeBytes": int(pin["size_bytes"]),
                "sha256": pin["sha256"],
                "stableDuringProcessing": True,
            }
            for field, expected in source_checks.items():
                if source.get(field) != expected:
                    problems.append(
                        f"{label}: expected source.{field}={expected!r}, got {source.get(field)!r}"
                    )
            if not is_nonnegative_int(source.get("lastModifiedMillis")):
                problems.append(f"{label}: source.lastModifiedMillis is invalid")

        detection = receipt.get("detection")
        if not isinstance(detection, dict) or set(detection) != DETECTION_KEYS:
            problems.append(f"{label}: detection fields differ from schema v1")
        else:
            expected_kind = KIND_WIRE_NAMES.get(row["expected_kind"])
            if expected_kind is None:
                problems.append(f"{label}: corpus kind has no CLI wire mapping")
            elif detection.get("resolvedKind") != expected_kind:
                problems.append(
                    f"{label}: expected detection.resolvedKind={expected_kind}, "
                    f"got {detection.get('resolvedKind')!r}"
                )
            if detection.get("nameAndBytes") not in {"match", "mismatch", "unknown"}:
                problems.append(f"{label}: detection.nameAndBytes is invalid")
            for field in DETECTION_KEYS - {"resolvedKind", "nameAndBytes"}:
                if detection.get(field) is not None and not isinstance(detection.get(field), str):
                    problems.append(f"{label}: detection.{field} is not a string or null")

        probe = receipt.get("probe")
        if not isinstance(probe, dict) or set(probe) != PROBE_KEYS:
            problems.append(f"{label}: probe fields differ from schema v1")
        else:
            expected_level = PROBE_WIRE_NAMES[row["expected_probe"]]
            if probe.get("level") != expected_level:
                problems.append(
                    f"{label}: expected probe.level={expected_level}, got {probe.get('level')!r}"
                )
            if probe.get("outcome") != expected_outcome:
                problems.append(
                    f"{label}: expected probe.outcome={expected_outcome}, "
                    f"got {probe.get('outcome')!r}"
                )
            if not is_nonnegative_int(probe.get("durationMillis")):
                problems.append(f"{label}: probe.durationMillis is invalid")
            if expected_status == "accepted":
                if probe.get("code") is not None or probe.get("message") is not None:
                    problems.append(f"{label}: accepted receipt has a failure code or message")
            else:
                if not isinstance(probe.get("code"), str) or not probe["code"]:
                    problems.append(f"{label}: non-accepted receipt omits its code")
                if not isinstance(probe.get("message"), str) or not probe["message"]:
                    problems.append(f"{label}: non-accepted receipt omits its message")

        limits = receipt.get("limits")
        expected_limits = {
            "maxSourceBytes": 268_435_456,
            "workerTimeoutMillis": worker_timeout_millis,
            "workerMaxHeapMiB": worker_max_heap_mib,
        }
        if not isinstance(limits, dict) or set(limits) != LIMIT_KEYS:
            problems.append(f"{label}: limit fields differ from schema v1")
        elif limits != expected_limits:
            problems.append(f"{label}: expected limits={expected_limits!r}, got {limits!r}")

        warnings = receipt.get("warnings")
        warning_problem = warnings_problem(warnings)
        if warning_problem:
            problems.append(f"{label}: {warning_problem}")
        else:
            expected_warnings = expected_warnings_for(row)
            if list(warnings) != expected_warnings:
                problems.append(
                    f"{label}: expected warnings={expected_warnings!r}, got {warnings!r}"
                )

    for index in range(len(rows), len(receipts)):
        problems.append(f"unexpected receipt at index {index}")
    return problems


def parse_json_lines(output: str) -> tuple[list[dict], list[str]]:
    receipts: list[dict] = []
    problems: list[str] = []
    # JSON Lines is delimited by LF, not by every character Python considers a
    # Unicode line boundary. U+2028/U+2029 are valid inside a JSON string and
    # occur in real PDF metadata; str.splitlines() corrupts those records.
    lines = output.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.removesuffix("\r")
        if not line.strip():
            problems.append(f"stdout line {line_number} is blank")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            problems.append(f"stdout line {line_number} is not JSON: {error}")
            continue
        if not isinstance(value, dict):
            problems.append(f"stdout line {line_number} is not a JSON object")
            continue
        receipts.append(value)
    return receipts, problems


def discover_launcher() -> Path | None:
    app_root = REPO_ROOT / "desktop-app" / "build" / "compose" / "binaries" / "main" / "app"
    names = {"ufo", "ufo.exe"}
    candidates = sorted(
        path
        for path in app_root.glob("*/bin/ufo*")
        if path.name.lower() in names and path.is_file()
    )
    return candidates[0] if len(candidates) == 1 else None


def container_command(
    image: str,
    corpus: Path,
    worker_timeout_millis: int,
    worker_max_heap_mib: int,
    cidfile: Path,
    container_user: str | None = None,
) -> list[str]:
    return restricted_container_command(
        image=image,
        corpus=corpus,
        worker_max_heap_mib=worker_max_heap_mib,
        cidfile=cidfile,
        arguments=[
            "intake",
            "--timeout-ms",
            str(worker_timeout_millis),
            "--max-heap-mib",
            str(worker_max_heap_mib),
            "--files0-from",
            "-",
        ],
        container_user=container_user,
    )


def restricted_container_command(
    image: str,
    corpus: Path,
    worker_max_heap_mib: int,
    cidfile: Path,
    arguments: list[str],
    container_user: str | None = None,
    worker_jobs: int = 1,
) -> list[str]:
    """Run one batch CLI command inside the documented restricted boundary."""
    if "," in str(corpus):
        raise ValueError("the corpus path cannot contain a comma for a Docker bind mount")
    memory_mib = max(1_024, worker_max_heap_mib * worker_jobs + 512)
    user_arguments = [f"--user={container_user}"] if container_user else []
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        f"--cidfile={cidfile}",
        *user_arguments,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--cpus=2",
        f"--memory={memory_mib}m",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=256m",
        "--mount",
        f"type=bind,src={corpus},dst=/corpus,readonly",
        image,
        *arguments,
    ]


def remove_container_from_cidfile(cidfile: Path | None) -> None:
    if cidfile is None:
        return
    try:
        container_id = cidfile.read_text(encoding="utf-8").strip()
        if container_id:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError:
        pass
    finally:
        cidfile.unlink(missing_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    surface = parser.add_mutually_exclusive_group()
    surface.add_argument("--ufo", type=Path, help="packaged ufo launcher; auto-detected when unique")
    surface.add_argument("--container-image", help="test this local Linux container image")
    parser.add_argument("--manifest", type=Path, default=download_realworld.DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=download_realworld.DEFAULT_PINS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--worker-timeout-ms", type=int, default=30_000)
    parser.add_argument("--worker-max-heap-mib", type=int, default=512)
    parser.add_argument("--command-timeout-seconds", type=int, default=600)
    parser.add_argument("--expect-version")
    args = parser.parse_args()
    args.manifest = args.manifest.resolve()
    args.pins = args.pins.resolve()
    args.corpus = args.corpus.resolve()
    args.receipts = args.receipts.resolve()
    args.summary = args.summary.resolve()
    launcher = args.ufo.resolve() if args.ufo else None
    if launcher is None and args.container_image is None:
        launcher = discover_launcher()

    if not 1 <= args.worker_timeout_ms <= 300_000:
        parser.error("--worker-timeout-ms must be between 1 and 300000")
    if not 32 <= args.worker_max_heap_mib <= 2048:
        parser.error("--worker-max-heap-mib must be between 32 and 2048")
    if args.command_timeout_seconds < 1:
        parser.error("--command-timeout-seconds must be positive")
    if launcher is None and args.container_image is None:
        print("FAIL: no unique packaged ufo launcher found; supply --ufo", file=sys.stderr)
        return 2
    if launcher is not None and (
        not launcher.is_file() or (os.name != "nt" and not os.access(launcher, os.X_OK))
    ):
        print(f"FAIL: ufo launcher is missing or not executable: {launcher}", file=sys.stderr)
        return 2
    if args.container_image is not None:
        if shutil.which("docker") is None:
            print("FAIL: docker is required for --container-image", file=sys.stderr)
            return 2
        try:
            container_image_id = docker_image_id(args.container_image, cwd=REPO_ROOT)
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
    else:
        container_image_id = None

    try:
        rows = download_realworld.read_manifest(args.manifest)
        pins = download_corpus.validate_manifest_pins(
            rows,
            download_corpus.read_pins(args.pins),
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid desktop corpus authority: {error}", file=sys.stderr)
        return 2

    if args.download:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "corpus" / "download_realworld.py"),
                "--manifest",
                str(args.manifest),
                "--pins",
                str(args.pins),
                "--output",
                str(args.corpus),
            ],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            return result.returncode

    problems = evaluate_desktop_corpus.local_corpus_problems(rows, pins, args.corpus)
    if problems:
        print("FAIL: CLI corpus cache is not the reviewed set:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    try:
        artifact = (
            container_artifact(args.container_image, container_image_id)
            if args.container_image is not None
            else launcher_artifact(launcher)
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: could not bind the tested artifact: {error}", file=sys.stderr)
        return 2

    cidfile: Path | None = None
    if args.container_image is not None:
        descriptor, raw_cidfile = tempfile.mkstemp(
            prefix="ufo-cli-corpus-",
            suffix=".cid",
            dir=REPO_ROOT / "local-corpus",
        )
        os.close(descriptor)
        cidfile = Path(raw_cidfile)
        cidfile.unlink()
        try:
            command = container_command(
                container_image_id,
                args.corpus,
                args.worker_timeout_ms,
                args.worker_max_heap_mib,
                cidfile,
                f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else None,
            )
        except ValueError as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
        source_root = Path("/corpus")
        requested_paths = [source_root / row["local_path"] for row in rows]
        surface_name = "container"
    else:
        command = [
            str(launcher),
            "intake",
            "--timeout-ms",
            str(args.worker_timeout_ms),
            "--max-heap-mib",
            str(args.worker_max_heap_mib),
            "--files0-from",
            "-",
        ]
        source_root = args.corpus
        requested_paths = [source_root / row["local_path"] for row in rows]
        surface_name = "launcher"
    files0_input = "\0".join(str(path) for path in requested_paths) + "\0"
    try:
        result = run_bounded_command(
            command,
            cwd=REPO_ROOT,
            input_bytes=files0_input.encode("utf-8"),
            timeout_seconds=args.command_timeout_seconds,
            label="ufo intake corpus",
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        remove_container_from_cidfile(cidfile)

    receipts, problems = parse_json_lines(result.stdout)
    problems.extend(
        validate_receipts(
            rows,
            pins,
            receipts,
            args.corpus,
            args.worker_timeout_ms,
            args.worker_max_heap_mib,
            args.expect_version,
            source_root,
        )
    )
    expected_exit = 0 if all(expected_result(row)[0] == "accepted" for row in rows) else 1
    if result.returncode != expected_exit:
        problems.append(f"expected process exit {expected_exit}, got {result.returncode}")
    if result.stderr:
        problems.append(f"stderr was not empty: {result.stderr.strip()!r}")
    if problems:
        print("FAIL: packaged CLI corpus contract mismatches:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if launcher is not None:
        try:
            if launcher_artifact(launcher) != artifact:
                print("FAIL: packaged app image changed during the CLI corpus run", file=sys.stderr)
                return 1
        except (OSError, ValueError) as error:
            print(f"FAIL: could not recheck the tested artifact: {error}", file=sys.stderr)
            return 1

    receipt_text = result.stdout if result.stdout.endswith("\n") else result.stdout + "\n"
    atomic_write(args.receipts, receipt_text)
    counts = Counter(receipt["status"] for receipt in receipts)
    warning_counts = Counter(code for receipt in receipts for code in receipt["warnings"])
    versions = sorted({receipt["engineVersion"] for receipt in receipts})
    summary = {
        "schema": "com.krauq.ufo.cli-corpus-summary",
        "schemaVersion": 1,
        "fixtureCount": len(rows),
        "surface": surface_name,
        "artifact": artifact,
        "statusCounts": dict(sorted(counts.items())),
        "warningCounts": dict(sorted(warning_counts.items())),
        "engineVersions": versions,
        "receiptsSha256": hashlib.sha256(receipt_text.encode("utf-8")).hexdigest(),
    }
    atomic_write(args.summary, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"Packaged CLI corpus: {len(rows)}/{len(rows)} aligned receipts; "
        + ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    )
    print(f"Receipts: {args.receipts}")
    print(f"Summary: {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
