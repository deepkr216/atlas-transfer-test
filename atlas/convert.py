"""
convert.py - save legacy Office files (.doc / .xls / .ppt) as .docx / .xlsx /
.pptx, a whole folder tree at a time, using the Office already installed on
the laptop (COM automation through PowerShell - nothing to install), or
LibreOffice when Office is absent. The modern copy is written NEXT TO the
original; nothing is deleted or modified. The build then indexes the modern
copy and skips the legacy one.

    python -m atlas.convert C:\\docs              # unzip archives, then convert everything under the folder
    python -m atlas.convert C:\\docs --dry-run    # list what would be done, touch nothing

Archives: every .zip (zips inside zips too) is extracted first into a folder
named <archive>.unzipped beside it, so the documents inside are converted and
indexed like the rest; an archive already extracted from the same bytes is
skipped.

Files that are password-protected, corrupt, or locked by another user are
reported as FAIL with Office's own message and left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Callable, List, Optional, Tuple

from .docs import LEGACY_TO_MODERN

SKIP_DIRS = {"out", "atlas_out", ".git", "__pycache__", ".stale"}

UNZIP_SUFFIX = ".unzipped"                 # specs.zip -> specs.unzipped\ beside it
UNZIP_MARKER = ".atlas-unzipped.json"      # which zip (size, mtime) the folder came from
MAX_UNZIP_BYTES = 4 * 1024 ** 3            # per archive


def _safe_member(name: str) -> Optional[str]:
    """A zip entry's relative path, or None when it would escape the folder."""
    rel = name.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or (len(rel) > 1 and rel[1] == ":"):
        return None
    return os.path.join(*parts)


def unzip_tree(root: str, log: Callable[[str], None] = print, dry_run: bool = False,
               max_depth: int = 5) -> Tuple[int, int, int]:
    """Extract every .zip under `root` (subfolders included, zips inside zips
    too) into `<name>.unzipped` beside the archive, so the documents in it
    can be converted and indexed like any other. An archive already
    extracted from the same bytes is skipped; nothing is deleted. Returns
    (extracted, up to date, failed)."""
    done = have = failed = 0
    seen: set = set()
    for _round in range(max_depth):
        zips: List[str] = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            zips += [os.path.join(dirpath, f) for f in sorted(files) if f.lower().endswith(".zip")]
        fresh = [z for z in zips if z not in seen]
        if not fresh:
            break
        for zpath in fresh:
            seen.add(zpath)
            dest = os.path.splitext(zpath)[0] + UNZIP_SUFFIX
            try:
                st = os.stat(zpath)
                stamp = {"zip": os.path.basename(zpath), "size": st.st_size, "mtime": int(st.st_mtime)}
            except OSError as e:
                log(f"  FAIL  {zpath}: {e}")
                failed += 1
                continue
            marker = os.path.join(dest, UNZIP_MARKER)
            if os.path.isdir(dest) and os.path.exists(marker):
                try:
                    with open(marker, encoding="utf-8") as fh:
                        if json.load(fh) == stamp:
                            have += 1
                            log(f"  have  {zpath} -> {os.path.basename(dest)} (up to date)")
                            continue
                except (OSError, ValueError):
                    pass
            if dry_run:
                log(f"  todo  {zpath} -> {os.path.basename(dest)}")
                continue
            try:
                total = 0
                with zipfile.ZipFile(zpath) as z:
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        rel = _safe_member(info.filename)
                        if rel is None:
                            log(f"  skip  {zpath}: entry outside the folder ignored: {info.filename}")
                            continue
                        total += info.file_size
                        if total > MAX_UNZIP_BYTES:
                            raise ValueError(f"more than {MAX_UNZIP_BYTES // 1024 ** 3} GB - not extracted further")
                        target = os.path.join(dest, rel)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with z.open(info) as src, open(target, "wb") as out:
                            shutil.copyfileobj(src, out)
                os.makedirs(dest, exist_ok=True)
                with open(marker, "w", encoding="utf-8") as fh:
                    json.dump(stamp, fh)
                done += 1
                log(f"  OK    {zpath} -> {os.path.basename(dest)}")
            except (OSError, zipfile.BadZipFile, ValueError) as e:
                failed += 1
                log(f"  FAIL  {zpath}: {e}")
    return done, have, failed

