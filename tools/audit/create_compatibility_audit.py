#!/usr/bin/env python3
"""Create a transactionally published UFO compatibility-audit evidence bundle."""

from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.corpus import evaluate_cli_corpus, evaluate_inspection_corpus
from tools.cli.artifact_identity import container_artifact, launcher_artifact
from tools.cli.bounded_process import docker_image_id, run_bounded_command


MAX_SOURCE_BYTES = 268_435_456
MAX_FILES = 20_000
MAX_FILES0_BYTES = 16 * 1024 * 1024
STATUSES = ("accepted", "limited", "refused", "failed")
INSPECTION_STATUSES = ("completed", "refused", "failed")
SEVERITIES = ("none", "low", "medium", "high")
AUDIT_SCHEMA_VERSION = 2
PRIVACY = (
    "Reports contain file names, parser outcomes, metadata, findings, and archive details; "
    "raw reports also contain processed paths and may repeat source content fragments."
)
RENAME_NOREPLACE = 1
RENAME_EXCL = 0x00000004


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stat_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        stat.S_IFMT(details.st_mode),
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def sha256_file(
    path: Path,
    maximum: int = MAX_SOURCE_BYTES,
    expected: os.stat_result | None = None,
) -> str:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
            expected is not None and stat_identity(opened) != stat_identity(expected)
        ):
            raise ValueError(f"source identity changed before it was read: {path}")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"source grew beyond the {maximum}-byte audit limit: {path}")
            digest.update(chunk)
        if stat_identity(os.fstat(source.fileno())) != stat_identity(opened):
            raise ValueError(f"source changed while it was read: {path}")
    return digest.hexdigest()


def discover_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"audit input is not a non-symlink directory: {root}")
    files: list[Path] = []
    def walk_error(error: OSError) -> None:
        raise ValueError(f"audit input could not be enumerated: {error}") from error

    for current, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                raise ValueError(f"audit input contains a symlinked directory: {candidate.relative_to(root)}")
        for name in names:
            candidate = current_path / name
            details = candidate.lstat()
            relative = candidate.relative_to(root)
            if stat.S_ISLNK(details.st_mode):
                raise ValueError(f"audit input contains a symlinked file: {relative}")
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"audit input contains a non-regular file: {relative}")
            if any(character in relative.as_posix() for character in ("\x00",)):
                raise ValueError(f"audit input has an unsupported path: {relative!r}")
            files.append(candidate)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    if not files:
        raise ValueError("audit input directory contains no regular files")
    if len(files) > MAX_FILES:
        raise ValueError(f"audit input exceeds the {MAX_FILES}-file batch limit")
    return files


def capture_identities(root: Path, files: list[Path]) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for path in files:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"audit input changed to a non-regular file: {path.relative_to(root)}")
        size = details.st_size
        digest = sha256_file(path, expected=details) if size <= MAX_SOURCE_BYTES else None
        after = path.lstat()
        if stat_identity(details) != stat_identity(after):
            raise ValueError(f"source changed while its audit identity was read: {path}")
        identities.append(
            {
                "path": path,
                "relativePath": path.relative_to(root).as_posix(),
                "name": path.name,
                "sizeBytes": size,
                "lastModifiedNanos": details.st_mtime_ns,
                "changeNanos": details.st_ctime_ns,
                "device": details.st_dev,
                "inode": details.st_ino,
                "sha256": digest,
            }
        )
    return identities


