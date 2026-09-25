#!/usr/bin/env python3
"""Evaluate a packaged UFO build on files the evaluator already owns.

The recipe reads each file, records what UFO could address and what it refused
and why, plans one reversible, content preserving edit per family, runs it into
a new output directory, and checks the result with its own ZIP, XML, PDF and
byte readers instead of asking UFO whether its edit worked. When a free
incumbent is installed it attempts the same edits with that tool through the
adapters in tools/corpus/compare_agent_tools.py and reports both sides with the
same oracle. Inputs are copied before anything runs and are never modified. No
network is used and nothing is ever overwritten.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.audit.create_compatibility_audit import (
    path_exists, publish_directory_noreplace, sha256_file, write_private_file,
)
from tools.cli.artifact_identity import container_artifact, launcher_artifact
from tools.cli.bounded_process import docker_image_id, run_bounded_command
from tools.cli.evaluate_sample import pdf_current_page_texts, remove_container, require
from tools.corpus import package_semantics
from tools.corpus.evaluate_edit_corpus import restricted_docker_prefix

RECIPE = "own-file-evaluation"
REPORT_VERSION = 1
MAX_FILES = 200
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_JSON_PARSE_BYTES = 8 * 1024 * 1024
MAX_PLAN_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024

AUTHOR = "UFO evaluation"
MARKER = "[UFO evaluation]"
MARKER_SUFFIX = " " + MARKER
COMMENT_TEXT = "UFO evaluation: an anchored comment, added so a reviewer can see comment authoring in Word."
INSERTED_TEXT = "Inserted by the UFO evaluation run."
APPENDED_ROW_TEXT = "UFO evaluation marker row"
MARKER_FORMULA = "=1+1"

FAMILY_BY_EXTENSION = {
    ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".pdf": "pdf",
    ".md": "text", ".txt": "text", ".json": "json", ".csv": "delimited",
}
STRUCTURE_CONTRACT = {
    "docx": "com.krauq.ufo.docx-paragraphs", "xlsx": "com.krauq.ufo.xlsx-cells",
    "pptx": "com.krauq.ufo.pptx-slides", "pdf": "com.krauq.ufo.pdf-pages",
    "text": "com.krauq.ufo.text-lines", "json": "com.krauq.ufo.text-lines",
}
NO_STRUCTURE_READ = (
    "this build's structured reader and find cover .docx, .xlsx, .pptx, .pdf and plain text or "
    "Markdown files; a delimited file is addressed by row and column through edit fields instead"
)
RESULTS = ("pass", "refused", "failed", "wrong", "unsupported")
NOT_PROVEN = (
    "This is one local run on the files it was given. It is not a survey of the evaluator's whole document set.",
    "No output was opened in Word, Excel, PowerPoint or a PDF reader here. Visual fidelity and native review behavior still need a human check in those applications.",
    "The edits are generic markers chosen by the runner, not the evaluator's real job. Timing, correction effort and agent token cost are not measured.",
    "A passing preservation verdict means untouched package members kept their exact bytes and only the parts the receipt accounts for changed. The one exception is a provenance or save stamp: for UFO only the members its receipt's stamp names, and only when the change to them is a stamp and nothing more; for an incumbent, which publishes no receipt, every change the stamp filter confirms is a stamp and nothing more, each one named. It is not a guarantee about every producer, every feature or every future file.",
    "An incumbent result is this adapter's attempt at the equivalent operation with that tool's documented commands. A tool marked unsupported here may still cover the job another way.",
    "This recipe's independent PDF reader follows classic cross-reference tables. A PDF built with cross-reference streams or object streams is reported as not checked rather than judged, and its output still needs a reader.",
    "Refusals are the tool declining work it will not do safely. They are not defects, and they are not evidence that another tool would have been wrong to proceed.",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def read_file(path: Path, maximum: int = MAX_FILE_BYTES) -> bytes:
    """Read a regular file without following a final symlink, refusing anything over the bound."""
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), f"not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        require(stat.S_ISREG(opened.st_mode) and (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino),
                f"file changed before it was read: {path}")
        value = handle.read(maximum + 1)
    require(len(value) <= maximum, f"file exceeds the {maximum} byte evaluation limit: {path}")
    return value


def file_identity(path: Path) -> dict:
    details = path.lstat()
    return {"sizeBytes": details.st_size, "sha256": sha256_file(path, maximum=MAX_FILE_BYTES, expected=details)}


def collect_files(entries: list[Path]) -> list[Path]:
    """Every named file and every regular file under a named directory, sorted, symlinks refused."""
    found: list[Path] = []
    for entry in entries:
        require(path_exists(entry), f"input does not exist: {entry}")
        require(not entry.is_symlink(), f"symlinks are refused: {entry}")
        if entry.is_dir():
            for child in sorted(entry.rglob("*")):
                require(not child.is_symlink(), f"symlinks are refused: {child}")
                if child.is_file():
                    found.append(child)
        else:
            require(entry.is_file(), f"not a regular file: {entry}")
            found.append(entry)
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in found:
        resolved = path.absolute()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    require(ordered, "no files were given")
    require(len(ordered) <= MAX_FILES, f"{len(ordered)} files exceed the {MAX_FILES} file evaluation limit")
    total = 0
    for path in ordered:
        size = path.lstat().st_size
        require(size <= MAX_FILE_BYTES, f"file exceeds the {MAX_FILE_BYTES} byte evaluation limit: {path}")
        total += size
    require(total <= MAX_TOTAL_BYTES, f"the inputs total {total} bytes, over the {MAX_TOTAL_BYTES} byte evaluation limit")
    return ordered


def slug_for(index: int, path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem)[:48].strip("-.") or "file"
    return f"{index:03d}-{stem}"


def choose_needle(texts: list[str]) -> str | None:
    """A neutral word the file already holds: an ordinary looking word, the rarest one first.

    Rarity makes the `find` result easy to read. The case filter keeps the recipe away from the
    glyph soup a damaged text layer can produce, which would prove nothing about the search.
    """
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for position, text in enumerate(texts):
        for match in re.finditer(r"[A-Za-z][A-Za-z'-]{3,15}", text or ""):
            word = match.group(0)
            counts[word] = counts.get(word, 0) + 1
            order.setdefault(word, position * 100000 + match.start())
    if not counts:
        return None
    ordinary = [word for word in counts if word.islower() or word.istitle()]
    return min(ordinary or list(counts), key=lambda word: (counts[word], -len(word), order[word]))


# ---------------------------------------------------------------- oracles


UNICODE_PATH_EXTRA_ID = 0x7075
UTF8_NAME_FLAG = 0x800


def declared_member_name(info: zipfile.ZipInfo) -> str | None:
    """The name this member's Info-ZIP Unicode path extra field declares, or None.

    A reader substitutes that name for the one the ZIP stores, so it is a name the member
    answers to. The field counts only when it parses as version 1 and its name CRC-32 covers the
    bytes the member really stores for its name, which is the rule the extra field carries for
    exactly this reason. Read here rather than taken from `ZipInfo.filename`, so this recipe
    judges a package the same way on every Python: 3.12 substitutes the declared name and earlier
    versions do not.
    """
    stored = info.orig_filename.encode("utf-8" if info.flag_bits & UTF8_NAME_FLAG else "cp437", "replace")
    extra, at = info.extra, 0
    while at + 4 <= len(extra):
        tag, size = struct.unpack("<HH", extra[at:at + 4])
        body = extra[at + 4:at + 4 + size]
        at += 4 + size
        if tag != UNICODE_PATH_EXTRA_ID or len(body) < 5:
            continue
        version, name_crc = struct.unpack("<BL", body[:5])
        if version != 1 or name_crc != zlib.crc32(stored):
            continue
        try:
            declared = body[5:].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if declared:
            return declared
    return None


def package_members(package: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Every member of an open package, refused when a name it answers to names two members.

    A member is identified by the name the ZIP STORES for it (`orig_filename`), because that is
    the name OPC, ODF and EPUB resolve a part by, and the name the receipts and this recipe's
    write sets are written in. A member may also answer to a name an Info-ZIP Unicode path extra
    field declares, which readers substitute (Python does from 3.12 on); in a package whose
    entries declare each other's names that points one name at a different member, so a
    judgement taken from it would compare one member of the source with another of the output.

    Both readings still have to name one member each. A package where either repeats a name
    cannot be opened by every reader, so it fails here: that refusal is what caught a rewrite
    leaving `word/document.xml` in the output twice.
    """
    infos = [info for info in package.infolist() if not info.is_dir()]
    for reading in (lambda info: info.orig_filename,
                    lambda info: declared_member_name(info) or info.orig_filename):
        require(len({reading(info) for info in infos}) == len(infos), "the package has duplicate members")
    return infos


def package_parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return {info.orig_filename: package.read(info) for info in package_members(package)}


