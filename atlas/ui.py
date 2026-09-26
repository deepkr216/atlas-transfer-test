"""
ui.py - a small desktop front end for "get the code, build the index".

    python -m atlas.ui                     # uses ./sources.json
    python -m atlas.ui --config C:/estate/sources.json

Tkinter only (ships with Python on Windows), so it runs on a locked-down
laptop with no installs. Everything it does goes through fetch.py, so the
command line and the UI can never behave differently: the table IS
sources.json; "Fetch" runs the same Zowe commands `--plan` prints; "Build"
runs atlas.build with the manifest generated from the table.

The window explains itself: a "What to do" strip lists the five steps in
order and marks the one that is done or possible now; every field has a
grey line under it saying what to type, with an example; every button has
a hover tip; the status line at the bottom says what just happened and
what to do next; Help opens the same in one page.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk
except ImportError:                                   # pragma: no cover
    tk = None                                         # type: ignore

from . import fetch

COLUMNS = ("dataset", "type", "kind", "system", "local", "auth", "enabled", "last")
# what the table says at the top of a column when the key alone is jargon
HEADINGS = {"local": "folder on this laptop", "auth": "production copy", "last": "last fetch"}
WIDTHS = (220, 50, 80, 90, 220, 100, 60, 260)
HELP_FG = "#555"
DONE_FG = "#1a7f37"
LATER_FG = "#8a8a8a"
BAD_FG = "#b00020"

# The toolkit runs the same command the developer's window does, from the
# same folder, and asks for nothing the window does not (LESSONS 179): the
# session password exists for shops whose profile stores none.
SIGN_IN_NOTE = ("Optional: not needed when your zowe command works without a prompt.\n"
                "Used only for the zowe commands of this session - never written to sources.json,\n"
                "the log or a crash file. Leave the user id empty to keep the profile's.")
NO_SESSION_PASSWORD = "no session password (not needed when your zowe command works without a prompt)"
DAEMON_OFF_LABEL = "Zowe daemon off (only if zowe hangs)"

# --------------------------------------------------------------------------
# the words the window shows: steps, help under each field, button tips
# --------------------------------------------------------------------------

# (number, name, what it is and the button it needs)
STEPS = (
    ("1", "Check Zowe", "Does zowe run from here and find your configuration? Button: Check Zowe."),
    ("2", "Add the datasets to fetch", "A dataset name, the folder it goes to, PDS or sequential. Buttons: Add, Bulk add."),
    ("3", "Fetch", "Downloads with the same command your window uses. Buttons: Fetch selected / Fetch system / Fetch all."),
    ("4", "Build", "Reads everything into atlas.db; the first build takes long, later ones are short. Button: Build index."),
    ("5", "Ask", "python -m atlas.query ... or the VS Code tasks; the pack is what Copilot reads."),
)
STATE_WORDS = {"done": "done", "now": "you can do this now", "later": "after step {prev}"}

# One grey line under every field: what to type, and an example.
HELP: Dict[str, str] = {
    "profile": "leave blank unless your window command uses --zosmf-profile, e.g. --zosmf-profile prod",
    "zowe_dir": "the folder your command window is in when zowe works without asking anything, "
                "e.g. the folder you cd to before typing zowe, such as C:\\Users\\you "
                "(blank = the folder of sources.json, this toolkit's folder)",
    "daemon": "tick only if zowe hangs when run from here",
    "sign_in": "Sign in: not needed when your zowe command works without a prompt",
    "root": "the folder on this laptop where the downloaded libraries go, e.g. C:\\estate - one folder per dataset under it",
    "db": "the index file Build writes and every question reads, e.g. atlas.db (a name alone = this toolkit's folder)",
    "extra": "folders of documents already on this laptop (Word, Excel, PDF, pictures), separated by ; "
             "e.g. C:\\docs\\claims;C:\\docs\\policy - indexed as documentation, pictures read by OCR",
    "filter": "show one department only, e.g. CLAIMS - Fetch system then fetches just that one (All = every row)",
    # the source form
    "dataset": "Dataset - the PDS on the host, as your zowe command names it, e.g. PROD.CLAIMS.SRC",
    "type": "pds = a library of members (most datasets); seq = one sequential file, e.g. a CSD extract or a scheduler export",
    "kind": "what the members are, so the build reads them the right way, e.g. cobol for a source library, copybook for a COPYLIB, jcl, proc",
    "system": "the department this library belongs to, e.g. CLAIMS - each department gets its own folder under the local root",
    "local": "Folder - where the members go on this laptop, under the local root, e.g. CLAIMS\\SRC gives estate\\CLAIMS\\SRC "
             "(filled in from the dataset name)",
    "ext": "the ending each member file gets, e.g. cbl - set from the kind; change it only if a tool needs another",
    "extra_args": "Extra zowe args - rarely needed; what you add to your own command, e.g. --encoding 1047",
    "authoritative": "tick for the live production library: when a member name exists in several libraries this copy wins",
    "enabled": "untick to keep the row but skip it on Fetch and Build",
    # sign in
    "user": "your mainframe user id, e.g. DEEPAK - leave empty to keep the one in your zowe profile",
    "password": "used for this session's zowe commands only - never saved anywhere; closing the window forgets it",
    # bulk add
    "bulk_system": "the department these libraries belong to, e.g. CLAIMS",
    "bulk_text": ("Datasets, one per line (SRC, COPYLIB, JCLLIB, PROCLIB, PARMLIB, PSBSOURCE, DBDSOURCE, BMS, MFS, "
                  "CSD extract, STAGE1 ...). PSBLIB/DBDLIB/ACBLIB/LOADLIB are compiled - skip them."),
}

# One sentence per button: what it does and what you will see.
TIPS: Dict[str, str] = {
    "Check Zowe": "Runs zowe from the folder above and asks the host for one member list; the log shows the four "
                  "stages and the status line at the bottom says what to do next.",
    "Sign in...": "Optional: gives zowe a password for this session only, for a shop whose profile stores none; "
                  "nothing is saved anywhere.",
    "Add folder": "Pick a folder of documents to index with the code; it is added to the list and saved.",
    "Add": "Opens a form for one dataset - name, type, kind, department, folder; the row is saved to sources.json.",
    "Bulk add": "Paste a department's dataset list, one per line; kinds are guessed from the names and compiled "
                "libraries are added disabled.",
    "Edit": "Opens the selected row for changes (double-clicking the row does the same).",
    "Remove": "Takes the selected rows out of the table after asking; files already downloaded stay on disk.",
    "Enable/Disable": "Keeps the selected rows but skips them (or includes them again) on Fetch and Build.",
    "Move up": "Moves the selected row up; for copybook libraries the order is the search order (first match wins).",
    "Move down": "Moves the selected row down; for copybook libraries the order is the search order (first match wins).",
    "Save": "Writes the table and the settings above to sources.json; every action saves by itself too.",
    "Plan": "Prints in the log every command exactly as Fetch and Build will run it, and the folder it runs from; "
            "nothing runs.",
    "Fetch selected": "Downloads the selected rows with zowe; the log shows each command and the last column shows "
                      "the result.",
    "Fetch system": "Downloads every enabled row of the department chosen in the System box.",
    "Fetch all": "Downloads every enabled row in the table, one after the other.",
    "Build index": "Reads everything under the local root and the document folders into the index; only new and "
                   "changed members are parsed, so a second build is short.",
    "Rebuild from empty": "Deletes the index and parses every member from zero - hours for a large estate; it asks first.",
    "OCR images": "Reads the text in the pictures of the indexed documents with the Windows OCR engine; run Build "
                  "index afterwards so the text is indexed.",
    "Prepare documents": "Unzips archives and saves old .doc / .xls / .ppt files as .docx / .xlsx / .pptx beside "
                         "them, so the build can read them.",
    "Coverage": "Prints the report to read before trusting the index: what was parsed, what only in part, and what "
                "to fix.",
    "Help": "Opens one page with the five steps, what each setting means and where the outputs are.",
    "Close": "Closes this window.",
    "OK": "Keeps what you typed and saves the row to sources.json.",
    "Cancel": "Closes this window and changes nothing.",
    "Use for this session": "Gives zowe this user id and password for the commands of this session only.",
    "Forget": "Drops the session password; zowe works with your profile alone again.",
}

HELP_TEXT = """WHAT TO DO, IN ORDER

