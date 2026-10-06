#!/usr/bin/env python3
"""Validate UFO receipts (JSON Lines or single JSON) against the checked-in schemas.

Each receipt names its own contract in its ``schema`` field, so one command
checks any mix of intake, inspection, text, clean, render, convert, and unpack
receipts:

    ufo inspect --deep *.docx | python3 tools/cli/validate_receipts.py
    python3 tools/cli/validate_receipts.py receipts.jsonl report.json

Exit 0 when every receipt validates, 1 when any does not, 2 on usage or a
missing ``jsonschema`` module. This is the same check a buyer can run on the
receipts a pipeline stores.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
from urllib.parse import urldefrag, urljoin

try:
    import jsonschema
except ModuleNotFoundError:  # pragma: no cover - environment dependent
    print("jsonschema is required: pip install jsonschema", file=sys.stderr)
    sys.exit(2)

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "docs" / "cli" / "schemas"
BY_CONTRACT = {
    ("com.krauq.ufo.intake-receipt", 1): "ufo-intake-receipt-v1.schema.json",
    ("com.krauq.ufo.inspection-report", 1): "ufo-inspection-report-v1.schema.json",
    ("com.krauq.ufo.folder-report", 1): "ufo-folder-report-v1.schema.json",
    ("com.krauq.ufo.watch-event", 1): "ufo-watch-event-v1.schema.json",
    ("com.krauq.ufo.sample-receipt", 1): "ufo-sample-receipt-v1.schema.json",
    ("com.krauq.ufo.compare-report", 1): "ufo-compare-report-v1.schema.json",
    ("com.krauq.ufo.text-receipt", 1): "ufo-text-receipt-v1.schema.json",
    ("com.krauq.ufo.docx-paragraphs", 1): "ufo-docx-paragraphs-v1.schema.json",
    ("com.krauq.ufo.xlsx-cells", 1): "ufo-xlsx-cells-v1.schema.json",
    ("com.krauq.ufo.pptx-slides", 1): "ufo-pptx-slides-v1.schema.json",
    ("com.krauq.ufo.pdf-pages", 1): "ufo-pdf-pages-v1.schema.json",
    ("com.krauq.ufo.recognized-pages", 1): "ufo-recognized-pages-v1.schema.json",
    ("com.krauq.ufo.text-lines", 1): "ufo-text-lines-v1.schema.json",
    ("com.krauq.ufo.json-fields", 1): "ufo-json-fields-v1.schema.json",
    ("com.krauq.ufo.pdf-outline", 1): "ufo-pdf-outline-v1.schema.json",
    ("com.krauq.ufo.pdf-forms", 1): "ufo-pdf-forms-v1.schema.json",
    ("com.krauq.ufo.epub-chapters", 1): "ufo-epub-chapters-v1.schema.json",
    ("com.krauq.ufo.delimited-fields", 1): "ufo-delimited-fields-v1.schema.json",
    ("com.krauq.ufo.find-results", 1): "ufo-find-results-v1.schema.json",
    ("com.krauq.ufo.find-folder-results", 1): "ufo-find-folder-results-v1.schema.json",
    ("com.krauq.ufo.clean-receipt", 1): "ufo-clean-receipt-v1.schema.json",
    ("com.krauq.ufo.render-receipt", 1): "ufo-render-receipt-v1.schema.json",
    ("com.krauq.ufo.edit-receipt", 1): "ufo-edit-receipt-v1.schema.json",
    ("com.krauq.ufo.combine-receipt", 1): "ufo-combine-receipt-v1.schema.json",
    ("com.krauq.ufo.create-receipt", 1): "ufo-create-receipt-v1.schema.json",
    ("com.krauq.ufo.batch-manifest", 1): "ufo-batch-manifest-v1.schema.json",
    ("com.krauq.ufo.convert-receipt", 1): "ufo-convert-receipt-v1.schema.json",
    ("com.krauq.ufo.unpack-receipt", 1): "ufo-unpack-receipt-v1.schema.json",
    ("com.krauq.ufo.capabilities", 1): "ufo-capabilities-v1.schema.json",
    ("com.krauq.ufo.file-capabilities", 1): "ufo-file-capabilities-v1.schema.json",
    ("com.krauq.ufo.edit-targets", 1): "ufo-edit-targets-v1.schema.json",
    ("com.krauq.ufo.license-status", 1): "ufo-license-status-v1.schema.json",
    ("com.krauq.ufo.license-refusal", 1): "ufo-license-refusal-v1.schema.json",
    ("com.krauq.ufo.doctor-report", 1): "ufo-doctor-report-v1.schema.json",
    ("com.krauq.ufo.compatibility-audit-summary", 1): "ufo-compatibility-audit-summary-v1.schema.json",
    ("com.krauq.ufo.compatibility-audit-summary", 2): "ufo-compatibility-audit-summary-v2.schema.json",
    ("com.krauq.ufo.compatibility-audit-comparison", 1): "ufo-compatibility-audit-comparison-v1.schema.json",
    ("com.krauq.ufo.compatibility-audit-comparison", 2): "ufo-compatibility-audit-comparison-v2.schema.json",
}
_validators: dict[tuple[str, int], jsonschema.Draft202012Validator] = {}


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def parse_json(text: str):
    return json.loads(text, object_pairs_hook=unique_object)


def _offline_validator(schema: dict, resources: list[dict]) -> jsonschema.Draft202012Validator:
    """Resolve embedded contracts only from the bundled schema catalogue."""
    local_ids = {resource["$id"] for resource in resources} | {schema["$id"]}

    def check_references(value, base):
        if isinstance(value, dict):
            if isinstance(value.get("$id"), str):
                base = urljoin(base, value["$id"])
            for keyword in ("$ref", "$dynamicRef"):
                if isinstance(value.get(keyword), str):
                    uri = urljoin(base, value[keyword])
                    if urldefrag(uri)[0] not in local_ids:
                        raise ValueError("External schema resolution is disabled: " + uri)
            for child in value.values():
                check_references(child, base)
        elif isinstance(value, list):
            for child in value:
                check_references(child, base)

    # Both validator APIs preload standard metaschemas; references must still
    # be limited to our explicit catalogue, rather than any implicit resources.
    for resource in [schema, *resources]:
        check_references(resource, resource["$id"])

    if "registry" in inspect.signature(jsonschema.Draft202012Validator).parameters:
        from referencing import Registry, Resource
        from referencing.exceptions import NoSuchResource

        def deny_retrieval(uri: str):
            raise NoSuchResource(ref=uri)

        registry = Registry(retrieve=deny_retrieval).with_resources(
            (resource["$id"], Resource.from_contents(resource)) for resource in resources
        )
        return jsonschema.Draft202012Validator(schema, registry=registry)

    # Debian/Ubuntu's jsonschema 4.10 predates Registry and referencing. Keep
    # their checkout-free bootstrap supported without adding network lookup.
    class LocalResolver(jsonschema.RefResolver):
        def resolve(self, ref):
            uri = urljoin(self.resolution_scope, ref)
            if urldefrag(uri)[0] not in local_ids:
                raise ValueError("External schema resolution is disabled: " + uri)
            return super().resolve(ref)

        def resolve_remote(self, uri):
            raise ValueError("External schema resolution is disabled: " + uri)

    resolver = LocalResolver.from_schema(schema, store={resource["$id"]: resource for resource in resources})
    return jsonschema.Draft202012Validator(schema, resolver=resolver)


def validator_for(schema_name: str, schema_version: int) -> jsonschema.Draft202012Validator | None:
    contract = (schema_name, schema_version)
    file_name = BY_CONTRACT.get(contract)
    if file_name is None:
        return None
    if contract not in _validators:
        schema = json.loads((SCHEMAS / file_name).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        # Discovery embeds existing contracts. Resolve their references locally,
        # never by fetching public $id locations over the network.
        if contract in {("com.krauq.ufo.file-capabilities", 1), ("com.krauq.ufo.edit-targets", 1)}:
            resources = [json.loads((SCHEMAS / name).read_text(encoding="utf-8")) for name in set(BY_CONTRACT.values())]
            _validators[contract] = _offline_validator(schema, resources)
            return _validators[contract]
        _validators[contract] = jsonschema.Draft202012Validator(schema)
    return _validators[contract]


def documents(text: str):
    stripped = text.strip()
    if not stripped:
        return
    try:
        document = parse_json(stripped)
    except json.JSONDecodeError:
        pass
    else:
        yield 1, document
        return
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.removesuffix("\r")
        if not line.strip():
            raise ValueError(f"blank JSONL record at line {number}")
        try:
            yield number, parse_json(line)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"line {number}: {error}") from error


def main(argv: list[str]) -> int:
    try:
        sources = (
            [("stdin", sys.stdin.read())]
            if len(argv) == 1
            else [(name, Path(name).read_text(encoding="utf-8")) for name in argv[1:]]
        )
    except (OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    checked = 0
    problems = 0
    for label, text in sources:
        try:
            docs = list(documents(text))
        except (json.JSONDecodeError, ValueError) as error:
            print(f"{label}: invalid JSON: {error}")
            return 1
        for number, doc in docs:
            checked += 1
            name = doc.get("schema") if isinstance(doc, dict) else None
            version = doc.get("schemaVersion") if isinstance(doc, dict) else None
            validator = (
                validator_for(name, version)
                if isinstance(name, str) and isinstance(version, int) and not isinstance(version, bool)
                else None
            )
            if validator is None:
                problems += 1
                print(f"{label}:{number}: unknown contract {(name, version)!r}")
                continue
            errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
            for error in errors:
                problems += 1
                where = "/".join(str(p) for p in error.path) or "<root>"
                print(f"{label}:{number}: {name} {where}: {error.message}")
    print(f"{checked} receipt(s) checked, {problems} problem(s)")
    return 0 if checked and problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
