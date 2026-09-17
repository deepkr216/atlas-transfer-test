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
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from .docs import LEGACY_TO_MODERN, long_path

SKIP_DIRS = {"out", "atlas_out", ".git", "__pycache__", ".stale"}

UNZIP_SUFFIX = ".unzipped"                 # specs.zip -> specs.unzipped\ beside it
UNZIP_MARKER = ".atlas-unzipped.json"      # which zip (size, mtime) the folder came from
MAX_UNZIP_BYTES = 4 * 1024 ** 3            # per archive


def _safe_member(name: str) -> Optional[str]:
    """A zip entry's relative path, or None when it would escape the folder.
    Every part is a name Windows accepts (a folder ending with a space is
    created without it, and the file inside it then 'does not exist')."""
    from .ocr import folder_name
    rel = name.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or (len(rel) > 1 and rel[1] == ":"):
        return None
    return os.path.join(*[folder_name(p) for p in parts])


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
param([string]$ListFile, [int]$Visible = 0)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$script:show = ($Visible -eq 1)
$script:done = @{ WINWORD = 0; EXCEL = 0; POWERPNT = 0 }
$script:restartEvery = 40      # Office leaks memory over a long run: a fresh instance every N files
function Tick-App([string]$Name) {
    $script:done[$Name] = $script:done[$Name] + 1
    if ($script:done[$Name] % $script:restartEvery -eq 0) {
        try {
            switch ($Name) {
                'WINWORD'  { if ($script:word)  { $script:word.Quit();  $script:word = $null } }
                'EXCEL'    { if ($script:excel) { $script:excel.Quit(); $script:excel = $null } }
                'POWERPNT' { if ($script:ppt)   { $script:ppt.Quit();   $script:ppt = $null } }
            }
        } catch {}
    }
}
$script:word = $null
$script:excel = $null
$script:ppt = $null

