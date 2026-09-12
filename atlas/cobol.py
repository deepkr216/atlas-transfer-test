"""
cobol.py - extract program-level facts: paragraphs, CALL edges, COPY uses,
file declarations, SQL, IMS DL/I calls, CICS commands and MQ usage.

The CALL graph is the spine of every impact analysis, and it is where naive
extraction quietly loses edges:

  * `CALL 'SUBPGM'` is easy. `CALL WS-PGM-NAME` is the one that matters, and a
    grep-based tool either ignores it or records the variable name as if it
    were a program. Both are wrong in a way that silently shrinks the impact
    set - the worst possible failure, because the answer still looks complete.
    Here, dynamic targets are resolved by tracing literals assigned to the
    variable (MOVE 'X' TO V, and VALUE 'X' in its declaration), every candidate
    is kept, and if nothing resolves the edge is recorded as UNRESOLVED rather
    than dropped.

  * EXEC CICS LINK/XCTL PROGRAM(...) are call edges too, and take the same
    literal-vs-variable treatment.

  * A `CALL` inside a comment or a debugging line is not a call. Comment and
    'D'-indicator filtering happens upstream in reader.py.

  * `PERFORM A THRU B` executes every paragraph between A and B in source
    order, not just A and B. That needs paragraph ordinals, which is why
    paragraphs are numbered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .reader import (Line, LogicalLine, cobol_statements,
                     join_cobol_continuations, read_cobol_lines)

# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

ID = r"[A-Z0-9][A-Z0-9\-_]*"

_PROGRAM_ID = re.compile(rf"\bPROGRAM-ID\s*\.?\s+({ID})", re.IGNORECASE)
_DIVISION = re.compile(rf"\b({ID})\s+DIVISION\b", re.IGNORECASE)
_SECTION = re.compile(rf"^({ID})\s+SECTION\s*\.", re.IGNORECASE)
_PARAGRAPH = re.compile(rf"^({ID})\s*\.\s*$", re.IGNORECASE)

_CALL_LIT = re.compile(r"\bCALL\s+(['\"])([^'\"]+)\1", re.IGNORECASE)
_CALL_VAR = re.compile(rf"\bCALL\s+({ID})\b", re.IGNORECASE)
_CANCEL = re.compile(r"\bCANCEL\s+(['\"])([^'\"]+)\1", re.IGNORECASE)
_USING = re.compile(r"\bUSING\b(.*?)(?:\bRETURNING\b|\bON\s+EXCEPTION\b|$)",
                    re.IGNORECASE | re.DOTALL)

_COPY = re.compile(
    rf"\bCOPY\s+({ID})(?:\s+(?:OF|IN)\s+({ID}))?(?P<rep>\s+REPLACING\b.*)?",
    re.IGNORECASE)

_SELECT = re.compile(
    rf"\bSELECT\s+(?:OPTIONAL\s+)?({ID})\s+ASSIGN\s+(?:TO\s+)?"
    rf"(?:(['\"])([^'\"]+)\2|({ID}))", re.IGNORECASE)
_ORGANIZATION = re.compile(r"\bORGANIZATION\s+(?:IS\s+)?(\w+)", re.IGNORECASE)
_ACCESS = re.compile(r"\bACCESS\s+(?:MODE\s+)?(?:IS\s+)?(\w+)", re.IGNORECASE)
_RECORD_KEY = re.compile(rf"\bRECORD\s+KEY\s+(?:IS\s+)?({ID})", re.IGNORECASE)
_ALT_KEY = re.compile(rf"\bALTERNATE\s+RECORD\s+KEY\s+(?:IS\s+)?({ID})", re.IGNORECASE)
_FD = re.compile(rf"^FD\s+({ID})", re.IGNORECASE)

_FILE_OP = re.compile(
    rf"\b(OPEN\s+(?:INPUT|OUTPUT|I-O|EXTEND)|READ|WRITE|REWRITE|DELETE|START|CLOSE)\s+({ID})",
    re.IGNORECASE)

_EXEC_SQL = re.compile(r"\bEXEC\s+SQL\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)
_EXEC_CICS = re.compile(r"\bEXEC\s+CICS\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)
_EXEC_DLI = re.compile(r"\bEXEC\s+DLI\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)

_CICS_PROG = re.compile(r"\b(LINK|XCTL)\b.*?\bPROGRAM\s*\(\s*([^)]*?)\s*\)",
                        re.IGNORECASE | re.DOTALL)
_CICS_VERB = re.compile(r"^\s*([A-Z-]+)", re.IGNORECASE)
_CICS_FILE = re.compile(r"\b(?:FILE|DATASET)\s*\(\s*([^)]*?)\s*\)", re.IGNORECASE)

_DLI_CALL = re.compile(
    rf"\bCALL\s+(['\"])(CBLTDLI|AIBTDLI|PLITDLI)\1\s+USING\b(.*)",
    re.IGNORECASE | re.DOTALL)
_MQ_CALL = re.compile(r"\bCALL\s+['\"](MQ[A-Z0-9]+)['\"]", re.IGNORECASE)

_MOVE_LIT = re.compile(rf"\bMOVE\s+(['\"])([^'\"]*)\1\s+TO\s+([A-Z0-9\-_,\s]+)",
                       re.IGNORECASE)
_SET_VALUE = re.compile(rf"^\s*\d{{1,2}}\s+({ID})\b.*\bVALUE\s+(?:IS\s+)?(['\"])([^'\"]*)\2",
                        re.IGNORECASE)

_PERFORM = re.compile(
    rf"\bPERFORM\s+({ID})(?:\s+(?:THRU|THROUGH)\s+({ID}))?", re.IGNORECASE)

# Single-word statements that end in a period and sit in Area A after a
# paragraph header on the same line (`1000-EXIT.  EXIT.`). Without this list
# they get indexed as paragraphs called EXIT / GOBACK.
_NOT_PARAGRAPHS = {"EXIT", "GOBACK", "CONTINUE", "STOP", "END-IF", "END-EVALUATE",
                   "END-PERFORM", "END-READ", "END-CALL", "ELSE", "END-EXEC"}

_DLI_FUNCS = {"GU", "GHU", "GN", "GHN", "GNP", "GHNP", "ISRT", "REPL", "DLET",
              "CHKP", "XRST", "ROLB", "ROLL", "PCB", "TERM", "SYNC", "INIT",
              "GSCD", "LOG", "STAT", "APSB", "DPSB"}

_SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE", "DECLARE", "OPEN",
              "FETCH", "CLOSE", "CALL", "MERGE", "COMMIT", "ROLLBACK",
              "WHENEVER", "SET", "PREPARE", "EXECUTE", "LOCK", "INCLUDE")

_SQL_TABLE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|INSERT\s+INTO)\s+"
    r"([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+)?)", re.IGNORECASE)
_SQL_HOSTVAR = re.compile(rf"[:]({ID})", re.IGNORECASE)
_SQL_CURSOR = re.compile(rf"\bDECLARE\s+({ID})\s+(?:\w+\s+)?CURSOR", re.IGNORECASE)


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------

@dataclass
class CallFact:
    kind: str               # static | dynamic | cics_link | cics_xctl | cancel
    target: Optional[str]
    via_var: Optional[str]
    resolved: List[str]
    resolution: str         # literal | move_literal | value_clause | unresolved
    using_args: List[str]
    line: int


@dataclass
class ParagraphFact:
    name: str
    section: Optional[str]
    start_line: int
    end_line: int
    ordinal: int


@dataclass
class SqlFact:
    stmt_type: str
    cursor_name: Optional[str]
    tables: List[str]
    host_vars: List[str]
    is_dynamic: bool
    start_line: int
    end_line: int
    text: str


@dataclass
class DliFact:
    interface: str
    func: Optional[str]
    pcb_arg: Optional[str]
    ssa_args: List[str]
    io_area: Optional[str]
    line: int


@dataclass
class FileDeclFact:
    select_name: str
    assign_dd: Optional[str]
    organization: Optional[str]
    access_mode: Optional[str]
    record_key: Optional[str]
    alt_keys: List[str]
    fd_record: Optional[str]
    line: int


@dataclass
class ProgramFacts:
    program_id: Optional[str] = None
    src_lines: int = 0
    uses_sql: bool = False
    uses_cics: bool = False
    uses_dli: bool = False
    uses_mq: bool = False
    linkage_using: List[str] = dc_field(default_factory=list)
    paragraphs: List[ParagraphFact] = dc_field(default_factory=list)
    performs: List[Tuple[str, str, Optional[str], int]] = dc_field(default_factory=list)
    calls: List[CallFact] = dc_field(default_factory=list)
    copies: List[Tuple[str, Optional[str], Optional[str], int]] = dc_field(default_factory=list)
    files: List[FileDeclFact] = dc_field(default_factory=list)
    io_ops: List[Tuple[str, str, str, int]] = dc_field(default_factory=list)
    sql: List[SqlFact] = dc_field(default_factory=list)
    dli: List[DliFact] = dc_field(default_factory=list)
    cics: List[Tuple[str, str, int]] = dc_field(default_factory=list)
    mq: List[Tuple[str, int]] = dc_field(default_factory=list)
    unresolved: List[Tuple[str, str, int]] = dc_field(default_factory=list)


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def parse_program(text: str, data: bytes = b"", enc: str = "utf-8") -> ProgramFacts:
    lines, _fixed = read_cobol_lines(text, data=data, enc=enc)
    logical = join_cobol_continuations(lines)
    stmts = list(cobol_statements(logical))

    f = ProgramFacts(src_lines=len(lines))

    # Pass 1: literal assignments, so dynamic CALLs can be resolved in pass 2.
    literal_map = _build_literal_map(stmts)

    # Pass 2: everything else.
    current_division = None
    current_section = None
    ordinal = 0
    para_open: Optional[ParagraphFact] = None

    want_program_id = False

    for st in stmts:
        up = st.upper
        body = st.text.strip()

        # `PROGRAM-ID. MULTILN.` on one line, or `PROGRAM-ID.` with the name
        # on the following line - both are common. The second form splits into
        # two statements, so the name is taken from the statement after.
        if want_program_id and f.program_id is None:
            tok = re.match(rf"({ID})", body, re.IGNORECASE)
            if tok:
                f.program_id = tok.group(1).upper()
            want_program_id = False
        m = _PROGRAM_ID.search(up)
        if m and f.program_id is None:
            f.program_id = m.group(1).upper()
        elif re.fullmatch(r"PROGRAM-ID\s*\.?", up.strip().rstrip(".")):
            want_program_id = True

        m = _DIVISION.search(up)
        if m and re.search(rf"^{m.group(1)}\s+DIVISION", up):
            current_division = m.group(1).upper()
            current_section = None
            if current_division == "PROCEDURE":
                f.linkage_using = _parse_using(body)
            if para_open:
                para_open.end_line = st.start - 1
                para_open = None
            continue

        # ---- paragraph / section headers (Area A only) --------------------
        if current_division == "PROCEDURE" and st.area_a:
            ms = _SECTION.match(body)
            if ms:
                current_section = ms.group(1).upper()
                if para_open:
                    para_open.end_line = st.start - 1
                    para_open = None
                continue
            mp = _PARAGRAPH.match(body)
            if mp and mp.group(1).upper() not in _NOT_PARAGRAPHS:
                if para_open:
                    para_open.end_line = st.start - 1
                ordinal += 1
                para_open = ParagraphFact(name=mp.group(1).upper(),
                                          section=current_section,
                                          start_line=st.start,
                                          end_line=st.end,
                                          ordinal=ordinal)
                f.paragraphs.append(para_open)
                continue

        here = para_open.name if para_open else (current_section or "")

        _extract_copy(f, st)
        if current_division in ("ENVIRONMENT", None):
            _extract_file_decl(f, st, body)
        if current_division == "DATA":
            _extract_fd(f, st, body)
        if current_division == "PROCEDURE":
            _extract_calls(f, st, literal_map)
            _extract_performs(f, st, here)
            _extract_file_ops(f, st)
        _extract_sql(f, st)
        _extract_cics(f, st, literal_map)
        _extract_dli(f, st)
        _extract_mq(f, st)

    if para_open:
        para_open.end_line = len(lines)

    f.uses_sql = bool(f.sql)
    f.uses_cics = bool(f.cics)
    f.uses_dli = bool(f.dli)
    f.uses_mq = bool(f.mq)
    return f


# --------------------------------------------------------------------------
# dynamic CALL resolution
# --------------------------------------------------------------------------

def _build_literal_map(stmts: Sequence[LogicalLine]) -> Dict[str, Set[str]]:
    """variable name -> every literal ever assigned to it.

    Covers `MOVE 'PGM' TO WS-VAR` and `01 WS-VAR PIC X(8) VALUE 'PGM'`. That
    catches the overwhelming majority of dynamic CALLs in ordinary batch code.
    It will NOT catch a target read from a control card, a DB2 table, or a
    dispatch table - and those must therefore remain visible as unresolved.
    """
    out: Dict[str, Set[str]] = {}

    for st in stmts:
        body = st.text

        mv = _SET_VALUE.match(body)
        if mv:
            out.setdefault(mv.group(1).upper(), set()).add(mv.group(3).strip().upper())

        for m in _MOVE_LIT.finditer(body):
            lit = m.group(2).strip().upper()
            targets = m.group(3)
            # Stop at the first COBOL keyword; MOVE targets are a name list.
            targets = re.split(r"\b(?:OF|IN|WHEN|END-|IF|ELSE|PERFORM|MOVE|CALL)\b",
                               targets, maxsplit=1, flags=re.IGNORECASE)[0]
            for t in re.split(r"[,\s]+", targets):
                t = t.strip().rstrip(".").upper()
                if t and re.fullmatch(ID, t, re.IGNORECASE):
                    out.setdefault(t, set()).add(lit)
    return out


def _extract_calls(f: ProgramFacts, st: LogicalLine,
                   literal_map: Dict[str, Set[str]]) -> None:
    body = st.text
    up = st.upper

    # DL/I and MQ calls are CALLs syntactically but are handled elsewhere.
    if _DLI_CALL.search(body) or _MQ_CALL.search(body):
        return

    using = _parse_using(body)

    for m in _CALL_LIT.finditer(body):
        target = m.group(2).strip().upper()
        f.calls.append(CallFact(kind="static", target=target, via_var=None,
                                resolved=[target], resolution="literal",
                                using_args=using, line=st.start))

    if not _CALL_LIT.search(body):
        m = _CALL_VAR.search(body)
        if m:
            var = m.group(1).upper()
            cands = sorted(literal_map.get(var, []))
            f.calls.append(CallFact(
                kind="dynamic", target=None, via_var=var,
                resolved=cands,
                resolution="move_literal" if cands else "unresolved",
                using_args=using, line=st.start))
            if not cands:
                f.unresolved.append((
                    "dynamic_call",
                    f"CALL {var} - target never assigned a literal in this program; "
                    f"it may come from a control card, a DB2 table, or LINKAGE",
                    st.start))

    for m in _CANCEL.finditer(body):
        f.calls.append(CallFact(kind="cancel", target=m.group(2).upper(),
                                via_var=None, resolved=[m.group(2).upper()],
                                resolution="literal", using_args=[], line=st.start))


def _parse_using(body: str) -> List[str]:
    m = _USING.search(body)
    if not m:
        return []
    raw = m.group(1)
    raw = re.split(r"\bEND-CALL\b|\.", raw)[0]
    out = []
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip().upper()
        if not tok or tok in ("BY", "REFERENCE", "CONTENT", "VALUE", "OF", "IN"):
            continue
        if re.fullmatch(ID, tok, re.IGNORECASE):
            out.append(tok)
    return out


# --------------------------------------------------------------------------
# other extractors
# --------------------------------------------------------------------------

def _extract_copy(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _COPY.finditer(st.text):
        rep = m.group("rep")
        f.copies.append((m.group(1).upper(),
                         m.group(2).upper() if m.group(2) else None,
                         rep.strip() if rep else None,
                         st.start))
        if rep:
            f.unresolved.append((
                "copy_replacing",
                f"COPY {m.group(1).upper()} REPLACING - field names in this "
                f"program differ from the copybook as stored; expand before "
                f"trusting field-level results",
                st.start))


def _extract_file_decl(f: ProgramFacts, st: LogicalLine, body: str) -> None:
    m = _SELECT.search(body)
    if not m:
        return
    dd = (m.group(3) or m.group(4) or "").upper()
    org = _ORGANIZATION.search(body)
    acc = _ACCESS.search(body)
    rk = _RECORD_KEY.search(body)
    f.files.append(FileDeclFact(
        select_name=m.group(1).upper(),
        assign_dd=dd or None,
        organization=org.group(1).upper() if org else None,
        access_mode=acc.group(1).upper() if acc else None,
        record_key=rk.group(1).upper() if rk else None,
        alt_keys=[a.upper() for a in _ALT_KEY.findall(body)],
        fd_record=None,
        line=st.start))


def _extract_fd(f: ProgramFacts, st: LogicalLine, body: str) -> None:
    m = _FD.match(body)
    if not m:
        return
    name = m.group(1).upper()
    for fd in f.files:
        if fd.select_name == name and fd.fd_record is None:
            fd.fd_record = name


def _extract_file_ops(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _FILE_OP.finditer(st.text):
        op = re.sub(r"\s+", " ", m.group(1).upper())
        f.io_ops.append((m.group(2).upper(), "file", op, st.start))


def _extract_sql(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _EXEC_SQL.finditer(st.text):
        inner = " ".join(m.group(1).split())
        verb = next((v for v in _SQL_VERBS
                     if re.match(rf"^{v}\b", inner, re.IGNORECASE)), "OTHER")
        cur = _SQL_CURSOR.search(inner)
        tables = sorted({t.upper() for t in _SQL_TABLE.findall(inner)})
        hvars = sorted({h.upper() for h in _SQL_HOSTVAR.findall(inner)})
        dynamic = bool(re.search(r"\b(PREPARE|EXECUTE\s+IMMEDIATE)\b", inner, re.IGNORECASE))
        f.sql.append(SqlFact(stmt_type=verb.upper(),
                             cursor_name=cur.group(1).upper() if cur else None,
                             tables=tables, host_vars=hvars, is_dynamic=dynamic,
                             start_line=st.start, end_line=st.end, text=inner))
        for t in tables:
            f.io_ops.append((t, "db2", verb.upper(), st.start))
        if dynamic:
            f.unresolved.append((
                "dynamic_sql",
                "dynamic SQL (PREPARE/EXECUTE IMMEDIATE) - the tables touched "
                "are not statically determinable from this source",
                st.start))


def _extract_cics(f: ProgramFacts, st: LogicalLine,
                  literal_map: Dict[str, Set[str]]) -> None:
    for m in _EXEC_CICS.finditer(st.text):
        inner = " ".join(m.group(1).split())
        verb = _CICS_VERB.match(inner)
        f.cics.append((verb.group(1).upper() if verb else "?", inner, st.start))

        mp = _CICS_PROG.search(inner)
        if mp:
            kind = f"cics_{mp.group(1).lower()}"
            arg = mp.group(2).strip()
            lit = re.fullmatch(r"['\"]([^'\"]+)['\"]", arg)
            if lit:
                t = lit.group(1).upper()
                f.calls.append(CallFact(kind=kind, target=t, via_var=None,
                                        resolved=[t], resolution="literal",
                                        using_args=[], line=st.start))
            else:
                var = arg.upper()
                cands = sorted(literal_map.get(var, []))
                f.calls.append(CallFact(
                    kind=kind, target=None, via_var=var, resolved=cands,
                    resolution="move_literal" if cands else "unresolved",
                    using_args=[], line=st.start))
                if not cands:
                    f.unresolved.append((
                        "dynamic_call",
                        f"EXEC CICS {mp.group(1).upper()} PROGRAM({var}) - target "
                        f"not resolvable from this source", st.start))

        mf = _CICS_FILE.search(inner)
        if mf and verb:
            arg = mf.group(1).strip().strip("'\"").upper()
            f.io_ops.append((arg, "cics", verb.group(1).upper(), st.start))


def _extract_dli(f: ProgramFacts, st: LogicalLine) -> None:
    m = _DLI_CALL.search(st.text)
    if m:
        iface = m.group(2).upper()
        args = _split_call_args(m.group(3))
        _record_dli(f, st, iface, args)
        return

    for m2 in _EXEC_DLI.finditer(st.text):
        inner = " ".join(m2.group(1).split())
        verb = _CICS_VERB.match(inner)
        f.dli.append(DliFact(interface="EXEC DLI",
                             func=verb.group(1).upper() if verb else None,
                             pcb_arg=None, ssa_args=[], io_area=None,
                             line=st.start))


def _record_dli(f: ProgramFacts, st: LogicalLine, iface: str,
                args: List[str]) -> None:
    """CALL 'CBLTDLI' USING func, pcb, io-area, ssa...

    The PCB argument is positional in the PSB, which is why it is captured
    verbatim: resolving which database it refers to requires the PSB, and
    guessing is how an analysis ends up naming the wrong IMS database.
    """
    func = None
    pcb = None
    io_area = None
    ssas: List[str] = []

    if args:
        first = args[0].strip().strip("'\"").upper()
        if first in _DLI_FUNCS:
            func = first
        elif re.fullmatch(ID, first, re.IGNORECASE):
            func = f"*{first}*"      # func held in a variable, e.g. WS-GU
    if len(args) > 1:
        pcb = args[1].strip().upper()
    if len(args) > 2:
        io_area = args[2].strip().upper()
    if len(args) > 3:
        ssas = [a.strip().upper() for a in args[3:]]

    f.dli.append(DliFact(interface=iface, func=func, pcb_arg=pcb,
                         ssa_args=ssas, io_area=io_area, line=st.start))

    if func and func.startswith("*"):
        f.unresolved.append((
            "dli_function",
            f"DL/I call function held in variable {func.strip('*')} - the "
            f"operation (read vs update) cannot be determined statically",
            st.start))
    if pcb:
        f.io_ops.append((pcb, "ims", func or "?", st.start))


def _split_call_args(raw: str) -> List[str]:
    raw = re.split(r"\bEND-CALL\b", raw, flags=re.IGNORECASE)[0]
    raw = raw.rstrip().rstrip(".")
    out = []
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip()
        if not tok or tok.upper() in ("BY", "REFERENCE", "CONTENT", "VALUE"):
            continue
        out.append(tok)
    return out


def _extract_mq(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _MQ_CALL.finditer(st.text):
        f.mq.append((m.group(1).upper(), st.start))


def _extract_performs(f: ProgramFacts, st: LogicalLine, here: str) -> None:
    if st.area_a and _PARAGRAPH.match(st.text.strip()):
        return
    for m in _PERFORM.finditer(st.text):
        to = m.group(1).upper()
        thru = m.group(2).upper() if m.group(2) else None
        # `PERFORM UNTIL`, `PERFORM VARYING`, `PERFORM n TIMES` are inline.
        if to in ("UNTIL", "VARYING", "WITH", "TEST", "FOREVER"):
            continue
        f.performs.append((here, to, thru, st.start))


# --------------------------------------------------------------------------
# PERFORM THRU expansion
# --------------------------------------------------------------------------

def expand_perform_thru(paragraphs: Sequence[ParagraphFact],
                        start: str, thru: Optional[str]) -> List[str]:
    """Every paragraph actually executed by PERFORM start THRU thru.

    Source order, not just the two endpoints. A range can cover a dozen
    paragraphs, and an impact analysis that lists only the endpoints
    understates the blast radius.
    """
    if not thru:
        return [start]
    by_name = {p.name: p for p in paragraphs}
    a, b = by_name.get(start.upper()), by_name.get(thru.upper())
    if not a or not b:
        return [start] + ([thru] if thru else [])
    lo, hi = sorted((a.ordinal, b.ordinal))
    return [p.name for p in paragraphs if lo <= p.ordinal <= hi]
