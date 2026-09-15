"""
docs.py - pull text out of the documentation half of the folder, using the
standard library only.

Word (.docx), Excel (.xlsx), PowerPoint (.pptx) and Visio (.vsdx) are ZIP
archives of XML, so zipfile + xml.etree read them without any third-party
package - which matters on a locked-down workstation where `pip install` is
not an option. PDF text is recovered on a best-effort basis from Flate
streams with zlib. Anything image-only (a scanned PDF, a diagram embedded as
a picture) is reported as such rather than silently indexed as empty text.

Images inside documents are LISTED, not OCR'd. OCR or a vision model is a
per-image decision because it costs model tokens; the manifest here is what
lets somebody decide which fifteen diagrams out of four thousand images are
worth spending on.

Documentation is indexed as PROSE, never as facts: nothing extracted here is
joined into the impact graph. Documents describe what the code was meant to
do; the code says what it does. Where they disagree, the answer must quote
both and say so.
"""

from __future__ import annotations

import os
import re
import zipfile
import zlib
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "v": "http://schemas.microsoft.com/office/visio/2012/main",
}

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".emf", ".wmf", ".tif", ".tiff", ".svg")

# atlas.convert writes the right-hand side beside the original (templates and
# RTF too - Word / Excel / PowerPoint open them all)
LEGACY_TO_MODERN = {".doc": ".docx", ".dot": ".docx", ".rtf": ".docx",
                    ".xls": ".xlsx", ".xlt": ".xlsx",
                    ".ppt": ".pptx", ".pot": ".pptx"}
LEGACY = {".doc": "Word 97-2003", ".dot": "Word 97-2003 template", ".rtf": "Rich Text",
          ".xls": "Excel 97-2003", ".xlt": "Excel 97-2003 template",
          ".ppt": "PowerPoint 97-2003", ".pot": "PowerPoint 97-2003 template",
          ".vsd": "Visio 2003-2010", ".msg": "Outlook message"}


def long_path(path: str) -> str:
    r"""Windows refuses paths longer than ~260 characters unless they carry
    the \\?\ prefix; deep OneDrive trees reach that easily."""
    if os.name == "nt" and len(path) > 240 and not path.startswith("\\\\?\\"):
        p = os.path.abspath(path)
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return path


@dataclass
class DocText:
    path: str
    kind: str                                    # docx|xlsx|pptx|vsdx|pdf|text|html|legacy|unsupported
    title: Optional[str] = None
    sections: List[Tuple[str, str]] = dc_field(default_factory=list)   # (heading, text)
    tables: List[List[List[str]]] = dc_field(default_factory=list)
    images: List[str] = dc_field(default_factory=list)                 # internal names
    image_anchor: Dict[str, str] = dc_field(default_factory=dict)      # internal name -> the sheet / slide / heading it sits in
    notes: List[str] = dc_field(default_factory=list)                  # caveats
    ok: bool = True

    @property
    def text(self) -> str:
        parts = []
        for h, t in self.sections:
            if h:
                parts.append(f"\n## {h}\n")
            if t:
                parts.append(t)
        for tbl in self.tables:
            parts.append("\n" + "\n".join("\t".join(r) for r in tbl) + "\n")
        return "\n".join(parts).strip()


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------

def extract(path: str) -> DocText:
    path = long_path(path)
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".docx" or ext == ".dotx":
            return _docx(path)
        if ext in (".xlsx", ".xlsm", ".xltx"):
            return _xlsx(path)
        if ext in (".pptx", ".potx"):
            return _pptx(path)
        if ext == ".vsdx":
            return _vsdx(path)
        if ext == ".pdf":
            return _pdf(path)
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"):
            # A standalone picture (a screen shot, a photographed printout). No
            # text until atlas.ocr has read it; listed so it is not forgotten.
            d = DocText(path=path, kind="image", images=[os.path.basename(path)])
            d.title = os.path.splitext(os.path.basename(path))[0]
            d.notes.append("standalone image - run `python -m atlas.ocr` to read it")
            return d
        if ext in (".htm", ".html"):
            return _html(path)
        if ext in (".txt", ".md", ".csv", ".tsv", ".log", ".rtf", ".xml", ".json"):
            return _plain(path)
        if ext in LEGACY:
            d = DocText(path=path, kind="legacy", ok=False)
            d.notes.append(f"{LEGACY[ext]} binary format cannot be read without Office; "
                           f"open it and Save As the modern format (.docx/.xlsx/.pptx/.vsdx) to index it")
            return d
        d = DocText(path=path, kind="unsupported", ok=False)
        d.notes.append(f"no extractor for {ext or 'no extension'}")
        return d
    except zipfile.BadZipFile:
        d = DocText(path=path, kind=ext.lstrip("."), ok=False)
        d.notes.append("file is not a valid Office package (corrupt, or a legacy format with a modern extension)")
        return d
    except Exception as e:                                  # noqa: BLE001
        d = DocText(path=path, kind=ext.lstrip("."), ok=False)
        d.notes.append(f"extraction failed: {type(e).__name__}: {e}")
        return d


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _q(ns: str, tag: str) -> str:
    return f"{{{NS[ns]}}}{tag}"


