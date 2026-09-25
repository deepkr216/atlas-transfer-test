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

A copybook the index HOLDS under another kind is not recovered but re-filed:
a member the classifier typed asm, listing or mfs by a line of its text (a
field or paragraph named START-..., a comment naming MODULE MAP, a line
whose first word is MSG - weak signatures checked before the level numbers
and the folder name; ROADMAP re-parse item 22) becomes a copybook in the
index, its programs are marked, and the next build expands it; the report
says which member the folder name decided instead (rename the folder) and
which it could not re-file, and why.

Every missing name is also looked for ON DISK before any listing is read -
the estate root walked once - and the report's first table says per name
what the disk holds: a file with that name that is not in the index (arrived
after the last build: build again; or there at the last build and skipped:
atlas-problems.txt), a member the build filed 'empty' because its text sits
in columns 1-7 (a bare number: the sequence area and the indicator column,
where the reader sees no code - ROADMAP re-parse item 23), the nearest
names in the copybook folders when no file carries the name (an extension
or a suffix in the stem, another spelling), or nothing near (fetch the
library, or it is gone from the host). The misfiled scan reads the index
only; 25 of his 27 missing copybooks had no member row at all while the
files sat in the COPYLIB folders (LESSONS 192).

The listing also settles a second question. After the source, Enterprise
COBOL prints one row per copybook: the member, the DD name and the LIBRARY
DATASET the compiler read it from. Where a copybook's name exists in several
libraries with different content the build could only choose one copy (its
'ambiguous_copybook' row says which and how); this tool reads the listing of
every such program too, stores the rows (listing_copy_source, each row with
whether the listing is the compile of the program as indexed) and checks
each choice. A name is not a fact about content, so the listing's library
is looked up in the index and its copy's text compared before anything is
called wrong: CONFIRMED (the listing names the library the build's copy came
from, or a held library whose copy has the same text - compiled against
staging, promoted to production unchanged), NOT HELD (a library the index
does not hold, or holds without that member: the copy used stands), OLDER
(the texts differ but the listing is an older compile's: not a wrong fact),
CONTRADICTED (a current listing names a held copy with different text - the
wrong fact, named in the report with the library the listing says) or
UNKNOWN. The build does not read the table yet (ROADMAP re-parse item 19);
nothing in the fact tables changes.

The same rows are the FETCH LIST: per library dataset named by the listings
this tool reads (those of the programs that copy a missing copybook, one the
index holds only as a recovered copy, or one chosen among several - not
every listing in the estate), which copybooks came from it (the ones to
fetch for, the ones chosen among several, the rest), how many programs'
listings name it, and whether the index already holds it (the `library`
table, or a folder named after the dataset). A copybook the build has read
as a recovered copy is no longer missing, but its library stays on the list,
the copybook shown as a recovered copy, until the real member arrives. The
report tables it, datasets with copybooks to fetch for first, and
work\fetch-list.txt holds the not-fetched ones one per line - datasets only,
never a member name - to paste into the UI's Bulk add or give to zowe. Once
a library is fetched and the build has run, its copybooks resolve and the
recovered copies of them go on the next run.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
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
FETCH_LIST = "fetch-list.txt"                                       # next to the report: the datasets to fetch, one per line
FETCH_NAMES = 12                                                    # copybook names shown per dataset in the report's table
RESOLVER_KINDS = ("copybook", "cobol", "sql", "unknown")          # what the build's resolver accepts
MAX_SOURCE_BYTES = 64 * 1024 * 1024
SHAPE_OK = 0.98                                                  # records that must look like 80-column source

# the name may be a literal (COPY 'NAME' - LESSONS 173): groups are (quote, name); the quote, if any, must close
_COPY_KW = re.compile(r"(?<![A-Z0-9\-])(?:COPY|\+\+INCLUDE|-INC)\s+(['\"]?)([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\1(?![A-Z0-9\-])", re.I)
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
RULER_MIN_COL = 9          # a listing prints its ruler after the line number; an editor's COLS line kept in a source starts in column 1-8
_RULER_HEAD = re.compile(r"\bLine\s*I[Dd]\b|\bLINE\b", re.I)   # the ruler line's own heading: LineID / LineId / LINE


def _ruler_at(ln: str) -> "Optional[re.Match[str]]":
    """The listing's own ruler on this line - after the line-number columns
    and under its own heading (LineID  PL SL  ----+-*A-1-B...). Never the
    editor's column ruler a program or copybook keeps as a comment, anywhere
    on the line: it made a plain source pass for a listing, only its numbered
    lines were read, and a copybook lost its other lines."""
    m = _RULER.search(ln)
    if not m or m.start() < RULER_MIN_COL or not _RULER_HEAD.search(ln[:m.start()]):
        return None
    return m
_NAME = re.compile(r"^[A-Z0-9@#$][A-Z0-9@#$\-_]{0,7}$")
# marker comments: groups are (quote, name) - a commented-out COPY 'NAME'. is a marker too
_START_MARK = re.compile(r"^\s*\*?\s*(?:\+\+INCLUDE|-INC|BEGIN(?:NING)?\s+(?:OF\s+)?COPY(?:BOOK)?|COPY(?:BOOK)?)\s+"
                         r"(['\"]?)([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\1(?![A-Z0-9@#$\-_])", re.I)
_END_MARK = re.compile(r"^\s*\*?\s*END(?:\s+OF)?\s+(?:COPY(?:BOOK)?|INCLUDE)\b(?:\s+(['\"]?)([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\1)?", re.I)
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
    m = _SQL_INCLUDE.search(masked)
    if m:
        return m.group(1).upper(), code[m.start():]
    for m in _COPY_KW.finditer(code):                                  # on the text itself: COPY 'NAME' is a literal
        if masked[m.start():m.start() + 4].upper() != code[m.start():m.start() + 4].upper():
            continue                                                   # the keyword sits inside a literal
        if not m.group(1) and masked[m.start(2):m.end(2)].upper() != m.group(2).upper():
            continue                                                   # a bare "name" that is the inside of a literal
        return m.group(2).upper(), code[m.start():]
    return None


def _is_comment(rec: str) -> bool:
    return (len(rec) > 6 and rec[6:7] in ("*", "/")) or rec.lstrip().startswith("*>")


def _copy_closed(stmt: str) -> bool:
    """Whether the COPY statement gathered so far has reached its period. A
    period inside a literal or inside pseudo-text (`REPLACING ==WS-REC.==
    BY ==WS-REC-2.==`) ends nothing, and an open `==` keeps the statement
    open on to the next line - his shop writes `BY ==XX-999-XXXXX-` and the
    rest of the pseudo-text on the line after (LESSONS 190). Read as closed
    at the first period, the COPY was forgotten at the next line and every
    copied line after it was 'not tied to a COPY statement'."""
    masked = expand._mask_literals(stmt)
    if masked.count("==") % 2:
        return False
    outside = re.sub(r"==.*?==", " ", masked, flags=re.S)
    return re.search(r"\.(?:\s|$)", outside) is not None


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
        m = _ruler_at(ln)
        if m:
            return m.start()
    base: Optional[int] = None
    start = 0
    number_end = 0
    for i, ln in enumerate(lines):
        m = _LISTING_LINE.match(ln)
        if not m:
            continue
        rest = ln[m.end():].upper()
        k = max(rest.find("IDENTIFICATION DIVISION"), rest.find("PROCEDURE DIVISION"))
        if k >= 0:
            base = m.end() + k - 7                                         # area A starts at source column 8
            start = i
            number_end = m.end()
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
        if off <= number_end:
            continue                                                   # the record starts after the line number: a guess of
                                                                       # column 1 is a plain source with sequence numbers
        score = shaped([ln[off:off + 80] for ln in sample])
        if score > best_score:
            best, best_score = off, score
    return best if best is not None and best_score >= FALLBACK_OK else None


# --------------------------------------------------------------------------
# 1b. an older compiler's listing: five-digit line numbers, no ruler
# --------------------------------------------------------------------------

# OS/VS COBOL and DOS/VS COBOL number every line they read - the program's and every copied one - with a
# FIVE-digit count (03008), print the 80-column record after it, and head each page with the page number,
# the program, the time and the date: "1  17  PROGNAME  14.31.19  FEB  5,1992" (LESSONS 176). They share
# nothing else with the Enterprise layout, so they are recognised on their own terms, and strictly: a
# plain expanded source must never pass for one, and the record's column must be PROVEN, never guessed -
# a copybook read one column off turns its comments into code and still passes every later check, and a
# missing copybook is better than that.
_OLD_LINE = re.compile(r"^[ 01\-+]?\s{0,4}(\d{5})([^\s\d]*)(?=\s|$)")
_OLD_PAGE = re.compile(r"^[1 ]?\s*\d{1,5}\s+\S{1,8}\s+\d\d\.\d\d\.\d\d\s+[A-Z]{3}\s+\d{1,2},\s?\d{2,4}\s*$", re.I)
_OLD_BANNER = re.compile(r"^[1 ]?\s?PP\s+(?:NO\.\s*)?57\d\d-", re.I)
_GAP_FLAG = re.compile(r"^[A-Z+*]{1,3}$", re.I)
# a division or section header, as a whole word
_HEADER_WORD = re.compile(r"(?<![A-Z0-9\-])(?:(?:IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION|"
                          r"(?:CONFIGURATION|INPUT-OUTPUT|FILE|WORKING-STORAGE|LOCAL-STORAGE|LINKAGE)\s+SECTION)"
                          r"(?![A-Z0-9\-])", re.I)
OLD_MIN_LINES = 20        # numbered lines a run needs before it can be a program's listing
OLD_STEP_OK = 0.8         # share of successive numbered lines whose number goes up by exactly one
OLD_GAP_MAX = 20          # columns between the end of the line number and column 1 of the record, at most
OLD_SAMPLE = 400          # numbered lines the column tests look at
OLD_COVER = 0.8           # without a copy mark, page header or banner, the run must hold this share of the text


def _old_flag(ln: str, m: "re.Match[str]", off: int) -> str:
    """The copy mark of an older listing line: attached to the number
    (03008C) or printed apart from it, alone in the gap before the record."""
    flag = m.group(2) or ""
    if not flag and off > m.end() and ln[off - 1:off] == " ":         # a mark stands apart from the record too: a letter
        gap = [t for t in ln[m.end():off].split() if not t.isdigit()]  # touching column 1 is the record's own (CHG001);
                                                                       # a number in the gap is never a mark
        if len(gap) == 1 and _GAP_FLAG.match(gap[0]):
            flag = gap[0]
    return flag


def _is_mark(flag: str) -> bool:
    return flag.replace("*", "").upper() in ("C", "+")


def _seq_hits(recs: Sequence[str]) -> int:
    """Records with six digits in columns 1-6 and an indicator in column 7:
    the sequence numbers line up at the true column only - a record read one
    column either way has a blank or a letter among its first six."""
    return sum(1 for r in recs if len(r) > 6 and r[:6].isdigit() and r[6] in INDICATORS)


def _comment_hits(recs: Sequence[str]) -> int:
    """Comment lines with text: the indicator in column 7, no asterisk in
    column 6 or 8. Border lines (*****) count nowhere, whichever column they
    start in; a comment's text counts at the true column only."""
    return sum(1 for r in recs if len(r) > 7 and r[6] in "*/" and r[5] != "*" and r[7] not in "*/" and r[7:].strip())


def _header_votes(lines: Sequence[str], run: Sequence[Tuple[int, int, "re.Match[str]"]], after_number: int) -> Counter:
    """{record column: division / section headers that start column 8 there}.
    A header counts only where the record would begin after the line number
    and nothing before it on the line is a comment mark or a quote - a
    comment or a literal naming a division is no header."""
    votes: Counter = Counter()
    for i, _n, _m in run:
        ln = lines[i]
        for h in _HEADER_WORD.finditer(ln, after_number):
            off = h.start() - 7
            if ln[off + 6:h.start()].strip():
                continue                                               # column 7 must be blank: no comment, no continuation
            if any(ch in ln[after_number:h.start()] for ch in "*/'\""):
                continue
            votes[off] += 1
            break
    return votes


def _old_column(lines: Sequence[str], run: Sequence[Tuple[int, int, "re.Match[str]"]]) -> Tuple[Optional[int], str]:
    """(column of record column 1, how it was proven) or (None, why not).
    Six-digit sequence numbers decide alone; without them, the comment lines
    (the indicator must be in column 7) and the majority of the division and
    section headers must agree. Headers alone prove nothing: area A runs from
    column 8 to 11, and a program that writes all of it in column 9 agrees
    with itself one column off."""
    sample = [lines[i] for i, _n, _m in run[:OLD_SAMPLE]]
    after_number = max(m.start(1) + 5 for _i, _n, m in run[:OLD_SAMPLE])
    votes = _header_votes(lines, run, after_number)
    if not votes:
        return None, "no division or section header in the numbered lines"
    cands = [c for c in range(after_number + 1, after_number + 1 + OLD_GAP_MAX)
             if shaped([ln[c:c + 80] for ln in sample]) >= FALLBACK_OK]
    if not cands:
        return None, "no column where the records look like 80-column source"
    (hcol, hn), = votes.most_common(1)
    seq = {c: _seq_hits([ln[c:c + 80] for ln in sample]) for c in cands}
    best = max(seq.values())
    if best >= max(5, len(sample) // 5) and list(seq.values()).count(best) == 1:
        scol = max(seq, key=seq.get)
        if 0 <= hcol - scol <= 3:                                      # the headers in area A (columns 8-11) of that record
            return scol, "sequence numbers in columns 1-6"
        # six digits that do not fit the headers are some other column of the listing: let the comments decide
    header_ok = hn >= 2 and hn >= 0.6 * sum(votes.values())
    com = {c: _comment_hits([ln[c:c + 80] for ln in sample]) for c in cands}
    cbest = max(com.values())
    if cbest >= 2 and list(com.values()).count(cbest) == 1:
        ccol = max(com, key=com.get)
        if header_ok and ccol == hcol:
            return ccol, "comment lines and division headers agree"
        return None, "the comment lines and the division headers point to different columns"
    return None, "no sequence numbers in columns 1-6 and too few comment lines to prove the column"


def _older_scan(lines: Sequence[str]) -> Tuple[Optional[Dict[str, object]], str, List[Tuple[int, int]]]:
    """(layout or None, why none was taken, the file lines of the runs seen
    as an older listing whose column could not be proven). The layout: where an older compiler's own
    listing sits in the file - the file lines (0-based, inclusive) of its
    numbering run, the record column, the copy marks in it. A run is a
    stretch of five-digit numbers that go up (a line printed again under the
    same number - a page eject, an overprint - is passed over); a new run
    starts where they go back: the next listing, or the diagnostics after the
    source, which cite earlier line numbers. The run chosen is the one with
    the most copy marks, the later on a tie (the compiler prints after the
    translator and the precompiler). Without a copy mark, a page header or a
    banner in the file, the run must hold most of the text - a numbered
    stretch inside a plain source is no listing."""
    nums: List[Tuple[int, int, "re.Match[str]"]] = []
    for i, ln in enumerate(lines):
        m = _OLD_LINE.match(ln)
        if m:
            nums.append((i, int(m.group(1)), m))
    if len(nums) < OLD_MIN_LINES:
        return None, f"fewer than {OLD_MIN_LINES} lines start with a five-digit number", []
    runs: List[List[Tuple[int, int, "re.Match[str]"]]] = [[nums[0]]]
    for item in nums[1:]:
        last = runs[-1][-1][1]
        if item[1] == last:
            continue                                                   # the same line printed again
        if item[1] < last:
            runs.append([item])
        else:
            runs[-1].append(item)
    signs = any(_OLD_PAGE.match(ln) or _OLD_BANNER.match(ln) for ln in lines)
    text_lines = sum(1 for ln in lines if ln.strip())
    best = None
    reasons: List[Tuple[int, str]] = []                               # (run length, why it was not taken)
    refused: List[Tuple[int, int]] = []
    for run in runs:
        if len(run) < OLD_MIN_LINES:
            reasons.append((len(run), f"the numbering run is {len(run)} lines, fewer than {OLD_MIN_LINES}"))
            continue
        steps = sum(1 for a, b in zip(run, run[1:]) if b[1] - a[1] == 1)
        if steps < OLD_STEP_OK * (len(run) - 1):
            reasons.append((len(run), f"{steps:,} of {len(run) - 1:,} steps between the numbers are +1 - a listing counts by one"))
            continue                                                   # sequence numbers step by 10 or 100; a listing counts
        off, how = _old_column(lines, run)
        if off is None:
            reasons.append((len(run), how))
            if signs:
                refused.append((run[0][0], run[-1][0]))
            continue
        marks = sum(1 for i, _n, m in run if _is_mark(_old_flag(lines[i], m, off)))
        if not marks and not signs and len(run) < OLD_COVER * text_lines:
            reasons.append((len(run), "no copy mark, page header or banner, and the numbered lines are a small part of the text"))
            continue
        key = (marks, run[0][0])
        if best is None or key > best[0]:
            best = (key, run, off, marks, how)
    if best is None:
        return None, max(reasons)[1] if reasons else "no numbering run", refused
    _key, run, off, marks, how = best
    return ({"start": run[0][0], "end": run[-1][0], "off": off, "marks": marks, "runs": len(runs), "lines": len(run),
             "how": how}, "", [])


def older_layout(lines: Sequence[str]) -> Optional[Dict[str, object]]:
    """Where an older compiler's own listing sits in the file, or None (_older_scan)."""
    return _older_scan(lines)[0]


def older_why_not(lines: Sequence[str]) -> str:
    """For trace: why no older layout was taken - the reason of the longest numbering run."""
    return _older_scan(lines)[1]


def reading(lines: Sequence[str]) -> Dict[str, object]:
    """How a text is read - the one choice extract() and trace() both make:
    'older' (an older compiler's listing), 'current' (the Enterprise layout)
    or 'source' (an expanded source). 'unprovable': an older listing is there
    (numbers counting up, a page header or banner) but its record column
    could not be proven - its copybooks are in the text, only unreadable."""
    head = "\n".join(lines[:400])
    ruled = any(_ruler_at(ln) for ln in lines[:400])
    banner = bool(_LISTING_HEAD.search(head))
    shape = sum(1 for ln in lines[:2000] if _LISTING_SHAPE.match(ln))
    # a carriage-control 0 glued to a five-digit number (003008) reads as a six-digit line too; a current
    # listing's six-digit lines never read as five-digit ones - so six-digit lines that are not also
    # five-digit lines belong to a current listing
    n6_own = sum(1 for ln in lines if _LISTING_LINE.match(ln) and not _OLD_LINE.match(ln))
    old, why, refused = _older_scan(lines)
    if old is not None and (n6_own == 0 or int(old["lines"]) > 2 * n6_own):  # type: ignore[call-overload]
        kind = "older"
    elif ruled or banner or shape >= 5:
        kind = "current"
        marked = any(_is_mark(m.group(2) or "") for ln in lines for m in [_LISTING_LINE.match(ln)] if m)
        if not ruled and not banner and not marked and listing_offset(lines) is None:
            kind = "source"                    # only the shape of some lines said 'listing' (a change log 'CR 102345'
                                               # can), no column, no copy mark: the source it is
    else:
        kind = "source"
    return {"kind": kind, "old": old if kind == "older" else None, "why_not_older": why,
            "unprovable": refused if kind != "older" else [], "ruled": ruled, "banner": banner, "shape": shape}


def listing_records(lines: Sequence[str], layout: Optional[Dict[str, int]] = None) -> Tuple[List[Tuple[str, str, int]], Optional[int]]:
    """[(flag, 80-column record, file line number)] for every source line of
    a listing - and only the source: after the program end the listing goes
    on with the data division map and the cross-reference tables, whose
    lines carry line numbers too, some followed by a letter, and where the
    copybook names appear again (LESSONS 170). A heading that opens one of
    those sections ends the reading. With `layout` (older_layout), only
    that numbering run is read, at its own column."""
    if layout is not None:
        off = int(layout["off"])
        out_old: List[Tuple[str, str, int]] = []
        last = -1
        for i in range(int(layout["start"]), int(layout["end"]) + 1):
            ln = lines[i]
            m = _OLD_LINE.match(ln)
            if m:
                if int(m.group(1)) == last:
                    continue                                           # printed again: a page eject, an overprint
                last = int(m.group(1))
                out_old.append((_old_flag(ln, m, off), ln[off:off + 80].rstrip("\r\n"), i + 1))
        return out_old, off                                            # the run ends where the source does: the
                                                                       # diagnostics after it cite earlier numbers
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


def from_ibm_listing(lines: Sequence[str], source: str, system: Optional[str],
                     layout: Optional[Dict[str, int]] = None) -> Tuple[List[Region], Dict[str, int]]:
    """A compiler listing: each source line carries a line number; a copied
    line carries a C after it (with `layout`, an older compiler's listing)."""
    recs, off = listing_records(lines, layout)
    stats: Dict[str, int] = Counter()
    if off is None:
        return [], {"no ruler": 1}
    stats["source column"] = off + 1                                   # 1-based, for the report
    if layout is not None:
        stats["older layout"] = 1                                      # the column proven, not guessed
    ruler_line = 0 if layout is not None else next((i for i, ln in enumerate(lines, 1) if _ruler_at(ln)), 0)
    stats["ruler line"] = ruler_line
    last_plain: Tuple[int, str] = (0, "")                              # the last program line seen (line, shape)
    closer: Tuple[int, str, str] = (0, "", "")                          # the program line that closed the last block: (line, copybook, shape)
    untied: List[Tuple[int, int, str, list, str]] = []                 # (first untied line, previous program line, its shape, the lines before, why)
    recent: List[Tuple[int, str, str]] = []                             # the last numbered lines seen: (line, raw mark, masked record)
    sql_line = 0                                                       # where the open EXEC SQL statement started
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
        recent = (recent + [(lineno, flag, masked(rec)[:60])])[-4:]
        flag = flag.replace("*", "").upper()                          # ** = out of sequence, not a copy mark
        if not flag:
            if _is_comment(rec) or not code.strip():
                continue                                               # a blank or a comment without the mark closes nothing:
                                                                       # one printed inside a copybook must not split the block
            if cur is not None:
                regions.append(cur)
                closer = (lineno, cur.name, masked(rec))
                cur = None
            last_plain = (lineno, masked(rec))
            found = copy_in(code)
            if sql_open and found and not _SQL_INCLUDE.search(expand._mask_literals(code)):
                sql_open = []                                          # a COPY statement: the EXEC SQL before it never closed
            if sql_open:
                sql_open.append(code.strip())
                if "END-EXEC" in code.upper():
                    found = copy_in(" ".join(sql_open))
                    pending = (found[0], found[1], True) if found else None
                    sql_open = []
                continue
            if pending and not pending[2] and not found:               # a COPY statement continued on the next line
                whole = pending[1] + " " + code.strip()
                pending = (pending[0], whole, _copy_closed(whole))
                continue
            if found:
                pending = (found[0], found[1], _copy_closed(found[1]))
            elif _EXEC_SQL.search(expand._mask_literals(code)) and "END-EXEC" not in code.upper():
                sql_open = [code.strip()]
                sql_line = lineno
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
            if pending is None and sql_open:
                found = copy_in(" ".join(sql_open))                    # an INCLUDE whose END-EXEC comes after the copied lines
                if found:
                    pending = (found[0], found[1], True)
                    sql_open = []
            if pending is None:
                stats["flagged lines with no COPY before them"] += 1
                if not untied or untied[-1][1] != last_plain[0]:
                    if sql_open:
                        why = f"an EXEC SQL statement that opened at line {sql_line} had not reached END-EXEC"
                    elif not last_plain[0]:
                        why = "no program line was read before it"
                    else:
                        why = f"the last program line, {last_plain[0]}, holds no COPY statement the tool recognises"
                    if closer[0]:
                        why += f"; the copied block before it was closed by program line {closer[0]}, shape `{closer[2]}`"
                    untied.append((lineno, last_plain[0], last_plain[1], list(recent[:-1]), why))
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
                cur = Region(m_start.group(2), [], source, "marker comments", end_guessed=True, trusted=False,
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


UNPROVABLE = "older compiler listing, source column not provable"


def extract(text: str, source: str, wanted: Set[str], original: Optional[Sequence[str]] = None,
            system: Optional[str] = None, lines: Optional[List[str]] = None,
            how: Optional[Dict[str, object]] = None) -> Tuple[str, List[Region], Dict[str, int]]:
    """(format seen, regions, notes) for one expanded text. `lines` and `how`
    (split_lines / reading) can be given when the caller has them already, so
    a 64 MB listing is not split and read twice."""
    lines = split_lines(text) if lines is None else lines
    how = reading(lines) if how is None else how
    records: List[str] = [ln.rstrip("\r\n") for ln in lines]
    fmt_base = "expanded source"
    carried: Dict[str, int] = {}                                       # the listing's own notes, whatever is found later
    if how["kind"] == "older":
        old = how["old"]
        regions, stats = from_ibm_listing(lines, source, system, layout=old)          # type: ignore[arg-type]
        if regions:
            return "older compiler listing", regions, stats
        carried = stats
        records = [r for _f, r, _n in listing_records(lines, old)[0]]                 # type: ignore[arg-type]
        fmt_base = ("older compiler listing, copied lines not tied to a COPY statement" if stats.get("flagged lines")
                    else "older compiler listing, copied lines not flagged")
    elif how["kind"] == "current":
        regions, stats = from_ibm_listing(lines, source, system)
        if regions:
            return "compiler listing", regions, stats
        recs, off = listing_records(lines)
        if off is None:
            return "compiler listing without a readable source column", [], stats
        carried = stats
        records = [r for _f, r, _n in recs]
        fmt_base = ("compiler listing, copied lines not tied to a COPY statement" if stats.get("flagged lines")
                    else "compiler listing, copied lines not flagged")
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
    if how["unprovable"]:
        # an older listing whose record column could not be proven: its copybooks ARE in the text, and the
        # report must say so rather than send the reader to fetch libraries (LESSONS 176)
        named = sorted({c[0] for a, b in how["unprovable"] for ln in lines[a:b + 1]                # type: ignore[union-attr]
                        for c in [copy_in(ln)] if c and (not wanted or c[0] in wanted)})
        return (UNPROVABLE, [], {"why": how["why_not_older"], "named": named})               # type: ignore[dict-item]
    has_copy = any(not _is_comment(r) and copy_in(r[7:72] if len(r) > 7 else "") for r in records)
    if not has_copy:
        return "no COPY statements", [], carried
    return (fmt_base + ", no copy marks" + ("" if original else " and the program is not in the index to line up with")), [], carried


# --------------------------------------------------------------------------
# the listing's copybook-source table: which library each copybook came from
# --------------------------------------------------------------------------
# After the source, an Enterprise COBOL listing prints one row per copybook
# it read: the member name, the DD name it was found through (SYSLIB), the
# LIBRARY DATASET the compiler read it from, then a number and dates. That
# table is the compiler's own statement of which copy went into the load
# module - where the estate holds a copybook's name in several libraries with
# different content, the build's resolver can only GUESS (an
# 'ambiguous_copybook' row says which copy it took and how); the listing's
# row is the truth the guess is checked against. Rows are recognised by
# SHAPE alone (a heading may read COPY/BASIS or name the columns, and is not
# relied on): a member name, a DD name of 1-8 characters, a dataset name of
# dotted qualifiers, and nothing after it but numbers, dates and times. A
# source line is numbered and a row is not, so a first token that is a line
# number is never a row.

_ROW_NAME = r"[A-Z@#$][A-Z0-9@#$]{0,7}"                               # a member or DD name never starts with a digit
_ROW_QUAL = r"[A-Z0-9@#$][A-Z0-9@#$\-]{0,7}"
_COPY_ROW = re.compile(rf"^\s*(?P<name>{_ROW_NAME})\s+(?P<dd>{_ROW_NAME})\s+"
                       rf"(?P<dsn>{_ROW_QUAL}(?:\.{_ROW_QUAL})+)(?:\((?P<mem>{_ROW_NAME})\))?"
                       r"(?P<rest>(?:\s+\S+)*)\s*$", re.I)
_ROW_REST = re.compile(r"^(?:\d+|\d{4}[/.\-]\d\d[/.\-]\d\d|\d\d/\d\d/\d{2,4}|\d\d[:.]\d\d[:.]\d\d)$")
_DSN_SHAPE = re.compile(rf"^{_ROW_QUAL}(?:\.{_ROW_QUAL})+$", re.I)
DSN_MAX = 44


def _copy_row(ln: str) -> Optional[Tuple[str, str, str, str]]:
    m = _COPY_ROW.match(ln)
    if m is None:
        return None
    name, dd, dsn, rest = m.group("name"), m.group("dd"), m.group("dsn"), (m.group("rest") or "").split()
    if name.isdigit() or _LISTING_LINE.match(ln) or _OLD_LINE.match(ln):
        return None                                                    # a numbered source line, whatever follows the number
    if len(dsn) > DSN_MAX or not all(_ROW_REST.match(t) for t in rest):
        return None
    return name.upper(), dd.upper(), dsn.upper(), " ".join(rest)


def copy_sources(lines: Sequence[str]) -> List[Tuple[str, str, str, str]]:
    """[(copybook, DD name, dataset, the numbers and dates after it)] - every
    row of the listing's copybook-source table, in order; [] when the listing
    has no such table (an older compiler's). A line may start with a
    carriage-control character glued to the name (a `0` for double spacing)."""
    out: List[Tuple[str, str, str, str]] = []
    for ln in lines:
        row = _copy_row(ln)
        if row is None and ln[:1] in "01-+" and ln[1:2].strip():
            row = _copy_row(ln[1:])
        if row is not None:
            out.append(row)
    return out


# The table the rows go to: one row per (program, copybook) the listing named,
# replaced per program each time that program's listing is read. Its own
# small table - no fact table changes (the build's use of it is ROADMAP
# re-parse item 19). `current`: 1 when the listing's source is the program
# as indexed, 0 when it is an older compile's (`matched`: how much of the
# source still matches, in percent), NULL when nothing dated it - the program
# is not in the index, or the row was stored before this tool dated
# listings; the two columns are added to a table written before them.
COPY_SOURCE_TABLE = ("CREATE TABLE IF NOT EXISTS listing_copy_source (program TEXT NOT NULL, copybook TEXT NOT NULL, "
                     "ddname TEXT, dataset TEXT, listing TEXT, seen TEXT, current INTEGER, matched INTEGER)")
COPY_SOURCE_ADDED = ("current INTEGER", "matched INTEGER")            # the columns a table written before them lacks
_PICK = re.compile(r"(\d+) copies of (\S+) with different content; used (.+?) \(([^()]*)\)\s*$")
CopySource = Tuple[str, str, str, str, Optional[bool], Optional[int]]   # (copybook, ddname, dataset, listing path,
                                                                        #  listing current?, its source match in percent)


def listing_is_current(records: Sequence[Tuple[str, str, int]], original: Optional[Sequence[str]]
                       ) -> Tuple[Optional[bool], Optional[int]]:
    """(current?, match in percent): whether a listing is the compile of the
    program as the index holds it. The listing's own program lines (the
    unflagged records - a copied line carries a mark) against the member's,
    both read the way the parser reads a member (reader.read_cobol_lines:
    comment lines and blank lines dropped, columns 8-72 with trailing blanks
    stripped, the member's own code margin when its records are not 80
    columns). Equal sequences: current. Otherwise older, with how much of
    the source still matches (difflib's ratio, never shown as 100). No
    original member (`original` None): (None, None) - nothing to date the
    listing against; the same when the listing shows no program line."""
    if original is None:
        return None, None
    plain = [rec for flag, rec, _n in records if not flag.replace("*", "")]
    if not plain:
        return None, None
    orig_text = "\n".join(original)
    orig_recs = reader._split_records(orig_text, b"", "")
    cut = (reader.detect_code_end(orig_recs) if reader.looks_fixed_format(orig_recs) else 72) - 7

    def code_lines(text: str) -> List[str]:
        lines, _fixed = reader.read_cobol_lines(text)
        return [ln.code[:cut].rstrip() for ln in lines if not ln.is_comment and ln.code.strip()]

    a = code_lines(orig_text)
    b = code_lines("\n".join(plain))
    if a == b:
        return True, 100
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return False, min(99, int(ratio * 100))


def ensure_copy_source_columns(conn: sqlite3.Connection) -> List[str]:
    """Create the table when it is not there; add the columns a table
    written before them lacks. Returns the columns added."""
    conn.execute(COPY_SOURCE_TABLE)
    have = {str(r[1]) for r in conn.execute("PRAGMA table_info(listing_copy_source)")}
    added = []
    for col in COPY_SOURCE_ADDED:
        if col.split()[0] not in have:
            conn.execute(f"ALTER TABLE listing_copy_source ADD COLUMN {col}")
            added.append(col.split()[0])
    return added


def _fkey(folder: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(folder)))


def library_datasets(conn: sqlite3.Connection) -> Dict[str, str]:
    """{folder (normalised): dataset} - the `library` table, which the build
    loads from the fetcher's `.atlas-library.json` in each fetched folder."""
    out: Dict[str, str] = {}
    try:
        rows = conn.execute("SELECT folder, dataset FROM library WHERE folder IS NOT NULL AND dataset IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return out
    for folder, dataset in rows:
        out[_fkey(folder)] = str(dataset).strip().upper()
    return out


def dataset_of(path: str, libs: Dict[str, str]) -> Optional[str]:
    """The library dataset a member on disk was fetched from: the `library`
    table first (the fetcher's marker ties the folder to its dataset), else
    the folder's own name when it is shaped like a dataset name - the fetcher
    names each folder after its dataset (fetch.new_source: `local` is the
    dataset, under the department's folder). None when neither says."""
    folder = os.path.dirname(path)
    dsn = libs.get(_fkey(folder))
    if dsn:
        return dsn
    base = os.path.basename(folder).upper()
    return base if "." in base and len(base) <= DSN_MAX and _DSN_SHAPE.match(base) else None


def chosen_picks(conn: sqlite3.Connection) -> List[Tuple[str, str, str, str, int]]:
    """[(program, copybook, the path the build used, how, member id)] - every
    'ambiguous_copybook' row in the index, its note read; a note the pattern
    does not read is skipped (it is still in the index, unchanged)."""
    out: List[Tuple[str, str, str, str, int]] = []
    try:
        rows = conn.execute("SELECT m.name, m.id, u.detail FROM unresolved u JOIN member m ON m.id = u.member_id "
                            "WHERE u.kind = 'ambiguous_copybook' ORDER BY m.name, u.id").fetchall()
    except sqlite3.OperationalError:
        return out
    for name, mid, detail in rows:
        m = _PICK.search(detail or "")
        if m:
            out.append((str(name).upper(), m.group(2).upper(), m.group(3), m.group(4), int(mid)))
    return out


def stored_copy_sources(conn: sqlite3.Connection) -> Dict[str, List[CopySource]]:
    """{program: [(copybook, ddname, dataset, listing, current, matched)]} as
    stored; a program whose listing was read and had no table is there with
    an empty list. A table written before the two dating columns reads with
    both None (the query side never alters the table)."""
    out: Dict[str, List[CopySource]] = {}
    try:
        rows = conn.execute("SELECT program, copybook, ddname, dataset, listing, current, matched FROM listing_copy_source "
                            "ORDER BY program, rowid").fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(*r, None, None) for r in conn.execute("SELECT program, copybook, ddname, dataset, listing FROM "
                                                           "listing_copy_source ORDER BY program, rowid").fetchall()]
        except sqlite3.OperationalError:
            return out
    for program, copybook, dd, dsn, listing, current, matched in rows:
        lst = out.setdefault(str(program).upper(), [])
        if copybook:
            lst.append((str(copybook).upper(), str(dd or ""), str(dsn or "").upper(), str(listing or ""),
                        None if current is None else bool(current), None if matched is None else int(matched)))
    return out


def store_copy_sources(conn: sqlite3.Connection, per_program: Dict[str, List[CopySource]]) -> None:
    """Replace each named program's rows. A program read with no table keeps
    one row with an empty copybook, so the next check says 'no table' rather
    than 'no listing read'; nothing of any other program is touched."""
    ensure_copy_source_columns(conn)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for program, rows in per_program.items():
        conn.execute("DELETE FROM listing_copy_source WHERE program = ?", (program,))
        if rows:
            conn.executemany("INSERT INTO listing_copy_source(program, copybook, ddname, dataset, listing, seen, current, matched) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             [(program, c, d, ds, lst, now, None if cur is None else int(cur), matched)
                              for c, d, ds, lst, cur, matched in rows])
        else:
            conn.execute("INSERT INTO listing_copy_source(program, copybook, ddname, dataset, listing, seen, current, matched) "
                         "VALUES(?,?,?,?,?,?,?,?)", (program, "", None, None, None, now, None, None))
    conn.commit()


def has_copy_sources(conn: sqlite3.Connection) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM listing_copy_source LIMIT 1").fetchone())
    except sqlite3.OperationalError:
        return False


def _tail(path: str, n: int = 3) -> str:
    return "/".join(path.replace("\\", "/").split("/")[-n:])


VERDICTS = ("CONFIRMED", "CONTRADICTED", "OLDER", "NOT HELD", "UNKNOWN")   # the order the counts are given in
NOT_YET_DATED = ("not yet dated: run recover again with --from FOLDER (the rows were stored before this tool dated "
                 "listings, or the listing's source could not be read)")
NOT_IN_INDEX_TO_DATE = "the program itself is not in the index to date the listing against"
NOT_HELD_WHY = ("the build could only choose among the copies it has: the copy it used stands; the listing names the "
                "library the program was compiled against (a staging library, or one gone from the host) - if it still "
                "exists and matters, the fetch list names it")


def _dating(rows: Sequence[CopySource]) -> Tuple[Optional[bool], Optional[int]]:
    """(current?, matched) over the rows naming one dataset: current when any
    listing naming it is, else older with the best match, else not dated."""
    if any(r[4] for r in rows):
        return True, 100
    older = [int(r[5] or 0) for r in rows if r[4] is not None and not r[4]]
    if older:
        return False, max(older)
    return None, None


def dated_cell(current: Optional[bool], matched: Optional[int]) -> str:
    """The report's 'listing current?' cell."""
    if current is True:
        return "yes"
    if current is False:
        return f"older ({matched or 0}%)"
    return "not dated"


def check_choices(conn: sqlite3.Connection, sources: Optional[Dict[str, List[CopySource]]] = None) -> List[Dict[str, object]]:
    """One verdict per copybook choice the build made, against the program's
    listing - a name is not a fact about content, so the listing's library
    is looked up in the index and its copy's TEXT compared (member.norm_sha:
    columns 8-72, comments dropped) before anything is called wrong:

    CONFIRMED - the listing names the dataset the chosen member came from;
      or the index holds the listing's copy and its text is the same as the
      chosen member's (a copybook compiled against a staging library and
      promoted to production unchanged: 'promoted');
    NOT HELD - the listing names a dataset the index does not hold at all,
      or holds without that member: the build could only choose among the
      copies it has, and the copy it used stands;
    OLDER - the index holds the listing's copy, the texts differ, and the
      listing is an older compile's (its source is not the program as
      indexed): the program may use another copy now - not a wrong fact;
    CONTRADICTED - the index holds the listing's copy, the texts differ, and
      the listing is current (or cannot be dated: the program is not in the
      index, or the row was stored before this tool dated listings): the
      real wrong fact;
    UNKNOWN - no listing read for the program, no table in it, no row for
      that copybook, or the chosen member's dataset is not known.

    Where listings name several other datasets the most serious finding
    decides (contradicted, then older, then not held, then promoted).
    `sources`: the rows to check against, else the stored table."""
    libs = library_datasets(conn)
    srcs = stored_copy_sources(conn) if sources is None else sources
    holders: Dict[str, str] = {}                                       # dataset -> a folder the index holds for it
    for folder, dsn in libs.items():
        holders.setdefault(dsn, folder)
    try:
        indexed = {str(n).upper() for (n,) in conn.execute("SELECT name FROM member WHERE kind = 'cobol'")}
    except sqlite3.OperationalError:
        indexed = set()
    copies: Dict[str, List[Tuple[str, Optional[str]]]] = {}           # copybook -> [(path, norm_sha)] the index holds

    def copies_of(name: str) -> List[Tuple[str, Optional[str]]]:
        if name not in copies:
            copies[name] = [(str(p), s) for p, s in conn.execute(
                f"SELECT path, norm_sha FROM member WHERE UPPER(name) = ? AND kind IN ({','.join('?' * len(RESOLVER_KINDS))}) "
                "ORDER BY id", (name, *RESOLVER_KINDS))]
        return copies[name]

    out: List[Dict[str, object]] = []
    for program, copybook, used, how, mid in chosen_picks(conn):
        used_dsn = dataset_of(used, libs)
        rows = [r for r in srcs.get(program, []) if r[0] == copybook]
        said = sorted({r[2] for r in rows if r[2]})
        v: Dict[str, object] = {"program": program, "copybook": copybook, "used": used, "used_dataset": used_dsn,
                                "how": how, "member_id": mid, "listing_datasets": said,
                                "ddnames": sorted({r[1] for r in rows if r[1]}),
                                "listings": sorted({r[3] for r in rows if r[3]}), "index_has": "",
                                "current": None, "matched": None, "dated": "", "named": ""}
        if program not in srcs:
            v.update(verdict="UNKNOWN", why="no listing of the program was read")
        elif not srcs[program]:
            v.update(verdict="UNKNOWN", why="the program's listing has no copybook-source table")
        elif not said:
            v.update(verdict="UNKNOWN", why=f"the listing's table has no row for {copybook}")
        elif used_dsn is None:
            v.update(verdict="UNKNOWN", why="the chosen member's library dataset is not known (no `library` row for its "
                                            "folder, and the folder is not named after a dataset)")
        elif said == [used_dsn]:
            cur, matched = _dating(rows)
            v.update(verdict="CONFIRMED", why="", current=cur, matched=matched, named=used_dsn)
        elif next((s for p, s in copies_of(copybook) if _fkey(p) == _fkey(used)), None) is None:
            v.update(verdict="UNKNOWN", why="the copy the build used is no longer a member of the index (its path is gone), "
                                            "so its text cannot be compared with the listing's copy")
        else:
            held = copies_of(copybook)
            used_sha = next(s for p, s in held if _fkey(p) == _fkey(used))
            findings: List[Tuple[str, str, Optional[str]]] = []         # (what, dataset, path or folder)
            for d in [d for d in said if d != used_dsn]:
                at = [(p, s) for p, s in held if dataset_of(p, libs) == d]
                if at:
                    p, s = at[0]
                    findings.append(("same" if s is not None and s == used_sha else "differs", d, p))
                elif d in holders:
                    findings.append(("no member", d, holders[d]))
                else:
                    findings.append(("not held", d, None))
            differs = [f for f in findings if f[0] == "differs"]
            unheld = [f for f in findings if f[0] in ("no member", "not held")]
            if differs:
                _what, d, p = differs[0]
                cur, matched = _dating([r for r in rows if r[2] == d])
                have = f"the index holds that copy at {_tail(str(p))} - the build chose the other"
                if cur is False:
                    v.update(verdict="OLDER", index_has=have, current=cur, matched=matched, named=d,
                             why=f"an older listing names {d}, whose text differs from the copy the build used; the "
                                 "program as indexed may use another copy now; not counted as a wrong fact")
                elif cur is True:
                    v.update(verdict="CONTRADICTED", index_has=have, current=cur, matched=matched, named=d,
                             why=f"a current listing names {d}, whose text differs from the copy the build used")
                else:
                    dated = NOT_YET_DATED if program in indexed else NOT_IN_INDEX_TO_DATE
                    v.update(verdict="CONTRADICTED", index_has=have, current=None, matched=None, named=d, dated=dated,
                             why=f"the listing names {d}, whose text differs from the copy the build used; {dated}")
            elif unheld:
                what, d, folder = unheld[0]
                cur, matched = _dating([r for r in rows if r[2] == d])
                have = (f"the index holds that library ({_tail(str(folder))}) but no {copybook} in it" if what == "no member"
                        else "not a library the index holds")
                v.update(verdict="NOT HELD", why=NOT_HELD_WHY, index_has=have, current=cur, matched=matched, named=d)
            else:
                _what, d, p = findings[0]
                cur, matched = _dating([r for r in rows if r[2] == d])
                v.update(verdict="CONFIRMED", why=f"same text as the copy the listing names in {d} - promoted",
                         index_has=f"the index holds that copy at {_tail(str(p))}", current=cur, matched=matched, named=d)
        out.append(v)
    return out


def choice_counts(checks: Sequence[Dict[str, object]]) -> Tuple[int, int, int, int, int]:
    """(confirmed, contradicted, older, not held, unknown) - the VERDICTS order."""
    c = Counter(str(v["verdict"]) for v in checks)
    return c["CONFIRMED"], c["CONTRADICTED"], c["OLDER"], c["NOT HELD"], c["UNKNOWN"]


def choice_words(checks: Sequence[Dict[str, object]]) -> str:
    """The five counts with their words, as every summary line prints them."""
    a, b, o, h, u = choice_counts(checks)
    return (f"{a} confirmed, {b} contradicted by a current listing, {o} named by an older listing, {h} name a library "
            f"the index does not hold, {u} unknown")


def choice_line(checks: Sequence[Dict[str, object]]) -> str:
    return "copybook choices checked against the listings: " + choice_words(checks)


def choice_report(checks: Sequence[Dict[str, object]], root: Optional[str]) -> List[str]:
    """The report section: the counts, then every choice with something to
    say - contradicted by a current listing (each a wrong fact in the index)
    first, then named by an older listing, then naming a library the index
    does not hold, then confirmed through a same-text copy (promoted) - with
    whether the listing is current and why the verdict is what it is."""
    a, b, o, h, u = choice_counts(checks)
    whys = Counter(str(v["why"]) for v in checks if v["verdict"] == "UNKNOWN")
    lines = ["\n## Copybook choices, checked against the listings\n\n"
             "Where a copybook's name exists in several libraries with different content the build chose one copy "
             "('ambiguous_copybook' rows: COPY..OF, then the program's own system in its declared order, then the "
             "authoritative copy, then the same folder, then the first found). The compiler listing names the library "
             "the compiler read each copybook from - that is the record the choice is checked against. A name is not a "
             "fact about content: the listing's library is looked up in the index and its copy's text compared with the "
             "copy the build used (a copybook compiled against a staging library and promoted unchanged has the same "
             "text: confirmed, 'promoted'); a library the index does not hold says nothing against the copy used; a "
             "listing whose source is not the program as indexed is an older compile's, and what it names is not counted "
             "as a wrong fact. Only a current listing naming a held copy with different text contradicts the choice. The "
             "build itself does not read this table yet (ROADMAP re-parse item 19); a contradicted choice is a wrong fact "
             "until then, and `program NAME` shows the listing's library under its notes.\n\n"
             f"- {len(checks)} choice{'s' if len(checks) != 1 else ''} checked: {choice_words(checks)}"
             + ("" if not whys else " (" + "; ".join(f"{n}: {w}" for w, n in sorted(whys.items(), key=lambda kv: (-kv[1], kv[0])))
                                           + ")") + "\n"]
    if not b:
        lines.append("\n_no choice contradicted by a current listing_\n")
    order = {"CONTRADICTED": 0, "OLDER": 1, "NOT HELD": 2, "CONFIRMED": 3}   # a confirmed choice is shown only when promoted
    shown = [v for v in checks if v["verdict"] in order and (v["verdict"] != "CONFIRMED" or v["why"])]
    if not shown:
        return lines
    shown.sort(key=lambda v: (order[str(v["verdict"])], str(v["program"]), str(v["copybook"])))
    lines.append("\n| program | copybook | the index used | the listing says | listing current? | verdict | why |\n"
                 "|---|---|---|---|---|---|---|\n")
    for v in shown:
        used = f"{v['used_dataset']} ({_tail(str(v['used']))}; {v['how']})"
        says = (", ".join(str(d) for d in v["listing_datasets"]) + (f" ({', '.join(str(d) for d in v['ddnames'])})" if v["ddnames"] else "")
                + (f" - {v['index_has']}" if v["index_has"] else ""))
        dated = dated_cell(v["current"], v["matched"])                 # type: ignore[arg-type]
        lines.append(f"| {v['program']} | {v['copybook']} | {used} | {says} | {dated} | {v['verdict']} | {v['why']} |\n")
    return lines


# --------------------------------------------------------------------------
# the index side
# --------------------------------------------------------------------------

def missing_copybooks(conn: sqlite3.Connection) -> Dict[str, int]:
    """{copybook name: programs copying it} for the copybooks no member of an
    accepted kind carries (a program copying a name that is only its own
    name counts as missing too; a COPYBOOK copying its own name does not -
    it is the member, and the expander skips that COPY as recursive)."""
    kinds = ",".join("?" * len(RESOLVER_KINDS))
    out: Dict[str, int] = {}
    for name, n in conn.execute(
            f"""SELECT UPPER(c.copybook), COUNT(DISTINCT c.member_id) FROM copy_use c
                WHERE c.resolved_member_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM member m WHERE UPPER(m.name)=UPPER(c.copybook)
                                  AND m.kind IN ({kinds}) AND (m.id != c.member_id OR m.kind = 'copybook'))
                GROUP BY 1""", RESOLVER_KINDS):
        if name and name not in expand._SYSTEM_INCLUDES:
            out[name] = int(n)
    return out


def members_named(conn: sqlite3.Connection, name: str, exclude_ids: Sequence[int] = ()
                  ) -> Tuple[List[Tuple[int, str, str, str]], List[Tuple[int, str, str, str]]]:
    """Every member carrying a copybook's name, as (id, kind, folder, path),
    split into the ones the build's resolver would expand (RESOLVER_KINDS)
    and the ones it never looks at (proc, ctlcard, doc, jcl, listing ...).
    A procedure copybook - paragraphs and statements, no level numbers, no
    DIVISION header - has no content signature, so the FOLDER NAME decides
    its kind: PROCS makes it a proc, CNTL a control card, a dataset-named
    folder with no hint 'unknown'. `exclude_ids`: the copiers themselves (a
    program copying its own name is not its own copybook)."""
    accepted: List[Tuple[int, str, str, str]] = []
    other: List[Tuple[int, str, str, str]] = []
    skip = set(exclude_ids)
    for mid, kind, folder, path in conn.execute("SELECT id, kind, library, path FROM member WHERE UPPER(name)=? "
                                                "ORDER BY kind, library, path", (name.upper(),)):
        if mid in skip:
            continue
        (accepted if kind in RESOLVER_KINDS else other).append((int(mid), kind, folder or "?", path))
    return accepted, other


_SKIPPED_RE = re.compile(r"COPY (\S+) skipped - (recursive|nesting deeper than \d+)")


def skipped_copies(conn: sqlite3.Connection, member_id: Optional[int] = None) -> Dict[Tuple[int, str], str]:
    """{(program member id, COPYBOOK): why} for every COPY the expander
    SKIPPED instead of looking up - a copybook copying itself (or a cycle:
    'recursive'), or one nested deeper than expand.MAX_DEPTH ('nesting
    deeper than 12'). expand.py writes the program's copy_use row with no
    resolved_member_id for a system include, a skipped COPY and a COPY NOT
    FOUND alike; the system includes are excluded by name, and a skipped
    COPY is told apart here by the program's own 'expand' note, the one
    place the build wrote the reason. The member exists (a recursive one
    was resolved a level up) and the program was parsed after it, so
    nothing arrived and nothing is missing: read as 'not found', such a row
    told him to run recover for a member the program had found, marked the
    program on every run, and the build left the row NULL - it never
    settled (LESSONS 185). A name the same program also reports NOT FOUND
    at another COPY stays not found. `member_id`: one program only."""
    out: Dict[Tuple[int, str], str] = {}
    sql = "SELECT member_id, detail FROM unresolved WHERE kind = 'expand' AND detail LIKE '%skipped - %'"
    args: Tuple[object, ...] = ()
    if member_id is not None:
        sql += " AND member_id = ?"
        args = (member_id,)
    for mid, detail in conn.execute(sql, args):
        m = _SKIPPED_RE.search(detail or "")
        if not m or mid is None:
            continue
        name = m.group(1).upper()
        not_found = conn.execute("SELECT 1 FROM unresolved WHERE member_id = ? AND kind = 'expand' AND detail LIKE ? LIMIT 1",
                                 (mid, f"%COPY {name} NOT FOUND%")).fetchone()
        if not not_found:
            out[(int(mid), name)] = m.group(2)
    return out


def unlinked_ok_programs(conn: sqlite3.Connection, member_id: Optional[int] = None) -> Dict[int, Tuple[str, List[str]]]:
    """{program member id: (program name, [COPYBOOK, ...])} for every program
    marked `ok` that has a COPY row no member resolves any more - not a
    system include, not a COPY the expander skipped. The build sets a
    program's copy_use row to NULL when the member it had expanded leaves
    the index (`_forget_member`: the file changed on disk, or went), and
    parses the program again only when the member's NEW text is filed as
    copybook or cobol; a re-filed copybook re-fetched with new text is filed
    by its weak signature again (asm / listing / mfs), so its programs keep
    'ok' with the fields of the earlier read and a NULL row nothing explains
    (LESSONS 188). `program` says so beside 'parse: ok' and `coverage`
    counts them apart from the partial members; a run of this tool re-files
    the member and marks them by name, and the build makes them whole."""
    out: Dict[int, Tuple[str, List[str]]] = {}
    sql = ("SELECT c.member_id, m.name, c.copybook FROM copy_use c JOIN member m ON m.id = c.member_id "
           "WHERE c.resolved_member_id IS NULL AND m.kind = 'cobol' AND m.parse_status = 'ok' "
           "AND UPPER(c.copybook) NOT IN ('SQLCA', 'SQLDA')")
    args: Tuple[object, ...] = ()
    if member_id is not None:
        sql += " AND c.member_id = ?"
        args = (member_id,)
    rows = conn.execute(sql + " ORDER BY c.member_id, c.line", args).fetchall()
    if not rows:
        return out
    skipped = skipped_copies(conn, member_id)
    for mid, name, book in rows:
        key = (int(mid), (book or "").upper())
        if key in skipped:
            continue
        entry = out.setdefault(int(mid), (str(name), []))
        if key[1] not in entry[1]:
            entry[1].append(key[1])
    return out


def arrived_copybooks(conn: sqlite3.Connection) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    """The COPY statements the build could not resolve whose copybook's name
    the index DOES hold now, in two lists, one entry per copybook name:

      arrived  - a member of a kind the resolver expands exists (copybook,
                 cobol, sql, unknown), so the programs were parsed BEFORE it
                 arrived and nothing parsed them again: the build re-parses
                 the copiers of a NEW member only when it is filed as
                 copybook or cobol (ROADMAP re-parse item 21), so a member
                 typed 'unknown' by its folder forces nothing. These
                 programs are marked for the next build.
      misfiled - the only members with that name are of a kind the resolver
                 never looks at (proc, ctlcard, doc, jcl, listing ...): a
                 re-parse would find nothing; the folder name decided the
                 kind (ROADMAP re-parse item 20) and must change, or the
                 library's kind be declared - or a LINE OF THE TEXT decided
                 it (asm / listing / mfs by a weak signature: ROADMAP item
                 22), which no folder change touches: read_misfiled() tells
                 the two apart per member and refile_misfiled() re-files the
                 second sort in the index. Each entry carries `found`, the
                 member rows, for that.

    missing_copybooks() leaves the arrived case out (a member of a resolver
    kind carries the name), so without this scan it fell through every
    report silently while coverage still said 'COPY X NOT FOUND'; the
    misfiled case it counted as missing and, with listings present, rejected
    as 'not a copybook the parser can read' - without a word that the member
    was on disk. Each entry: copybook, members [(kind, folder)], programs
    [(member id, name, kind)], and, for `arrived`, ids to mark.

    Only a PROGRAM's copy_use row says whether a COPY was found: the build
    records a copybook member's own COPY statements with NO
    resolved_member_id (index_copybook parses it for copies only, never
    resolves them), so those rows are NULL for ever. Reading them as
    unresolved called every copybook that copies another copybook 'arrived'
    on an estate with nothing wrong, marked the COPYBOOK pending, and the
    next build re-inserted it under a new id and nulled the links of every
    program copying it (LESSONS 184). A program carries its own row for
    every nested COPY, so its rows are the whole picture - less the COPYs
    the expander skipped (recursive, nested too deep: skipped_copies()),
    whose member the program had found (LESSONS 185).

    A copybook this tool re-filed on an EARLIER run (refiled_members) whose
    programs are all marked already is neither: nothing parsed them, by
    design - the build is what they wait for. Read as 'arrived', the second
    run before the build said 'a copybook that has arrived since they were
    parsed' over the previous run's 'Re-filed as copybook' (LESSONS 187):
    arrival_scan() keeps such names in its third list, `waiting`."""
    arrived, misfiled, _waiting = arrival_scan(conn)
    return arrived, misfiled


def arrival_scan(conn: sqlite3.Connection
                 ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    """arrived_copybooks() with its third list: (arrived, misfiled, waiting)
    - `waiting` the copybooks re-filed on an earlier run whose programs are
    marked already (see arrived_copybooks)."""
    skipped = skipped_copies(conn)
    refiled = refiled_members(conn)
    by_book: Dict[str, Dict[Tuple[int, str, str], None]] = defaultdict(dict)   # copybook -> copiers (ordered set)
    status: Dict[int, str] = {}                                                # copier id -> parse_status
    for book, mid, mname, mkind, mstatus in conn.execute(
            "SELECT UPPER(c.copybook), m.id, UPPER(m.name), m.kind, m.parse_status FROM copy_use c "
            "JOIN member m ON m.id = c.member_id "
            "WHERE c.resolved_member_id IS NULL AND m.kind = 'cobol' "
            "AND EXISTS (SELECT 1 FROM member x WHERE UPPER(x.name) = UPPER(c.copybook) "
            "AND x.id != c.member_id) ORDER BY 1, 3"):
        if book and book not in expand._SYSTEM_INCLUDES and (int(mid), book) not in skipped:
            by_book[book][(int(mid), str(mname), str(mkind))] = None
            status[int(mid)] = str(mstatus or "")
    arrived: List[Dict[str, object]] = []
    misfiled: List[Dict[str, object]] = []
    waiting: List[Dict[str, object]] = []
    for book in sorted(by_book):
        copiers = list(by_book[book])
        accepted, other = members_named(conn, book, [c[0] for c in copiers])
        if accepted:
            entry = {"copybook": book, "members": [(k, f) for _i, k, f, _p in accepted], "programs": copiers,
                     "ids": [c[0] for c in copiers]}
            if refiled and all(i in refiled for i, _k, _f, _p in accepted) and all(status.get(c[0]) == "pending" for c in copiers):
                waiting.append(entry)                            # re-filed earlier, marked already: the build is next
            else:
                arrived.append(entry)
        elif other:
            misfiled.append({"copybook": book, "members": [(k, f) for _i, k, f, _p in other], "programs": copiers, "ids": [],
                             "found": other})                       # the full rows, for read_misfiled()
    return arrived, misfiled, waiting


def _kinds_folders(members: Sequence[Tuple[str, str]]) -> Tuple[str, str]:
    """('unknown', 'PROD.GC.CPYLIB') - or, for several copies, the kinds and
    the folders in the SAME order (the first kind is the first folder's),
    each once: sorted apart, 'doc, proc | downloads, PROD.CLM.PROCS' would
    not say which folder holds the proc."""
    kinds = ", ".join(dict.fromkeys(k for k, _f in members))
    folders = ", ".join(dict.fromkeys(f for _k, f in members))
    return kinds, folders


MISFILED_FIX = ("the folder name decided the kind (a copybook with no level numbers has no signature): rename the folder to "
                "end in COPYLIB, or declare the library's kind in the UI's table (the manifest kinds) and run the build")
# a member typed 'unknown' is expanded into its programs but has no parser of its own: its lines are not in
# src_fts, so `paragraph` prints empty Source lines for them and nothing can cite them - the same folder fix
# as the misfiled case, which makes it the one thing to do in both
UNKNOWN_FIX = ("rename the folder to end in COPYLIB (or declare the library's kind in the UI's table) so the copybook's "
               "own lines are indexed and citable")
MISFILED_NEXT = ("next: rename the folder(s) the report names to end in COPYLIB (or declare the library's kind in the UI's "
                 "table), then run your usual build command")


# --------------------------------------------------------------------------
# how the classifier read a misfiled member - by a line of its text, or by
# its folder - and the stand-in for ROADMAP re-parse item 22
# --------------------------------------------------------------------------
#
# classify.classify() checks the content signatures before the level numbers
# and before the folder hint, and three of them are weak: _SIG_ASM fires on
# any line whose first or second word BEGINS with START, CSECT or DSECT
# (`05 START-DATE PIC X(8).`, `PERFORM START-PARA.`), _SIG_LISTING on a
# comment saying MODULE MAP or CROSS REFERENCE TABLE, _SIG_MFS on a line
# whose first word is MSG / FMT / DEV / DFLD / MFLD. A copybook in a folder
# ending in COPYLIB is then filed asm / listing / mfs, the resolver never
# looks at it, and every program copying it says COPY X NOT FOUND - and the
# folder fix the misfiled report gave (rename the folder, declare the kind)
# changes nothing: the content decided, and the declared kind in sources.json
# only replaces 'unknown' (build.py _inventory_one). The classifier is a fact
# module, so the fix waits for the re-parse batch; meanwhile refile_misfiled()
# sets such a member's kind to copybook in the index. That is enough for the
# programs: the build keeps the stored kind of an unchanged member whose
# outcome is settled ('skipped' is), and the expander reads the copybook's
# text from disk through the resolver's member list. The member's own field
# rows stay absent until the re-parse files it as a copybook itself.

REFILED_MARK = "re-filed as copybook by atlas.recover"      # parse_error prefix of a member refile_misfiled() re-filed
WEAK_KINDS = ("asm", "listing", "mfs")                        # the three kinds a COBOL line trips by a weak signature
HEAD_BYTES = 64 * 1024                                        # read to say how the classifier read a member
REFILED_ITEM = "ROADMAP re-parse item 22"

# the shape a REAL member of each weak kind has and a COBOL copybook never has (the tighter signatures of
# ROADMAP item 22): CSECT / DSECT as the operation after a label of any length or none (HLASM labels run
# to 63 characters; a label of 1-8 let `PLLONGLABEL CSECT` and an unlabelled `CSECT` through, and their
# copiers went 'ok' with Assembler text expanded as COBOL), START alone or with a numeric or quoted
# operand (`START X'100'`; a remark may follow) or one bare symbol ending the line (never a hyphenated
# COBOL name: `START CUST-FILE KEY IS ...` is the COBOL verb), DFHEIENT, a DS / DC with a type - the
# address constants A, V, S, Q and AL2 / VL4 included (`DC A(TABLE)`), `USING *,15`, `EQU *`, `BR 14`;
# a listing's banner or its numbered source lines; an MFS macro with a label in column 1, TYPE= / POS= /
# LTH= operands, MSGEND / FMTEND
_ASM_LABEL = r"(?:[A-Z@#$_][A-Z0-9@#$_]*)?"                   # an HLASM label in column 1: any length, or none
_ASM_LABEL_REQ = r"[A-Z@#$_][A-Z0-9@#$_]*"                     # ... required: the CSECT name before START
# START: the COBOL verb (`START CUSTFILE`, its KEY IS or INVALID KEY clause on the NEXT line - his procedure
# copybook, LESSONS 189) is `START` alone on its line with the file name after it, and a COBOL line never begins
# in column 1; the Assembler statement names the control section in column 1 (`PGM      START 0`) or gives a
# self-defining term (` START X'100'`). So START counts as Assembler only with a label, or with a numeric or
# quoted operand - never unlabelled with a bare symbol, never unlabelled and alone.
_ASM_SHAPE = re.compile(
    rf"^{_ASM_LABEL}[ \t]+(?:CSECT|DSECT)(?:[ \t]|$)"                                                   # the exact word: not CSECT-NAME
    rf"|^{_ASM_LABEL_REQ}[ \t]+START(?:[ \t]+[^ \t\n].*)?[ \t]*$"
    rf"|^[ \t]+START[ \t]+(?:\d+|[XBC]'[^']*')(?:[ \t]+.*)?[ \t]*$"
    rf"|^{_ASM_LABEL}[ \t]+DFHEIENT\b"
    rf"|^{_ASM_LABEL}[ \t]+D[SC][ \t]+\d*[ABCDEFHPXZVSQ]L?\d*(?:'|\(|[ \t]|$)"
    rf"|^{_ASM_LABEL}[ \t]+(?:USING|EQU)[ \t]+\*"
    rf"|^{_ASM_LABEL}[ \t]+(?:BR|BALR|BASR)[ \t]+R?1[45]\b", re.I | re.M)
ASM_SHAPES = ("CSECT or DSECT as the operation, with a label of any length or none; START with a label in column 1, or with a "
              "numeric or quoted operand; DFHEIENT; DS / DC with a type, address constants included; USING * or EQU *; BR 14")
_MFS_SHAPE = re.compile(r"^[A-Z@#$][A-Z0-9@#$]{0,7}[ \t]+(?:MSG|FMT|DEV|DFLD|MFLD)\b"
                        r"|[ \t](?:MSG|DEV)[ \t]+TYPE=|[ \t]DFLD[ \t]+(?:POS|LTH)=|^[ \t]+(?:MSGEND|FMTEND)\b", re.I | re.M)
_JCL_LINE = re.compile(r"^//", re.M)
_WORD_AT = re.compile(r"[A-Z0-9@#$\-_]+", re.I)
# the strong signatures, by kind, for the word that fired (a member of these kinds is not a copybook)
_STRONG_SIGS = {"jcl": (classify._SIG_JOB, classify._SIG_EXEC), "proc": (classify._SIG_PROC,),
                "imsgen": (classify._SIG_IMSGEN,), "csd": (classify._SIG_CSD,),
                "dbd": (classify._SIG_DBD, classify._SIG_SEGM), "psb": (classify._SIG_PSB,), "bms": (classify._SIG_BMS,),
                "rexx": (classify._SIG_REXX,), "sql": (classify._SIG_SQL_DDL,), "cobol": (classify._SIG_COBOL, classify._SIG_PROGRAM_ID)}
_WEAK_WORDS = {"asm": "Assembler - a first or second word beginning with START, CSECT or DSECT",
               "listing": "a compiler listing - a comment naming MODULE MAP or CROSS REFERENCE TABLE",
               "mfs": "an MFS statement - a line whose first word is MSG, FMT, DEV, DFLD or MFLD"}
CONTENT_FIX = ("no folder change helps (the content decided): run `python -m atlas.recover --db atlas.db`, which re-files "
               "it as a copybook in the index, then the build")
CONTENT_FIX_DRY = ("no folder change helps (the content decided): a run without --dry-run re-files it as a copybook in the "
                   "index and marks the programs")
REFILED_NOTE = "indexed as a copybook (re-filed by atlas.recover; its own layout rows arrive with the next full re-parse)"
# a re-file lives in the member row, and the build keeps that row only while it keeps every unchanged member's
# stored kind (build.py: `not force_all`): --rebuild is one way to lose it, and a build that re-parses every member
# for its own reasons is another - the manifest changed (the UI rewrites manifest.json from the sources table on
# every build, so every library he adds or re-kinds in its table changes it - his routine of LESSONS 183) or a
# parser module changed. Such a build files the member as before and parses its programs again without it (they
# read partial, COPY NOT FOUND); nothing but a run of this tool re-files it (LESSONS 188).
REPARSE_UNDOES = ("So does any build that re-parses every member - the manifest changed (a library added or re-kinded in "
                  "the UI's table rewrites it) or a parser module changed: run this tool after such a build and it "
                  "re-files them again.")
# a member a weak signature typed that sits in a folder with no COPY hint and carries no level numbers (a procedure
# copybook he fetched by hand into a dataset-named folder - LESSONS 183): nothing says copybook, so this tool does
# not re-file it - but a COPYLIB folder would let it, so the folder IS the fix here, then a second run; the cell
# must not say 'no folder change helps' beside it (LESSONS 187)
FOLDER_LETS = "a folder ending in COPYLIB would let this tool re-file it"
FOLDER_THEN_RECOVER = ("the content decided the kind, and neither the folder name (no COPYLIB) nor the text (no level numbers) "
                       "says copybook, so this tool did not re-file it: rename the folder to end in COPYLIB (or move the "
                       "member into one), run `python -m atlas.recover --db atlas.db` again - it then re-files the member - "
                       "then the build")
NEXT_FOLDER_REFILE = ("next: rename the folder(s) the report names to end in COPYLIB (or move the member into one), run this "
                      "tool again without --dry-run - it re-files the member - then your usual build command")
# a member with no signature in a folder with no hint takes the kind declared for its library in the UI's table
# (sources.json -> the manifest kinds; build.py _inventory_one): 'the folder name decided' is false for it, and so
# is 'the file now reads as unknown ... run the build first' - the build would file it the same again
DECLARED_FIX = ("the kind declared for the library in the UI's table decided (the classifier read no signature and no folder "
                "hint): declare it copybook there - or rename the folder to end in COPYLIB - and run the build")
DECLARED_CELL = ("the library's kind in the UI's table decided it - declare it copybook there (or rename the folder to end in "
                 "COPYLIB), then build")
DECLARABLE_KINDS = ("cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs", "csd", "imsgen", "sql",
                    "listing", "doc", "sched")                    # what build.load_declared_kinds accepts from the manifest
# a member the build filed 'empty' whose text is a bare number in column 1 (two of his 27 missing copybooks, LESSONS
# 192): columns 1-6 are the sequence area and column 7 the indicator of fixed-format COBOL, so the reader sees no code
# and build.code_line_count gives 0 - 'no code lines' said nothing about where the text sits
STUB_ITEM = "ROADMAP re-parse item 23"
EMPTY_SEEN = "no code lines, nothing a program could copy"
EMPTY_WHY_NOT = "it has no code lines (comments and blanks only): nothing a program could copy"


def stub_lines(text: str, data: bytes = b"", enc: str = "utf-8") -> int:
    """How many non-blank lines the text has when EVERY one of them keeps its
    text within columns 1-7 - the sequence area and the indicator column of
    fixed-format COBOL, which the reader never reads as code: `1234567` in
    column 1 gives build.code_line_count 0 and the build files the member
    'empty'. 0 when the text has no line or any non-blank line reaches
    column 8 (a member of comments only: the old sentence stands). Records
    split as the reader splits them, tabs widened as it widens them."""
    n = 0
    for rec in reader._split_records(text, data, enc):
        rec = rec.replace("\t", "    ")
        if not rec.strip():
            continue
        if rec[7:].strip():
            return 0
        n += 1
    return n


def stub_sentence(n: int) -> str:
    """The sentence for a member whose every non-blank line sits in columns 1-7."""
    return (f"the file holds {n} line(s) whose text sits in columns 1-7 - the sequence area and the indicator column of "
            f"fixed-format COBOL - so the reader sees no code ({STUB_ITEM})")


def _signature_hit(kind: str, head: str) -> Tuple[int, str]:
    """(line, word) of the content signature that filed `head` as `kind`:
    the word that begins with START / CSECT / DSECT, the MODULE MAP comment,
    the MSG - so the report can quote the line the classifier read."""
    m = None
    if kind == "asm":
        m = classify._SIG_ASM.search(head)
    elif kind == "listing":
        m = classify._SIG_LISTING.search(head)
    elif kind == "mfs":
        m = classify._SIG_MFS.search(head)
    else:
        m = next((x for rx in _STRONG_SIGS.get(kind, ()) for x in [rx.search(head)] if x), None)
    if not m:
        return 0, ""
    line = head.count("\n", 0, m.start()) + 1
    if kind in ("asm", "mfs"):
        w = _WORD_AT.match(head, m.start(1))
        word = w.group(0) if w else m.group(1)
    else:
        word = m.group(0).strip()[:40]
    return line, word


def _file_sha(path: str, head: bytes) -> str:
    """sha256 of the file's raw bytes, as the build stores it (member.sha256):
    the head already read is the whole file when the file is shorter than
    HEAD_BYTES; otherwise the file is read again, whole."""
    if len(head) < HEAD_BYTES:
        return hashlib.sha256(head).hexdigest()
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def how_classified(path: str, stored_kind: Optional[str] = None, stored_sha: Optional[str] = None) -> Dict[str, object]:
    """How the build's classifier came to file a member: classify.classify()
    run again over the member's first 8 KB, decoded as the build decodes it,
    and its REASON read. `by` is 'content' (a signature fired: the line and
    the word are quoted in `seen`), 'folder' (the library folder name),
    'extension', 'declared' (no signature, no folder hint: the kind declared
    for the library in the UI's table - known by the file being unchanged
    since the build, `stored_sha`, while the classifier reads 'unknown'),
    'changed' (the file now reads as another kind than the index holds:
    build first), 'build' (unchanged, yet the build filed it otherwise by a
    rule of its own) or 'unreadable'. `text` is what was read (up to
    HEAD_BYTES), for the checks refile_verdict() makes."""
    out: Dict[str, object] = {"kind": stored_kind, "reason": "", "by": "unreadable", "line": 0, "word": "", "text": "",
                              "seen": "the file could not be read from disk to say what decided its kind - is the estate "
                                      "where the build saw it?"}
    try:
        with open(path, "rb") as fh:
            data = fh.read(HEAD_BYTES)
    except OSError:
        return out
    text, enc = reader.decode_bytes(data)
    head = text[:8192]                                              # as the build hands it to the classifier
    kind, reason = classify.classify(path, head)
    # the shape checks are line-anchored: a member downloaded with CR LF must not slip past a `$`
    out.update(kind=kind, reason=reason, text=text.replace("\r\n", "\n").replace("\r", "\n"))
    ext = os.path.splitext(path)[1].lower()
    if stored_kind == "empty":
        # the build files a code member with no code lines as empty, after classifying: comments and blanks only -
        # or a bare number in column 1, which the fixed-format reader takes for the sequence area and the indicator
        # (LESSONS 192): the sentence says where the text sits, and `stub` how many such lines there are
        whole = text
        if len(data) >= HEAD_BYTES:                                 # every line counts: the whole file, when it is longer
            try:
                whole = reader.load(path)[0]
            except OSError:
                whole = text
        n = stub_lines(whole, data, enc)
        out.update(kind="empty", by="content", stub=n,
                   seen=f"the classifier read it as {kind} ({reason}) and the build filed it empty: "
                        + (stub_sentence(n) if n else EMPTY_SEEN))
    elif stored_kind and kind != stored_kind:
        # the same bytes the build read, and the classifier's own answer differs from the index: the build's step
        # AFTER the classifier decided - 'unknown' becomes the kind declared for the library (_inventory_one) -
        # and a build would file it the same again; different bytes: the file changed since, and the build comes first
        try:
            same = stored_sha is not None and _file_sha(path, data) == stored_sha
        except OSError:
            same = False
        parent = os.path.basename(os.path.dirname(path))
        if same and kind == "unknown" and stored_kind in DECLARABLE_KINDS:
            out.update(kind=stored_kind, by="declared",
                       seen=f"the kind declared for library {parent} in the UI's table (sources.json, the manifest kinds) - "
                            f"the classifier itself read no signature and no folder hint ({reason})")
        elif same:
            out.update(kind=stored_kind, by="build",
                       seen=f"the classifier reads it as {kind} ({reason}) and the build filed it {stored_kind} by a rule of "
                            "its own, the file unchanged since")
        else:
            out.update(kind=stored_kind, by="changed",
                       seen=f"the file now reads as {kind} ({reason}) and the index holds {stored_kind} from an earlier read: "
                            "run the build first")
    elif reason.startswith("library folder"):
        parent = os.path.basename(os.path.dirname(path))
        tail = next((m.group(0) for rx, _k in classify.DIR_HINTS for m in [rx.search(parent)] if m), parent)
        out.update(by="folder", seen=f"the folder name ends in {tail.upper()}")
    elif reason.startswith("binary extension"):
        out.update(by="extension", seen=f"the extension {ext} is a binary document's")
    elif reason.startswith(("extension", "unrecognised member", "no signature")):
        out.update(by="extension", seen=f"the extension {ext or '(none)'} in a folder with no library hint")
    elif kind == "listing" and not classify._SIG_LISTING.search(head):
        out.update(by="extension", seen=f"the extension {ext} reads as a compiler listing")
    else:
        line, word = _signature_hit(kind, head)
        if kind in _WEAK_WORDS:
            seen = (f"line {line} `{word}` reads as {_WEAK_WORDS[kind]}, checked before the level numbers and the folder "
                    f"name; {REFILED_ITEM}")
        else:
            seen = f"line {line} `{word}`: {reason}" if word else reason
        out.update(by="content", line=line, word=word, seen=seen)
    return out


def _code_only(text: str) -> str:
    """The text without its COBOL comment lines (an asterisk or a slash in
    column 7, or a line beginning with an asterisk when nothing says fixed
    format): what a signature must fire on to say what the member IS."""
    try:
        lines, _fixed = reader.read_cobol_lines(text)
    except Exception:                                                  # noqa: BLE001 - any text: never a crash here
        return text
    return "\n".join(ln.raw for ln in lines if not ln.is_comment)     # the whole record: a JCL // or a label sits in column 1


def _signature_only_in_comments(kind: str, text: str) -> bool:
    """A strong signature (a BMS macro name, a REXX header, a DBD macro ...)
    that fires on the whole text and on none of its code lines came from a
    comment - `* BMS FIELD DFHMDF MAPPED HERE` - and says nothing about
    what the member is. A signature the classifier applied to a kind this
    table does not know is trusted as it stands."""
    sigs = _STRONG_SIGS.get(kind)
    if not sigs:
        return False
    code = _code_only(text)
    return not any(rx.search(code) for rx in sigs)


def refile_verdict(r: Dict[str, object], folder: str) -> Tuple[bool, str]:
    """(can be re-filed as a copybook, why not). Only a member the classifier
    typed asm / listing / mfs BY ITS CONTENT that (a) sits in a folder
    classify.DIR_HINTS maps to copybook or (b) carries COBOL level numbers,
    (c) has no JCL line and no IDENTIFICATION DIVISION / PROGRAM-ID, and (d)
    has none of the shapes a real member of that kind has. The strong
    signatures (JOB card, PROC, DBD, PSB, BMS, CSD, stage-1) are never
    overridden: such a member is not a copybook. A copy of the member in
    another folder or under another name reads the same, so nothing but the
    re-parse batch helps a member this refuses."""
    if r["by"] != "content":
        return False, ""
    kind = str(r["kind"])
    text = str(r["text"])
    if kind == "empty":
        # the reader would expand nothing: comments and blanks only - or text in columns 1-7 alone, said as such
        n = int(r.get("stub") or 0)                                 # type: ignore[arg-type]
        return False, stub_sentence(n) if n else EMPTY_WHY_NOT
    if kind not in WEAK_KINDS and not _signature_only_in_comments(kind, text):
        return False, (f"its text carries a {kind} signature ({r['reason']}): not a COBOL copybook - the copybook the "
                       "programs copy is another member, still to fetch")
    hint = next((k for rx, k in classify.DIR_HINTS if rx.search(folder or "")), None)
    if hint != "copybook" and not classify._SIG_DATA_LEVEL.search(text):
        return False, ("neither its folder (the name does not end in COPYLIB) nor its text (no level numbers) says it is a "
                       f"copybook - {FOLDER_LETS}; the classifier itself reads it as {kind} until {REFILED_ITEM}")
    if _JCL_LINE.search(text):
        return False, f"it holds a JCL line (//) - if it is the copybook after all, wait for {REFILED_ITEM}"
    if classify._SIG_COBOL.search(text) or classify._SIG_PROGRAM_ID.search(text):
        return False, "it holds an IDENTIFICATION DIVISION or PROGRAM-ID: a program, not a copybook"
    shape = ""
    if kind == "asm" and _ASM_SHAPE.search(text):
        shape = f"an Assembler member ({ASM_SHAPES})"
    elif kind == "listing" and (_LISTING_HEAD.search(text) or sum(1 for ln in text.splitlines() if _LISTING_SHAPE.match(ln)) >= 3):
        shape = "a compiler listing (the banner, or numbered source lines)"
    elif kind == "mfs" and _MFS_SHAPE.search(text):
        shape = "MFS source (a labelled MSG / FMT / DEV / DFLD / MFLD, or TYPE= / POS= / LTH= operands)"
    if shape:
        return False, (f"it has the shape of {shape} - the copybook the programs copy is then another member, still to "
                       f"fetch; if this member IS the COBOL copybook, wait for {REFILED_ITEM}")
    return True, ""


def member_readings(found: Sequence[Tuple[int, str, str, str]], conn: Optional[sqlite3.Connection] = None
                    ) -> List[Dict[str, object]]:
    """Per member (id, kind, folder, path) as members_named() lists them: how
    the classifier decided (how_classified) and whether refile_misfiled()
    may re-file it (refile_verdict). Reads each member's file once. With
    `conn`, the stored sha256 of each member goes to how_classified(), which
    tells a kind the UI's table declared from a file changed since the
    build."""
    shas: Dict[int, str] = {}
    if conn is not None and found:
        ids = [int(m[0]) for m in found]
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            shas.update({int(i): str(s) for i, s in conn.execute(
                f"SELECT id, sha256 FROM member WHERE id IN ({','.join('?' * len(chunk))})", chunk)})
    readings: List[Dict[str, object]] = []
    for mid, kind, folder, path in found:
        r = how_classified(path, kind, shas.get(int(mid)))
        r.update(id=int(mid), folder=folder, path=path)
        ok, why_not = refile_verdict(r, folder)
        r.update(refile=ok, why_not=why_not)
        readings.append(r)
    return readings


def read_misfiled(entries: Sequence[Dict[str, object]], conn: Optional[sqlite3.Connection] = None) -> None:
    """Give every misfiled entry of arrived_copybooks() its `readings`
    (member_readings over its `found` rows), once."""
    for e in entries:
        if "readings" not in e:
            e["readings"] = member_readings(e.get("found", []), conn)          # type: ignore[arg-type]


def filed_phrase(r: Dict[str, object]) -> str:
    """'filed as asm by its content (line 3 `START-DATE` reads as ...)' /
    'filed as proc by its folder (the folder name ends in PROCS)' / 'filed
    as doc by its extension (...)' / 'filed as proc by its declared kind
    (the kind declared for library ...)' - one member, the way it was
    decided."""
    by = str(r["by"])
    if by in ("content", "folder", "extension"):
        return f"filed as {r['kind']} by its {by} ({r['seen']})"
    if by == "declared":
        return f"filed as {r['kind']} by its declared kind ({r['seen']})"
    return f"filed as {r['kind']} ({r['seen']})"


def folder_helps(r: Dict[str, object]) -> bool:
    """A member typed by its content that this tool did NOT re-file only
    because nothing said copybook - no COPYLIB folder, no level numbers: a
    folder ending in COPYLIB lets the next run re-file it, so the folder
    fix is the instruction for it, not 'no folder change helps'."""
    return not r.get("refile") and FOLDER_LETS in str(r.get("why_not", ""))


def folder_fix(r: Dict[str, object]) -> str:
    """The instruction for a member the content did NOT decide: the rename /
    declare sentence, or, for a kind the UI's table declared, the declare
    sentence (the folder name did not decide it, so 'the folder name decided
    the kind' would be a wrong word)."""
    return DECLARED_FIX if r.get("by") == "declared" else MISFILED_FIX


def content_fix(r: Dict[str, object], dry_run: bool = False) -> str:
    """What to do for a member typed by its content: this tool re-files it
    (or would, on a dry run), the folder fix and a second run when only a
    COPYLIB folder is missing, or why it cannot."""
    if r.get("refile"):
        return CONTENT_FIX_DRY if dry_run else CONTENT_FIX
    if folder_helps(r):
        return FOLDER_THEN_RECOVER
    return f"no folder change helps (the content decided); not re-filed: {r['why_not']}"


def folder_decided(e: Dict[str, object]) -> bool:
    """A misfiled entry with at least one member the folder name, the
    extension, the declared kind, or nothing readable decided: the rename /
    declare sentence applies to it."""
    return any(r["by"] != "content" for r in e.get("readings", []))              # type: ignore[union-attr]


def folder_would_let(e: Dict[str, object]) -> bool:
    """A misfiled entry with a member typed by its content that a COPYLIB
    folder would let this tool re-file (folder_helps), and none re-filed."""
    readings = list(e.get("readings", []))                                         # type: ignore[arg-type]
    return any(folder_helps(r) for r in readings) and not any(r.get("refile") for r in readings)


def refiled_members(conn: sqlite3.Connection) -> Dict[int, str]:
    """{member id: parse_error} for every member this tool re-filed as a
    copybook (kind copybook, parse_status skipped, the marker in
    parse_error): not misfiled any more, its own layout rows still absent."""
    return {int(mid): str(err) for mid, err in conn.execute("SELECT id, parse_error FROM member WHERE parse_error LIKE ?",
                                                             (REFILED_MARK + "%",))}


_REFILED_KIND = re.compile(r"the classifier filed it as (\w+) by its content")


def refiled_kind_before(parse_error: Optional[str]) -> str:
    """'asm' from a re-filed member's parse_error."""
    m = _REFILED_KIND.search(parse_error or "")
    return m.group(1) if m else "?"


def _refile_member(conn: sqlite3.Connection, r: Dict[str, object], today: str) -> None:
    """The member row becomes a copybook the build keeps as it is (settled:
    'skipped'), hashed as the build hashes a copybook; the rows its parse as
    the other kind wrote (an MFS member's 'no macros recognised' note, a
    screen) are void for a copybook and go."""
    from . import build as _build                                   # local: build imports nothing from here
    mid = int(r["id"])                                              # type: ignore[arg-type]
    text, data, enc = reader.load(str(r["path"]))
    norm, nlines, fixed = _build.norm_hash("copybook", text, data, enc)
    conn.execute("DELETE FROM unresolved WHERE member_id=?", (mid,))
    for (sid,) in conn.execute("SELECT id FROM screen WHERE member_id=?", (mid,)).fetchall():
        conn.execute("DELETE FROM screen_field WHERE screen_id=?", (sid,))
    conn.execute("DELETE FROM screen WHERE member_id=?", (mid,))
    conn.execute("DELETE FROM literal_ref WHERE member_id=? AND program_id IS NULL", (mid,))
    conn.execute("UPDATE member SET kind='copybook', parse_status='skipped', parse_error=?, norm_sha=?, lines=?, "
                 "fixed_format=? WHERE id=?",
                 (f"{REFILED_MARK} on {today}: the classifier filed it as {r['kind']} by its content ({REFILED_ITEM}); "
                  "the programs copying it were marked", norm, nlines, fixed, mid))


def refile_misfiled(conn: sqlite3.Connection, misfiled: Sequence[Dict[str, object]], dry_run: bool = False
                    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], int]:
    """The stand-in for ROADMAP re-parse item 22: every misfiled member
    refile_verdict() accepts becomes a copybook in the index and the
    programs copying it are marked pending (by name: none of them resolved
    it, so none is left alone wrongly). Returns (entries re-filed - or, on a
    dry run, that would be - entries still misfiled, programs marked). On a
    dry run nothing changes and every entry stays misfiled."""
    read_misfiled(misfiled, conn)
    refiled: List[Dict[str, object]] = []
    remaining: List[Dict[str, object]] = []
    marked = 0
    today = time.strftime("%Y-%m-%d")
    for e in misfiled:
        todo = [r for r in e["readings"] if r["refile"]]                         # type: ignore[union-attr]
        if not todo:
            remaining.append(e)
            continue
        if dry_run:
            refiled.append(e)
            remaining.append(e)
            continue
        done = 0
        for r in todo:
            try:
                _refile_member(conn, r, today)
                done += 1
            except OSError as exc:                                  # gone since it was read: left misfiled, said why
                r.update(refile=False, why_not=f"the file could not be read to re-file it ({exc})")
        if not done:
            remaining.append(e)
            continue
        refiled.append(e)
        marked += _mark_programs_in(conn, [str(e["copybook"])])
    if refiled and not dry_run:
        conn.commit()
    return refiled, remaining, marked


def _unknown_among(entries: Sequence[Dict[str, object]]) -> int:
    """How many of the arrived copybooks have a member the index filed 'unknown'."""
    return sum(1 for e in entries if any(k == "unknown" for k, _f in e["members"]))    # type: ignore[union-attr]


def misfiled_cells(e: Dict[str, object], dry_run: bool = False) -> Tuple[str, str]:
    """('what to do', 'why that kind') for a misfiled entry: the rename /
    declare sentence for the members the folder name decided, the re-file
    (or why not) for the ones a line of their text decided - both when the
    copybook's name has members of both sorts."""
    readings = list(e.get("readings", []))                                         # type: ignore[arg-type]
    if not readings:
        return MISFILED_FIX, ""
    why = "; ".join(dict.fromkeys(filed_phrase(r) for r in readings))
    todo: List[str] = list(dict.fromkeys(folder_fix(r) for r in readings if r["by"] != "content"))
    by_content = [r for r in readings if r["by"] == "content"]
    if by_content:
        fixes = list(dict.fromkeys(content_fix(r, dry_run) for r in by_content))
        todo.append(("the copy filed by its content: " if todo else "") + "; ".join(fixes))
    return "; ".join(todo), why


def arrival_report(arrived: Sequence[Dict[str, object]], misfiled: Sequence[Dict[str, object]], dry_run: bool = False,
                   limit: int = 8, refiled: Sequence[Dict[str, object]] = (), waiting: Sequence[Dict[str, object]] = ()
                   ) -> List[str]:
    """The report's sections for arrival_scan(): what arrived after its
    programs were parsed (marked for the next build), what exists only under
    a kind the build does not expand (what to do, in words - the folder fix,
    or that no folder fix helps), what this run re-filed as a copybook
    (refile_misfiled), and what an earlier run re-filed that still waits for
    the build."""
    def progs(e: Dict[str, object]) -> str:
        names = [n + (" (copybook)" if k == "copybook" else "") for _i, n, k in e["programs"]]   # type: ignore[union-attr]
        return ", ".join(names[:limit]) + (f", +{len(names) - limit:,} more" if len(names) > limit else "")

    lines: List[str] = []
    if arrived:
        lines.append("\n## Copybooks that arrived after the program was parsed\n\n"
                     "The index holds a member with the copybook's name, but the programs below were parsed while it was "
                     "missing and nothing parsed them again: the build re-parses the copiers of a new member only when "
                     "it is filed as copybook or cobol (ROADMAP re-parse item 21), so one typed by its folder name - "
                     "'unknown' in a dataset-named folder - forces nothing. "
                     + ("They would be marked for the next build (dry run: nothing marked). " if dry_run
                        else "They are marked for the next build: run your usual build command. ")
                     + (f"A member filed 'unknown' ({_unknown_among(arrived)} below) is expanded into its programs but has no "
                        f"parser of its own, so its own lines are not indexed - `paragraph` shows them empty and nothing can "
                        f"cite them: {UNKNOWN_FIX}.\n\n" if _unknown_among(arrived) else "\n\n")
                     + "| copybook | kind the index filed it as | folder | programs |\n|---|---|---|---|\n")
        for e in arrived:
            kinds, folders = _kinds_folders(e["members"])                            # type: ignore[arg-type]
            lines.append(f"| {e['copybook']} | {kinds} | {folders} | {progs(e)} |\n")
    if misfiled:
        read_misfiled(misfiled)
        lines.append("\n## A member with the copybook's name exists but is filed as something else\n\n"
                     "The build expands only members filed as copybook, cobol, sql or unknown; the members below are of "
                     "another kind, so every program that copies them stays parsed only in part - a re-parse would find "
                     "nothing, so nothing is marked. A procedure copybook (paragraphs and statements, no level numbers, no "
                     "DIVISION header) has no content signature, and the folder name types it: PROCS makes it a proc, CNTL a "
                     "control card, a plain folder a document (ROADMAP re-parse item 20). A member whose text trips a weak "
                     "content signature - a field or paragraph named START-..., a comment naming MODULE MAP, a line whose "
                     "first word is MSG - is typed by that line before its level numbers and its folder are looked at "
                     f"({REFILED_ITEM}): no folder change helps, and a copy of it elsewhere or under another name reads the "
                     "same; this tool re-files such a member as a copybook in the index when its text is COBOL (the section "
                     "'Re-filed as copybook'), and says why when it cannot - one it cannot re-file only because nothing says "
                     "copybook (a folder with no COPY hint, no level numbers) needs the folder renamed to end in COPYLIB first, "
                     "then a second run. A member with no signature in a folder with no hint takes the kind declared for its "
                     "library in the UI's table: declare it copybook there. The last column says which decided.\n\n"
                     "| copybook | filed as | folder | programs | what to do | why that kind |\n|---|---|---|---|---|---|\n")
        for e in misfiled:
            kinds, folders = _kinds_folders(e["members"])                            # type: ignore[arg-type]
            todo, why = misfiled_cells(e, dry_run)
            lines.append(f"| {e['copybook']} | {kinds} | {folders} | {progs(e)} | {todo} | {why} |\n")
    if refiled:
        lines.append("\n## Re-filed as copybook\n\n"
                     "The classifier reads a member's first 8 KB for content signatures before its level numbers and before "
                     "its folder name, and three of them are weak: a first or second word beginning with START, CSECT or "
                     "DSECT reads as Assembler, a comment naming MODULE MAP or CROSS REFERENCE TABLE as a compiler listing, "
                     f"a line whose first word is MSG, FMT, DEV, DFLD or MFLD as an MFS statement ({REFILED_ITEM}). The "
                     "members below are COBOL by their folder (COPYLIB) or their level numbers, carry no JCL line and no "
                     "IDENTIFICATION DIVISION, and have none of those kinds' shapes, so "
                     + ("a run without --dry-run sets their kind to copybook in the index and marks the programs that copy "
                        "them (dry run: nothing changed). " if dry_run else
                        "this run set their kind to copybook in the index and marked the programs that copy them: run your "
                        "usual build command - it expands them (the build keeps the stored kind of an unchanged member, and "
                        "the expander reads the copybook's text from disk). ")
                     + "Their own layout rows - fields, offsets - stay absent until the next full re-parse files them as "
                     "copybooks itself; a --rebuild before that files them as before, and this tool re-files them again. "
                     f"{REPARSE_UNDOES} "
                     "If a re-filed member's text changes on disk before the re-parse, the next build files the new text as "
                     "before too (under a new member id) and un-links the programs copying it without parsing them again - "
                     "they read 'ok' with the fields of the old text until this tool has run again and the build after it.\n\n"
                     "| copybook | had been filed as | why (the signature that fired, in words) | folder | programs |\n"
                     "|---|---|---|---|---|\n")
        for e in refiled:
            done = [r for r in e["readings"] if r["refile"]]                         # type: ignore[union-attr]
            kinds = ", ".join(dict.fromkeys(str(r["kind"]) for r in done))
            why = "; ".join(dict.fromkeys(str(r["seen"]) for r in done))
            folders = ", ".join(dict.fromkeys(str(r["folder"]) for r in done))
            lines.append(f"| {e['copybook']} | {kinds} | {why} | {folders} | {progs(e)} |\n")
    if waiting:
        lines.append("\n## Re-filed on an earlier run - waiting for the build\n\n"
                     "An earlier run of this tool set the kind of the members below to copybook in the index and marked the "
                     "programs that copy them; nothing has built since, so those programs still say COPY X NOT FOUND. Nothing "
                     "arrived and nothing is marked again: run your usual build command - it expands them.\n\n"
                     "| copybook | folder | programs |\n|---|---|---|\n")
        for e in waiting:
            _kinds, folders = _kinds_folders(e["members"])                           # type: ignore[arg-type]
            lines.append(f"| {e['copybook']} | {folders} | {progs(e)} |\n")
    return lines


def needing_programs(conn: sqlite3.Connection, missing: Dict[str, int]) -> Set[str]:
    """The programs that copy a missing copybook: only their expanded text
    can hold it, so only theirs is read. A copybook that copies the missing
    one has no expanded text of its own: the programs that copy THAT
    copybook are read instead (through as many levels as it takes)."""
    if not missing:
        return set()
    names: Set[str] = set()
    wanted = {n.upper() for n in missing}
    for _level in range(6):
        q = ",".join("?" * len(wanted))
        copybooks: Set[str] = set()
        for name, kind in conn.execute(f"SELECT DISTINCT UPPER(m.name), m.kind FROM copy_use c JOIN member m ON m.id = c.member_id "
                                       f"WHERE UPPER(c.copybook) IN ({q})", tuple(wanted)):
            if kind == "copybook":
                copybooks.add(name)
            else:
                names.add(name)
        wanted = copybooks - names
        if not wanted:
            break
    return names


def copiers_by_kind(conn: sqlite3.Connection, missing: Dict[str, int]) -> Dict[str, Tuple[int, int]]:
    """{copybook: (programs copying it, copybooks copying it)}: a copybook
    copied only from inside other copybooks has no COPY statement in any
    program - its lines sit inside the outer copybook's block in a listing."""
    if not missing:
        return {}
    out: Dict[str, Tuple[int, int]] = {}
    q = ",".join("?" * len(missing))
    for name, kind, n in conn.execute(f"SELECT UPPER(c.copybook), m.kind, COUNT(DISTINCT c.member_id) FROM copy_use c "
                                      f"JOIN member m ON m.id = c.member_id WHERE UPPER(c.copybook) IN ({q}) GROUP BY 1, 2",
                                      tuple(missing)):
        p, b = out.get(name, (0, 0))
        out[name] = (p + n, b) if kind != "copybook" else (p, b + n)
    return out


def copier_names(conn: sqlite3.Connection, missing: Dict[str, int], limit: int = 8) -> Dict[str, str]:
    """{copybook: 'PGM1, PGM2, OUTER (copybook), +3 more'} - where each missing
    copybook is used, for the report's table ('list the copybooks you could
    not find anywhere and the programs where they are used')."""
    if not missing:
        return {}
    users: Dict[str, List[str]] = defaultdict(list)
    q = ",".join("?" * len(missing))
    for name, user, kind, line in conn.execute(f"SELECT UPPER(c.copybook), UPPER(m.name), m.kind, MIN(c.line) FROM copy_use c "
                                               f"JOIN member m ON m.id = c.member_id WHERE UPPER(c.copybook) IN ({q}) "
                                               f"GROUP BY 1, 2, 3 ORDER BY 2", tuple(missing)):
        users[name].append(user + (" (copybook)" if kind == "copybook" else "") + (f":{line}" if line else ""))
        # NAME:line - the line of the COPY statement, so a name that is not a copybook at all can be looked at
    return {n: ", ".join(u[:limit]) + (f", +{len(u) - limit:,} more" if len(u) > limit else "") for n, u in users.items()}


# --------------------------------------------------------------------------
# the missing copybooks looked for on disk (LESSONS 192)
# --------------------------------------------------------------------------
#
# 'missing copybooks in the index: 27' while the files sat in the COPYLIB
# folders he had fetched that day. Only two of the names had a member row at
# all - both 'empty': a 7-digit number in column 1 and nothing else, which the
# fixed-format reader takes for the sequence area and the indicator, so it
# sees no code - and the misfiled scan (arrival_scan) reads the index only,
# so the other 25 (on disk under another name, or arrived after the build, or
# skipped by it) got no word at all; he spent an evening on it. Now every
# missing name is looked for under the estate root ONCE - one os.walk over his
# 121k files, the stems kept in a local map, never a walk per name - and the
# report says per name what the disk holds and what to do.

DISK_READ_BYTES = 4 * 1024 * 1024        # a copybook is never this long: the code lines of a file on disk are counted in it
NEAR_CUTOFF = 0.75                       # difflib ratio for a near name (6 of 8 characters in order)
NEAR_MAX = 6                             # near names shown per missing copybook
NEAR_PREFIX_MIN = 4                      # a stem the name starts with must be this long to count (`B` is not a near name)
NO_FILE_TODO = ("no file with this name under the estate: fetch the library the listings name (the fetch list) - or the "
                "library is gone from the host")
EMPTY_TODO = ("open the file: if the number is all it holds, the library copy is a stub and the copybook's text is in the "
              "listings of the programs copying it (this run reads them) or on the host (fetch the member again); until "
              f"{STUB_ITEM} the build reads no code from it")
THERE_TODO = ("see atlas-problems.txt for a skip (a time limit, unreadable, too large) and the coverage report's 'failed' "
              "table, or a build that stopped before it recorded every file")


def estate_files(root: str, copy_folders: Sequence[str] = (), log=None) -> Tuple[Dict[str, List[str]], Set[str]]:
    """One walk of the estate root, listing what the build lists
    (build.SKIP_DIRS and every folder or file whose name starts with a dot
    left out, a folder the toolkit wrote - build.OUTPUT_MARKER - not
    entered, `.exp.cbl` left out; RECOVERED-COPYBOOKS is listed): ({STEM:
    [path, ...]}, the stems in the copybook folders) - the stem being the
    member name the build gives a file, its name without the extension in
    upper case. A copybook folder is one classify.DIR_HINTS maps to
    copybook, or one of `copy_folders` (the folders the index files
    copybooks in: his hand-fetched libraries carry no COPY hint). A line
    every 10 s while the walk runs."""
    from . import build as _build                                   # local: build imports nothing from here
    files: Dict[str, List[str]] = defaultdict(list)
    copy_stems: Set[str] = set()
    extra = {str(f).upper() for f in copy_folders}
    t0 = last = time.time()
    n = 0
    for dirpath, dirs, fnames in os.walk(root):
        if _build.OUTPUT_MARKER in fnames:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in _build.SKIP_DIRS and not d.startswith(".")]
        parent = os.path.basename(dirpath)
        hint = next((k for rx, k in classify.DIR_HINTS if rx.search(parent)), None)
        is_copy = hint == "copybook" or parent.upper() in extra
        for fn in fnames:
            if fn.startswith(".") or fn.lower().endswith(".exp.cbl"):
                continue
            stem = os.path.splitext(fn)[0].upper()
            files[stem].append(os.path.join(dirpath, fn))
            if is_copy:
                copy_stems.add(stem)
            n += 1
        if log and time.time() - last >= 10:
            last = time.time()
            log(f"  ... {n:,} files listed under the estate, {int(time.time() - t0)} s")
    return files, copy_stems