def package_member_names(data: bytes) -> list[str]:
    """Every member's name, without reading a single member's bytes.

    A stored CRC-32 that is wrong makes Python refuse to READ that member, which says nothing
    about the names a package declares. A check that only needs the names asks for the names.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return [info.orig_filename for info in package_members(package)]


PART_IN_DETAIL = re.compile(r"\b(?:xl|word|ppt|customXml|docProps)/[A-Za-z0-9_./-]+\.(?:xml|rels|bin)\b")

# The comment and note parts a DOCX paragraph batch may write beside the paragraph part: the
# comment body, the modern thread, id, extensible and people parts Word has written since 2016,
# and the footnote part a `--footnote` writes (docs/cli/CLI-WORD.md, "Comments work on the package
# Word has written since 2016").
DOCX_NOTE_PARTS = {
    "word/comments.xml", "word/commentsExtended.xml", "word/commentsIds.xml",
    "word/commentsExtensible.xml", "word/people.xml", "word/footnotes.xml",
}


# The relationship types whose target belongs to the one slide that references it. PowerPoint's
# own Duplicate Slide writes a new part for each of these and shares everything else, so a copy
# owns its charts, SmartArt, embeddings, VML, tags, comments and speaker notes
# (docs/cli/CLI-DECK.md, "PPTX slide duplicates and deletes"). This recipe re-derives that closure from
# the produced package instead of trusting the receipt's own list.
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
PPTX_PRIVATE_REL_TYPES = {
    OFFICE_REL + name for name in (
        "notesSlide", "chart", "chartUserShapes", "themeOverride", "diagramData", "diagramLayout",
        "diagramColors", "diagramQuickStyle", "oleObject", "package", "vmlDrawing", "tags",
        "comments", "control",
    )
} | {
    "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing",
    "http://schemas.microsoft.com/office/2011/relationships/chartColorStyle",
    "http://schemas.microsoft.com/office/2011/relationships/chartStyle",
    "http://schemas.microsoft.com/office/2014/relationships/chartEx",
    "http://schemas.microsoft.com/office/2006/relationships/activeXControlBinary",
    "http://schemas.microsoft.com/office/2018/10/relationships/comments",
}

RELATIONSHIP_IN_RELS = re.compile(rb"<Relationship\b[^>]*>")
ATTRIBUTE_IN_RELS = re.compile(rb'(Type|Target|TargetMode)="([^"]*)"')


def relationships_path(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"


def resolve_target(part: str, target: str) -> str | None:
    """A relationship target as a package member name, or None when it names no member."""
    if target.startswith("#") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
        return None
    base = "" if target.startswith("/") else part.rpartition("/")[0]
    segments: list[str] = []
    for piece in f"{base}/{target.lstrip('/')}".replace("\\", "/").split("/"):
        if not piece or piece == ".":
            continue
        if piece == "..":
            if segments:
                segments.pop()
            continue
        segments.append(piece)
    return "/".join(segments) or None


def pptx_slide_owned_parts(package: dict[str, bytes], slides: set[str]) -> set[str]:
    """Every part [slides] own in [package]: each slide, each part reached from it by a
    relationship type private to a slide, and the `_rels` part of each of those."""
    owned: set[str] = set()
    pending = [name for name in sorted(slides) if name in package]
    seen = set(pending)
    while pending:
        part = pending.pop()
        owned.add(part)
        rels = relationships_path(part)
        if rels not in package:
            continue
        owned.add(rels)
        for match in RELATIONSHIP_IN_RELS.findall(package[rels]):
            fields = {key.decode(): value.decode() for key, value in ATTRIBUTE_IN_RELS.findall(match)}
            if fields.get("TargetMode") == "External" or fields.get("Type") not in PPTX_PRIVATE_REL_TYPES:
                continue
            target = resolve_target(part, fields.get("Target", ""))
            if target and target in package and target not in seen:
                seen.add(target)
                pending.append(target)
    return owned


def declared_parts(receipt: dict | None) -> set[str]:
    """Every package member the receipt names in its own change rows.

    The receipt is the operation's declaration of what it wrote: `edit rows` names the table
    part it grew or moved (`Table1 (xl/tables/table1.xml) extended to A1:D3`). The oracle
    permits a member outside the fixed write set only when the receipt named it, so a rewrite
    the tool did not declare is still reported.
    """
    applied = ((receipt or {}).get("result") or {}).get("applied") or []
    named: set[str] = set()
    for row in applied:
        named |= set(PART_IN_DETAIL.findall(row.get("detail") or ""))
    return named


def allowed_parts(engine: str, kinds: list[str], hints: dict, before: dict, after: dict,
                  declared: set[str] | None = None) -> dict:
    """The OPC members each engine is permitted to write, by the operation the receipt names."""
    part = hints.get("part")
    notes = hints.get("notesPart")
    changed: set[str] = set()
    added: set[str] = set()
    removed: set[str] = set()
    pattern: str | None = None
    if engine == "docx-paragraphs":
        changed = {part or "word/document.xml"}
        if any(kind.startswith(("comment_", "link_", "footnote_")) for kind in kinds):
            changed |= {"[Content_Types].xml", "word/_rels/document.xml.rels"}
            added = DOCX_NOTE_PARTS | {"word/_rels/document.xml.rels"}
            # A comment or a reply also extends the comment parts the document ALREADY carried,
            # and the receipt names every member it wrote (docs/cli/CLI-WORD.md, "--comment"). Only the
            # parts the receipt named, and only the ones the source already has: a comment part a
            # receipt is silent about, or any other member, stays undeclared and is reported.
            changed |= {name for name in (declared or set()) if name in added and name in before}
    elif engine in ("xlsx-cells", "xlsx-rows"):
        changed = {part or "xl/worksheets/sheet1.xml", "xl/workbook.xml", "xl/sharedStrings.xml"}
        added = {"xl/sharedStrings.xml"}
        removed = {"xl/calcChain.xml"}
        if "xl/calcChain.xml" in before and "xl/calcChain.xml" not in after:
            changed |= {"[Content_Types].xml", "xl/_rels/workbook.xml.rels"}
        if engine == "xlsx-cells":
            # Writing a table's header cell renames that column, so the table part takes the new
            # name and every structured reference that resolved through the old one follows it, on
            # this worksheet and on any other; a write also rewrites any worksheet whose saved
            # formula results it outdated (docs/cli/CLI-WORKBOOK.md, "Writing a table header cell" and "What
            # a write does to the results other cells saved").
            # Only the parts the receipt named, and only members the source already carries: a
            # table or worksheet the receipt is silent about stays undeclared and is reported.
            changed |= {name for name in (declared or set())
                        if (name.startswith("xl/tables/") or name.startswith("xl/worksheets/"))
                        and name.endswith(".xml") and name in before}
        if engine == "xlsx-rows":
            # A row operation writes a worksheet table when it grows over the appended rows or
            # moves with a shift, and it rewrites any other worksheet whose saved formula results
            # the new cells outdated; the receipt names both (docs/cli/CLI-WORKBOOK.md, "Guarded XLSX row
            # appends" and "Inserting and deleting XLSX rows in the middle"). Only the parts the
            # receipt named, and only members the source already carries: a table or worksheet the
            # receipt is silent about stays undeclared, and every other member must stay identical.
            changed |= {name for name in (declared or set())
                        if (name.startswith("xl/tables/") or name.startswith("xl/worksheets/"))
                        and name.endswith(".xml") and name in before}
    elif engine == "pptx-slides":
        changed = {name for name in (part, notes) if name}
    elif engine == "pptx-deck":
        changed = {"[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"}
        pattern = r"ppt/(slides|notesSlides)/(_rels/)?(slide|notesSlide)\d+\.xml(\.rels)?"
        # A duplicate may add the copy's own parts and nothing else. This recipe walks the copied
        # slide's private relationship closure in the PRODUCED package and permits exactly the
        # members of that closure the source did not already carry, so a part the tool copied
        # without wiring it to the copy, or one it added for no slide at all, is still reported.
        copies = {name for name in (declared or set()) if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)}
        added = {name for name in pptx_slide_owned_parts(after, copies) if name not in before}
    else:
        return {}
    return {"changed": changed, "added": added, "removed": removed, "pattern": pattern}


def stamp_problems(source: bytes, first: dict[str, bytes], second: dict[str, bytes], stamp: dict) -> list[str]:
    """What a receipt's declared stamp claims that the output does not bear out.

    The receipt names the properties it wrote; the output's custom-properties part must carry
    exactly those UFO properties, and `UFO.SourceSha256` must be the hash of the source this
    recipe handed the build, computed here rather than taken from the receipt.
    """
    declared = {str(key): str(value) for key, value in (stamp.get("properties") or {}).items()}
    carried = {key: value for key, value in package_semantics.tool_properties(second).items() if key.startswith("UFO.")}
    problems = []
    if declared != carried:
        problems.append(f"the receipt's stamp properties {sorted(declared)} are not the UFO properties the output "
                        f"carries {sorted(carried)} with the same values")
    if "UFO.SourceSha256" in declared and declared["UFO.SourceSha256"] != sha(source):
        problems.append("the stamp's UFO.SourceSha256 is not the hash of the source this edit was given")
    return problems


def package_preservation(before: bytes, after: bytes, allowed: dict, stamp: dict | None = None,
                         receiptless: bool = False) -> dict:
    """Untouched members byte-identical; only the members the receipt accounts for may move.

    [stamp] is the receipt's own `result.stamp`. The members it names are set aside only when
    the change to them is a provenance stamp and nothing more; an undeclared stamp, and any other
    change a receipt tries to pass off as one, stays for the byte comparison to report. A
    [receiptless] tool has no receipt to declare a stamp in, so every change the stamp filter
    confirms is a stamp and nothing more is set aside for it and named, as the comparison
    runner does for every tool.
    """
    try:
        first = package_parts(before)
    except (zipfile.BadZipFile, ValueError) as error:
        return {"verdict": "not checked",
                "reason": f"the source package could not be read by this recipe: {error}"}
    try:
        second = package_parts(after)
    except (zipfile.BadZipFile, ValueError) as error:
        return {"verdict": "fail", "problems": [f"the produced package could not be read: {error}"]}
    produced = second
    if receiptless:
        declared_stamp = [row["member"] for row in package_semantics.stamp_differences(first, produced)[1]]
    else:
        declared_stamp = package_semantics.receipt_stamp_parts({"result": {"stamp": stamp}})
    second, admitted = package_semantics.admit_stamps(first, second, declared_stamp)
    notes = [f"a stamp this run did not declare stays a change: {row['description']}"
             for row in package_semantics.stamp_differences(first, produced)[1]
             if row["member"].lower() not in {name.lower() for name in declared_stamp}]
    stamp_checks = stamp_problems(before, first, produced, stamp) if isinstance(stamp, dict) else []
    changed = sorted(name for name in first.keys() & second.keys() if first[name] != second[name])
    added = sorted(second.keys() - first.keys())
    removed = sorted(first.keys() - second.keys())
    identical = sorted(name for name in first.keys() & second.keys() if first[name] == second[name])
    if not allowed:
        return {"verdict": "not checked", "reason": "this operation has no declared package write set in this recipe",
                "changedMembers": changed, "addedMembers": added, "removedMembers": removed,
                "byteIdenticalMembers": len(identical), "stampSetAside": admitted, "notes": notes}
    pattern = allowed.get("pattern")

    def permitted(name: str, listed: set[str]) -> bool:
        return name in listed or (pattern is not None and re.fullmatch(pattern, name) is not None)

    problems = [f"member changed outside the write set this operation declares: {name}" for name in changed
                if not permitted(name, allowed["changed"])]
    # An ADDED member is judged by the declared set alone. A pattern over part names would permit
    # any `slideN.xml`, including one no slide of the produced deck owns.
    problems += [f"member added outside the write set this operation declares: {name}" for name in added
                 if name not in allowed["added"]]
    problems += [f"member removed outside the write set this operation declares: {name}" for name in removed
                 if not permitted(name, allowed["removed"])]
    problems += stamp_checks
    return {
        "verdict": "pass" if not problems else "fail", "problems": problems,
        "changedMembers": changed, "addedMembers": added, "removedMembers": removed,
        "byteIdenticalMembers": len(identical),
        "allowedChanged": sorted(allowed["changed"]), "allowedAdded": sorted(allowed["added"]),
        "allowedRemoved": sorted(allowed["removed"]),
        "stampSetAside": admitted, "notes": notes,
    }


def pdf_preservation(before: bytes, after: bytes, expected_pages: list[int] | None,
                     appended: bool = False) -> dict:
    """The newest page tree holds what was asked for, with the untouched content streams unchanged.

    Only the annotation writer promises an appended revision, so `appended` asks for that;
    for the page operations the retained-bytes fact is reported without being a verdict.
    """
    result: dict = {"verdict": "pass", "problems": [], "notes": [],
                    "sourceBytesRetained": after.startswith(before)}
    if appended and not result["sourceBytesRetained"]:
        result["problems"].append("the output is not an appended revision of the source, which this "
                                  "operation documents")
    elif not result["sourceBytesRetained"]:
        result["notes"].append("this output is a rewritten file, not an appended revision; the page "
                               "operations do not promise one")
    try:
        original_pages = pdf_current_page_texts(before)
        produced_pages = pdf_current_page_texts(after)
    except (ValueError, IndexError, KeyError, re.error) as error:
        result["independentPageCheck"] = f"unavailable for this PDF structure: {error}"
        if not result["problems"]:
            result["verdict"] = "not checked"
            result["reason"] = ("this recipe's independent PDF reader follows classic cross-reference "
                                f"tables only, so it could not judge this output: {error}")
            return result
    else:
        expected = original_pages if expected_pages is None else [original_pages[number - 1] for number in expected_pages]
        result["independentPageCheck"] = "classic xref page tree, content streams compared byte for byte"
        result["pageCountBefore"] = len(original_pages)
        result["pageCountAfter"] = len(produced_pages)
        if produced_pages != expected:
            result["problems"].append("the newest page tree does not hold exactly the requested pages, unchanged")
    if result["problems"]:
        result["verdict"] = "fail"
    return result


def split_lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def line_preservation(before: bytes, after: bytes, numbers: set[int]) -> dict:
    """Every byte outside the addressed lines, the endings included, stays identical."""
    first, second = split_lines(before), split_lines(after)
    problems = []
    if len(first) != len(second):
        problems.append(f"the line count changed from {len(first)} to {len(second)}")
    differing = [index + 1 for index in range(min(len(first), len(second))) if first[index] != second[index]]
    unexpected = sorted(set(differing) - numbers)
    if unexpected:
        problems.append(f"lines changed outside the edit: {unexpected}")
    return {
        "verdict": "pass" if not problems else "fail", "problems": problems,
        "lineCountBefore": len(first), "lineCountAfter": len(second),
        "changedLines": differing, "allowedLines": sorted(numbers),
    }


def json_pointer_get(value: object, pointer: str) -> object:
    current = value
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        else:
            current = current[token]
    return current


def json_field_preservation(before: bytes, after: bytes, pointer: str, expected: str) -> dict:
    """Only the addressed scalar differs, and the rest of the document decodes identically."""
    problems = []
    try:
        first = json.loads(before.decode("utf-8"))
        second = json.loads(after.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        return {"verdict": "fail", "problems": [f"the produced JSON could not be decoded: {error}"]}
    try:
        written = json_pointer_get(second, pointer)
    except (KeyError, IndexError, ValueError) as error:
        return {"verdict": "fail", "problems": [f"the addressed pointer is gone from the output: {error}"]}
    if written != expected:
        problems.append(f"the addressed field holds {written!r}, not the requested value")
    scrubbed_before = json.loads(before.decode("utf-8"))
    scrubbed_after = json.loads(after.decode("utf-8"))
    for document in (scrubbed_before, scrubbed_after):
        container = json_pointer_get(document, pointer.rsplit("/", 1)[0]) if "/" in pointer[1:] else document
        key = pointer.rsplit("/", 1)[1].replace("~1", "/").replace("~0", "~")
        if isinstance(container, list):
            container[int(key)] = None
        else:
            container[key] = None
    if scrubbed_before != scrubbed_after:
        problems.append("a value outside the addressed field changed")
    before_lines, after_lines = len(split_lines(before)), len(split_lines(after))
    notes = []
    if before_lines != after_lines:
        notes.append("the JSON document was rewritten with the documented indentation, so line numbers moved; "
                     "every decoded value outside the addressed field is unchanged")
    return {
        "verdict": "pass" if not problems else "fail", "problems": problems, "notes": notes,
        "lineCountBefore": before_lines, "lineCountAfter": after_lines,
    }


def delimited_rows(data: bytes, extension: str) -> tuple[list[list[str]], str]:
    """RFC 4180 rows read here, by this recipe, so the oracle never asks UFO about its own edit."""
    text = data.decode("utf-8")
    delimiter = "\t" if extension == ".tsv" else ","
    if extension != ".tsv":
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)), delimiter


def delimited_field_preservation(before: bytes, after: bytes, extension: str,
                                 row: int, column: int, expected: str) -> dict:
    """Only the addressed field differs; every other field and the line count stay as they were."""
    problems = []
    try:
        first, _ = delimited_rows(before, extension)
        second, _ = delimited_rows(after, extension)
    except (ValueError, UnicodeDecodeError) as error:
        return {"verdict": "fail", "problems": [f"the produced delimited file could not be read: {error}"]}
    if len(first) != len(second):
        problems.append(f"the row count changed from {len(first)} to {len(second)}")
    differing = []
    for index in range(min(len(first), len(second))):
        if len(first[index]) != len(second[index]):
            problems.append(f"row {index + 1} changed its field count")
            continue
        for position in range(len(first[index])):
            if first[index][position] != second[index][position]:
                differing.append((index + 1, position + 1))
    if differing != [(row, column)]:
        problems.append(f"fields changed outside the edit: {differing}")
    elif second[row - 1][column - 1] != expected:
        problems.append("the addressed field does not hold the requested value")
    line_check = line_preservation(before, after, set(range(1, len(split_lines(after)) + 1)))
    return {"verdict": "pass" if not problems else "fail", "problems": problems,
            "changedFields": [f"R{r}C{c}" for r, c in differing],
            "lineCountBefore": line_check["lineCountBefore"], "lineCountAfter": line_check["lineCountAfter"],
            "changedLines": line_check["changedLines"]}


def delimited_diff(before: bytes, after: bytes, extension: str) -> list[str]:
    try:
        first, _ = delimited_rows(before, extension)
        second, _ = delimited_rows(after, extension)
    except (ValueError, UnicodeDecodeError) as error:
        return [f"the delimited file could not be read after the edit: {error}"]
    lines = []
    if len(first) != len(second):
        lines.append(f"rows {len(first)} to {len(second)}")
    for index in range(min(len(first), len(second))):
        for position in range(min(len(first[index]), len(second[index]))):
            if first[index][position] != second[index][position]:
                lines.append(f"field R{index + 1}C{position + 1}: "
                             f"{first[index][position]!r} to {second[index][position]!r}")
    return lines or ["no difference in the delimited fields"]


# ---------------------------------------------------------------- structure reading


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def docx_rows(document: dict) -> list[dict]:
    return document.get("paragraphs") or []


def structure_summary(family: str, document: dict) -> dict:
    """What the read found, what is addressable and what is not, with the reader's own reasons."""
    counts: dict[str, int | str] = {}
    editable: list[str] = []
    blocked: list[dict] = []
    if family == "docx":
        rows = docx_rows(document)
        plain = [row for row in rows if row.get("limitation") is None and row.get("text") is not None]
        counts = {
            "paragraphsOnPage": len(rows), "totalParagraphs": document.get("totalParagraphs", len(rows)),
            "plainParagraphs": len(plain), "paragraphsInTables": sum(1 for row in rows if row.get("inTable")),
            "outlineHeadings": len(document.get("outline") or []), "comments": len(document.get("comments") or []),
            "footnotes": len(document.get("footnotes") or []), "pictures": len(document.get("pictures") or []),
            "charts": len(document.get("charts") or []), "links": sum(len(row.get("links") or []) for row in rows),
        }
        editable = [f"{plural(len(plain), 'plain paragraph')} on this page can be rewritten, "
                    "commented or used as an insert anchor"]
        reasons: dict[str, int] = {}
        for row in rows:
            if row.get("limitation"):
                reasons[row["limitation"]] = reasons.get(row["limitation"], 0) + 1
        blocked = [{"item": plural(count, "paragraph"), "reason": reason} for reason, count in sorted(reasons.items())]
    elif family == "xlsx":
        sheets = document.get("sheets") or []
        selection = document.get("selection") or {}
        cells = selection.get("cells") or []
        by_type: dict[str, int] = {}
        for cell in cells:
            by_type[cell["type"]] = by_type.get(cell["type"], 0) + 1
        counts = {
            "worksheets": len(sheets), "charts": sum(len(sheet.get("charts") or []) for sheet in sheets),
            "dataValidations": sum(len(sheet.get("validations") or []) for sheet in sheets),
            "conditionalFormats": sum(len(sheet.get("conditionalFormats") or []) for sheet in sheets),
            "cellsRead": len(cells), **{f"cells.{name}": value for name, value in sorted(by_type.items())},
        }
        literals = by_type.get("string", 0) + by_type.get("number", 0) + by_type.get("boolean", 0)
        editable = [f"{plural(literals, 'literal cell')} in the read range can be rewritten with a typed value",
                    "new rows can be appended after the last used row"]
        if by_type.get("formula"):
            blocked.append({"item": plural(by_type["formula"], "formula cell"),
                            "reason": "a formula is preserved, not overwritten, unless its own formula text is named as the preimage"})
        for cell in cells:
            if cell.get("limitation"):
                blocked.append({"item": f"cell {cell['ref']}", "reason": cell["limitation"]})
    elif family == "pptx":
        slides = document.get("slides") or []
        selection = document.get("selection") or {}
        shapes = selection.get("shapes") or []
        other = selection.get("other") or []
        notes = (selection.get("notes") or {}).get("paragraphs") or []
        counts = {
            "slides": len(slides), "plainTextShapesOnSlide": len(shapes),
            "paragraphsOnSlide": sum(len(shape.get("paragraphs") or []) for shape in shapes),
            "speakerNotesParagraphs": len(notes), "pictures": len(selection.get("pictures") or []),
            "charts": len(selection.get("charts") or []), "tables": len(selection.get("tables") or []),
        }
        editable = [f"{plural(sum(len(shape.get('paragraphs') or []) for shape in shapes), 'paragraph')} "
                    "in plain text shapes on the read slide can be rewritten",
                    "whole slides can be duplicated or deleted"]
        reasons = {}
        for gap in other:
            reason = gap.get("limitation") or "not_a_plain_text_shape"
            reasons[reason] = reasons.get(reason, 0) + 1
        blocked = [{"item": plural(count, "paragraph"), "reason": reason} for reason, count in sorted(reasons.items())]
    elif family == "pdf":
        pages = document.get("pages") or []
        selection = document.get("selection") or {}
        items = selection.get("items") or []
        paragraphs = [row for item in items for row in (item.get("paragraphs") or [])]
        counts = {
            "pages": document.get("pageCount", len(pages)),
            "textlessPages": sum(1 for page in pages if page.get("textless")),
            "paragraphsOnReadPage": len(paragraphs),
            "editableParagraphsOnReadPage": sum(1 for row in paragraphs if row.get("editable")),
        }
        editable = ["pages can be kept or reordered into a new copy, and text found on a page can be highlighted"]
        reasons = {}
        for row in paragraphs:
            if not row.get("editable"):
                reason = row.get("limitation") or "the in-place text editor declined this block"
                reasons[reason] = reasons.get(reason, 0) + 1
        blocked = [{"item": plural(count, "page paragraph"), "reason": reason} for reason, count in sorted(reasons.items())]
    elif family == "delimited":
        rows = document.get("rows") or []
        counts = {"rows": len(rows), "columns": max((len(row) for row in rows), default=0),
                  "delimiter": document.get("delimiter", ",")}
        editable = ["single fields can be rewritten by row and column with edit fields"]
        blocked = [{"item": "rows and columns", "reason": "a delimited field write never adds or removes rows or columns"},
                   {"item": "structured read and find", "reason": NO_STRUCTURE_READ}]
    else:
        selection = document.get("selection") or {}
        items = selection.get("items") or []
        counts = {
            "lines": document.get("lineCount", 0), "linesRead": len(items),
            "encoding": document.get("encoding", "unknown"), "lineEnding": document.get("lineEnding", "unknown"),
        }
        editable = ["whole lines that were read can be rewritten in place"]
        blocked = [{"item": "line insertion and deletion", "reason": "a line write never inserts, deletes or renumbers lines"}]
    return {"counts": counts, "editable": editable, "nonEditable": blocked}


