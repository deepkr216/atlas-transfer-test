"""
diag.py - a short, shareable report when something goes wrong. Contains no
source code, no dataset names, no paths - only shapes, counts and errors.

    python -m atlas.diag --db atlas.db            # environment, last build, failure signatures
    python -m atlas.diag --db atlas.db --redact   # additionally hash member names

Crash reports: build.py and query.py write `atlas-crash.txt` automatically
when they fail. Paste that file, or the output of this command, and it is
enough to reproduce and fix the problem without seeing the estate.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import Counter
from typing import Iterable, List, Optional

from . import __version__

HERE = os.path.dirname(os.path.abspath(__file__))

_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|]+")
_NIX_PATH = re.compile(r"(?<![\w.])/(?:[^\s'\"<>|/]+/)+[^\s'\"<>|/]*")
# two qualifiers (PROD.MASTER) are a dataset name too
_DSN = re.compile(r"\b[A-Z0-9$#@]{1,8}(?:\.[A-Z0-9$#@]{1,8}){1,}(?:\([+-]?\d+\))?(?![A-Z0-9$#@.])")


def redact(text: str, names: Iterable[str] = ()) -> str:
    """Strip paths and dataset names; optionally replace known member names."""
    if not text:
        return text
    text = _WIN_PATH.sub("<path>", text)
    text = _NIX_PATH.sub("<path>", text)
    text = _DSN.sub("<dsn>", text)
    for n in sorted(set(names), key=len, reverse=True):
        if len(n) >= 3:
            tag = "M" + hashlib.sha1(n.encode()).hexdigest()[:4].upper()
            text = re.sub(r"\b" + re.escape(n) + r"\b", tag, text)
    return text


def _git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(HERE),
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "?"
    except Exception:                                   # noqa: BLE001
        return "?"


def environment() -> List[str]:
    fts = "?"
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = "yes"
    except sqlite3.OperationalError:
        fts = "NO - full-text search unavailable"
    return [
        f"atlas {__version__} commit {_git_commit()}",
        f"python {platform.python_version()} ({platform.python_implementation()})",
        f"os {platform.system()} {platform.release()} {platform.machine()}",
        f"sqlite {sqlite3.sqlite_version}, fts5: {fts}",
        f"cwd-is-repo: {os.path.isdir(os.path.join(os.path.dirname(HERE), '.git'))}",
    ]


def _signature(msg: str) -> str:
    """Collapse an error message to its shape so identical bugs group together."""
    s = re.sub(r"\d+", "#", msg or "")
    s = re.sub(r"'[^']*'", "'…'", s)
    s = re.sub(r"\b[A-Z][A-Z0-9\-]{3,}\b", "NAME", s)
    return s[:140]


def report(db_path: str, redact_names: bool = False) -> str:
    out: List[str] = ["=== atlas diag ===", *environment()]
    if not os.path.exists(db_path):
        out.append(f"db: not found ({os.path.basename(db_path)}) - build has not run or failed before writing")
        return "\n".join(out)

    conn = sqlite3.connect(db_path)
    names: List[str] = []
    if redact_names:
        # member names, and every other estate name that can appear in an
        # error text: job names, copybook names, call targets, PROGRAM-IDs
        names = [r[0] for r in conn.execute("SELECT DISTINCT name FROM member")]
        for sql in ("SELECT DISTINCT job_name FROM job", "SELECT DISTINCT copybook FROM copy_use",
                    "SELECT DISTINCT target FROM call_edge WHERE target IS NOT NULL",
                    "SELECT DISTINCT program_id FROM program", "SELECT DISTINCT proc_name FROM proc_def"):
            try:
                names += [r[0] for r in conn.execute(sql) if r[0]]
            except sqlite3.OperationalError:
                pass

    run = conn.execute("SELECT * FROM build_run ORDER BY id DESC LIMIT 1").fetchone()
    if run:
        cols = [d[1] for d in conn.execute("PRAGMA table_info(build_run)")]
        r = dict(zip(cols, run))
        out.append(f"last build: started {r.get('started_at')} finished {r.get('finished_at') or 'NOT FINISHED'}; "
                   f"members {r.get('members')} ok {r.get('ok')} partial {r.get('partial')} failed {r.get('failed')}")
    else:
        out.append("last build: no build_run row (build crashed before inventory?)")

    out.append("members by kind/status:")
    for k, s, n in conn.execute("SELECT kind, parse_status, COUNT(*) FROM member GROUP BY 1,2 ORDER BY 1,2"):
        out.append(f"  {k:<10} {s:<8} {n}")

    fails = conn.execute("SELECT kind, name, parse_error FROM member WHERE parse_status='failed'").fetchall()
    if fails:
        sig = Counter(_signature(e) for _k, _n, e in fails)
        out.append(f"failure signatures ({len(fails)} members):")
        for s, n in sig.most_common(12):
            example = next((f"{k} {nm}: {redact(e, names)[:160]}" for k, nm, e in fails if _signature(e) == s), "")
            out.append(f"  x{n:<5} {s}")
            out.append(f"         e.g. {redact(example, names)}")

    unres = conn.execute("SELECT kind, COUNT(*) FROM unresolved GROUP BY 1 ORDER BY 2 DESC").fetchall()
    if unres:
        out.append("unresolved by kind: " + ", ".join(f"{k}={n}" for k, n in unres))
    exp = conn.execute("SELECT detail FROM unresolved WHERE kind='expand'").fetchall()
    if exp:
        sig = Counter(_signature(d[0]) for d in exp)
        out.append("expansion warning shapes:")
        for s, n in sig.most_common(6):
            out.append(f"  x{n:<5} {s}")

    fmt = conn.execute("SELECT fixed_format, COUNT(*) FROM member WHERE kind IN ('cobol','copybook') GROUP BY 1").fetchall()
    if fmt:
        out.append("cobol format detection: " + ", ".join(f"{'fixed' if f else 'free'}={n}" for f, n in fmt))
    widths = conn.execute("SELECT MIN(lines), MAX(lines), AVG(lines) FROM member WHERE kind='cobol'").fetchone()
    if widths and widths[0] is not None:
        out.append(f"cobol program sizes: min {widths[0]} max {widths[1]} avg {int(widths[2])} lines")
    amb = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member WHERE distinct_content>1").fetchone()[0]
    out.append(f"members with same name and different content: {amb}")
    conn.close()
    out.append("=== end ===")
    return "\n".join(out)


def write_crash(exc: BaseException, argv: List[str], path: str = "atlas-crash.txt") -> str:
    """Write a compact, redacted crash report. Called by build/query on failure."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if os.sep + "atlas" + os.sep in f.filename
              or "/atlas/" in f.filename.replace("\\", "/")]
    lines = ["=== atlas crash ===", time.strftime("%Y-%m-%dT%H:%M:%S"),
             "command: " + redact(" ".join((os.path.basename(a) if i == 0 else a)
                                           for i, a in enumerate(argv or []))),
             *environment(),
             f"exception: {type(exc).__name__}: {redact(str(exc))[:400]}",
             "where (toolkit frames only):"]
    for f in frames[-6:]:
        lines.append(f"  {os.path.basename(f.filename)}:{f.lineno} in {f.name}  |  {(f.line or '').strip()[:100]}")
    if not frames:
        lines.append("  (no toolkit frames - error raised outside atlas/; full type above)")
    lines.append("next: python -m atlas.diag --db <your.db> --redact   and paste both blocks")
    lines.append("=== end ===")
    text = "\n".join(lines)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass
    return text


