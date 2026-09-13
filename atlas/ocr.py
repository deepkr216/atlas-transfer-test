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

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")
MIN_BYTES = 2048           # smaller than this is an icon or a bullet, not content
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


def _run_ps(script: str, args: List[str], timeout: int = 1800) -> Tuple[int, str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", path, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def render_text_png(text: str, out_path: str) -> bool:
    """Draw `text` into a PNG (Windows only). Used by tests and selfcheck."""
    ok, _ = ocr_available()
    if not ok:
        return False
    rc, out, _err = _run_ps(_PS_RENDER, ["-Text", text, "-Out", out_path], timeout=120)
    return rc == 0 and os.path.exists(out_path)


def ocr_images(paths: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """{path: text} for every image Windows OCR could read, plus warnings."""
    ok, why = ocr_available()
    if not ok or not paths:
        return {}, ([] if ok else [why])
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(os.path.abspath(p) for p in paths))
        listfile = fh.name
    try:
        rc, out, err = _run_ps(_PS_OCR, ["-ListFile", listfile])
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

def extract_images(conn: sqlite3.Connection, out_dir: str, member: Optional[str] = None
                   ) -> List[Tuple[int, str, str, str]]:
    """Pull images out of the indexed Office documents (and pick up standalone
    image files). Returns (member_id, member_name, image_name, extracted_path)."""
    q = "SELECT id, name, path, ext FROM member WHERE kind='doc'"
    args: tuple = ()
    if member:
        q += " AND UPPER(name)=?"
        args = (member.upper(),)
    out: List[Tuple[int, str, str, str]] = []
    for mid, name, path, ext in conn.execute(q, args).fetchall():
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
                            if info.file_size < MIN_BYTES:
                                continue
                            os.makedirs(dest_dir, exist_ok=True)
                            dest = os.path.join(dest_dir, os.path.basename(n))
                            with z.open(n) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            out.append((mid, name, n, dest))
            except zipfile.BadZipFile:
                continue
        elif ext in [e.lstrip(".") for e in IMAGE_EXT]:
            if os.path.getsize(path) >= MIN_BYTES:
                out.append((mid, name, os.path.basename(path), path))
    for mid, _n, img, dest in out:
        conn.execute("UPDATE doc_image SET extracted_path=? WHERE member_id=? AND name=?", (dest, mid, img))
        if conn.execute("SELECT 1 FROM doc_image WHERE member_id=? AND name=?", (mid, img)).fetchone() is None:
            conn.execute("INSERT INTO doc_image(member_id,name,extracted_path) VALUES(?,?,?)", (mid, img, dest))
    conn.commit()
    return out


def run(conn: sqlite3.Connection, out_dir: str, do_ocr: bool = True, member: Optional[str] = None,
        log=print) -> Dict[str, int]:
    images = extract_images(conn, out_dir, member)
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
    texts, warnings = ocr_images([d for (_m, _n, _i, d) in todo])
    for w in warnings[:20]:
        log("  " + w)
    for mid, name, img, dest in todo:
        text = texts.get(os.path.normcase(os.path.abspath(dest)))
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
        heading = f"image: {os.path.basename(img)}"
        conn.execute("INSERT INTO doc_section(member_id,heading,text,ordinal) VALUES(?,?,?,?)",
                     (mid, heading, text, ordinal))
        conn.execute("INSERT INTO src_fts(member_name,kind,member_id,line_no,text) VALUES(?,?,?,?,?)",
                     (name, "doc", mid, ordinal, (heading + "\n" + text)[:20000]))
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
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"no such db: {a.db}")
        return 1
    conn = sqlite3.connect(a.db)
    try:
        run(conn, a.out, do_ocr=not a.no_ocr, member=a.member)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