def receipt_problem(
    receipt: Any,
    sequence: int,
    identity: dict[str, Any],
    expected_path: str,
    timeout_millis: int,
    heap_mib: int,
    expected_version: str | None,
) -> str | None:
    label = identity["relativePath"]
    if not isinstance(receipt, dict) or set(receipt) != evaluate_cli_corpus.RECEIPT_KEYS:
        return f"{label}: receipt fields differ from intake schema v1"
    constants = {
        "schema": "com.krauq.ufo.intake-receipt",
        "schemaVersion": 1,
        "sequence": sequence,
        "action": "intake",
        "network": "not_used",
    }
    for key, value in constants.items():
        if receipt.get(key) != value:
            return f"{label}: expected {key}={value!r}, got {receipt.get(key)!r}"
    version = receipt.get("engineVersion")
    if not isinstance(version, str) or not version:
        return f"{label}: engineVersion is empty"
    if expected_version is not None and version != expected_version:
        return f"{label}: expected engineVersion={expected_version!r}, got {version!r}"
    if receipt.get("status") not in STATUSES:
        return f"{label}: invalid status {receipt.get('status')!r}"
    if not evaluate_cli_corpus.is_nonnegative_int(receipt.get("durationMillis")):
        return f"{label}: invalid durationMillis"

    source = receipt.get("source")
    if not isinstance(source, dict) or set(source) != evaluate_cli_corpus.SOURCE_KEYS:
        return f"{label}: source fields differ from intake schema v1"
    source_constants = {
        "path": expected_path,
        "name": identity["name"],
        "sizeBytes": identity["sizeBytes"],
        "sha256": identity["sha256"],
        "stableDuringProcessing": True if identity["sha256"] else None,
    }
    for key, value in source_constants.items():
        if source.get(key) != value:
            return f"{label}: expected source.{key}={value!r}, got {source.get(key)!r}"
    if not evaluate_cli_corpus.is_nonnegative_int(source.get("lastModifiedMillis")):
        return f"{label}: invalid source.lastModifiedMillis"

    limits = receipt.get("limits")
    expected_limits = {
        "maxSourceBytes": MAX_SOURCE_BYTES,
        "workerTimeoutMillis": timeout_millis,
        "workerMaxHeapMiB": heap_mib,
    }
    if not isinstance(limits, dict) or set(limits) != evaluate_cli_corpus.LIMIT_KEYS:
        return f"{label}: limits differ from intake schema v1"
    if limits != expected_limits:
        return f"{label}: expected limits={expected_limits!r}, got {limits!r}"

    probe = receipt.get("probe")
    if not isinstance(probe, dict) or set(probe) != evaluate_cli_corpus.PROBE_KEYS:
        return f"{label}: probe fields differ from intake schema v1"
    if not evaluate_cli_corpus.is_nonnegative_int(probe.get("durationMillis")):
        return f"{label}: invalid probe.durationMillis"
    detection = receipt.get("detection")
    if detection is not None and (
        not isinstance(detection, dict) or set(detection) != evaluate_cli_corpus.DETECTION_KEYS
    ):
        return f"{label}: detection fields differ from intake schema v1"

    status = receipt["status"]
    outcome = probe.get("outcome")
    level = probe.get("level")
    code = probe.get("code")
    message = probe.get("message")
    if status == "accepted":
        valid = outcome == "parsed" and level in {"worker_parsed", "in_process_parsed"} and code is None and message is None
    elif status == "limited":
        valid = outcome == "detected_only" and level == "detected_only" and isinstance(code, str) and bool(code) and isinstance(message, str) and bool(message)
    elif status == "refused":
        valid = outcome in {"refused", "not_run"} and isinstance(code, str) and bool(code) and isinstance(message, str) and bool(message)
    else:
        valid = outcome in {"worker_crashed", "worker_timed_out", "probe_failed", "source_changed", "not_run"} and isinstance(code, str) and bool(code) and isinstance(message, str) and bool(message)
    if not valid:
        return f"{label}: status/probe semantics differ from intake schema v1"
    if identity["sizeBytes"] > MAX_SOURCE_BYTES and not (
        status == "refused" and detection is None and outcome == "not_run" and code == "source_too_large"
    ):
        return f"{label}: oversized source did not produce source_too_large refusal"
    warnings = receipt.get("warnings")
    warning_problem = evaluate_cli_corpus.warnings_problem(warnings)
    if warning_problem:
        return f"{label}: {warning_problem}"
    if warnings and (detection is None or outcome == "source_changed"):
        return f"{label}: warnings claimed without stable, detected bytes"
    return None