# ---------------------------------------------------------------- the automatic edit plan


PDFTOTEXT_TEXT_PAGE_CHARS = 20
PDFTOTEXT_SHORTFALL_CHARS = 200
PDFTOTEXT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def independent_pdf_pages(path: Path) -> list[int] | None:
    """Non-space characters per page as Poppler's pdftotext reads them; None when it is not installed.

    The structured read cannot be its own oracle: a page UFO decodes wrongly reads as textless in
    UFO's report and in every check that trusts it, which is how a decoding defect once scored
    "wrong 0" here while a whole page of a digital PDF was missing.
    """
    binary = shutil.which("pdftotext")
    if binary is None:
        return None
    try:
        completed = run_bounded_command([binary, "-enc", "UTF-8", str(path), "-"], cwd=path.parent,
                                        timeout_seconds=120, label="pdftotext",
                                        max_stdout_bytes=PDFTOTEXT_MAX_OUTPUT_BYTES)
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    # pdftotext ends every page, the last one included, with a form feed.
    pages = completed.stdout.split("\f")
    if completed.stdout.endswith("\f"):
        pages = pages[:-1]
    return [sum(1 for character in page if not character.isspace()) for page in pages]


def pdf_text_oracle(path: Path, document: dict) -> dict:
    """Compare the page rows of a PDF structure read with an independent reader of the same file."""
    independent = independent_pdf_pages(path)
    if independent is None:
        return {"verdict": "not checked", "reader": None,
                "reason": "pdftotext (Poppler) is not installed or could not read the file, "
                          "so no independent reader checked which pages hold text"}
    rows = document.get("pages") or []
    problems: list[str] = []
    notes: list[str] = []
    if len(independent) != len(rows):
        notes.append(f"pdftotext reads {len(independent)} pages and the structured read lists {len(rows)}; "
                     "the pages both list are compared")
    for row, theirs in zip(rows, independent):
        page = row.get("index")
        ours = row.get("chars") or 0
        if row.get("textless") and theirs >= PDFTOTEXT_TEXT_PAGE_CHARS:
            problems.append(f"page {page} was read as having no text, but pdftotext reads {theirs} characters on it")
        elif theirs >= PDFTOTEXT_SHORTFALL_CHARS and ours < theirs // 2:
            notes.append(f"page {page}: the read found {ours} characters where pdftotext reads {theirs} non-space characters")
    return {"verdict": "fail" if problems else "pass", "reader": "pdftotext", "problems": problems, "notes": notes}


def body_ordinal(rows: list[dict], index: int) -> int | None:
    """Zero-based position among direct body paragraphs, the coordinate the incumbents address."""
    ordinal = 0
    for row in rows:
        if row["index"] == index:
            return ordinal if not row.get("inTable") else None
        if not row.get("inTable"):
            ordinal += 1
    return None


def column_letters(reference: str) -> str:
    return re.match(r"([A-Z]+)", reference).group(1)


def next_row_number(sheet: dict) -> int:
    dimension = sheet.get("dimension") or ""
    numbers = [int(value) for value in re.findall(r"[A-Z]+([0-9]+)", dimension)]
    return (max(numbers) if numbers else 0) + 1


