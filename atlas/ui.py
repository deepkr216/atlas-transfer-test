"""
ui.py - a small desktop front end for "get the code, build the index".

    python -m atlas.ui                     # uses ./sources.json
    python -m atlas.ui --config C:/estate/sources.json

Tkinter only (ships with Python on Windows), so it runs on a locked-down
laptop with no installs. Everything it does goes through fetch.py, so the
command line and the UI can never behave differently: the table IS
sources.json; "Fetch" runs the same Zowe commands `--plan` prints; "Build"
runs atlas.build with the manifest generated from the table.
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
from typing import Callable, Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:                                   # pragma: no cover
    tk = None                                         # type: ignore

from . import fetch

COLUMNS = ("dataset", "type", "kind", "system", "local", "auth", "enabled", "last")
WIDTHS = (220, 50, 80, 90, 220, 50, 60, 260)


class SourceDialog(tk.Toplevel if tk else object):
    """Add / edit one source row."""

    def __init__(self, parent, src: Optional[Dict] = None):
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
            ("Dataset (PDS/PDSE or sequential)", ttk.Entry(f, textvariable=self.vars["dataset"], width=44)),
            ("Type", ttk.Combobox(f, textvariable=self.vars["type"], values=("pds", "seq"), state="readonly", width=10)),
            ("Kind", ttk.Combobox(f, textvariable=self.vars["kind"], values=fetch.KINDS, state="readonly", width=14)),
            ("System (CLAIMS, POLICY, ...)", ttk.Entry(f, textvariable=self.vars["system"], width=20)),
            ("Local folder / file under root", ttk.Entry(f, textvariable=self.vars["local"], width=44)),
            ("File extension (pds)", ttk.Entry(f, textvariable=self.vars["ext"], width=10)),
            ("Extra zowe args", ttk.Entry(f, textvariable=self.vars["extra_args"], width=44)),
        ]
        for i, (label, widget) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=i, column=1, sticky="w", pady=3)
        ttk.Checkbutton(f, text="Production (authoritative) copy", variable=self.vars["authoritative"]).grid(
            row=len(rows), column=1, sticky="w")
        ttk.Checkbutton(f, text="Enabled", variable=self.vars["enabled"]).grid(row=len(rows) + 1, column=1, sticky="w")
        ttk.Label(f, wraplength=420, foreground="#555",
                  text="dbd / psb = the SOURCE libraries (DBDGEN / PSBGEN macros). PSBLIB, DBDLIB, ACBLIB and "
                       "LOADLIB are compiled output - nothing to parse, do not fetch them. csd = a DFHCSDUP "
                       "extract; imsgen = IMS stage-1 source; sched = a scheduler CSV export.").grid(
            row=len(rows) + 2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.grid(row=len(rows) + 3, column=0, columnspan=2, pady=(10, 0), sticky="e")
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="OK", command=self._ok).pack(side="right")
        self.vars["kind"].trace_add("write", lambda *_: self.vars["ext"].set(
            fetch.EXT_FOR_KIND.get(self.vars["kind"].get(), "txt")) if not self.vars["ext"].get() else None)
        self.vars["dataset"].trace_add("write", lambda *_: self.vars["local"].set(self.vars["dataset"].get().upper())
                                       if not src else None)
        self.transient(parent)
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
        self.geometry("1180x700")
        self.config_path = os.path.abspath(config_path)
        self.cfg = fetch.load_config(self.config_path)
        self.q: "queue.Queue[str]" = queue.Queue()
        self.busy = False

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.v_profile = tk.StringVar(value=self.cfg["zowe"].get("profile", ""))
        self.v_root = tk.StringVar(value=self.cfg["local_root"])
        self.v_db = tk.StringVar(value=self.cfg.get("db", "atlas.db"))
        ttk.Label(top, text="Zowe profile").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.v_profile, width=18).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(top, text="Local root").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=self.v_root, width=48).grid(row=0, column=3, padx=4)
        ttk.Button(top, text="Browse", command=self._browse_root).grid(row=0, column=4, padx=(0, 16))
        ttk.Label(top, text="Database").grid(row=0, column=5, sticky="w")
        ttk.Entry(top, textvariable=self.v_db, width=22).grid(row=0, column=6, padx=4)
        ttk.Button(top, text="Check Zowe", command=self.check_zowe).grid(row=0, column=7, padx=(16, 0))
        ttk.Button(top, text="Sign in...", command=self.sign_in).grid(row=0, column=8, padx=(4, 0))
        # One estate, many departments: filter the table to one system, fetch
        # or inspect that system alone, keep the rest untouched.
        ttk.Label(top, text="System").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.v_filter = tk.StringVar(value="All")
        self.cb_filter = ttk.Combobox(top, textvariable=self.v_filter, state="readonly", width=16, values=("All",))
        self.cb_filter.grid(row=1, column=1, padx=(4, 16), pady=(6, 0), sticky="w")
        self.cb_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        # Documents are not mainframe datasets: they already sit in a folder on
        # this laptop. Name those folders here and the build indexes them too.
        ttk.Label(top, text="Document folders").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_extra = tk.StringVar(value=";".join(self.cfg.get("extra_roots") or []))
        ttk.Entry(top, textvariable=self.v_extra, width=70).grid(row=2, column=1, columnspan=3, sticky="w", padx=(4, 4), pady=(6, 0))
        ttk.Button(top, text="Add folder", command=self._add_extra_root).grid(row=2, column=4, pady=(6, 0), sticky="w")
        ttk.Label(top, text="(semicolon-separated; indexed as documentation, images read by OCR)",
                  foreground="#555").grid(row=2, column=5, columnspan=3, sticky="w", pady=(6, 0))
        self.v_status = tk.StringVar(value=f"config: {self.config_path}")
        ttk.Label(top, textvariable=self.v_status, foreground="#555").grid(row=1, column=2, columnspan=6, sticky="w", pady=(6, 0))

        mid = ttk.Frame(self, padding=(8, 0, 8, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", selectmode="extended", height=12)
        for c, w in zip(COLUMNS, WIDTHS):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.edit_source())

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        for text, cmd in (("Add", self.add_source), ("Bulk add", self.bulk_add), ("Edit", self.edit_source),
                          ("Remove", self.remove_source), ("Enable/Disable", self.toggle_source),
                          ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                          ("Save", self.save)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        for text, cmd in (("Plan", self.plan), ("Fetch selected", self.fetch_selected),
                          ("Fetch system", self.fetch_system), ("Fetch all", self.fetch_all),
                          ("Build index", lambda: self.build(False)), ("Rebuild from empty", lambda: self.build(True)),
                          ("OCR images", self.ocr_images), ("Convert legacy Office", self.convert_legacy),
                          ("Coverage", self.coverage)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=2)

        self.log = scrolledtext.ScrolledText(self, height=14, wrap="none", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log.configure(state="disabled")

        self.refresh()
        self.after(150, self._poll)
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

    def _selected(self) -> List[int]:
        return [int(i) for i in self.tree.selection()]

    def _sync_cfg(self) -> None:
        self.cfg["zowe"]["profile"] = self.v_profile.get().strip()
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

    def add_source(self) -> None:
        d = SourceDialog(self)
        if d.result:
            self.cfg["sources"].append(d.result)
            self.refresh()
            self.save()

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

    def remove_source(self) -> None:
        sel = self._selected()
        if sel and messagebox.askyesno("Remove", f"Remove {len(sel)} source(s) from the config? Local files are kept."):
            for i in sorted(sel, reverse=True):
                del self.cfg["sources"][i]
            self.refresh()
            self.save()

    def toggle_source(self) -> None:
        for i in self._selected():
            s = self.cfg["sources"][i]
            s["enabled"] = not s.get("enabled", True)
        self.refresh()
        self.save()

    def save(self) -> None:
        self._sync_cfg()
        fetch.save_config(self.cfg, self.config_path)
        self.v_status.set(f"saved {self.config_path}")

    def _browse_root(self) -> None:
        d = filedialog.askdirectory(initialdir=self.v_root.get() or os.getcwd())
        if d:
            self.v_root.set(d)

    # ---- logging / background ------------------------------------------

    def _log(self, text: str) -> None:
        self.q.put(text)

    def _poll(self) -> None:
        try:
            while True:
                line = self.q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", line.rstrip("\n") + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll)

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
            finally:
                self.busy = False
                if done:
                    self.after(0, done)
        threading.Thread(target=target, daemon=True).start()

    def _stream(self, cmd: List[str]) -> int:
        self._log("> " + fetch.redact_cmd(cmd))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")
        assert p.stdout is not None
        for line in p.stdout:
            self._log(line)
        return p.wait()

    # ---- actions -------------------------------------------------------

    def check_zowe(self) -> None:
        self._sync_cfg()

        def go():
            ok, msg = fetch.check_zowe(self.cfg)
            self._log(("OK   " if ok else "FAIL ") + msg)
            self.after(0, lambda: self.v_status.set(msg))
        self._run_bg(go)

    def sign_in(self) -> None:
        """The mainframe password for THIS SESSION: zowe reads it from its own
        environment variable in the fetch subprocess. Nothing is saved to
        sources.json, the log or a crash file; closing the window forgets it."""
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
        e_user.grid(row=0, column=1, padx=6, pady=2)
        ttk.Label(f, text="Password").grid(row=1, column=0, sticky="w")
        e_pw = ttk.Entry(f, textvariable=v_pw, width=24, show="*")
        e_pw.grid(row=1, column=1, padx=6, pady=2)
        ttk.Label(f, text="Used only for the zowe commands of this session - never written to sources.json,\n"
                          "the log or a crash file. Leave the user id empty to keep the profile's.",
                  foreground="#555", justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 8))

        def use() -> None:
            fetch.set_session_credentials(v_user.get().strip() or None, v_pw.get() or None)
            v_pw.set("")
            self.v_status.set("signed in for this session" + (f" as {v_user.get().strip()}" if v_user.get().strip() else "")
                              if fetch.has_session_credentials() else "no session password")
            self._log("session password " + ("set (not stored)" if fetch.has_session_credentials() else "cleared"))
            win.destroy()

        def forget() -> None:
            fetch.set_session_credentials(None, None)
            self.v_status.set("no session password")
            win.destroy()

        b = ttk.Frame(f)
        b.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(b, text="Use for this session", command=use).pack(side="left", padx=2)
        ttk.Button(b, text="Forget", command=forget).pack(side="left", padx=2)
        ttk.Button(b, text="Cancel", command=win.destroy).pack(side="left", padx=2)
        (e_pw if v_user.get() else e_user).focus_set()
        win.bind("<Return>", lambda e: use())

    def plan(self) -> None:
        self._sync_cfg()
        for s in self.cfg["sources"]:
            self._log(" ".join(fetch.download_cmd(self.cfg, s)) + ("" if s.get("enabled", True) else "   (disabled)"))
        self._log(" ".join(fetch.build_cmd(self.cfg, self._manifest_path())))

    def _fetch(self, only: Optional[List[str]]) -> None:
        self._sync_cfg()
        self.save()

        def go():
            res = fetch.fetch_all(self.cfg, log=self._log, only=only)
            fetch.save_config(self.cfg, self.config_path)
            bad = sum(1 for r in res if not r.ok)
            self._log(f"done: {len(res) - bad} ok, {bad} failed")
        self._run_bg(go, self.refresh)

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
        self._sync_cfg()
        self.save()
        man = self._manifest_path()
        fetch.write_manifest(self.cfg, man)
        self._log(f"manifest written: {man}")
        self._run_bg(lambda: self._stream(fetch.build_cmd(self.cfg, man, rebuild)))

    def coverage(self) -> None:
        self._sync_cfg()
        db = self.cfg.get("db") or "atlas.db"
        self._run_bg(lambda: self._stream([sys.executable, "-m", "atlas.query", "--db", db, "coverage"]))

    # ---- departments -----------------------------------------------------

    def bulk_add(self) -> None:
        """Paste a department's dataset list, one per line; kinds are inferred
        from the names, load libraries are added disabled."""
        dlg = tk.Toplevel(self)
        dlg.title("Bulk add a department's libraries")
        f = ttk.Frame(dlg, padding=12)
        f.grid()
        v_sys = tk.StringVar(value=self.v_filter.get() if self.v_filter.get() != "All" else "")
        v_auth = tk.BooleanVar(value=True)
        ttk.Label(f, text="System / department").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=v_sys, width=20).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Checkbutton(f, text="These are the production (authoritative) libraries", variable=v_auth).grid(
            row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Datasets, one per line (SRC, COPYLIB, JCLLIB, PROCLIB, PARMLIB, PSBSOURCE, DBDSOURCE, "
                          "BMS, MFS, CSD extract, STAGE1 ...). PSBLIB/DBDLIB/ACBLIB/LOADLIB are compiled - skip them.",
                  wraplength=520, foreground="#555").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 2))
        txt = tk.Text(f, width=64, height=14, font=("Consolas", 10))
        txt.grid(row=3, column=0, columnspan=2)
        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))

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
        ttk.Button(b, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)
        ttk.Button(b, text="Add", command=ok).pack(side="right")
        dlg.transient(self)
        dlg.grab_set()

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

    def fetch_system(self) -> None:
        sysname = self.v_filter.get()
        if sysname == "All":
            messagebox.showinfo("Fetch system", "Pick a system in the filter first")
            return
        self._fetch([self.cfg["sources"][i]["dataset"] for i in fetch.filter_sources(self.cfg, sysname)
                     if self.cfg["sources"][i].get("enabled", True)])

    def ocr_images(self) -> None:
        """Pull the pictures out of the indexed documents and read them with the
        Windows OCR engine - no install, no network, no model tokens."""
        self._sync_cfg()
        db = self.cfg.get("db") or "atlas.db"
        out = os.path.join(self.cfg["local_root"], "out", "images")
        self._run_bg(lambda: self._stream([sys.executable, "-m", "atlas.ocr", "--db", db, "--out", out]))

    def convert_legacy(self) -> None:
        """Save .doc / .xls / .ppt under the document folders as .docx / .xlsx /
        .pptx beside the originals, with the Office installed on this laptop
        (nothing installed, nothing deleted). The build then reads the copies."""
        self._sync_cfg()
        roots = [r for r in (self.cfg.get("extra_roots") or []) if os.path.isdir(r)]
        if not roots:
            messagebox.showinfo("Convert legacy Office", "Name the document folder(s) under 'Document folders' first.")
            return
        self._run_bg(lambda: self._stream([sys.executable, "-m", "atlas.convert", *roots]))

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