# One PowerShell process for the whole tree: Word / Excel / PowerPoint are
# started once each, on first use, and closed at the end. Files are opened
# read-only, macros disabled (AutomationSecurity=3), alerts off, so nothing
# pops up and nothing in the original can run.
_PS_SCRIPT = r'''
param([string]$ListFile)
$ErrorActionPreference = 'Continue'
$script:word = $null
$script:excel = $null
$script:ppt = $null

function Get-Word {
    if ($script:word -eq $null) {
        $script:word = New-Object -ComObject Word.Application
        $script:word.Visible = $false
        $script:word.DisplayAlerts = 0
        try { $script:word.AutomationSecurity = 3 } catch {}
    }
    return $script:word
}
function Get-Excel {
    if ($script:excel -eq $null) {
        $script:excel = New-Object -ComObject Excel.Application
        $script:excel.Visible = $false
        $script:excel.DisplayAlerts = $false
        try { $script:excel.AutomationSecurity = 3 } catch {}
    }
    return $script:excel
}
function Get-PPT {
    if ($script:ppt -eq $null) {
        $script:ppt = New-Object -ComObject PowerPoint.Application
        try { $script:ppt.AutomationSecurity = 3 } catch {}
    }
    return $script:ppt
}

$pairs = Get-Content -LiteralPath $ListFile -Encoding UTF8 | Where-Object { $_ -ne '' }
foreach ($line in $pairs) {
    $src, $dst = $line -split "`t", 2
    $ext = [System.IO.Path]::GetExtension($src).ToLower()
    try {
        switch ($ext) {
            '.doc' { $w = Get-Word; $d = $w.Documents.Open($src, $false, $true); $d.SaveAs2([ref]$dst, [ref]12); $d.Close(0) }
            '.xls' { $x = Get-Excel; $b = $x.Workbooks.Open($src, 0, $true); $b.SaveAs($dst, 51); $b.Close($false) }
            '.ppt' { $p = Get-PPT; $r = $p.Presentations.Open($src, -1, 0, 0); $r.SaveAs($dst, 24); $r.Close() }
        }
        if (Test-Path -LiteralPath $dst) { Write-Output "OK`t$src`t$dst" } else { Write-Output "FAIL`t$src`tno output written" }
    } catch {
        $msg = $_.Exception.Message -replace "[`r`n]+", " "
        Write-Output "FAIL`t$src`t$msg"
    }
}
foreach ($app in @($script:word, $script:excel, $script:ppt)) {
    if ($app -ne $null) { try { $app.Quit() } catch {} }
}
'''


def plan(root: str) -> List[Tuple[str, str, str]]:
    """(source, target, 'convert' | 'exists') for every legacy Office file
    under `root`, subfolders included. `exists` = a modern copy is already
    beside it, so nothing to do."""
    out: List[Tuple[str, str, str]] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        lower = {f.lower() for f in files}
        for fn in sorted(files):
            stem, ext = os.path.splitext(fn)
            ext = ext.lower()
            if ext not in LEGACY_TO_MODERN or fn.startswith("~$"):
                continue
            target = stem + LEGACY_TO_MODERN[ext]
            src_path, dst_path = os.path.join(dirpath, fn), os.path.join(dirpath, target)
            if target.lower() in lower:
                # both exist: the copy is current unless the legacy file was
                # changed AFTER the copy was made - then the copy is stale
                try:
                    stale = os.path.getmtime(src_path) > os.path.getmtime(dst_path) + 1
                except OSError:
                    stale = False
                status = "stale" if stale else "exists"
            else:
                status = "convert"
            out.append((src_path, dst_path, status))
    return out


