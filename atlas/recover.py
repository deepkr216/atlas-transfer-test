r"""
atlas.recover - the copybooks the estate lacks, rebuilt from the expanded
text of the programs that copy them (LESSONS 167, 168).

    python -m atlas.recover --db atlas.db                       from the compiler listings the index holds
    python -m atlas.recover --db atlas.db --from "C:\estate\GC\PDS.MEM.LISTING"
    python -m atlas.recover --db atlas.db --dry-run             say what would be written, write nothing

A program whose copybook is not in the estate folder is parsed only in
part: the fields of that copybook are missing from every answer. But the
expanded version of the program - a compiler listing, or an expanded source
member - holds the copybook's text inline. This tool finds where each copied
block starts and ends, writes the block out as the copybook member the
index lacks (marked as recovered, in a folder of its own under the estate),
and the next build resolves every program that copies it. Nothing in the
parsers changes.

Four ways of finding the copied lines, tried in this order for each text:

  * a compiler listing, where every copied line carries a C after its line
    number (IBM Enterprise COBOL) - the ruler `----+-*A-1-B` on the page
    says where the 80-column record starts, and every record is checked to
    have the shape of one (sequence area, indicator column) before anything
    is trusted;
  * the text lined up against the ORIGINAL program in the index: the lines
    the expanded text adds at a COPY statement are that copybook - no marks
    needed, and it also covers a listing whose copied lines are not flagged;
  * an expanded source whose copied records carry the copybook's name in
    columns 73-80 (what most include expanders leave there);
  * a comment naming the copybook before the block (`*COPY NAME`,
    `++INCLUDE NAME`) - the block's end is guessed, so such a copybook is
    written only when two programs give the same text.

A copybook seen in several programs is compared across them: identical
copies confirm it; where they differ, the copy without a REPLACING clause
wins, then the most common, and the report says so. When two SYSTEMS (GC
and GC-TEST) hold different texts, each system gets its own copy under its
own folder, so no program expands the other system's layout. A block is
written only when the copybook parser reads it as data items, procedure
code, or comments, and the build would file it as a copybook. When the
real copybook later arrives in the estate, the recovered one is removed on
the next run and the programs that had expanded it are marked for the next
build.

Every recovered copybook says in its first lines where it came from; it is
the copybook's text as one program saw it, not the library copy, and the
report (work\recover.md) names every one.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import classify, copybook, expand, reader

FOLDER = "RECOVERED-COPYBOOKS"
MARKER = ".atlas-recovered.json"
REPORT = os.path.join("work", "recover.md")
RESOLVER_KINDS = ("copybook", "cobol", "sql", "unknown")          # what the build's resolver accepts
MAX_SOURCE_BYTES = 64 * 1024 * 1024
SHAPE_OK = 0.98                                                  # records that must look like 80-column source

_COPY_KW = re.compile(r"(?<![A-Z0-9\-])(?:COPY|\+\+INCLUDE|-INC)\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})(?![A-Z0-9\-])", re.I)
_SQL_INCLUDE = re.compile(r"(?<![A-Z0-9\-])EXEC\s+SQL\s+INCLUDE\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})(?![A-Z0-9\-])", re.I)
_EXEC_SQL = re.compile(r"(?<![A-Z0-9\-])EXEC\s+SQL\b", re.I)
# the flag column after the line number is a run: C for a copied line, ** for a statement out of
# sequence (IBM prints both, and C** together), anything else this tool does not know
_LISTING_LINE = re.compile(r"^[ 01\-+]?\s*(\d{6})([^\s\d]*)(?=\s|$)")
# a listing line: the line number, the blank PL/SL columns, then the source record with its OWN
# sequence number in columns 1-6 - two numbers, where a plain source record has one
_LISTING_SHAPE = re.compile(r"^[ 01\-+]?\s*\d{6}[^\s\d]*\s+\d{6}[ *\-/D]")
_LISTING_HEAD = re.compile(r"^\s*LineID\s+PL\s+SL\b|IBM Enterprise COBOL|^1?PP\s+5655-", re.I | re.M)
_RULER = re.compile(r"-{3,}\+-\*A")
_NAME = re.compile(r"^[A-Z0-9@#$][A-Z0-9@#$\-_]{0,7}$")
_START_MARK = re.compile(r"^\s*\*?\s*(?:\+\+INCLUDE|-INC|BEGIN(?:NING)?\s+(?:OF\s+)?COPY(?:BOOK)?|COPY(?:BOOK)?)\s+"
                         r"([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\b", re.I)
_END_MARK = re.compile(r"^\s*\*?\s*END(?:\s+OF)?\s+(?:COPY(?:BOOK)?|INCLUDE)\b(?:\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9}))?", re.I)
_HEADER = re.compile(r"^\s*(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b|^\s*[A-Z0-9\-]+\s+SECTION\s*\.", re.I)
_LEVEL = re.compile(r"^\s*(0[1-9]|[1-4]\d|66|77|88)\s+(?::[A-Z0-9]+:)?-?[A-Z0-9][A-Z0-9\-]*", re.I)
_VERB = re.compile(r"\b(MOVE|PERFORM|IF|EVALUATE|CALL|READ|WRITE|REWRITE|DELETE|START|OPEN|CLOSE|COMPUTE|ADD|SUBTRACT|"
                   r"MULTIPLY|DIVIDE|DISPLAY|ACCEPT|GOBACK|EXEC|SET|STRING|UNSTRING|INSPECT|INITIALIZE|GO|EXIT|CONTINUE|"
                   r"SEARCH|STOP|SORT|RETURN|RELEASE)\b", re.I)
_PARAGRAPH = re.compile(r"^\s*[A-Z0-9][A-Z0-9\-]*\s*\.\s*$", re.I)
_DATE = re.compile(r"\bDate\s+(\d\d/\d\d/\d{4})", re.I)


class Region:
    """One copied block as one source saw it."""

    def __init__(self, name: str, records: List[str], source: str, fmt: str, replacing: bool = False,
                 nested: Optional[List[str]] = None, end_guessed: bool = False, system: Optional[str] = None,
                 suspect: str = "", trusted: bool = True, statement: str = "", line: int = 0):
        self.name = name.upper()
        self.line = line                  # line of the file where the block starts (1-based), when known
        self.records = records
        self.source = source
        self.fmt = fmt
        self.replacing = replacing
        self.nested = nested or []
        self.end_guessed = end_guessed
        self.system = system
        self.suspect = suspect            # why this copy must not be written on its own
        self.trusted = trusted            # False: needs another program to agree
        self.statement = statement

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
# reading a COPY statement
# --------------------------------------------------------------------------

def copy_in(code: str) -> Optional[Tuple[str, str]]:
    """(name, statement text) when a COPY / INCLUDE statement starts in
    `code` - anywhere on the line, outside literals."""
    masked = expand._mask_literals(code)
    m = _SQL_INCLUDE.search(masked) or _COPY_KW.search(masked)
    if not m:
        return None
    return m.group(1).upper(), code[m.start():]


def _is_comment(rec: str) -> bool:
    return (len(rec) > 6 and rec[6:7] in ("*", "/")) or rec.lstrip().startswith("*>")


def _blank_statement(rec: str, statement: str) -> str:
    """The record with the COPY statement text blanked, the rest kept."""
    k = rec.find(statement.strip()[:20])
    if k < 0:
        return _comment_out(rec)
    end = rec.find(".", k)
    end = len(rec) if end < 0 else end + 1
    return rec[:k] + " " * (end - k) + rec[end:]


def _comment_out(rec: str) -> str:
    rec = rec.ljust(8)
    return rec[:6] + "*" + rec[7:]


INDICATORS = (" ", "*", "-", "D", "d", "/", "$")


def unshaped(rec: str) -> str:
    """Why a record is not 80-column COBOL source, or ''. The compiler
    ignores columns 1-6, so anything printable may sit there (a sequence
    number, a change tag, a date); column 7 is the indicator and must be one
    of the few the compiler knows - a shifted record fails there."""
    seq, ind = rec[:6], rec[6:7] if len(rec) > 6 else " "
    if any(ord(ch) < 32 for ch in seq):
        return "a control character in columns 1-6"
    if ind not in INDICATORS:
        return f"column 7 is `{ind}`, not blank / * / - / D"
    return ""


def shaped(records: Sequence[str]) -> float:
    """The share of records that look like 80-column COBOL source."""
    n = ok = 0
    for r in records:
        if not r.strip():
            continue
        n += 1
        if not unshaped(r):
            ok += 1
    return ok / n if n else 1.0


# --------------------------------------------------------------------------
# 1. a compiler listing with flagged lines
# --------------------------------------------------------------------------

FALLBACK_OK = 0.9          # a guessed source column must make this share of the first source lines look like source
# the headings that open the sections after the source. NOT "Cross Reference" with a space: every
# source page's ruler ends with "Map and Cross Reference", and a page break is not the end
_SOURCE_END = re.compile(r"Data Division Map|Message code|Cross-reference of|Program Statistics|"
                         r"Nested Program Map|Diagnostic Messages|End of compilation|"
                         r"COPY/BASIS|Procedure Division Map|Constant Global Table", re.I)


def _section_heading(ln: str) -> bool:
    """A line that opens a section after the source - never a page header,
    which carries the ruler and the banner."""
    return bool(_SOURCE_END.search(ln)) and not _RULER.search(ln) and not _LISTING_HEAD.search(ln)


def listing_offset(lines: Sequence[str]) -> Optional[int]:
    """Where the 80-column source record starts on a listing line. The ruler
    `----+-*A-1-B` is the compiler's own statement of it and is trusted.
    Without a ruler the division header gives a guess, tried a few columns
    either way against the shape of the first source lines - only those:
    the maps and cross-reference tables after the source carry line numbers
    too and are not source records. Every recovered block is checked for
    its shape again on its own before it is written."""
    for ln in lines:
        m = _RULER.search(ln)
        if m:
            return m.start()
    base: Optional[int] = None
    start = 0
    for i, ln in enumerate(lines):
        m = _LISTING_LINE.match(ln)
        if not m:
            continue
        rest = ln[m.end():].upper()
        k = max(rest.find("IDENTIFICATION DIVISION"), rest.find("PROCEDURE DIVISION"))
        if k >= 0:
            base = m.end() + k - 7                                         # area A starts at source column 8
            start = i
            break
    if base is None:
        return None
    sample: List[str] = []
    for ln in lines[start:]:
        if _section_heading(ln):
            break                                                      # the maps and tables after the source
        if _LISTING_LINE.match(ln):
            sample.append(ln)
            if len(sample) >= 300:
                break
    best, best_score = None, 0.0
    for off in (base, base - 1, base + 1, base - 2, base + 2, base - 3, base + 3):
        if off < 0:
            continue
        score = shaped([ln[off:off + 80] for ln in sample])
        if score > best_score:
            best, best_score = off, score
    return best if best is not None and best_score >= FALLBACK_OK else None


def listing_records(lines: Sequence[str]) -> Tuple[List[Tuple[str, str, int]], Optional[int]]:
    """[(flag, 80-column record, file line number)] for every source line of
    a listing - and only the source: after the program end the listing goes
    on with the data division map and the cross-reference tables, whose
    lines carry line numbers too, some followed by a letter, and where the
    copybook names appear again (LESSONS 170). A heading that opens one of
    those sections ends the reading."""
    off = listing_offset(lines)
    if off is None:
        return [], None
    out = []
    seen = 0
    for i, ln in enumerate(lines, 1):
        m = _LISTING_LINE.match(ln)
        if m:
            out.append((m.group(2) or "", ln[off:off + 80].rstrip("\r\n"), i))
            seen += 1
        elif seen >= 20 and _section_heading(ln):
            break                                                      # a heading, not a numbered line: the source is over
    return out, off


def masked(text: str) -> str:
    """A line with its letters and digits masked (A, 9): the shape, nothing from the estate."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", (text or "").rstrip()))


