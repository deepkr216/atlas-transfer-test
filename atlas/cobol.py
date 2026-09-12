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
# Word boundaries that treat '-' as part of the word. `\bCALL\b` matches inside
# WS-CALL-FLAG, and `MOVE WS-CALL TO X` would then be indexed as a dynamic call
# to a program named TO. These lookarounds are used on every verb pattern.
B = r"(?<![A-Z0-9\-])"
E = r"(?![A-Z0-9\-])"

_PROGRAM_ID = re.compile(rf"\bPROGRAM-ID\s*\.?\s+({ID})", re.IGNORECASE)
_DIVISION = re.compile(rf"\b({ID})\s+DIVISION\b", re.IGNORECASE)
_SECTION = re.compile(rf"^({ID})\s+SECTION\s*\.", re.IGNORECASE)
_PARAGRAPH = re.compile(rf"^({ID})\s*\.\s*$", re.IGNORECASE)

_CALL_LIT = re.compile(B + r"CALL\s+(['\"])([^'\"]+)\1", re.IGNORECASE)
_CALL_VAR = re.compile(B + rf"CALL\s+({ID})" + E, re.IGNORECASE)
_CANCEL = re.compile(B + r"CANCEL\s+(['\"])([^'\"]+)\1", re.IGNORECASE)
_USING = re.compile(r"\bUSING\b(.*?)(?:\bRETURNING\b|\bON\s+EXCEPTION\b|$)",
                    re.IGNORECASE | re.DOTALL)

_COPY = re.compile(
    B + rf"COPY\s+({ID})(?:\s+(?:OF|IN)\s+({ID}))?(?P<rep>\s+REPLACING" + E + r".*)?",
    re.IGNORECASE)
