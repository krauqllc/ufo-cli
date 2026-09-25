#!/usr/bin/env python3
"""Artifact identities shared by CLI corpus and compatibility-audit gates."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from tools.release.package_manifest import package_manifest


MAX_APP_IMAGE_ENTRIES = 20_000
MAX_APP_IMAGE_BYTES = 1024 * 1024 * 1024
MAX_APP_IMAGE_FILE_BYTES = 512 * 1024 * 1024


def launcher_artifact(launcher: Path) -> dict[str, Any]:
    """Bind a packaged launcher to every file and link in its app image."""
    actual = launcher.resolve(strict=True)
    if actual.parent.name != "bin":
        raise ValueError(f"packaged ufo launcher is not under an app-image bin/: {actual}")
    app_image = actual.parent.parent
    app_payload = app_image / "lib/app"
    if not app_payload.is_dir() or app_payload.is_symlink():
        raise ValueError(f"packaged ufo launcher has no app-image lib/app payload: {actual}")
    if os.name != "nt" and not os.access(actual, os.X_OK):
        raise ValueError(f"packaged ufo launcher is not executable: {actual}")

    manifest, records = package_manifest(
        app_image,
        max_entries=MAX_APP_IMAGE_ENTRIES,
        max_total_bytes=MAX_APP_IMAGE_BYTES,
        max_file_bytes=MAX_APP_IMAGE_FILE_BYTES,
    )
    launcher_relative = actual.relative_to(app_image).as_posix()
    if not any(
        record["path"] == launcher_relative and record["type"] == "file"
        for record in records
    ):
        raise ValueError("packaged launcher is absent from its app-image manifest")
    return {
        "surface": "launcher",
        "launcher": str(actual),
        "appImageRoot": str(app_image),
        "appImageTreeSha256": hashlib.sha256(manifest).hexdigest(),
        "appImageTreeEntries": len(records),
        "appImageBytes": sum(record["size"] for record in records),
    }


def container_artifact(image: str, image_id: str) -> dict[str, str]:
    """Return the common identity shape for an immutable local image run."""
    return {"surface": "container", "image": image, "imageId": image_id}