def from_ibm_listing(lines: Sequence[str], source: str, system: Optional[str]) -> Tuple[List[Region], Dict[str, int]]:
    """A compiler listing: each source line carries a line number; a copied
    line carries a C after it."""
    recs, off = listing_records(lines)
    stats: Dict[str, int] = Counter()
    if off is None:
        return [], {"no ruler": 1}
    stats["source column"] = off + 1                                   # 1-based, for the report
    ruler_line = next((i for i, ln in enumerate(lines, 1) if _RULER.search(ln)), 0)
    stats["ruler line"] = ruler_line
    last_plain: Tuple[int, str] = (0, "")                              # the last program line seen (line, shape)
    untied: List[Tuple[int, int, str]] = []                            # (first untied line, previous program line, its shape)
    compiled = ""
    for ln in lines[:60]:
        m = _DATE.search(ln)
        if m:
            compiled = m.group(1)
            break
    regions: List[Region] = []
    pending: Optional[Tuple[str, str, bool]] = None                   # (name, statement, closed)
    sql_open: List[str] = []                                          # an EXEC SQL statement gathered over lines
    cur: Optional[Region] = None
    for flag, rec, lineno in recs:
        code = rec[7:72] if len(rec) > 7 else ""
        flag = flag.replace("*", "").upper()                          # ** = out of sequence, not a copy mark
        if not flag:
            if cur is not None:
                regions.append(cur)
                cur = None
            if _is_comment(rec) or not code.strip():
                continue
            last_plain = (lineno, masked(rec))
            if sql_open:
                sql_open.append(code.strip())
                if "END-EXEC" in code.upper():
                    found = copy_in(" ".join(sql_open))
                    pending = (found[0], found[1], True) if found else None
                    sql_open = []
                continue
            if pending and not pending[2]:                           # a COPY statement continued on the next line
                pending = (pending[0], pending[1] + " " + code.strip(), "." in code)
                continue
            found = copy_in(code)
            if found:
                pending = (found[0], found[1], "." in found[1])
            elif _EXEC_SQL.search(expand._mask_literals(code)) and "END-EXEC" not in code.upper():
                sql_open = [code.strip()]
            else:
                pending = None
            continue
        if flag not in ("C", "+"):
            stats[f"lines with an unknown flag {flag!r}"] += 1
            if cur is not None:
                cur.suspect = f"a line carries the flag {flag!r}, which this tool does not know"
            continue
        stats["flagged lines"] += 1
        if cur is None:
            if pending is None:
                stats["flagged lines with no COPY before them"] += 1
                if not untied or untied[-1][1] != last_plain[0]:
                    untied.append((lineno, last_plain[0], last_plain[1]))
                continue
            cur = Region(pending[0], [], source, "compiler listing", replacing="REPLACING" in pending[1].upper(),
                         system=system, statement=pending[1], line=lineno)
            pending = None
        inner = None if _is_comment(rec) else copy_in(code)
        if inner:
            cur.nested.append(inner[0])
            cur.records.append(_blank_statement(rec, inner[1]))        # the nested COPY: its text follows inline
            continue
        cur.records.append(rec)
    if cur is not None:
        regions.append(cur)
    for r in regions:
        r.compiled = compiled                                          # type: ignore[attr-defined]
    out = dict(stats)
    if untied:
        out["untied"] = untied                                         # type: ignore[assignment]
    return regions, out