def _images_in(z: zipfile.ZipFile, prefix: str) -> List[str]:
    return sorted(n for n in z.namelist()
                  if n.startswith(prefix) and n.lower().endswith(IMAGE_EXT))


def _clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


def render_rows(rows: List[List[str]], numbers: Optional[List[int]] = None) -> str:
    """A table as lines the model can read and cite: `row 12: a | b | c`.
    Empty trailing cells are dropped, empty rows skipped; `numbers` are the
    spreadsheet's own row numbers (a QA sheet's row 12 is row 12 to the
    tester too), else 1-based."""
    out = []
    for i, cells in enumerate(rows):
        n = numbers[i] if numbers and i < len(numbers) else i + 1
        vals = [re.sub(r"[\r\n]+", " ", (c or "").replace("|", "/")).strip() for c in cells]
        while vals and not vals[-1]:
            vals.pop()
        if not any(vals):
            continue
        out.append(f"row {n}: " + " | ".join(vals))
    return "\n".join(out)


def _anchor(d: "DocText", name: str, where: str) -> None:
    """Record where a picture sits; a picture pasted on two tabs (Excel keeps
    ONE media file for a copied screenshot) lists both: `sheet: A; sheet: B`."""
    cur = d.image_anchor.get(name)
    if not cur:
        d.image_anchor[name] = where
    elif where not in cur.split("; "):
        d.image_anchor[name] = cur + "; " + where


def _body_blocks(el):
    """The paragraphs and tables of a Word body in order, looking inside
    content controls (w:sdt / w:sdtContent - cover pages, tables of
    contents, template blocks) which otherwise hide their text."""
    for child in el:
        tag = child.tag
        if tag == _q("w", "p") or tag == _q("w", "tbl"):
            yield child
        elif tag == _q("w", "sdt"):
            content = child.find(_q("w", "sdtContent"))
            if content is not None:
                yield from _body_blocks(content)
        elif tag.endswith("}sdtContent"):
            yield from _body_blocks(child)


def _rels(z: zipfile.ZipFile, part: str) -> Dict[str, str]:
    """Relationships of a package part, Id -> target part name (resolved
    against the part's folder), e.g. xl/worksheets/sheet2.xml -> its drawing."""
    d, base = part.rsplit("/", 1) if "/" in part else ("", part)
    rel_part = f"{d}/_rels/{base}.rels" if d else f"_rels/{base}.rels"
    if rel_part not in z.namelist():
        return {}
    try:
        root = ET.fromstring(z.read(rel_part))
    except ET.ParseError:
        return {}
    out: Dict[str, str] = {}
    for r in root.iter(_q("rel", "Relationship")):
        rid, target = r.get("Id"), r.get("Target") or ""
        if not rid or not target or (r.get("TargetMode") or "").lower() == "external":
            continue
        if target.startswith("/"):
            out[rid] = target.lstrip("/")
        else:
            segs = (d.split("/") if d else []) + target.split("/")
            stack: List[str] = []
            for sgm in segs:
                if sgm == "..":
                    if stack:
                        stack.pop()
                elif sgm and sgm != ".":
                    stack.append(sgm)
            out[rid] = "/".join(stack)
    return out


def _media_targets(z: zipfile.ZipFile, part: str) -> List[str]:
    return [t for t in _rels(z, part).values() if t.lower().endswith(IMAGE_EXT)]


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------

