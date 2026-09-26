"""
query.py - answer questions from atlas.db as Markdown, with citations.
No model calls. Every line of output comes from a row in the index.

    python -m atlas.query program  SAMPPGM
    python -m atlas.query job      SAMPJOB
    python -m atlas.query callers  RATECALC --depth 3
    python -m atlas.query callees  SAMPPGM --depth 2
    python -m atlas.query field    PM-POLICY-STATUS
    python -m atlas.query literal  E001
    python -m atlas.query dataset  PROD.POLICY.EXTRACT
    python -m atlas.query copybook PMASTREC
    python -m atlas.query dead
    python -m atlas.query ambiguous
    python -m atlas.query coverage
    python -m atlas.query search   "WS-PLCY-STAT-CD"
    python -m atlas.query cite     SAMPPGM 25-30
    python -m atlas.query pack     SAMPPGM          # fact pack for a model

Program facts carry EXPANDED line numbers (COPY members inlined). Citations
printed here are always translated back to  MEMBER:line  in the original
member, which is what verify_citations.py checks against.

Every report ends with the unresolved items in its scope. An answer that
omits them is incomplete by construction, not merely by accident.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import re

from . import cobol, expand, reader, screens
from . import flow   # the `flow` engine; it imports this module back, both at run time only


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def connect(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit(f"no such db: {path}  (build it: python -m atlas.build ROOT --db {path})")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _jl(v: Optional[str]) -> list:
    try:
        return json.loads(v) if v else []
    except (TypeError, ValueError):
        return []


def origin(conn: sqlite3.Connection, program_id: int, exp_line: Optional[int]
           ) -> Tuple[Optional[str], Optional[int], int, Optional[str]]:
    """(member_tag, source_line, depth, via_copy) for an expanded line. The
    tag is the member name, or SYSTEM/NAME when the member belongs to a
    system - so that with two copies of a program (GC and GC-DEV) a cite
    and a source lookup reach THIS program's copy, not the first by path."""
    if exp_line is None:
        return None, None, 0, None
    r = conn.execute("""
        SELECT m.name, m.system, r.src_start + (? - r.exp_start) AS src_line, r.depth, r.via_copy
        FROM expand_run r JOIN member m ON m.id = r.src_member
        WHERE r.program_id = ? AND ? BETWEEN r.exp_start AND r.exp_end""",
        (exp_line, program_id, exp_line)).fetchone()
    if not r:
        return None, exp_line, 0, None
    tag = f"{r['system']}/{r['name']}" if r["system"] else r["name"]
    return tag, r["src_line"], r["depth"], r["via_copy"]


def _tag_member_id(conn: sqlite3.Connection, tag: str) -> Optional[int]:
    """The member a tag names: SYSTEM/NAME -> that system's copy; NAME -> the
    production copy first (the order the gate uses)."""
    if "/" in tag:
        system, _, name = tag.partition("/")
        r = conn.execute("SELECT id FROM member WHERE UPPER(COALESCE(system,''))=? AND UPPER(name)=? "
                         "ORDER BY authoritative DESC, path LIMIT 1", (system.upper(), name.upper())).fetchone()
    else:
        r = conn.execute("SELECT id FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC, path LIMIT 1",
                         (tag.upper(),)).fetchone()
    return r[0] if r else None


def cite(conn: sqlite3.Connection, program_id: int, exp_line: Optional[int]) -> str:
    m, ln, depth, via = origin(conn, program_id, exp_line)
    if not m:
        return f"?:{exp_line}"
    tag = f"{m}:{ln}"
    if depth:
        tag += f" (via COPY {via})"
    return tag


def _spans(conn: sqlite3.Connection, member_id: int) -> Optional[list]:
    """The rowid ranges of a member's search rows, or None on an index built
    before they were recorded (then a scan is the only way)."""
    try:
        rows = conn.execute("SELECT lo, hi FROM fts_span WHERE member_id=? ORDER BY lo", (member_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    return [(r[0], r[1]) for r in rows] if rows else None


def _code_member_id(conn: sqlite3.Connection, tag: str) -> Optional[int]:
    """Like _tag_member_id, preferring the COBOL / copybook copy of a name (a
    program and a job can share one)."""
    order = "CASE kind WHEN 'cobol' THEN 0 WHEN 'copybook' THEN 1 ELSE 2 END, authoritative DESC, path"
    if "/" in tag:
        system, _, name = tag.partition("/")
        r = conn.execute(f"SELECT id FROM member WHERE UPPER(COALESCE(system,''))=? AND UPPER(name)=? ORDER BY {order} LIMIT 1",
                         (system.upper(), name.upper())).fetchone()
    else:
        r = conn.execute(f"SELECT id FROM member WHERE UPPER(name)=? ORDER BY {order} LIMIT 1", (tag.upper(),)).fetchone()
    return r[0] if r else None


def _fts_line(conn: sqlite3.Connection, member_tag: str, line: int) -> Optional[str]:
    mid = _code_member_id(conn, member_tag)
    spans = _spans(conn, mid) if mid is not None else None
    if spans:
        for lo, hi in spans:                      # a range read: fast whatever the size of the index
            r = conn.execute("SELECT text FROM src_fts WHERE rowid BETWEEN ? AND ? AND line_no=? LIMIT 1",
                             (lo, hi, line)).fetchone()
            if r:
                return r["text"]
        return None
    if "/" in member_tag:
        if mid is None:
            return None
        r = conn.execute("SELECT text FROM src_fts WHERE member_id=? AND line_no=? LIMIT 1", (mid, line)).fetchone()
    else:
        r = conn.execute("SELECT text FROM src_fts WHERE member_name=? AND line_no=? LIMIT 1",
                         (member_tag, line)).fetchone()
    return r["text"] if r else None


def source_line(conn: sqlite3.Connection, member_name: str, line: int) -> str:
    t = _fts_line(conn, member_name, line)
    return t.strip() if t else ""


def source_line_raw(conn: sqlite3.Connection, member_name: str, line: int) -> str:
    """The code area as written (columns 8-72), indentation kept - the IF /
    ELSE nesting an analyst reads by eye."""
    t = _fts_line(conn, member_name, line)
    return t.rstrip() if t else ""


def programs_named(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    """Programs called `name`: by PROGRAM-ID, member name, or an ENTRY alias
    (CALL 'RATEENT' reaches RATECALC). `SYSTEM/NAME` picks the copy in that
    system (GC-DEV/CLMPOST: the changed copy, not production's)."""
    system = None
    if "/" in name:
        system, _, name = name.partition("/")
    n = name.upper()
    rows = conn.execute("""
        SELECT p.*, m.name AS member_name, m.path, m.library, m.authoritative, m.parse_status, m.system
        FROM program p JOIN member m ON m.id = p.member_id
        WHERE UPPER(p.program_id) = ? OR UPPER(m.name) = ?
           OR p.id IN (SELECT program_id FROM program_alias WHERE UPPER(alias) = ?)
        ORDER BY m.authoritative DESC, m.path""", (n, n, n)).fetchall()
    if system:
        rows = [r for r in rows if (r["system"] or "").upper() == system.upper()]
    return rows


def _names_of(conn: sqlite3.Connection, name: str) -> List[str]:
    """A program plus every ENTRY alias it answers to."""
    n = name.upper()
    out = {n}
    for p in programs_named(conn, n):
        out.add(p["program_id"].upper())
        for (a,) in conn.execute("SELECT alias FROM program_alias WHERE program_id=?", (p["id"],)):
            out.add(a.upper())
    return sorted(out)


def table(headers: Sequence[str], rows: Iterable[Sequence]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
    return "\n".join(out) + "\n"


def clip(text: Optional[str], n: int) -> str:
    """`text` whole when it fits in `n` characters, else cut at the last space
    before and ' ...' said: a note cut at a fixed width ended mid-word ('the
    facts after it were r') and read as if that were all it said (LESSONS 212)."""
    text = text or ""
    if len(text) <= n:
        return text
    cut = text.rfind(" ", 0, n - 3)
    return (text[:cut] if cut > n // 2 else text[:n - 4]).rstrip(" ,;:-") + " ..."


def unresolved_line_cell(conn: sqlite3.Connection, program_id: Optional[int], line: Optional[int]):
    """The line an Unresolved row names, as the member's own: a COBOL parser
    note is stored at its line in the EXPANDED text, so after a COPY the
    number was not the member's line (a dynamic_call at member line 9 after a
    5-line copybook read 15; the exec_no_period row named line 6 in its
    sentence and 12 in this column - LESSONS 212). A line a copybook brought in
    is `BOOK:N (via COPY BOOK)`. A member with no program row (a copybook, a
    job, a map) stores its own lines; an index without the line map keeps the
    stored number."""
    if line is None or program_id is None:
        return line
    tag, src, depth, via = origin(conn, program_id, line)
    if tag is None:
        return line
    return f"{tag}:{src} (via COPY {via})" if depth else src


# The resolver's own wording when several members share a copybook's name
# with different content and one was chosen (build.make_resolver): the
# 'ambiguous_copybook' row carries it. An index built BEFORE ROADMAP re-parse
# item 18 carries it a second time as an 'expand' note ("L12: COPY X: 2 copies
# of X with different content; used PATH (how)": expand.py repeated it as a
# COPY warning and build.py stored every warning as a gap), which marks the
# program `partial` although every COPY expanded; since the batch the build
# hands the expander no note and such a program is `ok` with the row alone.
# Every query-side reader of parse_status='partial' tells the two apart
# through partial_kind(), never by the status column alone (LESSONS 181), and
# chose_a_copybook() reads the choice on either shape.
AMBIGUOUS_PICK = " with different content; used "
PARSE_CHOSEN = "complete (copybook chosen among several - see notes)"
# SQL: member alias `m` is marked partial only by the resolver's pick - every
# 'expand' note is that note (one bound parameter: AMBIGUOUS_PICK); an index
# built before ROADMAP re-parse item 18
_CHOSEN_PRED = """EXISTS (SELECT 1 FROM unresolved u WHERE u.member_id = m.id AND u.kind = 'expand')
    AND NOT EXISTS (SELECT 1 FROM unresolved u WHERE u.member_id = m.id AND u.kind = 'expand'
                    AND instr(COALESCE(u.detail, ''), ?) = 0)"""
# SQL: member alias `m` is `ok` with an 'ambiguous_copybook' row - the choice as the build of the batch records
# it. An IN over the rows of that kind, not an EXISTS per member: `unresolved` is indexed by kind only, SQLite
# materialises the (few thousand) rows once, and `coverage` walks his ~121k members against that set
_CHOSEN_OK_PRED = """m.parse_status = 'ok' AND m.id IN (SELECT u.member_id FROM unresolved u
    WHERE u.kind = 'ambiguous_copybook')"""
# SQL: member alias `m` is complete with a copybook chosen among several, on either shape of the index (one bound
# parameter: AMBIGUOUS_PICK)
_COMPLETE_WITH_CHOICE = f"((m.parse_status = 'partial' AND {_CHOSEN_PRED}) OR ({_CHOSEN_OK_PRED}))"


def partial_kind(conn: sqlite3.Connection, member_id: int) -> Optional[str]:
    """None: the member is not marked partial - an `ok` program that took a
    copybook chosen among several (the build of the batch) is here, and
    chose_a_copybook() says so. 'chosen': marked partial only because a
    copybook was chosen among several same-named ones (every 'expand' note is
    the resolver's pick - an index built before ROADMAP re-parse item 18) -
    the program expanded COMPLETELY with the copy the 'ambiguous_copybook'
    row names. 'partial': parsed only in part (a copybook NOT FOUND, a COPY
    skipped as recursive or too deeply nested, a scan with no text, an
    unrecognised map)."""
    row = conn.execute("SELECT parse_status FROM member WHERE id=?", (member_id,)).fetchone()
    if not row or row[0] != "partial":
        return None
    n_exp, n_true = conn.execute("""SELECT COUNT(*), COALESCE(SUM(instr(COALESCE(detail, ''), ?) = 0), 0)
                                    FROM unresolved WHERE member_id=? AND kind='expand'""",
                                 (AMBIGUOUS_PICK, member_id)).fetchone()
    return "chosen" if n_exp and not n_true else "partial"


def is_truly_partial(conn: sqlite3.Connection, member_id: int) -> bool:
    """The member's facts are incomplete (not merely a copybook chosen among several)."""
    return partial_kind(conn, member_id) == "partial"


def chose_a_copybook(conn: sqlite3.Connection, member_id: int) -> bool:
    """The member carries an 'ambiguous_copybook' row: a copybook it copies
    exists in several libraries with different content and the build
    expanded one copy (the row names it and says how). A decision, not a
    gap - since ROADMAP re-parse item 18 such a program is `ok`."""
    return conn.execute("SELECT 1 FROM unresolved WHERE member_id=? AND kind='ambiguous_copybook' LIMIT 1",
                        (member_id,)).fetchone() is not None


def parse_label(conn: sqlite3.Connection, member_id: int, status: Optional[str]) -> str:
    """The parse status as a header line states it: `ok`, `partial`, or the
    chosen-among-several wording for a member complete with a copybook chosen
    among several - `ok` with its 'ambiguous_copybook' row (built by the
    re-parse batch) or `partial` only by the resolver's note (built before
    it): the same words on both, so a program's header does not change with
    the re-parse."""
    kind = partial_kind(conn, member_id)
    if kind == "chosen" or (kind is None and status == "ok" and chose_a_copybook(conn, member_id)):
        return PARSE_CHOSEN
    return status or "?"


# a program marked `ok` whose COPY row no member resolves any more: the build un-links a program's copy_use row
# when the member it had expanded leaves the index (its text changed on disk, or the file went), and the build before
# ROADMAP re-parse item 21 parsed the program again only when the new text was filed as copybook or cobol - on an
# index built before ROADMAP re-parse items 20 and 22, a copybook atlas.recover re-filed and then re-fetched with new
# text was filed by its weak signature again, so the program read 'ok' with the fields of the earlier read and a NULL
# row beside 'NOT FOUND' on the same page (LESSONS 188); said here and counted in coverage, apart from the partial
# members. The same build left it for a copybook recorded again under a new id with its text unchanged - marked
# pending, a build stopped before it was parsed, a parser exception (LESSONS 201, 202). The build of this toolkit
# parses the program again whenever the member goes, changes, is re-typed or recorded again (build.moved_names), so on
# an index it built nothing is said
UNLINKED_WHY = ("the member it had expanded went out of the index since - its text changed on disk and the classifier "
                "filed the new text as another kind, the file went, or it was recorded again under a new id with its "
                "text unchanged - and the build that made this index did not parse the program again")
UNLINKED_FIX = "run `python -m atlas.recover --db atlas.db`, then the build"


def unlinked_why(programs: int, books: int) -> str:
    """UNLINKED_WHY in the number of the sentence it sits in: one program and
    one copybook keep the pinned wording."""
    if programs == 1 and books == 1:
        return UNLINKED_WHY
    member = "the member it had expanded" if books == 1 else "the members they had expanded" if programs != 1 \
        else "the members it had expanded"
    text = "its text changed" if books == 1 else "their text changed"
    file = "the file went" if books == 1 else "the files went"
    again = "it was recorded again under a new id with its text" if books == 1 else \
        "they were recorded again under a new id with their text"
    program = "the program" if programs == 1 else "the programs"
    return (f"{member} went out of the index since - {text} on disk and the classifier filed the new text as another "
            f"kind, {file}, or {again} unchanged - and the build that made this index did not parse {program} again")


def unlinked_ok_note(conn: sqlite3.Connection, member_id: int, status: Optional[str]) -> str:
    """`program`'s clause after 'parse: ok' when a COPY row of the program is
    unresolved although the parse was whole: its fields are those of the
    earlier read of that copybook (recover.unlinked_ok_programs)."""
    if status != "ok":
        return ""
    from . import recover
    entry = recover.unlinked_ok_programs(conn, member_id).get(int(member_id))
    if not entry:
        return ""
    books = entry[1]
    n = len(books)
    return (f", but {n} COPY row{'s' if n != 1 else ''} {'is' if n == 1 else 'are'} unresolved ({', '.join(books)}): "
            f"{unlinked_why(1, n)}, so its fields are those of the earlier read of "
            f"{'that copybook' if n == 1 else 'those copybooks'}; {UNLINKED_FIX} (the Copybooks table below says what "
            "happened to each)")


def unlinked_ok_clause(stale: Dict[int, Tuple[str, List[str]]]) -> str:
    """`coverage`'s note under 'Members parsed only in part' for the programs
    recover.unlinked_ok_programs() found: not partial, not counted in that
    table, and not whole either."""
    if not stale:
        return ""
    n = len(stale)
    names = sorted(name for name, _b in stale.values())
    books = sorted({b for _n, bs in stale.values() for b in bs})
    return (f"\n> Not counted above: {n} program{'s' if n != 1 else ''} marked `ok` {'has' if n == 1 else 'have'} a COPY row "
            f"no member resolves any more ({', '.join(names[:8])}{f', +{n - 8:,} more' if n > 8 else ''}; "
            f"copybook{'s' if len(books) != 1 else ''} {', '.join(books[:8])}{f', +{len(books) - 8:,} more' if len(books) > 8 else ''}): "
            f"{unlinked_why(n, len(books))}, so {'its fields are' if n == 1 else 'their fields are'} those of the earlier "
            f"read. `program NAME` says so beside `parse: ok`; {UNLINKED_FIX} - the 'Copybooks not found' table names "
            "each copybook with what to do.\n")


def call_target_cell(target: Optional[str], via_var: Optional[str], resolved: Sequence[str]) -> str:
    """`program`'s Calls cell: the program called, or - for a CALL / LINK / XCTL / START that names a variable -
    the variable with the targets it resolves to (`WS-NEXT-PGM -> BILONL02`). A variable holding one literal
    resolves to a target of its own, and the cell printed that target alone: the variable a reader greps
    for was not on the page (the synthetic estate's BILONL01, `EXEC CICS XCTL PROGRAM(WS-NEXT-PGM)`)."""
    if via_var:
        return f"{via_var} -> {', '.join(resolved) or target or 'UNRESOLVED'}"
    return target or "UNRESOLVED"


def unresolved_for(conn: sqlite3.Connection, member_ids: Sequence[int], limit: int = 40) -> str:
    if not member_ids:
        return ""
    q = ",".join("?" * len(member_ids))
    # the 'expand' copy of the resolver's pick says what the member's own
    # 'ambiguous_copybook' row says: shown once, as the choice it is
    not_pick = "NOT (u.kind = 'expand' AND instr(COALESCE(u.detail, ''), ?) > 0)"
    rows = conn.execute(f"""
        SELECT m.id AS mid, m.name, u.kind, u.detail, u.line FROM unresolved u JOIN member m ON m.id = u.member_id
        WHERE u.member_id IN ({q}) AND {not_pick} ORDER BY u.kind, m.name LIMIT ?""",
                        (*member_ids, AMBIGUOUS_PICK, limit)).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM unresolved u WHERE u.member_id IN ({q}) AND {not_pick}",
                         (*member_ids, AMBIGUOUS_PICK)).fetchone()[0]
    if not total:
        return "\n### Unresolved in scope\n_none - but see `coverage` for estate-wide blind spots_\n"
    # each member's program row, whose line map turns a parser note's expanded line into the member's own
    mids = sorted({r["mid"] for r in rows})
    pid_of: Dict[int, int] = {}
    for mid, pid in conn.execute(f"SELECT member_id, id FROM program WHERE member_id IN ({','.join('?' * len(mids))}) "
                                 "ORDER BY id DESC", mids):
        pid_of[mid] = pid
    out = [f"\n### Unresolved in scope ({total}) - the answer is incomplete to this extent\n"]
    out.append(table(["member", "kind", "detail", "line"],
                     [(r["name"], r["kind"], clip(r["detail"], 160),
                       unresolved_line_cell(conn, pid_of.get(r["mid"]), r["line"])) for r in rows]))
    if total > limit:
        out.append(f"_...{total - limit} more_\n")
    return "".join(out)


# --------------------------------------------------------------------------
# program
# --------------------------------------------------------------------------

def cmd_program(conn: sqlite3.Connection, name: str) -> str:
    progs = programs_named(conn, name)
    if not progs:
        # No source - but the JCL, the CSD/stage-1 and other programs may still
        # know it. A load-module-only program is normal in a 40-year estate.
        n = name.upper()
        steps = conn.execute("""
            SELECT j.job_name, s.step_name, s.launcher, s.from_proc, m.name AS jm, s.line, pd.proc_name
            FROM step s LEFT JOIN job j ON j.id=s.job_id LEFT JOIN member m ON m.id=j.member_id
            LEFT JOIN proc_def pd ON pd.id=s.proc_id
            WHERE UPPER(s.effective_pgm)=?
              AND NOT (s.proc_id IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.from_proc=pd.proc_name))
            ORDER BY j.job_name, s.ordinal""", (n,)).fetchall()
        callers = _callers_of(conn, n)
        tx = conn.execute("SELECT tran_code, system FROM transaction_def WHERE UPPER(program)=?", (n,)).fetchall()
        # A control-card member (PARMLIB/CNTL) is asked about by name too:
        # "where is SRTCLM used" is a job question, not a program question.
        # Effective (expanded) steps carry the job name; a bare PROC row is
        # shown only when no job expands that PROC; a job step's own
        # //PROCSTEP.DD override is shown once, on the effective step.
        cards = conn.execute("""
            SELECT j.job_name, s.step_name, s.from_proc, d.dd_name, d.dsn_resolved, m.name AS jm, d.line, pd.proc_name
            FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
            LEFT JOIN member m ON m.id=j.member_id LEFT JOIN proc_def pd ON pd.id=s.proc_id
            WHERE UPPER(d.card_member)=?
              AND NOT (s.proc_id IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.from_proc=pd.proc_name))
              AND NOT (s.proc_called IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.job_id=s.job_id
                                                            AND x.parent_step=s.step_name))
            ORDER BY j.job_name, s.ordinal, d.line""", (n,)).fetchall()
        other = conn.execute("SELECT kind, path FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC",
                             (n,)).fetchall()
        if not steps and not callers and not tx and not cards and not other:
            # Listed by the host but never downloaded is a different answer
            # from "does not exist": the fetch record knows which.
            for lib in conn.execute("SELECT dataset, fetched_at, missing FROM library WHERE complete=0"):
                if n in [x.upper() for x in _jl(lib["missing"])]:
                    return (f"# {name}\n\n**NOT FETCHED** - `{lib['dataset']}` lists a member {n} (host listing "
                            f"{(lib['fetched_at'] or '?')[:10]}) but it was not downloaded. Fetch the library again "
                            f"before concluding anything about it.\n")
            return f"# {name}\n\n**NOT FOUND** - no member with this name, and nothing indexed runs or calls it.\n"
        if other:
            out = [f"# {n}\n\n**Not a program**: indexed as a `{other[0]['kind']}` member (`{other[0]['path']}`). "
                   f"What the estate knows about it:\n"]
            out.append(_copied_as_copybook(conn, n))
        else:
            out = [f"# Program {n}\n\n**Source not indexed** (no member with this PROGRAM-ID or name). "
                   f"What the estate knows about it:\n"]
        if cards:
            out.append("\n### Used as control cards by\n")
            out.append(table(["job / PROC", "step", "DD", "library(member)", "cite"],
                             [(c["job_name"] or f"(PROC {c['proc_name']})", c["step_name"], c["dd_name"],
                               c["dsn_resolved"], f"{c['from_proc'] or c['jm'] or c['proc_name']}:{c['line']}")
                              for c in cards]))
            out.append("Its text is loaded as that step's cards: `job <JOB>` shows the resolved program / sort fields.\n")
        if steps:
            out.append("\n### Runs in\n")
            out.append(table(["job", "step", "launcher", "cite"],
                             [(s["job_name"] or f"(PROC {s['proc_name']})", s["step_name"], s["launcher"],
                               f"{s['from_proc'] or s['jm'] or s['proc_name']}:{s['line']}") for s in steps]))
        if tx:
            out.append("Online: " + ", ".join(f"{t['tran_code']} ({t['system']})" for t in tx) + "\n")
        if callers:
            out.append("\n### Called by\n")
            out.append(table(["caller", "kind"], callers))
        if not other:
            out.append("\n> Obtain the source or the compile listing to go further; until then its datasets, "
                       "tables and callees are unknown, not empty.\n")
        return "".join(out)
    out = [f"# Program {name.upper()}\n"]
    if len(progs) > 1:
        out.append(f"> **{len(progs)} copies indexed.** Which one is production is not knowable from "
                   f"the folder; declare it in a manifest. Facts below are per copy.\n")
    for p in progs:
        pid = p["id"]
        out.append(f"\n## {p['member_name']}  `{p['path']}`"
                   + ("  **[authoritative]**" if p["authoritative"] else "") + "\n")
        out.append(f"- PROGRAM-ID `{p['program_id']}` - {p['src_lines']} source lines, "
                   f"{p['exp_lines']} after COPY expansion - parse: "
                   f"{parse_label(conn, p['member_id'], p['parse_status'])}"
                   f"{unlinked_ok_note(conn, p['member_id'], p['parse_status'])}\n")
        out.append(f"- system: {p['system'] or 'not declared (add it to sources.json / manifest systems)'}\n")
        flags = [k for k in ("sql", "cics", "dli", "mq") if p[f"uses_{k}"]]
        out.append(f"- uses: {', '.join(flags) if flags else 'files only'}\n")
        lk = _jl(p["linkage_using"])
        if lk:
            out.append(f"- called with (positional): {', '.join(f'{i+1}={a}' for i, a in enumerate(lk))}\n")

        # jobs / steps
        # Effective (expanded) steps carry the job; a PROC's own row is shown only
        # when no indexed job expands that PROC.
        steps = conn.execute("""
            SELECT j.job_name, s.step_name, s.launcher, s.parm, s.from_proc, m.name AS jm, s.line,
                   pd.proc_name
            FROM step s LEFT JOIN job j ON j.id = s.job_id LEFT JOIN member m ON m.id = j.member_id
            LEFT JOIN proc_def pd ON pd.id = s.proc_id
            WHERE UPPER(s.effective_pgm) = ?
              AND NOT (s.proc_id IS NOT NULL AND EXISTS (SELECT 1 FROM step x WHERE x.from_proc = pd.proc_name))
            ORDER BY j.job_name, s.ordinal""", (p["program_id"].upper(),)).fetchall()
        out.append("\n### Runs in\n")
        out.append(table(["job", "step", "launcher", "parm/notes", "cite"],
                         [(s["job_name"] or f"(PROC {s['proc_name']}: no indexed job expands it)",
                           s["step_name"], s["launcher"], (s["parm"] or "")[:60],
                           f"{s['from_proc'] or s['jm'] or s['proc_name']}:{s['line']}") for s in steps]))
        tx = conn.execute("SELECT tran_code, system FROM transaction_def WHERE UPPER(program)=?",
                          (p["program_id"].upper(),)).fetchall()
        if tx:
            out.append("Online: " + ", ".join(f"{t['tran_code']} ({t['system']})" for t in tx) + "\n")

        # calls out
        calls = conn.execute("SELECT * FROM call_edge WHERE program_id=? ORDER BY line", (pid,)).fetchall()
        out.append("\n### Calls\n")
        out.append(table(["kind", "target", "resolution", "using", "cite"],
                         [(c["kind"], call_target_cell(c["target"], c["via_var"], _jl(c["resolved"])),
                           c["resolution"], ", ".join(_jl(c["using_args"]))[:50], cite(conn, pid, c["line"]))
                          for c in calls]))

        # callers
        callers = conn.execute("""
            SELECT q.program_id AS caller, c.kind, c.line, c.program_id AS cpid
            FROM call_edge c JOIN program q ON q.id = c.program_id
            WHERE UPPER(c.target) = ? OR c.resolved LIKE ?""",
            (p["program_id"].upper(), f'%"{p["program_id"].upper()}"%')).fetchall()
        out.append("\n### Called by\n")
        out.append(table(["caller", "kind", "cite"],
                         [(c["caller"], c["kind"], cite(conn, c["cpid"], c["line"])) for c in callers]))

        # copybooks
        cps = conn.execute("""
            SELECT c.copybook, c.replacing, c.line, m.name AS resolved, m.kind AS rkind, m.library AS rlib,
                   (SELECT u.detail FROM unresolved u WHERE u.member_id = c.resolved_member_id AND u.kind = 'declared_kind'
                    LIMIT 1) AS declared_over
            FROM copy_use c LEFT JOIN member m ON m.id = c.resolved_member_id
            WHERE c.member_id = ? ORDER BY c.line""", (p["member_id"],)).fetchall()
        out.append("\n### Copybooks\n")
        out.append(table(["copybook", "resolved to", "REPLACING", "line"],
                         [(c["copybook"], (unknown_cell(c["resolved"], c["rlib"]) if c["rkind"] == "unknown"
                                           else declared_over_cell(c["resolved"], c["declared_over"]) if c["resolved"]
                                           else _not_found_cell(conn, c["copybook"], p["member_id"])),
                           (c["replacing"] or "")[:40], f"{p['member_name']}:{c['line']}") for c in cps]))

        # files
        files = conn.execute("SELECT * FROM file_decl WHERE program_id=?", (pid,)).fetchall()
        frows = []
        for f in files:
            dsns = conn.execute("""
                SELECT DISTINCT d.dsn_resolved, d.mode, d.mode_source, j.job_name
                FROM dd d JOIN step s ON s.id = d.step_id LEFT JOIN job j ON j.id = s.job_id
                WHERE UPPER(s.effective_pgm)=? AND UPPER(d.dd_name) LIKE ?""",
                (p["program_id"].upper(), f"%{(f['assign_dd'] or '').upper()}")).fetchall()
            opens = conn.execute("SELECT DISTINCT op FROM io_op WHERE program_id=? AND target=?",
                                 (pid, f["select_name"])).fetchall()
            frows.append((f["select_name"], f["assign_dd"], f["organization"] or "",
                          ", ".join(o["op"] for o in opens),
                          "; ".join(f"{d['dsn_resolved']} [{d['mode']}/{d['mode_source']}] {d['job_name'] or ''}"
                                    for d in dsns) or "_no JCL found_",
                          cite(conn, pid, f["line"])))
        out.append("\n### Files (SELECT/ASSIGN -> JCL DD -> dataset)\n")
        out.append(table(["file", "DD", "org", "ops", "datasets via JCL", "cite"], frows))

        # EXEC CICS READ FILE('X') names an FCT entry; the CSD says which dataset.
        cf = conn.execute("""
            SELECT o.target, GROUP_CONCAT(DISTINCT o.op) AS ops, f.dsname
            FROM io_op o LEFT JOIN cics_file f ON UPPER(f.name)=UPPER(o.target)
            WHERE o.program_id=? AND o.target_kind='cics' GROUP BY o.target, f.dsname""", (pid,)).fetchall()
        if cf:
            out.append("\n### CICS files (FCT -> dataset, from the CSD)\n")
            out.append(table(["file", "ops", "dataset"],
                             [(c["target"], c["ops"], c["dsname"] or "**not in any indexed CSD**") for c in cf]))

        # DB2
        sqls = conn.execute("SELECT stmt_type, tables, is_dynamic, start_line FROM sql_stmt WHERE program_id=?",
                            (pid,)).fetchall()
        if sqls:
            agg: Dict[str, set] = defaultdict(set)
            first: Dict[str, int] = {}
            for s in sqls:
                for t in _jl(s["tables"]):
                    agg[t].add(s["stmt_type"])
                    first.setdefault(t, s["start_line"])
            out.append("\n### DB2\n")
            out.append(table(["table", "verbs", "first cite"],
                             [(t, ", ".join(sorted(v)), cite(conn, pid, first[t])) for t, v in sorted(agg.items())]))
            dyn = [s for s in sqls if s["is_dynamic"]]
            if dyn:
                out.append(f"**{len(dyn)} dynamic SQL statement(s)** - tables not statically knowable.\n")

        # IMS
        dli = conn.execute("""SELECT func, pcb_arg, io_area, ssa_args, line, resolution, dest, psb_name, dbd_name,
                                     procopt, pcb_source FROM dli_call WHERE program_id=? ORDER BY line""", (pid,)).fetchall()
        if dli:
            out.append("\n### IMS DL/I calls (PCB resolved through the PSB)\n")
            out.append(table(["func", "PCB arg", "database", "PROCOPT", "I/O area", "segment/SSA", "cite"],
                             [(d["func"] + (f" (via {d['resolution']})" if d["resolution"] in ("value_clause", "parmcount") else ""),
                               d["pcb_arg"] or "", d["dbd_name"] or "?", d["procopt"] or "", d["io_area"] or "",
                               ", ".join(_jl(d["ssa_args"]))[:40] or (f"-> {d['dest']}" if d["dest"] else ""),
                               cite(conn, pid, d["line"])) for d in dli]))
            srcs = sorted({d["pcb_source"] for d in dli if d["pcb_source"]})
            for s in srcs[:4]:
                out.append(f"- PCB mapping: {s}\n")
            if any(d["dbd_name"] is None for d in dli):
                out.append("> `database ?` = position not resolved (see the mapping notes); the model must not guess it.\n")
            switches = [d for d in dli if d["dest"]]
            if switches:
                out.append("- **message switch**: CHNG to " + ", ".join(sorted({d["dest"] for d in switches}))
                           + " - the ISRT that follows reaches that transaction/LTERM\n")

        # CICS commands: maps, transaction chaining, queues (facts, not name coincidence)
        cc = conn.execute("""SELECT verb, resource_kind, resource, direction, line FROM cics_cmd
                             WHERE program_id=? ORDER BY resource_kind, resource, line""", (pid,)).fetchall()
        if cc:
            out.append("\n### CICS commands (resources named in EXEC CICS)\n")
            out.append(table(["verb", "kind", "resource", "dir", "cite"],
                             [(c["verb"], c["resource_kind"], c["resource"], c["direction"] or "",
                               cite(conn, pid, c["line"])) for c in cc]))
        aliases = conn.execute("SELECT alias, linkage_using, line FROM program_alias WHERE program_id=?", (pid,)).fetchall()
        if aliases:
            out.append("- ENTRY points: " + ", ".join(f"`{a['alias']}` USING {', '.join(_jl(a['linkage_using'])) or '-'} "
                                                     f"@{cite(conn, pid, a['line'])}" for a in aliases) + "\n")

        # interfaces
        ifs = conn.execute("SELECT kind, detail, direction, line FROM interface_edge WHERE member_id=?",
                           (p["member_id"],)).fetchall()
        if ifs:
            out.append("\n### External interfaces\n")
            out.append(table(["kind", "detail", "dir", "line"],
                             [(i["kind"], i["detail"], i["direction"], i["line"]) for i in ifs]))

        # paragraphs
        paras = conn.execute("SELECT name, section, kind, start_line, end_line FROM paragraph WHERE program_id=? ORDER BY ordinal",
                             (pid,)).fetchall()
        # Reached by ANY edge kind: PERFORM, GO TO, ALTER, fall-through, a
        # THRU range, or a PERFORMed SECTION that contains the paragraph.
        edges = conn.execute("SELECT to_para, thru_para, kind FROM perform_edge WHERE program_id=?", (pid,)).fetchall()
        reached: Dict[str, set] = defaultdict(set)
        for e in edges:
            reached[e["to_para"].split(" OF ")[0]].add(e["kind"])
        names = [q["name"] for q in paras if q["kind"] == "paragraph"]
        for e in edges:
            if e["thru_para"] and e["to_para"] in names and e["thru_para"] in names:
                lo, hi = sorted((names.index(e["to_para"]), names.index(e["thru_para"])))
                for n_ in names[lo:hi + 1]:
                    reached[n_].add("thru")
        for q in paras:
            if q["kind"] == "paragraph" and q["section"] and q["section"] in reached:
                reached[q["name"]].add("section")
        plain = [q for q in paras if q["kind"] == "paragraph"]
        never = [q["name"] for q in plain[1:] if q["name"] not in reached]
        secs = sum(1 for q in paras if q["kind"] == "section")
        by_kind = defaultdict(int)
        for q in plain:
            for k in reached.get(q["name"], ()):
                by_kind[k] += 1
        out.append(f"\n### Structure\n- {len(plain)} paragraphs" + (f" in {secs} sections" if secs else "")
                   + f"; reached by: " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items()))
                   + f"; **{len(never)} reached by nothing** (no PERFORM, GO TO, fall-through or THRU range): "
                   f"{', '.join(never[:12])}{' ...' if len(never) > 12 else ''}\n")

        # literals set here
        lits = conn.execute("""
            SELECT literal, field, COUNT(*) n FROM literal_ref
            WHERE program_id=? AND context='move_to' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15""", (pid,)).fetchall()
        if lits:
            out.append("- codes/literals set here: " + ", ".join(f"'{l['literal']}'->{l['field']}" for l in lits) + "\n")

        out.append(unresolved_for(conn, [p["member_id"]]))
        out.append(_listing_says(conn, p["member_id"]))
    out.append(_docs_section(conn, name.upper()))
    return "".join(out)


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------

def cmd_job(conn: sqlite3.Connection, name: str) -> str:
    jobs = conn.execute("""
        SELECT j.*, m.name AS member_name, m.path, m.authoritative FROM job j JOIN member m ON m.id=j.member_id
        WHERE UPPER(j.job_name)=? OR UPPER(m.name)=? ORDER BY m.authoritative DESC""",
        (name.upper(), name.upper())).fetchall()
    procs = conn.execute("""
        SELECT p.*, m.name AS member_name, m.path FROM proc_def p JOIN member m ON m.id=p.member_id
        WHERE UPPER(p.proc_name)=? OR UPPER(m.name)=?""", (name.upper(), name.upper())).fetchall()
    incl = conn.execute("""SELECT m.name, j.job_name FROM include_use i JOIN member m ON m.id=i.member_id
                           LEFT JOIN job j ON j.member_id=m.id WHERE UPPER(i.include_member)=? ORDER BY m.name""",
                        (name.upper(),)).fetchall()
    if not jobs and not procs:
        if incl:
            return (f"# {name.upper()}\n\nAn INCLUDE member, not a job: its statements are spliced into "
                    + ", ".join(f"{r['job_name'] or r['name']}" for r in incl)
                    + f". Run `job <JOB>` for the effective steps; `cite {name.upper()} a-b` for its text.\n")
        return f"# {name}\n\n**NOT FOUND** - no job or PROC with this name is indexed.\n"
    out = [f"# Job {name.upper()}\n"]
    if incl:
        out.append("Included by (INCLUDE MEMBER=): " + ", ".join(r["job_name"] or r["name"] for r in incl) + "\n")
    for j in jobs:
        spliced = [r[0] for r in conn.execute("SELECT include_member FROM include_use WHERE member_id=? ORDER BY 1",
                                              (j["member_id"],))]
        if spliced:
            out.append(f"INCLUDE members spliced in: {', '.join(spliced)} (their DDs appear under the steps below)\n")
    sched = conn.execute("SELECT * FROM sched_dep WHERE UPPER(job_name)=? OR UPPER(depends_on)=?",
                         (name.upper(), name.upper())).fetchall()
    if sched:
        out.append("Scheduler: " + "; ".join(f"{s['job_name']} <- {s['depends_on']} ({s['kind']})" for s in sched) + "\n")
    else:
        out.append("> No scheduler export loaded: predecessor/trigger relationships are **unknown**, "
                   "not absent.\n")

    def render_step(s, member_name: str, job_for_children: Optional[int]) -> None:
        cite_member = s["from_proc"] or member_name
        tag = f"  (from PROC {s['from_proc']})" if s["from_proc"] else ""
        out.append(f"\n### {s['step_name']}{tag}  `{cite_member}:{s['line']}`\n")
        what = s["effective_pgm"] or (f"PROC {s['proc_called']}" if s["proc_called"] else "?")
        out.append(f"- runs **{what}**" + (f" (JCL says PGM={s['pgm']} - launcher)" if s["launcher"] else "")
                   + (f"; COND={s['cond']}" if s["cond"] else "") + "\n")
        if s["guard"]:
            out.append(f"- **runs only when** `{s['guard']}` - not the normal flow of the job\n")
        if s["parm"]:
            out.append(f"- PARM/notes: `{s['parm'][:200]}`\n")
        dds = conn.execute("SELECT * FROM dd WHERE step_id=? ORDER BY line, id", (s["id"],)).fetchall()
        drows = []
        for d in dds:
            if d["dsn_resolved"]:
                g = f"({d['gdg_rel']})" if d["gdg_rel"] else ""
                ovr = " (override)" if d["is_override"] else ""
                extra = ""
                if d["is_temp"]:
                    extra += " (job-local &&)"
                if d["dsn"] and d["dsn"].startswith("*."):
                    extra += f" (via {d['dsn']})"
                if d["card_member"]:
                    n = len(d["sysin_text"].strip().splitlines()) if d["sysin_text"] else 0
                    extra += (f" -> {n} card lines loaded from member" if n
                              else " -> card member NOT indexed: this step's cards are unknown")
                drows.append((d["dd_name"] or "  +concat", f"{d['dsn_resolved']}{g}{ovr}{extra}", d["disp"] or "",
                              f"{d['mode']} [{d['mode_source']}]"))
            elif d["sysin_text"]:
                first = d["sysin_text"].strip().splitlines()
                drows.append((d["dd_name"], f"inline cards ({len(first)} lines): "
                              + " / ".join(x.strip() for x in first[:3])[:90], "", "control"))
            elif d["dsn"]:
                why = "referback: target step/DD not found" if d["dsn"].startswith("*.") else "unresolved"
                drows.append((d["dd_name"], f"{d['dsn']} ({why})", d["disp"] or "", "unknown"))
        if drows:
            out.append(table(["DD", "dataset / cards", "DISP", "direction [source]"], drows))
        cards = conn.execute("SELECT card_kind, pos, length, fmt FROM card_field_ref WHERE step_id=? ORDER BY pos",
                             (s["id"],)).fetchall()
        if cards:
            out.append("- sort card byte positions: " + ", ".join(
                f"{c['card_kind']} {c['pos']}-{c['pos'] + c['length'] - 1} {c['fmt'] or ''}" for c in cards) + "\n")
        if s["proc_called"]:
            kids = conn.execute(
                "SELECT * FROM step WHERE job_id=? AND parent_step=? AND from_proc IS NOT NULL ORDER BY ordinal",
                (job_for_children, s["step_name"])).fetchall() if job_for_children else []
            if kids:
                out.append(f"- expands PROC {s['proc_called']}: effective steps below, with this job's "
                           f"symbolics and //STEP.DD overrides applied\n")
                for k in kids:
                    render_step(k, member_name, job_for_children)
            else:
                pd = conn.execute("SELECT p.id, m.name FROM proc_def p JOIN member m ON m.id=p.member_id "
                                  "WHERE UPPER(p.proc_name)=?", (s["proc_called"].upper(),)).fetchone()
                if pd:
                    out.append(f"- PROC {s['proc_called']} ({pd['name']}) is indexed but could not be expanded "
                               f"here; `job {s['proc_called']}` shows it with symbolics unresolved\n")
                else:
                    out.append(f"- **PROC {s['proc_called']} NOT FOUND** in the index - its steps are unknown\n")

    def render_steps(where: str, arg, member_name: str, job_for_children: Optional[int] = None) -> None:
        for s in conn.execute(f"SELECT * FROM step WHERE {where} ORDER BY ordinal", (arg,)).fetchall():
            render_step(s, member_name, job_for_children)

    mids = []
    for j in jobs:
        out.append(f"\n## {j['member_name']}  `{j['path']}`" + ("  **[authoritative]**" if j["authoritative"] else "") + "\n")
        if j["job_cond"]:
            out.append(f"JOB card COND={j['job_cond']}: applies to every step below.\n")
        if j["jcllib"]:
            out.append("JCLLIB ORDER: " + ", ".join(_jl(j["jcllib"])) + " (PROCs and INCLUDEs are searched there first)\n")
        if j["joblib"]:
            out.append("JOBLIB: " + ", ".join(_jl(j["joblib"])) + " - the load libraries of every step without "
                       "its own STEPLIB (shown per step as `[joblib]`)\n")
        inproc = conn.execute("SELECT proc_name FROM proc_def WHERE member_id=? AND instream=1 ORDER BY line",
                              (j["member_id"],)).fetchall()
        if inproc:
            out.append("Instream PROCs defined in this member (they shadow cataloged PROCs of the same name): "
                       + ", ".join(p["proc_name"] for p in inproc) + "\n")
        render_steps("job_id=? AND from_proc IS NULL", j["id"], j["member_name"], j["id"])
        temps = conn.execute("""
            SELECT d.dsn_resolved AS dsn, s.step_name, d.dd_name, d.mode FROM dd d JOIN step s ON s.id=d.step_id
            WHERE s.job_id=? AND d.is_temp=1 AND NOT (s.proc_called IS NOT NULL AND d.dd_name LIKE '%.%')
            ORDER BY d.dsn_resolved, s.ordinal, d.line""", (j["id"],)).fetchall()
        if temps:
            by: Dict[str, List[str]] = {}
            for t in temps:
                by.setdefault(t["dsn"], []).append(f"{t['step_name']} {t['dd_name']} ({t['mode']})")
            out.append("\n### Job-local (&&) datasets\n")
            out.append("These exist only while this job runs. No other job can read them, so they are "
                       "not dataset lineage and `dataset` hides them:\n")
            for dsn, uses in by.items():
                out.append(f"- `{dsn}`: " + " -> ".join(uses) + "\n")
        mids.append(j["member_id"])
    for p in procs:
        out.append(f"\n## PROC {p['proc_name']}  `{p['path']}`  symbolics: `{p['symbolics'] or '{}'}`\n")
        execs = conn.execute("""SELECT j.job_name, s.step_name, s.line, m.name AS mem FROM step s
                                JOIN job j ON j.id=s.job_id JOIN member m ON m.id=j.member_id
                                WHERE UPPER(s.proc_called)=? AND s.from_proc IS NULL ORDER BY j.job_name""",
                             (p["proc_name"].upper(),)).fetchall()
        if execs:
            out.append(f"Executed by ({len(execs)}): " + "; ".join(
                f"{e['job_name']} {e['step_name']} @{e['mem']}:{e['line']}" for e in execs[:40]) + "\n")
            out.append("The steps below carry the PROC's DEFAULT symbolics; `job <JOB>` shows them with the job's values.\n")
        else:
            out.append("Executed by: **no indexed job** (its default symbolics below are all the index knows).\n")
        render_steps("proc_id=?", p["id"], p["member_name"])
        mids.append(p["member_id"])
    out.append(unresolved_for(conn, mids))
    out.append(_docs_section(conn, name.upper()))
    return "".join(out)


# --------------------------------------------------------------------------
# call graph
# --------------------------------------------------------------------------

def _callees_of(conn: sqlite3.Connection, name: str) -> Tuple[List[Tuple[str, str]], int]:
    rows = conn.execute("""
        SELECT c.kind, c.target, c.resolved, c.resolution FROM call_edge c JOIN program p ON p.id=c.program_id
        WHERE UPPER(p.program_id)=?""", (name.upper(),)).fetchall()
    out, unres = [], 0
    for r in rows:
        if r["target"]:
            out.append((r["target"], r["kind"]))
        else:
            res = _jl(r["resolved"])
            if res:
                out.extend((t, "dynamic") for t in res)
            else:
                unres += 1
    return out, unres


def _callers_of(conn: sqlite3.Connection, name: str) -> List[Tuple[str, str]]:
    """Programs that reach `name`: CALL / LINK / XCTL / SQL CALL to the
    program or any ENTRY alias of it, plus START/RETURN TRANSID and IMS
    message switches to a transaction that the CSD / stage-1 routes to it."""
    names = _names_of(conn, name)
    out: List[Tuple[str, str]] = []
    seen = set()
    for n in names:
        for r in conn.execute("""
                SELECT DISTINCT p.program_id, c.kind FROM call_edge c JOIN program p ON p.id=c.program_id
                WHERE (UPPER(c.target)=? OR c.resolved LIKE ?) AND c.kind NOT IN ('cics_start','cics_return','ims_switch')""",
                              (n, f'%"{n}"%')):
            if (r["program_id"], r["kind"]) not in seen:
                seen.add((r["program_id"], r["kind"]))
                out.append((r["program_id"], r["kind"] + ("" if n == name.upper() else f" via ENTRY {n}")))
        for r in conn.execute("""
                SELECT DISTINCT p.program_id, c.kind, t.tran_code FROM call_edge c JOIN program p ON p.id=c.program_id
                JOIN transaction_def t ON (UPPER(c.target)=UPPER(t.tran_code) OR c.resolved LIKE '%"' || UPPER(t.tran_code) || '"%')
                WHERE UPPER(t.program)=? AND c.kind IN ('cics_start','cics_return','ims_switch')""", (n,)):
            key = (r["program_id"], r["kind"] + " " + r["tran_code"])
            if key not in seen:
                seen.add(key)
                out.append((r["program_id"], f"{r['kind']} (transaction {r['tran_code']})"))
    return out


def _call_site(conn: sqlite3.Connection, caller: str, callee: str) -> Tuple[str, List[str]]:
    """(cite, USING list) of one call site from caller to callee."""
    names = _names_of(conn, callee)
    for p in programs_named(conn, caller):
        for n in names:
            r = conn.execute("""SELECT line, using_args FROM call_edge WHERE program_id=? AND
                                (UPPER(target)=? OR resolved LIKE ?) ORDER BY line LIMIT 1""",
                             (p["id"], n, f'%"{n}"%')).fetchone()
            if r:
                return cite(conn, p["id"], r["line"]), _jl(r["using_args"])
    return "?", []


def cmd_graph(conn: sqlite3.Connection, name: str, direction: str, depth: int, args: bool = False) -> str:
    out = [f"# {'Callers' if direction == 'callers' else 'Callees'} of {name.upper()} (depth {depth})\n"]
    seen = {name.upper()}
    q = deque([(name.upper(), 0)])
    rows = []
    edge_seen = set()
    unresolved_total = 0
    missing = []
    while q:
        cur, d = q.popleft()
        if d >= depth:
            continue
        if direction == "callers":
            nxt = _callers_of(conn, cur)
        else:
            nxt, u = _callees_of(conn, cur)
            unresolved_total += u
        for tgt, kind in nxt:
            if (cur, tgt, kind) in edge_seen:
                continue                 # several call sites, one edge
            edge_seen.add((cur, tgt, kind))
            caller, callee = (tgt, cur) if direction == "callers" else (cur, tgt)
            ct, _u = _call_site(conn, caller, callee)
            rows.append((d + 1, cur, tgt, kind, ct))
            if tgt not in seen:
                seen.add(tgt)
                if not programs_named(conn, tgt):
                    missing.append(tgt)
                q.append((tgt, d + 1))
    out.append(table(["depth", "from", "to", "kind", "cite"] if direction == "callees"
                     else ["depth", "callee", "caller", "kind", "cite"], rows))
    if args and direction == "callers":
        # Every call site with its full USING list against the callee's
        # LINKAGE: a count mismatch is the S0C4 waiting to happen.
        callee = programs_named(conn, name)
        expect = _jl(callee[0]["linkage_using"]) if callee else []
        out.append(f"\n### Call sites and arguments (callee LINKAGE: {len(expect)} - {', '.join(expect) or 'none'})\n")
        srows = []
        for r in conn.execute("""SELECT p.program_id, p.id AS pid, c.kind, c.using_args, c.line FROM call_edge c
                                 JOIN program p ON p.id=c.program_id
                                 WHERE UPPER(c.target)=? OR c.resolved LIKE ? ORDER BY p.program_id, c.line""",
                              (name.upper(), f'%"{name.upper()}"%')):
            ua = _jl(r["using_args"])
            flag = ""
            if expect and ua and len(ua) != len(expect):
                flag = f"**passes {len(ua)}, callee expects {len(expect)}**"
            elif expect and not ua and r["kind"] not in ("cics_link", "cics_xctl"):
                flag = "no USING list seen"
            srows.append((r["program_id"], r["kind"], ", ".join(ua) or "-", cite(conn, r["pid"], r["line"]), flag))
        out.append(table(["caller", "kind", "USING (positional)", "cite", "check"], srows))
        out.append("> BY REFERENCE is the default: the callee can change every argument. COMMAREA is the "
                   "single positional argument of a LINK/XCTL.\n")
    if unresolved_total:
        out.append(f"\n**{unresolved_total} dynamic CALL(s) with no resolvable target in this graph** - "
                   f"the graph is incomplete to that extent.\n")
    if missing:
        out.append(f"\nReferenced but NOT in index (no source, or a name mismatch member/PROGRAM-ID/load): "
                   f"{', '.join(sorted(set(missing))[:30])}\n")
    return "".join(out)


# --------------------------------------------------------------------------
# field / literal
# --------------------------------------------------------------------------

def _field_defs(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    return conn.execute("""
        SELECT f.*, m.name AS member_name, m.kind, m.path FROM field f JOIN member m ON m.id=f.member_id
        WHERE UPPER(f.name)=? ORDER BY m.kind, m.name""", (name.upper(),)).fetchall()


def _root_of(conn: sqlite3.Connection, field_id: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM field WHERE id=?", (field_id,)).fetchone()
    while r and r["parent_id"]:
        r = conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
    return r


def _file_decls(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    """The SELECT / FD declarations of a file name, program by program."""
    return conn.execute("""SELECT p.id AS pid, p.program_id, f.assign_dd, f.organization, f.fd_record, f.line
                           FROM file_decl f JOIN program p ON p.id = f.program_id
                           WHERE UPPER(f.select_name) = ? ORDER BY p.program_id, f.line""", (name.upper(),)).fetchall()


# the statements an index built before ROADMAP re-parse item 26 recorded as reads of the file they name
_OLD_FILE_STMTS = ("OPEN", "CLOSE", "START", "DELETE")


def _file_name_uses(conn: sqlite3.Connection, name: str, files: Sequence[sqlite3.Row]
                    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, List[str], List[str]]]]:
    """The field references of a name the index holds as a SELECT / FD file:
    ([(program, statement)] - the file statements of a program that declares
    the file, which an index built before ROADMAP re-parse item 26 recorded as
    reads of it; [(program, [statements], [copybooks it misses])] - the
    programs that declare no such file, where the name is a data item whose
    definition the index does not hold). A file and a data item of one
    program cannot share a name, but two programs can: FLTBAT02's `MOVE SPACES
    TO VOY-FILE` read as a file statement of an older index (LESSONS 213)."""
    from . import recover
    declaring = {f["pid"] for f in files}
    old: set = set()
    data: Dict[str, Tuple[int, set]] = {}
    for r in conn.execute("""SELECT r.program_id AS pid, p.program_id AS prog, p.member_id, r.stmt, r.mode
                             FROM field_ref r JOIN program p ON p.id = r.program_id WHERE UPPER(r.name) = ?""",
                          (name.upper(),)):
        stmt = (r["stmt"] or "").upper()
        if r["pid"] in declaring:
            if stmt in _OLD_FILE_STMTS and r["mode"] == "read":
                old.add((r["prog"], stmt))
        else:
            data.setdefault(r["prog"], (r["member_id"], set()))[1].add(stmt)
    missing = recover.not_found_copies(conn, [mid for mid, _s in data.values()]) if data else set()
    users = [(prog, sorted(stmts), sorted(book for mid2, book in missing if mid2 == mid))
             for prog, (mid, stmts) in sorted(data.items())]
    return sorted(old), users


def _file_not_field(conn: sqlite3.Connection, name: str, files: Sequence[sqlite3.Row],
                    uses: Optional[Tuple[List[Tuple[str, str]], List[Tuple[str, List[str], List[str]]]]] = None,
                    shown: int = 12) -> str:
    """`field` for a name the index holds as a SELECT / FD file and nowhere
    as a data item. Since ROADMAP re-parse item 26 no statement records a file
    as a field reference (OPEN / CLOSE recorded it as a read), and `field
    VOY-FILE` answered only NOT DEFINED with 'check spelling' - wrong advice
    for a name the index holds (LESSONS 212). Says where it is declared and
    which report answers for a file. The file statements an index built
    before item 26 recorded as reads are said to be so - those of a program
    that declares the file, and only those: another program's `MOVE SPACES
    TO VOY-FILE` names a data item of its own, and the page said the name was
    no data item and called that MOVE an older index's OPEN (LESSONS 213)."""
    nm = name.upper()
    old, users = uses if uses is not None else _file_name_uses(conn, nm, files)
    n = len(files)
    if users:
        out = [f"`{nm}` is a **file** (SELECT ... ASSIGN, FD) in {n} program{'' if n == 1 else 's'}, and a data item "
               f"in {len(users)} other{'' if len(users) == 1 else 's'} (below) - declared as a file in:\n"]
    else:
        out = [f"`{nm}` is a **file** (SELECT ... ASSIGN, FD), not a data item - declared in {n} "
               f"program{'' if n == 1 else 's'}:\n"]
    out.append(table(["program", "ASSIGN TO", "organization", "FD record", "cite"],
                     [(f["program_id"], f["assign_dd"] or "", f["organization"] or "", f["fd_record"] or "",
                       cite(conn, f["pid"], f["line"])) for f in files[:shown]]))
    if len(files) > shown:
        out.append(f"_... {len(files) - shown} more_\n")
    rec = next((f["fd_record"] for f in files if f["fd_record"]), None)
    out.append(f"\n`program {files[0]['program_id']}` shows what the program does with the file (OPEN, READ, WRITE) "
               "and the dataset each job step gives its DD"
               + (f"; `field {rec}` the reads and writes of its record" if rec else "") + ".\n")
    if old:
        by_prog: Dict[str, List[str]] = defaultdict(list)
        for prog, stmt in old:
            by_prog[prog].append(stmt)
        said = ", ".join(f"{prog} ({', '.join(stmts)})" for prog, stmts in sorted(by_prog.items())[:8]) \
            + (f", +{len(by_prog) - 8} more" if len(by_prog) > 8 else "")
        one = len(old) == 1
        out.append(f"\nThe {'read' if one else 'reads'} below by {said} {'is a file statement' if one else 'are file statements'} "
                   "(OPEN, CLOSE, START, DELETE) that an index built before ROADMAP re-parse item 26 recorded as "
                   f"{'a read' if one else 'reads'} of the file; a build of this toolkit records none.\n")
    if users:
        said = ", ".join(f"{prog} ({', '.join(stmts)})" for prog, stmts, _b in users[:8]) \
            + (f", +{len(users) - 8} more" if len(users) > 8 else "")
        why = "; ".join(f"{prog} copies {', '.join(books)}" for prog, _s, books in users if books)
        books = sorted({b for _p, _s, bs in users for b in bs})
        out.append(f"\nIn {said} `{nm}` names a data item, not this file, and no indexed program or copybook defines it"
                   + (f": {why}, which the index does not hold - `copybook {books[0]}` says what to fetch, and the "
                      "build after that reads the item's definition if it is there" if books else "")
                   + f". The references below from {'that program' if len(users) == 1 else 'those programs'} are the "
                   "data item's.\n")
    return "".join(out)


NOT_DEFINED_HINT = "check spelling, REPLACING renames, or an 88-level name - try `literal`"


def _not_defined(conn: sqlite3.Connection, name: str) -> str:
    """`field` for a name no indexed copybook or program defines, no 88 and
    no file. When programs referencing it copy a copybook the compile reads
    from a product's own library and the estate does not hold
    (recover.supplied_copybooks: DFHAID, CMQV, a name of the manifest's
    system_includes), the name is most likely one of its items: said so,
    with the copybook. `field DFHENTER` sent him to check a spelling that
    was right (LESSONS 216)."""
    nm = name.upper()
    progs = {int(r[0]): str(r[1]) for r in conn.execute(
        "SELECT DISTINCT p.member_id, p.program_id FROM field_ref r JOIN program p ON p.id = r.program_id "
        "WHERE UPPER(r.name) = ?", (nm,))}
    plain = f"**NOT DEFINED** in any indexed copybook or program ({NOT_DEFINED_HINT}).\n"
    if not progs:
        return plain
    from . import recover
    supplied = recover.supplied_copybooks(conn)
    books: Dict[str, List[str]] = defaultdict(list)                 # program -> the supplied copybooks it copies
    ids = sorted(progs)
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        for mid, book in conn.execute(
                f"SELECT DISTINCT member_id, UPPER(copybook) FROM copy_use WHERE resolved_member_id IS NULL "
                f"AND member_id IN ({','.join('?' * len(part))}) ORDER BY member_id, id", part):
            if book in supplied and book not in books[progs[int(mid)]]:
                books[progs[int(mid)]].append(book)
    books = {p: bs for p, bs in books.items() if bs}
    if not books:
        return plain
    n = len(progs)
    # the verbs agree with the counts: '1 of the 2 programs referencing it copies', 'For the other program' (it said
    # 'copy' and 'programs', LESSONS 219)
    lead = ("the program referencing it copies" if n == 1 else "every program referencing it copies"
            if len(books) == n else f"{len(books)} of the {n} programs referencing it "
                                    f"{'copies' if len(books) == 1 else 'copy'}")
    said = "; ".join(f"{p} copies {', '.join(bs)}" for p, bs in sorted(books.items())[:8]) \
        + (f"; +{len(books) - 8} more" if len(books) > 8 else "")
    first = next(iter(sorted(books.items())))[1][0]
    whose = "its" if len({b for bs in books.values() for b in bs}) == 1 else "their"
    return (f"**NOT DEFINED** in any indexed copybook or program - {lead} a copybook the compile reads from a product's "
            f"own library, not from the estate ({said}). The name is probably one of {whose} items, which the index "
            f"does not hold (`copybook {first}` says which product supplies it)"
            + (f". For the other program{'' if n - len(books) == 1 else 's'}: {NOT_DEFINED_HINT}"
               if len(books) < n else "") + ".\n")


def cmd_field(conn: sqlite3.Connection, name: str, show_all: bool = False,
              program: Optional[str] = None) -> str:
    defs = _field_defs(conn, name)
    out = [f"# Field {name.upper()}\n"]
    names = [name.upper()]
    files: List[sqlite3.Row] = []            # the name is a SELECT / FD file and no data item
    file_uses: Tuple[List[Tuple[str, str]], List[Tuple[str, List[str], List[str]]]] = ([], [])
    if not defs:
        # An 88-level name (PM-LAPSED): the question is really about its
        # parent field and the value it stands for.
        c88 = conn.execute("""SELECT c.name, c.values_lit, f.name AS parent, f.offset, f.length, f.pic, m.name AS mem, c.line
                              FROM cond88 c JOIN field f ON f.id=c.field_id JOIN member m ON m.id=f.member_id
                              WHERE UPPER(c.name)=?""", (name.upper(),)).fetchall()
        if c88:
            out.append(f"`{name.upper()}` is an **88-level condition name**, not a field:\n")
            out.append(table(["88 name", "parent field", "offset", "len", "PIC", "values", "cite"],
                             [(c["name"], c["parent"], c["offset"], c["length"], c["pic"] or "",
                               " ".join(_jl(c["values_lit"])), f"{c['mem']}:{c['line']}") for c in c88]))
            parents = sorted({c["parent"].upper() for c in c88})
            out.append(f"References below cover the 88 name itself (`SET ... TO TRUE`, `IF {name.upper()}`) - "
                       f"run `field {parents[0]}` for the parent's own references and `values {parents[0]}` "
                       f"for every value the code assumes.\n")
            defs = []
        else:
            files = _file_decls(conn, name)
            if files:
                file_uses = _file_name_uses(conn, name, files)
                out.append(_file_not_field(conn, name, files, file_uses))
            else:
                out.append(_not_defined(conn, name))
    # COPY ... REPLACING renamed it in some programs: their references are
    # indexed under the new name.
    for a in conn.execute("SELECT DISTINCT new_name FROM field_alias WHERE UPPER(orig_name)=?", (name.upper(),)):
        if a["new_name"].upper() not in names:
            names.append(a["new_name"].upper())
    for a in conn.execute("SELECT DISTINCT orig_name FROM field_alias WHERE UPPER(new_name)=?", (name.upper(),)):
        if a["orig_name"].upper() not in names:
            names.append(a["orig_name"].upper())
    if len(names) > 1:
        out.append(f"Also known as **{', '.join(names[1:])}** (COPY REPLACING) - references below include those names.\n")
    out.append("\n### Definitions\n")
    out.append(table(["member", "kind", "lvl", "PIC", "usage", "offset", "len", "under", "line"],
                     [(d["member_name"], d["kind"], d["level"], d["pic"] or "(group)", d["usage"] or "",
                       d["offset"], d["length"],
                       (d["qualified"].rsplit(".", 1)[0] if "." in d["qualified"] else "")
                       .replace("*COPYBOOK-FRAGMENT*", "(fragment: 01 is in the including program)"),
                       d["line"]) for d in defs]))
    layouts = {(d["offset"], d["length"]) for d in defs if d["kind"] == "copybook"}
    if len(layouts) > 1:
        out.append("> **VERSION SKEW**: this field sits at different offsets/lengths in different copybooks "
                   "- data is already being read differently by different programs.\n")
    cps = {d["member_name"] for d in defs if d["kind"] == "copybook"}
    users = []
    for cb in sorted(cps):
        rows = conn.execute("""SELECT DISTINCT m.name FROM copy_use c JOIN member m ON m.id=c.member_id
                               WHERE UPPER(c.copybook)=? AND m.kind='cobol'""", (cb,)).fetchall()
        users.append((cb, len(rows), ", ".join(r["name"] for r in rows[:25]) + (" ..." if len(rows) > 25 else "")))
    if users:
        out.append("\n### Programs including the defining copybooks\n")
        out.append(table(["copybook", "programs", "names"], users))
    c88 = conn.execute("""SELECT c.name, c.values_lit, m.name AS mem FROM cond88 c JOIN field f ON f.id=c.field_id
                          JOIN member m ON m.id=f.member_id WHERE UPPER(f.name)=?""", (name.upper(),)).fetchall()
    if c88:
        out.append("\n### 88-levels (test conditions for free)\n")
        out.append(table(["88 name", "values", "member"], [(c["name"], " ".join(_jl(c["values_lit"])), c["mem"]) for c in c88]))

    qn = ",".join("?" * len(names))
    pfilter = " AND UPPER(p.program_id)=?" if program else ""
    refs = conn.execute(f"""SELECT p.program_id, p.id AS pid, r.name, r.mode, r.stmt, r.line FROM field_ref r
                            JOIN program p ON p.id=r.program_id
                            WHERE UPPER(r.name) IN ({qn}){pfilter} ORDER BY r.mode, p.program_id, r.line""",
                        (*names, *([program.upper()] if program else []))).fetchall()
    # Every program appears, never silently dropped: one cite per (program,
    # statement kind) with the count, and `--all` for every site.
    out.append("\n### References by mode" + (" (every site)" if show_all else " (one cite per program and statement; `--all` for every site)") + "\n")
    for mode in ("write", "display", "test", "read"):
        rs = [r for r in refs if r["mode"] == mode]
        if not rs:
            continue
        if show_all:
            items = [f"{r['program_id']} {r['stmt']} @{cite(conn, r['pid'], r['line'])}"
                     + (f" as {r['name']}" if r["name"].upper() != name.upper() else "") for r in rs]
        else:
            grp: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
            for r in rs:
                grp[(r["program_id"], r["stmt"])].append(r)
            items = [f"{pg} {stmt} x{len(v)} @{cite(conn, v[0]['pid'], v[0]['line'])}"
                     + (f" as {v[0]['name']}" if v[0]["name"].upper() != name.upper() else "")
                     for (pg, stmt), v in sorted(grp.items())]
        out.append(f"- **{mode}** ({len(rs)} in {len({r['program_id'] for r in rs})} programs): " + "; ".join(items) + "\n")
    lits = conn.execute(f"""SELECT l.literal, l.context, p.program_id, l.line, p.id AS pid FROM literal_ref l
                            LEFT JOIN program p ON p.id=l.program_id
                            WHERE UPPER(l.field) IN ({qn}) OR {' OR '.join('UPPER(l.field) LIKE ?' for _ in names)}
                            ORDER BY l.context, l.literal""", (*names, *[f"{x}/%" for x in names])).fetchall()
    if lits:
        out.append("\n### Literal values associated\n")
        rows, seen = [], set()
        for l in lits:
            ct = cite(conn, l["pid"], l["line"]) if l["pid"] else f"{defs[0]['member_name'] if defs else '?'}:{l['line']}"
            key = (l["literal"], l["context"], ct.split(" (via")[0])
            if key in seen:
                continue
            seen.add(key)
            rows.append((l["literal"], l["context"], l["program_id"] or "(copybook)", ct))
        cap = None if show_all else 60
        out.append(table(["literal", "how", "program", "cite"], rows[:cap] if cap else rows))
        if cap and len(rows) > cap:
            dropped = sorted({r[2] for r in rows[cap:]})
            out.append(f"_...{len(rows) - cap} more (programs: {', '.join(dropped[:20])}); `--all` shows them_\n")
    # a file's bytes travel as its record: `flow` follows the FD record, not the file name - and a program where
    # the name is a data item follows the name itself
    flow_name = next((f["fd_record"] for f in files if f["fd_record"]), None) or name.upper()
    where = f"`flow {flow_name} --program P`"
    if file_uses[1]:
        where = (f"`flow {flow_name} --program {files[0]['program_id']}` for the file's record, `flow {name.upper()} "
                 f"--program {file_uses[1][0][0]}` for the data item")
    out.append(f"\n> Where the VALUE goes - group MOVEs, READ INTO / WRITE FROM, CALL USING positions, the file's "
               f"bytes to the reader, DB2 columns: {where} (`--up`: where it comes from).\n")

    # DB2 columns this field is loaded from / stored to (column-level lineage)
    sc = conn.execute("""SELECT c.tbl, c.col, c.mode, c.stmt, c.line, p.program_id, p.id AS pid
                         FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                         WHERE UPPER(c.host_var)=? ORDER BY c.mode, p.program_id, c.line""", (name.upper(),)).fetchall()
    if sc:
        out.append("\n### DB2 columns (read = column -> this field; write = this field -> column)\n")
        out.append(table(["table", "column", "mode", "stmt", "program", "cite"],
                         [(r["tbl"], r["col"], r["mode"], r["stmt"], r["program_id"], cite(conn, r["pid"], r["line"]))
                          for r in sc]))

    # IMS: DL/I calls whose I/O area (the 01 this field lives under) is read or written
    roots = set()
    for d in defs:
        r = _root_of(conn, d["id"])
        if r and r["name"] != "*COPYBOOK-FRAGMENT*":
            roots.add(r["name"].upper())
    if roots:
        q = ",".join("?" * len(roots))
        dl = conn.execute(f"""SELECT d.func, d.pcb_arg, d.io_area, d.line, p.program_id, p.id AS pid
                              FROM dli_call d JOIN program p ON p.id=d.program_id
                              WHERE UPPER(d.io_area) IN ({q}) ORDER BY p.program_id, d.line""", tuple(roots)).fetchall()
        if dl:
            out.append("\n### IMS DL/I calls whose I/O area holds this field\n")
            out.append(table(["program", "func", "PCB (positional)", "I/O area", "cite"],
                             [(r["program_id"], r["func"], r["pcb_arg"], r["io_area"], cite(conn, r["pid"], r["line"]))
                              for r in dl]))
            out.append("> GU/GN/GHU read the segment INTO the area; ISRT/REPL write it FROM the area. Map the PCB "
                       "through the PSB (`program` dossier) to name the database and segment.\n")

    out.append(_screen_section(conn, name.upper()))

    # sort cards overlapping this field's bytes (for copybook definitions)
    hits = []
    for d in defs:
        if d["kind"] != "copybook":
            continue
        lo, hi = d["offset"] + 1, d["offset"] + d["length"]
        rows = conn.execute("""SELECT DISTINCT j.job_name, s.step_name, c.card_kind, c.pos, c.length
            FROM card_field_ref c JOIN step s ON s.id=c.step_id LEFT JOIN job j ON j.id=s.job_id
            WHERE c.pos<=? AND c.pos+c.length-1>=?""", (hi, lo)).fetchall()
        for r in rows:
            hits.append((d["member_name"], f"{lo}-{hi}", r["job_name"], r["step_name"], r["card_kind"], f"{r['pos']}-{r['pos']+r['length']-1}"))
    if hits:
        out.append("\n### Sort/control cards addressing these bytes (any dataset - verify the record type matches)\n")
        out.append(table(["copybook", "field bytes", "job", "step", "card", "card bytes"], hits[:40]))
    out.append(_docs_section(conn, name.upper()))
    return "".join(out)


def cmd_flow(conn: sqlite3.Connection, name: str, program: Optional[str] = None, up: bool = False, hops: int = 3,
             width: int = 12, nodes: int = 200, derived: bool = False, show_all: bool = False,
             budget: Optional[int] = None) -> str:
    """Where the VALUE in a field goes (or, --up, comes from): the engine is
    atlas/flow.py, outside the fact modules, so a walk fix costs no re-parse."""
    return flow.render(conn, name, program, up=up, hops=hops, width=width, nodes=nodes, derived=derived,
                       show_all=show_all, budget=budget)


def cmd_literal(conn: sqlite3.Connection, value: str, field: Optional[str] = None,
                like: bool = False) -> str:
    v = value.strip().strip("'\"")
    where = "UPPER(l.literal) LIKE UPPER(?)" if like else "(l.literal=? OR UPPER(l.literal)=?)"
    args: list = [v if "%" in v else f"%{v}%"] if like else [v, v.upper()]
    if field:
        # Common values (3, 'M') appear everywhere; the field is what makes them specific.
        where += " AND (UPPER(l.field)=? OR UPPER(l.field) LIKE ?)"
        args += [field.upper(), f"{field.upper()}/%"]
    rows = conn.execute(f"""SELECT l.literal, l.context, l.field, l.line, l.member_id,
                                  p.id AS pid, p.program_id AS pname, m.name AS member_name, m.kind
                           FROM literal_ref l
                           LEFT JOIN program p ON p.id=l.program_id JOIN member m ON m.id=l.member_id
                           WHERE {where} ORDER BY l.context, m.name, l.line""", args).fetchall()
    out = [f"# Literal '{v}'" + (f" on field {field.upper()}" if field else "") + "\n"]
    if not rows:
        out.append("**not found** as a literal anywhere (VALUE, 88, MOVE, IF/WHEN, DISPLAY). "
                   "It may be built by STRING/arithmetic, read from a table, or spelled differently.\n")
        return "".join(out)
    groups: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[r["context"]].append(r)

    def rows_for(ctx_names: Sequence[str]) -> List[Tuple]:
        res, seen = [], set()
        for c in ctx_names:
            for r in groups.get(c, []):
                where = r["pname"] if r["pid"] else f"{r['member_name']} ({r['kind']})"
                ct = cite(conn, r["pid"], r["line"]) if r["pid"] else f"{r['member_name']}:{r['line']}"
                key = (r["literal"], c, r["field"] or "", ct.split(" (via")[0])
                if key in seen:
                    continue              # same copybook line seen via several programs
                seen.add(key)
                res.append((r["literal"], c, where, r["field"] or "", ct))
        return res

    def message_text(r: sqlite3.Row) -> str:
        """A code kept in a message TABLE: the FILLER text beside it.
            05 FILLER PIC X(4)  VALUE 'E101'.
            05 FILLER PIC X(30) VALUE 'GENDER MUST BE MALE FOR SON'.
        The group's assembled text is indexed as value_group; the piece
        after this one is what the user sees."""
        fld = conn.execute("SELECT id, parent_id FROM field WHERE member_id=? AND line=?",
                           (r["member_id"], r["line"])).fetchone()
        if not fld or not fld["parent_id"]:
            return ""
        sibs = conn.execute("SELECT id, value_lit FROM field WHERE parent_id=? ORDER BY id", (fld["parent_id"],)).fetchall()
        ids = [s["id"] for s in sibs]
        i = ids.index(fld["id"]) if fld["id"] in ids else -1
        nxt = sibs[i + 1]["value_lit"] if 0 <= i < len(sibs) - 1 else None
        if nxt and nxt[:1] in ("'", '"'):
            return f"next piece: {cobol._norm_lit(nxt)}"
        return ""

    hdr = ["literal", "how", "where", "field / column", "cite"]
    out.append("\n### Defined (VALUE / 88-level)\n")
    defined = rows_for(["value", "cond88"])
    texts = {}
    for r in groups.get("value", []):
        t = message_text(r)
        if t:
            texts[(r["literal"], f"{r['member_name']}:{r['line']}")] = t
    if texts:
        out.append(table(hdr + ["message text beside it"],
                         [row + (texts.get((row[0], row[4].split(" (via")[0]), ""),) for row in defined]))
    else:
        out.append(table(hdr, defined))
    out.append("\n### Set (MOVE / STRING)\n")
    out.append(table(hdr, rows_for(["move_to", "string"])))
    out.append("\n### Tested (IF / WHEN)\n")
    out.append(table(hdr, rows_for(["compare", "when"])))
    out.append("\n### In SQL (predicate / SET / INSERT)\n")
    out.append(table(hdr, rows_for(["sql_predicate", "sql_set", "sql_insert"])))
    out.append("\n### Displayed as a literal\n")
    out.append(table(hdr, rows_for(["display"])))

    # Follow the value out of each program that sets it.
    out.append("\n### Where the value goes after it is set (cross-program)\n")
    setters = {(r["pname"], r["pid"], r["field"]) for r in groups.get("move_to", []) if r["pid"] and r["field"]}
    any_flow = False
    for pname, pid, fld in sorted(setters):
        disp = conn.execute("""SELECT stmt, line FROM field_ref WHERE program_id=? AND name=? AND mode='display'""",
                            (pid, fld)).fetchall()
        wr = conn.execute("""SELECT stmt, line FROM field_ref WHERE program_id=? AND name=? AND mode='write' AND stmt<>'MOVE'""",
                          (pid, fld)).fetchall()
        out.append(f"\n**{pname}.{fld}**\n")
        if disp:
            out.append("- displayed in the same program: " + "; ".join(f"{d['stmt']} @{cite(conn, pid, d['line'])}" for d in disp) + "\n")
        # the `flow` engine (MOVE, WRITE, the job, the reader; CALL USING positions; DB2): two hops reach
        # the program that reads the file the value was written to
        for line in flow.lines_from(conn, pid, fld, hops=2):
            out.append(line + "\n")
            any_flow = True
        if wr:
            out.append("- also written via: " + "; ".join(f"{w['stmt']} @{cite(conn, pid, w['line'])}" for w in wr[:6]) + "\n")
    if not any_flow:
        out.append("_`flow` found no hop from the setting programs (no copy, CALL, file, DB2, CICS or MQ route "
                   "in the index) - see Unresolved below for what the index could not read_\n")
    mids = sorted({r["member_id"] for r in rows})
    out.append(unresolved_for(conn, mids, limit=20))
    return "".join(out)


# --------------------------------------------------------------------------
# dataset / copybook impact
# --------------------------------------------------------------------------

def cmd_dataset(conn: sqlite3.Connection, dsn: str) -> str:
    rows = conn.execute("""SELECT * FROM v_dataset_flow WHERE UPPER(dsn) LIKE ? ORDER BY dsn, mode, job_name""",
                        (f"%{dsn.upper()}%",)).fetchall()
    out = [f"# Dataset {dsn.upper()}\n"]
    for a in conn.execute("""SELECT dsn, vsam_type, recordsize_max, key_len, key_off, gdg_limit, relates_to FROM dataset
                             WHERE UPPER(dsn) LIKE ? AND (vsam_type IS NOT NULL OR gdg_limit IS NOT NULL)""",
                          (f"%{dsn.upper()}%",)):
        bits = [a["vsam_type"] or ""]
        if a["recordsize_max"]:
            bits.append(f"RECORDSIZE max {a['recordsize_max']} (the copybook length must agree)")
        if a["key_len"] is not None:
            bits.append(f"KEYS len {a['key_len']} at offset {a['key_off']} (the RECORD KEY field must sit there)")
        if a["gdg_limit"]:
            bits.append(f"GDG LIMIT {a['gdg_limit']}")
        if a["relates_to"]:
            bits.append(f"relates to {a['relates_to']}")
        out.append(f"- IDCAMS DEFINE `{a['dsn']}`: " + "; ".join(b for b in bits if b) + "\n")
    hidden = 0
    if not dsn.startswith("&&"):
        # &&TEMP names repeat across unrelated jobs; showing them here would
        # join jobs that never share a byte. Ask for the && name to see them.
        keep = [r for r in rows if not (r["dsn"] or "").startswith("&&")]
        hidden, rows = len(rows) - len(keep), keep
    if hidden:
        out.append(f"> {hidden} row(s) for job-local `&&` datasets hidden - they never leave their job. "
                   f"Query the `&&NAME` itself, or `job <JOB>`, to see them.\n")
    # A bare PROC's rows carry its DEFAULT symbolics (TEST.CLM.MASTER): shown
    # only when no indexed job expands that PROC. A job step's own
    # //PS.DD override is shown once, on the effective step, not also on the
    # EXEC PROC= step itself.
    expanded = {r[0].upper() for r in conn.execute("SELECT DISTINCT from_proc FROM step WHERE from_proc IS NOT NULL")}
    kept, proc_hidden, ov_hidden = [], 0, 0
    for r in rows:
        if r["job_name"] is None and r["proc_name"] and r["proc_name"].upper() in expanded:
            proc_hidden += 1
            continue
        if r["proc_called"] and r["job_id"] and conn.execute(
                "SELECT 1 FROM step WHERE job_id=? AND parent_step=? LIMIT 1",
                (r["job_id"], r["step_name"])).fetchone():
            ov_hidden += 1
            continue
        kept.append(r)
    rows = kept
    if proc_hidden:
        out.append(f"> {proc_hidden} row(s) from PROC members' default symbolics hidden: the jobs that "
                   f"expand those PROCs are listed with the real names.\n")
    # Every row carries the DD line it comes from: "job X writes DSN Y" is a
    # claim about one JCL/PROC line, and the gate needs that line.
    trows = [(r["dsn"], f"{r['mode']} [{r['mode_source'] or ''}]".replace(" []", ""),
              r["system"] or "?", r["pgm"],
              r["job_name"] or (f"(PROC {r['proc_name']} defaults - no indexed job runs it)"
                                if r["proc_name"] else ""),
              r["step_name"], r["gdg_rel"] or "",
              f"{r['from_proc'] or r['member_name'] or os.path.basename(r['path'] or '')}:{r['dd_line'] or '?'}")
             for r in rows]
    # ONLINE access: the CSD names the dataset (FILE ... DSNAME), the program
    # names the FCT entry (EXEC CICS READ/REWRITE FILE). A VSAM master is
    # updated all day by CICS; a batch-only answer here would be wrong.
    online = conn.execute("""
        SELECT cf.dsname, cf.name AS fct, p.program_id, p.id AS pid, m.system,
               GROUP_CONCAT(DISTINCT o.op) AS ops, MIN(o.line) AS line
        FROM cics_file cf JOIN io_op o ON UPPER(o.target)=UPPER(cf.name) AND o.target_kind='cics'
        JOIN program p ON p.id=o.program_id JOIN member m ON m.id=p.member_id
        WHERE UPPER(cf.dsname) LIKE ? GROUP BY cf.dsname, cf.name, p.program_id, p.id, m.system""",
                          (f"%{dsn.upper()}%",)).fetchall()
    online_rows = []
    for o in online:
        ops = {x.strip().upper() for x in (o["ops"] or "").split(",")}
        writes = bool(ops & {"WRITE", "REWRITE", "DELETE"})
        reads = bool(ops & {"READ", "STARTBR", "READNEXT", "READPREV"})
        mode = "both" if writes and reads else "output" if writes else "input"
        trans = [t[0] for t in conn.execute("SELECT tran_code FROM transaction_def WHERE UPPER(program)=?",
                                            (o["program_id"].upper(),))]
        online_rows.append({"dsn": o["dsname"], "mode": mode, "system": o["system"], "pgm": o["program_id"],
                            "job": f"(online) FCT {o['fct']}" + (f" tran {', '.join(trans[:4])}" if trans else ""),
                            "ops": ", ".join(sorted(ops)), "cite": cite(conn, o["pid"], o["line"])})
        trows.append((o["dsname"], f"{mode} [cics_verb]", o["system"] or "?", o["program_id"],
                      online_rows[-1]["job"], ", ".join(sorted(ops)), "", f"{cite(conn, o['pid'], o['line'])}"))
    tdq = conn.execute("SELECT detail, direction, line, member_id FROM interface_edge WHERE kind='cics_tdq' AND UPPER(detail) LIKE ?",
                       (f"%{dsn.upper()}%",)).fetchall()
    for t in tdq:
        trows.append((t["detail"].split("->")[-1].strip(), f"{'output' if t['direction'] == 'out' else 'input' if t['direction'] == 'in' else 'unknown'} [cics_tdq]",
                      "?", "(CICS)", t["detail"].split("->")[0].strip(), "", "", f"CSD:{t['line']}"))
    out.append(table(["dataset", "direction [source]", "system", "program", "job", "step", "gdg", "member/cite"], trows))
    # delete / alloc / none (IEFBR14 housekeeping) are not writers.
    writers = {r["system"] for r in rows if r["mode"] in ("output", "mod", "both", "create") and r["system"]}
    readers = {r["system"] for r in rows if r["mode"] in ("input", "both") and r["system"]}
    writers |= {o["system"] for o in online_rows if o["mode"] in ("output", "both") and o["system"]}
    readers |= {o["system"] for o in online_rows if o["mode"] in ("input", "both") and o["system"]}
    if writers and readers and writers != readers:
        out.append(f"\n**Crosses departments:** written by {', '.join(sorted(writers))}; read by "
                   f"{', '.join(sorted(readers))}. A layout or value change here is an interface change "
                   f"between systems, not an internal one.\n")
    ext = _interface_rows(conn, dsn=dsn)
    if ext:
        out.append("\n**Crosses the mainframe boundary**: " + "; ".join(
            f"{k} {d}" + (f" to/from {p}" if p else "") + f" ({w})" for (k, d, p, _what, w, _s, _c) in sorted(ext)[:6])
            + " - see `interfaces --dsn`.\n")
    out.append("\n> Direction marked `undetermined` means DISP alone was available; DISP is not direction. "
               "`open_verb` is the program's own OPEN and is authoritative. Rows from expanded PROC steps carry "
               "the calling job's name.\n")
    return "".join(out)


def _copied_as_copybook(conn: sqlite3.Connection, name: str) -> str:
    """`program NAME` on a name that is not a program: when programs COPY it,
    say what it is to them - a copybook atlas.recover re-filed (its layout
    rows still absent), or a member filed as another kind by a line of its
    text (no folder change helps: recover re-files it) or by its folder
    name (rename it), with the programs that say NOT FOUND for it."""
    from . import recover
    accepted, other = recover.members_named(conn, name)
    if accepted:
        refiled = recover.refiled_members(conn)
        notes = [refiled[i] for i, _k, _f, _p in accepted if i in refiled]
        if notes:
            return f"\n> {_cap(recover.REFILED_NOTE)}: {notes[0]}.\n"
        return ""
    if not other:
        # a stub (only numbers - ROADMAP re-parse item 23): what it is to the programs copying it, in their own note
        # said of as many programs as copy it (LESSONS 205)
        copiers = recover.stub_copiers(conn, name)
        note = recover.stub_note_of(conn, name, [i for i, _n in copiers]) if copiers else ""
        if not note:
            return ""
        who = ", ".join(n for _i, n in copiers[:8]) + (f", +{len(copiers) - 8} more" if len(copiers) > 8 else "")
        return (f"\n> {len(copiers)} program{'s' if len(copiers) != 1 else ''} cop{'y' if len(copiers) != 1 else 'ies'} it as "
                f"a copybook and {'are' if len(copiers) != 1 else 'is'} parsed only in part ({who}): {note}.\n")
    unresolved = [r[0] for r in conn.execute(
        "SELECT DISTINCT UPPER(m.name) FROM copy_use c JOIN member m ON m.id=c.member_id "
        "WHERE UPPER(c.copybook)=? AND m.kind='cobol' AND c.resolved_member_id IS NULL ORDER BY 1", (name.upper(),))]
    if not unresolved:
        return ""
    readings = recover.member_readings(other, conn)
    parts = []
    for r in readings:
        fix = recover.folder_fix(r) if r["by"] != "content" else recover.content_fix(r)
        parts.append(f"in {r['folder']} it is {recover.filed_phrase(r)} - {fix}")
    who = ", ".join(unresolved[:8]) + (f", +{len(unresolved) - 8} more" if len(unresolved) > 8 else "")
    return (f"\n> {len(unresolved)} program{'s' if len(unresolved) != 1 else ''} cop{'y' if len(unresolved) != 1 else 'ies'} it "
            f"as a copybook and say{'' if len(unresolved) != 1 else 's'} `COPY {name.upper()} NOT FOUND` ({who}): "
            + "; ".join(dict.fromkeys(parts)) + ".\n")


def _exists_as_other_kind(conn: sqlite3.Connection, name: str) -> str:
    """`copybook` found no copybook / cobol / unknown member: a member of
    another kind may still carry the name - filed as a proc, a control card
    or a document by its folder name (a procedure copybook has no content
    signature), which the build never expands; say so, with the fix."""
    from . import recover
    accepted, other = recover.members_named(conn, name)
    if not accepted and not other:
        return ""
    stub = f" {_cap(recover.stub_note_of(conn, name))}." if recover.stubs_named(conn, name) else ""
    if other and not accepted and not conn.execute("SELECT 1 FROM copy_use WHERE UPPER(copybook)=? LIMIT 1",
                                                   (name.upper(),)).fetchone():
        # no program copies the name - a date or a sort card the jobs read, one system's card a stub (LESSONS 206):
        # nothing is parsed in part for it, and renaming the folder or declaring its kind copybook would file a card
        # as a copybook, which no card lookup reads - what it is, and the jobs naming a card member of it
        where = "; ".join(f"filed as {k} (folder {f})" for _i, k, f, _p in other[:4]) + (" ..." if len(other) > 4 else "")
        jobs = recover.card_jobs(conn, name)
        cards = (f" The DDs of job{'s' if len(jobs) != 1 else ''} {', '.join(jobs[:8])}"
                 + (f", +{len(jobs) - 8} more" if len(jobs) > 8 else "")
                 + f" name a card member {name.upper()}: `job NAME` shows the cards each one read." if jobs else "")
        return (f" as a copybook - no program copies it; a member with this name exists, {where}: nothing is parsed in "
                "part for it, so there is no folder to rename and no kind to declare." + cards + stub)
    if accepted:                                                        # sql: the resolver expands it, this report does not read it
        where = "; ".join(f"filed as {k} (folder {f})" for _i, k, f, _p in accepted[:4]) + (" ..." if len(accepted) > 4 else "")
        return (f" as a copybook - a member with this name exists, {where}: a kind the build expands, but not one this "
                "report reads; `program NAME` shows each COPY of it.")
    # the folder name decided the kind (rename it), the kind declared for the library did (declare it copybook), or a
    # line of the text did (no folder change helps: atlas.recover re-files it, or says why it cannot - or the folder
    # fix first when only a COPYLIB folder is missing for it to) - said per member
    readings = recover.member_readings(other[:4], conn)
    wheres = []
    for r in readings:
        if r["by"] in ("content", "declared"):
            wheres.append(f"filed as {r['kind']} {recover.by_words(r)} (folder {r['folder']}; {r['seen']})")
        else:
            wheres.append(f"filed as {r['kind']} (folder {r['folder']})")
    where = "; ".join(wheres) + (" ..." if len(other) > 4 else "")
    fixes = []
    for fix in dict.fromkeys(recover.folder_fix(r) for r in readings if r["by"] != "content"):
        fixes.append(_sentence(fix))
    for fix in dict.fromkeys(recover.content_fix(r) for r in readings if r["by"] == "content"):
        fixes.append(f"For the copy filed by its content, {fix}." if fixes else _sentence(fix))
    return (f" as a copybook - a member with this name exists, {where}: not a kind the build expands, so every program "
            f"that copies it is parsed only in part. " + " ".join(fixes) + stub)


def disk_verdicts(conn: sqlite3.Connection, names: Sequence[str]) -> Tuple[str, Dict[str, str]]:
    """(the estate root, or why it was not walked; {NAME: one cell}) - what
    the disk says for copybook names the index holds NO member for
    (recover.disk_check, one walk of the estate root when there is a name
    to look for): 'on disk at PATH, not in the index - arrived after the
    last build ...: run your usual build command', 'on disk at PATH, not in
    the index - was there at the last build ...', 'no file with this name
    under the estate; nearest names in the copybook folders: ...' or the
    fetch sentence. Nothing is walked when the index records no root or the
    root is not on disk from here (the cell says so)."""
    from . import recover
    wanted = [str(n).upper() for n in names]
    if not wanted:
        return "", {}
    root = recover.estate_root(conn)
    if not root:
        why = "the index records no estate root: not looked for on disk"
        return "", {n: why for n in wanted}
    if not os.path.isabs(root) and os.path.isdir(root):
        root = os.path.abspath(root)                                # built as `estate` from the toolkit's folder
    if not os.path.isdir(root):
        why = f"the estate root {root} is not on disk from here: not looked for on disk"
        return "", {n: why for n in wanted}
    checked = recover.disk_check(wanted, root, conn)
    return root, {n: recover.disk_cell(v) for n, v in checked.items()}


def _disk_note(conn: sqlite3.Connection, name: str) -> str:
    """`copybook`'s line under NOT FOUND for a name no member carries."""
    root, cells = disk_verdicts(conn, [name])
    cell = cells.get(name.upper(), "")
    if not cell:
        return ""
    return f"\nLooked for on disk{f' under the estate root `{root}`' if root else ''}: {cell}.\n"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _sentence(s: str) -> str:
    """'the folder name decided ...' -> 'The folder name decided ....'"""
    return _cap(s) + "."


_OVER_SHAPE = re.compile(r"over the shape of [^:]*")


def unknown_cell(resolved: str, folder: Optional[str]) -> str:
    """`program`'s 'resolved to' cell for a copy expanded from a member filed
    'unknown' (no content signature - a literal, a procedure copybook in
    lower case - in a dataset-named folder with no hint): the program is
    whole, but the build has no parser for that kind, so the member's own
    lines are not indexed - `paragraph` prints them empty and nothing can
    cite them. Before ROADMAP re-parse item 21 the one moment this was said
    was recover's arrived step; the build of this toolkit parses such a
    program again at once, so it is said here (LESSONS 202)."""
    from . import recover
    return f"{resolved} - filed `unknown` in {folder or '?'}: its own lines are not indexed; {recover.UNKNOWN_FIX}"


def unknown_note(conn: sqlite3.Connection, names: Sequence[str]) -> str:
    """The note under a report whose lines come from members filed 'unknown'
    (unknown_cell): '' when none is."""
    rows = [r for n in dict.fromkeys(names) for r in conn.execute(
        "SELECT name, library FROM member WHERE UPPER(name)=? AND kind='unknown' ORDER BY library", (n.upper(),))]
    if not rows:
        return ""
    from . import recover
    where = ", ".join(dict.fromkeys(f"{r[0]} ({r[1] or '?'})" for r in rows))
    return (f"\n> Filed `unknown`: {where} - the build expands it into its programs but has no parser for that kind, so "
            f"its own lines are not indexed - `paragraph` prints them empty and nothing can cite them; "
            f"{recover.UNKNOWN_FIX}.\n")


def declared_over_cell(resolved: str, detail: Optional[str]) -> str:
    """`program`'s 'resolved to' cell: the member's name - and, for a member
    the kind declared for its library typed copybook over the shape of an
    Assembler, listing or MFS member (its 'declared_kind' row), that it was,
    so a program whole over such a member does not say only 'ok' (LESSONS
    199)."""
    if not detail:
        return resolved
    m = _OVER_SHAPE.search(detail)
    return (f"{resolved} (filed copybook by its library's declared kind, {m.group(0) if m else 'over a shape'} - see "
            f"`copybook {resolved}`)")


def cmd_copybook(conn: sqlite3.Connection, name: str) -> str:
    out = [f"# Impact of copybook {name.upper()}\n"]
    copies = conn.execute("SELECT m.* FROM member m WHERE UPPER(m.name)=? AND m.kind IN ('copybook','cobol','unknown')",
                          (name.upper(),)).fetchall()
    if not copies and name.upper() in expand._SYSTEM_INCLUDES:
        # no member of the name, and every program's row for it is the precompiler's (no NOT FOUND note of its
        # own): nothing to fetch - the disk check's 'fetch the library the listings name' would send him looking
        # for a library that does not exist (LESSONS 203). A COBOL `COPY SQLCA` with no member says NOT FOUND below.
        from . import recover
        users = conn.execute("SELECT DISTINCT m.id, m.name, c.line FROM copy_use c JOIN member m ON m.id = c.member_id "
                             "WHERE UPPER(c.copybook) = ? AND c.resolved_member_id IS NULL ORDER BY m.name, c.line",
                             (name.upper(),)).fetchall()
        noted = recover.not_found_copies(conn, [u[0] for u in users])
        if not any((u[0], name.upper()) in noted for u in users):
            return (out[0] + f"\n**Supplied by the DB2 precompiler** - {precompiler_why(name)}.\n"
                    + f"\n### Programs including it ({len({u[0] for u in users})})\n"
                    + (", ".join(f"{u[1]} @{u[1]}:{u[2]}" for u in users) or "_none_") + "\n")
    if not copies:
        from . import recover
        product = recover.supplied_copybooks(conn).get(name.upper())
        if product:
            # a copybook IBM supplies (or the manifest's system_includes names) and no member of any kind carries: the
            # compile reads it from the product's library - no NOT FOUND, no disk check, no library to fetch (ROADMAP
            # re-parse item 27). On an index built before the item its programs still say NOT FOUND: said, not advised
            users = conn.execute("SELECT DISTINCT m.id, m.name, c.line FROM copy_use c JOIN member m ON m.id = c.member_id "
                                 "WHERE UPPER(c.copybook) = ? AND c.resolved_member_id IS NULL AND m.kind = 'cobol' "
                                 "ORDER BY m.name, c.line", (name.upper(),)).fetchall()
            noted = recover.not_found_copies(conn, [u[0] for u in users])
            before = sorted({u[1] for u in users if (u[0], name.upper()) in noted})
            return (out[0] + f"\n**{_cap(supplied_label(product))}** - {supplied_why(name, product)}. A copy the shop "
                    "keeps in its own COPYLIB would be expanded like any copybook.\n"
                    + (f"\n{len(before)} of the programs below {'is' if len(before) == 1 else 'are'} still `partial` "
                       f"for it ({', '.join(before[:8])}{', ...' if len(before) > 8 else ''}): "
                       f"{supplied_before(len(before))}.\n"
                       if before else "")
                    + f"\n### Programs including it ({len({u[0] for u in users})})\n"
                    + (", ".join(f"{u[1]} @{u[1]}:{u[2]}" for u in users) or "_none_") + "\n")
        other = _exists_as_other_kind(conn, name)
        from . import recover
        if not other and recover.stubs_named(conn, name):
            # only a stub carries the name (ROADMAP re-parse item 23): what it holds, who copies it, what to do - the
            # disk has nothing to add
            users = recover.stub_copiers(conn, name)
            who = ", ".join(u[1] for u in users[:8]) + (f", +{len(users) - 8} more" if len(users) > 8 else "")
            # no program copying it (a date or a count card a job names): what it is, and the jobs naming a card member
            # of the name - no COPY clause, nothing to write (LESSONS 205)
            jobs = [] if users else recover.card_jobs(conn, name)
            return (out[0] + f"\n**NOT FOUND** as a copybook - {recover.stub_note_of(conn, name, [u[0] for u in users])}.\n"
                    + (f"\n{len(users)} program{'s' if len(users) != 1 else ''} cop{'y' if len(users) != 1 else 'ies'} it and "
                       f"{'are' if len(users) != 1 else 'is'} parsed only in part: {who}.\n" if users else "")
                    + (f"\nThe DDs of job{'s' if len(jobs) != 1 else ''} {', '.join(jobs[:8])}"
                       + (f", +{len(jobs) - 8} more" if len(jobs) > 8 else "")
                       + f" name a card member {name.upper()}: `job NAME` shows the cards each one read.\n" if jobs else "")
                    + f"\nA stub is {recover.stub_todo(len(users))}.\n" + _listing_sources_of(conn, name, missing=True))
        # no member at all under the name: the estate root is walked once for a file with that name, or a near one
        # (LESSONS 192) - a member of another kind is said by the note above, the disk check has nothing to add
        disk = "" if other else _disk_note(conn, name)
        return out[0] + "\n**NOT FOUND**" + other + "\n" + disk + _listing_sources_of(conn, name, missing=True)
    if len(copies) > 1:
        out.append(f"> **{len(copies)} copies of this member** ({len({c['norm_sha'] for c in copies})} distinct contents). "
                   f"Record lengths per copy:\n")
        for c in copies:
            r = conn.execute("SELECT MAX(offset+length) AS len, COUNT(*) AS n FROM field WHERE member_id=?", (c["id"],)).fetchone()
            out.append(f"  - `{c['path']}`: {r['len']} bytes, {r['n']} fields" + ("  [authoritative]" if c["authoritative"] else "") + "\n")
    from . import recover
    stubs = recover.stubs_named(conn, name)
    if stubs:
        # a real copy beats a stub of the name wherever each sits (ROADMAP re-parse item 23): said, so the stub's
        # library is not taken for the one the programs expand
        folders = ", ".join(dict.fromkeys(f for _i, f, _p in stubs))
        out.append(f"\n> Also a stub of this name in {folders}: it holds only numbers - the build never expands it, and "
                   "the programs below expand the copy above.\n")
    # a member atlas.recover re-filed as a copybook (the classifier had typed it asm / listing / mfs by a line of
    # its text): indexed, expanded into its programs, its own layout rows absent until the next full re-parse
    refiled = [c for c in copies if (c["parse_error"] or "").startswith(recover.REFILED_MARK)]
    if refiled and len(refiled) == len(copies):
        out.append(f"\n**{_cap(recover.REFILED_NOTE)}**: {refiled[0]['parse_error']}.\n")
    # a copy the kind declared for its library typed copybook over the shape of an Assembler, listing or MFS member:
    # the programs below expand it - said here, not only in the unresolved rows at the end (LESSONS 199)
    for c in copies:
        over = conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind='declared_kind' LIMIT 1",
                            (c["id"],)).fetchone()
        if over:
            out.append(f"\n**Declared copybook over a shape** (`{c['path']}`): {over[0]}.\n")
        if c["kind"] == "unknown" and conn.execute("SELECT 1 FROM copy_use WHERE resolved_member_id=? LIMIT 1",
                                                   (c["id"],)).fetchone():
            # expanded into the programs below, but no parser reads it: its lines are not indexed (unknown_cell). Said
            # only when a program expands it, as `program` says it: a card member filed 'unknown' that a job reads and
            # no program copies is no copybook, and the folder fix would file its cards as one, which no card lookup
            # reads (build.CARD_KINDS) - the job would lose them (LESSONS 203)
            out.append(f"\n**Filed `unknown`** (`{c['path']}`): the build expands it into the programs below but has no "
                       f"parser for that kind, so its own lines are not indexed - `paragraph` prints them empty and "
                       f"nothing can cite them; {recover.UNKNOWN_FIX}.\n")
    out.append(_listing_sources_of(conn, name, recovered=all(recover.FOLDER.lower() in (c["path"] or "").lower() for c in copies)))
    progs = conn.execute("""SELECT DISTINCT m.name AS member_name, m.id AS mid, m.parse_status AS mstatus, p.program_id,
                                   p.id AS pid, c.replacing, c.line,
                                   c.resolved_member_id, rm.path AS rpath, rm.system AS rsys, rm.norm_sha AS rsha
                            FROM copy_use c JOIN member m ON m.id=c.member_id JOIN program p ON p.member_id=m.id
                            LEFT JOIN member rm ON rm.id=c.resolved_member_id
                            WHERE UPPER(c.copybook)=? ORDER BY rm.path, p.program_id""", (name.upper(),)).fetchall()
    # a program's row with no resolved member: a COPY not found - or one the expander SKIPPED (recursive,
    # nested too deep), whose member the program had found (LESSONS 185)
    skipped = recover.skipped_copies(conn)

    def skip_why(p: sqlite3.Row) -> Optional[str]:
        return skipped.get((p["mid"], name.upper())) if p["resolved_member_id"] is None else None

    # a row with no resolved member and no skip: NOT FOUND when the program's own note says so; otherwise the
    # program had expanded a copy that left the index after the parse (recover.not_found_copies, LESSONS 202)
    noted = recover.not_found_copies(conn, [p["mid"] for p in progs if p["resolved_member_id"] is None])

    def supplied(p: sqlite3.Row) -> bool:
        # `EXEC SQL INCLUDE SQLCA`: the precompiler's area, never this member - no copy of it left (LESSONS 203)
        return p["resolved_member_id"] is None and not skip_why(p) and precompiler_row(name, (p["mid"], name.upper()) in noted)

    def left_since(p: sqlite3.Row) -> bool:
        return (p["resolved_member_id"] is None and not skip_why(p) and (p["mid"], name.upper()) not in noted
                and not supplied(p))

    out.append(f"\n### Programs including it ({len(progs)})\n")
    expanded = {p["resolved_member_id"] for p in progs}
    if len(expanded) > 1:
        # Two copies of the copybook: say which programs compile against which
        # layout - that IS the version-skew answer. One copy beside rows with no
        # member (NOT FOUND, skipped, the precompiler's SQLCA) is no skew.
        out.append("Grouped by the copy each program actually expanded"
                   + (" (version skew)" if len(expanded - {None}) > 1 else "") + ":\n")
        by_copy: Dict[Optional[int], List[sqlite3.Row]] = defaultdict(list)
        for p in progs:
            by_copy[p["resolved_member_id"]].append(p)
        for rid, ps in by_copy.items():
            if rid:
                ln = conn.execute("SELECT MAX(offset+length) FROM field WHERE member_id=?", (rid,)).fetchone()[0]
                out.append(f"- `{ps[0]['rpath']}` ({ps[0]['rsys'] or 'no system'}, {ln} bytes): "
                           + ", ".join(f"{p['program_id']} @{p['member_name']}:{p['line']}" for p in ps) + "\n")
            else:
                by_why: Dict[Optional[str], List[sqlite3.Row]] = defaultdict(list)
                for p in ps:
                    # the key: '' a copy that left, PRECOMPILED the precompiler's area, a skip's reason, None NOT FOUND
                    by_why["" if left_since(p) else PRECOMPILED if supplied(p) else skip_why(p)].append(p)
                for why, qs in by_why.items():
                    out.append(("- copy NOT FOUND: " if why is None
                                else "- no longer linked (the copy it had expanded left the index after the parse): "
                                if why == "" else f"- supplied by the DB2 precompiler (`EXEC SQL INCLUDE {name.upper()}`), "
                                                  "not this member: " if why == PRECOMPILED else f"- {skipped_cell(why)}: ")
                               + ", ".join(f"{p['program_id']} @{p['member_name']}:{p['line']}" for p in qs) + "\n")
    out.append(table(["program", "member", "REPLACING", "cite"],
                     [(p["program_id"], p["member_name"], (p["replacing"] or "")[:40], f"{p['member_name']}:{p['line']}")
                      for p in progs]))
    skips: Dict[str, List[str]] = {}                                    # why -> programs whose COPY of it was skipped
    for p in progs:
        w = skip_why(p)
        if w and p["program_id"] not in skips.setdefault(w, []):
            skips[w].append(p["program_id"])
    if skips:
        out.append("\n> " + "; ".join(f"{skipped_cell(w)}: {', '.join(ps)}" for w, ps in skips.items()) + ".\n")
    stale = {p["program_id"] for p in progs if p["resolved_member_id"] is None and not skip_why(p)
             and not left_since(p) and not supplied(p)}                                 # programs, not COPY sites
    gone = {p["program_id"] for p in progs if left_since(p)}
    if stale and refiled and len(refiled) == len(copies):
        # re-filed by atlas.recover: the programs were parsed while it was filed as the other kind, and the same run
        # marked them - unless something un-marked them since
        pending = all(p["mstatus"] == "pending" for p in progs if p["program_id"] in stale)
        before = recover.refiled_kind_before(refiled[0]["parse_error"])
        how = recover.refiled_how_before(refiled[0]["parse_error"])
        out.append(f"\n> {len(stale)} of the programs above still say{'s' if len(stale) == 1 else ''} `COPY {name.upper()} NOT "
                   f"FOUND` (under 'Unresolved in scope' below): parsed while this member was filed as `{before}` {how}; "
                   "atlas.recover re-filed it"
                   + (" and marked them - run your usual build command" if pending
                      else "; `python -m atlas.recover --db atlas.db` marks them, then run your usual build") + ".\n")
    elif stale:
        # the member is here and a program still says NOT FOUND: it was parsed before the member arrived, and the
        # build before ROADMAP re-parse item 21 parsed its programs again only for a member filed copybook or cobol -
        # the build of this toolkit does it for every kind the resolver expands, so on an index it built this is not
        # said; say why, and the whole fix
        odd = ", ".join(sorted({c["kind"] for c in copies} - {"copybook", "cobol"}))
        why = (f" - the build that made this index parsed the programs again only for a member filed copybook or cobol, "
               f"and this one is filed `{odd}` (ROADMAP re-parse item 21)") if odd else ""
        out.append(f"\n> {len(stale)} of the programs above still say{'s' if len(stale) == 1 else ''} `COPY {name.upper()} NOT "
                   "FOUND` (under 'Unresolved in scope' below): parsed before this member arrived and nothing parsed them "
                   f"again{why}. `python -m atlas.recover --db atlas.db` marks them, then run your usual build"
                   + (f"; and {recover.UNKNOWN_FIX}" if any(c["kind"] == "unknown" for c in copies) else "") + ".\n")
    if gone:
        # parsed with a copy of this copybook that left the index after the parse - no NOT FOUND note, the fields of
        # that copy - and the build that made the index did not parse them again (LESSONS 188, 202)
        one = len(gone) == 1
        pending = all(p["mstatus"] == "pending" for p in progs if p["program_id"] in gone)
        out.append(f"\n> {len(gone)} of the programs above {'was' if one else 'were'} parsed with a copy of this copybook "
                   f"that has left the index since - {recover.LEFT_CAUSES} - and the build that made this index did not "
                   f"parse {'it' if one else 'them'} again (ROADMAP re-parse item 21): {'its' if one else 'their'} fields "
                   "are those of the earlier read. "
                   + ("atlas.recover marked them - run your usual build command" if pending
                      else "`python -m atlas.recover --db atlas.db` marks them, then run your usual build")
                   + (f"; and {recover.UNKNOWN_FIX}" if any(c["kind"] == "unknown" for c in copies) else "") + ".\n")
    pnames = [p["program_id"].upper() for p in progs]
    if pnames:
        q = ",".join("?" * len(pnames))
        steps = conn.execute(f"""SELECT DISTINCT j.job_name, s.step_name, s.effective_pgm, s.line, s.from_proc, m.name AS mem
                                 FROM step s LEFT JOIN job j ON j.id=s.job_id LEFT JOIN member m ON m.id=j.member_id
                                 WHERE UPPER(s.effective_pgm) IN ({q}) AND s.job_id IS NOT NULL""", pnames).fetchall()
        out.append(f"\n### Jobs/steps running those programs ({len(steps)})\n")
        out.append(table(["job", "step", "program", "cite"],
                         [(s["job_name"], s["step_name"], s["effective_pgm"], f"{s['from_proc'] or s['mem']}:{s['line']}")
                          for s in steps]))
        outs = conn.execute(f"""SELECT DISTINCT d.dsn_resolved, s.effective_pgm, d.mode, d.line, s.from_proc, m.name AS mem
                                FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                LEFT JOIN member m ON m.id=j.member_id
                                WHERE UPPER(s.effective_pgm) IN ({q}) AND d.dsn_resolved IS NOT NULL
                                  AND d.mode IN ('output','mod','both') AND s.job_id IS NOT NULL""",
                            pnames).fetchall()
        out.append(f"\n### Datasets WRITTEN by those programs ({len(outs)}) - downstream consumers are one hop away\n")
        rows = []
        for o in outs:
            cons = conn.execute("""SELECT DISTINCT s.effective_pgm FROM dd d JOIN step s ON s.id=d.step_id
                                   WHERE d.dsn_resolved=? AND d.mode IN ('input','both','unknown') AND UPPER(s.effective_pgm) NOT IN (%s)""" % q,
                                (o["dsn_resolved"], *pnames)).fetchall()
            rows.append((o["dsn_resolved"], o["effective_pgm"], ", ".join(c["effective_pgm"] or "?" for c in cons) or "_none indexed_",
                         f"{o['from_proc'] or o['mem']}:{o['line']}"))
        out.append(table(["dataset", "written by", "then read by", "cite"], rows))
        callers = set()
        for pn in pnames:
            callers.update(c for c, _k in _callers_of(conn, pn))
        if callers:
            out.append(f"\n### Programs that CALL the including programs (LINKAGE may carry this record)\n"
                       + ", ".join(sorted(callers)) + "\n")
        dyn = conn.execute(f"""SELECT COUNT(*) FROM call_edge c JOIN program p ON p.id=c.program_id
                               WHERE UPPER(p.program_id) IN ({q}) AND c.resolution='unresolved'""", pnames).fetchone()[0]
        if dyn:
            out.append(f"\n**{dyn} unresolved dynamic CALL(s) inside these programs** - their targets may also carry the record.\n")
        cards = conn.execute(f"""SELECT COUNT(*) FROM card_field_ref c JOIN step s ON s.id=c.step_id WHERE UPPER(s.effective_pgm) IN ({q})""", pnames).fetchone()[0]
        cards2 = 0
        dsn_list = [o["dsn_resolved"] for o in outs]
        if dsn_list:
            qd = ",".join("?" * len(dsn_list))
            cards2 = conn.execute(f"""SELECT COUNT(DISTINCT c.id) FROM card_field_ref c JOIN step s ON s.id=c.step_id
                                      JOIN dd d ON d.step_id=s.id
                                      WHERE d.dsn_resolved IN ({qd}) AND d.mode IN ('input','both','unknown')""",
                                  dsn_list).fetchone()[0]
        out.append(f"\nSort-card byte references: {cards} on these steps, **{cards2} on steps that consume the "
                   f"datasets written above** (see `job <name>` for positions; any field-length change moves them).\n")
    mids = [c["id"] for c in copies] + [conn.execute("SELECT member_id FROM program WHERE id=?", (p["pid"],)).fetchone()[0] for p in progs]
    out.append(unresolved_for(conn, mids, limit=30))
    return "".join(out)


# --------------------------------------------------------------------------
# hygiene reports
# --------------------------------------------------------------------------

def cmd_dead(conn: sqlite3.Connection) -> str:
    out = ["# Dead-code candidates\n\n> Candidates only. A never-PERFORMed paragraph is still live if the "
           "paragraph above it falls through, and a never-called program is still live if the scheduler, "
           "an online transaction, or an unresolved dynamic CALL runs it. Every caveat below is real.\n"]
    called = set()
    codes = set()
    for r in conn.execute("SELECT kind, target, resolved FROM call_edge"):
        tgt = {r["target"].upper()} if r["target"] else set()
        tgt.update(t.upper() for t in _jl(r["resolved"]))
        if r["kind"] in ("cics_start", "cics_return", "ims_switch"):
            codes.update(tgt)            # transaction codes, routed to programs below
        else:
            called.update(tgt)
    # CALL 'RATEENT' reaches RATECALC through its ENTRY point.
    for r in conn.execute("SELECT p.program_id, a.alias FROM program_alias a JOIN program p ON p.id=a.program_id"):
        if r["alias"].upper() in called:
            called.add(r["program_id"].upper())
    run = {r[0].upper() for r in conn.execute("SELECT DISTINCT effective_pgm FROM step WHERE effective_pgm IS NOT NULL")}
    tx = {r[0].upper() for r in conn.execute("SELECT DISTINCT program FROM transaction_def WHERE program IS NOT NULL")}
    if codes:
        q = ",".join("?" * len(codes))
        tx.update(r[0].upper() for r in conn.execute(
            f"SELECT DISTINCT program FROM transaction_def WHERE program IS NOT NULL AND UPPER(tran_code) IN ({q})",
            sorted(codes)))
    progs = conn.execute("SELECT p.program_id, m.name, m.path FROM program p JOIN member m ON m.id=p.member_id").fetchall()
    dead = [(p["program_id"], p["name"], p["path"]) for p in progs
            if p["program_id"].upper() not in called | run | tx]
    unres = conn.execute("SELECT COUNT(*) FROM call_edge WHERE resolution='unresolved'").fetchone()[0]
    has_sched = conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]
    out.append(f"\n### Programs never called, never run by indexed JCL, never a transaction ({len(dead)})\n")
    out.append(f"Caveats: {unres} unresolved dynamic CALLs; scheduler loaded: {'yes' if has_sched else 'NO'}; "
               f"online definitions loaded: {'yes' if tx else 'NO'}.\n\n")
    out.append(table(["program", "member", "path"], dead[:200]))
    if len(dead) > 200:
        out.append(f"_...{len(dead) - 200} more_\n")
    out.append(_dead_extras(conn))
    return "".join(out)


def cmd_ambiguous(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT * FROM v_ambiguous_member ORDER BY distinct_content DESC, copies DESC").fetchall()
    out = [f"# Duplicate member names ({len(rows)})\n\n> Same name in more than one place. When contents differ, "
           "the index cannot know which is production; declare it in a manifest (`--manifest`) or every answer "
           "about these members is a coin toss.\n\n"]
    out.append(table(["member", "kind", "copies", "distinct contents", "systems", "paths"],
                     [(r["name"], r["kind"], r["copies"], r["distinct_content"], r["systems"] or "?", r["paths"][:160])
                      for r in rows[:300]]))
    out.append("\n> Same name in two DEPARTMENTS is normal and safe once each department's programs resolve "
               "to their own copy (sources.json `system` + copybook order). Same name with different content "
               "inside ONE department is the dangerous case.\n")
    cross = conn.execute("""SELECT name, GROUP_CONCAT(DISTINCT kind) AS kinds FROM member
                            WHERE kind IN ('cobol','copybook','jcl','proc','dbd','psb')
                            GROUP BY name HAVING COUNT(DISTINCT kind) > 1 ORDER BY name LIMIT 200""").fetchall()
    if cross:
        out.append(f"\n### Same name, different kinds ({len(cross)}) - informational\n")
        out.append("A PSB or a job named after its program is a convention, not a duplicate. Cite such a member as "
                   "`[[NAME(kind) line \"token\"]]` so the gate checks the right file.\n")
        out.append(table(["member", "kinds"], [(c["name"], c["kinds"]) for c in cross[:60]]))
    return "".join(out)


def index_header(conn: sqlite3.Connection) -> str:
    """When the index was built and from what - every answer is 'as of'
    this, and a build that never finished is said so."""
    last = conn.execute("SELECT started_at, finished_at, root, members FROM build_run WHERE finished_at IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    hanging = conn.execute("SELECT started_at FROM build_run WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    parts = []
    if last:
        parts.append(f"index built {last['finished_at']} from {os.path.basename(last['root'] or '') or last['root']} "
                     f"({last['members']} members)")
    else:
        parts.append("index never completed a build")
    if hanging and (not last or hanging["started_at"] > (last["finished_at"] or "")):
        parts.append(f"WARNING: a build started {hanging['started_at']} has not finished (running, or crashed) - "
                     f"facts may be partial")
    inc = conn.execute("SELECT COUNT(*) FROM library WHERE complete=0").fetchone()[0]
    if inc:
        parts.append(f"WARNING: {inc} fetched librar{'y is' if inc == 1 else 'ies are'} INCOMPLETE (see coverage)")
    return "; ".join(parts)


# What each blind spot in `unresolved` means, and what closes it. The parsers
# record the kind; a reader should not have to guess what the word implies.
UNRESOLVED_MEANING = {
    "expand": ("a COPY statement whose copybook is not in the index, or is held only as a stub of numbers (or skipped: "
               "recursive, nested too deep)",
               "fetch that copybook library and build again (a stub's copybook: `python -m atlas.recover --db atlas.db "
               "--from FOLDER` writes it from the compiler listings) - until then the program's fields are incomplete"),
    "expand (copybook chosen among several)": (
        "the resolver's own choice repeated as a COPY warning: the program expanded completely with the copy its "
        "'ambiguous_copybook' row names - not a missing copybook",
        "nothing to fetch; an index built before ROADMAP re-parse item 18 repeats the choice as a COPY warning, and "
        "the next full re-parse stops it - check the manifest's system / copybook order if the copy named is the "
        "wrong one"),
    "ambiguous_copybook": ("two copies of one copybook with different content; one was chosen",
                           "declare the department's copybook order (manifest `copylib_order`) or remove the stale copy"),
    "ambiguous_copybook (decided by the listing)": (
        "two copies of one copybook with different content; the program's compiler listing names the library the "
        "compiler read it from, and that copy was expanded",
        "nothing - the compiler's own record, not a guess; nothing to declare"),
    "ambiguous_copybook (confirmed by the listing)": (
        "two copies of one copybook with different content; system and library order picked one, and the program's "
        "compiler listing names a library whose copy has the same text (compiled against staging, promoted unchanged)",
        "nothing - the compiler's own record agrees with the copy expanded; nothing to declare"),
    "ambiguous_copybook (a recovered copy used)": (
        "two copies of one copybook with different content; the copy expanded is a recovered one - a stand-in "
        "atlas.recover wrote from the compiler listings for a member the estate did not hold",
        "fetch the program's own copybook library - the one its compiler listing names (`python -m atlas.recover --db "
        "atlas.db` writes it to work/fetch-list.txt) - and build: the real member replaces the recovered copy. Do not "
        "remove the recovered copy by hand: the build would expand the other copy instead. Where this report warns "
        "above that a real member has arrived for such a copy, run recover and the build first"),
    "include_member": ("a JCL `INCLUDE MEMBER=` whose member is not in the index",
                       "fetch the JCLLIB / PARMLIB holding it: its DDs, symbols and steps are missing from the job"),
    "card_member": ("a SYSIN DD naming a control-card member that is not in the index",
                    "fetch the CNTL / PARMLIB library: sort fields, IDCAMS names and utility cards are unknown"),
    "sort_symbols": ("a SORT step with a SYMNAMES DD whose member is not in the index",
                     "fetch that member - the symbolic field positions in its sort cards cannot be resolved"),
    "intrdr": ("a step that submits a job through the internal reader",
               "nothing to fetch: the submitted JCL is written at run time, so that job's steps are invisible here"),
    "sql_cursor": ("a FETCH whose cursor is declared somewhere else (another program, or a copybook not found)",
                   "fetch the copybook holding the DECLARE CURSOR; otherwise the columns of that FETCH are unknown"),
    "dynamic_sql": ("PREPARE / EXECUTE IMMEDIATE: the SQL text is built at run time",
                    "nothing to fetch: the tables touched cannot be known from the source - a human must confirm"),
    "dynamic_call": ("a CALL through a variable (or EXEC CICS LINK/XCTL) whose target was never a literal here",
                     "the target may come from a control card, a DB2 table or LINKAGE: `callers` / `crud` are incomplete for it"),
    "screen": ("a BMS / MFS member in which no map, format or message macro was recognised",
               "check it really is a map source; if it is, send its first lines (no data) so the parser can be fixed"),
    "operand_parse": ("a statement that split into different verbs with and without its OF/IN qualifiers",
                      "its data_flow rows were taken from the qualifier-blanked text: `flow` through that line may miss a qualified operand - send the statement shape (no data) so the parser can be fixed"),
    "ambiguous_field": ("a data name declared more than once in one program and referenced without a qualifier that picks one",
                        "nothing to fetch: `flow` stops at that name rather than guess; qualify the reference (A OF B) in the source, or read both declarations - the note names them"),
    "flow_pruned": ("a reference resolved into a WORKING-STORAGE root the build stored without its children",
                    "a toolkit bug (the pruning rule missed a reference source): send the note text so _flow_names in build.py can be fixed; `flow` stops at that root until then"),
    "no_commarea": ("a CICS program that tests EIBCALEN / DFHCOMMAREA but declares no 01 DFHCOMMAREA in LINKAGE",
                    "nothing to fetch: a LINK or XCTL into it ends at the program - `flow` cannot place the caller's COMMAREA bytes without the 01"),
    "dli_function": ("a DL/I call whose function code is not a literal in this program",
                     "the function (GU/ISRT/REPL...) comes from a variable: read/update intent for that call is unknown"),
    "ims_psb": ("a program whose PSB could not be matched",
                "fetch the PSB source library: PCB order, databases and PROCOPT for its DL/I calls are unknown"),
    "ims_dbd": ("a DBD referenced but not indexed",
                "fetch the DBD source library: segments, keys and the database's fields are unknown"),
    "ims_msw": ("an IMS message switch (CHNG) whose destination is not a literal",
                "the transaction it switches to cannot be known from the source"),
    "ims_switch": ("an IMS message switch destination built at run time", "as above - a human must confirm the target"),
    "layout_warning": ("a record layout the parser could not compute exactly (SYNC, a REDEFINES larger than its object, "
                       "01s mixed with stray lower levels, a copybook's own layout without the text of a COPY in it - not "
                       "found, skipped, a stub, IBM-supplied)",
                       "check the offsets in `layout` before using them for test data or an interface contract; for a COPY "
                       "not found, fetch that copybook's library and build again - `layout RECORD --program PGM` gives a "
                       "program's view"),
    "declared_kind": ("a member the UI's table declares copybook whose text has the shape of an Assembler, listing or MFS "
                      "member: the kind declared wins over a shape, so the build expands it into every program copying it",
                      "check it is the COBOL copybook those programs copy; if it is not, correct the library's kind in the "
                      "UI's table and run the build"),
    "procedure_copybook": ("a copybook that contributes PROCEDURE DIVISION code, not data",
                           "nothing to fix: its paragraphs belong to every program that copies it"),
    "missing_proc": ("an EXEC PROC= whose PROC member is not in the index",
                     "fetch the PROCLIB: that step's real program, DDs and datasets are unknown"),
    "ambiguous_proc": ("two PROCs of one name; one was chosen",
                       "declare the department (manifest `system_of`) so a job takes its own PROC"),
    "proc_synthesised": ("a system PROC (IBM-supplied) referenced but not indexed; its shape was assumed",
                         "harmless unless your shop overrides it - then fetch the overriding PROCLIB"),
    "override_target": ("a PROC step override naming a step or DD that is not there",
                        "usually a typo in the JCL, or the PROC changed since - worth reading"),
    "referback": ("a DSN=*.STEP.DD referback naming no earlier DD with a dataset",
                  "usually a typo or a step that was removed: the lineage has a hole there"),
    "symbolic": ("a JCL symbolic that is never given a value",
                 "the dataset name it forms is unknown - the value comes from a SET, a PROC default or the scheduler"),
    "gdg": ("a GDG relative generation that could not be resolved to a real name",
            "nothing to fix: (+1)/(0) depends on the run - the base name is what the index keeps"),
    "cics_file_var": ("an EXEC CICS FILE() named by a variable", "the file it touches cannot be known from the source"),
    "cics_transid": ("an EXEC CICS START/RETURN TRANSID() named by a variable", "the transaction started is unknown"),
    "cics_tdq": ("a transient-data queue named by a variable", "the queue written or read is unknown"),
    "routing": ("a CSD / stage-1 routing entry that could not be matched to a program",
                "fetch the CSD extract or the IMS stage-1 for that region"),
    "ndm_process": ("a Connect:Direct process member named by a SUBMIT PROC= that is not indexed",
                    "fetch that process library: the datasets sent or received are unknown"),
    "mq": ("an MQ queue name built at run time", "the queue cannot be known from the source"),
    "card_seq_assumed": ("a card deck with no sequence field; the order on disk was assumed",
                         "harmless for reading, but confirm before regenerating the deck"),
    "scheduler_format": ("a scheduler export line the loader did not understand",
                         "send one line of the export (no names) so the loader can be extended"),
    "copy_replacing": ("a COPY ... REPLACING: the field names in this program differ from the copybook as stored",
                       "nothing to fetch - ask for the program's own names (`layout X --program PGM`), not the copybook's"),
    "assign_dataname": ("a SELECT ... ASSIGN TO a data name (dynamic allocation), so the DD name is a guess",
                        "the real DD is set at run time (a MOVE, a parm or BPXWDYN): confirm it in the job before relying on it"),
    "launcher_parm": ("a utility step (IDCAMS, SORT, ICETOOL, IKJEFT01, DFSRRC00, FTP, Easytrieve, SAS) whose cards or "
                      "program are not inline and not in the index, so what it does is not known",
                      "fetch the control-card library its SYSIN / SYSTSIN / TOOLIN points at (or the PARM's PROC member) "
                      "and build again"),
    "scheduler_symbol": ("a dataset name holding a scheduler symbol (%%ODATE, #JI, &DATE) that is only filled in at run "
                         "time, so the real name is not known",
                         "read the scheduler's own definition of that symbol; the index keeps the name with <VAR> in it"),
    "exec_no_period": ("an EXEC SQL / CICS / DLI block before the PROCEDURE DIVISION with no period after its "
                       "END-EXEC: the index read it as if the period were there, and the member is partial because "
                       "the facts after that line rest on it",
                       "look at the member at the line the note names - a period lost in an edit, or a copy that "
                       "never compiled. Whether a compile accepted it depends on the step that read the block - a "
                       "separate precompiler or translator (DB2's, CICS's) turns it into comment lines before the "
                       "compiler runs, the compiler's own SQL / CICS option reads it itself: the program's compile "
                       "JCL or listing says which. Nothing to fetch; the facts after that line are right when the "
                       "period is all that is missing"),
    "no_program_id": ("a program with no PROGRAM-ID paragraph the parser could read - the member name stands in for it",
                      "check the member: a copybook or a card deck filed in a source library, or a PROGRAM-ID written "
                      "in a form the parser does not read (report the shape)"),
    "sql_group_into": ("an EXEC SQL ... INTO :group where the group or its DCLGEN is not in this program, so the "
                       "column-to-field mapping of that statement is missing",
                       "fetch the copybook (DCLGEN) the group comes from and build again"),
}


COVERAGE_ROWS = 15            # members named per table; `coverage --all` names every one


def _partial_members(conn: sqlite3.Connection, limit: int = COVERAGE_ROWS) -> str:
    """Members whose facts are incomplete, with the reason the parser gave -
    a COBOL member is `partial` mostly because a copybook it copies was not
    found, and then the fields of that copybook are missing from the index.
    The reason shown is the one that MADE it partial (a missing copybook, an
    EXEC block with no period after its END-EXEC, an unrecognised map), not
    the first note the parser happened to write - and never the resolver's
    chosen-among-several note, which does not make a member partial (see
    _chosen_members: those members are not in this table). exec_no_period
    ranked with the notes that make no member partial, so a CICS program's
    no_commarea note, written before it, was given as the reason (LESSONS
    212). The advice under the table speaks of the reasons the table shows."""
    rows = conn.execute(f"""
        SELECT m.kind, m.name, m.library, m.parse_error,
               (SELECT u.kind || ': ' || SUBSTR(COALESCE(u.detail, ''), 1, 200) FROM unresolved u
                WHERE u.member_id = m.id
                ORDER BY CASE WHEN u.kind = 'expand' AND instr(COALESCE(u.detail, ''), ?) = 0 THEN 0
                              WHEN u.kind IN ('exec_no_period', 'expand', 'screen') THEN 1 ELSE 2 END,
                         u.id LIMIT 1) AS why,
               (SELECT COUNT(*) FROM doc_image i WHERE i.member_id = m.id AND i.ocr_text IS NOT NULL
                AND i.ocr_text <> '') AS ocr_read
        FROM member m WHERE m.parse_status = 'partial' AND NOT ({_CHOSEN_PRED})
        ORDER BY m.kind, m.name""", (AMBIGUOUS_PICK, AMBIGUOUS_PICK)).fetchall()
    from . import recover
    # a program marked `ok` whose COPY row no member resolves any more (the copybook it had expanded changed on
    # disk or went, and nothing parsed it again): not in this table, not whole either - said under it
    stale = recover.unlinked_ok_programs(conn)
    if not rows:
        return "\n### Members parsed only in part\n_none_\n" + unlinked_ok_clause(stale)
    by_kind: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    out = [f"\n### Members parsed only in part ({len(rows)}) - their facts are incomplete to this extent\n"]
    out.append(table(["kind", "members", "most common reason"],
                     [(k, len(v), _top_reason(v)) for k, v in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))]))
    shown = [(r["kind"], r["name"], r["library"], clip(r["why"] or r["parse_error"] or "", 150)
              + (f" - {r['ocr_read']} picture(s) read by OCR since: text in sections 1001+" if r["ocr_read"] else ""))
             for r in rows[:limit]]
    out.append("\n" + table(["kind", "member", "library", "reason"], shown))
    scanned = sum(1 for r in rows if r["kind"] == "doc" and r["ocr_read"])
    if scanned:
        out.append(f"\n_{scanned} of the documents above are scans whose pictures OCR has read since the build: their "
                   "text is in the index (sections 1001+); the partial mark is only the build's history_\n")
    if len(rows) > limit:
        # the copybooks clause only for the members not shown that a copybook made partial: an EXEC block with no
        # period after its END-EXEC misses none, and 22 such members were promised missing copybooks (LESSONS 213)
        hidden = rows[limit:]
        by_book = sum(1 for r in hidden if (r["why"] or "").startswith("expand:"))
        clause = ("" if not by_book else "; the copybooks they miss are the 'Copybooks not found' table below"
                  if by_book == len(hidden) else
                  f"; the copybooks {by_book} of them miss are the 'Copybooks not found' table below")
        out.append(f"_... {len(hidden)} more; every one of them: `coverage --all`{clause}_\n")
    # the advice speaks of the reasons the table holds: a member made partial by an EXEC block with no period after
    # its END-EXEC has nothing to fetch, and the copybook advice alone sent the reader to fetch a library
    no_period = sum(1 for r in rows if (r["why"] or "").startswith("exec_no_period:"))
    if no_period < len(rows):
        others = len(rows) - no_period
        # beside exec_no_period members the advice is said of the others alone: it opened 'A COBOL member is usually
        # partial because a copybook ...' when 22 of the 24 were partial for their EXEC block (LESSONS 213)
        lead = ("" if not no_period else
                f"The other {others} member{'' if others == 1 else 's'} {'is' if others == 1 else 'are'} partial for "
                "another reason. ")
        out.append(f"\n> {lead}A COBOL member is usually partial because a copybook it copies is not in the index: "
                   "fetch that copybook library and build again - or, when the estate holds compiler listings or "
                   "expanded programs, `python -m atlas.recover --db atlas.db` rebuilds the missing copybooks from "
                   "them. A document is partial when no text could be extracted (a scan - run `OCR images`). A screen "
                   "member is partial when no map or format macro was recognised.\n")
    if no_period:
        # counted over every partial member, not only the rows shown (the table names COVERAGE_ROWS of them)
        if no_period == len(rows):
            lead = {1: "This member is", 2: "Both members are"}.get(no_period, f"All {no_period} of these members are")
        else:
            lead = f"{no_period} of these {len(rows)} members {'is' if no_period == 1 else 'are'}"
        out.append(f"\n> {lead} partial because an EXEC block before the PROCEDURE DIVISION has no period after its "
                   "END-EXEC (exec_no_period): nothing to fetch - look at the member at the line its note names; its "
                   "row in 'Unresolved by kind' below says what to check.\n")
    if any(r["kind"] in ("cobol", "copybook") for r in rows):
        arrived, misfiled, waiting = recover.arrival_scan(conn)
        clauses = []                                                    # only the clause whose count is not zero
        if waiting:
            # re-filed by atlas.recover on an earlier run, the programs marked: nothing arrived - the build is next
            clauses.append(f"{len(waiting)} of the copybooks reported NOT FOUND {'was' if len(waiting) == 1 else 'were'} "
                           "re-filed as a copybook by atlas.recover on an earlier run and the programs copying "
                           f"{'it' if len(waiting) == 1 else 'them'} are marked: run your usual build command")
        # a copybook whose programs only lost their link to it (a copy that left the index after the parse) is not one
        # 'reported NOT FOUND': those programs are said under the table (unlinked_ok_clause) and in its cell
        arrived = [e for e in arrived if len(e.get("left", [])) < len(e["programs"])]      # type: ignore[arg-type]
        if arrived:
            unknown = any(k == "unknown" for e in arrived for k, _f in e["members"])   # type: ignore[union-attr]
            clauses.append(f"{len(arrived)} of the copybooks reported NOT FOUND {'has' if len(arrived) == 1 else 'have'} a "
                           "member with that name in the index now - the programs were parsed before it arrived and nothing "
                           "parsed them again: `python -m atlas.recover --db atlas.db` marks them, then build"
                           + ("; a member filed `unknown` also needs its folder renamed to end in COPYLIB (or the library's "
                              "kind declared in the UI) so its own lines are indexed" if unknown else ""))
        if misfiled:
            # the folder name decided the kind (rename it, or declare the library's kind), or a line of the text did -
            # the shape of an Assembler, listing or MFS member: no folder change helps, the cell says what does; on an
            # index built before ROADMAP re-parse items 20 and 22, atlas.recover re-files what this toolkit's classifier
            # reads as a copybook
            recover.read_misfiled(misfiled, conn)
            by_folder = [e for e in misfiled if recover.folder_decided(e)]
            by_content = [e for e in misfiled if not recover.folder_decided(e)]
            if by_folder:
                declared = sum(1 for e in by_folder if all(r["by"] == "declared" for r in e["readings"]))   # type: ignore[union-attr]
                if declared == len(by_folder):
                    why = "the kind declared for the library in the UI's table decided it"
                elif declared:
                    why = f"the folder name decided it; for {declared} of them the kind declared for the library in the UI's table did"
                else:
                    why = "the folder name decided it"
                clauses.append(f"{len(by_folder)} exist{'s' if len(by_folder) == 1 else ''} only as a member of a kind the build "
                               f"does not expand ({why}): rename the folder to end in COPYLIB or declare the library's kind in "
                               "the UI, then build")
            # on an index built before ROADMAP re-parse items 20 and 22: the older classifier filed them (a line of
            # their text, or the folder name for a procedure copybook) and the classifier of this toolkit reads them as
            # copybooks - atlas.recover re-files them; said apart from the members a shape typed
            earlier = [e for e in by_content if any(r.get("earlier") for r in e["readings"])]   # type: ignore[union-attr]
            by_content = [e for e in by_content if not any(e is x for x in earlier)]
            if earlier:
                n = len(earlier)
                kinds = ", ".join(sorted({k for e in earlier for k, _f in e["members"]}))   # type: ignore[union-attr]
                clauses.append(f"{n} exist{'s' if n == 1 else ''} only as a member the classifier this index was built with "
                               f"filed as another kind ({kinds}) and the classifier of this toolkit reads as a copybook "
                               f"({recover.EARLIER_ITEMS}): no folder change helps - `python -m atlas.recover --db atlas.db` "
                               f"re-files {'it' if n == 1 else 'them'} as a copybook in the index, then build")
            if by_content:
                kinds = ", ".join(sorted({k for e in by_content for k, _f in e["members"]}))   # type: ignore[union-attr]
                can = sum(1 for e in by_content if any(r["refile"] for r in e["readings"]))    # type: ignore[union-attr]
                # only a COPYLIB folder is missing for atlas.recover to re-file it (no COPY hint, no level numbers):
                # the folder fix first, then a run - 'no folder change helps' is a wrong word for these
                need = sum(1 for e in by_content if recover.folder_would_let(e))
                n = len(by_content)
                stuck = n - can - need
                if need:
                    parts = []
                    if can:
                        parts.append(f"`python -m atlas.recover --db atlas.db` re-files {can} of them as a copybook in the index")
                    parts.append(f"{need} {'sits' if need == 1 else 'sit'} in a folder with no COPY hint and "
                                 f"{'has' if need == 1 else 'have'} no level numbers to go by: rename the folder to end in "
                                 f"COPYLIB, then `python -m atlas.recover --db atlas.db` re-files {'it' if need == 1 else 'them'}"
                                 + (" too" if can else ""))
                    if stuck:
                        parts.append(f"{stuck} cannot be re-filed (the 'Copybooks not found' table says why)")
                    fix = "; ".join(parts) + "; then build"
                elif can == n:
                    fix = (f"no folder change helps - `python -m atlas.recover --db atlas.db` re-files {'it' if n == 1 else 'them'} "
                           "as a copybook in the index, then build")
                elif can:
                    fix = (f"no folder change helps - `python -m atlas.recover --db atlas.db` re-files {can} of them as a copybook "
                           "in the index, then build; the 'Copybooks not found' table says why not for the rest")
                else:
                    fix = (f"no folder change helps - atlas.recover cannot re-file {'it' if n == 1 else 'them'}; the 'Copybooks "
                           "not found' table says why")
                clauses.append(f"{n} exist{'s' if n == 1 else ''} only as a member the classifier typed by a line of its text "
                               f"({kinds}): {fix}")
        stub_books = sorted({book for (_mid, book) in recover.stub_copies(conn)})
        if stub_books:
            n = len(stub_books)
            clauses.append(f"{n} of the copybooks {'is a stub' if n == 1 else 'are stubs'} - only numbers, which the build "
                           "never expands: the programs were compiled against another copy, which `python -m atlas.recover "
                           "--db atlas.db --from FOLDER` writes from their compiler listings (or fetch the library the "
                           "listings name)")
        if clauses:
            out.append("\n> " + "; ".join(clauses) + ". The 'Copybooks not found' table below says which.\n")
    out.append(unlinked_ok_clause(stale))
    return "".join(out)


# the how may carry one level of parentheses: 'confirmed by the program's listing (same text as DATASET)'
_PICK_RE = re.compile(r"(\d+) copies of (\S+) with different content; used (.+?) \(((?:[^()]|\([^()]*\))*)\)\s*$")


# The compiler listing's own statement of which library each copybook came
# from: atlas.recover reads the listing's copybook-source table into
# listing_copy_source and checks every 'ambiguous_copybook' choice against it
# (the resolver's guess versus the compiler's fact). Absent until recover has
# run over an index with listings; nothing here depends on it being there.

def _choices_checked(conn: sqlite3.Connection) -> str:
    """One sentence for coverage: how the choices stand against the listings."""
    from . import recover
    if not recover.has_copy_sources(conn):
        return ""
    checks = recover.check_choices(conn)
    a, _b, o, h, u = recover.choice_counts(checks)
    # the contradicted in two parts - by a current listing, and by one not yet dated (recover.contradicted_words): an
    # undated listing is never counted as a current one
    return (f"\n{a} of these choices {'is' if a == 1 else 'are'} confirmed by the program's listing, "
            f"{recover.contradicted_words(checks)} (see work/recover.md), {o} named by an older listing, {h} name a "
            f"library the index does not hold{recover.stub_named_words(recover.stub_named(checks))}, {u} unknown - the "
            "listing names the library the compiler read the copybook "
            "from; a name is not a fact "
            "about content, so the listing's copy is compared by text with the copy used before a choice is called wrong.\n")


def _listing_says(conn: sqlite3.Connection, member_id: int) -> str:
    """`program`: what the program's listing says each copybook came from,
    and, for a copybook chosen among several, whether that agrees with the
    copy the build used."""
    from . import recover
    if not recover.has_copy_sources(conn):
        return ""
    row = conn.execute("SELECT name, system FROM member WHERE id=?", (member_id,)).fetchone()
    if not row:
        return ""
    program = str(row[0]).upper()
    system = (str(row[1]).strip().upper() or None) if row[1] else None
    stored = recover.stored_copy_sources(conn, program).get(program, [])     # with each listing's `current`, when dated
    # the rows that speak for THIS program (build.rows_that_count, the resolver's rule, per copybook - the same rows as
    # the resolver read for each (program, copybook)): a twin in another system - its own system's listings only
    # (GC-TEST's listing of its own GCPGM2 says nothing for GC's); else its own system's when one of them is current for
    # that copybook, every listing of its name when none is
    twins = recover.program_systems(conn, [program]).get(program, set()) - {system}
    own = recover.rows_of_system(stored, system, recover.listing_systems(conn, {program: stored}), twins)
    rows = sorted({(r[0], r[1], r[2]) for r in own if r[0]})                  # a listing read with no table names nothing
    if not rows:
        return ""
    verdicts = {v["copybook"]: v for v in recover.check_choices(conn) if v["member_id"] == member_id}
    out = ["\n### Listing says\n"]
    for cb, dd, dsn in rows:
        line = f"- listing says: {cb} came from {dsn} ({dd or 'no DD name'})"
        v = verdicts.get(cb)
        if v is None or v["verdict"] == "UNKNOWN":
            pass
        elif v["verdict"] == "CONFIRMED":
            # per library named: the one the copy used came from, a held one whose copy has the same text (promoted),
            # or another one named beside it - the verdict's why says which
            line += (" - confirms the copy the build used" if dsn == v["used_dataset"] else
                     f" - confirms the copy the build used (same text, promoted from {dsn})" if dsn == v["named"] else
                     f" - the build used {v['used_dataset']}: {v['why']}")
        elif dsn == v["used_dataset"]:
            line += " - the library of the copy the build used"         # another library named decides the verdict
        elif dsn != v["named"]:
            pass
        elif v["verdict"] == "CONTRADICTED":
            dated = "a current listing: its source matches this program as indexed" if v["current"] else str(v["dated"])
            line += (f" - CONTRADICTS the copy the build used ({v['used_dataset']}; {v['index_has']}; {dated}) - see "
                     "work/recover.md")
        elif v["verdict"] == "OLDER":
            line += (f" - an older listing names {v['named']} (its source differs from this program as indexed; the text "
                     "differs from the copy used)")
        elif v["verdict"] == "NOT HELD" and v.get("recovered"):
            # the copy used is one atlas.recover wrote: the library the listing names is the one to fetch (LESSONS 210)
            line += (f" - names {v['named']} ({v['index_has']}); the build used a recovered copy, a stand-in written from "
                     "the listings: fetch the real member from there and build - it replaces the recovered copy")
        elif v["verdict"] == "NOT HELD":
            line += f" - names {v['named']}, a library the index does not hold - the copy the build used stands"
        elif v["verdict"] == recover.STUB_NAMED:
            # the library is held, its member of the name only a stub (ROADMAP re-parse item 23, LESSONS 206)
            line += (f" - names {v['named']}, which holds only a stub of {cb}: the program was compiled against the text "
                     f"the listing prints, which the index does not hold - the copy the build used ({v['used_dataset']}) "
                     "may differ from it")
        out.append(line + "\n")
    return "".join(out)


def _listing_sources_of(conn: sqlite3.Connection, copybook: str, missing: bool = False, recovered: bool = False) -> str:
    """`copybook`: the datasets the programs' listings say this copybook came
    from, per program count. For a copybook the index lacks (`missing`) or
    holds only as a copy atlas.recover wrote (`recovered`), the library named
    is the one to fetch - the same rows atlas.recover puts in its fetch list
    (work/fetch-list.txt)."""
    from . import recover
    if not recover.has_copy_sources(conn):
        return ""
    where = "FROM listing_copy_source WHERE copybook=? AND dataset IS NOT NULL AND dataset<>'' GROUP BY dataset, program"
    try:                                                               # per (dataset, program): is any listing naming it current?
        rows = conn.execute(f"SELECT dataset, program, MAX(current) AS current {where}", (copybook.upper(),)).fetchall()
    except sqlite3.OperationalError:                                   # a table written before the dating columns
        rows = conn.execute(f"SELECT dataset, program, NULL AS current {where}", (copybook.upper(),)).fetchall()
    if not rows:
        return ""
    per: Dict[str, Dict[str, int]] = {}
    for r in rows:
        e = per.setdefault(str(r["dataset"]), {"n": 0, "older": 0, "undated": 0, "no_program": 0})
        e["n"] += 1
        if r["current"] is None:
            # undated: the row is from before the tool dated listings, or the program is not in the index to date it against
            in_index = conn.execute("SELECT 1 FROM member WHERE kind='cobol' AND UPPER(name)=? LIMIT 1",
                                    (str(r["program"]).upper(),)).fetchone()
            e["undated" if in_index else "no_program"] += 1
        elif not r["current"]:
            e["older"] += 1

    def cell(dsn: str, e: Dict[str, int]) -> str:
        parts = [f"{e['n']} program{'s' if e['n'] != 1 else ''}"]
        if e["older"]:
            parts.append(f"{e['older']} by an older listing")
        if e["undated"]:
            parts.append(f"{e['undated']} not yet dated")
        if e["no_program"]:
            parts.append(f"{e['no_program']} whose program is not in the index to date the listing against")
        return f"{dsn} ({'; '.join(parts)})"

    said = ", ".join(cell(d, e) for d, e in sorted(per.items(), key=lambda kv: (-kv[1]["n"], kv[0])))
    how = ("the UI's Bulk add takes the dataset name; `python -m atlas.recover` writes every such dataset to "
           "work/fetch-list.txt")
    if missing:
        return f"\nNot in the index, but the listings say it came from: {said} - fetch that library ({how}).\n"
    if recovered:
        return (f"\nThe index holds this copybook only as a recovered copy (rebuilt from a listing by atlas.recover, not "
                f"the library's member), and the listings say it came from: {said} - fetch that library ({how}); the "
                "recovered copy goes on the next recover run once the real member is in the estate.\n")
    return (f"\nThe programs' compiler listings say this copybook came from: {said}"
            " - the library the compiler read, per listing; `program NAME` shows each one.\n")


def same_named_note(conn: sqlite3.Connection, copybook: str, exclude_ids: Sequence[int] = ()) -> str:
    """same_named_state()'s note alone."""
    return same_named_state(conn, copybook, exclude_ids)[0]


def same_named_state(conn: sqlite3.Connection, copybook: str, exclude_ids: Sequence[int] = ()
                     ) -> Tuple[str, Dict[str, object]]:
    """(note, state) - state: `late` / `left` the copiers of each cause
    below, `kinds` the kinds of the members of a kind the resolver expands.

    The note, for a COPY the build could not resolve: '' when no member carries the
    copybook's name; otherwise where that member is and what to do. Two
    cases, both of which coverage used to print as a bare NOT FOUND while
    the member sat on disk: the member is of a kind the resolver expands
    (copybook, cobol, sql, unknown) but arrived AFTER the program was parsed
    and nothing parsed the program again - atlas.recover marks it, the build
    re-parses it; or the only members with the name are of a kind the build
    never expands (proc, ctlcard, doc ...), because a procedure copybook has
    no content signature and the folder name decided its kind - the folder
    must be renamed or the library's kind declared. `exclude_ids`: the
    copiers themselves. A member of a kind the resolver expands may be
    there for a second reason, told apart by the program's own note
    (recover.not_found_copies): a program with no NOT FOUND note for it HAD
    expanded a copy of it, which left the index after the parse (the file
    went while another copy stays, its text changed, or it was recorded
    again under a new id), and the build that made the index did not parse
    it again - 'parsed before it arrived' said of it sent him looking for an
    arrival that never happened (LESSONS 202). 'Run recover, then the build' is not the whole fix
    for a member typed 'unknown': the build has no parser for it, so its
    own lines are not in the index (`paragraph` shows them empty, nothing
    can cite them) until the same folder fix is applied - the note says so.
    A member of another kind may also have been typed by a LINE OF ITS TEXT:
    the shape of an Assembler, listing or MFS member, which no folder change
    alters (the note says what does); or, on an index built before ROADMAP
    re-parse items 20 and 22, a line of a copybook's own text (a START- name
    read as Assembler, a MODULE MAP comment as a listing, a first word MSG
    as MFS) or, for a procedure copybook, the folder name - the classifier
    of this toolkit reads it as a copybook, atlas.recover re-files it as one
    in the index, and the note says that instead; once re-filed, the note
    says the programs are marked for the build (or, after a build, nothing:
    the COPY resolves). A member filed `stub` - only numbers, never expanded
    (ROADMAP re-parse item 23) - is none of these: the note is the
    program's own stub note (recover.stub_note_of) and `state` says `stub`,
    with nothing to re-file, rename or declare."""
    from . import recover
    accepted, other = recover.members_named(conn, copybook, exclude_ids)
    state: Dict[str, object] = {"late": [], "left": [], "kinds": []}
    # a stub of the name (only numbers - ROADMAP re-parse item 23) is neither: not misfiled, nothing to re-file,
    # rename or declare - the program's own note says what it is, and a real copy of the name comes first
    stub = "" if accepted else recover.stub_note_of(conn, copybook, exclude_ids)
    if stub and not other:
        state["stub"] = True
        return stub, state
    if accepted:
        refiled = recover.refiled_members(conn)
        if all(i in refiled for i, _k, _f, _p in accepted):
            # a member atlas.recover re-filed as a copybook (the classifier had typed it by a line of its text):
            # not arrived - the programs it marked re-expand on the next build
            where = ", ".join(sorted({f"{f} (copybook, re-filed by atlas.recover)" for _i, _k, f, _p in accepted}))
            ids = [int(i) for i in exclude_ids]
            pending = bool(ids) and all(
                r[0] == "pending" for r in conn.execute(f"SELECT parse_status FROM member WHERE id IN ({','.join('?' * len(ids))})",
                                                        ids))
            return f"{where} - " + ("the programs copying it are marked: run your usual build command" if pending
                                    else "parsed before it was re-filed: run recover, then the build"), state
        where = ", ".join(sorted({f"{f} ({k})" for _i, k, f, _p in accepted}))
        ids = [int(i) for i in exclude_ids]
        noted = recover.not_found_copies(conn, ids)
        late = [i for i in ids if (i, copybook.upper()) in noted]
        left = [i for i in ids if (i, copybook.upper()) not in noted]
        state = {"late": late, "left": left, "kinds": sorted({k for _i, k, _f, _p in accepted})}
        if left and not late:
            why = (f"the copy {'the program' if len(left) == 1 else 'the programs'} had expanded left the index after the "
                   f"parse and the build that made this index did not parse {'it' if len(left) == 1 else 'them'} again")
        elif left:
            why = (f"{len(late)} program(s) parsed before it arrived, {len(left)} parsed with a copy that left the index "
                   "after the parse")
        else:
            why = "parsed before it arrived"
        return (f"{where} - {why}: run recover, then the build"
                + (f"; and {recover.UNKNOWN_FIX}" if any(k == "unknown" for _i, k, _f, _p in accepted) else "")), state
    if other:
        # the folder name decided the kind for some members, a line of the text for others (a START- name reads
        # as Assembler: no folder change helps, atlas.recover re-files it) - each with its own instruction
        readings = recover.member_readings(other, conn)
        parts = []
        by_folder = [r for r in readings if r["by"] not in ("content", "declared")]
        by_declared = [r for r in readings if r["by"] == "declared"]
        by_content = [r for r in readings if r["by"] == "content"]
        if by_folder:
            # each folder with its own kind: 'PROD.CLM.PROCS, downloads, filed as doc, proc' would not say which is which
            where = "; ".join(dict.fromkeys(f"{r['folder']}, filed as {r['kind']}" for r in by_folder))
            parts.append(f"{where} - not a kind the build expands: rename the folder to end in COPYLIB "
                         "or declare its kind in the UI, then build")
        if by_declared:
            # the kind declared for the library in the UI's table (sources.json) typed it: the folder name did not
            where = "; ".join(dict.fromkeys(f"{r['folder']}, filed as {r['kind']} by its declared kind" for r in by_declared))
            parts.append(f"{where} - not a kind the build expands: "
                         + "; ".join(dict.fromkeys(recover.declared_cell(r) for r in by_declared)))
        if by_content:
            where = "; ".join(dict.fromkeys(f"{r['folder']}, {recover.filed_phrase(r)}" for r in by_content))
            fixes = "; ".join(dict.fromkeys(recover.content_fix(r) for r in by_content))
            parts.append(f"{where} - not a kind the build expands, and {fixes}")
        if stub:
            parts.append(stub)
        return "; ".join(parts), state
    return "", state


def skipped_cell(why: str) -> str:
    """`program`'s 'resolved to' cell and `copybook`'s group line for a COPY
    the expander SKIPPED (recursive, nested too deep): the member is in the
    index and the program was parsed after it, so it is not a copybook not
    found and no instruction belongs beside it (LESSONS 185)."""
    return (f"**COPY skipped - {why}** - not a copybook not found: the member is in the index and the expander stopped "
            "there (the notes below say so)")


# cmd_copybook's group key for the rows precompiler_row() accounts for (a skip's reason is never this word)
PRECOMPILED = "precompiled"


def precompiler_cell(copybook: str) -> str:
    """`program`'s 'resolved to' cell for `EXEC SQL INCLUDE SQLCA` / `SQLDA`
    (expand._SYSTEM_INCLUDES): the DB2 precompiler writes the area into the
    program, so expand.py records the row with no member and no note, on
    purpose - nothing is missing and there is nothing to run. Read by its
    note alone, the row looked like a copy that had left the index, and
    `program` and `pack` told him to run recover, then the build, on every
    DB2 program - on an index this toolkit built too, where neither run
    changes the row (LESSONS 203)."""
    return f"**supplied by the DB2 precompiler** - {precompiler_why(copybook)}"


def precompiler_why(copybook: str) -> str:
    """What precompiler_cell says after its bold words; `copybook` says it too."""
    return (f"`EXEC SQL INCLUDE {copybook.upper()}` is written into the program by the precompiler, not copied from a "
            "library: no member is expanded for it and none is missing")


def precompiler_row(copybook: str, noted: bool) -> bool:
    """A COPY row with no member that the precompiler accounts for: SQLCA or
    SQLDA with no 'COPY X NOT FOUND' note of the program's own (a COBOL
    `COPY SQLCA` the resolver found no member for carries that note, and
    stays NOT FOUND). The same names coverage, recover and the un-linked
    note leave out. The row keeps no trace of the statement that wrote it:
    a COBOL `COPY SQLCA` that had expanded a SQLCA copybook which then left
    the index, on an index whose build did not parse the program again
    (one built before ROADMAP re-parse item 21), reads as the precompiler's
    too, as coverage and recover have always read it - the first build of
    this toolkit parses every program again, and it says NOT FOUND or links
    the copy then."""
    return (copybook or "").upper() in expand._SYSTEM_INCLUDES and not noted


def supplied_before(programs: int = 1) -> str:
    """Said beside such a row on an index built before ROADMAP re-parse item
    27, whose build wrote NOT FOUND for it and marked the program partial."""
    one = programs == 1
    return (f"the build that made this index counted it as not found and marked {'the program' if one else 'them'} "
            f"partial for it; the next build parses every program again (the toolkit changed) and does not")


def supplied_label(product: str) -> str:
    """The bold words for a copybook no member carries that the compile reads
    from a product library: IBM's (expand.IBM_COPYBOOKS), or one the
    manifest's system_includes names (whose product this tool cannot name)."""
    return ("supplied by a product library (the manifest's system_includes), not in the estate"
            if product == expand.MANIFEST_PRODUCT else "IBM-supplied, not in the estate")


def supplied_why(copybook: str, product: str) -> str:
    """What supplied_cell says after its bold words; `copybook` says it too.
    The manifest names the copybook, never the library it comes from."""
    lib = expand.IBM_LIBRARY.get(product)
    where = ("a product's own library (the manifest's `system_includes` names the copybook)"
             if product == expand.MANIFEST_PRODUCT else f"{product}'s own library" + (f" ({lib})" if lib else ""))
    return (f"`COPY {copybook.upper()}` is read by the compile from {where}, not from the shop's copybook libraries: "
            "nothing is to fetch, and its items are not in the index")


def supplied_cell(copybook: str, product: str, noted: bool) -> str:
    """`program`'s 'resolved to' cell for a COPY of a copybook IBM supplies
    that no member of the index carries (recover.supplied_copybooks): the
    build of ROADMAP re-parse item 27 records it with no member and no note
    and leaves the program `ok`. Read as a row with no note, it said 'no
    longer linked ... run recover, then the build' on every CICS program;
    before the item the build wrote NOT FOUND (`noted`) - said as the old
    index's, since nothing is missing either way."""
    return (f"**{supplied_label(product)}** - {supplied_why(copybook, product)}"
            + (f"; {supplied_before()}" if noted else ""))


def _not_found_cell(conn: sqlite3.Connection, copybook: str, member_id: int) -> str:
    """`program`'s 'resolved to' cell for an unresolved COPY: the skip reason
    when the expander skipped it; the precompiler's for SQLCA / SQLDA; the
    product's for a copybook IBM supplies that no member carries; else
    NOT FOUND, and where a member with that name exists now, why the build
    did not use it."""
    from . import recover
    stub = recover.stub_copies(conn, [member_id]).get((member_id, copybook.upper()))
    if stub:
        # the only members of the name are stubs (ROADMAP re-parse item 23): the program's own note says so
        return recover.stub_cell(stub)
    why = recover.skipped_copies(conn, member_id).get((member_id, copybook.upper()))
    if why:
        return skipped_cell(why)
    noted = (member_id, copybook.upper()) in recover.not_found_copies(conn, [member_id])
    product = recover.supplied_copybooks(conn).get(copybook.upper())
    if product:
        return supplied_cell(copybook, product, noted)
    if precompiler_row(copybook, noted):
        return precompiler_cell(copybook)
    note = same_named_note(conn, copybook, (member_id,))
    if not noted:
        # no NOT FOUND note: the program had expanded a copy that left the index after the parse, and the build that
        # made the index did not parse it again (LESSONS 188, 202) - its fields are of that copy, not missing
        return ("**no longer linked** - " + (f"a member with this name exists: {note}" if note else
                                             "the copy this program had expanded left the index after the parse and no "
                                             "member carries the name now: run recover, then the build"))
    return "**NOT FOUND**" + (f" - a member with this name exists: {note}" if note else "")


def _chosen_marked(n_old: int, n_new: int) -> str:
    """How the index marks the members complete with a chosen copybook:
    `ok` (the build of the batch records the choice once), `partial` (an
    index built before ROADMAP re-parse item 18 repeats it as a COPY
    warning), or both when members of both builds sit in one index."""
    if not n_old:
        return ("They are marked `ok`: the build records the choice once, as the 'ambiguous_copybook' row "
                "(ROADMAP re-parse item 18).")
    if not n_new:
        return ("The index marks them `partial` only because the build that made it repeated the resolver's choice as "
                "a COPY warning - an index built before ROADMAP re-parse item 18; the next full re-parse marks them `ok`.")
    return (f"{n_new} of them {'is' if n_new == 1 else 'are'} marked `ok` (the build records the choice once, as the "
            f"'ambiguous_copybook' row - ROADMAP re-parse item 18); the other {n_old} {'is' if n_old == 1 else 'are'} marked "
            "`partial` only because the build that parsed them repeated the resolver's choice as a COPY warning (parsed "
            "before the item); the next full re-parse marks them `ok`.")


def _chosen_members(conn: sqlite3.Connection, limit: int = COVERAGE_ROWS) -> str:
    """Members complete with a copybook chosen among several same-named ones
    with different content: every COPY expanded, so they are complete for the
    copy the resolver named - `ok` with the 'ambiguous_copybook' row (the
    build of the batch), or `partial` only by the resolver's repeated note
    (an index built before ROADMAP re-parse item 18). Their own table, so
    that `coverage` does not count them with the members whose copybook is
    missing (his 701 'partial' with ~60 copybooks missing). A
    choice the program's compiler listing decided (ROADMAP re-parse item 19)
    is the compiler's own record and is said apart from the chain's guesses,
    and so is a chain's pick the listing confirmed through a copy with the
    same text in the library it names (build.LISTING_SAME)."""
    from .build import LISTING_HOW, LISTING_SAME
    rows = conn.execute(f"""
        SELECT m.kind, m.name, m.library, m.parse_status,
               (SELECT COUNT(*) FROM unresolved u WHERE u.member_id = m.id AND u.kind = 'ambiguous_copybook') AS picks,
               (SELECT GROUP_CONCAT(u.detail, CHAR(10)) FROM unresolved u
                WHERE u.member_id = m.id AND u.kind = 'ambiguous_copybook') AS notes
        FROM member m WHERE {_COMPLETE_WITH_CHOICE}
        ORDER BY m.kind, m.name""", (AMBIGUOUS_PICK,)).fetchall()
    if not rows:
        return ""
    by_kind: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)

    def pick_names(members: List[sqlite3.Row]) -> Dict[str, int]:
        """copybook name -> members that took a chosen copy of it"""
        counts: Dict[str, int] = defaultdict(int)
        for r in members:
            for m in (_PICK_RE.search(n) for n in (r["notes"] or "").split("\n")):
                if m:
                    counts[m.group(2)] += 1
        return counts

    def top_names(members: List[sqlite3.Row]) -> str:
        best = sorted(pick_names(members).items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        return ", ".join(f"{k} ({v})" for k, v in best) or "(note not readable)"

    def used(r: sqlite3.Row) -> str:
        m = _PICK_RE.search((r["notes"] or "").split("\n")[0])
        if m:
            tail = "/".join(m.group(3).replace("\\", "/").split("/")[-3:])     # SYSTEM/LIBRARY/member
            how = m.group(4)
            if how.startswith(LISTING_HOW):                                   # 'listing: DATASET' - the word that matters, whole
                how = "listing: " + how[len(LISTING_HOW):]
            elif how.startswith(LISTING_SAME):                                # 'listing: same text as DATASET'
                how = "listing: same text as " + how[len(LISTING_SAME):].rstrip(")")
            first = f"{m.group(2)}: {m.group(1)} copies, used {tail} ({how})"
        else:
            first = (r["notes"] or "")[:140]
        return first[:140] + (f" - and {r['picks'] - 1} more copybook(s)" if r["picks"] > 1 else "")

    # who decided: the program's compiler listing (build.LISTING_HOW in the note - the compiler's own record), the
    # chain with the listing confirming it through a same-text copy (build.LISTING_SAME - a staging library promoted
    # unchanged), or the precedence chain alone (system and library order - a guess the manifest's order can correct)
    n_listing = sum(1 for r in rows for n in (r["notes"] or "").split("\n") if LISTING_HOW in n)
    n_same = sum(1 for r in rows for n in (r["notes"] or "").split("\n") if LISTING_SAME in n)
    n_chain = sum(int(r["picks"]) for r in rows) - n_listing - n_same
    if n_same:
        who = (f"{n_listing} choice{'s' if n_listing != 1 else ''} decided by the program's compiler listing, {n_same} "
               f"picked by system and library order and confirmed by the listing (the same text as the library it names), "
               f"{n_chain} picked by system and library order alone")
    else:
        who = (f"{n_listing} choice{'s' if n_listing != 1 else ''} decided by the program's compiler listing, {n_chain} "
               "picked by system and library order" if n_listing else "the build picked by system and library order")
    out = [f"\n### Complete, with a copybook chosen among several: {len(rows)} member{'s' if len(rows) != 1 else ''} - "
           f"{who}; the 'ambiguous_copybook' rows name the copy used\n"]
    out.append(table(["kind", "members", "most often chosen"],
                     [(k, len(v), top_names(v)) for k, v in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))]))
    out.append("\n" + table(["kind", "member", "library", "copy used"],
                            [(r["kind"], r["name"], r["library"], used(r)) for r in rows[:limit]]))
    if len(rows) > limit:
        out.append(f"_... {len(rows) - limit} more; every one of them: `coverage --all`; `program NAME` shows each "
                   "member's choice under Unresolved in scope_\n")
    out.append(_choices_checked(conn))
    names = f"{len(pick_names(rows))} copybook name(s) are involved - `ambiguous` lists them per department"
    if n_same:
        parts = []
        if n_listing:
            parts.append(f"{n_listing} of the choices {'are' if n_listing != 1 else 'is'} the compiler's own: the program's "
                         "listing names the library the copybook was read from, and that copy was expanded (`listing: "
                         "DATASET` in the table) - nothing to declare for those.")
        parts.append(f"{n_same} {'are' if n_same != 1 else 'is'} confirmed by the listing: it names a library whose copy has "
                     "the same text as the copy expanded - a copybook compiled against staging and promoted unchanged "
                     "(`listing: same text as DATASET`) - nothing to declare for those either.")
        if n_chain:
            parts.append(f"The other {n_chain} follow{'s' if n_chain == 1 else ''} `COPY ... OF`, then the member's own "
                         "system in its declared copybook order, then the manifest's authoritative copy, and one of those "
                         "is wrong only where the manifest's system or copybook order is.")
        rule = " ".join(parts) + f" {names[0].upper()}{names[1:]}."
    elif not n_listing:
        rule = ("The choice follows `COPY ... OF`, then the member's own system in its declared copybook order, then the "
                f"manifest's authoritative copy; {names}, and a choice is wrong only where the manifest's system or "
                "copybook order is.")
    elif not n_chain:
        rule = ("Every choice is the compiler's own: the program's listing names the library the copybook was read from, "
                f"and that copy was expanded (`listing: DATASET` in the table) - nothing to declare; {names}.")
    else:
        rule = (f"{n_listing} of the choices {'are' if n_listing != 1 else 'is'} the compiler's own: the program's listing "
                "names the library the copybook was read from, and that copy was expanded (`listing: DATASET` in the "
                f"table) - nothing to declare for those. The other {n_chain} follow{'s' if n_chain == 1 else ''} `COPY ... OF`, "
                "then the member's own system in its declared copybook order, then the manifest's authoritative copy, and "
                f"one of those is wrong only where the manifest's system or copybook order is; {names}.")
    n_old = sum(1 for r in rows if r["parse_status"] == "partial")
    marked = _chosen_marked(n_old, len(rows) - n_old)
    out.append(f"\n> {'This member is' if len(rows) == 1 else f'These {len(rows)} members are'} NOT parsed only in "
               f"part: every COPY expanded, and their facts are complete for the copy named. {marked} {rule}\n")
    return "".join(out)


def _failed_members(conn: sqlite3.Connection, limit: int = COVERAGE_ROWS) -> str:
    """Members not indexed at all. The build printed each one when it happened
    and lists them at the end of ITS run - but a member that hit a time
    limit or froze the parser is not parsed again on a later run, so only
    the index still knows it: this table is the complete list."""
    rows = conn.execute("""
        SELECT kind, name, library, COALESCE(parse_error, '') AS parse_error, NULL AS why
        FROM member WHERE parse_status = 'failed' ORDER BY kind, name""").fetchall()
    if not rows:
        return "\n### Members not indexed (failed)\n_none_\n"
    by_kind: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_kind[r["kind"]].append(r)
    out = [f"\n### Members not indexed (failed) ({len(rows)}) - nothing of them is in any answer\n"]
    out.append(table(["kind", "members", "most common reason"],
                     [(k, len(v), _top_reason(v)) for k, v in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))]))
    shown = [(r["kind"], r["name"], r["library"], r["parse_error"][:110]) for r in rows[:limit]]
    out.append("\n" + table(["kind", "member", "library", "reason"], shown))
    if len(rows) > limit:
        out.append(f"_... {len(rows) - limit} more; every one of them: `coverage --all`_\n")
    out.append("\n> A member that hit the time limit or froze the parser (MemberTimeout, ParserStuck, "
               "InventoryTimeout) is not tried again until the parser changes: report its kind, size and shape "
               "(never its content) so the parser can be fixed. Any other reason names the toolkit line that failed - "
               "report that line.\n")
    return "".join(out)


def _top_reason(rows: List[sqlite3.Row]) -> str:
    counts: Dict[str, int] = {}
    for r in rows:
        key = (r["why"] or r["parse_error"] or "not stated").split(":", 1)[0][:40]
        counts[key] = counts.get(key, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1])
    return f"{best[0]} ({best[1]})"


def _recovered_shadowing(conn: sqlite3.Connection) -> str:
    """COPY statements that expand a recovered copybook (atlas.recover) which
    the build of this toolkit would not expand for that program: a real
    member of the name sits in the program's own system or in SHARED (or
    has no system), or the copy was written for another system and a real
    member is held anywhere (build.recovered_gives_way). A program is never
    the real member - a member of kind cobol the build parsed as one, any
    status but 'skipped' (build.holds_program): the copying program itself,
    which the build never expands into itself, and a callee whose parameter
    copybook has its own name (LESSONS 209, 210). A build before ROADMAP
    re-parse item 11 ranked the recovered copy like any library (the same
    folder, first found), so until the recovered one is removed a program
    may carry the recovered layout. The build of this toolkit drops a
    recovered copy on exactly this test and parses the copying programs
    again when the real member arrives: on an index it built this finds
    nothing. A real member only another system holds is that system's copy:
    a program's own system's recovered copy, and SHARED's, stay while it is
    the only one (atlas.recover keeps them), and this says nothing of them
    (LESSONS 209)."""
    home = "(CASE WHEN UPPER(COALESCE({0}.system, '')) = 'SHARED' THEN '' ELSE UPPER(COALESCE({0}.system, '')) END)"
    m, p, r = home.format("m"), home.format("p"), home.format("r")
    rows = conn.execute(f"""
        SELECT r.name, COUNT(*) FROM copy_use c
        JOIN member r ON r.id = c.resolved_member_id
        JOIN member p ON p.id = c.member_id
        WHERE UPPER(r.library) = 'RECOVERED-COPYBOOKS'
          AND EXISTS (SELECT 1 FROM member m WHERE UPPER(m.name) = UPPER(r.name) AND m.id != r.id
                      AND m.id != c.member_id
                      AND m.kind IN ('copybook', 'cobol', 'sql', 'unknown')
                      AND NOT (m.kind = 'cobol' AND COALESCE(m.parse_status, '') != 'skipped')
                      AND UPPER(COALESCE(m.library, '')) != 'RECOVERED-COPYBOOKS'
                      AND ({m} IN ('', {p}) OR {r} NOT IN ('', {p})))
        GROUP BY r.name ORDER BY 2 DESC""").fetchall()
    if not rows:
        return ""
    names = ", ".join(r[0] for r in rows[:8]) + (" ..." if len(rows) > 8 else "")
    return (f"\n> **{sum(r[1] for r in rows)} COPY statement(s) still expand a recovered copybook although the estate now "
            f"holds the real member** ({len(rows)} copybook(s): {names}). Run `python -m atlas.recover --db atlas.db` - it "
            "removes the recovered copies whose real member arrived and marks their programs - then your usual build.\n")


def _supplied_table(conn: sqlite3.Connection, supplied: Dict[str, str], copiers: Dict[str, Dict[int, None]]) -> str:
    """coverage's 'IBM-supplied copybooks, not in the estate': per copybook
    IBM supplies (or the manifest's system_includes names) that no member
    carries and a program copies, how many programs are parsed only in part
    for it and how many copy it. The build of ROADMAP re-parse item 27
    counts none of them as a gap, so the first column reads 0; an index
    built before the item wrote NOT FOUND for each and marked every CICS
    program partial (tools/synth/repro/F16) - the column counts those, and
    the note says the next build clears them. '' when no program copies
    one."""
    if not copiers:
        return ""
    from . import recover
    noted = recover.not_found_copies(conn, [i for ids in copiers.values() for i in ids])
    rows = []
    before: Set[int] = set()                                  # programs whose own note still says NOT FOUND for one
    for book in sorted(copiers, key=lambda b: (-len(copiers[b]), b)):
        part = {i for i in copiers[book] if (i, book) in noted}
        before |= part
        rows.append((book, len(part), len(copiers[book]), recover.supplied_where(supplied[book])))
    # the words follow what the table holds (LESSONS 216): the heading names IBM only for IBM's copybooks, the
    # libraries only of the products listed, DFHENTER only beside DFHAID, and 'no program is parsed only in part'
    # only when none is - on the index he has, built before ROADMAP re-parse item 27, the table counts them
    products = {supplied[b] for b in copiers}
    ibm = products - {expand.MANIFEST_PRODUCT}
    manifest = expand.MANIFEST_PRODUCT in products
    heading = ("IBM-supplied copybooks, not in the estate" + (", and those the manifest's `system_includes` names"
                                                               if manifest else "")
               if ibm else "Copybooks the manifest's `system_includes` names, not in the estate")
    libs = [f"{p}: {expand.IBM_LIBRARY[p]}" for p in sorted(ibm) if p in expand.IBM_LIBRARY]
    if manifest:
        libs.append("a name the manifest's `system_includes` adds: the product library the shop's compile names")
    items = "Their items (DFHAID's DFHENTER, for one)" if "DFHAID" in copiers else "Their items"
    out = [f"\n### {heading}\n",
           table(["copybook", "programs parsed only in part for it", "programs copying it", "supplied with"], rows),
           f"\n> The compile reads these from the product's own library ({'; '.join(libs)}), which a shop does not "
           "keep among its own copybooks: nothing is to fetch"
           + ("" if before else ", and no program is parsed only in part for one")
           + f". {items} are not in the index, so `field` finds none of them. A copy the shop keeps in its own "
           "COPYLIB is expanded like any copybook, and is not in this table.\n"]
    if before:
        one = len(before) == 1
        out.append(f"\n> {len(before)} program{'' if one else 's'} copying them still say{'s' if one else ''} NOT FOUND "
                   f"for one and {'is' if one else 'are'} in 'Members parsed only in part' above: "
                   f"{supplied_before(len(before))}.\n")
    return "".join(out)


def cmd_coverage(conn: sqlite3.Connection, everything: bool = False) -> str:
    limit = 10 ** 9 if everything else COVERAGE_ROWS
    out = ["# Coverage - what the index does and does not know\n", f"\n_{index_header(conn)}_\n"]
    libs = conn.execute("SELECT dataset, fetched_at, expected, present, complete, missing, stale FROM library ORDER BY dataset").fetchall()
    if libs:
        out.append("\n### Fetched libraries (what the host listed vs what arrived)\n")
        out.append(table(["dataset", "fetched", "members", "present", "state", "missing (first few)"],
                         [(l["dataset"], (l["fetched_at"] or "")[:10], l["expected"], l["present"],
                           "complete" if l["complete"] else "**INCOMPLETE**",
                           ", ".join(_jl(l["missing"])[:6]) + (" ..." if len(_jl(l["missing"])) > 6 else "")) for l in libs]))
    out.append("\n### Members\n")
    out.append(table(["kind", "status", "count"], conn.execute(
        "SELECT kind, parse_status, COUNT(*) FROM member GROUP BY 1,2 ORDER BY 1,2").fetchall()))
    n_part = conn.execute("SELECT COUNT(*) FROM member WHERE parse_status = 'partial'").fetchone()[0]
    n_chosen = conn.execute(f"SELECT COUNT(*) FROM member m WHERE m.parse_status = 'partial' AND {_CHOSEN_PRED}",
                            (AMBIGUOUS_PICK,)).fetchone()[0]
    n_ok_chosen = conn.execute(f"SELECT COUNT(*) FROM member m WHERE {_CHOSEN_OK_PRED}").fetchone()[0]
    # the `partial` word covers a chosen copybook only on an index built before ROADMAP re-parse item 18
    chosen_clause = (" - or, for a COBOL member, a copybook was chosen among several same-named ones: complete, its own "
                     "table" if n_chosen else "")
    out.append("\n- **ok**: parsed, its facts are in the index. **skipped**: nothing to parse - either the kind has "
               "no parser of its own (control cards, REXX, SQL scripts, assembler: still indexed and searchable, and a "
               "control card is read in full where a job points at it; listings and unrecognised members are recorded "
               "by name only - `search` does not see their text), or the "
               "member sits in a source library but is not a program (no PROGRAM-ID and no DIVISION header - a "
               "procedure copybook or a card deck filed there); the member itself says which. **partial**: a parser "
               f"ran but could not complete the picture - the next table says which members and why{chosen_clause}. "
               "**failed**: not "
               "indexed at all - the table after it names every one, including those given up on before a restart.\n")
    if n_chosen:
        n_true = n_part - n_chosen
        out.append(f"- of the {n_part} members marked `partial`, **{n_true} {'is' if n_true == 1 else 'are'} parsed only "
                   f"in part** and **{n_chosen} {'is' if n_chosen == 1 else 'are'} complete with a copybook chosen among "
                   "several** (the two tables below).\n")
    if n_ok_chosen:
        one = n_ok_chosen == 1
        out.append(f"- {n_ok_chosen} member{'' if one else 's'} marked `ok` took a copybook chosen among several same-named "
                   f"ones: complete for the copy named, and listed in a table of {'its' if one else 'their'} own below.\n")
    from . import recover
    n_refiled = len(recover.refiled_members(conn))
    if n_refiled:
        # a copybook marked `skipped` because atlas.recover re-filed it is not a gap: the programs copying it expand
        # it; only its own layout rows wait for the re-parse
        one = n_refiled == 1
        out.append(f"- {n_refiled} copybook{'' if one else 's'} marked `skipped` {'was' if one else 'were'} re-filed by "
                   f"atlas.recover (the classifier had filed {'it' if one else 'them'} as another kind - "
                   f"{recover.EARLIER_ITEMS}): not a gap in the parse - the programs copying "
                   f"such a member expand it; its own layout rows arrive with the next full re-parse. `copybook NAME` says so "
                   "on each.\n")
    out.append(_partial_members(conn, limit))
    out.append(_chosen_members(conn, limit))
    out.append(_failed_members(conn, limit))
    out.append(_recovered_shadowing(conn))
    out.append("\n### Call resolution\n")
    out.append(table(["kind", "resolution", "count"], conn.execute(
        "SELECT kind, resolution, COUNT(*) FROM call_edge GROUP BY 1,2").fetchall()))
    out.append("\n### Copybooks not found\n")
    # a PROGRAM's row only: the build records a copybook member's own COPY statements with no
    # resolved_member_id, ever (it parses a copybook for copies, never resolves them), so such a row says
    # nothing - it listed a copybook every program had found as 'not found' (LESSONS 184); a program
    # carries its own row for every nested COPY, so its rows are the whole picture - less the COPYs the
    # expander SKIPPED (recursive, nested too deep), whose member the program had found: those are said
    # under the table, not counted in it (LESSONS 185)
    from . import recover
    skipped = recover.skipped_copies(conn)
    # a copybook IBM supplies that no member carries is not a copybook not found: the compile reads it from the
    # product's library - its own table after this one (ROADMAP re-parse item 27; DFHAID sent him to fetch a library
    # the estate never holds, tools/synth/repro/F16)
    supplied = recover.supplied_copybooks(conn)
    uses: Dict[str, int] = {}
    copiers_of: Dict[str, Dict[int, None]] = defaultdict(dict)
    supplied_by: Dict[str, Dict[int, None]] = defaultdict(dict)          # copybook -> programs copying it
    skips: Dict[Tuple[str, str], List[str]] = defaultdict(list)          # (copybook, why) -> programs
    for book, mid, mname in conn.execute(
            "SELECT c.copybook, c.member_id, m.name FROM copy_use c JOIN member m ON m.id = c.member_id "
            "WHERE c.resolved_member_id IS NULL AND m.kind = 'cobol' AND c.copybook NOT IN ('SQLCA','SQLDA') "
            "ORDER BY c.id").fetchall():
        if (book or "").upper() in supplied:
            supplied_by[(book or "").upper()][int(mid)] = None
            continue
        why = skipped.get((int(mid), (book or "").upper()))
        if why:
            if mname not in skips[(book, why)]:
                skips[(book, why)].append(mname)
            continue
        uses[book] = uses.get(book, 0) + 1
        copiers_of[book][int(mid)] = None
    nf_rows = []
    states: List[Dict[str, object]] = []
    for book, n in sorted(uses.items(), key=lambda kv: (-kv[1], kv[0]))[:40]:
        note, st = same_named_state(conn, book, list(copiers_of[book]))
        nf_rows.append((book, n, f"yes: {note}" if note else "-"))
        states.append(st)
    # the names no member carries, looked for on disk - one walk of the estate root, only when the table has such a
    # name (LESSONS 192); a name a member carries is said by the column before
    _root, cells = disk_verdicts(conn, [b for b, _n, note in nf_rows if note == "-"])
    nf_rows = [(book, n, note, cells.get(str(book).upper(), "in the index (the column before says as what)"))
               for book, n, note in nf_rows]
    out.append(table(["copybook", "uses", "a member with this name exists?", "on disk?"], nf_rows))
    if skips:
        out.append("\n> Not in this table: " + "; ".join(f"`COPY {b}` skipped - {w} in {', '.join(ps[:6])}"
                                                          + (f", +{len(ps) - 6} more" if len(ps) > 6 else "")
                                                          for (b, w), ps in sorted(skips.items()))
                   + ". A skipped COPY is not a copybook not found: the member is in the index and the expander stopped "
                   "there (a copybook copying itself, or nesting deeper than 12); the reason stands beside the program "
                   "under 'Members parsed only in part' (a long chain of nested COPYs is cut short in that column; "
                   "`program NAME` prints it whole).\n")
    if any(r[2] != "-" and not st.get("stub") for r, st in zip(nf_rows, states)):
        # 'parsed before it arrived' and 'left the index after the parse' are states only a build before ROADMAP
        # re-parse item 21 left (the build of this toolkit parses the programs again for every kind the resolver
        # expands, and whenever a member they expanded goes, changes or is recorded again): each is said when a cell
        # says it, not offered as a cause on an index where no row is of that sort; and why nothing parsed them again
        # after an arrival - the kind - only for a member of a kind the earlier forcing left out (LESSONS 202)
        late = [st for st in states if st.get("late")]
        left = [st for st in states if st.get("left")]
        odd = any(k not in ("copybook", "cobol") for st in late for k in st["kinds"])      # type: ignore[union-attr]
        causes = []
        if late:
            causes.append("the programs that copy it were parsed before it arrived and nothing parsed them again"
                          + (" (the build that made this index parsed them again only for a member filed copybook or "
                             "cobol - ROADMAP re-parse item 21)" if odd else "")
                          + ": `python -m atlas.recover --db atlas.db` marks them, then run your usual build")
        if left:
            causes.append("the programs had expanded a copy of it that left the index after their parse - "
                          f"{recover.LEFT_CAUSES} - and the build that made this index did not parse them again (ROADMAP "
                          "re-parse item 21), so their fields are those of the earlier read: the same run of recover marks "
                          "them, then run your usual build")
        out.append("\n> A copybook marked `yes` is not missing - a member with its name is in the index. "
                   + ("Either " + "; or ".join(causes) + "; or the member" if causes else "Where the cell says so, the member")
                   + " is filed as a kind the build never "
                   "expands, because the folder name decided its kind (its text carries no signature - no level numbers, "
                   "no COBOL statements): rename the folder to end in COPYLIB, or declare the library's kind in the UI's "
                   "table (the manifest kinds), and build. "
                   + ("A member filed `unknown` is expanded but has no parser of its own, so its own lines are not indexed "
                      "or citable until that same folder fix is applied. "
                      if any("unknown" in st["kinds"] for st in late + left) else "")      # type: ignore[operator]
                   + "Where the cell "
                   "says `by its content`, a line of the member's own text has the shape of an Assembler, listing or MFS "
                   "member, which no folder change alters - the cell says what does. On an index built before "
                   f"{recover.EARLIER_ITEMS} a line of a copybook's own text could decide it (a START- name read as "
                   "Assembler, a comment naming MODULE MAP as a listing, a first word MSG as MFS) and a procedure copybook "
                   "took its folder's kind: where the classifier of this toolkit reads it as a copybook, `python -m "
                   "atlas.recover --db atlas.db` re-files it as one in the index, then build.\n")
    if any(st.get("stub") for st in states):
        # only a stub carries the name (ROADMAP re-parse item 23)
        out.append("\n> A copybook whose cell says `a stub` is held only as a member of numbers - no text a compiler could "
                   "compile - so the build never expands it and the programs copying it are parsed only in part: they were "
                   "compiled against another copy. `python -m atlas.recover --db atlas.db --from FOLDER` writes the copybook "
                   "from their compiler listings; or fetch the library the listings name.\n")
    out.append(_supplied_table(conn, supplied, supplied_by))
    out.append("\n### Unresolved by kind - what the index could NOT work out, and what closes each one\n")
    # a chosen copybook's rows apart from the rest; the ones the program's compiler listing decided (build.LISTING_HOW
    # in the note) or confirmed through a same-text copy (build.LISTING_SAME) apart again: those have nothing to declare.
    # An 'expand' note repeating a choice comes only from a build before ROADMAP re-parse item 18, and no such build read
    # the listing (item 19 ships with item 18): it is always the chain's choice
    # a choice whose copy used is a recovered one (a RECOVERED-COPYBOOKS folder in the path the note names) apart too:
    # it is kept on purpose - the program's own system and SHARED hold no real member - and removing the copy would
    # hand the program the other library's layout (LESSONS 210)
    from .build import LISTING_HOW, LISTING_SAME, RECOVERED_FOLDER
    rows = conn.execute("""SELECT CASE WHEN kind = 'expand' AND instr(COALESCE(detail, ''), ?) > 0
                                       THEN 'expand (copybook chosen among several)'
                                       WHEN kind = 'ambiguous_copybook' AND instr(COALESCE(detail, ''), ?) > 0
                                       THEN 'ambiguous_copybook (decided by the listing)'
                                       WHEN kind = 'ambiguous_copybook' AND instr(COALESCE(detail, ''), ?) > 0
                                       THEN 'ambiguous_copybook (confirmed by the listing)'
                                       WHEN kind = 'ambiguous_copybook' AND (instr(UPPER(COALESCE(detail, '')), ?) > 0
                                                                          OR instr(UPPER(COALESCE(detail, '')), ?) > 0)
                                       THEN 'ambiguous_copybook (a recovered copy used)'
                                       ELSE kind END AS k, COUNT(*)
                           FROM unresolved GROUP BY 1 ORDER BY 2 DESC""",
                        (AMBIGUOUS_PICK, LISTING_HOW, LISTING_SAME, f"\\{RECOVERED_FOLDER}\\",
                         f"/{RECOVERED_FOLDER}/")).fetchall()
    out.append(table(["kind", "count", "what it means", "what closes it"],
                     [(k, n, *UNRESOLVED_MEANING.get(k, ("(see the members below)", "send this kind's name for a fix")))
                      for k, n in rows]))
    if rows:
        out.append("\n> An answer that depends on one of these is incomplete to that extent - every report repeats "
                   "the ones in its own scope under `Unresolved in scope`, and the model must copy them into its "
                   "answer. Most are closed by fetching one more library; the rest are run-time values that no "
                   "source can tell you.\n")
    out.append("\n### DD direction sources\n")
    out.append(table(["source", "count"], conn.execute(
        "SELECT mode_source, COUNT(*) FROM dd WHERE dsn_resolved IS NOT NULL GROUP BY 1 ORDER BY 2 DESC").fetchall()))
    n_sched = conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]
    n_tx = conn.execute("SELECT COUNT(*) FROM transaction_def").fetchone()[0]
    n_amb = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member WHERE distinct_content>1").fetchone()[0]
    out.append(f"\n### Not loaded\n- scheduler definitions: {n_sched} jobs\n- online transaction definitions: {n_tx}\n"
               f"- members with same name+kind and DIFFERENT content: {n_amb}\n")
    out.append(_coverage_extras(conn))
    out.append("\nAnything in this report that reads 'NO' or a large count is a class of question the index "
               "will answer WRONG, not just incompletely. Fix the input before trusting the output.\n")
    return "".join(out)


def cmd_search(conn: sqlite3.Connection, term: str, kind: Optional[str], limit: int) -> str:
    q = "SELECT member_name, kind, line_no, text FROM src_fts WHERE src_fts MATCH ?"
    args: list = [term]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " LIMIT ?"
    args.append(limit)
    try:
        rows = conn.execute(q, args).fetchall()
    except sqlite3.OperationalError as e:
        return f"search error: {e}\n(quote the term: \"WS-PLCY-STAT-CD\")\n"
    out = [f"# search {term!r} ({len(rows)} hits, limit {limit})\n\n"]
    for r in rows:
        out.append(f"- `{r['member_name']}:{r['line_no']}` [{r['kind']}] {r['text'].strip()[:110]}\n")
    return "".join(out)


def cmd_cite(conn: sqlite3.Connection, member: str, rng: str, program: Optional[str],
             kind: Optional[str] = None) -> str:
    a, _, b = rng.partition("-")
    s, e = int(a), int(b or a)
    if program:
        prog = programs_named(conn, program)
        if not prog:
            return f"program {program} not found\n"
        m1, l1, _d, _v = origin(conn, prog[0]["id"], s)
        m2, l2, _d, _v = origin(conn, prog[0]["id"], e)
        if m1 != m2:
            return f"expanded lines {s}-{e} span two members ({m1} / {m2}); cite each separately\n"
        member, s, e = m1, l1, l2
    # SAMPPGM.cbl / SAMPPGM.psb / SAMPPGM.jcl share a name: `--kind` picks,
    # else the program-ish kind wins and the others are named.
    order = {"cobol": 0, "copybook": 1, "jcl": 2, "proc": 3, "psb": 4, "dbd": 5}
    want_sys = None
    if "/" in member:                                     # GC-DEV/WALKPGM: that system's copy
        want_sys, _, member = member.partition("/")
    rows = conn.execute("SELECT path, kind, system FROM member WHERE UPPER(name)=? ORDER BY authoritative DESC, path",
                        (member.upper(),)).fetchall()
    if want_sys:
        rows = [r for r in rows if (r["system"] or "").upper() == want_sys.upper()]
        if not rows:
            return f"member {member} has no copy in system {want_sys}\n"
    if kind:
        rows = [r for r in rows if (r["kind"] or "").lower() == kind.lower()]
    if not rows:
        return f"member {member} not found" + (f" with kind {kind}" if kind else "") + "\n"
    rows = sorted(rows, key=lambda r: order.get(r["kind"], 9))
    row = rows[0]
    text, data, enc = reader.load(row["path"])
    recs = reader._split_records(text, data, enc)
    tag = f"{row['system']}/{member.upper()}" if row["system"] else member.upper()
    out = [f"{tag}({row['kind']})  {row['path']}\n"]
    others = sorted({r["kind"] for r in rows[1:]} - {row["kind"]})
    if others:
        out.append(f"  (other members named {member.upper()}: {', '.join(others)} - use --kind; "
                   f"cite as [[{tag}({row['kind']}) line \"token\"]])\n")
    if row["system"] and len(rows) > 1:
        out.append(f"  (this is the {row['system']} copy - cite as [[{tag} line \"token\"]])\n")
    for i in range(max(1, s), min(len(recs), e) + 1):
        out.append(f"{i:6d} | {recs[i-1].rstrip()}\n")
    return "".join(out)


# --------------------------------------------------------------------------
# pack - the token-frugal handoff to a model
# --------------------------------------------------------------------------

_PATH_RX = re.compile(r"`?[A-Za-z]:\\[^`\s|]+`?|`?/[A-Za-z0-9_./\-]+/[^`\s|]+`?")


def _strip_paths(text: str) -> str:
    """Full Windows/POSIX paths carry nothing a model needs and leak the
    laptop's layout into whatever is pasted outside: keep the file name."""
    def repl(m: "re.Match[str]") -> str:
        s = m.group(0).strip("`")
        return "`" + os.path.basename(s.rstrip("/\\")) + "`" if m.group(0).startswith("`") else os.path.basename(s)
    return _PATH_RX.sub(repl, text)


def _fit_budget(sections: List[Tuple[str, str, int]], budget: Optional[int]) -> str:
    """Assemble (title, text, priority) sections inside a character budget:
    lowest priority number is kept first; a section that does not fit is
    truncated with a note rather than silently dropped."""
    if not budget:
        return "".join(t for _n, t, _p in sections)
    keep: Dict[int, str] = {}
    used = 0
    for idx, (_name, text, _prio) in sorted(enumerate(sections), key=lambda x: (x[1][2], x[0])):
        room = budget - used
        if room <= 0:
            continue
        if len(text) <= room:
            keep[idx] = text
            used += len(text)
        elif room > 120:
            keep[idx] = text[:room - 60].rstrip() + f"\n_...section truncated to fit --budget {budget}_\n"
            used = budget
    return "".join(keep[i] for i in range(len(sections)) if i in keep)


def _evidence_lines(conn: sqlite3.Connection, pid: int, member_name: str, max_lines: int,
                    want: Optional[set] = None) -> str:
    out = ["\n---\n## Evidence lines (source, for citation)\n\n",
           "Cite as `[[MEMBER line \"token\"]]` or `[[MEMBER:line \"token\"]]`. Lines are from the ORIGINAL member.\n\n"]
    picks: List[Tuple[str, int]] = []
    q = {
        "CALL": "SELECT line FROM call_edge WHERE program_id=? ORDER BY line",
        "SQL": "SELECT start_line FROM sql_stmt WHERE program_id=? ORDER BY start_line",
        "DLI": "SELECT line FROM dli_call WHERE program_id=? ORDER BY line",
        "CICS": "SELECT line FROM cics_cmd WHERE program_id=? ORDER BY line",
        "SELECT": "SELECT line FROM file_decl WHERE program_id=? ORDER BY line",
        "IO": "SELECT line FROM io_op WHERE program_id=? AND target_kind='file' ORDER BY line",
        "PARA": "SELECT start_line FROM paragraph WHERE program_id=? ORDER BY ordinal",
        "SET": "SELECT line FROM literal_ref WHERE program_id=? AND context='move_to' ORDER BY line",
        # IF/WHEN with a literal or an 88: the test conditions a tester needs.
        "COND": "SELECT line FROM literal_ref WHERE program_id=? AND context IN ('compare','when') ORDER BY line",
    }
    for tag, sql in q.items():
        if want and tag not in want:
            continue
        for r in conn.execute(sql, (pid,)):
            picks.append((tag, r[0]))
    seen = set()
    n = 0
    for tag, exp_line in picks:
        m, ln, _d, _v = origin(conn, pid, exp_line)
        if not m or (m, ln) in seen:
            continue
        seen.add((m, ln))
        txt = source_line(conn, m, ln)
        out.append(f"- {tag:<6} `{m}:{ln}`  {txt[:100]}\n")
        n += 1
        if n >= max_lines:
            out.append(f"_...capped at {max_lines} evidence lines; use `cite {member_name} a-b` for more_\n")
            break
    return "".join(out)


def cmd_pack(conn: sqlite3.Connection, name: str, max_lines: int = 120, kind: Optional[str] = None,
             budget: Optional[int] = None, sections: Optional[str] = None, keep_paths: bool = False) -> str:
    """The token-frugal hand-off: a dossier plus the exact source lines
    behind its facts, inside a character budget, for a program, a job (with
    the FULL control cards), a copybook (with its layout), a transaction or a
    field. The header states the approximate token cost."""
    kind = (kind or "").lower() or None
    if kind is None:
        if programs_named(conn, name):
            kind = "program"
        elif conn.execute("SELECT 1 FROM job j JOIN member m ON m.id=j.member_id WHERE UPPER(j.job_name)=? OR UPPER(m.name)=?",
                          (name.upper(), name.upper())).fetchone() or \
                conn.execute("SELECT 1 FROM proc_def WHERE UPPER(proc_name)=?", (name.upper(),)).fetchone():
            kind = "job"
        elif conn.execute("SELECT 1 FROM member WHERE UPPER(name)=? AND kind='copybook'", (name.upper(),)).fetchone():
            kind = "copybook"
        elif conn.execute("SELECT 1 FROM transaction_def WHERE UPPER(tran_code)=?", (name.upper(),)).fetchone():
            kind = "transaction"
        elif _field_defs(conn, name):
            kind = "field"
        else:
            return f"{name}: not a program, job/PROC, copybook, transaction or field in the index\n"
    wanted = {s.strip().upper() for s in sections.split(",")} if sections else None
    parts: List[Tuple[str, str, int]] = []           # (name, text, priority)

    def add(title: str, text: str, prio: int) -> None:
        if not text:
            return
        if wanted and title.upper() not in wanted and title.upper() != "HEADER":
            return
        parts.append((title, text, prio))

    if kind == "program":
        p = programs_named(conn, name)[0]
        doss = cmd_program(conn, name)
        # split the dossier at its section headers so the budget can rank them
        head, *rest = re.split(r"(?m)^(?=### )", doss)
        add("header", head, 0)
        prio = {"RUNS": 1, "CALLS": 1, "CALLED": 1, "FILES": 2, "COPYBOOKS": 2, "DB2": 2, "IMS": 2, "CICS": 2,
                "EXTERNAL": 2, "STRUCTURE": 5, "UNRESOLVED": 1, "DOCUMENTS": 6}
        for sec in rest:
            title = sec.split("\n", 1)[0].lstrip("# ").strip()
            key = title.split()[0].upper() if title else "OTHER"
            add(key, sec, prio.get(key, 4))
        add("evidence", _evidence_lines(conn, p["id"], p["member_name"], max_lines), 3)
    elif kind == "job":
        add("header", cmd_job(conn, name), 0)
        # the cards ARE the behaviour of a batch step: full text, cited
        cards = []
        for r in conn.execute("""SELECT j.job_name, s.step_name, s.from_proc, d.dd_name, d.sysin_text, d.card_member, d.line,
                                        m.name AS mem
                                 FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                 LEFT JOIN member m ON m.id=j.member_id LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                 WHERE d.sysin_text IS NOT NULL
                                   AND (((UPPER(j.job_name)=? OR UPPER(m.name)=?) AND s.proc_called IS NULL)
                                        OR UPPER(pd.proc_name)=?)
                                 ORDER BY s.ordinal, d.line""",
                              (name.upper(), name.upper(), name.upper())):
            src = r["card_member"] or r["from_proc"] or r["mem"]
            cards.append(f"\n**{r['step_name']} {r['dd_name']}** (cards from `{src}`"
                         + (f", line {r['line']}" if not r["card_member"] else ", the whole member") + ")\n```\n")
            for i, ln in enumerate(r["sysin_text"].splitlines(), 1):
                cards.append(f"{(i if r['card_member'] else r['line'] + i):5d} | {ln.rstrip()[:100]}\n")
            cards.append("```\n")
        if cards:
            add("cards", "\n---\n## Control cards (full text; cite `MEMBER:line`)\n" + "".join(cards), 1)
    elif kind == "copybook":
        add("header", cmd_copybook(conn, name), 0)
        add("layout", "\n---\n" + cmd_layout(conn, name), 1)
    elif kind == "transaction":
        add("header", cmd_transaction(conn, name), 0)
        for (pg,) in conn.execute("SELECT DISTINCT program FROM transaction_def WHERE UPPER(tran_code)=? AND program IS NOT NULL",
                                  (name.upper(),)):
            if programs_named(conn, pg):
                add("program", "\n---\n" + cmd_program(conn, pg), 2)
    else:
        add("header", cmd_field(conn, name), 0)
        add("flow", "\n---\n" + flow.render(conn, name, hops=2, width=8, nodes=60, header=False), 2)
    if not keep_paths:
        # per section, before the budget; the flow section names members, never files, and
        # "COMPUTE/STRING/FUNCTION" would read as a POSIX path
        parts = [(t, x if t == "flow" else _strip_paths(x), p) for (t, x, p) in parts]
    text = _fit_budget(parts, budget)
    est = len(text) // 4
    return (f"<!-- pack {kind} {name.upper()}: ~{est} tokens ({len(text)} chars)"
            + (f", budget {budget}" if budget else "") + f"; {index_header(conn)} -->\n") + text


# --------------------------------------------------------------------------
# documents (prose - never facts) and the pictures inside them
# --------------------------------------------------------------------------

def _section_label(n: Optional[int]) -> str:
    if not n:
        return "table"
    return f"image {n - 1000}" if n >= 1000 else f"section {n}"


def _doc_mentions(conn: sqlite3.Connection, term: str, limit: int = 8) -> List[sqlite3.Row]:
    try:
        rows = conn.execute("""SELECT member_name, member_id, line_no, substr(text,1,200) AS snip FROM src_fts
                               WHERE src_fts MATCH ? AND kind='doc' LIMIT 80""", (f'"{term}"',)).fetchall()
    except sqlite3.OperationalError:
        return []
    seen, out = set(), []
    for r in rows:
        key = (r["member_name"], r["line_no"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _hit_line(conn: sqlite3.Connection, member_id: int, ordinal: Optional[int], term: str, fallback: str,
              width: int = 160) -> str:
    """The line of the section that holds the term - for a spreadsheet tab
    that is the ROW (`row 12: TC-GEN-01 | ... | PASS`), not the tab's first
    line; the section's start when the term is not on one line."""
    if ordinal:
        r = conn.execute("SELECT text FROM doc_section WHERE member_id=? AND ordinal=?", (member_id, ordinal)).fetchone()
        if r and r["text"]:
            t = term.strip('"').upper()
            for ln in r["text"].split("\n"):
                if t in ln.upper():
                    return ln.strip()[:width]
    return fallback.replace("\n", " ")[:width]


def _docs_section(conn: sqlite3.Connection, term: str) -> str:
    hits = _doc_mentions(conn, term)
    if not hits:
        return ""
    return ("\n### Documents mentioning it (prose: what was INTENDED, not what the code does)\n"
            + table(["document", "where", "excerpt", "cite"],
                    [(h["member_name"], _section_label(h["line_no"]),
                      _hit_line(conn, h["member_id"], h["line_no"], term, h["snip"], 120),
                      f"{h['member_name']}:{h['line_no'] or 0}") for h in hits]))


def cmd_docs(conn: sqlite3.Connection, term: str) -> str:
    try:
        rows = conn.execute("""SELECT f.member_name, f.member_id, f.line_no, substr(f.text,1,260) AS snip
                               FROM src_fts f WHERE src_fts MATCH ? AND kind='doc' LIMIT 400""",
                            (f'"{term}"',)).fetchall()
    except sqlite3.OperationalError as e:
        return f"search error: {e}\n"
    out = [f"# Documents mentioning `{term}`\n"]
    if not rows:
        return out[0] + ("\n_none_ - no indexed document contains it. Documents are indexed from the folders in "
                         "`extra_roots` (UI: Document folders); pictures only after `OCR images`.\n")
    by_doc: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_doc[r["member_name"]].append(r)
    for doc, hits in by_doc.items():
        mid = hits[0]["member_id"]
        m = conn.execute("SELECT path FROM member WHERE id=?", (mid,)).fetchone()
        n_img, n_ocr = conn.execute("SELECT COUNT(*), SUM(ocr_text IS NOT NULL) FROM doc_image WHERE member_id=?",
                                    (mid,)).fetchone()
        out.append(f"\n## {doc}  `{m['path'] if m else ''}`  - {n_img or 0} image(s), {n_ocr or 0} read by OCR\n")
        seen = set()
        trows = []
        for h in hits:
            if h["line_no"] in seen:
                continue
            seen.add(h["line_no"])
            head = conn.execute("SELECT heading FROM doc_section WHERE member_id=? AND ordinal=?",
                                (mid, h["line_no"])).fetchone()
            trows.append((_section_label(h["line_no"]), (head["heading"] if head and head["heading"] else "")[:40],
                          _hit_line(conn, mid, h["line_no"], term, h["snip"]), f"{doc}:{h['line_no'] or 0}"))
        out.append(table(["where", "heading", "excerpt", "cite"], trows[:15]))
    out.append("\n> Full text of a section: `doc DOCNAME --sections n` (or `n-m`); every section about a term: "
               "`doc DOCNAME --grep TERM`; the outline: `doc DOCNAME`.\n"
               "> Cite a document as `[[DOCNAME <section> \"token\"]]`; the gate checks the token against that "
               "section (images are sections 1001+). Where a document and the code disagree, say so - the "
               "code is what runs.\n")
    return "".join(out)


# build.QUERY_INDEXES' index on UPPER(TRIM(name)) (ROADMAP re-parse item 12)
TRIMMED_NAME_INDEX = "ix_q_member_utname"
NAME_INDEX = "ix_q_member_uname"


def _has_index(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone() is not None


def _doc_members(conn: sqlite3.Connection, name: str) -> List[sqlite3.Row]:
    n = name.upper()
    stem = os.path.splitext(n)[0]
    # `+kind` when the name's index is there: with no statistics the planner took the kind index (every document) over
    # the name's for this OR of two names too (LESSONS 210)
    kind = "+kind" if _has_index(conn, NAME_INDEX) else "kind"
    rows = conn.execute(f"SELECT id, name, path FROM member WHERE {kind}='doc' AND (UPPER(name)=? OR UPPER(name)=?) "
                        "ORDER BY path", (n, stem)).fetchall()
    # a file named `PLAN .docx` is member `PLAN ` - nobody types the trailing space; shown after
    # an exact PLAN.docx, never instead of it. `+kind`: with no statistics the planner took the kind index (every
    # document) over the name's; an index built before ROADMAP re-parse item 12 has no index on the trimmed name, and
    # reads the kind index as before (LESSONS 209)
    kind = "+kind" if _has_index(conn, TRIMMED_NAME_INDEX) else "kind"
    twins = conn.execute(f"SELECT id, name, path FROM member WHERE {kind}='doc' AND "
                         "(UPPER(TRIM(name))=? OR UPPER(TRIM(name))=?) ORDER BY path", (n.strip(), stem.strip())).fetchall()
    seen = {r["id"] for r in rows}
    rows = list(rows) + [r for r in twins if r["id"] not in seen]
    if not rows:
        rows = conn.execute("SELECT id, name, path FROM member WHERE kind='doc' AND UPPER(name) LIKE ? ORDER BY path",
                            (f"%{stem}%",)).fetchall()
    return rows


def _parse_sections(spec: str) -> set:
    """'3' | '3-5' | '2,7,9-12' -> {section numbers}"""
    out: set = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        if a.strip().isdigit() and (not b or b.strip().isdigit()):
            lo, hi = int(a), int(b or a)
            out.update(range(min(lo, hi), max(lo, hi) + 1))
    return out


def cmd_doc_list(conn: sqlite3.Connection, pattern: Optional[str] = None) -> str:
    """The indexed documents - by name, folder or extension - with their
    section and picture counts: what `doc NAME` can be asked for."""
    pat = (pattern or "").strip().upper()
    rows = conn.execute("""SELECT m.id, m.name, m.path, m.parse_status,
                                  (SELECT COUNT(*) FROM doc_section s WHERE s.member_id=m.id AND s.ordinal<1000) AS secs,
                                  (SELECT COUNT(*) FROM doc_image i WHERE i.member_id=m.id) AS imgs,
                                  (SELECT COUNT(*) FROM doc_image i WHERE i.member_id=m.id AND i.ocr_text IS NOT NULL) AS read
                           FROM member m WHERE m.kind='doc' ORDER BY m.path""").fetchall()
    if pat:
        rows = [r for r in rows if pat in r["path"].upper()]
    out = [f"# Documents in the index" + (f" matching `{pattern}`" if pat else "") + "\n\n"]
    if not rows:
        return out[0] + ("_none_" + (f" match `{pattern}`" if pat else " - documents are indexed from the folders in "
                         "`extra_roots` (UI: Document folders / `--also`)") + "\n")
    out.append(table(["document", "file", "folder", "sections", "pictures", "read by OCR", "status"],
                     [(r["name"], os.path.basename(r["path"]), os.path.basename(os.path.dirname(r["path"])),
                       r["secs"], r["imgs"], r["read"], r["parse_status"] or "") for r in rows[:500]]))
    if len(rows) > 500:
        out.append(f"_... {len(rows) - 500} more_\n")
    out.append(f"\n- {len(rows)} document(s). `doc NAME` for the outline (a workbook's tabs are its sections), "
               f"`doc NAME --sections n` for the rows / text, `docs TERM` to find which one mentions a term\n")
    return "".join(out)


def _doc_pictures(conn: sqlite3.Connection, member_id: int, doc_name: str) -> str:
    """The pictures of a document grouped by where they sit, with the OCR
    section that holds each one's text once `OCR images` has run."""
    try:
        imgs = conn.execute("SELECT name, anchor, ocr_text FROM doc_image WHERE member_id=? ORDER BY id",
                            (member_id,)).fetchall()
    except sqlite3.OperationalError:
        return "\n_pictures: the index was built by an older toolkit - run the build once (no --rebuild needed)_\n"
    if not imgs:
        return ""
    groups: Dict[str, List[str]] = {}
    for im in imgs:
        base = os.path.basename(im["name"])
        # the OCR section that holds this picture's text: `image: NAME (place)` / `page N (OCR ...)`
        like = (f"page {int(base[5:])} (OCR%" if base.startswith("page-") and base[5:].isdigit()
                else f"image: {base}%")
        sec = conn.execute("SELECT ordinal FROM doc_section WHERE member_id=? AND ordinal>=1000 AND heading LIKE ?",
                           (member_id, like)).fetchone()
        tag = base + (f" = section {sec['ordinal']}" if sec else (" (nothing recognised)" if im["ocr_text"] == "" else ""))
        groups.setdefault(im["anchor"] or "(place unknown)", []).append(tag)
    out = ["\n## Pictures (screenshots) and where they sit\n"]
    for where, names in list(groups.items())[:80]:
        shown = ", ".join(names[:12]) + (f" ... +{len(names) - 12}" if len(names) > 12 else "")
        out.append(f"- **{where}**: {shown}\n")
    if len(groups) > 80:
        out.append(f"- ... {len(groups) - 80} more places\n")
    out.append(f"- a picture's text is its section (1001+): `doc {doc_name} --sections 1001`; "
               f"cite it as `[[{doc_name} 1001 \"token\"]]`\n")
    return "".join(out)


def cmd_doc(conn: sqlite3.Connection, name: str, sections: Optional[str] = None, grep: Optional[str] = None,
            budget: Optional[int] = None) -> str:
    """A document the model can read a section at a time: the outline
    (section numbers, headings, sizes), or the FULL text of chosen sections
    - by number or by content - each citable as [[DOC n "token"]]. Documents
    are prose: what was intended, never a fact about what runs."""
    rows = _doc_members(conn, name)
    if not rows:
        return (f"# Document {name}\n\n**NOT FOUND** - no indexed document is called that. `docs TERM` finds documents "
                "by content; documents are indexed from the folders in `extra_roots` (UI: Document folders).\n")
    m = rows[0]
    out = [f"# Document {m['name']}  ({os.path.basename(m['path'])})\n"]
    if len(rows) > 1:
        out.append(f"- {len(rows)} documents share this name; showing the first. Others: "
                   + ", ".join(os.path.basename(r["path"]) for r in rows[1:4]) + "\n")
    secs = conn.execute("SELECT ordinal, heading, text FROM doc_section WHERE member_id=? ORDER BY ordinal",
                        (m["id"],)).fetchall()
    n_img, n_ocr = conn.execute("SELECT COUNT(*), SUM(ocr_text IS NOT NULL) FROM doc_image WHERE member_id=?",
                                (m["id"],)).fetchone()
    if not sections and not grep:
        total = sum(len(s["text"] or "") for s in secs)
        out.append(f"- {len(secs)} sections, {total} characters (~{total // 4} tokens); {n_img or 0} image(s), "
                   f"{n_ocr or 0} read by OCR (their text is sections 1001+ once `OCR images` has run)\n")
        out.append(f"- read one: `doc {m['name']} --sections 3` (or `3-5`, `2,7,9-12`); by content: "
                   f"`doc {m['name']} --grep \"waiver\"`; cite as `[[{m['name']} 3 \"token\"]]`\n\n")
        trows = [(s["ordinal"], (s["heading"] or "")[:50], len(s["text"] or ""),
                  (s["text"] or "").strip().split("\n", 1)[0][:70]) for s in secs]
        out.append(table(["section", "heading", "chars", "starts with"], trows[:400]))
        if len(secs) > 400:
            out.append(f"_... {len(secs) - 400} more sections_\n")
        out.append(_doc_pictures(conn, m["id"], m["name"]))
        return "".join(out)
    if sections:
        want = _parse_sections(sections)
        chosen = [s for s in secs if s["ordinal"] in want]
        what = f"section(s) {sections}"
    else:
        g = (grep or "").upper()
        chosen = [s for s in secs if g in ((s["heading"] or "") + "\n" + (s["text"] or "")).upper()]
        what = f"section(s) containing `{grep}`"
    if not chosen:
        out.append(f"\n_no {what}_ - `doc {m['name']}` lists what there is\n")
        return "".join(out)
    out.append(f"- {len(chosen)} {what}; cite as `[[{m['name']} <section> \"token\"]]` with the token copied "
               "from the text. Prose describes intent; where it disagrees with the code, say so.\n")
    used = sum(len(x) for x in out)
    shown = 0
    for s in chosen:
        block = (f"\n### {m['name']} section {s['ordinal']}" + (f" - {s['heading']}" if s["heading"] else "")
                 + "\n```\n" + (s["text"] or "").rstrip() + "\n```\n")
        if budget and shown and used + len(block) > budget:
            rest = chosen[shown:]
            out.append(f"\n_budget {budget}: {len(rest)} more section(s) not shown - "
                       + ", ".join(str(x["ordinal"]) for x in rest[:30]) + (" ..." if len(rest) > 30 else "") + "_\n")
            break
        out.append(block)
        used += len(block)
        shown += 1
    return "".join(out)


def cmd_images(conn: sqlite3.Connection, name: Optional[str] = None) -> str:
    q = """SELECT m.name, m.path, COUNT(i.id) AS n, SUM(i.extracted_path IS NOT NULL) AS extracted,
                  SUM(i.ocr_text IS NOT NULL) AS read, SUM(i.ocr_text IS NOT NULL AND i.ocr_text<>'') AS with_text
           FROM doc_image i JOIN member m ON m.id=i.member_id"""
    args: tuple = ()
    if name:
        stem = os.path.splitext(name)[0]                 # `images CLAIMS PLAN.docx` means the document
        q += " WHERE (UPPER(m.name) IN (?, ?) OR UPPER(TRIM(m.name)) IN (?, ?))"
        args = (name.upper(), stem.upper(), name.strip().upper(), stem.strip().upper())
    q += " GROUP BY m.id ORDER BY n DESC"
    rows = conn.execute(q, args).fetchall()
    out = [f"# Pictures inside documents{' - ' + name.upper() if name else ''}\n"]
    if not rows:
        return out[0] + "\n_no images recorded_ (documents are indexed from `extra_roots`; legacy .doc/.xls must be saved as .docx/.xlsx first)\n"
    out.append(table(["document", "images", "extracted", "OCR read", "with text", "path"],
                     [(r["name"], r["n"], r["extracted"] or 0, r["read"] or 0, r["with_text"] or 0,
                       os.path.basename(r["path"])) for r in rows[:200]]))
    total = sum(r["n"] for r in rows)
    unread = sum(r["n"] - (r["read"] or 0) for r in rows)
    out.append(f"\n{total} image(s) in {len(rows)} document(s); {unread} not yet read. "
               f"`python -m atlas.ocr --db atlas.db --out out/images` reads them with the Windows OCR engine "
               f"(no tokens). Pictures that OCR cannot read (diagrams, handwriting) are the ones worth "
               f"spending a vision-model call on - they are listed with their extracted path.\n")
    if name:
        try:
            rows2 = conn.execute("""SELECT i.name, i.anchor, i.extracted_path, i.ocr_text FROM doc_image i JOIN member m ON m.id=i.member_id
                                    WHERE UPPER(m.name) IN (?, ?) OR UPPER(TRIM(m.name)) IN (?, ?) ORDER BY i.id""",
                                 (name.upper(), os.path.splitext(name)[0].upper(), name.strip().upper(),
                                  os.path.splitext(name)[0].strip().upper())).fetchall()
        except sqlite3.OperationalError:
            return "".join(out) + "\n_the index was built by an older toolkit - run the build once (no --rebuild needed) to see where each picture sits_\n"
        out.append(table(["image", "where it sits", "extracted to", "OCR text (first 80 chars)"],
                         [(r["name"], r["anchor"] or "", r["extracted_path"] or "",
                           (r["ocr_text"] or ("(not read)" if r["ocr_text"] is None else "(nothing recognised)"))[:80].replace("\n", " "))
                          for r in rows2]))
    return "".join(out)


# --------------------------------------------------------------------------
# DB2 columns
# --------------------------------------------------------------------------

def cmd_column(conn: sqlite3.Connection, name: str) -> str:
    """Where a DB2 column is written from, read into, and filtered by - and,
    through the host variable, where that value came from or went next."""
    n = name.upper()
    tbl, _, col = n.rpartition(".")
    # PRD.POLICY_TBL and POLICY_TBL are the same table written two ways: match
    # on the unqualified name unless the question itself carries a qualifier.
    base = tbl.rpartition(".")[2] if tbl else ""
    if tbl and "." in tbl:
        q, args = "UPPER(c.col)=? AND UPPER(c.tbl)=?", (col, tbl)
    elif tbl:
        q, args = "UPPER(c.col)=? AND (UPPER(c.tbl)=? OR UPPER(c.tbl) LIKE ?)", (col, base, f"%.{base}")
    else:
        q, args = "UPPER(c.col)=?", (col,)
    rows = conn.execute(f"""SELECT c.*, p.program_id AS pname FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                            WHERE {q} ORDER BY c.mode, p.program_id, c.line""", args).fetchall()
    out = [f"# DB2 column {n}\n"]
    decl = conn.execute("""SELECT o.qualifier, o.name, o.source, dc.type, dc.ordinal FROM db2_column dc
                           JOIN db2_object o ON o.id=dc.object_id WHERE UPPER(dc.name)=?""" +
                        (" AND UPPER(o.name)=?" if base else ""), (col, base) if base else (col,)).fetchall()
    for d in decl:
        out.append(f"- declared: `{(d['qualifier'] + '.') if d['qualifier'] else ''}{d['name']}.{col}` "
                   f"{d['type'] or ''} (column #{d['ordinal']}, from {d['source']})\n")
    if not rows:
        return "".join(out) + ("\n**No static SQL references** to this column in indexed programs - dynamic SQL, "
                               "a view, or a different column name. Check `search \"" + col + "\"`.\n")
    for mode, title in (("write", "Written (INSERT / UPDATE from a host variable)"),
                        ("read", "Read (SELECT INTO / FETCH INTO a host variable)"),
                        ("predicate", "Used in predicates (WHERE)")):
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        out.append(f"\n### {title}\n")
        trows = []
        for r in rs:
            hv, pid = r["host_var"], r["program_id"]
            extra = ""
            if hv and mode == "write":
                src = conn.execute("""SELECT stmt, line FROM field_ref WHERE program_id=? AND name=? AND mode='write'
                                      AND stmt<>'EXEC-SQL' ORDER BY line""", (pid, hv)).fetchall()
                lits = conn.execute("""SELECT literal, line FROM literal_ref WHERE program_id=? AND field=?
                                       AND context='move_to' ORDER BY line""", (pid, hv)).fetchall()
                extra = "; ".join([f"set by {x['stmt']} @{cite(conn, pid, x['line'])}" for x in src[:3]]
                                  + [f"'{l['literal']}' @{cite(conn, pid, l['line'])}" for l in lits[:3]]) \
                    or "host var never set by name here (group MOVE / CALL / file record?)"
            elif hv and mode == "read":
                dst = conn.execute("""SELECT mode, stmt, line FROM field_ref WHERE program_id=? AND name=?
                                      AND mode IN ('display','write','test') AND stmt<>'EXEC-SQL' ORDER BY line""",
                                   (pid, hv)).fetchall()
                extra = "; ".join(f"{x['mode']} {x['stmt']} @{cite(conn, pid, x['line'])}" for x in dst[:4]) \
                    or "value not used by name afterwards in this program"
            trows.append((r["tbl"], r["col"], r["pname"], r["stmt"], hv or "", cite(conn, pid, r["line"]), extra))
        out.append(table(["table", "column", "program", "stmt", "host variable", "cite",
                          "value from / value to"], trows))
    out.append("\n> Table `?` = the column was unqualified in a multi-table FROM. Views, dynamic SQL and "
               "stored procedures are not resolved. For the field side of any host variable, run `field <name>`.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# transactions (CICS CSD / IMS stage-1)
# --------------------------------------------------------------------------

def cmd_transaction(conn: sqlite3.Connection, code: str) -> str:
    n = code.upper()
    rows = conn.execute("""SELECT t.*, m.name AS mem FROM transaction_def t LEFT JOIN member m ON m.id=t.member_id
                           WHERE UPPER(t.tran_code)=? OR UPPER(t.program)=? OR UPPER(t.psb)=?
                           ORDER BY t.system, t.tran_code""", (n, n, n)).fetchall()
    out = [f"# Transaction / program {n}\n"]
    if not rows:
        loaded = conn.execute("SELECT COUNT(*) FROM transaction_def").fetchone()[0]
        out.append("**NOT FOUND** in any indexed CSD or IMS stage-1 member. ")
        out.append("Load the DFHCSDUP extract and the stage-1 source; until then online routing is unknown.\n"
                   if not loaded else f"({loaded} transactions are indexed - this name is not one of them, "
                   f"nor a program/PSB they route to.)\n")
        return "".join(out)
    out.append(table(["code", "system", "program", "PSB", "group", "detail", "cite"],
                     [(r["tran_code"], r["system"], r["program"] or "(none)", r["psb"] or "",
                       r["group_name"] or "", (r["detail"] or "")[:120], f"{r['mem']}:{r['line']}") for r in rows]))
    for p in sorted({r["program"] for r in rows if r["program"]}):
        if programs_named(conn, p):
            out.append(f"- `{p}` is indexed - `program {p}` for its dossier\n")
        else:
            out.append(f"- **{p} is not in the index** (no source, or member / PROGRAM-ID / load name differ)\n")
    # Routing written in code: START/RETURN TRANSID, IMS CHNG message switches.
    codes = sorted({r["tran_code"].upper() for r in rows})
    q = ",".join("?" * len(codes))
    chain = conn.execute(f"""SELECT p.program_id, p.id AS pid, c.kind, c.target, c.resolved, c.line FROM call_edge c
                             JOIN program p ON p.id=c.program_id
                             WHERE c.kind IN ('cics_start','cics_return','ims_switch') AND
                                   (UPPER(c.target) IN ({q}) OR {' OR '.join('c.resolved LIKE ?' for _ in codes)})
                             ORDER BY p.program_id, c.line""", (*codes, *[f'%"{c}"%' for c in codes])).fetchall()
    if chain:
        out.append("\n### Reached from code (not from the CSD / stage-1)\n")
        out.append(table(["from program", "how", "cite"],
                         [(c["program_id"], {"cics_start": "START TRANSID", "cics_return": "RETURN TRANSID (next in the conversation)",
                                             "ims_switch": "IMS message switch (CHNG + ISRT)"}[c["kind"]],
                           cite(conn, c["pid"], c["line"])) for c in chain]))
    # Which transactions the routed programs themselves start / return to.
    progs = sorted({r["program"].upper() for r in rows if r["program"]})
    if progs:
        qp = ",".join("?" * len(progs))
        nxt = conn.execute(f"""SELECT p.program_id, c.kind, COALESCE(c.target, c.resolved) AS t FROM call_edge c
                               JOIN program p ON p.id=c.program_id WHERE UPPER(p.program_id) IN ({qp})
                               AND c.kind IN ('cics_start','cics_return','ims_switch')""", progs).fetchall()
        if nxt:
            out.append("- next in the chain: " + "; ".join(
                f"{x['program_id']} {x['kind'].replace('cics_', '').replace('ims_switch', 'switches to')} "
                f"{', '.join(_jl(x['t'])) if (x['t'] or '').startswith('[') else x['t']}" for x in nxt) + "\n")
    out.append("\n> IMS: the load module is assumed to be named as the PSB unless APPLCTN used GPSB=. "
               "CICS: a REMOTESYSTEM transaction runs in another region.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# screens (BMS / MFS)
# --------------------------------------------------------------------------

def _screen_hits(conn: sqlite3.Connection, n: str) -> List[sqlite3.Row]:
    """Screen fields called n, or whose BMS symbolic name (nI / nO / nL / nF / nA) is n."""
    base = n[:-1] if len(n) > 2 and n[-1] in "IOLAF" else None
    names = [n] + ([base] if base else [])
    q = ",".join("?" * len(names))
    return conn.execute(f"""
        SELECT sf.*, s.kind, s.name AS sname, s.mode, s.parent, m.name AS mem
        FROM screen_field sf JOIN screen s ON s.id=sf.screen_id JOIN member m ON m.id=s.member_id
        WHERE UPPER(sf.name) IN ({q}) ORDER BY s.kind, s.name""", names).fetchall()


def _screen_refs(conn: sqlite3.Connection, field_name: str, kind: str) -> List[sqlite3.Row]:
    """Program references to a screen field: BMS through the generated
    symbolic names (GENDERI / GENDERO ...), MFS through the MFLD name."""
    names = screens.bms_symbolic_names(field_name) if kind == "bms_map" else [field_name.upper()]
    q = ",".join("?" * len(names))
    return conn.execute(f"""
        SELECT p.program_id, p.id AS pid, r.name, r.mode, r.stmt, r.line FROM field_ref r
        JOIN program p ON p.id=r.program_id WHERE UPPER(r.name) IN ({q})
        ORDER BY p.program_id, r.line""", names).fetchall()


def _screen_section(conn: sqlite3.Connection, n: str) -> str:
    hits = _screen_hits(conn, n)
    if not hits:
        return ""
    out = ["\n### Screen fields (BMS / MFS)\n"]
    out.append(table(["screen", "kind", "mode", "field", "row,col", "len", "offset/seg", "attrb",
                      "default", "PICIN/OUT", "cite"],
                     [(h["sname"], h["kind"], h["mode"] or "", h["name"],
                       f"{h['row']},{h['col']}" if h["row"] else "",
                       h["length"], f"{h['offset']}/{h['seg']}" if h["offset"] is not None else "",
                       h["attrb"] or "", h["initial"] or "",
                       "/".join(x for x in (h["picin"], h["picout"]) if x), f"{h['mem']}:{h['line']}")
                      for h in hits]))
    rows, seen = [], set()
    for h in hits:
        for r in _screen_refs(conn, h["name"], h["kind"]):
            key = (r["program_id"], r["name"], r["line"])
            if key in seen:
                continue
            seen.add(key)
            rows.append((r["program_id"], h["name"], r["name"], r["mode"], r["stmt"],
                         cite(conn, r["pid"], r["line"])))
    if rows:
        out.append("\n**Programs referencing these screen fields** (this is where online validation lives)\n")
        out.append(table(["program", "screen field", "as", "mode", "stmt", "cite"], rows))
    out.append("> BMS programs see a field as `<name>I` (input) / `<name>O` (output). MFS programs address "
               "the message by BYTE OFFSET in their I/O copybook: match the offset column against `field` "
               "output for that copybook.\n")
    return "".join(out)


def cmd_screen(conn: sqlite3.Connection, name: str) -> str:
    n = name.upper()
    scr = conn.execute("""SELECT s.*, m.name AS mem, m.path FROM screen s JOIN member m ON m.id=s.member_id
                          WHERE UPPER(s.name)=? OR UPPER(s.parent)=? ORDER BY s.kind, s.name""", (n, n)).fetchall()
    if not scr:
        return f"# Screen {n}\n\n**NOT FOUND** - no BMS map/mapset or MFS FMT/MSG with this name is indexed.\n"
    out = [f"# Screen {n}\n"]
    mids = set()
    for s in scr:
        mids.add(s["member_id"])
        head = f"\n## {s['kind']} {s['name']}"
        if s["parent"]:
            head += f" (in {s['parent']})"
        head += f"  mode {s['mode'] or '?'}"
        if s["next_msg"]:
            head += f"  next {s['next_msg']}"
        out.append(head + f"  `{s['mem']}:{s['line']}`\n")
        flds = conn.execute("SELECT * FROM screen_field WHERE screen_id=? ORDER BY ordinal", (s["id"],)).fetchall()
        out.append(table(["#", "field", "row,col", "len", "offset/seg", "attrb", "default", "constant",
                          "PICIN/OUT", "line"],
                         [(f["ordinal"], f["name"] or "", f"{f['row']},{f['col']}" if f["row"] else "",
                           f["length"], f"{f['offset']}/{f['seg']}" if f["offset"] is not None else "",
                           f["attrb"] or "", f["initial"] or "", f["literal"] or "",
                           "/".join(x for x in (f["picin"], f["picout"]) if x), f["line"]) for f in flds]))
        # EXEC CICS SEND/RECEIVE MAP('X') MAPSET('Y'): the program<->map edge
        # as a fact, before any field-name coincidence.
        if s["kind"] == "bms_map":
            keys = [s["name"].upper()] + ([f"{s['parent'].upper()}.{s['name'].upper()}"] if s["parent"] else [])
            q = ",".join("?" * len(keys))
            cm = conn.execute(f"""SELECT p.program_id, p.id AS pid, c.verb, c.direction, c.line FROM cics_cmd c
                                  JOIN program p ON p.id=c.program_id
                                  WHERE c.resource_kind='map' AND (UPPER(c.resource) IN ({q}) OR UPPER(c.resource) LIKE ?)
                                  ORDER BY p.program_id, c.line""", (*keys, f"%.{s['name'].upper()}")).fetchall()
            if cm:
                out.append("\n**Programs that SEND / RECEIVE this map (EXEC CICS facts)**\n")
                out.append(table(["program", "verb", "dir", "cite"],
                                 [(c["program_id"], c["verb"], c["direction"] or "", cite(conn, c["pid"], c["line"])) for c in cm]))
        rows = []
        for fld in flds:
            if not fld["name"]:
                continue
            for r in _screen_refs(conn, fld["name"], s["kind"]):
                rows.append((r["program_id"], fld["name"], r["name"], r["mode"], r["stmt"],
                             cite(conn, r["pid"], r["line"])))
        if rows:
            out.append("\n**Programs referencing its fields**\n")
            out.append(table(["program", "screen field", "as", "mode", "stmt", "cite"], rows))
        elif s["kind"] != "mfs_fmt":
            out.append("_No program references found by name. MFS programs address the message by copybook "
                       "offset; BMS programs use the generated symbolic copybook, which must be in the index._\n")
    out.append(unresolved_for(conn, sorted(mids)))
    return "".join(out)


# --------------------------------------------------------------------------
# value-domain changes: values / pair / messages
# --------------------------------------------------------------------------
#
# "Add gender N; son and daughter both become relationship 4" is a change to
# the VALUE DOMAIN of two fields. The work is not finding the fields - it is
# finding every place a specific value is assumed: 88-levels, IF/WHEN tests,
# SQL predicates, sort INCLUDE cards, derivation rules that tie one field's
# value to another's, and the messages that name the old rule. These three
# queries produce those inventories deterministically.

def _norm_val(v: str) -> str:
    s = v.strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return str(int(s))
    return s


def _cond88_map(conn: sqlite3.Connection, name: str) -> Dict[str, List[Tuple[str, str]]]:
    """value -> [(88-name, member)] for every 88 under any field called `name`."""
    out: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for r in conn.execute("""SELECT c.name, c.values_lit, m.name AS mem FROM cond88 c JOIN field f ON f.id=c.field_id
                             JOIN member m ON m.id=f.member_id WHERE UPPER(f.name)=?""", (name.upper(),)):
        raw = _jl(r["values_lit"])
        vals = [cobol._norm_lit(v) for v in raw if v.upper() not in ("THRU", "THROUGH")]
        if any(x.upper() in ("THRU", "THROUGH") for x in raw) and len(vals) >= 2:
            out[f"{_norm_val(vals[0])} THRU {_norm_val(vals[1])}"].append((r["name"], r["mem"]))
        else:
            for v in vals:
                out[_norm_val(v)].append((r["name"], r["mem"]))
    return out


def _column_aliases(name: str) -> List[str]:
    """COBOL field -> plausible DB2 column names (DCLGEN style, prefixes dropped)."""
    n = name.upper().replace("-", "_")
    out = [n]
    for pre in ("WS_", "LK_", "WK_", "W_", "IN_", "OUT_"):
        if n.startswith(pre):
            out.append(n[len(pre):])
    return out


def cmd_values(conn: sqlite3.Connection, name: str) -> str:
    n = name.upper()
    cols = _column_aliases(n)
    defs = _field_defs(conn, n)
    out = [f"# Observed value domain of {n}\n"]
    if not defs:
        out.append("> Not defined in any indexed member; the rows below come from literal usage only.\n")
    documented = _cond88_map(conn, n)

    qc = ",".join("?" * len(cols))
    rows = conn.execute(f"""
        SELECT l.literal, l.context, l.line, l.member_id, p.program_id AS pname, p.id AS pid, m.name AS mem
        FROM literal_ref l LEFT JOIN program p ON p.id=l.program_id JOIN member m ON m.id=l.member_id
        WHERE (UPPER(l.field)=? AND l.context IN ('move_to','compare','when','string','value','screen_initial'))
           OR (UPPER(l.field) IN ({qc}) AND l.context LIKE 'sql_%')""", (n, *cols)).fetchall()
    uses: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        v = _norm_val(r["literal"])
        ct = cite(conn, r["pid"], r["line"]) if r["pid"] else f"{r['mem']}:{r['line']}"
        uses[v][r["context"]].append(f"{r['pname'] or r['mem']}@{ct}")

    # Tests through 88-level names never mention the literal; join them in.
    for v, lst in documented.items():
        for (c88, _mem) in lst:
            for r in conn.execute("""SELECT p.program_id, p.id AS pid, r.mode, r.line FROM field_ref r
                                     JOIN program p ON p.id=r.program_id WHERE UPPER(r.name)=?""", (c88.upper(),)):
                # Say HOW it was reached: a test through an 88 name never mentions
                # the value, which is exactly why a grep for 'M' misses it.
                uses[v][f"via 88 {c88} ({r['mode']})"].append(
                    f"via 88 {c88} {r['program_id']}@{cite(conn, r['pid'], r['line'])}")

    # Sort/INCLUDE cards comparing these bytes to constants.
    for d in defs:
        if d["kind"] != "copybook":
            continue
        lo, hi = d["offset"] + 1, d["offset"] + d["length"]
        for c in conn.execute("""SELECT c.raw, c.card_kind, j.job_name, s.step_name FROM card_field_ref c
                                 JOIN step s ON s.id=c.step_id LEFT JOIN job j ON j.id=s.job_id
                                 WHERE c.pos<=? AND c.pos+c.length-1>=? AND c.card_kind IN ('INCLUDE','OMIT')""", (hi, lo)):
            for m in re.finditer(r"C'([^']*)'|(?<=,)(\d+)(?=[,)])", c["raw"]):
                val = m.group(1) if m.group(1) is not None else m.group(2)
                uses[_norm_val(val)]["sort_card"].append(f"{c['job_name']}.{c['step_name']} {c['card_kind']}")

    all_vals = sorted(set(documented) | set(uses),
                      key=lambda v: (not v.split(" ")[0].lstrip("+-").isdigit(), v))
    trows = []
    for v in all_vals:
        u = uses.get(v, {})

        def cnt(*prefixes):
            return sum(len(l) for k, l in u.items() if k.startswith(prefixes))

        # One example per KIND of use per PROGRAM, so a test through an 88 name
        # or a sort card stays visible, and every program touching the value
        # gets at least one citation even when one program produces dozens.
        examples, seen_kp = [], set()
        for k in sorted(u):
            for w in u[k]:
                prog = w.split("@")[0]
                if (k, prog) in seen_kp:
                    continue
                seen_kp.add((k, prog))
                examples.append(w)
        total = sum(len(l) for l in u.values())
        shown = examples[:8]
        where = "; ".join(shown) + (f" (+{total - len(shown)} more)" if total > len(shown) else "")
        trows.append((v, ", ".join(sorted({c for c, _m in documented.get(v, [])})) or "(none)",
                      cnt("move_to", "string", "sql_set", "sql_insert"),
                      cnt("compare", "when", "via 88"), cnt("sql_predicate"), cnt("sort_card"), where))
    out.append(table(["value", "documented by 88", "set", "tested", "SQL", "sort cards", "where (first few)"], trows))

    undocumented = [v for v in all_vals if v not in documented and uses.get(v)
                    and any(not k.startswith("value") for k in uses[v])]
    unused = [v for v in all_vals if v in documented and not uses.get(v)]
    if undocumented:
        out.append(f"\n**USED BUT NOT DOCUMENTED BY ANY 88-LEVEL:** {', '.join(undocumented)} - undocumented "
                   f"codes are where a value-domain change breaks silently.\n")
    if unused:
        out.append(f"\n**Documented but never set, tested or queried in indexed code:** {', '.join(unused)} "
                   f"(may be reached via a group MOVE, a table, a screen, or code outside the index).\n")
    if any(k.startswith("sql") for u in uses.values() for k in u):
        out.append(f"\nSQL rows are for column name(s) {', '.join(f'`{c}`' for c in cols)} (derived from the field "
                   f"name); if the column is called something else, run `values <COLUMN>` as well.\n")
    out.append(_screen_section(conn, n))
    out.append("\n> This is the domain the CODE knows about. Values that arrive in files, DB2 rows, IMS segments or "
               "screens are data, not literals - compare against the data's actual distinct values before "
               "calling the change complete.\n")
    out.append(unresolved_for(conn, sorted({r["member_id"] for r in rows}), limit=15))
    return "".join(out)


def cmd_pair(conn: sqlite3.Connection, a: str, b: str, window: int = 8) -> str:
    """Statements where two fields (or their 88-levels) are used together -
    the validation and derivation rules that bind one value to the other."""
    a, b = a.upper(), b.upper()

    def names_for(n: str) -> List[str]:
        names = {n}
        for r in conn.execute("""SELECT c.name FROM cond88 c JOIN field f ON f.id=c.field_id
                                 WHERE UPPER(f.name)=?""", (n,)):
            names.add(r["name"].upper())
        # A BMS screen field is referenced by programs as nI / nO (symbolic map).
        if conn.execute("""SELECT 1 FROM screen_field sf JOIN screen s ON s.id=sf.screen_id
                           WHERE UPPER(sf.name)=? AND s.kind='bms_map' LIMIT 1""", (n,)).fetchone():
            names.update(screens.bms_symbolic_names(n)[:2])
        return sorted(names)

    def refs(names: List[str]):
        q = ",".join("?" * len(names))
        return conn.execute(f"SELECT program_id, name, mode, stmt, line FROM field_ref "
                            f"WHERE UPPER(name) IN ({q})", names).fetchall()

    na, nb = names_for(a), names_for(b)
    ra, rb = refs(na), refs(nb)
    by_prog: Dict[int, list] = defaultdict(list)
    for r in rb:
        by_prog[r["program_id"]].append(r)
    hits, seen = [], set()
    for x in ra:
        for y in by_prog.get(x["program_id"], []):
            if abs(x["line"] - y["line"]) > window:
                continue
            key = (x["program_id"], x["line"], y["line"])
            if key in seen:
                continue
            seen.add(key)
            lo = min(x["line"], y["line"])
            para = conn.execute("SELECT name FROM paragraph WHERE program_id=? AND ? BETWEEN start_line AND end_line",
                                (x["program_id"], lo)).fetchone()
            pname = conn.execute("SELECT program_id FROM program WHERE id=?", (x["program_id"],)).fetchone()[0]
            hits.append((pname, para["name"] if para else "", f"{x['name']} {x['mode']}",
                         f"{y['name']} {y['mode']}", cite(conn, x["program_id"], x["line"]),
                         cite(conn, x["program_id"], y["line"])))
    hits.sort()
    out = [f"# Statements relating {a} and {b}\n",
           f"Names searched: {', '.join(na)}  x  {', '.join(nb)}; same program, within {window} lines.\n\n",
           table(["program", "paragraph", a, b, f"cite {a}", f"cite {b}"], hits)]
    progs = {h[0] for h in hits}
    out.append(f"\n{len(hits)} co-reference(s) in {len(progs)} program(s). Each is a cross-field rule "
               f"(validation or derivation) to re-read; a rule such as 'SON must be MALE' is exactly what a "
               f"new value invalidates.\n")
    out.append("> Not covered: rules expressed through a group-level MOVE, a lookup table, a sort card, or a "
               "screen map.\n")
    return "".join(out)


def cmd_messages(conn: sqlite3.Connection, pattern: str) -> str:
    pat = pattern if "%" in pattern else f"%{pattern}%"
    rows = conn.execute("""
        SELECT l.literal, l.context, l.field, l.line, p.program_id AS pname, p.id AS pid, m.name AS mem
        FROM literal_ref l LEFT JOIN program p ON p.id=l.program_id JOIN member m ON m.id=l.member_id
        WHERE UPPER(l.literal) LIKE UPPER(?)
          AND l.context IN ('display','move_to','value','string','value_group','string_group',
                            'display_group','screen_initial','screen_literal')
        ORDER BY l.literal""", (pat,)).fetchall()
    groups: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        ct = cite(conn, r["pid"], r["line"]) if r["pid"] else f"{r['mem']}:{r['line']}"
        tgt = f" -> {r['field']}" if r["field"] else ""
        groups[r["literal"]].append(f"{r['context']}{tgt} @{r['pname'] or r['mem']} {ct}")
    out = [f"# Message / text literals matching `{pattern}` ({len(groups)})\n",
           table(["text", "where"], [(t, "; ".join(w[:5])) for t, w in groups.items()]),
           "\n`string_group` / `display_group` rows are messages assembled at run time: literals verbatim, "
           "fields as `<NAME>`. `value_group` rows are texts assembled from consecutive FILLER VALUEs.\n"]

    # Messages assembled by MOVEs into sibling fields of one group: show the whole set.
    assembled: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for r in rows:
        if r["context"] != "move_to" or not r["field"] or not r["pid"]:
            continue
        par = conn.execute("""SELECT f2.name FROM field f JOIN field f2 ON f2.id=f.parent_id
                              WHERE UPPER(f.name)=? LIMIT 1""", (r["field"].upper(),)).fetchone()
        if not par:
            continue
        sibs = conn.execute("""SELECT l.literal, l.field FROM literal_ref l
                               JOIN field f ON UPPER(f.name)=UPPER(l.field) JOIN field f2 ON f2.id=f.parent_id
                               WHERE l.program_id=? AND l.context='move_to' AND UPPER(f2.name)=?
                               ORDER BY l.line""", (r["pid"], par["name"].upper())).fetchall()
        if len(sibs) > 1:
            assembled[(r["pname"], par["name"])] = [(s["literal"], s["field"]) for s in sibs]
    if assembled:
        out.append("\n### Messages assembled by MOVEs into one group\n")
        out.append(table(["program", "group", "pieces in source order"],
                         [(p, g, " + ".join(f"'{l}'->{fld}" for l, fld in v)) for (p, g), v in assembled.items()]))
    docs = conn.execute("SELECT member_name, line_no, substr(text,1,120) FROM src_fts "
                        "WHERE kind='doc' AND UPPER(text) LIKE UPPER(?) LIMIT 10", (pat,)).fetchall()
    if docs:
        out.append("\n### Documents mentioning it (prose, not facts)\n")
        out.append(table(["document", "section", "excerpt"], [tuple(d) for d in docs]))
    out.append("\n> Messages held in a DB2 message table, an ISPF message member or an online message file are "
               "not literals in code; load an unload of that source to cover them.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# table / dbd / segment / layout
# --------------------------------------------------------------------------

def cmd_table(conn: sqlite3.Connection, name: str) -> str:
    """Who creates / reads / updates / deletes a DB2 table, its declared
    columns, cursors, dynamic SQL and stored-procedure use - estate-wide."""
    n = name.upper()
    qual, _, base = n.rpartition(".")
    out = [f"# DB2 table {n}\n"]
    objs = conn.execute("""SELECT o.*, m.name AS mem FROM db2_object o LEFT JOIN member m ON m.id=o.member_id
                           WHERE UPPER(o.name)=? AND o.kind='table'""" + (" AND UPPER(o.qualifier)=?" if qual else ""),
                        (base, qual) if qual else (base,)).fetchall()
    for o in objs:
        cols = conn.execute("SELECT ordinal, name, type FROM db2_column WHERE object_id=? ORDER BY ordinal", (o["id"],)).fetchall()
        out.append(f"\n### Columns of {(o['qualifier'] + '.') if o['qualifier'] else ''}{o['name']} "
                   f"(from {o['source']}{(' in ' + o['mem']) if o['mem'] else ''})\n")
        out.append(table(["#", "column", "type"], [(c["ordinal"], c["name"], c["type"] or "") for c in cols]))
    if not objs:
        out.append("\n_No DCLGEN / DDL declares this table in the index: column list unknown (host structures "
                   "are still matched by SQL statement)._\n")
    # statements: sql_stmt.tables is a JSON array of names as written
    pats = [f'%"{base}"%', f'%.{base}"%']
    rows = conn.execute("""SELECT s.stmt_type, s.tables, s.cursor_name, s.is_dynamic, s.start_line, p.program_id, p.id AS pid
                           FROM sql_stmt s JOIN program p ON p.id=s.program_id
                           WHERE s.tables LIKE ? OR s.tables LIKE ? ORDER BY p.program_id, s.start_line""", pats).fetchall()
    if qual:
        rows = [r for r in rows if any(t.upper() in (n, base) for t in _jl(r["tables"]))]
    agg: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        agg[r["program_id"]][r["stmt_type"]].append(cite(conn, r["pid"], r["start_line"]))
    out.append(f"\n### Programs and verbs ({len(agg)} programs, {len(rows)} statements)\n")
    trows = []
    for pgm in sorted(agg):
        crud = "".join(c for c, verbs in (("C", ("INSERT",)), ("R", ("SELECT", "DECLARE", "FETCH", "OPEN")),
                                           ("U", ("UPDATE", "MERGE")), ("D", ("DELETE",)))
                       if any(v in agg[pgm] for v in verbs))
        trows.append((pgm, crud, ", ".join(f"{v} x{len(c)} @{c[0]}" for v, c in sorted(agg[pgm].items()))))
    out.append(table(["program", "CRUD", "statements (first cite each)"], trows))
    dyn = conn.execute("""SELECT COUNT(*) FROM sql_stmt s WHERE s.is_dynamic=1 AND (s.tables LIKE ? OR s.tables LIKE ?)""",
                       pats).fetchone()[0]
    dyn_all = conn.execute("SELECT COUNT(*) FROM sql_stmt WHERE is_dynamic=1").fetchone()[0]
    if dyn_all:
        out.append(f"\n**{dyn_all} dynamic SQL statement(s) in the estate** ({dyn} naming this table statically) - "
                   f"their tables are not knowable from source.\n")
    # column-level detail
    cols = conn.execute("""SELECT c.col, c.mode, COUNT(*) AS n, COUNT(DISTINCT c.program_id) AS pgms FROM sql_col_ref c
                           WHERE UPPER(c.tbl)=? OR UPPER(c.tbl) LIKE ? GROUP BY c.col, c.mode ORDER BY c.col, c.mode""",
                        (base, f"%.{base}")).fetchall()
    if cols:
        out.append("\n### Columns referenced (see `column TABLE.COL` for the field lineage)\n")
        out.append(table(["column", "mode", "refs", "programs"], [(c["col"], c["mode"], c["n"], c["pgms"]) for c in cols]))
    util = conn.execute("""SELECT st.op, st.tbl, st.via_dd, st.direction, j.job_name, s.step_name, s.line, s.from_proc,
                                  m.name AS mem, d.dsn_resolved
                           FROM step_table st JOIN step s ON s.id=st.step_id LEFT JOIN job j ON j.id=s.job_id
                           LEFT JOIN member m ON m.id=j.member_id
                           LEFT JOIN dd d ON d.step_id=s.id AND UPPER(d.dd_name)=UPPER(st.via_dd)
                           WHERE (UPPER(st.tbl)=? OR UPPER(st.tbl) LIKE ?) AND s.job_id IS NOT NULL
                           ORDER BY j.job_name, s.ordinal""", (base, f"%.{base}")).fetchall()
    if util:
        out.append("\n### Batch utilities (LOAD writes the table from a file; UNLOAD / DSNTIAUL read it into one)\n")
        out.append(table(["job", "step", "op", "direction", "via DD", "dataset", "cite"],
                         [(u["job_name"], u["step_name"], u["op"], u["direction"], u["via_dd"] or "",
                           u["dsn_resolved"] or "", f"{u['from_proc'] or u['mem']}:{u['line']}") for u in util]))
    plans = conn.execute("SELECT DISTINCT tran_code, detail FROM transaction_def WHERE detail LIKE '%DB2 plan%'").fetchall()
    if plans:
        out.append("\n- CICS transactions with a DB2 plan (from DB2ENTRY/DB2TRAN): "
                   + "; ".join(f"{p['tran_code']}: {re.search(r'DB2 plan ([A-Z0-9@#$]+)', p['detail']).group(1)}"
                               for p in plans if re.search(r'DB2 plan ([A-Z0-9@#$]+)', p['detail'] or ''))[:400] + "\n")
    out.append("\n> Views, aliases and BIND QUALIFIER are not resolved: a program reading a VIEW over this table is "
               "not listed. Batch LOAD/UNLOAD steps are not yet joined (see ROADMAP).\n")
    return "".join(out)


def cmd_dbd(conn: sqlite3.Connection, name: str) -> str:
    """An IMS database: segments and fields, the PSBs/PCBs that address it
    (with PROCOPT), the programs whose DL/I calls resolve to it, the utility
    jobs that copy/unload/reload it, and its online definition."""
    n = name.upper()
    dbds = conn.execute("""SELECT d.*, m.name AS mem, m.path FROM ims_dbd d JOIN member m ON m.id=d.member_id
                           WHERE UPPER(d.name)=?""", (n,)).fetchall()
    out = [f"# IMS database {n}\n"]
    if not dbds:
        out.append("**NOT FOUND** - no DBD with this name is indexed.\n")
    for d in dbds:
        out.append(f"\n## DBD {d['name']}  ACCESS={d['access'] or '?'}  `{d['mem']}:{d['line']}`"
                   + (f"  DD {d['dd1']}" + (f"/{d['dd2']}" if d['dd2'] else "") if d["dd1"] else "") + "\n")
        segs = conn.execute("SELECT * FROM ims_segment WHERE dbd_id=? ORDER BY id", (d["id"],)).fetchall()
        srows = []
        for s in segs:
            flds = conn.execute("SELECT name, start, bytes, is_seq FROM ims_field WHERE segment_id=? ORDER BY start", (s["id"],)).fetchall()
            srows.append((s["name"], s["parent"] or "(root)", s["bytes"], s["seq_field"] or "",
                          ", ".join(f"{f['name']}@{f['start']}/{f['bytes']}" + ("*" if f["is_seq"] else "") for f in flds)[:120],
                          _segment_traffic(conn, s["name"], d["name"])))
        out.append(table(["segment", "parent", "bytes", "seq field", "fields (name@start/len, *=SEQ)", "programs"], srows))
        out.append("- `segment NAME --dbd " + d["name"] + "` puts each DBD field at its bytes in every program's "
                   "I/O area, whatever that program calls it\n")
        xd = conn.execute("SELECT name, segment, srch FROM ims_xdfld WHERE dbd_id=?", (d["id"],)).fetchall()
        if xd:
            out.append("- secondary index search fields (XDFLD): " + "; ".join(
                f"{x['name']} on {x['segment']} from {', '.join(_jl(x['srch']))}" for x in xd) + "\n")
    pcbs = conn.execute("""SELECT ps.name AS psb, pc.ordinal, pc.procopt, pc.procseq, pc.list_no, pc.sensegs, m.name AS mem, pc.line
                           FROM ims_pcb pc JOIN ims_psb ps ON ps.id=pc.psb_id JOIN member m ON m.id=ps.member_id
                           WHERE UPPER(pc.dbd_name)=? ORDER BY ps.name, pc.ordinal""", (n,)).fetchall()
    if pcbs:
        out.append("\n### PSBs / PCBs addressing it\n")
        out.append(table(["PSB", "PCB #", "PROCOPT", "PROCSEQ", "LIST", "sensitive segments", "cite"],
                         [(p["psb"], p["ordinal"], p["procopt"] or "", p["procseq"] or "", "NO" if p["list_no"] else "",
                           ", ".join(_jl(p["sensegs"]))[:60], f"{p['mem']}:{p['line']}") for p in pcbs]))
    calls = conn.execute("""SELECT p.program_id, p.id AS pid, d.func, d.procopt, d.psb_name, d.io_area, d.line
                            FROM dli_call d JOIN program p ON p.id=d.program_id WHERE UPPER(d.dbd_name)=?
                            ORDER BY p.program_id, d.line""", (n,)).fetchall()
    if calls:
        out.append("\n### Programs whose DL/I calls resolve to it\n")
        agg: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for c in calls:
            agg[c["program_id"]].append(c)
        out.append(table(["program", "PSB", "functions", "PROCOPT", "updates?", "first cite"],
                         [(pg, cs[0]["psb_name"] or "", ", ".join(sorted({c["func"] or "?" for c in cs})),
                           cs[0]["procopt"] or "",
                           "YES" if any((c["func"] or "") in ("ISRT", "REPL", "DLET") for c in cs) else "no",
                           cite(conn, cs[0]["pid"], cs[0]["line"])) for pg, cs in sorted(agg.items())]))
    util = conn.execute("""SELECT j.job_name, s.step_name, s.effective_pgm, d.mode, s.line, s.from_proc, m.name AS mem
                           FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                           LEFT JOIN member m ON m.id=j.member_id
                           WHERE d.dd_name='*DBD*' AND UPPER(d.dsn_resolved)=?""", (n,)).fetchall()
    if util:
        out.append("\n### Utility jobs (image copy / unload / reload)\n")
        out.append(table(["job", "step", "utility", "reads/writes", "cite"],
                         [(u["job_name"] or "(PROC)", u["step_name"], u["effective_pgm"], u["mode"],
                           f"{u['from_proc'] or u['mem']}:{u['line']}") for u in util]))
    onl = conn.execute("SELECT dbd, access, line FROM ims_online_db WHERE UPPER(dbd)=?", (n,)).fetchall()
    if onl:
        out.append("- online (stage-1 DATABASE): " + ", ".join(f"ACCESS={o['access'] or '?'}" for o in onl) + "\n")
    out.append("\n> A program is listed only when its PCB position resolved through an indexed PSB; "
               "`program X` shows the mapping notes for the rest.\n")
    return "".join(out)


_DLI_STORE = ("ISRT", "REPL")
_DLI_READ = ("GU", "GN", "GHU", "GHN", "GNP", "GHNP")


def _segment_pcbs(conn: sqlite3.Connection, seg: str, dbds: List[str]) -> List[sqlite3.Row]:
    """The PCBs (of the named DBDs) sensitive to a segment."""
    if not dbds:
        return []
    q = ",".join("?" * len(dbds))
    return conn.execute(f"""SELECT ps.name AS psb, pc.ordinal, pc.dbd_name, pc.procopt, pc.sensegs
                            FROM ims_pcb pc JOIN ims_psb ps ON ps.id=pc.psb_id
                            WHERE UPPER(pc.dbd_name) IN ({q}) AND pc.sensegs LIKE ?
                            ORDER BY ps.name, pc.ordinal""", (*dbds, f'%"{seg}"%')).fetchall()


def _segment_calls(conn: sqlite3.Connection, seg: str, dbds: List[str],
                   pcbs: Optional[List[sqlite3.Row]] = None) -> List[Tuple[sqlite3.Row, str]]:
    """The DL/I calls that can touch a segment: on a PCB sensitive to it, or
    EXEC DLI SEGMENT(X) naming it (that names the segment even when the PCB
    position could not be resolved through a PSB). Each with how it was tied."""
    if not dbds:
        return []
    if pcbs is None:
        pcbs = _segment_pcbs(conn, seg, dbds)
    keys = {(p["psb"].upper(), p["ordinal"]) for p in pcbs}
    q = ",".join("?" * len(dbds))
    calls = conn.execute(f"""SELECT p.program_id, p.id AS pid, p.member_id, d.func, d.procopt, d.psb_name,
                                    d.pcb_ordinal, d.ssa_args, d.io_area, d.line, d.dbd_name
                             FROM dli_call d JOIN program p ON p.id=d.program_id
                             WHERE UPPER(d.dbd_name) IN ({q}) OR d.ssa_args LIKE ? ORDER BY p.program_id, d.line""",
                         (*dbds, f'%"{seg}"%')).fetchall()
    out = []
    for c in calls:
        via_pcb = (c["psb_name"] or "").upper(), c["pcb_ordinal"]
        named = seg in [x.upper() for x in _jl(c["ssa_args"])]
        if named:
            out.append((c, "SEGMENT() names it"))
        elif via_pcb in keys:
            out.append((c, "PCB sensitive to it"))
    return out


def _count_phrase(n: int, verb: str, plural_verb: str) -> str:
    return f"{n} program{'s' if n != 1 else ''} {plural_verb if n != 1 else verb} it"


def _segment_traffic(conn: sqlite3.Connection, seg: str, dbd: str) -> str:
    """'2 programs store it, 1 reads it' for one segment of one DBD."""
    calls = _segment_calls(conn, seg.upper(), [dbd.upper()])
    store = {c["program_id"] for c, _h in calls if (c["func"] or "") in _DLI_STORE}
    read = {c["program_id"] for c, _h in calls if (c["func"] or "") in _DLI_READ}
    dele = {c["program_id"] for c, _h in calls if (c["func"] or "") == "DLET"}
    if not (store or read or dele):
        return "no program touches it"
    parts = [_count_phrase(len(store), "stores", "store"), _count_phrase(len(read), "reads", "read")]
    if dele:
        parts.append(_count_phrase(len(dele), "deletes", "delete"))
    return ", ".join(parts)


class _AreaItem:
    """One data item of a program's I/O area at THIS program's offsets, with
    the line to cite (the copybook's own line when it came from a COPY)."""
    __slots__ = ("id", "parent_id", "name", "level", "pic", "usage", "offset", "length", "occurs",
                 "is_group", "redefines", "src_name", "src_line", "from_copy")

    def __init__(self, r: sqlite3.Row, src_name: Optional[str], src_line: Optional[int], from_copy: bool):
        self.id, self.parent_id, self.name, self.level = r["id"], r["parent_id"], r["name"], r["level"]
        self.pic, self.usage, self.offset, self.length = r["pic"], r["usage"], r["offset"] or 0, r["length"] or 0
        self.occurs = r["occurs_max"] or 1
        self.is_group, self.redefines = bool(r["is_group"]), r["redefines"]
        self.src_name, self.src_line, self.from_copy = src_name, src_line, from_copy

    @property
    def extent(self) -> int:                      # bytes covered, every occurrence
        return self.length * self.occurs

    def cite(self) -> str:
        if self.src_name and self.src_line:
            return f"`{self.src_name}:{self.src_line}`"
        return "(line not in the index's line map)"   # said, never dropped in silence

    def shape(self) -> str:                       # X(01) / S9(7)V99 COMP-3 / group
        if self.is_group:
            return "group"
        return (self.pic or "?") + (f" {self.usage}" if self.usage and self.usage != "DISPLAY" else "")


def _next_root_exp(conn: sqlite3.Connection, pid: int, member_id: int, root_exp: int, has_pfield: bool) -> int:
    """The expanded line where a program's 01 ends: the next 01/77 of the
    program's own (pfield roots, or `field` roots on an older index) or the
    first paragraph, whichever comes first; a far line when nothing follows."""
    bounds = []
    if has_pfield:
        bounds.append(conn.execute("SELECT MIN(exp_line) FROM pfield WHERE program_id=? AND parent_id IS NULL "
                                   "AND exp_line>?", (pid, root_exp)).fetchone()[0])
    else:
        bounds.append(conn.execute("SELECT MIN(line) FROM field WHERE member_id=? AND parent_id IS NULL AND line>?",
                                   (member_id, root_exp)).fetchone()[0])
    try:
        bounds.append(conn.execute("SELECT MIN(start_line) FROM paragraph WHERE program_id=?", (pid,)).fetchone()[0])
    except sqlite3.OperationalError:              # an index without the paragraph table: the roots bound it
        pass
    live = [b for b in bounds if b is not None and b > root_exp]
    return min(live) if live else 10 ** 9


def _copies_inside_01(conn: sqlite3.Connection, pid: int, member_id: int, root_exp: int, nxt_exp: int
                      ) -> Tuple[List[sqlite3.Row], List[str]]:
    """The COPY statements written inside a program's 01 (from its line to
    the next 01's), in ONE coordinate system. The 01's bounds come from
    `pfield` / `field`, whose lines are EXPANDED; `copy_use` counts the
    member's own lines. Both bounds are mapped back through the line map
    before they meet copy_use, so an 01 that follows a COPY is paired with
    ITS copybook and not the previous one's. A resolved copybook is kept
    only when its expanded run sits inside the 01 (a COPY nested in a
    copybook is recorded in that copybook's lines, not the program's).
    (the COPY rows in the index, the copybook names missing from it)."""
    m1, l1, d1, _v = origin(conn, pid, root_exp)
    if not m1 or d1 or l1 is None:
        return [], []                             # the 01 itself came from a copybook: nothing to pair
    l2 = 10 ** 9
    if nxt_exp < 10 ** 9:
        m2, ln2, d2, _v2 = origin(conn, pid, nxt_exp)
        if m2 == m1 and not d2 and ln2 is not None:
            l2 = ln2
        else:                                     # the next entry is inside a copybook: the program's first line after it
            r = conn.execute("SELECT MIN(src_start) FROM expand_run WHERE program_id=? AND depth=0 AND src_member=? "
                             "AND exp_start>?", (pid, member_id, nxt_exp)).fetchone()[0]
            l2 = r if r is not None else 10 ** 9
    rows = conn.execute("""SELECT copybook, resolved_member_id, replacing, line FROM copy_use
                           WHERE member_id=? AND line>? AND line<? ORDER BY line""", (member_id, l1, l2)).fetchall()
    kept, missing = [], []
    for c in rows:
        if c["resolved_member_id"]:
            run = conn.execute("SELECT 1 FROM expand_run WHERE program_id=? AND depth=1 AND src_member=? "
                               "AND exp_start>? AND exp_start<? LIMIT 1",
                               (pid, c["resolved_member_id"], root_exp, nxt_exp)).fetchone()
            if run:
                kept.append(c)
        elif c["copybook"].upper() not in expand._SYSTEM_INCLUDES:   # SQLCA / SQLDA: the compiler supplies them
            missing.append(c["copybook"].upper())
    return kept, missing


def _incomplete(missing: List[str], caveat: str) -> Tuple[None, str, str, str]:
    names = ", ".join(sorted(set(missing)))
    return (None, f"layout incomplete: copybook {names} not in the index", caveat,
            f"layout incomplete (copybook {names} missing) - bytes unknown")


def _io_area_items(conn: sqlite3.Connection, pid: int, member_id: int, area: str
                   ) -> Tuple[Optional[List[_AreaItem]], str, str, str]:
    """The items of a program's I/O area (the 01 a DL/I call names), at the
    program's offsets: the program's pfield rows first (they carry the
    copybook line to cite and the program's own offsets); on an index built
    before pfield existed, the program's `field` rows, else the rows of the
    one copybook COPYed inside that 01 (the copybook's own offsets - right
    when nothing precedes the COPY, which is the usual shape). An 01 holding
    a COPY of a copybook the index lacks is an incomplete layout, said as
    such - never a 0-byte area.
    (items or None, where the layout came from, a caveat or '', and when
    there are no items the plain-English reason a DBD field line prints)."""
    n = area.upper()
    prog = conn.execute("SELECT program_id FROM program WHERE id=?", (pid,)).fetchone()[0]
    has_pfield = True
    try:
        root = conn.execute("SELECT * FROM pfield WHERE program_id=? AND parent_id IS NULL AND UPPER(name)=? "
                            "ORDER BY id LIMIT 1", (pid, n)).fetchone()
    except sqlite3.OperationalError:
        root, has_pfield = None, False            # an index built before the value-flow tables
    undeclared = (f"{n} is not declared in {prog} (no 01 of that name; a LINKAGE item or a copybook "
                  f"missing from the index?)")
    if has_pfield:
        if root is None:
            sub = conn.execute("""SELECT p.level, r.name AS root_name FROM pfield p JOIN pfield r ON r.id=p.root_id
                                  WHERE p.program_id=? AND UPPER(p.name)=? AND p.parent_id IS NOT NULL
                                  ORDER BY p.id LIMIT 1""", (pid, n)).fetchone()
            if sub:
                why = (f"{n} is a level {sub['level']} item under 01 {sub['root_name']} in {prog}, not an 01; "
                       f"this report places DBD fields in 01-level areas only")
                return None, f"not an 01 (level {sub['level']} under {sub['root_name']})", "", why
            return None, f"not declared in {prog}", "", undeclared
        if not root["pruned"]:
            nxt_exp = _next_root_exp(conn, pid, member_id, root["exp_line"], True)
            _kept, missing = _copies_inside_01(conn, pid, member_id, root["exp_line"], nxt_exp)
            if missing:
                return _incomplete(missing, "")
            rows = conn.execute("""SELECT p.*, m.name AS src_name FROM pfield p LEFT JOIN member m ON m.id=p.src_member
                                   WHERE p.root_id=? ORDER BY p.offset, p.id""", (root["root_id"],)).fetchall()
            items = [_AreaItem(r, r["src_name"], r["src_line"], bool(r["src_member"] and r["src_member"] != member_id))
                     for r in rows]
            books = sorted({i.src_name for i in items if i.from_copy and i.src_name})
            return items, ("copybook " + ", ".join(books)) if books else "in the program", "", ""
        caveat = ("the index stored this 01 without its items: the rows below are the copybook's or the "
                  "program's own, not at this program's offsets")
    else:
        caveat = "this index was built before the value-flow tables: rebuild it for this program's own offsets"

    def subtree(roots: List[sqlite3.Row], src: str, from_copy: bool) -> List[_AreaItem]:
        items, todo = [], list(roots)
        while todo:
            cur = todo.pop(0)
            if from_copy:                         # a copybook's rows count its own lines: cite as they are
                items.append(_AreaItem(cur, src, cur["line"], True))
            else:                                 # the program's rows count EXPANDED lines: map back to the member's
                tag, ln, _d, _v = origin(conn, pid, cur["line"])
                items.append(_AreaItem(cur, tag, ln if tag else None, False))
            todo.extend(conn.execute("SELECT * FROM field WHERE parent_id=? ORDER BY id", (cur["id"],)).fetchall())
        items.sort(key=lambda i: (i.offset, i.id))
        return items

    root = conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL AND UPPER(name)=? ORDER BY id LIMIT 1",
                        (member_id, n)).fetchone()
    if root is None:
        return None, f"not declared in {prog}", caveat, undeclared
    nxt_exp = _next_root_exp(conn, pid, member_id, root["line"] or 0, False)
    copies, missing = _copies_inside_01(conn, pid, member_id, root["line"] or 0, nxt_exp)
    if missing:
        return _incomplete(missing, caveat)
    mem = conn.execute("SELECT name FROM member WHERE id=?", (member_id,)).fetchone()[0]
    if not root["is_group"] or conn.execute("SELECT 1 FROM field WHERE parent_id=? LIMIT 1", (root["id"],)).fetchone():
        return subtree([root], mem, False), "in the program", caveat, ""
    lib = _osvs_library_root(conn, member_id, root)
    if lib:
        _p, cb_mid, cb_root = lib
        cbm = conn.execute("SELECT name FROM member WHERE id=?", (cb_mid,)).fetchone()[0]
        return subtree([cb_root], cbm, True), f"copybook {cbm}", caveat, ""
    # `01 X.` followed by `COPY Y.`: the one copybook inside the 01 answers with its own rows
    if len(copies) == 1 and not copies[0]["replacing"]:
        cb_mid = copies[0]["resolved_member_id"]
        cbm = conn.execute("SELECT name FROM member WHERE id=?", (cb_mid,)).fetchone()[0]
        tops = conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL ORDER BY id", (cb_mid,)).fetchall()
        if tops:
            return subtree(tops, cbm, True), f"copybook {cbm}", caveat, ""
    why = (f"{len(copies)} COPY statements inside the 01" if len(copies) > 1 else
           "COPY with REPLACING inside the 01" if copies and copies[0]["replacing"] else
           "the copybook inside the 01 has no items in the index" if copies else "the 01 has no items in this index")
    return None, "not indexed as a layout", f"{why}; {caveat}", f"I/O area {n} not indexed as a layout ({why})"


def _under_redefines(items: List[_AreaItem], it: _AreaItem) -> bool:
    by_id = {i.id: i for i in items}
    cur: Optional[_AreaItem] = it
    while cur is not None:
        if cur.redefines:
            return True
        cur = by_id.get(cur.parent_id) if cur.parent_id is not None else None
    return False


def _at_offset(items: List[_AreaItem], lo: int, hi: int) -> Tuple[List[_AreaItem], List[_AreaItem]]:
    """The elementary items covering 0-based bytes lo..hi: (the plain ones,
    the ones under a REDEFINES - the same bytes under another name)."""
    hits = [i for i in items if not i.is_group and i.offset <= hi and i.offset + i.extent - 1 >= lo]
    plain = [i for i in hits if not _under_redefines(items, i)]
    other = [i for i in hits if _under_redefines(items, i)]
    return plain, other


def _describe_hit(items: List[_AreaItem], lo: int, hi: int, src: str) -> Tuple[str, Optional[tuple]]:
    """One program's answer for one DBD field: the text after the program
    name, and a key (layout source, names, bytes, PIC) that says whether two
    programs see the same layout at these bytes. Never the nearest name: a
    miss is said as a miss, a FILLER as a FILLER, a longer or shorter item
    or a run of items as such."""
    plain, other = _at_offset(items, lo, hi)
    where = f"via {src}" if src.startswith("copybook") else "in the program"
    if not plain and not other:
        area_len = max((i.offset + i.extent for i in items), default=0)
        return f": no field at that offset (area shorter: {area_len} bytes)", None
    hits = plain or other
    note = ""
    if plain and other:
        note = " - also " + ", ".join(f"{o.name} ({o.shape()}, REDEFINES {o.redefines or 'a parent'})" for o in other)
    elif not plain:
        note = " - under a REDEFINES"
    cites = " ".join(sorted({h.cite() for h in hits if h.cite()}))
    key = (src, tuple(h.name for h in hits), tuple((h.offset, h.length, h.pic) for h in hits))
    if len(hits) == 1:
        h = hits[0]
        first, last = h.offset + 1, h.offset + h.extent
        if h.name == "FILLER":                    # a difference in itself: no 'layout differs' on top
            return f": FILLER at that offset {where} ({h.shape()}, bytes {first}-{last}) {cites}{note}".rstrip(), None
        size = ""
        if (h.offset, h.extent) != (lo, hi - lo + 1):
            size = (f", bytes {first}-{last}: " + ("longer than" if h.extent > hi - lo + 1 else "shorter than")
                    + " the DBD field" + (f", OCCURS {h.occurs}" if h.occurs > 1 else ""))
        return f" {h.name} {where} ({h.shape()}{size}) {cites}{note}".rstrip(), key
    names = f"{hits[0].name}..{hits[-1].name}"
    shapes = f"{hits[0].shape()}..{hits[-1].shape()}"
    fillers = " (FILLER inside)" if any(h.name == "FILLER" for h in hits) else ""
    return f" spans {names} {where} ({shapes}){fillers} {cites}{note}".rstrip(), key


def _segment_offsets(conn: sqlite3.Connection, seg: sqlite3.Row,
                     calls: List[Tuple[sqlite3.Row, str]]) -> str:
    """For each DBD FIELD of a segment, the item at its bytes in every
    program's I/O area - whatever that program calls it. A field NAME is not
    the handle across programs (each copybook names the byte its own way);
    the byte range inside the I/O area is."""
    flds = conn.execute("SELECT name, start, bytes, is_seq, line FROM ims_field WHERE segment_id=? ORDER BY start, id",
                        (seg["id"],)).fetchall()
    dbd_mem = conn.execute("SELECT m.name FROM ims_dbd d JOIN member m ON m.id=d.member_id WHERE d.id=?",
                           (seg["dbd_id"],)).fetchone()[0]
    out = [f"\n### DBD {seg['dbd']} segment {seg['name']}: each DBD field at its bytes in every program's I/O area\n"]
    # one row per (program, I/O area): the area's layout and whether the program stores the segment
    areas: Dict[Tuple[str, str], dict] = {}
    for c, how in calls:
        area = (c["io_area"] or "").upper()
        key = (c["program_id"], area)
        a = areas.get(key)
        if a is None:
            items, src, caveat, why = (_io_area_items(conn, c["pid"], c["member_id"], area) if area
                                       else (None, "no I/O area on the call", "", "no I/O area on the call"))
            a = areas[key] = {"pid": c["pid"], "items": items, "src": src, "caveat": caveat, "why": why,
                              "funcs": set(), "first": c}
        a["funcs"].add(c["func"] or "?")
    if not areas:
        out.append("_no program's DL/I call reaches this segment through an indexed PSB_\n")
        return "".join(out)
    rows = []
    for (pg, area), a in sorted(areas.items()):
        items = a["items"]
        size = max((i.offset + i.extent for i in items), default=0) if items else None
        stores = any(f in _DLI_STORE for f in a["funcs"])
        rows.append((pg, area or "(none)", a["src"] + (f" ({a['caveat']})" if a["caveat"] else ""),
                     size if size is not None else "?", ", ".join(sorted(a["funcs"])), "stores" if stores else "reads",
                     cite(conn, a["pid"], a["first"]["line"])))
    out.append(table(["program", "I/O area", "layout from", "bytes", "functions", "stores/reads", "first call"], rows))
    if seg["bytes"]:
        out.append(f"- the DBD says the segment is {seg['bytes']} bytes; an area of another size holds a "
                   f"sibling segment, a prefix, or a partial layout\n")
    if not flds:
        out.append("_the DBD declares no FIELD for this segment (a DBD names only its SEQ and search fields); "
                   "the I/O areas above hold the whole segment - `layout` shows each one_\n")
        return "".join(out)
    per_prog: Dict[str, int] = defaultdict(int)
    for (pg, _a) in areas:
        per_prog[pg] += 1
    for f in flds:
        if f["start"] is None or f["bytes"] is None:
            out.append(f"- {f['name']}: START / BYTES not given in the DBD `{dbd_mem}:{f['line']}`\n")
            continue
        lo, hi = f["start"] - 1, f["start"] - 1 + f["bytes"] - 1
        parts = []
        first_key: Optional[tuple] = None
        first_pg: Optional[str] = None
        for (pg, area), a in sorted(areas.items()):
            if not area:                          # a call with no I/O area: say which call
                label = f"{pg} (call at {cite(conn, a['pid'], a['first']['line'])})"
            else:
                label = pg + (f" ({area})" if per_prog[pg] > 1 else "")
            stores = any(fn in _DLI_STORE for fn in a["funcs"])
            if a["items"] is None:
                parts.append(f"{label}: {a['why']}")
                continue
            text, key = _describe_hit(a["items"], lo, hi, a["src"])
            notes = []
            if stores:
                notes.append("stored by this program")
            if key is not None:
                if first_key is None:
                    first_key, first_pg = key, label
                elif key != first_key:
                    notes.append(f"layout differs from {first_pg}'s")
            parts.append(label + text + (" - " + ", ".join(notes) if notes else ""))
        seq = " (SEQ)" if f["is_seq"] else ""
        out.append(f"- {f['name']}{seq} (bytes {f['start']}-{f['start'] + f['bytes'] - 1}): " + "; ".join(parts) + "\n")
    out.append("> Bytes are 1-based as the DBD counts them; the match is by byte range inside the I/O area, never "
               "by name. Cite a copybook line as `[[COPYBOOK line \"05  FIELD-NAME\"]]`. 'stored by this program' = "
               "it ISRTs or REPLs on this PCB: those programs are the writers of the value.\n")
    return "".join(out)


def cmd_segment(conn: sqlite3.Connection, name: str, dbd: Optional[str] = None) -> str:
    """Who touches an IMS segment: the DBDs holding it, the PCBs sensitive to
    it, the programs using those PCBs (with function and PROCOPT), and each
    DBD field at its bytes in every program's I/O area - the way to find where
    'the gender field' is populated when every program names the byte
    differently. `dbd` keeps one DBD when several use the segment name."""
    n = name.upper()
    segs = conn.execute("""SELECT s.*, d.name AS dbd FROM ims_segment s JOIN ims_dbd d ON d.id=s.dbd_id
                           WHERE UPPER(s.name)=? ORDER BY d.name, s.id""", (n,)).fetchall()
    out = [f"# IMS segment {n}" + (f" in DBD {dbd.upper()}" if dbd else "") + "\n"]
    if dbd:
        segs = [s for s in segs if s["dbd"].upper() == dbd.upper()]
    if not segs:
        return out[0] + "\n**NOT FOUND** in any indexed DBD" + (f" named {dbd.upper()}" if dbd else "") + ".\n"
    out.append(table(["DBD", "parent", "bytes", "seq field"],
                     [(s["dbd"], s["parent"] or "(root)", s["bytes"], s["seq_field"] or "") for s in segs]))
    dbds = sorted({s["dbd"].upper() for s in segs})
    if len(dbds) > 1:
        out.append(f"- {len(dbds)} DBDs hold a segment of this name; `segment {n} --dbd NAME` keeps one\n")
    pcbs = _segment_pcbs(conn, n, dbds)
    if pcbs:
        out.append("\n### PCBs sensitive to it\n")
        out.append(table(["PSB", "PCB #", "DBD", "PROCOPT"],
                         [(p["psb"], p["ordinal"], p["dbd_name"], p["procopt"] or "") for p in pcbs]))
    calls = _segment_calls(conn, n, dbds, pcbs)
    rows = [(c["program_id"], c["func"] or "?", c["procopt"] or "", c["psb_name"] or "", c["io_area"] or "", how,
             cite(conn, c["pid"], c["line"])) for c, how in calls]
    if rows:
        out.append("\n### Programs (DL/I calls on a PCB sensitive to this segment)\n")
        out.append(table(["program", "func", "PROCOPT", "PSB", "I/O area", "how", "cite"], rows))
        out.append("> CBLTDLI calls name the segment inside the SSA data, not in the call: a call on a "
                   "multi-segment PCB may touch a sibling segment instead. EXEC DLI SEGMENT() is exact.\n")
    for s in segs:
        if len(segs) > 1:
            # each DBD's own callers: a call is tied to a DBD through its PCB
            pcb_keys = {(p["psb"].upper(), p["ordinal"]) for p in pcbs if p["dbd_name"].upper() == s["dbd"].upper()}
            seg_calls = [(c, how) for c, how in calls
                         if ((c["psb_name"] or "").upper(), c["pcb_ordinal"]) in pcb_keys
                         or (how == "SEGMENT() names it" and (c["dbd_name"] or "").upper() in ("", s["dbd"].upper()))]
        else:
            seg_calls = calls
        out.append(_segment_offsets(conn, s, seg_calls))
    return "".join(out)


def _osvs_library_root(conn: sqlite3.Connection, mid: int,
                       root: sqlite3.Row) -> Optional[Tuple[str, int, sqlite3.Row]]:
    """A program's 01 with no children that "01 X COPY Y." (OS/VS) renamed
    from the library's own 01: (program id, copybook member, the copybook's
    01 row - it holds the fields). None for anything else."""
    has_kids = "SELECT 1 FROM field WHERE parent_id=? LIMIT 1"
    if conn.execute(has_kids, (root["id"],)).fetchone():
        return None
    for a in conn.execute("""SELECT p.program_id, a.copybook, a.orig_name FROM field_alias a
                             JOIN program p ON p.id=a.program_id
                             WHERE p.member_id=? AND UPPER(a.new_name)=?""", (mid, root["name"].upper())):
        book = (a["copybook"] or "").upper()
        cb = conn.execute("""SELECT resolved_member_id FROM copy_use WHERE member_id=? AND UPPER(copybook)=?
                             AND resolved_member_id IS NOT NULL""", (mid, book)).fetchone()
        if not cb:
            # copied from inside another copybook: the member by name
            cb = conn.execute("SELECT id FROM member WHERE UPPER(name)=? AND kind='copybook'", (book,)).fetchone()
        if not cb:
            continue
        lib = conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL AND UPPER(name)=?",
                           (cb[0], a["orig_name"].upper())).fetchone()
        if lib and conn.execute(has_kids, (lib["id"],)).fetchone():
            return a["program_id"], cb[0], lib
    return None


def built_before_item_27(conn: sqlite3.Connection) -> bool:
    """True for an index whose last build ran before ROADMAP re-parse item 27:
    it recorded no system_includes (the item's build records the manifest's,
    '[]' for none). Such a build left a copybook's own COPY rows unresolved
    and wrote no 'layout_warning' for a nested COPY."""
    try:
        if "system_includes" not in {r[1] for r in conn.execute("PRAGMA table_info(build_run)")}:
            return True
        row = conn.execute("SELECT system_includes FROM build_run ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return True
    return not row or row[0] is None


def _member_line(conn: sqlite3.Connection, member_id: int, line: int) -> Optional[str]:
    """One line of a member's own text, from its search rows (every line of a
    member is one), or None when the index holds no row of that line."""
    try:
        spans = _spans(conn, member_id)
        if spans:
            for lo, hi in spans:                      # a range read: fast whatever the size of the index
                r = conn.execute("SELECT text FROM src_fts WHERE rowid BETWEEN ? AND ? AND line_no=? LIMIT 1",
                                 (lo, hi, line)).fetchone()
                if r:
                    return str(r[0])
            return None
        r = conn.execute("SELECT text FROM src_fts WHERE member_id=? AND line_no=? LIMIT 1", (member_id, line)).fetchone()
    except sqlite3.Error:
        return None
    return str(r[0]) if r else None


# the words of `EXEC SQL INCLUDE X` as one line of it holds them: the whole statement, its first line
# (`EXEC SQL`) or its INCLUDE line when written over three (tools/synth/repro/F05)
_SQL_INCLUDE_LINE = re.compile(r"(?<![A-Z0-9\-])EXEC\s+SQL(?![A-Z0-9\-])|^\s*INCLUDE\s", re.I)


def system_include_written(conn: sqlite3.Connection, member_id: int, book: str, line: int) -> Optional[str]:
    """How the copybook `member_id` writes `book` (SQLCA / SQLDA) at `line`,
    for an index built before ROADMAP re-parse item 27, whose row says neither:
    'copy' (`COPY SQLCA` - a copybook like any other, NOT FOUND when no
    member carries it), 'include' (`EXEC SQL INCLUDE SQLCA` - the
    precompiler's), or None when nothing in the index says. The copybook's
    own line first, read with the expander's own patterns; else the
    programs copying it - that build wrote '(in COPY <copybook>) L<line>:
    COPY SQLCA NOT FOUND' into a copier's notes for a COPY, and nothing for
    an INCLUDE. With no member of the name, `layout` took every such row for
    the precompiler's: a copybook's `COPY SQLCA` was said to be `EXEC SQL
    INCLUDE SQLCA`, which moves nothing, while `program` said NOT FOUND for
    the same line (LESSONS 219)."""
    book = (book or "").upper()
    me = conn.execute("SELECT name FROM member WHERE id = ?", (member_id,)).fetchone()
    if not me:
        return None
    text = _member_line(conn, member_id, line)
    if text is not None:
        masked = expand._mask_literals(text)
        m = expand.find_copy_start(text, masked)
        if m and m.group(3).upper() == book:
            return "copy"
        if _SQL_INCLUDE_LINE.search(masked):
            return "include"
    try:
        noted = conn.execute("""SELECT 1 FROM unresolved u WHERE u.kind = 'expand' AND instr(u.detail, ?) > 0
                                AND u.member_id IN (SELECT c.member_id FROM copy_use c WHERE c.resolved_member_id = ?)
                                LIMIT 1""", (f"(in COPY {str(me[0]).upper()}) L{line}: COPY {book} NOT FOUND",
                                             member_id)).fetchone()
    except sqlite3.Error:
        noted = None
    return "copy" if noted else None


def nested_copy_lines(conn: sqlite3.Connection, member_id: int) -> List[str]:
    """Under a copybook's own layout, one line per COPY written in it. The
    build of ROADMAP re-parse item 27 computes the layout with the nested
    copybook in place: its bytes are counted in the offsets, its items are
    its own member's rows (cited there, not listed here), and the copy_use
    row names the member expanded. A COPY whose text is not in the layout
    carries the copybook's 'layout_warning' row, said as it is - one of the
    copybook's own lines ('L2: COPY X NOT FOUND ...') or one deeper, in the
    copybook it copies ('(in COPY B) L2: COPY C NOT FOUND ...'), said under
    B's line: B's bytes are counted then, all but C's. `EXEC SQL INCLUDE
    SQLCA` / `SQLDA` has neither by design - the precompiler writes it, a
    record of its own. A row with neither is one an index built before the
    item holds - every nested COPY was left out of a copybook's own layout
    and each item after it sat that many bytes too early
    (tools/synth/repro/F06); a program's view counts the copy only when a
    member of its name is in the index. Before the verifier's round on the
    item, the SQLCA line and the IBM-supplied one said a program's view
    counts them, and a NOT FOUND two levels down was never shown (LESSONS
    216). On such an index a SQLCA / SQLDA row is read by the copybook's own
    line (system_include_written): a `COPY SQLCA` no member carries is NOT
    FOUND, as `program` says of it, not the precompiler's (LESSONS 219)."""
    rows = conn.execute("""SELECT c.copybook, c.line, c.resolved_member_id, r.path, r.norm_sha FROM copy_use c
                           LEFT JOIN member r ON r.id = c.resolved_member_id
                           WHERE c.member_id = ? ORDER BY c.line, c.id""", (member_id,)).fetchall()
    if not rows:
        return []
    from . import build as _build, recover          # local imports: build imports nothing from query
    # the warnings of a COPY whose text is not in the layout, in the order the build wrote them (a SYNC or a
    # REDEFINES warning is another kind of row)
    nested = [str(r[0]) for r in conn.execute(
        "SELECT detail FROM unresolved WHERE member_id = ? AND kind = 'layout_warning' ORDER BY id", (member_id,))
        if _build.NESTED_NOT_COUNTED in str(r[0])]
    used: Set[str] = set()
    me = conn.execute("SELECT name FROM member WHERE id = ?", (member_id,)).fetchone()
    own_name = str(me[0]).upper() if me else ""
    supplied: Optional[Dict[str, str]] = None
    kinds = ",".join("?" * len(recover.RESOLVER_KINDS))
    out = []
    for c in rows:
        book = str(c["copybook"]).upper()
        line = c["line"]
        if c["resolved_member_id"]:
            texts = conn.execute("SELECT COUNT(DISTINCT norm_sha) FROM member WHERE name = ? AND kind IN "
                                 "('copybook', 'cobol', 'sql', 'unknown')", (book,)).fetchone()[0]
            chosen = (f" - the copy in `{c['path']}`, one of {texts} texts of the name (`copybook {book}`)"
                      if texts > 1 else "")
            # a COPY inside it whose text is not here (NOT FOUND, recursive, IBM-supplied): its warning, under this line
            deeper = list(dict.fromkeys(w for w in nested if w.startswith(f"(in COPY {book}) ")))
            used.update(deeper)
            if deeper:
                out.append(f"- `COPY {book}` at line {line}: its bytes are counted in the offsets above, all but those of "
                           f"the COPY{'' if len(deeper) == 1 else 's'} in it said below; its items are {book}'s own rows, "
                           f"not listed here (`layout {book}`){chosen}\n")
                out.extend(f"  - {w}\n" for w in deeper)
            else:
                out.append(f"- `COPY {book}` at line {line}: its bytes are counted in the offsets above; its items are "
                           f"{book}'s own rows, not listed here (`layout {book}`){chosen}\n")
            continue
        head = re.compile(rf"L\d+: COPY {re.escape(book)}[ :]")
        # the warning at this line; else the one of the name not said yet (an `01 X` / `COPY Y.` statement starts on
        # the line before the COPY: the parser's line and the expander's may differ)
        said = (next((w for w in nested if w not in used and w.startswith(f"L{line}: ") and head.match(w)), None)
                or next((w for w in nested if w not in used and head.match(w)), None))
        if said:
            used.add(said)
            out.append(f"- {said}\n")
            continue
        if supplied is None:
            supplied = recover.supplied_copybooks(conn)
        carried = conn.execute(f"SELECT 1 FROM member WHERE name = ? AND kind IN ({kinds}) LIMIT 1",
                               (book, *recover.RESOLVER_KINDS)).fetchone()
        # SQLCA / SQLDA with no member and no warning: on an index built by the item always `EXEC SQL INCLUDE` (a
        # `COPY SQLCA` it found no member for has its warning); on one built before, the copybook's own line says,
        # else a copier's note (system_include_written) - a `COPY SQLCA` no member carries was said to be the
        # precompiler's beside `program`'s NOT FOUND (LESSONS 219)
        how = None
        if book in expand._SYSTEM_INCLUDES:
            how = (system_include_written(conn, member_id, book, line) if built_before_item_27(conn)
                   else "include")
        if how == "include":
            # `EXEC SQL INCLUDE SQLCA`: recorded with no member and no warning on purpose (LESSONS 203); the
            # precompiler writes the area as an 01 of its own, inside no record of this copybook
            out.append(f"- At line {line}: {precompiler_cell(book)}. The precompiler writes it as a record of its own "
                       f"(`01 {book}`), so it moves no offset above\n")
        elif book in expand._SYSTEM_INCLUDES and how is None:
            copy_reading = (f"it is the copy in the index (`copybook {book}`), whose bytes are NOT counted above"
                            if carried else f"no member of the index carries {book}, so its bytes are NOT counted "
                                            "above nor in any program's view")
            out.append(f"- `{book}` at line {line}: this index was built before ROADMAP re-parse item 27 and does not say "
                       f"which statement names it. Written `EXEC SQL INCLUDE {book}`, the precompiler writes it as a "
                       f"record of its own (`01 {book}`) and it moves no offset above; written `COPY {book}`, "
                       f"{copy_reading} - an item after it in the same record sits further on by its length. The next "
                       "build says which\n")
        elif book in supplied:
            product = supplied[book]
            out.append(f"- `COPY {book}` at line {line}: **{supplied_label(product)}** - {supplied_why(book, product)}. "
                       "Its bytes are not in this layout nor in any program's view, so an item after it in the same "
                       "record sits further on by its length\n")
        elif book == own_name:
            out.append(f"- `COPY {book}` at line {line}: the copybook copies itself - the expander skips such a COPY as "
                       "recursive in every view, so its bytes are in none of them\n")
        elif not carried:
            out.append(f"- `COPY {book}` at line {line}: its bytes are NOT counted in the offsets above, so an item after "
                       f"it in the same record sits further on by {book}'s length. No member of the index carries {book}, "
                       f"so no program's view counts it either (`copybook {book}` says what to fetch)\n")
        else:
            out.append(f"- `COPY {book}` at line {line}: its bytes are NOT counted in the offsets above, so an item "
                       f"after it in the same record sits further on by {book}'s length. An index built before ROADMAP "
                       "re-parse item 27 leaves every nested copybook out of a copybook's own layout; `layout RECORD "
                       "--program PGM` gives a program's view, which counts it\n")
    # a warning no row above holds (its COPY row spells the name otherwise): said as it is, never dropped
    out.extend(f"- {w}\n" for w in dict.fromkeys(w for w in nested if w not in used))
    return out


def cmd_layout(conn: sqlite3.Connection, name: str, program: Optional[str] = None) -> str:
    """The byte layout of a copybook / 01 record from the parser's numbers:
    offsets, lengths, PIC, usage, OCCURS/ODO/REDEFINES, 88 values, record
    length. This is what test data and interface contracts are built from -
    never from the model's own COMP-3 arithmetic."""
    n = name.upper()
    out = [f"# Layout {n}" + (f" (as seen in {program.upper()})" if program else "") + "\n"]
    def top_of(fid: int) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM field WHERE id=?", (fid,)).fetchone()
        while r and r["parent_id"]:
            r = conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
        return r

    members: List[Tuple[int, sqlite3.Row]] = []
    if program:
        progs = programs_named(conn, program)
        if not progs:
            return out[0] + f"\nprogram {program} not found\n"
        mid, pid = progs[0]["member_id"], progs[0]["id"]
        roots = conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL AND UPPER(name)=?", (mid, n)).fetchall()
        members = [(mid, r) for r in roots]
        if not members:
            # COPY X REPLACING: the renamed fields live in the PROGRAM's rows
            # under the 01 that holds the COPY - find it through the aliases.
            renamed = [r["new_name"].upper() for r in conn.execute(
                "SELECT new_name FROM field_alias WHERE program_id=? AND (UPPER(copybook)=? OR UPPER(orig_name)=?)",
                (pid, n, n))]
            seen_ids = set()
            for nm in renamed:
                fr = conn.execute("SELECT id FROM field WHERE member_id=? AND UPPER(name)=?", (mid, nm)).fetchone()
                if fr:
                    top = top_of(fr["id"])
                    if top and top["id"] not in seen_ids:
                        seen_ids.add(top["id"])
                        members.append((mid, top))
        if not members:
            # a copybook the program includes without REPLACING: its own rows
            cb = conn.execute("""SELECT resolved_member_id FROM copy_use WHERE member_id=? AND UPPER(copybook)=?
                                 AND resolved_member_id IS NOT NULL""", (mid, n)).fetchone()
            if cb:
                members = [(cb[0], r) for r in conn.execute(
                    "SELECT * FROM field WHERE member_id=? AND parent_id IS NULL ORDER BY id", (cb[0],)).fetchall()]
    else:
        for m in conn.execute("SELECT id, name, path FROM member WHERE UPPER(name)=? AND kind IN ('copybook','cobol')", (n,)):
            for r in conn.execute("SELECT * FROM field WHERE member_id=? AND parent_id IS NULL ORDER BY id", (m["id"],)):
                members.append((m["id"], r))
        if not members:
            for r in conn.execute("""SELECT f.* FROM field f WHERE UPPER(f.name)=? AND f.parent_id IS NULL""", (n,)):
                members.append((r["member_id"], r))
    if not members:
        return out[0] + "\n**NOT FOUND** - no copybook member or 01 level with this name is indexed.\n"

    # OS/VS "01 X COPY Y.": the program keeps only its renamed 01 (no children);
    # the fields are the library's own rows, shown under X's name.
    renames: Dict[int, List[Tuple[str, str, int]]] = {}   # library 01 id -> (X, program, level)
    swapped: List[Tuple[int, sqlite3.Row]] = []
    for mid, root in members:
        lib = _osvs_library_root(conn, mid, root)
        if not lib:
            swapped.append((mid, root))
            continue
        pname, cb_mid, cb_root = lib
        if cb_root["id"] not in renames:
            renames[cb_root["id"]] = []
            swapped.append((cb_mid, cb_root))
        if (root["name"], pname, root["level"]) not in renames[cb_root["id"]]:
            renames[cb_root["id"]].append((root["name"], pname, root["level"]))
    members = swapped

    def shown(r: sqlite3.Row) -> str:
        names = {x for (x, _p, _l) in renames.get(r["id"], [])}
        return names.pop() if len(names) == 1 else r["name"]

    # A copybook FRAGMENT (a run of 05s with the 01 in the including program)
    # is one layout, not one table per 05.
    groups: List[Tuple[int, List[sqlite3.Row]]] = []
    for mid, root in members:
        if groups and groups[-1][0] == mid and root["level"] > 1 and groups[-1][1][-1]["level"] > 1:
            groups[-1][1].append(root)
        else:
            groups.append((mid, [root]))
    nested_said: Set[int] = set()                     # a copybook's nested COPY lines, once under its last table
    last_of = {m: k for k, (m, _r) in enumerate(groups)}
    for gi, (mid, roots) in enumerate(groups):
        mem = conn.execute("SELECT name, path, kind FROM member WHERE id=?", (mid,)).fetchone()
        root = roots[0]
        title = shown(root) if len(roots) == 1 else f"(fragment: {len(roots)} top-level items, 01 is in the including program)"
        out.append(f"\n## {title}  in `{mem['name']}` ({mem['kind']})  line {root['line']}\n")
        out.append("```\n")
        out.append(f"{'OFF':>6} {'LEN':>4} {'LVL':>3}  {'FIELD':<34} {'PIC':<16} {'USAGE':<8}\n")
        out.append("-" * 82 + "\n")
        warn = []

        def walk(f, depth):
            ind = "  " * depth
            occ = f"  OCCURS {f['occurs_max']}" if f["occurs_max"] else ""
            odo = f" DEPENDING ON {f['odo_on']}" if f["odo_on"] else ""
            red = f"  REDEFINES {f['redefines']}" if f["redefines"] else ""
            nm = shown(f) if depth == 0 else f["name"]
            out.append(f"{f['offset']:>6} {f['length']:>4} {f['level']:>3}  {(ind + nm):<34} {(f['pic'] or ''):<16} "
                       f"{(f['usage'] or ''):<8}{occ}{odo}{red}\n")
            if f["odo_on"]:
                warn.append(f"{nm}: OCCURS DEPENDING ON {f['odo_on']} - offsets after it hold for the MAXIMUM "
                            f"({f['occurs_max']}) occurrences only")
            for c in conn.execute("SELECT name, values_lit FROM cond88 WHERE field_id=? ORDER BY id", (f["id"],)):
                out.append(f"{'':>6} {'':>4}  88  {(ind + '  ' + c['name']):<34} VALUE {' '.join(_jl(c['values_lit']))}\n")
            for c in conn.execute("SELECT * FROM field WHERE parent_id=? ORDER BY id", (f["id"],)):
                walk(c, depth + 1)

        for r in roots:
            walk(r, 0)
        total = max(r["offset"] + r["length"] for r in roots)
        out.append("-" * 82 + "\n")
        out.append(f"record length: {total} bytes (VB files: +4 for the RDW; the LRECL in the JCL/IDCAMS must agree)\n")
        out.append("```\n")
        for w in warn:
            out.append(f"- {w}\n")
        for (x, pname, lvl) in renames.get(root["id"], []):
            out.append(f"- `{lvl:02d} {x} COPY {mem['name']}.` in {pname} (OS/VS): {x} is this copybook's "
                       f"{root['name']} under the program's name; these are its fields\n")
        aliases = conn.execute("SELECT p.program_id, a.new_name, a.orig_name FROM field_alias a JOIN program p ON p.id=a.program_id "
                               "WHERE UPPER(a.copybook)=? LIMIT 12", (mem["name"].upper(),)).fetchall()
        osvs = {(p, x) for (x, p, _l) in renames.get(root["id"], [])}   # said above, not REPLACING
        aliases = [a for a in aliases if (a["program_id"], a["new_name"]) not in osvs]
        if aliases:
            out.append("- REPLACING in: " + "; ".join(f"{a['program_id']} ({a['orig_name']} -> {a['new_name']})" for a in aliases[:6]) + "\n")
        if mem["kind"] == "copybook" and not program and last_of[mid] == gi and mid not in nested_said:
            nested_said.add(mid)
            out.extend(nested_copy_lines(conn, mid))
    out.append("\n> Offsets are 0-based bytes from the parser (COMP-3 packed, COMP 2/4/8, SIGN SEPARATE +1); SYNC "
               "alignment is NOT modelled. Cite as `[[MEMBER line \"05  FIELD-NAME\"]]`.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# crud / conditions / paragraph - the small analysis questions
# --------------------------------------------------------------------------

def _crud_of(conn: sqlite3.Connection, pid: int) -> Dict[Tuple[str, str], Dict[str, str]]:
    """{(kind, target): {'C'|'R'|'U'|'D': first cite}} for one program from
    file OPEN modes + WRITE/REWRITE/DELETE, SQL verbs, DL/I functions (with
    the resolved database) and CICS file verbs."""
    out: Dict[Tuple[str, str], Dict[str, str]] = defaultdict(dict)

    def mark(key: Tuple[str, str], letters: str, line: int) -> None:
        for c in letters:
            out[key].setdefault(c, cite(conn, pid, line))

    # files: the SELECT -> DD -> dataset name when a job is indexed, else the DD
    dsn_by_sel: Dict[str, str] = {}
    for f in conn.execute("SELECT select_name, assign_dd FROM file_decl WHERE program_id=?", (pid,)):
        d = conn.execute("""SELECT d.dsn_resolved FROM dd d JOIN step s ON s.id=d.step_id
                            WHERE UPPER(d.dd_name)=? AND s.effective_pgm=(SELECT program_id FROM program WHERE id=?)
                              AND d.dsn_resolved IS NOT NULL AND s.job_id IS NOT NULL LIMIT 1""",
                         ((f["assign_dd"] or "").upper(), pid)).fetchone()
        dsn_by_sel[f["select_name"]] = d[0] if d else f"DD {f['assign_dd'] or '?'}"
    for o in conn.execute("SELECT target, target_kind, op, line FROM io_op WHERE program_id=? ORDER BY line", (pid,)):
        t, k, op = o["target"], o["target_kind"], o["op"].upper()
        if k == "file":
            key = ("file", dsn_by_sel.get(t, t))
            if op.startswith("OPEN INPUT") or op in ("READ", "START", "SORT READ", "RETURN"):
                mark(key, "R", o["line"])
            elif op.startswith("OPEN OUTPUT") or op in ("WRITE", "SORT WRITE", "RELEASE"):
                mark(key, "C", o["line"])
            elif op.startswith("OPEN EXTEND"):
                mark(key, "C", o["line"])
            elif op.startswith("OPEN I-O"):
                mark(key, "RU", o["line"])
            elif op == "REWRITE":
                mark(key, "U", o["line"])
            elif op == "DELETE":
                mark(key, "D", o["line"])
        elif k == "db2":
            key = ("db2", t)
            mark(key, {"SELECT": "R", "DECLARE": "R", "FETCH": "R", "OPEN": "R", "INSERT": "C", "UPDATE": "U",
                       "MERGE": "CU", "DELETE": "D", "CALL": "X"}.get(op, ""), o["line"])
        elif k == "cics":
            key = ("cics file", t)
            mark(key, {"READ": "R", "STARTBR": "R", "READNEXT": "R", "READPREV": "R", "WRITE": "C",
                       "REWRITE": "U", "DELETE": "D"}.get(op, ""), o["line"])
    for d in conn.execute("SELECT func, dbd_name, pcb_arg, line FROM dli_call WHERE program_id=?", (pid,)):
        key = ("ims", d["dbd_name"] or f"PCB {d['pcb_arg'] or '?'} (unresolved)")
        mark(key, {"GU": "R", "GHU": "R", "GN": "R", "GHN": "R", "GNP": "R", "GHNP": "R", "ISRT": "C",
                   "REPL": "U", "DLET": "D"}.get(d["func"] or "", ""), d["line"])
    return out


def cmd_crud(conn: sqlite3.Connection, programs: List[str], job: Optional[str] = None,
             copybook: Optional[str] = None, system: Optional[str] = None) -> str:
    """One matrix: programs x (datasets, DB2 tables, IMS databases, CICS
    files) with C/R/U/D and a cite per cell - the D1 design-document table."""
    names = [p.upper() for p in programs]
    if job:
        names += [r[0].upper() for r in conn.execute("""SELECT DISTINCT s.effective_pgm FROM step s JOIN job j ON j.id=s.job_id
                                                        WHERE UPPER(j.job_name)=? AND s.effective_pgm IS NOT NULL
                                                          AND s.effective_pgm NOT LIKE '*%'""", (job.upper(),))]
    if copybook:
        names += [r[0].upper() for r in conn.execute("""SELECT DISTINCT p.program_id FROM copy_use c
                                                        JOIN program p ON p.member_id=c.member_id WHERE UPPER(c.copybook)=?""",
                                                     (copybook.upper(),))]
    if system:
        names += [r[0].upper() for r in conn.execute("""SELECT p.program_id FROM program p JOIN member m ON m.id=p.member_id
                                                        WHERE UPPER(m.system)=?""", (system.upper(),))]
    names = sorted(set(names))
    out = [f"# CRUD matrix ({len(names)} programs)\n"]
    if not names:
        return out[0] + "\n_no programs selected_\n"
    rows = []
    for n in names:
        progs = programs_named(conn, n)
        if not progs:
            rows.append((n, "-", "-", "**not in index**", ""))
            continue
        for (kind, target), cells in sorted(_crud_of(conn, progs[0]["id"]).items()):
            letters = "".join(c for c in "CRUDX" if c in cells)
            rows.append((n, kind, target, letters, "; ".join(f"{c}@{cells[c]}" for c in letters)))
    out.append(table(["program", "kind", "file / table / database", "CRUD", "cites"], rows))
    out.append("\n> C = creates rows/records (INSERT, WRITE, OPEN OUTPUT/EXTEND, ISRT), R = reads, U = updates "
               "(UPDATE, REWRITE, REPL, OPEN I-O), D = deletes, X = stored-procedure call. A file shows its dataset "
               "name only when an indexed job runs the program; otherwise the DD name. IMS rows need the PSB "
               "(see `program`).\n")
    return "".join(out)


def cmd_conditions(conn: sqlite3.Connection, name: str) -> str:
    """The test-condition inventory of a program: every IF/WHEN/UNTIL with the
    field, the literal or 88 name it tests (88s expanded to their values),
    per paragraph, plus one out-of-domain negative per tested field."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    pid = p["id"]
    out = [f"# Test conditions for {p['program_id']}\n"]
    paras = conn.execute("SELECT name, start_line, end_line FROM paragraph WHERE program_id=? ORDER BY ordinal", (pid,)).fetchall()

    def para_of(line: int) -> str:
        for q in paras:
            if q["start_line"] <= line <= q["end_line"]:
                return q["name"]
        return "(before the first paragraph)"

    lits = conn.execute("""SELECT literal, context, field, line FROM literal_ref WHERE program_id=?
                           AND context IN ('compare','when') ORDER BY line""", (pid,)).fetchall()
    tests = conn.execute("""SELECT name, stmt, line FROM field_ref WHERE program_id=? AND mode='test' ORDER BY line""",
                         (pid,)).fetchall()
    c88 = {}
    for r in conn.execute("""SELECT c.name, c.values_lit, f.name AS parent FROM cond88 c JOIN field f ON f.id=c.field_id
                             WHERE f.member_id=? OR f.member_id IN (SELECT resolved_member_id FROM copy_use WHERE member_id=?)""",
                          (p["member_id"], p["member_id"])):
        c88[r["name"].upper()] = (r["parent"].upper(), " ".join(_jl(r["values_lit"])))
    rows = []
    by_field: Dict[str, set] = defaultdict(set)
    seen = set()
    for l in lits:
        key = (l["line"], l["field"], l["literal"])
        if key in seen:
            continue
        seen.add(key)
        fld = (l["field"] or "").split("/")[0]
        rows.append((para_of(l["line"]), l["context"].upper(), fld or "?", f"'{l['literal']}'", cite(conn, pid, l["line"])))
        if fld:
            by_field[fld].add(l["literal"])
    for t in tests:
        n = t["name"].upper()
        if n in c88:
            parent, vals = c88[n]
            rows.append((para_of(t["line"]), t["stmt"], parent, f"{n} = {vals}", cite(conn, pid, t["line"])))
            for v in vals.replace("'", "").split():
                if v not in ("THRU",):
                    by_field[parent].add(v)
    rows.sort(key=lambda r: r[4])
    out.append(table(["paragraph", "test", "field", "condition", "cite"], rows))
    other = conn.execute("SELECT COUNT(*) FROM literal_ref WHERE program_id=? AND context='when' AND literal='OTHER'", (pid,)).fetchone()[0]
    out.append(f"\n{len(rows)} conditions in {len(paras)} paragraphs; WHEN OTHER branches: {other}.\n")
    if by_field:
        out.append("\n### Negative cases (one value outside every tested value per field)\n")
        neg = []
        for fld, vals in sorted(by_field.items()):
            dom = sorted(vals)
            neg.append((fld, ", ".join(dom)[:80], "any value not listed (e.g. a blank / 'ZZ' / 99) - and one per boundary of a THRU"))
        out.append(table(["field", "values tested", "negative"], neg))
    out.append("\n> Conditions on fields tested only through group MOVEs, arithmetic or SQL predicates are not "
               "listed; `values <FIELD>` shows every value the estate assumes for a field.\n")
    return "".join(out)


def cmd_paragraph(conn: sqlite3.Connection, name: str, para: str) -> str:
    """One paragraph (or the paragraph holding a line number): who reaches
    it and how, what it performs/calls/does to which fields, with the source."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    pid = p["id"]
    if para.isdigit():
        row = conn.execute("""SELECT * FROM paragraph WHERE program_id=? AND ? BETWEEN start_line AND end_line
                              ORDER BY kind='section', start_line DESC LIMIT 1""", (pid, int(para))).fetchone()
    else:
        row = conn.execute("SELECT * FROM paragraph WHERE program_id=? AND UPPER(name)=?", (pid, para.upper())).fetchone()
    if not row:
        return f"# {p['program_id']} {para}\n\n**NOT FOUND** - no such paragraph/section in this program.\n"
    s, e = row["start_line"], row["end_line"]
    out = [f"# {p['program_id']} {row['name']} ({row['kind']}, expanded lines {s}-{e}, "
           f"{cite(conn, pid, s)} - {cite(conn, pid, e)})\n"]
    inbound = conn.execute("""SELECT from_para, kind, thru_para, line FROM perform_edge WHERE program_id=? AND
                              (UPPER(to_para)=? OR UPPER(thru_para)=? OR UPPER(to_para) LIKE ? OR UPPER(thru_para) LIKE ?)
                              ORDER BY line""",
                           (pid, row["name"].upper(), row["name"].upper(),
                            row["name"].upper() + " OF %", row["name"].upper() + " OF %")).fetchall()
    # a THRU range that covers this paragraph reaches it too
    names = [q["name"] for q in conn.execute("SELECT name FROM paragraph WHERE program_id=? AND kind='paragraph' ORDER BY ordinal", (pid,))]
    thru = []
    if row["name"] in names:
        i = names.index(row["name"])
        for r in conn.execute("SELECT from_para, to_para, thru_para, line FROM perform_edge WHERE program_id=? AND thru_para IS NOT NULL", (pid,)):
            if r["to_para"] in names and r["thru_para"] in names and names.index(r["to_para"]) < i < names.index(r["thru_para"]):
                thru.append(r)
    out.append("\n### Reached by\n")
    rows = [(r["from_para"] or "(top)", r["kind"] + (f" THRU {r['thru_para']}" if r["thru_para"] else ""), cite(conn, pid, r["line"]))
            for r in inbound] + [(r["from_para"], f"inside PERFORM {r['to_para']} THRU {r['thru_para']}", cite(conn, pid, r["line"])) for r in thru]
    out.append(table(["from", "how", "cite"], rows) if rows else "_never reached by PERFORM / GO TO / fall-through / THRU range_\n")
    out.append("\n### Does\n")
    perf = conn.execute("SELECT to_para, thru_para, kind, line FROM perform_edge WHERE program_id=? AND UPPER(from_para)=? ORDER BY line",
                        (pid, row["name"].upper())).fetchall()
    if perf:
        out.append("- control: " + "; ".join(f"{r['kind']} {r['to_para']}" + (f" THRU {r['thru_para']}" if r["thru_para"] else "")
                                             + f" @{cite(conn, pid, r['line'])}" for r in perf) + "\n")
    calls = conn.execute("SELECT kind, target, via_var, line FROM call_edge WHERE program_id=? AND line BETWEEN ? AND ?", (pid, s, e)).fetchall()
    if calls:
        out.append("- calls: " + "; ".join(f"{c['kind']} {c['target'] or c['via_var']} @{cite(conn, pid, c['line'])}" for c in calls) + "\n")
    for kind, sql in (("SQL", "SELECT stmt_type AS what, tables AS detail, start_line AS line FROM sql_stmt WHERE program_id=? AND start_line BETWEEN ? AND ?"),
                      ("DL/I", "SELECT func AS what, COALESCE(dbd_name, pcb_arg) AS detail, line FROM dli_call WHERE program_id=? AND line BETWEEN ? AND ?"),
                      ("CICS", "SELECT verb AS what, resource AS detail, line FROM cics_cmd WHERE program_id=? AND line BETWEEN ? AND ?"),
                      ("file", "SELECT op AS what, target AS detail, line FROM io_op WHERE program_id=? AND target_kind='file' AND line BETWEEN ? AND ?")):
        rs = conn.execute(sql, (pid, s, e)).fetchall()
        if rs:
            out.append(f"- {kind}: " + "; ".join(f"{r['what']} {r['detail']} @{cite(conn, pid, r['line'])}" for r in rs) + "\n")
    for mode in ("write", "test", "display"):
        rs = conn.execute("SELECT DISTINCT name FROM field_ref WHERE program_id=? AND mode=? AND line BETWEEN ? AND ? ORDER BY name",
                          (pid, mode, s, e)).fetchall()
        if rs:
            out.append(f"- fields {mode}: " + ", ".join(r["name"] for r in rs[:40]) + (" ..." if len(rs) > 40 else "") + "\n")
    lits = conn.execute("SELECT literal, context, field, line FROM literal_ref WHERE program_id=? AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)).fetchall()
    if lits:
        out.append("- literals: " + "; ".join(f"'{l['literal']}' {l['context']}" + (f" {l['field']}" if l["field"] else "") for l in lits[:30]) + "\n")
    out.append("\n### Source\n```\n")
    m1, l1, _d, _v = origin(conn, pid, s)
    m2, l2, _d2, _v2 = origin(conn, pid, e)
    # lines expanded from a member filed 'unknown' are not in the index: say so, not only print them empty - and offer
    # no cite for them
    unknown = [r[0] for r in conn.execute(
        "SELECT m.name FROM expand_run r JOIN member m ON m.id = r.src_member WHERE r.program_id = ? "
        "AND r.exp_start <= ? AND r.exp_end >= ? AND m.kind = 'unknown' ORDER BY r.exp_start", (pid, e, s))]
    if m1 == m2 and m1:
        for i in range(l1, l2 + 1):
            out.append(f"{i:6d} | {source_line(conn, m1, i)}\n")
        out.append("```\n" if m1.rpartition("/")[2].upper() in {n.upper() for n in unknown}
                   else f"```\n(cite as `[[{m1} line \"token\"]]`)\n")
    else:
        out.append("(the paragraph spans an expanded copybook - use `cite --program` with the expanded lines)\n```\n")
    out.append(unknown_note(conn, unknown))
    return "".join(out)


# --------------------------------------------------------------------------
# diff - two versions of a member: the production copy and the changed one
# --------------------------------------------------------------------------

_DIFF_KINDS = ("cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs", "sql", "unknown")


class _Loose(dict):
    """A file on disk that is not in the index (a member just downloaded
    into a scratch folder): its lines can be compared, its facts cannot."""

    def __getitem__(self, k):
        return dict.get(self, k)


def _member_by_ref(conn: sqlite3.Connection, ref: str) -> list:
    """The member(s) a reference names, production copy first:
    SYSTEM/NAME, NAME@LIBRARY, NAME(kind), NAME, or a path on disk."""
    import hashlib
    r = ref.strip().strip('"').strip("'")
    if not r:
        return []
    if os.path.isfile(r):
        p = os.path.normpath(os.path.abspath(r))
        rows = conn.execute("SELECT * FROM member WHERE UPPER(path)=UPPER(?)", (p,)).fetchall()
        if rows:
            return rows
        with open(p, "rb") as fh:
            data = fh.read()
        return [_Loose(id=None, path=p, name=os.path.splitext(os.path.basename(p))[0].upper(), kind=None,
                       library=os.path.basename(os.path.dirname(p)), system=None, authoritative=0,
                       fixed_format=None, sha256=hashlib.sha256(data).hexdigest(), norm_sha=None, lines=None)]
    m = re.match(r"^(?:([\w$#@.\-]+)/)?([^/\\()]+?)(?:\(([a-z]+)\))?(?:@([\w$#@.\-]+))?$", r)
    if not m:
        return []
    system, name, kind, library = m.group(1), m.group(2), m.group(3), m.group(4)
    rows = _members_named(conn, system, name, kind, library)
    if not rows and library:                      # a member really named AB@CD
        rows = _members_named(conn, system, f"{name}@{library}", kind, None)
    if not rows:                                  # a document named `PLAN .docx` is member `PLAN `
        rows = _members_named(conn, system, name, kind, library, trim=True)
    return rows


def _members_named(conn: sqlite3.Connection, system, name, kind, library, trim: bool = False) -> list:
    q = "SELECT * FROM member WHERE UPPER(TRIM(name))=?" if trim else "SELECT * FROM member WHERE UPPER(name)=?"
    args: list = [name.strip().upper() if trim else name.upper()]
    if system:
        q += " AND UPPER(COALESCE(system,''))=?"
        args.append(system.upper())
    if library:
        q += " AND UPPER(COALESCE(library,'')) LIKE ?"
        args.append("%" + library.upper() + "%")
    if kind:
        q += " AND kind=?"
        args.append(kind)
    else:
        q += " AND kind IN (%s)" % ",".join("?" * len(_DIFF_KINDS))
        args.extend(_DIFF_KINDS)
    return conn.execute(q + " ORDER BY authoritative DESC, system, path", args).fetchall()


def _ident(m) -> str:
    """How the gate names this copy: SYSTEM/NAME when the copy has a system
    (the folder under estate\\), else NAME@LIBRARY."""
    if m["system"]:
        return f"{m['system']}/{m['name']}"
    if m["library"]:
        return f"{m['name']}@{m['library']}"
    return m["name"]


def _pick_versions(rows: list, system: Optional[str] = None) -> Tuple[object, object, str]:
    """Among copies of one name: (old, new, why). Old is the declared
    production copy, else the copy whose library or system says PROD, else
    the first by path - and the report says which rule decided."""
    if system:
        news = [r for r in rows if (r["system"] or "").upper() == system.upper()]
        olds = [r for r in rows if (r["system"] or "").upper() != system.upper()]
        if news and olds:
            olds.sort(key=lambda r: (0 if r["authoritative"] else 1,
                                     0 if "PROD" in (r["library"] or "").upper() else 1, r["path"]))
            return olds[0], news[0], f"new = the copy in {system.upper()}"
    auth = [r for r in rows if r["authoritative"]]
    prod = [r for r in rows if "PROD" in ((r["library"] or "") + "/" + (r["system"] or "")).upper()]
    if len(auth) == 1:
        old, why = auth[0], "old = the copy declared production in manifest.json"
    elif not auth and len(prod) == 1:
        old, why = prod[0], "old = the copy whose library name says PROD"
    else:
        old, why = rows[0], ("old = the first copy by path - NEITHER is declared production; give both sides "
                             "(diff OLD NEW) to be sure")
    others = [r for r in rows if r is not old]
    new = next((r for r in others if r["norm_sha"] != old["norm_sha"]), others[0])
    return old, new, why


def _facts_of(conn: sqlite3.Connection, m) -> Optional[Dict[str, dict]]:
    """Comparable facts of one copy, by kind: name -> detail (fields carry
    (picture, offset, length) so a shifted field is told from a changed one).
    None for a file that is not in the index."""
    if m["id"] is None:
        return None
    out: Dict[str, dict] = {"paragraphs": {}, "calls": {}, "copybooks": {}, "tables": {}, "files": {},
                            "dli": {}, "cics": {}, "fields": {}}
    for c in conn.execute("SELECT copybook, replacing FROM copy_use WHERE member_id=?", (m["id"],)):
        out["copybooks"][c["copybook"].upper()] = "with REPLACING" if c["replacing"] else ""
    for f in conn.execute("SELECT name, pic, usage, offset, length, level, is_group FROM field WHERE member_id=? "
                          "ORDER BY id", (m["id"],)):
        pic = (f["pic"] or ("group" if f["is_group"] else "?")) + \
              (f" {f['usage']}" if f["usage"] and f["usage"] != "DISPLAY" else "")
        out["fields"][f"{f['level']:02d} {f['name']}"] = (pic, f["offset"], f["length"])
    prog = conn.execute("SELECT id FROM program WHERE member_id=?", (m["id"],)).fetchone()
    if prog:
        pid = prog["id"]
        for p in conn.execute("SELECT name, kind FROM paragraph WHERE program_id=?", (pid,)):
            out["paragraphs"][p["name"]] = p["kind"]
        for c in conn.execute("SELECT kind, target, via_var, resolved FROM call_edge WHERE program_id=?", (pid,)):
            key = c["target"] or ", ".join(_jl(c["resolved"])) or f"{c['via_var']} (unresolved)"
            out["calls"][f"{_CALL_LABEL.get(c['kind'], c['kind'])} {key}"] = ""
        tabs: Dict[str, set] = {}
        for s in conn.execute("SELECT stmt_type, tables FROM sql_stmt WHERE program_id=?", (pid,)):
            for t in _jl(s["tables"]):
                tabs.setdefault(str(t).upper(), set()).add(s["stmt_type"] or "?")
        out["tables"] = {t: "/".join(sorted(v)) for t, v in tabs.items()}
        for f in conn.execute("SELECT select_name, assign_dd FROM file_decl WHERE program_id=?", (pid,)):
            out["files"][f["select_name"]] = f"DD {f['assign_dd'] or '?'}"
        for d in conn.execute("SELECT func, dbd_name, procopt FROM dli_call WHERE program_id=?", (pid,)):
            out["dli"][f"{d['func']} {d['dbd_name'] or '?'}"] = f"PROCOPT={d['procopt']}" if d["procopt"] else ""
        for c in conn.execute("SELECT verb, resource_kind, resource FROM cics_cmd WHERE program_id=?", (pid,)):
            out["cics"][f"{c['verb']} {c['resource_kind'] or ''} {c['resource'] or ''}".strip()] = ""
    return out


def _para_spans(conn: sqlite3.Connection, m) -> List[Tuple[int, int, str]]:
    """(first source line, last source line, paragraph) for the paragraphs
    written in this member itself (a copybook's paragraphs are its own)."""
    if m["id"] is None:
        return []
    prog = conn.execute("SELECT id FROM program WHERE member_id=?", (m["id"],)).fetchone()
    if not prog:
        return []
    spans = []
    for p in conn.execute("SELECT name, start_line, end_line FROM paragraph WHERE program_id=? AND kind='paragraph'",
                          (prog["id"],)):
        m1, l1, d1, _v = origin(conn, prog["id"], p["start_line"])
        m2, l2, _d2, _v2 = origin(conn, prog["id"], p["end_line"])
        mine = {m["name"], f"{m['system']}/{m['name']}" if m["system"] else m["name"]}
        if m1 in mine and l1 is not None and not d1:
            spans.append((l1, l2 if (m2 in mine and l2 is not None) else l1, p["name"]))
    return spans


def _load_lines(m) -> List[str]:
    text, data, enc = reader.load(m["path"])
    return [r.rstrip("\r\n") for r in reader._split_records(text, data, enc)]


def _line_key(fixed: bool):
    # sequence numbers (1-6) and change stamps (73-80) are not changes
    return (lambda s: s[6:72].rstrip()) if fixed else (lambda s: s.rstrip())


def _fmt_field(v: tuple) -> str:
    pic, off, ln = v
    return f"{pic} @{off} len {ln}"


def cmd_diff(conn: sqlite3.Connection, old_ref: Optional[str] = None, new_ref: Optional[str] = None,
             context: int = 3, budget: Optional[int] = None, system: Optional[str] = None) -> str:
    """Two versions of a member: what changed in the facts the index tracks
    (paragraphs, calls, copybooks, tables, files, DL/I, CICS; for data, the
    fields whose picture or length changed and the fields that only shifted),
    then the changed lines of both sides with their own line numbers, each
    citable as [[SYSTEM/NAME line "token"]]. With no member: every member
    that exists in two libraries with different content (the release)."""
    import difflib
    if not old_ref:
        return cmd_diff_list(conn, system)
    olds = _member_by_ref(conn, old_ref)
    if not olds:
        return (f"# Diff\n\n**NOT FOUND**: {old_ref} - give SYSTEM/NAME (as `ambiguous` and `program` show it), "
                f"NAME@LIBRARY, or the path of the file\n")
    why = ""
    if new_ref:
        news = _member_by_ref(conn, new_ref)
        if not news:
            return f"# Diff\n\n**NOT FOUND**: new side {new_ref} - SYSTEM/NAME, NAME@LIBRARY, or a path on disk\n"
        old, new = olds[0], news[0]
    else:
        cands = [r for r in olds if r["id"] is not None]
        if len(cands) < 2:
            return (f"# Diff {old_ref}\n\n{len(olds)} copy of {old_ref} in the index - a diff needs two. Put the "
                    f"changed copy under estate\\<SYSTEM-TEST>\\<its library>\\, build again (only new members are "
                    f"parsed), then `diff {old_ref}`; or name the downloaded file: `diff {old_ref} C:\\path\\{old_ref}.cbl`\n")
        old, new, why = _pick_versions(cands, system)
    if old["id"] is not None and old["id"] == new["id"]:
        return f"# Diff\n\nboth references name the same copy ({old['path']})\n"

    a, b = _load_lines(old), _load_lines(new)
    fixed = bool(old["fixed_format"] if old["fixed_format"] is not None else new["fixed_format"]) \
        and bool(new["fixed_format"] if new["fixed_format"] is not None else old["fixed_format"])
    key = _line_key(fixed)
    sm = difflib.SequenceMatcher(None, [key(s) for s in a], [key(s) for s in b], autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    added = sum(j2 - j1 for _t, _i1, _i2, j1, j2 in ops)
    removed = sum(i2 - i1 for _t, i1, i2, _j1, _j2 in ops)
    oi, ni = _ident(old), _ident(new)
    loose = new["id"] is None or old["id"] is None

    def side(tag: str, m, n: int) -> str:
        return (f"- {tag}: `{os.path.basename(m['path'])}` in {m['library'] or '?'}"
                + (f", system {m['system']}" if m["system"] else "")
                + (" **[production]**" if m["authoritative"] else "")
                + (" (not in the index: lines only)" if m["id"] is None else "")
                + f", {n} lines, sha {(m['sha256'] or '')[:12]}\n")

    out = [f"# Diff {oi} -> {ni}\n", side("old", old, len(a)), side("new", new, len(b))]
    if why:
        out.append(f"- {why}\n")
    out.append(f"- {len(ops)} changed region(s): +{added} / -{removed} lines"
               + (" (columns 1-6 and 73-80 ignored)" if fixed else "") + "\n")
    if not ops:
        out.append("\n**identical**" + (" apart from sequence numbers / change stamps" if fixed else "") + "\n")
        return "".join(out)
    if old["system"] and new["system"]:
        out.append(f"- cite the old side as `[[{oi} line \"token\"]]`, the new side as `[[{ni} line \"token\"]]`\n")
    elif loose:
        out.append(f"- the copy that is not in the index cannot be cited; cite `[[{oi if old['id'] is not None else ni} "
                   f"line \"token\"]]` on the other side, or put the folder under estate\\ and build\n")
    else:
        out.append(f"- cite as `[[{oi} line \"token\"]]` / `[[{ni} line \"token\"]]` (NAME@LIBRARY: without a system "
                   f"per environment the plain [[NAME line]] form checks the production copy only)\n")

    # ---- where the items set on a changed line of NEW go: `flow --hops 2` over the indexed copy's facts
    # each changed line read as its whole statement: a MOVE's TO, a CALL's USING may sit on an unchanged line
    flow_targets = flow.changed_targets(b, fixed, [y for _t, _i1, _i2, j1, j2 in ops for y in range(j1, j2)])
    flow_section: List[str] = []
    walked = new if new["id"] is not None else old
    prow = conn.execute("SELECT id, program_id FROM program WHERE member_id=?", (walked["id"],)).fetchone() \
        if walked["id"] is not None and flow_targets else None
    if prow:
        flow_section.append(f"\n## Flow from the changed statements (`flow --hops 2` over the facts of the indexed "
                            f"copy {_ident(walked)})\n")
        for t in flow_targets:
            got = flow.lines_from(conn, prow["id"], t, hops=2)
            flow_section.append(f"\n**{prow['program_id']}.{t}**\n")
            flow_section.extend(ln + "\n" for ln in got)
            if not got:
                flow_section.append("- no hop in the index (not declared here, or nothing copies it on)\n")
        flow_section.append(f"\n{flow.FOOTER}\n")

    # ---- facts that changed
    fo, fn = _facts_of(conn, old), _facts_of(conn, new)
    out.append("\n## What changed (from the index)\n")
    if fo is None or fn is None:
        out.append("- the copy that is not in the index has no facts: lines only. Put it under estate\\ and build "
                   "for the paragraph / call / copybook / field comparison\n")
    else:
        labels = [("paragraphs", "Paragraphs"), ("calls", "Calls"), ("copybooks", "Copybooks"), ("tables", "DB2 tables"),
                  ("files", "Files"), ("dli", "DL/I"), ("cics", "CICS")]
        any_fact = False
        for k, label in labels:
            o, n = fo[k], fn[k]
            add = sorted(set(n) - set(o))
            rem = sorted(set(o) - set(n))
            chg = sorted(x for x in set(o) & set(n) if o[x] != n[x])
            if not (add or rem or chg):
                continue
            any_fact = True
            parts = []
            if add:
                parts.append("added " + ", ".join(f"`{x}`" + (f" ({n[x]})" if n[x] else "") for x in add))
            if rem:
                parts.append("removed " + ", ".join(f"`{x}`" + (f" ({o[x]})" if o[x] else "") for x in rem))
            if chg:
                parts.append("changed " + ", ".join(f"`{x}` {o[x] or '-'} -> {n[x] or '-'}" for x in chg))
            out.append(f"- **{label}**: " + "; ".join(parts) + "\n")
        o, n = fo["fields"], fn["fields"]
        add = sorted(set(n) - set(o), key=lambda x: n[x][1] if n[x][1] is not None else 1 << 30)
        rem = sorted(set(o) - set(n), key=lambda x: o[x][1] if o[x][1] is not None else 1 << 30)
        chg = [x for x in o if x in n and o[x] != n[x]]
        redef = [x for x in chg if o[x][0] != n[x][0] or o[x][2] != n[x][2]]    # picture or length
        moved = [x for x in chg if x not in redef]                              # only the offset
        if add or rem or redef or moved:
            any_fact = True
            parts = []
            if add:
                parts.append("added " + ", ".join(f"`{x}` {_fmt_field(n[x])}" for x in add))
            if rem:
                parts.append("removed " + ", ".join(f"`{x}` {_fmt_field(o[x])}" for x in rem))
            if redef:
                parts.append("changed " + ", ".join(f"`{x}` {_fmt_field(o[x])} -> {_fmt_field(n[x])}" for x in redef))
            if parts:
                out.append("- **Fields**: " + "; ".join(parts) + "\n")
            if moved:
                by_delta: Dict[int, List[str]] = {}
                for x in moved:
                    by_delta.setdefault((n[x][1] or 0) - (o[x][1] or 0), []).append(x)
                for delta, names in sorted(by_delta.items()):
                    names.sort(key=lambda x: o[x][1] or 0)
                    out.append(f"- **Fields shifted** by {delta:+d} byte(s), same picture: {len(names)} field(s), "
                               f"from `{names[0]}` to `{names[-1]}` - every program that reads this record by "
                               f"position, and every file written with the OLD layout, sees them move\n")
        # a pure insert marks no old line (the old line after the insertion point is not a change),
        # a pure delete marks no new line
        changed_old = {ln for _t, i1, i2, _j1, _j2 in ops for ln in range(i1 + 1, i2 + 1)}
        changed_new = {ln for _t, _i1, _i2, j1, j2 in ops for ln in range(j1 + 1, j2 + 1)}
        new_or_gone = set(fn["paragraphs"]) ^ set(fo["paragraphs"])          # already listed as added / removed
        touched = sorted(({nm for s, e, nm in _para_spans(conn, new) if any(s <= ln <= e for ln in changed_new)}
                          | {nm for s, e, nm in _para_spans(conn, old) if any(s <= ln <= e for ln in changed_old)})
                         - new_or_gone)
        if touched:
            any_fact = True
            out.append("- **Paragraphs with changed lines**: " + ", ".join(f"`{p}`" for p in touched)
                       + f" - `walk {ni.split('/')[-1].split('@')[0]} --from PARA` / `paragraph` for what each does now\n")
        if not any_fact:
            if flow_targets:
                out.append("- no declaration, call, copybook, file or table changed: the edit is inside statements - "
                           "where the items they set go is under **Flow from the changed statements**\n")
            else:
                out.append("- no fact the index tracks changed: the edit is inside statements (a condition, a "
                           "literal, a comment) - see the lines\n")
    out.extend(flow_section)

    # ---- the lines, both sides, their own numbers
    out.append(f"\n## Lines (`-` old {oi}, `+` new {ni}, two spaces = unchanged context)\n```\n")
    used = sum(len(x) for x in out) + 40
    shown = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        hunk: List[str] = []
        for x in range(max(0, i1 - context), i1):
            hunk.append(f"  {x + 1:6d} | {a[x]}\n")
        for x in range(i1, i2):
            hunk.append(f"- {x + 1:6d} | {a[x]}\n")
        for y in range(j1, j2):
            hunk.append(f"+ {y + 1:6d} | {b[y]}\n")
        for y in range(j2, min(len(b), j2 + context)):
            hunk.append(f"  {y + 1:6d} | {b[y]}\n")
        hunk.append("\n")
        text = "".join(hunk)
        if budget and used + len(text) > budget and shown:
            out.append(f"... budget {budget}: {len(ops) - shown} more changed region(s) not shown - "
                       f"`cite` the member for the rest\n")
            break
        out.append(text)
        used += len(text)
        shown += 1
    out.append("```\n")
    return "".join(out)


def release_pairs(conn: sqlite3.Connection, system: Optional[str] = None) -> Tuple[List[dict], List[dict], int]:
    """The release as data: (pairs, new_members, identical_pairs). A pair is
    {name, kind, old, new, added, removed, extra} with old / new as the
    gate names them (SYSTEM/NAME); new_members exist only in `system`."""
    import difflib
    system = (system or "").strip() or None
    names = conn.execute("SELECT name, kind FROM member WHERE kind IN (%s) GROUP BY name, kind "
                         "HAVING COUNT(*) > 1 AND COUNT(DISTINCT COALESCE(norm_sha, sha256)) > 1 ORDER BY kind, name"
                         % ",".join("?" * len(_DIFF_KINDS)), _DIFF_KINDS).fetchall()
    pairs: List[dict] = []
    same = 0
    for nm in names:
        copies = conn.execute("SELECT * FROM member WHERE name=? AND kind=? ORDER BY authoritative DESC, path",
                              (nm["name"], nm["kind"])).fetchall()
        if system and not any((c["system"] or "").upper() == system.upper() for c in copies):
            continue
        old, new, _why = _pick_versions(copies, system)
        if old["norm_sha"] == new["norm_sha"]:
            same += 1
            continue
        a, b = _load_lines(old), _load_lines(new)
        key = _line_key(bool(old["fixed_format"]) and bool(new["fixed_format"]))
        sm = difflib.SequenceMatcher(None, [key(s) for s in a], [key(s) for s in b], autojunk=False)
        ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
        pairs.append({"name": nm["name"], "kind": nm["kind"], "old": _ident(old), "new": _ident(new),
                      "added": sum(j2 - j1 for _t, _i1, _i2, j1, j2 in ops),
                      "removed": sum(i2 - i1 for _t, i1, i2, _j1, _j2 in ops), "extra": len(copies) - 2})
    fresh: List[dict] = []
    if system:
        fresh = [dict(name=f["name"], kind=f["kind"], library=f["library"]) for f in conn.execute(
            "SELECT m.name, m.kind, m.library FROM member m WHERE UPPER(COALESCE(m.system,''))=? "
            "AND m.kind IN (%s) AND NOT EXISTS (SELECT 1 FROM member o WHERE o.name=m.name AND o.kind=m.kind "
            "AND o.id<>m.id) ORDER BY m.kind, m.name" % ",".join("?" * len(_DIFF_KINDS)),
            (system.upper(), *_DIFF_KINDS)).fetchall()]
    return pairs, fresh, same


def cmd_diff_list(conn: sqlite3.Connection, system: Optional[str] = None) -> str:
    """Every member that exists in more than one library with different
    content - the contents of a release when one folder per environment is
    indexed (estate\\GC = production, estate\\GC-TEST = the changed copies)."""
    system = (system or "").strip() or None
    pairs, fresh, same = release_pairs(conn, system)
    title = "# Release contents - members whose copies differ" + (f" (changed copy in {system.upper()})" if system else "")
    out = [title + "\n\n"]
    if pairs:
        out.append("| member | kind | old (production) | new (changed) | lines | command |\n|---|---|---|---|---|---|\n")
        out.extend(f"| {p['name']} | {p['kind']} | {p['old']} | {p['new']} | +{p['added']} / -{p['removed']} | "
                   f"`diff {p['old']} {p['new']}`" + (f" (+{p['extra']} more copies)" if p["extra"] > 0 else "") + " |\n"
                   for p in pairs)
    else:
        out.append("_no member has two copies with different content_" + (f" involving {system.upper()}" if system else "") + "\n")
    if fresh:
        out.append(f"\n## New in {system.upper()} (no copy anywhere else)\n")
        out.extend(f"- {f['name']} ({f['kind']}) in {f['library']}\n" for f in fresh)
    out.append(f"\n- {len(pairs)} member(s) differ" + (f", {same} pair(s) identical apart from sequence numbers" if same else "")
               + "; `diff OLD NEW` for each - the facts that changed, then the lines of both sides\n")
    if not system:
        out.append("- `diff --system GC-TEST` when the changed copies are in one system folder: pairs each with its "
                   "production copy and lists members new to that system\n")
    return "".join(out)


# --------------------------------------------------------------------------
# walk - a program in READING order: the entry paragraph first, then every
# paragraph the first time control reaches it (PERFORM, GO TO, fall-through,
# THRU range, performed SECTION), each with its resolved facts and its
# source; the data it touches - and only that; the paragraphs nothing
# reaches. This is the read an analyst does by hand, so a model can do it
# from a few hundred lines instead of the whole member.
# --------------------------------------------------------------------------

_HOW_LABEL = {"perform": "PERFORM", "goto": "GO TO", "goto_depending": "GO TO DEPENDING", "alter": "ALTER GO TO",
              "sort_proc": "SORT PROCEDURE", "fallthrough": "falls through", "section": "in section",
              "range": "in THRU range", "entry": "ENTRY", "start": "entry"}
_CALL_LABEL = {"static": "CALL", "dynamic": "CALL", "cics_link": "LINK", "cics_xctl": "XCTL", "cics_start": "START",
               "cics_return": "RETURN TRANSID", "ims_switch": "CHNG ->", "proc_call": "SQL CALL"}
_NOTED_REACH = ("perform", "goto", "goto_depending", "alter", "sort_proc", "fallthrough")


def _walk_facts(conn: sqlite3.Connection, pid: int, s: int, e: int) -> str:
    """One line of RESOLVED facts for the statements on expanded lines s..e:
    calls with their targets, SQL verbs and tables, DL/I with the database
    and PROCOPT the PSB gave it, CICS resources, file operations, codes set."""
    bits: List[str] = []
    for c in conn.execute("SELECT kind, target, via_var, resolved, line FROM call_edge WHERE program_id=? "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        label = _CALL_LABEL.get(c["kind"], c["kind"].upper())
        if c["target"]:
            bits.append(f"{label} {c['target']}")
        else:
            bits.append(f"{label} {c['via_var']} -> {', '.join(_jl(c['resolved'])) or 'unresolved'}")
    for r in conn.execute("SELECT stmt_type, tables, cursor_name FROM sql_stmt WHERE program_id=? "
                          "AND start_line BETWEEN ? AND ? ORDER BY start_line", (pid, s, e)):
        bits.append(f"SQL {r['stmt_type']} " + (", ".join(_jl(r["tables"])) or (r["cursor_name"] or "")).strip())
    for r in conn.execute("SELECT func, dbd_name, procopt, pcb_arg, dest FROM dli_call WHERE program_id=? "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        if r["dest"]:
            bits.append(f"DL/I {r['func']} -> {r['dest']}")
        elif r["dbd_name"]:
            bits.append(f"DL/I {r['func']} {r['dbd_name']}" + (f" (PROCOPT={r['procopt']})" if r["procopt"] else ""))
        else:
            bits.append(f"DL/I {r['func']} (PCB {r['pcb_arg'] or '?'} unresolved)")
    for r in conn.execute("SELECT verb, resource_kind, resource FROM cics_cmd WHERE program_id=? "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        bits.append(f"CICS {r['verb']} {r['resource_kind'] or ''} {r['resource'] or ''}".rstrip())
    seen_io = set()
    for r in conn.execute("SELECT op, target, target_kind FROM io_op WHERE program_id=? AND target_kind IN ('file','mq') "
                          "AND line BETWEEN ? AND ? ORDER BY line", (pid, s, e)):
        key = (r["op"], r["target"])
        if key not in seen_io:
            seen_io.add(key)
            bits.append(f"{'MQ ' if r['target_kind'] == 'mq' else ''}{r['op']} {r['target']}")
    sets = conn.execute("SELECT literal, field FROM literal_ref WHERE program_id=? AND context='move_to' "
                        "AND line BETWEEN ? AND ? ORDER BY line LIMIT 7", (pid, s, e)).fetchall()
    if sets:
        bits.append("sets " + ", ".join(f"'{r['literal']}'->{r['field']}" for r in sets[:6]) + (" ..." if len(sets) > 6 else ""))
    return "; ".join(bits)


def _walk_block(conn: sqlite3.Connection, pid: int, pname: str, row: sqlite3.Row, how: str, depth: int,
                frm: Optional[str], via: Optional[int], first_para_line: Optional[int],
                source: bool, max_lines: int) -> Tuple[str, str]:
    """(full, summary) text for one paragraph / section header. The summary
    keeps the position, the reach and the facts; only the source goes."""
    s, e = row["start_line"], row["end_line"]
    if row["kind"] == "section" and first_para_line and first_para_line > s:
        e = first_para_line - 1               # the header and any statements before its first paragraph
    # the last paragraph's end_line may run past the expanded text, and a
    # paragraph's trailing blank lines say nothing: end at the last real line
    while e > s:
        m, ln, _d, _v = origin(conn, pid, e)
        if m and ln is not None and source_line(conn, m, ln):
            break
        e -= 1
    m1, l1, d1, v1 = origin(conn, pid, s)
    m2, l2, _d2, _v2 = origin(conn, pid, e)
    span = f"{m1}:{l1}-{l2}" if m1 == m2 else f"{m1}:{l1} .. {m2}:{l2}"
    if d1 and v1:
        span += f" (via COPY {v1})"
    ind = "  " * min(depth, 8)
    if how == "start":
        reach = "- entry"
    elif how == "entry":
        reach = "- ENTRY point"
    else:
        reach = f"<- {_HOW_LABEL.get(how, how)}" + (f" from {frm}" if frm else "") + (f" @{cite(conn, pid, via)}" if via else "")
    title = row["name"] + (" SECTION" if row["kind"] == "section" else "")
    head = f"#### {ind}{title}  {reach}  [depth {depth}]  {span}\n"
    facts = _walk_facts(conn, pid, s, e)
    fl = f"{ind}- facts: {facts}\n" if facts else ""
    first_txt = source_line(conn, m1, l1) if m1 and l1 is not None else ""
    if d1 and first_txt and not first_txt.upper().startswith(row["name"].upper()):
        fl += (f"{ind}- note: the copybook line reads `{first_txt}` - COPY REPLACING renames it to {row['name']} "
               f"in this program; cite the copybook's own text\n")
    summ = head + fl                          # the span in the header is the `cite` range when source is omitted
    if not source:
        return summ, summ
    body: List[str] = []
    cur: Optional[str] = None
    shown = 0
    for x in range(s, e + 1):
        m, ln, d, v = origin(conn, pid, x)
        if not m or ln is None:
            continue
        if m != cur:
            if cur is not None or d:
                body.append(f"------ {'from COPY ' + v + ' - ' if d and v else ''}cite as [[{m} line \"token\"]] ------\n")
            cur = m
        txt = source_line_raw(conn, m, ln)
        if not txt:
            continue                                    # a blank line: nothing to read, nothing to cite
        body.append(f"{ln:6d} | {txt}\n")
        shown += 1
        if shown >= max_lines and x < e:
            body.append(f"   ... {e - x} more lines: `cite {m} {ln + 1}-{l2}`\n")
            break
    full = head + fl + "```\n" + "".join(body) + "```\n"
    return full, summ


def _walk_data(conn: sqlite3.Connection, pid: int, mid: int, limit: int = 200) -> Tuple[str, int]:
    """The fields the PROCEDURE DIVISION names - and only those - grouped
    under their 01 with offset, length, PIC and how they are used. Program
    rows (WORKING-STORAGE, COPY ... REPLACING) cite through the line map;
    copybook rows cite the copybook. Returns (text, rows)."""
    refs = conn.execute("""SELECT UPPER(name) AS name, GROUP_CONCAT(DISTINCT mode) AS modes, MIN(line) AS first_line
                           FROM field_ref WHERE program_id=? GROUP BY UPPER(name) ORDER BY MIN(line)""", (pid,)).fetchall()
    # OPEN / READ / CLOSE name files, not fields
    files = {r[0].upper() for r in conn.execute("SELECT select_name FROM file_decl WHERE program_id=?", (pid,))}
    refs = [r for r in refs if r["name"] not in files]
    if not refs:
        return "", 0, []

    def root_of(fid: int) -> sqlite3.Row:
        r = conn.execute("SELECT * FROM field WHERE id=?", (fid,)).fetchone()
        while r and r["parent_id"]:
            r = conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
        return r

    def def_cite(f: sqlite3.Row, member_name: str) -> str:
        return cite(conn, pid, f["line"]) if f["member_id"] == mid else f"{member_name}:{f['line']}"

    groups: Dict[int, Tuple[sqlite3.Row, str, List[Tuple[sqlite3.Row, sqlite3.Row, Optional[str]]]]] = {}
    order: List[int] = []
    unknown: List[str] = []
    for ref in refs:
        defs = conn.execute("""SELECT f.*, CASE WHEN m.system IS NULL OR m.system='' THEN m.name ELSE m.system || '/' || m.name END AS member_name FROM field f JOIN member m ON m.id=f.member_id
                               WHERE f.member_id=? AND UPPER(f.name)=?""", (mid, ref["name"])).fetchall()
        if not defs:
            defs = conn.execute("""SELECT f.*, CASE WHEN m.system IS NULL OR m.system='' THEN m.name ELSE m.system || '/' || m.name END AS member_name FROM copy_use c
                                   JOIN field f ON f.member_id=c.resolved_member_id JOIN member m ON m.id=f.member_id
                                   WHERE c.member_id=? AND UPPER(f.name)=?""", (mid, ref["name"])).fetchall()
        c88: List[sqlite3.Row] = []
        if not defs:
            c88 = conn.execute("""SELECT c.name AS c88_name, c.values_lit, f.*, CASE WHEN m.system IS NULL OR m.system='' THEN m.name ELSE m.system || '/' || m.name END AS member_name FROM cond88 c
                                  JOIN field f ON f.id=c.field_id JOIN member m ON m.id=f.member_id
                                  WHERE UPPER(c.name)=? AND (f.member_id=? OR f.member_id IN
                                        (SELECT resolved_member_id FROM copy_use WHERE member_id=? AND resolved_member_id IS NOT NULL))""",
                               (ref["name"], mid, mid)).fetchall()
        if not defs and not c88:
            unknown.append(ref["name"])
            continue
        tagged = [(d, None) for d in defs[:3]] + \
                 [(c, f"88 {c['c88_name']} = {', '.join(_jl(c['values_lit'])) or '?'}") for c in c88[:3]]
        for d, tag in tagged:
            root = root_of(d["id"])
            if root is None:
                continue
            # a copybook FRAGMENT (05s whose 01 sits in the program) groups under the copybook itself
            gkey = -d["member_id"] if (root["level"] > 1 and d["member_id"] != mid) else root["id"]
            if gkey not in groups:
                groups[gkey] = (None if gkey < 0 else root, d["member_name"], [])
                order.append(gkey)
            groups[gkey][2].append((d, ref, tag))

    out = ["\n### Data it touches (only the fields the PROCEDURE DIVISION names; offsets and lengths are the parser's)\n"]
    total = 0
    heads: List[str] = []                     # one short label per group, for a budget-cut data section
    for rid in order:
        root, member_name, items = groups[rid]
        if root is None:
            n_all = conn.execute("SELECT COUNT(*) FROM field WHERE member_id=?", (-rid,)).fetchone()[0]
            out.append(f"\n**COPY {member_name}** (fragment - its 01 is in the program) - {len(items)} of {n_all} fields used\n")
            heads.append(f"COPY {member_name} {len(items)}/{n_all}")
        elif root["is_group"]:
            # the 01 itself named (CALL USING, MOVE, INITIALIZE) is said on its header, not counted as a field
            n_all = conn.execute("""WITH RECURSIVE sub(id) AS (SELECT id FROM field WHERE id=? UNION ALL
                                    SELECT f.id FROM field f JOIN sub ON f.parent_id=sub.id) SELECT COUNT(*)-1 FROM sub""",
                                 (rid,)).fetchone()[0]
            own = [(f, ref, tag) for f, ref, tag in items if f["id"] == rid and tag is None]
            items = [t for t in items if t not in own]
            used_as = ""
            if own:
                modes = "".join(k[0] for k in ("write", "read", "test", "display") if k in (own[0][1]["modes"] or ""))
                used_as = f", the group itself used as {modes} @{cite(conn, pid, own[0][1]['first_line'])}"
            out.append(f"\n**{root['level']:02d} {root['name']}** ({def_cite(root, member_name)}"
                       + (f", {root['length']} bytes" if root["length"] else "") + f"{used_as}) - {len(items)} of {n_all} fields used\n")
            heads.append(f"{root['level']:02d} {root['name']} {len(items)}/{n_all}")
        else:                                 # an elementary 01 / 77: the item is its own table
            out.append(f"\n**{root['level']:02d} {root['name']}** ({def_cite(root, member_name)}"
                       + (f", {root['length']} bytes" if root["length"] else "") + ")\n")
            heads.append(f"{root['level']:02d} {root['name']}")
        rows = []
        for f, ref, tag in sorted(items, key=lambda t: (t[0]["offset"] if t[0]["offset"] is not None else 10 ** 9, t[0]["line"] or 0)):
            modes = "".join(k[0] for k in ("write", "read", "test", "display") if k in (ref["modes"] or ""))
            pic = (f["pic"] or ("group" if f["is_group"] else "")) + (f" {f['usage']}" if f["usage"] and f["usage"] != "DISPLAY" else "")
            name = f"{f['level']:02d} {f['name']}" + (f" ({tag})" if tag else "")
            rows.append((name, pic, "" if f["offset"] is None else f["offset"], f["length"] or "",
                         modes, def_cite(f, member_name), cite(conn, pid, ref["first_line"])))
            total += 1
        out.append(table(["field", "PIC / usage", "offset", "len", "w/r/t/d", "defined", "first use"], rows))
        if total >= limit:
            out.append(f"_... capped at {limit} fields; `field NAME --program` for the rest_\n")
            break
    if unknown:
        out.append(f"\n- referenced but not defined in this program or its copybooks ({len(unknown)}): "
                   + ", ".join(unknown[:20]) + (" ..." if len(unknown) > 20 else "")
                   + " - LINKAGE items are here too when the caller's layout was not copied\n")
    return "".join(out), total, heads


def _walk_touches(conn: sqlite3.Connection, pid: int) -> str:
    out: List[str] = []
    files = conn.execute("SELECT select_name, assign_dd, organization FROM file_decl WHERE program_id=? ORDER BY line", (pid,)).fetchall()
    if files:
        parts = []
        for f in files:
            ops = [r[0] for r in conn.execute("SELECT DISTINCT op FROM io_op WHERE program_id=? AND target_kind='file' AND UPPER(target)=? ORDER BY op",
                                              (pid, f["select_name"].upper()))]
            parts.append(f"{f['select_name']} (DD {f['assign_dd'] or '?'}" + (f", {f['organization']}" if f["organization"] else "") + ")"
                         + (f" {'/'.join(ops)}" if ops else ""))
        out.append("- files: " + "; ".join(parts[:12]) + (" ..." if len(parts) > 12 else "") + "\n")
    tabs: Dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT stmt_type, tables FROM sql_stmt WHERE program_id=?", (pid,)):
        for t in _jl(r["tables"]):
            tabs[t].add(r["stmt_type"] or "?")
    if tabs:
        out.append("- DB2: " + "; ".join(f"{t} {'/'.join(sorted(v))}" for t, v in list(tabs.items())[:12]) + "\n")
    dbs: Dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT func, dbd_name, procopt, pcb_arg FROM dli_call WHERE program_id=?", (pid,)):
        key = (r["dbd_name"] + (f" (PROCOPT={r['procopt']})" if r["procopt"] else "")) if r["dbd_name"] else f"PCB {r['pcb_arg'] or '?'} unresolved"
        dbs[key].add(r["func"] or "?")
    if dbs:
        out.append("- IMS: " + "; ".join(f"{k} {'/'.join(sorted(v))}" for k, v in list(dbs.items())[:12]) + "\n")
    cics = conn.execute("SELECT DISTINCT verb, resource_kind, resource FROM cics_cmd WHERE program_id=? ORDER BY resource_kind, resource", (pid,)).fetchall()
    if cics:
        out.append("- CICS: " + "; ".join(f"{c['verb']} {c['resource_kind'] or ''} {c['resource'] or ''}".strip() for c in cics[:14])
                   + (" ..." if len(cics) > 14 else "") + "\n")
    calls = []
    for c in conn.execute("SELECT kind, target, via_var, resolved FROM call_edge WHERE program_id=? ORDER BY line", (pid,)):
        t = c["target"] or (", ".join(_jl(c["resolved"])) or f"{c['via_var']} (unresolved)")
        item = f"{_CALL_LABEL.get(c['kind'], c['kind'].upper())} {t}"
        if item not in calls:
            calls.append(item)
    if calls:
        out.append("- calls: " + "; ".join(calls[:14]) + (" ..." if len(calls) > 14 else "") + "\n")
    return "".join(out)


def cmd_walk(conn: sqlite3.Connection, name: str, start: Optional[str] = None, budget: Optional[int] = None,
             max_depth: Optional[int] = None, source: bool = True, data: bool = True, max_lines: int = 400) -> str:
    """The program in reading order. PERFORM returns at the end of its
    target (or THRU range), so the paragraph after a performed one is NOT
    reached by falling out of it; GO TO does not return; a performed
    SECTION runs its paragraphs in order; ENTRY points are extra roots;
    DECLARATIVES run on their USE condition and are listed, not walked."""
    progs = programs_named(conn, name)
    if not progs:
        return f"program {name} not found\n"
    p = progs[0]
    pid, mid, pname, mname = p["id"], p["member_id"], p["program_id"], p["member_name"]
    paras = conn.execute("SELECT id, name, section, kind, start_line, end_line, ordinal FROM paragraph "
                         "WHERE program_id=? ORDER BY start_line, kind='paragraph'", (pid,)).fetchall()
    if not paras:
        return (f"# Walk {pname}\n\nThe PROCEDURE DIVISION has no paragraphs or sections in the index (parse status: "
                f"{parse_label(conn, mid, p['parse_status'])}). Read it with `cite {mname} <first>-<last>`; "
                f"`program {pname}` lists its facts.\n")
    by_name: Dict[str, List[sqlite3.Row]] = defaultdict(list)   # a name can live in several sections
    for r in paras:
        by_name[r["name"].upper()].append(r)
    by_id: Dict[int, sqlite3.Row] = {r["id"]: r for r in paras}
    plain = [r for r in paras if r["kind"] == "paragraph"]
    ordinal = {r["id"]: i for i, r in enumerate(plain)}
    in_section: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in plain:
        if r["section"]:
            in_section[r["section"].upper()].append(r)

    def resolve(key: Optional[str], from_section: Optional[str] = None) -> Optional[sqlite3.Row]:
        """A target as the parser wrote it: NAME, or NAME OF SECTION. A bare
        name that exists in several sections prefers the caller's own
        section, then a paragraph over a section, then the first in source."""
        if not key:
            return None
        name, _sep, qual = key.upper().partition(" OF ")
        cands = by_name.get(name.strip(), [])
        if qual:
            cands = [c for c in cands if (c["section"] or "").upper() == qual.strip()]
        if not cands:
            return None
        if len(cands) > 1 and from_section:
            same = [c for c in cands if (c["section"] or "").upper() == from_section.upper()]
            cands = same or cands
        return sorted(cands, key=lambda c: (c["kind"] != "paragraph", c["start_line"]))[0]

    edges: Dict[str, List[sqlite3.Row]] = defaultdict(list)     # keyed by the parser's from_para name
    for e in conn.execute("SELECT from_para, to_para, thru_para, line, kind FROM perform_edge WHERE program_id=? "
                          "ORDER BY line, id", (pid,)):
        edges[(e["from_para"] or "").upper()].append(e)

    # DECLARATIVES sections run on a USE condition, never from the entry
    decl_lo = decl_hi = None
    spans = _spans(conn, mid)
    if spans:
        decl_rows = [r for lo, hi in spans for r in conn.execute(
            "SELECT line_no, text FROM src_fts WHERE rowid BETWEEN ? AND ? AND UPPER(text) LIKE '%DECLARATIVES%'", (lo, hi))]
        decl_rows.sort(key=lambda r: r["line_no"])
    else:
        decl_rows = conn.execute("SELECT line_no, text FROM src_fts WHERE member_name=? AND UPPER(text) LIKE '%DECLARATIVES%' "
                                 "ORDER BY line_no", (mname,)).fetchall()
    for r in decl_rows:
        t = r["text"].upper()
        if re.search(r"\bEND\s+DECLARATIVES\b", t):
            decl_hi = r["line_no"]
        elif re.search(r"\bDECLARATIVES\s*\.", t) and decl_lo is None:
            decl_lo = r["line_no"]

    def in_declaratives(row: sqlite3.Row) -> bool:
        if decl_lo is None or decl_hi is None:
            return False
        m, ln, depth, _v = origin(conn, pid, row["start_line"])
        return m == mname and not depth and ln is not None and decl_lo <= ln <= decl_hi

    if start:
        entry = resolve(start)
        if entry is None:
            return f"# Walk {pname}\n\n**NOT FOUND** - no paragraph or section named {start.upper()} in {pname}.\n"
    else:
        entry = next((r for r in paras if not in_declaratives(r)), paras[0])

    def thru_range(a: sqlite3.Row, b: sqlite3.Row) -> List[sqlite3.Row]:
        ia, ib = ordinal.get(a["id"]), ordinal.get(b["id"])
        if ia is not None and ib is not None and ia < ib:
            return plain[ia + 1: ib + 1]
        return []

    def first_para_line(sec: sqlite3.Row) -> Optional[int]:
        ps = in_section.get(sec["name"].upper(), [])
        return ps[0]["start_line"] if ps else None

    visited: Dict[int, int] = {}                          # paragraph id -> order shown
    blocks: List[Tuple[str, str, int]] = []               # (full, summary, depth)
    repeats: Dict[Tuple[str, str], int] = defaultdict(int)
    noted: Dict[int, int] = defaultdict(int)
    missing: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    # a frame: (target key, how, depth, from name, via line, stop paragraph id, caller's section, explicit row id)
    def children_of(row: sqlite3.Row, depth: int, stop: Optional[int], how: str) -> List[tuple]:
        out: List[tuple] = []
        key = row["name"].upper()
        from_sec = row["name"] if row["kind"] == "section" else row["section"]
        if row["kind"] == "section":
            secp = in_section.get(key, [])
            # a PERFORMed section returns after its last paragraph; a section
            # entered by the entry, a GO TO or fall-through keeps flowing
            last = secp[-1]["id"] if (secp and how in ("perform", "sort_proc", "range")) else stop
            out += [(q["name"].upper(), "section", depth + 1, row["name"], None, last, from_sec, q["id"]) for q in secp]
        for e in edges.get(key, []):
            kind, tgt = e["kind"], e["to_para"].upper()
            thru = (e["thru_para"] or "").upper() or None
            if kind == "fallthrough":
                if stop is not None and row["id"] == stop:
                    continue                              # the PERFORM returns here
                out.append((tgt, "fallthrough", depth, row["name"], e["line"], stop, from_sec, None))
            elif kind in ("perform", "sort_proc"):
                t_row = resolve(tgt, from_sec)
                th_row = resolve(thru, from_sec) if thru else None
                end = th_row or t_row
                out.append((tgt, kind, depth + 1, row["name"], e["line"], end["id"] if end else None, from_sec, None))
                if t_row and th_row:
                    out += [(q["name"].upper(), "range", depth + 1, row["name"], e["line"], end["id"], from_sec, q["id"])
                            for q in thru_range(t_row, th_row)]
            else:                                         # goto / goto_depending / alter: no return, same return point
                out.append((tgt, kind, depth + 1, row["name"], e["line"], stop, from_sec, None))
        return out

    def run(root: sqlite3.Row, how0: str) -> None:
        stack: List[tuple] = [(root["name"].upper(), how0, 0, None, None, None, None, root["id"])]
        while stack:
            key, how, depth, frm, via, stop, from_sec, rid = stack.pop()
            row = by_id.get(rid) if rid is not None else resolve(key, from_sec)
            if row is None:
                missing[(key, how)].append(f"{frm or '?'} @{cite(conn, pid, via)}" if via else (frm or "?"))
                continue
            if row["id"] in visited:
                if how in _NOTED_REACH:
                    repeats[(row["name"], how)] += 1
                    if noted[row["id"]] < 2:
                        noted[row["id"]] += 1
                        line = (f"{'  ' * min(depth, 8)}> {row['name']} <- {_HOW_LABEL[how]}" + (f" from {frm}" if frm else "")
                                + (f" @{cite(conn, pid, via)}" if via else "") + " - shown above\n")
                        blocks.append((line, line, depth))
                continue
            visited[row["id"]] = len(visited) + 1
            full, summ = _walk_block(conn, pid, pname, row, how, depth, frm, via,
                                     first_para_line(row) if row["kind"] == "section" else None, source, max_lines)
            blocks.append((full, summ, depth))
            for c in reversed(children_of(row, depth, stop, how)):
                stack.append(c)

    run(entry, "start")
    for a in conn.execute("SELECT alias, line FROM program_alias WHERE program_id=? ORDER BY line", (pid,)):
        r = conn.execute("""SELECT id FROM paragraph WHERE program_id=? AND kind='paragraph' AND ? BETWEEN start_line AND end_line
                            ORDER BY start_line DESC LIMIT 1""", (pid, a["line"])).fetchone()
        if r and r["id"] not in visited:
            note = f"\n### Entry point `{a['alias']}` (ENTRY @{cite(conn, pid, a['line'])}) - callers of that name start here\n"
            blocks.append((note, note, 0))
            run(by_id[r["id"]], "entry")

    # ---- assemble, then fit the budget by dropping SOURCE from the end (order and facts stay)
    # `DECLARATIVES.` itself parses like a paragraph header; it is a keyword, not code
    never = [r for r in paras if r["id"] not in visited and r["name"].upper() != "DECLARATIVES"
             and not (r["kind"] == "section" and any(q["id"] in visited for q in in_section.get(r["name"].upper(), [])))]
    secs = [r for r in paras if r["kind"] == "section"]
    names = _names_of(conn, pname)
    ph = ",".join("?" for _ in names)
    runs = conn.execute(f"""SELECT DISTINCT j.job_name || '.' || COALESCE(s.step_name, '?') AS r FROM step s
                            JOIN job j ON j.id=s.job_id WHERE UPPER(s.effective_pgm) IN ({ph}) ORDER BY 1 LIMIT 9""",
                        [n.upper() for n in names]).fetchall()
    trans = conn.execute(f"SELECT DISTINCT tran_code || ' (' || COALESCE(system, '?') || ')' AS t FROM transaction_def "
                         f"WHERE UPPER(program) IN ({ph}) ORDER BY 1 LIMIT 9", [n.upper() for n in names]).fetchall()
    head = [f"# Walk {pname}  (member {mname}, {len(plain)} paragraphs"
            + (f" in {len(secs)} section{'s' if len(secs) != 1 else ''}" if secs else "")
            + f"; entry {entry['name']}; {len(visited)} reached, {len(never)} not reached)\n"]
    if runs or trans:
        head.append("- runs in: " + ", ".join([r["r"] for r in runs] + [t["t"] for t in trans]) + "\n")
    else:
        head.append("- runs in: no job step or transaction of the index names it (callers: `callers " + pname + "`)\n")
    sysrow = conn.execute("SELECT system FROM member WHERE id=?", (mid,)).fetchone()
    tag = f"{sysrow[0]}/{mname}" if sysrow and sysrow[0] else mname
    head.append("- order: the entry first, then each paragraph the first time control reaches it - PERFORM (returns at the end "
                "of its target or THRU range), GO TO (no return), fall-through, THRU range, performed SECTION. `>` marks a "
                "reach of a paragraph already shown. [depth] is PERFORM nesting. Lines are ORIGINAL member lines: cite as "
                f"`[[{tag} line \"token\"]]`" + (" (this copy, not another system's)" if sysrow and sysrow[0] else "")
                + "; copybook lines carry their own tag.\n")
    touch = _walk_touches(conn, pid)
    if touch:
        head.append("\n### Touches\n" + touch)
    data_txt, data_rows, data_heads = _walk_data(conn, pid, mid) if data else ("", 0, [])
    tail: List[str] = []
    if repeats:
        tail.append("\n### Repeated reaches (already shown above)\n")
        tail.append(table(["paragraph", "how", "times"], [(k[0], _HOW_LABEL[k[1]], v) for k, v in sorted(repeats.items(), key=lambda kv: -kv[1])[:40]]))
    if never:
        tail.append(f"\n### Not reached from the entry ({len(never)})\n")
        rows = []
        for r in never:
            if in_declaratives(r):
                why = "DECLARATIVES - runs on its USE condition, not from the entry"
            elif r["kind"] == "section":
                why = "section: never PERFORMed and never entered"
            else:
                why = "no PERFORM, GO TO, fall-through, THRU range or performed SECTION reaches it"
            rows.append((r["name"], r["kind"], cite(conn, pid, r["start_line"]), why))
        tail.append(table(["paragraph", "kind", "starts", "why"], rows[:80]))
        if len(rows) > 80:
            tail.append(f"_... {len(rows) - 80} more_\n")
    if missing:
        tail.append("\n### PERFORM / GO TO targets not found in this program\n")
        tail.append(table(["target", "how", "from"], [(k[0], _HOW_LABEL.get(k[1], k[1]), ", ".join(v[:5])) for k, v in missing.items()]))
    tail.append(unresolved_for(conn, [mid]))

    chosen: List[str] = []
    for full, summ, d in blocks:
        chosen.append(summ if (max_depth is not None and d > max_depth) else full)
    ih = index_header(conn)
    # the leading comment (token count, budget note, index header) is part of the budget too
    fixed = sum(len(x) for x in head) + sum(len(x) for x in tail) + len("\n## Walk\n\n") + len(ih) + 260
    total = fixed + len(data_txt) + sum(len(c) for c in chosen)
    omitted = 0
    note = ""
    if budget and total > budget:
        for i in range(len(blocks) - 1, -1, -1):
            if total <= budget:
                break
            full, summ, _d = blocks[i]
            if chosen[i] is full and full is not summ:
                total -= len(full) - len(summ)
                chosen[i] = summ
                omitted += 1
        note = (f", budget {budget}: source omitted for {omitted} of {len(visited)} paragraphs (from the end; the first "
                f"reached keep theirs; each header's span is the `cite` range)")
        if total > budget and data_txt:
            # sources gone and still over: keep the data section's shape, drop its tables
            keep = (f"\n### Data it touches - cut for budget ({data_rows} fields named; `field NAME --program {pname}` "
                    f"for offsets): " + "; ".join(data_heads) + "\n")
            total -= len(data_txt) - len(keep)
            data_txt = keep
            note += "; data tables cut"
        if total > budget:
            note += f"; still ~{total - budget} chars over - the skeleton alone exceeds the budget"
    text = "".join(head) + data_txt + "\n## Walk\n\n" + "".join(chosen) + "".join(tail)
    est = len(text) // 4
    return f"<!-- walk {pname}: ~{est} tokens ({len(text)} chars){note}; {index_header(conn)} -->\n" + text


def _dead_extras(conn: sqlite3.Connection) -> str:
    """Decommissioning candidates beyond programs: datasets written but never
    read (or read but never written) IN THE INDEX, copybooks nobody COPYs,
    jobs absent from the scheduler when one is loaded."""
    out = []
    rows = conn.execute("""SELECT dsn,
                             SUM(CASE WHEN mode IN ('output','mod','both','create') THEN 1 ELSE 0 END) AS w,
                             SUM(CASE WHEN mode IN ('input','both') THEN 1 ELSE 0 END) AS r
                           FROM v_dataset_flow WHERE job_name IS NOT NULL AND is_temp=0 AND dsn NOT LIKE '&&%'
                           GROUP BY dsn""").fetchall()
    wnr = [r["dsn"] for r in rows if r["w"] and not r["r"]]
    rnw = [r["dsn"] for r in rows if r["r"] and not r["w"]]
    out.append(f"\n### Datasets written but never read in the index ({len(wnr)})\n"
               "Caveat: online CICS readers, unindexed departments, FTP/NDM and reports are not counted.\n")
    out.append(table(["dataset"], [(d,) for d in wnr[:100]]))
    out.append(f"\n### Datasets read but never written in the index ({len(rnw)}) - they come from somewhere not indexed\n")
    out.append(table(["dataset"], [(d,) for d in rnw[:100]]))
    cps = conn.execute("""SELECT m.name, m.path FROM member m WHERE m.kind='copybook'
                          AND NOT EXISTS (SELECT 1 FROM copy_use c WHERE UPPER(c.copybook)=UPPER(m.name))
                          ORDER BY m.name""").fetchall()
    out.append(f"\n### Copybooks no indexed program COPYs ({len(cps)})\n")
    out.append(table(["copybook", "path"], [(c["name"], os.path.basename(c["path"])) for c in cps[:100]]))
    if conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]:
        jobs = conn.execute("""SELECT j.job_name FROM job j WHERE NOT EXISTS
                               (SELECT 1 FROM sched_job s WHERE UPPER(s.job_name)=UPPER(j.job_name))
                               AND NOT EXISTS (SELECT 1 FROM sched_dep d WHERE UPPER(d.job_name)=UPPER(j.job_name))
                               ORDER BY j.job_name""").fetchall()
        out.append(f"\n### Jobs not in the scheduler export ({len(jobs)}) - on-request, obsolete, or a different scheduler\n")
        out.append(table(["job"], [(j["job_name"],) for j in jobs[:100]]))
    return "".join(out)


def _coverage_extras(conn: sqlite3.Connection) -> str:
    """What contributes FACTS and what is only searchable text - stated, so
    that 'ctlcard skipped 3100' reads as the gap it is."""
    from . import build as _build          # local import: build imports nothing from query
    out = ["\n### What contributes facts\n"]
    kinds = conn.execute("SELECT kind, COUNT(*) AS n FROM member GROUP BY kind ORDER BY kind").fetchall()
    lost = {"ctlcard": "cards are read only when a job's DD names the member",
            "asm": "no program rows, calls or DSECT layouts", "rexx": "no calls / SUBMITs / ALLOCs",
            "sql": "DDL not parsed (columns, views, triggers unknown)", "unknown": "nothing - misfiled library?",
            "doc": "prose only, never facts", "listing": "not parsed (offsets unknown)", "other": "nothing",
            "empty": "no code lines",
            "stub": "nothing: only numbers, never expanded - a program copying one was compiled against another copy"}
    out.append(table(["kind", "members", "handler", "what is lost without one"],
                     [(k["kind"], k["n"], "yes" if k["kind"] in _build.HANDLERS else "NO",
                       "" if k["kind"] in _build.HANDLERS else lost.get(k["kind"], "not parsed")) for k in kinds]))
    unk = conn.execute("""SELECT library, ext, COUNT(*) AS n FROM member WHERE kind IN ('unknown','other')
                          GROUP BY library, ext ORDER BY n DESC LIMIT 20""").fetchall()
    if unk:
        out.append("\n### Unrecognised members by folder and extension (misfiled libraries show up here)\n")
        out.append(table(["folder", "ext", "members"], [(u["library"], u["ext"] or "(none)", u["n"]) for u in unk]))
    checks = [
        ("scheduler export", conn.execute("SELECT COUNT(*) FROM sched_job").fetchone()[0]),
        ("CICS CSD", conn.execute("SELECT COUNT(*) FROM transaction_def WHERE system='cics'").fetchone()[0]),
        ("IMS stage-1", conn.execute("SELECT COUNT(*) FROM transaction_def WHERE system='ims_dc'").fetchone()[0]),
        ("PSB source", conn.execute("SELECT COUNT(*) FROM ims_psb").fetchone()[0]),
        ("DBD source", conn.execute("SELECT COUNT(*) FROM ims_dbd").fetchone()[0]),
        ("DCLGEN / DDL columns", conn.execute("SELECT COUNT(*) FROM db2_column").fetchone()[0]),
        ("BMS / MFS screens", conn.execute("SELECT COUNT(*) FROM screen").fetchone()[0]),
        ("control-card members", conn.execute("SELECT COUNT(*) FROM member WHERE kind='ctlcard'").fetchone()[0]),
        ("documents", conn.execute("SELECT COUNT(*) FROM member WHERE kind='doc'").fetchone()[0]),
    ]
    out.append("\n### Optional inputs\n")
    out.append(table(["input", "loaded"], [(n, str(v) if v else "NO") for n, v in checks]))
    miss = conn.execute("""SELECT card_member, COUNT(*) AS n FROM dd WHERE card_member IS NOT NULL AND sysin_text IS NULL
                           GROUP BY card_member ORDER BY n DESC LIMIT 30""").fetchall()
    if miss:
        out.append(f"\n### Control-card members referenced by JCL but NOT indexed ({len(miss)} shown)\n"
                   "Fetch the PARMLIB/CNTL library: until then these steps' cards, programs and sort fields are unknown.\n")
        out.append(table(["member", "DDs"], [(m["card_member"], m["n"]) for m in miss]))
    unres = conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='dli_function'").fetchone()[0]
    return "".join(out)


# --------------------------------------------------------------------------
# interfaces - the mainframe / non-mainframe boundary
# --------------------------------------------------------------------------

def _interface_rows(conn: sqlite3.Connection, system: Optional[str] = None,
                    dsn: Optional[str] = None) -> List[Tuple]:
    """(kind, direction, peer, what, where, system, cite) from every source:
    FTP/NDM/BPXBATCH steps, MQ calls, IMS message switches, CICS TD queues,
    URIMAP web entry points, REMOTESYSTEM transactions, and the manifest's
    declared external_interfaces."""
    rows: List[Tuple] = []
    q = """SELECT i.kind, i.detail, i.direction, i.line, m.name AS mem, m.kind AS mkind, m.system,
                  (SELECT program_id FROM program p WHERE p.member_id=m.id LIMIT 1) AS pgm,
                  (SELECT job_name FROM job j WHERE j.member_id=m.id LIMIT 1) AS job
           FROM interface_edge i JOIN member m ON m.id=i.member_id"""
    for r in conn.execute(q):
        if system and (r["system"] or "").upper() != system.upper():
            continue
        where = r["pgm"] or r["job"] or r["mem"]
        rows.append((r["kind"], r["direction"] or "?", "", r["detail"][:90], where, r["system"] or "?",
                     f"{r['mem']}:{r['line']}"))
    for r in conn.execute("""SELECT d.dd_name, d.dsn_resolved, d.mode, d.mode_source, d.line, s.step_name, s.from_proc,
                                    j.job_name, m.name AS mem, m.system
                             FROM dd d JOIN step s ON s.id=d.step_id JOIN job j ON j.id=s.job_id JOIN member m ON m.id=j.member_id
                             WHERE d.dd_name IN ('*FTP*','*NDM*') OR d.mode_source IN ('pathopts')"""):
        if system and (r["system"] or "").upper() != system.upper():
            continue
        if dsn and dsn.upper() not in (r["dsn_resolved"] or "").upper():
            continue
        kind = {"*FTP*": "ftp", "*NDM*": "ndm"}.get(r["dd_name"], "uss")
        direction = "out" if r["mode"] == "input" and kind != "uss" else "in" if r["mode"] == "output" and kind != "uss" else r["mode"]
        rows.append((kind, direction, "", r["dsn_resolved"], f"{r['job_name']} {r['step_name']}", r["system"] or "?",
                     f"{r['from_proc'] or r['mem']}:{r['line']}"))
    for r in conn.execute("SELECT tran_code, program, detail, member_id, line FROM transaction_def WHERE system='cics_web' "
                          "OR detail LIKE 'REMOTESYSTEM%'"):
        rows.append(("web" if r["detail"] and r["detail"].startswith("URIMAP") else "remote-region", "in", "",
                     f"{r['tran_code']} -> {r['program'] or '?'} ({(r['detail'] or '')[:40]})", r["program"] or "", "?",
                     f"CSD:{r['line']}"))
    for r in conn.execute("SELECT kind, peer, direction, target_kind, target, note FROM external_interface"):
        if dsn and r["target_kind"] == "dataset" and dsn.upper() not in r["target"].upper():
            continue
        rows.append((r["kind"], r["direction"] or "?", r["peer"] or "?", f"{r['target_kind']} {r['target']}",
                     "(declared in the manifest)", "-", r["note"] or "manifest"))
    if dsn:
        rows = [x for x in rows if dsn.upper() in x[3].upper() or dsn.upper() in x[4].upper()]
    return rows


def cmd_interfaces(conn: sqlite3.Connection, system: Optional[str] = None, dsn: Optional[str] = None) -> str:
    """What leaves and enters the mainframe: files sent by FTP / Connect:Direct,
    MQ queues with their message layout, IMS message switches, CICS TD queues
    and web entry points, plus the peers declared in the manifest."""
    rows = _interface_rows(conn, system, dsn)
    out = [f"# Interfaces" + (f" of {system.upper()}" if system else "") + (f" touching {dsn.upper()}" if dsn else "") + "\n"]
    if not rows:
        out.append("\n_none found_ - no FTP/NDM/MQ/TDQ/web facts in scope and no `external_interfaces` in the manifest.\n")
    else:
        out.append(table(["kind", "dir", "peer", "what", "where", "system", "cite"], sorted(rows)))
    out.append("\n> A peer system that no source names (the reinsurer, the warehouse, the portal) comes only from the "
               "manifest's `external_interfaces`; MQ queue names come from the MQOD literal; a `(queue not "
               "resolvable)` row means the name is set at run time. Message layouts: `layout <01-NAME>`.\n")
    return "".join(out)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Query atlas.db (markdown out, citations in).")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--out", help="write the report to this file (UTF-8, LF, folders created) instead of "
                                  "the terminal - use this rather than the shell's '>' (PowerShell 5.1 "
                                  "writes UTF-16)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("program", "job", "dataset", "copybook", "values", "screen", "transaction", "column",
              "table", "dbd"):
        sub.add_parser(c).add_argument("name")
    s = sub.add_parser("segment", help="who touches an IMS segment, and each DBD field at its bytes in every "
                                       "program's I/O area (whatever that program calls it)")
    s.add_argument("name")
    s.add_argument("--dbd", help="only this DBD's segment when several DBDs use the segment name")
    s = sub.add_parser("field")
    s.add_argument("name")
    s.add_argument("--all", action="store_true", help="every reference site (default: one cite per program/statement)")
    s.add_argument("--program", help="only this program's references")
    s = sub.add_parser("flow", help="where a field's VALUE goes: file bytes to the reader, CALL USING positions, "
                                    "DB2 columns, CICS carriers, local copies - every stop labelled")
    s.add_argument("name")
    s.add_argument("--program", help="start in this program (default: one tree per declaring program)")
    s.add_argument("--up", action="store_true", help="upstream: where the value comes from")
    s.add_argument("--hops", type=int, default=3, help="hops to follow (a dataset with its pass-through steps is one)")
    s.add_argument("--width", type=int, default=12, help="branches per node before the collapse line")
    s.add_argument("--nodes", type=int, default=200, help="nodes in total")
    s.add_argument("--derived", action="store_true", help="also follow COMPUTE/STRING/UNSTRING/FUNCTION/INSPECT")
    s.add_argument("--all", action="store_true", help="no width cap")
    s.add_argument("--budget", type=int, help="character budget for the tree: the deepest hops are cut first")
    s = sub.add_parser("layout")
    s.add_argument("name")
    s.add_argument("--program", help="the 01 as this program sees it (REPLACING applied)")
    s = sub.add_parser("crud")
    s.add_argument("programs", nargs="*")
    s.add_argument("--job", help="every program the job runs")
    s.add_argument("--copybook", help="every program including the copybook")
    s.add_argument("--system", help="every program of a department")
    sub.add_parser("conditions").add_argument("name")
    s = sub.add_parser("interfaces")
    s.add_argument("--system", help="one department")
    s.add_argument("--dsn", help="interfaces touching this dataset")
    s = sub.add_parser("paragraph")
    s.add_argument("name")
    s.add_argument("para", help="paragraph / section name, or a line number")
    s = sub.add_parser("doc", help="a document's outline, or the full text of its sections by number / by content; "
                                   "--list shows the indexed documents")
    s.add_argument("name", nargs="?", help="document name (file name without extension)")
    s.add_argument("--list", nargs="?", const="", metavar="PATTERN",
                   help="list the indexed documents (optionally only paths containing PATTERN, e.g. a folder name)")
    s.add_argument("--sections", help="which sections to print in full: 3, 3-5, or 2,7,9-12")
    s.add_argument("--grep", help="print every section containing this text (case-insensitive)")
    s.add_argument("--budget", type=int, help="character budget for the printed sections")
    s = sub.add_parser("diff", help="two versions of a member (production vs changed): changed facts, then the lines "
                                    "of both sides; with no member, every member whose copies differ (the release)")
    s.add_argument("old", nargs="?", help="SYSTEM/NAME, NAME@LIBRARY, NAME(kind), NAME, or a path - the production copy")
    s.add_argument("new", nargs="?", help="the changed copy; omitted = the other copy of the same name in the index")
    s.add_argument("--system", help="the system folder holding the changed copies (e.g. GC-TEST)")
    s.add_argument("--context", type=int, default=3, help="unchanged lines shown around each change")
    s.add_argument("--budget", type=int, help="character budget for the lines")
    s = sub.add_parser("walk", help="the program in reading order: entry first, each paragraph as control reaches it")
    s.add_argument("name")
    s.add_argument("--from", dest="start", help="start at this paragraph / section instead of the entry")
    s.add_argument("--budget", type=int, help="character budget (about 4 per token): later paragraphs keep their "
                                              "position and facts, lose their source")
    s.add_argument("--depth", type=int, help="print source only down to this PERFORM depth; deeper ones are summarised")
    s.add_argument("--no-source", action="store_true", help="skeleton only: order, reach, facts")
    s.add_argument("--no-data", action="store_true", help="skip the data section")
    s.add_argument("--max-lines", type=int, default=400, help="source lines per paragraph before truncation")
    s = sub.add_parser("literal")
    s.add_argument("name")
    s.add_argument("--field", help="only uses on this field (common values like 3 or 'M' appear everywhere)")
    s.add_argument("--like", action="store_true", help="substring match instead of exact")
    s = sub.add_parser("pair")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--window", type=int, default=8)
    sub.add_parser("messages").add_argument("pattern")
    sub.add_parser("docs").add_argument("term")
    sub.add_parser("images").add_argument("name", nargs="?")
    for c in ("callers", "callees"):
        s = sub.add_parser(c)
        s.add_argument("name")
        s.add_argument("--depth", type=int, default=2)
        s.add_argument("--args", action="store_true", help="every call site with its USING list vs the callee's LINKAGE")
    sub.add_parser("dead")
    sub.add_parser("ambiguous")
    s = sub.add_parser("coverage")
    s.add_argument("--all", action="store_true", help="name every partial and failed member, not the first 15")
    s = sub.add_parser("search")
    s.add_argument("term")
    s.add_argument("--kind")
    s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("cite")
    s.add_argument("member")
    s.add_argument("range", help="e.g. 120 or 120-135")
    s.add_argument("--program", help="treat lines as EXPANDED lines of this program and translate")
    s.add_argument("--kind", help="cobol|copybook|jcl|proc|psb|... when several members share the name")
    s = sub.add_parser("pack")
    s.add_argument("name")
    s.add_argument("--max-lines", type=int, default=120)
    s.add_argument("--kind", help="program|job|copybook|transaction|field (default: detected)")
    s.add_argument("--budget", type=int, help="maximum characters (about 4 per token)")
    s.add_argument("--sections", help="comma-separated section names to keep, e.g. runs,calls,files,evidence")
    s.add_argument("--keep-paths", action="store_true", help="keep full file paths (default: file names only)")
    a = ap.parse_args(argv)
    if a.out:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _run(a)
        write_out(a.out, buf.getvalue())
        return rc
    return _run(a)


def write_out(path: str, text: str) -> None:
    """Write a report as UTF-8 with LF, creating the folder. The shell's own
    redirection is not used because its encoding is the shell's choice
    (PowerShell 5.1 `>` produces UTF-16), which the chat model and the gate
    then misread."""
    # a folder typed with a trailing space is created without it by Windows, and the file inside it
    # then 'does not exist': strip it from every part of the path
    path = os.sep.join(c.rstrip(" ") for c in path.replace("/", os.sep).split(os.sep))
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"written {path}  ({len(text)} chars, ~{len(text) // 4} tokens)")


def _run(a: argparse.Namespace) -> int:
    conn = connect(a.db)
    try:
        if a.cmd == "program":
            print(cmd_program(conn, a.name))
        elif a.cmd == "job":
            print(cmd_job(conn, a.name))
        elif a.cmd in ("callers", "callees"):
            print(cmd_graph(conn, a.name, a.cmd, a.depth, a.args))
        elif a.cmd == "field":
            print(cmd_field(conn, a.name, a.all, a.program))
        elif a.cmd == "flow":
            print(cmd_flow(conn, a.name, a.program, a.up, a.hops, a.width, a.nodes, a.derived, a.all, a.budget))
        elif a.cmd == "crud":
            print(cmd_crud(conn, a.programs, a.job, a.copybook, a.system))
        elif a.cmd == "conditions":
            print(cmd_conditions(conn, a.name))
        elif a.cmd == "interfaces":
            print(cmd_interfaces(conn, a.system, a.dsn))
        elif a.cmd == "paragraph":
            print(cmd_paragraph(conn, a.name, a.para))
        elif a.cmd == "walk":
            print(cmd_walk(conn, a.name, a.start, a.budget, a.depth, not a.no_source, not a.no_data, a.max_lines))
        elif a.cmd == "doc":
            if a.list is not None or not a.name:
                print(cmd_doc_list(conn, a.list if a.list is not None else a.name))
            else:
                print(cmd_doc(conn, a.name, a.sections, a.grep, a.budget))
        elif a.cmd == "diff":
            print(cmd_diff(conn, a.old, a.new, a.context, a.budget, a.system))
        elif a.cmd == "literal":
            print(cmd_literal(conn, a.name, a.field, a.like))
        elif a.cmd == "values":
            print(cmd_values(conn, a.name))
        elif a.cmd == "pair":
            print(cmd_pair(conn, a.a, a.b, a.window))
        elif a.cmd == "messages":
            print(cmd_messages(conn, a.pattern))
        elif a.cmd == "screen":
            print(cmd_screen(conn, a.name))
        elif a.cmd == "transaction":
            print(cmd_transaction(conn, a.name))
        elif a.cmd == "column":
            print(cmd_column(conn, a.name))
        elif a.cmd == "table":
            print(cmd_table(conn, a.name))
        elif a.cmd == "dbd":
            print(cmd_dbd(conn, a.name))
        elif a.cmd == "segment":
            print(cmd_segment(conn, a.name, a.dbd))
        elif a.cmd == "layout":
            print(cmd_layout(conn, a.name, a.program))
        elif a.cmd == "docs":
            print(cmd_docs(conn, a.term))
        elif a.cmd == "images":
            print(cmd_images(conn, a.name))
        elif a.cmd == "dataset":
            print(cmd_dataset(conn, a.name))
        elif a.cmd == "copybook":
            print(cmd_copybook(conn, a.name))
        elif a.cmd == "dead":
            print(cmd_dead(conn))
        elif a.cmd == "ambiguous":
            print(cmd_ambiguous(conn))
        elif a.cmd == "coverage":
            print(cmd_coverage(conn, a.all))
        elif a.cmd == "search":
            print(cmd_search(conn, a.term, a.kind, a.limit))
        elif a.cmd == "cite":
            print(cmd_cite(conn, a.member, a.range, a.program, a.kind))
        elif a.cmd == "pack":
            print(cmd_pack(conn, a.name, a.max_lines, a.kind, a.budget, a.sections, a.keep_paths))
    finally:
        conn.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Any crash becomes a short, redacted atlas-crash.txt you can share."""
    try:
        return _main(argv)
    except Exception as e:                                              # noqa: BLE001
        from .diag import write_crash
        text = write_crash(e, sys.argv)
        print("\n" + text, file=sys.stderr)
        print("\nSaved to atlas-crash.txt - paste it to get a fix.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