# --------------------------------------------------------------------------
# 2. the text lined up against the original program
# --------------------------------------------------------------------------

def _norm(rec: str) -> str:
    return (rec[6:72] if len(rec) > 6 else rec).rstrip()


def from_alignment(original: Sequence[str], expanded: Sequence[str], source: str,
                   system: Optional[str]) -> Tuple[List[Region], Dict[str, int]]:
    """The lines the expanded text adds at a COPY statement of the original
    are that copybook. A text that differs from the original anywhere else
    is not a pure expansion: its copybooks need a second program to agree."""
    a = [_norm(r) for r in original]
    b = [_norm(r) for r in expanded]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    stats: Dict[str, int] = Counter()
    found: List[Tuple[int, int, int]] = []                             # (orig index of the COPY, j1, j2)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            stats["lines the expanded text lacks"] += i2 - i1
            continue
        def statement_before(i: int) -> List[int]:
            """The COPY statement that ends on line i-1: its first line, when
            the lines before are one unterminated statement."""
            k = i - 1
            while k >= 0 and (not a[k].strip() or _is_comment(original[k])):
                k -= 1
            steps = 0
            while k > 0 and "." not in a[k - 1] and a[k - 1].strip() and not _is_comment(original[k - 1]) and steps < 5:
                k -= 1
                steps += 1
            return [k] if k >= 0 and not _is_comment(original[k]) and copy_in(a[k][1:] if a[k] else "") else []

        if tag == "replace":
            copies = [k for k in range(i1, i2) if not _is_comment(original[k]) and copy_in(a[k][1:] if a[k] else "")]
            if not copies and all(not a[k].strip() or _is_comment(original[k]) for k in range(i1, i2)):
                copies = statement_before(i1)                     # only a blank or a comment gave way to the block
        else:
            copies = statement_before(i1)
        if tag == "replace":
            # the original lines the block replaced must all belong to the COPY statement (which may
            # run on over the next lines up to its period) or be its commented echo
            in_stmt: Set[int] = set()
            for k in copies:
                in_stmt.add(k)
                kk = k
                while "." not in a[kk] and kk + 1 < len(a) and kk - k < 5:
                    kk += 1
                    in_stmt.add(kk)
            others = [k for k in range(i1, i2) if k not in in_stmt and a[k].strip() and not _is_comment(original[k])]
            if others:
                stats["changes not at a COPY"] += 1
                continue
        if len(copies) != 1:
            stats["changes not at a COPY" if not copies else "two COPY statements at one change"] += 1
            continue
        found.append((copies[-1], j1, j2))
    regions: List[Region] = []
    stray = stats.get("changes not at a COPY", 0) + stats.get("lines the expanded text lacks", 0)
    for k, j1, j2 in found:
        name, stmt = copy_in(a[k][1:] if a[k] else "")                 # type: ignore[misc]
        block = []
        for rec in expanded[j1:j2]:
            body = _norm(rec)
            if body[1:].strip() == a[k][1:].strip() or (copy_in(body[1:] if body else "") or ("",))[0] == name and _is_comment(rec):
                continue                                               # the COPY statement's own echo
            inner = None if _is_comment(rec) else copy_in(body[1:] if body else "")
            block.append(_blank_statement(rec, inner[1]) if inner else rec)
        if not block:
            continue
        nested = [c[0] for c in (copy_in(_norm(r)[1:]) for r in expanded[j1:j2] if not _is_comment(r)) if c and c[0] != name]
        regions.append(Region(name, block, source, "lined up with the program", replacing="REPLACING" in stmt.upper(),
                              nested=nested, system=system, trusted=stray == 0, statement=stmt,
                              suspect="" if stray == 0 else "the expanded text differs from the program elsewhere too"))
    return regions, dict(stats)


# --------------------------------------------------------------------------
# 3. columns 73-80    4. marker comments
# --------------------------------------------------------------------------

def from_cols_73_80(records: Sequence[str], source: str, wanted: Set[str], system: Optional[str]) -> List[Region]:
    regions: List[Region] = []
    stmt_of: Dict[str, str] = {}
    for r in records:
        if not _is_comment(r):
            found = copy_in(r[7:72] if len(r) > 7 else "")
            if found:
                stmt_of[found[0]] = found[1]
    i, n = 0, len(records)
    while i < n:
        tag = records[i][72:80].strip().upper() if len(records[i]) > 72 else ""
        if not tag or not _NAME.match(tag) or (tag not in stmt_of and tag not in wanted):
            i += 1
            continue
        j = i
        while j < n and len(records[j]) > 72 and records[j][72:80].strip().upper() == tag:
            j += 1
        body = list(records[i:j])
        own = copy_in(body[0][7:72]) if body and not _is_comment(body[0]) else None
        if own and own[0] == tag:
            body = body[1:]                                            # the statement itself carries the tag too
        if body:
            nested, out = [], []
            for b in body:
                inner = None if _is_comment(b) else copy_in(b[7:72] if len(b) > 7 else "")
                if inner:
                    nested.append(inner[0])
                    out.append(_blank_statement(b, inner[1]))
                else:
                    out.append(b)
            regions.append(Region(tag, out, source, "columns 73-80", replacing="REPLACING" in stmt_of.get(tag, "").upper(),
                                  nested=nested, system=system, statement=stmt_of.get(tag, "")))
        i = j
    return regions