def _docx(path: str) -> DocText:
    d = DocText(path=path, kind="docx")
    with zipfile.ZipFile(path) as z:
        d.images = _images_in(z, "word/media/")
        root = ET.fromstring(z.read("word/document.xml"))
        body = root.find(_q("w", "body"))
        if body is None:
            d.notes.append("no document body")
            return d

        heading = ""
        buf: List[str] = []
        rels = _rels(z, "word/document.xml")
        r_embed, r_id = f"{{{NS['r']}}}embed", f"{{{NS['r']}}}id"

        def flush():
            if buf:
                d.sections.append((heading, "\n".join(buf)))
                buf.clear()

        def anchor_pictures(el, where: str) -> List[str]:
            names: List[str] = []
            for x in el.iter():
                if x.tag.endswith("}blip") or x.tag.endswith("}imagedata"):
                    target = rels.get(x.get(r_embed) or x.get(r_id) or "")
                    if target:
                        _anchor(d, target, where)
                        base = target.rsplit("/", 1)[-1]
                        if base not in names:           # Word writes a picture twice (Choice + Fallback)
                            names.append(base)
            return names

        for el in _body_blocks(body):
            if el.tag == _q("w", "p"):
                style = el.find(f"./{_q('w','pPr')}/{_q('w','pStyle')}")
                sval = (style.get(_q("w", "val")) if style is not None else "") or ""
                txt = _clean("".join(t.text or "" for t in el.iter(_q("w", "t"))))
                if re.match(r"(?i)heading\d*|title|caption", sval):
                    flush()                              # a picture in a heading / caption belongs to THAT heading
                    heading = txt
                    if d.title is None and re.match(r"(?i)title", sval):
                        d.title = txt
                elif txt:
                    buf.append(txt)
                if el.find(f".//{_q('w','drawing')}") is not None or el.find(f".//{_q('w','pict')}") is not None:
                    pics = anchor_pictures(el, heading or "before the first heading")
                    buf.append("[image" + (": " + ", ".join(pics) if pics else "") + "]")
            elif el.tag == _q("w", "tbl"):
                rows: List[List[str]] = []
                for tr in el.iter(_q("w", "tr")):
                    cells = []
                    for tc in tr.findall(_q("w", "tc")):
                        cells.append(_clean(" ".join(t.text or "" for t in tc.iter(_q("w", "t")))))
                    rows.append(cells)
                if rows:
                    d.tables.append(rows)
                    buf.append(f"[table {len(rows)}x{max(len(r) for r in rows)}]\n" + render_rows(rows))
                    anchor_pictures(el, heading or "before the first heading")
        flush()

    if d.title is None and d.sections:
        d.title = d.sections[0][0] or d.sections[0][1].split("\n", 1)[0][:120]
    if d.images and not any(t for _, t in d.sections):
        d.notes.append("document is image-only; nothing to index without OCR")
    return d


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
MAX_ROWS_PER_SHEET = 20000


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def _xlsx(path: str) -> DocText:
    d = DocText(path=path, kind="xlsx")
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        d.images = _images_in(z, "xl/media/")

        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.iter(_q("s", "si")):
                shared.append("".join(t.text or "" for t in si.iter(_q("s", "t"))))

        # sheet order and names from workbook.xml, mapped through the rels.
        sheets: List[Tuple[str, str]] = []
        if "xl/workbook.xml" in names:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels: Dict[str, str] = {}
            if "xl/_rels/workbook.xml.rels" in names:
                rr = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                for rel in rr.iter(_q("rel", "Relationship")):
                    rels[rel.get("Id")] = rel.get("Target")
            for sh in wb.iter(_q("s", "sheet")):
                rid = sh.get(_q("r", "id"))
                target = rels.get(rid, "")
                if target and not target.startswith("/"):
                    target = "xl/" + target
                sheets.append((sh.get("name") or "Sheet", target.lstrip("/")))
        if not sheets:
            sheets = [(n.rsplit("/", 1)[-1], n) for n in sorted(names)
                      if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]

        for sheet_name, target in sheets:
            if target not in names:
                d.notes.append(f"sheet {sheet_name!r}: part {target} missing")
                continue
            root = ET.fromstring(z.read(target))
            rows: List[List[str]] = []
            numbers: List[int] = []
            truncated = False
            last_r = 0
            for i, row in enumerate(root.iter(_q("s", "row"))):
                if i >= MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                # a row without r sits right after the previous one (ECMA-376)
                last_r = int(row.get("r")) if (row.get("r") or "").isdigit() else last_r + 1
                cells: Dict[int, str] = {}
                for c in row.findall(_q("s", "c")):
                    ref = c.get("r") or ""
                    m = _CELL_REF.match(ref)
                    col = _col_index(m.group(1)) if m else len(cells) + 1
                    t = c.get("t")
                    v = c.find(_q("s", "v"))
                    if t == "s" and v is not None and v.text is not None:
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = v.text
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(_q("s", "t")))
                    elif t == "b" and v is not None:
                        val = "TRUE" if v.text == "1" else "FALSE"
                    else:
                        val = (v.text if v is not None and v.text is not None else "")
                    cells[col] = _clean(val)
                if cells:
                    width = max(cells)
                    rows.append([cells.get(i, "") for i in range(1, width + 1)])
                    numbers.append(last_r)
            # the pictures pasted on this sheet (a tester's screenshots): sheet -> drawing -> media
            for drawing in _rels(z, target).values():
                if "/drawings/" in drawing:
                    for media in _media_targets(z, drawing):
                        _anchor(d, media, f"sheet: {sheet_name}")
            if rows:
                d.tables.append(rows)
                d.sections.append((f"sheet: {sheet_name}", render_rows(rows, numbers)))
            if truncated:
                d.notes.append(f"sheet {sheet_name!r} truncated at {MAX_ROWS_PER_SHEET} rows")
    d.title = os.path.splitext(os.path.basename(path))[0]
    return d


