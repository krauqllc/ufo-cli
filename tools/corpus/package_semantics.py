#!/usr/bin/env python3
"""What an edit changed in an Office package outside its write set, and whether anyone could tell.

Preservation judged byte for byte is the strictest reading, and every caller keeps it: a member a
task does not need keeps its exact bytes. This module adds the two readings that make that check
usable across tools.

``stamp_filter`` sets aside the provenance and save stamps editors add, and nothing else:

* custom document properties named ``UFO.*`` or ``OfficeCLI.*`` in the part the root
  relationships name as the custom-properties part, when every other property (name, type and
  value) is the same on both sides, including a new part that holds only such properties;
* the one ``[Content_Types].xml`` override and the one root relationship that part needs;
* ``dcterms:modified``, ``cp:lastModifiedBy`` and ``cp:revision`` in the core properties;
* the application name and the document statistics in the extended properties (never
  ``Company``, the titles of parts or anything else a person wrote).

A filtered member ends up identical in the returned maps, so a byte check runs on them
unchanged. When the rest of a stamped part was also re-serialized, the stamp is still set aside
but the member is left as a same-content rewrite of the source, which a byte check still fails
and ``classify_member`` reports as ``rewritten``.

``grade`` then classifies what remains outside the write set:

* ``preserved``: nothing differs;
* ``rewritten``: only same-content rewrites and bookkeeping no reader can see (relationship ids
  renumbered, a calculation chain dropped, a new slide's own copied parts). A rewrite is its own
  class, never merged into ``preserved``, because re-serializing a part is where bugs start;
* ``changed``: at least one difference a person or a program reading the document could
  notice. Each is reported under a category.

"Same content" is equality after a canonicalization that ignores attribute order, namespace
prefixes, whitespace-only text, the XML declaration, attributes written out at their default
value, and editor bookkeeping (``w:rsid*``, ``w14:paraId``, ``w14:textId``,
``w16cid:durableId``, PowerPoint ``creationId`` extensions, ``x14ac:dyDescent``, ``xr:uid``,
proofing marks). With both packages at hand, relationship ids are compared by what they point
at. A workbook is judged by what a reader sees: every cell resolved to its text or number,
formula, saved result and full formatting, independent of how a writer numbered its shared
strings, styles or parts, and each sheet's columns, rows, merges, rules, filters, print setup,
headers, footers and extension lists, the workbook's sheets and names, and its colour palette.

Standard library only, so it can ship in the evaluation kit. The inputs are packages a caller
already read under its own size bound, and the work here is bounded again: bytes parsed per part
and per call, elements, nesting depth and cells. A part over a bound is reported as not
compared, which grades as changed, never as preserved.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import posixpath
import re
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- names

CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
EXTENDED = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
CORE = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
X14AC = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
A16 = "http://schemas.microsoft.com/office/drawing/2014/main"
OFFICE_VML = "urn:schemas-microsoft-com:office:office"

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
REL_OFFICE_DOCUMENT = REL + "officeDocument"
REL_CUSTOM = REL + "custom-properties"
REL_EXTENDED = REL + "extended-properties"
REL_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CT_CUSTOM = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
CUSTOM_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"

#: A custom property whose name starts with one of these is a tool's provenance stamp.
TOOL_STAMP_PREFIXES = ("UFO.", "OfficeCLI.")
#: Core properties every save rewrites: when, by whom, how many times.
CORE_STAMP_FIELDS = frozenset({(DCTERMS, "modified"), (CORE, "lastModifiedBy"), (CORE, "revision")})
#: Extended properties that name the writing application or count the document. `Company`,
#: `Manager`, `Template`, the titles of parts and anything a person typed are not stamps.
APP_STAMP_FIELDS = frozenset((EXTENDED, name) for name in (
    "Application", "AppVersion", "TotalTime", "Pages", "Words", "Characters", "CharactersWithSpaces",
    "Lines", "Paragraphs", "Slides", "Notes", "HiddenSlides", "MMClips", "DocSecurity",
    "HyperlinksChanged", "LinksUpToDate", "ScaleCrop", "SharedDoc",
))

PRESERVED, REWRITTEN, CHANGED = "preserved", "rewritten", "changed"
VERDICTS = (PRESERVED, REWRITTEN, CHANGED)

# ---------------------------------------------------------------- bounds

MAX_MEMBERS = 20_000
MAX_PART_BYTES = 32 * 1024 * 1024
MAX_WORK_BYTES = 512 * 1024 * 1024
MAX_ELEMENTS = 2_000_000
MAX_DEPTH = 256
MAX_CELLS = 4_000_000
MAX_LISTED = 24
MAX_EXAMPLES = 6
PARSE_CHUNK = 1 << 20


class Unjudged(ValueError):
    """A part this module does not judge: over a bound, not XML, or not well formed.

    A ValueError, so every evaluator that already treats a malformed package as a failure
    treats a part over one of these bounds the same way.
    """


class Budget:
    """The work one call may do, charged as parts are parsed and cells are read."""

    def __init__(self, work_bytes: int = MAX_WORK_BYTES, cells: int = MAX_CELLS) -> None:
        self.work_bytes = work_bytes
        self.cells = cells

    def charge(self, size: int) -> None:
        if size > MAX_PART_BYTES:
            raise Unjudged(f"a part of {size} bytes is over the {MAX_PART_BYTES} byte comparison bound")
        self.work_bytes -= size
        if self.work_bytes < 0:
            raise Unjudged(f"the {MAX_WORK_BYTES} byte comparison work bound was reached")

    def cell(self) -> None:
        self.cells -= 1
        if self.cells < 0:
            raise Unjudged(f"the {MAX_CELLS} cell comparison bound was reached")


# ---------------------------------------------------------------- bounded parsing

_DTD = re.compile(rb"<!(?:DOCTYPE|ENTITY)")
_MC_CHOICE = f"{{{MC}}}Choice"
_PREFIX_LISTS = {f"{{{MC}}}Ignorable", f"{{{MC}}}MustUnderstand"}
_QNAME_LISTS = {f"{{{MC}}}ProcessContent", f"{{{MC}}}PreserveElements", f"{{{MC}}}PreserveAttributes"}
_XSI_TYPE = f"{{{XSI}}}type"


class _Tree:
    """A parsed part and the attribute values whose prefixes were resolved while parsing."""

    __slots__ = ("root", "qnames")

    def __init__(self, root: ET.Element, qnames: dict) -> None:
        self.root = root
        self.qnames = qnames


class _Parse:
    def __init__(self) -> None:
        self.scopes = [{"xml": XML_NS}]
        self.pending: list[tuple[str, str]] = []
        self.qnames: dict = {}
        self.root = None
        self.count = 0

    def consume(self, events) -> None:
        for event, item in events:
            if event == "start":
                self.count += 1
                if self.count > MAX_ELEMENTS:
                    raise Unjudged(f"a part with over {MAX_ELEMENTS} elements is over the comparison bound")
                scope = self.scopes[-1]
                if self.pending:
                    scope = dict(scope)
                    scope.update(self.pending)
                    self.pending = []
                self.scopes.append(scope)
                if len(self.scopes) > MAX_DEPTH + 1:
                    raise Unjudged(f"a part nested over {MAX_DEPTH} levels deep is over the comparison bound")
                if self.root is None:
                    self.root = item
                if item.attrib:
                    for key, value in item.attrib.items():
                        if key in _PREFIX_LISTS or (key == "Requires" and item.tag == _MC_CHOICE):
                            resolved = " ".join(sorted(scope.get(token, "?" + token) for token in value.split()))
                        elif key in _QNAME_LISTS:
                            resolved = " ".join(sorted(_qname(token, scope) for token in value.split()))
                        elif key == _XSI_TYPE:
                            resolved = _qname(value.strip(), scope)
                        else:
                            continue
                        self.qnames.setdefault(item, {})[key] = resolved
            elif event == "end":
                self.scopes.pop()
            else:
                self.pending.append(item)


def _qname(token: str, scope: dict) -> str:
    prefix, colon, local = token.rpartition(":")
    if not colon:
        return f"{{{scope.get('', '')}}}{token}"
    return f"{{{scope.get(prefix, '?' + prefix)}}}{local}"


def parse_part(data: bytes, budget: Budget | None = None) -> _Tree:
    """One XML part, parsed under the size, element and depth bounds, with no DTD accepted."""
    size = len(data)
    if budget is not None:
        budget.charge(size)
    elif size > MAX_PART_BYTES:
        raise Unjudged(f"a part of {size} bytes is over the {MAX_PART_BYTES} byte comparison bound")
    if _DTD.search(data):
        raise Unjudged("the part declares a DTD, which no Office part carries")
    parser = ET.XMLPullParser(events=("start", "end", "start-ns"))
    state = _Parse()
    try:
        for offset in range(0, size, PARSE_CHUNK):
            parser.feed(data[offset:offset + PARSE_CHUNK])
            state.consume(parser.read_events())
        parser.close()
        state.consume(parser.read_events())
    except ET.ParseError as error:
        raise Unjudged(f"not well-formed XML: {error}") from None
    if state.root is None:
        raise Unjudged("the part holds no element")
    return _Tree(state.root, state.qnames)


def _split(name: str) -> tuple[str, str]:
    if name[:1] == "{":
        namespace, _, local = name[1:].partition("}")
        return namespace, local
    return "", name


# ---------------------------------------------------------------- canonical form

_BOOLEAN = {"true": "1", "True": "1", "TRUE": "1", "on": "1", "false": "0", "False": "0", "FALSE": "0", "off": "0"}
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_XR = re.compile(r"http://schemas\.microsoft\.com/office/spreadsheetml/20\d\d/revision\d*\Z")
# Editor bookkeeping: revision-session ids, paragraph ids, comment durable ids, font descent
# hints, workbook revision ids, spelling and grammar marks and the last page-break cache.
_BOOKKEEPING_ELEMENTS = frozenset({(W, "proofErr"), (W, "lastRenderedPageBreak"), (W, "rsids"),
                                   (W, "rsid"), (W, "rsidRoot")})
_BOOKKEEPING_ATTRIBUTES = frozenset({(W14, "paraId"), (W14, "textId"), (W16CID, "durableId"),
                                     (X14AC, "dyDescent"), (X14AC, "knownFonts"), (MC, "Ignorable")})
_CREATION_IDS = frozenset({f"{{{P14}}}creationId", f"{{{A16}}}creationId"})
_TEXT_ELEMENTS = frozenset({(W, "t"), (W, "delText"), (W, "instrText"), (W, "delInstrText"), (A, "t"),
                            (S, "t"), (S, "v"), (C, "v"), (M, "t"), (VT, "lpwstr"), (VT, "lpstr"), (VT, "bstr")})
_TEXT_TAGS = frozenset(f"{{{ns}}}{local}" for ns, local in (
    (W, "t"), (W, "delText"), (W, "instrText"), (A, "t"), (S, "t"), (M, "t")))
# WordprocessingML on/off properties whose `w:val` defaults to on when the element is present.
_WORD_TOGGLES = frozenset({
    "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow", "emboss",
    "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "specVanish", "rtl", "cs", "oMath",
    "keepNext", "keepLines", "pageBreakBefore", "widowControl", "suppressLineNumbers",
    "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE",
    "autoSpaceDN", "bidi", "adjustRightInd", "contextualSpacing", "mirrorIndents", "suppressOverlap",
    "titlePg", "rtlGutter", "formProt", "noWrap", "hideMark", "tblHeader", "cantSplit", "bidiVisual",
})
# SpreadsheetML attribute values a reader assumes when the attribute is absent, so writing them
# out changes nothing. None means the attribute is only a writer's hint and never matters.
_S_DEFAULTS = {
    "alignment": {"horizontal": "general", "vertical": "bottom", "wrapText": "0", "shrinkToFit": "0",
                  "indent": "0", "textRotation": "0", "readingOrder": "0", "justifyLastLine": "0",
                  "relativeIndent": "0"},
    "protection": {"locked": "1", "hidden": "0"},
    "pageSetup": {"paperSize": "1", "scale": "100", "firstPageNumber": "1", "fitToWidth": "1",
                  "fitToHeight": "1", "pageOrder": "downThenOver", "orientation": "default",
                  "usePrinterDefaults": "1", "blackAndWhite": "0", "draft": "0", "cellComments": "none",
                  "useFirstPageNumber": "0", "errors": "displayed", "horizontalDpi": "600",
                  "verticalDpi": "600", "copies": "1"},
    "printOptions": {"horizontalCentered": "0", "verticalCentered": "0", "headings": "0", "gridLines": "0",
                     "gridLinesSet": "1"},
    "pageMargins": {"left": "0.7", "right": "0.7", "top": "0.75", "bottom": "0.75", "header": "0.3",
                    "footer": "0.3"},
    "sheetView": {"showGridLines": "1", "showRowColHeaders": "1", "showZeros": "1", "defaultGridColor": "1",
                  "rightToLeft": "0", "tabSelected": "0", "showRuler": "1", "showOutlineSymbols": "1",
                  "showWhiteSpace": "1", "view": "normal", "windowProtection": "0", "showFormulas": "0",
                  "zoomScale": "100", "zoomScaleNormal": "0", "zoomScalePageLayoutView": "0",
                  "zoomScaleSheetLayoutView": "0", "colorId": "64", "topLeftCell": "A1"},
    "selection": {"pane": "topLeft", "activeCell": "A1", "activeCellId": "0", "sqref": "A1"},
    "pane": {"xSplit": "0", "ySplit": "0", "activePane": "topLeft", "state": "split"},
    "workbookView": {"visibility": "visible", "minimized": "0", "showHorizontalScroll": "1",
                     "showVerticalScroll": "1", "showSheetTabs": "1", "tabRatio": "600", "firstSheet": "0",
                     "activeTab": "0", "autoFilterDateGrouping": "1"},
    "col": {"collapsed": "0", "hidden": "0", "outlineLevel": "0", "customWidth": None, "bestFit": "0",
            "phonetic": "0"},
    "definedName": {"function": "0", "vbProcedure": "0", "xlm": "0", "hidden": "0", "publishToServer": "0",
                    "workbookParameter": "0"},
    "dataValidation": {"allowBlank": "0", "showErrorMessage": "0", "showInputMessage": "0",
                       "showDropDown": "0", "errorStyle": "stop", "imeMode": "noControl",
                       "operator": "between", "type": "none"},
    "cfRule": {"stopIfTrue": "0", "aboveAverage": "1", "percent": "0", "bottom": "0", "equalAverage": "0"},
    "b": {"val": "1"}, "i": {"val": "1"}, "strike": {"val": "1"}, "outline": {"val": "1"},
    "shadow": {"val": "1"}, "condense": {"val": "1"}, "extend": {"val": "1"}, "u": {"val": "single"},
    "patternFill": {"patternType": "none"},
    "brk": {"min": "0", "max": "0", "man": "0", "pt": "0"},
    "filterColumn": {"hiddenButton": "0", "showButton": "1"},
    "border": {"diagonalUp": "0", "diagonalDown": "0", "outline": "1"},
    "headerFooter": {"alignWithMargins": "1", "scaleWithDoc": "1", "differentFirst": "0",
                     "differentOddEven": "0"},
    "sheetProtection": {"objects": "0", "scenarios": "0", "formatCells": "1", "formatColumns": "1",
                        "formatRows": "1", "insertColumns": "1", "insertRows": "1", "insertHyperlinks": "1",
                        "deleteColumns": "1", "deleteRows": "1", "selectLockedCells": "0", "sort": "1",
                        "autoFilter": "1", "pivotTables": "1", "selectUnlockedCells": "0", "sheet": "0"},
    "workbookPr": {"date1904": "0", "dateCompatibility": "1", "showObjects": "all", "filterPrivacy": "0",
                   "backupFile": "0", "hidePivotFieldList": "0", "publishItems": "0", "showBorderUnselectedTables": "1",
                   "promptedSolutions": "0", "showInkAnnotation": "1", "saveExternalLinkValues": "1",
                   "updateLinks": "userSet", "checkCompatibility": "0", "autoCompressPictures": "1",
                   "refreshAllConnections": "0", "allowRefreshQuery": "0", "showPivotChartFilter": "0",
                   "defaultThemeVersion": None},
    "sheetPr": {"enableFormatConditionsCalculation": "1", "filterMode": "0", "published": "1",
                "syncHorizontal": "0", "syncVertical": "0", "transitionEvaluation": "0", "transitionEntry": "0"},
    "outlinePr": {"applyStyles": "0", "summaryBelow": "1", "summaryRight": "1", "showOutlineSymbols": "1"},
    "pageSetUpPr": {"autoPageBreaks": "1", "fitToPage": "0"},
    "table": {"headerRowCount": "1", "insertRow": "0", "insertRowShift": "0", "totalsRowCount": "0",
              "totalsRowShown": "1", "published": "0"},
}
_S_EMPTY_WHEN_DEFAULT = frozenset({"alignment", "protection", "printOptions", "pageMargins", "selection", "pane",
                                   "sheetPr", "outlinePr", "pageSetUpPr"})
# Header and footer texts: an empty one prints what an absent one prints.
_S_EMPTY_TEXT = frozenset({"oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"})
_S_BORDER_SIDES = frozenset({"left", "right", "top", "bottom", "diagonal", "start", "end", "vertical", "horizontal"})
# SpreadsheetML and chart attributes whose value is text even when it looks like a number.
_TEXTUAL_ATTRIBUTES = frozenset({"rgb", "formatCode", "name", "uri", "ref", "sqref", "r", "id", "guid",
                                 "uid", "text", "spans", "activeCell", "topLeftCell", "codeName"})
_O_RELID = f"{{{OFFICE_VML}}}relid"
_UNORDERED_ROOTS = frozenset({f"{{{CT}}}Types", f"{{{PR}}}Relationships"})
_UNORDERED_TOP = frozenset({f"{{{CORE}}}coreProperties", f"{{{EXTENDED}}}Properties", f"{{{CUSTOM}}}Properties"})


class _Rules:
    __slots__ = ("qnames", "references", "rels_owner", "sort_all", "sort_root", "dxfs")

    def __init__(self, qnames=None, references=None, rels_owner=None, sort_all=False, sort_root=False,
                 dxfs=None) -> None:
        self.qnames = qnames or {}
        self.references = references
        self.rels_owner = rels_owner
        self.sort_all = sort_all
        self.sort_root = sort_root
        self.dxfs = dxfs


def _integer(value, default: int = -1) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _same_value(value: str, default: str) -> bool:
    if value == default:
        return True
    if _NUMBER.fullmatch(value) and _NUMBER.fullmatch(default):
        return float(value) == float(default)
    return False


def _attributes(element: ET.Element, namespace: str, local: str, rules: _Rules, edges: bool) -> tuple:
    if not element.attrib:
        return ()
    resolved = rules.qnames.get(element)
    defaults = _S_DEFAULTS.get(local) if namespace == S else None
    out = []
    for key, value in element.attrib.items():
        key_namespace, key_local = _split(key)
        if key_namespace:
            if ((key_namespace == W and key_local.startswith("rsid"))
                    or (key_namespace, key_local) in _BOOKKEEPING_ATTRIBUTES or _XR.match(key_namespace)):
                continue
            if key_namespace == XML_NS and key_local == "space" and not edges:
                continue
        if resolved and key in resolved:
            value = resolved[key]
        value = _BOOLEAN.get(value, value)
        if key_namespace == R_NS or key == _O_RELID:
            if namespace == S and local == "pageSetup":
                continue  # the printer settings link: the printer settings part is judged on its own
            if rules.references is not None:
                value = rules.references.get(value, f"(no relationship {value})")
        elif namespace == PR and not key_namespace:
            if key_local == "Target" and rules.rels_owner is not None and element.get("TargetMode") != "External":
                value = resolve_target(rules.rels_owner, value).lower()
            elif key_local == "TargetMode" and value == "Internal":
                continue
        elif namespace == CT and not key_namespace and key_local in ("PartName", "Extension", "ContentType"):
            value = value.lower()
        elif namespace == CUSTOM and local == "property" and key_local == "pid":
            continue
        elif not key_namespace and namespace in (S, C):
            if defaults and key_local in defaults:
                default = defaults[key_local]
                if default is None or _same_value(value, default):
                    continue
            if key_local == "rgb":
                value = value.upper()
            elif key_local not in _TEXTUAL_ATTRIBUTES and _NUMBER.fullmatch(value):
                # A formatting number (a width, a tint, a margin) to the 15 significant digits
                # Excel itself keeps; a writer that prints 16 or 17 changes nothing anyone sees.
                value = f"{float(value):.15g}"
            if key_local == "dxfId" and rules.dxfs is not None:
                index = _integer(value)
                value = repr(rules.dxfs[index]) if 0 <= index < len(rules.dxfs) else f"(missing format {value})"
        elif namespace == W and key_namespace == W and key_local == "val" and local in _WORD_TOGGLES and value == "1":
            continue
        out.append((key, value))
    out.sort()
    return tuple(out)


def _canon(element: ET.Element, rules: _Rules, depth: int = 0):
    """An order-aware signature of one element subtree, or None when it carries nothing."""
    tag = element.tag
    if not isinstance(tag, str):
        return None
    namespace, local = _split(tag)
    if (namespace, local) in _BOOKKEEPING_ELEMENTS or (namespace and _XR.match(namespace)):
        return None
    if local == "ext" and len(element) and all(child.tag in _CREATION_IDS for child in element):
        return None
    children = []
    for child in element:
        signature = _canon(child, rules, depth + 1)
        if signature is not None:
            children.append(signature)
    if local == "extLst" and not children:
        return None
    raw = element.text or ""
    leaf = len(element) == 0
    if leaf:
        text = raw
        stripped = text.strip()
        if not stripped and (namespace, local) not in _TEXT_ELEMENTS \
                and element.get(f"{{{XML_NS}}}space") != "preserve":
            text = ""
        elif namespace in (S, C) and local == "v" and _NUMBER.fullmatch(stripped):
            text = repr(float(stripped))
        elif namespace == VT and local == "bool":
            text = _BOOLEAN.get(stripped, stripped)
    else:
        text = "".join(piece for piece in [raw, *(child.tail or "" for child in element)] if piece.strip())
    attributes = _attributes(element, namespace, local, rules, leaf and raw != raw.strip())
    if namespace == S:
        if local in _S_EMPTY_WHEN_DEFAULT and not attributes and not children:
            return None
        if local == "headerFooter" and not children:
            return None
        if local == "sheetProtection" and ("sheet", "1") not in attributes:
            return None
        if local in _S_BORDER_SIDES and not attributes and not children:
            return None
        if local in _S_EMPTY_TEXT and not text and not attributes and not children:
            return None
    if rules.sort_all or (rules.sort_root and depth == 0):
        children.sort()
    return (tag, attributes, text, tuple(children))


def _signature(data: bytes, name: str, budget: Budget, references=None):
    """The comparable signature of one XML part: package lists (content types, relationships)
    compared as sets, property parts by field, every other part in document order."""
    tree = parse_part(data, budget)
    tag = tree.root.tag
    rels_owner = owner_of(name) if tag == f"{{{PR}}}Relationships" else None
    rules = _Rules(tree.qnames, references, rels_owner, tag in _UNORDERED_ROOTS, tag in _UNORDERED_TOP)
    return _canon(tree.root, rules)


def _texts(signature) -> list[str]:
    found: list[str] = []
    pending = [signature]
    while pending:
        node = pending.pop()
        if node is None:
            continue
        if node[0] in _TEXT_TAGS:
            found.append(node[2])
        pending.extend(reversed(node[3]))
    return found


# ---------------------------------------------------------------- relationships

# LibreOffice writes the core-properties relationship under an officeDocument namespace, and
# relationship types are compared as OPC compares URIs here: without regard to case.
_TYPE_ALIASES = {
    REL_CORE.lower(): {REL_CORE.lower(),
                       "http://schemas.openxmlformats.org/officedocument/2006/relationships/metadata/core-properties"},
}


def _same_type(found: str, kind: str) -> bool:
    found, kind = found.lower(), kind.lower()
    return found == kind or found in _TYPE_ALIASES.get(kind, ())


def owner_of(rels_name: str) -> str:
    """The part a relationships part belongs to: `word/_rels/document.xml.rels` is
    `word/document.xml`, and `_rels/.rels` belongs to the package itself, named ''."""
    folder, _, base = rels_name.rpartition("/")
    parent, _, marker = folder.rpartition("/") if "/" in folder else ("", "", folder)
    if marker != "_rels" or not base.endswith(".rels"):
        return rels_name
    base = base[:-len(".rels")]
    return f"{parent}/{base}" if parent else base


def relationships_path(owner: str) -> str:
    folder, _, base = owner.rpartition("/")
    return f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"


def resolve_target(owner: str, target: str) -> str:
    """A relationship target resolved against the part that owns it, as a package member name.

    A target that is only a fragment (`#_ftn1`) points into the owner itself; the fragment is
    kept, so two such links stay two different targets.
    """
    target, hash_mark, fragment = target.partition("#")
    if not target:
        path = owner
    else:
        path = target[1:] if target.startswith("/") else posixpath.join(posixpath.dirname(owner), target)
        path = posixpath.normpath(path) if path else path
        path = "" if path == "." else path.lstrip("/")
    return f"{path}#{fragment}" if hash_mark else path


class _Package:
    """One side of a comparison: its members, looked up case-insensitively as OPC names are."""

    def __init__(self, parts: dict[str, bytes], budget: Budget, renames: dict | None = None) -> None:
        self.parts = parts
        self.budget = budget
        self.lower = {}
        for name in parts:
            self.lower.setdefault(name.lower(), name)
        self.renames = renames or {}
        self._relationships: dict = {}
        self._trees: dict = {}

    def member(self, path: str | None) -> str | None:
        return None if path is None else self.lower.get(path.lower())

    def tree(self, name: str) -> _Tree:
        if name not in self._trees:
            self._trees[name] = parse_part(self.parts[name], self.budget)
        return self._trees[name]

    def relationships(self, owner: str) -> list[tuple]:
        """Every relationship [owner] declares: (id, type, target, external)."""
        if owner not in self._relationships:
            rows = []
            name = self.member(relationships_path(owner))
            if name is not None:
                for rel in self.tree(name).root:
                    if rel.tag != f"{{{PR}}}Relationship":
                        continue
                    external = rel.get("TargetMode") == "External"
                    target = rel.get("Target") or ""
                    if not external:
                        target = resolve_target(owner, target).lower()
                    rows.append((rel.get("Id"), rel.get("Type") or "", target, external))
            self._relationships[owner] = rows
        return self._relationships[owner]

    def compared(self, target: str, external: bool = False) -> str:
        """A relationship target as a comparison names it: a renamed part by its source name."""
        return target if external else self.renames.get(target, target)

    def references(self, owner: str) -> dict[str, str]:
        return {rid: f"{kind} -> {self.compared(target, external)}"
                for rid, kind, target, external in self.relationships(owner) if rid}

    def linked(self, kind: str, owner: str = "") -> str | None:
        """The member the first internal relationship of [kind] from [owner] points at."""
        for _, rel_type, target, external in self.relationships(owner):
            if _same_type(rel_type, kind) and not external:
                return self.member(target)
        return None


# ---------------------------------------------------------------- the stamp filter

_TOOL_PROPERTY = re.compile(
    rb"<((?:[A-Za-z_][\w.-]*:)?)property\b[^>]*?\bname\s*=\s*([\"'])(?:UFO|OfficeCLI)\.[^\"']*\2[^>]*?"
    rb"(?:/>|>.*?</\1property\s*>)", re.S)
_EMPTY_PROPERTIES_ROOT = re.compile(rb"<((?:[A-Za-z_][\w.-]*:)?)Properties\b([^>]*?)\s*/>")
_OVERRIDE = re.compile(rb"<((?:[A-Za-z_][\w.-]*:)?)Override\b[^>]*?(?:/>|>\s*</\1Override\s*>)")
_RELATIONSHIP = re.compile(rb"<((?:[A-Za-z_][\w.-]*:)?)Relationship\b[^>]*?(?:/>|>\s*</\1Relationship\s*>)")
_ATTRIBUTE = re.compile(rb"([\w:.-]+)\s*=\s*([\"'])(.*?)\2", re.S)
_DECLARATION = re.compile(rb"\A(\xef\xbb\xbf)?<\?xml[^>]*\?>[ \t\r\n]*")


def _field_pattern(fields) -> re.Pattern:
    names = b"|".join(re.escape(local.encode()) for _, local in sorted(fields, key=lambda f: -len(f[1])))
    return re.compile(rb"<((?:[A-Za-z_][\w.-]*:)?)(" + names + rb")\b[^>]*?(?:/>|>.*?</\1\2\s*>)", re.S)


_CORE_FIELD = _field_pattern(CORE_STAMP_FIELDS)
_APP_FIELD = _field_pattern(APP_STAMP_FIELDS)


def _unescape(value: bytes) -> str:
    text = value.decode("utf-8", "replace")
    return (text.replace("&quot;", '"').replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


def _without(data: bytes, pattern: re.Pattern, keep=None) -> bytes:
    """[data] with every match of [pattern] that [keep] does not keep cut out, byte for byte."""
    return pattern.sub(lambda match: match.group(0) if keep is not None and keep(match.group(0)) else b"", data)


def _attribute(element: bytes, name: bytes) -> str | None:
    for key, _, value in _ATTRIBUTE.findall(element):
        if key == name or key.endswith(b":" + name):
            return _unescape(value)
    return None


def _without_tool_properties(data: bytes) -> bytes:
    stripped = _without(data, _TOOL_PROPERTY)
    return _EMPTY_PROPERTIES_ROOT.sub(rb"<\1Properties\2></\1Properties>", stripped)


def _rewrite_of(data: bytes) -> bytes:
    """A same-content rewrite of [data]: its XML declaration taken away, or one put in front.

    Standing in for a stamped member whose remaining bytes were re-serialized, so a byte check
    still sees a rewrite and a content check still sees the source's content.
    """
    match = _DECLARATION.match(data)
    if match:
        return (match.group(1) or b"") + data[match.end():]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return ET.tostring(ET.fromstring(data), encoding="utf-8")
    declaration = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    if data.startswith(b"\xef\xbb\xbf"):
        return b"\xef\xbb\xbf" + declaration + data[3:]
    return declaration + data


def _custom_properties(data: bytes, budget: Budget) -> tuple[Counter, dict] | None:
    """(every property that is not a tool stamp, the tool stamps by name), or None when [data]
    is not a custom-properties part."""
    tree = parse_part(data, budget)
    if tree.root.tag != f"{{{CUSTOM}}}Properties":
        return None
    rules = _Rules(tree.qnames)
    ordinary, stamps = Counter(), {}
    for child in tree.root:
        if child.tag != f"{{{CUSTOM}}}property":
            ordinary[("(element)", child.tag, repr(_canon(child, rules)))] += 1
            continue
        name = child.get("name") or ""
        values = [value for value in child if isinstance(value.tag, str)]
        kind = values[0].tag if values else ""
        value = repr(_canon(values[0], rules)) if values else ""
        if name.startswith(TOOL_STAMP_PREFIXES):
            stamps[name] = (kind, value)
        else:
            ordinary[(name, kind, value)] += 1
    return ordinary, stamps


def tool_properties(parts: dict[str, bytes]) -> dict[str, str]:
    """The tool stamp properties of the custom-properties part the package links: name to text."""
    try:
        package = _Package(parts, Budget())
        name = package.linked(REL_CUSTOM)
        if name is None:
            return {}
        tree = package.tree(name)
    except Unjudged:
        return {}
    found = {}
    for child in tree.root:
        name = child.get("name") or ""
        if child.tag == f"{{{CUSTOM}}}property" and name.startswith(TOOL_STAMP_PREFIXES):
            found[name] = "".join(child.itertext())
    return found


class _Stamps:
    def __init__(self, before: dict[str, bytes], after: dict[str, bytes]) -> None:
        self.before, self.after = before, after
        self.budget = Budget(work_bytes=64 * 1024 * 1024)
        self.first = _Package(before, self.budget)
        self.second = _Package(after, self.budget)
        self.filtered = dict(after)
        self.rows: list[dict] = []

    def neutralize(self, name: str, identical: bool, description: str) -> None:
        if name in self.before:
            self.filtered[name] = self.before[name] if identical else _rewrite_of(self.before[name])
        else:
            self.filtered.pop(name, None)
        rest = "identical" if identical else "rewritten"
        if not identical:
            description += "; the rest of the part was rewritten"
        self.rows.append({"member": name, "description": description, "rest": rest})

    def run(self) -> None:
        custom = self._guarded(self.custom)
        if custom:
            self._guarded(lambda: self.content_types(custom))
            self._guarded(lambda: self.root_relationships(custom))
        self._guarded(lambda: self.save_stamp(REL_CORE, f"{{{CORE}}}coreProperties", CORE_STAMP_FIELDS,
                                              _CORE_FIELD, "save stamp"))
        self._guarded(lambda: self.save_stamp(REL_EXTENDED, f"{{{EXTENDED}}}Properties", APP_STAMP_FIELDS,
                                              _APP_FIELD, "application or statistics field"))

    @staticmethod
    def _guarded(step):
        """A stamp this filter cannot confirm is not set aside: the difference stays for the byte check."""
        try:
            return step()
        except (ValueError, KeyError, IndexError):
            return None

    def custom(self) -> set[str] | None:
        """The custom-properties part names involved, when every difference in them is a stamp."""
        first, second = self.first.linked(REL_CUSTOM), self.second.linked(REL_CUSTOM)
        if first is None and second is None:
            return None

        def effective(package, name):
            if name is None:
                return Counter(), {}
            return _custom_properties(package.parts[name], self.budget)

        one, other = effective(self.first, first), effective(self.second, second)
        if one is None or other is None or one[0] != other[0]:
            return None
        names = {name for name in (first, second) if name}
        for name in sorted(names):
            old, new = self.before.get(name), self.after.get(name)
            if old == new:
                continue
            old_stamps = _custom_properties(old, self.budget) if old is not None else (Counter(), {})
            new_stamps = _custom_properties(new, self.budget) if new is not None else (Counter(), {})
            if old_stamps is None or new_stamps is None or old_stamps[1] == new_stamps[1]:
                continue
            if (old is None and new_stamps[0]) or (new is None and old_stamps[0]):
                continue
            touched = sorted(key for key in old_stamps[1].keys() | new_stamps[1].keys()
                             if old_stamps[1].get(key) != new_stamps[1].get(key))
            verb = "added" if old is None else "removed" if new is None else "written"
            # A part added or dropped whole leaves nothing else behind; a part both sides carry
            # is compared byte for byte once the tool's own properties are cut out of each.
            identical = old is None or new is None or _without_tool_properties(old) == _without_tool_properties(new)
            self.neutralize(name, identical, f"{name}: tool stamp properties {verb}: {', '.join(touched)}")
        return names

    def content_types(self, custom: set[str]) -> None:
        name = self.first.member("[Content_Types].xml")
        if name is None or name not in self.after or self.before[name] == self.after[name]:
            return
        targets = {"/" + part.lower() for part in custom}

        def entries(data):
            root = parse_part(data, self.budget).root
            if root.tag != f"{{{CT}}}Types":
                raise Unjudged("not a content types part")
            stamp, rest = Counter(), Counter(root.attrib.items())
            for child in root:
                local = _split(child.tag)[1]
                if local == "Override":
                    entry = ("Override", "/" + (child.get("PartName") or "").lstrip("/").lower(),
                             (child.get("ContentType") or "").lower())
                    (stamp if entry[1] in targets else rest)[entry] += 1
                elif local == "Default":
                    rest[("Default", (child.get("Extension") or "").lower(), (child.get("ContentType") or "").lower())] += 1
                else:
                    rest[("other", repr(_canon(child, _Rules())))] += 1
            return stamp, rest

        (stamp_one, rest_one), (stamp_two, rest_two) = entries(self.before[name]), entries(self.after[name])
        if rest_one != rest_two or stamp_one == stamp_two:
            return
        if any(entry[2] != CT_CUSTOM for entry in stamp_two):
            return

        def ours(element: bytes) -> bool:
            part = _attribute(element, b"PartName") or ""
            return "/" + part.lstrip("/").lower() not in targets
        identical = _without(self.before[name], _OVERRIDE, ours) == _without(self.after[name], _OVERRIDE, ours)
        verb = "added" if sum(stamp_two.values()) > sum(stamp_one.values()) else "changed"
        self.neutralize(name, identical, f"{name}: content-type override for the custom properties {verb}")

    def root_relationships(self, custom: set[str]) -> None:
        name = self.first.member("_rels/.rels")
        if name is None or name not in self.after or self.before[name] == self.after[name]:
            return
        targets = {part.lower() for part in custom}

        def entries(package):
            stamp, rest = Counter(), Counter()
            for row in package.relationships(""):
                (stamp if _same_type(row[1], REL_CUSTOM) and not row[3] and row[2] in targets else rest)[row] += 1
            return stamp, rest

        (stamp_one, rest_one), (stamp_two, rest_two) = entries(self.first), entries(self.second)
        if rest_one != rest_two or stamp_one == stamp_two:
            return

        def ours(element: bytes) -> bool:
            return not _same_type(_attribute(element, b"Type") or "", REL_CUSTOM)
        identical = _without(self.before[name], _RELATIONSHIP, ours) == _without(self.after[name], _RELATIONSHIP, ours)
        verb = "added" if sum(stamp_two.values()) > sum(stamp_one.values()) else "changed"
        self.neutralize(name, identical, f"{name}: root relationship to the custom properties {verb}")

    def save_stamp(self, kind: str, root_tag: str, fields: frozenset, pattern: re.Pattern, label: str) -> None:
        name = self.first.linked(kind)
        if name is None or name != self.second.linked(kind) or self.before[name] == self.after[name]:
            return

        def entries(data):
            tree = parse_part(data, self.budget)
            if tree.root.tag != root_tag:
                raise Unjudged("not the expected properties part")
            rules = _Rules(tree.qnames)
            stamp, rest = Counter(), Counter(tree.root.attrib.items())
            for child in tree.root:
                if not isinstance(child.tag, str):
                    continue
                if len(child) == 0 and not (child.text or "").strip():
                    continue  # an empty element says what an absent one says
                entry = (child.tag, repr(_canon(child, rules)))
                (stamp if _split(child.tag) in fields else rest)[entry] += 1
            return stamp, rest

        (stamp_one, rest_one), (stamp_two, rest_two) = entries(self.before[name]), entries(self.after[name])
        if rest_one != rest_two or stamp_one == stamp_two:
            return
        touched = sorted({_split(tag)[1] for tag, _ in (stamp_one - stamp_two) + (stamp_two - stamp_one)})
        identical = _without(self.before[name], pattern) == _without(self.after[name], pattern)
        self.neutralize(name, identical, f"{name}: {label} {', '.join(touched)} changed")


def stamp_differences(before: dict[str, bytes], after: dict[str, bytes]) -> tuple[dict[str, bytes], list[dict]]:
    """[after] with every stamp difference against [before] set aside, and one row per member
    it set aside: {"member", "description", "rest"}, where rest is "identical" when the member
    now carries the source's exact bytes and "rewritten" when the rest of it was re-serialized."""
    stamps = _Stamps(before, after)
    stamps.run()
    return stamps.filtered, stamps.rows


