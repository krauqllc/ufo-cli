#!/usr/bin/env python3
"""Download the real-world per-family corpus described by realworld_corpus_manifest.tsv.

This extends the real-world download tooling in download_realworld.py to the
DOCX, XLSX, PPTX and PDF families, where the question is not "does it open" but
"whose producer wrote it". Each row records the family, the producer group, the
exact producer string read out of the file itself, the document lineage, the
holdout flag, the source URL and the rights basis. Bytes are pinned by size and
SHA-256 in realworld_corpus_sources.lock.tsv and verified on every run, and the
producer recorded in the manifest is re-read from the downloaded bytes, so a row
cannot quietly mislabel who produced the file.

Nothing here is redistributed: the manifest holds URLs and hashes, and the files
land in local-corpus/realworld/, which git ignores.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib

try:
    from download_corpus import (
        LOCK_HEADER,
        download_pinned,
        read_pins,
        sha256,
        validate_manifest_pins,
        verify_pinned_file,
    )
except ModuleNotFoundError:  # Loaded as tools.corpus.download_realworld_corpus in unit tests.
    from tools.corpus.download_corpus import (
        LOCK_HEADER,
        download_pinned,
        read_pins,
        sha256,
        validate_manifest_pins,
        verify_pinned_file,
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "tools" / "corpus" / "realworld_corpus_manifest.tsv"
DEFAULT_PINS = REPO_ROOT / "tools" / "corpus" / "realworld_corpus_sources.lock.tsv"
DEFAULT_OUTPUT = REPO_ROOT / "local-corpus" / "realworld"
MANIFEST_HEADER = [
    "id",
    "local_path",
    "family",
    "producer",
    "producer_detail",
    "lineage",
    "holdout",
    "origin",
    "url",
    "license",
    "rights_basis",
    "source_project",
    "source_ref",
]
FAMILIES = {"docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx", "pdf": ".pdf"}
# tools/cli/own_file_evaluation.py MAX_FILES: one evaluation run reads at most 200 files.
# The corpus is larger than that, so it is split into per-family batches at that bound,
# never by raising it.
MAX_BATCH_FILES = 200
ORIGINS = {"published document", "upstream test corpus"}
HOLDOUT_VALUES = {"yes", "no"}
# docs/cli/ROADMAP.md "Acceptance rules for the first release": 20 independently
# varied lineages per launch family, at least three producers, 20% unseen holdout.
MIN_LINEAGES_PER_FAMILY = 20
MIN_PRODUCERS_PER_FAMILY = 3
MIN_HOLDOUT_FRACTION = 0.20
# Producers that describe a package the readers cannot open rather than an application.
UNREADABLE_PRODUCERS = {
    "encrypted OOXML (OLE2 container)",
    "not a readable OOXML package",
    "no Application recorded",
    "no Producer recorded",
    "encrypted PDF",
}


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != MANIFEST_HEADER:
            raise ValueError(f"Real-world corpus manifest has unexpected columns: {reader.fieldnames}")
        rows = [row for row in reader if row.get("id")]
    validate_provenance(rows)
    validate_corpus_floor(rows)
    return rows


def validate_provenance(rows: list[dict[str, str]]) -> None:
    paths: set[str] = set()
    for row in rows:
        row_id = row["id"]
        local_path = Path(row["local_path"])
        if local_path.is_absolute() or ".." in local_path.parts or not local_path.name:
            raise ValueError(f"Real-world corpus fixture {row_id} has an unsafe local_path")
        normalized = local_path.as_posix()
        if normalized in paths:
            raise ValueError(f"Real-world corpus manifest has duplicate local_path: {normalized}")
        paths.add(normalized)
        family = row["family"]
        if family not in FAMILIES:
            raise ValueError(f"Real-world corpus fixture {row_id} has unknown family {family!r}")
        if local_path.suffix.lower() != FAMILIES[family]:
            raise ValueError(f"Real-world corpus fixture {row_id} has a local_path outside its family")
        if local_path.parts[0] != family:
            raise ValueError(f"Real-world corpus fixture {row_id} is not stored under its family")
        for field in ("producer", "producer_detail", "lineage", "license", "rights_basis",
                      "source_project", "source_ref"):
            if not row[field].strip():
                raise ValueError(f"Real-world corpus fixture {row_id} lacks {field}")
        if row["holdout"] not in HOLDOUT_VALUES:
            raise ValueError(f"Real-world corpus fixture {row_id} has an unknown holdout flag")
        if row["origin"] not in ORIGINS:
            raise ValueError(f"Real-world corpus fixture {row_id} has an unknown origin")
        if "raw.githubusercontent.com" in row["url"]:
            source_ref = row["source_ref"]
            if len(source_ref) != 40 or f"/{source_ref}/" not in row["url"]:
                raise ValueError(
                    f"Real-world corpus fixture {row_id} must use its immutable 40-character source ref"
                )


def validate_corpus_floor(rows: list[dict[str, str]]) -> None:
    """Refuse a manifest that no longer meets the roadmap's acceptance floor."""
    for family in sorted(FAMILIES):
        family_rows = [row for row in rows if row["family"] == family]
        if not family_rows:
            raise ValueError(f"Real-world corpus manifest has no {family} rows")
        lineages = {row["lineage"] for row in family_rows}
        if len(lineages) < MIN_LINEAGES_PER_FAMILY:
            raise ValueError(
                f"Real-world corpus {family} has {len(lineages)} lineages, "
                f"below the {MIN_LINEAGES_PER_FAMILY} lineage floor"
            )
        producers = {row["producer"] for row in family_rows} - UNREADABLE_PRODUCERS
        if len(producers) < MIN_PRODUCERS_PER_FAMILY:
            raise ValueError(
                f"Real-world corpus {family} names {len(producers)} producers, "
                f"below the {MIN_PRODUCERS_PER_FAMILY} producer floor"
            )
        holdout = {row["lineage"] for row in family_rows if row["holdout"] == "yes"}
        required = max(1, math.ceil(len(lineages) * MIN_HOLDOUT_FRACTION))
        if len(holdout) < required:
            raise ValueError(
                f"Real-world corpus {family} holds out {len(holdout)} of {len(lineages)} lineages, "
                f"below the required {required}"
            )