# EXEC SQL INCLUDE is how every DCLGEN host structure arrives; a COPY-only
# pattern misses all of them and the DB2 column-to-field mapping goes dark.
_SQL_INCLUDE = re.compile(B + rf"EXEC\s+SQL\s+INCLUDE\s+({ID})", re.IGNORECASE)
# Panvalet / Librarian inclusion sits in columns 1-9, i.e. in the sequence
# area, so it must be matched against the RAW record, not the code area.
_LIB_INCLUDE = re.compile(r"^(?:\+\+INCLUDE\s+([A-Z0-9@#$]{1,10})|-INC\s+([A-Z0-9@#$]{1,10}))",
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
    B + rf"(OPEN\s+(?:INPUT|OUTPUT|I-O|EXTEND)|READ|WRITE|REWRITE|DELETE|START|CLOSE)\s+({ID})",
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

_MOVE_LIT = re.compile(B + rf"MOVE\s+(['\"])([^'\"]*)\1\s+TO\s+([A-Z0-9\-_,\s]+)",
                       re.IGNORECASE)
_SET_VALUE = re.compile(rf"^\s*\d{{1,2}}\s+({ID})\b.*\bVALUE\s+(?:IS\s+)?(['\"])([^'\"]*)\2",
                        re.IGNORECASE)

_PERFORM = re.compile(
    B + rf"PERFORM\s+({ID})(?:\s+(?:THRU|THROUGH)\s+({ID}))?", re.IGNORECASE)

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

# INTO is a table only after INSERT/MERGE. In SELECT ... INTO :hv and
# FETCH ... INTO :hv it introduces HOST VARIABLES; harvesting those as table
# names poisons the lineage graph. `FOR UPDATE OF col` is excluded too.
_SQL_TABLE = re.compile(
    r"\b(?:FROM|JOIN|(?<!FOR )UPDATE|DELETE\s+FROM|INSERT\s+INTO|MERGE\s+INTO)\s+"
    r"(?!ONLY\b)([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+){0,2})", re.IGNORECASE)
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
    # (field, mode read|write|test|display, statement verb, line). `write` is
    # what makes "where is this error code SET" answerable; `display` is what
    # makes "and where is it shown/written" answerable - often another program.
    field_refs: List[Tuple[str, str, str, int]] = dc_field(default_factory=list)
    # (literal, context move_to|compare|when|display|value|cond88|string, field, line)
    # Error codes, status codes and switches are LITERALS first and field names
    # second. Indexing the literal is how 'E123' is traced from the copybook
    # 88-level that defines it, to the MOVE that sets it in one program, to the
    # DISPLAY in a different program that shows it.
    literal_refs: List[Tuple[str, str, Optional[str], int]] = dc_field(default_factory=list)
    unresolved: List[Tuple[str, str, int]] = dc_field(default_factory=list)


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def parse_program(text: str, data: bytes = b"", enc: str = "utf-8") -> ProgramFacts:
    lines, _fixed = read_cobol_lines(text, data=data, enc=enc)
    logical = join_cobol_continuations(lines)
    stmts = list(cobol_statements(logical))

    f = ProgramFacts(src_lines=len(lines))

    # Library-manager includes live in the sequence area of the RAW record.
    for ln in lines:
        m = _LIB_INCLUDE.match(ln.raw)
        if m:
            f.copies.append(((m.group(1) or m.group(2)).upper(), None, None, ln.no))

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
            _extract_field_and_literal_refs(f, st)
        elif current_division in ("DATA", None):
            _extract_value_literals(f, st)
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
    for m in _SQL_INCLUDE.finditer(st.text):
        f.copies.append((m.group(1).upper(), None, None, st.start))
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


_ASSIGN_PREFIXES = {"UT", "AS", "S", "DA", "UR", "OR", "UP", "DN", "SYS"}


def _assign_to_ddname(literal: Optional[str], name: Optional[str]) -> Tuple[str, str]:
    """ASSIGN TO x does not simply yield a DD name.

    Real assignment names carry organisation/device prefixes (UT-S-POLMAST,
    AS-POLMAST, S-POLMAST), may be the literal 'DD:POLMAST', or may be a
    data-name used for dynamic allocation. The DD name is the last
    hyphen-delimited qualifier, truncated to 8, and the data-name case is
    flagged rather than guessed. Returns (ddname, kind).
    """
    if literal:
        s = literal.strip().upper()
        if s.startswith("DD:"):
            return s[3:][:8], "literal"
        return s.split("-")[-1][:8], "literal"
    s = (name or "").strip().upper()
    if not s:
        return "", "missing"
    parts = s.split("-")
    if len(parts) == 1:
        return s[:8], "name"
    if parts[0] in _ASSIGN_PREFIXES or (len(parts) == 2 and len(parts[0]) <= 3):
        return parts[-1][:8], "prefixed"
    # Hyphenated and not a known prefix form: almost certainly a data-name
    # whose value is set at run time (dynamic allocation).
    return parts[-1][:8], "dataname_guess"


def _extract_file_decl(f: ProgramFacts, st: LogicalLine, body: str) -> None:
    m = _SELECT.search(body)
    if not m:
        return
    dd, dd_kind = _assign_to_ddname(m.group(3), m.group(4))
    if dd_kind == "dataname_guess":
        f.unresolved.append((
            "assign_dataname",
            f"SELECT {m.group(1).upper()} ASSIGN TO {m.group(4)} looks like a data-name "
            f"(dynamic allocation); DD name {dd!r} is a guess from the last qualifier",
            st.start))
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


_SQL_PRED = re.compile(
    r"\b([A-Z_][A-Z0-9_]*)\s*(?:=|<>|!=|>=|<=|>|<)\s*('(?:[^']|'')*'|[+-]?\d+(?:\.\d+)?)(?![A-Z0-9_])", re.IGNORECASE)
_SQL_IN = re.compile(r"\b([A-Z_][A-Z0-9_]*)\s+(?:NOT\s+)?IN\s*\(([^)]*)\)", re.IGNORECASE)
_SQL_INSERT = re.compile(r"\bINSERT\s+INTO\s+\S+\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)", re.IGNORECASE | re.S)
_SQL_LIT = re.compile(r"'(?:[^']|'')*'|(?<![A-Z0-9_:])[+-]?\d+(?:\.\d+)?(?![A-Z0-9_])", re.IGNORECASE)


def _sql_literals(f: ProgramFacts, inner: str, verb: str, ln: int) -> None:
    """Column/literal pairs inside SQL: WHERE REL_CD = 3, GENDER_CD IN ('M','F'),
    SET REL_CD = 4, INSERT ... VALUES. A value-domain change (a new code) has
    to find these as surely as the COBOL IFs, and they are not COBOL."""
    up = inner.upper()
    where_at = up.find(" WHERE ")
    for m in _SQL_PRED.finditer(inner):
        col, lit = m.group(1).upper(), m.group(2)
        if col in ("SELECT", "AND", "OR", "WHEN", "THEN", "ELSE", "SET"):
            continue
        ctx = "sql_set" if (verb == "UPDATE" and (where_at < 0 or m.start() < where_at)) else "sql_predicate"
        f.literal_refs.append((_norm_lit(lit), ctx, col, ln))
    for m in _SQL_IN.finditer(inner):
        col = m.group(1).upper()
        for lit in _SQL_LIT.findall(m.group(2)):
            f.literal_refs.append((_norm_lit(lit), "sql_predicate", col, ln))
    m = _SQL_INSERT.search(inner)
    if m:
        cols = [c.strip().upper() for c in m.group(1).split(",")]
        vals = [v.strip() for v in re.split(r",(?=(?:[^']*'[^']*')*[^']*$)", m.group(2))]
        for col, val in zip(cols, vals):
            if _SQL_LIT.fullmatch(val):
                f.literal_refs.append((_norm_lit(val), "sql_insert", col, ln))


def _sql_host_modes(f: ProgramFacts, inner: str, ln: int) -> None:
    """Host variables after SELECT/FETCH ... INTO are WRITTEN by DB2; all
    others are read. This is how 'where does this field get populated' finds
    a value that arrives from a table rather than from a MOVE."""
    writes = set()
    if not re.search(r"\b(?:INSERT|MERGE)\s+INTO\b", inner, re.IGNORECASE):
        m = re.search(r"\bINTO\s+((?::[A-Z0-9\-_]+(?:\s*:[A-Z0-9\-_]+)?\s*,?\s*)+)",
                      inner, re.IGNORECASE)
        if m:
            writes = {h.upper() for h in re.findall(r":([A-Z0-9\-_]+)", m.group(1), re.IGNORECASE)}
    for h in {h.upper() for h in _SQL_HOSTVAR.findall(inner)}:
        f.field_refs.append((h, "write" if h in writes else "read", "EXEC-SQL", ln))


def _extract_sql(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _EXEC_SQL.finditer(st.text):
        inner = " ".join(m.group(1).split())
        verb = next((v for v in _SQL_VERBS
                     if re.match(rf"^{v}\b", inner, re.IGNORECASE)), "OTHER")
        if verb == "INCLUDE":
            continue                     # handled as a copy in _extract_copy
        cur = _SQL_CURSOR.search(inner)
        tables = sorted({t.upper() for t in _SQL_TABLE.findall(inner)})
        hvars = sorted({h.upper() for h in _SQL_HOSTVAR.findall(inner)})
        _sql_host_modes(f, inner, st.start)
        _sql_literals(f, inner, verb.upper(), st.start)
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


# --------------------------------------------------------------------------
# field references (read / WRITE / test / display) and literal references
# --------------------------------------------------------------------------
#
# Why this exists: the questions that go wrong most often in practice are
# "where is this error code SET" and "where is it DISPLAYED / written out",
# and the two answers are routinely in different programs. A name index
# cannot answer either: the value is a LITERAL ('E123'), it is defined as an
# 88-level or VALUE clause in a copybook, MOVEd to a field in one program,
# passed through CALL USING or a file record, and DISPLAYed by another program
# under a different field name. So literals are indexed as facts in their own
# right, and every field reference carries a mode so a write can be told from
# a read.

# COBOL words are hyphenated, so `\b` is the wrong boundary: `\bREAD\b` matches
# inside WS-READ-FLAG. These lookarounds treat '-' as part of the word.
B = r"(?<![A-Z0-9\-])"
E = r"(?![A-Z0-9\-])"
LIT = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[+-]?\d+(?:\.\d+)?|SPACES?|ZEROE?S?|LOW-VALUES?|HIGH-VALUES?|QUOTES?|NULLS?"

_RESERVED = set("""
ACCEPT ADD ADDRESS ADVANCING AFTER ALL ALPHABETIC ALSO ALTER AND ANY ARE AREA AT
BEFORE BY CALL CANCEL CLOSE COMP COMP-3 COMPUTE CONTINUE CORRESPONDING CORR COUNT
DELETE DELIMITED DELIMITER DEPENDING DISPLAY DIVIDE DIVISION ELSE END END-ADD
END-CALL END-COMPUTE END-DELETE END-DIVIDE END-EVALUATE END-EXEC END-IF END-MULTIPLY
END-PERFORM END-READ END-RETURN END-REWRITE END-SEARCH END-START END-STRING
END-SUBTRACT END-UNSTRING END-WRITE EQUAL EQUALS ERROR EVALUATE EXCEPTION EXEC EXIT
FALSE FILE FIRST FOR FROM FUNCTION GIVING GO GOBACK GREATER HIGH-VALUE HIGH-VALUES
IF IN INITIALIZE INPUT INSPECT INTO INVALID IS I-O KEY LEADING LESS LINE LOW-VALUE
LOW-VALUES MOVE MULTIPLY NEGATIVE NEXT NOT NUMERIC OF ON OPEN OR OTHER OUTPUT OVERFLOW
PERFORM POINTER POSITIVE PROCEDURE PROGRAM READ RECORD REFERENCE REMAINDER REPLACING
RETURN RETURNING REWRITE ROUNDED RUN SEARCH SECTION SENTENCE SET SIZE SPACE SPACES
START STOP STRING SUBTRACT TALLYING TEST THAN THEN THROUGH THRU TIMES TO TRUE
UNSTRING UNTIL UPON USING VALUE VALUES VARYING WHEN WITH WRITE ZERO ZEROS ZEROES
SQL CICS DLI INCLUDE COPY CONTENT LENGTH NULL NULLS QUOTE QUOTES CONVERTING
CHARACTERS INITIAL ALPHANUMERIC NO KEY WHILE EXTEND RELEASE PREVIOUS DOWN UP
""".split())

_TOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[A-Z0-9][A-Z0-9\-]*(?:\([^)]*\))?", re.I)
_VERBS = ("MOVE|COMPUTE|ADD|SUBTRACT|MULTIPLY|DIVIDE|STRING|UNSTRING|INITIALIZE|SET|"
          "ACCEPT|READ|WRITE|REWRITE|RETURN|RELEASE|INSPECT|DISPLAY|CALL|PERFORM|IF|"
          "EVALUATE|WHEN|ELSE|SEARCH|EXEC|GO|GOBACK|STOP|OPEN|CLOSE|DELETE|START|"
          "CONTINUE|EXIT|END-[A-Z]+")
_SPLIT = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|" + B + "(" + _VERBS + ")" + E, re.I)

_MOVE_TO = re.compile(r"^(?:CORR(?:ESPONDING)?\s+)?(.+?)\s+" + B + "TO" + E + r"\s+(.+)$", re.I | re.S)
_LEADING_LIT = re.compile(r"^(?:" + LIT + r")$", re.I)
_COMPUTE = re.compile(r"^(.+?)\s*=\s*(.+)$", re.I | re.S)
_ARITH = re.compile(r"^(.+?)\s+" + B + r"(TO|FROM|BY|INTO)" + E + r"\s+(.+?)"
                    r"(?:\s+GIVING\s+(.+?))?(?:\s+REMAINDER\s+(\S+))?"
                    r"(?:\s+(?:NOT\s+)?(?:ON\s+)?SIZE\s+ERROR.*)?$", re.I | re.S)
_INTO_TGT = re.compile(B + r"INTO\s+(.+?)(?:\s+(?:WITH\s+)?POINTER|\s+(?:NOT\s+)?(?:ON\s+)?OVERFLOW"
                       r"|\s+TALLYING|\s+DELIMITER|\s+COUNT|$)", re.I | re.S)
_SET = re.compile(r"^(.+?)\s+(?:TO|UP\s+BY|DOWN\s+BY)\s+(.+)$", re.I | re.S)
_FROM_SRC = re.compile(B + r"FROM\s+([A-Z0-9][A-Z0-9\-]*)", re.I)
_EVAL_SUBJ = re.compile(r"^(?:TRUE|FALSE|([A-Z0-9][A-Z0-9\-]*))", re.I)
_CMP_OP = (r"(?:IS\s+)?(?:NOT\s+)?(?:=|EQUAL(?:S|\s+TO)?|>=|<=|>|<|"
           r"GREATER(?:\s+THAN)?(?:\s+OR\s+EQUAL(?:\s+TO)?)?|LESS(?:\s+THAN)?(?:\s+OR\s+EQUAL(?:\s+TO)?)?)")
_CMP = re.compile(B + r"([A-Z0-9][A-Z0-9\-]*)(?:\([^)]*\))?\s+" + _CMP_OP + r"\s+(" + LIT + ")", re.I)
_CMP_REV = re.compile(r"(" + LIT + r")\s+" + _CMP_OP + r"\s+" + B + r"([A-Z0-9][A-Z0-9\-]*)", re.I)
_CMP_MORE = re.compile(B + r"(?:OR|AND)\s+(?:NOT\s+)?(?:=\s+)?(" + LIT + ")" + E, re.I)
_ALL_LITS = re.compile(LIT, re.I)
_LEVEL_ENTRY = re.compile(r"^(\d{1,2})\s+([A-Z0-9][A-Z0-9\-]*)" + E + r"(.*)$", re.I | re.S)
_VALUE_PART = re.compile(B + r"VALUES?\s+(?:IS\s+|ARE\s+)?(.+)$", re.I | re.S)


def _norm_lit(lit: str) -> str:
    s = lit.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].replace(s[0] * 2, s[0])
    return s.upper()


