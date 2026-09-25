#!/usr/bin/env python3
"""Download the local regression corpus described by corpus_manifest.tsv."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "tools" / "corpus" / "corpus_manifest.tsv"
DEFAULT_PINS = REPO_ROOT / "tools" / "corpus" / "corpus_sources.lock.tsv"
DEFAULT_OUTPUT = REPO_ROOT / "local-corpus" / "files"
LOCK_HEADER = ["id", "local_path", "size_bytes", "sha256", "url", "license"]


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh, delimiter="\t") if row.get("id")]


def read_pins(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames != LOCK_HEADER:
            raise ValueError(f"Corpus pin lock has unexpected columns: {reader.fieldnames}")
        return [row for row in reader if row.get("id")]


def index_unique(rows: list[dict[str, str]], label: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        row_id = row.get("id", "")
        if not row_id or row_id in indexed:
            raise ValueError(f"{label} has an empty or duplicate id: {row_id!r}")
        indexed[row_id] = row
    return indexed


def validate_manifest_pins(
    manifest_rows: list[dict[str, str]], pin_rows: list[dict[str, str]]
) -> dict[str, dict[str, str]]:
    manifest = index_unique(manifest_rows, "corpus manifest")
    pins = index_unique(pin_rows, "corpus pin lock")
    if set(manifest) != set(pins):
        missing = sorted(set(manifest) - set(pins))
        extra = sorted(set(pins) - set(manifest))
        raise ValueError(f"Corpus pin IDs differ from manifest (missing={missing}, extra={extra})")
    for row_id, row in manifest.items():
        pin = pins[row_id]
        for field in ("local_path", "url", "license"):
            if row.get(field) != pin.get(field):
                raise ValueError(f"Corpus pin {row_id} has stale {field}")
        local_path = Path(pin.get("local_path") or "")
        if local_path.is_absolute() or ".." in local_path.parts:
            raise ValueError(f"Corpus pin {row_id} has unsafe local_path")
        try:
            size = int(pin.get("size_bytes") or "")
        except (TypeError, ValueError) as error:
            raise ValueError(f"Corpus pin {row_id} has invalid size_bytes") from error
        digest = pin.get("sha256") or ""
        if size <= 0 or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError(f"Corpus pin {row_id} has invalid size/hash")
    return pins


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_pinned(url: str, dst: Path, pin: dict[str, str], force: bool) -> None:
    """Download one reviewed fixture without replacing a good cache with bad bytes.

    Upstreams occasionally revise even nominally stable URLs. The old flow
    replaced the cached fixture first and only then noticed the hash mismatch.
    Verify the temporary file before the atomic replace instead, and stop after
    ``expected size + 1`` bytes so a changed URL cannot become an unbounded
    download.
    """
    if dst.exists() and not force:
        verify_pinned_file(dst, pin)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    expected_size = int(pin["size_bytes"])
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "UniversalFileOpener-corpus-downloader"},
    )
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name, suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp_path.open("wb") as out:
            written = 0
            while True:
                chunk = response.read(min(1024 * 256, expected_size + 1 - written))
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if written > expected_size:
                    raise ValueError(
                        f"Corpus fixture {pin['id']} exceeds its reviewed size "
                        f"of {expected_size} bytes"
                    )
        verify_pinned_file(tmp_path, pin)
        tmp_path.replace(dst)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def verify_pinned_file(path: Path, pin: dict[str, str]) -> None:
    if not path.is_file():
        raise ValueError(f"Pinned corpus file is missing after download: {path}")
    actual_size = path.stat().st_size
    actual_sha = sha256(path)
    if actual_size != int(pin["size_bytes"]) or actual_sha != pin["sha256"]:
        raise ValueError(
            f"Corpus fixture {pin['id']} differs from the reviewed pin: "
            f"size={actual_size} sha256={actual_sha}. Do not accept moving-upstream "
            "bytes implicitly; review and update corpus_sources.lock.tsv explicitly."
        )


def write_lock(rows: list[dict[str, str]], output: Path) -> None:
    lock_path = output.parent / "corpus-lock.tsv"
    with lock_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
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
                },
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=DEFAULT_PINS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.manifest = args.manifest.resolve()
    args.pins = args.pins.resolve()
    args.output = args.output.resolve()

    rows = read_manifest(args.manifest)
    try:
        pins = validate_manifest_pins(rows, read_pins(args.pins))
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid corpus source pins: {error}", file=sys.stderr)
        return 2
    start = time.time()
    for index, row in enumerate(rows, start=1):
        dst = args.output / row["local_path"]
        status = "refresh" if args.force or not dst.exists() else "cached"
        print(f"[{index:02d}/{len(rows):02d}] {status} {row['id']} -> {dst.relative_to(REPO_ROOT)}")
        try:
            download_pinned(row["url"], dst, pins[row["id"]], args.force)
            verify_pinned_file(dst, pins[row["id"]])
        except (OSError, ValueError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 2
    write_lock(rows, args.output)
    elapsed = time.time() - start
    print(f"Downloaded/verified {len(rows)} files in {elapsed:.1f}s")
    print(f"Corpus: {args.output}")
    print(f"Lock: {args.output.parent / 'corpus-lock.tsv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
