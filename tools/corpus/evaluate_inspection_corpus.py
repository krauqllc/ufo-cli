#!/usr/bin/env python3
"""Verify packaged ``ufo inspect --deep`` against reviewed stable expectations."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

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

DEFAULT_CORPUS = REPO_ROOT / "local-corpus" / "desktop-public"
DEFAULT_EXPECTATIONS = Path(__file__).with_name("deep_inspection_expectations.jsonl")
DEFAULT_RECEIPTS = REPO_ROOT / "local-corpus" / "reports" / "cli-inspection-receipts.jsonl"
DEFAULT_SUMMARY = REPO_ROOT / "local-corpus" / "reports" / "cli-inspection-summary.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "cli" / "schemas" / "ufo-inspection-report-v1.schema.json"
INSPECTION_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

REPORT_KEYS = {
    "schema", "schemaVersion", "engineVersion", "sequence", "action", "status",
    "source", "detection", "warnings", "metadata", "flags", "findings",
    "textSelectable", "container", "notInspected", "highestSeverity", "code",
    # Every report records the licence the run was under, free tier included.
    "message", "limits", "durationMillis", "license", "network",
}
SOURCE_KEYS = {
    "path", "name", "sizeBytes", "lastModifiedMillis", "sha256",
    "stableDuringProcessing",
}
FINDING_KEYS = {
    "category", "severity", "code", "title", "count", "detail", "where", "removable",
}
STABLE_FINDING_KEYS = {"category", "severity", "code", "count", "removable"}
CONTAINER_KEYS = {
    "format", "entryCount", "entries", "entriesTruncated", "nestedArchives", "executables",
}
CONTAINER_ENTRY_KEYS = {"path", "sizeBytes", "compressedBytes", "isDirectory", "kind", "sha256", "hashed"}
STABLE_CONTAINER_KEYS = {
    "format", "entryCount", "entriesTruncated", "nestedArchives", "executables",
}
STABLE_KEYS = {
    "status", "code", "flags", "findings", "highestSeverity", "textSelectable",
    "container", "notInspected",
}
EXPECTATION_KEYS = STABLE_KEYS | {"id"}
SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
MAX_SOURCE_BYTES = 268_435_456
MAX_METADATA_ENTRIES = 128
MAX_FINDINGS = 64
MAX_NOT_INSPECTED = 32
MAX_LISTED_ENTRIES = 2_000
MAX_CONTAINER_ENTRIES = 20_000
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


def inspection_flags() -> tuple[str, ...]:
    return tuple(INSPECTION_SCHEMA["properties"]["flags"]["items"]["enum"])


INSPECTION_FLAGS = inspection_flags()
INSPECTION_KINDS = tuple(
    INSPECTION_SCHEMA["properties"]["detection"]["oneOf"][0]
    ["properties"]["resolvedKind"]["enum"]
)


def stable_inspection(report: dict[str, Any]) -> dict[str, Any]:
    """Remove paths, prose, metadata values, timings, and large entry listings."""
    container = report["container"]
    return {
        "status": report["status"],
        "code": report["code"],
        "flags": list(report["flags"]),
        "findings": sorted(
            ({key: finding[key] for key in STABLE_FINDING_KEYS} for finding in report["findings"]),
            key=lambda finding: (
                finding["code"], finding["category"], finding["severity"],
                finding["count"], str(finding["removable"]),
            ),
        ),
        "highestSeverity": report["highestSeverity"],
        "textSelectable": report["textSelectable"],
        "container": (
            None
            if container is None
            else {key: container[key] for key in STABLE_CONTAINER_KEYS}
        ),
        "notInspected": sorted({item["what"] for item in report["notInspected"]}),
    }


def stable_problem(value: Any, label: str) -> str | None:
    if not isinstance(value, dict) or set(value) != STABLE_KEYS:
        return f"{label}: stable inspection fields differ"
    if value.get("status") not in {"completed", "refused", "failed"}:
        return f"{label}: invalid inspection status"
    if value.get("code") is not None and not isinstance(value["code"], str):
        return f"{label}: invalid inspection code"
    flags = value.get("flags")
    if not isinstance(flags, list) or any(flag not in INSPECTION_FLAGS for flag in flags):
        return f"{label}: invalid inspection flags"
    if len(set(flags)) != len(flags) or flags != sorted(flags, key=INSPECTION_FLAGS.index):
        return f"{label}: inspection flags are not unique and canonical"
    findings = value.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        return f"{label}: findings is not a list"
    finding_keys: list[tuple[Any, ...]] = []
    finding_codes: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != STABLE_FINDING_KEYS:
            return f"{label}: invalid stable finding fields"
        if (
            finding.get("category") not in {"privacy", "hidden", "security", "integrity", "info"}
            or finding.get("severity") not in {"low", "medium", "high"}
            or not isinstance(finding.get("code"), str)
            or not finding["code"]
            or not evaluate_cli_corpus.is_nonnegative_int(finding.get("count"))
            or finding["count"] < 1
            or (finding.get("removable") is not None and type(finding["removable"]) is not bool)
        ):
            return f"{label}: invalid stable finding"
        if finding["code"] in finding_codes:
            return f"{label}: duplicate stable finding code"
        finding_codes.add(finding["code"])
        finding_keys.append(
            (
                finding["code"], finding["category"], finding["severity"],
                finding["count"], str(finding["removable"]),
            )
        )
    if finding_keys != sorted(finding_keys) or len(set(finding_keys)) != len(finding_keys):
        return f"{label}: stable findings are not unique and canonical"
    highest = value.get("highestSeverity")
    if highest not in SEVERITY_RANK:
        return f"{label}: invalid highestSeverity"
    expected_highest = max(
        (finding["severity"] for finding in findings),
        key=SEVERITY_RANK.__getitem__,
        default="none",
    )
    if highest != expected_highest:
        return f"{label}: highestSeverity does not match findings"
    if value.get("textSelectable") is not None and type(value["textSelectable"]) is not bool:
        return f"{label}: invalid textSelectable"
    container = value.get("container")
    if container is not None:
        if not isinstance(container, dict) or set(container) != STABLE_CONTAINER_KEYS:
            return f"{label}: invalid stable container fields"
        if (
            not isinstance(container.get("format"), str)
            or not container["format"]
            or any(
                not evaluate_cli_corpus.is_nonnegative_int(container.get(key))
                for key in ("entryCount", "nestedArchives", "executables")
            )
            or type(container.get("entriesTruncated")) is not bool
            or container["entryCount"] > MAX_CONTAINER_ENTRIES
            or container["nestedArchives"] > container["entryCount"]
            or container["executables"] > container["entryCount"]
        ):
            return f"{label}: invalid stable container"
    not_inspected = value.get("notInspected")
    if (
        not isinstance(not_inspected, list)
        or any(not isinstance(item, str) or not item for item in not_inspected)
        or not_inspected != sorted(set(not_inspected))
    ):
        return f"{label}: invalid notInspected scopes"
    return None


def read_expectations(
    path: Path = DEFAULT_EXPECTATIONS,
    manifest_rows: list[dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    values, framing = evaluate_cli_corpus.parse_json_lines(path.read_text(encoding="utf-8"))
    if framing:
        raise ValueError(f"invalid deep-inspection expectation JSONL: {framing[0]}")
    expected: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for number, value in enumerate(values, start=1):
        if set(value) != EXPECTATION_KEYS:
            raise ValueError(f"deep-inspection expectation line {number} has unexpected fields")
        fixture_id = value["id"]
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in expected:
            raise ValueError(f"deep-inspection expectation line {number} has an invalid or duplicate id")
        stable = {key: value[key] for key in STABLE_KEYS}
        problem = stable_problem(stable, fixture_id)
        if problem:
            raise ValueError(problem)
        expected[fixture_id] = stable
        order.append(fixture_id)
    if manifest_rows is not None:
        manifest_order = [row["id"] for row in manifest_rows]
        if order != manifest_order:
            raise ValueError("deep-inspection expectations do not exactly match manifest order and ids")
    return expected


def raw_structure_problem(report: dict[str, Any], label: str) -> str | None:
    metadata = report.get("metadata")
    if not isinstance(metadata, dict) or len(metadata) > MAX_METADATA_ENTRIES or any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in metadata.items()
    ):
        return f"{label}: invalid metadata"
    findings = report.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        return f"{label}: findings is not a list"
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
            return f"{label}: finding fields differ from inspection schema v1"
        if (
            not isinstance(finding.get("title"), str)
            or (finding.get("detail") is not None and not isinstance(finding["detail"], str))
            or (finding.get("where") is not None and not isinstance(finding["where"], str))
        ):
            return f"{label}: invalid finding display fields"
    not_inspected = report.get("notInspected")
    if not isinstance(not_inspected, list) or len(not_inspected) > MAX_NOT_INSPECTED or any(
        not isinstance(item, dict)
        or set(item) != {"what", "why"}
        or not isinstance(item["what"], str)
        or not item["what"]
        or not isinstance(item["why"], str)
        or not item["why"]
        for item in not_inspected
    ):
        return f"{label}: invalid notInspected rows"
    scopes = [item["what"] for item in not_inspected]
    if len(scopes) != len(set(scopes)):
        return f"{label}: duplicate notInspected scopes"
    container = report.get("container")
    if container is not None:
        if not isinstance(container, dict) or set(container) != CONTAINER_KEYS:
            return f"{label}: container fields differ from inspection schema v1"
        entries = container.get("entries")
        if not isinstance(entries, list) or len(entries) > MAX_LISTED_ENTRIES or any(
            not isinstance(entry, dict) or set(entry) != CONTAINER_ENTRY_KEYS
            for entry in entries
        ):
            return f"{label}: invalid container entries"
        for entry in entries:
            if (
                not isinstance(entry.get("path"), str)
                or not entry["path"]
                or not evaluate_cli_corpus.is_nonnegative_int(entry.get("sizeBytes"))
                or (
                    entry.get("compressedBytes") is not None
                    and not evaluate_cli_corpus.is_nonnegative_int(entry["compressedBytes"])
                )
                or type(entry.get("isDirectory")) is not bool
                or (
                    entry.get("kind") is not None
                    and entry["kind"] not in INSPECTION_KINDS
                )
                or type(entry.get("hashed")) is not bool
                or (entry["hashed"] != (entry.get("sha256") is not None))
                or (
                    entry.get("sha256") is not None
                    and not HEX_SHA256.fullmatch(entry["sha256"])
                )
                or (entry["isDirectory"] and entry.get("sha256") is not None)
            ):
                return f"{label}: invalid container entry"
        entry_count = container.get("entryCount")
        if evaluate_cli_corpus.is_nonnegative_int(entry_count):
            if entry_count > MAX_CONTAINER_ENTRIES:
                return f"{label}: container entryCount exceeds the inspection limit"
            if len(entries) > entry_count:
                return f"{label}: container listing exceeds entryCount"
            if container.get("entriesTruncated") != (len(entries) < entry_count):
                return f"{label}: entriesTruncated differs from the listed entry count"
    try:
        stable = stable_inspection(report)
    except (KeyError, TypeError, ValueError):
        return f"{label}: inspection result cannot be stabilized"
    return stable_problem(stable, label)


def validate_reports(
    rows: list[dict[str, str]],
    pins: dict[str, dict[str, Any]],
    reports: list[dict[str, Any]],
    corpus: Path,
    worker_timeout_millis: int,
    worker_max_heap_mib: int,
    expected_version: str | None = None,
    source_root: Path | None = None,
    expectations: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    problems: list[str] = []
    expected_source_root = source_root or corpus
    if len(reports) != len(rows):
        problems.append(f"expected {len(rows)} inspection reports, got {len(reports)}")
    for sequence, row in enumerate(rows):
        label = row["id"]
        if sequence >= len(reports):
            problems.append(f"{label}: inspection report is missing")
            continue
        report = reports[sequence]
        if not isinstance(report, dict) or set(report) != REPORT_KEYS:
            problems.append(f"{label}: report fields differ from inspection schema v1")
            continue
        constants = {
            "schema": "com.krauq.ufo.inspection-report",
            "schemaVersion": 1,
            "sequence": sequence,
            "action": "inspect",
            "network": "not_used",
        }
        for field, expected in constants.items():
            if report.get(field) != expected:
                problems.append(f"{label}: expected {field}={expected!r}, got {report.get(field)!r}")
        version = report.get("engineVersion")
        if not isinstance(version, str) or not version:
            problems.append(f"{label}: engineVersion is empty")
        elif expected_version is not None and version != expected_version:
            problems.append(f"{label}: expected engineVersion={expected_version!r}, got {version!r}")
        if not evaluate_cli_corpus.is_nonnegative_int(report.get("durationMillis")):
            problems.append(f"{label}: durationMillis is invalid")
        source = report.get("source")
        expected_path = (expected_source_root / row["local_path"]).resolve()
        pin = pins[label]
        source_size = int(pin["size_bytes"])
        source_is_hashable = source_size <= MAX_SOURCE_BYTES
        if source_is_hashable != (pin["sha256"] is not None):
            problems.append(f"{label}: source size and pinned digest availability disagree")
        expected_source = {
            "path": str(expected_path),
            "name": expected_path.name,
            "sizeBytes": source_size,
            "sha256": pin["sha256"],
            "stableDuringProcessing": True if source_is_hashable else None,
        }
        if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
            problems.append(f"{label}: source fields differ from inspection schema v1")
        else:
            for field, expected in expected_source.items():
                if source.get(field) != expected:
                    problems.append(f"{label}: expected source.{field}={expected!r}, got {source.get(field)!r}")
            if not evaluate_cli_corpus.is_nonnegative_int(source.get("lastModifiedMillis")):
                problems.append(f"{label}: source.lastModifiedMillis is invalid")
        detection = report.get("detection")
        expected_kind = evaluate_cli_corpus.KIND_WIRE_NAMES.get(row.get("expected_kind", ""))
        if detection is None and expected_kind is None:
            pass
        elif not isinstance(detection, dict) or set(detection) != evaluate_cli_corpus.DETECTION_KEYS:
            problems.append(f"{label}: detection fields differ from inspection schema v1")
        else:
            if (
                detection.get("resolvedKind") not in INSPECTION_KINDS
                or detection.get("nameAndBytes") not in {"match", "mismatch", "unknown"}
                or any(
                    value is not None and not isinstance(value, str)
                    for value in (
                        detection.get("resolvedFormat"),
                        detection.get("claimedExtension"),
                        detection.get("claimedFormat"),
                        detection.get("contentExtension"),
                        detection.get("contentFormat"),
                    )
                )
            ):
                problems.append(f"{label}: detection values differ from inspection schema v1")
            if expected_kind is not None and detection.get("resolvedKind") != expected_kind:
                problems.append(
                    f"{label}: expected detection.resolvedKind={expected_kind!r}, "
                    f"got {detection.get('resolvedKind')!r}"
                )
        warning_problem = evaluate_cli_corpus.warnings_problem(report.get("warnings"))
        if warning_problem:
            problems.append(f"{label}: {warning_problem}")
        elif "expected_warnings" in row and report["warnings"] != evaluate_cli_corpus.expected_warnings_for(row):
            problems.append(f"{label}: deep-inspection warnings differ from intake authority")
        elif report["warnings"] and detection is None:
            problems.append(f"{label}: warnings claimed without detected bytes")
        expected_limits = {
            "maxSourceBytes": MAX_SOURCE_BYTES,
            "workerTimeoutMillis": worker_timeout_millis,
            "workerMaxHeapMiB": worker_max_heap_mib,
        }
        if report.get("limits") != expected_limits:
            problems.append(f"{label}: expected limits={expected_limits!r}, got {report.get('limits')!r}")
        structure_problem = raw_structure_problem(report, label)
        if structure_problem:
            problems.append(structure_problem)
            continue
        stable = stable_inspection(report)
        if stable["status"] == "completed":
            if report.get("code") is not None or report.get("message") is not None:
                problems.append(f"{label}: completed inspection has a code or message")
        else:
            if (
                not isinstance(report.get("code"), str)
                or not report["code"]
                or not isinstance(report.get("message"), str)
                or not report["message"]
            ):
                problems.append(f"{label}: incomplete inspection omits its code or message")
            empty_result = {
                "metadata": {},
                "flags": [],
                "findings": [],
                "textSelectable": None,
                "container": None,
                "notInspected": [],
                "highestSeverity": "none",
            }
            if any(report.get(field) != expected for field, expected in empty_result.items()):
                problems.append(f"{label}: incomplete inspection carries untrusted result data")
        if not source_is_hashable and not (
            report.get("status") == "refused"
            and report.get("code") == "source_too_large"
            and report.get("detection") is None
            and report.get("warnings") == []
        ):
            problems.append(f"{label}: oversized source did not produce source_too_large refusal")
        if expectations is not None and stable != expectations[label]:
            problems.append(
                f"{label}: stable inspection differs from reviewed expectation: "
                f"expected {expectations[label]!r}, got {stable!r}"
            )
    for sequence in range(len(rows), len(reports)):
        problems.append(f"unexpected inspection report at sequence {sequence}")
    return problems


def _container_image_id(image: str) -> str:
    return docker_image_id(image, cwd=REPO_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    surface = parser.add_mutually_exclusive_group()
    surface.add_argument("--ufo", type=Path, help="packaged ufo launcher; auto-detected when unique")
    surface.add_argument("--container-image", help="test this local Linux container image")
    parser.add_argument("--manifest", type=Path, default=download_realworld.DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=download_realworld.DEFAULT_PINS)
    parser.add_argument("--expectations", type=Path, default=DEFAULT_EXPECTATIONS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--download", action="store_true")
    # One job is what an unlicensed build runs; a licensed machine can raise it.
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--worker-timeout-ms", type=int, default=30_000)
    parser.add_argument("--worker-max-heap-mib", type=int, default=512)
    parser.add_argument("--command-timeout-seconds", type=int, default=1_200)
    parser.add_argument("--expect-version")
    args = parser.parse_args()
    for name in ("manifest", "pins", "expectations", "corpus", "receipts", "summary"):
        setattr(args, name, getattr(args, name).resolve())
    launcher = args.ufo.resolve() if args.ufo else None
    if launcher is None and args.container_image is None:
        launcher = evaluate_cli_corpus.discover_launcher()
    if args.jobs not in range(1, 65):
        parser.error("--jobs must be between 1 and 64")
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
    if args.container_image is not None and shutil.which("docker") is None:
        print("FAIL: docker is required for --container-image", file=sys.stderr)
        return 2

    try:
        rows = download_realworld.read_manifest(args.manifest)
        pins = download_corpus.validate_manifest_pins(rows, download_corpus.read_pins(args.pins))
        expectations = read_expectations(args.expectations, rows)
        image_id = _container_image_id(args.container_image) if args.container_image else None
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid inspection corpus authority: {error}", file=sys.stderr)
        return 2

    if args.download:
        downloaded = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "corpus" / "download_realworld.py"),
                "--manifest", str(args.manifest), "--pins", str(args.pins),
                "--output", str(args.corpus),
            ],
            cwd=REPO_ROOT,
            check=False,
        )
        if downloaded.returncode != 0:
            return downloaded.returncode
    corpus_problems = evaluate_desktop_corpus.local_corpus_problems(rows, pins, args.corpus)
    if corpus_problems:
        print("FAIL: inspection corpus cache is not the reviewed set:", file=sys.stderr)
        for problem in corpus_problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    try:
        artifact = (
            container_artifact(args.container_image, image_id)
            if args.container_image
            else launcher_artifact(launcher)
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: could not bind the tested artifact: {error}", file=sys.stderr)
        return 2

    cidfile: Path | None = None
    if args.container_image:
        descriptor, raw_cidfile = tempfile.mkstemp(
            prefix="ufo-inspection-corpus-", suffix=".cid", dir=REPO_ROOT / "local-corpus"
        )
        os.close(descriptor)
        cidfile = Path(raw_cidfile)
        cidfile.unlink()
        source_root = Path("/corpus")
        command = evaluate_cli_corpus.restricted_container_command(
            image=image_id,
            corpus=args.corpus,
            worker_max_heap_mib=args.worker_max_heap_mib,
            cidfile=cidfile,
            arguments=[
                "inspect", "--deep", "--timeout-ms", str(args.worker_timeout_ms),
                "--max-heap-mib", str(args.worker_max_heap_mib), "--jobs", str(args.jobs),
                "--files0-from", "-",
            ],
            container_user=f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else None,
            worker_jobs=args.jobs,
        )
        surface_name = "container"
    else:
        source_root = args.corpus
        command = [
            str(launcher), "inspect", "--deep", "--timeout-ms", str(args.worker_timeout_ms),
            "--max-heap-mib", str(args.worker_max_heap_mib), "--jobs", str(args.jobs),
            "--files0-from", "-",
        ]
        surface_name = "launcher"
    requested = [source_root / row["local_path"] for row in rows]
    files0 = "\0".join(str(path) for path in requested) + "\0"
    try:
        result = run_bounded_command(
            command,
            cwd=REPO_ROOT,
            input_bytes=files0.encode("utf-8"),
            timeout_seconds=args.command_timeout_seconds,
            label="ufo inspection corpus",
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        evaluate_cli_corpus.remove_container_from_cidfile(cidfile)

    reports, problems = evaluate_cli_corpus.parse_json_lines(result.stdout)
    problems.extend(
        validate_reports(
            rows, pins, reports, args.corpus, args.worker_timeout_ms,
            args.worker_max_heap_mib, args.expect_version, source_root, expectations,
        )
    )
    expected_exit = 0 if all(value["status"] == "completed" for value in expectations.values()) else 1
    if result.returncode != expected_exit:
        problems.append(f"expected process exit {expected_exit}, got {result.returncode}")
    if result.stderr:
        problems.append(f"stderr was not empty: {result.stderr.strip()!r}")
    if problems:
        print("FAIL: packaged deep-inspection corpus mismatches:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if launcher is not None:
        try:
            if launcher_artifact(launcher) != artifact:
                print(
                    "FAIL: packaged app image changed during the inspection corpus run",
                    file=sys.stderr,
                )
                return 1
        except (OSError, ValueError) as error:
            print(f"FAIL: could not recheck the tested artifact: {error}", file=sys.stderr)
            return 1

    receipt_text = result.stdout if result.stdout.endswith("\n") else result.stdout + "\n"
    evaluate_cli_corpus.atomic_write(args.receipts, receipt_text)
    stable = [stable_inspection(report) for report in reports]
    status_counts = Counter(row["status"] for row in stable)
    flag_counts = Counter(flag for row in stable for flag in row["flags"])
    finding_counts = Counter(finding["code"] for row in stable for finding in row["findings"])
    severity_counts = Counter(row["highestSeverity"] for row in stable)
    summary = {
        "schema": "com.krauq.ufo.inspection-corpus-summary",
        "schemaVersion": 1,
        "fixtureCount": len(rows),
        "surface": surface_name,
        "artifact": artifact,
        "statusCounts": dict(sorted(status_counts.items())),
        "flagCounts": dict(sorted(flag_counts.items())),
        "findingCounts": dict(sorted(finding_counts.items())),
        "highestSeverityCounts": dict(sorted(severity_counts.items())),
        "engineVersions": sorted({report["engineVersion"] for report in reports}),
        "jobs": args.jobs,
        "expectationsSha256": hashlib.sha256(args.expectations.read_bytes()).hexdigest(),
        "receiptsSha256": hashlib.sha256(receipt_text.encode("utf-8")).hexdigest(),
    }
    evaluate_cli_corpus.atomic_write(args.summary, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"Packaged deep inspection: {len(rows)}/{len(rows)} aligned reports; "
        f"flags={sum(flag_counts.values())}, findings={sum(finding_counts.values())}"
    )
    print(f"Receipts: {args.receipts}")
    print(f"Summary: {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