def _idents(text: str) -> List[str]:
    """Identifiers in a fragment: literals skipped, reserved words removed,
    subscripts stripped from the name and returned as their own reads."""
    out: List[str] = []
    for m in _TOKEN.finditer(text or ""):
        t = m.group(0)
        if t[0] in ("'", '"'):
            continue
        base = t.split("(")[0].upper()
        if base in _RESERVED or re.fullmatch(r"[+\-\d.]+", base):
            continue
        if not re.fullmatch(ID, base, re.I):
            continue
        out.append(base)
        if "(" in t:
            for s in re.findall(r"[A-Z][A-Z0-9\-]*", t[t.index("(") + 1:], re.I):
                if s.upper() not in _RESERVED:
                    out.append(s.upper())
    return out


def _split_verbs(text: str) -> List[Tuple[str, str]]:
    """Break a (possibly compound) statement into (verb, fragment) pieces.

    `IF A = 'X' MOVE 'E1' TO RC ELSE MOVE 'E2' TO RC END-IF` is one COBOL
    statement containing three verbs. Dispatching on the first verb alone
    would lose both MOVEs - i.e. lose exactly the writes that set error codes.
    """
    parts: List[Tuple[str, str, int]] = []
    last, pos, at = None, 0, 0
    for m in _SPLIT.finditer(text):
        if m.group(1) is None:
            continue
        if last is not None:
            parts.append((last, text[pos:m.start()].strip(), at))
        last, pos, at = m.group(1).upper(), m.end(), m.start()
    if last is not None:
        parts.append((last, text[pos:].strip(), at))
    return parts


