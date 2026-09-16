"""
ocr.py - read the pictures inside the documents, with zero model tokens.

    python -m atlas.ocr --db atlas.db --out C:/estate/out/images          # extract + OCR every image
    python -m atlas.ocr --db atlas.db --out ... --member DESIGN01           # one document
    python -m atlas.ocr --db atlas.db --out ... --no-ocr                    # extract only

Documents in an insurance shop carry the important part as pictures: a
screen shot of the online map, a Visio flow pasted into Word, a table
photographed from a printout. Text extraction skips all of it. This module
pulls every image out of the .docx/.pptx/.xlsx/.vsdx packages (they are
ZIP files) into a folder, then runs the OCR engine that ships with Windows
10/11 (Windows.Media.Ocr, driven through PowerShell - no install, no
network, no tokens) and stores the recognised text as a section of the
document: searchable, and citable as  [[DOCNAME 10nn "token"]].

OCR text is prose from a picture: it is indexed like documentation, never
as a fact, and the section heading says which image it came from.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".emf", ".wmf")
MIN_BYTES = 2048           # smaller than this is an icon or a bullet, not content
MIN_VECTOR_BYTES = 400     # ... but a metafile is vector: a whole flow chart of text fits in 2 KB
OCR_ORDINAL_BASE = 1000    # doc_section ordinals 1001.. are OCR'd images

_MEDIA_PREFIX = {"docx": "word/media/", "dotx": "word/media/", "pptx": "ppt/media/", "potx": "ppt/media/",
                 "xlsx": "xl/media/", "xlsm": "xl/media/", "vsdx": "visio/media/"}

# PowerShell 5.1 + WinRT. Reads a list of image paths, writes one JSON object
# per line: {"path": ..., "text": ...} or {"path": ..., "error": ...}.
_PS_OCR = r'''
param([string]$ListFile)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
  [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
} catch { Write-Output (@{engine="unavailable"; error="$_"} | ConvertTo-Json -Compress); exit 0 }
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($WinRtTask, $ResultType) { $asTask = $asTaskGeneric.MakeGenericMethod($ResultType); $netTask = $asTask.Invoke($null, @($WinRtTask)); $netTask.Wait(-1) | Out-Null; $netTask.Result }
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Output (@{engine="none"; error="no OCR language pack installed"} | ConvertTo-Json -Compress); exit 0 }
Write-Output (@{engine=$engine.RecognizerLanguage.LanguageTag} | ConvertTo-Json -Compress)
foreach ($raw in Get-Content -LiteralPath $ListFile) {
  # Get-Content strings carry PSPath/ReadCount note properties, which
  # ConvertTo-Json would turn into an object; interpolate to a plain string.
  $p = "$raw".Trim()
  if (-not $p) { continue }
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $lines = @($result.Lines | ForEach-Object { $_.Text })
    Write-Output (@{path=$p; text=($lines -join "`n")} | ConvertTo-Json -Compress)
    $stream.Dispose()
  } catch { Write-Output (@{path=$p; error="$_"} | ConvertTo-Json -Compress) }
}
'''

# Test / self-check helper: draw text into a PNG with System.Drawing.
_PS_RENDER = r'''
param([string]$Text, [string]$Out)
Add-Type -AssemblyName System.Drawing
$bmp = New-Object System.Drawing.Bitmap 1100, 160
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.Clear([System.Drawing.Color]::White)
$font = New-Object System.Drawing.Font("Arial", 28)
$g.DrawString($Text, $font, [System.Drawing.Brushes]::Black, 10, 50)
$g.Dispose(); $bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
Write-Output "ok"
'''


def ocr_available() -> Tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Windows OCR is only available on Windows; on other systems images are extracted, not read"
    if shutil.which("powershell") is None:
        return False, "powershell.exe not found on PATH"
    return True, "Windows OCR via PowerShell"


def _run_ps(script: str, args: List[str], timeout: int = 1800,
            on_line=None) -> Tuple[int, str, str]:
    """Run a PowerShell script; every line it prints is handed to `on_line`
    the moment it appears (a thousand images are minutes of silence
    otherwise). Returns (rc, everything printed, stderr)."""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    lines: List[str] = []
    try:
        p = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", path, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        assert p.stdout is not None
        for raw in p.stdout:
            line = raw.rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        try:
            err = p.communicate(timeout=timeout)[1] or ""
        except subprocess.TimeoutExpired:
            p.kill()
            return 124, "\n".join(lines) + "\n", f"timed out after {timeout}s"
        return p.returncode, "\n".join(lines) + "\n", err
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _ticker(log, what: str, total: int):
    """A callback that says `n/total what` every 10 s as JSON result lines stream past."""
    import time as _t
    state = {"n": 0, "t": _t.time()}

    def on_line(line: str) -> None:
        if line.startswith("{") and ('"path"' in line or '"page"' in line):
            state["n"] += 1
        if log and _t.time() - state["t"] >= 10:
            state["t"] = _t.time()
            log(f"  ... {state['n']}/{total} {what}")
    return on_line


def render_text_png(text: str, out_path: str) -> bool:
    """Draw `text` into a PNG (Windows only). Used by tests and selfcheck."""
    ok, _ = ocr_available()
    if not ok:
        return False
    rc, out, _err = _run_ps(_PS_RENDER, ["-Text", text, "-Out", out_path], timeout=120)
    return rc == 0 and os.path.exists(out_path)


# PDF pages -> PNG with the Windows PDF renderer (Windows.Data.Pdf, part of
# Windows 10/11), so a scanned PDF - or one whose fonts defeat the text
# extractor - can be read by the same OCR engine as the pictures.
_PS_PDF = r"""
param([string]$Pdf, [string]$OutDir, [int]$MaxPages, [int]$Width)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
  [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFolder, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
} catch { Write-Output (@{error="pdf renderer unavailable: $_"} | ConvertTo-Json -Compress); exit 0 }
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
$asTaskGeneric = ($methods | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ($methods | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function Await($WinRtTask, $ResultType) { $asTask = $asTaskGeneric.MakeGenericMethod($ResultType); $netTask = $asTask.Invoke($null, @($WinRtTask)); $netTask.Wait(-1) | Out-Null; $netTask.Result }
function AwaitAction($WinRtAction) { $netTask = $asTaskAction.Invoke($null, @($WinRtAction)); $netTask.Wait(-1) | Out-Null }
try {
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Pdf)) ([Windows.Storage.StorageFile])
  $doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
  $folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($OutDir)) ([Windows.Storage.StorageFolder])
} catch { Write-Output (@{error="cannot open: $_"} | ConvertTo-Json -Compress); exit 0 }
$total = [int]$doc.PageCount
$n = [Math]::Min($total, $MaxPages)
Write-Output (@{pages=$total; rendering=$n} | ConvertTo-Json -Compress)
for ($i = 0; $i -lt $n; $i++) {
  try {
    $page = $doc.GetPage([uint32]$i)
    $name = "page-{0:d4}.png" -f ($i + 1)
    $out = Await ($folder.CreateFileAsync($name, [Windows.Storage.CreationCollisionOption]::ReplaceExisting)) ([Windows.Storage.StorageFile])
    $stream = Await ($out.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) ([Windows.Storage.Streams.IRandomAccessStream])
    $opts = New-Object Windows.Data.Pdf.PdfPageRenderOptions
    $opts.DestinationWidth = [uint32]$Width
    AwaitAction ($page.RenderToStreamAsync($stream, $opts))
    $stream.Dispose(); $page.Dispose()
    Write-Output (@{page=($i + 1); path=(Join-Path $OutDir $name)} | ConvertTo-Json -Compress)
  } catch { Write-Output (@{page=($i + 1); error="$_"} | ConvertTo-Json -Compress) }
}
"""

PDF_MAX_PAGES = 400        # per document; more is almost never a specification
PDF_RENDER_WIDTH = 1700    # pixels across the page: enough for 9-point print


def render_pdf_pages(pdf_path: str, out_dir: str, max_pages: int = PDF_MAX_PAGES,
                     width: int = PDF_RENDER_WIDTH, log=None) -> Tuple[int, List[Tuple[int, str]], List[str]]:
    """Render a PDF's pages to page-NNNN.png under out_dir with the Windows
    PDF renderer. Returns (pages in the document, [(page, png path)], warnings)."""
    ok, why = ocr_available()
    if not ok:
        return 0, [], [why]
    os.makedirs(out_dir, exist_ok=True)
    rc, out, err = _run_ps(_PS_PDF, ["-Pdf", os.path.abspath(pdf_path), "-OutDir", os.path.abspath(out_dir),
                                     "-MaxPages", str(max_pages), "-Width", str(width)], timeout=3600,
                           on_line=_ticker(log, f"pages of {os.path.basename(pdf_path)} rendered", max_pages))
    pages: List[Tuple[int, str]] = []
    warnings: List[str] = []
    total = 0
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if "pages" in obj:
            total = int(obj["pages"])
        elif "path" in obj:
            pages.append((int(obj["page"]), obj["path"]))
        elif "error" in obj:
            warnings.append(f"{os.path.basename(pdf_path)}" + (f" page {obj['page']}" if "page" in obj else "") + f": {obj['error']}")
    if rc != 0 and not pages:
        warnings.append(f"pdf render rc {rc}: {err.strip()[:200]}")
    if total > max_pages:
        warnings.append(f"{os.path.basename(pdf_path)}: {total} pages, only the first {max_pages} rendered")
    return total, pages, warnings


def ocr_images(paths: List[str], log=None) -> Tuple[Dict[str, str], List[str]]:
    """{path: text} for every image Windows OCR could read, plus warnings.
    With `log`, a progress line every 10 s."""
    ok, why = ocr_available()
    if not ok or not paths:
        return {}, ([] if ok else [why])
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(os.path.abspath(p) for p in paths))
        listfile = fh.name
    if log:
        log(f"  OCR engine starting on {len(paths)} image(s) - a line every 10 s")
    try:
        rc, out, err = _run_ps(_PS_OCR, ["-ListFile", listfile], timeout=max(1800, 3 * len(paths)),
                               on_line=_ticker(log, "images read", len(paths)))
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass
    texts: Dict[str, str] = {}
    warnings: List[str] = []
    if rc != 0 and not out.strip():
        return {}, [f"powershell rc {rc}: {err.strip()[:200]}"]
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if "engine" in obj and obj["engine"] in ("unavailable", "none"):
            return {}, [f"OCR engine {obj['engine']}: {obj.get('error', '')}"]
        if "path" in obj:
            raw = obj["path"]
            if isinstance(raw, dict):                  # PS note-property serialisation
                raw = raw.get("value") or raw.get("Value") or ""
            key = os.path.normcase(os.path.abspath(str(raw)))
            if obj.get("error"):
                warnings.append(f"{os.path.basename(str(raw))}: {str(obj['error'])[:120]}")
            else:
                texts[key] = (obj.get("text") or "").strip()
    return texts, warnings


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def _pdf_needs_pages(parse_error: Optional[str]) -> bool:
    """The text extractor gave up (a scan) or warned (fonts it cannot decode)."""
    note = parse_error or ""
    return "no extractable text" in note or "may be garbled" in note


def extract_images(conn: sqlite3.Connection, out_dir: str, member: Optional[str] = None,
                   pdf_pages: str = "scans", log=print) -> List[Tuple[int, str, str, str]]:
    """Pull images out of the indexed Office documents (and pick up standalone
    image files); render the pages of PDFs the text extractor could not read
    (`pdf_pages`: scans | all | none). Returns (member_id, member_name,
    image_name, extracted_path)."""
    q = "SELECT id, name, path, ext, parse_error FROM member WHERE kind='doc'"
    args: tuple = ()
    if member:
        q += " AND UPPER(name)=?"
        args = (member.upper(),)
    out: List[Tuple[int, str, str, str]] = []
    for mid, name, path, ext, perr in conn.execute(q, args).fetchall():
        ext = (ext or "").lower().lstrip(".")
        dest_dir = os.path.join(out_dir, name)
        marker = os.path.join(out_dir, ".atlas-output")
        if not os.path.exists(marker):
            # the build skips folders carrying this marker: extracted images
            # must never be re-indexed as documents
            os.makedirs(out_dir, exist_ok=True)
            with open(marker, "w") as mh:
                mh.write("written by atlas.ocr; never indexed\n")
        if ext in _MEDIA_PREFIX:
            try:
                with zipfile.ZipFile(path) as z:
                    for n in z.namelist():
                        if n.startswith(_MEDIA_PREFIX[ext]) and n.lower().endswith(IMAGE_EXT):
                            info = z.getinfo(n)
                            floor = MIN_VECTOR_BYTES if n.lower().endswith((".emf", ".wmf")) else MIN_BYTES
                            if info.file_size < floor:
                                continue
                            os.makedirs(dest_dir, exist_ok=True)
                            dest = os.path.join(dest_dir, os.path.basename(n))
                            with z.open(n) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            out.append((mid, name, n, dest))
            except zipfile.BadZipFile:
                continue
        elif ext in [e.lstrip(".") for e in IMAGE_EXT]:
            if os.path.getsize(path) >= (MIN_VECTOR_BYTES if ext in ("emf", "wmf") else MIN_BYTES):
                out.append((mid, name, os.path.basename(path), path))
        elif ext == "pdf" and pdf_pages != "none" and (pdf_pages == "all" or _pdf_needs_pages(perr)):
            done = conn.execute("SELECT COUNT(*) FROM doc_image WHERE member_id=? AND name LIKE 'page-%' AND ocr_text IS NOT NULL",
                                (mid,)).fetchone()[0]
            if done:
                continue                                    # pages already read on an earlier run
            log(f"  {name}: rendering PDF pages for OCR ...")
            total, pages, warns = render_pdf_pages(path, dest_dir, log=log)
            for w in warns[:5]:
                log("  " + w)
            if pages:
                log(f"  {name}: {len(pages)} of {total} page(s) rendered for OCR")
            out += [(mid, name, f"page-{p:04d}", png) for p, png in pages]
    for mid, _n, img, dest in out:
        conn.execute("UPDATE doc_image SET extracted_path=? WHERE member_id=? AND name=?", (dest, mid, img))
        if conn.execute("SELECT 1 FROM doc_image WHERE member_id=? AND name=?", (mid, img)).fetchone() is None:
            conn.execute("INSERT INTO doc_image(member_id,name,extracted_path) VALUES(?,?,?)", (mid, img, dest))
    conn.commit()
    return out


def image_heading(img: str, anchor: Optional[str] = None) -> str:
    """The section heading of an OCR'd picture: which image, and where it
    sits (the sheet, slide or heading) - `image: image3.png (sheet: Gender Tests)`."""
    if img.startswith("page-") and img[5:].isdigit():
        return f"page {int(img[5:])} (OCR of the scanned page)"
    return f"image: {os.path.basename(img)}" + (f" ({anchor})" if anchor else "")



# A picture the OCR engine cannot decode, turned into one it can: a metafile
# (EMF/WMF - what Word and Visio store a pasted diagram as) is drawn onto a
# white bitmap at twice its size, and every frame of a multi-page TIFF is
# written out. One line per result: OK<TAB>source<TAB>png  or  FAIL<TAB>source<TAB>why
_PS_TO_PNG = r'''
param([string]$ListFile)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
foreach ($p in Get-Content -LiteralPath $ListFile) {
  if (-not $p) { continue }
  try {
    $img = [System.Drawing.Image]::FromFile($p)
    $dir = Split-Path -Parent $p
    $base = [System.IO.Path]::GetFileNameWithoutExtension($p)
    $frames = 1
    try {
      $dim = New-Object System.Drawing.Imaging.FrameDimension $img.FrameDimensionsList[0]
      $frames = $img.GetFrameCount($dim)
    } catch { $frames = 1 }
    for ($i = 0; $i -lt $frames; $i++) {
      if ($frames -gt 1) { $img.SelectActiveFrame($dim, $i) | Out-Null }
      $w = [Math]::Min(6000, [Math]::Max(1000, $img.Width * 2))
      $h = [Math]::Min(6000, [Math]::Max(300, [int]($img.Height * ($w / $img.Width))))
      $bmp = New-Object System.Drawing.Bitmap($w, $h)
      $bmp.SetResolution(300, 300)
      $g = [System.Drawing.Graphics]::FromImage($bmp)
      $g.Clear([System.Drawing.Color]::White)
      $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
      $g.DrawImage($img, 0, 0, $w, $h)
      $g.Dispose()
      $suffix = if ($frames -gt 1) { "-p{0:d3}" -f ($i + 1) } else { "" }
      $out = Join-Path $dir ($base + $suffix + ".ocr.png")
      $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
      $bmp.Dispose()
      [Console]::Out.WriteLine("OK`t$p`t$out")
    }
    $img.Dispose()
  } catch {
    [Console]::Out.WriteLine("FAIL`t$p`t" + $_.Exception.Message)
  }
}
'''

CONVERT_EXT = (".emf", ".wmf", ".tif", ".tiff")     # the OCR engine decodes none of these


def to_readable_images(paths: List[str], log=None) -> Dict[str, List[str]]:
    """{original: [png ...]} for pictures the OCR engine cannot decode: EMF and
    WMF diagrams (what Word and Visio store a pasted drawing as - the engine
    fails on them outright) and multi-page TIFF scans (it reads only the first
    page). Anything that cannot be converted is left out and stays unread."""
    want = [p for p in paths if p.lower().endswith(CONVERT_EXT)]
    if not want:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(os.path.abspath(p) for p in want))
        listfile = fh.name
    if log:
        log(f"  converting {len(want)} diagram(s) / multi-page scan(s) the OCR engine cannot decode")
    try:
        rc, out, err = _run_ps(_PS_TO_PNG, ["-ListFile", listfile], timeout=max(600, 5 * len(want)))
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass
    made: Dict[str, List[str]] = {}
    for line in (out or "").splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) == 3 and parts[0] == "OK" and os.path.isfile(parts[2]):
            made.setdefault(os.path.normcase(os.path.abspath(parts[1])), []).append(parts[2])
        elif len(parts) == 3 and parts[0] == "FAIL" and log:
            log(f"  cannot convert {os.path.basename(parts[1])}: {parts[2][:120]}")
    if rc != 0 and log:
        log(f"  image conversion ended with {rc}: {(err or '').strip()[:200]}")
    if log:
        log(f"  converted {sum(len(v) for v in made.values())} image(s) for the OCR engine")
    return made


def run(conn: sqlite3.Connection, out_dir: str, do_ocr: bool = True, member: Optional[str] = None,
        log=print, pdf_pages: str = "scans") -> Dict[str, int]:
    images = extract_images(conn, out_dir, member, pdf_pages, log)
    stats = {"images": len(images), "ocr_text": 0, "ocr_empty": 0, "ocr_failed": 0}
    log(f"images extracted: {len(images)} -> {out_dir}")
    if not do_ocr or not images:
        return stats
    ok, why = ocr_available()
    if not ok:
        log("OCR skipped: " + why)
        return stats
    todo = [(mid, name, img, dest) for (mid, name, img, dest) in images
            if conn.execute("SELECT ocr_text FROM doc_image WHERE member_id=? AND name=?", (mid, img)).fetchone()[0] is None]
    log(f"OCR: {len(todo)} image(s) to read ({len(images) - len(todo)} already done)")
    converted = to_readable_images([d for (_m, _n, _i, d) in todo], log=log)
    read_paths: List[str] = []
    for _mid, _name, _img, dest in todo:
        read_paths.extend(converted.get(os.path.normcase(os.path.abspath(dest)), [dest]))
    texts, warnings = ocr_images(read_paths, log=log)
    for w in warnings[:20]:
        log("  " + w)
    for mid, name, img, dest in todo:
        key = os.path.normcase(os.path.abspath(dest))
        parts = [texts.get(os.path.normcase(os.path.abspath(p))) for p in converted.get(key, [dest])]
        got = [p for p in parts if p is not None]
        text = "\n".join(p for p in got if p) if got else None       # several pages of one scan read as one
        if text is None:
            stats["ocr_failed"] += 1
            continue
        conn.execute("UPDATE doc_image SET ocr_text=? WHERE member_id=? AND name=?", (text, mid, img))
        if not text:
            stats["ocr_empty"] += 1
            continue
        n = conn.execute("SELECT COUNT(*) FROM doc_section WHERE member_id=? AND ordinal>=?",
                         (mid, OCR_ORDINAL_BASE)).fetchone()[0]
        ordinal = OCR_ORDINAL_BASE + n + 1
        anchor = conn.execute("SELECT anchor FROM doc_image WHERE member_id=? AND name=?", (mid, img)).fetchone()
        heading = image_heading(img, anchor[0] if anchor else None)
        conn.execute("INSERT INTO doc_section(member_id,heading,text,ordinal) VALUES(?,?,?,?)",
                     (mid, heading, text, ordinal))
        from .build import fts_insert
        fts_insert(conn, mid, [(name, "doc", mid, ordinal, (heading + "\n" + text)[:20000])])
        stats["ocr_text"] += 1
    conn.commit()
    log(f"OCR: {stats['ocr_text']} with text, {stats['ocr_empty']} empty, {stats['ocr_failed']} failed")
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Extract and OCR the images inside indexed documents (Windows OCR, no tokens).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--out", default=os.path.join("out", "images"))
    ap.add_argument("--member", help="only this document")
    ap.add_argument("--no-ocr", action="store_true", help="extract images only")
    ap.add_argument("--pdf-pages", choices=("scans", "all", "none"), default="scans",
                    help="render PDF pages for OCR: only PDFs whose text could not be extracted (default), all PDFs, or none")
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"no such db: {a.db}")
        return 1
    conn = sqlite3.connect(a.db)
    try:
        run(conn, a.out, do_ocr=not a.no_ocr, member=a.member, pdf_pages=a.pdf_pages)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