def time_document(path: str, stack_every: float = 30.0, out=None) -> int:
    """Run ONE document through the same steps the build runs, in the
    foreground, timing each; while any step runs, every `stack_every`
    seconds the exact position (toolkit line numbers only) is written to
    stderr - the way to find out where a build sits on a file."""
    import faulthandler
    from . import docs
    say = out or (lambda s: print(s, flush=True))
    say(f"document: {os.path.basename(path)} ({os.path.getsize(path) // 1024} KB)")
    if stack_every:
        faulthandler.dump_traceback_later(stack_every, repeat=True)
    try:
        t0 = time.time()
        with open(path, "rb") as fh:
            data = fh.read()
        say(f"  read            {time.time() - t0:8.2f} s  ({len(data) // 1024} KB)")
        t0 = time.time()
        d = docs.extract(path)
        say(f"  extract         {time.time() - t0:8.2f} s  ({len(d.sections)} sections, {len(d.images)} pictures, "
            f"{sum(len(t) for _h, t in d.sections)} characters, kind {d.kind}, ok={d.ok})")
        for n in d.notes[:5]:
            say(f"    note: {n}")
        t0 = time.time()
        parts = docs.chunk_sections(d.sections)
        say(f"  chunk           {time.time() - t0:8.2f} s  ({len(parts)} citable sections)")
        say("  verdict: this document is not what holds the build" if True else "")
    finally:
        if stack_every:
            faulthandler.cancel_dump_traceback_later()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Shareable diagnostics for atlas (no source content).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--redact", action="store_true", help="hash member names too")
    ap.add_argument("--doc", metavar="FILE", help="time ONE document through the build's steps; if it hangs, its exact "
                                                  "position is printed every 30 s (toolkit line numbers only)")
    ap.add_argument("--stack-every", type=float, default=30.0, help="seconds between position dumps with --doc (0 = off)")
    a = ap.parse_args(argv)
    if a.doc:
        return time_document(a.doc, a.stack_every)
    print(report(a.db, a.redact))
    return 0


if __name__ == "__main__":
    sys.exit(main())