def first_string_pointer(value: object, prefix: str = "") -> tuple[str, str] | None:
    if isinstance(value, str) and 0 < len(value) <= 200 and prefix:
        return prefix, value
    if isinstance(value, dict):
        for key, child in value.items():
            found = first_string_pointer(child, prefix + "/" + str(key).replace("~", "~0").replace("/", "~1"))
            if found:
                return found
    if isinstance(value, list):
        for position, child in enumerate(value):
            found = first_string_pointer(child, prefix + "/" + str(position))
            if found:
                return found
    return None


def plan_edits(family: str, document: dict, needle: dict | None, source: bytes) -> tuple[list[dict], list[str]]:
    """One reversible, content preserving edit per documented capability, chosen from the file itself."""
    digest = document["source"]["sha256"]
    edits: list[dict] = []
    skipped: list[str] = []
    if family == "docx":
        rows = docx_rows(document)
        target = next((row for row in rows if row.get("limitation") is None and (row.get("text") or "").strip()), None)
        if target is None:
            return [], ["no plain paragraph with text was found on the first page, so no DOCX edit was planned"]
        index, text = target["index"], target["text"]
        part = document.get("part") or "word/document.xml"
        ordinal = body_ordinal(rows, index)
        incumbent = {"paragraph": index, "path": f"/body/p[{ordinal + 1}]", "at": f"p{ordinal}"} if ordinal is not None else None
        if incumbent is None:
            skipped.append("the chosen paragraph sits inside a table, so the incumbent adapters were not given a body coordinate")
        edits.append({
            "name": "docx-tracked-rewrite", "operation": "paragraphs",
            "engine": "docx-paragraphs", "kinds": ["paragraph_tracked"], "deterministic": False, "evidence": MARKER,
            "title": f'Rewrite paragraph {index} as a tracked change by "{AUTHOR}", keeping its text and adding the marker',
            "arguments": ["--expect-sha256", digest, "--track", "--author", AUTHOR,
                          "--expect", f"{index}={text}", "--set", f"{index}={text}{MARKER_SUFFIX}"],
            "extension": "docx", "oracle": "package", "hints": {"part": part},
            "incumbent": dict(incumbent, family="docx", operation="track", before=text,
                              after=text + MARKER_SUFFIX, author=AUTHOR) if incumbent else None,
        })
        edits.append({
            "name": "docx-comment", "operation": "paragraphs",
            "engine": "docx-paragraphs", "kinds": ["comment_added"], "deterministic": False, "evidence": COMMENT_TEXT,
            "title": f"Anchor a review comment on paragraph {index}",
            "arguments": ["--expect-sha256", digest, "--author", AUTHOR,
                          "--expect", f"{index}={text}", "--comment", f"{index}={COMMENT_TEXT}"],
            "extension": "docx", "oracle": "package", "hints": {"part": part},
            "incumbent": dict(incumbent, family="docx", operation="comment", text=COMMENT_TEXT,
                              author=AUTHOR) if incumbent else None,
        })
        edits.append({
            "name": "docx-insert-paragraph", "operation": "paragraphs",
            "engine": "docx-paragraphs", "kinds": ["paragraph_inserted"], "evidence": INSERTED_TEXT,
            "title": f"Insert a new paragraph after paragraph {index}",
            "arguments": ["--expect-sha256", digest, "--insert-after", f"{index}={INSERTED_TEXT}"],
            "extension": "docx", "oracle": "package", "hints": {"part": part},
            "incumbent": dict(incumbent, family="docx", operation="insert", text=INSERTED_TEXT) if incumbent else None,
        })
    elif family == "xlsx":
        selection = document.get("selection") or {}
        cells = selection.get("cells") or []
        sheet_name = selection.get("sheet")
        part = selection.get("part")
        sheet = next((row for row in (document.get("sheets") or []) if row.get("name") == sheet_name), {})
        literal = next((cell for cell in cells if cell["type"] == "string" and (cell.get("value") or "").strip()), None)
        if literal is None:
            skipped.append("no literal string cell was found in the read range, so no cell rewrite was planned")
        else:
            value = literal["value"]
            edits.append({
                "name": "xlsx-string-cell", "operation": "cells",
                "engine": "xlsx-cells", "kinds": ["cell_written"], "evidence": MARKER,
                "title": f"Rewrite {sheet_name}!{literal['ref']} to its own value plus the marker",
                "arguments": ["--expect-sha256", digest, "--expect", f"{literal['ref']}=string:{value}",
                              "--set", f"{literal['ref']}=string:{value}{MARKER_SUFFIX}"],
                "extension": "xlsx", "oracle": "package", "hints": {"part": part},
                "incumbent": {"family": "xlsx", "operation": "string", "sheet": sheet_name,
                              "cell": literal["ref"], "before": value, "after": value + MARKER_SUFFIX},
            })
        if sheet_name:
            row_number = next_row_number(sheet)
            column = column_letters(cells[0]["ref"]) if cells else "A"
            edits.append({
                "name": "xlsx-row-append", "operation": "rows",
                "engine": "xlsx-rows", "kinds": ["row_appended"], "evidence": APPENDED_ROW_TEXT,
                "title": f"Append one marker row after the last used row of {sheet_name}",
                "arguments": ["--expect-sha256", digest, "--sheet", sheet_name,
                              "--append-row", json.dumps({column: f"string:{APPENDED_ROW_TEXT}"})],
                "extension": "xlsx", "oracle": "package", "hints": {"part": part},
                "incumbent": {"family": "xlsx", "operation": "append", "sheet": sheet_name, "row": row_number,
                              "values": {column: f"string:{APPENDED_ROW_TEXT}"}},
            })
        empty = next((cell for cell in cells if cell["type"] == "empty"), None)
        if empty is None:
            skipped.append("no empty cell was found in the read range, so no formula write was planned")
        else:
            edits.append({
                "name": "xlsx-formula", "operation": "cells",
                "engine": "xlsx-cells", "kinds": ["formula_written"], "evidence": MARKER_FORMULA.lstrip("="),
                "title": f"Write {MARKER_FORMULA} into the empty cell {sheet_name}!{empty['ref']}",
                "arguments": ["--expect-sha256", digest, "--expect", f"{empty['ref']}=empty",
                              "--set", f"{empty['ref']}=formula:{MARKER_FORMULA}"],
                "extension": "xlsx", "oracle": "package", "hints": {"part": part},
                "incumbent": {"family": "xlsx", "operation": "formula", "sheet": sheet_name,
                              "cell": empty["ref"], "after": MARKER_FORMULA.lstrip("=")},
            })
    elif family == "pptx":
        selection = document.get("selection") or {}
        shapes = selection.get("shapes") or []
        target = next(((position, shape, paragraph) for position, shape in enumerate(shapes)
                       for paragraph in (shape.get("paragraphs") or []) if (paragraph.get("text") or "").strip()), None)
        if target is None:
            skipped.append("slide 1 has no plain text shape with text, so no slide text edit was planned")
        else:
            position, shape, paragraph = target
            edits.append({
                "name": "pptx-shape-text", "operation": "paragraphs",
                "engine": "pptx-slides", "kinds": ["paragraph_written"], "evidence": MARKER,
                "title": f"Rewrite paragraph {paragraph['index']} of shape {shape['id']} on slide 1, keeping its text",
                "arguments": ["--expect-sha256", digest, "--slide", "1",
                              "--expect", f"{paragraph['index']}={paragraph['text']}",
                              "--set", f"{paragraph['index']}={paragraph['text']}{MARKER_SUFFIX}"],
                "extension": "pptx", "oracle": "package",
                "hints": {"part": selection.get("part"), "notesPart": (selection.get("notes") or {}).get("part")},
                "incumbent": {"family": "pptx", "operation": "replace", "slide": 1, "shape_id": shape["id"],
                              "shapeOrdinal": position + 1, "before": paragraph["text"],
                              "after": paragraph["text"] + MARKER_SUFFIX},
            })
        if document.get("slides"):
            edits.append({
                "name": "pptx-slide-duplicate", "operation": "slides",
                "engine": "pptx-deck", "kinds": ["slide_duplicated"], "evidenceKind": "slide-count",
                "title": "Duplicate slide 1 as a new slide 2, with its own relationships",
                "arguments": ["--expect-sha256", digest, "--duplicate", "1"],
                "extension": "pptx", "oracle": "package", "hints": {},
                "incumbent": {"family": "pptx", "operation": "duplicate", "slide": 1},
            })
    elif family == "pdf":
        if (document.get("pageCount") or 0) < 2:
            skipped.append("the PDF has a single page, so keeping page 1 alone would be a no-op and "
                           "this build refuses a no-op; no page selection was planned")
        else:
            edits.append({
                "name": "pdf-keep-page-1", "operation": "pages",
                "engine": "pdf-pages", "kinds": ["page_kept"], "evidenceKind": "none",
                "title": "Keep page 1 alone in a new copy",
                "arguments": ["--keep", "1"], "extension": "pdf", "oracle": "pdf",
                "hints": {"pages": [1]}, "incumbent": {"family": "pdf", "operation": "keep", "pages": [1]},
            })
        if needle and needle.get("page"):
            edits.append({
                "name": "pdf-highlight", "operation": "annotate",
                "engine": "pdf-annotations", "kinds": ["annotation_added"], "evidenceKind": "pdf-annotation",
                "title": f'Highlight "{needle["text"]}" on page {needle["page"]}',
                "arguments": ["--expect-sha256", digest, "--page", str(needle["page"]),
                              "--highlight", needle["text"]],
                "extension": "pdf", "oracle": "pdf", "hints": {"pages": None, "appended": True},
                "incumbent": {"family": "pdf", "operation": "annotate"},
            })
        else:
            skipped.append("no neutral word was found on the read page, so no highlight was planned")
    elif family == "text":
        items = ((document.get("selection") or {}).get("items")) or []
        target = next((item for item in items if (item.get("text") or "").strip() and not item.get("truncated")), None)
        if target is None:
            skipped.append("no non-empty whole line was read, so no line rewrite was planned")
        else:
            edits.append({
                "name": "text-line-rewrite", "operation": "lines",
                "engine": "text-lines", "kinds": ["line_written"], "evidence": MARKER,
                "title": f"Rewrite line {target['line']}, keeping its text and adding the marker",
                "arguments": ["--expect-sha256", digest, "--expect", f"{target['line']}={target['text']}",
                              "--set", f"{target['line']}={target['text']}{MARKER_SUFFIX}"],
                "extension": Path(document["source"]["name"]).suffix.lstrip(".") or "txt",
                "oracle": "lines", "hints": {"lines": [target["line"]]},
                "incumbent": {"family": "text", "operation": "lines"},
            })
    elif family == "delimited":
        rows = document.get("rows") or []
        target = next(((index + 1, position + 1, value) for index, row in enumerate(rows)
                       for position, value in enumerate(row)
                       if value.strip() and not re.fullmatch(r"[-+0-9.,eE]+", value.strip())), None)
        if target is None:
            skipped.append("no text field was found in the delimited file, so no field write was planned")
        else:
            row, column, value = target
            address = f"R{row}C{column}"
            edits.append({
                "name": "delimited-field-rewrite", "operation": "fields",
                "engine": "csv-fields", "kinds": ["field_written"], "evidenceKind": "none",
                "title": f"Rewrite the field {address} to its own value plus the marker",
                "arguments": ["--expect-sha256", digest, "--expect", f"{address}=string:{value}",
                              "--set", f"{address}=string:{value}{MARKER_SUFFIX}"],
                "extension": Path(document["source"]["name"]).suffix.lstrip(".") or "csv",
                "oracle": "delimited",
                "hints": {"row": row, "column": column, "expected": value + MARKER_SUFFIX,
                          "extension": Path(document["source"]["name"]).suffix.lower()},
                "incumbent": {"family": "delimited", "operation": "fields"},
            })
    elif family == "json":
        if len(source) > MAX_JSON_PARSE_BYTES:
            skipped.append(f"the JSON file is over {MAX_JSON_PARSE_BYTES} bytes, so no field write was planned")
        else:
            try:
                parsed = json.loads(source.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                skipped.append(f"the JSON file could not be decoded here, so no field write was planned: {error}")
            else:
                found = first_string_pointer(parsed)
                if found is None:
                    skipped.append("the JSON document holds no string member, so no field write was planned")
                else:
                    pointer, value = found
                    edits.append({
                        "name": "json-field-rewrite", "operation": "fields",
                        "engine": "json-fields", "kinds": ["field_written"], "evidenceKind": "none",
                        "title": f"Rewrite the string member {pointer} to its own value plus the marker",
                        "arguments": ["--expect-sha256", digest, "--expect", f"{pointer}=string:{value}",
                                      "--set", f"{pointer}=string:{value}{MARKER_SUFFIX}"],
                        "extension": "json", "oracle": "json",
                        "hints": {"pointer": pointer, "expected": value + MARKER_SUFFIX},
                        "incumbent": {"family": "json", "operation": "fields"},
                    })
    return edits, skipped


def evaluate_output(edit: dict, before: bytes, after: bytes, declared: set[str] | None = None,
                    stamp: dict | None = None, receiptless: bool = False) -> dict:
    """One oracle per family, judging only the input and output bytes this recipe read itself.

    `declared` is what the receipt of the run being judged named for itself, and `stamp` is its
    `result.stamp`; a tool that published no such declaration gets none, and any member outside
    the fixed write set is reported. A `receiptless` tool's confirmed stamps are set aside and
    named, because it has nowhere to declare them.
    """
    oracle = edit["oracle"]
    if oracle == "package":
        # A source this recipe's own reader cannot open is a limit of the oracle, not a fault in
        # the output: Python's zipfile refuses a member whose stored CRC-32 is wrong, which some
        # real packages carry and this tool preserves byte for byte. Such a run is reported as
        # unjudged; an OUTPUT that cannot be read is still a failure of the edit.
        try:
            first = package_parts(before)
        except (zipfile.BadZipFile, ValueError) as error:
            return {"verdict": "not checked",
                    "reason": f"the source package could not be read by this recipe: {error}"}
        try:
            second = package_parts(after)
        except (zipfile.BadZipFile, ValueError) as error:
            return {"verdict": "fail", "problems": [f"the produced package could not be read: {error}"]}
        allowed = allowed_parts(edit["engine"], edit["kinds"], edit.get("hints") or {}, first, second,
                                declared)
        return package_preservation(before, after, allowed, stamp, receiptless)
    if oracle == "pdf":
        hints = edit.get("hints") or {}
        return pdf_preservation(before, after, hints.get("pages"), bool(hints.get("appended")))
    if oracle == "lines":
        return line_preservation(before, after, set((edit.get("hints") or {}).get("lines") or []))
    if oracle == "json":
        hints = edit.get("hints") or {}
        return json_field_preservation(before, after, hints["pointer"], hints["expected"])
    if oracle == "delimited":
        hints = edit.get("hints") or {}
        return delimited_field_preservation(before, after, hints["extension"], hints["row"],
                                            hints["column"], hints["expected"])
    return {"verdict": "not checked", "reason": "no oracle is declared for this edit"}


def change_evidence(edit: dict, before: bytes, after: bytes) -> dict:
    """Did the requested change actually reach the output? A tool that did nothing must not pass."""
    kind = edit.get("evidenceKind", "text")
    try:
        if kind == "none":
            return {"found": True, "looked": "the oracle above already checks the requested change"}
        if kind == "slide-count":
            first = len([name for name in package_member_names(before) if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)])
            second = len([name for name in package_member_names(after) if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)])
            return {"found": second == first + 1, "looked": f"slide parts {first} to {second}"}
        if kind == "pdf-annotation":
            return {"found": b"/Highlight" in after and len(after) > len(before),
                    "looked": "a Highlight annotation object appended after the source bytes"}
        needle = edit["evidence"].encode("utf-8")
        if edit["oracle"] == "package":
            first_parts, second_parts = package_parts(before), package_parts(after)
            touched = [name for name, value in second_parts.items()
                       if name not in first_parts or value != first_parts[name]]
            return {"found": any(needle in second_parts[name] for name in touched),
                    "looked": f"{edit['evidence']!r} inside a package member this edit wrote"}
        return {"found": needle in after, "looked": f"{edit['evidence']!r} in the produced file"}
    except (zipfile.BadZipFile, ValueError, KeyError) as error:
        return {"found": False, "looked": f"the output could not be searched: {error}"}