def last_build_started(conn: sqlite3.Connection) -> Optional[float]:
    """When the last build started, as seconds since the epoch (build_run
    .started_at is local time, `%Y-%m-%dT%H:%M:%S`); None when no run is
    recorded or the stamp does not read."""
    row = conn.execute("SELECT started_at FROM build_run ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row[0]:
        return None
    try:
        return time.mktime(time.strptime(str(row[0])[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def file_dated(path: str) -> Optional[float]:
    """When the file came to be where it is: the later of its modification
    time and, on Windows, its creation time - Explorer and `copy` keep the
    modification time of the original, so a copybook copied into the estate
    after the build carries a date before it, and only the creation time
    says it arrived."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return max(st.st_mtime, st.st_ctime)


def file_reading(path: str) -> Dict[str, object]:
    """What the build would make of a file on disk: `kind` as
    classify.classify reads its first 8 KB, 'empty' for a copybook / cobol
    member with no code lines (build._inventory_one), `code_lines` for a
    copybook / cobol kind (build.code_line_count), `stub` for an empty one
    (stub_lines), and `what` - the sentence for the report's 'what the file
    is' cell. Reads the first DISK_READ_BYTES."""
    from . import build as _build
    out: Dict[str, object] = {"kind": "?", "reason": "", "code_lines": None, "stub": 0, "what": ""}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            data = fh.read(DISK_READ_BYTES)
    except OSError as exc:
        out.update(reason=str(exc), what=f"could not be read from disk ({exc.strerror or exc})")
        return out
    text, enc = reader.decode_bytes(data)
    kind, reason = classify.classify(path, text[:8192])
    what = f"{kind} ({reason})"
    if kind == "unknown":
        what = f"unknown ({reason}) - or the kind declared for its library in the UI's table"
    if kind in ("copybook", "cobol"):
        n = _build.code_line_count(kind, text, data, enc)
        out["code_lines"] = n
        if n == 0:
            stub = stub_lines(text, data, enc)
            out["stub"] = stub
            kind = "empty"
            what = ("the build files it empty: " + (stub_sentence(stub) if stub else "no code lines (comments and blanks only)"))
        else:
            what = f"{kind} ({reason}), {n:,} code line(s)" + (f" in the first {DISK_READ_BYTES // 1024 // 1024} MB"
                                                               if size > DISK_READ_BYTES else "")
    out.update(kind=kind, reason=reason, what=what)
    return out


def _stamp(t: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:                                              # another drive
        return path


def near_names(name: str, copy_stems: Sequence[str], files: Dict[str, List[str]], root: str) -> List[Tuple[str, str]]:
    """[(stem, path relative to the root)] - the copybook-folder stems nearest
    to a missing name: every stem that starts with the name (`BOOKB-V2`,
    `BOOKB.CPY` - an extension or a suffix in the stem: the likeliest
    member under another name, so first) or that the name starts with
    (NEAR_PREFIX_MIN characters at least), then difflib.get_close_matches
    (NEAR_CUTOFF, at most 3: another spelling), NEAR_MAX in all.
    `copy_stems` is the sorted list estate_files() gives, made once per
    run."""
    found: List[str] = [s for s in copy_stems if s != name and s.startswith(name)]
    found += [s for s in copy_stems if s != name and len(s) >= NEAR_PREFIX_MIN and name.startswith(s)]
    for s in difflib.get_close_matches(name, copy_stems, n=3, cutoff=NEAR_CUTOFF):
        if s != name and s not in found:
            found.append(s)
    return [(s, _rel(files[s][0], root)) for s in found[:NEAR_MAX]] + ([(f"+{len(found) - NEAR_MAX:,} more", "")]
                                                                       if len(found) > NEAR_MAX else [])


def disk_check(missing: "Sequence[str] | Dict[str, int]", root: str, conn: sqlite3.Connection, log=None
               ) -> Dict[str, Dict[str, object]]:
    """Per missing copybook name, what the estate's files say - the estate
    root walked once (estate_files), then each name looked up in the map:

      (a) a file whose stem is the name -> `status` 'indexed' (the file is a
          member of the index filed 'empty': the stub sentence says where
          its text sits), 'late' (not in the index, dated after the last
          build started: build again) or 'there' (not in the index, there
          at the last build: the build did not index it - a skip, said in
          atlas-problems.txt); a name the index holds under another kind
          (asm, proc, doc ...) is left to the misfiled section, which says
          where that member is and what to do;
      (b) no such file -> 'near': the nearest stems in the copybook folders
          (near_names), for a COPY whose name is not the member's name;
      (c) nothing near -> 'none': the library is to fetch, or gone.

    Only the estate root is walked - a --from folder is read for expanded
    programs, not for copybooks. Each verdict carries `path` (relative to
    the root, '' when none), `kind`, `what` (the file, in words), `todo`,
    `on_disk` (the report's cell), `near` and `dated`."""
    names = [str(n).upper() for n in missing]
    out: Dict[str, Dict[str, object]] = {}
    if not names:
        return out
    copy_folders = [str(f) for (f,) in conn.execute("SELECT DISTINCT library FROM member WHERE kind = 'copybook' "
                                                     "AND library IS NOT NULL AND library <> ''")]
    files, copy_set = estate_files(root, copy_folders, log)
    copy_stems = sorted(copy_set)
    indexed: Dict[str, str] = {}                                    # normcase(abspath(path)) -> kind, for the missing names
    said_already: Set[str] = set()                                  # names a member of another kind carries: the misfiled section
    for k in range(0, len(names), 500):
        chunk = names[k:k + 500]
        for name, path, kind in conn.execute("SELECT UPPER(name), path, kind FROM member WHERE UPPER(name) IN "
                                             f"({','.join('?' * len(chunk))})", chunk):
            indexed[os.path.normcase(os.path.abspath(path))] = str(kind)
            if kind != "empty":
                said_already.add(str(name))
    started = last_build_started(conn)
    for name in names:
        if name in said_already:
            # the index holds the name under a kind the build does not expand (asm, proc, doc ...): the section 'A
            # member with the copybook's name exists but is filed as something else' says where it is and what to
            # do, and the disk has nothing to add - only a member filed 'empty' has (where its text sits)
            continue
        paths = files.get(name, [])
        if paths:
            out[name] = _found_verdict(name, paths, indexed, started, root)
            continue
        near = near_names(name, copy_stems, files, root)
        if near:
            shown = ", ".join(s if not p else f"{s} ({p})" for s, p in near)
            out[name] = {"status": "near", "path": "", "paths": [], "kind": "", "what": "-", "near": near, "dated": None,
                         "on_disk": "no",
                         "todo": f"no file with this name under the estate; nearest names in the copybook folders: {shown} "
                                 "(is the COPY statement's name the member's name?)"}
        else:
            out[name] = {"status": "none", "path": "", "paths": [], "kind": "", "what": "-", "near": [], "dated": None,
                         "on_disk": "no", "todo": NO_FILE_TODO}
    return out


def _found_verdict(name: str, paths: Sequence[str], indexed: Dict[str, str], started: Optional[float], root: str
                   ) -> Dict[str, object]:
    """The verdict for a name a file carries: a copy the index holds first
    (the misfiled section says the rest), else the one dated latest."""
    in_index = [(p, indexed[os.path.normcase(os.path.abspath(p))]) for p in paths
                if os.path.normcase(os.path.abspath(p)) in indexed]
    rels = [_rel(p, root) for p in paths]

    def more(path: str) -> str:
        others = [r for r in rels if r != _rel(path, root)]
        return f" (+{len(others)} more file(s) with this name: {', '.join(others[:3])})" if others else ""

    if in_index:
        path, kind = in_index[0]
        reading = file_reading(path)
        what = str(reading["what"])
        if kind == "empty":
            stub = int(reading["stub"] or 0)                        # type: ignore[arg-type]
            what = stub_sentence(stub) if stub else "no code lines (comments and blanks only): nothing a program could copy"
        return {"status": "indexed", "path": _rel(path, root), "paths": rels, "kind": kind, "what": what,
                "near": [], "dated": file_dated(path),
                "on_disk": f"yes: {_rel(path, root)} - in the index, filed as {kind}{more(path)}", "todo": EMPTY_TODO}
    dated = [(file_dated(p) or 0.0, p) for p in paths]
    when, path = max(dated)
    reading = file_reading(path)
    kind = str(reading["kind"])
    late = started is not None and when > started
    on_disk = f"yes: {_rel(path, root)} - not in the index{more(path)}"
    if late:
        todo = (f"arrived after the last build (file dated {_stamp(when)}, last build started {_stamp(started)}): run your "
                "usual build command")
    elif started is None:
        todo = f"the index records no build start to date it against: the build did not index it - its kind would be {kind}; {THERE_TODO}"
    else:
        todo = (f"was there at the last build (file dated {_stamp(when)}, last build started {_stamp(started)}): the build did "
                f"not index it - its kind would be {kind}"
                + (" / it has no code lines" if kind == "empty" else "") + f"; {THERE_TODO}")
    return {"status": "late" if late else "there", "path": _rel(path, root), "paths": rels, "kind": kind,
            "what": str(reading["what"]), "near": [], "dated": when, "on_disk": on_disk, "todo": todo}


def disk_counts(checked: Dict[str, Dict[str, object]]) -> Dict[str, int]:
    """The stats: on_disk (a file with the name, in the index or not),
    arrived_late, no_file - and the parts the console line needs."""
    c = Counter(str(v["status"]) for v in checked.values())
    return {"on_disk": c["late"] + c["there"] + c["indexed"], "arrived_late": c["late"], "no_file": c["near"] + c["none"],
            "there": c["there"], "indexed": c["indexed"], "near": c["near"]}


def disk_line(checked: Dict[str, Dict[str, object]]) -> str:
    """The console's one line, zero counts left out."""
    c = disk_counts(checked)
    on = []
    if c["arrived_late"]:
        on.append(f"{c['arrived_late']:,} arrived after the last build - not in the index yet")
    if c["indexed"]:
        on.append(f"{c['indexed']:,} in the index filed empty - no code lines the reader sees")
    if c["there"]:
        on.append(f"{c['there']:,} there at the last build yet not in the index")
    parts = []
    if c["on_disk"]:
        parts.append(f"{c['on_disk']:,} on disk ({', '.join(on)})")
    if c["no_file"]:
        parts.append(f"{c['no_file']:,} with no file under the estate"
                     + (f" ({c['near']:,} with a near name)" if c["near"] else ""))
    return "  missing copybooks checked on disk: " + ", ".join(parts)


def disk_next(checked: Dict[str, Dict[str, object]]) -> str:
    """The single most useful 'next:' after the console line: the build when
    a file arrived after it; else the fetch list; else the report's table."""
    c = disk_counts(checked)
    if c["arrived_late"]:
        return (f"  next: run your usual build command - it indexes the {c['arrived_late']:,} copybook(s) that arrived after "
                "the last build, and the programs copying them re-expand by themselves")
    if c["no_file"]:
        return ("  next: fetch the libraries that hold these copybooks - the report's fetch list names the datasets the "
                r"listings say (work\fetch-list.txt)"
                + (f"; the {c['near']:,} near name(s) the report lists may be the members under another name: check them "
                   "against the COPY statements" if c["near"] else ""))
    return ("  next: the report's table says per file why the build did not index it as a copybook - a member filed empty "
            "(its text in columns 1-7), a skipped file (atlas-problems.txt)")


def disk_cell(v: Dict[str, object]) -> str:
    """One cell for `coverage` and `copybook`: where the file is and what to do."""
    status = str(v["status"])
    if status in ("late", "there"):
        return f"on disk at {v['path']}, not in the index - {v['todo']}"
    if status == "indexed":
        return f"on disk at {v['path']}, in the index as {v['kind']}: {v['what']}"
    return str(v["todo"])


def disk_report(checked: Dict[str, Dict[str, object]], missing: Dict[str, int], users: Dict[str, str], root: str
                ) -> List[str]:
    """The report's section, right after the summary lines."""
    if not checked:
        return []
    lines = ["\n## Missing copybooks, checked on disk\n\n"
             f"Every missing name was looked for under the estate root `{root}` - the folders the build lists; a --from "
             "folder is read for expanded programs, not for copybooks - first as a file whose name without its extension "
             "is the copybook's name, then as a near name in the copybook folders. A file on disk that is not in the index "
             "either arrived after the last build started (run your usual build command) or was there and the build did "
             "not index it (atlas-problems.txt and the coverage report's 'failed' table say why a file was skipped). A "
             "member the build filed 'empty' holds no code the reader sees: when its text sits in columns 1-7 - the "
             f"sequence area and the indicator column of fixed-format COBOL - the table says so ({STUB_ITEM}).\n\n"
             "| copybook | programs copying it | on disk? | what the file is | what to do |\n|---|---|---|---|---|\n"]
    for name in sorted(checked):
        v = checked[name]
        progs = f"{missing.get(name, 0)}" + (f" ({users[name]})" if users.get(name) else "")
        lines.append(f"| {name} | {progs} | {v['on_disk']} | {v['what']} | {v['todo']} |\n")
    return lines


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


def recovered_only(conn: sqlite3.Connection, roots: Sequence[str], real: Set[str]) -> Dict[str, int]:
    """{copybook: programs copying it} for the copybooks the index holds ONLY
    as recovered copies: a member inside a recovered folder and no real
    member anywhere. Once the build has read a recovered copy the copybook is
    no longer missing - every COPY of it resolves - but the library the
    listing named is still the one to fetch: the real member replaces the
    copy. So these stay on the fetch list, and their programs' listings are
    still read for the table that names the library."""
    rec = [os.path.normcase(os.path.abspath(r)) + os.sep for r in roots]
    names: Set[str] = set()
    for name, path in conn.execute(f"SELECT name, path FROM member WHERE kind IN ({','.join('?' * len(RESOLVER_KINDS))})",
                                   RESOLVER_KINDS):
        p = os.path.normcase(os.path.abspath(path))
        if name.upper() not in real and (any(p.startswith(r) for r in rec) or FOLDER.lower() in p.lower()):
            names.add(name.upper())
    if not names:
        return {}
    q = ",".join("?" * len(names))
    return {str(n): int(k) for n, k in conn.execute(f"SELECT UPPER(copybook), COUNT(DISTINCT member_id) FROM copy_use "
                                                     f"WHERE UPPER(copybook) IN ({q}) GROUP BY 1", tuple(sorted(names)))}


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
    build (a vanished member forces nothing by itself). Programs only: a
    copybook that copies the removed one gains nothing from a re-parse (its
    own COPY rows are never resolved), and a copybook marked pending is
    re-inserted under a new id, which nulls the links of every program
    copying it (LESSONS 184) - those programs carry their own row for the
    nested COPY and are marked here by it."""
    if not names:
        return 0
    conn = sqlite3.connect(db)
    try:
        n = _mark_programs_in(conn, names)
        conn.commit()
        return n
    finally:
        conn.close()


def _mark_programs_in(conn: sqlite3.Connection, names: Sequence[str]) -> int:
    """_mark_programs on an open connection (the caller commits)."""
    n = 0
    for name in names:
        n += conn.execute("UPDATE member SET parse_status='pending', parse_error=NULL WHERE kind='cobol' AND id IN "
                          "(SELECT member_id FROM copy_use WHERE UPPER(copybook)=?)", (name.upper(),)).rowcount
    return n


def _mark_members(db: str, ids: Sequence[int]) -> int:
    """Members by id (the programs that copy a copybook which arrived after
    they were parsed) are parsed again on the next build. By id, not by
    copybook name: a program in another system whose own copy resolved is
    left alone. Programs only, whatever ids are given: a copybook member
    marked pending is re-inserted under a new id by the next build, which
    nulls the links of every program copying it (LESSONS 184)."""
    ids = sorted(set(int(i) for i in ids))
    if not ids:
        return 0
    conn = sqlite3.connect(db)
    try:
        n = 0
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            n += conn.execute(f"UPDATE member SET parse_status='pending', parse_error=NULL "
                              f"WHERE kind='cobol' AND id IN ({','.join('?' * len(chunk))})", tuple(chunk)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _write_report(report: str, lines: Sequence[str]) -> None:
    os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
    with open(report, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(lines))


def held_datasets(conn: sqlite3.Connection, libs: Dict[str, str]) -> Dict[str, str]:
    """{dataset: a folder the index holds it as} - the `library` table first
    (the fetcher's marker), then every member's folder that is named after a
    dataset (the fetcher names each folder after its dataset)."""
    out: Dict[str, str] = {}
    try:                                                               # the folder as the table spells it: `libs` keys are
        for folder, dsn in conn.execute("SELECT folder, dataset FROM library WHERE folder IS NOT NULL AND dataset IS NOT NULL "
                                        "ORDER BY id"):                # normalised (lower-cased on Windows), not for showing
            out.setdefault(str(dsn).strip().upper(), str(folder))
    except sqlite3.OperationalError:
        pass
    try:
        paths = [str(p) for (p,) in conn.execute("SELECT DISTINCT path FROM member WHERE path IS NOT NULL")]
    except sqlite3.OperationalError:
        return out
    for folder in sorted({os.path.dirname(p) for p in paths}):
        dsn = dataset_of(os.path.join(folder, "x"), libs)
        if dsn:
            out.setdefault(dsn, folder)
    return out


def fetch_list(conn: sqlite3.Connection, sources: Dict[str, List[CopySource]], missing: Dict[str, int],
               checks: Sequence[Dict[str, object]], recovered: Sequence[str] = ()) -> Tuple[List[Dict[str, object]], List[str]]:
    """The libraries the listings name, one entry per dataset, and the
    copybooks to fetch for that no listing's table names. `missing` holds
    every copybook to fetch for: the ones the index lacks and (`recovered`)
    the ones it holds only as a recovered copy. Each entry: dataset, held (a
    folder the index holds it as, or None), programs (whose listings name
    it), and the copybooks that came from it in four lists - missing (the
    index lacks them), recovered (held only as a recovered copy: still to
    fetch for), chosen (the build chose among several and this listing says
    this dataset), other. Datasets with copybooks to fetch for first, then
    the most programs first: the order to fetch in."""
    holders = held_datasets(conn, library_datasets(conn))
    rec = {n.upper() for n in recovered}
    chosen_at: Dict[str, Set[str]] = defaultdict(set)                  # dataset -> copybooks chosen among several
    for v in checks:
        for d in v["listing_datasets"]:                                 # type: ignore[union-attr]
            chosen_at[str(d)].add(str(v["copybook"]))
    per: Dict[str, Dict[str, object]] = {}
    named: Set[str] = set()
    for program, rows in sources.items():
        for row in rows:
            cb, dsn = row[0], row[2]
            if not dsn or not cb:
                continue
            named.add(cb)
            e = per.setdefault(dsn, {"dataset": dsn, "programs": set(), "missing": set(), "recovered": set(),
                                     "chosen": set(), "other": set()})
            e["programs"].add(program)                                  # type: ignore[union-attr]
            bucket = ("recovered" if cb in rec else "missing") if cb in missing else \
                     "chosen" if cb in chosen_at.get(dsn, ()) else "other"
            e[bucket].add(cb)                                           # type: ignore[union-attr]
    out: List[Dict[str, object]] = []
    for dsn, e in per.items():
        e["held"] = holders.get(dsn)
        e["programs"] = len(e["programs"])                              # type: ignore[arg-type]
        for k in ("missing", "recovered", "chosen", "other"):
            e[k] = sorted(e[k])                                         # type: ignore[arg-type]
        out.append(e)
    out.sort(key=lambda e: (0 if (e["missing"] or e["recovered"]) else 1, -int(e["programs"]), str(e["dataset"])))  # type: ignore[arg-type]
    unnamed = sorted(n for n in missing if n not in named)
    return out, unnamed


def _to_fetch_for(e: Dict[str, object]) -> str:
    """The table cell: the copybooks the index lacks, then the ones it holds
    only as recovered copies, said once for the lot."""
    missing, rec = list(e["missing"]), list(e["recovered"])           # type: ignore[arg-type]
    if not rec:
        return _names(missing)
    said = f"recovered cop{'ies' if len(rec) != 1 else 'y'}: {_names(rec)}"
    return f"{_names(missing)}; {said}" if missing else said


def _names(names: Sequence[str], limit: int = FETCH_NAMES) -> str:
    if not names:
        return "-"
    return ", ".join(names[:limit]) + (f", +{len(names) - limit:,}" if len(names) > limit else "")


def fetch_report(fetch: Sequence[Dict[str, object]], unnamed: Sequence[str], root: Optional[str], list_file: str) -> List[str]:
    """The report section: the table of libraries the listings name, the
    sentence on how to fetch one, and the copybooks to fetch for that no
    table names."""
    lines = ["\n## Libraries the listings name (fetch list)\n\n"
             "After the source, a compiler listing names the library dataset each copybook was read from. A dataset "
             "that holds a copybook to fetch for - one the index lacks, or holds only as a recovered copy (the build "
             "has read the copy this tool wrote, so the copybook is no longer missing, but the real member is still on "
             "the host) - and is not fetched yet is the one to fetch: its members, once in the estate, make the "
             "recovered copies unnecessary.\n\n"]
    if fetch:
        lines.append("| dataset | held? | copybooks to fetch for (missing, or held only as a recovered copy) | "
                     "copybooks the build chose among several | programs |\n|---|---|---|---|---|\n")
        for e in fetch:
            held = f"held as {_tail(str(e['held']), 2)}" if e["held"] else "not fetched"
            lines.append(f"| {e['dataset']} | {held} | {_to_fetch_for(e)} | {_names(e['chosen'])} | {e['programs']} |\n")  # type: ignore[arg-type]
        to_fetch = [e for e in fetch if not e["held"]]
        lines.append("\nFetch a dataset with the UI's Bulk add (paste the dataset names) or your zowe command; then run the "
                     "build; the missing copybooks it holds resolve, and the recovered copies of them are removed on the next "
                     f"recover run. `{list_file}` holds the {len(to_fetch)} not-fetched dataset{'s' if len(to_fetch) != 1 else ''}, "
                     "one per line, the ones with copybooks to fetch for first, datasets only - paste it into Bulk add.\n")
    else:
        lines.append("_no listing read has a copybook-source table (an older compiler's listing prints none), so no library "
                     "is named_\n")
    n = len(unnamed)
    lines.append(f"\n- {n} copybook{'s' if n != 1 else ''} to fetch for {'are' if n != 1 else 'is'} named by no listing's table"
                 + (": " + _names(unnamed) if unnamed else "") + "\n")
    return lines


def _write_fetch_list(path: str, fetch: Sequence[Dict[str, object]]) -> int:
    """work/fetch-list.txt: the not-fetched datasets, one per line, the ones
    with missing copybooks first - datasets only, never a member name, so it
    pastes into the UI's Bulk add as it is. Rewritten on every run."""
    to_fetch = [str(e["dataset"]) for e in fetch if not e["held"]]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(d + "\n" for d in to_fetch))
    return len(to_fetch)


def _check_after(db: str, per_program: Dict[str, List[CopySource]], dry_run: bool, missing: Dict[str, int],
                 chosen: bool, recovered: Sequence[str] = ()) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], List[str]]:
    """Store this run's copybook-source rows (not on a dry run); check every
    choice against the rows now known (this run's for the programs read, the
    stored ones for the rest) when there is a choice to check; and build the
    fetch list from the same rows. `missing` is every copybook to fetch for,
    `recovered` the ones among them the index holds only as recovered copies.
    Returns (checks, fetch list, the copybooks to fetch for no table names)."""
    conn = sqlite3.connect(db)
    try:
        if per_program and not dry_run:
            store_copy_sources(conn, per_program)
        known = stored_copy_sources(conn)
        known.update(per_program)
        checks = check_choices(conn, known) if (per_program or chosen) else []
        fetch, unnamed = fetch_list(conn, known, missing, checks, recovered)
        return checks, fetch, unnamed
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
        # the copybooks that are NOT missing any more but whose programs still say NOT FOUND: a member with
        # the name arrived after they were parsed (marked below) - missing_copybooks() leaves that case out,
        # so it fell through silently - or exists only under a kind the build never expands (said, with
        # what to do) - counted as missing, and rejected by the listing path, without a word that the
        # member was on disk
        arrived, misfiled, waiting = arrival_scan(conn)
        # a misfiled member a LINE OF ITS TEXT typed asm / listing / mfs (a START- name, a MODULE MAP comment, a
        # first word MSG) is re-filed as a copybook here, not on a dry run - no folder fix helps it (ROADMAP
        # re-parse item 22); the missing count is taken after, so a re-filed name is not sent to the listings
        refiled, misfiled, marked_refiled = refile_misfiled(conn, misfiled, dry_run)
        missing = missing_copybooks(conn)
        copiers = copiers_by_kind(conn, missing)
        users = copier_names(conn, missing)
        # every missing name looked for on disk, before any listing is read (also on a dry run): the misfiled scan
        # above sees the index only, and 25 of his 27 missing copybooks had no member row at all while the files
        # sat in the COPYLIB folders - under another name, arrived after the build, or skipped by it (LESSONS 192)
        checked: Dict[str, Dict[str, object]] = {}
        not_checked = ""
        if missing:
            if root and os.path.isdir(root):
                checked = disk_check(missing, root, conn, log=log)
            elif root:
                not_checked = f"  the estate root {root} is not on disk from here: the missing names were not looked for on disk"
            else:
                not_checked = "  the index records no estate root: the missing names were not looked for on disk"
        disk_stats = disk_counts(checked)
        sources = listing_sources(conn, folders)
        index = originals(conn)
        # the programs whose copybook the build CHOSE among several: their listings say which copy was right
        chosen = {p[0] for p in chosen_picks(conn)}
        roots_out = [out_dir] + ([os.path.join(root, d, FOLDER) for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
                                 if root and os.path.isdir(root) else [])
        real = real_copies(conn, roots_out)
        # held only as a recovered copy: no longer missing to the build, still to fetch for - the listings of the
        # programs copying them are read too, so the fetch list keeps naming their libraries after the build has run
        recovered = recovered_only(conn, roots_out, real)
        to_fetch_for = dict(missing)
        to_fetch_for.update(recovered)
        needing = needing_programs(conn, to_fetch_for)
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
    marked_removed = 0                                                  # programs marked because their recovered copy went
    removed_lines: List[str] = []                                       # the report's section for it
    if removed:
        names = sorted(set(removed))
        log(f"  {len(removed)} recovered copybook(s) {'would be removed' if dry_run else 'removed'} - the estate now holds "
            "the real member: " + ", ".join(names[:8]) + (" ..." if len(names) > 8 else ""))
        if not dry_run:
            marked_removed = _mark_programs(db, names)
            log(f"  {marked_removed} program(s) that had expanded them are marked for the next build")
            for d, ents in entries.items():
                if os.path.isdir(d):
                    _save_marker(d, ents)
        # said in the report too: a run whose only work this was must not end as 'nothing to report'
        removed_lines.append("\n## Recovered copies removed - the real member arrived\n\n"
                             + ("Would be removed (dry run: nothing removed, nothing marked): " if dry_run else "Removed: ")
                             + ", ".join(names)
                             + ("" if dry_run else f". {marked_removed} program(s) that had expanded them are marked for the "
                                                   "next build: run your usual build command") + ".\n")
    marked = 0
    if arrived:
        ids = sorted({i for e in arrived for i in e["ids"]})                    # type: ignore[union-attr]
        if not dry_run:
            marked = _mark_members(db, ids)
        log(f"  {len(ids):,} program(s) copy a copybook that has arrived since they were parsed "
            f"({len(arrived):,} copybook{'s' if len(arrived) != 1 else ''}): "
            f"{'would be marked' if dry_run else 'marked'} for the next build")
        if _unknown_among(arrived):
            log(f"  {_unknown_among(arrived):,} of them filed 'unknown' (a folder with no COPY hint): the build expands it but "
                f"has no parser for it, so its own lines are not indexed - {UNKNOWN_FIX}")
    if refiled:
        n_prog = len({p[0] for e in refiled for p in e["programs"]})            # type: ignore[union-attr]
        kinds = "/".join(sorted({str(r["kind"]) for e in refiled for r in e["readings"] if r["refile"]}))   # type: ignore[union-attr]
        if dry_run:
            log(f"  {len(refiled):,} misfiled copybook(s) would be re-filed as copybook in the index (the classifier had read "
                f"them as {kinds} by a line of their text): {n_prog:,} program(s) would be marked for the next build (dry run: "
                "nothing changed)")
        else:
            log(f"  {len(refiled):,} misfiled copybook(s) re-filed as copybook in the index (the classifier had read them as "
                f"{kinds} by a line of their text): {marked_refiled:,} program(s) marked for the next build")
    if waiting:
        n_prog = len({p[0] for e in waiting for p in e["programs"]})            # type: ignore[union-attr]
        log(f"  {len(waiting):,} copybook(s) re-filed on an earlier run wait for the build ({n_prog:,} program(s) marked "
            "already): run your usual build command")
    # still misfiled: the folder name (or the declared kind) decided (the rename / declare sentence); a line of the
    # text decided and only a COPYLIB folder is missing for this tool to re-file it (the folder fix, then a second
    # run); or a line of the text decided and this run could not re-file it (the report says why); on a dry run
    # the would-be re-filed stay in `misfiled` too, and are left out of every list here: a name carried by a
    # re-filable COPYLIB copy AND a folder-typed PROCS copy needs no folder change - the run without --dry-run
    # re-files the one copy and the build resolves it (the real run drops the entry from `misfiled`, so only the
    # dry run could send him to rename a folder; LESSONS 188)
    folder_fix = [e for e in misfiled if folder_decided(e) and not any(e is x for x in refiled)]
    by_content = [e for e in misfiled if not folder_decided(e) and not any(e is x for x in refiled)]
    content_folder = [e for e in by_content if folder_would_let(e)]
    content_left = [e for e in by_content if not folder_would_let(e)]
    if folder_fix:
        n_prog = len({p[0] for e in folder_fix for p in e["programs"]})         # type: ignore[union-attr]
        kinds = ", ".join(sorted({k for e in folder_fix for k, _f in e["members"]}))   # type: ignore[union-attr]
        log(f"  {len(folder_fix):,} copybook name(s) exist in the index only as a member of a kind the build does not expand "
            f"({kinds}): {n_prog:,} program(s) stay parsed only in part until the folder is renamed to end in COPYLIB or "
            "the library's kind declared in the UI's table - the report names each")
    if content_folder:
        n_prog = len({p[0] for e in content_folder for p in e["programs"]})     # type: ignore[union-attr]
        kinds = ", ".join(sorted({k for e in content_folder for k, _f in e["members"]}))   # type: ignore[union-attr]
        log(f"  {len(content_folder):,} copybook name(s) exist in the index only as a member the classifier typed by a line "
            f"of its text ({kinds}) in a folder with no COPY hint and with no level numbers to go by: {n_prog:,} program(s) "
            "stay parsed only in part until the folder is renamed to end in COPYLIB and this tool is run again - it then "
            f"re-files the member ({REFILED_ITEM})")
    if content_left:
        n_prog = len({p[0] for e in content_left for p in e["programs"]})       # type: ignore[union-attr]
        kinds = ", ".join(sorted({k for e in content_left for k, _f in e["members"]}))   # type: ignore[union-attr]
        log(f"  {len(content_left):,} copybook name(s) exist in the index only as a member the classifier typed by a line of "
            f"its text ({kinds}) and this run could not re-file: {n_prog:,} program(s) stay parsed only in part - no folder "
            f"change helps; the report says why for each ({REFILED_ITEM})")
    arrival_lines = removed_lines + arrival_report(arrived, misfiled, dry_run, refiled=refiled, waiting=waiting)
    disk_lines = disk_report(checked, missing, users, root or "")   # right after the summary lines, before the rest
    # every missing name is one a member of another kind carries and a rename (or a run without --dry-run, or a
    # rename and then a run) settles: that comes before any listing - a member this run could not re-file for any
    # other reason is left to the listings
    folder_names = {str(e["copybook"]) for e in folder_fix}
    then_run_names = {str(e["copybook"]) for e in content_folder}
    settle_first = folder_names | then_run_names | {str(e["copybook"]) for e in refiled}
    only_misfiled = bool(missing) and set(missing) <= settle_first

    def early(stats: Dict[str, object], nothing: str) -> Dict[str, object]:
        """A run that ends before the listings are read still reports the
        removed copies and the arrived and misfiled copybooks; with nothing
        to say it replaces the earlier run's report rather than leave it in
        place still saying 'marked for the next build' (`nothing`: why this
        run has nothing)."""
        if report:
            head = [f"# Recovered copybooks - {time.strftime('%Y-%m-%d %H:%M')}\n",
                    f"\n- missing in the index: {len(missing)}; expanded texts read: 0; removed (real member arrived): "
                    f"{len(removed)}\n"]
            if disk_lines or arrival_lines:
                _write_report(report, head + disk_lines + arrival_lines)
                log(f"every name: {report}")
            elif os.path.exists(report):
                _write_report(report, head + [f"\nNothing to report on this run: {nothing}.\n"])
                log(f"{report} - nothing to report on this run (the earlier run's report is replaced)")
        if marked or marked_removed or marked_refiled or waiting:
            log("next: run your usual build command - the programs marked re-expand by themselves")
        if folder_fix:
            log(MISFILED_NEXT)
        if content_folder:
            log(NEXT_FOLDER_REFILE)
        stats.update({"arrived": len(arrived), "misfiled": len(misfiled), "refiled": 0 if dry_run else len(refiled),
                      "waiting": len(waiting), "marked": marked + marked_removed + marked_refiled,
                      "on_disk": disk_stats["on_disk"], "arrived_late": disk_stats["arrived_late"],
                      "no_file": disk_stats["no_file"]})
        return stats

    log(f"missing copybooks in the index: {len(missing):,}, used in {sum(missing.values()):,} places (a place = one program "
        "copying one of them)")
    if only_misfiled:
        if any(n in folder_names for n in missing):
            log("  every one of them is the name of a member filed as a kind the build does not expand (above): the folder fix "
                "comes first" + (" (then a second run of this tool, for the member a COPYLIB folder lets it re-file)"
                                 if any(n in then_run_names for n in missing) else "")
                + " - the listings only if a program still says NOT FOUND after the build")
        elif any(n in then_run_names for n in missing):
            log("  every one of them is the name of a member a COPYLIB folder would let this tool re-file (above): the folder "
                "fix and a second run come first - the listings only if a program still says NOT FOUND after the build")
        else:
            log("  every one of them is the name of a member this run would re-file as a copybook (above): run without "
                "--dry-run first - the listings only if a program still says NOT FOUND after the build")
    if checked:
        log(disk_line(checked))
        log(disk_next(checked))
    elif not_checked:
        log(not_checked)
    if recovered:
        log(f"  held only as recovered copies: {len(recovered):,} - the build has read them, so they are not missing any more; "
            "their libraries stay on the fetch list until the real members arrive")
    check_only = False                                                 # nothing to recover: read listings only to check the choices
    if not missing and not everything:
        log("nothing to recover: every copybook the programs copy is in the index")
        if not chosen and not recovered:
            return early({"missing": 0, "sources": len(sources), "written": 0, "rejected": 0, "not_found": 0,
                          "removed": len(removed), "kept": 0, "formats": {}, "out": out_dir, "unconfirmed": 0, "per_system": 0},
                         "every copybook the programs copy is in the index, and every program that copies one was parsed "
                         "after it arrived")
        check_only = True                                              # ... and to name the libraries of the recovered copies
    if not sources:
        log("expanded texts to read: 0 - the index holds no compiler listings and no --from folder was given")
        if not only_misfiled:
            log('next: run again as  python -m atlas.recover --db atlas.db --from "FOLDER"  with the folder that holds the '
                "programs' compiler listings or expanded source")
        checked = None
        if chosen:
            checks, _fetch, _unnamed = _check_after(db, {}, True, to_fetch_for, True, recovered)
            log(choice_line(checks) + " - the listings would say which copy of a copybook chosen among several was right")
            checked = choice_counts(checks)
        return early({"missing": len(missing), "sources": 0, "written": 0, "rejected": 0, "not_found": len(missing),
                      "removed": 0, "kept": 0, "formats": {}, "out": out_dir, "unconfirmed": 0, "checked": checked},
                     f"{len(missing)} copybook(s) missing in the index and no expanded text to read them from - run again "
                     "with --from FOLDER")
    found_all = len(sources)
    if not everything:
        # only the expanded text of a program that copies a missing copybook can hold it; a text
        # whose name the index does not know at all is read too, since nothing says it cannot; and
        # the listing of a program whose copybook was chosen among several says which copy was right
        sources = [(p, s) for p, s in sources
                   if os.path.splitext(os.path.basename(p))[0].upper() in needing
                   or os.path.splitext(os.path.basename(p))[0].upper() in chosen
                   or os.path.splitext(os.path.basename(p))[0].upper() not in index]
    log(f"expanded texts to read: {len(sources):,} of {found_all:,} found "
        f"({'compiler listings the index holds' if not folders else 'listings + the folders given'})"
        + ("" if everything else f" - only the {len(needing):,} programs that copy a missing copybook"
                                  + (" (or one held only as a recovered copy)" if recovered else "")
                                  + (f", the {len(chosen):,} with a copybook chosen among several (to check the choice "
                                     "against the listing)" if chosen else "")
                                  + ", and texts the index cannot place"))
    if not sources:
        log("none of the expanded texts belongs to a program that copies a missing copybook"
            + (" (or one held only as a recovered copy)" if recovered else "")
            + (" or has a copybook chosen among several" if chosen else "") + ": are these the "
            "listings of the programs the coverage report calls partial?")
        return early({"missing": len(missing), "sources": 0, "written": 0, "rejected": 0, "not_found": len(missing),
                      "removed": len(removed), "kept": 0, "formats": {}, "out": out_dir, "unconfirmed": 0, "per_system": 0})
    wanted = set(missing) if not everything else set()
    formats: Counter = Counter()
    by_name: Dict[str, List[Region]] = defaultdict(list)
    nested_in: Dict[str, Counter] = defaultdict(Counter)                 # inner copybook -> {outer copybook: listings}
    unprovable: Dict[str, List[str]] = defaultdict(list)                 # copybook -> older listings naming it, column unproven
    unprovable_why: Dict[str, str] = {}                                  # listing -> why its column could not be proven
    unattached = 0
    lined_up = 0
    look_untied: List[Tuple[str, int, int, str, list, str]] = []        # (listing, untied line, program line before, its shape, lines before, why)
    columns: Counter = Counter()                                         # where the source starts, per listing
    copy_src: Dict[str, List[CopySource]] = {}                           # program -> the listing's copybook-source rows
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
        lines_ = split_lines(text)
        how = reading(lines_)
        fmt, regions, stats = extract(text, path, wanted or set(), original, system, lines=lines_, how=how)
        if how["kind"] in ("current", "older"):
            # a listing: its copybook-source table says which library each copybook came from (no table: an empty list,
            # stored as such, so the check says 'no table' rather than 'no listing read'); each row carries whether the
            # listing is the compile of the program as indexed - its own program lines against the member's - so what
            # an older compile's listing names is never counted as a wrong fact (LESSONS 193)
            table = copy_sources(lines_)
            cur: Optional[bool] = None
            matched: Optional[int] = None
            if table and original is not None:
                recs, _off = listing_records(lines_, how["old"] if how["kind"] == "older" else None)   # type: ignore[arg-type]
                cur, matched = listing_is_current(recs, original)
            copy_src.setdefault(stem.upper(), []).extend((c, d, ds, path, cur, matched) for c, d, ds, _n in table)
        del lines_
        formats[fmt] += 1
        unattached += stats.get("flagged lines with no COPY before them", 0)
        if stats.get("source column"):
            columns[(stats["source column"], "ruler" if stats.get("ruler line")
                     else "older layout, proven" if stats.get("older layout") else "guessed")] += 1
        if fmt == UNPROVABLE:
            for n in stats.get("named", []):                             # type: ignore[union-attr]
                unprovable[n].append(path)
            unprovable_why[path] = str(stats.get("why", ""))
        if stats.get("untied") and len(look_untied) < 2:
            first = stats["untied"][0]                                  # type: ignore[index]
            look_untied.append((path, first[0], first[1], first[2], first[3], first[4]))
        if "lined up" in fmt:
            lined_up += 1
        for r in regions:
            for inner in r.nested:                                       # a COPY inside the block: the inner copybook's
                nested_in[inner.upper()][r.name] += 1                    # lines sit inside this block, unmarked as its own
            if everything or r.name in missing:
                by_name[r.name].append(r)
    log("formats seen: " + ", ".join(f"{f} in {n:,}" for f, n in formats.most_common()))
    if formats.get("unreadable"):
        log(f"  {formats['unreadable']:,} could not be read from disk - is the estate where the build saw it?")
    if columns:
        log("  source column: " + ", ".join(f"{col} ({how}) in {n:,}" for (col, how), n in columns.most_common(4)))
    if unattached:
        log(f"  {unattached:,} copied line(s) could not be tied to a COPY statement - the report's 'Please look' "
            "section names a listing and the line to open")

    written: List[Tuple[str, str, str]] = []                            # (name, folder, how)
    rejected: List[Tuple[str, str]] = []
    unconfirmed: List[Tuple[str, str]] = []
    kept = 0
    kept_names: List[str] = []
    have_now = {n for ents in entries.values() for n in ents}
    for name in sorted(by_name):
        if name in real and not everything:
            continue
        if name in have_now and not refresh:
            kept += 1
            kept_names.append(name)
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
            kind, why_kind = classify.classify(os.path.join(folder, name + ".cpy"), body[:8192])
            misread = ""
            if kind not in ("copybook", "cobol"):
                # the classifier reads a line of the copybook as another kind (a START- name as Assembler, LESSONS
                # 191): written all the same when this tool could re-file it afterwards - which it does on the run
                # after the build - and refused when the text really has that kind's shape
                read_as = {"kind": kind, "reason": why_kind, "by": "content", "text": body, "line": 0, "word": ""}
                ok, why_not = refile_verdict(read_as, FOLDER)
                if not ok:
                    rejected.append((name, f"the build would file this as {kind} ({why_not}): " + shape_of(pick.records) + where))
                    continue
                misread = kind
            written.append((name, folder, how + f"; {shape} copybook, {len(pick.records)} lines"
                            + (f"; the build's classifier reads it as {misread} by a line of its text ({REFILED_ITEM}): "
                               "run this tool again after the build - it re-files it - then build once more" if misread else "")))
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
                                                        "lines": len(pick.records),
                                                        **({"misread_as": misread} if misread else {})}
    if not dry_run:
        for d, ents in entries.items():
            if ents or os.path.isdir(d):
                _save_marker(d, ents)
    # the listings' copybook-source rows, stored per program read, and every choice the build made checked against them
    # ... and, from the same rows, the libraries the listings name: the fetch list
    checks, fetch, unnamed = _check_after(db, copy_src, dry_run, to_fetch_for, bool(chosen), recovered)
    list_file = os.path.join(os.path.dirname(report) or ".", FETCH_LIST) if report else None
    seen_names = set(by_name) | have_now
    unread = sorted(n for n in missing if n not in seen_names and n in unprovable)   # in a listing, column unproven
    not_found = sorted(n for n in missing if n not in seen_names and n not in unprovable)
    no_marks = sum(n for f, n in formats.items() if "no copy marks" in f or "not flagged" in f)
    misread_written = {n: ents[n]["misread_as"] for _d, ents in entries.items() for n in ents
                       if ents[n].get("misread_as") and any(w[0] == n for w in written)}
    if dry_run:                                                        # nothing entered the marker: read the note instead
        misread_written = {n: h.split("classifier reads it as ")[1].split(" ")[0] for n, _f, h in written
                           if "classifier reads it as " in h}

    lines = [f"# Recovered copybooks - {time.strftime('%Y-%m-%d %H:%M')}\n",
             f"\n- missing in the index: {len(missing)}; expanded texts read: {len(sources)}; formats: "
             + ", ".join(f"{f} in {n}" for f, n in formats.most_common()),
             f"\n- written: {len(written)}" + (" (dry run: nothing written)" if dry_run else "")
             + f"; already there: {kept}; unconfirmed: {len(unconfirmed)}; rejected: {len(rejected)}; removed (real member arrived): {len(removed)}",
             f"\n- not in any expanded text: {len(not_found)}"
             + (f"; in an older listing whose source column could not be proven: {len(unread)}" if unread else "") + "\n"]
    lines += disk_lines                                                 # every missing name, looked for on disk
    lines += arrival_lines                                              # arrived after the parse / filed as another kind
    if written:
        lines.append("\n## Written\n\n| copybook | programs copying it | folder | how |\n|---|---|---|---|\n")
        lines += [f"| {n} | {missing.get(n, 0)} | {os.path.relpath(f, root) if root else f} | {how} |\n" for n, f, how in written]
    if unconfirmed:
        lines.append("\n## Seen, not written - needs a second program to agree\n\n| copybook | why |\n|---|---|\n")
        lines += [f"| {n} | {why} |\n" for n, why in unconfirmed]
    if rejected:
        lines.append("\n## Rejected\n\n| copybook | why |\n|---|---|\n")
        lines += [f"| {n} | {why} |\n" for n, why in rejected]
    nested_only = [n for n in not_found if nested_in.get(n)]
    handled = {str(e["copybook"]) for e in list(refiled) + list(misfiled) + list(waiting)}   # said in their own sections
    stale = [n for n in kept_names if n in missing and n not in handled]   # recovered earlier, the index still lacks it
    if not_found:
        lines.append("\n## Not in any expanded text (fetch these libraries)\n\n"
                     "A copybook copied only from inside another copybook has no COPY statement in any program: its lines "
                     "sit inside the outer copybook's block in the listing, which this tool does not cut apart.\n\n"
                     "| copybook | copied by | used in | seen inside another copybook's block |\n|---|---|---|---|\n")
        for n in not_found:
            p, b = copiers.get(n, (missing[n], 0))
            inside = ", ".join(f"{o} ({k} listing{'s' if k != 1 else ''})" for o, k in nested_in[n].most_common(3)) if nested_in.get(n) else "-"
            lines.append(f"| {n} | {p} program{'s' if p != 1 else ''}, {b} copybook{'s' if b != 1 else ''} | "
                         f"{users.get(n, '?')} | {inside} |\n")
    if fetch or to_fetch_for:
        lines += fetch_report(fetch, unnamed, root, list_file.replace("\\", "/") if list_file else FETCH_LIST)
    if unread:
        lines.append("\n## In an older listing whose source column could not be proven\n\nThe copybook IS in the listing, but "
                     "the tool could not prove at which column the listing's records start, so it read nothing rather than "
                     "risk a copybook shifted by a column. `--trace PROGRAM` shows what it saw in one listing.\n\n"
                     "| copybook | listings | first listing | why |\n|---|---|---|---|\n")
        for n in unread:
            first = unprovable[n][0]
            lines.append(f"| {n} | {len(unprovable[n])} | {os.path.basename(first)} | {unprovable_why.get(first, '')} |\n")
    if stale:
        lines.append("\n## Recovered on an earlier run, still missing in the index\n\nThe file exists but the index does not "
                     "list it: either the build has not run since, or the build did not see the folder (it must sit under the "
                     "estate root the build command names).\n\n| copybook | file |\n|---|---|\n")
        for n in stale:
            where = next((os.path.join(d, n + ".cpy") for d, ents in entries.items() if n in ents), "?")
            lines.append(f"| {n} | {os.path.relpath(where, root) if root and where != '?' else where} |\n")
    if checks:
        lines += choice_report(checks, root)
    if look_untied or rejected:
        lines.append("\n## Please look\n\nOpen the listing named below in VS Code and go to the line (Ctrl+G). "
                     "Answer in words and numbers only - nothing from the file needs to be copied.\n")
    for path, at, before, shape, recent, why in look_untied:
        lines.append(f"\n### Copied lines with no COPY statement found before them\n\n- listing: `{path}`\n"
                     f"- line {at}: the first copied line (a C after its line number) that nothing claimed\n"
                     f"- line {before}: the last program line before it; its shape (letters A, digits 9): `{shape}`\n"
                     f"- why the tool had no COPY in hand: {why}\n"
                     "- the numbered lines just before it, as the tool read them (mark after the line number, then the "
                     "record with letters A and digits 9):\n")
        lines += [f"    - line {ln}: mark `{mark or '(none)'}`  record `{rec}`\n" for ln, mark, rec in recent]
        lines.append("- Please paste these lines back as they are: they carry no names.\n")
    for name, why in rejected[:2]:
        lines.append(f"\n### Rejected block {name}\n\n- {why}\n- Questions: open the listing at that line: (1) at what "
                     "column does the copied record start there, against the column on the IDENTIFICATION DIVISION line? "
                     "(2) is the first copied line a data item (a level number and a name), a comment, or something else?\n")
    to_fetch = sum(1 for e in fetch if not e["held"])
    if report:
        _write_report(report, lines)
    if list_file:
        _write_fetch_list(list_file, fetch)                             # rewritten each run: never stale, never a member name
    agree =sum(1 for _n, _f, how in written if "identical in" in how or "confirmed by a second" in how)
    differ = sum(1 for _n, _f, how in written if "different texts" in how)
    per_system = sum(1 for _n, _f, how in written if "differs between systems" in how)
    if check_only:
        pass                                                           # nothing was missing: said above, once
    elif not written and kept:
        log(f"nothing new to write: {kept:,} of {len(missing):,} missing copybooks were recovered on an earlier run"
            + (f"; {len(not_found):,} in no expanded text" if not_found else ""))
        log("next: run your usual build command if you have not since - the programs that copy them re-expand by themselves")
    else:
        log(f"recovered: {len(written):,} of {len(missing):,} missing copybooks"
            + (" (dry run - nothing written)" if dry_run else f" -> {out_dir}")
            + f"; {agree:,} confirmed by 2+ programs, {differ:,} differ between programs (most common taken)"
            + (f", {per_system:,} written per system" if per_system else "")
            + f", {len(unconfirmed):,} unconfirmed, {len(rejected):,} rejected, {len(not_found):,} in no expanded text"
            + (f", {len(unread):,} in older listings whose source column could not be proven (see the report)" if unread else "")
            + (f"; {lined_up:,} text(s) read by lining up with the program" if lined_up else "")
            + (f"; {kept:,} already recovered earlier" if kept else ""))
    if misread_written:
        kinds = ", ".join(sorted(set(misread_written.values())))
        log(f"  {len(misread_written):,} of them carr{'ies' if len(misread_written) == 1 else 'y'} a line the build's classifier "
            f"misreads ({kinds}): after the build, run this tool once more - it re-files them in the index - then build again "
            f"({REFILED_ITEM})")
    if checks:
        n_contra = choice_counts(checks)[1]
        log(choice_line(checks) + (" - every choice contradicted by a current listing is a wrong fact in the index: the "
                                   "report names each one with the library the listing says" if n_contra else ""))
    if fetch:
        log(f"libraries the listings name: {len(fetch):,} datasets, {to_fetch:,} not fetched yet - the report's fetch list "
            "names them" + (f"; {list_file} holds the {to_fetch:,} to fetch, one dataset per line, for the UI's Bulk add"
                            if list_file and to_fetch else ""))
    if unnamed and (fetch or not_found or recovered):
        log(f"  {len(unnamed):,} copybook{'s' if len(unnamed) != 1 else ''} to fetch for {'are' if len(unnamed) != 1 else 'is'} "
            "named by no listing's table")
    if nested_only:
        log(f"  {len(nested_only):,} of the {len(not_found):,} not found are copied from INSIDE another copybook: their lines sit "
            "inside that copybook's block in the listings, which this tool does not cut apart yet - the report's table names the "
            "outer copybook")
    if stale:
        log(f"  {len(stale):,} recovered on an earlier run are still missing in the index: if the build has run since, it did not "
            "see the file - the report lists each file; the recovered folder must sit under the estate root the build names")
    if no_marks and not_found:
        log(f"  {no_marks:,} expanded text(s) show the COPY statements but not the copied lines and could not be lined up "
            "with a program in the index: listings compiled with the copybook text, or the programs themselves, would close more")
    if report:
        log(f"every name: {report}")
    if written and not dry_run:
        log("next: run your usual build command - the programs that copy them re-expand by themselves"
            + (" (and the programs marked above)" if marked or marked_removed or marked_refiled or waiting else ""))
    elif marked or marked_removed or marked_refiled or waiting:
        log("next: run your usual build command - the programs marked re-expand by themselves")
    if folder_fix:
        log(MISFILED_NEXT)
    if content_folder:
        log(NEXT_FOLDER_REFILE)
    return {"missing": len(missing), "sources": len(sources), "written": len(written), "rejected": len(rejected),
            "not_found": len(not_found), "removed": len(removed), "kept": kept, "formats": dict(formats),
            "out": out_dir, "unconfirmed": len(unconfirmed), "per_system": per_system,
            "checked": choice_counts(checks) if checks else None,
            "fetch": (len(fetch), to_fetch), "unnamed": len(unnamed), "misread": len(misread_written),
            "arrived": len(arrived), "misfiled": len(misfiled), "refiled": 0 if dry_run else len(refiled),
            "waiting": len(waiting), "marked": marked + marked_removed + marked_refiled,
            "on_disk": disk_stats["on_disk"], "arrived_late": disk_stats["arrived_late"], "no_file": disk_stats["no_file"]}


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
            f"ruler in the first 400 lines: {'yes' if any(_ruler_at(ln) for ln in lines[:400]) else 'no'}; "
            f"numbered lines in the first 2,000: {sum(1 for ln in lines[:2000] if _LISTING_SHAPE.match(ln)):,}")
        how = reading(lines)
        old = how["old"] if how["kind"] == "older" else None
        n_old = sum(1 for ln in lines if _OLD_LINE.match(ln))
        log(f"   lines starting with a five-digit number (an older compiler's layout): {n_old:,}; page headers of that "
            f"layout: {sum(1 for ln in lines if _OLD_PAGE.match(ln)):,}; its banner: "
            f"{'yes' if any(_OLD_BANNER.match(ln) for ln in lines) else 'no'}")
        log(f"   read as: {'an older compiler listing' if old else 'a compiler listing' if how['kind'] == 'current' else 'an expanded source'}")
        if old is not None:
            log(f"   older compiler listing: file lines {int(old['start']) + 1}-{int(old['end']) + 1} ({int(old['lines']):,} numbered "
                f"lines in that run; {int(old['runs']):,} numbering run(s) in the file); source column: {int(old['off']) + 1} "
                f"({old['how']}); copy marks: {int(old['marks']):,}")
            recs = listing_records(lines, old)[0]                                          # type: ignore[arg-type]
        else:
            if n_old >= OLD_MIN_LINES:
                log(f"   not taken as an older listing: {older_why_not(lines)}")
            ruler_line = next((i for i, ln in enumerate(lines, 1) if _ruler_at(ln)), 0)
            off = listing_offset(lines) if how["kind"] == "current" else None
            log(f"   ruler: {'line ' + str(ruler_line) if ruler_line else 'none'}; source column: "
                f"{off + 1 if off is not None else 'not found'}")
            recs = listing_records(lines)[0] if off is not None else []
        if recs:
            first_l = recs[0][2]
            last_l = recs[-1][2]
            if old is not None:
                log(f"   taken as source: {len(recs):,} numbered lines (file lines {first_l}-{last_l}); the reading ends where "
                    f"the numbering run ends, file line {int(old['end']) + 1} of {len(lines):,}")
            else:
                numbered_all = sum(1 for ln in lines if _LISTING_LINE.match(ln))
                stop = next((i for i, ln in enumerate(lines[last_l:], last_l + 1) if _section_heading(ln)), 0)
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