def observed_producer(path: Path, family: str) -> str:
    """Read the producer back out of the downloaded bytes, or "" when there is none."""
    data = path.read_bytes()
    if family == "pdf":
        return pdf_producer(data)
    return ooxml_application(data)


def ooxml_application(data: bytes) -> str:
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
        source = package.read("docProps/app.xml")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return ""
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == "Application":
            return " ".join((child.text or "").split())
    return ""


_INFO = re.compile(rb"/Producer\s*(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)")


def pdf_producer(data: bytes) -> str:
    match = _INFO.search(data)
    if match:
        return _decode_pdf_string(match.group(1))
    for stream in re.finditer(rb"stream\r?\n", data):
        start = stream.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        try:
            expanded = zlib.decompress(data[start:end])
        except zlib.error:
            continue
        inner = _INFO.search(expanded)
        if inner:
            return _decode_pdf_string(inner.group(1))
    return ""


ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
_LITERAL_ESCAPE = re.compile(rb"\\(?:([0-7]{1,3})|(\r\n|[\r\n])|(.))", re.S)


def _unescape_pdf_literal(raw: bytes) -> bytes:
    """Resolve PDF literal-string escapes, octal ones included.

    A producer written as \\055 or as a backslash-escaped space is the same string as the
    plain one; without this the manifest would record the escape text as the producer.
    """

    def replace(match: re.Match[bytes]) -> bytes:
        octal, newline, other = match.groups()
        if octal is not None:
            return bytes([int(octal, 8) & 0xFF])
        if newline is not None:
            return b""
        return ESCAPES.get(other, other)

    return _LITERAL_ESCAPE.sub(replace, raw)


def _decode_pdf_string(raw: bytes) -> str:
    if raw.startswith(b"<"):
        digits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
        if len(digits) % 2:
            digits += b"0"
        raw = bytes.fromhex(digits.decode("ascii"))
    else:
        raw = _unescape_pdf_literal(raw[1:-1])
    if raw.startswith(b"\xfe\xff"):
        return " ".join(raw[2:].decode("utf-16-be", "replace").split())
    return " ".join(raw.decode("latin-1", "replace").split())


def verify_producer(row: dict[str, str], path: Path) -> None:
    """A recorded producer must still be the one the file's own metadata names."""
    if row["producer"] in UNREADABLE_PRODUCERS:
        return
    detail = row["producer_detail"].split(" (AppVersion ")[0].strip()
    actual = observed_producer(path, row["family"])
    if not actual:
        raise ValueError(
            f"Real-world corpus fixture {row['id']} records producer {detail!r} but the file names none"
        )
    # The manifest truncates a very long producer string; a prefix match is the honest comparison.
    if not (actual.startswith(detail) or detail.startswith(actual)):
        raise ValueError(
            f"Real-world corpus fixture {row['id']} records producer {detail!r} "
            f"but the file names {actual!r}"
        )


def selected_rows(rows: list[dict[str, str]], only: list[str], families: list[str],
                  exclude_holdout: bool) -> list[dict[str, str]]:
    chosen = rows
    if families:
        unknown = sorted(set(families) - set(FAMILIES))
        if unknown:
            raise ValueError(f"Unknown real-world corpus families: {unknown}")
        chosen = [row for row in chosen if row["family"] in set(families)]
    if exclude_holdout:
        chosen = [row for row in chosen if row["holdout"] == "no"]
    if only:
        wanted = set(only)
        unknown = sorted(wanted - {row["id"] for row in rows})
        if unknown:
            raise ValueError(f"Unknown real-world corpus fixture ids: {unknown}")
        chosen = [row for row in chosen if row["id"] in wanted]
    if not chosen:
        raise ValueError("The real-world corpus selection is empty")
    return chosen