# ---------------------------------------------------------------- content diff


def structure_diff(family: str, before: dict, after: dict) -> list[str]:
    """What the same structured read says before and after, item by item."""
    lines: list[str] = []
    if family == "docx":
        first = {row["index"]: row for row in docx_rows(before)}
        second = {row["index"]: row for row in docx_rows(after)}
        if before.get("totalParagraphs") != after.get("totalParagraphs"):
            lines.append(f"paragraph count {before.get('totalParagraphs')} to {after.get('totalParagraphs')}")
        for index in sorted(first.keys() | second.keys()):
            old, new = first.get(index), second.get(index)
            if old is None:
                lines.append(f"paragraph {index} appeared: {new.get('text')!r}")
            elif new is None:
                lines.append(f"paragraph {index} is past the page after the edit")
            elif old.get("text") != new.get("text") or old.get("limitation") != new.get("limitation"):
                lines.append(f"paragraph {index}: {old.get('text')!r} ({old.get('limitation')}) to "
                             f"{new.get('text')!r} ({new.get('limitation')})")
        if len(before.get("comments") or []) != len(after.get("comments") or []):
            lines.append(f"comments {len(before.get('comments') or [])} to {len(after.get('comments') or [])}")
    elif family == "xlsx":
        first = {cell["ref"]: cell for cell in ((before.get("selection") or {}).get("cells") or [])}
        second = {cell["ref"]: cell for cell in ((after.get("selection") or {}).get("cells") or [])}
        for ref in sorted(first.keys() | second.keys()):
            old, new = first.get(ref), second.get(ref)
            if old != new:
                def render(cell):
                    if cell is None:
                        return "not in range"
                    return f"{cell['type']}:{cell.get('formula') or cell.get('value')}"
                lines.append(f"cell {ref}: {render(old)} to {render(new)}")
        if len(before.get("sheets") or []) != len(after.get("sheets") or []):
            lines.append(f"worksheets {len(before.get('sheets') or [])} to {len(after.get('sheets') or [])}")
    elif family == "pptx":
        if len(before.get("slides") or []) != len(after.get("slides") or []):
            lines.append(f"slides {len(before.get('slides') or [])} to {len(after.get('slides') or [])}")

        def paragraphs(document):
            selection = document.get("selection") or {}
            return {(shape["id"], row["index"]): row.get("text")
                    for shape in (selection.get("shapes") or []) for row in (shape.get("paragraphs") or [])}
        first, second = paragraphs(before), paragraphs(after)
        for key in sorted(first.keys() | second.keys()):
            if first.get(key) != second.get(key):
                lines.append(f"slide shape {key[0]} paragraph {key[1]}: {first.get(key)!r} to {second.get(key)!r}")
    elif family == "pdf":
        if before.get("pageCount") != after.get("pageCount"):
            lines.append(f"pages {before.get('pageCount')} to {after.get('pageCount')}")
        first = {page["index"]: page.get("chars") for page in (before.get("pages") or [])}
        second = {page["index"]: page.get("chars") for page in (after.get("pages") or [])}
        for index in sorted(first.keys() | second.keys()):
            if first.get(index) != second.get(index):
                lines.append(f"page {index} characters {first.get(index)} to {second.get(index)}")
    else:
        first = {item["line"]: item.get("text") for item in (((before.get("selection") or {}).get("items")) or [])}
        second = {item["line"]: item.get("text") for item in (((after.get("selection") or {}).get("items")) or [])}
        if before.get("lineCount") != after.get("lineCount"):
            lines.append(f"lines {before.get('lineCount')} to {after.get('lineCount')}")
        for number in sorted(first.keys() | second.keys()):
            if first.get(number) != second.get(number):
                lines.append(f"line {number}: {first.get(number)!r} to {second.get(number)!r}")
    return lines or ["no difference in the structured read"]


# ---------------------------------------------------------------- running UFO


# The instant every tracked change and comment this recipe writes is stamped with
# (2026-01-01T00:00:00Z), through the reproducible-builds SOURCE_DATE_EPOCH convention the CLI
# honors: the same edit of the same file then writes the same bytes on every run, so a native
# Office verdict about those bytes can be reused instead of opening them again.
EVALUATION_EPOCH = "1767225600"


def evaluation_environment() -> dict[str, str]:
    return {**os.environ, "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", EVALUATION_EPOCH)}


class Surface:
    """One packaged build, reached either through its launcher or through the restricted image."""

    def __init__(self, launcher: Path | None, image: str | None, inputs: Path, produced: Path, staging: Path) -> None:
        self.launcher = launcher
        self.image = image
        self.inputs = inputs
        self.produced = produced
        self.staging = staging
        self.artifact = launcher_artifact(launcher) if launcher else container_artifact(image, docker_image_id(image, cwd=REPO))
        self.input_root = str(inputs) if launcher else "/corpus"
        self.output_root = str(produced) if launcher else "/output"

    def command(self, arguments: list[str], cidfile: Path) -> list[str]:
        if self.launcher:
            return [str(self.launcher), *arguments]
        return [*restricted_docker_prefix(cidfile, self.inputs, self.produced, output_writable=True),
                "--env", "SOURCE_DATE_EPOCH=" + evaluation_environment()["SOURCE_DATE_EPOCH"],
                "--pull=never", self.artifact["imageId"], *arguments]


def run_ufo(surface: Surface, staging: Path, label: str, arguments: list[str]) -> dict:
    """One bounded UFO command; the receipt and the exit code are recorded whatever happened."""
    cidfile = staging / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', label)}.cid"
    started = time.monotonic_ns()
    try:
        process = run_bounded_command(
            surface.command(arguments, cidfile), cwd=REPO, timeout_seconds=TIMEOUT_SECONDS,
            label=f"own-file {label}", max_stdout_bytes=MAX_STDOUT_BYTES, max_stderr_bytes=MAX_STDERR_BYTES,
            env=evaluation_environment(),
        )
    finally:
        if not surface.launcher:
            remove_container(cidfile)
    elapsed = (time.monotonic_ns() - started) // 1_000_000
    receipt: dict | None = None
    if process.stdout.strip():
        try:
            receipt = json.loads(process.stdout)
        except ValueError:
            receipt = None
    return {"argv": list(arguments), "exitCode": process.returncode, "elapsedMillis": elapsed,
            "stdout": process.stdout, "stderr": process.stderr.strip()[:1000], "receipt": receipt}


def publish_receipt(destination: Path, result: dict) -> bool:
    """Publish the receipt a command printed; publish nothing when it printed none.

    A zero byte file named `.json` is not a receipt, and a reader who opens one has no way to
    tell "this run published nothing" from "this run published an empty result". A command that
    exits without a receipt, which a usage error does, leaves no file and the row says so.
    """
    if not (result.get("stdout") or "").strip():
        return False
    write_private_file(destination, result["stdout"].encode("utf-8"))
    return True


def receipt_is_valid(receipt: dict | None) -> bool | None:
    """Schema validation when jsonschema is installed; None when it is not available here."""
    if receipt is None:
        return False
    try:
        from tools.cli.validate_receipts import validator_for
    except SystemExit:
        return None
    except ImportError:
        return None
    schema = receipt.get("schema")
    version = receipt.get("schemaVersion")
    if not isinstance(schema, str) or not isinstance(version, int):
        return False
    validator = validator_for(schema, version)
    return None if validator is None else validator.is_valid(receipt)


# ---------------------------------------------------------------- incumbents


def incumbent_adapters():
    """The comparison adapters, imported from the pinned comparison runner, never copied."""
    from tools.corpus import compare_agent_tools

    return compare_agent_tools


INCUMBENT_FAMILIES = {"officecli": ("docx", "xlsx", "pptx"), "docx-cli": ("docx",)}


