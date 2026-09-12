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
        b = ttk.Frame(f)
        b.grid(row=len(rows) + 2, column=0, columnspan=2, pady=(10, 0), sticky="e")
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
        self.v_status = tk.StringVar(value=f"config: {self.config_path}")
        ttk.Label(top, textvariable=self.v_status, foreground="#555").grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))

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
        for text, cmd in (("Add", self.add_source), ("Edit", self.edit_source), ("Remove", self.remove_source),
                          ("Enable/Disable", self.toggle_source), ("Save", self.save)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        for text, cmd in (("Plan", self.plan), ("Fetch selected", self.fetch_selected), ("Fetch all", self.fetch_all),
                          ("Build index", lambda: self.build(False)), ("Rebuild from empty", lambda: self.build(True)),
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
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.cfg["sources"]):
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
        self._log("> " + " ".join(cmd))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