def _refs(f: ProgramFacts, names: List[str], mode: str, verb: str, ln: int) -> None:
    seen = set()
    for n in names:
        if n not in seen:
            seen.add(n)
            f.field_refs.append((n, mode, verb, ln))


def _compare_literals(f: ProgramFacts, frag: str, context: str, ln: int,
                      subject: Optional[str] = None) -> None:
    """IF X = 'A' OR 'B'   /   IF 'A' = X   /   WHEN 'A'   -> literal_refs."""
    found = False
    matches = [m for m in _CMP.finditer(frag) if m.group(1).upper() not in _RESERVED]
    for i, m in enumerate(matches):
        fld, lit = m.group(1).upper(), m.group(2)
        found = True
        f.literal_refs.append((_norm_lit(lit), context, fld, ln))
        # Abbreviated conditions (A = 1 OR 2) belong to the NEAREST preceding
        # compare only: in `A = 1 AND B = 'M' OR 'F'` the 'F' is B's, not A's.
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(frag)
        for m2 in _CMP_MORE.finditer(frag, m.end(), stop):
            f.literal_refs.append((_norm_lit(m2.group(1)), context, fld, ln))
    if not found:
        for m in _CMP_REV.finditer(frag):
            fld = m.group(2).upper()
            if fld not in _RESERVED:
                found = True
                f.literal_refs.append((_norm_lit(m.group(1)), context, fld, ln))
    if not found and subject:
        # WHEN 'A'  /  WHEN 'A' THRU 'C'  /  WHEN NOT 'A'
        for m in _ALL_LITS.finditer(frag):
            f.literal_refs.append((_norm_lit(m.group(0)), context, subject, ln))