def run_incumbent(name: str, binary: Path, task: dict, source: bytes, extension: str,
                  edit: dict, workspace: Path) -> dict:
    """One incumbent attempt at the equivalent edit, on its own copy, judged by the same oracle."""
    record: dict = {"result": "failed", "reason": None, "commands": [], "output": None,
                    "preservation": None, "notes": []}
    family = task.get("family")
    if family not in INCUMBENT_FAMILIES[name]:
        return {**record, "result": "unsupported",
                "reason": f"{name} documents {', '.join(INCUMBENT_FAMILIES[name])} only; it offers no {family} command"}
    tools = incumbent_adapters()
    workspace.mkdir(parents=True, exist_ok=True)
    local = workspace / f"input.{extension}"
    produced = workspace / f"output.{extension}"
    local.write_bytes(source)
    runner = tools.Runner(str(binary), workspace)
    try:
        adapter = tools.officecli_adapter if name == "officecli" else tools.docx_adapter
        adapter(task, runner, local, produced)
        if local.read_bytes() != source:
            record.update(result="wrong", reason="the tool modified its own source copy")
        elif not produced.exists():
            record.update(result="failed", reason="the tool published no output file")
        else:
            after = produced.read_bytes()
            preservation = evaluate_output(edit, source, after, receiptless=True)
            evidence = change_evidence(edit, source, after)
            record["preservation"] = preservation
            record["evidence"] = evidence
            record["output"] = {"sizeBytes": len(after), "sha256": sha(after)}
            if not evidence["found"]:
                record.update(result="wrong",
                              reason=f"the command reported success, but this recipe could not find "
                                     f"{evidence['looked']}")
            elif preservation.get("verdict") == "pass":
                record["result"] = "pass"
            elif preservation.get("verdict") == "fail":
                record.update(result="wrong", reason="; ".join(preservation.get("problems") or []) or None)
            else:
                record["result"] = "pass"
                record["notes"].append(preservation.get("reason") or "no independent oracle applies to this edit")
    except tools.Unsupported as error:
        record.update(result="unsupported", reason=str(error))
    except tools.CommandError as error:
        record.update(result="failed", reason=str(error))
    except (OSError, ValueError, KeyError, IndexError, TypeError, StopIteration) as error:
        record.update(result="failed", reason=f"adapter error: {type(error).__name__}: {error}")
    record["commands"] = [{"argv": item["argv"], "exitCode": item.get("returncode"),
                           "stdout": (item.get("stdout") or "")[:2000],
                           "stderr": (item.get("stderr") or "")[:2000]} for item in runner.commands]
    return record


# ---------------------------------------------------------------- the report


def counter() -> dict:
    return {name: 0 for name in RESULTS}


def score(result: dict) -> str:
    """One command's outcome: a receipt that completed, one that declined, one that has no parser, or neither."""
    receipt = result["receipt"] or {}
    if result["exitCode"] == 0 and receipt.get("status") in ("accepted", "completed", "planned", "passed"):
        return "pass"
    if receipt.get("status") == "refused":
        return "refused"
    # `limited` is intake saying it detected the format and ran no deep parser on it.
    if receipt.get("status") == "limited":
        return "unsupported"
    return "failed"


def summary_table(summary: dict) -> list[str]:
    rows = ["| Tool | Pass | Refused | Failed | Wrong | Unsupported |",
            "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for tool, counts in summary.items():
        rows.append(f"| {tool} | {counts['pass']} | {counts['refused']} | {counts['failed']} "
                    f"| {counts['wrong']} | {counts['unsupported']} |")
    return rows


def render_report(report: dict) -> str:
    lines = [f"# UFO on your own files: {report['startedAt']}", ""]
    lines += [
        f"Recipe `{RECIPE}` report version {report['reportVersion']}, engine version "
        f"{report['engineVersion']}, {plural(report['fileCount'], 'file')}, {report['elapsedMillis']} ms.",
        "",
        "The build states its own contract in `receipts/capabilities.json`: "
        + ", ".join(f"{key} {json.dumps(value)}" for key, value
                    in sorted(((report.get("capabilities") or {}).get("surface") or {}).items()))
        + ". This recipe checked that against what it saw.",
        "",
        "Every input was copied before anything ran and every copy was compared with its original "
        "afterwards. No input was modified, no network was used, and nothing was overwritten: each "
        "output is a new file under `outputs/` and each receipt is a new file under `receipts/`.",
        "",
        "This report quotes text out of the files it was given: a paragraph, a cell, a line, and the "
        "word it searched for. The receipts and the produced copies under `outputs/` hold more of it. "
        "Share the directory the way you would share those files.",
        "",
        "## Summary", "",
    ]
    lines += summary_table(report["summary"])
    lines += ["", "The table counts the operations this report demonstrates: one intake and one inspection per "
                  "file, the structured read, the `find`, and each edit. The supporting reads (the sheet "
                  "inventory and the read back of each output) are receipts in the bundle, not rows here.", "",
              "`pass` means the command completed and this recipe's own reader agreed with the receipt. "
              "`refused` means the tool declined and published nothing. `failed` means the command did not "
              "run to a receipt. `wrong` means the command reported success but an independent check "
              "disagreed. `unsupported` means the operation is not offered for that file.", ""]
    if report.get("edits") == "none":
        lines += ["Edits were not requested for this run (`--edits none`); only the reads were exercised.", ""]
    if report.get("plan"):
        lines += ["## Your own plan", ""]
        plan = report["plan"]
        lines += [f"Plan `{plan['name']}` ran {plan['stepCount']} steps through `ufo batch`; "
                  f"dry run status `{plan['dryRunStatus']}`, real run status `{plan['status']}`.", ""]
        lines += ["| Step | Status | Exit | Output |", "| --- | --- | ---: | --- |"]
        for step in plan["steps"]:
            lines.append(f"| {step['step']} | {step['status']} | {step['exitCode']} "
                         f"| {step.get('output') or step.get('message') or ''} |")
        lines.append("")
    lines += ["## Files", ""]
    for record in report["files"]:
        lines += [f"### {record['name']}", ""]
        lines += [f"- Family: `{record.get('family') or 'not a family this recipe reads'}`, "
                  f"{record['sizeBytes']} bytes, sha256 `{record['sha256']}`",
                  f"- Source path: `{record['path']}`",
                  f"- Source unchanged after the run: {'yes' if record.get('sourceUnchanged') else 'NO'}"]
        intake = record.get("intake") or {}
        lines.append(f"- Intake: `{intake.get('status')}`, detected `{intake.get('resolvedKind')}` / "
                     f"`{intake.get('resolvedFormat')}`, probe `{intake.get('probe')}`")
        inspection = record.get("inspect") or {}
        if inspection:
            findings = inspection.get("findings") or []
            lines.append(f"- Inspection: `{inspection.get('status')}`, highest severity "
                         f"`{inspection.get('highestSeverity')}`, {len(findings)} findings, flags "
                         f"{inspection.get('flags') or []}")
        read = record.get("read")
        if read and read.get("status") != "completed":
            detail = " ".join(part for part in (read.get("code"), read.get("message")) if part)
            lines += ["", f"Structured read `{read.get('status')}`: {detail}".rstrip(": "), ""]
        elif not read:
            lines += ["", "No structured read applies to this format in this build.", ""]
        if read and read.get("summary"):
            counts = ", ".join(f"{key} {value}" for key, value in read["summary"]["counts"].items())
            lines += ["", f"What the read found: {counts}.", ""]
            for item in read["summary"]["editable"]:
                lines.append(f"- Editable: {item}")
            for item in read["summary"]["nonEditable"]:
                lines.append(f"- Not editable: {item['item']}, reason `{item['reason']}`")
        independent = (read or {}).get("independentText")
        if independent:
            if independent["verdict"] == "not checked":
                lines.append(f"- Independent text check: **not checked**, {independent['reason']}")
            else:
                lines.append(f"- Independent text check against {independent['reader']}: **{independent['verdict']}**")
                for problem in independent["problems"]:
                    lines.append(f"  - Wrong: {problem}")
                for note in independent["notes"]:
                    lines.append(f"  - Note: {note}")
        find = record.get("find")
        if find and find.get("status") == "completed":
            lines.append(f"- `find` for the neutral word \"{find['needle']}\" taken from the file itself: "
                         f"{plural(find['total'] or 0, 'match')}, {find['editableHits']} of them at "
                         "coordinates this build writes")
        elif find:
            lines.append(f"- `find` {find.get('status')}: {find.get('message')}")
        for note in record.get("notPlanned") or []:
            lines.append(f"- No edit planned: {note}")
        if record.get("error"):
            lines.append(f"- This file stopped early: {record['error']}")
        lines.append("")
        for edit in record.get("edits") or []:
            lines += [f"#### {edit['title']}", ""]
            plan = edit.get("dryRun") or {}
            lines.append(f"- Dry run: `{plan.get('status')}`" + (
                f", planned output sha256 `{plan['plan']['outputSha256']}`" if plan.get("plan") else ""))
            for tool, outcome in edit["tools"].items():
                lines += render_tool_outcome(tool, outcome)
            lines.append("")
    lines += ["## What this run does not prove", ""]
    for item in report["notProven"]:
        lines.append(f"- {item}")
    lines += ["", "## Bounds this run kept", ""]
    for key, value in report["bounds"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def render_tool_outcome(tool: str, outcome: dict) -> list[str]:
    lines = [f"- {tool}: **{outcome['result']}**" + (f", {outcome['reason']}" if outcome.get("reason") else "")]
    published = outcome.get("output")
    if published and published.get("path"):
        lines.append(f"  - Output `{published['path']}`, {published['sizeBytes']} bytes, sha256 `{published['sha256']}`")
    preservation = outcome.get("preservation")
    if preservation:
        verdict = preservation.get("verdict")
        detail = ""
        if "byteIdenticalMembers" in preservation:
            detail = (f"; {preservation['byteIdenticalMembers']} untouched members byte-identical; "
                      f"changed {preservation.get('changedMembers')}, added {preservation.get('addedMembers')}, "
                      f"removed {preservation.get('removedMembers')}")
        elif "changedLines" in preservation:
            detail = f"; changed lines {preservation.get('changedLines')}"
        elif "sourceBytesRetained" in preservation:
            detail = (f"; source bytes retained as the first revision: {preservation['sourceBytesRetained']}; "
                      f"{preservation.get('independentPageCheck')}")
        lines.append(f"  - Preservation: **{verdict}**{detail}")
        for problem in preservation.get("problems") or []:
            lines.append(f"  - Preservation problem: {problem}")
        for stamp in preservation.get("stampSetAside") or []:
            lines.append(f"  - Provenance stamp set aside: {stamp}")
        for note in preservation.get("notes") or []:
            lines.append(f"  - Preservation note: {note}")
    evidence = outcome.get("evidence")
    if evidence:
        lines.append(f"  - Requested change found: {'yes' if evidence['found'] else 'NO'}, looked for {evidence['looked']}")
    for line in outcome.get("contentDiff") or []:
        lines.append(f"  - Content diff: {line}")
    for note in outcome.get("notes") or []:
        lines.append(f"  - Note: {note}")
    return lines


# ---------------------------------------------------------------- the run


def read_arguments(family: str, source: str, output: str, sheet: str | None = None) -> list[str]:
    arguments = ["text", "--format", "structure"]
    if family == "docx":
        arguments += ["--offset", "0", "--limit", "50"]
    elif family == "xlsx" and sheet is not None:
        arguments += ["--sheet", sheet, "--range", "A1:J20"]
    elif family == "pptx":
        arguments += ["--slide", "1"]
    elif family == "pdf":
        arguments += ["--pages", "1"]
    elif family in ("text", "json"):
        arguments += ["--lines", "1-200"]
    return [*arguments, "-o", output, source]


def needle_texts(family: str, document: dict) -> tuple[list[str], int | None]:
    if family == "docx":
        return [row.get("text") or "" for row in docx_rows(document)], None
    if family == "xlsx":
        return [cell.get("value") or "" for cell in ((document.get("selection") or {}).get("cells") or [])], None
    if family == "pptx":
        selection = document.get("selection") or {}
        return [row.get("text") or "" for shape in (selection.get("shapes") or [])
                for row in (shape.get("paragraphs") or [])], None
    if family == "pdf":
        items = ((document.get("selection") or {}).get("items")) or []
        return [item.get("text") or "" for item in items], (items[0]["index"] if items else None)
    return [item.get("text") or "" for item in (((document.get("selection") or {}).get("items")) or [])], None


def run_evaluation(args: argparse.Namespace) -> dict:
    requested = args.output.absolute()
    require(not path_exists(requested), "evaluation output already exists; choose a new directory")
    require(requested.parent.is_dir(), "evaluation output parent must already exist")
    output = requested.parent.resolve(strict=True) / requested.name
    require(not path_exists(output), "evaluation output already exists; choose a new directory")
    launcher = args.ufo.resolve(strict=True) if args.ufo else None
    officecli = args.officecli.resolve(strict=True) if args.officecli else None
    docx_cli = args.docx_cli.resolve(strict=True) if args.docx_cli else None
    for binary in (officecli, docx_cli):
        require(binary is None or os.access(binary, os.X_OK), f"incumbent tool is not executable: {binary}")
    plan_document = None
    if args.edits not in ("auto", "none"):
        plan_path = Path(args.edits).resolve(strict=True)
        plan_document = json.loads(read_file(plan_path, MAX_PLAN_BYTES).decode("utf-8"))
        if isinstance(plan_document, dict):
            plan_document = plan_document.get("steps")
        require(isinstance(plan_document, list) and 1 <= len(plan_document) <= 200,
                "a plan must hold 1 to 200 {step, argv} objects")
    sources = collect_files([Path(entry) for entry in args.files])
    started_wall = time.time()
    started = time.monotonic_ns()
    report: dict = {
        "recipe": RECIPE, "reportVersion": REPORT_VERSION, "status": "incomplete",
        "engineVersion": args.expect_version,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall)),
        "evaluatorSha256": sha(Path(__file__).read_bytes()),
        "edits": "plan" if plan_document is not None else args.edits,
        "fileCount": len(sources), "files": [],
        "summary": {}, "notProven": list(NOT_PROVEN),
        "bounds": {
            "files": MAX_FILES, "bytesPerFile": MAX_FILE_BYTES, "bytesTotal": MAX_TOTAL_BYTES,
            "secondsPerCommand": TIMEOUT_SECONDS, "network": "never used",
            "sourceModification": "never; inputs are copied and rechecked",
            "outputReplacement": "never; one new directory, published once",
        },
    }
    with tempfile.TemporaryDirectory(prefix=".ufo-own-file-work-", dir=output.parent) as raw_work, \
            tempfile.TemporaryDirectory(prefix=".ufo-own-file-", dir=output.parent) as raw_staging:
        work, staging = Path(raw_work), Path(raw_staging)
        inputs, produced, compare_root = (work / name for name in ("inputs", "produced", "compare"))
        receipts, outputs = (staging / name for name in ("receipts", "outputs"))
        for directory in (inputs, produced, compare_root, receipts, outputs):
            directory.mkdir(mode=0o700)
        surface = Surface(launcher, args.container_image, inputs, produced, staging)
        report["artifact"] = surface.artifact
        report["tools"] = {
            "ufo": surface.artifact,
            "officecli": {"path": str(officecli), "sha256": sha(read_file(officecli))} if officecli else None,
            "docx-cli": {"path": str(docx_cli), "sha256": sha(read_file(docx_cli))} if docx_cli else None,
        }
        capabilities = run_ufo(surface, staging, "capabilities", ["capabilities"])
        publish_receipt(receipts / "capabilities.json", capabilities)
        stated = capabilities["receipt"] or {}
        require(capabilities["exitCode"] == 0 and stated.get("engineVersion") == args.expect_version,
                f"this build reports engine version {stated.get('engineVersion')!r}, not the expected "
                f"{args.expect_version}; evaluate the build you mean to evaluate")
        report["capabilities"] = {"receipt": "receipts/capabilities.json",
                                  "surface": stated.get("surface"),
                                  "schemaValid": receipt_is_valid(capabilities["receipt"])}
        summary = {"ufo": counter()}
        if officecli:
            summary["officecli"] = counter()
        if docx_cli:
            summary["docx-cli"] = counter()
        report["summary"] = summary
        for position, path in enumerate(sources, start=1):
            slug = slug_for(position, path)
            extension = path.suffix.lower()
            local = inputs / f"{slug}{extension}"
            original = file_identity(path)
            shutil.copyfile(path, local)
            record: dict = {
                "name": path.name, "path": str(path), "slug": slug,
                "family": FAMILY_BY_EXTENSION.get(extension), **original,
                "sourceUnchanged": True, "edits": [], "notPlanned": [], "error": None,
            }
            (receipts / slug).mkdir(mode=0o700)
            try:
                evaluate_one_file(record, surface, staging, receipts / slug, outputs, produced,
                                  compare_root / slug, local, slug, summary,
                                  {"officecli": officecli, "docx-cli": docx_cli},
                                  plan_document is None and args.edits == "auto")
            except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError) as error:
                record["error"] = str(error)[:500]
            record["sourceUnchanged"] = file_identity(path) == original
            report["files"].append(record)
        if plan_document is not None:
            report["plan"] = run_plan(surface, staging, plan_document, receipts, outputs, produced, summary)
        report["elapsedMillis"] = (time.monotonic_ns() - started) // 1_000_000
        report["status"] = "completed"
        write_private_file(staging / "report.json", json_bytes(report))
        write_private_file(staging / "report.md", render_report(report).encode("utf-8"))
        publish_directory_noreplace(staging, output)
    return report


