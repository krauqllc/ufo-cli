#!/usr/bin/env python3
"""Run the documented, synthetic file-intake evaluation on a packaged UFO build.

No input dataset, download, account, or upload. Publish a new evidence directory
only after every expected receipt, output, and preservation check passes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
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
from tools.corpus import package_semantics
from tools.corpus.evaluate_edit_corpus import restricted_docker_prefix

SAMPLE_TEXT = "UFO synthetic evaluation.\nVisible text survives metadata cleaning."
# The suite's identity and size: the evaluation kit checks an extracted run against both,
# so a changed step list is a deliberate version bump here, never an accident downstream.
SUITE_VERSION = 26
COMMAND_COUNT = 130
AUTHOR = "UFO Synthetic Author"
# The rendered pictures this suite asks for are 400 pixels wide; the oracle
# decodes nothing larger, so a runaway render is a failure, not a long wait.
MAX_RENDER_SIDE = 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
#: A picture of rendered words with no text layer anywhere in it, which is what
#: a scan is. Every other fixture in this file authors its own bytes, and this
#: one cannot: readable glyphs are not something to hand-assemble. It was drawn
#: once and is carried verbatim, so the evaluation runs against exactly these
#: pixels on every machine. Provenance, reproducible with ImageMagick 6.9:
#:
#:   convert -size 620x110 xc:white -fill black -font DejaVu-Serif \
#:     -pointsize 52 -annotate +30+72 "Invoice 4417 overdue" -strip \
#:     +set date:create +set date:modify -define png:compression-level=9 \
#:     -depth 8 -colorspace Gray PNG8:scan.png
#:
#: 4194 bytes, sha256 4a880521f65e904d0ea0ebec6f26423290e309aefc237a355281b673f3722a15.
#: Stripped of every metadata chunk on purpose: this file is here to be read,
#: not to plant a finding, and the folder report's totals say so.
SCAN_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAmwAAABuCAMAAACtHL81AAAC61BMVEX///9tbW1HR0fJycnk5OSWlpacnJzv7+/Z2dkICAgA"
    "AADDw8O3t7cdHR3n5+czMzOzs7Pf399TU1PLy8sQEBAeHh7l5eX+/v4/Pz/u7u5fX1+rq6s+Pj5kZGSZmZmurq4YGBifn5/o"
    "6OhDQ0MuLi4KCgqBgYE8PDzm5ubq6upWVla0tLSvr68nJydNTU1ycnJRUVGmpqYUFBREREQTExP39/fe3t45OTnw8PABAQEJ"
    "CQlOTk6dnZ37+/uFhYWNjY13d3f5+fnFxcXKysr8/PwaGhpYWFinp6dQUFACAgJmZmYXFxdXV1elpaXi4uIDAwPBwcGqqqpV"
    "VVV6enq4uLj4+Pjd3d1xcXEbGxuxsbEWFhYlJSUMDAykpKSPj4/MzMxqamorKysHBwc7Ozt+fn7b29vs7OxZWVkxMTEGBgYq"
    "KipJSUna2trt7e1aWloFBQUNDQ0cHBw2NjZjY2OSkpLV1dWamppPT08sLCwVFRUtLS2YmJjh4eHS0tI4ODjc3Nx/f389PT0R"
    "ERGUlJQmJiYLCwuIiIjz8/NdXV12dnb9/f3X19e7u7vHx8dzc3MhISF0dHTp6eng4OA3NzfOzs5lZWVvb28EBAQjIyN5eXln"
    "Z2deXl4SEhJwcHCEhIS+vr4iIiJpaWm1tbUODg4vLy9bW1tiYmLQ0NA6Ojr19fVCQkKDg4P6+vp8fHy/v79oaGiioqI0NDQw"
    "MDB7e3sPDw9UVFR1dXXy8vKKioqXl5coKCi9vb3R0dE1NTV9fX3CwsL09PQkJCRAQEDExMRISEiMjIzU1NQgICBsbGxMTEyb"
    "m5vAwMDNzc3r6+ujo6O6urqoqKihoaGLi4tra2vIyMiRkZG2traCgoJGRkawsLCsrKwpKSlcXFxLS0vx8fGJiYlBQUFKSkqp"
    "qalFRUWAgIBhYWGGhob29vYfHx+enp5gYGAyMjJSUlKHh4fT09O8vLzY2NjGxsZ4eHitra0ZGRmysrJubm65ubmTk5P72NBq"
    "AAANMklEQVR42u2ceWAWxRnGlxAJGU7lCKHKjUjlvg+5AzTcIEgqRyCcEUI4lENBgRJCw1EQJKgBrICWglUQEKyW+xAIsSqH"
    "UsJVCpZqbbXHn513Nrs7s9/uzE6+JKTy/v7J7sz7zfF8z7c7OzMbw0AQBEEQBEEQBEEQBEEQBEEQBEEQBEEQBEEQBEEQBEEQ"
    "BEEQBEEQBEEQBEEQBEEQBEEQBEEQBEEQBEEQxKFERB4l77eeRz5QKqpIKiodTShl8htQthzNLP+jkLwCyaPivWvDg0TgoSKp"
    "tFJlQqpULYKKYqoRudmkAbHVWWao2aoTH36CZit2ZnsYqnqkCCqqQRRmkwXUrEV+RGYDat9js9WpS6kHjagPR48WSaXsW2xQ"
    "+PU8VllhNklAw59aDtIw2+NoNiWNoBGNi66+JlBf08KvpxlRmM0/oHkLmtxS02ytWqPZip3Z2jQmpG27Qq+mJiHtO8jM5htQ"
    "tSNNrPxEJ3+zda7koguN7Gqg2Yqd2Yxu3XvEFXol7XrSgWFjidn8A3rRtI5VDYnZervTfkZI/INotmJotiKhDyF9Y2Vm8w/o"
    "R1r0p380zDZgICFNDDTbfWq2Qe0JGWxIzCYJ6DWkoaFntidp4FA0231qtmGN2aXG32yygKfMP35mG56Q8HPXuOBpQkbEoNnu"
    "U7PRS83IATKzKQP8zRbKKBo32kCzhWu2xDFjRyb1HDd+gm6pE0dNmtwzudUzU0ZPDRSf0n/a5J6p02fMnPXscyGZ5WfPmTsv"
    "udbzzV6YH6iwBamEPGtIvKQM0DLbi4TUjypwRXQFeGnhonmpi59p9ovSdlLs7ClL0qITWi6tmm+z6UqvZzZnVaGTkb7MOk79"
    "ZYaZvZyfWlphpvW1E1ZavRye7ERNec6zcJ6oVVw8+dVqwdtr+sTbWe2nvazuUcZMQtZmSLykDNAy2zoa9ooqyFuRYGr6CcCr"
    "OWx9pnWywerl6PpWUvKr3QSzzbIyGuXFvmYliFcVbenzb7aoyZwUr2vIsyKLdbrfxuov0nE4GbhJYbbNrSDpjX4bf92khdmx"
    "ZU7mm1sgZWv1jduYNaJ7K3s0mv44FhgSLykDtMz2Fg17WxHjo0ggNX0F4NRM/41TTp7Ztv8275ztNmi5PR9m05de02ztdlCe"
    "h7SdEXTc+8o7wyOSWDMGs+z51aqxxU1SjZJofqQZPaS/q5nVqpm/2N7wIx7xO3bcZg4Ev5t3XSwPn5rnNpsZv5kdxw1OENvz"
    "Hvxid61jxyt30+P4BxQdejmNkPcNiZeUAVpmi+2gXn3zUySImv4COF9VpyaE7Nm7al/HVNtsretCRlLTda2ND1ZMIqQXb7b9"
    "tI4lvNm60IRybrPpS5+vMVs/SDtAEt40vxz2s/nQzm0Lp78XPvARIeWsmdpBA2n2x+lWVlOI/oO7cM5sByF+5iH70n2Yb89m"
    "uI4fsVaCti+CLh+Vd+gYIUtay7ykDNAy23HiMcvrmmeRKaJQUyUAU7MG6bC/G5ydOGmZ7RSkz7CuuI9Hk09cY7bTvNmAri6z"
    "5UP6/Jut1jxrJBvFbm6JtuPhbK/wgXcJ2WEN9WfAak+kM0A6Aq0842s2Fp/1mJN/tr3TnhS4zmVl23l14MI+YrusP+foXdi8"
    "hvh4SRmgZbaYDVSpnfKHH6kiCjVVAjA1+yblWK7KM9tKcEpSJTvs05AtRiqz5UP6MMxG/mifvw+nn9mTVDDESuW/hvTpzvnn"
    "EMtvIPkCEpZk+JmNxW/ia2/mtGcMcU0r7ICE85LuZLdwNjB5ekkZoGe2CzRovDxErohcTaUA5ld10W51fdNs4yB1Pfe5S7pm"
    "05c+HLOddOYpD8K5M2hfDac7uPgDhAyxfhDwCJSZwpfGbhRf+phtIosXZg6+stvDCouO5fIGwC/2sqQ7f4JlKJmXlAF6ZrtC"
    "Wx8pv7ApFFGqKRWAqZk1zD4/vxD8cAJS4/l25WiaLR/Sh2O2Vc55Lpwvsk8nwBNVOW41fasz6rgKoQ8LpXWGpGs+ZlsKZ0fE"
    "a4/dHlbYdSGTbb/r7tuboTT3hiHxkjJAz2xt4t33wBBUiijVlArA1HwtZJkDUtcK3WmlZzZ96cMy21dcAuwz3Oqc3oR8Z9Gm"
    "O1cAe+D+s1DaSjZTt93bbNfc9xh4NtpxgCtsqZB3C5I6+3XmEB0gf2JIvKQM0DQbXJbOyEOUiqjUlArA1GzurhOeHck7QlKE"
    "ntm0pQ/PbMu5hJMwD+ac1oT8Y/bpMufmvpNNJH4qTtmyr22zp9m84m3MTHGJ+y+Q1NGvMxvNZSh/LykD9MzWOouQsfIQtSIq"
    "NaUCMDVPuOq8nRlqwVVaZtOXPjyzZXMJl+n5Hec0Dp5U4ttYt/do0iHWeWqnnBWLYw+zVz3N9rZXvCFkiqs7TKOePn3pMdBc"
    "hvL1kjJA02xf0xDF5JNaEX81AwjA1GzoqrMS+1wlIW2/ltm0pQ/TbPw2562i2YyFhNubepyQv9pT5ayRro2EIyBttafZPOPF"
    "QW3dCJ6tbOib4e2PennLUH5eUgbomq0CIVvSFYsHakV81QwgQMhXBZT1qPOAltl0pQ/XbJ0kZouE6/S8buZME1WupDifs8Zj"
    "BHHLs3DPeNfkkAcpnvGdrWUoPy8pAzTNBnMYJRT6BlBEoaZUgH6hK82G0dyjztpaZtOVvjDNZr4t0t/6Edy100uxBuWKxb0o"
    "zFsKhXvGi4V5UdorfGo0IU8aEi8pA3TNtsw1tPXvhFQRhZpSATzNdtqjTj2zaUpfuGbLcV7I+xshp+30G16/Y3b9fcuz8Buy"
    "K5uZGfjlmANEwjdBAjTNlp1E7zSqVgVQRKGmVABPs/UO+8qmKX3hmi0GVjDJt/SoaibZcshO/ztrZBuxOBa7z7Nwz3ixsMhi"
    "a7ZvaEBNVasCKKJQM1LbbEM96lSO2TbyZtOUvnDNZnwGIZ/Tg0cJOeUkL/B4DjKyhNUuoXAzPtG7aWbm8qA9qVjXBXt832Ue"
    "9w4SoGe2jDviZKysE1JFFGou1zZbIvtcSfXT6A0uYBZvNk3pC9lsE2GbFH0Si5shTPN0Ghk6w5PCGn7Gs3Az3mfXhJl5Id9d"
    "a6z4xzJhjtm+DDTHGUARhZoXtM3WGrYakXPSebavxeVvw7jOmy1c6QvWbGxwTGrAx4WlmJvuHUVsSpyQ5NvehbP4hT5tu+m6"
    "AJgjpezs3GJhNtq65DXqVqgVUagpFcDTbMYuSH1VuoIwGM67cAF3hBWEMKUvYLMxwXZDH4RfLWzvIuOEyItsq6hP4cdDVw6/"
    "oylj2BG8aU7miPWWFJdt753ZSrcnZFuAVqgV8VdTLYC32fZBagU+Ja6Fy2xsXMfN27QRd+qGKX0Bm81cl+3f3pogslZi4Po7"
    "kF99MD4WuykWzuIz+d0FxnmaYm7bbDcdth58EKLjP4qD2dZzrwnIUCvir6ZaAG+zfcc2188PmVuu6App65xvEs0WpvQFbTb2"
    "eNOBkH+GfgfCXeMoqyDGr/D1wn4sYC4hPfMkZ7sX9vOZuZUJqRdTDMx2aA8/IyZDqYhETaUA3mYzYH9m3u2Bu4vyZsuAp6PU"
    "OvaocbHrHYTwpC9os6VPZ82Ldz3VR1WBsa7zo4prAEGDfAtn8YurihNCVifbwQ6APZGuZ6bvi8MDAvx3tycCNUOpiERNpQA+"
    "ZkuEnejJK7l1jFT31fQYJPTJOxl2hIwVzRae9AVtNti9DO+luZOPQj/v2gPJEqp3EI6KO/R70LvOXHsz4EpYyRmbLVzsA//T"
    "wkI1G5Uk63awdqgUkampEsDHbObD5w/W+n/F6Pj1brONYhX2gQ35GTlzSQP3OwhhSR9KHVheZe/rpcGRuTf7Fj1i/7DvEj3I"
    "zUtgN3CI4Zd8zf0M/wopdjC8jvVGDrvgTrjCv13FKrQLb8THH17B4tOP01vJ4m+dwr6HSYG+75nu+wje2qgX4D/yPsRWjtPY"
    "FkJ2WEYjYDRLMPdRX2PHHvu+y/Ar6Sr8FVGrKRFA+KoixI92Yxeu5H9X2m5E/Yc6cjxbQbhL46wh/s6x5muTl6+tbUXIyKnM"
    "bItowAthSe+H57853cAnZbsThC0O8NZVFY9ZTfM90JNDTi1jI+GBF70rHC3G39lbok9L+Pr7CvtxDrJX/6Zff6TENHYhOhKk"
    "w3tU/4NUFjArZHXhSmgNvegt76nASvsqEkBNfwGEb4b8V/zY7ZvWC+bstd+M2lac/WCcuMX5cHSOuVwlvLqQL+kLy2znXJvn"
    "nVEK/4b7sROGwmxCfObrE8XCcrtGO5+ZMTvQCLXQzVY+1b1pWjFu81EkiJq+AkjNZmScT7OyRh6PMULNZpy9bKUd/sLwMFu+"
    "pL8HpJybtLVWataHEV2masRPP9nkqsd6XFTzbbt/SE1LaLCxbIbx/4quIgUgQOzsY4frJyVc6pLtExDXe9uIPZlPR5QaVsA1"
    "IwiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIAiCIMb/AMyG"
    "jEjb3wKwAAAAAElFTkSuQmCC"
)

#: The words the recognizer has to come back with. A run that recognizes
#: nothing, or recognizes something else, is a failure and not a slow success.
SCAN_WORDS = "Invoice 4417 overdue"

#: The needle `find` looks for in those same pixels. One word of the drawn line,
#: so a hit proves the search read the recognizer's text and not a text layer.
SCAN_NEEDLE = "4417"

EXPECTED_CLEAN_OUTPUT = {
    "sizeBytes": 2743,
    "sha256": "86c3735f9ed152d6d88a2ea6feba42ee510fa486396d761b9186867db7232eb9",
}
EXPECTED = {
    "intake": {"status": "accepted", "detection": {"resolvedKind": "office", "resolvedFormat": "word-ooxml"}},
    "inspect-before": {"status": "completed", "metadata": {"author": AUTHOR}},
    "text-before": {"status": "completed", "result": {"truncated": False}},
    "clean": {"status": "completed", "result": {"engine": "office-clean", "removed": ["DocumentProperties"], "skipped": []}},
    "inspect-after": {"status": "completed"},
    "text-after": {"status": "completed", "result": {"truncated": False}},
    # The synthetic case the documentation's demonstration starts from: fourteen planted files
    # and the manifest that says what is in each one, published without replacing anything.
    "sample": {
        "status": "completed",
        "case": {"manifest": "MANIFEST.json"},
        "output": {"fileCount": 15},
        "code": None,
    },
    # Two files, one answer, over the two contracts the sample case just wrote: the same author
    # and the same package, one changed member and one changed paragraph.
    "compare": {
        "status": "completed",
        "identical": False,
        "detection": {"sameKind": True},
        "text": {"status": "completed", "unit": "paragraph"},
        "code": None,
    },
    "compare-identical": {
        "status": "completed",
        "identical": True,
        "bytes": {"identical": True, "members": None},
        "text": {"status": "skipped", "code": "identical_bytes"},
        "code": None,
    },
    "no-replace": {"status": "refused", "code": "destination_exists", "output": None},
    "unsupported-clean": {"status": "refused", "code": "parser_refused", "output": None},
    # One report about the whole input folder, before anything writes into it: every generated
    # file walked and inspected, nothing refused, no archive, no two files alike and no name that
    # disagrees with its own bytes.
    "report": {
        "status": "completed",
        "totals": {"roots": 1, "filesRefused": 0, "filesFailed": 0, "archiveEntries": 0},
        "findings": {
            "total": 4,
            "bySeverity": {"high": 0, "medium": 4, "low": 0},
            "byCategory": {"privacy": 1, "hidden": 2, "security": 1, "integrity": 0, "info": 0},
        },
        "duplicates": {"identical": [], "sameNameDifferentBytes": []},
        "mismatches": [],
    },
    # One needle across the same folder: ten of the generated files carry "client" in some case,
    # the CSV among them, searched field by field, and seven are ruled out by the byte pre-filter
    # without any read; find addresses every family generated here, so none is skipped. The three
    # pictures are searched rather than skipped, because a picture holds no text of its own and
    # only a reading of its pixels can answer; none of them carries the needle.
    "find-folder": {
        "status": "completed",
        "needle": {"text": "client", "ignoreCase": True, "regex": False},
        "totals": {
            "roots": 1, "filesWalked": 22, "filesSearched": 15, "filesRejected": 7,
            "filesSkipped": 0, "filesWithHits": 10, "filesWithoutHits": 12,
            "matches": 11, "hits": 11, "truncated": False,
        },
    },
}


def sample_files() -> dict[str, bytes]:
    """Fixed ZIP timestamps, order, permissions, and stored bytes, independent of zlib."""
    parts = with_word_settings({
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>',
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in SAMPLE_TEXT.split("\n"))
        + '</w:body></w:document>',
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:creator>{AUTHOR}</dc:creator><dc:title>Synthetic evaluation</dc:title></cp:coreProperties>',
    })
    return {
        "report.docx": stored_zip(parts),
        "letter.docx": stored_zip(LETTER_PARTS),
        "readme.txt": b"Synthetic text is not a supported clean-copy format.\n",
        "table.docx": stored_zip(TABLE_PARTS),
        "template.docx": stored_zip(TEMPLATE_PARTS),
        "modern.docx": stored_zip(MODERN_PARTS),
        "invoice.docx": stored_zip(INVOICE_PARTS),
        "invoice.xlsx": stored_zip(INVOICE_WORKBOOK_PARTS),
        "brief.md": BRIEF_MARKDOWN.encode("utf-8"),
        "rows.csv": ROWS_CSV.encode("utf-8"),
        "data.xlsx": stored_zip(WORKBOOK_PARTS),
        "merged.xlsx": stored_zip(MERGED_PARTS),
        "chartdata.xlsx": stored_zip(CHART_DATA_PARTS),
        "deck.pptx": stored_zip(DECK_PARTS),
        "tabledeck.pptx": stored_zip(TABLE_DECK_PARTS),
        "chartdeck.pptx": stored_zip({**CHART_DECK_PARTS, "ppt/embeddings/Microsoft_Excel_Worksheet.xlsx": stored_zip(CHART_WORKBOOK_PARTS)}),
        "update.json": SAMPLE_JSON.encode("utf-8"),
        "photo.png": stored_png(PHOTO_ROWS),
        "photo2.png": stored_png(SECOND_PHOTO_ROWS),
        "report.pdf": classic_pdf(PDF_PAGE_TEXTS),
        "notes.pdf": classic_pdf([NOTES_PAGE_TEXT]),
        "scan.png": base64.b64decode(SCAN_PNG_BASE64),
    }


def stored_zip(parts: dict[str, str | bytes]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as package:
        for name, content in parts.items():
            entry = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            package.writestr(entry, content if isinstance(content, bytes) else content.encode("utf-8"))
    return target.getvalue()


_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_WML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# Word reads a DOCX that declares no compatibility mode as a Word 2007 document: it opens the file
# in Compatibility Mode and raises the Compatibility Checker when a save touches a newer feature.
# Every synthetic DOCX below carries the same minimal settings part, and nothing else in it, so the
# native lab sees these fixtures in the mode their content is actually written in.
WORD_SETTINGS_PART = "word/settings.xml"
WORD_SETTINGS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
WORD_SETTINGS_RELATIONSHIP = f"{_REL}/settings"
WORD_SETTINGS_XML = (
    f'<w:settings xmlns:w="{_WML}"><w:compat>'
    '<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'
    '</w:compat></w:settings>'
)
DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"


def with_word_settings(parts: dict[str, str]) -> dict[str, str]:
    """The same DOCX parts plus word/settings.xml, its content-type override and its relationship.

    Every other part, id and planted string comes back untouched; the settings relationship takes
    the next free rId so no fixture's own relationship ids move.
    """
    if WORD_SETTINGS_PART in parts:
        raise ValueError("the fixture already carries a Word settings part")
    updated = dict(parts)
    rels = updated.get(DOCUMENT_RELS_PART, f'<Relationships xmlns="{_PKG_REL}"></Relationships>')
    identifier = f"rId{rels.count('<Relationship ') + 1}"
    if f'Id="{identifier}"' in rels or "</Relationships>" not in rels:
        raise ValueError(f"{DOCUMENT_RELS_PART} cannot take the settings relationship as {identifier}")
    updated[DOCUMENT_RELS_PART] = rels.replace(
        "</Relationships>",
        f'<Relationship Id="{identifier}" Type="{WORD_SETTINGS_RELATIONSHIP}" Target="settings.xml"/></Relationships>',
    )
    content_types = updated["[Content_Types].xml"]
    if "</Types>" not in content_types:
        raise ValueError("the fixture content types cannot take the settings override")
    updated["[Content_Types].xml"] = content_types.replace(
        "</Types>",
        f'<Override PartName="/{WORD_SETTINGS_PART}" ContentType="{WORD_SETTINGS_CONTENT_TYPE}"/></Types>',
    )
    updated[WORD_SETTINGS_PART] = WORD_SETTINGS_XML
    return updated


LETTER_HEADER_TEXT = "Quarterly report header"
LETTER_BODY_TEXT = "Dear client, the quarterly summary follows."
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
LETTER_PARTS = with_word_settings({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>',
    "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>',
    "word/document.xml": f'<w:document xmlns:w="{_W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
    f'<w:p><w:r><w:t>{LETTER_BODY_TEXT}</w:t></w:r></w:p>'
    '<w:sectPr><w:headerReference w:type="default" r:id="rId1"/></w:sectPr></w:body></w:document>',
    "word/header1.xml": f'<w:hdr xmlns:w="{_W}"><w:p><w:r><w:t>{LETTER_HEADER_TEXT}</w:t></w:r></w:p></w:hdr>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
})

PDF_PAGE_TEXTS = ["Page one opens the appendix", "", "Page three closes the appendix"]
NOTES_PAGE_TEXT = "Notes page for the combined packet"


def classic_pdf(page_texts: list[str]) -> bytes:
    """A classic uncompressed PDF with one Courier line per page; an empty text makes a textless page."""
    objects: dict[int, str] = {}
    font = 3 + len(page_texts) * 2
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = "<< /Type /Pages /Kids [" + " ".join(f"{3 + index * 2} 0 R" for index in range(len(page_texts))) + f"] /Count {len(page_texts)} >>"
    for index, text in enumerate(page_texts):
        page = 3 + index * 2
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objects[page] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {page + 1} 0 R >>"
        objects[page + 1] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    objects[font] = "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>"
    out = "%PDF-1.7\n%\u00e2\u00e3\u00cf\u00d3\n"
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out.encode("latin-1"))
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n"
    xref = len(out.encode("latin-1"))
    largest = max(objects)
    out += f"xref\n0 {largest + 1}\n0000000000 65535 f \n"
    for number in range(1, largest + 1):
        out += f"{offsets[number]:010d} 00000 n \n" if number in offsets else "0000000000 00000 f \n"
    out += f"trailer\n<< /Size {largest + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def pdf_current_objects(data: bytes) -> tuple[dict[int, int], bytes]:
    """Object offsets of a classic-xref PDF as its newest revision sees them, plus the last trailer.

    An incremental update keeps every earlier byte and appends replacements, so the
    newest xref section wins and earlier sections only fill the objects it left alone.
    """
    tail = data.rfind(b"startxref")
    require(tail >= 0, "the PDF has no startxref")
    start_match = re.match(rb"startxref\s+(\d+)", data[tail:])
    require(start_match is not None, "the PDF startxref is malformed")
    start: int | None = int(start_match.group(1))
    offsets: dict[int, int] = {}
    seen: set[int] = set()
    trailer = b""
    while start is not None and start not in seen:
        seen.add(start)
        require(data[start:start + 4] == b"xref", "this oracle reads classic xref tables only")
        position = start + 4
        while True:
            section = re.compile(rb"\s*(\d+)\s+(\d+)\s+").match(data, position)
            if section is None:
                break
            first, count = int(section.group(1)), int(section.group(2))
            position = section.end()
            for number in range(first, first + count):
                entry = re.compile(rb"(\d{10}) (\d{5}) ([nf])\s*").match(data, position)
                require(entry is not None, "a PDF xref entry is malformed")
                position = entry.end()
                if entry.group(3) == b"n" and number not in offsets:
                    offsets[number] = int(entry.group(1))
        trailer_at = data.find(b"trailer", position)
        require(trailer_at >= 0, "a PDF xref section has no trailer")
        section_trailer = data[trailer_at:data.find(b"startxref", trailer_at)]
        if not trailer:
            trailer = section_trailer
        previous = re.search(rb"/Prev\s+(\d+)", section_trailer)
        start = int(previous.group(1)) if previous else None
    return offsets, trailer


def pdf_object(data: bytes, offsets: dict[int, int], number: int) -> bytes:
    offset = offsets.get(number)
    require(offset is not None, f"PDF object {number} is not in the current xref")
    header = re.compile(rb"(\d+)\s+(\d+)\s+obj\s*").match(data, offset)
    require(header is not None and int(header.group(1)) == number, f"PDF object {number} is not at its xref offset")
    end = data.find(b"endobj", header.end())
    require(end >= 0, f"PDF object {number} has no end")
    return data[header.end():end]


def pdf_reference(body: bytes, key: bytes) -> int:
    match = re.search(key + rb"\s+(\d+)\s+\d+\s+R", body)
    require(match is not None, f"PDF dictionary has no {key.decode()} reference")
    return int(match.group(1))


def pdf_stream_body(body: bytes) -> bytes:
    start = body.find(b"stream")
    require(start >= 0, "PDF object is not a stream")
    start += len(b"stream")
    start += 2 if body[start:start + 2] == b"\r\n" else 1
    end = body.find(b"endstream", start)
    require(end >= 0, "PDF stream has no end")
    raw = body[start:end]
    try:
        return zlib.decompress(raw, bufsize=65536)
    except zlib.error:
        return raw


def pdf_current_page_texts(data: bytes) -> list[bytes]:
    """Content stream bodies of the pages the newest revision's page tree lists, in order."""
    offsets, trailer = pdf_current_objects(data)
    catalog = pdf_object(data, offsets, pdf_reference(trailer, b"/Root"))
    pages = pdf_object(data, offsets, pdf_reference(catalog, b"/Pages"))
    kids = re.search(rb"/Kids\s*\[([^\]]*)\]", pages)
    require(kids is not None, "the page tree has no Kids array")
    numbers = [int(match.group(1)) for match in re.finditer(rb"(\d+)\s+\d+\s+R", kids.group(1))]
    count = re.search(rb"/Count\s+(\d+)", pages)
    require(count is not None and int(count.group(1)) == len(numbers), "the page tree count does not match its kids")
    texts: list[bytes] = []
    for number in numbers:
        page = pdf_object(data, offsets, number)
        require(re.search(rb"/Type\s*/Page\b", page) is not None, f"kid {number} is not a page")
        texts.append(b"\n".join(pdf_stream_body(pdf_object(data, offsets, part)) for part in pdf_content_streams(page)))
    return texts


def pdf_current_page_images(data: bytes) -> list[int]:
    """How many image XObjects each page of the newest revision draws, in page order."""
    offsets, trailer = pdf_current_objects(data)
    catalog = pdf_object(data, offsets, pdf_reference(trailer, b"/Root"))
    pages = pdf_object(data, offsets, pdf_reference(catalog, b"/Pages"))
    kids = re.search(rb"/Kids\s*\[([^\]]*)\]", pages)
    require(kids is not None, "the page tree has no Kids array")
    numbers = [int(match.group(1)) for match in re.finditer(rb"(\d+)\s+\d+\s+R", kids.group(1))]
    counts: list[int] = []
    for number in numbers:
        page = pdf_object(data, offsets, number)
        resources = re.search(rb"/XObject\s*<<([^>]*)>>", page)
        drawn = 0
        if resources is not None:
            for match in re.finditer(rb"(\d+)\s+\d+\s+R", resources.group(1)):
                referenced = pdf_object(data, offsets, int(match.group(1)))
                if re.search(rb"/Subtype\s*/Image\b", referenced) is not None:
                    drawn += 1
        counts.append(drawn)
    return counts


def pdf_content_streams(page: bytes) -> list[int]:
    """The object numbers of a page's content streams, whether /Contents is one reference or an array."""
    array = re.search(rb"/Contents\s*\[([^\]]*)\]", page)
    if array is not None:
        numbers = [int(match.group(1)) for match in re.finditer(rb"(\d+)\s+\d+\s+R", array.group(1))]
        require(bool(numbers), "a page /Contents array holds no references")
        return numbers
    return [pdf_reference(page, b"/Contents")]


RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)
# An 8x4 picture in four solid quadrants, so crops, quarter turns and 2:1
# shrinks have exact expected pixels without any resampling tolerance.
PHOTO_ROWS = [[RED] * 4 + [GREEN] * 4] * 2 + [[BLUE] * 4 + [WHITE] * 4] * 2
# A 4x4 replacement picture. It is square where PHOTO_ROWS is 2:1, so a fitted extent
# has to keep the drawn width and double the drawn height, which a kept extent cannot fake.
SECOND_PHOTO_ROWS = [[WHITE] * 2 + [BLUE] * 2] * 2 + [[GREEN] * 2 + [RED] * 2] * 2