def stable_result(
    receipt: dict[str, Any],
    inspection_report: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    detection = receipt["detection"] or {}
    probe = receipt["probe"]
    return {
        "sequence": receipt["sequence"],
        "relativePath": identity["relativePath"],
        "sizeBytes": identity["sizeBytes"],
        "sha256": identity["sha256"],
        "status": receipt["status"],
        "resolvedKind": detection.get("resolvedKind"),
        "resolvedFormat": detection.get("resolvedFormat"),
        "nameAndBytes": detection.get("nameAndBytes"),
        "probeOutcome": probe["outcome"],
        "probeLevel": probe["level"],
        "code": probe["code"],
        "warnings": list(receipt["warnings"]),
        "inspection": evaluate_inspection_corpus.stable_inspection(inspection_report),
    }


def input_manifest_row(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "relativePath": identity["relativePath"],
        "sizeBytes": identity["sizeBytes"],
        "sha256": identity["sha256"],
        "identityStatus": "hashed" if identity["sha256"] else "over_limit_not_hashed",
    }


def json_lines(values: list[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        for value in values
    )


def manifest_bytes(outputs: dict[str, bytes]) -> bytes:
    return "".join(
        f"{sha256_bytes(data)}  {name}\n" for name, data in sorted(outputs.items())
    ).encode("utf-8")


def path_exists(path: Path) -> bool:
    """Existence without following a final symlink, including dangling ones."""
    return os.path.lexists(os.fspath(path))


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(data)
        destination.flush()
        os.fsync(destination.fileno())


def publish_directory_noreplace(staging: Path, output: Path) -> None:
    """Publish one complete bundle without ever replacing the destination."""
    staging_parent = staging.parent.resolve(strict=True)
    output_parent = output.parent.resolve(strict=True)
    if staging_parent != output_parent:
        raise ValueError("audit staging and output must share one directory")
    if path_exists(output):
        raise ValueError(f"audit output appeared during processing: {output}")
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise ValueError("atomic non-replacing audit publication is unavailable")
        renameat2.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(output_parent, flags)
        try:
            if renameat2(
                directory_fd,
                os.fsencode(staging.name),
                directory_fd,
                os.fsencode(output.name),
                RENAME_NOREPLACE,
            ) != 0:
                failure = ctypes.get_errno()
                if failure == errno.EEXIST:
                    raise ValueError(f"audit output appeared during processing: {output}")
                raise OSError(failure, os.strerror(failure), output)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise ValueError("atomic non-replacing audit publication is unavailable")
        renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(staging), os.fsencode(output), RENAME_EXCL) != 0:
            failure = ctypes.get_errno()
            if failure == errno.EEXIST:
                raise ValueError(f"audit output appeared during processing: {output}")
            raise OSError(failure, os.strerror(failure), output)
        return
    if os.name == "nt":
        # Python's Windows rename contract refuses an existing destination.
        try:
            os.rename(staging, output)
        except FileExistsError as error:
            raise ValueError(f"audit output appeared during processing: {output}") from error
        return
    raise ValueError(
        f"atomic non-replacing audit publication is unavailable on {sys.platform}"
    )


def normalized_jsonl(output: str) -> bytes:
    if not output:
        return b""
    return (output if output.endswith("\n") else output + "\n").encode("utf-8")


def inspection_problems(
    reports: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    identities: list[dict[str, Any]],
    source_root: Path,
    worker_timeout_millis: int,
    worker_max_heap_mib: int,
    expected_version: str | None,
) -> list[str]:
    rows = [
        {"id": identity["relativePath"], "local_path": identity["relativePath"]}
        for identity in identities
    ]
    pins = {
        identity["relativePath"]: {
            "size_bytes": str(identity["sizeBytes"]),
            "sha256": identity["sha256"],
        }
        for identity in identities
    }
    problems = evaluate_inspection_corpus.validate_reports(
        rows,
        pins,
        reports,
        source_root,
        worker_timeout_millis,
        worker_max_heap_mib,
        expected_version,
        source_root,
    )
    for identity, receipt, report in zip(identities, receipts, reports):
        label = identity["relativePath"]
        if not isinstance(receipt, dict) or not isinstance(report, dict):
            continue
        if report.get("engineVersion") != receipt.get("engineVersion"):
            problems.append(f"{label}: intake and inspection engine versions differ")
        if report.get("detection") != receipt.get("detection"):
            problems.append(f"{label}: intake and inspection detections differ")
        if report.get("warnings") != receipt.get("warnings"):
            problems.append(f"{label}: intake and inspection warnings differ")
        source = report.get("source")
        intake_source = receipt.get("source")
        if isinstance(source, dict) and isinstance(intake_source, dict):
            for field in ("name", "sizeBytes", "sha256", "stableDuringProcessing"):
                if source.get(field) != intake_source.get(field):
                    problems.append(f"{label}: intake and inspection source.{field} differ")
    return problems


def run_audit(args: argparse.Namespace) -> int:
    root = args.input.absolute()
    output = args.output.absolute()
    if path_exists(output):
        raise ValueError(f"audit output already exists: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"audit output parent does not exist: {output.parent}")
    root_real = root.resolve(strict=True)
    output_real = output.parent.resolve(strict=True) / output.name
    try:
        output_real.relative_to(root_real)
    except ValueError:
        pass
    else:
        raise ValueError("audit output must not be inside the input directory")
    output = output_real
    if path_exists(output):
        raise ValueError(f"audit output already exists: {output}")

    files = discover_files(root)
    identities = capture_identities(root, files)
    launcher = args.ufo.resolve(strict=True) if args.ufo else None
    if launcher is None and args.container_image is None:
        launcher = evaluate_cli_corpus.discover_launcher()
    if launcher is not None and (
        not launcher.is_file() or (os.name != "nt" and not os.access(launcher, os.X_OK))
    ):
        raise ValueError(f"ufo launcher is missing or not executable: {launcher}")
    if launcher is None and args.container_image is None:
        raise ValueError("no unique packaged ufo launcher found; supply --ufo")
    launcher_identity = launcher_artifact(launcher) if launcher is not None else None

    staging = Path(tempfile.mkdtemp(prefix=".ufo-audit-", dir=output.parent))
    cidfile: Path | None = None
    try:
        if args.container_image is not None:
            image_id = docker_image_id(
                args.container_image,
                cwd=REPO,
                timeout_seconds=min(args.command_timeout_seconds, 30),
            )
            requested = [f"/corpus/{identity['relativePath']}" for identity in identities]
            source_root = Path("/corpus")
            artifact = container_artifact(args.container_image, image_id)
            execution_image = image_id
        else:
            requested = [str(identity["path"].resolve()) for identity in identities]
            source_root = root_real
            assert launcher_identity is not None
            artifact = launcher_identity
        files0 = "\0".join(requested) + "\0"
        if len(files0.encode("utf-8")) > MAX_FILES0_BYTES:
            raise ValueError("audit path list exceeds the 16 MiB intake transport limit")

        def run_surface(label: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
            nonlocal cidfile
            if args.container_image is not None:
                cidfile = staging / f"container-{label}.cid"
                command = evaluate_cli_corpus.restricted_container_command(
                    image=execution_image,
                    corpus=root,
                    worker_max_heap_mib=args.worker_max_heap_mib,
                    cidfile=cidfile,
                    arguments=arguments,
                    container_user=(
                        f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else None
                    ),
                    worker_jobs=args.jobs,
                )
            else:
                command = [str(launcher), *arguments]
            try:
                return run_bounded_command(
                    command,
                    cwd=REPO,
                    input_bytes=files0.encode("utf-8"),
                    timeout_seconds=args.command_timeout_seconds,
                    label=f"ufo {label}",
                )
            finally:
                evaluate_cli_corpus.remove_container_from_cidfile(cidfile)
                cidfile = None

        common_arguments = [
            "--timeout-ms", str(args.worker_timeout_ms),
            "--max-heap-mib", str(args.worker_max_heap_mib),
            "--jobs", str(args.jobs),
            "--files0-from", "-",
        ]
        intake_process = run_surface("intake", ["intake", *common_arguments])
        receipts, problems = evaluate_cli_corpus.parse_json_lines(intake_process.stdout)
        if len(receipts) != len(identities):
            problems.append(f"expected {len(identities)} receipts, got {len(receipts)}")
        for sequence, identity in enumerate(identities):
            if sequence >= len(receipts):
                break
            problem = receipt_problem(
                receipts[sequence], sequence, identity, requested[sequence],
                args.worker_timeout_ms, args.worker_max_heap_mib, args.expect_version,
            )
            if problem:
                problems.append(problem)
        if intake_process.stderr:
            problems.append(f"ufo intake stderr was not empty: {intake_process.stderr.strip()!r}")
        expected_exit = 0 if receipts and all(row.get("status") == "accepted" for row in receipts) else 1
        if intake_process.returncode != expected_exit:
            problems.append(f"expected ufo intake exit {expected_exit}, got {intake_process.returncode}")
        if problems:
            raise ValueError("invalid UFO intake execution:\n" + "\n".join(f"- {item}" for item in problems))

        inspection_process = run_surface(
            "inspection", ["inspect", "--deep", *common_arguments]
        )
        reports, problems = evaluate_cli_corpus.parse_json_lines(inspection_process.stdout)
        problems.extend(
            inspection_problems(
                reports,
                receipts,
                identities,
                source_root,
                args.worker_timeout_ms,
                args.worker_max_heap_mib,
                args.expect_version,
            )
        )
        expected_exit = (
            0 if reports and all(row.get("status") == "completed" for row in reports) else 1
        )
        if inspection_process.returncode != expected_exit:
            problems.append(
                f"expected ufo inspection exit {expected_exit}, got {inspection_process.returncode}"
            )
        if inspection_process.stderr:
            problems.append(
                f"ufo inspection stderr was not empty: {inspection_process.stderr.strip()!r}"
            )
        if problems:
            raise ValueError(
                "invalid UFO deep-inspection execution:\n"
                + "\n".join(f"- {item}" for item in problems)
            )

        # The CLI already rehashes each in-limit source after its probe. The
        # harness repeats custody independently before publishing its reports.
        post_files = discover_files(root)
        if post_files != files:
            raise ValueError("audit input file set changed during processing")
        post = capture_identities(root, post_files)
        for before, after in zip(identities, post, strict=True):
            fields = (
                "relativePath", "sizeBytes", "lastModifiedNanos", "changeNanos",
                "device", "inode", "sha256",
            )
            if any(before[field] != after[field] for field in fields):
                raise ValueError(f"source changed during the compatibility audit: {before['relativePath']}")
        if launcher is not None and launcher_artifact(launcher) != launcher_identity:
            raise ValueError("packaged app image changed during the compatibility audit")

        stable = [
            stable_result(receipt, report, identity)
            for receipt, report, identity in zip(receipts, reports, identities, strict=True)
        ]
        input_manifest_data = json_lines([input_manifest_row(identity) for identity in identities])
        receipt_data = normalized_jsonl(intake_process.stdout)
        inspection_data = normalized_jsonl(inspection_process.stdout)
        stable_data = json_lines(stable)
        status_counts = Counter(row["status"] for row in stable)
        kind_counts = Counter(row["resolvedKind"] or "unresolved" for row in stable)
        format_counts = Counter(row["resolvedFormat"] or "unresolved" for row in stable)
        code_counts = Counter(row["code"] for row in stable if row["code"])
        name_byte_counts = Counter(row["nameAndBytes"] or "unresolved" for row in stable)
        warning_counts = Counter(code for row in stable for code in row["warnings"])
        inspection_status_counts = Counter(row["inspection"]["status"] for row in stable)
        inspection_flag_counts = Counter(
            flag for row in stable for flag in row["inspection"]["flags"]
        )
        inspection_finding_counts = Counter(
            finding["code"]
            for row in stable
            for finding in row["inspection"]["findings"]
        )
        inspection_severity_counts = Counter(
            row["inspection"]["highestSeverity"] for row in stable
        )
        inspection_not_inspected_counts = Counter(
            scope for row in stable for scope in row["inspection"]["notInspected"]
        )
        versions = {
            row["engineVersion"] for row in [*receipts, *reports]
        }
        if len(versions) != 1:
            raise ValueError(f"audit reports contain multiple engine versions: {sorted(versions)}")
        all_clear = all(
            row["status"] == "accepted"
            and not row["warnings"]
            and row["inspection"]["status"] == "completed"
            and not row["inspection"]["flags"]
            and not row["inspection"]["findings"]
            and not row["inspection"]["notInspected"]
            for row in stable
        )
        summary = {
            "schema": "com.krauq.ufo.compatibility-audit-summary",
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "engineVersion": versions.pop(),
            "auditOutcome": "all_clear" if all_clear else "review_required",
            "fileCount": len(stable),
            "totalBytes": sum(row["sizeBytes"] for row in stable),
            "statusCounts": {status: status_counts[status] for status in STATUSES},
            "resolvedKindCounts": dict(sorted(kind_counts.items())),
            "resolvedFormatCounts": dict(sorted(format_counts.items())),
            "nameAndBytesCounts": dict(sorted(name_byte_counts.items())),
            "codeCounts": dict(sorted(code_counts.items())),
            "warningCounts": dict(sorted(warning_counts.items())),
            "inspectionStatusCounts": {
                status: inspection_status_counts[status] for status in INSPECTION_STATUSES
            },
            "inspectionFlagCounts": dict(sorted(inspection_flag_counts.items())),
            "inspectionFindingCounts": dict(sorted(inspection_finding_counts.items())),
            "inspectionHighestSeverityCounts": {
                severity: inspection_severity_counts[severity] for severity in SEVERITIES
            },
            "inspectionNotInspectedCounts": dict(
                sorted(inspection_not_inspected_counts.items())
            ),
            "limits": {
                "maxSourceBytes": MAX_SOURCE_BYTES,
                "workerTimeoutMillis": args.worker_timeout_ms,
                "workerMaxHeapMiB": args.worker_max_heap_mib,
                "commandTimeoutSeconds": args.command_timeout_seconds,
                "jobs": args.jobs,
            },
            "artifact": artifact,
            "inputManifestSha256": sha256_bytes(input_manifest_data),
            "stableResultsSha256": sha256_bytes(stable_data),
            "receiptsSha256": sha256_bytes(receipt_data),
            "inspectionReportsSha256": sha256_bytes(inspection_data),
            "privacy": PRIVACY,
        }
        summary_data = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8")
        outputs = {
            "ufo-input-manifest.jsonl": input_manifest_data,
            "ufo-intake-receipts.jsonl": receipt_data,
            "ufo-inspection-reports.jsonl": inspection_data,
            "ufo-stable-results.jsonl": stable_data,
            "ufo-audit-summary.json": summary_data,
        }
        outputs["MANIFEST.sha256"] = manifest_bytes(outputs)
        for name, data in outputs.items():
            write_private_file(staging / name, data)
        fsync_directory(staging)
        publish_directory_noreplace(staging, output)
        print(
            f"UFO compatibility audit: {len(stable)} files; {summary['auditOutcome']}; "
            + ", ".join(f"intake.{status}={status_counts[status]}" for status in STATUSES)
        )
        print(
            "Inspection: "
            + ", ".join(
                f"{status}={inspection_status_counts[status]}"
                for status in INSPECTION_STATUSES
            )
        )
        print(f"Stable results sha256: {summary['stableResultsSha256']}")
        print(f"Reports: {output}")
        return 0
    finally:
        evaluate_cli_corpus.remove_container_from_cidfile(cidfile)
        if staging.exists():
            shutil.rmtree(staging)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", type=Path, required=True, help="customer-held directory to audit")
    result.add_argument("--output", type=Path, required=True, help="new private report directory")
    surface = result.add_mutually_exclusive_group()
    surface.add_argument("--ufo", type=Path, help="packaged ufo launcher; auto-detected when unique")
    surface.add_argument("--container-image", help="local restricted worker image")
    result.add_argument("--expect-version")
    result.add_argument("--worker-timeout-ms", type=int, default=30_000)
    result.add_argument("--worker-max-heap-mib", type=int, default=512)
    # One job is what an unlicensed build runs; a licensed machine can raise it.
    result.add_argument("--jobs", type=int, default=1)
    result.add_argument("--command-timeout-seconds", type=int, default=3_600)
    return result


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.worker_timeout_ms <= 300_000:
        parser().error("--worker-timeout-ms must be between 1 and 300000")
    if not 32 <= args.worker_max_heap_mib <= 2048:
        parser().error("--worker-max-heap-mib must be between 32 and 2048")
    if args.jobs not in range(1, 65):
        parser().error("--jobs must be between 1 and 64")
    if args.command_timeout_seconds < 1:
        parser().error("--command-timeout-seconds must be positive")
    try:
        return run_audit(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