def publish(produced: Path, name: str, destination: Path) -> dict:
    """Move one produced file out of the private workspace into the published bundle."""
    source = produced / name
    require(source.is_file(), f"the command reported an output that is not here: {name}")
    require(not path_exists(destination), f"published output already exists: {destination}")
    data = read_file(source)
    write_private_file(destination, data)
    source.unlink()
    return {"sizeBytes": len(data), "sha256": sha(data)}


def evaluate_one_file(record: dict, surface: Surface, staging: Path, receipts: Path, outputs: Path,
                      produced: Path, compare_root: Path, local: Path, slug: str, summary: dict,
                      incumbents: dict, auto_edits: bool) -> None:
    family = record["family"]
    source_argument = f"{surface.input_root}/{local.name}"
    source_bytes = read_file(local)

    published: set[str] = set()

    def record_step(label: str, result: dict) -> None:
        if publish_receipt(receipts / f"{label}.json", result):
            published.add(label)

    def receipt_path(label: str) -> str | None:
        """Where the receipt for this step was published, or null when the run published none."""
        return f"receipts/{slug}/{label}.json" if label in published else None

    intake = run_ufo(surface, staging, f"{slug}-intake", ["intake", source_argument])
    record_step("intake", intake)
    receipt = intake["receipt"] or {}
    record["intake"] = {
        "status": receipt.get("status", "failed"), "receipt": receipt_path("intake"),
        "resolvedKind": (receipt.get("detection") or {}).get("resolvedKind"),
        "resolvedFormat": (receipt.get("detection") or {}).get("resolvedFormat"),
        "probe": (receipt.get("probe") or {}).get("outcome"),
        "code": receipt.get("code"), "message": receipt.get("message") or intake["stderr"] or None,
        "schemaValid": receipt_is_valid(intake["receipt"]),
    }
    summary["ufo"][score(intake)] += 1
    inspection = run_ufo(surface, staging, f"{slug}-inspect", ["inspect", "--deep", source_argument])
    record_step("inspect", inspection)
    receipt = inspection["receipt"] or {}
    record["inspect"] = {
        "status": receipt.get("status", "failed"), "receipt": receipt_path("inspect"),
        "highestSeverity": receipt.get("highestSeverity"), "flags": receipt.get("flags"),
        "findings": [{"code": item.get("code"), "title": item.get("title"), "removable": item.get("removable")}
                     for item in (receipt.get("findings") or [])],
        "notInspected": receipt.get("notInspected"),
        "code": receipt.get("code"), "message": receipt.get("message") or inspection["stderr"] or None,
        "schemaValid": receipt_is_valid(inspection["receipt"]),
    }
    summary["ufo"][score(inspection)] += 1
    if family is None:
        record["read"] = None
        record["notPlanned"].append("this recipe reads DOCX, XLSX, PPTX, PDF, Markdown, text, CSV and JSON; "
                                    "the intake and inspection receipts above are what it can show for this file")
        summary["ufo"]["unsupported"] += 1
        return
    if family == "delimited":
        record["read"] = {"status": "unsupported", "code": None, "message": NO_STRUCTURE_READ, "contract": None}
        record["find"] = {"status": "not attempted", "needle": None, "message": NO_STRUCTURE_READ}
        summary["ufo"]["unsupported"] += 1
        if len(source_bytes) > MAX_JSON_PARSE_BYTES:
            record["notPlanned"].append(f"the delimited file is over {MAX_JSON_PARSE_BYTES} bytes, "
                                        "so this recipe did not parse it to choose a field")
            return
        try:
            rows, delimiter = delimited_rows(source_bytes, local.suffix.lower())
        except (ValueError, UnicodeDecodeError) as error:
            record["notPlanned"].append(f"the delimited file could not be read here: {error}")
            return
        document = {"source": {"name": local.name, "sha256": sha(source_bytes)},
                    "rows": rows, "delimiter": delimiter}
        record["read"]["summary"] = structure_summary(family, document)
        if not auto_edits:
            return
        plans, skipped = plan_edits(family, document, None, source_bytes)
        record["notPlanned"].extend(skipped)
        for edit in plans:
            record["edits"].append(run_one_edit(edit, surface, staging, receipts, outputs, produced,
                                                compare_root, slug, summary, incumbents,
                                                source_argument, source_bytes, family, document, None))
        return
    sheet = None
    document: dict | None = None
    if family == "xlsx":
        inventory = run_ufo(surface, staging, f"{slug}-sheets",
                            read_arguments("xlsx", source_argument, f"{surface.output_root}/{slug}-sheets.json"))
        record_step("sheets", inventory)
        if inventory["exitCode"] == 0 and (inventory["receipt"] or {}).get("status") == "completed":
            inventory_document = json.loads(read_file(produced / f"{slug}-sheets.json"))
            publish(produced, f"{slug}-sheets.json", receipts / "sheets.document.json")
            sheets = inventory_document.get("sheets") or []
            sheet = sheets[0]["name"] if sheets else None
    read_label = f"{slug}-read"
    read = run_ufo(surface, staging, read_label,
                   read_arguments(family, source_argument, f"{surface.output_root}/{read_label}.json", sheet))
    record_step("read", read)
    receipt = read["receipt"] or {}
    record["read"] = {
        "status": receipt.get("status", "failed"), "receipt": receipt_path("read"),
        "contract": STRUCTURE_CONTRACT[family], "code": receipt.get("code"),
        "message": receipt.get("message") or read["stderr"] or None,
        "schemaValid": receipt_is_valid(read["receipt"]),
    }
    summary["ufo"][score(read)] += 1
    if read["exitCode"] != 0 or receipt.get("status") != "completed":
        record["notPlanned"].append("the structured read did not complete, so no edit was planned")
        return
    document = json.loads(read_file(produced / f"{read_label}.json"))
    record["read"]["document"] = f"receipts/{slug}/read.document.json"
    publish(produced, f"{read_label}.json", receipts / "read.document.json")
    record["read"]["summary"] = structure_summary(family, document)
    if family == "pdf":
        oracle = pdf_text_oracle(local, document)
        record["read"]["independentText"] = oracle
        if oracle["verdict"] == "fail":
            # A read that completed can still be wrong about what the file holds.
            summary["ufo"]["pass"] -= 1
            summary["ufo"]["wrong"] += 1
    texts, page = needle_texts(family, document)
    needle = choose_needle(texts)
    if needle:
        find_label = f"{slug}-find"
        found = run_ufo(surface, staging, find_label,
                        ["find", "--text", needle, "--expect-sha256", document["source"]["sha256"],
                         "-o", f"{surface.output_root}/{find_label}.json", source_argument])
        record_step("find", found)
        receipt = found["receipt"] or {}
        summary["ufo"][score(found)] += 1
        if found["exitCode"] == 0 and receipt.get("status") == "completed":
            hits = json.loads(read_file(produced / f"{find_label}.json"))
            publish(produced, f"{find_label}.json", receipts / "find.document.json")
            record["find"] = {
                "status": "completed", "needle": needle, "total": hits.get("total"),
                "editableHits": sum(1 for hit in (hits.get("hits") or []) if hit.get("editable")),
                "receipt": receipt_path("find"), "document": f"receipts/{slug}/find.document.json",
            }
        else:
            record["find"] = {"status": receipt.get("status", "failed"), "needle": needle,
                              "message": receipt.get("message") or found["stderr"],
                              "receipt": receipt_path("find")}
    else:
        record["find"] = {"status": "not attempted", "needle": None,
                          "message": "the read found no ordinary word to search for"}
    if not auto_edits:
        return
    plans, skipped = plan_edits(family, document, {"text": needle, "page": page} if needle else None, source_bytes)
    record["notPlanned"].extend(skipped)
    for edit in plans:
        record["edits"].append(run_one_edit(edit, surface, staging, receipts, outputs, produced,
                                            compare_root, slug, summary, incumbents,
                                            source_argument, source_bytes, family, document, sheet))