def from_markers(records: Sequence[str], source: str, system: Optional[str]) -> List[Region]:
    regions: List[Region] = []
    cur: Optional[Region] = None
    for rec in records:
        text = rec[7:72] if len(rec) > 7 else rec
        if _is_comment(rec):
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
                cur = Region(m_start.group(1), [], source, "marker comments", end_guessed=True, trusted=False,
                             system=system, suspect="the end of the block was guessed (no END marker)")
                continue
            if cur is not None:
                cur.records.append(rec)
            continue
        if cur is not None:
            own = copy_in(text)
            if _HEADER.match(text) or (own and own[0] == cur.name):
                regions.append(cur)
                cur = None
                continue
            cur.records.append(rec)
    if cur is not None:
        regions.append(cur)
    for r in regions:
        if not r.end_guessed:
            r.trusted = True
            r.suspect = ""
    return [r for r in regions if r.records]


# --------------------------------------------------------------------------
# one text
# --------------------------------------------------------------------------

def split_lines(text: str) -> List[str]:
    lines = text.splitlines()
    if len(lines) <= 1 and len(text) >= 160:                          # a listing unloaded as fixed records, no line ends
        lines = reader._split_records(text, b"", "")
    return lines


def extract(text: str, source: str, wanted: Set[str], original: Optional[Sequence[str]] = None,
            system: Optional[str] = None) -> Tuple[str, List[Region], Dict[str, int]]:
    """(format seen, regions, notes) for one expanded text."""
    lines = split_lines(text)
    head = "\n".join(lines[:400])
    listing = _RULER.search(head) or _LISTING_HEAD.search(head) or sum(1 for ln in lines[:2000] if _LISTING_SHAPE.match(ln)) >= 5
    records: List[str]
    carried: Dict[str, int] = {}                                       # the listing's own notes, whatever is found later
    if listing:
        regions, stats = from_ibm_listing(lines, source, system)
        if regions:
            return "compiler listing", regions, stats
        carried = stats
        recs, off = listing_records(lines)
        if off is None:
            return "compiler listing without a readable source column", [], stats
        records = [r for _f, r, _n in recs]
        fmt_base = ("compiler listing, copied lines not tied to a COPY statement" if stats.get("flagged lines")
                    else "compiler listing, copied lines not flagged")
    else:
        records = [ln.rstrip("\r\n") for ln in lines]
        fmt_base = "expanded source"
    if original:
        regions, stats = from_alignment(original, records, source, system)
        if regions:
            return fmt_base + ", lined up with the program", regions, {**carried, **stats}
    regions = from_cols_73_80(records, source, wanted, system)
    if regions:
        return "columns 73-80", regions, carried
    regions = from_markers(records, source, system)
    if regions:
        return "marker comments", regions, carried
    if carried.get("flagged lines"):
        return fmt_base, [], carried
    has_copy = any(not _is_comment(r) and copy_in(r[7:72] if len(r) > 7 else "") for r in records)
    if not has_copy:
        return "no COPY statements", [], carried
    return (fmt_base + ", no copy marks" + ("" if original else " and the program is not in the index to line up with")), [], carried


# --------------------------------------------------------------------------
# the index side
# --------------------------------------------------------------------------

def missing_copybooks(conn: sqlite3.Connection) -> Dict[str, int]:
    """{copybook name: programs copying it} for the copybooks no member of an
    accepted kind carries (a program copying a name that is only its own
    name counts as missing too)."""
    kinds = ",".join("?" * len(RESOLVER_KINDS))
    out: Dict[str, int] = {}
    for name, n in conn.execute(
            f"""SELECT UPPER(c.copybook), COUNT(DISTINCT c.member_id) FROM copy_use c
                WHERE c.resolved_member_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM member m WHERE UPPER(m.name)=UPPER(c.copybook)
                                  AND m.kind IN ({kinds}) AND m.id != c.member_id)
                GROUP BY 1""", RESOLVER_KINDS):
        if name and name not in expand._SYSTEM_INCLUDES:
            out[name] = int(n)
    return out


def needing_programs(conn: sqlite3.Connection, missing: Dict[str, int]) -> Set[str]:
    """The programs that copy a missing copybook: only their expanded text
    can hold it, so only theirs is read."""
    if not missing:
        return set()
    names: Set[str] = set()
    q = ",".join("?" * len(missing))
    for (name,) in conn.execute(f"SELECT DISTINCT UPPER(m.name) FROM copy_use c JOIN member m ON m.id = c.member_id "
                                f"WHERE c.resolved_member_id IS NULL AND UPPER(c.copybook) IN ({q})", tuple(missing)):
        names.add(name)
    return names


def real_copies(conn: sqlite3.Connection, roots: Sequence[str]) -> Set[str]:
    """Names for which the estate now holds a real member outside every
    recovered folder."""
    rec = [os.path.normcase(os.path.abspath(r)) + os.sep for r in roots]
    names: Set[str] = set()
    for name, path in conn.execute(f"SELECT name, path FROM member WHERE kind IN ({','.join('?' * len(RESOLVER_KINDS))})",
                                   RESOLVER_KINDS):
        p = os.path.normcase(os.path.abspath(path))
        if not any(p.startswith(r) for r in rec) and FOLDER.lower() not in p.lower():
            names.add(name.upper())
    return names


def estate_root(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT root FROM build_run WHERE root IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def shared_out(root: str) -> str:
    shared = os.path.join(root, "SHARED")
    return os.path.join(shared if os.path.isdir(shared) else root, FOLDER)


def system_of(path: str, root: Optional[str]) -> Optional[str]:
    """The system a file belongs to by the estate layout (estate\\SYSTEM\\LIBRARY\\member)."""
    if not root:
        return None
    root_n = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    p = os.path.abspath(path)
    if not os.path.normcase(p).startswith(root_n + os.sep):
        return None
    parts = p[len(root_n) + 1:].split(os.sep)
    return parts[0].upper() if len(parts) >= 3 else None


def listing_sources(conn: sqlite3.Connection, folders: Sequence[str]) -> List[Tuple[str, Optional[str]]]:
    """[(path, system)] - the listings the index holds, then the folders given."""
    out: List[Tuple[str, Optional[str]]] = []
    seen: Set[str] = set()
    for path, system in conn.execute("SELECT path, system FROM member WHERE kind='listing' ORDER BY path"):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            out.append((path, system))
    root = estate_root(conn)
    for folder in folders:
        if os.path.isfile(folder):
            key = os.path.normcase(os.path.abspath(folder))
            if key not in seen:
                seen.add(key)
                out.append((folder, system_of(folder, root)))
            continue
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                if f.startswith("."):
                    continue
                p = os.path.join(dirpath, f)
                key = os.path.normcase(os.path.abspath(p))
                if key not in seen:
                    seen.add(key)
                    out.append((p, system_of(p, root)))
    return out


def originals(conn: sqlite3.Connection) -> Dict[str, List[Tuple[str, Optional[str]]]]:
    """{program name: [(path, system)]} - the programs in the index, to line an expanded text up with."""
    out: Dict[str, List[Tuple[str, Optional[str]]]] = defaultdict(list)
    for name, path, system in conn.execute("SELECT name, path, system FROM member WHERE kind='cobol'"):
        out[name.upper()].append((path, system))
    return out


def pick_original(name: str, path: str, system: Optional[str], index: Dict[str, List[Tuple[str, Optional[str]]]]) -> Optional[str]:
    cands = [(p, s) for p, s in index.get(name.upper(), []) if os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.path.abspath(path))]
    if not cands:
        return None
    same = [p for p, s in cands if system and s == system]
    return (same or [p for p, _s in cands])[0]