def plan_batches(rows: list[dict[str, str]], output: Path, directory: Path,
                 batch_size: int) -> list[Path]:
    """Write per-family file lists the own-file recipe accepts, one path per line.

    The recipe refuses more than MAX_BATCH_FILES files in a run, so a corpus this size is
    evaluated as several runs. Batching keeps each family whole and its rows in manifest
    order, so a batch is reproducible from the manifest alone.
    """
    if not 1 <= batch_size <= MAX_BATCH_FILES:
        raise ValueError(
            f"a batch holds 1 to {MAX_BATCH_FILES} files, the own-file recipe's bound"
        )
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for family in sorted(FAMILIES):
        family_rows = [row for row in rows if row["family"] == family]
        for start in range(0, len(family_rows), batch_size):
            batch = family_rows[start:start + batch_size]
            path = directory / f"{family}-{start // batch_size + 1:02d}.txt"
            path.write_text(
                "".join(f"{output / row['local_path']}\n" for row in batch), encoding="utf-8"
            )
            written.append(path)
    return written


def write_receipt(rows: list[dict[str, str]], output: Path, partial: bool) -> Path:
    suffix = "-selection-lock.tsv" if partial else "-lock.tsv"
    receipt = output.parent / f"{output.name}{suffix}"
    with receipt.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, delimiter="\t", fieldnames=LOCK_HEADER, lineterminator="\n")
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


def summarize(rows: list[dict[str, str]]) -> list[str]:
    lines = []
    for family in sorted(FAMILIES):
        family_rows = [row for row in rows if row["family"] == family]
        if not family_rows:
            continue
        lineages = {row["lineage"] for row in family_rows}
        holdout = {row["lineage"] for row in family_rows if row["holdout"] == "yes"}
        producers = {row["producer"] for row in family_rows}
        lines.append(
            f"  {family}: {len(family_rows)} files, {len(lineages)} lineages, "
            f"{len(producers)} producers, {len(holdout)} holdout lineages"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--only", action="append", default=[], help="download only these ids")
    parser.add_argument("--family", action="append", default=[], help="limit to these families")
    parser.add_argument("--exclude-holdout", action="store_true",
                        help="skip the reserved holdout lineages")
    parser.add_argument("--download", action="store_true",
                        help="fetch anything missing; without it the run verifies the local cache only")
    parser.add_argument("--force", action="store_true", help="re-fetch and re-verify cached files")
    parser.add_argument("--plan-batches", type=Path, metavar="DIR",
                        help="write per-family batch file lists the own-file recipe accepts")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_FILES,
                        help=f"files per batch, at most the recipe's {MAX_BATCH_FILES}")
    args = parser.parse_args(argv)
    args.manifest = args.manifest.resolve()
    args.pins = args.pins.resolve()
    args.output = args.output.resolve()

    try:
        all_rows = read_manifest(args.manifest)
        pins = validate_manifest_pins(all_rows, read_pins(args.pins))
        rows = selected_rows(all_rows, args.only, args.family, args.exclude_holdout)
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid real-world corpus authority: {error}", file=sys.stderr)
        return 2

    started = time.time()
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        destination = args.output / row["local_path"]
        cached = destination.exists() and not args.force
        if not cached and not args.download:
            print(f"FAIL: {row['id']} is not cached; rerun with --download", file=sys.stderr)
            return 2
        print(f"[{index:03d}/{total:03d}] {'verify' if cached else 'fetch '} {row['id']}")
        try:
            if cached:
                verify_pinned_file(destination, pins[row["id"]])
            else:
                download_pinned(row["url"], destination, pins[row["id"]], args.force)
            verify_producer(row, destination)
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2

    partial = bool(args.only or args.family or args.exclude_holdout)
    receipt = write_receipt(rows, args.output, partial)
    batches: list[Path] = []
    if args.plan_batches is not None:
        try:
            batches = plan_batches(rows, args.output, args.plan_batches.resolve(), args.batch_size)
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
    total_bytes = sum((args.output / row["local_path"]).stat().st_size for row in rows)
    print(
        f"Downloaded/verified {total} files ({total_bytes / 1_048_576:.1f} MiB) "
        f"in {time.time() - started:.1f}s"
    )
    for line in summarize(rows):
        print(line)
    print(f"Corpus: {args.output}")
    print(f"Receipt: {receipt}")
    for path in batches:
        print(f"Batch: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