def stamp_filter(before: dict[str, bytes], after: dict[str, bytes]) -> tuple[dict, dict, list[str]]:
    """Copies of both part maps with stamp-only differences neutralized, and a line for each.

    A filtered member carries the source's bytes in the returned `after` map (an added
    stamp-only part is dropped), so an existing byte check runs on the result unchanged.
    """
    filtered, rows = stamp_differences(before, after)
    return dict(before), filtered, [row["description"] for row in rows]


def admit_stamps(before: dict[str, bytes], after: dict[str, bytes], declared) -> tuple[dict, list[str]]:
    """[after] with exactly the stamp differences on the members [declared] names set aside.

    [declared] is what an edit receipt says its stamp wrote (`result.stamp.parts`). A declared
    member is admitted only when its change is a stamp change, and a stamp on a member the
    receipt does not name is left in place, so an undeclared stamp still fails a byte check
    and a receipt cannot pass off any other change as its stamp.
    """
    names = {str(name).lower() for name in (declared or ())}
    filtered, rows = stamp_differences(before, after)
    result, admitted = dict(after), []
    for row in rows:
        member = row["member"]
        if member.lower() not in names:
            continue
        if member in filtered:
            result[member] = filtered[member]
        else:
            result.pop(member, None)
        admitted.append(row["description"])
    return result, admitted