def parse_output(text: str) -> List[Tuple[str, str, str]]:
    """OK / FAIL lines from the PowerShell script -> (status, source, detail)."""
    rows: List[Tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.rstrip("\r").split("\t", 2)
        if len(parts) == 3 and parts[0] in ("OK", "FAIL"):
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _run_powershell(pairs: List[Tuple[str, str]], timeout: int) -> Tuple[int, str, str]:
    if shutil.which("powershell") is None:
        return 127, "", "powershell.exe not found on PATH"
    td = tempfile.mkdtemp(prefix="atlas-convert-")
    script = os.path.join(td, "convert.ps1")
    listing = os.path.join(td, "files.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_PS_SCRIPT)
    with open(listing, "w", encoding="utf-8") as fh:
        for src, dst in pairs:
            fh.write(f"{src}\t{dst}\n")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", script, "-ListFile", listing],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _run_libreoffice(pairs: List[Tuple[str, str]], timeout: int, log: Callable[[str], None]) -> List[Tuple[str, str, str]]:
    """Fallback when Office is not installed: `soffice --headless --convert-to`."""
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    rows: List[Tuple[str, str, str]] = []
    if not soffice:
        return rows
    for src, dst in pairs:
        fmt = os.path.splitext(dst)[1].lstrip(".")
        try:
            p = subprocess.run([soffice, "--headless", "--convert-to", fmt, "--outdir", os.path.dirname(dst), src],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               stdin=subprocess.DEVNULL)
            ok = p.returncode == 0 and os.path.exists(dst)
            rows.append(("OK" if ok else "FAIL", src, dst if ok else (p.stderr or p.stdout).strip()[:200]))
        except subprocess.TimeoutExpired:
            rows.append(("FAIL", src, f"timed out after {timeout}s"))
        log(f"  {rows[-1][0]:<4} {os.path.basename(src)}")
    return rows


def convert_tree(root: str, dry_run: bool = False, log: Callable[[str], None] = print,
                 timeout: int = 3600, refresh: bool = False) -> Tuple[int, int, int]:
    """Convert every legacy Office file under `root`. Returns
    (converted, already present, failed). A copy older than its legacy
    original is reported as STALE and left alone unless `refresh`."""
    items = plan(root)
    todo = [(s, d) for s, d, st in items if st == "convert" or (refresh and st == "stale")]
    have = sum(1 for _s, _d, st in items if st == "exists")
    stale = [s for s, _d, st in items if st == "stale"]
    log(f"{len(items)} legacy Office file(s) under {root}: {len(todo)} to convert, {have} already have a modern copy"
        + (f", {len(stale)} copies STALE (the legacy file changed after the copy was made)" if stale else ""))
    if stale and not refresh:
        for s in stale:
            log(f"  STALE {s} - its converted copy is older than it; run with --refresh to remake the copy")
    if dry_run or not todo:
        for s, d, st in items:
            tag = {"exists": "have ", "stale": "stale", "convert": "todo "}[st]
            log(f"  {tag} {s} -> {os.path.basename(d)}")
        return 0, have, 0
    rc, out, err = _run_powershell(todo, timeout)
    rows = parse_output(out)
    com_missing = rc != 0 and not rows or any("80040154" in d or "Cannot create" in d or "COM class factory" in d
                                              for _s, _src, d in rows if _s == "FAIL")
    if com_missing and (shutil.which("soffice") or shutil.which("soffice.exe")):
        log("  Office automation is not available here - trying LibreOffice (soffice --headless)")
        done = {src for st, src, _d in rows if st == "OK"}
        rows = [r for r in rows if r[0] == "OK"] + _run_libreoffice([(s, d) for s, d in todo if s not in done], timeout, log)
    elif rc != 0 and not rows:
        log(f"  powershell rc {rc}: {(err or out).strip()[:300]}")
        log("  Is Microsoft Office installed on this laptop? Without it (or LibreOffice on PATH) nothing can "
            "open a .doc/.xls/.ppt - save them as .docx/.xlsx/.pptx from the application once.")
        return 0, have, len(todo)
    ok = sum(1 for st, _s, _d in rows if st == "OK")
    for st, src, detail in rows:
        log(f"  {st:<4} {src}" + ("" if st == "OK" else f": {detail}"))
    missing = [s for s, _d in todo if s not in {src for _st, src, _d in rows}]
    for s in missing:
        log(f"  FAIL {s}: no result reported")
    return ok, have, len(todo) - ok


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Save legacy Office files (.doc/.xls/.ppt) as .docx/.xlsx/.pptx beside the originals.")
    ap.add_argument("folder", nargs="+", help="folder(s) to convert, subfolders included")
    ap.add_argument("--dry-run", action="store_true", help="list what would be converted; touch nothing")
    ap.add_argument("--refresh", action="store_true",
                    help="also remake a copy whose legacy original changed after the copy was made (overwrites the copy)")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--no-unzip", action="store_true", help="do not extract .zip archives first")
    a = ap.parse_args(argv)
    tot_ok = tot_have = tot_fail = 0
    for folder in a.folder:
        if not os.path.isdir(folder):
            print(f"not a folder: {folder}")
            return 2
        if not a.no_unzip:
            z_ok, z_have, z_fail = unzip_tree(folder, print, a.dry_run)
            print(f"archives: {z_ok} extracted, {z_have} already extracted, {z_fail} failed")
            tot_fail += z_fail
        ok, have, fail = convert_tree(folder, a.dry_run, print, a.timeout, a.refresh)
        tot_ok, tot_have, tot_fail = tot_ok + ok, tot_have + have, tot_fail + fail
    print(f"converted {tot_ok}, already present {tot_have}, failed {tot_fail}")
    return 1 if tot_fail else 0


if __name__ == "__main__":
    sys.exit(main())
