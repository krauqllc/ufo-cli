#!/usr/bin/env python3
"""Download, verify, and production-probe the pinned desktop public corpus."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    import download_corpus
    import download_realworld
except ModuleNotFoundError:  # Loaded as tools.corpus.evaluate_desktop_corpus in tests.
    from tools.corpus import download_corpus, download_realworld


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = download_realworld.DEFAULT_OUTPUT
DEFAULT_REPORT = REPO_ROOT / "local-corpus" / "reports" / "desktop-public-open.json"
DEFAULT_GAPS = REPO_ROOT / "local-corpus" / "reports" / "desktop-public-coverage-gaps.json"


def local_corpus_problems(
    rows: list[dict[str, str]],
    pins: dict[str, dict[str, str]],
    corpus: Path,
) -> list[str]:
    problems: list[str] = []
    expected_paths = {Path(row["local_path"]).as_posix() for row in rows}
    for row in rows:
        path = corpus / row["local_path"]
        if path.is_symlink():
            problems.append(f"{row['id']}: fixture must not be a symlink")
            continue
        try:
            download_corpus.verify_pinned_file(path, pins[row["id"]])
        except ValueError as error:
            problems.append(str(error))
    if corpus.is_dir():
        actual_paths = {
            path.relative_to(corpus).as_posix()
            for path in corpus.rglob("*")
            if path.is_file()
        }
        for extra in sorted(actual_paths - expected_paths):
            problems.append(f"unexpected file in managed corpus: {extra}")
    return problems


def validate_report(
    rows: list[dict[str, str]],
    report: dict,
    corpus: Path,
) -> list[str]:
    problems: list[str] = []
    actual: dict[str, dict] = {}
    for item in report.get("files", []):
        try:
            relative = Path(item["path"]).resolve().relative_to(corpus.resolve()).as_posix()
        except (KeyError, ValueError):
            problems.append(f"runner reported a path outside the corpus: {item.get('path')!r}")
            continue
        if relative in actual:
            problems.append(f"runner reported duplicate path: {relative}")
        actual[relative] = item

    expected_paths = {row["local_path"] for row in rows}
    for extra in sorted(set(actual) - expected_paths):
        problems.append(f"runner reported unmanaged file: {extra}")
    for row in rows:
        item = actual.get(row["local_path"])
        if item is None:
            problems.append(f"{row['id']}: no desktop runner result")
            continue
        checks = (
            ("kind", row["expected_kind"]),
            ("probe", row["expected_probe"]),
            ("outcome", row["expected_outcome"]),
        )
        for field, expected in checks:
            if item.get(field) != expected:
                problems.append(
                    f"{row['id']}: expected {field}={expected}, got {item.get(field)!r}"
                )
        if item.get("outcome") == "refused" and not str(item.get("detail", "")).strip():
            problems.append(f"{row['id']}: refusal omitted its reason")
        millis = item.get("millis")
        if not isinstance(millis, int) or millis < 0:
            problems.append(f"{row['id']}: invalid timing {millis!r}")

    if report.get("total") != len(actual):
        problems.append(
            f"runner total {report.get('total')!r} does not match {len(actual)} file rows"
        )
    return problems


def gap_summary(rows: list[dict[str, str]], report: dict, corpus: Path) -> dict:
    observed: dict[str, dict] = {}
    for item in report.get("files", []):
        try:
            relative = Path(item["path"]).resolve().relative_to(corpus.resolve()).as_posix()
        except (KeyError, ValueError):
            continue
        observed[relative] = item
    gaps = []
    for row in rows:
        reason = row["coverage_gap"].strip()
        if not reason:
            continue
        item = observed.get(row["local_path"], {})
        gaps.append(
            {
                "id": row["id"],
                "kind": row["expected_kind"],
                "currentProbe": item.get("probe", row["expected_probe"]),
                "targetProbe": row["target_probe"],
                "reason": reason,
            }
        )
    current_counts = Counter(
        item.get("probe", "missing") for item in report.get("files", [])
    )
    return {
        "schemaVersion": 1,
        "fixtureCount": len(rows),
        "currentProbeCounts": dict(sorted(current_counts.items())),
        "gapCount": len(gaps),
        "gaps": gaps,
    }


def gradle_command(corpus: Path, report: Path) -> list[str]:
    if os.name == "nt":
        wrapper = REPO_ROOT / "gradlew.bat"
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/c",
            str(wrapper),
            ":desktop-app:corpusRun",
            f"-Pdir={corpus}",
            f"-Pjson={report}",
            "--rerun",
        ]
    return [
        str(REPO_ROOT / "gradlew"),
        ":desktop-app:corpusRun",
        f"-Pdir={corpus}",
        f"-Pjson={report}",
        "--rerun",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=download_realworld.DEFAULT_MANIFEST)
    parser.add_argument("--pins", type=Path, default=download_realworld.DEFAULT_PINS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--gaps", type=Path, default=DEFAULT_GAPS)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()
    args.manifest = args.manifest.resolve()
    args.pins = args.pins.resolve()
    args.corpus = args.corpus.resolve()
    args.report = args.report.resolve()
    args.gaps = args.gaps.resolve()

    try:
        rows = download_realworld.read_manifest(args.manifest)
        pins = download_corpus.validate_manifest_pins(
            rows,
            download_corpus.read_pins(args.pins),
        )
    except (OSError, ValueError) as error:
        print(f"FAIL: invalid desktop corpus authority: {error}", file=sys.stderr)
        return 2

    if args.download or args.force_download:
        command = [
            sys.executable,
            str(REPO_ROOT / "tools" / "corpus" / "download_realworld.py"),
            "--manifest",
            str(args.manifest),
            "--pins",
            str(args.pins),
            "--output",
            str(args.corpus),
        ]
        if args.force_download:
            command.append("--force")
        result = subprocess.run(command, cwd=REPO_ROOT, check=False)
        if result.returncode != 0:
            return result.returncode

    problems = local_corpus_problems(rows, pins, args.corpus)
    if problems:
        print("FAIL: desktop corpus cache is not the reviewed set:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.gaps.parent.mkdir(parents=True, exist_ok=True)
    args.report.unlink(missing_ok=True)
    args.gaps.unlink(missing_ok=True)
    result = subprocess.run(
        gradle_command(args.corpus, args.report),
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    if not args.report.is_file():
        print(
            f"FAIL: desktop corpus runner wrote no fresh report at {args.report}",
            file=sys.stderr,
        )
        return 2
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"FAIL: invalid desktop corpus report: {error}", file=sys.stderr)
        return 2

    problems = validate_report(rows, report, args.corpus)
    summary = gap_summary(rows, report, args.corpus)
    args.gaps.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if problems:
        print("FAIL: desktop corpus contract mismatches:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(
        f"Desktop public corpus: {len(rows)}/{len(rows)} expected routes and outcomes; "
        f"{summary['gapCount']} explicit coverage gaps"
    )
    print(f"Report: {args.report}")
    print(f"Gap summary: {args.gaps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