# --------------------------------------------------------------------------
# PowerPoint
# --------------------------------------------------------------------------

def _pptx(path: str) -> DocText:
    d = DocText(path=path, kind="pptx")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        d.images = _images_in(z, "ppt/media/")
        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for n in slides:
            num = int(re.search(r"(\d+)", n).group(1))
            root = ET.fromstring(z.read(n))
            paras: List[str] = []
            title = ""
            for sp in root.iter(_q("p", "sp")):
                ph = sp.find(f".//{_q('p','ph')}")
                is_title = ph is not None and (ph.get("type") or "") in ("title", "ctrTitle")
                for para in sp.iter(_q("a", "p")):
                    t = _clean("".join(r.text or "" for r in para.iter(_q("a", "t"))))
                    if not t:
                        continue
                    if is_title and not title:
                        title = t
                    else:
                        paras.append(t)
            for tbl in root.iter(_q("a", "tbl")):
                rows = []
                for tr in tbl.iter(_q("a", "tr")):
                    rows.append([_clean("".join(t.text or "" for t in tc.iter(_q("a", "t"))))
                                 for tc in tr.findall(_q("a", "tc"))])
                if rows:
                    d.tables.append(rows)
                    paras.append(f"[table {len(rows)} rows]\n" + render_rows(rows))
            media = list(dict.fromkeys(_media_targets(z, n)))
            for m_name in media:
                _anchor(d, m_name, f"slide {num}: {title}".rstrip(": "))
            if root.find(f".//{_q('p','pic')}") is not None or media:
                paras.append("[image" + (": " + ", ".join(x.rsplit("/", 1)[-1] for x in media) if media else "") + "]")
            notes_part = f"ppt/notesSlides/notesSlide{num}.xml"
            if notes_part in names:
                nroot = ET.fromstring(z.read(notes_part))
                nt = _clean(" ".join(t.text or "" for t in nroot.iter(_q("a", "t"))))
                if nt:
                    paras.append(f"[speaker notes] {nt}")
            d.sections.append((f"slide {num}: {title}".rstrip(": "), "\n".join(paras)))
            if d.title is None and title:
                d.title = title
    return d


# --------------------------------------------------------------------------
# Visio (.vsdx) - architecture diagrams often live here
# --------------------------------------------------------------------------