1  Check Zowe
   Button: Check Zowe. Runs zowe from "Run zowe from folder" and asks the host for one member list.
   The log shows four stages; the one that fails is the problem, and the line under it says what to do.
   If zowe finds no configuration, set "Run zowe from folder" to the folder your command window is in
   when zowe works without asking anything. Sign in is not needed when your zowe command works
   without a prompt.

2  Add the datasets to fetch
   Buttons: Add (one dataset) or Bulk add (a pasted list). For each one: the dataset name as your zowe
   command names it (PROD.CLAIMS.SRC), the folder it goes to on this laptop, PDS or sequential, the kind
   (cobol, copybook, jcl, proc ...) and the department (CLAIMS). Fetch source libraries only:
   PSBLIB, DBDLIB, ACBLIB and LOADLIB are compiled output with nothing to read.

3  Fetch
   Buttons: Fetch selected, Fetch system, Fetch all. For each row it runs
      zowe zos-files download all-members "DATASET" -d folder
   - the same command your window uses, from the same folder. Plan prints the commands without
   running them; the last column of the table shows the result of each row.

4  Build
   Button: Build index. Reads everything into the database (atlas.db). The first build takes long
   (hours for a large estate); later ones take seconds to minutes, because only new and changed
   members are parsed. After a stop, Build index again continues where it was. Rebuild from empty
   starts over from nothing - for the very first build only.

5  Ask
   Coverage first: it says what the index cannot answer and what to fix. Then, in a command window
   in this folder:
      python -m atlas.query --db atlas.db --out work\\pack.md pack PROGRAM
   or in VS Code: Terminal > Run Task > Atlas: pack NAME. The pack is what Copilot reads.

THE SETTINGS (sources.json, written by this window)
   zowe.profile       leave blank unless your window command uses --zosmf-profile
   zowe.working_dir   "Run zowe from folder": where zowe finds your configuration (blank = this folder)
   zowe.daemon        "window" = as your window has it; "off" only if zowe hangs when run from here
   zowe.extra_args    what you add to your own command, e.g. --encoding 1047 (also per dataset)
   local_root         where the downloaded libraries go, e.g. C:\\estate
   extra_roots        document folders indexed as well
   db                 the index file, e.g. atlas.db
   sources            one row per dataset: dataset, type (pds or seq), kind, system (the department),
                      local (its folder under local_root), ext, authoritative (the production copy),
                      enabled, extra_args
   system_includes    (by hand, optional) copybook names your compiles read from a product library you do
                      not fetch, beside the IBM ones the build knows (DFHAID, CMQV ...): never NOT FOUND

WHERE THINGS ARE
   atlas.db           the index - every answer comes from here
   work\\              reports and packs written by the tasks: coverage.md, pack.md, answer.md
   out\\               the toolkit's own outputs, e.g. out\\images for the pictures read by OCR
   sources.json       this table and the settings above
   manifest.json      written from the table on every Build
   Beside atlas.db after a build: atlas-problems.txt (what could not be indexed) and, after a crash,
   atlas-crash.txt - paste it to get a fix.