def stored_png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """An 8-bit RGB PNG whose deflate stream uses stored blocks, so the bytes never depend on a zlib version."""
    width, height = len(rows[0]), len(rows)
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows)
    blocks, remaining = b"", raw
    while True:
        chunk, remaining = remaining[:65535], remaining[65535:]
        final = 0 if remaining else 1
        blocks += bytes([final]) + len(chunk).to_bytes(2, "little") + (0xFFFF - len(chunk)).to_bytes(2, "little") + chunk
        if final:
            break
    stream = b"\x78\x01" + blocks + zlib.adler32(raw).to_bytes(4, "big")

    def chunk_bytes(tag: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk_bytes(b"IHDR", header) + chunk_bytes(b"IDAT", stream) + chunk_bytes(b"IEND", b"")


def drawing_extent(package: bytes) -> tuple[int, int]:
    """The one inline drawing's cx and cy in EMU, read straight from word/document.xml."""
    with zipfile.ZipFile(io.BytesIO(package)) as document:
        body = ET.fromstring(document.read("word/document.xml"))
    extents = body.findall(f".//{{{_WPD}}}extent")
    require(len(extents) == 1, "the document does not carry exactly one inline drawing")
    return int(extents[0].get("cx")), int(extents[0].get("cy"))


def decode_png(data: bytes, max_side: int = 64) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """An independent decoder for 8-bit RGB and RGBA non-interlaced PNG output; returns RGB rows."""
    require(data[:8] == b"\x89PNG\r\n\x1a\n", "the image output is not a PNG")
    position, idat, width, height, color_type = 8, b"", 0, 0, -1
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        tag, body = data[position + 4:position + 8], data[position + 8:position + 8 + length]
        position += 12 + length
        if tag == b"IHDR":
            width, height = int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big")
            require(body[8] == 8 and body[9] in (2, 6) and body[12] == 0, "the oracle decodes 8-bit RGB or RGBA non-interlaced PNG only")
            color_type = body[9]
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    require(0 < width <= max_side and 0 < height <= max_side, "the image output has an unexpected size")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    raw = zlib.decompress(idat, bufsize=stride * height + height)
    require(len(raw) == (stride + 1) * height, "the PNG pixel stream has the wrong length")
    rows: list[list[tuple[int, int, int]]] = []
    previous = bytearray(stride)
    for y in range(height):
        method = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        require(method <= 4, "unknown PNG filter method")
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            if method == 1:
                line[i] = (line[i] + left) & 0xFF
            elif method == 2:
                line[i] = (line[i] + up) & 0xFF
            elif method == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif method == 4:
                estimate = left + up - up_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - up_left))
                predictor = left if distances[0] <= distances[1] and distances[0] <= distances[2] else (up if distances[1] <= distances[2] else up_left)
                line[i] = (line[i] + predictor) & 0xFF
        rows.append([tuple(line[x * channels:x * channels + 3]) for x in range(width)])
        previous = line
    return width, height, rows


def element_shape(element: ET.Element) -> tuple:
    """Tag, sorted attributes, text and children of an element, independent of namespace prefixes."""
    return (
        element.tag.split("}")[-1],
        tuple(sorted(element.attrib.items())),
        (element.text or "").strip(),
        tuple(element_shape(child) for child in element),
    )


def worksheet_cells(sheet: ET.Element, shared: list[str]) -> dict[str, tuple[str, str]]:
    """Every cell of a worksheet as (type, value), resolving shared strings like a reader does."""
    namespace = {"s": _SML}
    values = {}
    for cell in sheet.findall(".//s:c", namespace):
        kind = cell.get("t")
        stored = cell.find("s:v", namespace)
        formula = cell.find("s:f", namespace)
        if formula is not None:
            values[cell.get("r")] = ("formula", formula.text or "")
        elif kind == "s":
            index = int((stored.text or "").strip())
            require(0 <= index < len(shared), "a cell references a shared string that does not exist")
            values[cell.get("r")] = ("string", shared[index])
        elif kind == "inlineStr":
            values[cell.get("r")] = ("string", "".join(cell.find("s:is", namespace).itertext()))
        elif kind == "b":
            values[cell.get("r")] = ("boolean", "TRUE" if (stored.text or "").strip() in ("1", "true") else "FALSE")
        elif stored is not None:
            values[cell.get("r")] = ("number", (stored.text or "").strip())
    return values


SAMPLE_JSON = '{\n  "client": "Acme",\n  "status": "draft",\n  "amount": 1.50,\n  "items": [{"id": 7, "done": false}]\n}\n'
_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WPD = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
TABLE_ROWS = [["Client", "Amount"], ["Acme", "10"]]
TABLE_PARTS = with_word_settings({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/document.xml": f'<w:document xmlns:w="{_WML}"><w:body>'
    '<w:p><w:r><w:t>Quarterly table</w:t></w:r></w:p>'
    '<w:tbl>'
    '<w:tr><w:trPr><w:tblHeader/></w:trPr>'
    '<w:tc><w:tcPr><w:shd w:fill="EEEEEE"/></w:tcPr><w:p><w:r><w:t>Client</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:t>Amount</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr>'
    '<w:tc><w:tcPr><w:gridSpan w:val="1"/></w:tcPr><w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
    '<w:r><w:rPr><w:i/></w:rPr><w:t>Acme</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:t>10</w:t></w:r></w:p></w:tc></w:tr>'
    '</w:tbl>'
    '<w:p><w:r><w:t>Closing</w:t></w:r></w:p>'
    '</w:body></w:document>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
})
# A merge template. `{{client}}` is deliberately split over two runs, the way a word processor
# leaves a placeholder an author typed in pieces; `{{date}}` sits whole in the next paragraph and
# `{{amount}}` in a table cell. `docProps/app.xml` must come back byte for byte.
TEMPLATE_CLIENT = "Northwind Trading"
TEMPLATE_DATE = "2026-09-15"
TEMPLATE_AMOUNT = "4,250.00"
TEMPLATE_MERGED_TEXTS = [
    f"Dear {TEMPLATE_CLIENT},",
    f"Your invoice is dated {TEMPLATE_DATE}.",
    f"Total: {TEMPLATE_AMOUNT}",
    "Regards.",
]
TEMPLATE_PARTS = with_word_settings({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/document.xml": f'<w:document xmlns:w="{_WML}"><w:body>'
    '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Dear {{cli</w:t></w:r>'
    '<w:r><w:rPr><w:i/></w:rPr><w:t>ent}},</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>Your invoice is dated {{date}}.</w:t></w:r></w:p>'
    '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Total: {{amount}}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    '<w:p><w:r><w:t>Regards.</w:t></w:r></w:p>'
    '</w:body></w:document>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
})
# An invoice template whose table carries a repeating block row, and a workbook whose block row
# sits inside the range its total sums, so the rows a merge adds land inside SUM(B2:B4).
INVOICE_NUMBER = "A-17"
INVOICE_CLIENT = "Northwind Trading"
INVOICE_LINES = [("Widget", "10.00"), ("Gadget", "20.00"), ("Sprocket", "30.00")]
INVOICE_MERGED_TEXTS = (
    [f"Invoice {INVOICE_NUMBER}", "Item", "Amount"]
    + [value for line in INVOICE_LINES for value in line]
    + [f"Thank you, {INVOICE_CLIENT}."]
)
INVOICE_PARTS = with_word_settings({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/document.xml": f'<w:document xmlns:w="{_WML}"><w:body>'
    '<w:p><w:r><w:t>Invoice {{number}}</w:t></w:r></w:p>'
    '<w:tbl>'
    '<w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:t>Amount</w:t></w:r></w:p></w:tc></w:tr>'
    '<w:tr><w:tc><w:tcPr><w:shd w:fill="EEEEEE"/></w:tcPr><w:p><w:r><w:rPr><w:i/></w:rPr>'
    '<w:t>{{#lines}}{{name}}</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:t>{{lines.amount}}{{/lines}}</w:t></w:r></w:p></w:tc></w:tr>'
    '</w:tbl>'
    '<w:p><w:r><w:t>Thank you, {{client}}.</w:t></w:r></w:p>'
    '</w:body></w:document>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
})
INVOICE_WORKBOOK_PARTS = {
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": f'<workbook xmlns="{_SML}" xmlns:r="{_REL}"><sheets><sheet name="Lines" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SML}"><dimension ref="A1:B6"/><sheetData>'
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Item</t></is></c><c r="B1" t="inlineStr"><is><t>Amount</t></is></c></row>'
    '<row r="2"><c r="A2" t="inlineStr"><is><t>Setup</t></is></c><c r="B2"><v>5</v></c></row>'
    '<row r="3"><c r="A3" t="inlineStr"><is><t>{{#lines}}{{name}}</t></is></c>'
    '<c r="B3" t="inlineStr"><is><t>{{lines.amount}}{{/lines}}</t></is></c></row>'
    '<row r="4"><c r="A4" t="inlineStr"><is><t>Shipping</t></is></c><c r="B4"><v>7</v></c></row>'
    '<row r="5"><c r="A5" t="inlineStr"><is><t>Total</t></is></c><c r="B5"><f>SUM(B2:B4)</f><v>12</v></c></row>'
    '<row r="6"><c r="A6" t="inlineStr"><is><t>Paid by {{client}}</t></is></c></row>'
    '</sheetData></worksheet>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
}
# A document saved the way Word has saved since 2016: commentsExtended, commentsIds and people
# beside word/comments.xml, with one comment already on the first paragraph. The comment steps
# add a span comment and a reply to it, so every one of those parts has to gain an entry.
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_WORDML_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml"
MODERN_TEXTS = ["Review this", "Second paragraph", "Third"]
MODERN_COMMENT_TEXT = "Please check this"
MODERN_PARA_ID = "1A2B3C4D"
MODERN_PARTS = with_word_settings({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{_WORDML_TYPE}.document.main+xml"/>'
    f'<Override PartName="/word/comments.xml" ContentType="{_WORDML_TYPE}.comments+xml"/>'
    f'<Override PartName="/word/commentsExtended.xml" ContentType="{_WORDML_TYPE}.commentsExtended+xml"/>'
    f'<Override PartName="/word/commentsIds.xml" ContentType="{_WORDML_TYPE}.commentsIds+xml"/>'
    f'<Override PartName="/word/people.xml" ContentType="{_WORDML_TYPE}.people+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/></Relationships>',
    "word/_rels/document.xml.rels": f'<Relationships xmlns="{_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_REL}/comments" Target="comments.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2011/relationships/commentsExtended" Target="commentsExtended.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.microsoft.com/office/2016/09/relationships/commentsIds" Target="commentsIds.xml"/>'
    '<Relationship Id="rId4" Type="http://schemas.microsoft.com/office/2011/relationships/people" Target="people.xml"/></Relationships>',
    "word/document.xml": f'<w:document xmlns:w="{_WML}"><w:body>'
    f'<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>{MODERN_TEXTS[0]}</w:t></w:r>'
    '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>'
    f'<w:p><w:r><w:t>{MODERN_TEXTS[1]}</w:t></w:r></w:p>'
    f'<w:p><w:r><w:t>{MODERN_TEXTS[2]}</w:t></w:r></w:p>'
    '</w:body></w:document>',
    "word/comments.xml": f'<w:comments xmlns:w="{_WML}" xmlns:w14="{_W14}">'
    '<w:comment w:id="0" w:author="Ann Park" w:initials="AP">'
    f'<w:p w14:paraId="{MODERN_PARA_ID}" w14:textId="77777777"><w:r><w:t>{MODERN_COMMENT_TEXT}</w:t></w:r></w:p>'
    '</w:comment></w:comments>',
    "word/commentsExtended.xml": f'<w15:commentsEx xmlns:w15="{_W15}">'
    f'<w15:commentEx w15:paraId="{MODERN_PARA_ID}" w15:done="0"/></w15:commentsEx>',
    "word/commentsIds.xml": f'<w16cid:commentsIds xmlns:w16cid="{_W16CID}">'
    f'<w16cid:commentId w16cid:paraId="{MODERN_PARA_ID}" w16cid:durableId="5F5F5F5F"/></w16cid:commentsIds>',
    "word/people.xml": f'<w15:people xmlns:w15="{_W15}">'
    '<w15:person w15:author="Ann Park"><w15:presenceInfo w15:providerId="None" w15:userId="Ann Park"/></w15:person>'
    '</w15:people>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
})
# The two sources `ufo create` turns into new Office files. Both are plain text, so the oracle
# below can state exactly what the produced document, deck and workbook must say.
BRIEF_MARKDOWN = """# Client brief

The **quarter** closed *early*. See the [guide](https://example.test/guide).

## Actions

- Send the invoice
- Book the review

Notes: mention the renewal

| Client | Amount |
| --- | --- |
| Acme | 10 |
"""
BRIEF_DOCX_TEXTS = [
    "Client brief",
    "The quarter closed early. See the guide (https://example.test/guide).",
    "Actions",
    "Send the invoice",
    "Book the review",
    "Notes: mention the renewal",
    "Client",
    "Amount",
    "Acme",
    "10",
]
BRIEF_DOCX_STYLES = ["Heading1", None, "Heading2", "ListParagraph", "ListParagraph", None]
BRIEF_SLIDE_TEXTS = [
    ["Client brief", "The quarter closed early. See the guide (https://example.test/guide)."],
    ["Actions", "Send the invoice", "Book the review", "Client | Amount", "Acme | 10"],
]
BRIEF_SLIDE_NOTES = [None, "mention the renewal"]
ROWS_CSV = "client,amount,paid\nAcme,1250.50,true\n\"Beta, Inc\",0,false\n"
ROWS_CELLS = [
    ("A1", "client"), ("B1", "amount"), ("C1", "paid"),
    ("A2", "Acme"), ("B2", "1250.50"), ("C2", "1"),
    ("A3", "Beta, Inc"), ("B3", "0"), ("C3", "0"),
]
WORKBOOK_PARTS = {
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": f'<workbook xmlns="{_SML}" xmlns:r="{_REL}"><sheets><sheet name="Summary" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{_REL}/sharedStrings" Target="sharedStrings.xml"/><Relationship Id="rId3" Type="{_REL}/styles" Target="styles.xml"/></Relationships>',
    "xl/styles.xml": f'<styleSheet xmlns="{_SML}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>',
    "xl/sharedStrings.xml": f'<sst xmlns="{_SML}" count="1" uniqueCount="1"><si><t>Client A</t></si></sst>',
    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SML}"><dimension ref="A1:D1"/><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>10</v></c><c r="C1" t="b"><v>1</v></c><c r="D1"><f>B1*2</f><v>20</v></c></row></sheetData></worksheet>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
}
# A quarterly table to chart: labels down column A, revenue down B, cost down C, so a
# category range and two series ranges all line up and every series cell holds a number.
CHART_DATA_ROWS = [("Q1", 10, 4), ("Q2", 20, 6), ("Q3", 30, 9), ("Q4", 40, 11)]
CHART_DATA_PARTS = {
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": f'<workbook xmlns="{_SML}" xmlns:r="{_REL}"><sheets><sheet name="Quarters" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SML}"><dimension ref="A1:C5"/><sheetData>'
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Quarter</t></is></c>'
    '<c r="B1" t="inlineStr"><is><t>Revenue</t></is></c>'
    '<c r="C1" t="inlineStr"><is><t>Cost</t></is></c></row>'
    + "".join(
        f'<row r="{index + 2}"><c r="A{index + 2}" t="inlineStr"><is><t>{label}</t></is></c>'
        f'<c r="B{index + 2}"><v>{revenue}</v></c><c r="C{index + 2}"><v>{cost}</v></c></row>'
        for index, (label, revenue, cost) in enumerate(CHART_DATA_ROWS)
    )
    + '</sheetData></worksheet>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
}
# A workbook whose only extra is a merge spanning rows 1 to 3, so an insertion inside it has to
# refuse rather than grow or split the merge.
MERGED_PARTS = {
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": f'<workbook xmlns="{_SML}" xmlns:r="{_REL}"><sheets><sheet name="Merged" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SML}"><dimension ref="A1:B3"/><sheetData>'
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Spanned</t></is></c><c r="B1"><v>1</v></c></row>'
    '<row r="2"><c r="B2"><v>2</v></c></row>'
    '<row r="3"><c r="B3"><v>3</v></c></row>'
    '</sheetData><mergeCells count="1"><mergeCell ref="A1:A3"/></mergeCells></worksheet>',
}
# ---- The master, layout, theme and notes-master chain PowerPoint refuses a deck without ----
# Ported from office-core's PptxBlankPresentation, which writes the same chain into every
# deck this build creates. Microsoft 365 PowerPoint 16.0 reports 0x80070570 ("corrupted and
# unreadable") for a deck that carries no slide master, slide layout or theme, and again
# when the notes master shares the slide master's theme part, so a deck with speaker notes
# carries a theme part of its own.
_SP_TREE_PREAMBLE = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)
_CLR_MAP = (
    'bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
    'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
    'hlink="hlink" folHlink="folHlink"'
)
SLIDE_LAYOUT1 = (
    f'<p:sldLayout xmlns:a="{_DML}" xmlns:r="{_REL}" xmlns:p="{_PML}" type="blank">'
    f'<p:cSld name="Blank"><p:spTree>{_SP_TREE_PREAMBLE}</p:spTree></p:cSld>'
    '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
)
SLIDE_MASTER1 = (
    f'<p:sldMaster xmlns:a="{_DML}" xmlns:r="{_REL}" xmlns:p="{_PML}">'
    f'<p:cSld><p:spTree>{_SP_TREE_PREAMBLE}</p:spTree></p:cSld>'
    f'<p:clrMap {_CLR_MAP}/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    '<p:txStyles>'
    '<p:titleStyle><a:lvl1pPr algn="ctr"><a:defRPr sz="4400"/></a:lvl1pPr></p:titleStyle>'
    '<p:bodyStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:bodyStyle>'
    '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle>'
    '</p:txStyles></p:sldMaster>'
)
NOTES_MASTER1 = (
    f'<p:notesMaster xmlns:a="{_DML}" xmlns:r="{_REL}" xmlns:p="{_PML}">'
    f'<p:cSld><p:spTree>{_SP_TREE_PREAMBLE}</p:spTree></p:cSld>'
    f'<p:clrMap {_CLR_MAP}/></p:notesMaster>'
)
# PowerPoint validates the whole clrScheme/fontScheme/fmtScheme trio (three fills, three
# lines, three effects, three background fills), so none of this theme is optional.
THEME1 = (
    f'<a:theme xmlns:a="{_DML}" name="Office Theme"><a:themeElements>'
    '<a:clrScheme name="Office">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    '</a:clrScheme>'
    '<a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
    '</a:fontScheme>'
    '<a:fmtScheme name="Office">'
    '<a:fillStyleLst>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:tint val="65000"/></a:schemeClr></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:shade val="95000"/></a:schemeClr></a:solidFill>'
    '</a:fillStyleLst>'
    '<a:lnStyleLst>'
    '<a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '</a:lnStyleLst>'
    '<a:effectStyleLst>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '</a:effectStyleLst>'
    '<a:bgFillStyleLst>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/></a:schemeClr></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"><a:shade val="92000"/></a:schemeClr></a:solidFill>'
    '</a:bgFillStyleLst>'
    '</a:fmtScheme>'
    '</a:themeElements></a:theme>'
)
_PRESENTATIONML = "application/vnd.openxmlformats-officedocument.presentationml"
_THEME_TYPE = "application/vnd.openxmlformats-officedocument.theme+xml"
_MASTER_CHAIN_TYPES = {
    "/ppt/slideLayouts/slideLayout1.xml": f"{_PRESENTATIONML}.slideLayout+xml",
    "/ppt/slideMasters/slideMaster1.xml": f"{_PRESENTATIONML}.slideMaster+xml",
    "/ppt/theme/theme1.xml": _THEME_TYPE,
}
_NOTES_CHAIN_TYPES = {
    "/ppt/notesMasters/notesMaster1.xml": f"{_PRESENTATIONML}.notesMaster+xml",
    "/ppt/theme/theme2.xml": _THEME_TYPE,
}
_EMPTY_RELS = f'<Relationships xmlns="{_PKG_REL}"></Relationships>'


def _rels_part(part: str) -> str:
    """The `_rels` member that carries one part's relationships."""
    folder, name = part.rsplit("/", 1)
    return f"{folder}/_rels/{name}.rels"


def _add_relationships(rels: str, entries: list[tuple[str, str]]) -> str:
    """Append relationships to one `_rels` part, numbered past the highest id it already uses."""
    first = max([int(found) for found in re.findall(r'Id="rId(\d+)"', rels)] or [0]) + 1
    return rels.replace("</Relationships>", "".join(
        f'<Relationship Id="rId{first + offset}" Type="{_REL}/{kind}" Target="{target}"/>'
        for offset, (kind, target) in enumerate(entries)
    ) + "</Relationships>")


