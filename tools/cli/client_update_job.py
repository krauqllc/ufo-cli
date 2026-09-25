#!/usr/bin/env python3
"""Run the flagship client-deliverable update job on a packaged UFO build.

The job unpacks a synthetic client bundle (a DOCX report, an XLSX table, a PPTX
summary, a text-bearing PDF appendix, a PNG and a JSON status file), reads only
the blocks the update needs, applies one guarded edit per file (a native tracked
change with a comment, typed cells, a slide text and its notes, a selected-page
PDF copy, a resized image and a JSON field), demonstrates a stale-target refusal
and its retry to a fresh path, and writes a job manifest with every step's
status, output hash and remaining review needs. It orchestrates the ordinary
single-file commands; it is not a workflow engine. No input dataset, download,
account or upload is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.audit.create_compatibility_audit import path_exists, publish_directory_noreplace, write_private_file
from tools.cli.artifact_identity import container_artifact, launcher_artifact
from tools.cli.bounded_process import docker_image_id, run_bounded_command
from tools.cli.evaluate_sample import (
    PDF_PAGE_TEXTS, SAMPLE_TEXT, decode_png, identity, pdf_current_page_texts, read_bounded, receipt,
    remove_container, require, require_subset, resolved_text, sample_files,
)
from tools.corpus.evaluate_edit_corpus import restricted_docker_prefix

JOB = "client-deliverable-update"
MANIFEST_VERSION = 1
BUNDLE_FILES = ("report.docx", "data.xlsx", "deck.pptx", "report.pdf", "photo.png", "update.json")
AUTHOR = "Account manager"
REPORT_LINE = "UFO updated client report for September."
COMMENT = "Please confirm this closing line before the deliverable goes out."
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class StepFailure(ValueError):
    """A step did not end the way the job expected; the manifest records it and the job stops."""

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


def bundle_bytes() -> bytes:
    """The client bundle: the sample documents in one stored ZIP with fixed timestamps."""
    files = sample_files()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as package:
        for name in BUNDLE_FILES:
            entry = zipfile.ZipInfo(f"client/{name}", date_time=(2000, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            package.writestr(entry, files[name])
    return target.getvalue()


def review_needs(step: str, value: dict) -> list[str]:
    """What a person still has to look at after this receipt, derived from what the receipt says ran."""
    if value.get("status") != "completed" or value.get("action") != "edit":
        return []
    kinds = [item["kind"] for item in value["result"].get("applied", [])]
    needs: list[str] = []
    if "paragraph_tracked" in kinds:
        needs.append(f"Open the report in Word and accept or reject the tracked change by {AUTHOR}; the receipt proves the change, not the wording.")
    if "comment_added" in kinds:
        needs.append("Read the anchored comment in Word; it asks the client contact to confirm the closing line.")
    if "cell_written" in kinds:
        needs.append("Formula cells were not recalculated; D1 shows its saved cached value until Excel recalculates the workbook.")
    if "paragraph_written" in kinds and value.get("result", {}).get("engine") == "pptx-slides":
        needs.append("Open the deck and confirm the body text still fits its placeholder after the longer wording.")
    if "page_kept" in kinds:
        needs.append("The appendix copy is an appended revision of the original PDF; the original pages stay in the file's history and the current page tree holds only the kept page.")
    if any(kind.startswith("image_") for kind in kinds):
        needs.append("The thumbnail carries no EXIF and was flattened to 8-bit sRGB; check it against the original if color accuracy matters.")
    return needs


def run_job(args: argparse.Namespace) -> dict:
    requested = args.output.absolute()
    require(not path_exists(requested), "job output already exists; choose a new directory")
    require(requested.parent.is_dir(), "job output parent must already exist")
    output = requested.parent.resolve(strict=True) / requested.name
    require(not path_exists(output), "job output already exists; choose a new directory")
    launcher = args.ufo.resolve(strict=True) if args.ufo else None
    artifact = launcher_artifact(launcher) if launcher else container_artifact(
        args.container_image, docker_image_id(args.container_image, cwd=REPO),
    )
    started_wall = time.time()
    started = time.monotonic_ns()
    with tempfile.TemporaryDirectory(prefix=".ufo-job-", dir=output.parent) as raw:
        staging = Path(raw)
        bundle_dir, work, receipts = (staging / name for name in ("bundle", "work", "receipts"))
        for directory in (bundle_dir, work, receipts):
            directory.mkdir(mode=0o700)
        bundle = bundle_bytes()
        write_private_file(bundle_dir / "client-bundle.zip", bundle)
        bundle_identity = identity(bundle_dir / "client-bundle.zip")
        bundle_root = str(bundle_dir) if launcher else "/corpus"
        work_root = str(work) if launcher else "/output"
        steps: list[dict] = []
        outputs: list[dict] = []
        manifest: dict = {
            "job": JOB, "manifestVersion": MANIFEST_VERSION, "status": "incomplete",
            "engineVersion": args.expect_version, "artifact": artifact,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall)),
            "bundle": {"name": "client-bundle.zip", **bundle_identity},
            "inputs": {}, "steps": steps, "outputs": outputs, "reviewNeeds": [], "incomplete": None,
            "limitations": [
                "Synthetic inputs and expected results; not customer coverage, visual fidelity or a review of business facts.",
                "Receipt paths name the temporary job workspace; outputs are published under this directory.",
                "Office outputs still need a licensed Word, Excel or PowerPoint check for layout and tracked-change behavior.",
            ],
        }

        def run(step: str, arguments: list[str], contract: str, expected_exit: int = 0, source: Path | None = None) -> dict:
            """Run one command; a step that ends unexpectedly is still recorded, as failed, before the job stops."""
            step_started = time.monotonic_ns()
            try:
                return run_recorded(step, arguments, contract, expected_exit, source)
            except StepFailure as failure:
                steps.append({
                    "step": step, "action": arguments[0], "operation": arguments[1] if arguments[0] == "edit" else None,
                    "status": "failed", "exitCode": None, "elapsedMillis": (time.monotonic_ns() - step_started) // 1_000_000,
                    "receipt": f"receipts/{step}.json" if path_exists(receipts / f"{step}.json") else None,
                    "source": None, "output": None, "applied": [], "reviewNeeds": [], "reason": failure.reason,
                })
                raise

        def run_recorded(step: str, arguments: list[str], contract: str, expected_exit: int, source: Path | None) -> dict:
            cidfile = staging / f"{step}.cid"
            command = [str(launcher), *arguments] if launcher else [
                *restricted_docker_prefix(cidfile, bundle_dir, work, output_writable=True),
                "--pull=never", artifact["imageId"], *arguments,
            ]
            step_started = time.monotonic_ns()
            try:
                process = run_bounded_command(
                    command, cwd=REPO, timeout_seconds=120, label=f"job {step}",
                    max_stdout_bytes=1024 * 1024, max_stderr_bytes=65536,
                )
            finally:
                if not launcher:
                    remove_container(cidfile)
            elapsed = (time.monotonic_ns() - step_started) // 1_000_000
            if process.returncode != expected_exit:
                if process.stdout.strip():
                    write_private_file(receipts / f"{step}.json", process.stdout.encode("utf-8"))
                raise StepFailure(step, f"expected exit {expected_exit}, got {process.returncode}: {process.stderr.strip()[:300] or process.stdout.strip()[:300]}")
            if process.stderr:
                raise StepFailure(step, f"unexpected stderr: {process.stderr.strip()[:300]}")
            write_private_file(receipts / f"{step}.json", process.stdout.encode("utf-8"))
            try:
                value = receipt(process.stdout, f"com.krauq.ufo.{contract}", args.expect_version)
            except ValueError as error:
                raise StepFailure(step, str(error)) from error
            record = {
                "step": step, "action": value.get("action"), "operation": value.get("operation"),
                "status": value.get("status"), "exitCode": process.returncode, "elapsedMillis": elapsed,
                "receipt": f"receipts/{step}.json",
                "source": {"name": value["source"]["name"], "sha256": value["source"]["sha256"]} if isinstance(value.get("source"), dict) else None,
                "output": None, "applied": [item["kind"] for item in value.get("result", {}).get("applied", [])] if isinstance(value.get("result"), dict) else [],
                "reviewNeeds": review_needs(step, value),
            }
            if source is not None:
                try:
                    require_subset(value["source"], {**identity(source), "stableDuringProcessing": True}, step + ".source")
                except ValueError as error:
                    raise StepFailure(step, str(error)) from error
            published = value.get("output")
            if isinstance(published, dict) and published.get("sha256"):
                local = work / Path(published["path"]).relative_to(work_root)
                try:
                    require_subset(published, identity(local), step + ".output")
                except ValueError as error:
                    raise StepFailure(step, str(error)) from error
                record["output"] = {"path": str(Path("work") / local.relative_to(work)), "sizeBytes": published["sizeBytes"], "sha256": published["sha256"]}
                outputs.append({**record["output"], "step": step})
            steps.append(record)
            manifest["reviewNeeds"].extend(record["reviewNeeds"])
            return value

        try:
            # 1. Unpack the bundle into the job's private input directory.
            unpacked = run("unpack", ["unpack", "-o", f"{work_root}/inputs", f"{bundle_root}/client-bundle.zip"], "unpack-receipt", source=bundle_dir / "client-bundle.zip")
            if unpacked["status"] != "completed":
                raise StepFailure("unpack", unpacked.get("message") or "unpack did not complete")
            inputs = work / "inputs" / "client"
            listed = {Path(item["path"]).name: item for item in unpacked["result"]["outputs"]}
            for name in BUNDLE_FILES:
                require(name in listed, f"unpack did not publish {name}")
                require_subset(listed[name], identity(inputs / name), f"unpack.{name}")
                manifest["inputs"][name] = {"sizeBytes": listed[name]["sizeBytes"], "sha256": listed[name]["sha256"]}
            client = f"{work_root}/inputs/client"
            out = f"{work_root}/outputs"
            (work / "outputs").mkdir(mode=0o700)
            selected = run("unpack-status-only", ["unpack", "-o", f"{work_root}/status-only", "--only", "client/update.json", f"{bundle_root}/client-bundle.zip"], "unpack-receipt", source=bundle_dir / "client-bundle.zip")
            require(selected["status"] == "completed" and [item["path"] for item in selected["result"]["outputs"]] == ["client/update.json"], "the selective unpack did not publish exactly the status file")
            require(selected["result"]["selection"] == {"only": ["client/update.json"], "matchedEntries": 1}, "the selective unpack did not record its selection")

            # 2. Read only the blocks the update needs; every read carries the source hash.
            for name in BUNDLE_FILES:
                intake = run(f"intake-{name}", ["intake", f"{client}/{name}"], "intake-receipt", source=inputs / name)
                if intake["status"] != "accepted":
                    raise StepFailure(f"intake-{name}", intake.get("message") or "not accepted")
            report_hash = manifest["inputs"]["report.docx"]["sha256"]
            paragraphs = run("read-report", ["text", "--format", "structure", "--expect-sha256", report_hash, "-o", f"{out}/read-report.json", f"{client}/report.docx"], "text-receipt", source=inputs / "report.docx")
            report_page = json.loads(read_bounded(work / "outputs" / "read-report.json").decode("utf-8"))
            originals = [item["text"] for item in report_page["paragraphs"]]
            require(originals == SAMPLE_TEXT.split("\n"), "the report paragraphs are not the expected synthetic text")
            table_hash = manifest["inputs"]["data.xlsx"]["sha256"]
            run("read-table", ["text", "--format", "structure", "--sheet", "Summary", "--range", "A1:D1", "--expect-sha256", table_hash, "-o", f"{out}/read-table.json", f"{client}/data.xlsx"], "text-receipt", source=inputs / "data.xlsx")
            cells = json.loads(read_bounded(work / "outputs" / "read-table.json").decode("utf-8"))["selection"]["cells"]
            b1 = next(cell for cell in cells if cell["ref"] == "B1")
            require(b1["type"] == "number" and b1["value"] == "10", "B1 is not the expected number")
            deck_hash = manifest["inputs"]["deck.pptx"]["sha256"]
            run("read-deck", ["text", "--format", "structure", "--slide", "1", "--expect-sha256", deck_hash, "-o", f"{out}/read-deck.json", f"{client}/deck.pptx"], "text-receipt", source=inputs / "deck.pptx")
            appendix_hash = manifest["inputs"]["report.pdf"]["sha256"]
            run("read-appendix", ["text", "--format", "structure", "--pages", "3", "--expect-sha256", appendix_hash, "-o", f"{out}/read-appendix.json", f"{client}/report.pdf"], "text-receipt", source=inputs / "report.pdf")
            appendix_page = json.loads(read_bounded(work / "outputs" / "read-appendix.json").decode("utf-8"))["selection"]["items"][0]
            require(appendix_page["text"].strip() == PDF_PAGE_TEXTS[2], "page 3 of the appendix is not the expected text")
            status_hash = manifest["inputs"]["update.json"]["sha256"]

            # 3. One guarded edit per file, each publishing a new output without replacement.
            run("update-report", [
                "edit", "paragraphs", "--expect-sha256", report_hash, "--track", "--author", AUTHOR,
                "--expect", f"0={originals[0]}", "--set", f"0={REPORT_LINE}",
                "--expect", f"1={originals[1]}", "--comment", f"1={COMMENT}",
                "-o", f"{out}/report-updated.docx", f"{client}/report.docx",
            ], "edit-receipt", source=inputs / "report.docx")
            with zipfile.ZipFile(io.BytesIO(read_bounded(work / "outputs" / "report-updated.docx"))) as changed:
                body = ET.fromstring(changed.read("word/document.xml")).findall(".//w:p", {"w": W})
                require(resolved_text(body[0], W, accept=True) == REPORT_LINE and resolved_text(body[0], W, accept=False) == originals[0], "the tracked change does not resolve to the intended texts")
                require(body[1].find(".//w:commentRangeStart", {"w": W}) is not None, "the comment is not anchored on the closing line")
                require(COMMENT.encode("utf-8") in changed.read("word/comments.xml"), "the comment text is missing")
            run("update-table", [
                "edit", "cells", "--expect-sha256", table_hash, "--expect", "B1=number:10", "--set", "B1=number:12",
                "-o", f"{out}/data-updated.xlsx", f"{client}/data.xlsx",
            ], "edit-receipt", source=inputs / "data.xlsx")
            with zipfile.ZipFile(io.BytesIO(read_bounded(work / "outputs" / "data-updated.xlsx"))) as changed:
                sheet = changed.read("xl/worksheets/sheet1.xml")
                require(b'<c r="B1"><v>12</v></c>' in sheet and b"<f>B1*2</f>" in sheet, "the cell write did not keep the formula next to the new value")
            run("update-deck", [
                "edit", "paragraphs", "--expect-sha256", deck_hash, "--slide", "1",
                "--expect", "1=Revenue up 10%", "--set", "1=Revenue up 12%",
                "--expect", "notes:0=Mention the new client", "--set", "notes:0=Thank the new client",
                "-o", f"{out}/deck-updated.pptx", f"{client}/deck.pptx",
            ], "edit-receipt", source=inputs / "deck.pptx")
            run("keep-appendix", ["edit", "pages", "--keep", "3", "-o", f"{out}/appendix-3.pdf", f"{client}/report.pdf"], "edit-receipt", source=inputs / "report.pdf")
            current = pdf_current_page_texts(read_bounded(work / "outputs" / "appendix-3.pdf"))
            require(len(current) == 1 and PDF_PAGE_TEXTS[2].encode() in current[0], "the appendix copy does not hold page 3 alone")
            run("thumbnail", ["edit", "image", "--resize", "4x2", "-o", f"{out}/photo-thumb.png", f"{client}/photo.png"], "edit-receipt", source=inputs / "photo.png")
            require(decode_png(read_bounded(work / "outputs" / "photo-thumb.png"))[:2] == (4, 2), "the thumbnail is not 4x2")
            first_status = run("update-status", [
                "edit", "fields", "--expect-sha256", status_hash, "--expect", "/status=string:draft", "--set", "/status=string:final",
                "-o", f"{out}/update-final.json", f"{client}/update.json",
            ], "edit-receipt", source=inputs / "update.json")
            require(json.loads(read_bounded(work / "outputs" / "update-final.json"))["status"] == "final", "the status field was not written")

            # 4. A stale target refuses and publishes nothing; the retry reads the fresh hash and writes to a fresh path.
            stale = run("stale-status-retry", [
                "edit", "fields", "--expect-sha256", status_hash, "--expect", "/items/0/done=boolean:false", "--set", "/items/0/done=boolean:true",
                "-o", f"{out}/update-final-2.json", f"{out}/update-final.json",
            ], "edit-receipt", expected_exit=1, source=work / "outputs" / "update-final.json")
            require(stale["status"] == "refused" and stale["code"] == "source_revision_mismatch" and stale["output"] is None, "the stale hash was not refused as a source revision mismatch")
            require(not path_exists(work / "outputs" / "update-final-2.json"), "a refused edit published an output")
            fresh_hash = first_status["output"]["sha256"]
            run("fresh-status-retry", [
                "edit", "fields", "--expect-sha256", fresh_hash, "--expect", "/items/0/done=boolean:false", "--set", "/items/0/done=boolean:true",
                "-o", f"{out}/update-final-2.json", f"{out}/update-final.json",
            ], "edit-receipt", source=work / "outputs" / "update-final.json")
            final = json.loads(read_bounded(work / "outputs" / "update-final-2.json"))
            require(final["status"] == "final" and final["items"][0]["done"] is True, "the retry did not apply the second field write")
            manifest["status"] = "completed"
        except StepFailure as failure:
            manifest["incomplete"] = {"step": failure.step, "reason": failure.reason, "completedOutputs": [item["path"] for item in outputs]}
        except ValueError as error:
            step = steps[-1]["step"] if steps else "setup"
            manifest["incomplete"] = {"step": step, "reason": str(error), "completedOutputs": [item["path"] for item in outputs]}
        manifest["elapsedMillis"] = (time.monotonic_ns() - started) // 1_000_000
        manifest["stepCount"] = len(steps)
        encoded = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        write_private_file(staging / "job-manifest.json", encoded)
        # The identity of the published manifest file, returned to the caller and printed, never stored inside itself.
        manifest["manifestSha256"] = hashlib.sha256(encoded).hexdigest()
        # 5. Publish the whole job directory once, even when incomplete, so the manifest and every completed output are reviewable.
        publish_directory_noreplace(staging, output)
    return manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    surface = result.add_mutually_exclusive_group(required=True)
    surface.add_argument("--ufo", type=Path, help="packaged Linux app-image bin/ufo launcher")
    surface.add_argument("--container-image", help="existing local image; never pulled")
    result.add_argument("--expect-version", required=True)
    result.add_argument("--output", type=Path, required=True, help="new job directory; parent must exist")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = run_job(args)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Job failed before it could be recorded: {error}", file=sys.stderr)
        return 1
    if manifest["status"] != "completed":
        print(f"INCOMPLETE at {manifest['incomplete']['step']}: {manifest['incomplete']['reason']}. Manifest: {args.output}/job-manifest.json", file=sys.stderr)
        return 1
    print(f"PASS: {manifest['stepCount']} steps, {len(manifest['outputs'])} outputs, {len(manifest['reviewNeeds'])} review needs in {manifest['elapsedMillis']} ms. Manifest: {args.output}/job-manifest.json sha256 {manifest['manifestSha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