def receipt_stamp_parts(receipt) -> list[str]:
    """The members an edit receipt says its provenance stamp wrote, or none."""
    result = receipt.get("result") if isinstance(receipt, dict) else None
    stamp = result.get("stamp") if isinstance(result, dict) else None
    parts = stamp.get("parts") if isinstance(stamp, dict) else None
    return [part for part in parts if isinstance(part, str)] if isinstance(parts, list) else []


def receipt_stamp_properties(receipt) -> dict:
    result = receipt.get("result") if isinstance(receipt, dict) else None
    stamp = result.get("stamp") if isinstance(result, dict) else None
    properties = stamp.get("properties") if isinstance(stamp, dict) else None
    return dict(properties) if isinstance(properties, dict) else {}


# ---------------------------------------------------------------- categories

INVISIBLE, NOTICEABLE = "invisible", "noticeable"
CATEGORY_EFFECT: dict[str, str] = {}


def _category(name: str, effect: str) -> str:
    CATEGORY_EFFECT[name] = effect
    return name


SAME = _category("re-serialized with the same content", INVISIBLE)
RENUMBERED = _category("relationship ids renumbered or content types listed differently", INVISIBLE)
RENAMED = _category("part renamed with the same bytes", INVISIBLE)
CACHE = _category("calculation chain dropped or rewritten (Excel rebuilds it)", INVISIBLE)
NEW_SLIDE = _category("the new slide's own parts", INVISIBLE)
LINKS_GONE = _category("relationships part dropped or added with the parts it links", INVISIBLE)
WORKBOOK_SAME = _category("workbook reads the same (strings, styles or parts renumbered)", INVISIBLE)
G_TEXT = _category("text of untouched content changed", NOTICEABLE)
G_FORMAT = _category("formatting or structure of untouched content changed", NOTICEABLE)
G_STYLES = _category("styles, numbering or fonts changed", NOTICEABLE)
G_THEME = _category("theme colors or fonts changed", NOTICEABLE)
G_SETTINGS = _category("document or presentation settings changed", NOTICEABLE)
G_VIEW = _category("window, zoom or view state changed", NOTICEABLE)
G_PROPS = _category("document properties changed or lost", NOTICEABLE)
G_COMMENTS = _category("comments, notes or their threads changed or lost", NOTICEABLE)
G_MEDIA = _category("charts, pictures, drawings or embedded objects changed or lost", NOTICEABLE)
G_MACROS = _category("macros or custom XML data changed or lost", NOTICEABLE)
G_THUMB = _category("preview thumbnail changed or lost", NOTICEABLE)
G_PRINTER = _category("printer settings changed or lost", NOTICEABLE)
G_PACKAGE = _category("relationships or content types changed", NOTICEABLE)
G_ADDED = _category("member added", NOTICEABLE)
G_LOST = _category("other package part lost", NOTICEABLE)
G_OTHER = _category("other part changed", NOTICEABLE)
G_UNJUDGED = _category("not compared: over a bound or not readable", NOTICEABLE)
X_TEXT = _category("text or values of untouched cells changed", NOTICEABLE)
X_FORMULA = _category("formulas of untouched cells rewritten", NOTICEABLE)
X_CACHED = _category("saved formula results dropped or changed", NOTICEABLE)
X_FORMAT = _category("formatting of untouched cells changed", NOTICEABLE)
X_LAYOUT = _category("columns, rows, page setup or print layout changed", NOTICEABLE)
X_HEADERS = _category("printed header or footer changed", NOTICEABLE)
X_MARGINS = _category("print margins changed", NOTICEABLE)
X_MERGES = _category("merged cells changed", NOTICEABLE)
X_RULES = _category("conditional formatting, data validation or filters changed", NOTICEABLE)
X_EXT = _category("newer-version features changed or dropped (extension lists)", NOTICEABLE)
X_VIEW = _category("window, zoom or selection state changed", NOTICEABLE)
X_LINKS = _category("hyperlinks changed or lost", NOTICEABLE)
X_PROTECT = _category("sheet or workbook protection changed", NOTICEABLE)
X_OBJECTS = _category("drawings, comments, tables or other sheet objects unlinked or added", NOTICEABLE)
X_STRUCTURE = _category("sheets, sheet order, defined names or workbook settings changed", NOTICEABLE)
X_PALETTE = _category("custom color palette changed", NOTICEABLE)
X_NAMED = _category("named cell styles changed", NOTICEABLE)