def _vsdx(path: str) -> DocText:
    d = DocText(path=path, kind="vsdx")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        d.images = _images_in(z, "visio/media/")
        pages = sorted((n for n in names if re.match(r"visio/pages/page\d+\.xml$", n)),
                       key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for n in pages:
            root = ET.fromstring(z.read(n))
            texts = []
            for el in root.iter():
                if el.tag.endswith("}Text"):
                    t = _clean("".join(el.itertext()))
                    if t:
                        texts.append(t)
            d.sections.append((n.rsplit("/", 1)[-1], "\n".join(texts)))
    d.notes.append("Visio shapes' text captured; connectors (which box points to which) are not - "
                   "read the diagram for flow direction")
    return d


# --------------------------------------------------------------------------
# PDF - best effort, standard library only
# --------------------------------------------------------------------------

# `(?<!end)`: the tail of `endstream` is not a stream start; the EOL before
# endstream is optional (requiring it made every such stream scan the rest
# of the file - quadratic, LESSONS 148)
_STREAM = re.compile(rb"(?<!end)stream\r?\n(.*?)(?:\r?\n)?endstream", re.S)
_TJ = re.compile(rb"\[(.*?)\]\s*TJ", re.S)
_Tj = re.compile(rb"\((.*?)(?<!\\)\)\s*(?:Tj|'|\")", re.S)
_PDF_STR = re.compile(rb"\((.*?)(?<!\\)\)", re.S)
# one pass over a content stream: literal strings (escapes, no unescaped
# nesting), array brackets and operator words
_PDF_TOKEN = re.compile(rb"\(((?:[^()\\]|\\.)*)\)|(\[)|(\])|([A-Za-z'\"]+)", re.S)


def _pdf_text_pieces(content: bytes) -> List[str]:
    """The strings a content stream shows with Tj, ' and " (the last string
    before the operator) and TJ (every string of the array), in order. A
    scanner, not two lazy regexes: `(...) ... Tj` searched per string ran to
    the end of the stream whenever the text used TJ arrays (quadratic)."""
    pieces: List[str] = []
    last: Optional[bytes] = None
    arr: Optional[List[bytes]] = None
    done_arr: Optional[List[bytes]] = None
    for m in _PDF_TOKEN.finditer(content):
        s, open_b, close_b, word = m.group(1), m.group(2), m.group(3), m.group(4)
        if s is not None:
            if arr is not None:
                arr.append(s)
            else:
                last = s
        elif open_b:
            arr = []
        elif close_b:
            done_arr, arr = arr, None
        else:
            if word == b"TJ" and done_arr is not None:
                pieces.append("".join(_pdf_unescape(x) for x in done_arr))
            elif word in (b"Tj", b"'", b'"') and last is not None:
                pieces.append(_pdf_unescape(last))
            last, done_arr = None, None
    return pieces


def _pdf_unescape(b: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5C and i + 1 < len(b):           # backslash
            nx = b[i + 1]
            if nx in b"nrtbf":
                out += {0x6E: b"\n", 0x72: b"\r", 0x74: b"\t", 0x62: b"\b", 0x66: b"\f"}[nx]
                i += 2
                continue
            if nx in b"()\\":
                out.append(nx)
                i += 2
                continue
            m = re.match(rb"[0-7]{1,3}", b[i + 1:i + 4])
            if m:
                out.append(int(m.group(0), 8) & 0xFF)
                i += 1 + len(m.group(0))
                continue
            i += 1
            continue
        out.append(c)
        i += 1
    return out.decode("latin-1", errors="replace")


PDF_TEXT_MAX_BYTES = 60 * 1024 * 1024     # bigger than this is a scan: the page renderer + OCR read it, not this


def _pdf(path: str) -> DocText:
    d = DocText(path=path, kind="pdf")
    size = os.path.getsize(path)
    if size > PDF_TEXT_MAX_BYTES:
        # a book-sized scan: the text extractor would chew on it for minutes
        # and find nothing; `OCR images` renders and reads its pages instead
        d.ok = False
        d.notes.append(f"no extractable text - {size // 1024 // 1024} MB PDF, too large for the text extractor; "
                       "likely a scan, its pages are read by `OCR images`")
        d.title = os.path.splitext(os.path.basename(path))[0]
        return d
    with open(path, "rb") as fh:
        data = fh.read()
    n_images = len(re.findall(rb"/Subtype\s*/Image", data))
    texts: List[str] = []
    for m in _STREAM.finditer(data):
        raw = m.group(1)
        try:
            content = zlib.decompress(raw)
        except zlib.error:
            try:
                content = zlib.decompressobj().decompress(raw)
            except zlib.error:
                content = raw
        if b"BT" not in content:
            continue
        pieces = _pdf_text_pieces(content)
        page = _clean(" ".join(pieces))
        if page:
            texts.append(page)
    if texts:
        for i, t in enumerate(texts, 1):
            d.sections.append((f"page-block {i}", t))
        # simple fonts come out readable; CID / Identity-H fonts (and text
        # that comes out mostly non-letters) need the pages rendered and OCR'd
        cid = bool(re.search(rb"/Identity-H|/Type0\b|/CIDFontType", data))
        joined = " ".join(texts)
        letters = sum(1 for c in joined if c.isalpha() or c.isspace())
        if cid or letters < 0.6 * max(1, len(joined)):
            d.notes.append("PDF text recovered without a font decoder and the file uses CID/Identity-H fonts "
                           "(or the text looks scrambled): it may be garbled - `OCR images` renders the pages and reads them")
        else:
            d.notes.append("PDF text recovered from content streams (simple fonts) - verify before quoting")
    else:
        d.ok = False
        d.notes.append("no extractable text" + (f"; {n_images} embedded image(s) - likely a scan, needs OCR"
                                                if n_images else ""))
    if n_images:
        d.images = [f"embedded-image-{i}" for i in range(1, n_images + 1)]
    d.title = os.path.splitext(os.path.basename(path))[0]
    return d


# --------------------------------------------------------------------------
# plain text / html
# --------------------------------------------------------------------------

def _plain(path: str) -> DocText:
    d = DocText(path=path, kind="text")
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        txt = raw.decode("latin-1", errors="replace")
    d.sections.append(("", txt))
    d.title = os.path.splitext(os.path.basename(path))[0]
    return d


def _strip_blocks(txt: str, tags: Tuple[str, ...]) -> str:
    """Remove <script>...</script> / <style>...</style> by searching forward:
    a lazy regex scanned to the end of the page for every unclosed tag."""
    low = txt.lower()
    out: List[str] = []
    pos = 0
    while True:
        starts = [(low.find("<" + t, pos), t) for t in tags]
        starts = [(k, t) for k, t in starts if k >= 0]
        if not starts:
            break
        k, t = min(starts)
        end = low.find("</" + t, k)
        if end < 0:
            break                                   # unclosed: keep the rest as text, as the browser would show nothing more
        close = low.find(">", end)
        out.append(txt[pos:k])
        out.append(" ")
        pos = (close + 1) if close >= 0 else len(txt)
    out.append(txt[pos:])
    return "".join(out)


def _html(path: str) -> DocText:
    d = _plain(path)
    d.kind = "html"
    txt = d.sections[0][1]
    txt = _strip_blocks(txt, ("script", "style"))
    txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"&lt;", "<", txt)
    txt = re.sub(r"&gt;", ">", txt)
    d.sections = [("", re.sub(r"\n\s*\n+", "\n\n", txt).strip())]
    return d


# --------------------------------------------------------------------------
# sections a model can be handed one at a time
# --------------------------------------------------------------------------

def chunk_sections(sections: List[Tuple[str, str]], max_chars: int = 4000) -> List[Tuple[str, str]]:
    """Split long sections so that every citable section is small enough to
    hand to a model whole. A heading-less PDF or text file arrives as ONE
    section (a 300-page specification, one number - LESSONS 129); it is cut
    at paragraph boundaries into parts of about `max_chars`, the heading
    carrying `(part k/n)`. Short sections are returned untouched."""
    out: List[Tuple[str, str]] = []
    for heading, text in sections:
        if len(text or "") <= max_chars:
            out.append((heading, text))
            continue
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        parts: List[str] = []
        buf = ""
        for p in paras:
            # one huge paragraph (a 30 MB table rendered as rows): cut between
            # lines, else at sentence ends - walking an index, never re-slicing
            # the remainder, which made a 100 MB paragraph take minutes
            pos, n = 0, len(p)
            while n - pos > max_chars:
                end = pos + max_chars
                cut = p.rfind("\n", pos, end)
                if cut > pos:                              # a short part beats a row cut in two
                    cut = cut + 1
                else:
                    cut = p.rfind(". ", pos, end)
                    cut = cut + 1 if cut - pos > max_chars // 2 else end
                head = p[pos:cut].rstrip()
                pos = cut
                while pos < n and p[pos] in " \t\r\n":
                    pos += 1
                if buf:
                    parts.append(buf)
                    buf = ""
                if head:
                    parts.append(head)
            if pos:
                p = p[pos:]
            if buf and len(buf) + len(p) + 2 > max_chars:
                parts.append(buf)
                buf = p
            else:
                buf = f"{buf}\n\n{p}" if buf else p
        if buf:
            parts.append(buf)
        n = len(parts)
        for k, part in enumerate(parts, 1):
            out.append((f"{heading} (part {k}/{n})" if heading else f"part {k}/{n}", part))
    return out
