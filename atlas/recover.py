r"""
atlas.recover - the copybooks the estate lacks, rebuilt from the expanded
text of the programs that copy them (LESSONS 167).

    python -m atlas.recover --db atlas.db                       from the compiler listings the index holds
    python -m atlas.recover --db atlas.db --from "C:\estate\GC\PDS.MEM.EXPANDED"
    python -m atlas.recover --db atlas.db --dry-run             say what would be written, write nothing

A program whose copybook is not in the estate folder is parsed only in
part: the fields of that copybook are missing from every answer. But the
expanded version of the program - a compiler listing, or an expanded source
member - holds the copybook's text inline. This tool finds where each copied
block starts and ends, writes the block out as the copybook member the
index lacks (marked as recovered, in a folder of its own under the estate),
and the next build resolves every program that copies it. Nothing in the
parsers changes.

Three ways an expanded text marks the copied lines are read:

  * a compiler listing, where each copied line carries a C after its line
    number (IBM Enterprise COBOL);
  * an expanded source whose copied records carry the copybook's name in
    columns 73-80 (what most include expanders leave there);
  * a comment line naming the copybook before the block (`*COPY NAME`,
    `++INCLUDE NAME`) - the end of the block is the next such comment or an
    END marker, and the report says the end was guessed.

A copybook seen in several programs is compared across them: identical
copies confirm it; where they differ, the copy without a REPLACING clause
wins, then the most common, and the report says so. A block that parses as
neither data items nor procedure code is not written. When the real
copybook later arrives in the estate, the recovered one is removed on the
next run, so the real one is the only one.

Every recovered copybook says in its first lines where it came from; it is
the copybook's text as one program saw it, not the library copy, and the
report (work\recover.md) names every one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import copybook, expand, reader

FOLDER = "RECOVERED-COPYBOOKS"
MARKER = ".atlas-recovered.json"
REPORT = os.path.join("work", "recover.md")
RESOLVER_KINDS = ("copybook", "cobol", "sql", "unknown")          # what the build's resolver accepts
MAX_SOURCE_BYTES = 64 * 1024 * 1024

_COPY_STMT = re.compile(r"^\s*(?:COPY|\+\+INCLUDE|-INC|EXEC\s+SQL\s+INCLUDE)\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\b", re.I)
_LISTING_LINE = re.compile(r"^[ 01\-+]?\s*(\d{6})([C+])?(?=\s)")
# a listing line: the line number, the blank PL/SL columns, then the source record with its OWN
# sequence number in columns 1-6 - two numbers, where a plain source record has one
_LISTING_SHAPE = re.compile(r"^[ 01\-+]?\s*\d{6}[C+]?\s+\d{6}[ *\-/D]")
_LISTING_HEAD = re.compile(r"^\s*LineID\s+PL\s+SL\b|IBM Enterprise COBOL|^1?PP\s+5655-", re.I | re.M)
_RULER = re.compile(r"-{3,}\+-\*A")
_NAME = re.compile(r"^[A-Z0-9@#$][A-Z0-9@#$\-_]{0,7}$")
_START_MARK = re.compile(r"^\s*\*?\s*(?:\+\+INCLUDE|-INC|BEGIN(?:NING)?\s+(?:OF\s+)?COPY(?:BOOK)?|COPY(?:BOOK)?)\s+"
                         r"([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\b", re.I)
_END_MARK = re.compile(r"^\s*\*?\s*END(?:\s+OF)?\s+(?:COPY(?:BOOK)?|INCLUDE)\b(?:\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9}))?", re.I)
_HEADER = re.compile(r"^\s*(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b|^\s*[A-Z0-9\-]+\s+SECTION\s*\.", re.I)
_LEVEL = re.compile(r"^\s*(0[1-9]|[1-4]\d|66|77|88)\s+[A-Z0-9][A-Z0-9\-]*", re.I)
_VERB = re.compile(r"\b(MOVE|PERFORM|IF|EVALUATE|CALL|READ|WRITE|OPEN|CLOSE|COMPUTE|ADD|DISPLAY|GOBACK|EXEC)\b", re.I)


class Region:
    """One copied block as one source saw it."""

    def __init__(self, name: str, records: List[str], source: str, fmt: str, replacing: bool = False,
                 nested: Optional[List[str]] = None, end_guessed: bool = False):
        self.name = name.upper()
        self.records = records
        self.source = source
        self.fmt = fmt
        self.replacing = replacing
        self.nested = nested or []
        self.end_guessed = end_guessed

    def key(self) -> str:
        """The text without sequence numbers, columns 73-80 or trailing blanks:
        what the compiler sees."""
        out = []
        for r in self.records:
            body = r[6:72].rstrip() if len(r) > 6 else ""
            if body.strip():
                out.append(body)
        return "\n".join(out)


# --------------------------------------------------------------------------
# the three formats
# --------------------------------------------------------------------------

def _copy_name(code: str) -> Optional[Tuple[str, str]]:
    """(name, the whole statement text) when `code` starts a COPY / INCLUDE."""
    m = _COPY_STMT.match(code)
    return (m.group(1).upper(), code) if m else None


def _comment_out(rec: str) -> str:
    rec = rec.ljust(8)
    return rec[:6] + "*" + rec[7:]


def from_ibm_listing(lines: Sequence[str], source: str) -> Tuple[List[Region], Dict[str, int]]:
    """A compiler listing: each source line carries a line number; a copied
    line carries a C after it. The ruler `----+-*A-1-B` fixes where the
    80-column source record starts on the line."""
    offset: Optional[int] = None
    for ln in lines[:400]:
        m = _RULER.search(ln)
        if m:
            offset = m.start()
            break
    if offset is None:
        for ln in lines:
            m = _LISTING_LINE.match(ln)
            if not m:
                continue
            rest = ln[m.end():]
            k = rest.upper().find("IDENTIFICATION DIVISION")
            if k < 0:
                k = rest.upper().find("PROCEDURE DIVISION")
            if k >= 0:
                offset = m.end() + k - 7                              # area A starts at source column 8
                break
    if offset is None:
        return [], {"no ruler": 1}
    regions: List[Region] = []
    stats: Dict[str, int] = Counter()
    pending: Optional[Tuple[str, str, bool]] = None                   # (name, statement, statement closed)
    cur: Optional[Region] = None
    nested_open = False
    for ln in lines:
        m = _LISTING_LINE.match(ln)
        if not m:
            continue                                                  # page headers, messages, blank lines
        flagged = bool(m.group(2))
        rec = ln[offset:offset + 80].rstrip("\r\n")
        code = rec[7:72] if len(rec) > 7 else ""
        if not flagged:
            if cur is not None:
                regions.append(cur)
                cur = None
                nested_open = False
            if pending and not pending[2]:                           # a COPY statement continued on the next line
                stmt = pending[1] + " " + code.strip()
                pending = (pending[0], stmt, "." in code)
                continue
            found = _copy_name(code)
            if found:
                pending = (found[0], found[1], "." in code)
            elif code.strip() and not code.startswith("*") and rec[6:7] not in ("*", "/"):
                pending = None
            continue
        stats["flagged lines"] += 1
        if cur is None:
            if pending is None:
                stats["flagged lines with no COPY before them"] += 1
                continue
            cur = Region(pending[0], [], source, "compiler listing", replacing="REPLACING" in pending[1].upper())
            pending = None
        if rec[6:7] not in ("*", "/") and (nested_open or _copy_name(code)):
            inner = _copy_name(code)
            if inner:
                cur.nested.append(inner[0])
            nested_open = "." not in code
            cur.records.append(_comment_out(rec))                    # the nested COPY: its text follows inline
            continue
        cur.records.append(rec)
    if cur is not None:
        regions.append(cur)
    return regions, dict(stats)


def from_cols_73_80(records: Sequence[str], source: str, wanted: Set[str]) -> List[Region]:
    """An expanded source whose copied records carry the copybook name in
    columns 73-80: a run of records with one name there, right after a COPY
    of that name, is the copybook."""
    regions: List[Region] = []
    i = 0
    n = len(records)
    stmt_of: Dict[str, str] = {}
    for r in records:
        found = _copy_name(r[7:72] if len(r) > 7 else "")
        if found:
            stmt_of[found[0]] = found[1]
    while i < n:
        tag = records[i][72:80].strip().upper() if len(records[i]) > 72 else ""
        if not tag or not _NAME.match(tag) or tag not in stmt_of and tag not in wanted:
            i += 1
            continue
        j = i
        while j < n and len(records[j]) > 72 and records[j][72:80].strip().upper() == tag:
            j += 1
        body = list(records[i:j])
        if body and _copy_name(body[0][7:72]) and _copy_name(body[0][7:72])[0] == tag:
            body = body[1:]                                          # the statement itself carries the tag too
        if body:
            nested = [c[0] for c in (_copy_name(b[7:72]) for b in body if len(b) > 7 and b[6:7] not in ("*", "/")) if c]
            body = [_comment_out(b) if (len(b) > 7 and b[6:7] not in ("*", "/") and _copy_name(b[7:72])) else b for b in body]
            regions.append(Region(tag, body, source, "cols 73-80", replacing="REPLACING" in stmt_of.get(tag, "").upper(),
                                  nested=nested))
        i = j
    return regions


def from_markers(records: Sequence[str], source: str) -> List[Region]:
    """A comment naming the copybook before the block; the block ends at an
    END marker, the next start marker, or a division / section header."""
    regions: List[Region] = []
    cur: Optional[Region] = None
    for rec in records:
        is_comment = len(rec) > 6 and rec[6:7] in ("*", "/") or rec.lstrip().startswith("*>")
        text = rec[7:72] if len(rec) > 7 else rec
        if is_comment:
            m_end = _END_MARK.match(text.lstrip("*> "))
            m_start = _START_MARK.match(text.lstrip("> "))
            if m_end and cur is not None:
                cur.end_guessed = False
                regions.append(cur)
                cur = None
                continue
            if m_start:
                if cur is not None:
                    regions.append(cur)
                cur = Region(m_start.group(1), [], source, "marker comments", end_guessed=True)
                continue
            if cur is not None:
                cur.records.append(rec)
            continue
        if cur is not None:
            if _HEADER.match(text) or (_copy_name(text) and _copy_name(text)[0] == cur.name):
                regions.append(cur)
                cur = None
                continue
            cur.records.append(rec)
    if cur is not None:
        regions.append(cur)
    return [r for r in regions if r.records]


def extract(text: str, source: str, wanted: Set[str]) -> Tuple[str, List[Region], Dict[str, int]]:
    """(format seen, regions, notes) for one expanded text."""
    lines = text.splitlines()
    head = "\n".join(lines[:400])
    if _RULER.search(head) or _LISTING_HEAD.search(head) or sum(1 for ln in lines[:2000] if _LISTING_SHAPE.match(ln)) >= 5:
        regions, stats = from_ibm_listing(lines, source)
        if regions or stats.get("flagged lines"):
            return "compiler listing", regions, stats
        if "no ruler" in stats:
            return "compiler listing without a source ruler", [], stats
        return "compiler listing with no copied line flagged", [], stats
    records = [ln.rstrip("\r\n") for ln in lines]
    regions = from_cols_73_80(records, source, wanted)
    if regions:
        return "columns 73-80", regions, {}
    regions = from_markers(records, source)
    if regions:
        return "marker comments", regions, {}
    has_copy = any(_copy_name(r[7:72] if len(r) > 7 else "") for r in records)
    return ("no copy marks" if has_copy else "no COPY statements"), [], {}


# --------------------------------------------------------------------------
# the index side
# --------------------------------------------------------------------------

def missing_copybooks(conn: sqlite3.Connection) -> Dict[str, int]:
    """{copybook name: programs copying it} for the copybooks no member of an
    accepted kind carries."""
    have = {r[0].upper() for r in conn.execute(
        f"SELECT DISTINCT name FROM member WHERE kind IN ({','.join('?' * len(RESOLVER_KINDS))})", RESOLVER_KINDS)}
    out: Dict[str, int] = {}
    for name, n in conn.execute("SELECT UPPER(copybook), COUNT(DISTINCT member_id) FROM copy_use "
                                "WHERE resolved_member_id IS NULL GROUP BY 1"):
        if name and name not in have and name not in expand._SYSTEM_INCLUDES:
            out[name] = int(n)
    return out


def real_copies(conn: sqlite3.Connection, out_dir: str) -> Set[str]:
    """Names for which the estate now holds a real member (outside the recovered folder)."""
    out_n = os.path.normcase(os.path.abspath(out_dir)) + os.sep
    names: Set[str] = set()
    for name, path in conn.execute(f"SELECT name, path FROM member WHERE kind IN ({','.join('?' * len(RESOLVER_KINDS))})",
                                   RESOLVER_KINDS):
        if not os.path.normcase(os.path.abspath(path)).startswith(out_n):
            names.add(name.upper())
    return names


def estate_root(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT root FROM build_run WHERE root IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def default_out(root: str) -> str:
    shared = os.path.join(root, "SHARED")
    return os.path.join(shared if os.path.isdir(shared) else root, FOLDER)


def listing_sources(conn: sqlite3.Connection, folders: Sequence[str]) -> List[str]:
    paths = [r[0] for r in conn.execute("SELECT path FROM member WHERE kind='listing' ORDER BY path")]
    seen = set(os.path.normcase(p) for p in paths)
    for folder in folders:
        if os.path.isfile(folder):
            if os.path.normcase(folder) not in seen:
                paths.append(folder)
            continue
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                p = os.path.join(dirpath, f)
                if os.path.normcase(p) not in seen and not f.startswith("."):
                    paths.append(p)
                    seen.add(os.path.normcase(p))
    return paths


# --------------------------------------------------------------------------
# choosing and checking
# --------------------------------------------------------------------------

def looks_like_copybook(records: Sequence[str]) -> str:
    """'data', 'procedure' or '' (nothing a copybook could be)."""
    text = "\n".join(records)
    try:
        lines, _fixed = reader.read_cobol_lines(text)
        logical = reader.join_cobol_continuations(lines)
        roots, _w = copybook.parse_data_division(logical)
        fields = [f for f in copybook.flatten(roots) if f.name != copybook.SYNTHETIC_ROOT]
    except Exception:                                                  # noqa: BLE001 - a block that breaks the parser is not a copybook
        fields = []
    if fields:
        return "data"
    code = "\n".join(r[7:72] for r in records if len(r) > 7 and r[6:7] not in ("*", "/"))
    if _VERB.search(code):
        return "procedure"
    if _LEVEL.search(code):
        return "data"
    return ""


def choose(regions: Sequence[Region]) -> Tuple[Optional[Region], str]:
    """One region per copybook: without REPLACING first, then the most common
    text, then the longest. Returns (region, how it was chosen)."""
    if not regions:
        return None, ""
    plain = [r for r in regions if not r.replacing] or list(regions)
    groups: Dict[str, List[Region]] = defaultdict(list)
    for r in plain:
        groups[r.key()].append(r)
    ranked = sorted(groups.values(), key=lambda g: (-len(g), -len(g[0].records)))
    best = ranked[0]
    pick = best[0]
    if len(groups) == 1:
        how = (f"seen identical in {len(best)} program(s)" if len(best) > 1 else "seen in one program")
    else:
        how = (f"{len(groups)} different texts across {len(plain)} program(s); the most common taken "
               f"({len(best)} program(s) agree)")
    if pick.replacing:
        how += "; every copy was under a REPLACING clause - the text is post-replacement"
    if pick.end_guessed:
        how += "; the end of the block was guessed (no END marker)"
    if pick.nested:
        how += f"; nested COPY {', '.join(sorted(set(pick.nested)))} kept inline"
    return pick, how


def render(name: str, region: Region, how: str) -> str:
    stamp = time.strftime("%Y-%m-%d")
    head = [
        f"      * RECOVERED by atlas.recover on {stamp} from the expanded text of",
        f"      * {os.path.splitext(os.path.basename(region.source))[0]} ({region.fmt}); {how}.",
        "      * The copybook as that program saw it, not the library copy: check offsets",
        "      * against the real copybook before relying on them; fetch the library when you can.",
    ]
    return "\n".join(head + [r.rstrip() for r in region.records]) + "\n"


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def run(db: str, folders: Sequence[str] = (), out_dir: Optional[str] = None, dry_run: bool = False,
        refresh: bool = False, everything: bool = False, log=print, report: Optional[str] = REPORT) -> Dict[str, object]:
    conn = sqlite3.connect(db)
    try:
        root = estate_root(conn)
        if out_dir is None:
            if not root:
                raise SystemExit("the index has no estate root recorded: give --out")
            out_dir = default_out(root)
        missing = missing_copybooks(conn)
        sources = listing_sources(conn, folders)
        real = real_copies(conn, out_dir)
    finally:
        conn.close()
    log(f"missing copybooks in the index: {len(missing):,} (copied by {sum(missing.values()):,} program-copies)")
    log(f"expanded texts to read: {len(sources):,} ({'compiler listings the index holds' if not folders else 'listings + the folders given'})")
    wanted = set(missing) if not everything else set()
    formats: Counter = Counter()
    by_name: Dict[str, List[Region]] = defaultdict(list)
    t0 = last = time.time()
    for k, path in enumerate(sources, 1):
        if time.time() - last >= 10:
            last = time.time()
            log(f"  ... {k:,}/{len(sources):,} read, {int(time.time() - t0)} s")
        try:
            if os.path.getsize(path) > MAX_SOURCE_BYTES:
                formats["too big"] += 1
                continue
            text, _data, _enc = reader.load(path)
        except OSError:
            formats["unreadable"] += 1
            continue
        fmt, regions, _stats = extract(text, path, wanted or set())
        formats[fmt] += 1
        for r in regions:
            if everything or r.name in missing:
                by_name[r.name].append(r)
    log("formats seen: " + ", ".join(f"{f} in {n:,}" for f, n in formats.most_common()))

    # the recovered folder: what is there, what the estate now has for real
    existing: Dict[str, dict] = {}
    marker = os.path.join(out_dir, MARKER)
    try:
        with open(marker, encoding="utf-8") as fh:
            existing = json.load(fh).get("copybooks", {})
    except (OSError, ValueError):
        existing = {}
    removed: List[str] = []
    for name in sorted(existing):
        if name in real:
            p = os.path.join(out_dir, name + ".cpy")
            if not dry_run:
                try:
                    os.remove(p)
                except OSError:
                    pass
            removed.append(name)
            existing.pop(name, None)
    if removed:
        log(f"  {len(removed)} recovered copybook(s) removed - the estate now holds the real member: "
            + ", ".join(removed[:8]) + (" ..." if len(removed) > 8 else ""))

    written: List[Tuple[str, str]] = []
    rejected: List[Tuple[str, str]] = []
    kept = 0
    for name in sorted(by_name):
        if name in real and not everything:
            continue
        if name in existing and not refresh:
            kept += 1
            continue
        pick, how = choose(by_name[name])
        if pick is None:
            continue
        shape = looks_like_copybook(pick.records)
        if not shape:
            rejected.append((name, f"the block is neither data items nor procedure code ({len(pick.records)} lines)"))
            continue
        written.append((name, how + f"; {shape} copybook, {len(pick.records)} lines"))
        if not dry_run:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, name + ".cpy"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render(name, pick, how))
            existing[name] = {"from": os.path.basename(pick.source), "format": pick.fmt, "how": how,
                              "written": time.strftime("%Y-%m-%d %H:%M:%S"), "lines": len(pick.records)}
    if not dry_run and (written or removed):
        os.makedirs(out_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"tool": "atlas.recover", "copybooks": existing}, fh, indent=1)
    not_found = sorted(n for n in missing if n not in by_name and n not in existing)

    lines = [f"# Recovered copybooks - {time.strftime('%Y-%m-%d %H:%M')}\n",
             f"\n- missing in the index: {len(missing)}; expanded texts read: {len(sources)}; formats: "
             + ", ".join(f"{f} in {n}" for f, n in formats.most_common()),
             f"\n- written to `{out_dir}`: {len(written)}" + (" (dry run: nothing written)" if dry_run else "")
             + f"; already there: {kept}; rejected: {len(rejected)}; removed (real member arrived): {len(removed)}",
             f"\n- not in any expanded text: {len(not_found)}\n"]
    if written:
        lines.append("\n## Written\n\n| copybook | programs copying it | how |\n|---|---|---|\n")
        lines += [f"| {n} | {missing.get(n, 0)} | {how} |\n" for n, how in written]
    if rejected:
        lines.append("\n## Rejected\n\n| copybook | why |\n|---|---|\n")
        lines += [f"| {n} | {why} |\n" for n, why in rejected]
    if not_found:
        lines.append("\n## Not in any expanded text (fetch these libraries)\n\n| copybook | programs copying it |\n|---|---|\n")
        lines += [f"| {n} | {missing[n]} |\n" for n in not_found]
    if report:
        os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
        with open(report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(lines))
    agree = sum(1 for _n, how in written if "identical in" in how)
    differ = sum(1 for _n, how in written if "different texts" in how)
    log(f"recovered: {len(written):,} of {len(missing):,} missing copybooks"
        + (" (dry run - nothing written)" if dry_run else f" -> {out_dir}")
        + f"; {agree:,} confirmed by 2+ programs, {differ:,} differ between programs (most common taken), "
        f"{len(rejected):,} rejected, {len(not_found):,} in no expanded text"
        + (f"; {kept:,} already recovered earlier" if kept else ""))
    if report:
        log(f"every name: {report}")
    if written and not dry_run:
        log("next: run your usual build command - the programs that copy them re-expand by themselves")
    return {"missing": len(missing), "sources": len(sources), "written": len(written), "rejected": len(rejected),
            "not_found": len(not_found), "removed": len(removed), "kept": kept, "formats": dict(formats),
            "out": out_dir}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the copybooks the estate lacks from the expanded text of the "
                                             "programs that copy them (compiler listings, expanded source).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--from", dest="folders", action="append", default=[], metavar="FOLDER",
                    help="a folder (or file) of expanded programs to read, besides the compiler listings the index "
                         "holds; repeatable")
    ap.add_argument("--out", metavar="FOLDER", help=f"where the recovered copybooks go (default: <estate>\\SHARED\\{FOLDER}, "
                                                    f"or <estate>\\{FOLDER})")
    ap.add_argument("--dry-run", action="store_true", help="say what would be written; write nothing")
    ap.add_argument("--refresh", action="store_true", help="write again the copybooks recovered on an earlier run")
    ap.add_argument("--all", action="store_true", help="recover every copybook seen, not only the missing ones")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.db):
        print(f"not found: {a.db} - run this from the folder holding the index, or give --db", flush=True)
        return 2
    for f in a.folders:
        if not os.path.exists(f):
            print(f"not found: {f}", flush=True)
            return 2
    try:
        run(a.db, a.folders, a.out, a.dry_run, a.refresh, a.all, log=lambda s: print(s, flush=True))
    except SystemExit as e:
        print(str(e), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