"""


# --------------------------------------------------------------------------
# pure helpers (no Tk): what the strip and the status line say
# --------------------------------------------------------------------------

def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def step_states(cfg: Dict, checked_ok: bool = False, db_exists: Optional[bool] = None) -> List[str]:
    """One of "done" / "now" / "later" per step, from what the window knows:
    a check that came back OK, rows in the table, their last result, the
    estate folder, and whether atlas.db exists and is newer than the last
    fetch."""
    sources = cfg.get("sources") or []
    enabled = [s for s in sources if s.get("enabled", True)]
    fetched = [str(s.get("last_fetched")) for s in sources if s.get("last_fetched")]
    root = str(cfg.get("local_root") or "")
    estate = False
    try:
        if os.path.isdir(root):
            with os.scandir(root) as it:
                estate = next(it, None) is not None
    except OSError:
        estate = False
    db = str(cfg.get("db") or "atlas.db")
    if db_exists is None:
        db_exists = os.path.isfile(db)
    db_stale = False
    if db_exists and fetched:
        try:
            built = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(db)))
            db_stale = max(fetched) > built
        except OSError:
            db_stale = False
    s1 = "done" if checked_ok else "now"
    s2 = "done" if sources else "now"
    if not sources:
        s3 = "later"
    elif enabled and all(str(s.get("last_result") or "").startswith("ok") for s in enabled):
        s3 = "done"
    else:
        s3 = "now"
    if db_exists and not db_stale:
        s4 = "done"
    elif db_exists or fetched or estate:
        s4 = "now"
    else:
        s4 = "later"
    s5 = "now" if db_exists else "later"
    return [s1, s2, s3, s4, s5]


def _no_config_line(lines: Sequence[str]) -> Optional[str]:
    return next((ln for ln in lines if "no zowe configuration found from" in ln), None)


def check_passed(ok: bool, msg: str) -> bool:
    """Whether the check proved that Fetch will work. `ok` alone is not
    enough: with no enabled PDS in the table the host stage is skipped and
    the check comes back OK even when stage 3 found no zowe configuration -
    that is the problem to fix first, not a pass."""
    lines = [ln.rstrip() for ln in (msg or "").splitlines() if ln.strip()]
    if not ok:
        return False
    if _no_config_line(lines) and not any(re.match(r"\s*4\.\s*host answered", ln) for ln in lines):
        return False
    return True


def status_after_check(ok: bool, msg: str, has_sources: bool) -> str:
    """The check's own words, one line: what it found and the next step, or
    the failing stage and the one thing to try."""
    lines = [ln.rstrip() for ln in (msg or "").splitlines() if ln.strip()]
    if check_passed(ok, msg):
        last = re.sub(r"^\s*[1-4]\.\s*", "", lines[-1].strip()) if lines else "zowe answers"
        last = last.partition(" - ")[0]
        nxt = "Fetch (Fetch all, or select rows and Fetch selected)" if has_sources \
            else "add the datasets to fetch (Add or Bulk add)"
        return f"Zowe OK: {_clip(last, 110)} - next: {nxt}"
    noconf = _no_config_line(lines)
    if noconf:
        folder = noconf.split("found from", 1)[1].split(" - ", 1)[0].strip()
        return (f"Zowe check failed: zowe found no configuration from {folder} - set 'Run zowe from folder' "
                "to the folder where your command works")
    stage = next((ln.strip() for ln in reversed(lines) if re.match(r"\s*[1-4]\.\s", ln)),
                 lines[-1].strip() if lines else "zowe did not answer")
    hint = next((ln.strip()[2:].strip() for ln in reversed(lines) if ln.strip().startswith("->")), "")
    body = re.sub(r"^\s*[1-4]\.\s*", "", stage)
    reason, _, rest = body.partition(" - ")
    hint = hint or rest or "read the failing stage in the log, then Check Zowe again"
    return f"Zowe check failed: {_clip(reason, 110)} - try: {_clip(hint, 140)}"


def status_after_fetch(results: Sequence) -> str:
    n = len(results)
    if not n:
        return "Nothing fetched: no enabled dataset matched - enable a row or add one, then Fetch again"
    bad = [r for r in results if not r.ok]
    if not bad:
        return f"Fetched {n} of {n} dataset{'s' if n != 1 else ''} - next: Build index"
    first = bad[0]
    reason, _, hint = str(first.message).partition(" - ")
    reason = reason.split(" | ")[0]
    if reason.startswith("INCOMPLETE"):
        hint = hint or "Fetch it again; the log lists the missing members"
    hint = hint or "Check Zowe, then Fetch again (the log has the whole message)"
    return (f"Fetched {n - len(bad)} of {n}; {first.dataset} failed: {_clip(reason, 90)} - try: {_clip(hint, 140)}"
            + (f" ({len(bad) - 1} more failed, see the log)" if len(bad) > 1 else ""))


def status_after_build(rc: Optional[int], lines: Sequence[str], db: str) -> str:
    """From the build's own summary: how many members, how many parsed only
    in part, and the next step; on a stop, the reason and what to try."""
    text = "\n".join(lines)
    if rc == 0:
        counts: Dict[str, int] = {}
        in_table = False
        for ln in lines:
            if ln.startswith("== members by kind"):
                in_table, counts = True, {}
                continue
            if in_table:
                m = re.match(r"\s+(\S+)\s+(\S+)\s+([\d,]+)\s*$", ln)
                if m:
                    counts[m.group(2)] = counts.get(m.group(2), 0) + int(m.group(3).replace(",", ""))
                elif ln.strip():
                    in_table = False
        total = sum(counts.values())
        nxt = (f"next: ask (python -m atlas.query --db {db} pack NAME, or the VS Code tasks) - "
               "read Coverage first")
        if not total:
            return f"Build finished - {nxt}"
        out = f"Build done: {total:,} members"
        if counts.get("partial"):
            out += f", {counts['partial']:,} parsed only in part"
        if counts.get("failed"):
            out += f", {counts['failed']:,} failed"
        m = re.search(r"== problems in this run: ([\d,]+) member", text)
        if m:
            out += f", {m.group(1)} not indexed (atlas-problems.txt)"
        return f"{out} - {nxt}"
    if rc == 130:
        return "Build stopped - Build index again continues from where it stopped; parsed members are kept"
    if rc is None:
        return "Build could not start - the log says why; fix it, then Build index again"
    m = re.search(r"BUILD STOPPED BY AN ERROR: (.*)", text)
    if m:
        after = text[m.end():].strip().splitlines()
        hint = after[0].strip() if after else "run Build index again; members parsed so far are kept"
        return f"Build stopped: {_clip(m.group(1), 100)} - try: {_clip(hint, 150)}"
    last = next((ln for ln in reversed(lines) if ln.strip()), f"exit code {rc}")
    return f"Build stopped (exit code {rc}): {_clip(last, 100)} - try: Build index again; members parsed so far are kept"


# --------------------------------------------------------------------------
# widgets: a hover tip, a grey help line
# --------------------------------------------------------------------------

class Tooltip:
    """A small window under the widget after a short hover, stdlib Tk only.
    `Tooltip.of(widget)` finds the one attached."""

    DELAY_MS = 500

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.win = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget._atlas_tooltip = self

    @staticmethod
    def of(widget) -> Optional["Tooltip"]:
        return getattr(widget, "_atlas_tooltip", None)

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:                              # noqa: BLE001 - widget gone
                pass
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self.win is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.win = tk.Toplevel(self.widget)
            self.win.wm_overrideredirect(True)
            self.win.wm_geometry(f"+{x}+{y}")
            try:
                self.win.wm_attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(self.win, text=self.text, justify="left", background="#ffffe0", foreground="#222",
                     relief="solid", borderwidth=1, wraplength=380, padx=6, pady=4).pack()
        except tk.TclError:
            self.win = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


def _button(parent, text: str, command, tip: Optional[str] = None):
    """A button with its hover tip attached (from TIPS unless given)."""
    b = ttk.Button(parent, text=text, command=command)
    Tooltip(b, tip if tip is not None else TIPS.get(text, ""))
    return b


def _help(widget, text: str, wraplength: int, **grid):
    """The grey line under a field, attached to the field as `_atlas_help`."""
    lab = ttk.Label(widget.master, text=text, foreground=HELP_FG, wraplength=wraplength, justify="left")
    lab.grid(**grid)
    widget._atlas_help = lab
    return lab


class SourceDialog(tk.Toplevel if tk else object):
    """Add / edit one source row."""

    def __init__(self, parent, src: Optional[Dict] = None, modal: bool = True):
        super().__init__(parent)
        self.title("Source" if src else "Add source")
        self.resizable(False, False)
        self.result: Optional[Dict] = None
        s = src or fetch.new_source("")
        self.vars = {
            "dataset": tk.StringVar(value=s["dataset"]),
            "type": tk.StringVar(value=s.get("type", "pds")),
            "kind": tk.StringVar(value=s.get("kind", "cobol")),
            "system": tk.StringVar(value=s.get("system", "")),
            "local": tk.StringVar(value=s.get("local", "")),
            "ext": tk.StringVar(value=s.get("ext", "")),
            "extra_args": tk.StringVar(value=" ".join(s.get("extra_args") or [])),
            "authoritative": tk.BooleanVar(value=bool(s.get("authoritative"))),
            "enabled": tk.BooleanVar(value=bool(s.get("enabled", True))),
        }
        f = ttk.Frame(self, padding=12)
        f.grid()
        rows = [
            ("dataset", "Dataset (PDS/PDSE or sequential)", ttk.Entry(f, textvariable=self.vars["dataset"], width=44)),
            ("type", "Type", ttk.Combobox(f, textvariable=self.vars["type"], values=("pds", "seq"), state="readonly", width=10)),
            ("kind", "Kind", ttk.Combobox(f, textvariable=self.vars["kind"], values=fetch.KINDS, state="readonly", width=14)),
            ("system", "System (CLAIMS, POLICY, ...)", ttk.Entry(f, textvariable=self.vars["system"], width=20)),
            ("local", "Local folder / file under root", ttk.Entry(f, textvariable=self.vars["local"], width=44)),
            ("ext", "File extension for members, e.g. .cbl",ttk.Entry(f, textvariable=self.vars["ext"], width=10)),
            ("extra_args", "Extra zowe args", ttk.Entry(f, textvariable=self.vars["extra_args"], width=44)),
        ]
        self.help_labels: Dict[str, "ttk.Label"] = {}
        r = 0
        for key, label, widget in rows:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=(4, 0), padx=(0, 10))
            widget.grid(row=r, column=1, sticky="w", pady=(4, 0))
            self.help_labels[key] = _help(widget, HELP[key], 500, row=r + 1, column=0, columnspan=2, sticky="w")
            r += 2
        for key, text in (("authoritative", "Production (authoritative) copy"), ("enabled", "Enabled")):
            cb = ttk.Checkbutton(f, text=text, variable=self.vars[key])
            cb.grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0))
            self.help_labels[key] = _help(cb, HELP[key], 500, row=r + 1, column=0, columnspan=2, sticky="w")
            r += 2
        ttk.Label(f, wraplength=500, foreground=HELP_FG, justify="left",
                  text="dbd / psb = the SOURCE libraries (DBDGEN / PSBGEN macros). PSBLIB, DBDLIB, ACBLIB and "
                       "LOADLIB are compiled output - nothing to parse, do not fetch them. csd = a DFHCSDUP "
                       "extract; imsgen = IMS stage-1 source; sched = a scheduler CSV export.").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=r + 1, column=0, columnspan=2, pady=(10, 0), sticky="e")
        _button(b, "Cancel", self.destroy).pack(side="right", padx=4)
        _button(b, "OK", self._ok).pack(side="right")
        self.vars["kind"].trace_add("write", lambda *_: self.vars["ext"].set(
            fetch.EXT_FOR_KIND.get(self.vars["kind"].get(), "txt")) if not self.vars["ext"].get() else None)
        self.vars["dataset"].trace_add("write", lambda *_: self.vars["local"].set(self.vars["dataset"].get().upper())
                                       if not src else None)
        self.transient(parent)
        if modal:
            self.grab_set()
            self.wait_window()

    def _ok(self):
        ds = self.vars["dataset"].get().strip().upper()
        if not ds:
            messagebox.showerror("Source", "Dataset name is required")
            return
        self.result = fetch.new_source(ds, self.vars["kind"].get())
        self.result.update({
            "type": self.vars["type"].get(), "system": self.vars["system"].get().strip().upper(),
            "local": self.vars["local"].get().strip() or ds, "ext": self.vars["ext"].get().strip() or "txt",
            "extra_args": self.vars["extra_args"].get().split(),
            "authoritative": self.vars["authoritative"].get(), "enabled": self.vars["enabled"].get(),
        })
        self.destroy()


class App(tk.Tk if tk else object):

    def __init__(self, config_path: str):
        super().__init__()
        self.title("Mainframe Atlas - sources")
        try:
            height = min(700, max(600, self.winfo_screenheight() - 90))
        except tk.TclError:
            height = 700
        self.geometry(f"1180x{height}")
        self.config_path = os.path.abspath(config_path)
        self.cfg = fetch.load_config(self.config_path)
        self.q: "queue.Queue[str]" = queue.Queue()
        self.busy = False
        self.checked_ok = False
        self.last_output: List[str] = []
        self._build_rc: Optional[int] = None
        self._fetch_results: List = []
        self.help_labels: Dict[str, "ttk.Label"] = {}

        # ---- what to do, in order ------------------------------------
        strip = ttk.LabelFrame(self, text="What to do", padding=(8, 2, 8, 4))
        strip.pack(fill="x", padx=8, pady=(6, 0))
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.strip_labels: List = []
        for i, (num, name, what) in enumerate(STEPS):
            col = ttk.Frame(strip)
            col.grid(row=0, column=i, sticky="nw", padx=(0, 6))
            strip.columnconfigure(i, weight=1, uniform="step")
            head = ttk.Label(col, text=f"{num}  {name}", font=bold)
            head.pack(anchor="w")
            body = ttk.Label(col, text=what, wraplength=200, justify="left", foreground=HELP_FG)
            body.pack(anchor="w")
            self.strip_labels.append((head, body))
        _button(strip, "Help", self.show_help).grid(row=0, column=len(STEPS), sticky="ne")

        # ---- settings -------------------------------------------------
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.v_profile = tk.StringVar(value=self.cfg["zowe"].get("profile", ""))
        self.v_root = tk.StringVar(value=self.cfg["local_root"])
        self.v_db = tk.StringVar(value=self.cfg.get("db", "atlas.db"))
        # Zowe runs from the folder the developer's own window runs it in
        # (where it finds its zowe.config.json), with the daemon as that
        # window has it - so a command that works there works here unchanged.
        self.v_zowe_dir = tk.StringVar(value=self.cfg["zowe"].get("working_dir") or "")
        self.v_daemon_off = tk.BooleanVar(value=fetch.daemon_off(self.cfg))
        ttk.Label(top, text="Zowe profile").grid(row=0, column=0, sticky="w")
        e_profile = ttk.Entry(top, textvariable=self.v_profile, width=18)
        e_profile.grid(row=0, column=1, padx=(4, 16), sticky="w")
        ttk.Label(top, text="Run zowe from folder").grid(row=0, column=2, sticky="w")
        e_zowe_dir = ttk.Entry(top, textvariable=self.v_zowe_dir, width=40)
        e_zowe_dir.grid(row=0, column=3, padx=4, sticky="w")
        _button(top, "Browse", self._browse_zowe_dir,
                "Pick the folder where your zowe command works; the path goes into the box.").grid(
            row=0, column=4, padx=(0, 16), sticky="w")
        cb_daemon = ttk.Checkbutton(top, text=DAEMON_OFF_LABEL, variable=self.v_daemon_off)
        cb_daemon.grid(row=0, column=5, sticky="w")
        b_check = _button(top, "Check Zowe", self.check_zowe)
        b_check.grid(row=0, column=6, padx=(16, 0), sticky="w")
        b_sign = _button(top, "Sign in...", self.sign_in)
        b_sign.grid(row=0, column=7, padx=(4, 0), sticky="w")
        self.help_labels["profile"] = _help(e_profile, HELP["profile"], 230, row=1, column=0, columnspan=2, sticky="nw")
        self.help_labels["zowe_dir"] = _help(e_zowe_dir, HELP["zowe_dir"], 470, row=1, column=2, columnspan=3, sticky="nw")
        self.help_labels["daemon"] = _help(cb_daemon, HELP["daemon"], 220, row=1, column=5, sticky="nw")
        self.help_labels["sign_in"] = _help(b_sign, HELP["sign_in"], 165, row=1, column=6, columnspan=2, sticky="nw",
                                            padx=(16, 0))
        b_check._atlas_help = self.help_labels["sign_in"]

        ttk.Label(top, text="Local root").grid(row=2, column=0, sticky="w", pady=(6, 0))
        e_root = ttk.Entry(top, textvariable=self.v_root, width=70)
        e_root.grid(row=2, column=1, columnspan=3, sticky="w", padx=(4, 4), pady=(6, 0))
        _button(top, "Browse", self._browse_root,
                "Pick the folder on this laptop where the downloaded libraries go.").grid(
            row=2, column=4, padx=(0, 16), pady=(6, 0), sticky="w")
        ttk.Label(top, text="Database").grid(row=2, column=5, sticky="w", pady=(6, 0))
        e_db = ttk.Entry(top, textvariable=self.v_db, width=20)
        e_db.grid(row=2, column=6, columnspan=2, padx=(16, 0), pady=(6, 0), sticky="w")
        self.help_labels["root"] = _help(e_root, HELP["root"], 700, row=3, column=0, columnspan=5, sticky="nw")
        self.help_labels["db"] = _help(e_db, HELP["db"], 360, row=3, column=5, columnspan=3, sticky="nw")

        # Documents are not mainframe datasets: they already sit in a folder on
        # this laptop. Name those folders here and the build indexes them too.
        ttk.Label(top, text="Document folders").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.v_extra = tk.StringVar(value=";".join(self.cfg.get("extra_roots") or []))
        e_extra = ttk.Entry(top, textvariable=self.v_extra, width=70)
        e_extra.grid(row=4, column=1, columnspan=3, sticky="w", padx=(4, 4), pady=(6, 0))
        _button(top, "Add folder", self._add_extra_root).grid(row=4, column=4, padx=(0, 16), pady=(6, 0), sticky="w")
        # One estate, many departments: filter the table to one system, fetch
        # or inspect that system alone, keep the rest untouched.
        ttk.Label(top, text="System").grid(row=4, column=5, sticky="w", pady=(6, 0))
        self.v_filter = tk.StringVar(value="All")
        self.cb_filter = ttk.Combobox(top, textvariable=self.v_filter, state="readonly", width=16, values=("All",))
        self.cb_filter.grid(row=4, column=6, columnspan=2, padx=(16, 0), pady=(6, 0), sticky="w")
        self.cb_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.help_labels["extra"] = _help(e_extra, HELP["extra"], 700, row=5, column=0, columnspan=5, sticky="nw")
        self.help_labels["filter"] = _help(self.cb_filter, HELP["filter"], 360, row=5, column=5, columnspan=3, sticky="nw")

        # ---- status line (packed before the table so it is never squeezed out)
        self.v_status = tk.StringVar(value=f"Settings read from {self.config_path} - start with step 1: Check Zowe")
        self.status_label = ttk.Label(self, textvariable=self.v_status, foreground="#333", wraplength=1150,
                                      justify="left", anchor="w", padding=(8, 2))
        self.status_label.pack(side="bottom", fill="x")

        # The table keeps four rows (it scrolls); the log below takes the
        # rest of the height, so on a 1280x720 laptop screen it shows eight
        # lines or more - that is where Fetch and Build say how far they are.
        mid = ttk.Frame(self, padding=(8, 0, 8, 0))
        mid.pack(fill="x")
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", selectmode="extended", height=4)
        for c, w in zip(COLUMNS, WIDTHS):
            self.tree.heading(c, text=HEADINGS.get(c, c))
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.edit_source())

        # Two rows so every button is on screen at 1280x720: the table's own
        # buttons, then the actions in the order of the steps.
        btns = ttk.Frame(self, padding=(8, 3, 8, 1))
        btns.pack(fill="x")
        for label, row in (("Table", (("Add", self.add_source), ("Bulk add", self.bulk_add), ("Edit", self.edit_source),
                                      ("Remove", self.remove_source), ("Enable/Disable", self.toggle_source),
                                      ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                                      ("Save", self.save))),
                           ("Do", (("Plan", self.plan), ("Fetch selected", self.fetch_selected),
                                   ("Fetch system", self.fetch_system), ("Fetch all", self.fetch_all),
                                   ("Build index", lambda: self.build(False)),
                                   ("Rebuild from empty", lambda: self.build(True)),
                                   ("OCR images", self.ocr_images), ("Prepare documents", self.convert_legacy),
                                   ("Coverage", self.coverage)))):
            line = ttk.Frame(btns)
            line.pack(fill="x", pady=(0, 2))
            ttk.Label(line, text=label, foreground=HELP_FG, width=8).pack(side="left")
            for text, cmd in row:
                _button(line, text, cmd).pack(side="left", padx=2)

        self.log = scrolledtext.ScrolledText(self, height=8, wrap="none", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.log.configure(state="disabled")

        self.refresh()
        self._poll_job = self.after(150, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---- table ---------------------------------------------------------

    def refresh(self) -> None:
        systems = fetch.systems_in(self.cfg)
        self.cb_filter["values"] = ("All", *systems)
        if self.v_filter.get() not in ("All", *systems):
            self.v_filter.set("All")
        self.tree.delete(*self.tree.get_children())
        for i in fetch.filter_sources(self.cfg, self.v_filter.get()):
            s = self.cfg["sources"][i]
            self.tree.insert("", "end", iid=str(i), values=(
                s["dataset"], s.get("type", "pds"), s.get("kind", ""), s.get("system", ""),
                fetch.local_path(self.cfg, s), "yes" if s.get("authoritative") else "",
                "yes" if s.get("enabled", True) else "no",
                (s.get("last_result") or "")[:60] if s.get("last_fetched") else "never"))
        self._refresh_strip()

    def _refresh_strip(self) -> None:
        """Mark each step done / possible now / later from what is known."""
        cfg = dict(self.cfg, local_root=self.v_root.get().strip() or self.cfg["local_root"],
                   db=self.v_db.get().strip() or "atlas.db")
        self.step_state = step_states(cfg, self.checked_ok)
        for i, ((head, body), state) in enumerate(zip(self.strip_labels, self.step_state)):
            num, name, _what = STEPS[i]
            word = STATE_WORDS[state].format(prev=i)
            head.configure(text=f"{num}  {name} - {word}",
                           foreground=DONE_FG if state == "done" else LATER_FG if state == "later" else "#000")
            body.configure(foreground=LATER_FG if state == "later" else HELP_FG)

    def _status(self, text: str, bad: bool = False) -> None:
        self.v_status.set(text)
        self.status_label.configure(foreground=BAD_FG if bad else "#333")

    def _selected(self) -> List[int]:
        return [int(i) for i in self.tree.selection()]

    def _sync_cfg(self) -> None:
        self.cfg["zowe"]["profile"] = self.v_profile.get().strip()
        self.cfg["zowe"]["working_dir"] = self.v_zowe_dir.get().strip()
        self.cfg["zowe"]["daemon"] = "off" if self.v_daemon_off.get() else "window"
        self.cfg["local_root"] = self.v_root.get().strip() or self.cfg["local_root"]
        self.cfg["db"] = self.v_db.get().strip() or "atlas.db"
        self.cfg["extra_roots"] = [p.strip() for p in self.v_extra.get().split(";") if p.strip()]

    def _add_extra_root(self) -> None:
        d = filedialog.askdirectory(title="Folder of documents to index")
        if d:
            cur = [p.strip() for p in self.v_extra.get().split(";") if p.strip()]
            if d not in cur:
                cur.append(d)
            self.v_extra.set(";".join(cur))
            self.save()
            self._status(f"Document folder added: {d} - next: Build index (it is read with the code)")

    def add_source(self) -> None:
        d = SourceDialog(self)
        if d.result:
            self.cfg["sources"].append(d.result)
            self.refresh()
            self.save()
            self._status(f"Added {d.result['dataset']} - next: add the other datasets, or Fetch")

    def edit_source(self) -> None:
        sel = self._selected()
        if len(sel) != 1:
            messagebox.showinfo("Edit", "Select one source")
            return
        d = SourceDialog(self, self.cfg["sources"][sel[0]])
        if d.result:
            keep = {k: self.cfg["sources"][sel[0]].get(k) for k in ("last_fetched", "last_result")}
            self.cfg["sources"][sel[0]] = dict(d.result, **{k: v for k, v in keep.items() if v})
            self.refresh()
            self.save()
            self._status(f"Changed {d.result['dataset']} - next: Fetch it again if the dataset or folder changed")

    def remove_source(self) -> None:
        sel = self._selected()
        if sel and messagebox.askyesno("Remove", f"Remove {len(sel)} source(s) from the config? Local files are kept."):
            for i in sorted(sel, reverse=True):
                del self.cfg["sources"][i]
            self.refresh()
            self.save()
            self._status(f"Removed {len(sel)} row(s) from the table - the downloaded files stay on disk")

    def toggle_source(self) -> None:
        for i in self._selected():
            s = self.cfg["sources"][i]
            s["enabled"] = not s.get("enabled", True)
        self.refresh()
        self.save()
        self._status("Enabled / disabled rows saved - a disabled row is skipped on Fetch and Build")

    def save(self) -> None:
        self._sync_cfg()
        fetch.save_config(self.cfg, self.config_path)
        self.v_status.set(f"saved {self.config_path} - the table and the settings above are kept for next time")
        self.status_label.configure(foreground="#333")

    def _browse_root(self) -> None:
        d = filedialog.askdirectory(initialdir=self.v_root.get() or os.getcwd())
        if d:
            self.v_root.set(d)
            self._refresh_strip()

    def _browse_zowe_dir(self) -> None:
        d = filedialog.askdirectory(title="Folder where your zowe command works",
                                    initialdir=self.v_zowe_dir.get().strip() or fetch.zowe_cwd(self.cfg))
        if d:
            self.v_zowe_dir.set(d)

    # ---- logging / background ------------------------------------------

    def _log(self, text: str) -> None:
        self.q.put(text)

    def _poll(self) -> None:
        """Drains the queue on the Tk thread: log lines go into the log, and
        a callable put by a worker thread runs here - Tk is touched from the
        main thread only, and a "done" put after the last line runs once
        that line is on screen. A callable that raises is written to the log
        and the status line goes red; the loop goes on, so later lines still
        arrive."""
        try:
            while True:
                item = self.q.get_nowait()
                if callable(item):
                    try:
                        item()
                    except Exception as e:                # noqa: BLE001 - the window stays alive
                        self._append_log(f"ERROR {type(e).__name__}: {e}")
                        self._status(f"Error: {_clip(e, 140)} - the log has the details", bad=True)
                    continue
                self._append_log(item)
        except queue.Empty:
            pass
        finally:
            self._poll_job = self.after(150, self._poll)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def destroy(self) -> None:
        # cancel the poll timer first: a timer left behind by a closed window
        # fires as "invalid command name" in a process that opens another
        job = getattr(self, "_poll_job", None)
        if job is not None:
            self._poll_job = None
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        super().destroy()

    def _on_main(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the Tk thread, after the log lines queued before it."""
        self.q.put(fn)

    def _run_bg(self, fn: Callable[[], None], done: Optional[Callable[[], None]] = None) -> None:
        if self.busy:
            messagebox.showinfo("Busy", "Wait for the current task to finish")
            return
        self.busy = True

        def target():
            try:
                fn()
            except Exception as e:                    # noqa: BLE001
                self._log(f"ERROR {type(e).__name__}: {e}")
                text = f"Error: {_clip(e, 140)} - the log has the details"
                self._on_main(lambda: self._status(text, bad=True))
            finally:
                self.busy = False
                if done:
                    self._on_main(done)
        threading.Thread(target=target, daemon=True).start()

    def _stream(self, cmd: List[str]) -> int:
        self._log("> " + fetch.redact_cmd(cmd))
        self.last_output = []
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        assert p.stdout is not None
        for line in p.stdout:
            self._log(line)
            self.last_output.append(line.rstrip("\n"))
            if len(self.last_output) > 4000:
                del self.last_output[:2000]
        return p.wait()

    def _run_cmd(self, cmd: List[str], ok_text: str, fail_text: str) -> None:
        """Stream one command into the log; the status line then says what
        happened in its own last line and the next step."""
        box: Dict[str, int] = {}

        def go():
            box["rc"] = self._stream(cmd)

        def done():
            rc = box.get("rc")
            if rc == 0:
                self._status(ok_text)
            else:
                last = next((ln for ln in reversed(self.last_output) if ln.strip()), f"exit code {rc}")
                self._status(f"{fail_text}: {_clip(last, 120)} - the log has the whole message", bad=True)
        self._run_bg(go, done)

    # ---- actions -------------------------------------------------------

    def check_zowe(self) -> None:
        self._sync_cfg()
        has_sources = any(s.get("enabled", True) for s in self.cfg["sources"])

        def go():
            ok, msg = fetch.check_zowe(self.cfg, log=self._log)
            passed = check_passed(ok, msg)
            self._log("RESULT: OK - Fetch will work" if passed
                      else "RESULT: FAIL - fix the stage above, then Check again")
            self.checked_ok = passed

            def show():
                self._status(status_after_check(ok, msg, has_sources), bad=not passed)
                self._refresh_strip()
            self._on_main(show)
        self._run_bg(go)

    def sign_in(self):
        """OPTIONAL - the mainframe password for THIS SESSION, for shops whose
        profile stores none; a zowe command that works in a window without a
        prompt needs nothing here. zowe reads it from its own environment
        variable in the fetch subprocess. Nothing is saved to sources.json,
        the log or a crash file; closing the window forgets it."""
        win = tk.Toplevel(self)
        win.title("Sign in - this session only")
        win.transient(self)
        win.grab_set()
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        v_user = tk.StringVar(value=os.environ.get("ZOWE_OPT_USER") or "")
        v_pw = tk.StringVar()
        ttk.Label(f, text="Mainframe user id").grid(row=0, column=0, sticky="w")
        e_user = ttk.Entry(f, textvariable=v_user, width=24)
        e_user.grid(row=0, column=1, padx=6, pady=2, sticky="w")
        _help(e_user, HELP["user"], 420, row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Password").grid(row=2, column=0, sticky="w")
        e_pw = ttk.Entry(f, textvariable=v_pw, width=24, show="*")
        e_pw.grid(row=2, column=1, padx=6, pady=2, sticky="w")
        _help(e_pw, HELP["password"], 420, row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text=SIGN_IN_NOTE, foreground=HELP_FG, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 8))

        def use() -> None:
            fetch.set_session_credentials(v_user.get().strip() or None, v_pw.get() or None)
            v_pw.set("")
            self._status(("signed in for this session"
                          + (f" as {v_user.get().strip()}" if v_user.get().strip() else "")
                          + " - next: Check Zowe, then Fetch")
                         if fetch.has_session_credentials() else NO_SESSION_PASSWORD)
            self._log("session password " + ("set (not stored)" if fetch.has_session_credentials() else "cleared"))
            win.destroy()

        def forget() -> None:
            fetch.set_session_credentials(None, None)
            self._status(NO_SESSION_PASSWORD)
            win.destroy()

        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=2, sticky="e")
        _button(b, "Use for this session", use).pack(side="left", padx=2)
        _button(b, "Forget", forget).pack(side="left", padx=2)
        _button(b, "Cancel", win.destroy).pack(side="left", padx=2)
        (e_pw if v_user.get() else e_user).focus_set()
        win.bind("<Return>", lambda e: use())
        return win

    def show_help(self):
        """One page: the five steps, the settings in plain words, where the
        outputs are."""
        win = tk.Toplevel(self)
        win.title("Mainframe Atlas - help")
        win.transient(self)
        win.geometry("820x600")
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("Consolas", 10), padx=10, pady=8)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", HELP_TEXT)
        txt.configure(state="disabled")
        _button(win, "Close", win.destroy).pack(pady=6)
        return win

    def plan(self) -> None:
        """The exact command lines and the folder they run from - compare
        them word for word with the command typed in a window."""
        self._sync_cfg()
        self._log(fetch.run_from_line(self.cfg))
        for s in self.cfg["sources"]:
            self._log(fetch.redact_cmd(fetch.download_cmd(self.cfg, s)) + ("" if s.get("enabled", True) else "   (disabled)"))
        self._log(" ".join(fetch.build_cmd(self.cfg, self._manifest_path())))
        self._status("Plan printed in the log: every command as it will run, and the folder it runs from - "
                     "compare it with the command you type; next: Fetch")

    def _fetch(self, only: Optional[List[str]]) -> None:
        self._sync_cfg()
        self.save()
        self._status("Fetching ... the log shows each command as it runs")

        def go():
            res = fetch.fetch_all(self.cfg, log=self._log, only=only)
            fetch.save_config(self.cfg, self.config_path)
            bad = sum(1 for r in res if not r.ok)
            self._log(f"done: {len(res) - bad} ok, {bad} failed")
            self._fetch_results = res

        def done():
            self.refresh()
            res = self._fetch_results
            self._status(status_after_fetch(res), bad=any(not r.ok for r in res))
        self._run_bg(go, done)

    def fetch_selected(self) -> None:
        sel = self._selected()
        if not sel:
            messagebox.showinfo("Fetch", "Select one or more sources")
            return
        self._fetch([self.cfg["sources"][i]["dataset"] for i in sel])

    def fetch_all(self) -> None:
        self._fetch(None)

    def _manifest_path(self) -> str:
        return os.path.join(os.path.dirname(self.config_path), "manifest.json")

    def build(self, rebuild: bool) -> None:
        if rebuild and not messagebox.askyesno(
                "Rebuild from empty",
                "This DELETES atlas.db and parses every member from zero - hours for a large estate.\n\n"
                "After a stop or a git pull, 'Build index' is enough: only new and changed members are parsed, and a "
                "toolkit change re-parses everything by itself.\n\nDelete the index and start from zero?"):
            self._log("rebuild cancelled - use Build index")
            self._status("Rebuild cancelled - Build index is enough: only new and changed members are parsed")
            return
        self._sync_cfg()
        self.save()
        man = self._manifest_path()
        fetch.write_manifest(self.cfg, man)
        self._log(f"manifest written: {man}")
        self._status("Building ... the log says how far it is; the first build of a large estate takes hours")
        self._build_rc = None

        def go():
            self._build_rc = self._stream(fetch.build_cmd(self.cfg, man, rebuild))
        self._run_bg(go, self._after_build)

    def _after_build(self) -> None:
        self.refresh()
        self._status(status_after_build(self._build_rc, self.last_output, self.cfg.get("db") or "atlas.db"),
                     bad=self._build_rc != 0)

    def coverage(self) -> None:
        self._sync_cfg()
        db = self.cfg.get("db") or "atlas.db"
        self._run_cmd([sys.executable, "-m", "atlas.query", "--db", db, "coverage"],
                      f"Coverage report is in the log - fix what it names (Fetch, Build index), then ask: "
                      f"python -m atlas.query --db {db} pack NAME",
                      "Coverage could not be read (is there an index yet? Build index first)")

    # ---- departments -----------------------------------------------------

    def bulk_add(self):
        """Paste a department's dataset list, one per line; kinds are inferred
        from the names, load libraries are added disabled."""
        dlg = tk.Toplevel(self)
        dlg.title("Bulk add a department's libraries")
        f = ttk.Frame(dlg, padding=12)
        f.grid()
        v_sys = tk.StringVar(value=self.v_filter.get() if self.v_filter.get() != "All" else "")
        v_auth = tk.BooleanVar(value=True)
        ttk.Label(f, text="System / department").grid(row=0, column=0, sticky="w")
        e_sys = ttk.Entry(f, textvariable=v_sys, width=20)
        e_sys.grid(row=0, column=1, sticky="w", pady=3)
        _help(e_sys, HELP["bulk_system"], 520, row=1, column=0, columnspan=2, sticky="w")
        cb_auth = ttk.Checkbutton(f, text="These are the production (authoritative) libraries", variable=v_auth)
        cb_auth.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        _help(cb_auth, HELP["authoritative"], 520, row=3, column=0, columnspan=2, sticky="w")
        txt = tk.Text(f, width=64, height=14, font=("Consolas", 10))
        _help(txt, HELP["bulk_text"], 520, row=4, column=0, columnspan=2, sticky="w", pady=(8, 2))
        txt.grid(row=5, column=0, columnspan=2)
        b = ttk.Frame(f)
        b.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))

        def ok():
            sysname = v_sys.get().strip().upper()
            if not sysname:
                messagebox.showerror("Bulk add", "System / department is required")
                return
            added, warnings = fetch.bulk_add(self.cfg, sysname, txt.get("1.0", "end"), v_auth.get())
            for w in warnings:
                self._log("bulk add: " + w)
            self._log(f"bulk add: {len(added)} source(s) added to {sysname}")
            dlg.destroy()
            self.v_filter.set(sysname)
            self.refresh()
            self.save()
            self._status(f"Added {len(added)} dataset(s) to {sysname}"
                         + (f"; {len(warnings)} note(s) in the log" if warnings else "")
                         + " - next: Fetch system (or Fetch all)")
        _button(b, "Cancel", dlg.destroy).pack(side="right", padx=4)
        _button(b, "Add", ok, "Adds one row per line to the table, saved to sources.json.").pack(side="right")
        dlg.transient(self)
        dlg.grab_set()
        return dlg

    def move(self, delta: int) -> None:
        """Order matters for copybook libraries: it is the SYSLIB concatenation
        order the department's programs compile against."""
        sel = self._selected()
        if len(sel) != 1:
            messagebox.showinfo("Move", "Select one source")
            return
        i, j = sel[0], sel[0] + delta
        if 0 <= j < len(self.cfg["sources"]):
            src = self.cfg["sources"]
            src[i], src[j] = src[j], src[i]
            self.refresh()
            self.tree.selection_set(str(j))
            self.save()
            self._status("Row moved - for copybook libraries the order is the search order (first match wins)")

    def fetch_system(self) -> None:
        sysname = self.v_filter.get()
        if sysname == "All":
            messagebox.showinfo("Fetch system", "Pick a system in the System box first")
            return
        self._fetch([self.cfg["sources"][i]["dataset"] for i in fetch.filter_sources(self.cfg, sysname)
                     if self.cfg["sources"][i].get("enabled", True)])

    def ocr_images(self) -> None:
        """Pull the pictures out of the indexed documents and read them with the
        Windows OCR engine - no install, no network, no model tokens."""
        self._sync_cfg()
        db = self.cfg.get("db") or "atlas.db"
        out = os.path.join(self.cfg["local_root"], "out", "images")
        self._run_cmd([sys.executable, "-m", "atlas.ocr", "--db", db, "--out", out],
                      f"OCR done - the text read from the pictures is under {out}; next: Build index so it is indexed",
                      "OCR stopped")

    def convert_legacy(self) -> None:
        """Extract .zip archives under the document folders (into <name>.unzipped
        beside each), then save .doc / .xls / .ppt as .docx / .xlsx / .pptx beside
        the originals with the Office installed on this laptop (nothing installed,
        nothing deleted). The build then reads the copies."""
        self._sync_cfg()
        roots = [r for r in (self.cfg.get("extra_roots") or []) if os.path.isdir(r)]
        if not roots:
            messagebox.showinfo("Prepare documents", "Name the document folder(s) under 'Document folders' first.")
            return
        self._run_cmd([sys.executable, "-m", "atlas.convert", *roots],
                      "Documents prepared - the copies sit beside the originals; next: Build index",
                      "Prepare documents stopped")

    def _close(self) -> None:
        try:
            self.save()
        finally:
            self.destroy()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Mainframe Atlas - sources UI")
    ap.add_argument("--config", default="sources.json")
    a = ap.parse_args(argv)
    if tk is None:
        print("tkinter is not available in this Python; use `python -m atlas.fetch` instead.")
        return 1
    App(a.config).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