EXTENSION_FEATURES = {
    "{78C0D931-6437-407d-A8EE-F0AAD7539E65}": "extended conditional formatting (data bars, icon sets)",
    "{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}": "data validation lists that point at another sheet",
    "{05C60535-1F16-4fd2-B633-F4F36F0B64E0}": "sparklines",
    "{A8765BA9-456A-4dab-B4F3-ACF838C121DE}": "slicers",
    "{3A4CF648-6AED-40f4-86FF-DC5316D8AED3}": "slicers",
    "{7E03D99C-DC04-49d9-9315-930204A7B6E9}": "timelines",
}


def _part_category(name: str, texts_differ: bool | None = None) -> str:
    low = name.lower()
    base = posixpath.basename(low)
    if "thumbnail" in low:
        return G_THUMB
    if "printersettings" in low:
        return G_PRINTER
    if low == "[content_types].xml" or low.endswith(".rels"):
        return G_PACKAGE
    if low.startswith("docprops/"):
        return G_PROPS
    if re.search(r"(^|/)(comments|threadedcomments|persons|notesslides|notesmasters)/", low) \
            or re.match(r"(comments|people|threadedcomment|person)", base):
        return G_COMMENTS
    if re.search(r"(^|/)(charts|drawings|diagrams|media|embeddings|activex|ink)/", low) or low.endswith(".vml"):
        return G_MEDIA
    if "vbaproject" in low or low.startswith("customxml/"):
        return G_MACROS
    if "/theme/" in low:
        return G_THEME
    if base in ("styles.xml", "numbering.xml", "fonttable.xml", "styleswitheffects.xml", "tablestyles.xml"):
        return G_STYLES
    if base == "viewprops.xml":
        return G_VIEW
    if base in ("settings.xml", "websettings.xml", "presprops.xml"):
        return G_SETTINGS
    if texts_differ is True:
        return G_TEXT
    if texts_differ is False:
        return G_FORMAT
    return G_OTHER