# --------------------------------------------------------------------------
# choosing and checking
# --------------------------------------------------------------------------

def copybook_check(records: Sequence[str]) -> Tuple[str, str]:
    """('data' | 'procedure' | 'comments' | '', why) - what the block is, or
    why it is nothing a copybook could be, in numbers and masked lines."""
    score = shaped(records)
    if score < SHAPE_OK:
        bad = next((r for r in records if r.strip() and unshaped(r)), "")
        return "", (f"shape check {score:.0%} (needs {SHAPE_OK:.0%}): first failing record `{masked(bad)[:40]}` - "
                    f"{unshaped(bad)}")
    text = "\n".join(records)
    try:
        lines, fixed = reader.read_cobol_lines(text)
        logical = reader.join_cobol_continuations(lines)
        roots, _w = copybook.parse_data_division(logical)
        fields = [f for f in copybook.flatten(roots) if f.name != copybook.SYNTHETIC_ROOT]
    except Exception as e:                                             # noqa: BLE001 - a block that breaks the parser is not a copybook
        return "", f"the copybook parser failed: {type(e).__name__}"
    if fields:
        return "data", f"{len(fields)} data item(s)"
    code = [r[7:72] for r in records if len(r) > 7 and not _is_comment(r) and r[7:72].strip()]
    joined = "\n".join(code)
    if not code:
        return ("comments", "comment lines only") if any(_is_comment(r) for r in records) else ("", "no code, no comments")
    if _VERB.search(expand._mask_literals(joined)) or any(_PARAGRAPH.match(c) for c in code):
        return "procedure", "procedure code"
    first = next((c for c in code), "")
    return "", (f"the parser found no data items and no verbs in {len(code)} code line(s) (read as "
                f"{'fixed' if fixed else 'free'} format); first code line masked `{masked(first)[:50]}`")


def looks_like_copybook(records: Sequence[str]) -> str:
    """'data', 'procedure', 'comments' or '' (nothing a copybook could be)."""
    return copybook_check(records)[0]


def resolve_replacing(region: Region) -> None:
    """A listing may print a REPLACING'd copy as the library text or as the
    replaced text: applying the clause forward tells which. Library text:
    the copy is as good as any other."""
    if not region.replacing:
        return
    m = re.search(r"REPLACING\s+(.*)", region.statement, re.I | re.S)
    pairs = expand.parse_replacing(m.group(1)) if m else []
    if not pairs:
        return
    before = [r[7:72] for r in region.records if len(r) > 7 and not _is_comment(r)]
    after = [expand.apply_replacing(c, pairs) for c in before]
    if after != before:
        region.replacing = False                                       # the listing shows the library text


def choose(regions: Sequence[Region]) -> Tuple[Optional[Region], str, str]:
    """One region per copybook: without REPLACING first, then the most common
    text, then the longest. Returns (region, how it was chosen, why not)."""
    if not regions:
        return None, "", ""
    for r in regions:
        resolve_replacing(r)
    plain = [r for r in regions if not r.replacing] or list(regions)
    groups: Dict[str, List[Region]] = defaultdict(list)
    for r in plain:
        groups[r.key()].append(r)
    ranked = sorted(groups.values(), key=lambda g: (-len(g), -len(g[0].records)))
    best = ranked[0]
    pick = next((r for r in best if r.trusted and not r.suspect), best[0])
    if not pick.trusted or pick.suspect:
        if len(best) < 2:
            return None, "", (pick.suspect or "needs a second program to agree") + f" ({pick.fmt}, {pick.source_name()})"
    if len(groups) == 1:
        how = (f"seen identical in {len(best)} program(s)" if len(best) > 1 else "seen in one program")
    else:
        how = (f"{len(groups)} different texts across {len(plain)} program(s); the most common taken "
               f"({len(best)} program(s) agree)")
    if pick.replacing:
        how += "; every copy was under a REPLACING clause and the text is post-replacement - other programs' clauses may not apply"
    if pick.end_guessed:
        how += "; the end of the block was guessed, confirmed by a second program"
    if pick.nested:
        how += f"; nested COPY {', '.join(sorted(set(pick.nested)))} kept inline"
    if pick.fmt.endswith("lined up with the program"):
        how += "; lined up with the program in the index"
    return pick, how, ""


def _source_name(self) -> str:
    return os.path.splitext(os.path.basename(self.source))[0]


Region.source_name = _source_name                                      # type: ignore[attr-defined]


def render(name: str, region: Region, how: str) -> str:
    stamp = time.strftime("%Y-%m-%d")
    head = [
        f"      * RECOVERED by atlas.recover on {stamp} from the expanded text of",
        f"      * {region.source_name()} ({region.fmt}); {how}.",
        "      * The copybook as that program saw it, not the library copy: check offsets",
        "      * against the real copybook before relying on them; fetch the library when you can.",
    ]
    return "\n".join(head + [r.rstrip() for r in region.records]) + "\n"


def shape_of(records: Sequence[str]) -> str:
    """What a rejected block looked like, in numbers and a masked first line
    (letters A, digits 9): nothing from the estate in it."""
    comments = sum(1 for r in records if _is_comment(r))
    blank = sum(1 for r in records if not r.strip())
    code = [r for r in records if r.strip() and not _is_comment(r)]
    first = re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", code[0][:72].rstrip())) if code else ""
    return (f"{len(records)} lines: {comments} comment, {blank} blank, {len(code)} code; shape {shaped(records):.0%} "
            f"source-like" + (f"; first code line looks like `{first}`" if first else ""))


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def _load_marker(out_dir: str) -> Dict[str, dict]:
    try:
        with open(os.path.join(out_dir, MARKER), encoding="utf-8") as fh:
            return json.load(fh).get("copybooks", {})
    except (OSError, ValueError):
        return {}


def _save_marker(out_dir: str, entries: Dict[str, dict]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, MARKER + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"tool": "atlas.recover", "copybooks": entries}, fh, indent=1)
    os.replace(tmp, os.path.join(out_dir, MARKER))


def _on_disk(out_dir: str) -> Dict[str, int]:
    try:
        return {os.path.splitext(f)[0].upper(): os.path.getsize(os.path.join(out_dir, f))
                for f in os.listdir(out_dir) if f.lower().endswith(".cpy")}
    except OSError:
        return {}


