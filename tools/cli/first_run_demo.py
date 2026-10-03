#!/usr/bin/env python3
"""An offline first-use demonstration with synthetic files and full UFO receipts.

Requires Python 3.10+, jsonschema, and the published Linux CLI. Creates a new
directory only. No account, agent, model API, Office installation, or downloads
are needed. Inspection is format-specific and every coverage gap is retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tools.cli.artifact_identity import launcher_artifact
from tools.cli.bounded_process import run_bounded_command
from tools.cli.validate_receipts import parse_json, validator_for

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
VISIBLE_TEXT = "Public update: shipment remains on Friday."
HIDDEN_TEXT = "Internal review: confirm the draft before sharing."
EXPECTED_FINDINGS = {"office_properties", "office_comments", "office_hidden_text"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, document: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def make_inputs(directory: Path) -> tuple[Path, Path]:
    """A deliberately unrecognized regular file and a minimal valid DOCX."""
    unknown = directory / "unfamiliar.ufo-unknown"
    with unknown.open("xb") as handle:
        handle.write(b"\x00\xff\x80UFO synthetic unfamiliar file\x00" + bytes(range(32)))
    docx = directory / "before-sharing.docx"
    parts = {
        "[Content_Types].xml": '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>''',
        "_rels/.rels": f'''<Relationships xmlns="{REL}">
<Relationship Id="rDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rCore" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rApp" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>''',
        "word/document.xml": f'''<w:document xmlns:w="{WORD}"><w:body>
<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>{VISIBLE_TEXT}</w:t></w:r><w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>
<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>{HIDDEN_TEXT}</w:t></w:r></w:p>
<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
</w:body></w:document>''',
        "word/_rels/document.xml.rels": f'''<Relationships xmlns="{REL}">
<Relationship Id="rStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>''',
        "word/styles.xml": f'''<w:styles xmlns:w="{WORD}"><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>''',
        "word/comments.xml": f'''<w:comments xmlns:w="{WORD}"><w:comment w:id="0" w:author="Synthetic Reviewer" w:date="2026-09-26T00:00:00Z"><w:p><w:r><w:t>Internal reviewer note.</w:t></w:r></w:p></w:comment></w:comments>''',
        "docProps/core.xml": '''<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Synthetic sharing example</dc:title><dc:creator>Synthetic Reviewer</dc:creator><cp:lastModifiedBy>Synthetic Owner</cp:lastModifiedBy></cp:coreProperties>''',
        "docProps/app.xml": '''<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Synthetic fixture generator</Application><Company>Synthetic Example LLC</Company></Properties>''',
    }
    with zipfile.ZipFile(docx, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 26, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, value.encode("utf-8"))
    return unknown, docx


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_receipt(document: dict) -> None:
    validator = validator_for(document.get("schema"), document.get("schemaVersion"))
    require(validator is not None, "CLI returned an unknown receipt contract")
    errors = list(validator.iter_errors(document))
    require(not errors, "CLI receipt failed its full JSON schema: " + "; ".join(error.message for error in errors[:3]))


def verify_clean_copy(source: Path, output: Path) -> dict:
    """Verify actual saved ZIP/XML bytes, independently of UFO findings."""
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
        old = {name: before.read(name) for name in before.namelist()}
        new = {name: after.read(name) for name in after.namelist()}
    removed = set(old) - set(new)
    changed = {name for name in set(old) & set(new) if old[name] != new[name]}
    require(removed == {"word/comments.xml"}, "cleaned package must remove the planted comments part only")
    allowed = {"[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels", "docProps/core.xml", "docProps/app.xml"}
    require(changed <= allowed and not set(new) - set(old), "clean copy changed an unrelated package part")
    document = ET.fromstring(new["word/document.xml"])
    texts = [element.text or "" for element in document.iter("{" + WORD + "}t")]
    require(texts == [VISIBLE_TEXT], "the visible sentence must survive and hidden text must be removed")
    for marker in ("vanish", "commentRangeStart", "commentRangeEnd", "commentReference"):
        require(not list(document.iter("{" + WORD + "}" + marker)), "hidden/comment markers remain in the saved output")
    for properties in ("docProps/core.xml", "docProps/app.xml"):
        require(len(ET.fromstring(new[properties])) == 0, "planted personal document properties remain")
    relationships = ET.fromstring(new["word/_rels/document.xml.rels"])
    require(not any(row.get("Type", "").endswith("/comments") for row in relationships), "comment relationship remains")
    types = ET.fromstring(new["[Content_Types].xml"])
    require(not any(row.get("PartName") == "/word/comments.xml" for row in types), "comment content-type registration remains")
    return {"removedParts": sorted(removed), "changedParts": sorted(changed),
            "unchangedParts": sorted(set(old) & set(new) - changed), "visibleTextPreserved": VISIBLE_TEXT}


def mcp_smoke(ufo: Path, root: Path, source: Path, expected_hash: str, version: str, environment: dict) -> dict:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "ufo-first-look", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "inspect", "arguments": {"path": str(source)}}},
    ]
    result = run_bounded_command([str(ufo), "mcp", "--allow-root", str(root)], cwd=root,
        input_bytes=("\n".join(json.dumps(row) for row in requests) + "\n").encode("utf-8"),
        timeout_seconds=30, max_stdout_bytes=4 * 1024 * 1024, max_stderr_bytes=64 * 1024,
        label="local stdio MCP smoke", env=environment)
    require(result.returncode == 0, "local MCP process failed")
    rows = [parse_json(line) for line in result.stdout.splitlines()]
    require(len(rows) == 3 and {row.get("id") for row in rows} == {1, 2, 3}, "MCP must answer initialization, discovery and inspection once")
    responses = {row["id"]: row for row in rows}
    require(all("error" not in row for row in rows), "MCP returned an RPC error")
    require(responses[1]["result"]["serverInfo"]["version"] == version, "MCP version differs from the CLI")
    tools = responses[2]["result"]["tools"]
    inspector = next((tool for tool in tools if tool["name"] == "inspect"), None)
    require(inspector is not None and "path" in inspector["inputSchema"]["required"], "MCP discovery must expose the inspect path contract")
    called = responses[3]["result"]
    require(not called.get("isError"), "MCP inspect refused the synthetic supported file")
    receipt = called["structuredContent"]
    validate_receipt(receipt)
    require(receipt["source"]["sha256"] == expected_hash, "MCP receipt source identity is incorrect")
    require(EXPECTED_FINDINGS <= {finding["code"] for finding in receipt["findings"]}, "MCP must report the planted findings")
    write_json(root / "receipts/mcp-transcript.json", {"requests": requests, "responses": rows})
    write_json(root / "receipts/mcp-inspection.json", receipt)
    return {"passed": True, "toolCount": len(tools), "protocolVersion": responses[1]["result"]["protocolVersion"],
            "installedAssistantSessionVerified": False, "notInspected": receipt["notInspected"]}


def run_demo(ufo: Path, root: Path, expect_version: str = "1.5.1", mcp: bool = False) -> dict:
    ufo, root = ufo.resolve(), root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for name in ("inputs", "outputs", "receipts", "config"):
        (root / name).mkdir()
    started = time.monotonic()
    identity = launcher_artifact(ufo)
    environment = dict(os.environ, XDG_CONFIG_HOME=str(root / "config"))
    environment.pop("UFO_LICENSE_FILE", None)
    commands = []

    def execute(label: str, args: list[str]):
        result = run_bounded_command([str(ufo), *args], cwd=root, timeout_seconds=40,
            max_stdout_bytes=4 * 1024 * 1024, max_stderr_bytes=64 * 1024, label=label, env=environment)
        commands.append({"step": label, "args": args, "exitCode": result.returncode,
                         "stdoutBytes": len(result.stdout.encode("utf-8")), "stderrBytes": len(result.stderr.encode("utf-8"))})
        return result

    version = execute("version", ["--version"])
    require(version.returncode == 0 and version.stdout.strip() == "ufo " + expect_version, "installed CLI version does not match --expect-version")
    (root / "receipts/version.txt").write_text(version.stdout, encoding="utf-8")

    def call(label: str, args: list[str], exit_code: int = 0, source: Path | None = None) -> dict:
        result = execute(label, args)
        with (root / "receipts" / (label + ".json")).open("x", encoding="utf-8") as handle:
            handle.write(result.stdout)
        require(result.returncode == exit_code, f"{label}: unexpected exit {result.returncode}: {result.stderr[:300]}")
        document = parse_json(result.stdout)
        validate_receipt(document)
        require(document["engineVersion"] == expect_version and document["network"] == "not_used", "receipt must identify this offline release")
        require(document["license"]["tier"] == "free" and not document["license"]["verified"], "this demo must run within free bounds without a license")
        if source:
            require(document["source"]["sha256"] == sha(source) and document["source"]["sizeBytes"] == source.stat().st_size,
                    "receipt source identity must match actual bytes")
        return document

    doctor = call("doctor", ["doctor"])
    require(doctor["status"] == "passed", "installed CLI doctor did not pass")
    unknown, supported = make_inputs(root / "inputs")
    originals = {path.name: sha(path) for path in (unknown, supported)}
    intake = call("unfamiliar-info", ["intake", str(unknown)], exit_code=1, source=unknown)
    require(intake["status"] == "limited" and intake["probe"]["code"] == "parser_not_available", "unknown format must retain its dedicated-parser limitation")
    require(intake["detection"]["resolvedKind"] == "binary", "unfamiliar bytes must be described as binary")
    unknown_inspection = call("unfamiliar-inspection", ["inspect", "--deep", str(unknown)], source=unknown)
    require(unknown_inspection["notInspected"], "unknown-format inspection gap must remain explicit")
    before = call("before-sharing", ["inspect", "--deep", str(supported)], source=supported)
    require(EXPECTED_FINDINGS <= {finding["code"] for finding in before["findings"]}, "supported inspection missed a planted finding")
    require({"has_properties", "has_comments", "has_hidden_text"} <= set(before["flags"]), "supported inspection missed a planted flag")
    output = root / "outputs/ready-to-review.docx"
    clean = call("make-clean-copy", ["clean", "-o", str(output), str(supported)], source=supported)
    require(clean["status"] == "completed" and clean["output"]["sha256"] == sha(output), "saved clean output must match its full receipt")
    require({"DocumentProperties", "Comments", "HiddenText"} <= set(clean["result"]["removed"]), "clean receipt must state all planted removals")
    verification = verify_clean_copy(supported, output)
    after = call("after-sharing", ["inspect", "--deep", str(output)], source=output)
    require(not EXPECTED_FINDINGS & {finding["code"] for finding in after["findings"]}, "a planted finding remains after cleaning")
    produced_hash = sha(output)
    refusal = call("existing-output-refused", ["clean", "-o", str(output), str(supported)], exit_code=1, source=supported)
    require(refusal["status"] == "refused" and refusal["code"] == "destination_exists" and sha(output) == produced_hash,
            "an existing output must be refused and preserved")
    mcp_result = mcp_smoke(ufo, root, supported, originals[supported.name], expect_version, environment) if mcp else {"passed": None, "installedAssistantSessionVerified": False}
    require(originals == {path.name: sha(path) for path in (unknown, supported)}, "a source changed during the demonstration")
    require(identity == launcher_artifact(ufo), "the tested application image changed")
    report = {"schema": "com.krauq.ufo.first-run-demo", "schemaVersion": 1, "status": "passed", "engineVersion": expect_version,
        "fictionalInputs": True, "network": "not_used", "license": "free", "sourceHashesUnchanged": originals,
        "demoSha256": sha(Path(__file__)),
        "artifact": identity, "commands": commands, "elapsedSeconds": round(time.monotonic() - started, 3),
        "unknownFile": {"status": intake["status"], "sizeBytes": intake["source"]["sizeBytes"], "sha256": intake["source"]["sha256"],
                        "probe": intake["probe"], "notInspected": unknown_inspection["notInspected"]},
        "supportedInspection": {"expectedFindingCodes": sorted(EXPECTED_FINDINGS), "beforeNotInspected": before["notInspected"],
                                "afterNotInspected": after["notInspected"], "remainingFindings": after["findings"]},
        "cleanCopy": {**verification, "outputSha256": produced_hash, "skipped": clean["result"]["skipped"]}, "mcp": mcp_result}
    write_json(root / "manifest.json", report)
    (root / "START_HERE.txt").write_text(
        "Your first look at UFO CLI\n\n"
        "1. Unfamiliar file: receipts/unfamiliar-info.json has its size, hash and detected kind.\n"
        "   Its dedicated parser is unavailable. receipts/unfamiliar-inspection.json keeps that inspection gap.\n"
        "2. Before sharing: receipts/before-sharing.json finds document properties, a reviewer comment and hidden text in a synthetic DOCX.\n"
        "3. Review a new copy: outputs/ready-to-review.docx preserves the public sentence and removes those planted items.\n"
        "   Independent ZIP/XML checks and all source hashes are recorded in manifest.json.\n\n"
        "Full JSON receipts remain in receipts/. Review findings, skipped categories and notInspected on your own files.\n"
        "The small fixture proves only these tested operations. Screening is format-specific and is not an antivirus verdict.\n"
        "The optional MCP check is a local protocol smoke, not an installed assistant discovery session.\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ufo", default="ufo", help="installed app-image executable")
    parser.add_argument("--output", type=Path, default=Path("ufo-first-look"), help="new directory, never replaces existing content")
    parser.add_argument("--expect-version", default="1.5.1")
    parser.add_argument("--mcp", action="store_true", help="also test local stdio discovery and one inspect call")
    args = parser.parse_args(argv)
    executable = shutil.which(args.ufo)
    if not executable:
        parser.error("Install UFO first: https://universalfileopener.com/cli/#install")
    try:
        report = run_demo(Path(executable), args.output.resolve(), args.expect_version, args.mcp)
    except (OSError, ValueError, KeyError) as error:
        print(f"First-use demo stopped: {error}. Completed files remain in {args.output}; retry with a new directory.", file=sys.stderr)
        return 1
    print(f"Passed {len(report['commands'])} CLI checks. Review {args.output / 'START_HERE.txt'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