def _extract_field_and_literal_refs(f: ProgramFacts, st: LogicalLine) -> None:
    body = st.text.strip().rstrip(".")
    # EVALUATE A ALSO B ... WHEN 3 ALSO 'M': one subject per ALSO position, and
    # each WHEN literal attaches to the subject at ITS position.
    eval_subjects: List[Optional[str]] = []

    for verb, frag, off in _split_verbs(body):
        ln = st.line_at(off)          # the verb's own physical line, not the IF's
        if verb == "MOVE":
            m = _MOVE_TO.match(frag)
            if not m:
                continue
            src, tgts = m.group(1), m.group(2)
            targets = _idents(tgts)
            _refs(f, _idents(src), "read", verb, ln)
            _refs(f, targets, "write", verb, ln)
            if _LEADING_LIT.match(src.strip()):
                for t in targets:
                    f.literal_refs.append((_norm_lit(src), "move_to", t, ln))

        elif verb == "COMPUTE":
            m = _COMPUTE.match(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)
                _refs(f, _idents(m.group(2)), "read", verb, ln)

        elif verb in ("ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"):
            m = _ARITH.match(frag)
            if m:
                operands, target, giving, rem = m.group(1), m.group(3), m.group(4), m.group(5)
                _refs(f, _idents(operands), "read", verb, ln)
                if giving:
                    _refs(f, _idents(target), "read", verb, ln)
                    _refs(f, _idents(giving), "write", verb, ln)
                else:
                    _refs(f, _idents(target), "write", verb, ln)
                if rem:
                    _refs(f, _idents(rem), "write", verb, ln)
            else:
                _refs(f, _idents(frag), "read", verb, ln)

        elif verb in ("STRING", "UNSTRING"):
            m = _INTO_TGT.search(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)
                _refs(f, _idents(frag[:m.start()]), "read", verb, ln)
                for lit in _ALL_LITS.finditer(frag[:m.start()]):
                    if lit.group(0)[0] in ("'", '"'):
                        for t in _idents(m.group(1)):
                            f.literal_refs.append((_norm_lit(lit.group(0)), "string", t, ln))
            else:
                _refs(f, _idents(frag), "read", verb, ln)

        elif verb == "INITIALIZE":
            head = re.split(B + r"(?:REPLACING|WITH)" + E, frag, maxsplit=1, flags=re.I)[0]
            _refs(f, _idents(head), "write", verb, ln)

        elif verb == "SET":
            m = _SET.match(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)
                _refs(f, _idents(m.group(2)), "read", verb, ln)

        elif verb == "ACCEPT":
            ids = _idents(frag)
            if ids:
                _refs(f, ids[:1], "write", verb, ln)

        elif verb in ("READ", "RETURN"):
            m = _INTO_TGT.search(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)

        elif verb in ("WRITE", "REWRITE", "RELEASE"):
            m = _FROM_SRC.search(frag)
            if m:
                _refs(f, [m.group(1).upper()], "read", verb, ln)

        elif verb == "INSPECT":
            ids = _idents(frag)
            if ids:
                if re.search(B + r"(?:REPLACING|CONVERTING)" + E, frag, re.I):
                    _refs(f, ids[:1], "write", verb, ln)
                else:
                    _refs(f, ids[:1], "read", verb, ln)
                mt = re.search(B + r"TALLYING\s+([A-Z0-9][A-Z0-9\-]*)", frag, re.I)
                if mt:
                    _refs(f, [mt.group(1).upper()], "write", verb, ln)

        elif verb == "DISPLAY":
            head = re.split(B + r"UPON" + E, frag, maxsplit=1, flags=re.I)[0]
            _refs(f, _idents(head), "display", verb, ln)
            for lit in _ALL_LITS.finditer(head):
                if lit.group(0)[0] in ("'", '"'):
                    f.literal_refs.append((_norm_lit(lit.group(0)), "display", None, ln))

        elif verb == "CALL":
            # BY REFERENCE is the default: the callee may write every argument.
            for a in _parse_using("CALL " + frag):
                f.field_refs.append((a, "write", "CALL-USING", ln))
                f.field_refs.append((a, "read", "CALL-USING", ln))

        elif verb == "IF":
            _refs(f, _idents(frag), "test", verb, ln)
            _compare_literals(f, frag, "compare", ln)

        elif verb == "EVALUATE":
            eval_subjects = []
            for part in re.split(B + "ALSO" + E, frag, flags=re.I):
                m = _EVAL_SUBJ.match(part.strip())
                eval_subjects.append(m.group(1).upper() if m and m.group(1) else None)
            _refs(f, _idents(frag), "test", verb, ln)

        elif verb == "WHEN":
            _refs(f, _idents(frag), "test", verb, ln)
            parts = re.split(B + "ALSO" + E, frag, flags=re.I)
            for i, part in enumerate(parts):
                subj = eval_subjects[i] if i < len(eval_subjects) else None
                _compare_literals(f, part, "when", ln, subject=subj)

        elif verb == "PERFORM":
            mu = re.search(B + r"UNTIL\s+(.+)$", frag, re.I | re.S)
            if mu:
                _refs(f, _idents(mu.group(1)), "test", verb, ln)
                _compare_literals(f, mu.group(1), "compare", ln)
            mv = re.search(B + r"VARYING\s+([A-Z0-9][A-Z0-9\-]*)", frag, re.I)
            if mv:
                _refs(f, [mv.group(1).upper()], "write", verb, ln)

        elif verb in ("SEARCH", "START", "DELETE", "OPEN", "CLOSE"):
            _refs(f, _idents(frag), "read", verb, ln)


def _extract_value_literals(f: ProgramFacts, st: LogicalLine) -> None:
    """VALUE clauses and 88-levels in the DATA DIVISION are literal SOURCES."""
    body = st.text.strip().rstrip(".")
    m = _LEVEL_ENTRY.match(body)
    if not m:
        return
    level, name, rest = m.group(1), m.group(2).upper(), m.group(3) or ""
    if level != "88":
        setattr(f, "_last_data_name", name)
    mv = _VALUE_PART.search(rest)
    if not mv:
        return
    context = "cond88" if level == "88" else "value"
    owner = name if level != "88" else f"{getattr(f, '_last_data_name', '')}/{name}"
    for lit in _ALL_LITS.finditer(mv.group(1)):
        f.literal_refs.append((_norm_lit(lit.group(0)), context, owner, st.start))
