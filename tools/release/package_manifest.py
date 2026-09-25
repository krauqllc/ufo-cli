#!/usr/bin/env python3
"""Canonical, race-aware manifest generation for a packaged application tree."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Any


def _identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        stat.S_IFMT(details.st_mode),
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _sha256_stable_regular(path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(expected):
            raise ValueError(f"packaged file changed before it was read: {path}")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        if _identity(os.fstat(source.fileno())) != _identity(opened):
            raise ValueError(f"packaged file changed while it was read: {path}")
    if _identity(path.lstat()) != _identity(expected):
        raise ValueError(f"packaged file was replaced while it was read: {path}")
    return digest.hexdigest()


def package_manifest(
    root: Path,
    *,
    max_entries: int | None = None,
    max_total_bytes: int | None = None,
    max_file_bytes: int | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Return the canonical TSV bytes and records for regular files and links."""
    root_details = root.lstat()
    if not stat.S_ISDIR(root_details.st_mode):
        raise ValueError(f"packaged app image is missing or is a symlink: {root}")
    root_real = root.resolve(strict=True)
    candidates: list[Path] = []
    enumerated_entries = 0

    def walk_error(error: OSError) -> None:
        raise ValueError(f"packaged app image could not be enumerated: {error}") from error

    for current, directories, names in os.walk(
        root_real, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        for name in list(directories):
            enumerated_entries += 1
            if max_entries is not None and enumerated_entries > max_entries:
                raise ValueError(
                    f"packaged app image exceeds the {max_entries}-entry limit"
                )
            candidate = current_path / name
            if candidate.is_symlink():
                candidates.append(candidate)
                directories.remove(name)
        for name in names:
            enumerated_entries += 1
            if max_entries is not None and enumerated_entries > max_entries:
                raise ValueError(
                    f"packaged app image exceeds the {max_entries}-entry limit"
                )
            candidates.append(current_path / name)
    candidates.sort(key=lambda item: item.relative_to(root_real).as_posix())

    records: list[dict[str, Any]] = []
    total_bytes = 0
    for path in candidates:
        relative = path.relative_to(root_real).as_posix()
        if any(character in relative for character in ("\t", "\n", "\r")):
            raise ValueError(f"unsupported control character in packaged path: {relative!r}")
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"packaged path is not valid UTF-8: {relative!r}") from error
        details = path.lstat()
        mode = f"{stat.S_IMODE(details.st_mode):04o}"
        if stat.S_ISREG(details.st_mode):
            if max_file_bytes is not None and details.st_size > max_file_bytes:
                raise ValueError(
                    f"packaged file exceeds the {max_file_bytes}-byte limit: {relative}"
                )
            digest = _sha256_stable_regular(path, details)
            record = {
                "type": "file",
                "mode": mode,
                "size": details.st_size,
                "sha256": digest,
                "path": relative,
                "target": "",
            }
        elif stat.S_ISLNK(details.st_mode):
            target = os.readlink(path)
            if any(character in target for character in ("\t", "\n", "\r")):
                raise ValueError(
                    f"unsupported control character in packaged link: {relative!r}"
                )
            try:
                target_bytes = target.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    f"packaged link target is not valid UTF-8: {relative!r}"
                ) from error
            try:
                resolved = (path.parent / target).resolve(strict=True)
                resolved.relative_to(root_real)
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"packaged symlink escapes or is dangling: {relative} -> {target}"
                ) from error
            if _identity(path.lstat()) != _identity(details):
                raise ValueError(f"packaged symlink changed while it was read: {relative}")
            record = {
                "type": "symlink",
                "mode": mode,
                "size": len(target_bytes),
                "sha256": hashlib.sha256(target_bytes).hexdigest(),
                "path": relative,
                "target": target,
            }
        else:
            raise ValueError(f"unsupported packaged file type: {relative}")
        records.append(record)
        total_bytes += record["size"]
        if max_total_bytes is not None and total_bytes > max_total_bytes:
            raise ValueError(
                f"packaged app image exceeds the {max_total_bytes}-byte limit"
            )

    if not records:
        raise ValueError(f"packaged app image contains no files: {root}")
    if _identity(root_real.lstat()) != _identity(root_details):
        raise ValueError(f"packaged app image changed while it was read: {root}")
    lines = ["type\tmode\tsize_bytes\tsha256\tpath\tsymlink_target"]
    lines.extend(
        "\t".join(
            [
                record["type"],
                record["mode"],
                str(record["size"]),
                record["sha256"],
                record["path"],
                record["target"],
            ]
        )
        for record in records
    )
    return ("\n".join(lines) + "\n").encode("utf-8"), records