def _removed_category(name: str) -> str:
    low = name.lower()
    if low.endswith("calcchain.xml"):
        return CACHE
    category = _part_category(name)
    return G_LOST if category in (G_OTHER, G_FORMAT, G_TEXT) else category


# ---------------------------------------------------------------- one member

class _Context:
    """Both packages of one comparison, for judgements that follow relationships."""

    def __init__(self, before: dict[str, bytes], after: dict[str, bytes], budget: Budget,
                 renames: dict[str, str] | None = None) -> None:
        self.budget = budget
        # Renames map an AFTER member (lowercased) to the BEFORE name it replaced.
        self.renames = {new.lower(): old.lower() for old, new in (renames or {}).items()}
        self.first = _Package(before, budget)
        self.second = _Package(after, budget, self.renames)


def _looks_like_xml(name: str, data: bytes) -> bool:
    low = name.lower()
    if low.endswith((".xml", ".rels", ".vml")) or low == "[content_types].xml":
        return True
    head = data[:64].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<")


def _is_rels(name: str) -> bool:
    return name.lower().endswith(".rels")


def _judge(name: str, before: bytes, after: bytes, context: _Context | None) -> tuple[str, str, str | None]:
    """(verdict, category, note) for one member both packages carry."""
    if before == after:
        return REWRITTEN, SAME, None
    if not (_looks_like_xml(name, before) and _looks_like_xml(name, after)):
        return CHANGED, _part_category(name), None
    budget = context.budget if context is not None else Budget()
    try:
        if context is None or _is_rels(name) or name.lower() == "[content_types].xml":
            first = _signature(before, name, budget)
            second = _signature(after, name, budget)
        else:
            first = _signature(before, name, budget, context.first.references(name))
            second = _signature(after, name, budget, context.second.references(name))
        if first == second:
            return REWRITTEN, SAME, None
        if context is not None and _is_rels(name) and _relationships_equivalent(context, name):
            return REWRITTEN, RENUMBERED, None
        if context is not None and name.lower() == "[content_types].xml" and _content_types_equivalent(context, name):
            return REWRITTEN, RENUMBERED, None
        return CHANGED, _part_category(name, _texts(first) != _texts(second)), None
    except Unjudged as error:
        return CHANGED, G_UNJUDGED, f"{name}: {error}"


def _reference_sequence(package: _Package, owner: str) -> tuple:
    """Every relationship reference [owner] makes, in document order, by what it points at."""
    name = package.member(owner)
    if name is None or not _looks_like_xml(name, package.parts[name]):
        return ()
    references = package.references(owner)
    found = []
    for element in package.tree(name).root.iter():
        for key, value in element.attrib.items():
            if key.startswith(f"{{{R_NS}}}") or key == _O_RELID:
                found.append((key, references.get(value, f"(no relationship {value})")))
    return tuple(found)


def _relationships_equivalent(context: _Context, name: str) -> bool:
    """The same relationships by what they point at, with the owner's references consistent.

    A relationship to a part only one package carries is left to that part's own judgement:
    dropping a part drops the link to it, and the drop is reported as the part's loss.
    """
    owner = owner_of(name)
    first_names = set(context.first.lower)
    second_names = {context.renames.get(member, member) for member in context.second.lower}

    def one_sided(target: str, here: set[str], there: set[str]) -> bool:
        return target in here and target not in there

    first = Counter((kind, target, external) for _, kind, target, external in context.first.relationships(owner)
                    if external or not one_sided(target, first_names, second_names))
    second = Counter((kind, context.second.compared(target, external), external)
                     for _, kind, target, external in context.second.relationships(owner)
                     if external or not one_sided(context.second.compared(target), second_names, first_names))
    if first != second:
        return False
    if not owner:
        return True  # nothing inside a package refers to a root relationship by its id
    return _reference_sequence(context.first, owner) == _reference_sequence(context.second, owner)


def _effective_types(package: _Package, name: str) -> dict[str, str]:
    root = package.tree(name).root
    defaults, overrides = {}, {}
    for child in root:
        local = _split(child.tag)[1]
        if local == "Default":
            defaults[(child.get("Extension") or "").lower()] = (child.get("ContentType") or "").lower()
        elif local == "Override":
            overrides[(child.get("PartName") or "").lstrip("/").lower()] = (child.get("ContentType") or "").lower()
    found = {}
    for member in package.parts:
        low = member.lower()
        if low == "[content_types].xml":
            continue
        low = package.renames.get(low, low)
        extension = low.rsplit(".", 1)[-1] if "." in posixpath.basename(low) else ""
        found[low] = overrides.get(low) or defaults.get(extension)
    return found


def _content_types_equivalent(context: _Context, name: str) -> bool:
    first = _effective_types(context.first, name)
    second_name = context.second.member(name)
    if second_name is None:
        return False
    second = _effective_types(context.second, second_name)
    return all(first[member] == second[member] for member in first.keys() & second.keys())


def classify_member(name: str, before_bytes: bytes, after_bytes: bytes, family: str | None = None,
                    before_parts: dict[str, bytes] | None = None,
                    after_parts: dict[str, bytes] | None = None) -> str:
    """"rewritten" when the two versions of [name] hold the same content, else "changed".

    Same content means equal after canonicalization (see the module docstring). Given both
    packages, relationship ids are compared by what they point at, a relationships part that
    only renumbered its ids consistently with its owner is the same content, and so is a
    content types part that lists the same effective type for every part. [family] is
    accepted for symmetry with `grade`; the canonical rules are namespace keyed.
    """
    context = None
    if before_parts is not None and after_parts is not None:
        context = _Context(before_parts, after_parts, Budget())
    return _judge(name, before_bytes, after_bytes, context)[0]


# ---------------------------------------------------------------- workbooks

BUILTIN_FORMATS = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
    12: "# ?/?", 13: "# ??/??", 14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "m/d/yy h:mm",
    37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)", 39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    41: '_(* #,##0_);_(* \\(#,##0\\);_(* "-"_);_(@_)', 42: '_("$"* #,##0_);_("$"* \\(#,##0\\);_("$"* "-"_);_(@_)',
    43: '_(* #,##0.00_);_(* \\(#,##0.00\\);_(* "-"??_);_(@_)',
    44: '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)', 45: "mm:ss", 46: "[h]:mm:ss",
    47: "mmss.0", 48: "##0.0E+0", 49: "@",
}
# The 64 colours an indexed colour means when a workbook carries no palette of its own. Writing
# them out explicitly changes nothing a reader sees.
DEFAULT_PALETTE = tuple(
    "000000 FFFFFF FF0000 00FF00 0000FF FFFF00 FF00FF 00FFFF 000000 FFFFFF FF0000 00FF00 0000FF FFFF00 "
    "FF00FF 00FFFF 800000 008000 000080 808000 800080 008080 C0C0C0 808080 9999FF 993366 FFFFCC CCFFFF "
    "660066 FF8080 0066CC CCCCFF 000080 FF00FF FFFF00 00FFFF 800080 800000 008080 0000FF 00CCFF CCFFFF "
    "CCFFCC FFFF99 99CCFF FF99CC CC99FF FFCC99 3366FF 33CCCC 99CC00 FFCC00 FF9900 FF6600 666699 969696 "
    "003366 339966 003300 333300 993300 993366 333399 333333".split())
_REL_WORKSHEET = REL + "worksheet"
_CELL_REF = re.compile(r"\$?([A-Z]{1,3})\$?([0-9]+)")
_EMPTY_VALUES = (None, ("number", None), ("text", "", None, None))


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + (ord(letter) - 64)
    return value