function Report-NewPid([string]$Name, $Before) {
    # the process this script just started, so an interrupted run can close
    # exactly that one and leave the user's own Word / Excel alone.
    # Straight to stdout: Write-Output inside a function becomes part of the
    # function's RETURN VALUE, which turned the Word object into a list and
    # broke every file (LESSONS 132).
    $after = @(Get-Process -Name $Name -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    foreach ($id in $after) { if ($Before -notcontains $id) { [Console]::Out.WriteLine("PID`t$Name`t$id") } }
}
function Get-Word {
    if ($script:word -eq $null) {
        $before = @(Get-Process -Name WINWORD -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $script:word = New-Object -ComObject Word.Application
        $script:word.Visible = $script:show
        $script:word.DisplayAlerts = 0
        try { $script:word.AutomationSecurity = 3 } catch {}
        Report-NewPid "WINWORD" $before
    }
    return $script:word
}
function Get-Excel {
    if ($script:excel -eq $null) {
        $before = @(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $script:excel = New-Object -ComObject Excel.Application
        $script:excel.Visible = $script:show
        $script:excel.DisplayAlerts = $false
        try { $script:excel.AutomationSecurity = 3 } catch {}
        Report-NewPid "EXCEL" $before
    }
    return $script:excel
}
function Get-PPT {
    if ($script:ppt -eq $null) {
        $before = @(Get-Process -Name POWERPNT -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $script:ppt = New-Object -ComObject PowerPoint.Application
        try { $script:ppt.AutomationSecurity = 3 } catch {}
        Report-NewPid "POWERPNT" $before
    }
    return $script:ppt
}


# Several ways to open and save, tried in turn: Office's "Command failed"
# never says what it objected to, so the way that works is found by trying,
# reported as a NOTE line, and the FAIL line of a file nothing works for
# carries every attempt's error and the document's own state.
function Close-Matching($app, $src) {
    try { foreach ($od in @($app.Documents)) { if ($od.FullName -eq $src) { $od.Close(0) } } } catch {}
}
function Describe-Doc($d) {
    try { return "ReadOnly=$($d.ReadOnly) HasPassword=$($d.HasPassword) Protection=$($d.ProtectionType) Type=$($d.Type) Compat=$($d.CompatibilityMode)" } catch { return "" }
}
function Convert-Doc($w, $src, $dst) {
    $miss = [System.Reflection.Missing]::Value
    $errs = @()
    $tries = @(
        @{ name = "readonly-open, docx"; open = { param($p) $w.Documents.Open($p, $false, $true, $false, "", "", $false, "", "", $miss, $miss, $script:show, $false, $miss, $true) }; fmt = 12 },
        @{ name = "plain open, docx";    open = { param($p) $w.Documents.Open($p, $false, $true) }; fmt = 12 },
        @{ name = "plain open, default"; open = { param($p) $w.Documents.Open($p, $false, $true) }; fmt = 16 },
        @{ name = "minimal open, docx";  open = { param($p) $w.Documents.Open($p) }; fmt = 12 }
    )
    for ($t = 0; $t -lt $tries.Count; $t++) {
        $try = $tries[$t]
        $info = ""
        try {
            $d = & $try.open $src
            $info = Describe-Doc $d
            $d.SaveAs2([ref]$dst, [ref]$try.fmt)
            $d.Close(0)
            if (Test-Path -LiteralPath $dst) { return "$($try.name)" }
            $errs += "$($try.name): no file written [$info]"
        } catch {
            $m = $_.Exception.Message -replace "[`r`n]+", " "
            $hr = ""; try { $hr = " hr=0x{0:x}" -f $_.Exception.HResult } catch {}
            $errs += "$($try.name): $m$hr [$info]"
            Close-Matching $w $src
        }
    }
    throw ($errs -join " | ")
}
function Convert-Ppt($p, $src, $dst, $win) {
    $errs = @()
    $tries = @(
        @{ name = "no-window open, SaveAs";  run = { param($s, $d) $r = $p.Presentations.Open($s, -1, 0, $win); $r.SaveAs($d, 24); $r.Close() } },
        @{ name = "windowed open, SaveAs";   run = { param($s, $d) $r = $p.Presentations.Open($s, -1, 0, -1); $r.SaveAs($d, 24); $r.Close() } },
        @{ name = "minimal open, SaveCopyAs"; run = { param($s, $d) $r = $p.Presentations.Open($s); $r.SaveCopyAs($d, 24); $r.Close() } }
    )
    for ($t = 0; $t -lt $tries.Count; $t++) {
        $try = $tries[$t]
        try {
            & $try.run $src $dst
            if (Test-Path -LiteralPath $dst) { return "$($try.name)" }
            $errs += "$($try.name): no file written"
        } catch {
            $m = $_.Exception.Message -replace "[`r`n]+", " "
            $errs += "$($try.name): $m"
            try { foreach ($op in @($p.Presentations)) { if ($op.FullName -eq $src) { $op.Close() } } } catch {}
        }
    }
    throw ($errs -join " | ")
}
function Convert-Xls($x, $src, $dst) {
    $miss = [System.Reflection.Missing]::Value
    $errs = @()
    $tries = @(
        @{ name = "readonly-open"; open = { param($p) $x.Workbooks.Open($p, 0, $true, $miss, "", "", $true, $miss, $miss, $false, $false, $miss, $false) } },
        @{ name = "plain open";    open = { param($p) $x.Workbooks.Open($p, 0, $true) } },
        @{ name = "minimal open";  open = { param($p) $x.Workbooks.Open($p) } }
    )
    for ($t = 0; $t -lt $tries.Count; $t++) {
        $try = $tries[$t]
        try {
            $b = & $try.open $src
            $b.SaveAs($dst, 51)
            $b.Close($false)
            if (Test-Path -LiteralPath $dst) { return "$($try.name)" }
            $errs += "$($try.name): no file written"
        } catch {
            $m = $_.Exception.Message -replace "[`r`n]+", " "
            $errs += "$($try.name): $m"
            try { foreach ($ob in @($x.Workbooks)) { if ($ob.FullName -eq $src) { $ob.Close($false) } } } catch {}
        }
    }
    throw ($errs -join " | ")
}

$pairs = Get-Content -LiteralPath $ListFile -Encoding UTF8 | Where-Object { $_ -ne '' }
foreach ($line in $pairs) {
    $src, $dst = $line -split "`t", 2
    $ext = [System.IO.Path]::GetExtension($src).ToLower()
    [Console]::Out.WriteLine("START`t$src")
    try {
        $win = 0; if ($script:show) { $win = -1 }
        # Open READ-ONLY with empty passwords given up front: a file with a
        # "password to modify" or "read-only recommended" opens without the
        # dialog the user would otherwise answer with Read Only; a file with a
        # "password to open" fails at once with Office's own message instead
        # of a prompt nobody can answer. No repair prompts, no encoding
        # dialog, nothing added to the recent-files list.
        $how = ""
        switch ($ext) {
            { $_ -in '.doc', '.dot', '.rtf' } { $w = Get-Word;  $how = Convert-Doc $w $src $dst;      Tick-App 'WINWORD' }
            { $_ -in '.xls', '.xlt' }         { $x = Get-Excel; $how = Convert-Xls $x $src $dst;      Tick-App 'EXCEL' }
            { $_ -in '.ppt', '.pot' }         { $p = Get-PPT;   $how = Convert-Ppt $p $src $dst $win; Tick-App 'POWERPNT' }
        }
        if ($how) { [Console]::Out.WriteLine("NOTE`t$src`tconverted via: $how") }
        if (Test-Path -LiteralPath $dst) { Write-Output "OK`t$src`t$dst" } else { Write-Output "FAIL`t$src`tno output written" }
    } catch {
        $msg = $_.Exception.Message -replace "[`r`n]+", " "
        # the Office instance died under us (killed, crashed): forget it, so
        # the next file starts a fresh one instead of failing the same way
        if ($msg -match "disconnected from its clients|RPC server is unavailable|0x80010108|0x800706BA") {
            switch ($ext) {
                { $_ -in '.doc', '.dot', '.rtf' } { $script:word = $null }
                { $_ -in '.xls', '.xlt' }         { $script:excel = $null }
                { $_ -in '.ppt', '.pot' }         { $script:ppt = $null }
            }
            $msg = "$msg - Office had gone away; a new one is started for the next file, rerun for this one"
        }
        Write-Output "FAIL`t$src`t$msg"
    }
}
foreach ($app in @($script:word, $script:excel, $script:ppt)) {
    if ($app -ne $null) { try { $app.Quit() } catch {} }
}
'''


_MAIN_PART = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}


def modern_copy_is_whole(path: str) -> bool:
    """A .docx / .xlsx / .pptx is a zip with one main part; a copy cut short
    by Ctrl+C or a crash mid-save is neither, and must be made again."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if z.testzip() is not None:
                return False
        return _MAIN_PART.get(ext, "") in names or ext not in _MAIN_PART
    except (OSError, zipfile.BadZipFile):
        return False


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
            if any(ord(c) < 32 for c in src_path):
                out.append((src_path, dst_path, "unusable"))    # a tab or newline in the name: cannot be listed
                continue
            if target.lower() in lower:
                # both exist: the copy is current unless the legacy file was
                # changed AFTER the copy was made (stale) - or the copy is not
                # a whole file (an interrupted save): then it is made again
                try:
                    stale = os.path.getmtime(src_path) > os.path.getmtime(dst_path) + 1
                except OSError:
                    stale = False
                if not modern_copy_is_whole(dst_path):
                    status = "convert"
                else:
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


STALL_SECONDS = 300     # Office silent on one file for this long = a dialog nobody can click


def _ps_command(script: str, listing: str, visible: bool) -> List[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", script, "-ListFile", listing, "-Visible", "1" if visible else "0"]


def _run_powershell(pairs: List[Tuple[str, str]], timeout: int,
                    log: Optional[Callable[[str], None]] = None, visible: bool = False,
                    stall_seconds: int = STALL_SECONDS,
                    names: Optional[Dict[str, Tuple[str, str]]] = None) -> Tuple[int, str, str]:
    """Run the conversion script and report every file THE MOMENT Office is
    done with it. A watchdog ends a run in which Office has gone silent on
    one file for `stall_seconds` (a dialog in an invisible window): that
    file is reported as failed, the Office processes this run started are
    closed, and the caller carries on with the rest. Returns (rc,
    everything the script printed, stderr); rc 124 = stopped by the watchdog."""
    if shutil.which("powershell") is None and _ps_command(".", ".", False)[0] == "powershell":
        return 127, "", "powershell.exe not found on PATH"
    td = tempfile.mkdtemp(prefix="atlas-convert-")
    script = os.path.join(td, "convert.ps1")
    listing = os.path.join(td, "files.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(_PS_SCRIPT)
    with open(listing, "w", encoding="utf-8") as fh:
        for src, dst in pairs:
            fh.write(f"{src}\t{dst}\n")
    if log:
        log(f"  starting Office for {len(pairs)} file(s) - the first answer takes 20-40 s, then one line per file"
            + (" (Office windows visible)" if visible else ""))
    lines: List[str] = []
    office_pids: List[Tuple[str, int]] = []
    current: Optional[str] = None
    p = None
    try:
        p = subprocess.Popen(_ps_command(script, listing, visible),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", stdin=subprocess.DEVNULL)
        assert p.stdout is not None
        q: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump(stream):
            for raw in stream:
                q.put(raw)
            q.put(None)
        threading.Thread(target=pump, args=(p.stdout,), daemon=True).start()
        while True:
            try:
                raw = q.get(timeout=stall_seconds)
            except queue.Empty:
                p.kill()
                _close_office(office_pids, log)
                stalled = current or (pairs[0][0] if pairs else "?")
                why = (f"Office did not finish this file in {stall_seconds} s - usually a dialog in an invisible "
                       f"window (Document Recovery, a repair prompt); rerun with --visible to see and dismiss it")
                lines.append(f"FAIL\t{stalled}\t{why}")
                if log:
                    log(f"  FAIL {names.get(stalled, (stalled, ''))[0] if names else stalled}: {why}")
                return 124, "\n".join(lines) + "\n", "stalled"
            if raw is None:
                break
            line = raw.rstrip("\r\n")
            if line.startswith("PID\t"):
                parts = line.split("\t")
                if len(parts) == 3 and parts[2].isdigit():
                    office_pids.append((parts[1], int(parts[2])))
                continue
            if line.startswith("START\t"):
                current = line.split("\t", 1)[1]
                continue
            if line.startswith("NOTE\t"):
                parts = line.split("\t", 2)
                if log and len(parts) == 3:
                    shown = names.get(parts[1], (parts[1], ""))[0] if names else parts[1]
                    log(f"  note {os.path.basename(shown)}: {parts[2]}")
                continue
            lines.append(line)
            rows = parse_output(line)
            if log and rows:
                st, src, detail = rows[0]
                shown = names.get(src, (src, ""))[0] if names else src
                log(f"  {st:<4} {shown}" + ("" if st == "OK" else f": {detail}"))
        try:
            err = p.communicate(timeout=timeout)[1] or ""
        except subprocess.TimeoutExpired:
            p.kill()
            _close_office(office_pids, log)
            return 124, "\n".join(lines) + "\n", f"timed out after {timeout}s"
        return p.returncode, "\n".join(lines) + "\n", err
    except KeyboardInterrupt:
        if p is not None:
            p.kill()
        _close_office(office_pids, log)
        done = sum(1 for l in lines if l.startswith("OK\t"))
        if log:
            log(f"  interrupted: {done} file(s) converted; Office closed; run the same command again to continue "
                "(finished files are skipped, the one in progress is made again)")
        raise
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _close_office(pids: List[Tuple[str, int]], log: Optional[Callable[[str], None]] = None) -> None:
    """End the Office processes THIS run started (by PID) - never the user's own."""
    for name, pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)
            if log:
                log(f"  closed {name} (pid {pid}) started by this run")
        except (OSError, subprocess.TimeoutExpired):
            pass


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




# --------------------------------------------------------------------------
# Office never works inside OneDrive / SharePoint / a network share: a file
# there is a cloud document to Word (AutoSave, a web address behind the local
# path, Protected View for synced files) and an automated Save As fails with
# "Command failed". Every file is copied to a plain local scratch folder,
# converted there, and the result put back beside the original.
# --------------------------------------------------------------------------

def _stage(pairs: List[Tuple[str, str]], log: Callable[[str], None]) -> Tuple[List[Tuple[str, str]], Dict[str, Tuple[str, str]], List[Tuple[str, str, str]]]:
    """Copy each source to a scratch folder. Returns (staged pairs for
    Office, staged source -> (original source, final destination), rows
    for sources that could not even be read)."""
    root = tempfile.mkdtemp(prefix="atlas-stage-")
    staged: List[Tuple[str, str]] = []
    back: Dict[str, Tuple[str, str]] = {}
    failed: List[Tuple[str, str, str]] = []
    for i, (src, dst) in enumerate(pairs):
        d = os.path.join(root, str(i))
        os.makedirs(d, exist_ok=True)
        ssrc = os.path.join(d, os.path.basename(src))
        sdst = os.path.join(d, os.path.basename(dst))
        try:
            shutil.copyfile(long_path(src), ssrc)   # data only: no Zone.Identifier "mark of the web" travels with it
        except OSError as e:
            failed.append(("FAIL", src, f"could not read the file ({e}) - a OneDrive file not downloaded to this "
                                        "laptop? open the folder once so it syncs, or right-click > Always keep on this device"))
            continue
        staged.append((ssrc, sdst))
        back[ssrc] = (src, dst)
    return staged, back, failed


def _unstage(rows: List[Tuple[str, str, str]], back: Dict[str, Tuple[str, str]],
             log: Callable[[str], None]) -> List[Tuple[str, str, str]]:
    """Move converted results beside their originals; rows re-keyed to the
    original source paths."""
    out: List[Tuple[str, str, str]] = []
    for st, ssrc, detail in rows:
        src, dst = back.get(ssrc, (ssrc, detail))
        if st == "OK":
            produced = detail if os.path.isabs(detail) and os.path.exists(detail) \
                else os.path.join(os.path.dirname(ssrc), os.path.basename(dst))
            if not os.path.exists(produced):
                out.append(("FAIL", src, "Office reported success but wrote no file"))
                continue
            err: Optional[OSError] = None
            for attempt in range(6):            # OneDrive / antivirus hold a new file for a moment
                try:
                    os.replace(produced, long_path(dst))   # an old copy is only replaced by a whole new one
                    out.append(("OK", src, dst))
                    err = None
                    break
                except OSError as e:
                    err = e
                    time.sleep(attempt + 1)
            if err is not None:
                out.append(("FAIL", src, f"converted, but the copy could not be written beside the original ({err})"))
        else:
            out.append((st, src, f"{detail} [{circumstances(src)}]{hint_for(detail)}"))
    return out


_HINTS = [
    (r"blocked|File Block", " -> Office's Trust Center > File Block Settings blocks this old format: untick it "
                            "(File > Options > Trust Center > Trust Center Settings > File Block Settings), then rerun"),
    (r"password", " -> a password to OPEN: nothing can read it without the password; leave it, or save an unprotected copy by hand"),
    (r"corrupt|not a valid|damaged|memory or disk space|unreadable content|cannot open the file",
     " -> the file is damaged, or not really an Office file (an exported report with a .xls name?): open it by hand once"),
    (r"locked for editing|in use|being used|sharing violation",
     " -> the file is open elsewhere (Word, Excel, OneDrive sync): close it and rerun"),
    (r"Command failed", " -> Word's generic refusal; the attempts and the document state above say which way was tried"),
]


def hint_for(detail: str) -> str:
    for rx, hint in _HINTS:
        if re.search(rx, detail, re.IGNORECASE):
            return hint
    return ""


_APP = {".doc": "Word", ".xls": "Excel", ".ppt": "PowerPoint"}


def circumstances(path: str) -> str:
    """What the toolkit knows about a file that Office refused - Office's own
    message ("Command failed") says nothing, the circumstances usually do:
    where the file lives (OneDrive / SharePoint / a share), whether it is a
    cloud placeholder not on the disk, its size, which application."""
    bits: List[str] = []
    low = path.lower()
    if "onedrive" in low or "sharepoint" in low:
        bits.append("path is under OneDrive/SharePoint")
    elif low.startswith("\\\\") or low.startswith("//"):
        bits.append("path is on a network share")
    try:
        st = os.stat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & 0x400000 or attrs & 0x1000:                  # RECALL_ON_DATA_ACCESS / OFFLINE
            bits.append("cloud placeholder - not downloaded to this laptop")
        if attrs & 0x1:
            bits.append("read-only attribute")
        bits.append(f"{st.st_size / 1024:.0f} KB")
    except OSError:
        bits.append("cannot stat the file")
    app = _APP.get(os.path.splitext(path)[1].lower())
    if app:
        bits.append(app)
    return "; ".join(bits)


def _cleanup_stage(back: Dict[str, Tuple[str, str]]) -> None:
    roots = {os.path.dirname(os.path.dirname(s)) for s in back}
    for r in roots:
        shutil.rmtree(r, ignore_errors=True)


def convert_tree(root: str, dry_run: bool = False, log: Callable[[str], None] = print,
                 timeout: int = 3600, refresh: bool = False, visible: bool = False,
                 stall_seconds: int = STALL_SECONDS) -> Tuple[int, int, int]:
    """Convert every legacy Office file under `root`. Returns
    (converted, already present, failed). A copy older than its legacy
    original is reported as STALE and left alone unless `refresh`."""
    items = plan(root)
    todo = [(s, d) for s, d, st in items if st == "convert" or (refresh and st == "stale")]
    have = sum(1 for _s, _d, st in items if st == "exists")
    for s, _d, st in items:
        if st == "unusable":
            log(f"  FAIL {s}: the file name contains a control character - rename it, then rerun")
    stale = [s for s, _d, st in items if st == "stale"]
    log(f"{len(items)} legacy .doc/.xls/.ppt file(s) under {root}: {len(todo)} to convert, {have} already converted earlier "
        f"(.docx/.xlsx/.pptx/.pdf files are read as they are and are not counted here)"
        + (f", {len(stale)} copies STALE (the legacy file changed after the copy was made)" if stale else ""))
    if stale and not refresh:
        for s in stale:
            log(f"  STALE {s} - its converted copy is older than it; run with --refresh to remake the copy")
    if dry_run or not todo:
        for s, d, st in items:
            tag = {"exists": "have ", "stale": "stale", "convert": "todo "}[st]
            log(f"  {tag} {s} -> {os.path.basename(d)}")
        return 0, have, 0
    staged, back, unreadable = _stage(todo, log)
    for _st, src, detail in unreadable:
        log(f"  FAIL {src}: {detail}")
    rc, out, err = _run_powershell(staged, timeout, log, visible, stall_seconds, back)
    srows = parse_output(out)
    restarts = 0
    while rc == 124 and err == "stalled" and restarts < 5:
        # the watchdog stopped Office on one file: go on with the ones not yet attempted
        reported = {src for _st, src, _d in srows}
        rest = [(s, d) for s, d in staged if s not in reported]
        if not rest:
            break
        restarts += 1
        log(f"  restarting Office for the remaining {len(rest)} file(s)")
        rc, out, err = _run_powershell(rest, timeout, log, visible, stall_seconds, back)
        srows += parse_output(out)
    rows = _unstage(srows, back, log) + unreadable
    _cleanup_stage(back)
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
    failed_rows = [(src, detail) for st, src, detail in rows if st == "FAIL"]
    if failed_rows:                                   # a recap of what needs a look, after the live lines
        log(f"  {len(failed_rows)} file(s) not converted:")
        for src, detail in failed_rows:
            log(f"  FAIL {src}: {detail}")
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
    ap.add_argument("--visible", action="store_true",
                    help="show the Word / Excel / PowerPoint windows while converting, to see and dismiss a dialog")
    ap.add_argument("--stall-seconds", type=int, default=STALL_SECONDS,
                    help="give up on a file Office has been silent on for this long (default 300) and go on")
    a = ap.parse_args(argv)
    tot_ok = tot_have = tot_fail = 0
    try:
        return _run_folders(a)
    except KeyboardInterrupt:
        print("interrupted")
        return 130


def _run_folders(a) -> int:
    tot_ok = tot_have = tot_fail = 0
    for folder in a.folder:
        if not os.path.isdir(folder):
            print(f"not a folder: {folder}")
            return 2
        if not a.no_unzip:
            z_ok, z_have, z_fail = unzip_tree(folder, print, a.dry_run)
            print(f"archives: {z_ok} extracted, {z_have} already extracted, {z_fail} failed")
            tot_fail += z_fail
        ok, have, fail = convert_tree(folder, a.dry_run, print, a.timeout, a.refresh, a.visible, a.stall_seconds)
        tot_ok, tot_have, tot_fail = tot_ok + ok, tot_have + have, tot_fail + fail
    print(f"converted {tot_ok}, already present {tot_have}, failed {tot_fail}")
    return 1 if tot_fail else 0


if __name__ == "__main__":
    sys.exit(main())