def _mark_programs(db: str, names: Sequence[str]) -> int:
    """Programs that copied a removed copybook are parsed again on the next
    build (a vanished member forces nothing by itself)."""
    if not names:
        return 0
    conn = sqlite3.connect(db)
    try:
        n = 0
        for name in names:
            n += conn.execute("UPDATE member SET parse_status='pending', parse_error=NULL WHERE id IN "
                              "(SELECT member_id FROM copy_use WHERE UPPER(copybook)=?)", (name.upper(),)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def run(db: str, folders: Sequence[str] = (), out_dir: Optional[str] = None, dry_run: bool = False,
        refresh: bool = False, everything: bool = False, log=print, report: Optional[str] = REPORT) -> Dict[str, object]:
    conn = sqlite3.connect(db)
    try:
        root = estate_root(conn)
        if root and not os.path.isabs(root):
            # the build was run as `estate` from the toolkit's folder, so every path in the index is
            # relative to that folder: from the same folder they all resolve
            if os.path.isdir(root):
                log(f"the index recorded the estate as `{root}`: found at {os.path.abspath(root)}")
                root = os.path.abspath(root)
            else:
                raise SystemExit(f"the index recorded the estate as a relative path ({root}) and there is no such folder "
                                 "here: run this from the folder the build ran in (the one holding atlas.db and that "
                                 "folder), or build once with the full path")
        if out_dir is None:
            if not root:
                raise SystemExit("the index has no estate root recorded: give --out")
            out_dir = shared_out(root)
        missing = missing_copybooks(conn)
        sources = listing_sources(conn, folders)
        index = originals(conn)
        needing = needing_programs(conn, missing)
        roots_out = [out_dir] + ([os.path.join(root, d, FOLDER) for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
                                 if root and os.path.isdir(root) else [])
        real = real_copies(conn, roots_out)
    finally:
        conn.close()
    # the recovered folders first: what is there, and what the estate now holds for real
    entries: Dict[str, Dict[str, dict]] = {d: _load_marker(d) for d in roots_out}
    removed: List[str] = []
    for d, ents in entries.items():
        disk = _on_disk(d)
        for name in sorted(set(ents) | set(disk)):
            if name in real:
                if not dry_run:
                    try:
                        os.remove(os.path.join(d, name + ".cpy"))
                    except OSError:
                        pass
                removed.append(name)
                ents.pop(name, None)
            elif name in ents and disk.get(name, 0) == 0:
                ents.pop(name, None)                                    # a file that never got written whole
    if removed:
        log(f"  {len(removed)} recovered copybook(s) {'would be removed' if dry_run else 'removed'} - the estate now holds "
            "the real member: " + ", ".join(sorted(set(removed))[:8]) + (" ..." if len(set(removed)) > 8 else ""))
        if not dry_run:
            n = _mark_programs(db, sorted(set(removed)))
            log(f"  {n} program(s) that had expanded them are marked for the next build")
            for d, ents in entries.items():
                if os.path.isdir(d):
                    _save_marker(d, ents)
    log(f"missing copybooks in the index: {len(missing):,}, used in {sum(missing.values()):,} places (a place = one program "
        "copying one of them)")
    if not missing and not everything:
        log("nothing to recover: every copybook the programs copy is in the index")
        return {"missing": 0, "sources": len(sources), "written": 0, "rejected": 0, "not_found": 0, "removed": len(removed),
                "kept": 0, "formats": {}, "out": out_dir, "unconfirmed": 0, "per_system": 0}
    if not sources:
        log("expanded texts to read: 0 - the index holds no compiler listings and no --from folder was given")
        log('next: run again as  python -m atlas.recover --db atlas.db --from "FOLDER"  with the folder that holds the '
            "programs' compiler listings or expanded source")
        return {"missing": len(missing), "sources": 0, "written": 0, "rejected": 0, "not_found": len(missing), "removed": 0,
                "kept": 0, "formats": {}, "out": out_dir, "unconfirmed": 0}
    found_all = len(sources)
    if not everything:
        # only the expanded text of a program that copies a missing copybook can hold it; a text
        # whose name the index does not know at all is read too, since nothing says it cannot
        sources = [(p, s) for p, s in sources
                   if os.path.splitext(os.path.basename(p))[0].upper() in needing
                   or os.path.splitext(os.path.basename(p))[0].upper() not in index]
    log(f"expanded texts to read: {len(sources):,} of {found_all:,} found "
        f"({'compiler listings the index holds' if not folders else 'listings + the folders given'})"
        + ("" if everything else f" - only the {len(needing):,} programs that copy a missing copybook, and texts the "
                                  "index cannot place"))
    if not sources:
        log("none of the expanded texts belongs to a program that copies a missing copybook: are these the "
            "listings of the programs the coverage report calls partial?")
        return {"missing": len(missing), "sources": 0, "written": 0, "rejected": 0, "not_found": len(missing), "removed": len(removed),
                "kept": 0, "formats": {}, "out": out_dir, "unconfirmed": 0, "per_system": 0}
    wanted = set(missing) if not everything else set()
    formats: Counter = Counter()
    by_name: Dict[str, List[Region]] = defaultdict(list)
    unattached = 0
    lined_up = 0
    look_untied: List[Tuple[str, int, int, str]] = []                   # (listing, untied line, program line before, its shape)
    columns: Counter = Counter()                                         # where the source starts, per listing
    t0 = last = time.time()
    for k, (path, system) in enumerate(sources, 1):
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
        stem = os.path.splitext(os.path.basename(path))[0]
        orig_path = pick_original(stem, path, system, index)
        original: Optional[List[str]] = None
        if orig_path:
            try:
                original = split_lines(reader.load(orig_path)[0])
            except OSError:
                original = None
        fmt, regions, stats = extract(text, path, wanted or set(), original, system)
        formats[fmt] += 1
        unattached += stats.get("flagged lines with no COPY before them", 0)
        if stats.get("source column"):
            columns[(stats["source column"], bool(stats.get("ruler line")))] += 1
        if stats.get("untied") and len(look_untied) < 2:
            first = stats["untied"][0]                                  # type: ignore[index]
            look_untied.append((path, first[0], first[1], first[2]))
        if "lined up" in fmt:
            lined_up += 1
        for r in regions:
            if everything or r.name in missing:
                by_name[r.name].append(r)
    log("formats seen: " + ", ".join(f"{f} in {n:,}" for f, n in formats.most_common()))
    if formats.get("unreadable"):
        log(f"  {formats['unreadable']:,} could not be read from disk - is the estate where the build saw it?")
    if columns:
        log("  source column: " + ", ".join(f"{col}{' (ruler)' if ruler else ' (guessed)'} in {n:,}"
                                             for (col, ruler), n in columns.most_common(4)))
    if unattached:
        log(f"  {unattached:,} copied line(s) could not be tied to a COPY statement - the report's 'Please look' "
            "section names a listing and the line to open")

    written: List[Tuple[str, str, str]] = []                            # (name, folder, how)
    rejected: List[Tuple[str, str]] = []
    unconfirmed: List[Tuple[str, str]] = []
    kept = 0
    have_now = {n for ents in entries.values() for n in ents}
    for name in sorted(by_name):
        if name in real and not everything:
            continue
        if name in have_now and not refresh:
            kept += 1
            continue
        # one copy for the whole estate, or one per system when the systems disagree
        by_sys: Dict[str, List[Region]] = defaultdict(list)
        for r in by_name[name]:
            by_sys[r.system or "SHARED"].append(r)
        winners: Dict[str, Tuple[Optional[Region], str, str]] = {s: choose(rs) for s, rs in by_sys.items()}
        texts = {w[0].key() for w in winners.values() if w[0] is not None}
        targets: List[Tuple[str, Region, str]] = []
        if len(texts) <= 1:
            pick, how, why = choose(by_name[name])
            if pick is None:
                unconfirmed.append((name, why))
                continue
            targets.append((out_dir, pick, how))
        else:
            for s, (pick, how, why) in winners.items():
                if pick is None:
                    unconfirmed.append((f"{name} ({s})", why))
                    continue
                folder = os.path.join(root, s, FOLDER) if root and s != "SHARED" and os.path.isdir(os.path.join(root, s)) else out_dir
                targets.append((folder, pick, how + f"; the text differs between systems - this is {s}'s"))
        for folder, pick, how in targets:
            shape = looks_like_copybook(pick.records)
            where = f" [{pick.source_name()} line {pick.line}]" if pick.line else f" [{pick.source_name()}]"
            if not shape:
                rejected.append((name, "not a copybook the parser can read: " + copybook_check(pick.records)[1] + "; "
                                 + shape_of(pick.records) + where))
                continue
            body = render(name, pick, how)
            kind, _why = classify.classify(os.path.join(folder, name + ".cpy"), body[:8192])
            if kind not in ("copybook", "cobol"):
                rejected.append((name, f"the build would file this as {kind}: " + shape_of(pick.records) + where))
                continue
            written.append((name, folder, how + f"; {shape} copybook, {len(pick.records)} lines"))
            if not dry_run:
                try:
                    os.makedirs(folder, exist_ok=True)
                    tmp = os.path.join(folder, name + ".cpy.tmp")
                    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(body)
                    os.replace(tmp, os.path.join(folder, name + ".cpy"))
                except OSError as e:
                    rejected.append((name, f"could not be written: {str(e)[:120]}"))
                    written.pop()
                    continue
                entries.setdefault(folder, {})[name] = {"from": pick.source_name(), "format": pick.fmt, "how": how,
                                                        "written": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                        "lines": len(pick.records)}
    if not dry_run:
        for d, ents in entries.items():
            if ents or os.path.isdir(d):
                _save_marker(d, ents)
    seen_names = set(by_name) | have_now
    not_found = sorted(n for n in missing if n not in seen_names)
    no_marks = sum(n for f, n in formats.items() if "no copy marks" in f or "not flagged" in f)

    lines = [f"# Recovered copybooks - {time.strftime('%Y-%m-%d %H:%M')}\n",
             f"\n- missing in the index: {len(missing)}; expanded texts read: {len(sources)}; formats: "
             + ", ".join(f"{f} in {n}" for f, n in formats.most_common()),
             f"\n- written: {len(written)}" + (" (dry run: nothing written)" if dry_run else "")
             + f"; already there: {kept}; unconfirmed: {len(unconfirmed)}; rejected: {len(rejected)}; removed (real member arrived): {len(removed)}",
             f"\n- not in any expanded text: {len(not_found)}\n"]
    if written:
        lines.append("\n## Written\n\n| copybook | programs copying it | folder | how |\n|---|---|---|---|\n")
        lines += [f"| {n} | {missing.get(n, 0)} | {os.path.relpath(f, root) if root else f} | {how} |\n" for n, f, how in written]
    if unconfirmed:
        lines.append("\n## Seen, not written - needs a second program to agree\n\n| copybook | why |\n|---|---|\n")
        lines += [f"| {n} | {why} |\n" for n, why in unconfirmed]
    if rejected:
        lines.append("\n## Rejected\n\n| copybook | why |\n|---|---|\n")
        lines += [f"| {n} | {why} |\n" for n, why in rejected]
    if not_found:
        lines.append("\n## Not in any expanded text (fetch these libraries)\n\n| copybook | programs copying it |\n|---|---|\n")
        lines += [f"| {n} | {missing[n]} |\n" for n in not_found]
    if look_untied or rejected:
        lines.append("\n## Please look\n\nOpen the listing named below in VS Code and go to the line (Ctrl+G). "
                     "Answer in words and numbers only - nothing from the file needs to be copied.\n")
    for path, at, before, shape in look_untied:
        lines.append(f"\n### Copied lines with no COPY statement found before them\n\n- listing: `{path}`\n"
                     f"- line {at}: the first copied line (a C after its line number) that nothing claimed\n"
                     f"- line {before}: the last program line before it; its shape (letters A, digits 9): `{shape}`\n"
                     "- Questions: (1) is there a COPY statement on line " + str(before) + " or just above it? (2) at what "
                     "column does the source record start on line " + str(at) + " (the first digit of its sequence number), "
                     "and at what column does it start on the line with IDENTIFICATION DIVISION? (3) what exactly sits "
                     "between the 6-digit line number and the record on line " + str(at) + " - the C and spaces, or "
                     "something more?\n")
    for name, why in rejected[:2]:
        lines.append(f"\n### Rejected block {name}\n\n- {why}\n- Questions: open the listing at that line: (1) at what "
                     "column does the copied record start there, against the column on the IDENTIFICATION DIVISION line? "
                     "(2) is the first copied line a data item (a level number and a name), a comment, or something else?\n")
    if report:
        os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
        with open(report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(lines))
    agree = sum(1 for _n, _f, how in written if "identical in" in how or "confirmed by a second" in how)
    differ = sum(1 for _n, _f, how in written if "different texts" in how)
    per_system = sum(1 for _n, _f, how in written if "differs between systems" in how)
    if not written and kept:
        log(f"nothing new to write: {kept:,} of {len(missing):,} missing copybooks were recovered on an earlier run"
            + (f"; {len(not_found):,} in no expanded text" if not_found else ""))
        log("next: run your usual build command if you have not since - the programs that copy them re-expand by themselves")
    else:
        log(f"recovered: {len(written):,} of {len(missing):,} missing copybooks"
            + (" (dry run - nothing written)" if dry_run else f" -> {out_dir}")
            + f"; {agree:,} confirmed by 2+ programs, {differ:,} differ between programs (most common taken)"
            + (f", {per_system:,} written per system" if per_system else "")
            + f", {len(unconfirmed):,} unconfirmed, {len(rejected):,} rejected, {len(not_found):,} in no expanded text"
            + (f"; {lined_up:,} text(s) read by lining up with the program" if lined_up else "")
            + (f"; {kept:,} already recovered earlier" if kept else ""))
    if no_marks and not_found:
        log(f"  {no_marks:,} expanded text(s) show the COPY statements but not the copied lines and could not be lined up "
            "with a program in the index: listings compiled with the copybook text, or the programs themselves, would close more")
    if report:
        log(f"every name: {report}")
    if written and not dry_run:
        log("next: run your usual build command - the programs that copy them re-expand by themselves")
    return {"missing": len(missing), "sources": len(sources), "written": len(written), "rejected": len(rejected),
            "not_found": len(not_found), "removed": len(removed), "kept": kept, "formats": dict(formats),
            "out": out_dir, "unconfirmed": len(unconfirmed), "per_system": per_system}


def trace(db: str, name: str, folders: Sequence[str] = (), log=print) -> int:
    """What the tool sees in ONE expanded text, as line numbers and counts -
    nothing from the estate - so a reader of the file can say where it goes
    wrong (LESSONS 172)."""
    conn = sqlite3.connect(db)
    try:
        root = estate_root(conn)
        if root and not os.path.isabs(root) and os.path.isdir(root):
            root = os.path.abspath(root)
        sources = listing_sources(conn, folders)
        index = originals(conn)
        missing = missing_copybooks(conn)
        needing = needing_programs(conn, missing)
    finally:
        conn.close()
    want = name.upper()
    hits = [(p, s) for p, s in sources if os.path.splitext(os.path.basename(p))[0].upper() == want]
    if not hits:
        log(f"no expanded text named {want} among the {len(sources):,} found (listings in the index + the --from folders)")
        return 2
    for path, system in hits:
        log(f"== {path}")
        log(f"   program in the index: {'yes' if want in index else 'no'}; copies a missing copybook: "
            f"{'yes' if want in needing else 'no'}; system: {system or '-'}")
        try:
            text, _data, enc = reader.load(path)
        except OSError as e:
            log(f"   cannot read: {e}")
            continue
        lines = split_lines(text)
        log(f"   {len(lines):,} lines, decoded as {enc}, {os.path.getsize(path):,} bytes")
        head = "\n".join(lines[:400])
        log(f"   listing banner in the first 400 lines: {'yes' if _LISTING_HEAD.search(head) else 'no'}; "
            f"ruler in the first 400 lines: {'yes' if _RULER.search(head) else 'no'}; "
            f"numbered lines in the first 2,000: {sum(1 for ln in lines[:2000] if _LISTING_SHAPE.match(ln)):,}")
        ruler_line = next((i for i, ln in enumerate(lines, 1) if _RULER.search(ln)), 0)
        off = listing_offset(lines)
        log(f"   ruler: {'line ' + str(ruler_line) if ruler_line else 'none'}; source column: "
            f"{off + 1 if off is not None else 'not found'}")
        if off is None:
            continue
        recs, _off = listing_records(lines)
        numbered_all = sum(1 for ln in lines if _LISTING_LINE.match(ln))
        first_l = recs[0][2] if recs else 0
        last_l = recs[-1][2] if recs else 0
        stop = next((i for i, ln in enumerate(lines[last_l:], last_l + 1) if _section_heading(ln)), 0) if recs else 0
        log(f"   numbered lines in the file: {numbered_all:,}; taken as source: {len(recs):,} (file lines {first_l}-{last_l})"
            + (f"; reading stopped at line {stop}: `{masked(lines[stop - 1])[:60]}`" if stop else "; read to the end of the file"))
        flagged = [(n, f) for f, r, n in recs if f.replace('*', '')]
        copies = [n for f, r, n in recs if not f.replace('*', '') and not _is_comment(r) and copy_in(r[7:72] if len(r) > 7 else "")]
        flags = Counter(f.replace('*', '').upper() for f, r, n in recs if f.replace('*', ''))
        log(f"   COPY statements found: {len(copies):,}" + (f" (first at file lines {', '.join(str(n) for n in copies[:5])})" if copies else "")
            + f"; lines with a mark after the line number: {len(flagged):,}"
            + (f" (marks: {', '.join(f'{k!r} x{v:,}' for k, v in flags.most_common(4))}; first at line {flagged[0][0]})" if flagged else ""))
        sample = next((r for f, r, n in recs if not _is_comment(r) and r.strip()), "")
        log(f"   first source record, masked: `{masked(sample)[:72]}` (columns 1-6 `{masked(sample[:6])}`, column 7 `{sample[6:7] or ' '}`)")
        if flagged:
            frec = next(r for f, r, n in recs if f.replace('*', ''))
            log(f"   first marked record, masked: `{masked(frec)[:72]}`")
        original = None
        orig_path = pick_original(want, path, system, index)
        if orig_path:
            try:
                original = split_lines(reader.load(orig_path)[0])
            except OSError:
                original = None
        fmt, regions, stats = extract(text, path, set(missing), original, system)
        log(f"   result: {fmt}; blocks: {len(regions)}"
            + (" - " + ", ".join(f"{r.name} ({len(r.records)} lines, from file line {r.line})" if r.line else f"{r.name} ({len(r.records)} lines)"
                                 for r in regions[:6]) if regions else ""))
        for r in regions[:6]:
            shape, why = copybook_check(r.records)
            log(f"   block {r.name}: {'missing in the index' if r.name in missing else 'the index has it'}; parses as "
                f"{shape or 'nothing a copybook could be'} ({why}); "
                f"{'trusted' if r.trusted and not r.suspect else 'needs a second program: ' + (r.suspect or 'end guessed')}")
            for k, rec in enumerate(r.records[:3], 1):
                log(f"      record {k} masked: `{masked(rec)[:72]}`")
        notes = {k: v for k, v in stats.items() if k not in ("untied",) and not isinstance(v, list)}
        if notes:
            log("   notes: " + ", ".join(f"{k} {v}" for k, v in notes.items()))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild the copybooks the estate lacks from the expanded text of the "
                                             "programs that copy them (compiler listings, expanded source).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--from", dest="folders", action="append", default=[], metavar="FOLDER",
                    help="a folder (or file) of expanded programs to read, besides the compiler listings the index "
                         "holds; repeatable")
    ap.add_argument("--out", metavar="FOLDER", help=f"where the recovered copybooks go (default: <estate>\\SHARED\\{FOLDER}, "
                                                    f"or <estate>\\{FOLDER}; a system's own copy goes under <estate>\\SYSTEM\\{FOLDER})")
    ap.add_argument("--dry-run", action="store_true", help="say what would be written; write nothing")
    ap.add_argument("--refresh", action="store_true", help="write again the copybooks recovered on an earlier run")
    ap.add_argument("--all", action="store_true", help="recover every copybook seen, not only the missing ones")
    ap.add_argument("--trace", metavar="PROGRAM", help="show, as line numbers and counts, what the tool sees in one "
                                                       "program's expanded text - nothing from the estate is printed")
    a = ap.parse_args(argv)
    say = lambda s: print(s, flush=True)                                  # noqa: E731
    if not os.path.isfile(a.db):
        say(f"not found: {a.db} - run this from the folder holding the index, or give --db")
        return 2
    folders = [os.path.normpath(f.strip()) for f in a.folders if f.strip()]
    for f in folders:
        if not os.path.exists(f):
            say(f"not found: {f} - type the folder as Explorer shows it")
            return 2
    out = os.path.normpath(a.out.strip()) if a.out and a.out.strip() else None
    if a.trace:
        return trace(a.db, a.trace.strip(), folders, log=say)
    try:
        run(a.db, folders, out, a.dry_run, a.refresh, a.all, log=say)
    except SystemExit as e:
        say(str(e))
        return 2
    except KeyboardInterrupt:
        say("stopped by Ctrl+C - copybooks written whole are kept; run the same command again to continue")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