def with_master_chain(parts: dict[str, str], slides: tuple[str, ...], notes_slides: tuple[str, ...] = ()) -> dict[str, str]:
    """One deck's parts plus the master chain PowerPoint refuses to open a deck without.

    Adds the slide master, the slide layout, the theme, and for a deck that carries speaker
    notes a notes master over a second theme part, then every relationship, content type and
    presentation id list that reaches them. Nothing the deck already plants is rewritten, so
    each pinned string, shape id, placeholder, table, chart and embedded workbook keeps its
    bytes; only the parts PowerPoint requires are added.
    """
    deck = dict(parts)
    overrides = dict(_MASTER_CHAIN_TYPES)
    chain = [("slideMaster", "slideMasters/slideMaster1.xml"), ("theme", "theme/theme1.xml")]
    if notes_slides:
        overrides.update(_NOTES_CHAIN_TYPES)
        chain.append(("notesMaster", "notesMasters/notesMaster1.xml"))
    package_rels = deck["ppt/_rels/presentation.xml.rels"]
    first = max([int(found) for found in re.findall(r'Id="rId(\d+)"', package_rels)] or [0]) + 1
    deck["ppt/_rels/presentation.xml.rels"] = _add_relationships(package_rels, chain)

    # Schema order: sldMasterIdLst, notesMasterIdLst, sldIdLst, sldSz, notesSz.
    identifiers = f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId{first}"/></p:sldMasterIdLst>'
    if notes_slides:
        identifiers += f'<p:notesMasterIdLst><p:notesMasterId r:id="rId{first + 2}"/></p:notesMasterIdLst>'
    presentation = deck["ppt/presentation.xml"]
    if "<p:sldIdLst>" not in presentation or "<p:sldSz " not in presentation:
        raise ValueError("a deck's presentation part must list its slides and state its slide size")
    presentation = presentation.replace("<p:sldIdLst>", identifiers + "<p:sldIdLst>", 1)
    # `notesSz` is mandatory in every presentation part, notes or not: PowerPoint refuses a
    # deck without it as corrupted, which the lab showed for the table and chart decks.
    presentation = presentation.replace("</p:presentation>", '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    deck["ppt/presentation.xml"] = presentation

    for slide in slides:
        rels = _rels_part(slide)
        deck[rels] = _add_relationships(deck.get(rels, _EMPTY_RELS), [("slideLayout", "../slideLayouts/slideLayout1.xml")])
    for notes in notes_slides:
        rels = _rels_part(notes)
        deck[rels] = _add_relationships(deck.get(rels, _EMPTY_RELS), [("notesMaster", "../notesMasters/notesMaster1.xml")])

    deck["[Content_Types].xml"] = deck["[Content_Types].xml"].replace("</Types>", "".join(
        f'<Override PartName="{part}" ContentType="{value}"/>' for part, value in overrides.items()
    ) + "</Types>")
    deck["ppt/slideLayouts/slideLayout1.xml"] = SLIDE_LAYOUT1
    deck["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
        f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/slideMaster" '
        'Target="../slideMasters/slideMaster1.xml"/></Relationships>'
    )
    deck["ppt/slideMasters/slideMaster1.xml"] = SLIDE_MASTER1
    deck["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = (
        f'<Relationships xmlns="{_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_REL}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        f'<Relationship Id="rId2" Type="{_REL}/theme" Target="../theme/theme1.xml"/></Relationships>'
    )
    deck["ppt/theme/theme1.xml"] = THEME1
    if notes_slides:
        deck["ppt/notesMasters/notesMaster1.xml"] = NOTES_MASTER1
        deck["ppt/notesMasters/_rels/notesMaster1.xml.rels"] = (
            f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/theme" '
            'Target="../theme/theme2.xml"/></Relationships>'
        )
        # The notes master gets a theme part of its own: PowerPoint refuses one it shares
        # with the slide master, so the same theme is written twice on purpose.
        deck["ppt/theme/theme2.xml"] = THEME1
    return deck


DECK_PARTS = with_master_chain({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/><Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
    "ppt/presentation.xml": f'<p:presentation xmlns:p="{_PML}" xmlns:r="{_REL}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
    '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>',
    "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/slide" Target="slides/slide1.xml"/></Relationships>',
    "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{_PML}" xmlns:a="{_DML}" xmlns:r="{_REL}"><p:cSld><p:spTree>'
    + _SP_TREE_PREAMBLE
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="838200" y="365125"/><a:ext cx="10515600" cy="1325563"/></a:xfrm></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Q3 review</a:t></a:r></a:p></p:txBody></p:sp>'
    '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Body 2"/><p:cNvSpPr/><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="838200" y="1825625"/><a:ext cx="10515600" cy="4351338"/></a:xfrm></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Revenue </a:t></a:r><a:r><a:rPr i="1"/><a:t>up 10%</a:t></a:r></a:p></p:txBody></p:sp>'
    '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>',
    "ppt/slides/_rels/slide1.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>',
    "ppt/notesSlides/notesSlide1.xml": f'<p:notes xmlns:p="{_PML}" xmlns:a="{_DML}" xmlns:r="{_REL}"><p:cSld><p:spTree>'
    + _SP_TREE_PREAMBLE
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder 1"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
    '<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Mention the new client</a:t></a:r></a:p></p:txBody></p:sp>'
    '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
}, ("ppt/slides/slide1.xml",), ("ppt/notesSlides/notesSlide1.xml",))


# A one-slide deck whose only content besides its title is a three-column table with a
# merged bottom-left pair, so a read shows both writable and refused cells.
def _table_cell(text: str, attributes: str = "") -> str:
    return (
        f'<a:tc{attributes}><a:txBody><a:bodyPr/><a:lstStyle/>'
        f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>'
    )


TABLE_DECK_ROWS = [["Region", "Q1", "Q2"], ["North", "20", "30"]]
TABLE_DECK_PARTS = with_master_chain({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
    "ppt/presentation.xml": f'<p:presentation xmlns:p="{_PML}" xmlns:r="{_REL}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
    '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>',
    "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/slide" Target="slides/slide1.xml"/></Relationships>',
    "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{_PML}" xmlns:a="{_DML}" xmlns:r="{_REL}"><p:cSld><p:spTree>'
    + _SP_TREE_PREAMBLE
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="838200" y="365125"/><a:ext cx="10515600" cy="1325563"/></a:xfrm></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Regional revenue</a:t></a:r></a:p></p:txBody></p:sp>'
    '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="Table 4"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
    '<p:xfrm><a:off x="838200" y="1825625"/><a:ext cx="6096000" cy="1097280"/></p:xfrm>'
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
    '<a:tbl><a:tblPr firstRow="1" bandRow="1"/>'
    '<a:tblGrid><a:gridCol w="2032000"/><a:gridCol w="2032000"/><a:gridCol w="2032000"/></a:tblGrid>'
    + "".join(
        '<a:tr h="365760">' + "".join(_table_cell(value) for value in row) + "</a:tr>"
        for row in TABLE_DECK_ROWS
    )
    + '<a:tr h="365760">'
    + _table_cell("Total", ' gridSpan="2"')
    + _table_cell("", ' hMerge="1"')
    + _table_cell("50")
    + '</a:tr>'
    '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
}, ("ppt/slides/slide1.xml",))


_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
CHART_CATEGORIES = ["Q1", "Q2", "Q3", "Q4"]
CHART_VALUES = ["10", "20", "30", "40"]
CHART_WORKBOOK = "ppt/embeddings/Microsoft_Excel_Worksheet.xlsx"


def _chart_cache(tag: str, entries: list[str], format_code: str | None) -> str:
    head = f"<c:formatCode>{format_code}</c:formatCode>" if format_code else ""
    points = "".join(f'<c:pt idx="{index}"><c:v>{value}</c:v></c:pt>' for index, value in enumerate(entries))
    return f'<c:{tag}>{head}<c:ptCount val="{len(entries)}"/>{points}</c:{tag}>'


CHART_PART = (
    f'<c:chartSpace xmlns:c="{_CHART}" xmlns:a="{_DML}" xmlns:r="{_REL}"><c:chart>'
    '<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Quarterly revenue</a:t></a:r></a:p></c:rich></c:tx></c:title>'
    '<c:plotArea><c:layout/><c:barChart><c:barDir val="col"/><c:ser><c:idx val="0"/><c:order val="0"/>'
    '<c:tx><c:strRef><c:f>Sheet1!$B$1</c:f><c:strCache><c:ptCount val="1"/>'
    '<c:pt idx="0"><c:v>Revenue</c:v></c:pt></c:strCache></c:strRef></c:tx>'
    '<c:cat><c:strRef><c:f>Sheet1!$A$2:$A$5</c:f>' + _chart_cache("strCache", CHART_CATEGORIES, None) + '</c:strRef></c:cat>'
    '<c:val><c:numRef><c:f>Sheet1!$B$2:$B$5</c:f>' + _chart_cache("numCache", CHART_VALUES, "General") + '</c:numRef></c:val>'
    '</c:ser><c:axId val="111111111"/><c:axId val="222222222"/></c:barChart>'
    '<c:catAx><c:axId val="111111111"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
    '<c:delete val="0"/><c:axPos val="b"/><c:crossAx val="222222222"/></c:catAx>'
    '<c:valAx><c:axId val="222222222"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
    '<c:delete val="0"/><c:axPos val="l"/><c:majorGridlines/><c:crossAx val="111111111"/></c:valAx>'
    '</c:plotArea></c:chart>'
    '<c:externalData r:id="rId1"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>'
)
CHART_WORKBOOK_PARTS = {
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
    "xl/workbook.xml": f'<workbook xmlns="{_SML}" xmlns:r="{_REL}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_SML}"><dimension ref="A1:B5"/><sheetData>'
    '<row r="1"><c r="B1" t="inlineStr"><is><t>Revenue</t></is></c></row>'
    + "".join(
        f'<row r="{index + 2}"><c r="A{index + 2}" t="inlineStr"><is><t>{category}</t></is></c>'
        f'<c r="B{index + 2}"><v>{value}</v></c></row>'
        for index, (category, value) in enumerate(zip(CHART_CATEGORIES, CHART_VALUES))
    )
    + '</sheetData></worksheet>',
}
CHART_DECK_PARTS = with_master_chain({
    "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>'
    '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
    '<Override PartName="/ppt/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/></Types>',
    "_rels/.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
    "ppt/presentation.xml": f'<p:presentation xmlns:p="{_PML}" xmlns:r="{_REL}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
    '<p:sldSz cx="9144000" cy="6858000"/></p:presentation>',
    "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/slide" Target="slides/slide1.xml"/></Relationships>',
    "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{_PML}" xmlns:a="{_DML}" xmlns:r="{_REL}"><p:cSld><p:spTree>'
    + _SP_TREE_PREAMBLE
    + '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="685800" y="457200"/><a:ext cx="7772400" cy="1143000"/></a:xfrm></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Quarterly review</a:t></a:r></a:p></p:txBody></p:sp>'
    '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="Chart 3"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
    '<p:xfrm><a:off x="1143000" y="1600200"/><a:ext cx="6858000" cy="4114800"/></p:xfrm>'
    f'<a:graphic><a:graphicData uri="{_CHART}"><c:chart xmlns:c="{_CHART}" r:id="rId2"/></a:graphicData></a:graphic>'
    '</p:graphicFrame></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>',
    "ppt/slides/_rels/slide1.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId2" Type="{_REL}/chart" Target="../charts/chart1.xml"/></Relationships>',
    "ppt/charts/chart1.xml": CHART_PART,
    "ppt/charts/_rels/chart1.xml.rels": f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" Type="{_REL}/package" Target="../embeddings/Microsoft_Excel_Worksheet.xlsx"/></Relationships>',
    "docProps/app.xml": "<Properties>Keep every byte</Properties>",
}, ("ppt/slides/slide1.xml",))


def chart_cache_values(package: bytes, part: str, tag: str, container: str = "val") -> list[str]:
    """The points of one series block's cache of a chart part, in idx order."""
    with zipfile.ZipFile(io.BytesIO(package)) as document:
        root = ET.fromstring(document.read(part))
    cache = root.find(f".//{{{_CHART}}}{container}//{{{_CHART}}}{tag}")
    require(cache is not None, f"the chart part has no {tag}")
    count = cache.find(f"{{{_CHART}}}ptCount")
    points = {int(point.get("idx")): (point.find(f"{{{_CHART}}}v").text or "") for point in cache.findall(f"{{{_CHART}}}pt")}
    require(count is not None and int(count.get("val")) == len(points), "the cache ptCount does not match its points")
    return [points[index] for index in sorted(points)]


def embedded_workbook_values(package: bytes, refs: list[str], part: str = CHART_WORKBOOK) -> list[str]:
    """The stored values of named cells of the chart's embedded workbook, read straight from its XML."""
    with zipfile.ZipFile(io.BytesIO(package)) as document:
        book = document.read(part)
    with zipfile.ZipFile(io.BytesIO(book)) as workbook:
        sheet = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    values = {}
    for cell in sheet.findall(f".//{{{_SML}}}c"):
        literal = cell.find(f"{{{_SML}}}v")
        inline = cell.find(f"{{{_SML}}}is/{{{_SML}}}t")
        values[cell.get("r")] = (literal.text if literal is not None else inline.text if inline is not None else "")
    return [values.get(ref, "") for ref in refs]


def slide_shape_ids(package: zipfile.ZipFile, part: str) -> list[int]:
    """The `p:cNvPr` id of every shape of one slide's shape tree, in document order.

    A shape tree opens with the `p:nvGrpSpPr`/`p:grpSpPr` preamble the schema requires, which
    describes the tree itself rather than a shape the slide draws, so its id is not listed.
    """
    tree = ET.fromstring(package.read(part)).find(f".//{{{_PML}}}spTree")
    require(tree is not None, "the slide has no shape tree")
    preamble = (f"{{{_PML}}}nvGrpSpPr", f"{{{_PML}}}grpSpPr")
    ids = []
    for child in tree:
        if child.tag in preamble:
            continue
        properties = child.find(f".//{{{_PML}}}cNvPr")
        if properties is not None:
            ids.append(int(properties.get("id")))
    return ids


def deck_slide_parts(package: zipfile.ZipFile) -> list[str]:
    """Slide parts in presentation order, resolved through the presentation relationships."""
    targets = {}
    for relationship in ET.fromstring(package.read("ppt/_rels/presentation.xml.rels")):
        if relationship.get("Type") == f"{_REL}/slide":
            targets[relationship.get("Id")] = "ppt/" + relationship.get("Target").lstrip("/")
    order = []
    for entry in ET.fromstring(package.read("ppt/presentation.xml")).findall(f".//{{{_PML}}}sldId"):
        identifier = entry.get(f"{{{_REL}}}id")
        require(identifier in targets, "a slide entry does not resolve to a slide relationship")
        order.append(targets[identifier])
    return order


def relationship_targets(package: zipfile.ZipFile, part: str) -> dict[str, str]:
    """Relationship type to target for one part, read from its own `_rels` member."""
    folder, name = part.rsplit("/", 1)
    return {r.get("Type"): r.get("Target") for r in ET.fromstring(package.read(f"{folder}/_rels/{name}.rels"))}


def content_type_overrides(package: zipfile.ZipFile) -> set[str]:
    return {entry.get("PartName") for entry in ET.fromstring(package.read("[Content_Types].xml")) if entry.tag.endswith("Override")}


def paragraph_texts(package: zipfile.ZipFile, part: str, namespace: str) -> list[str]:
    return ["".join(node.itertext()) for node in ET.fromstring(package.read(part)).findall(f".//{{{namespace}}}p")]


def paragraph_styles(package: zipfile.ZipFile) -> list[str | None]:
    """The style id of each top-level body paragraph of a DOCX, in document order."""
    body = ET.fromstring(package.read("word/document.xml")).find(f"{{{_WML}}}body")
    require(body is not None, "the document has no body")
    styles: list[str | None] = []
    for node in body.findall(f"{{{_WML}}}p"):
        style = node.find(f"{{{_WML}}}pPr/{{{_WML}}}pStyle")
        styles.append(None if style is None else style.get(f"{{{_WML}}}val"))
    return styles


def sheet_names(package: zipfile.ZipFile) -> list[str]:
    """The worksheet names a workbook declares, in workbook order."""
    root = ET.fromstring(package.read("xl/workbook.xml"))
    return [node.get("name") for node in root.findall(f"{{{_SML}}}sheets/{{{_SML}}}sheet")]


def created_worksheet_cells(package: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Every stored cell of the first worksheet as (reference, text), in document order."""
    root = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
    found: list[tuple[str, str]] = []
    for row in root.findall(f"{{{_SML}}}sheetData/{{{_SML}}}row"):
        for cell in row.findall(f"{{{_SML}}}c"):
            inline = cell.find(f"{{{_SML}}}is")
            if inline is not None:
                found.append((cell.get("r"), "".join(inline.itertext())))
                continue
            value = cell.find(f"{{{_SML}}}v")
            if value is not None:
                found.append((cell.get("r"), value.text or ""))
    return found


def table_row_texts(package: zipfile.ZipFile) -> list[list[str]]:
    """Cell texts of the first top-level table of a DOCX main part, row by row."""
    body = ET.fromstring(package.read("word/document.xml")).find(f"{{{_WML}}}body")
    require(body is not None, "the document has no body")
    table = body.find(f"{{{_WML}}}tbl")
    require(table is not None, "the document has no top-level table")
    return [
        ["".join(node.text or "" for node in cell.iter(f"{{{_WML}}}t")) for cell in row.findall(f"{{{_WML}}}tc")]
        for row in table.findall(f"{{{_WML}}}tr")
    ]


def run_property_coverage(paragraph: ET.Element, marker: str) -> str:
    """The text of the runs whose own properties carry `marker`, in document order."""
    covered = []
    for run in paragraph.findall(f"{{{_WML}}}r"):
        properties = run.find(f"{{{_WML}}}rPr")
        if properties is not None and properties.find(f"{{{_WML}}}{marker}") is not None:
            covered.append("".join(node.text or "" for node in run.iter(f"{{{_WML}}}t")))
    return "".join(covered)


def cell_style_index(sheet: ET.Element, ref: str) -> int:
    """The `s` index a worksheet cell points at; 0 when it carries none."""
    for cell in sheet.findall(f".//{{{_SML}}}c"):
        if cell.get("r") == ref:
            return int(cell.get("s") or "0")
    raise ValueError(f"the worksheet has no cell {ref}")


def number_format_code(styles: ET.Element, index: int) -> str | None:
    """The number format code the cell format at `index` names, or None for a builtin id."""
    formats = styles.find(f"{{{_SML}}}cellXfs")
    require(formats is not None, "the styles part has no cellXfs")
    entries = formats.findall(f"{{{_SML}}}xf")
    require(0 <= index < len(entries), "a cell points outside the cell formats")
    identifier = entries[index].get("numFmtId") or "0"
    declared = styles.find(f"{{{_SML}}}numFmts")
    if declared is None:
        return None
    for entry in declared.findall(f"{{{_SML}}}numFmt"):
        if entry.get("numFmtId") == identifier:
            return entry.get("formatCode")
    return None


def resolved_text(paragraph: ET.Element, w: str, accept: bool) -> str:
    """The paragraph text after accepting (or rejecting) its tracked changes, in document order."""
    pieces: list[str] = []

    def walk(node: ET.Element, inserted: bool) -> None:
        inserted = inserted or node.tag == f"{{{w}}}ins"
        if node.tag == f"{{{w}}}t" and (accept or not inserted):
            pieces.append(node.text or "")
        elif node.tag == f"{{{w}}}delText" and not accept:
            pieces.append(node.text or "")
        for child in node:
            walk(child, inserted)

    walk(paragraph, False)
    return "".join(pieces)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_subset(actual: object, expected: object, label: str) -> None:
    if isinstance(expected, dict):
        require(isinstance(actual, dict), f"{label}: expected an object")
        for key, value in expected.items():
            require(key in actual, f"{label}: missing {key}")
            require_subset(actual[key], value, f"{label}.{key}")
    else:
        require(type(actual) is type(expected) and actual == expected, f"{label}: unexpected result; expected {expected!r}")


LICENSE_KEYS = {"tier", "organization", "expiresAt", "keyId", "source", "verified", "note"}
TIERS = ("free", "organization-50", "organization-500", "organization-unbounded")
# The free bounds, as the build must declare them. The suite's own steps stay inside every one:
# fifteen generated files against the walk bound, three plan steps against the step bound, and
# --jobs 1 throughout, so the documented workflow runs on an unlicensed build exactly as it is.
FREE_BOUNDS = {"tier": "free", "batchSteps": 25, "folderFiles": 200, "jobs": 1, "mcpConcurrentRequests": 1}


def require_license_object(value: dict, label: str) -> None:
    """Every receipt records the licence the run was under, with nothing else changed by it.

    The tier is not pinned, because the machine running the evaluation may hold a licence and the
    workflow is identical either way. What is pinned is the shape, the internal consistency and
    the fact that the object is there at all.
    """
    license_object = value.get("license")
    require(isinstance(license_object, dict), f"{label}: the receipt carries no license object")
    require(set(license_object) == LICENSE_KEYS, f"{label}: the license object carries unexpected fields")
    require(license_object["tier"] in TIERS, f"{label}: unknown licence tier")
    require(isinstance(license_object["verified"], bool), f"{label}: verified must be a boolean")
    if not license_object["verified"]:
        require(
            license_object["organization"] is None and license_object["keyId"] is None
            and license_object["expiresAt"] is None,
            f"{label}: an unverified licence must not name an organization, a key or an expiry",
        )
        require(license_object["tier"] == "free", f"{label}: an unverified licence is the free tier")


def receipt(output: str, contract: str, version: str) -> dict:
    # Fixture/PDF helpers are also imported by the stdlib-only comparison runner.
    # Load the optional schema dependency only when evaluating UFO receipts.
    from tools.cli.validate_receipts import documents, validator_for

    parsed = list(documents(output))
    require(len(parsed) == 1 and isinstance(parsed[0][1], dict), "expected exactly one JSON receipt")
    value = parsed[0][1]
    validator = validator_for(contract, 1)
    require(validator is not None and validator.is_valid(value), f"receipt does not validate as {contract} v1")
    require(value["engineVersion"] == version, "receipt engine version does not match --expect-version")
    if contract not in ("com.krauq.ufo.capabilities", "com.krauq.ufo.license-status"):
        require_license_object(value, contract)
    return value


def read_bounded(path: Path) -> bytes:
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), "expected a regular output file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        require(stat.S_ISREG(opened.st_mode) and (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), "output changed before reading")
        value = handle.read(MAX_FILE_BYTES + 1)
    require(len(value) <= MAX_FILE_BYTES, "sample output exceeded the 4 MiB evaluation limit")
    return value


def unstamped(source: bytes, output: bytes, receipt: dict | None) -> bytes:
    """The output as this suite's byte checks read it.

    UFO stamps the Office files an edit writes with its provenance (`UFO.Version` and
    `UFO.SourceSha256`), and the receipt's `result.stamp` names every member the stamp wrote.
    Exactly those members are put back to the source's bytes here, each only when the change to
    it is a stamp and nothing more (a part the stamp added is left out), so every check below
    still holds every other member to its exact bytes, and a receipt cannot pass off another
    change as its stamp. The stamp's own claims are checked against the file first.
    """
    parts = package_semantics.receipt_stamp_parts(receipt)
    if not parts:
        return output
    with zipfile.ZipFile(io.BytesIO(source)) as first_package, zipfile.ZipFile(io.BytesIO(output)) as second_package:
        first = {info.filename: first_package.read(info) for info in first_package.infolist() if not info.is_dir()}
        order = [info.filename for info in second_package.infolist() if not info.is_dir()]
        second = {name: second_package.read(name) for name in order}
    declared = {str(key): str(value) for key, value in package_semantics.receipt_stamp_properties(receipt).items()}
    carried = {key: value for key, value in package_semantics.tool_properties(second).items() if key.startswith("UFO.")}
    require(declared == carried, "the receipt's stamp properties are not the UFO properties the output carries")
    require(declared.get("UFO.SourceSha256") == hashlib.sha256(source).hexdigest(),
            "the stamp's UFO.SourceSha256 is not the hash of the edit's source")
    admitted, _ = package_semantics.admit_stamps(first, second, parts)
    names = [name for name in order if name in admitted] + [name for name in admitted if name not in order]
    return stored_zip({name: admitted[name] for name in names})


def identity(path: Path) -> dict:
    details = path.lstat()
    digest = sha256_file(path, maximum=MAX_FILE_BYTES, expected=details)
    return {"sizeBytes": details.st_size, "sha256": digest}


def remove_container(cidfile: Path) -> None:
    if not cidfile.exists():
        return
    container_id = read_bounded(cidfile).decode("ascii").strip()
    require(re.fullmatch(r"[0-9a-f]{64}", container_id) is not None, "invalid evaluation container identity")
    # Only the exact container created by this invocation, never an image/tag.
    result = run_bounded_command(
        ["docker", "rm", "--force", container_id], cwd=REPO,
        timeout_seconds=15, label="evaluation container cleanup", max_stdout_bytes=65536, max_stderr_bytes=65536,
    )
    require(result.returncode == 0 or "No such container" in result.stderr, "could not remove the evaluation container")
    cidfile.unlink()


def validate_skill_document(text: str, capabilities: dict) -> None:
    require(0 < len(text) < 6000, "agent skill must stay below 6000 characters")
    for operation in capabilities["edit"]["operations"]:
        require(f"- `{operation['name']}`:" in text, f"agent skill omitted edit operation {operation['name']}")


def run_evaluation(args: argparse.Namespace) -> dict:
    from tools.cli.validate_receipts import validator_for

    requested = args.output.absolute()
    require(not path_exists(requested), "evaluation output already exists; choose a new directory")
    require(requested.parent.is_dir(), "evaluation output parent must already exist")
    output = requested.parent.resolve(strict=True) / requested.name
    require(not path_exists(output), "evaluation output already exists; choose a new directory")
    launcher = args.ufo.resolve(strict=True) if args.ufo else None
    artifact = launcher_artifact(launcher) if launcher else container_artifact(
        args.container_image, docker_image_id(args.container_image, cwd=REPO),
    )
    with tempfile.TemporaryDirectory(prefix=".ufo-evaluation-", dir=output.parent) as raw:
        staging = Path(raw)
        inputs, outputs, receipts = (staging / name for name in ("input", "output", "receipts"))
        for directory in (inputs, outputs, receipts):
            directory.mkdir(mode=0o700)
        for name, content in sample_files().items():
            write_private_file(inputs / name, content)
        source_identities = {name: identity(inputs / name) for name in sample_files()}
        measurements = []
        input_root = str(inputs) if launcher else "/corpus"
        output_root = str(outputs) if launcher else "/output"
        source = f"{input_root}/report.docx"
        cleaned = f"{output_root}/clean.docx"

        def run(label: str, arguments: list[str], contract: str | None, expected_exit: int = 0) -> dict | str:
            cidfile = staging / f"{label}.cid"
            if launcher:
                command = [str(launcher), *arguments]
            else:
                command = [
                    *restricted_docker_prefix(cidfile, inputs, outputs, output_writable=arguments[0] in ("text", "find", "clean", "edit", "combine", "create", "render", "convert", "batch", "report", "sample", "compare")),
                    "--pull=never", artifact["imageId"], *arguments,
                ]
            started = time.monotonic_ns()
            try:
                process = run_bounded_command(
                    command, cwd=REPO, timeout_seconds=120, label=f"sample {label}",
                    max_stdout_bytes=1024 * 1024, max_stderr_bytes=65536,
                )
            finally:
                if not launcher:
                    remove_container(cidfile)
            elapsed = (time.monotonic_ns() - started) // 1_000_000
            require(process.returncode == expected_exit, f"{label}: expected exit {expected_exit}, got {process.returncode}")
            require(not process.stderr, f"{label}: unexpected stderr")
            value = receipt(process.stdout, contract, args.expect_version) if contract else process.stdout
            if label in EXPECTED:
                require_subset(value, EXPECTED[label], label)
            suffix = "json" if contract else "txt"
            write_private_file(receipts / f"{label}.{suffix}", process.stdout.encode("utf-8"))
            measurements.append({"step": label, "exitCode": process.returncode, "elapsedMillis": elapsed})
            return value

        capabilities = run("capabilities", ["capabilities"], "com.krauq.ufo.capabilities")
        require_subset(capabilities, {"surface": {"network": "none", "sourceModification": "never", "outputReplacement": "never"}}, "capabilities")
        # Enforcement is on scale, never on fidelity, and the build says so in the one place a
        # pipeline reads. The free row is fixed whatever licence this machine happens to hold.
        require_subset(capabilities, {"license": {"enforcement": "scale", "outputsMarked": False, "network": "none"}}, "capabilities.license")
        bounds_rows = capabilities["license"]["bounds"]
        require([row["tier"] for row in bounds_rows] == list(TIERS), "capabilities does not declare the bounds for every tier")
        require_subset(bounds_rows[0], FREE_BOUNDS, "capabilities.license.bounds.free")
        skill = run("skill-print", ["skill", "print", "--agent", "generic"], None)
        validate_skill_document(skill, capabilities)
        doctor = run("doctor", ["doctor"], "com.krauq.ufo.doctor-report")
        require(doctor["status"] == "passed", "installation self-check failed; inspect ufo doctor before evaluating documents")

        # ---- license: what this installation is licensed for, verified locally, offline ----
        license_status = run("license-status", ["license", "status"], "com.krauq.ufo.license-status")
        require_subset(license_status, {"action": "status", "network": "not_used", "verification": {"clock": "local"}}, "license-status")
        require_license_object(license_status, "license-status")
        require(
            license_status["bounds"]["tier"] == license_status["license"]["tier"],
            "the bounds reported are not the bounds of the tier reported",
        )
        if license_status["verification"]["origin"] == "none":
            require_subset(license_status, {"status": "completed", "license": {"tier": "free", "verified": False}, "bounds": FREE_BOUNDS}, "license-status")
            require(license_status["detail"] is None, "no licence applied, so there is no record to detail")

        # ---- report: the whole input folder as one document, joined from the same inspections ----
        # This runs before any step writes into the input directory, so the folder the report
        # describes is exactly the generated sample and every row has an independent oracle here.
        # The suite runs unlicensed on purpose, so every step stays inside the free bounds: the
        # generated case is fifteen files against a two hundred file walk, and --jobs stays at one.
        folder = run("report", ["report", "-o", f"{output_root}/folder.json", "--jobs", "1", "--", input_root], "com.krauq.ufo.folder-report")
        require_subset(folder["output"], {**identity(outputs / "folder.json"), "format": "json"}, "report.output")
        walked = {row["path"].rsplit("/", 1)[-1]: row for row in folder["files"]}
        require(set(walked) == set(source_identities), "the folder report did not walk exactly the generated sample files")
        for name, row in walked.items():
            require_subset(row, {**source_identities[name], "name": name, "status": "completed"}, "report.files." + name)
            require(row["nameAndBytes"] != "mismatch", f"report.files.{name}: a generated sample must not read as renamed")
        require_subset(folder["totals"], {
            "filesWalked": len(source_identities), "filesInspected": len(source_identities),
            "bytesWalked": sum(value["sizeBytes"] for value in source_identities.values()),
            "bytesInspected": sum(value["sizeBytes"] for value in source_identities.values()),
        }, "report.totals")
        require(
            AUTHOR in [row["value"] for row in folder["entities"] if row["kind"] == "person"],
            "the folder report did not name the planted author the per-file reports already carry",
        )
        require(
            {"has_author", "has_properties"} <= set(walked["report.docx"]["flags"]),
            "the folder report's file table lost a flag the per-file inspection reported",
        )
        require(
            all(gap["path"] is None or gap["path"].rsplit("/", 1)[-1] in source_identities for gap in folder["notInspected"]),
            "the folder report declared a gap about something it did not walk",
        )
        published_report = json.loads(read_bounded(outputs / "folder.json"))
        require(published_report["output"] is None, "the published report named its own bytes")
        require(
            {key: value for key, value in published_report.items() if key not in ("output", "durationMillis")}
            == {key: value for key, value in folder.items() if key not in ("output", "durationMillis")},
            "the published report is not the document the run printed",
        )

        # ---- find: one needle across the whole folder, and the filter that must not lose a hit ----
        # Also before anything writes into the input directory, for the same reason. The oracle is
        # the property itself: the cheap pre-filter is on by default and off with --no-prefilter,
        # and the two runs have to report the same hits at the same coordinates, so a saved read is
        # never a missed answer.
        from tools.cli.validate_receipts import validator_for as contract_validator

        one_file_find = contract_validator("com.krauq.ufo.find-results", 1)
        require(one_file_find is not None, "find-results schema is missing")
        needle = ["find", "--text", "client", "--ignore-case", "--jobs", "1"]
        found = run("find-folder", [*needle, "-o", f"{output_root}/found.json", "--", input_root], "com.krauq.ufo.find-folder-results")
        require_subset(found["output"], {**identity(outputs / "found.json"), "format": "json"}, "find-folder.output")
        require(found["totals"]["bytesWalked"] == sum(value["sizeBytes"] for value in source_identities.values()), "the folder search walked different bytes than the runner wrote")
        rows = {row["path"].rsplit("/", 1)[-1]: row for row in found["files"]}
        require(set(rows) == set(source_identities), "the folder search did not walk exactly the generated sample files")
        for name, row in rows.items():
            require_subset(row, {"name": name, "sizeBytes": source_identities[name]["sizeBytes"]}, "find-folder.files." + name)
            if row["status"] == "searched":
                # Every row is the single-file contract, liftable as it stands, about these bytes.
                one_file_find.validate(row["results"])
                require(row["results"]["source"]["sha256"] == source_identities[name]["sha256"], f"find-folder.files.{name}: the read answered about different bytes")
            else:
                require(row["results"] is None, f"find-folder.files.{name}: a file that was never read carries results")
        filtered_hits = {name: (row["results"] or {}).get("hits", []) for name, row in rows.items()}
        whole = run("find-folder-unfiltered", [*needle, "--no-prefilter", "-o", f"{output_root}/found-unfiltered.json", "--", input_root], "com.krauq.ufo.find-folder-results")
        whole_rows = {row["path"].rsplit("/", 1)[-1]: row for row in whole["files"]}
        require(
            {name: (row["results"] or {}).get("hits", []) for name, row in whole_rows.items()} == filtered_hits,
            "the pre-filter changed what the folder search found",
        )
        require(whole["totals"]["filesRejected"] == 0, "--no-prefilter still rejected a file without reading it")
        require(whole["totals"]["filesSearched"] > found["totals"]["filesSearched"], "the pre-filter saved no structured read at all")
        require(whole["totals"]["matches"] == found["totals"]["matches"], "the two runs disagree about how many matches the folder holds")
        published_find = json.loads(read_bounded(outputs / "found.json"))
        require(published_find["output"] is None, "the published folder search named its own bytes")
        require(
            {key: value for key, value in published_find.items() if key not in ("output", "durationMillis")}
            == {key: value for key, value in found.items() if key not in ("output", "durationMillis")},
            "the published folder search is not the document the run printed",
        )

        # ---- sample: the synthetic case one command writes for a reader who has no corpus ----
        # The receipt names every published file; each one is re-hashed here, and the manifest the
        # case publishes about itself has to describe exactly the files that were written.
        case_root = outputs / "sample-case"
        case = run("sample", ["sample", "-o", f"{output_root}/sample-case"], "com.krauq.ufo.sample-receipt")
        published_case = {row["path"]: row for row in case["output"]["files"]}
        require(len(published_case) == case["output"]["fileCount"], "the sample receipt disagrees with its own file count")
        for name, row in published_case.items():
            require_subset(row, {**identity(case_root / name), "path": name}, "sample.files." + name)
        require(
            case["output"]["sizeBytes"] == sum(row["sizeBytes"] for row in published_case.values()),
            "the sample receipt's byte total is not the sum of the files it names",
        )
        planted = json.loads(read_bounded(case_root / "MANIFEST.json"))
        require(
            [row["path"] for row in planted["files"]] + ["MANIFEST.json"] == list(published_case),
            "the sample manifest does not describe exactly the files the receipt published",
        )
        require(
            all(row["flags"] and row["findings"] and row["planted"] for row in planted["files"]),
            "a sample manifest row promises nothing about its file",
        )

        # ---- compare: two files, one answer, over the two contracts the case just published ----
        # Every claim here has an oracle beside it: the member hashes are re-computed from the two
        # packages, the changed paragraph has to be one the structured read addresses, and the
        # identical run is the same bytes under another name.
        contracts = case_root / "contracts"
        first = f"{output_root}/sample-case/contracts/Q3-services-agreement-v2.docx"
        second = f"{output_root}/sample-case/contracts/Q3-services-agreement-v3.docx"
        compared = run(
            "compare",
            ["compare", "--format", "markdown", "-o", f"{output_root}/contracts.md", "--", first, second],
            "com.krauq.ufo.compare-report",
        )
        require_subset(compared["output"], {**identity(outputs / "contracts.md"), "format": "markdown"}, "compare.output")
        require_subset(compared["a"], identity(contracts / "Q3-services-agreement-v2.docx"), "compare.a")
        require_subset(compared["b"], identity(contracts / "Q3-services-agreement-v3.docx"), "compare.b")
        members = compared["bytes"]["members"]
        require(members is not None and members["complete"] is True, "the package member diff is missing or incomplete")
        shared = set(members["identicalNames"]) | {row["path"] for row in members["changed"]}
        for label, side, name in (("a", "onlyInA", "Q3-services-agreement-v2.docx"), ("b", "onlyInB", "Q3-services-agreement-v3.docx")):
            with zipfile.ZipFile(io.BytesIO(read_bounded(contracts / name))) as package:
                own = {entry.filename for entry in package.infolist() if not entry.is_dir()}
                require(
                    shared | {row["path"] for row in members[side]} == own,
                    f"the member diff did not account for exactly the entries of side {label}",
                )
                for row in members["changed"]:
                    require(
                        hashlib.sha256(package.read(row["path"])).hexdigest() == row[label]["sha256"],
                        f"compare named a hash for {row['path']} on side {label} that is not the entry's own",
                    )
        require(members["changed"] and not members["onlyInA"], "the later draft should change members the earlier one already carried")
        require(members["counts"]["unproven"] == 0, "a member pair was left unproven in a package both sides carry in full")
        unit_diff = compared["text"]
        require(unit_diff["changed"] + unit_diff["added"] + unit_diff["removed"] > 0, "compare found no paragraph difference between the two drafts")
        require(
            all(row["kind"] == "paragraph" and row["story"] == "main" and isinstance(row["index"], int) for row in unit_diff["changes"]),
            "a compare change does not carry the coordinate a paragraph read addresses",
        )
        require(
            all(row["a"] is None or len(row["a"]["text"]) <= 160 for row in unit_diff["changes"]),
            "a compare change carried more text than its fixed width",
        )
        published_compare = read_bounded(outputs / "contracts.md")
        require(published_compare.startswith(b"# UFO file comparison"), "the published comparison is not the Markdown document")
        # Nothing read out of a file is rendered as Markdown: every value is inside a code span.
        require(b"\n# " not in published_compare[len(b"# UFO file comparison"):].replace(b"\n## ", b"\n").replace(b"\n### ", b"\n"), "the published comparison let a file value open a heading")

        # The same bytes under another name: identical, and no unit diff is attempted.
        twin = outputs / "twin.docx"
        write_private_file(twin, read_bounded(contracts / "Q3-services-agreement-v2.docx"))
        same = run("compare-identical", ["compare", "--", first, f"{output_root}/twin.docx"], "com.krauq.ufo.compare-report")
        require(same["a"]["sha256"] == same["b"]["sha256"], "two copies of one file did not hash alike")
        require(same["output"] is None, "a comparison with no -o published a file")

        def document_step(label: str, arguments: list[str], contract: str, source_path: Path, expected_exit: int = 0) -> dict:
            value = run(label, arguments, f"com.krauq.ufo.{contract}", expected_exit)
            require_subset(value["source"], {**identity(source_path), "stableDuringProcessing": True}, label + ".source")
            return value

        document_step("intake", ["intake", source], "intake-receipt", inputs / "report.docx")
        before = document_step("inspect-before", ["inspect", "--deep", source], "inspection-report", inputs / "report.docx")
        require({"has_author", "has_properties"} <= set(before["flags"]), "inspection did not find the planted document properties")
        text_before = document_step("text-before", ["text", "-o", f"{output_root}/before.txt", source], "text-receipt", inputs / "report.docx")
        clean = document_step("clean", ["clean", "-o", cleaned, source], "clean-receipt", inputs / "report.docx")
        after = document_step("inspect-after", ["inspect", "--deep", cleaned], "inspection-report", outputs / "clean.docx")
        require(not ({"has_author", "has_properties"} & set(after["flags"])), "cleaned copy still reports the planted properties")
        text_after = document_step("text-after", ["text", "-o", f"{output_root}/after.txt", cleaned], "text-receipt", outputs / "clean.docx")
        for value, name in ((text_before, "before.txt"), (text_after, "after.txt"), (clean, "clean.docx")):
            require_subset(value["output"], identity(outputs / name), name + ".output")
        before_text, after_text = (read_bounded(outputs / name) for name in ("before.txt", "after.txt"))
        require(before_text == after_text, "text changed during metadata cleaning")

        # ---- reading a scan: pixels with no text layer, recognized offline ----
        # What is asserted depends on what this build carries, and the matrix it
        # printed a moment ago is what decides. A build with the recognizer has
        # to come back with the exact words that were drawn, labelled as
        # recognized; a build without one has to refuse and say so. Both run the
        # same two commands, so the step count does not depend on the surface.
        scan = f"{input_root}/scan.png"
        recognition = capabilities["limits"]["textRecognition"]
        require(
            recognition["available"] == capabilities["surface"]["textRecognition"],
            "the matrix disagrees with itself about whether this build recognizes text",
        )
        if recognition["available"]:
            read = document_step(
                "text-scan",
                ["text", "--ocr-language", recognition["defaultLanguage"], "-o", f"{output_root}/scan.txt", scan],
                "text-receipt", inputs / "scan.png",
            )
            require(read["result"]["method"] == "ocr-text", "recognized text was reported as a text layer")
            require_subset(read["output"], identity(outputs / "scan.txt"), "text-scan.output")
            recognized = read_bounded(outputs / "scan.txt").decode("utf-8")
            require(SCAN_WORDS in recognized, f"the recognizer did not read the drawn words: {recognized[:120]!r}")
            reading = read["result"]["ocr"]
            require(reading["engine"].startswith("tesseract "), "the receipt does not name the engine that read the pixels")
            require(reading["language"] == recognition["defaultLanguage"], "the receipt does not name the language that was asked for")
            require(reading["pages"] == [1] and reading["pagesAvailable"] == 1, "an image is one page and the receipt must say so")
            require(reading["truncated"] is False, "a one-page image cannot be truncated by the page bound")
            require(reading["dpi"] is None, "an image is read at its own resolution, so no density was chosen")
            require(reading["approximate"] is True and reading["notice"] == recognition["notice"],
                    "the receipt does not carry the statement that recognized text is approximate")
            require(isinstance(reading["meanConfidence"], int) and reading["meanConfidence"] >= 60,
                    f"the recognizer read the drawn words at only {reading['meanConfidence']} confidence")
            # The same pixels, asked the other question a pipeline asks: where do
            # the words sit. A recognized hit is a coordinate for reading, so this
            # requires it to say so rather than offer something an edit could use.
            located = document_step(
                "find-scan",
                ["find", "--text", SCAN_NEEDLE, "-o", f"{output_root}/find-scan.json", scan],
                "text-receipt", inputs / "scan.png",
            )
            require(located["result"]["method"] == "find-results", "find published something other than a coordinate list")
            require_subset(located["output"], identity(outputs / "find-scan.json"), "find-scan.output")
            scan_hits = json.loads(read_bounded(outputs / "find-scan.json"))
            one_file_find.validate(scan_hits)
            require_subset(scan_hits, {
                "source": {"name": "scan.png"},
                "needle": {"text": SCAN_NEEDLE, "ignoreCase": False, "regex": False},
                "total": 1, "truncated": False,
            }, "find-scan")
            hit = scan_hits["hits"][0]
            require_subset(hit, {
                "kind": "recognized", "page": 1, "line": 1, "length": len(SCAN_NEEDLE), "editable": False,
            }, "find-scan.hits.0")
            require(SCAN_NEEDLE in hit["snippet"], f"the hit's snippet does not carry the needle: {hit['snippet']!r}")
            require(hit["confidence"] >= 60, f"the recognizer located the words at only {hit['confidence']} confidence")
            require(scan_hits["ocr"]["engine"].startswith("tesseract "), "the search does not name the engine that read the pixels")
            require(scan_hits["ocr"]["notice"] == recognition["notice"],
                    "the search does not carry the statement that recognized text is approximate")
            # And the structured read of the same pixels: the lines the engine saw, addressable
            # by the same page and line the search reported, and labelled approximate.
            structured = document_step(
                "text-scan-structure",
                ["text", "--format", "structure", "-o", f"{output_root}/scan-structure.json", scan],
                "text-receipt", inputs / "scan.png",
            )
            require(structured["result"]["method"] == "recognized-pages",
                    "a structured read of pixels was reported as a text layer")
            require_subset(structured["output"], identity(outputs / "scan-structure.json"), "text-scan-structure.output")
            pages = json.loads(read_bounded(outputs / "scan-structure.json"))
            recognized_validator = contract_validator("com.krauq.ufo.recognized-pages", 1)
            require(recognized_validator is not None, "recognized-pages schema is missing")
            recognized_validator.validate(pages)
            require_subset(pages, {
                "source": {"name": "scan.png"},
                "pageCount": 1,
                "partial": False,
            }, "text-scan-structure")
            page = pages["pages"][0]
            require(page["index"] == 1 and page["truncated"] is False, "a picture is one page and it was not cut")
            require(page["confidence"] >= 60, f"the recognizer read the page at only {page['confidence']} confidence")
            drawn = [line for line in page["lines"] if SCAN_WORDS in line["text"]]
            require(len(drawn) == 1, f"the structured read did not carry the drawn line: {page['lines']}")
            require(drawn[0]["line"] == hit["line"], "the structured read and the search disagree about the line")
            require(drawn[0]["chars"] == len(drawn[0]["text"]), "a line's character count is not its text")
            require(pages["ocr"]["notice"] == recognition["notice"],
                    "the structured read does not carry the statement that recognized text is approximate")
        else:
            unavailable = document_step(
                "text-scan", ["text", "-o", f"{output_root}/scan.txt", scan],
                "text-receipt", inputs / "scan.png", 1,
            )
            require(unavailable["code"] == "parser_refused", "a build without a recognizer must refuse, not fail")
            require(unavailable["output"] is None, "a refused read published something")
            require(not path_exists(outputs / "scan.txt"), "a refused read left a file behind")
            missing = document_step(
                "find-scan",
                ["find", "--text", SCAN_NEEDLE, "-o", f"{output_root}/find-scan.json", scan],
                "text-receipt", inputs / "scan.png", 1,
            )
            require(missing["code"] == "parser_refused", "a build without a recognizer must refuse to search pixels")
            require(missing["output"] is None and not path_exists(outputs / "find-scan.json"),
                    "a refused search published something")
            absent = document_step(
                "text-scan-structure",
                ["text", "--format", "structure", "-o", f"{output_root}/scan-structure.json", scan],
                "text-receipt", inputs / "scan.png", 1,
            )
            require(absent["code"] == "parser_refused", "a build without a recognizer must refuse a structured read of pixels")
            require(absent["output"] is None and not path_exists(outputs / "scan-structure.json"),
                    "a refused structured read published something")
        # Recognition off is a refusal on an image whichever build this is:
        # there is no text layer to fall back to.
        declined = document_step(
            "text-scan-no-ocr", ["text", "--no-ocr", "-o", f"{output_root}/scan-off.txt", scan],
            "text-receipt", inputs / "scan.png", 1,
        )
        require(declined["code"] == "parser_refused", "--no-ocr on an image must refuse with the reader's own reason")
        require(declined["output"] is None and not path_exists(outputs / "scan-off.txt"),
                "a refused read published something")
        require(before_text.decode("utf-8").strip() == SAMPLE_TEXT, "extracted text did not match the planted paragraphs")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original:
            original_body = original.read("word/document.xml")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "clean.docx"))) as copy:
            body = copy.getinfo("word/document.xml")
            require(body.file_size <= MAX_FILE_BYTES, "cleaned document part exceeded evaluation limit")
            require(copy.read(body) == original_body, "untouched document XML was not byte-identical")
        clean_identity = identity(outputs / "clean.docx")
        require(clean_identity == EXPECTED_CLEAN_OUTPUT, "cleaned bytes differ from the pinned cross-surface sample result")
        document_step("no-replace", ["clean", "-o", cleaned, source], "clean-receipt", inputs / "report.docx", 1)
        require(identity(outputs / "clean.docx") == clean_identity, "existing output changed during refusal")
        document_step("unsupported-clean", ["clean", "-o", f"{output_root}/unsupported.txt", f"{input_root}/readme.txt"], "clean-receipt", inputs / "readme.txt", 1)
        require(not path_exists(outputs / "unsupported.txt"), "unsupported operation published an output")
        source_hash = source_identities["report.docx"]["sha256"]
        paragraph_validator = validator_for("com.krauq.ufo.docx-paragraphs", 1)
        require(paragraph_validator is not None, "paragraph schema is missing")
        for offset, line in enumerate(SAMPLE_TEXT.split("\n")):
            name = f"paragraph-{offset}.json"
            page_receipt = document_step(f"paragraph-read-{offset}", [
                "text", "--format", "structure", "--offset", str(offset), "--limit", "1",
                "--expect-sha256", source_hash, "-o", f"{output_root}/{name}", source,
            ], "text-receipt", inputs / "report.docx")
            require_subset(page_receipt["output"], identity(outputs / name), name)
            page = json.loads(read_bounded(outputs / name))
            paragraph_validator.validate(page)
            require_subset(page, {
                "source": {"sha256": source_hash}, "offset": offset, "totalParagraphs": 2,
                "nextOffset": 1 if offset == 0 else None, "partial": False,
                "paragraphs": [{
                    "index": offset, "text": line, "inTable": False, "table": None,
                    "style": None, "limitation": None, "links": [],
                }],
            }, name)
        edited = f"{output_root}/edited.docx"
        originals = SAMPLE_TEXT.split("\n")
        replacements = ["UFO updated client report \U0001f680.", "Both paragraphs changed in one output copy."]
        batch = ["edit", "paragraphs", "--expect-sha256", source_hash]
        for index, (before_line, after_line) in enumerate(zip(originals, replacements)):
            batch.extend(["--expect", f"{index}={before_line}", "--set", f"{index}={after_line}"])
        # ---- the same batch planned first: the whole run, nothing published, a hash to compare ----
        planned = document_step("paragraph-plan", [*batch, "--dry-run", "-o", edited, source], "edit-receipt", inputs / "report.docx")
        require_subset(planned, {
            "status": "planned", "output": None, "code": None, "message": None,
            "result": {"engine": "docx-paragraphs"},
        }, "paragraph-plan")
        require(not path_exists(outputs / "edited.docx"), "a dry run published an output")
        plan = planned["plan"]
        require(isinstance(plan, dict) and set(plan) == {"outputSha256", "outputSizeBytes"}, "the dry run reported no plan")
        edit = document_step("paragraph-batch", [*batch, "-o", edited, source], "edit-receipt", inputs / "report.docx")
        require_subset(edit, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "paragraph-batch")
        require(edit["plan"] is None, "a published edit reported a plan")
        edited_identity = identity(outputs / "edited.docx")
        # The oracle for a plan: the real run's own bytes, hashed here, not by UFO.
        require(
            plan["outputSha256"] == edited_identity["sha256"] and plan["outputSizeBytes"] == edited_identity["sizeBytes"],
            "the real batch did not produce the bytes the dry run planned",
        )
        require(planned["result"] == edit["result"], "the dry run and the real run reported different results")
        require_subset(edit["output"], edited_identity, "paragraph-batch.output")
        # An independent ZIP/XML oracle checks the output without asking UFO to
        # confirm its own edit. Untouched package members must retain exact bytes.
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "edited.docx"), edit))) as changed:
            require(original.namelist() == changed.namelist(), "paragraph edit changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "paragraph edit changed an untouched package part")
            body = changed.getinfo("word/document.xml")
            require(body.file_size <= MAX_FILE_BYTES, "edited main part exceeded the evaluation limit")
            root = ET.fromstring(changed.read(body))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            observed = ["".join(p.itertext()) for p in root.findall(".//w:p", ns)]
            require(observed == replacements, "paragraph batch did not make exactly the expected text changes")
        refused = document_step("paragraph-no-replace", [*batch, "-o", edited, source], "edit-receipt", inputs / "report.docx", 1)
        require_subset(refused, {"status": "refused", "code": "destination_exists", "output": None}, "paragraph-no-replace")
        require(identity(outputs / "edited.docx") == edited_identity, "paragraph refusal replaced an existing output")
        for label, bad_batch in (
            ("paragraph-stale", ["0" * 64 if arg == source_hash else arg for arg in batch]),
            ("paragraph-preimage", ["1=Wrong expected text" if arg == f"1={originals[1]}" else arg for arg in batch]),
        ):
            refused = document_step(label, [*bad_batch, "-o", f"{output_root}/{label}.docx", source], "edit-receipt", inputs / "report.docx", 1)
            require_subset(refused, {"status": "refused", "output": None}, label)
            require(not path_exists(outputs / f"{label}.docx"), f"{label}: refused batch published an output")
        # ---- DOCX: one guarded batch that inserts a whole paragraph and deletes another ----
        inserted_line = "Inserted by the guarded structure batch."
        structure_batch = [
            "edit", "paragraphs", "--expect-sha256", source_hash,
            "--insert-after", f"1={inserted_line}", "--expect", f"0={originals[0]}", "--delete", "0",
        ]
        structured = document_step("paragraph-structure", [*structure_batch, "-o", f"{output_root}/paragraph-structure.docx", source], "edit-receipt", inputs / "report.docx")
        require_subset(structured, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "paragraph-structure")
        require([item["kind"] for item in structured["result"]["applied"]] == ["paragraph_inserted", "paragraph_deleted"], "structure batch did not report one insert and one delete")
        require_subset(structured["output"], identity(outputs / "paragraph-structure.docx"), "paragraph-structure.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "paragraph-structure.docx"), structured))) as changed:
            require(original.namelist() == changed.namelist(), "structure batch changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "structure batch changed an untouched package part")
            structured_body = changed.getinfo("word/document.xml")
            require(structured_body.file_size <= MAX_FILE_BYTES, "restructured main part exceeded the evaluation limit")
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            restructured = ["".join(p.itertext()) for p in ET.fromstring(changed.read(structured_body)).findall(".//w:p", ns)]
            require(restructured == [originals[1], inserted_line], "the structure batch did not leave exactly the inserted layout")
        refused = document_step("paragraph-anchor-deleted", [
            "edit", "paragraphs", "--expect-sha256", source_hash, "--expect", f"1={originals[1]}", "--delete", "1",
            "--insert-after", f"1={inserted_line}", "-o", f"{output_root}/paragraph-anchor-deleted.docx", source,
        ], "edit-receipt", inputs / "report.docx", 1)
        require_subset(refused, {"status": "refused", "output": None}, "paragraph-anchor-deleted")
        require(not path_exists(outputs / "paragraph-anchor-deleted.docx"), "a batch that deletes its own insert anchor published an output")
        # ---- DOCX: a hyperlink over a character span, and a footnote at a character offset ----
        link_url = "https://example.com/brief"
        linked = document_step("link-add", [
            "edit", "paragraphs", "--expect-sha256", source_hash, "--link", f"0=0:3={link_url}",
            "-o", f"{output_root}/report-linked.docx", source,
        ], "edit-receipt", inputs / "report.docx")
        require_subset(linked, {"status": "completed", "result": {
            "engine": "docx-paragraphs", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "link_added", "count": 1, "detail": "paragraphs: 0 (0:3)"}],
        }}, "link-add")
        require_subset(linked["output"], identity(outputs / "report-linked.docx"), "link-add.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "report-linked.docx"))) as changed:
            body = ET.fromstring(changed.read("word/document.xml"))
            wrappers = body.findall(f".//{{{_WML}}}hyperlink")
            require(len(wrappers) == 1, "the link batch did not wrap exactly one span")
            require(
                "".join(node.text or "" for node in wrappers[0].iter(f"{{{_WML}}}t")) == "UFO",
                "the hyperlink does not cover exactly the named span",
            )
            relationship = wrappers[0].get(f"{{{_REL}}}id")
            require(relationship is not None, "the hyperlink carries no relationship id")
            rows = {
                row.get("Id"): (row.get("Target"), row.get("TargetMode"))
                for row in ET.fromstring(changed.read("word/_rels/document.xml.rels"))
            }
            require(rows.get(relationship) == (link_url, "External"), "the link relationship does not target the address as an external one")
            require(
                paragraph_texts(changed, "word/document.xml", _WML) == SAMPLE_TEXT.split("\n"),
                "the link batch changed a character of the document's text",
            )

        footnote_text = "Source: the client brief"
        noted = document_step("footnote-add", [
            "edit", "paragraphs", "--expect-sha256", source_hash, "--footnote", f"1=7={footnote_text}",
            "-o", f"{output_root}/report-noted.docx", source,
        ], "edit-receipt", inputs / "report.docx")
        require_subset(noted, {"status": "completed", "result": {
            "engine": "docx-paragraphs", "skipped": [], "outputExtension": "docx",
        }}, "footnote-add")
        require(
            [row["kind"] for row in noted["result"]["applied"]] == ["footnote_added"],
            "the footnote batch did not report exactly one added footnote",
        )
        require_subset(noted["output"], identity(outputs / "report-noted.docx"), "footnote-add.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "report-noted.docx"))) as changed:
            require("word/footnotes.xml" in changed.namelist(), "the footnote batch created no footnotes part")
            notes = ET.fromstring(changed.read("word/footnotes.xml"))
            real = [
                note for note in notes.findall(f"{{{_WML}}}footnote")
                if note.get(f"{{{_WML}}}type") not in ("separator", "continuationSeparator")
            ]
            require(len(real) == 1, "the footnotes part does not hold exactly one real note")
            note_id = real[0].get(f"{{{_WML}}}id")
            require(
                "".join(node.text or "" for node in real[0].iter(f"{{{_WML}}}t")).strip() == footnote_text,
                "the footnote does not hold the requested text",
            )
            body = ET.fromstring(changed.read("word/document.xml"))
            references = body.findall(f".//{{{_WML}}}footnoteReference")
            require(len(references) == 1 and references[0].get(f"{{{_WML}}}id") == note_id, "the document does not cite the new footnote once")
            require(
                paragraph_texts(changed, "word/document.xml", _WML) == SAMPLE_TEXT.split("\n"),
                "the footnote batch changed a character of the document's text",
            )
            require(
                f"/word/footnotes.xml" in content_type_overrides(changed),
                "the new footnotes part is not registered in the content types",
            )

        # ---- DOCX: the same structural batch as native tracked changes, resolved both ways ----
        redlined = "Added by the sample reviewer."
        tracked_structure = document_step("paragraph-tracked-structure", [
            "edit", "paragraphs", "--expect-sha256", source_hash, "--track", "--author", "Sample reviewer",
            "--insert-after", f"1={redlined}", "--expect", f"0={originals[0]}", "--delete", "0",
            "-o", f"{output_root}/tracked-structure.docx", source,
        ], "edit-receipt", inputs / "report.docx")
        require_subset(tracked_structure, {"status": "completed", "result": {"engine": "docx-paragraphs", "skipped": []}}, "paragraph-tracked-structure")
        require(
            [item["kind"] for item in tracked_structure["result"]["applied"]] == ["paragraph_inserted_tracked", "paragraph_deleted_tracked"],
            "the tracked structure batch did not report one tracked insert and one tracked delete",
        )
        require(
            all("Sample reviewer" in item["detail"] for item in tracked_structure["result"]["applied"]),
            "the tracked structure receipt does not name the author",
        )
        require_subset(tracked_structure["output"], identity(outputs / "tracked-structure.docx"), "paragraph-tracked-structure.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "tracked-structure.docx"), tracked_structure))) as changed:
            require(original.namelist() == changed.namelist(), "the tracked structure batch changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "the tracked structure batch changed an untouched package part")
            marked = ET.fromstring(changed.read("word/document.xml")).findall(f".//{{{_WML}}}p")
            require(len(marked) == 3, "a tracked batch removes nothing yet, so the paragraph count is the original plus the insert")
            dropped, inserted_paragraph = marked[0], marked[2]
            require(dropped.find(f"{{{_WML}}}pPr/{{{_WML}}}rPr/{{{_WML}}}del") is not None, "the deleted paragraph carries no deleted paragraph mark")
            require(dropped.findall(f".//{{{_WML}}}del") and dropped.findall(f".//{{{_WML}}}delText"), "the deleted paragraph's runs are not deleted runs with delText")
            require(inserted_paragraph.find(f"{{{_WML}}}pPr/{{{_WML}}}rPr/{{{_WML}}}ins") is not None, "the inserted paragraph carries no inserted paragraph mark")
            require(inserted_paragraph.findall(f".//{{{_WML}}}ins/{{{_WML}}}r"), "the inserted paragraph's runs are not inserted runs")
            require(
                {node.get(f"{{{_WML}}}author") for node in marked[0].iter() if node.get(f"{{{_WML}}}author")} == {"Sample reviewer"},
                "the redlines do not carry the reviewer's name",
            )
        tracked_hash = identity(outputs / "tracked-structure.docx")["sha256"]
        intended = [originals[1], redlined]
        for label, verb, name, expected_texts in (
            ("tracked-structure-accept", "accept-changes", "tracked-accepted.docx", intended),
            ("tracked-structure-reject", "reject-changes", "tracked-rejected.docx", originals),
        ):
            resolved = document_step(label, [
                "edit", verb, "-o", f"{output_root}/{name}", f"{output_root}/tracked-structure.docx",
            ], "edit-receipt", outputs / "tracked-structure.docx")
            require_subset(resolved, {"status": "completed", "result": {"engine": "docx-revisions"}}, label)
            require_subset(resolved["source"], {"sha256": tracked_hash}, label + ".source")
            with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / name))) as changed:
                require(
                    paragraph_texts(changed, "word/document.xml", _WML) == expected_texts,
                    f"{label}: resolving the redline did not give the expected paragraphs",
                )
                require(not changed.read("word/document.xml").count(b"<w:del "), f"{label}: a tracked change survived")
        # ---- DOCX: table row coordinates, a row copy filled through paragraph writes, and a row delete ----
        table_source = f"{input_root}/table.docx"
        table_hash = source_identities["table.docx"]["sha256"]
        table_read = document_step("table-read", [
            "text", "--format", "structure", "--expect-sha256", table_hash,
            "-o", f"{output_root}/table-read.json", table_source,
        ], "text-receipt", inputs / "table.docx")
        require_subset(table_read["output"], identity(outputs / "table-read.json"), "table-read.output")
        table_page = json.loads(read_bounded(outputs / "table-read.json"))
        paragraph_validator.validate(table_page)
        require_subset(table_page, {"totalParagraphs": 6, "partial": False, "paragraphs": [
            {"index": 0, "text": "Quarterly table", "inTable": False, "table": None, "style": None, "limitation": None, "links": []},
            {"index": 1, "text": "Client", "inTable": True, "table": {"table": 0, "row": 0, "cell": 0}, "style": None, "limitation": None, "links": []},
            {"index": 2, "text": "Amount", "inTable": True, "table": {"table": 0, "row": 0, "cell": 1}, "style": None, "limitation": None, "links": []},
            {"index": 3, "text": "Acme", "inTable": True, "table": {"table": 0, "row": 1, "cell": 0}, "style": None, "limitation": None, "links": []},
            {"index": 4, "text": "10", "inTable": True, "table": {"table": 0, "row": 1, "cell": 1}, "style": None, "limitation": None, "links": []},
            {"index": 5, "text": "Closing", "inTable": False, "table": None, "style": None, "limitation": None, "links": []},
        ]}, "table-read")
        row_copy = document_step("table-copy-row", [
            "edit", "tables", "--expect-sha256", table_hash, "--copy-row", "0:1",
            "-o", f"{output_root}/table-copied.docx", table_source,
        ], "edit-receipt", inputs / "table.docx")
        require_subset(row_copy, {"status": "completed", "result": {
            "engine": "docx-tables", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "table_row_copied", "count": 1, "detail": "table 0 row 1 copied to row 2 with 2 empty cells"}],
        }}, "table-copy-row")
        require_subset(row_copy["output"], identity(outputs / "table-copied.docx"), "table-copy-row.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "table.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "table.docx"), read_bounded(outputs / "table-copied.docx"), row_copy))) as changed:
            require(original.namelist() == changed.namelist(), "the row copy changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "the row copy changed an untouched package part")
            require(table_row_texts(original) == TABLE_ROWS, "the oracle does not read the source table")
            require(table_row_texts(changed) == [*TABLE_ROWS, ["", ""]], "the copied row is not an empty row of the source row's cells")
            require(
                paragraph_texts(changed, "word/document.xml", _WML) == ["Quarterly table", "Client", "Amount", "Acme", "10", "", "", "Closing"],
                "the row copy did not leave exactly two new empty paragraphs",
            )
            rows = ET.fromstring(changed.read("word/document.xml")).find(f"{{{_WML}}}body").find(f"{{{_WML}}}tbl").findall(f"{{{_WML}}}tr")
            require(len(rows[2].findall(f"{{{_WML}}}tc")) == 2, "the copied row does not carry the source row's cells")
            require(
                element_shape(rows[1].findall(f"{{{_WML}}}tc")[0].find(f"{{{_WML}}}tcPr"))
                == element_shape(rows[2].findall(f"{{{_WML}}}tc")[0].find(f"{{{_WML}}}tcPr")),
                "the copied cell does not keep the source cell's properties",
            )
            require(len(rows[2].findall(f".//{{{_WML}}}t")) == 0, "the copied row carries text nobody asked for")
        copied_hash = identity(outputs / "table-copied.docx")["sha256"]
        # The copied row's cells are empty, so they can be merged into one without losing text.
        cell_merge = document_step("table-cell-merge", [
            "edit", "tables", "--expect-sha256", copied_hash, "--merge-cells", "0:2:0-0:2:1",
            "-o", f"{output_root}/table-merged.docx", f"{output_root}/table-copied.docx",
        ], "edit-receipt", outputs / "table-copied.docx")
        require_subset(cell_merge, {"status": "completed", "result": {
            "engine": "docx-tables", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "cells_merged", "count": 1, "detail": "ranges: 0:2:0-0:2:1"}],
        }}, "table-cell-merge")
        require_subset(cell_merge["output"], identity(outputs / "table-merged.docx"), "table-cell-merge.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "table-copied.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(outputs / "table-copied.docx"), read_bounded(outputs / "table-merged.docx"), cell_merge))) as joined:
            require(original.namelist() == joined.namelist(), "the cell merge changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == joined.read(name), "the cell merge changed an untouched package part")
            merged_rows = ET.fromstring(joined.read("word/document.xml")).find(f"{{{_WML}}}body").find(f"{{{_WML}}}tbl").findall(f"{{{_WML}}}tr")
            require(len(merged_rows) == 3, "the cell merge changed the row count")
            merged_cells = merged_rows[2].findall(f"{{{_WML}}}tc")
            require(len(merged_cells) == 1, "the merged row does not hold exactly one cell")
            span = merged_cells[0].find(f"{{{_WML}}}tcPr").find(f"{{{_WML}}}gridSpan")
            require(span is not None and span.get(f"{{{_WML}}}val") == "2", "the merged cell does not span both columns")
            require(table_row_texts(joined) == [*TABLE_ROWS, [""]], "the cell merge changed the table's text")
            require(
                [len(row.findall(f"{{{_WML}}}tc")) for row in merged_rows[:2]] == [2, 2],
                "the cell merge touched a row it was not given",
            )

        fresh = document_step("table-fresh-read", [
            "text", "--format", "structure", "--expect-sha256", copied_hash,
            "-o", f"{output_root}/table-fresh.json", f"{output_root}/table-copied.docx",
        ], "text-receipt", outputs / "table-copied.docx")
        require_subset(fresh["output"], identity(outputs / "table-fresh.json"), "table-fresh-read.output")
        fresh_page = json.loads(read_bounded(outputs / "table-fresh.json"))
        paragraph_validator.validate(fresh_page)
        new_cells = [row for row in fresh_page["paragraphs"] if row["table"] == {"table": 0, "row": 2, "cell": 0} or row["table"] == {"table": 0, "row": 2, "cell": 1}]
        require([row["index"] for row in new_cells] == [5, 6], "the fresh read does not address the new row's cells")
        require(all(row["text"] == "" for row in new_cells), "the new row's cells are not empty")
        filled_values = ["Globex", "30"]
        fill = ["edit", "paragraphs", "--expect-sha256", copied_hash]
        for row, value in zip(new_cells, filled_values):
            fill.extend(["--expect", f"{row['index']}=", "--set", f"{row['index']}={value}"])
        filled = document_step("table-fill-row", [
            *fill, "-o", f"{output_root}/table-filled.docx", f"{output_root}/table-copied.docx",
        ], "edit-receipt", outputs / "table-copied.docx")
        require_subset(filled, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "table-fill-row")
        require_subset(filled["output"], identity(outputs / "table-filled.docx"), "table-fill-row.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "table-filled.docx"))) as changed:
            require(table_row_texts(changed) == [*TABLE_ROWS, filled_values], "the filled row does not carry the written cell texts")
        row_delete = document_step("table-delete-row", [
            "edit", "tables", "--expect-sha256", table_hash, "--delete-row", "0:1", "--expect-row", "0:1=Acme|10",
            "-o", f"{output_root}/table-trimmed.docx", table_source,
        ], "edit-receipt", inputs / "table.docx")
        require_subset(row_delete, {"status": "completed", "result": {
            "engine": "docx-tables", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "table_row_deleted", "count": 1, "detail": "table 0 row 1; table 0 rows now 1"}],
        }}, "table-delete-row")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "table.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "table.docx"), read_bounded(outputs / "table-trimmed.docx"), row_delete))) as changed:
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "the row delete changed an untouched package part")
            require(table_row_texts(changed) == [TABLE_ROWS[0]], "the row delete did not leave exactly the header row")
            require(
                paragraph_texts(changed, "word/document.xml", _WML) == ["Quarterly table", "Client", "Amount", "Closing"],
                "the row delete removed more or less than the row it named",
            )
        refused = document_step("table-expect-mismatch", [
            "edit", "tables", "--expect-sha256", table_hash, "--delete-row", "0:1", "--expect-row", "0:1=Acme|11",
            "-o", f"{output_root}/table-mismatch.docx", table_source,
        ], "edit-receipt", inputs / "table.docx", 1)
        require_subset(refused, {"status": "refused", "output": None}, "table-expect-mismatch")
        require("does not match the expected cell texts" in refused["message"], "the refusal does not name the expected cell texts")
        require(not path_exists(outputs / "table-mismatch.docx"), "a refused row delete published an output")

        # ---- XLSX: inventory, dense range, guarded typed cell batch, formula refusal ----
        cells_validator = validator_for("com.krauq.ufo.xlsx-cells", 1)
        require(cells_validator is not None, "xlsx-cells schema is missing")
        workbook = f"{input_root}/data.xlsx"
        workbook_hash = source_identities["data.xlsx"]["sha256"]
        inventory_receipt = document_step("cells-inventory", ["text", "--format", "structure", "-o", f"{output_root}/cells-inventory.json", workbook], "text-receipt", inputs / "data.xlsx")
        require_subset(inventory_receipt["output"], identity(outputs / "cells-inventory.json"), "cells-inventory")
        inventory = json.loads(read_bounded(outputs / "cells-inventory.json"))
        cells_validator.validate(inventory)
        require_subset(inventory, {"source": {"sha256": workbook_hash}, "selection": None, "sheets": [{"index": 0, "name": "Summary", "part": "xl/worksheets/sheet1.xml", "visibility": "visible",
                          "dimension": "A1:D1", "dimensionSource": "declared", "charts": [], "chartsComplete": True,
                          "validations": [], "validationsComplete": True,
                          "conditionalFormats": [], "conditionalFormatsComplete": True}]}, "cells-inventory")
        range_receipt = document_step("cells-range", [
            "text", "--format", "structure", "--sheet", "Summary", "--range", "A1:D1", "--expect-sha256", workbook_hash,
            "-o", f"{output_root}/cells-range.json", workbook,
        ], "text-receipt", inputs / "data.xlsx")
        require_subset(range_receipt["output"], identity(outputs / "cells-range.json"), "cells-range")
        cell_page = json.loads(read_bounded(outputs / "cells-range.json"))
        cells_validator.validate(cell_page)
        require_subset(cell_page["selection"], {"sheet": "Summary", "range": "A1:D1", "cells": [
            {"ref": "A1", "row": 1, "column": 1, "type": "string", "value": "Client A", "formula": None, "cachedValue": None, "limitation": None},
            {"ref": "B1", "row": 1, "column": 2, "type": "number", "value": "10", "formula": None, "cachedValue": None, "limitation": None},
            {"ref": "C1", "row": 1, "column": 3, "type": "boolean", "value": "TRUE", "formula": None, "cachedValue": None, "limitation": None},
            {"ref": "D1", "row": 1, "column": 4, "type": "formula", "value": None, "formula": "=B1*2", "cachedValue": "20", "limitation": None},
        ]}, "cells-range")
        cell_batch = ["edit", "cells", "--expect-sha256", workbook_hash, "--expect", "A1=string:Client A", "--set", "A1=string:Client B", "--expect", "B1=number:10", "--set", "B1=number:20"]
        cell_edit = document_step("cells-batch", [*cell_batch, "-o", f"{output_root}/edited.xlsx", workbook], "edit-receipt", inputs / "data.xlsx")
        require_subset(cell_edit, {"status": "completed", "result": {"engine": "xlsx-cells"}}, "cells-batch")
        require_subset(cell_edit["output"], identity(outputs / "edited.xlsx"), "cells-batch.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "edited.xlsx"), cell_edit))) as changed:
            require(original.namelist() == changed.namelist(), "cell edit changed the package member set/order")
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/workbook.xml"):
                    require(original.read(name) == changed.read(name), "cell edit changed an untouched package part")
            sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            ns = {"s": _SML}
            by_ref = {c.get("r"): c for c in sheet.findall(".//s:c", ns)}
            require(by_ref["A1"].get("t") == "inlineStr" and "".join(by_ref["A1"].itertext()) == "Client B", "A1 was not written as the string Client B")
            require(by_ref["B1"].get("t") is None and by_ref["B1"].find("s:v", ns).text == "20", "B1 was not written as the number 20")
            require(by_ref["D1"].find("s:f", ns).text == "B1*2", "the formula in D1 was not preserved")
            # B1 feeds D1: the shared editor either refreshes the cached result itself
            # or marks the workbook for recalculation on open. Either is honest; a
            # stale 20 presented as current is not.
            d1_cached = by_ref["D1"].find("s:v", ns)
            workbook_part = changed.read("xl/workbook.xml")
            refreshed = d1_cached is not None and d1_cached.text == "40" and workbook_part == original.read("xl/workbook.xml")
            deferred = b'fullCalcOnLoad="1"' in workbook_part
            require(refreshed or deferred, "a literal write feeding a formula must refresh its cached result or ask the host to recalculate")
        refused = document_step("cells-formula-target", ["edit", "cells", "--expect-sha256", workbook_hash, "--expect", "D1=number:20", "--set", "D1=number:1", "-o", f"{output_root}/cells-formula-target.xlsx", workbook], "edit-receipt", inputs / "data.xlsx", 1)
        require_subset(refused, {"status": "refused", "output": None}, "cells-formula-target")
        require(not path_exists(outputs / "cells-formula-target.xlsx"), "a refused cell batch published an output")

        # ---- XLSX: guarded formula writes, and the literal expectation that still refuses one ----
        formula_edit = document_step("cells-formula-write", [
            "edit", "cells", "--expect-sha256", workbook_hash,
            "--expect", "E1=empty", "--set", "E1=formula:=B1+1",
            "--expect", "D1=formula:=B1*2", "--set", "D1=formula:=B1*3",
            "-o", f"{output_root}/formulas.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(formula_edit, {"status": "completed", "result": {
            "engine": "xlsx-cells", "skipped": [], "outputExtension": "xlsx",
            "applied": [{"kind": "formula_written", "count": 2, "detail": "Summary: E1,D1"}],
        }}, "cells-formula-write")
        require_subset(formula_edit["output"], identity(outputs / "formulas.xlsx"), "cells-formula-write.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "formulas.xlsx"), formula_edit))) as changed:
            require(original.namelist() == changed.namelist(), "the formula write changed the package member set/order")
            require("xl/calcChain.xml" not in changed.namelist(), "a formula write must never leave a stale calculation chain")
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/workbook.xml"):
                    require(original.read(name) == changed.read(name), "the formula write changed an untouched package part")
            ns = {"s": _SML}
            after_sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            by_ref = {c.get("r"): c for c in after_sheet.findall(".//s:c", ns)}
            for ref, expression in (("D1", "B1*3"), ("E1", "B1+1")):
                cell = by_ref[ref]
                require(cell.find("s:f", ns) is not None and cell.find("s:f", ns).text == expression, f"{ref} does not hold the written formula")
                # No cached value and no type may survive: nothing presents a stale result as current.
                require(cell.find("s:v", ns) is None, f"{ref} kept a cached value after a formula write")
                require(cell.get("t") is None, f"{ref} kept a cell type after a formula write")
            require(worksheet_cells(after_sheet, ["Client A"])["D1"] == ("formula", "B1*3"), "the oracle does not read the rewritten formula")
            after_workbook = ET.fromstring(changed.read("xl/workbook.xml"))
            recalculation = after_workbook.findall("s:calcPr", ns)
            require(
                len(recalculation) == 1 and recalculation[0].get("fullCalcOnLoad") == "1",
                "a written formula must ask the host to recalculate once on open",
            )
            for element in recalculation:
                after_workbook.remove(element)
            require(
                element_shape(ET.fromstring(original.read("xl/workbook.xml"))) == element_shape(after_workbook),
                "the formula write changed the workbook beyond the recalculation request",
            )
        # A formula target still needs its own formula preimage; a literal expectation refuses.
        refused = document_step("cells-formula-literal-expect", [
            "edit", "cells", "--expect-sha256", workbook_hash,
            "--expect", "D1=number:20", "--set", "D1=formula:=B1*3",
            "-o", f"{output_root}/cells-formula-literal.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx", 1)
        require_subset(refused, {"status": "refused", "output": None}, "cells-formula-literal-expect")
        require("formulas are preserved, not overwritten" in refused["message"], "the refusal does not say that formulas are preserved")
        require(not path_exists(outputs / "cells-formula-literal.xlsx"), "a refused formula batch published an output")

        # ---- XLSX: guarded row append after the last used row, and a stale-hash refusal ----
        row_batch = [
            "edit", "rows", "--expect-sha256", workbook_hash, "--sheet", "Summary",
            "--append-row", '{"A": "string:Client B", "B": "number:12.5", "C": "boolean:false", "D": "string:pending"}',
            "--append-row", '{"A": "string:Client A", "B": "number:30"}',
        ]
        row_edit = document_step("rows-batch", [*row_batch, "-o", f"{output_root}/rows.xlsx", workbook], "edit-receipt", inputs / "data.xlsx")
        require_subset(row_edit, {"status": "completed", "result": {
            "engine": "xlsx-rows", "skipped": [], "outputExtension": "xlsx",
            "applied": [{"kind": "row_appended", "count": 2, "detail": "Summary: rows 2-3, columns A:D"}],
        }}, "rows-batch")
        require_subset(row_edit["output"], identity(outputs / "rows.xlsx"), "rows-batch.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "rows.xlsx"), row_edit))) as changed:
            require(original.namelist() == changed.namelist(), "row append changed the package member set/order")
            # Strings were added, so the shared table grows; the workbook holds a formula, so it
            # carries the recalculation request. Every other part, docProps/app.xml included,
            # stays byte-identical.
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/sharedStrings.xml", "xl/workbook.xml"):
                    require(original.read(name) == changed.read(name), "row append changed an untouched package part")
            require(original.read("docProps/app.xml") == changed.read("docProps/app.xml"), "row append changed docProps/app.xml")
            ns = {"s": _SML}
            before_sheet = ET.fromstring(original.read("xl/worksheets/sheet1.xml"))
            after_sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            before_rows = before_sheet.findall(".//s:row", ns)
            after_rows = after_sheet.findall(".//s:row", ns)
            require([row.get("r") for row in after_rows] == ["1", "2", "3"], "the appended worksheet does not hold exactly rows 1 to 3")
            require(
                [element_shape(cell) for cell in before_rows[0]] == [element_shape(cell) for cell in after_rows[0]],
                "the row append changed row 1's cell list",
            )
            require(after_sheet.find("s:dimension", ns).get("ref") == "A1:D3", "the dimension does not cover the appended rows")
            require(not any(row.findall(".//s:f", ns) for row in after_rows[1:]), "an appended row carries a formula")
            shared = ["".join(item.itertext()) for item in ET.fromstring(changed.read("xl/sharedStrings.xml")).findall("s:si", ns)]
            written = worksheet_cells(after_sheet, shared)
            require({ref: written.get(ref) for ref in ("A2", "B2", "C2", "D2", "A3", "B3")} == {
                "A2": ("string", "Client B"), "B2": ("number", "12.5"), "C2": ("boolean", "FALSE"), "D2": ("string", "pending"),
                "A3": ("string", "Client A"), "B3": ("number", "30"),
            }, "the appended cells do not carry the requested types and values")
            require("C3" not in written and "D3" not in written, "the append created cells nobody asked for")
            before_workbook = ET.fromstring(original.read("xl/workbook.xml"))
            after_workbook = ET.fromstring(changed.read("xl/workbook.xml"))
            recalculation = after_workbook.findall("s:calcPr", ns)
            require(
                len(recalculation) == 1 and recalculation[0].get("fullCalcOnLoad") == "1",
                "a workbook holding formulas was not asked to recalculate once on open",
            )
            for element in recalculation:
                after_workbook.remove(element)
            require(element_shape(before_workbook) == element_shape(after_workbook), "the row append changed the workbook beyond the recalculation request")
        refused = document_step("rows-stale", [
            "edit", "rows", "--expect-sha256", "0" * 64, "--append-row", '{"A": "string:Client Z"}',
            "-o", f"{output_root}/rows-stale.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx", 1)
        require_subset(refused, {"status": "refused", "code": "source_revision_mismatch", "output": None}, "rows-stale")
        require(not path_exists(outputs / "rows-stale.xlsx"), "a refused row append published an output")

        # ---- XLSX: a row inserted in the middle, with the formula below it moved to match ----
        inserted = document_step("row-insert", [
            "edit", "rows", "--expect-sha256", workbook_hash, "--sheet", "Summary",
            "--insert-before", '1={"A": "string:Header", "B": "number:1"}',
            "-o", f"{output_root}/rows-inserted.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(inserted, {"status": "completed", "result": {
            "engine": "xlsx-rows", "skipped": [], "outputExtension": "xlsx",
            "applied": [{"kind": "row_inserted", "count": 1,
                         "detail": "Summary: new row 1; rows 1.. shifted down by 1; 1 formula adjusted"}],
        }}, "row-insert")
        require_subset(inserted["output"], identity(outputs / "rows-inserted.xlsx"), "row-insert.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "rows-inserted.xlsx"), inserted))) as changed:
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/workbook.xml"):
                    require(original.read(name) == changed.read(name), "the row insert changed an untouched package part")
            ns = {"s": _SML}
            after_sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            rows = after_sheet.findall(".//s:row", ns)
            require([row.get("r") for row in rows] == ["1", "2"], "the insert did not produce exactly rows 1 and 2")
            require(after_sheet.find("s:dimension", ns).get("ref") == "A1:D2", "the dimension does not cover the inserted row")
            new_row = {cell.get("r"): "".join(cell.itertext()).strip() for cell in rows[0]}
            require(new_row.get("A1") == "Header" and new_row.get("B1") == "1", "the inserted row does not hold what was asked for")
            moved = {cell.get("r"): cell for cell in rows[1]}
            require(set(moved) == {"A2", "B2", "C2", "D2"}, "the shifted row did not keep its four cells")
            formula = moved["D2"].find("s:f", ns)
            require(formula is not None and formula.text == "B2*2", "the shifted formula was not rewritten from B1*2 to B2*2")
            recalculation = ET.fromstring(changed.read("xl/workbook.xml")).findall("s:calcPr", ns)
            require(
                len(recalculation) == 1 and recalculation[0].get("fullCalcOnLoad") == "1",
                "the shifted workbook was not asked to recalculate once on open",
            )

        merged = f"{input_root}/merged.xlsx"
        merged_refusal = document_step("row-insert-merge-refusal", [
            "edit", "rows", "--expect-sha256", source_identities["merged.xlsx"]["sha256"], "--sheet", "Merged",
            "--insert-before", "2", "-o", f"{output_root}/merged-inserted.xlsx", merged,
        ], "edit-receipt", inputs / "merged.xlsx", 1)
        require_subset(merged_refusal, {"status": "refused", "output": None}, "row-insert-merge-refusal")
        require("straddles the insertion point" in merged_refusal["message"], "the refusal does not name the straddling merge")
        require(not path_exists(outputs / "merged-inserted.xlsx"), "a refused row insert published an output")

        # ---- XLSX: a column inserted in the middle, with the formula right of it moved to match ----
        column_inserted = document_step("column-insert", [
            "edit", "columns", "--expect-sha256", workbook_hash, "--sheet", "Summary", "--insert-before", "B",
            "-o", f"{output_root}/columns-inserted.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(column_inserted, {"status": "completed", "result": {
            "engine": "xlsx-columns", "skipped": [], "outputExtension": "xlsx",
        }}, "column-insert")
        require(
            [row["kind"] for row in column_inserted["result"]["applied"]] == ["column_inserted"],
            "the column insert did not report exactly one inserted column",
        )
        require_subset(column_inserted["output"], identity(outputs / "columns-inserted.xlsx"), "column-insert.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "columns-inserted.xlsx"), column_inserted))) as changed:
            require(original.namelist() == changed.namelist(), "the column insert changed the package member set")
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/workbook.xml"):
                    require(original.read(name) == changed.read(name), "the column insert changed an untouched package part")
            ns = {"s": _SML}
            before_sheet = ET.fromstring(original.read("xl/worksheets/sheet1.xml"))
            before_cells = {cell.get("r"): cell for cell in before_sheet.findall(".//s:row/s:c", ns)}
            require(before_cells["D1"].find("s:f", ns).text == "B1*2", "the sample workbook no longer holds B1*2 in D1")
            after_sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            after_cells = {cell.get("r"): cell for cell in after_sheet.findall(".//s:row/s:c", ns)}
            require(set(after_cells) == {"A1", "C1", "D1", "E1"}, "the insert did not leave column B empty and move the rest right")
            require(after_cells["E1"].find("s:f", ns).text == "C1*2", "the moved formula was not rewritten from B1*2 to C1*2")
            require(after_sheet.find("s:dimension", ns).get("ref") == "A1:E1", "the dimension does not cover the inserted column")
            recalculation = ET.fromstring(changed.read("xl/workbook.xml")).findall("s:calcPr", ns)
            require(
                len(recalculation) == 1 and recalculation[0].get("fullCalcOnLoad") == "1",
                "the shifted workbook was not asked to recalculate once on open",
            )

        # ---- XLSX: a frozen header row and one column width, with nothing else touched ----
        laid_out = document_step("layout-freeze-width", [
            "edit", "layout", "--expect-sha256", workbook_hash, "--sheet", "Summary",
            "--freeze", "A2", "--column-width", "A=20",
            "-o", f"{output_root}/layout.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(laid_out, {"status": "completed", "result": {
            "engine": "xlsx-layout", "skipped": [], "outputExtension": "xlsx",
            "applied": [
                {"kind": "panes_frozen", "count": 1, "detail": "'Summary': A2 (xSplit 0, ySplit 1)"},
                {"kind": "column_width_set", "count": 1, "detail": "'Summary': A=20"},
            ],
        }}, "layout-freeze-width")
        require_subset(laid_out["output"], identity(outputs / "layout.xlsx"), "layout-freeze-width.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "layout.xlsx"), laid_out))) as changed:
            require(original.namelist() == changed.namelist(), "the layout edit changed the package member set")
            for name in original.namelist():
                if name != "xl/worksheets/sheet1.xml":
                    require(original.read(name) == changed.read(name), "the layout edit changed a part other than the worksheet")
            ns = {"s": _SML}
            after_sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            pane = after_sheet.find("s:sheetViews/s:sheetView/s:pane", ns)
            require(pane is not None, "the layout edit wrote no frozen pane")
            require(
                (pane.get("ySplit"), pane.get("xSplit"), pane.get("topLeftCell"), pane.get("activePane"), pane.get("state")) ==
                ("1", None, "A2", "bottomLeft", "frozen"),
                "the frozen pane is not the one Excel writes for a frozen first row",
            )
            columns = after_sheet.findall("s:cols/s:col", ns)
            require(len(columns) == 1, "the layout edit did not write exactly one column width entry")
            require(
                (columns[0].get("min"), columns[0].get("max"), columns[0].get("width"), columns[0].get("customWidth")) ==
                ("1", "1", "20", "1"),
                "the column width entry does not size column A at 20 characters",
            )
            require(
                ET.fromstring(original.read("xl/worksheets/sheet1.xml")).findall(".//s:c", ns) is not None and
                [cell.get("r") for cell in after_sheet.findall(".//s:row/s:c", ns)] == ["A1", "B1", "C1", "D1"],
                "the layout edit moved or dropped a cell",
            )

        # ---- XLSX: a number format on one cell, through a cloned cell format ----
        cell_format = document_step("cell-format", [
            "edit", "format", "--expect-sha256", workbook_hash, "--sheet", "Summary",
            "--expect", "B1=number:10", "--apply", "B1=numfmt:0.00",
            "-o", f"{output_root}/formatted.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(cell_format, {"status": "completed", "result": {
            "engine": "xlsx-format", "skipped": [], "outputExtension": "xlsx",
            "applied": [{"kind": "format_applied", "count": 1, "detail": "Summary: B1 (numfmt:0.00)"}],
        }}, "cell-format")
        require_subset(cell_format["output"], identity(outputs / "formatted.xlsx"), "cell-format.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "formatted.xlsx"), cell_format))) as changed:
            require(original.namelist() == changed.namelist(), "the cell format changed the package member set/order")
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/styles.xml"):
                    require(original.read(name) == changed.read(name), "the cell format changed an untouched package part")
            before_styles = ET.fromstring(original.read("xl/styles.xml"))
            after_styles = ET.fromstring(changed.read("xl/styles.xml"))
            before_sheet = ET.fromstring(original.read("xl/worksheets/sheet1.xml"))
            after_sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            require(cell_style_index(before_sheet, "B1") == 0, "the source cell already carried a style")
            index = cell_style_index(after_sheet, "B1")
            require(index != 0, "the formatted cell still points at the default cell format")
            require(number_format_code(after_styles, index) == "0.00", "the cell's format does not carry the requested code")
            require(
                element_shape(before_styles.find(f"{{{_SML}}}cellXfs").findall(f"{{{_SML}}}xf")[0])
                == element_shape(after_styles.find(f"{{{_SML}}}cellXfs").findall(f"{{{_SML}}}xf")[0]),
                "the cell format rewrote an existing cell format instead of appending one",
            )
            require(
                worksheet_cells(after_sheet, ["Client A"]) == worksheet_cells(before_sheet, ["Client A"]),
                "the cell format changed a cell value",
            )

        # ---- PPTX: inventory, slide read, guarded shape and notes batch ----
        slides_validator = validator_for("com.krauq.ufo.pptx-slides", 1)
        require(slides_validator is not None, "pptx-slides schema is missing")
        deck = f"{input_root}/deck.pptx"
        deck_hash = source_identities["deck.pptx"]["sha256"]
        slide_inventory_receipt = document_step("slides-inventory", ["text", "--format", "structure", "-o", f"{output_root}/slides-inventory.json", deck], "text-receipt", inputs / "deck.pptx")
        require_subset(slide_inventory_receipt["output"], identity(outputs / "slides-inventory.json"), "slides-inventory")
        slide_inventory = json.loads(read_bounded(outputs / "slides-inventory.json"))
        slides_validator.validate(slide_inventory)
        require_subset(slide_inventory, {"selection": None, "slides": [{"index": 1, "part": "ppt/slides/slide1.xml", "notesPart": "ppt/notesSlides/notesSlide1.xml", "plainTextShapes": 2}]}, "slides-inventory")
        slide_receipt = document_step("slide-read", ["text", "--format", "structure", "--slide", "1", "--expect-sha256", deck_hash, "-o", f"{output_root}/slide-1.json", deck], "text-receipt", inputs / "deck.pptx")
        require_subset(slide_receipt["output"], identity(outputs / "slide-1.json"), "slide-read")
        slide_page = json.loads(read_bounded(outputs / "slide-1.json"))
        slides_validator.validate(slide_page)
        require_subset(slide_page["selection"], {"slide": 1, "part": "ppt/slides/slide1.xml", "shapes": [
            {"id": 2, "name": "Title 1", "placeholder": "title", "paragraphs": [{"index": 0, "text": "Q3 review"}]},
            {"id": 3, "name": "Body 2", "placeholder": None, "paragraphs": [{"index": 1, "text": "Revenue up 10%"}]},
        ], "other": [], "notes": {"part": "ppt/notesSlides/notesSlide1.xml", "paragraphs": [{"index": 0, "text": "Mention the new client"}]}}, "slide-read")
        slide_batch = ["edit", "paragraphs", "--expect-sha256", deck_hash, "--slide", "1", "--expect", "1=Revenue up 10%", "--set", "1=Revenue up 12%", "--expect", "notes:0=Mention the new client", "--set", "notes:0=Thank the new client"]
        slide_edit = document_step("slide-batch", [*slide_batch, "-o", f"{output_root}/edited.pptx", deck], "edit-receipt", inputs / "deck.pptx")
        require_subset(slide_edit, {"status": "completed", "result": {"engine": "pptx-slides"}}, "slide-batch")
        require_subset(slide_edit["output"], identity(outputs / "edited.pptx"), "slide-batch.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "deck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "deck.pptx"), read_bounded(outputs / "edited.pptx"), slide_edit))) as changed:
            require(original.namelist() == changed.namelist(), "slide edit changed the package member set/order")
            for name in original.namelist():
                if name not in ("ppt/slides/slide1.xml", "ppt/notesSlides/notesSlide1.xml"):
                    require(original.read(name) == changed.read(name), "slide edit changed an untouched package part")
            ns = {"a": _DML}
            slide_text = ["".join(p.itertext()) for p in ET.fromstring(changed.read("ppt/slides/slide1.xml")).findall(".//a:p", ns)]
            notes_text = ["".join(p.itertext()) for p in ET.fromstring(changed.read("ppt/notesSlides/notesSlide1.xml")).findall(".//a:p", ns)]
            require(slide_text == ["Q3 review", "Revenue up 12%"], "slide batch did not make exactly the expected text changes")
            require(notes_text == ["Thank the new client"], "slide batch did not update the speaker notes")
            require(b'<a:rPr i="1"/>' in changed.read("ppt/slides/slide1.xml"), "slide batch dropped run formatting")

        # ---- PPTX: a guarded slide duplicate with its notes, and a delete that would empty the deck ----
        duplicate = document_step("slide-duplicate", [
            "edit", "slides", "--expect-sha256", deck_hash, "--duplicate", "1", "--after", "1",
            "-o", f"{output_root}/deck-duplicated.pptx", deck,
        ], "edit-receipt", inputs / "deck.pptx")
        require_subset(duplicate, {"status": "completed", "result": {
            "engine": "pptx-deck", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "slide_duplicated", "count": 1, "detail": "slide 1 copied to position 2 as ppt/slides/slide2.xml"}],
        }}, "slide-duplicate")
        require_subset(duplicate["output"], identity(outputs / "deck-duplicated.pptx"), "slide-duplicate.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "deck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "deck.pptx"), read_bounded(outputs / "deck-duplicated.pptx"), duplicate))) as changed:
            rewritten = {"[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"}
            added = {
                "ppt/slides/slide2.xml", "ppt/slides/_rels/slide2.xml.rels",
                "ppt/notesSlides/notesSlide2.xml", "ppt/notesSlides/_rels/notesSlide2.xml.rels",
            }
            require(set(changed.namelist()) == set(original.namelist()) | added, "the slide duplicate changed the package member set")
            for name in original.namelist():
                if name not in rewritten:
                    require(original.read(name) == changed.read(name), "the slide duplicate changed an untouched package part")
            parts = deck_slide_parts(changed)
            require(parts == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"], "the deck does not list the source slide and its copy in order")
            texts = [paragraph_texts(changed, part, _DML) for part in parts]
            require(texts[0] == ["Q3 review", "Revenue up 10%"] and texts[0] == texts[1], "the copy does not carry the source slide's shape text")
            require(changed.read("ppt/slides/slide2.xml") == original.read("ppt/slides/slide1.xml"), "the slide part was not copied byte for byte")
            require(
                relationship_targets(changed, "ppt/slides/slide2.xml").get(f"{_REL}/notesSlide") == "../notesSlides/notesSlide2.xml",
                "the copy does not point at its own notes slide",
            )
            require(
                relationship_targets(changed, "ppt/notesSlides/notesSlide2.xml").get(f"{_REL}/slide") == "../slides/slide2.xml",
                "the copied notes slide does not point back at the copy",
            )
            require(
                changed.read("ppt/notesSlides/notesSlide2.xml") == original.read("ppt/notesSlides/notesSlide1.xml"),
                "the speaker notes were not copied byte for byte",
            )
            require(
                {"/ppt/slides/slide2.xml", "/ppt/notesSlides/notesSlide2.xml"} <= content_type_overrides(changed),
                "the new parts are not registered in the content types",
            )
        # ---- PPTX: a move over the order the duplicate left behind ----
        duplicated_hash = identity(outputs / "deck-duplicated.pptx")["sha256"]
        moved = document_step("slide-move", [
            "edit", "slides", "--expect-sha256", duplicated_hash, "--move", "2", "--to", "1",
            "-o", f"{output_root}/deck-moved.pptx", f"{output_root}/deck-duplicated.pptx",
        ], "edit-receipt", outputs / "deck-duplicated.pptx")
        require_subset(moved, {"status": "completed", "result": {
            "engine": "pptx-deck", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "slide_moved", "count": 1, "detail": "slide 2 moved from position 2 to 1"}],
        }}, "slide-move")
        require_subset(moved["output"], identity(outputs / "deck-moved.pptx"), "slide-move.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "deck-duplicated.pptx"))) as before_move, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(outputs / "deck-duplicated.pptx"), read_bounded(outputs / "deck-moved.pptx"), moved))) as after_move:
            require(
                deck_slide_parts(before_move) == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"],
                "the duplicated deck does not list the source slide and its copy in order",
            )
            require(
                deck_slide_parts(after_move) == ["ppt/slides/slide2.xml", "ppt/slides/slide1.xml"],
                "the move did not reorder the presentation's slide list",
            )
            require(set(before_move.namelist()) == set(after_move.namelist()), "the slide move changed the package member set")
            # A move touches the slide list and nothing else: every slide and notes part keeps its bytes.
            for name in before_move.namelist():
                if name != "ppt/presentation.xml":
                    require(before_move.read(name) == after_move.read(name), "the slide move rewrote a part other than the presentation")
        refused = document_step("slide-delete-last", [
            "edit", "slides", "--expect-sha256", deck_hash, "--delete", "1",
            "-o", f"{output_root}/deck-emptied.pptx", deck,
        ], "edit-receipt", inputs / "deck.pptx", 1)
        require_subset(refused, {"status": "refused", "output": None}, "slide-delete-last")
        require("a deck keeps at least one slide" in refused["message"], "the refusal does not say that a deck keeps at least one slide")
        require(not path_exists(outputs / "deck-emptied.pptx"), "a refused slide batch published an output")

        # ---- JSON: guarded pointer writes with literal numbers preserved ----
        update = f"{input_root}/update.json"
        update_hash = source_identities["update.json"]["sha256"]
        field_batch = ["edit", "fields", "--expect-sha256", update_hash, "--expect", "/status=string:draft", "--set", "/status=string:final", "--expect", "/items/0/done=boolean:false", "--set", "/items/0/done=boolean:true"]
        field_edit = document_step("fields-batch", [*field_batch, "-o", f"{output_root}/update-final.json", update], "edit-receipt", inputs / "update.json")
        require_subset(field_edit, {"status": "completed", "result": {"engine": "json-fields"}}, "fields-batch")
        require_subset(field_edit["output"], identity(outputs / "update-final.json"), "fields-batch.output")
        final_text = read_bounded(outputs / "update-final.json").decode("utf-8")
        final_json = json.loads(final_text)
        require(final_json == {"client": "Acme", "status": "final", "amount": 1.5, "items": [{"id": 7, "done": True}]}, "field batch did not make exactly the expected value changes")
        require('"amount": 1.50' in final_text, "field batch normalized an untouched number literal")
        refused = document_step("fields-preimage", ["edit", "fields", "--expect-sha256", update_hash, "--expect", "/status=string:final", "--set", "/status=string:x", "-o", f"{output_root}/fields-preimage.json", update], "edit-receipt", inputs / "update.json", 1)
        require_subset(refused, {"status": "refused", "output": None}, "fields-preimage")
        require(not path_exists(outputs / "fields-preimage.json"), "a refused field batch published an output")

        # ---- DOCX: native tracked changes and an anchored comment, checked by an independent XML reader ----
        W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        tracked_batch = ["edit", "paragraphs", "--expect-sha256", source_hash, "--track", "--author", "Sample reviewer", "--expect", f"0={originals[0]}", "--set", f"0={replacements[0]}"]
        tracked_edit = document_step("paragraph-tracked", [*tracked_batch, "-o", f"{output_root}/tracked.docx", source], "edit-receipt", inputs / "report.docx")
        require_subset(tracked_edit, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "paragraph-tracked")
        require([item["kind"] for item in tracked_edit["result"]["applied"]] == ["paragraph_tracked"], "tracked batch did not report paragraph_tracked")
        require_subset(tracked_edit["output"], identity(outputs / "tracked.docx"), "paragraph-tracked.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "tracked.docx"), tracked_edit))) as changed:
            require(original.namelist() == changed.namelist(), "tracked edit changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "tracked edit changed an untouched package part")
            paragraphs = ET.fromstring(changed.read("word/document.xml")).findall(".//w:p", {"w": W})
            require(len(paragraphs) == 2, "tracked edit changed the paragraph count")
            insertions = paragraphs[0].findall(".//w:ins", {"w": W})
            deletions = paragraphs[0].findall(".//w:del", {"w": W})
            require(insertions and deletions, "tracked edit did not record an insertion and a deletion")
            require({node.get(f"{{{W}}}author") for node in insertions + deletions} == {"Sample reviewer"}, "tracked edit did not carry the author")
            require(resolved_text(paragraphs[0], W, accept=True) == replacements[0], "accepting the tracked change would not give the replacement text")
            require(resolved_text(paragraphs[0], W, accept=False) == originals[0], "rejecting the tracked change would not restore the original text")
            require("".join(paragraphs[1].itertext()) == originals[1], "tracked edit touched the other paragraph")
        comment_text = "Please confirm this line before sending."
        comment_batch = ["edit", "paragraphs", "--expect-sha256", source_hash, "--author", "Sample reviewer", "--expect", f"1={originals[1]}", "--comment", f"1={comment_text}"]
        comment_edit = document_step("paragraph-comment", [*comment_batch, "-o", f"{output_root}/commented.docx", source], "edit-receipt", inputs / "report.docx")
        require_subset(comment_edit, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "paragraph-comment")
        require([item["kind"] for item in comment_edit["result"]["applied"]] == ["comment_added"], "comment batch did not report comment_added")
        require_subset(comment_edit["output"], identity(outputs / "commented.docx"), "paragraph-comment.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "commented.docx"), comment_edit))) as changed:
            rewritten = {"[Content_Types].xml", "word/_rels/document.xml.rels", "word/document.xml", "word/comments.xml"}
            require(set(changed.namelist()) == set(original.namelist()) | {"word/comments.xml", "word/_rels/document.xml.rels"}, "comment edit changed the package member set")
            for name in original.namelist():
                if name not in rewritten:
                    require(original.read(name) == changed.read(name), "comment edit changed an untouched package part")
            comments = ET.fromstring(changed.read("word/comments.xml"))
            entries = comments.findall("w:comment", {"w": W})
            require(len(entries) == 1 and entries[0].get(f"{{{W}}}author") == "Sample reviewer", "comment part does not hold one comment by the author")
            require("".join(entries[0].itertext()).strip() == comment_text, "comment part does not hold the comment text")
            paragraphs = ET.fromstring(changed.read("word/document.xml")).findall(".//w:p", {"w": W})
            require(["".join(node.text or "" for node in p.iter(f"{{{W}}}t")) for p in paragraphs] == originals, "comment edit changed paragraph text")
            require(paragraphs[1].find(".//w:commentRangeStart", {"w": W}) is not None and paragraphs[0].find(".//w:commentRangeStart", {"w": W}) is None, "comment is not anchored on the requested paragraph only")

        # ---- DOCX: bold exactly one span of a paragraph, and refuse a style the document cannot have ----
        span_word = "synthetic"
        span_offset = originals[0].index(span_word)
        formatted = document_step("run-format-span", [
            "edit", "format", "--expect-sha256", source_hash,
            "--expect", f"0={originals[0]}", "--apply", "0=bold",
            "--range", f"0={span_offset}:{len(span_word)}",
            "-o", f"{output_root}/bolded.docx", source,
        ], "edit-receipt", inputs / "report.docx")
        require_subset(formatted, {"status": "completed", "result": {
            "engine": "docx-format", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "format_applied", "count": 1, "detail": f"paragraphs: 0 (bold) [span {span_offset}:{len(span_word)}]"}],
        }}, "run-format-span")
        require_subset(formatted["output"], identity(outputs / "bolded.docx"), "run-format-span.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "bolded.docx"), formatted))) as changed:
            require(original.namelist() == changed.namelist(), "the run format changed the package member set/order")
            for name in original.namelist():
                if name != "word/document.xml":
                    require(original.read(name) == changed.read(name), "the run format changed an untouched package part")
            bolded = ET.fromstring(changed.read("word/document.xml")).findall(f".//{{{_WML}}}p")
            require(
                ["".join(node.itertext()) for node in bolded] == originals,
                "the run format changed the paragraph text it was only supposed to format",
            )
            require(
                run_property_coverage(bolded[0], "b") == span_word,
                "the bold property does not cover exactly the requested span",
            )
            require(run_property_coverage(bolded[1], "b") == "", "the run format reached a paragraph it did not name")
        refused = document_step("paragraph-style-refusal", [
            "edit", "paragraphs", "--expect-sha256", source_hash,
            "--expect", f"0={originals[0]}", "--set-style", "0=Heading1",
            "-o", f"{output_root}/styled.docx", source,
        ], "edit-receipt", inputs / "report.docx", 1)
        require_subset(refused, {"status": "refused", "output": None}, "paragraph-style-refusal")
        require("no styles part" in refused["message"], "the refusal does not say the document has no styles part")
        require(not path_exists(outputs / "styled.docx"), "a refused paragraph style published an output")

        # ---- PNG: crop, quarter turn and 2:1 shrink checked by an independent decoder ----
        photo = f"{input_root}/photo.png"
        thumb = document_step("image-crop-rotate", ["edit", "image", "--crop", "0,0,4,4", "--rotate", "90", "-o", f"{output_root}/thumb.png", photo], "edit-receipt", inputs / "photo.png")
        require_subset(thumb, {"status": "completed", "result": {"engine": "image-raster"}}, "image-crop-rotate")
        require([item["kind"] for item in thumb["result"]["applied"]] == ["image_cropped", "image_rotated"], "image edit did not report the crop and the rotation")
        require_subset(thumb["output"], identity(outputs / "thumb.png"), "image-crop-rotate.output")
        require(decode_png(read_bounded(outputs / "thumb.png")) == (4, 4, [[BLUE, BLUE, RED, RED]] * 4), "crop and quarter turn did not produce the expected pixels")
        small = document_step("image-resize", ["edit", "image", "--resize", "4x2", "-o", f"{output_root}/small.png", photo], "edit-receipt", inputs / "photo.png")
        require_subset(small, {"status": "completed", "result": {"engine": "image-raster"}}, "image-resize")
        require(decode_png(read_bounded(outputs / "small.png")) == (4, 2, [[RED, RED, GREEN, GREEN], [BLUE, BLUE, WHITE, WHITE]]), "2:1 shrink did not average each block to its own color")
        # ---- DOCX and PPTX: one inline picture each, stored byte for byte ----
        illustrated = document_step("image-insert-docx", [
            "edit", "image-insert", "--expect-sha256", source_hash, "--image", photo,
            "--after", "1", "--alt", "The synthetic evaluation photo",
            "-o", f"{output_root}/illustrated.docx", source,
        ], "edit-receipt", inputs / "report.docx")
        require_subset(illustrated, {"status": "completed", "result": {
            "engine": "docx-images", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "image_inserted", "count": 1, "detail": "word/media/image1.png 8x4 px after paragraph 1"}],
        }}, "image-insert-docx")
        require_subset(illustrated["output"], identity(outputs / "illustrated.docx"), "image-insert-docx.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "report.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "report.docx"), read_bounded(outputs / "illustrated.docx"), illustrated))) as changed:
            added = {"word/media/image1.png"}
            # The picture joins the settings relationship the source already carries, so the
            # relationship part is rewritten; word/settings.xml itself must not be.
            rewritten = {"word/document.xml", "[Content_Types].xml", "word/_rels/document.xml.rels"}
            require(set(changed.namelist()) == set(original.namelist()) | added, "the inserted picture changed the package member set")
            for name in original.namelist():
                if name not in rewritten:
                    require(original.read(name) == changed.read(name), "the inserted picture changed an untouched package part")
            require(changed.read("word/media/image1.png") == read_bounded(inputs / "photo.png"), "the stored media part is not photo.png")
            rows = list(ET.fromstring(changed.read("word/_rels/document.xml.rels")))
            kept = list(ET.fromstring(original.read("word/_rels/document.xml.rels")))
            relationships = [row for row in rows if row.get("Target") == "media/image1.png"]
            require(len(rows) == len(kept) + 1 and len(relationships) == 1, "the document does not carry exactly one image relationship")
            require(
                [(row.get("Id"), row.get("Type"), row.get("Target")) for row in rows[:len(kept)]]
                == [(row.get("Id"), row.get("Type"), row.get("Target")) for row in kept],
                "the picture insert moved or rewrote the settings relationship",
            )
            defaults = {e.get("Extension"): e.get("ContentType") for e in ET.fromstring(changed.read("[Content_Types].xml")) if e.tag.endswith("Default")}
            require(defaults.get("png") == "image/png", "the package does not declare the PNG content type")
            body = ET.fromstring(changed.read("word/document.xml"))
            picture_paragraphs = body.findall(f".//{{{_WML}}}p")
            require(len(picture_paragraphs) == 3, "the picture did not add exactly one paragraph")
            blips = picture_paragraphs[2].findall(f".//{{{_DML}}}blip")
            require(
                len(blips) == 1 and blips[0].get(f"{{{_REL}}}embed") == relationships[0].get("Id"),
                "the new paragraph does not reference the new picture",
            )
            require(b'descr="The synthetic evaluation photo"' in changed.read("word/document.xml"), "the picture carries no alt text")
        deck_illustrated = document_step("image-insert-pptx", [
            "edit", "image-insert", "--expect-sha256", deck_hash, "--image", photo,
            "--slide", "1", "--x", "914400", "--y", "914400",
            "-o", f"{output_root}/illustrated.pptx", deck,
        ], "edit-receipt", inputs / "deck.pptx")
        require_subset(deck_illustrated, {"status": "completed", "result": {
            "engine": "pptx-images", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "image_inserted", "count": 1, "detail": "ppt/media/image1.png on slide 1 at 914400,914400"}],
        }}, "image-insert-pptx")
        require_subset(deck_illustrated["output"], identity(outputs / "illustrated.pptx"), "image-insert-pptx.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "deck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "deck.pptx"), read_bounded(outputs / "illustrated.pptx"), deck_illustrated))) as changed:
            require(set(changed.namelist()) == set(original.namelist()) | {"ppt/media/image1.png"}, "the slide picture changed the package member set")
            for name in original.namelist():
                if name not in ("ppt/slides/slide1.xml", "ppt/slides/_rels/slide1.xml.rels", "[Content_Types].xml"):
                    require(original.read(name) == changed.read(name), "the slide picture changed an untouched package part")
            require(changed.read("ppt/media/image1.png") == read_bounded(inputs / "photo.png"), "the stored slide media part is not photo.png")
            require(
                relationship_targets(changed, "ppt/slides/slide1.xml").get(f"{_REL}/image") == "../media/image1.png",
                "the slide does not point at the new picture",
            )
            defaults = {e.get("Extension") for e in ET.fromstring(changed.read("[Content_Types].xml")) if e.tag.endswith("Default")}
            require("png" in defaults, "the deck does not declare the PNG content type")
            slide_root = ET.fromstring(changed.read("ppt/slides/slide1.xml"))
            pictures = slide_root.findall(f".//{{{_PML}}}pic")
            require(len(pictures) == 1, "the slide does not carry exactly one picture")
            offsets = pictures[0].findall(f".//{{{_DML}}}off")
            require(len(offsets) == 1 and (offsets[0].get("x"), offsets[0].get("y")) == ("914400", "914400"), "the picture is not at the requested EMU offset")
            require(
                paragraph_texts(changed, "ppt/slides/slide1.xml", _DML) == ["Q3 review", "Revenue up 10%"],
                "the slide picture changed the slide's text",
            )
        # ---- DOCX: the inserted picture is swapped for a second one, in place, with --fit ----
        second_photo = f"{input_root}/photo2.png"
        illustrated_identity = identity(outputs / "illustrated.docx")
        drawn = drawing_extent(read_bounded(outputs / "illustrated.docx"))
        replaced = document_step("image-replace-docx", [
            "edit", "image-replace", "--expect-sha256", illustrated_identity["sha256"],
            "--target", "word/media/image1.png", "--image", second_photo, "--fit",
            "-o", f"{output_root}/replaced.docx", f"{output_root}/illustrated.docx",
        ], "edit-receipt", outputs / "illustrated.docx")
        require_subset(replaced, {"status": "completed", "result": {
            "engine": "docx-images", "skipped": [], "outputExtension": "docx",
            "applied": [{"kind": "image_replaced", "count": 1, "detail": "word/media/image1.png -> word/media/image2.png, 4x4 px, extent fitted"}],
        }}, "image-replace-docx")
        require_subset(replaced["output"], identity(outputs / "replaced.docx"), "image-replace-docx.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "illustrated.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(outputs / "illustrated.docx"), read_bounded(outputs / "replaced.docx"), replaced))) as changed:
            require(
                set(changed.namelist()) == (set(original.namelist()) - {"word/media/image1.png"}) | {"word/media/image2.png"},
                "the replacement did not swap exactly one media part",
            )
            require(changed.read("word/media/image2.png") == read_bounded(inputs / "photo2.png"), "the new media part is not photo2.png")
            for name in original.namelist():
                if name not in ("word/media/image1.png", "word/_rels/document.xml.rels", "word/document.xml"):
                    require(original.read(name) == changed.read(name), "the replacement changed an untouched package part")
            targets = {e.get("Id"): e.get("Target") for e in ET.fromstring(changed.read("word/_rels/document.xml.rels"))}
            require(set(targets.values()) == {"settings.xml", "media/image2.png"}, "the document points at a picture other than the new one")
            # --fit keeps the drawn width and doubles the height, because the new picture is square where the old was 2:1.
            fitted = drawing_extent(read_bounded(outputs / "replaced.docx"))
            require(fitted[0] == drawn[0], "the fitted replacement changed the drawn width")
            require(fitted[1] == drawn[0], "the fitted replacement did not rescale the drawn height to the new aspect")
            require(fitted[1] == drawn[1] * 2, "the fitted height is not twice the height the 2:1 picture was drawn at")
        # ---- PPTX charts: the caches every reader draws and the workbook Edit Data opens ----
        chart_deck = f"{input_root}/chartdeck.pptx"
        chart_hash = source_identities["chartdeck.pptx"]["sha256"]
        chart_receipt = document_step("chart-read", [
            "text", "--format", "structure", "--slide", "1", "--expect-sha256", chart_hash,
            "-o", f"{output_root}/chart-slide.json", chart_deck,
        ], "text-receipt", inputs / "chartdeck.pptx")
        require_subset(chart_receipt["output"], identity(outputs / "chart-slide.json"), "chart-read")
        chart_page = json.loads(read_bounded(outputs / "chart-slide.json"))
        slides_validator.validate(chart_page)
        require_subset(chart_page["selection"], {"charts": [{
            "index": 1, "part": "ppt/charts/chart1.xml", "title": "Quarterly revenue", "type": "bar",
            "categories": CHART_CATEGORIES, "embeddedWorkbook": CHART_WORKBOOK,
            "series": [{"index": 1, "name": "Revenue", "values": [10, 20, 30, 40], "formulaRef": "Sheet1!$B$2:$B$5"}],
        }], "chartsComplete": True}, "chart-read")

        chart_edit = document_step("chart-write", [
            "edit", "chart", "--expect-sha256", chart_hash, "--slide", "1", "--chart", "1",
            "--expect-series", "Revenue=10,20,30,40", "--set-series", "Revenue=12,24,36,48",
            "--categories", "H1,H2,H3,H4",
            "-o", f"{output_root}/chartdeck-updated.pptx", chart_deck,
        ], "edit-receipt", inputs / "chartdeck.pptx")
        require_subset(chart_edit, {"status": "completed", "result": {
            "engine": "pptx-charts", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "chart_series_written", "count": 1, "detail":
                         "slide 1 chart 1: Revenue (4 values), categories rewritten; "
                         f"embedded workbook {CHART_WORKBOOK} updated"}],
        }}, "chart-write")
        require_subset(chart_edit["output"], identity(outputs / "chartdeck-updated.pptx"), "chart-write.output")
        updated_deck = read_bounded(outputs / "chartdeck-updated.pptx")
        require(
            chart_cache_values(updated_deck, "ppt/charts/chart1.xml", "numCache") == ["12", "24", "36", "48"],
            "the chart's numeric cache does not hold the new values",
        )
        require(
            chart_cache_values(updated_deck, "ppt/charts/chart1.xml", "strCache", container="cat") == ["H1", "H2", "H3", "H4"],
            "the chart's category cache does not hold the new categories",
        )
        require(
            embedded_workbook_values(updated_deck, ["B2", "B3", "B4", "B5"]) == ["12", "24", "36", "48"],
            "the embedded workbook does not hold the new values",
        )
        require(
            embedded_workbook_values(updated_deck, ["A2", "A3", "A4", "A5"]) == ["H1", "H2", "H3", "H4"],
            "the embedded workbook does not hold the new categories",
        )
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "chartdeck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "chartdeck.pptx"), updated_deck, chart_edit))) as changed:
            require(original.namelist() == changed.namelist(), "the chart edit changed the package member set")
            for name in original.namelist():
                if name not in ("ppt/charts/chart1.xml", CHART_WORKBOOK):
                    require(original.read(name) == changed.read(name), "the chart edit changed an untouched package part")

        chart_refused = document_step("chart-expect-refusal", [
            "edit", "chart", "--expect-sha256", chart_hash, "--slide", "1", "--chart", "1",
            "--expect-series", "Revenue=1,2,3,4", "--set-series", "Revenue=9,9,9,9",
            "-o", f"{output_root}/chart-refused.pptx", chart_deck,
        ], "edit-receipt", inputs / "chartdeck.pptx", 1)
        require_subset(chart_refused, {"status": "refused", "output": None}, "chart-expect-refusal")
        require("does not match the expected values" in chart_refused["message"], "the chart refusal does not name the stale preimage")
        require(not path_exists(outputs / "chart-refused.pptx"), "a refused chart edit published an output")

        # ---- CSV export: one worksheet rectangle, byte for byte ----
        csv_receipt = document_step("csv-export", [
            "text", "--format", "csv", "--sheet", "Summary", "--expect-sha256", workbook_hash,
            "-o", f"{output_root}/data.csv", workbook,
        ], "text-receipt", inputs / "data.xlsx")
        require_subset(csv_receipt, {"status": "completed", "result": {
            "method": "csv-export", "format": "csv", "truncated": False, "units": ["sheet Summary A1:D1"],
        }}, "csv-export")
        require_subset(csv_receipt["output"], identity(outputs / "data.csv"), "csv-export.output")
        # The used range is one row: a shared string, a number, a boolean as TRUE, and the
        # formula's cached value, with CRLF records exactly as RFC 4180 spells them.
        require(
            read_bounded(outputs / "data.csv") == b"Client A,10,TRUE,20\r\n",
            "the CSV export is not the bytes the worksheet stores",
        )

        # ---- PPTX: a table's cells read by row and column, then one of them written ----
        table_deck = f"{input_root}/tabledeck.pptx"
        table_deck_hash = source_identities["tabledeck.pptx"]["sha256"]
        table_read = document_step("slide-table-read", [
            "text", "--format", "structure", "--slide", "1", "--expect-sha256", table_deck_hash,
            "-o", f"{output_root}/tabledeck-slide.json", table_deck,
        ], "text-receipt", inputs / "tabledeck.pptx")
        require_subset(table_read["output"], identity(outputs / "tabledeck-slide.json"), "slide-table-read.output")
        table_page = json.loads(read_bounded(outputs / "tabledeck-slide.json"))
        slides_validator.validate(table_page)
        require_subset(table_page["selection"], {"tablesComplete": True, "tables": [{
            "shapeId": 5, "rows": 3, "columns": 3,
            "cells": [
                {"row": 0, "column": 0, "text": "Region", "editable": True, "merged": False},
                {"row": 0, "column": 1, "text": "Q1", "editable": True, "merged": False},
                {"row": 0, "column": 2, "text": "Q2", "editable": True, "merged": False},
                {"row": 1, "column": 0, "text": "North", "editable": True, "merged": False},
                {"row": 1, "column": 1, "text": "20", "editable": True, "merged": False},
                {"row": 1, "column": 2, "text": "30", "editable": True, "merged": False},
                # The merge origin and the cell it swallowed are listed, reported merged and refused.
                {"row": 2, "column": 0, "text": "Total", "editable": False, "merged": True},
                {"row": 2, "column": 1, "text": "", "editable": False, "merged": True},
                {"row": 2, "column": 2, "text": "50", "editable": True, "merged": False},
            ],
        }]}, "slide-table-read")
        # Every one of those cells is still a gap the paragraph editor refuses.
        require(
            {row["text"] for row in table_page["selection"]["other"]} >= {"Region", "Q1", "30", "Total"},
            "the table paragraphs stopped being reported as gaps",
        )

        table_write = document_step("slide-table-write", [
            "edit", "slide-table", "--expect-sha256", table_deck_hash, "--slide", "1", "--shape", "5",
            "--expect", "1:2=30", "--set", "1:2=35",
            "-o", f"{output_root}/tabledeck-updated.pptx", table_deck,
        ], "edit-receipt", inputs / "tabledeck.pptx")
        require_subset(table_write, {"status": "completed", "result": {
            "engine": "pptx-tables", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "table_cell_written", "count": 1, "detail": "slide 1 table 5: 1:2"}],
        }}, "slide-table-write")
        require_subset(table_write["output"], identity(outputs / "tabledeck-updated.pptx"), "slide-table-write.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "tabledeck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "tabledeck.pptx"), read_bounded(outputs / "tabledeck-updated.pptx"), table_write))) as changed:
            require(original.namelist() == changed.namelist(), "the table write changed the package member set")
            for name in original.namelist():
                if name != "ppt/slides/slide1.xml":
                    require(original.read(name) == changed.read(name), "the table write changed an untouched package part")
            written = changed.read("ppt/slides/slide1.xml").decode("utf-8")
            require("<a:t>35</a:t>" in written, "the written cell does not hold its new text")
            require("<a:t>30</a:t>" not in written, "the old cell text survived the write")
            require(written.count("<a:tc") == original.read("ppt/slides/slide1.xml").decode("utf-8").count("<a:tc"),
                    "the table write changed the cell count")

        unmerged = document_step("slide-table-unmerge", [
            "edit", "slide-table", "--expect-sha256", table_deck_hash, "--slide", "1", "--shape", "5",
            "--unmerge", "2:0", "-o", f"{output_root}/tabledeck-unmerged.pptx", table_deck,
        ], "edit-receipt", inputs / "tabledeck.pptx")
        require_subset(unmerged, {"status": "completed", "result": {
            "engine": "pptx-tables", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "cells_unmerged", "count": 1, "detail": "slide 1 table 5: 2:0"}],
        }}, "slide-table-unmerge")
        require_subset(unmerged["output"], identity(outputs / "tabledeck-unmerged.pptx"), "slide-table-unmerge.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "tabledeck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "tabledeck.pptx"), read_bounded(outputs / "tabledeck-unmerged.pptx"), unmerged))) as freed:
            require(original.namelist() == freed.namelist(), "the unmerge changed the package member set")
            for name in original.namelist():
                if name != "ppt/slides/slide1.xml":
                    require(original.read(name) == freed.read(name), "the unmerge changed an untouched package part")
            slide_xml = freed.read("ppt/slides/slide1.xml").decode("utf-8")
            require("gridSpan" not in slide_xml, "the unmerged cell still spans two columns")
            require("hMerge" not in slide_xml, "the unmerged table still carries a continuation cell")
            require(slide_xml.count("<a:tc") == original.read("ppt/slides/slide1.xml").decode("utf-8").count("<a:tc"),
                    "the unmerge changed the cell count")

        unmerged_hash = identity(outputs / "tabledeck-unmerged.pptx")["sha256"]
        remerged = document_step("slide-table-merge", [
            "edit", "slide-table", "--expect-sha256", unmerged_hash, "--slide", "1", "--shape", "5",
            "--merge", "2:0-2:1", "-o", f"{output_root}/tabledeck-merged.pptx", f"{output_root}/tabledeck-unmerged.pptx",
        ], "edit-receipt", outputs / "tabledeck-unmerged.pptx")
        require_subset(remerged, {"status": "completed", "result": {
            "engine": "pptx-tables", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "cells_merged", "count": 1, "detail": "slide 1 table 5: 2:0-2:1"}],
        }}, "slide-table-merge")
        require_subset(remerged["output"], identity(outputs / "tabledeck-merged.pptx"), "slide-table-merge.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "tabledeck-merged.pptx"))) as joined:
            slide_xml = joined.read("ppt/slides/slide1.xml").decode("utf-8")
            require('gridSpan="2"' in slide_xml, "the merged cell does not span two columns")
            require('hMerge="1"' in slide_xml, "the merge wrote no continuation cell")
            require("rowSpan" not in slide_xml, "a horizontal merge wrote a row span nobody asked for")
            require("<a:t>Total</a:t>" in slide_xml, "the merge lost the top-left cell's text")
            require(
                [row[0] for row in TABLE_DECK_ROWS] == ["Region", "North"],
                "the deck oracle no longer reads the fixture it checks",
            )

        table_refused = document_step("slide-table-merged-refusal", [
            "edit", "slide-table", "--expect-sha256", table_deck_hash, "--slide", "1", "--shape", "5",
            "--expect", "2:0=Total", "--set", "2:0=Sum",
            "-o", f"{output_root}/tabledeck-refused.pptx", table_deck,
        ], "edit-receipt", inputs / "tabledeck.pptx", 1)
        require_subset(table_refused, {"status": "refused", "code": "parser_refused", "output": None}, "slide-table-merged-refusal")
        require("is part of a merge" in table_refused["message"], "the merged-cell refusal does not say why")
        require(not path_exists(outputs / "tabledeck-refused.pptx"), "a refused table write published an output")

        # ---- XLSX: a chart created from named ranges, a second one beside it, and the inventory ----
        chart_data = f"{input_root}/chartdata.xlsx"
        chart_data_hash = source_identities["chartdata.xlsx"]["sha256"]
        created = document_step("chart-create", [
            "edit", "chart-create", "--expect-sha256", chart_data_hash, "--type", "bar",
            "--categories", "A2:A5", "--series", "Revenue=B2:B5", "--series", "Cost=C2:C5",
            "--title", "Revenue", "-o", f"{output_root}/charted.xlsx", chart_data,
        ], "edit-receipt", inputs / "chartdata.xlsx")
        require_subset(created, {"status": "completed", "result": {
            "engine": "xlsx-charts", "skipped": [], "outputExtension": "xlsx",
            "applied": [{
                "kind": "chart_created", "count": 1,
                "detail": "Quarters: bar chart 'Revenue' over A2:A5 with 2 series at E2",
            }],
        }}, "chart-create")
        require_subset(created["output"], identity(outputs / "charted.xlsx"), "chart-create.output")
        charted_bytes = read_bounded(outputs / "charted.xlsx")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "chartdata.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "chartdata.xlsx"), charted_bytes, created))) as changed:
            added = [name for name in changed.namelist() if name not in original.namelist()]
            require(
                sorted(added) == ["xl/charts/chart1.xml", "xl/drawings/_rels/drawing1.xml.rels", "xl/drawings/drawing1.xml", "xl/worksheets/_rels/sheet1.xml.rels"],
                f"the chart created an unexpected part set: {sorted(added)}",
            )
            for name in original.namelist():
                if name not in ("[Content_Types].xml", "xl/worksheets/sheet1.xml"):
                    require(original.read(name) == changed.read(name), "creating a chart changed an untouched package part")
            chart_part = changed.read("xl/charts/chart1.xml").decode("utf-8")
            # The live references point at the named ranges and the caches hold the cells' own values.
            for formula in ("'Quarters'!$A$2:$A$5", "'Quarters'!$B$2:$B$5", "'Quarters'!$C$2:$C$5"):
                require(f"<c:f>{formula}</c:f>" in chart_part, f"the chart part lost the reference {formula}")
            require(chart_part.count("<c:ser>") == 2, "the chart part does not hold exactly two series")
            require(
                chart_cache_values(charted_bytes, "xl/charts/chart1.xml", "numCache") == [str(row[1]) for row in CHART_DATA_ROWS],
                "the chart cache does not hold the revenue the worksheet stores",
            )
            require(
                chart_cache_values(charted_bytes, "xl/charts/chart1.xml", "strCache", container="cat") == [row[0] for row in CHART_DATA_ROWS],
                "the chart category cache does not hold the worksheet labels",
            )
            drawing_rels = changed.read("xl/drawings/_rels/drawing1.xml.rels").decode("utf-8")
            require("../charts/chart1.xml" in drawing_rels, "the drawing does not point at the chart part")
            require("<drawing r:id=" in changed.read("xl/worksheets/sheet1.xml").decode("utf-8"), "the worksheet does not point at its drawing")
            overrides = content_type_overrides(changed)
            require({"/xl/charts/chart1.xml", "/xl/drawings/drawing1.xml"} <= overrides, "the new parts have no content types")

        charted_hash = identity(outputs / "charted.xlsx")["sha256"]
        second = document_step("chart-create-second", [
            "edit", "chart-create", "--expect-sha256", charted_hash, "--type", "line",
            "--categories", "A2:A5", "--series", "Cost=C2:C5", "--at", "E20", "--size", "320x200",
            "-o", f"{output_root}/charted-twice.xlsx", f"{output_root}/charted.xlsx",
        ], "edit-receipt", outputs / "charted.xlsx")
        require_subset(second, {"status": "completed", "result": {"engine": "xlsx-charts", "applied": [{
            "kind": "chart_created", "count": 1, "detail": "Quarters: line chart over A2:A5 with 1 series at E20",
        }]}}, "chart-create-second")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "charted-twice.xlsx"))) as twice:
            require("xl/charts/chart2.xml" in twice.namelist(), "the second chart did not get its own part")
            drawing = twice.read("xl/drawings/drawing1.xml").decode("utf-8")
            require(drawing.count("<xdr:twoCellAnchor") == 2, "the second chart did not join the sheet's existing drawing")
            require("<c:lineChart>" in twice.read("xl/charts/chart2.xml").decode("utf-8"), "the second chart is not a line chart")

        chart_inventory = document_step("chart-inventory", [
            "text", "--format", "structure", "--expect-sha256", identity(outputs / "charted-twice.xlsx")["sha256"],
            "-o", f"{output_root}/charted-sheets.json", f"{output_root}/charted-twice.xlsx",
        ], "text-receipt", outputs / "charted-twice.xlsx")
        require_subset(chart_inventory["output"], identity(outputs / "charted-sheets.json"), "chart-inventory.output")
        charted_sheets = json.loads(read_bounded(outputs / "charted-sheets.json"))
        cells_validator.validate(charted_sheets)
        require_subset(charted_sheets, {"sheets": [{
            "index": 0, "name": "Quarters", "part": "xl/worksheets/sheet1.xml", "visibility": "visible",
            "dimension": "A1:C5", "dimensionSource": "declared", "chartsComplete": True,
            "charts": [
                {"index": 1, "part": "xl/charts/chart1.xml", "title": "Revenue", "type": "bar", "series": ["Revenue", "Cost"]},
                {"index": 2, "part": "xl/charts/chart2.xml", "title": None, "type": "line", "series": ["Cost"]},
            ],
            "validations": [], "validationsComplete": True,
            "conditionalFormats": [], "conditionalFormatsComplete": True,
        }]}, "chart-inventory")

        # ---- XLSX: the created chart updated, caches and the cells its references name ----
        twice_hash = identity(outputs / "charted-twice.xlsx")["sha256"]
        chart_written = document_step("chart-sheet-write", [
            "edit", "chart", "--expect-sha256", twice_hash, "--sheet", "Quarters", "--chart", "1",
            "--expect-series", "Revenue=10,20,30,40", "--set-series", "Revenue=12,24,36,48",
            "-o", f"{output_root}/charted-updated.xlsx", f"{output_root}/charted-twice.xlsx",
        ], "edit-receipt", outputs / "charted-twice.xlsx")
        require_subset(chart_written, {"status": "completed", "result": {
            "engine": "xlsx-charts", "skipped": [], "outputExtension": "xlsx",
            "applied": [{
                "kind": "chart_series_written", "count": 1,
                "detail": "Quarters chart 1: Revenue (4 values), categories unchanged; 4 worksheet cells written",
            }],
        }}, "chart-sheet-write")
        require_subset(chart_written["output"], identity(outputs / "charted-updated.xlsx"), "chart-sheet-write.output")
        updated_book = read_bounded(outputs / "charted-updated.xlsx")
        require(
            chart_cache_values(updated_book, "xl/charts/chart1.xml", "numCache") == ["12", "24", "36", "48"],
            "the worksheet chart's cache does not hold the new values",
        )
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "charted-twice.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(outputs / "charted-twice.xlsx"), updated_book, chart_written))) as changed:
            require(original.namelist() == changed.namelist(), "the worksheet chart edit changed the package member set")
            for name in original.namelist():
                if name not in ("xl/charts/chart1.xml", "xl/worksheets/sheet1.xml"):
                    require(original.read(name) == changed.read(name), "the worksheet chart edit changed an untouched package part")
            sheet = ET.fromstring(changed.read("xl/worksheets/sheet1.xml"))
            written_cells = worksheet_cells(sheet, [])
            require(
                [written_cells[f"B{row}"] for row in range(2, 6)] == [("number", value) for value in ("12", "24", "36", "48")],
                "the cells the chart references do not hold the values the chart now draws",
            )
            require(
                [written_cells[f"C{row}"][1] for row in range(2, 6)] == [str(row[2]) for row in CHART_DATA_ROWS],
                "the worksheet chart edit changed a cell no series referenced",
            )
        stale_chart = document_step("chart-sheet-refusal", [
            "edit", "chart", "--expect-sha256", twice_hash, "--sheet", "Quarters", "--chart", "1",
            "--expect-series", "Revenue=1,2,3,4", "--set-series", "Revenue=5,6,7,8",
            "-o", f"{output_root}/charted-refused.xlsx", f"{output_root}/charted-twice.xlsx",
        ], "edit-receipt", outputs / "charted-twice.xlsx", 1)
        require_subset(stale_chart, {"status": "refused", "output": None}, "chart-sheet-refusal")
        require("does not match the expected values" in stale_chart["message"], "the worksheet chart refusal does not name the stale preimage")
        require(not path_exists(outputs / "charted-refused.xlsx"), "a refused worksheet chart edit published an output")

        # ---- PPTX: a chart created on a slide, then updated through its embedded workbook ----
        deck_source = f"{input_root}/deck.pptx"
        deck_hash = source_identities["deck.pptx"]["sha256"]
        slide_workbook = "ppt/embeddings/Microsoft_Excel_Worksheet_1.xlsx"
        slide_chart = document_step("slide-chart-create", [
            "edit", "chart-create", "--expect-sha256", deck_hash, "--slide", "1", "--type", "bar",
            "--categories", ",".join(CHART_CATEGORIES), "--series", "Revenue=" + ",".join(CHART_VALUES),
            "--series", "Cost=4,6,9,11", "--title", "Quarterly revenue",
            "-o", f"{output_root}/deck-charted.pptx", deck_source,
        ], "edit-receipt", inputs / "deck.pptx")
        require_subset(slide_chart, {"status": "completed", "result": {
            "engine": "pptx-charts", "skipped": [], "outputExtension": "pptx",
            "applied": [{
                "kind": "chart_created", "count": 1,
                "detail": "slide 1: bar chart 'Quarterly revenue' over 4 categories with 2 series "
                          "at 3048000,1714500 sized 6096000x3429000; "
                          f"embedded workbook {slide_workbook} written",
            }],
        }}, "slide-chart-create")
        require_subset(slide_chart["output"], identity(outputs / "deck-charted.pptx"), "slide-chart-create.output")
        charted_deck = read_bounded(outputs / "deck-charted.pptx")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "deck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "deck.pptx"), charted_deck, slide_chart))) as changed:
            added = set(changed.namelist()) - set(original.namelist())
            require(
                sorted(added) == [
                    "ppt/charts/_rels/chart1.xml.rels", "ppt/charts/chart1.xml", slide_workbook,
                ],
                f"the slide chart created an unexpected part set: {sorted(added)}",
            )
            # The slide, its relationships and the content types name the new chart; nothing else moves.
            touched = ("ppt/slides/slide1.xml", "ppt/slides/_rels/slide1.xml.rels", "[Content_Types].xml")
            for name in original.namelist():
                if name not in touched:
                    require(original.read(name) == changed.read(name), "creating a slide chart changed an untouched package part")
            require(
                "../charts/chart1.xml" in changed.read("ppt/slides/_rels/slide1.xml.rels").decode("utf-8"),
                "the slide does not relate to the chart part",
            )
            require(
                "notesSlide1.xml" in changed.read("ppt/slides/_rels/slide1.xml.rels").decode("utf-8"),
                "the slide lost a relationship it already had",
            )
            chart_part = changed.read("ppt/charts/chart1.xml").decode("utf-8")
            for formula in ("'Sheet1'!$A$2:$A$5", "'Sheet1'!$B$2:$B$5", "'Sheet1'!$C$2:$C$5", "'Sheet1'!$B$1"):
                require(f"<c:f>{formula}</c:f>" in chart_part, f"the created chart part lost the reference {formula}")
            require("<c:externalData" in chart_part, "the created chart part does not link its embedded workbook")
            slide_xml = changed.read("ppt/slides/slide1.xml").decode("utf-8")
            require('<a:off x="3048000" y="1714500"/>' in slide_xml, "the created chart frame is not where the default places it")
            require('<a:ext cx="6096000" cy="3429000"/>' in slide_xml, "the created chart frame is not half the slide")
            require(slide_xml.count("graphicFrame") == 2, "the slide does not hold exactly one new graphic frame")
            require(slide_shape_ids(changed, "ppt/slides/slide1.xml") == [2, 3, 4], "the created chart did not take a fresh shape id")
            overrides = content_type_overrides(changed)
            require("/ppt/charts/chart1.xml" in overrides, "the created chart part has no content type")
        require(
            chart_cache_values(charted_deck, "ppt/charts/chart1.xml", "numCache") == CHART_VALUES,
            "the created chart's cache does not hold the values that were given",
        )
        require(
            chart_cache_values(charted_deck, "ppt/charts/chart1.xml", "strCache", container="cat") == CHART_CATEGORIES,
            "the created chart's category cache does not hold the labels that were given",
        )
        require(
            embedded_workbook_values(charted_deck, ["B1", "C1"], slide_workbook) == ["Revenue", "Cost"],
            "the created embedded workbook does not name each series",
        )
        require(
            embedded_workbook_values(charted_deck, ["A2", "A3", "A4", "A5"], slide_workbook) == CHART_CATEGORIES,
            "the created embedded workbook does not hold the categories the chart references",
        )
        require(
            embedded_workbook_values(charted_deck, ["B2", "B3", "B4", "B5"], slide_workbook) == CHART_VALUES,
            "the created embedded workbook does not hold the values the chart references",
        )
        # The chart the creator wrote is the chart the editor then writes through, caches and
        # workbook cells together, which is the whole point of writing both pictures of the data.
        charted_deck_hash = identity(outputs / "deck-charted.pptx")["sha256"]
        slide_chart_updated = document_step("slide-chart-write", [
            "edit", "chart", "--expect-sha256", charted_deck_hash, "--slide", "1", "--chart", "1",
            "--expect-series", "Revenue=" + ",".join(CHART_VALUES), "--set-series", "Revenue=12,24,36,48",
            "-o", f"{output_root}/deck-charted-updated.pptx", f"{output_root}/deck-charted.pptx",
        ], "edit-receipt", outputs / "deck-charted.pptx")
        require_subset(slide_chart_updated, {"status": "completed", "result": {
            "engine": "pptx-charts", "skipped": [],
            "applied": [{
                "kind": "chart_series_written", "count": 1,
                "detail": "slide 1 chart 1: Revenue (4 values), categories unchanged; "
                          f"embedded workbook {slide_workbook} updated",
            }],
        }}, "slide-chart-write")
        synced_deck = read_bounded(outputs / "deck-charted-updated.pptx")
        require(
            chart_cache_values(synced_deck, "ppt/charts/chart1.xml", "numCache") == ["12", "24", "36", "48"],
            "the created chart's cache was not updated",
        )
        require(
            embedded_workbook_values(synced_deck, ["B2", "B3", "B4", "B5"], slide_workbook) == ["12", "24", "36", "48"],
            "the created chart's embedded workbook was not updated with its caches",
        )
        require(
            embedded_workbook_values(synced_deck, ["C2", "C3", "C4", "C5"], slide_workbook) == ["4", "6", "9", "11"],
            "the update changed a series it did not name",
        )

        # ---- PPTX: a text box added to the chart deck, then deleted again under its own guard ----
        box_text = "Added by the evaluation"
        boxed = document_step("shape-add", [
            "edit", "shapes", "--expect-sha256", chart_hash, "--slide", "1", "--add-text", box_text,
            "-o", f"{output_root}/chartdeck-boxed.pptx", chart_deck,
        ], "edit-receipt", inputs / "chartdeck.pptx")
        require_subset(boxed, {"status": "completed", "result": {
            "engine": "pptx-shapes", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "shape_added", "count": 1, "detail": "slide 1: text box 4 at 3048000,3246120 sized 3048000x365760 EMU"}],
        }}, "shape-add")
        require_subset(boxed["output"], identity(outputs / "chartdeck-boxed.pptx"), "shape-add.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "chartdeck.pptx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "chartdeck.pptx"), read_bounded(outputs / "chartdeck-boxed.pptx"), boxed))) as changed:
            require(original.namelist() == changed.namelist(), "the text box changed the package member set")
            for name in original.namelist():
                if name != "ppt/slides/slide1.xml":
                    require(original.read(name) == changed.read(name), "the text box changed an untouched package part")
            require(slide_shape_ids(original, "ppt/slides/slide1.xml") == [2, 3], "the chart deck slide does not hold exactly its title and its chart")
            require(slide_shape_ids(changed, "ppt/slides/slide1.xml") == [2, 3, 4], "the text box did not land in the shape tree with a fresh id")
            require(
                paragraph_texts(changed, "ppt/slides/slide1.xml", _DML) == ["Quarterly review", box_text],
                "the added text box does not hold exactly the requested text",
            )
        boxed_hash = identity(outputs / "chartdeck-boxed.pptx")["sha256"]
        trimmed = document_step("shape-delete", [
            "edit", "shapes", "--expect-sha256", boxed_hash, "--slide", "1",
            "--delete-shape", "4", "--expect-shape", f"4={box_text}",
            "-o", f"{output_root}/chartdeck-trimmed.pptx", f"{output_root}/chartdeck-boxed.pptx",
        ], "edit-receipt", outputs / "chartdeck-boxed.pptx")
        require_subset(trimmed, {"status": "completed", "result": {
            "engine": "pptx-shapes", "skipped": [], "outputExtension": "pptx",
            "applied": [{"kind": "shape_deleted", "count": 1, "detail": "slide 1: shape 4 (text, TextBox 4); shapes now 2"}],
        }}, "shape-delete")
        require_subset(trimmed["output"], identity(outputs / "chartdeck-trimmed.pptx"), "shape-delete.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "chartdeck-trimmed.pptx"))) as changed:
            require(slide_shape_ids(changed, "ppt/slides/slide1.xml") == [2, 3], "the delete did not take the text box back out of the shape tree")
            require(
                paragraph_texts(changed, "ppt/slides/slide1.xml", _DML) == ["Quarterly review"],
                "the delete did not take the added text with it",
            )
        chart_guard = document_step("shape-chart-guard", [
            "edit", "shapes", "--expect-sha256", chart_hash, "--slide", "1",
            "--delete-shape", "3", "--expect-shape", "3=Chart 3",
            "-o", f"{output_root}/chartdeck-degraded.pptx", chart_deck,
        ], "edit-receipt", inputs / "chartdeck.pptx", 1)
        require_subset(chart_guard, {"status": "refused", "output": None}, "shape-chart-guard")
        require("--expect-shape 3=chart" in chart_guard["message"], "the refusal does not ask for the chart frame to be named by its kind")
        require(not path_exists(outputs / "chartdeck-degraded.pptx"), "a refused shape delete published an output")

        refused = document_step("image-outside", ["edit", "image", "--crop", "6,0,4,4", "-o", f"{output_root}/image-outside.png", photo], "edit-receipt", inputs / "photo.png", 1)
        require_subset(refused, {"status": "refused", "output": None}, "image-outside")
        require(not path_exists(outputs / "image-outside.png"), "a refused image edit published an output")

        # ---- Text: a line window and a guarded whole-line write on the plain-text fixture ----
        lines_validator = validator_for("com.krauq.ufo.text-lines", 1)
        require(lines_validator is not None, "text-lines schema is missing")
        readme = f"{input_root}/readme.txt"
        readme_hash = source_identities["readme.txt"]["sha256"]
        readme_line = sample_files()["readme.txt"].decode("utf-8").rstrip("\n")
        window_receipt = document_step("lines-window", ["text", "--format", "structure", "--lines", "1", "--expect-sha256", readme_hash, "-o", f"{output_root}/lines-window.json", readme], "text-receipt", inputs / "readme.txt")
        require_subset(window_receipt, {"status": "completed", "result": {"method": "text-lines"}}, "lines-window")
        window = json.loads(read_bounded(outputs / "lines-window.json").decode("utf-8"))
        lines_validator.validate(window)
        require_subset(window, {"encoding": "utf-8", "lineEnding": "lf", "lineCount": 1, "finalLineEnding": True, "nextLine": None, "selection": {"lines": "1", "items": [{"line": 1, "text": readme_line, "truncated": False}]}}, "lines-window")
        new_line = "Synthetic text now carries a guarded line write."
        line_edit = document_step("lines-write", ["edit", "lines", "--expect-sha256", readme_hash, "--expect", f"1={readme_line}", "--set", f"1={new_line}", "-o", f"{output_root}/readme-updated.txt", readme], "edit-receipt", inputs / "readme.txt")
        require_subset(line_edit, {"status": "completed", "result": {"engine": "text-lines"}}, "lines-write")
        require_subset(line_edit["output"], identity(outputs / "readme-updated.txt"), "lines-write.output")
        require(read_bounded(outputs / "readme-updated.txt") == (new_line + "\n").encode("utf-8"), "the line write did not produce exactly the new line with its ending")
        refused = document_step("lines-preimage", ["edit", "lines", "--expect-sha256", readme_hash, "--expect", f"1={new_line}", "--set", "1=x", "-o", f"{output_root}/lines-preimage.txt", readme], "edit-receipt", inputs / "readme.txt", 1)
        require_subset(refused, {"status": "refused", "output": None}, "lines-preimage")
        require(not path_exists(outputs / "lines-preimage.txt"), "a refused line batch published an output")

        # ---- PDF: page inventory, selected-page text and a selected-page copy checked against its streams ----
        pages_validator = validator_for("com.krauq.ufo.pdf-pages", 1)
        require(pages_validator is not None, "pdf-pages schema is missing")
        report_pdf = f"{input_root}/report.pdf"
        report_hash = source_identities["report.pdf"]["sha256"]
        pdf_inventory_receipt = document_step("pdf-inventory", ["text", "--format", "structure", "-o", f"{output_root}/pdf-inventory.json", report_pdf], "text-receipt", inputs / "report.pdf")
        require_subset(pdf_inventory_receipt, {"status": "completed", "result": {"method": "pdf-pages"}}, "pdf-inventory")
        pdf_inventory = json.loads(read_bounded(outputs / "pdf-inventory.json").decode("utf-8"))
        pages_validator.validate(pdf_inventory)
        require_subset(pdf_inventory, {"source": {"sha256": report_hash}, "pageCount": 3, "selection": None, "partial": False}, "pdf-inventory")
        require([page["textless"] for page in pdf_inventory["pages"]] == [False, True, False], "page inventory did not mark the textless page only")
        pdf_read_receipt = document_step("pdf-page-read", ["text", "--format", "structure", "--pages", "3", "--expect-sha256", report_hash, "-o", f"{output_root}/pdf-page-3.json", report_pdf], "text-receipt", inputs / "report.pdf")
        require_subset(pdf_read_receipt, {"status": "completed", "result": {"method": "pdf-pages"}}, "pdf-page-read")
        pdf_page = json.loads(read_bounded(outputs / "pdf-page-3.json").decode("utf-8"))
        pages_validator.validate(pdf_page)
        items = pdf_page["selection"]["items"]
        require(len(items) == 1 and items[0]["index"] == 3 and items[0]["text"].strip() == PDF_PAGE_TEXTS[2] and not items[0]["truncated"], "selected page read did not return page three's text")
        kept = document_step("pdf-keep", ["edit", "pages", "--keep", "3", "-o", f"{output_root}/appendix-3.pdf", report_pdf], "edit-receipt", inputs / "report.pdf")
        require_subset(kept, {"status": "completed", "result": {"engine": "pdf-pages"}}, "pdf-keep")
        require_subset(kept["output"], identity(outputs / "appendix-3.pdf"), "pdf-keep.output")
        # The page editor appends a revision, so the original bytes stay in the file; the
        # oracle follows the newest xref to the current page tree instead of grepping.
        current_pages = pdf_current_page_texts(read_bounded(outputs / "appendix-3.pdf"))
        require(len(current_pages) == 1 and PDF_PAGE_TEXTS[2].encode() in current_pages[0] and PDF_PAGE_TEXTS[0].encode() not in current_pages[0], "the selected-page copy's current page tree does not hold exactly page three")
        source_pages = pdf_current_page_texts(read_bounded(inputs / "report.pdf"))
        require([PDF_PAGE_TEXTS[i].encode() in source_pages[i] for i in range(3)] == [True, True, True] and len(source_pages) == 3, "the oracle does not read the source page tree")
        page_three = items[0]["paragraphs"]
        require(
            [row["index"] for row in page_three] == [0] and page_three[0]["text"] == PDF_PAGE_TEXTS[2],
            "the selected page does not list its one visual paragraph",
        )
        require(page_three[0]["editable"] and page_three[0]["limitation"] is None, "the page's paragraph is not reported as editable")
        replacement = "Page three ends the appendix"
        rewritten = document_step("pdf-text-write", [
            "edit", "pdf-text", "--expect-sha256", report_hash, "--page", "3",
            "--expect", f"0={PDF_PAGE_TEXTS[2]}", "--set", f"0={replacement}",
            "-o", f"{output_root}/appendix-text.pdf", report_pdf,
        ], "edit-receipt", inputs / "report.pdf")
        require_subset(rewritten, {"status": "completed", "result": {
            "engine": "pdf-text", "outputExtension": None,
            "applied": [{"kind": "pdf_paragraph_written", "count": 1, "detail": "page 3: paragraphs 0"}],
        }}, "pdf-text-write")
        require_subset(rewritten["output"], identity(outputs / "appendix-text.pdf"), "pdf-text-write.output")
        edited_pdf = read_bounded(outputs / "appendix-text.pdf")
        require(edited_pdf[:len(read_bounded(inputs / "report.pdf"))] == read_bounded(inputs / "report.pdf"), "the in-place text edit did not keep the source as the first revision")
        rewritten_pages = pdf_current_page_texts(edited_pdf)
        require(len(rewritten_pages) == 3, "the edited PDF no longer has three pages")
        require(replacement.encode() in rewritten_pages[2], "the newest revision's page three does not hold the new text")
        require(PDF_PAGE_TEXTS[2].encode() not in rewritten_pages[2], "the newest revision's page three still holds the old text")
        require(PDF_PAGE_TEXTS[0].encode() in rewritten_pages[0], "the in-place text edit changed another page")
        refused = document_step("pdf-text-too-long", [
            "edit", "pdf-text", "--expect-sha256", report_hash, "--page", "3",
            "--expect", f"0={PDF_PAGE_TEXTS[2]}", "--set", "0=" + "Page three closes the appendix and keeps on going " * 12,
            "-o", f"{output_root}/pdf-text-too-long.pdf", report_pdf,
        ], "edit-receipt", inputs / "report.pdf", 1)
        require_subset(refused, {"status": "refused", "output": None}, "pdf-text-too-long")
        require(not path_exists(outputs / "pdf-text-too-long.pdf"), "a refused PDF text edit published an output")
        refused = document_step("pdf-page-outside", ["text", "--format", "structure", "--pages", "4", "-o", f"{output_root}/pdf-page-4.json", report_pdf], "text-receipt", inputs / "report.pdf", 1)
        require_subset(refused, {"status": "refused", "output": None}, "pdf-page-outside")
        require(not path_exists(outputs / "pdf-page-4.json"), "a refused page read published an output")

        # ---- find: the coordinates the edit verbs consume, before any edit is planned ----
        find_validator = validator_for("com.krauq.ufo.find-results", 1)
        require(find_validator is not None, "find-results schema is missing")
        docx_find = document_step("find-docx", [
            "find", "--text", "Visible text", "--expect-sha256", source_hash,
            "-o", f"{output_root}/find-report.json", source,
        ], "text-receipt", inputs / "report.docx")
        require_subset(docx_find, {"status": "completed", "result": {"method": "find-results"}}, "find-docx")
        require_subset(docx_find["output"], identity(outputs / "find-report.json"), "find-docx.output")
        docx_hits = json.loads(read_bounded(outputs / "find-report.json"))
        find_validator.validate(docx_hits)
        require_subset(docx_hits, {
            "source": {"name": "report.docx", "sha256": source_hash},
            "needle": {"text": "Visible text", "ignoreCase": False, "regex": False},
            "total": 1, "truncated": False,
            "hits": [{
                "kind": "paragraph", "story": "main", "index": 1, "table": None, "offset": 0,
                "length": len("Visible text"), "snippet": SAMPLE_TEXT.split("\n")[1], "editable": True,
            }],
        }, "find-docx")
        xlsx_find = document_step("find-xlsx", [
            "find", "--text", "client a", "--ignore-case", "--expect-sha256", workbook_hash,
            "-o", f"{output_root}/find-data.json", workbook,
        ], "text-receipt", inputs / "data.xlsx")
        require_subset(xlsx_find, {"status": "completed", "result": {"method": "find-results"}}, "find-xlsx")
        xlsx_hits = json.loads(read_bounded(outputs / "find-data.json"))
        find_validator.validate(xlsx_hits)
        require_subset(xlsx_hits, {
            "source": {"name": "data.xlsx", "sha256": workbook_hash},
            "needle": {"text": "client a", "ignoreCase": True, "regex": False},
            "total": 1, "truncated": False,
            "hits": [{
                "kind": "cell", "sheet": "Summary", "ref": "A1", "row": 1, "column": 1,
                "type": "string", "snippet": "Client A", "editable": True,
            }],
        }, "find-xlsx")
        # The hit is exactly the coordinate the earlier guarded cell batch wrote to.
        require(xlsx_hits["hits"][0]["ref"] == cell_page["selection"]["cells"][0]["ref"], "the find hit is not the cell the range read reports")

        # ---- render: a slide and a worksheet as pictures, on the surface that carries the renderer ----
        # The app image bundles PDFBox, Compose, Skiko and fonts; the restricted
        # container omits them on purpose, so the same two commands complete on
        # one surface and refuse on the other, and neither surface guesses.
        for label, name, source_name, arguments in (
            ("render-pptx", "slide.png", "deck.pptx", ["--page", "1", "--width", "400"]),
            ("render-xlsx", "sheet.png", "data.xlsx", ["--width", "400"]),
        ):
            rendered = document_step(
                label, ["render", "-o", f"{output_root}/{name}", *arguments, "--", f"{input_root}/{source_name}"],
                "render-receipt", inputs / source_name, 0 if launcher else 1,
            )
            if launcher:
                require_subset(rendered, {"status": "completed", "result": {"page": 1, "width": 400, "partial": False}}, label)
                require(rendered["result"]["engine"].startswith("compose-"), f"{label} did not name a Compose engine")
                require_subset(rendered["output"], identity(outputs / name), label + ".output")
                width, _, rows = decode_png(read_bounded(outputs / name), max_side=MAX_RENDER_SIDE)
                require(width == 400, f"{label} did not produce a 400 pixel wide picture")
                require(len({pixel for row in rows for pixel in row}) > 1, f"{label} produced a blank picture")
            else:
                require_subset(rendered, {"status": "refused", "code": "parser_refused", "output": None}, label)
                require("rendering" in rendered["message"], "the container's render refusal does not say what is missing")
                require(not path_exists(outputs / name), f"{label} published a picture on a surface that cannot render")

        # ---- convert: a document, a deck and a workbook to PDF, on the surface that renders ----
        # Same rule as render: the app image draws these pages, the restricted
        # container refuses. The oracle proves the produced PDF itself, page by
        # page, rather than believing the receipt's own count.
        for label, name, source_name, arguments, expected_pages, needle in (
            ("convert-docx", "report-converted.pdf", "report.docx", [], 1, b"Visible text survives"),
            ("convert-pptx", "deck-converted.pdf", "deck.pptx", ["--pages", "1", "--dpi", "96"], 1, b"Q3 review"),
            ("convert-xlsx", "data-converted.pdf", "data.xlsx", ["--dpi", "96"], 1, b"Client A"),
        ):
            converted = document_step(
                label,
                ["convert", "-o", f"{output_root}/{name}", "--to", "pdf", *arguments, "--", f"{input_root}/{source_name}"],
                "convert-receipt", inputs / source_name, 0 if launcher else 1,
            )
            if launcher:
                require_subset(converted, {"status": "completed", "result": {"pages": expected_pages, "raster": True}}, label)
                require(converted["result"]["engine"].endswith("-raster"), f"{label} did not name a raster engine")
                require(converted["result"]["dpi"] in (96, 150), f"{label} did not report the density it drew at")
                require_subset(converted["output"], identity(outputs / name), label + ".output")
                produced = read_bounded(outputs / name)
                texts = pdf_current_page_texts(produced)
                require(len(texts) == expected_pages, f"{label} did not produce {expected_pages} PDF page(s)")
                # Every page is a picture of the rendered page, so every page draws an image
                # XObject, and the invisible text layer keeps the source's own words findable.
                require(
                    all(count >= 1 for count in pdf_current_page_images(produced)),
                    f"{label} produced a page with no image",
                )
                require(all(b"/Im0 Do" in text for text in texts), f"{label} produced a page that draws nothing")
                require(needle in b"\n".join(texts), f"{label} lost the source text from the searchable layer")
            else:
                require_subset(converted, {"status": "refused", "code": "parser_refused", "output": None}, label)
                require("rendering" in converted["message"], "the container's convert refusal does not say what is missing")
                require(not path_exists(outputs / name), f"{label} published a PDF on a surface that cannot render")

        # ---- DOCX: a header story reads and writes its own part, and nothing else ----
        letter = f"{input_root}/letter.docx"
        letter_hash = source_identities["letter.docx"]["sha256"]
        header_receipt = document_step("header-read", [
            "text", "--format", "structure", "--story", "header:1", "--expect-sha256", letter_hash,
            "-o", f"{output_root}/letter-header.json", letter,
        ], "text-receipt", inputs / "letter.docx")
        require_subset(header_receipt["output"], identity(outputs / "letter-header.json"), "header-read.output")
        header_page = json.loads(read_bounded(outputs / "letter-header.json"))
        paragraph_validator.validate(header_page)
        require_subset(header_page, {
            "story": "header:1", "part": "word/header1.xml", "offset": 0, "totalParagraphs": 1,
            "nextOffset": None, "partial": False, "outline": [], "outlineComplete": True,
            "comments": [], "commentsComplete": True,
            "paragraphs": [{
                "index": 0, "text": LETTER_HEADER_TEXT, "inTable": False, "table": None,
                "style": None, "limitation": None, "links": [],
            }],
        }, "header-read")
        header_edit = document_step("header-set", [
            "edit", "paragraphs", "--expect-sha256", letter_hash, "--story", "header:1",
            "--expect", f"0={LETTER_HEADER_TEXT}", "--set", "0=Annual report header",
            "-o", f"{output_root}/letter-header.docx", letter,
        ], "edit-receipt", inputs / "letter.docx")
        require_subset(header_edit, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "header-set")
        require([item["kind"] for item in header_edit["result"]["applied"]] == ["paragraph_written"], "the header batch did not report paragraph_written")
        require(header_edit["result"]["applied"][0]["detail"].startswith("header:1: "), "the header receipt does not name the story it wrote")
        require_subset(header_edit["output"], identity(outputs / "letter-header.docx"), "header-set.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "letter.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "letter.docx"), read_bounded(outputs / "letter-header.docx"), header_edit))) as changed:
            require(original.namelist() == changed.namelist(), "the header edit changed the package member set/order")
            for entry in original.namelist():
                if entry != "word/header1.xml":
                    require(original.read(entry) == changed.read(entry), "the header edit changed a part outside the header story")
            require(paragraph_texts(changed, "word/header1.xml", W) == ["Annual report header"], "the header part does not hold the new text")
            require(LETTER_HEADER_TEXT.encode() not in changed.read("word/header1.xml"), "the header part still holds the old text")
            require(paragraph_texts(changed, "word/document.xml", W) == [LETTER_BODY_TEXT], "the header edit changed the body")

        # ---- DOCX: the planted comment, inventoried, then resolved, then deleted ----
        commented = f"{output_root}/commented.docx"
        commented_identity = identity(outputs / "commented.docx")
        comment_receipt = document_step("comment-inventory", [
            "text", "--format", "structure", "--expect-sha256", commented_identity["sha256"],
            "-o", f"{output_root}/commented-page.json", commented,
        ], "text-receipt", outputs / "commented.docx")
        require_subset(comment_receipt["output"], identity(outputs / "commented-page.json"), "comment-inventory.output")
        comment_page = json.loads(read_bounded(outputs / "commented-page.json"))
        paragraph_validator.validate(comment_page)
        require(comment_page["commentsComplete"], "the comment inventory says it is incomplete")
        require(len(comment_page["comments"]) == 1, "the inventory does not hold exactly the planted comment")
        planted = comment_page["comments"][0]
        require_subset(planted, {
            "author": "Sample reviewer", "text": comment_text, "paragraph": 1, "resolved": False, "parent": None,
        }, "comment-inventory.comments")
        comment_id = planted["id"]
        require(comment_id.isdigit(), "the comment id is not the id the edit verbs take")

        resolved = document_step("comment-resolve", [
            "edit", "paragraphs", "--expect-sha256", commented_identity["sha256"],
            "--resolve-comment", comment_id, "-o", f"{output_root}/resolved.docx", commented,
        ], "edit-receipt", outputs / "commented.docx")
        require_subset(resolved, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "comment-resolve")
        require([item["kind"] for item in resolved["result"]["applied"]] == ["comment_resolved"], "the resolve batch did not report comment_resolved")
        require_subset(resolved["output"], identity(outputs / "resolved.docx"), "comment-resolve.output")
        W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "commented.docx"))) as before_resolve, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(outputs / "commented.docx"), read_bounded(outputs / "resolved.docx"), resolved))) as after_resolve:
            gained = {"word/commentsExtended.xml"}
            rewritten = {"[Content_Types].xml", "word/_rels/document.xml.rels", "word/comments.xml"}
            require(set(after_resolve.namelist()) == set(before_resolve.namelist()) | gained, "resolving changed the package member set")
            for entry in before_resolve.namelist():
                if entry not in rewritten:
                    require(before_resolve.read(entry) == after_resolve.read(entry), "resolving changed an untouched package part")
            extended = ET.fromstring(after_resolve.read("word/commentsExtended.xml"))
            entries = extended.findall(f"{{{W15}}}commentEx")
            require(len(entries) == 1 and entries[0].get(f"{{{W15}}}done") == "1", "commentsExtended does not mark exactly one comment done")
            key = entries[0].get(f"{{{W15}}}paraId")
            W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
            keyed = ET.fromstring(after_resolve.read("word/comments.xml")).findall(f".//{{{W}}}p")
            require(any(node.get(f"{{{W14}}}paraId") == key for node in keyed), "the resolved entry is not keyed on the comment's own paragraph")
            require(
                f"/word/commentsExtended.xml" in content_type_overrides(after_resolve),
                "the created part is not registered in the content types",
            )
            require(
                "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
                in {relationship.get("Type") for relationship in ET.fromstring(after_resolve.read("word/_rels/document.xml.rels"))},
                "the created part has no relationship from the main document",
            )

        deleted = document_step("comment-delete", [
            "edit", "paragraphs", "--expect-sha256", commented_identity["sha256"],
            "--delete-comment", comment_id, "-o", f"{output_root}/uncommented.docx", commented,
        ], "edit-receipt", outputs / "commented.docx")
        require_subset(deleted, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "comment-delete")
        require([item["kind"] for item in deleted["result"]["applied"]] == ["comment_deleted"], "the delete batch did not report comment_deleted")
        require_subset(deleted["output"], identity(outputs / "uncommented.docx"), "comment-delete.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "commented.docx"))) as before_delete, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(outputs / "commented.docx"), read_bounded(outputs / "uncommented.docx"), deleted))) as after_delete:
            require(before_delete.namelist() == after_delete.namelist(), "deleting changed the package member set/order")
            for entry in before_delete.namelist():
                if entry not in {"word/comments.xml", "word/document.xml"}:
                    require(before_delete.read(entry) == after_delete.read(entry), "deleting changed an untouched package part")
            remaining = ET.fromstring(after_delete.read("word/comments.xml")).findall(f"{{{W}}}comment")
            require([node.get(f"{{{W}}}id") for node in remaining] == [], "comments.xml still lists the deleted comment")
            body = after_delete.read("word/document.xml")
            for marker in (b"commentRangeStart", b"commentRangeEnd", b"commentReference"):
                require(marker not in body, "document.xml still carries a marker of the deleted comment")
            require(paragraph_texts(after_delete, "word/document.xml", W) == originals, "deleting a comment changed the paragraph text")

        # ---- DOCX: a span comment and a reply on the package Word writes today ----
        modern = f"{input_root}/modern.docx"
        modern_hash = source_identities["modern.docx"]["sha256"]
        W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
        W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
        span_comment_text = "Define this term"
        span_receipt = document_step("modern-span-comment", [
            "edit", "paragraphs", "--expect-sha256", modern_hash, "--author", "Sample reviewer",
            "--expect", f"1={MODERN_TEXTS[1]}", "--comment", f"1=7:9={span_comment_text}",
            "-o", f"{output_root}/modern-span.docx", modern,
        ], "edit-receipt", inputs / "modern.docx")
        require_subset(span_receipt, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "modern-span-comment")
        require(
            [item["kind"] for item in span_receipt["result"]["applied"]] == ["comment_added"],
            "the span comment batch did not report comment_added",
        )
        require(
            "1 (7:9)" in span_receipt["result"]["applied"][0]["detail"],
            "the span comment receipt does not name the character span it anchored to",
        )
        require_subset(span_receipt["output"], identity(outputs / "modern-span.docx"), "modern-span-comment.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "modern.docx"))) as before_span, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "modern.docx"), read_bounded(outputs / "modern-span.docx"), span_receipt))) as after_span:
            require(before_span.namelist() == after_span.namelist(), "the span comment changed the package member set/order")
            rewritten = {
                "word/document.xml", "word/comments.xml", "word/commentsExtended.xml",
                "word/commentsIds.xml", "word/people.xml",
            }
            for entry in before_span.namelist():
                if entry not in rewritten:
                    require(before_span.read(entry) == after_span.read(entry), "the span comment changed an untouched package part")
            require(paragraph_texts(after_span, "word/document.xml", W) == MODERN_TEXTS, "the span comment changed the paragraph text")
            bodies = ET.fromstring(after_span.read("word/comments.xml")).findall(f"{{{W}}}comment")
            require(
                [node.get(f"{{{W}}}id") for node in bodies] == ["0", "1"],
                "comments.xml does not hold the planted comment and exactly one new one",
            )
            added = bodies[1]
            require("".join(added.itertext()) == span_comment_text, "the new comment does not hold the text it was given")
            require(added.get(f"{{{W}}}author") == "Sample reviewer", "the new comment does not name its author")
            span_key = added.find(f"{{{W}}}p").get(f"{{{W14}}}paraId")
            require(span_key is not None and span_key != MODERN_PARA_ID, "the new comment paragraph did not gain its own paraId")
            require(added.find(f"{{{W}}}p").get(f"{{{W14}}}textId") is not None, "the new comment paragraph has no textId")
            entries = ET.fromstring(after_span.read("word/commentsExtended.xml")).findall(f"{{{W15}}}commentEx")
            keyed = {node.get(f"{{{W15}}}paraId"): node for node in entries}
            require(set(keyed) == {MODERN_PARA_ID, span_key}, "commentsExtended does not key both comments")
            require(keyed[span_key].get(f"{{{W15}}}done") == "0", "the new comment was filed as already done")
            require(keyed[span_key].get(f"{{{W15}}}paraIdParent") is None, "a plain comment was filed as a reply")
            ids = ET.fromstring(after_span.read("word/commentsIds.xml")).findall(f"{{{W16CID}}}commentId")
            durable = [node.get(f"{{{W16CID}}}durableId") for node in ids]
            require(
                sorted(node.get(f"{{{W16CID}}}paraId") for node in ids) == sorted([MODERN_PARA_ID, span_key]),
                "commentsIds does not key both comments",
            )
            require(len(durable) == len(set(durable)) == 2, "commentsIds did not give the new comment its own durable id")
            require(
                {node.get(f"{{{W15}}}author") for node in ET.fromstring(after_span.read("word/people.xml")).findall(f"{{{W15}}}person")}
                == {"Ann Park", "Sample reviewer"},
                "people.xml does not list the new comment's author",
            )
            span_body = after_span.read("word/document.xml").decode("utf-8")
            require(
                span_body.index('<w:commentRangeStart w:id="1"/>') < span_body.index("<w:t>paragraph</w:t>")
                < span_body.index('<w:commentRangeEnd w:id="1"/>'),
                "the span markers do not wrap exactly the characters that were named",
            )
            require(
                span_body.index("Second </w:t>") < span_body.index('<w:commentRangeStart w:id="1"/>'),
                "the text before the span was pulled inside the comment range",
            )

        reply_text = "Agreed, fixing it"
        reply_receipt = document_step("modern-reply", [
            "edit", "paragraphs", "--expect-sha256", modern_hash, "--author", "Sample reviewer",
            "--reply", f"0={reply_text}", "-o", f"{output_root}/modern-reply.docx", modern,
        ], "edit-receipt", inputs / "modern.docx")
        require_subset(reply_receipt, {"status": "completed", "result": {"engine": "docx-paragraphs"}}, "modern-reply")
        require(
            [item["kind"] for item in reply_receipt["result"]["applied"]] == ["comment_replied"],
            "the reply batch did not report comment_replied",
        )
        require_subset(reply_receipt["output"], identity(outputs / "modern-reply.docx"), "modern-reply.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "modern-reply.docx"))) as after_reply:
            require(paragraph_texts(after_reply, "word/document.xml", W) == MODERN_TEXTS, "the reply changed the paragraph text")
            bodies = ET.fromstring(after_reply.read("word/comments.xml")).findall(f"{{{W}}}comment")
            require([node.get(f"{{{W}}}id") for node in bodies] == ["0", "1"], "comments.xml does not hold the parent and its reply")
            require("".join(bodies[1].itertext()) == reply_text, "the reply does not hold the text it was given")
            reply_key = bodies[1].find(f"{{{W}}}p").get(f"{{{W14}}}paraId")
            entries = {
                node.get(f"{{{W15}}}paraId"): node
                for node in ET.fromstring(after_reply.read("word/commentsExtended.xml")).findall(f"{{{W15}}}commentEx")
            }
            require(set(entries) == {MODERN_PARA_ID, reply_key}, "commentsExtended does not key the parent and its reply")
            require(
                entries[reply_key].get(f"{{{W15}}}paraIdParent") == MODERN_PARA_ID,
                "the reply does not name the comment it answers",
            )
            require(entries[MODERN_PARA_ID].get(f"{{{W15}}}paraIdParent") is None, "the parent comment was filed as a reply")
            reply_body = after_reply.read("word/document.xml").decode("utf-8")
            require(
                reply_body.index('<w:commentRangeStart w:id="0"/>') < reply_body.index('<w:commentRangeStart w:id="1"/>')
                < reply_body.index(f"<w:t>{MODERN_TEXTS[0]}</w:t>"),
                "the reply range does not open on the parent's own words",
            )
            require(
                reply_body.index('<w:commentReference w:id="0"/>') < reply_body.index('<w:commentRangeEnd w:id="1"/>')
                < reply_body.index('<w:commentReference w:id="1"/>'),
                "the reply range does not close after the comment it answers",
            )
        require(identity(inputs / "modern.docx") == source_identities["modern.docx"], "a comment step changed its own source")

        # ---- merge: a template filled from one flat data object, and a refusal that fills nothing ----
        template = f"{input_root}/template.docx"
        template_hash = source_identities["template.docx"]["sha256"]
        merge_data = json.dumps({
            "client": TEMPLATE_CLIENT, "date": TEMPLATE_DATE, "amount": TEMPLATE_AMOUNT, "spare": "unused",
        }, separators=(",", ":"))
        merged = document_step("template-merge", [
            "edit", "merge", "--expect-sha256", template_hash, "--data-json", merge_data,
            "-o", f"{output_root}/letter-merged.docx", template,
        ], "edit-receipt", inputs / "template.docx")
        require_subset(merged, {"status": "completed", "result": {"engine": "docx-merge", "unusedKeys": ["spare"], "missingKeys": []}}, "template-merge")
        applied_merge = merged["result"]["applied"]
        require(len(applied_merge) == 1 and applied_merge[0]["kind"] == "field_merged", "the merge batch did not report field_merged")
        require(applied_merge[0]["count"] == 3, "the merge did not report the three placeholders the template carries")
        require(
            applied_merge[0]["detail"] == "3 placeholders in main; keys: amount, client, date; unused: 1",
            "the merge receipt does not describe what it filled",
        )
        require_subset(merged["output"], identity(outputs / "letter-merged.docx"), "template-merge.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "template.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "template.docx"), read_bounded(outputs / "letter-merged.docx"), merged))) as filled:
            require(original.namelist() == filled.namelist(), "the merge changed the package member set/order")
            for entry in original.namelist():
                if entry != "word/document.xml":
                    require(original.read(entry) == filled.read(entry), "the merge changed a part outside the document story")
            require(
                paragraph_texts(filled, "word/document.xml", _WML) == TEMPLATE_MERGED_TEXTS,
                "the merged document does not read as the data said it would",
            )
            body = filled.read("word/document.xml")
            require(b"{{" not in body, "a placeholder survived the merge")
            # The value lands in the run the split placeholder began in, so it reads in one style.
            require(
                b"<w:b/></w:rPr><w:t>Dear " + TEMPLATE_CLIENT.encode() in body,
                "the merged name did not take the opening run's formatting",
            )

        refusal = document_step("template-merge-refusal", [
            "edit", "merge", "--expect-sha256", template_hash,
            "--data-json", json.dumps({"client": TEMPLATE_CLIENT}, separators=(",", ":")),
            "-o", f"{output_root}/letter-short.docx", template,
        ], "edit-receipt", inputs / "template.docx", 1)
        require_subset(refusal, {"status": "refused", "code": "parser_refused", "output": None}, "template-merge-refusal")
        require(
            "amount" in refusal["message"] and "date" in refusal["message"],
            "the merge refusal does not name the placeholders it could not fill",
        )
        require(not path_exists(outputs / "letter-short.docx"), "a refused merge published an output")

        # ---- merge: a repeating table row, and a workbook block that shifts the total below it ----
        invoice = f"{input_root}/invoice.docx"
        invoice_hash = source_identities["invoice.docx"]["sha256"]
        invoice_data = json.dumps({
            "number": INVOICE_NUMBER, "client": INVOICE_CLIENT,
            "lines": [{"name": name, "amount": amount} for name, amount in INVOICE_LINES],
        }, separators=(",", ":"))
        repeated = document_step("merge-block-rows", [
            "edit", "merge", "--expect-sha256", invoice_hash, "--data-json", invoice_data,
            "-o", f"{output_root}/invoice-merged.docx", invoice,
        ], "edit-receipt", inputs / "invoice.docx")
        require_subset(repeated, {"status": "completed", "result": {"engine": "docx-merge", "missingKeys": []}}, "merge-block-rows")
        kinds = [item["kind"] for item in repeated["result"]["applied"]]
        require(kinds == ["field_merged", "block_repeated"], "the block merge did not report field_merged and block_repeated")
        require(
            "; blocks: lines x 3 rows" in repeated["result"]["applied"][0]["detail"],
            "the merge receipt does not name the block it repeated",
        )
        require(repeated["result"]["applied"][1]["count"] == 3, "the block receipt does not count the rows it wrote")
        require_subset(repeated["output"], identity(outputs / "invoice-merged.docx"), "merge-block-rows.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "invoice.docx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "invoice.docx"), read_bounded(outputs / "invoice-merged.docx"), repeated))) as filled:
            require(original.namelist() == filled.namelist(), "the block merge changed the package member set/order")
            for entry in original.namelist():
                if entry != "word/document.xml":
                    require(original.read(entry) == filled.read(entry), "the block merge changed a part outside the body")
            require(
                paragraph_texts(filled, "word/document.xml", W) == INVOICE_MERGED_TEXTS,
                "the repeated rows are not the lines the data named, in order",
            )
            body = ET.fromstring(filled.read("word/document.xml")).find(f"{{{W}}}body")
            rows = body.find(f"{{{W}}}tbl").findall(f"{{{W}}}tr")
            require(len(rows) == 1 + len(INVOICE_LINES), "the table does not hold one header row and one row per line")
            shading = filled.read("word/document.xml").decode("utf-8").count('<w:shd w:fill="EEEEEE"/>')
            require(shading == len(INVOICE_LINES), "a repeated row did not keep the template row's cell shading")
            require(b"{{" not in filled.read("word/document.xml"), "the merged document still carries a placeholder")

        invoice_book = f"{input_root}/invoice.xlsx"
        invoice_book_hash = source_identities["invoice.xlsx"]["sha256"]
        shifted = document_step("merge-block-workbook", [
            "edit", "merge", "--expect-sha256", invoice_book_hash, "--data-json", invoice_data,
            "-o", f"{output_root}/invoice-merged.xlsx", invoice_book,
        ], "edit-receipt", inputs / "invoice.xlsx")
        require_subset(shifted, {"status": "completed", "result": {"engine": "xlsx-merge"}}, "merge-block-workbook")
        require(
            [item["kind"] for item in shifted["result"]["applied"]] == ["field_merged", "block_repeated"],
            "the workbook block merge did not report field_merged and block_repeated",
        )
        require_subset(shifted["output"], identity(outputs / "invoice-merged.xlsx"), "merge-block-workbook.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "invoice-merged.xlsx"))) as book:
            sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
            cells = {}
            formulas = {}
            for row in sheet.iter(f"{{{_SML}}}sheetData"):
                for line in row.findall(f"{{{_SML}}}row"):
                    for cell in line.findall(f"{{{_SML}}}c"):
                        reference = cell.get("r")
                        inline = cell.find(f"{{{_SML}}}is")
                        value = cell.find(f"{{{_SML}}}v")
                        formula = cell.find(f"{{{_SML}}}f")
                        if inline is not None:
                            cells[reference] = "".join(inline.itertext())
                        elif value is not None:
                            cells[reference] = (value.text or "")
                        if formula is not None:
                            formulas[reference] = formula.text or ""
            require(
                [cells.get(f"A{row}") for row in (3, 4, 5)] == [name for name, _ in INVOICE_LINES],
                "the repeated worksheet rows are not the lines the data named",
            )
            require(
                [cells.get(f"B{row}") for row in (3, 4, 5)] == [amount for _, amount in INVOICE_LINES],
                "the repeated worksheet rows do not carry their own amounts",
            )
            require(cells.get("A6") == "Shipping" and cells.get("A7") == "Total", "the rows below the block did not move down")
            require(formulas.get("B7") == "SUM(B2:B6)", "the total formula did not grow with the rows the block added")
            require(cells.get("A8") == f"Paid by {INVOICE_CLIENT}", "the last row did not merge after the shift")
            require(b"{{" not in book.read("xl/worksheets/sheet1.xml"), "the merged workbook still carries a placeholder")
        for name in ("invoice.docx", "invoice.xlsx"):
            require(identity(inputs / name) == source_identities[name], "a merge step changed its own source")

        # ---- sheets: a worksheet added and another renamed, with the formula moved to match ----
        managed = document_step("sheet-batch", [
            "edit", "sheets", "--expect-sha256", workbook_hash,
            "--add", "Notes", "--after", "Summary", "--rename", "Summary=Overview",
            "-o", f"{output_root}/managed.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(managed, {"status": "completed", "result": {"engine": "xlsx-sheets"}}, "sheet-batch")
        require(
            [item["kind"] for item in managed["result"]["applied"]] == ["sheet_added", "sheet_renamed"],
            "the sheet batch did not report the add and the rename",
        )
        require(
            managed["result"]["applied"][1]["detail"].endswith("sheets: Overview, Notes"),
            "the sheet receipt does not report the worksheet order the workbook is left in",
        )
        require_subset(managed["output"], identity(outputs / "managed.xlsx"), "sheet-batch.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "managed.xlsx"))) as changed:
            require(sheet_names(changed) == ["Overview", "Notes"], "the workbook does not list the sheets in the planned order")
            require("xl/worksheets/sheet2.xml" in changed.namelist(), "the added worksheet has no part")
            require('PartName="/xl/worksheets/sheet2.xml"' in changed.read("[Content_Types].xml").decode(), "the added part is not registered in the content types")
            require("worksheets/sheet2.xml" in changed.read("xl/_rels/workbook.xml.rels").decode(), "the added part has no relationship")
            # D1 never named a sheet, so the rename leaves its formula exactly as it was.
            require(b"<f>B1*2</f>" in changed.read("xl/worksheets/sheet1.xml"), "the rename changed a formula that named no sheet")
            require(changed.read("docProps/app.xml") == b"<Properties>Keep every byte</Properties>", "the sheet batch changed an untouched part")

        only_sheet = document_step("sheet-delete-refusal", [
            "edit", "sheets", "--expect-sha256", workbook_hash, "--delete", "Summary",
            "-o", f"{output_root}/emptied.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx", 1)
        require_subset(only_sheet, {"status": "refused", "code": "parser_refused", "output": None}, "sheet-delete-refusal")
        require(
            only_sheet["message"] == "a workbook keeps at least one sheet",
            "the refusal does not say why the only sheet of a workbook stays",
        )
        require(not path_exists(outputs / "emptied.xlsx"), "a refused sheet batch published an output")

        # ---- rules: what a person may type into a range, and what the sheet shows about it ----
        ruled = document_step("rules-add", [
            "edit", "rules", "--expect-sha256", workbook_hash, "--sheet", "Summary",
            "--validate", "A2:A20=list:Draft,In review,Final",
            "--input-message", "A2:A20=Status|Pick one of the three states",
            "--error", "A2:A20=Not a status|Use Draft, In review or Final",
            "--highlight", "B1:B20=greaterThan:5:FFC7CE",
            "-o", f"{output_root}/ruled.xlsx", workbook,
        ], "edit-receipt", inputs / "data.xlsx")
        require_subset(ruled, {"status": "completed", "result": {
            "engine": "xlsx-rules", "skipped": [], "outputExtension": "xlsx",
            "applied": [
                {"kind": "validation_added", "count": 1, "detail": "Summary: A2:A20 list"},
                {"kind": "conditional_format_added", "count": 1, "detail": "Summary: B1:B20 greaterThan"},
            ],
        }}, "rules-add")
        require_subset(ruled["output"], identity(outputs / "ruled.xlsx"), "rules-add.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(inputs / "data.xlsx"))) as original, \
                zipfile.ZipFile(io.BytesIO(unstamped(read_bounded(inputs / "data.xlsx"), read_bounded(outputs / "ruled.xlsx"), ruled))) as changed:
            # Rules live beside the cells, so only the worksheet and the styles part the
            # highlight's differential format goes into are rewritten.
            require(original.namelist() == changed.namelist(), "adding rules changed the package member set")
            for name in original.namelist():
                if name not in ("xl/worksheets/sheet1.xml", "xl/styles.xml"):
                    require(original.read(name) == changed.read(name), "adding rules changed an untouched package part")
            sheet_xml = changed.read("xl/worksheets/sheet1.xml").decode("utf-8")
            require('<formula1>"Draft,In review,Final"</formula1>' in sheet_xml, "the list validation does not carry its own choices")
            require('sqref="A2:A20"' in sheet_xml and 'type="list"' in sheet_xml, "the validation does not cover the range that was named")
            require('promptTitle="Status"' in sheet_xml, "the validation carries no input prompt")
            require('errorTitle="Not a status"' in sheet_xml, "the validation carries no error title")
            require('<conditionalFormatting sqref="B1:B20">' in sheet_xml, "the highlight does not cover the range that was named")
            require('operator="greaterThan"' in sheet_xml and "<formula>5</formula>" in sheet_xml, "the highlight does not compare the way it was asked to")
            # No cell moved: the rules sit beside the sheet data, never inside it.
            require(
                original.read("xl/worksheets/sheet1.xml").decode("utf-8").split("<sheetData>")[1].split("</sheetData>")[0]
                == sheet_xml.split("<sheetData>")[1].split("</sheetData>")[0],
                "adding rules changed a cell",
            )
            styles_xml = changed.read("xl/styles.xml").decode("utf-8")
            require("FFC7CE" in styles_xml and "<dxfs" in styles_xml, "the highlight fill was not appended to the styles part")
            # The cell formats the workbook already had keep their own numbers.
            require('<cellXfs count="1">' in styles_xml, "adding a highlight renumbered an existing cell format")

        unruled = document_step("rules-clear", [
            "edit", "rules", "--expect-sha256", identity(outputs / "ruled.xlsx")["sha256"],
            "--sheet", "Summary", "--clear-rules", "A2:A20", "--clear-rules", "B1:B20",
            "-o", f"{output_root}/unruled.xlsx", f"{output_root}/ruled.xlsx",
        ], "edit-receipt", outputs / "ruled.xlsx")
        require_subset(unruled, {"status": "completed", "result": {
            "engine": "xlsx-rules", "skipped": [],
            "applied": [{
                "kind": "rules_cleared", "count": 2,
                "detail": "Summary: A2:A20, B1:B20 (1 validations, 1 conditional formats)",
            }],
        }}, "rules-clear")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "unruled.xlsx"))) as cleared:
            cleared_sheet = cleared.read("xl/worksheets/sheet1.xml").decode("utf-8")
            require("dataValidation" not in cleared_sheet, "clearing left a data validation behind")
            require("conditionalFormatting" not in cleared_sheet, "clearing left a conditional format behind")
            # Clearing a rule never renumbers the differential formats the styles part holds.
            require("FFC7CE" in cleared.read("xl/styles.xml").decode("utf-8"), "clearing renumbered or dropped an existing dxf")
        require(identity(inputs / "data.xlsx")["sha256"] == workbook_hash, "the rules batch changed its own source")

        # ---- create: a new document, deck and workbook from plain-text sources ----
        brief = f"{input_root}/brief.md"
        created_docx = document_step("create-docx", [
            "create", "-o", f"{output_root}/brief.docx", "--from", brief,
        ], "create-receipt", inputs / "brief.md")
        require_subset(created_docx, {"status": "completed", "result": {"engine": "docx-create", "skipped": []}}, "create-docx")
        require(created_docx["result"]["blocks"] == 7, "the created document did not report the blocks the brief holds")
        require_subset(created_docx["output"], identity(outputs / "brief.docx"), "create-docx.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "brief.docx"))) as written:
            require(paragraph_texts(written, "word/document.xml", _WML) == BRIEF_DOCX_TEXTS, "the created document does not read as the brief said")
            require(paragraph_styles(written) == BRIEF_DOCX_STYLES, "the created document does not carry the heading and list styles")
            styles = written.read("word/styles.xml")
            for level in range(1, 7):
                require(f'w:styleId="Heading{level}"'.encode() in styles, "the created document does not define every heading style")
            require(b"<w:numFmt w:val=\"bullet\"/>" in written.read("word/numbering.xml"), "the created document has no bullet list shape")
            require(b"<w:rPr><w:b/></w:rPr><w:t xml:space=\"preserve\">quarter</w:t>" in written.read("word/document.xml"), "bold text did not become bold run formatting")

        created_pptx = document_step("create-pptx", [
            "create", "-o", f"{output_root}/brief.pptx", "--from", brief,
        ], "create-receipt", inputs / "brief.md")
        require_subset(created_pptx, {"status": "completed", "result": {"engine": "pptx-create", "blocks": 2, "skipped": []}}, "create-pptx")
        require_subset(created_pptx["output"], identity(outputs / "brief.pptx"), "create-pptx.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "brief.pptx"))) as deck:
            listed = deck.namelist()
            require("ppt/slides/slide2.xml" in listed and "ppt/slides/slide3.xml" not in listed, "the created deck does not hold exactly two slides")
            for index, expected in enumerate(BRIEF_SLIDE_TEXTS, start=1):
                require(paragraph_texts(deck, f"ppt/slides/slide{index}.xml", _DML) == expected, "the created deck does not read as the brief said")
            require("ppt/notesSlides/notesSlide2.xml" in listed, "the Notes paragraph did not become a speaker-notes part")
            require("ppt/notesSlides/notesSlide1.xml" not in listed, "a slide without notes was given a notes part")
            require(paragraph_texts(deck, "ppt/notesSlides/notesSlide2.xml", _DML) == [BRIEF_SLIDE_NOTES[1]], "the speaker notes do not hold what the brief said")

        created_xlsx = document_step("create-xlsx", [
            "create", "-o", f"{output_root}/created-rows.xlsx", "--from", f"{input_root}/rows.csv",
            "--sheet", "Invoices", "--header", "--infer-types",
        ], "create-receipt", inputs / "rows.csv")
        require_subset(created_xlsx, {"status": "completed", "result": {"engine": "xlsx-create", "blocks": 3, "skipped": []}}, "create-xlsx")
        require_subset(created_xlsx["output"], identity(outputs / "created-rows.xlsx"), "create-xlsx.output")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "created-rows.xlsx"))) as book:
            require(sheet_names(book) == ["Invoices"], "the created workbook does not carry the worksheet that was asked for")
            require(created_worksheet_cells(book) == ROWS_CELLS, "the created workbook does not hold the rows the CSV held")
            sheet = book.read("xl/worksheets/sheet1.xml")
            require(b'<c r="B2"><v>1250.50</v></c>' in sheet, "a decimal field did not become a number cell")
            require(b'<c r="C2" t="b"><v>1</v></c>' in sheet, "a true field did not become a boolean cell")
            require(b'<row r="1" s="1" customFormat="1">' in sheet, "the header row was not written bold")
            require(b"<f>" not in sheet, "a created workbook must hold no formula")

        # ---- annotate: a highlight over found text, written as an appended revision ----
        annotated = document_step("pdf-annotate", [
            "edit", "annotate", "--expect-sha256", report_hash, "--page", "3",
            "--highlight", "appendix", "-o", f"{output_root}/annotated.pdf", report_pdf,
        ], "edit-receipt", inputs / "report.pdf")
        require_subset(annotated, {"status": "completed", "result": {"engine": "pdf-annotations"}}, "pdf-annotate")
        annotations = annotated["result"]["applied"]
        require(len(annotations) == 1 and annotations[0]["kind"] == "annotation_added", "the annotate batch did not report annotation_added")
        require(annotations[0]["detail"] == "page 3: 1 highlight of 'appendix'", "the annotate receipt does not say what it marked")
        require_subset(annotated["output"], identity(outputs / "annotated.pdf"), "pdf-annotate.output")
        annotated_bytes = read_bounded(outputs / "annotated.pdf")
        source_bytes = read_bounded(inputs / "report.pdf")
        require(annotated_bytes[:len(source_bytes)] == source_bytes, "the annotation did not append a revision to the original bytes")
        annotated_offsets, _ = pdf_current_objects(annotated_bytes)
        page_three = pdf_object(annotated_bytes, annotated_offsets, 7)
        annots = re.search(rb"/Annots\s*\[\s*(\d+)\s+\d+\s+R\s*\]", page_three)
        require(annots is not None, "page three of the annotated copy has no /Annots array")
        annotation = pdf_object(annotated_bytes, annotated_offsets, int(annots.group(1)))
        require(b"/Subtype /Highlight" in annotation, "the added annotation is not a highlight")
        require(b"/QuadPoints" in annotation, "the highlight carries no QuadPoints over the matched glyphs")
        require(b"/P 7 0 R" in annotation, "the highlight does not belong to the page it was asked for")
        require(
            pdf_current_page_texts(annotated_bytes) == pdf_current_page_texts(source_bytes),
            "annotating changed the text of a page",
        )

        # ---- batch: one plan of ordinary commands, one manifest, one invocation ----
        batch_hash = source_identities["data.xlsx"]["sha256"]
        batch_steps = [
            {"step": "read", "argv": [
                "text", "--format", "structure", "--sheet", "Summary", "--range", "A1:D1",
                "--expect-sha256", batch_hash, "-o", f"{output_root}/batch-read.json", f"{input_root}/data.xlsx",
            ]},
            {"step": "update", "argv": [
                "edit", "cells", "--expect-sha256", batch_hash, "--expect", "B1=number:10",
                "--set", "B1=number:12", "-o", f"{output_root}/batch-updated.xlsx", f"{input_root}/data.xlsx",
            ]},
            {"step": "verify", "argv": [
                "text", "--format", "structure", "--sheet", "Summary", "--range", "A1:D1",
                "-o", f"{output_root}/batch-verify.json", f"{output_root}/batch-updated.xlsx",
            ]},
        ]
        write_private_file(inputs / "batch-plan.jsonl", ("\n".join(json.dumps(step) for step in batch_steps) + "\n").encode("utf-8"))
        batch = run("batch", [
            "batch", "--plan", f"{input_root}/batch-plan.jsonl", "-o", f"{output_root}/batch-manifest.json",
        ], "com.krauq.ufo.batch-manifest")
        require_subset(batch, {"status": "completed", "dryRun": False, "continueOnRefusal": False, "network": "not_used", "totals": {
            "steps": 3, "completed": 3, "planned": 0, "refused": 0, "failed": 0, "invocationError": 0, "skipped": 0,
        }}, "batch")
        require_subset(batch["plan"], {**identity(inputs / "batch-plan.jsonl"), "name": "batch-plan.jsonl", "steps": 3}, "batch.plan")
        require_subset(batch["manifest"], identity(outputs / "batch-manifest.json"), "batch.manifest")
        require([row["step"] for row in batch["steps"]] == ["read", "update", "verify"], "the batch manifest does not name each step in plan order")
        require([row["status"] for row in batch["steps"]] == ["completed"] * 3, "a batch step did not complete")
        require([row["argv"] for row in batch["steps"]] == [step["argv"] for step in batch_steps], "the manifest does not echo each step's own argv")
        require_subset(batch["steps"][1]["output"], identity(outputs / "batch-updated.xlsx"), "batch.update.output")
        require(
            batch["steps"][2]["source"]["sha256"] == batch["steps"][1]["output"]["sha256"],
            "the third step did not read the second step's own published output",
        )
        require_subset(batch["steps"][1]["receipt"], {
            "schema": "com.krauq.ufo.edit-receipt", "operation": "cells", "status": "completed",
        }, "batch.update.receipt")
        with zipfile.ZipFile(io.BytesIO(read_bounded(outputs / "batch-updated.xlsx"))) as changed:
            sheet = changed.read("xl/worksheets/sheet1.xml")
            require(b'<c r="B1"><v>12</v></c>' in sheet and b"<f>B1*2</f>" in sheet, "the batch cell write did not keep the formula next to the new value")
        verified = json.loads(read_bounded(outputs / "batch-verify.json"))["selection"]["cells"]
        require(next(cell for cell in verified if cell["ref"] == "B1")["value"] == "12", "the batch verification read does not show the written cell")
        require(identity(inputs / "data.xlsx") == source_identities["data.xlsx"], "the batch changed its own source")
        published_manifest = json.loads(read_bounded(outputs / "batch-manifest.json"))
        require(
            {key: value for key, value in published_manifest.items() if key != "manifest"}
            == {key: value for key, value in batch.items() if key != "manifest"},
            "the published manifest is not the document the batch printed",
        )

        # ---- combine: two PDFs into one, in the planned page order ----
        packet = f"{output_root}/packet.pdf"
        combined = run("combine", [
            "combine", "-o", packet, "--pages", "3,1", "--pages", "all", "--", report_pdf, f"{input_root}/notes.pdf",
        ], "com.krauq.ufo.combine-receipt")
        require_subset(combined, {"status": "completed", "result": {
            "engine": "pdf-combine", "pages": 3, "dropped": ["links_to_pages_left_out"],
        }}, "combine")
        require([row["name"] for row in combined["sources"]] == ["report.pdf", "notes.pdf"], "the combine receipt does not name both sources in order")
        require([row["pages"] for row in combined["sources"]] == ["3,1", "all"], "the combine receipt does not echo each page selection")
        require([row["selected"] for row in combined["sources"]] == [2, 1], "the combine receipt does not report what each source contributed")
        for row, name in zip(combined["sources"], ("report.pdf", "notes.pdf")):
            require_subset(row, {**identity(inputs / name), "stableDuringProcessing": True}, "combine.sources." + name)
        require_subset(combined["output"], identity(outputs / "packet.pdf"), "combine.output")
        packet_pages = pdf_current_page_texts(read_bounded(outputs / "packet.pdf"))
        require(len(packet_pages) == 3, "the combined packet does not hold exactly three pages")
        require(
            [PDF_PAGE_TEXTS[2].encode() in packet_pages[0], PDF_PAGE_TEXTS[0].encode() in packet_pages[1], NOTES_PAGE_TEXT.encode() in packet_pages[2]] == [True, True, True],
            "the combined packet's pages are not the planned pages in the planned order",
        )
        require(PDF_PAGE_TEXTS[0].encode() not in packet_pages[0], "the combined packet's first page is not the page that was planned for it")
        require(source_identities == {name: identity(inputs / name) for name in sample_files()}, "a generated source changed")
        require(len(measurements) == COMMAND_COUNT, f"the suite ran {len(measurements)} commands, not the {COMMAND_COUNT} it declares")
        if launcher:
            require(artifact == launcher_artifact(launcher), "packaged installation changed during evaluation")
        summary = {
            "suite": "ufo-file-intake-sample", "suiteVersion": SUITE_VERSION, "status": "passed",
            "engineVersion": args.expect_version, "artifact": artifact,
            "evaluatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "inputs": source_identities, "cleanedOutput": clean_identity, "editedOutput": edited_identity,
            "checks": {name: True for name in (
                "receiptContracts", "agentSkillInventory", "agentSkillSize", "expectedOutcomes", "folderReport", "folderFind", "folderFindPreFilter", "sourcePreservation", "textPreservation",
                "documentXmlPreservation", "outputNoReplace", "unsupportedRefusal",
                "paragraphPaging", "paragraphBatch", "untouchedPartsPreservation", "staleAndPreimageRefusal",
                "paragraphInsertAndDelete", "structuralAnchorRefusal",
                "trackedInsertAndDelete", "trackedStructureAcceptAndReject", "inlineImageInsert",
                "tableCoordinates", "tableRowCopyAndFill", "tableRowDelete", "tableRowRefusal",
                "cellInventoryAndRange", "cellBatch", "formulaRefusal", "formulaWrite", "formulaExpectationRefusal",
                "rowAppend", "rowStaleRefusal", "rowInsertShift", "rowInsertMergeRefusal",
                "slideInventoryAndRead", "slideBatch", "slideDuplicate", "slideDeleteRefusal", "fieldBatch",
                "trackedChanges", "anchoredComment", "runFormatSpan", "paragraphStyleRefusal",
                "cellFormatting", "imageEdits", "pdfPageReads", "pdfParagraphRows",
                "pdfParagraphText", "pdfParagraphRefusal", "lineWindowAndWrite",
                "findCoordinates", "pdfCombine",
                "renderSurfaces", "headerStoryReadAndWrite", "commentInventoryResolveDelete",
                "modernThreadSpanComment", "modernThreadReply",
                "inlineImageReplace", "chartDataRead", "chartDataWrite", "chartExpectationRefusal",
                "templateMerge", "templateMergeRefusal", "mergeBlockRows", "mergeBlockWorkbook",
                "createDocument", "createPresentation", "createWorkbook",
                "sheetAddAndRename", "sheetDeleteRefusal", "pdfHighlightAnnotation",
                "editDryRunPlan", "slideMove", "shapeTextBoxAddAndDelete", "shapeKindGuard",
                "columnInsertShift", "worksheetLayout", "hyperlinkSpan", "footnoteReference",
                "batchPlanRunner", "worksheetChartWrite", "slideChartCreate",
                "worksheetRules", "worksheetRulesCleared",
                "slideTableUnmergeAndMerge", "docxTableCellMerge",
            )},
            "measurements": measurements,
            "measurementScope": "Single sequential synthetic run; wall time includes process startup and container cleanup. Not a throughput or memory benchmark.",
            "limitations": [
                "Twenty-one generated files and one generated plan, not representative customer coverage or visual-fidelity evidence.",
                "Clean copies are not antivirus scans, complete sanitization guarantees, or production approval.",
                "Receipt paths identify the temporary execution workspace; bundle paths are relative to this directory.",
            ],
        }
        write_private_file(staging / "expected.json", (json.dumps({
            "receipts": EXPECTED, "cleanedOutput": EXPECTED_CLEAN_OUTPUT, "text": SAMPLE_TEXT,
            "paragraphReplacements": replacements,
            "sourceIdentities": source_identities,
        }, indent=2) + "\n").encode())
        write_private_file(staging / "evaluation.json", (json.dumps(summary, indent=2) + "\n").encode())
        publish_directory_noreplace(staging, output)
    return summary


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    surface = result.add_mutually_exclusive_group(required=True)
    surface.add_argument("--ufo", type=Path, help="packaged Linux app-image bin/ufo launcher")
    surface.add_argument("--container-image", help="existing local image; never pulled")
    result.add_argument("--expect-version", required=True)
    result.add_argument("--output", type=Path, required=True, help="new evidence directory; parent must exist")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        summary = run_evaluation(args)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Evaluation failed: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {len(summary['measurements'])} commands and all preservation checks. Evidence: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
