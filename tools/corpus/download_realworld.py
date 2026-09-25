#!/usr/bin/env python3
"""Download the reviewed real-world desktop corpus from immutable pins."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
import time

try:
    from download_corpus import (
        LOCK_HEADER,
        download_pinned,
        read_pins,
        sha256,
        validate_manifest_pins,
    )
except ModuleNotFoundError:  # Loaded as tools.corpus.download_realworld in unit tests.
    from tools.corpus.download_corpus import (
        LOCK_HEADER,
        download_pinned,
        read_pins,
        sha256,
        validate_manifest_pins,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "tools" / "corpus" / "realworld_manifest.tsv"
DEFAULT_PINS = REPO_ROOT / "tools" / "corpus" / "realworld_sources.lock.tsv"
DEFAULT_OUTPUT = REPO_ROOT / "local-corpus" / "desktop-public"
MANIFEST_HEADER = [
    "id",
    "local_path",
    "url",
    "license",
    "source_project",
    "source_ref",
    "expected_kind",
    "expected_probe",
    "target_probe",
    "expected_outcome",
    "coverage_gap",
    "why",
]
KNOWN_PROBES = {"WorkerParsed", "Parsed", "DetectedOnly"}
KNOWN_OUTCOMES = {"opened", "refused"}


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != MANIFEST_HEADER:
            raise ValueError(f"Real-world manifest has unexpected columns: {reader.fieldnames}")
        rows = [row for row in reader if row.get("id")]
    validate_provenance(rows)
    return rows


def validate_provenance(rows: list[dict[str, str]]) -> None:
    paths: set[str] = set()
    for row in rows:
        row_id = row["id"]
        local_path = Path(row["local_path"])
        if local_path.is_absolute() or ".." in local_path.parts or not local_path.name:
            raise ValueError(f"Real-world fixture {row_id} has an unsafe local_path")
        normalized = local_path.as_posix()
        if normalized in paths:
            raise ValueError(f"Real-world manifest has duplicate local_path: {normalized}")
        paths.add(normalized)
        if not row["source_project"].strip() or not row["source_ref"].strip():
            raise ValueError(f"Real-world fixture {row_id} lacks source provenance")
        if not row["license"].strip():
            raise ValueError(f"Real-world fixture {row_id} lacks a reviewed license")
        if row["expected_probe"] not in KNOWN_PROBES:
            raise ValueError(f"Real-world fixture {row_id} has unknown expected_probe")
        if row["target_probe"] not in KNOWN_PROBES:
            raise ValueError(f"Real-world fixture {row_id} has unknown target_probe")
        if row["expected_outcome"] not in KNOWN_OUTCOMES:
            raise ValueError(f"Real-world fixture {row_id} has unknown expected_outcome")
        if row["expected_probe"] != row["target_probe"] and not row["coverage_gap"].strip():
            raise ValueError(f"Real-world fixture {row_id} hides a probe-depth gap")
        if "raw.githubusercontent.com" in row["url"]:
            source_ref = row["source_ref"]
            if len(source_ref) != 40 or f"/{source_ref}/" not in row["url"]:
                raise ValueError(
                    f"Real-world fixture {row_id} must use its immutable 40-character source ref"
                )


def selected_rows(rows: list[dict[str, str]], only: list[str]) -> list[dict[str, str]]:
    if not only:
        return rows
    wanted = set(only)
    known = {row["id"] for row in rows}
    unknown = sorted(wanted - known)
    if unknown:
        raise ValueError(f"Unknown real-world fixture ids: {unknown}")
    return [row for row in rows if row["id"] in wanted]


def write_receipt(rows: list[dict[str, str]], output: Path, partial: bool = False) -> Path:
    suffix = "-selection-lock.tsv" if partial else "-lock.tsv"
    receipt = output.parent / f"{output.name}{suffix}"
    with receipt.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            delimiter="\t",
            fieldnames=LOCK_HEADER,
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            path = output / row["local_path"]
            writer.writerow(
                {
                    "id": row["id"],
                    "local_path": row["local_path"],
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256(path),
                    "url": row["url"],
                    "license": row["license"],
                }
            )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", action="append", default=[], help="download only these ids")
    parser.add_argument("--force", action="store_true", help="re-fetch and re-verify cached files")
    args = parser.parse_args()
    args.manifest = args.manifest.resolve()
    args.pins = args.pins.resolve()
    args.output = args.output.resolve()

    try:
        all_rows = read_manifest(args.manifest)
        pins = validate_manifest_pins(all_rows, read_pins(args.pins))
        rows = selected_rows(all_rows, args.only)
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid real-world corpus authority: {error}", file=sys.stderr)
        return 2

    started = time.time()
    for index, row in enumerate(rows, start=1):
        destination = args.output / row["local_path"]
        state = "refresh" if args.force or not destination.exists() else "verify"
        print(f"[{index:02d}/{len(rows):02d}] {state} {row['id']} -> {row['local_path']}")
        try:
            download_pinned(row["url"], destination, pins[row["id"]], args.force)
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2

    receipt = write_receipt(rows, args.output, partial=bool(args.only))
    total_bytes = sum((args.output / row["local_path"]).stat().st_size for row in rows)
    print(
        f"Downloaded/verified {len(rows)} files ({total_bytes / 1_048_576:.1f} MiB) "
        f"in {time.time() - started:.1f}s"
    )
    print(f"Corpus: {args.output}")
    print(f"Receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