def run_one_edit(edit: dict, surface: Surface, staging: Path, receipts: Path, outputs: Path,
                 produced: Path, compare_root: Path, slug: str, summary: dict, incumbents: dict,
                 source_argument: str, source_bytes: bytes, family: str, before: dict, sheet: str | None) -> dict:
    """Plan the edit, run it, judge the output with this recipe's own readers, then try the incumbents."""
    name, extension = edit["name"], edit["extension"]
    output_name = f"{slug}-{name}.{extension}"
    entry: dict = {"name": name, "title": edit["title"], "operation": edit["operation"],
                   "engine": edit["engine"], "tools": {}}
    plan = run_ufo(surface, staging, f"{slug}-{name}-plan",
                   ["edit", edit["operation"], *edit["arguments"], "--dry-run",
                    "-o", f"{surface.output_root}/{output_name}", source_argument])
    planned_receipt = publish_receipt(receipts / f"{name}.plan.json", plan)
    plan_receipt = plan["receipt"] or {}
    entry["dryRun"] = {"status": plan_receipt.get("status", "failed"), "plan": plan_receipt.get("plan"),
                       "receipt": f"receipts/{slug}/{name}.plan.json" if planned_receipt else None,
                       "code": plan_receipt.get("code"),
                       "message": plan_receipt.get("message") or plan["stderr"] or None}
    real = run_ufo(surface, staging, f"{slug}-{name}",
                   ["edit", edit["operation"], *edit["arguments"],
                    "-o", f"{surface.output_root}/{output_name}", source_argument])
    real_receipt = publish_receipt(receipts / f"{name}.json", real)
    receipt = real["receipt"] or {}
    outcome: dict = {"result": "failed", "reason": None,
                     "receipt": f"receipts/{slug}/{name}.json" if real_receipt else None,
                     "status": receipt.get("status"), "code": receipt.get("code"),
                     "message": receipt.get("message"), "notes": [],
                     "schemaValid": receipt_is_valid(real["receipt"])}
    entry["tools"]["ufo"] = outcome
    if real["exitCode"] == 1 and receipt.get("status") == "refused":
        outcome.update(result="refused", reason=receipt.get("message") or receipt.get("code"))
        if receipt.get("output") is not None:
            outcome["notes"].append("the refusal reported an output, which the contract forbids")
        if path_exists(produced / output_name):
            outcome["notes"].append("the refusal left a file at the destination, which the contract forbids")
        summary["ufo"]["refused"] += 1
    elif real["exitCode"] != 0 or receipt.get("status") != "completed":
        outcome.update(result="failed",
                       reason=receipt.get("message") or real["stderr"] or f"exit code {real['exitCode']}")
        summary["ufo"]["failed"] += 1
    else:
        kinds = [item["kind"] for item in ((receipt.get("result") or {}).get("applied") or [])]
        outcome["applied"] = kinds
        engine = (receipt.get("result") or {}).get("engine") or ""
        if engine != edit["engine"]:
            outcome["notes"].append(f"the receipt names engine {engine}, not the planned {edit['engine']}")
        after_bytes = read_file(produced / output_name)
        preservation = evaluate_output(edit, source_bytes, after_bytes, declared_parts(receipt),
                                       ((receipt.get("result") or {}).get("stamp")))
        outcome["preservation"] = preservation
        evidence = change_evidence(edit, source_bytes, after_bytes)
        outcome["evidence"] = evidence
        if family == "delimited":
            outcome["contentDiff"] = delimited_diff(source_bytes, after_bytes, (edit.get("hints") or {})["extension"])
        else:
            outcome["contentDiff"] = read_back(
                surface, staging, produced, family, sheet, before,
                f"{surface.output_root}/{output_name}", f"{slug}-{name}-after",
                receipts / f"{name}.after.json", receipts / f"{name}.after.document.json")
        (outputs / slug).mkdir(mode=0o700, exist_ok=True)
        identity = publish(produced, output_name, outputs / slug / f"{name}.{extension}")
        outcome["output"] = {"path": f"outputs/{slug}/{name}.{extension}", **identity}
        planned = (entry["dryRun"].get("plan") or {}).get("outputSha256")
        # Adding a package member is no longer a reason for the bytes to differ: a part the tool
        # adds is dated from the package it was added to, not from the clock, so the same edit on
        # the same source writes the same bytes. Only an edit that stamps an author date INSIDE
        # the content it writes is exempt, and the plan above marks those.
        reproducible = edit.get("deterministic", True)
        if planned == identity["sha256"]:
            outcome["notes"].append("the real run produced exactly the bytes the dry run planned")
        elif planned and reproducible:
            outcome.update(result="wrong", reason="the real run did not produce the bytes the dry run planned")
        elif planned:
            outcome["notes"].append("the dry run planned a different hash because this edit stamps a time in "
                                    "the content it writes")
        if outcome["result"] == "wrong":
            pass
        elif not evidence["found"]:
            outcome.update(result="wrong",
                           reason=f"the receipt says the edit completed, but this recipe could not find "
                                  f"{evidence['looked']}")
        elif preservation.get("verdict") == "pass":
            outcome["result"] = "pass"
        elif preservation.get("verdict") == "not checked":
            outcome["result"] = "pass"
            outcome["notes"].append(preservation.get("reason") or "no independent oracle applies to this edit")
        else:
            outcome.update(result="wrong",
                           reason="; ".join(preservation.get("problems") or ["an independent check disagreed"]))
        summary["ufo"][outcome["result"]] += 1
    for tool, binary in incumbents.items():
        if binary is None:
            continue
        if edit.get("incumbent") is None:
            entry["tools"][tool] = {"result": "unsupported", "notes": [],
                                    "reason": "this recipe has no equivalent coordinate for that tool on this file"}
            summary[tool]["unsupported"] += 1
            continue
        attempt = run_incumbent(tool, binary, dict(edit["incumbent"]), source_bytes, extension, edit,
                                compare_root / f"{name}-{tool}")
        if attempt.get("output"):
            staged = f"{slug}-{name}-{tool}.{extension}"
            write_private_file(produced / staged, read_file(compare_root / f"{name}-{tool}" / f"output.{extension}"))
            attempt["contentDiff"] = read_back(surface, staging, produced, family, sheet, before,
                                               f"{surface.output_root}/{staged}", f"{slug}-{name}-{tool}-after",
                                               receipts / f"{name}.{tool}.after.json",
                                               receipts / f"{name}.{tool}.after.document.json")
            published = publish(produced, staged, outputs / slug / f"{name}.{tool}.{extension}")
            attempt["output"] = {"path": f"outputs/{slug}/{name}.{tool}.{extension}", **published}
        attempt["notes"].append("the adapter used that tool's own documented commands, its own author name and "
                                "its own defaults; the same oracle judged both sides")
        write_private_file(receipts / f"{name}.{tool}.commands.json", json_bytes(attempt["commands"]))
        attempt["commands"] = f"receipts/{slug}/{name}.{tool}.commands.json"
        entry["tools"][tool] = attempt
        summary[tool][attempt["result"]] += 1
    return entry


def read_back(surface: Surface, staging: Path, produced: Path, family: str, sheet: str | None,
              before: dict, source_argument: str, label: str, receipt_path: Path, document_path: Path) -> list[str]:
    """The same structured read on the produced file, so the diff is item by item, not byte counts."""
    result = run_ufo(surface, staging, label,
                     read_arguments(family, source_argument, f"{surface.output_root}/{label}.json", sheet))
    publish_receipt(receipt_path, result)
    receipt = result["receipt"] or {}
    if result["exitCode"] != 0 or receipt.get("status") != "completed":
        return [f"the structured read of this output {receipt.get('status', 'failed')}: "
                f"{receipt.get('message') or result['stderr'] or 'no receipt'}"]
    document = json.loads(read_file(produced / f"{label}.json"))
    publish(produced, f"{label}.json", document_path)
    return structure_diff(family, before, document)


def run_plan(surface: Surface, staging: Path, steps: list, receipts: Path, outputs: Path,
             produced: Path, summary: dict) -> dict:
    """The evaluator's own edits, in the documented batch plan format, through `ufo batch`.

    The dry run and the real run get their own directories, because a read step writes its
    own output in both and an output is never replaced.
    """
    lines = []
    for step in steps:
        require(isinstance(step, dict) and isinstance(step.get("step"), str) and isinstance(step.get("argv"), list),
                "every plan entry must be a {step, argv} object")
        lines.append(json.dumps({"step": step["step"], "argv": [str(value) for value in step["argv"]]}))
    plan_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    dry_root, real_root = produced / "plan-dry", produced / "plan-real"
    for directory in (dry_root, real_root):
        directory.mkdir(mode=0o700)
        write_private_file(directory / "plan.jsonl", plan_bytes)
    write_private_file(outputs / "plan.jsonl", plan_bytes)
    result: dict = {"name": "plan.jsonl", "stepCount": len(steps), "sha256": sha(plan_bytes),
                    "dryRunStatus": "not run", "status": "not run", "steps": []}
    dry = run_ufo(surface, staging, "plan-dry-run",
                  ["batch", "--plan", f"{surface.output_root}/plan-dry/plan.jsonl",
                   "-o", f"{surface.output_root}/plan-dry/plan-check.json", "--dry-run"])
    publish_receipt(receipts / "plan-dry-run.json", dry)
    result["dryRunStatus"] = (dry["receipt"] or {}).get("status", "failed")
    if (dry_root / "plan-check.json").is_file():
        publish(dry_root, "plan-check.json", outputs / "plan-check.json")
    real = run_ufo(surface, staging, "plan",
                   ["batch", "--plan", f"{surface.output_root}/plan-real/plan.jsonl",
                    "-o", f"{surface.output_root}/plan-real/plan-manifest.json"])
    publish_receipt(receipts / "plan.json", real)
    manifest = real["receipt"] or {}
    result["status"] = manifest.get("status", "failed")
    for step in manifest.get("steps") or []:
        published = None
        if isinstance(step.get("output"), dict) and step["output"].get("path"):
            name = Path(step["output"]["path"]).name
            if (real_root / name).is_file():
                publish(real_root, name, outputs / name)
                published = f"outputs/{name}"
        result["steps"].append({"step": step.get("step"), "status": step.get("status"),
                                "exitCode": step.get("exitCode"), "output": published,
                                "message": step.get("message") or (step.get("receipt") or {}).get("message")})
        if step.get("status") in ("completed", "planned"):
            summary["ufo"]["pass"] += 1
        elif step.get("status") == "refused":
            summary["ufo"]["refused"] += 1
        elif step.get("status") == "skipped":
            summary["ufo"]["unsupported"] += 1
        else:
            summary["ufo"]["failed"] += 1
    if (real_root / "plan-manifest.json").is_file():
        publish(real_root, "plan-manifest.json", outputs / "plan-manifest.json")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--files", nargs="+", required=True, metavar="DIR_OR_FILE",
                        help="the evaluator's own files, or directories holding them; never modified")
    surface = result.add_mutually_exclusive_group(required=True)
    surface.add_argument("--ufo", type=Path, help="packaged Linux app-image bin/ufo launcher")
    surface.add_argument("--container-image", help="existing local image; never pulled")
    result.add_argument("--expect-version", required=True)
    result.add_argument("--output", type=Path, required=True, help="new report directory; parent must exist")
    result.add_argument("--officecli", type=Path, help="an installed OfficeCLI binary to compare against")
    result.add_argument("--docx-cli", type=Path, dest="docx_cli", help="an installed docx-cli binary to compare against")
    result.add_argument("--edits", default="auto",
                        help="auto (default), none, or the path to a PLAN.json in the batch plan format")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = run_evaluation(args)
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as error:
        print(f"Own-file evaluation failed before it could publish: {error}", file=sys.stderr)
        return 1
    counts = report["summary"]["ufo"]
    print(f"Report: {args.output}/report.md ({report['fileCount']} files, {report['elapsedMillis']} ms)")
    for tool, row in report["summary"].items():
        print(f"  {tool}: " + ", ".join(f"{name} {row[name]}" for name in RESULTS))
    if counts["failed"] or counts["wrong"]:
        print("Some operations failed or disagreed with an independent check; read report.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