def _column_name(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _shift_formula(text: str, rows: int, columns: int) -> str | None:
    """A shared formula's master text as its dependent at (+rows, +columns) reads it, or None when
    the formula holds a construct this shifter does not move (structured or external references)."""
    pieces, index, size = [], 0, len(text)
    plain = []

    def flush() -> bool:
        segment = "".join(plain)
        plain.clear()
        if "[" in segment:
            return False
        failed = []

        def cell(match):
            column_absolute, letters, row_absolute, digits = match.groups()
            column = _column_number(letters) + (0 if column_absolute else columns)
            row = int(digits) + (0 if row_absolute else rows)
            if not (1 <= column <= 16384 and 1 <= row <= 1048576):
                failed.append(match.group(0))
                return match.group(0)
            return f"{column_absolute}{_column_name(column)}{row_absolute}{row}"

        def column_range(match):
            first_absolute, first, second_absolute, second = match.groups()
            moved = []
            for absolute, letters in ((first_absolute, first), (second_absolute, second)):
                number = _column_number(letters) + (0 if absolute else columns)
                if not 1 <= number <= 16384:
                    failed.append(match.group(0))
                    return match.group(0)
                moved.append(f"{absolute}{_column_name(number)}")
            return ":".join(moved)

        def row_range(match):
            first_absolute, first, second_absolute, second = match.groups()
            moved = []
            for absolute, digits in ((first_absolute, first), (second_absolute, second)):
                number = int(digits) + (0 if absolute else rows)
                if not 1 <= number <= 1048576:
                    failed.append(match.group(0))
                    return match.group(0)
                moved.append(f"{absolute}{number}")
            return ":".join(moved)

        segment = re.sub(r"(?<![A-Za-z0-9_.$])(\$?)([A-Z]{1,3}):(\$?)([A-Z]{1,3})(?![A-Za-z0-9_(.!])", column_range, segment)
        segment = re.sub(r"(?<![A-Za-z0-9_.$:])(\$?)([0-9]+):(\$?)([0-9]+)(?![A-Za-z0-9_(.!:])", row_range, segment)
        segment = re.sub(r"(?<![A-Za-z0-9_.$])(\$?)([A-Z]{1,3})(\$?)([0-9]+)(?![A-Za-z0-9_(.!])", cell, segment)
        pieces.append(segment)
        return not failed

    while index < size:
        character = text[index]
        if character in "\"'":
            if not flush():
                return None
            end = index + 1
            while end < size:
                if text[end] == character:
                    if end + 1 < size and text[end + 1] == character:
                        end += 2
                        continue
                    break
                end += 1
            pieces.append(text[index:end + 1])
            index = end + 1
            continue
        plain.append(character)
        index += 1
    if not flush():
        return None
    return "".join(pieces)


def _formula_text(text: str) -> str:
    """A formula with its leading equals sign and the whitespace outside string literals removed."""
    out, quote = [], None
    for character in text.lstrip().removeprefix("="):
        if quote:
            out.append(character)
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
            out.append(character)
        elif not character.isspace():
            out.append(character)
    return "".join(out)


class _Styles:
    """Every cell format a workbook defines, resolved to what it looks like."""

    def __init__(self, package: _Package, name: str | None) -> None:
        self.xfs = [("default",)]
        self.dxfs: list = []
        self.palette = DEFAULT_PALETTE
        self.named = ()
        if name is None:
            return
        tree = package.tree(name)
        root = tree.root
        rules = _Rules(tree.qnames, sort_all=True)
        formats = dict(BUILTIN_FORMATS)
        for element in root.iterfind(f"{{{S}}}numFmts/{{{S}}}numFmt"):
            formats[_integer(element.get("numFmtId"))] = element.get("formatCode") or ""
        self.dxfs = [_canon(element, rules) for element in root.iterfind(f"{{{S}}}dxfs/{{{S}}}dxf")]
        fonts = [_canon(element, rules) for element in root.iterfind(f"{{{S}}}fonts/{{{S}}}font")]
        fills = [_canon(element, rules) for element in root.iterfind(f"{{{S}}}fills/{{{S}}}fill")]
        borders = [_canon(element, rules) for element in root.iterfind(f"{{{S}}}borders/{{{S}}}border")]

        def pick(table, element, key):
            index = _integer(element.get(key, "0"))
            return table[index] if 0 <= index < len(table) else ("missing", element.get(key))

        xfs = []
        for element in root.iterfind(f"{{{S}}}cellXfs/{{{S}}}xf"):
            number = _integer(element.get("numFmtId", "0"))
            xfs.append((formats.get(number, f"#{number}"), pick(fonts, element, "fontId"),
                        pick(fills, element, "fillId"), pick(borders, element, "borderId"),
                        _canon(element.find(f"{{{S}}}alignment"), rules) if element.find(f"{{{S}}}alignment") is not None else None,
                        _canon(element.find(f"{{{S}}}protection"), rules) if element.find(f"{{{S}}}protection") is not None else None,
                        _BOOLEAN.get(element.get("quotePrefix", "0"), element.get("quotePrefix", "0"))))
        self.xfs = xfs or self.xfs
        palette = root.find(f"{{{S}}}colors/{{{S}}}indexedColors")
        if palette is not None:
            self.palette = tuple((element.get("rgb") or "").upper()[-6:] for element in palette)
        self.named = tuple(sorted((element.get("name") or "", element.get("builtinId") or "")
                                  for element in root.iterfind(f"{{{S}}}cellStyles/{{{S}}}cellStyle")))

    def resolve(self, index: int):
        return self.xfs[index] if 0 <= index < len(self.xfs) else ("missing", index)


class _Cell:
    __slots__ = ("value", "formula", "cached", "style", "raw")

    def __init__(self, value, formula, cached, style, raw) -> None:
        self.value, self.formula, self.cached, self.style, self.raw = value, formula, cached, style, raw


_UNRESOLVED = "(shared formula this comparison does not move)"


def _string_value(element: ET.Element | None, rules: _Rules) -> tuple:
    """A shared or inline string as a reader sees it: its text, its run formatting, its phonetics."""
    if element is None:
        return ("text", "", None, None)
    text = "".join(node.text or "" for node in element.findall(f"{{{S}}}t"))
    runs = element.findall(f"{{{S}}}r")
    text += "".join(node.text or "" for run in runs for node in run.findall(f"{{{S}}}t"))
    rich = tuple(_canon(run.find(f"{{{S}}}rPr"), rules) if run.find(f"{{{S}}}rPr") is not None else None
                 for run in runs) or None
    phonetic = tuple("".join(node.itertext()) for node in element.findall(f"{{{S}}}rPh")) or None
    return ("text", text, rich, phonetic)


def _typed(kind: str, value: str | None, shared: list, inline: ET.Element | None, rules: _Rules):
    if kind == "s":
        if value is not None and value.strip().isdigit() and int(value) < len(shared):
            return shared[int(value)]
        return ("broken shared string", value)
    if kind == "inlineStr":
        return _string_value(inline, rules)
    if kind == "str":
        return ("text", value or "", None, None)
    if kind == "b":
        return ("bool", _BOOLEAN.get((value or "").strip(), (value or "").strip()))
    if kind == "e":
        return ("error", value)
    if kind == "d":
        return ("date", value)
    if value is None:
        return ("number", None)
    stripped = value.strip()
    return ("number", repr(float(stripped)) if _NUMBER.fullmatch(stripped) else stripped)


class _Sheet:
    """One worksheet as a reader sees it."""

    def __init__(self, package: _Package, name: str, workbook: "_Workbook") -> None:
        tree = package.tree(name)
        root = tree.root
        budget = package.budget
        references = package.references(name)
        styles = workbook.styles
        resolved = _Rules(tree.qnames, references, sort_all=True, dxfs=styles.dxfs)
        raw_rules = _Rules(tree.qnames, references, sort_all=True)
        self.cells: dict[str, _Cell] = {}
        masters, dependents = {}, []
        row_specs = []
        grid = root.find(f"{{{S}}}sheetData")
        number = 0
        for row in (list(grid) if grid is not None else []):
            if row.tag != f"{{{S}}}row":
                continue
            declared = row.get("r") or ""
            number = int(declared) if declared.isdigit() and int(declared) >= 1 else number + 1
            hidden = _BOOLEAN.get(row.get("hidden", "0"), row.get("hidden", "0"))
            outline = row.get("outlineLevel") or "0"
            height = repr(float(row.get("ht"))) if row.get("ht") and _NUMBER.fullmatch(row.get("ht")) else None
            style = None
            if _BOOLEAN.get(row.get("customFormat", "0"), row.get("customFormat")) == "1":
                style = _integer(row.get("s", "0"))
            if height or hidden == "1" or outline != "0" or style is not None:
                row_specs.append((number, height, hidden, outline, style))
            column = 0
            for cell in row.findall(f"{{{S}}}c"):
                budget.cell()
                reference = (cell.get("r") or "").upper()
                match = _CELL_REF.fullmatch(reference)
                if match:
                    column = _column_number(match.group(1))
                    reference = f"{match.group(1)}{match.group(2)}"
                else:
                    column += 1
                    reference = f"{_column_name(column)}{number}"
                kind = cell.get("t", "n")
                value = cell.findtext(f"{{{S}}}v")
                inline = cell.find(f"{{{S}}}is")
                typed = _typed(kind, value, workbook.shared, inline, resolved)
                formula_element = cell.find(f"{{{S}}}f")
                formula = cached = None
                if formula_element is not None:
                    shape = formula_element.get("t") or "normal"
                    text = _formula_text(formula_element.text or "")
                    if shape == "shared":
                        index = formula_element.get("si")
                        if text:
                            masters.setdefault(index, (number, column, text))
                            formula = (text, None)
                        else:
                            dependents.append((reference, number, column, index))
                            formula = (_UNRESOLVED, index)
                    elif shape == "array":
                        formula = (text, "array " + (formula_element.get("ref") or "").upper())
                    elif shape == "dataTable":
                        formula = (text, repr(_attributes(formula_element, S, "f", raw_rules, False)))
                    else:
                        formula = (text, None)
                    cached = typed if value is not None or inline is not None else None
                    typed = None
                style_index = _integer(cell.get("s", "0"))
                self.cells[reference] = _Cell(typed, formula, cached, style_index,
                                              (kind, value, repr(_canon(inline, raw_rules)) if inline is not None else None))
        for reference, row_number, column, index in dependents:
            master = masters.get(index)
            if master is None:
                continue
            moved = _shift_formula(master[2], row_number - master[0], column - master[1])
            if moved is not None:
                self.cells[reference].formula = (moved, None)
        self.rows = sorted(row_specs)
        self.columns_raw, self.columns = self._columns(root, styles)

        def all_of(path, rules=resolved):
            found = (_canon(node, rules) for node in root.iterfind(path))
            return tuple(sorted(repr(signature) for signature in found if signature is not None))

        self.merges = tuple(sorted((node.get("ref") or "").upper()
                                   for node in root.iterfind(f"{{{S}}}mergeCells/{{{S}}}mergeCell")))
        rule_paths = (f"{{{S}}}conditionalFormatting", f"{{{S}}}dataValidations", f"{{{S}}}autoFilter")
        self.rules = tuple(item for path in rule_paths for item in all_of(path))
        self.rules_raw = tuple(item for path in rule_paths for item in all_of(path, raw_rules))
        self.extensions = all_of(f"{{{S}}}extLst")
        self.extension_uris = tuple(sorted(node.get("uri") or "" for node in root.iterfind(f"{{{S}}}extLst/{{{S}}}ext")))
        self.view = all_of(f"{{{S}}}sheetViews")
        self.print = (all_of(f"{{{S}}}pageSetup") + all_of(f"{{{S}}}printOptions") + all_of(f"{{{S}}}rowBreaks")
                      + all_of(f"{{{S}}}colBreaks") + all_of(f"{{{S}}}sheetPr"))
        self.headers = all_of(f"{{{S}}}headerFooter")
        self.margins = all_of(f"{{{S}}}pageMargins")
        self.links = all_of(f"{{{S}}}hyperlinks")
        self.protection = all_of(f"{{{S}}}sheetProtection") + all_of(f"{{{S}}}protectedRanges")
        objects = Counter()
        for element in root:
            local = _split(element.tag)[1]
            if local in ("drawing", "legacyDrawing", "legacyDrawingHF", "picture", "oleObjects", "controls",
                         "tableParts", "webPublishItems"):
                for node in element.iter():
                    for key, value in node.attrib.items():
                        if key.startswith(f"{{{R_NS}}}"):
                            objects[(local, references.get(value, "(no relationship)").split(" -> ")[0])] += 1
        self.objects = objects

    @staticmethod
    def _columns(root: ET.Element, styles: _Styles):
        raw, resolved = [], []
        for column in root.iterfind(f"{{{S}}}cols/{{{S}}}col"):
            low = _integer(column.get("min", "1"))
            high = min(_integer(column.get("max"), low), 16384)
            style = _integer(column.get("style", "0"))
            width = column.get("width")
            width = repr(float(width)) if width and _NUMBER.fullmatch(width) else None
            hidden = _BOOLEAN.get(column.get("hidden", "0"), column.get("hidden", "0"))
            outline = column.get("outlineLevel") or "0"
            raw.append((low, high, (width, hidden, outline, style)))
            resolved.append((low, high, (width, hidden, outline, repr(styles.resolve(style)))))

        def merged(spans):
            out = []
            for low, high, spec in sorted(spans):
                if out and out[-1][2] == spec and out[-1][1] + 1 >= low:
                    out[-1] = (out[-1][0], max(high, out[-1][1]), spec)
                else:
                    out.append((low, high, spec))
            return tuple(out)
        return merged(raw), merged(resolved)


class _Workbook:
    """A workbook as a reader sees it: sheets, names, strings and formats by what they mean."""

    def __init__(self, package: _Package) -> None:
        self.package = package
        self.part = package.linked(REL_OFFICE_DOCUMENT)
        if self.part is None:
            raise Unjudged("the package links no workbook part")
        tree = package.tree(self.part)
        root = tree.root
        by_type = defaultdict(list)
        for _, kind, target, external in package.relationships(self.part):
            member = package.member(target) if not external else None
            if member is not None:
                by_type[kind.rsplit("/", 1)[-1]].append(member)
        self.strings_part = (by_type.get("sharedStrings") or [None])[0]
        self.styles_part = (by_type.get("styles") or [None])[0]
        self.chain_part = (by_type.get("calcChain") or [None])[0]
        rules = _Rules(tree.qnames, sort_all=True)
        self.shared = []
        if self.strings_part is not None:
            strings = package.tree(self.strings_part)
            string_rules = _Rules(strings.qnames, sort_all=True)
            self.shared = [_string_value(item, string_rules) for item in strings.root.iterfind(f"{{{S}}}si")]
        self.styles = _Styles(package, self.styles_part)
        references = {rid: (kind, target) for rid, kind, target, _ in package.relationships(self.part)}
        self.order, self.sheet_parts, self.sheets = [], {}, {}
        for sheet in root.iterfind(f"{{{S}}}sheets/{{{S}}}sheet"):
            name = sheet.get("name") or ""
            self.order.append((name, sheet.get("state") or "visible"))
            kind, target = references.get(sheet.get(f"{{{R_NS}}}id"), ("", ""))
            member = package.member(target)
            if member is None:
                continue
            self.sheet_parts[name] = member
            if kind == _REL_WORKSHEET:
                self.sheets[name] = _Sheet(package, member, self)
        names = [name for name, _ in self.order]
        self.names = {}
        for element in root.iterfind(f"{{{S}}}definedNames/{{{S}}}definedName"):
            local = element.get("localSheetId")
            scope = names[int(local)] if local is not None and local.isdigit() and int(local) < len(names) else local
            text = re.sub(r"'([A-Za-z_][A-Za-z0-9_.]*)'!", r"\1!", (element.text or "").strip())
            self.names[(element.get("name") or "", scope)] = (text, repr(_attributes(element, S, "definedName", rules, False)))
        self.settings = tuple(repr(_canon(element, rules)) for element in (
            root.find(f"{{{S}}}workbookPr"), root.find(f"{{{S}}}workbookProtection"), root.find(f"{{{S}}}fileSharing"))
            if element is not None)
        self.views = tuple(repr(_canon(element, rules)) for element in root.iterfind(f"{{{S}}}bookViews"))

    def governed(self) -> set[str]:
        return {name for name in (self.part, self.strings_part, self.styles_part, self.chain_part) if name} | {
            self.sheet_parts[name] for name in self.sheets}


def _describe(value) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _compare_workbooks(first: _Workbook, second: _Workbook, write_set: set[str]) -> list[tuple]:
    """Every practical difference as (attributed members, category, example).

    A difference in a cell's text, formatting or rule format is attributed to the part that
    caused it: the shared strings or the styles when the cell itself still says the same
    thing, the worksheet otherwise. A worksheet in the write set is compared only for what
    those helper parts decide, because the rest of it is the edit's own to judge.
    """
    found = []

    def parts(*names):
        return frozenset(name for name in names if name)

    book = parts(first.part, second.part)
    styles = parts(first.styles_part, second.styles_part)
    strings = parts(first.strings_part, second.strings_part)
    if first.order != second.order:
        found.append((book, X_STRUCTURE, f"sheet list {_describe(first.order)} -> {_describe(second.order)}"))
    changed_names = sorted(str(key[0]) for key in first.names.keys() | second.names.keys()
                           if first.names.get(key) != second.names.get(key))
    if changed_names:
        found.append((book, X_STRUCTURE, f"defined names changed: {', '.join(changed_names[:4])}"))
    if first.settings != second.settings:
        found.append((book, X_STRUCTURE, "workbook properties or protection"))
    if first.views != second.views:
        found.append((book, X_VIEW, "workbook window"))
    if first.styles.palette != second.styles.palette:
        found.append((styles, X_PALETTE, "indexedColors"))
    if first.styles.named != second.styles.named:
        found.append((styles, X_NAMED, "cellStyles"))
    for name, one in first.sheets.items():
        other = second.sheets.get(name)
        if other is None:
            found.append((parts(first.sheet_parts.get(name)) | book, X_STRUCTURE, f"sheet {name} lost"))
            continue
        sheet = parts(first.sheet_parts.get(name), second.sheet_parts.get(name))
        found.extend(_compare_sheets(name, one, other, first.styles, second.styles, sheet, styles, strings,
                                     bool(sheet & write_set)))
    for name in second.sheets.keys() - first.sheets.keys():
        found.append((parts(second.sheet_parts.get(name)) | book, X_STRUCTURE, f"sheet {name} added"))
    kept = []
    for members, category, example in found:
        members = members - write_set
        if members:
            kept.append((members, category, example))
    return kept


def compare_workbooks(before: dict[str, bytes], after: dict[str, bytes], write_set=()) -> list[dict]:
    """Every difference a reader of the workbook could notice, outside the members in [write_set].

    Each finding is {"members", "category", "example"}: the parts it is attributed to, what kind
    of difference it is and one example. Cells are compared by what they resolve to, so shared
    strings moved inline, styles renumbered, shared formulas written out or numbers spelled
    differently are no finding at all. Raises `Unjudged` when a part is over a bound.
    """
    budget = Budget()
    first, second = _Workbook(_Package(before, budget)), _Workbook(_Package(after, budget))
    return [{"members": sorted(members), "category": category, "example": example}
            for members, category, example in _compare_workbooks(first, second, set(write_set))]


def _compare_sheets(name, one: _Sheet, other: _Sheet, first_styles: _Styles, second_styles: _Styles,
                    sheet: frozenset, styles: frozenset, strings: frozenset, edited: bool) -> list[tuple]:
    counts: Counter = Counter()
    examples: dict = {}
    emptied = 0

    def note(members, category, example):
        counts[(members, category)] += 1
        examples.setdefault((members, category), example)

    default_before, default_after = first_styles.resolve(0), second_styles.resolve(0)
    references = (one.cells.keys() & other.cells.keys()) if edited else (one.cells.keys() | other.cells.keys())
    for reference in references:
        before, after = one.cells.get(reference), other.cells.get(reference)
        label = f"{name}!{reference}"
        if before is None or after is None:
            present = before or after
            verb = "added" if before is None else "lost"
            if present.value not in _EMPTY_VALUES or present.formula is not None:
                note(sheet, X_TEXT, f"{label} {verb}")
            elif (first_styles.resolve(present.style) if before is not None else second_styles.resolve(present.style)) \
                    != (default_before if before is not None else default_after):
                note(sheet, X_FORMAT, f"{label} formatted cell {verb}")
            continue
        if before.formula is None and after.formula is None:
            if before.value != after.value:
                if before.raw == after.raw and before.raw[0] == "s":
                    note(strings, X_TEXT, f"{label} {_describe(before.value)} -> {_describe(after.value)}")
                elif not edited:
                    note(sheet, X_TEXT, f"{label} {_describe(before.value)} -> {_describe(after.value)}")
        elif not edited:
            if (before.formula is None) != (after.formula is None):
                note(sheet, X_FORMULA, f"{label} formula {'added' if before.formula is None else 'removed'}")
            else:
                same = before.formula == after.formula or _UNRESOLVED in (before.formula[0], after.formula[0])
                if not same:
                    note(sheet, X_FORMULA, f"{label} {_describe(before.formula[0])} -> {_describe(after.formula[0])}")
                if before.cached != after.cached:
                    emptied += after.cached in _EMPTY_VALUES
                    note(sheet, X_CACHED, f"{label} {_describe(before.cached)} -> {_describe(after.cached)}")
        if first_styles.resolve(before.style) != second_styles.resolve(after.style):
            if before.style == after.style:
                note(styles, X_FORMAT, f"{label} format")
            elif not edited:
                note(sheet, X_FORMAT, f"{label} format")
    if one.columns != other.columns:
        if one.columns_raw == other.columns_raw:
            note(styles, X_FORMAT, f"{name}: column formats")
        elif not edited:
            note(sheet, X_LAYOUT, f"{name}: columns")
    resolved_rows_one = [(row[:4], first_styles.resolve(row[4]) if row[4] is not None else None) for row in one.rows]
    resolved_rows_two = [(row[:4], second_styles.resolve(row[4]) if row[4] is not None else None) for row in other.rows]
    if resolved_rows_one != resolved_rows_two:
        if one.rows == other.rows:
            note(styles, X_FORMAT, f"{name}: row formats")
        elif not edited:
            note(sheet, X_LAYOUT, f"{name}: rows")
    if one.rules != other.rules:
        if one.rules_raw == other.rules_raw:
            note(styles, X_FORMAT, f"{name}: conditional formats")
        elif not edited:
            note(sheet, X_RULES, f"{name}: rules")
    if not edited:
        for attribute, category in (("merges", X_MERGES), ("view", X_VIEW), ("print", X_LAYOUT),
                                    ("headers", X_HEADERS), ("margins", X_MARGINS), ("links", X_LINKS),
                                    ("protection", X_PROTECT), ("objects", X_OBJECTS)):
            if getattr(one, attribute) != getattr(other, attribute):
                note(sheet, category, f"{name}: {attribute}")
        if one.extensions != other.extensions:
            lost = [uri for uri in one.extension_uris if uri not in other.extension_uris]
            detail = ", ".join(sorted({EXTENSION_FEATURES.get(uri, uri) for uri in lost})) or "rewritten"
            note(sheet, X_EXT, f"{name}: {detail}")
    out = []
    for (members, category), count in counts.items():
        example = examples[(members, category)]
        if category == X_CACHED and emptied:
            example = f"{example}; {emptied} emptied"
        out.append((members, category, f"{count} cells, e.g. {example}" if count > 1 else example))
    return out


# ---------------------------------------------------------------- the grade

PPTX_PRIVATE_RELATIONSHIPS = {REL + name for name in (
    "notesSlide", "chart", "chartUserShapes", "themeOverride", "diagramData", "diagramLayout", "diagramColors",
    "diagramQuickStyle", "oleObject", "package", "vmlDrawing", "tags", "comments", "control",
)} | {
    "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing",
    "http://schemas.microsoft.com/office/2011/relationships/chartColorStyle",
    "http://schemas.microsoft.com/office/2011/relationships/chartStyle",
    "http://schemas.microsoft.com/office/2014/relationships/chartEx",
    "http://schemas.microsoft.com/office/2006/relationships/activeXControlBinary",
    "http://schemas.microsoft.com/office/2018/10/relationships/comments",
}


def _slides(package: _Package) -> list[str]:
    presentation = package.linked(REL_OFFICE_DOCUMENT)
    if presentation is None:
        return []
    references = {rid: target for rid, _, target, external in package.relationships(presentation) if not external}
    found = []
    for element in package.tree(presentation).root.iterfind(f"{{{P}}}sldIdLst/{{{P}}}sldId"):
        member = package.member(references.get(element.get(f"{{{R_NS}}}id"), ""))
        if member is not None:
            found.append(member)
    return found


def _new_slide_parts(first: _Package, second: _Package) -> set[str]:
    """Every member a slide the source did not list owns in the produced deck: the slide, each
    part reached from it by a relationship private to one slide, and their relationships."""
    try:
        known = {name.lower() for name in _slides(first)}
        pending = [name for name in _slides(second) if name.lower() not in known]
    except Unjudged:
        return set()
    owned, seen = set(), set(pending)
    while pending:
        part = pending.pop()
        owned.add(part)
        rels = second.member(relationships_path(part))
        if rels is None:
            continue
        owned.add(rels)
        try:
            rows = second.relationships(part)
        except Unjudged:
            continue
        for _, kind, target, external in rows:
            member = second.member(target) if not external and kind in PPTX_PRIVATE_RELATIONSHIPS else None
            if member is not None and member not in seen:
                seen.add(member)
                pending.append(member)
    return owned


def family_of(parts: dict[str, bytes]) -> str | None:
    names = {name.lower() for name in parts}
    for family, main in (("docx", "word/document.xml"), ("xlsx", "xl/workbook.xml"), ("pptx", "ppt/presentation.xml")):
        if main in names:
            return family
    return None


_WORKBOOK_MEMBER = re.compile(r"xl/(worksheets/|sharedstrings|styles\.xml|workbook\.xml|calcchain)", re.I)


class _Outcome:
    def __init__(self) -> None:
        self.rewritten: dict[str, None] = {}
        self.changed: dict[str, None] = {}
        self.categories: dict[str, list[str]] = defaultdict(list)
        self.notes: list[str] = []

    def add(self, verdict: str, member: str, category: str, example: str | None = None) -> None:
        (self.changed if verdict == CHANGED else self.rewritten)[member] = None
        examples = self.categories[category]
        if len(examples) < MAX_EXAMPLES:
            examples.append(example or member)


def grade(before: dict[str, bytes], after: dict[str, bytes], allowed_changed=(), allowed_added=(),
          allowed_removed=(), family: str | None = None) -> dict:
    """The three-way preservation verdict for everything outside the write set.

    Stamps are filtered first (see `stamp_filter`). Then every member outside the allowed sets
    that differs is judged: `preserved` when nothing does, `rewritten` when every difference is
    a same-content rewrite or bookkeeping no reader can see, `changed` when at least one could
    be noticed. `categories` names each kind of difference with a few examples.
    """
    filtered, rows = stamp_differences(before, after)
    return _grade(before, filtered, set(allowed_changed), set(allowed_added), set(allowed_removed), family,
                  [row["description"] for row in rows])


def _grade(before, after, allowed_changed, allowed_added, allowed_removed, family, stamps) -> dict:
    changed = sorted(name for name in before.keys() & after.keys()
                     if before[name] != after[name] and name not in allowed_changed)
    added = sorted(name for name in after.keys() - before.keys() if name not in allowed_added)
    removed = sorted(name for name in before.keys() - after.keys() if name not in allowed_removed)
    outcome = _Outcome()
    if changed or added or removed:
        if len(before) > MAX_MEMBERS or len(after) > MAX_MEMBERS:
            for name in changed + added + removed:
                outcome.add(CHANGED, name, G_UNJUDGED, f"{name}: the package has over {MAX_MEMBERS} members")
        else:
            _judge_all(before, after, changed, added, removed, allowed_changed, family or family_of(before), outcome)
    verdict = CHANGED if outcome.changed else REWRITTEN if outcome.rewritten else PRESERVED
    return {
        "verdict": verdict,
        "stamps": stamps[:MAX_LISTED],
        "rewritten": list(outcome.rewritten)[:MAX_LISTED],
        "changed": list(outcome.changed)[:MAX_LISTED],
        "counts": {"stamps": len(stamps), "rewritten": len(outcome.rewritten), "changed": len(outcome.changed)},
        "categories": {name: examples for name, examples in sorted(outcome.categories.items())},
        "notes": outcome.notes[:MAX_EXAMPLES],
    }


def _judge_all(before, after, changed, added, removed, write_set, family, outcome: _Outcome) -> None:
    budget = Budget()
    by_content = defaultdict(list)
    for name in added:
        by_content[(len(after[name]), hash(after[name]))].append(name)
    renames = {}
    for name in removed:
        for candidate in by_content.get((len(before[name]), hash(before[name])), []):
            if candidate not in renames.values() and after[candidate] == before[name]:
                renames[name] = candidate
                break
    context = _Context(before, after, budget, renames)
    governed: set[str] = set()
    findings: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if family == "xlsx" and any(_WORKBOOK_MEMBER.match(name) for name in changed + added + removed):
        try:
            first, second = _Workbook(context.first), _Workbook(context.second)
            governed = first.governed() | second.governed()
            for members, category, example in _compare_workbooks(first, second, write_set):
                for member in members:
                    findings[member].append((category, example))
        except Unjudged as error:
            governed = set()
            findings.clear()
            outcome.notes.append(f"the workbook was judged part by part, not as a workbook: {error}")
    new_slides = _new_slide_parts(context.first, context.second) if family == "pptx" and added else set()

    def governed_verdict(name: str, present: bool) -> None:
        if findings.get(name):
            for category, example in findings[name]:
                outcome.add(CHANGED, name, category, example)
        elif name.lower().endswith("calcchain.xml"):
            outcome.add(REWRITTEN, name, CACHE)
        elif present and _judge(name, before[name], after[name], context)[0] == REWRITTEN:
            outcome.add(REWRITTEN, name, SAME)
        else:
            outcome.add(REWRITTEN, name, WORKBOOK_SAME)

    for name in changed:
        if name in governed:
            governed_verdict(name, True)
            continue
        verdict, category, note = _judge(name, before[name], after[name], context)
        outcome.add(verdict, name, category, note)
    for old, new in renames.items():
        if old in governed or new in governed:
            governed_verdict(old, False)
        else:
            outcome.add(REWRITTEN, f"{old} -> {new}", RENAMED)
    def links_only_one_side(name: str, package: _Package, other: _Package, other_names: set[str]) -> bool:
        """A relationships part whose every link points at a part the other package lacks."""
        if not _is_rels(name):
            return False
        try:
            rows = package.relationships(owner_of(name))
        except Unjudged:
            return False
        return all(not external and package.member(target) is not None
                   and package.compared(target) not in other_names for _, _, target, external in rows)

    before_names = set(context.first.lower)
    after_names = {context.renames.get(member, member) for member in context.second.lower}
    for name in removed:
        if name in renames:
            continue
        if name in governed:
            governed_verdict(name, False)
            continue
        if links_only_one_side(name, context.first, context.second, after_names):
            outcome.add(REWRITTEN, name, LINKS_GONE, f"{name} lost")
            continue
        category = _removed_category(name)
        outcome.add(REWRITTEN if CATEGORY_EFFECT[category] == INVISIBLE else CHANGED, name, category, f"{name} lost")
    for name in added:
        if name in renames.values():
            continue
        if name in governed:
            governed_verdict(name, False)
        elif name in new_slides:
            outcome.add(REWRITTEN, name, NEW_SLIDE)
        elif links_only_one_side(name, context.second, context.first, before_names):
            outcome.add(REWRITTEN, name, LINKS_GONE, f"{name} added")
        else:
            outcome.add(CHANGED, name, G_ADDED, f"{name} added")


__all__ = [
    "APP_STAMP_FIELDS", "CATEGORY_EFFECT", "CORE_STAMP_FIELDS", "TOOL_STAMP_PREFIXES", "VERDICTS", "Unjudged",
    "admit_stamps", "classify_member", "compare_workbooks", "family_of", "grade", "parse_part", "receipt_stamp_parts",
    "receipt_stamp_properties", "stamp_differences", "stamp_filter", "tool_properties",
]
