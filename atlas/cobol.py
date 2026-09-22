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

# the text-name may be a literal: COPY 'NAME' and COPY "NAME" OF 'LIB' are legal and some shops
# write every COPY that way (LESSONS 173) - the quote, if any, must close. A member name may hold
# the national characters @ # $ (like _LIB_INCLUDE below), which a data-name (ID) may not
TEXT_NAME = r"[A-Z0-9@#$][A-Z0-9@#$\-_]*"
_COPY = re.compile(
    B + rf"COPY\s+(?P<q>['\"]?)(?P<name>{TEXT_NAME})(?P=q)(?:\s+(?:OF|IN)\s+(?P<q2>['\"]?)(?P<lib>{TEXT_NAME})(?P=q2))?"
    r"(?:\s+SUPPRESS)?(?P<rep>\s+REPLACING" + E + r".*)?",
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
_FD = re.compile(rf"^(FD|SD)\s+({ID})", re.IGNORECASE)
_DATA_SECTION = re.compile(r"^(FILE|WORKING-STORAGE|LOCAL-STORAGE|LINKAGE)\s+SECTION", re.IGNORECASE)

# READ/WRITE/... name ONE file (or record); OPEN/CLOSE take a LIST per mode:
#   OPEN INPUT POLICY-IN CLAIM-IN OUTPUT REPORT-OUT
_FILE_OP = re.compile(
    B + rf"(READ|WRITE|REWRITE|DELETE|START|RELEASE|RETURN)\s+({ID})", re.IGNORECASE)
_OPEN_CLOSE = re.compile(B + r"(OPEN|CLOSE)\s+((?:\S|(?=(?P<ws>\s+))(?P=ws))*?)(?=\s*(?:$|\.|" + B + r"(?:END-|ELSE|WHEN|IF|MOVE|PERFORM|"
                         r"READ|WRITE|OPEN|CLOSE|CALL|DISPLAY|GO|GOBACK|STOP)" + E + "))",
                         re.IGNORECASE | re.DOTALL)
_OPEN_MODE = re.compile(r"\b(INPUT|OUTPUT|I-O|EXTEND)\b", re.IGNORECASE)

_EXEC_SQL = re.compile(r"\bEXEC\s+SQL\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)
_EXEC_CICS = re.compile(r"\bEXEC\s+CICS\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)
_EXEC_DLI = re.compile(r"\bEXEC\s+DLI\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)

_CICS_PROG = re.compile(r"\b(LINK|XCTL)\b.*?\bPROGRAM\s*\(\s*([^)]*?)\s*\)",
                        re.IGNORECASE | re.DOTALL)
_CICS_VERB = re.compile(r"^\s*([A-Z-]+)", re.IGNORECASE)
_CICS_FILE = re.compile(r"\b(?:FILE|DATASET)\s*\(([^)]*)\)", re.IGNORECASE)          # value stripped by the caller

_DLI_CALL = re.compile(
    rf"\bCALL\s+(['\"])(CBLTDLI|AIBTDLI|PLITDLI)\1\s+USING\b(.*)",
    re.IGNORECASE | re.DOTALL)
_MQ_CALL = re.compile(r"\bCALL\s+['\"](MQ[A-Z0-9]+)['\"]", re.IGNORECASE)

_MOVE_LIT = re.compile(B + rf"MOVE\s+(['\"])([^'\"]*)\1\s+TO\s+([A-Z0-9\-_,\s]+)",
                       re.IGNORECASE)
_SET_VALUE = re.compile(rf"^\s*\d{{1,2}}\s+({ID})\b.*\bVALUE\s+(?:IS\s+)?(['\"])([^'\"]*)\2",
                        re.IGNORECASE)
_LEVEL_ENTRY = re.compile(rf"^\s*(\d{{1,2}})\s+({ID})\b(.*)$", re.IGNORECASE | re.DOTALL)
_SET_TRUE = re.compile(B + rf"SET\s+({ID})\s+TO\s+TRUE" + E, re.IGNORECASE)
# target lists are identifiers separated by whitespace or a comma: with an
# optional separator one word could be split at every character and an
# unterminated `GO TO name MOVE ...` backtracked exponentially (LESSONS 146)
_GO_TO = re.compile(B + r"GO\s+TO\s+(" + ID + r"(?:[\s,]+" + ID + r")*?)(?:[\s,]+DEPENDING\s+(?:ON\s+)?(" + ID + r"))?(?=\s*(?:$|\.|" + B + r"(?:END-|ELSE|WHEN)))",
                    re.IGNORECASE)
MAX_RESOLVED = 40                         # candidate targets kept per dynamic CALL
_ALTER = re.compile(B + rf"ALTER\s+({ID})\s+TO\s+(?:PROCEED\s+TO\s+)?({ID})", re.IGNORECASE)
_SORT_PROC = re.compile(B + rf"(INPUT|OUTPUT)\s+PROCEDURE\s+(?:IS\s+)?({ID})(?:\s+(?:THRU|THROUGH)\s+({ID}))?",
                        re.IGNORECASE)
_SORT_USING = re.compile(B + r"(USING|GIVING)\s+(" + ID + r"(?:[\s,]+" + ID + r")*?)(?=\s*(?:$|\.|" + B + r"(?:USING|GIVING|INPUT|OUTPUT|ON|WITH|COLLATING|END-)))",
                         re.IGNORECASE)
_SECTION_HDR = re.compile(rf"^({ID})\s+SECTION(?:\s+\d{{1,2}})?\s*\.", re.IGNORECASE)

# PERFORM X [OF/IN SECTION] [THRU Y [OF/IN SECTION]] - the qualifier matters
# when the same paragraph name lives in two sections.
_PERFORM = re.compile(
    B + rf"PERFORM\s+({ID})(?:\s+(?:OF|IN)\s+({ID}))?(?:\s+(?:THRU|THROUGH)\s+({ID})(?:\s+(?:OF|IN)\s+({ID}))?)?",
    re.IGNORECASE)
_QUALIFIED_TARGET = re.compile(rf"({ID})(?:\s+(?:OF|IN)\s+({ID}))?", re.IGNORECASE)

# Single-word statements that end in a period and sit in Area A after a
# paragraph header on the same line (`1000-EXIT.  EXIT.`). Without this list
# they get indexed as paragraphs called EXIT / GOBACK.
_NOT_PARAGRAPHS = {"EXIT", "GOBACK", "CONTINUE", "STOP", "END-IF", "END-EVALUATE",
                   "END-PERFORM", "END-READ", "END-CALL", "ELSE", "END-EXEC"}

_DLI_FUNCS = {"GU", "GHU", "GN", "GHN", "GNP", "GHNP", "ISRT", "REPL", "DLET",
              "CHKP", "XRST", "ROLB", "ROLL", "ROLS", "PCB", "TERM", "SYNC", "INIT",
              "GSCD", "LOG", "STAT", "APSB", "DPSB",
              # IMS DC / system services: CHNG+ISRT on an alternate PCB is a
              # message switch - the only way some transactions are reached.
              "CHNG", "PURG", "AUTH", "SETS", "SETU", "INQY", "GMSG", "ICMD", "RCMD",
              "CMD", "SNAP", "POS", "DEQ"}
# Older programs pass a parameter count first: CALL 'CBLTDLI' USING PARM-CT GU PCB ...
_PARMCOUNT_NAME = re.compile(r"^(?:PARM|PARMS|PARMCOUNT|PARM-COUNT|PARM-CT|PARMCT|PARM-CNT|PARM-NBR|"
                             r".*-PARM-COUNT|.*-PARMCT|.*-PARM-CT|.*-PARM-CNT|.*-NBR-PARMS?)$", re.IGNORECASE)
_ENTRY = re.compile(B + r"ENTRY\s+(['\"])([^'\"]+)\1(?:\s+USING\b(.*))?", re.IGNORECASE | re.DOTALL)
_SQL_CALL = re.compile(r"^\s*CALL\s+(?:([A-Z0-9_$#@]+)\.)?([A-Z0-9_$#@]+)\s*(?:\(|$)", re.IGNORECASE)
_SQL_DECLARE_TABLE = re.compile(r"\bDECLARE\s+([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+)?)\s+TABLE\s*\((.*)\)\s*$",
                                re.IGNORECASE | re.DOTALL)
_EXEC_ANY = re.compile(r"\bEXEC\s+(CICS|DLI|SQL)\b(.*?)\bEND-EXEC\b", re.IGNORECASE | re.DOTALL)
_CICS_OPT = re.compile(r"\b([A-Z][A-Z0-9]*)\s*\(([^()]*)\)", re.IGNORECASE)          # value stripped by the callers
# SET(ADDRESS OF x): locate mode - x is the area the command fills. ADDRESS
# is reserved and OF would eat x as a qualifier, so _operands saw nothing.
_CICS_SET_ADDR = re.compile(r"^\s*ADDRESS\s+OF\s+", re.IGNORECASE)
# EXEC CICS options that RETURN data to the program (the program WRITES the
# field) vs. options the command READS.
_CICS_WRITE_OPTS = {"INTO", "SET", "RESP", "RESP2", "NUMITEMS", "NUMREC", "ASSIGN", "ABCODE", "TERMID",
                    "EIBRESP", "DATE", "TIME", "ABSTIME", "RETRIEVE"}
_CICS_READ_OPTS = {"FROM", "RIDFLD", "COMMAREA", "QUEUE", "KEYLENGTH", "LENGTH", "FLENGTH", "PROGRAM",
                   "TRANSID", "MAP", "MAPSET", "FILE", "DATASET", "CHANNEL", "CONTAINER", "SYSID", "ITEM",
                   "REQID", "TERMID", "FROMLENGTH", "CURSOR", "INTERVAL", "TIME", "QNAME"}
_CICS_NOT_FIELDS = {"ERASE", "FREEKB", "ALARM", "NOHANDLE", "MAPONLY", "DATAONLY", "ASIS", "MAIN",
                    "AUXILIARY", "REWRITE", "UPDATE", "EQUAL", "GTEQ", "TD", "TS", "WAIT", "LAST",
                    "TRANSACTION", "IMMEDIATE", "NOCHECK", "PROTECT", "SYNCONRETURN", "NOTRUNCATE",
                    "ANYKEY", "CTLCHAR", "STRFIELD", "CURSOR"}

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
class ArgFact:
    """One CALL argument as written: its position, how it is passed
    (reference | content | value | length_of | address_of | commarea |
    start_from), and the name with its OF/IN chain and subscript. A literal
    argument keeps its position with name None."""
    pos: int
    name: Optional[str]
    quals: List[str] = dc_field(default_factory=list)
    sub: Optional[str] = None
    how: str = "reference"


@dataclass
class CallFact:
    kind: str               # static | dynamic | cics_link | cics_xctl | cancel
    target: Optional[str]
    via_var: Optional[str]
    resolved: List[str]
    resolution: str         # literal | move_literal | value_clause | unresolved
    using_args: List[str]
    line: int
    args: List[ArgFact] = dc_field(default_factory=list)    # USING detail; COMMAREA / START FROM for CICS
    returning: Optional[str] = None                          # CALL ... RETURNING x


@dataclass
class Operand:
    """One operand of a data-moving verb: a data-name with its OF/IN chain
    (as written, outermost last), subscript and ref-mod text, or a literal /
    figurative constant (`lit`), or an identifier inside FUNCTION f(...)
    (`func` = f). A subscript name is never an operand of its own."""
    name: Optional[str] = None
    quals: List[str] = dc_field(default_factory=list)
    sub: Optional[str] = None
    refmod: Optional[str] = None
    lit: Optional[str] = None
    func: Optional[str] = None


@dataclass
class FlowFact:
    """One (source, target) pair the compiler moves data between - a data_flow
    row. `line` is the verb's expanded line as field_refs stores it; `kind`
    is the verb shape (move | move_corr | literal | figurative | function |
    arith | string | unstring | initialize | set | set_address | accept |
    read_into | write_from | io_in | io_out | inspect | returning | cics_in |
    cics_out | dli_in | dli_out | mq_in | mq_out); `guard` the enclosing IF."""
    line: int
    verb: str
    kind: str
    src_name: Optional[str] = None
    src_qual: Optional[str] = None      # 'REC-A' or 'REC-A OF REC-X', as written
    src_sub: Optional[str] = None
    src_refmod: Optional[str] = None
    src_lit: Optional[str] = None
    dst_name: Optional[str] = None
    dst_qual: Optional[str] = None
    dst_sub: Optional[str] = None
    dst_refmod: Optional[str] = None
    ordinal: int = 0
    note: Optional[str] = None
    guard: Optional[str] = None


@dataclass
class ParagraphFact:
    name: str
    section: Optional[str]
    start_line: int
    end_line: int
    ordinal: int
    kind: str = "paragraph"          # paragraph | section (PERFORM of a section runs all its paragraphs)


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
    pcb_index: Optional[int] = None     # EXEC DLI ... USING PCB(n): the n-th PCB of the PSB
    resolution: Optional[str] = None    # literal | value_clause | parmcount | unresolved
    dest: Optional[str] = None          # CHNG destination (transaction / LTERM) - a message switch


@dataclass
class FileDeclFact:
    select_name: str
    assign_dd: Optional[str]
    organization: Optional[str]
    access_mode: Optional[str]
    record_key: Optional[str]
    alt_keys: List[str]
    fd_record: Optional[str]            # first 01 under the FD - WRITE/REWRITE name THIS, not the file
    line: int
    fd_records: List[str] = dc_field(default_factory=list)   # every 01 under the FD (multi-record files)
    is_sd: bool = False


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
    # (from, to, thru, line, kind) - kind: perform | goto | goto_depending | alter | fallthrough | sort_proc
    performs: List[Tuple[str, str, Optional[str], int, str]] = dc_field(default_factory=list)
    calls: List[CallFact] = dc_field(default_factory=list)
    copies: List[Tuple[str, Optional[str], Optional[str], int]] = dc_field(default_factory=list)
    files: List[FileDeclFact] = dc_field(default_factory=list)
    io_ops: List[Tuple[str, str, str, int]] = dc_field(default_factory=list)
    sql: List[SqlFact] = dc_field(default_factory=list)
    dli: List[DliFact] = dc_field(default_factory=list)
    cics: List[Tuple[str, str, int]] = dc_field(default_factory=list)
    # (verb, resource kind map|tdq|tsq|container|webservice|transid|file|program, resource, direction, line)
    cics_cmds: List[Tuple[str, str, str, Optional[str], int]] = dc_field(default_factory=list)
    # (call, queue name or None, direction in|out|None, message layout 01 or None, line)
    mq: List[Tuple[str, Optional[str], Optional[str], Optional[str], int]] = dc_field(default_factory=list)
    mq_handles: Dict[str, str] = dc_field(default_factory=dict)       # HOBJ variable -> MQOD structure
    entries: List[Tuple[str, List[str], int]] = dc_field(default_factory=list)   # ENTRY 'name' USING ...
    # DCLGEN / DECLARE TABLE: table -> [(column, type)] in declared order
    declared_tables: Dict[str, List[Tuple[str, str]]] = dc_field(default_factory=dict)
    # SELECT * / SELECT a,b,c INTO :GROUP - resolved against the group's children by build
    sql_group_intos: List[Tuple[str, List[Tuple[str, Optional[str]]], List[str], str, int]] = dc_field(default_factory=list)
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
    # (table, column, host_var, mode read|write|predicate, stmt, line) - column-level DB2 lineage
    sql_cols: List[Tuple[str, str, Optional[str], str, str, int]] = dc_field(default_factory=list)
    # cursor name -> (tables/aliases, select columns) so FETCH INTO can be paired positionally
    cursors: Dict[str, Tuple[List[Tuple[str, Optional[str]]], List[str]]] = dc_field(default_factory=dict)
    unresolved: List[Tuple[str, str, int]] = dc_field(default_factory=list)
    # value flow: one row per (source, target) a verb moves data between
    flows: List[FlowFact] = dc_field(default_factory=list)
    # (expanded line, FILE|WORKING-STORAGE|LOCAL-STORAGE|LINKAGE, FD/SD name or None) - every section header and FD
    section_marks: List[Tuple[int, str, Optional[str]]] = dc_field(default_factory=list)
    returning: Optional[str] = None                                    # PROCEDURE DIVISION ... RETURNING y


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
    cur_fd: Optional[FileDeclFact] = None        # the FD/SD whose 01 records come next
    last_stmt: Dict[str, str] = {}               # paragraph -> its last statement (fall-through)

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
                f.returning = _returning_of(body)
            if para_open:
                para_open.end_line = st.start - 1
                para_open = None
            continue

        # ---- paragraph / section headers (Area A only) --------------------
        if current_division == "PROCEDURE" and st.area_a:
            ms = _SECTION_HDR.match(body)
            if ms:
                current_section = ms.group(1).upper()
                if para_open:
                    para_open.end_line = st.start - 1
                # A SECTION is a PERFORM target too: `PERFORM 1000-PROCESS`
                # runs every paragraph under it.
                ordinal += 1
                para_open = ParagraphFact(name=current_section, section=current_section,
                                          start_line=st.start, end_line=st.end, ordinal=ordinal,
                                          kind="section")
                f.paragraphs.append(para_open)
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
        if para_open is not None and current_division == "PROCEDURE":
            last_stmt[para_open.name] = up

        _extract_copy(f, st)
        if current_division in ("ENVIRONMENT", None):
            _extract_file_decl(f, st, body)
        if current_division == "DATA":
            fd = _extract_fd(f, st, body)
            msec = _DATA_SECTION.match(up)
            if msec:
                # where each 01 lives: build places every pfield by these marks
                f.section_marks.append((st.start, msec.group(1).upper(), None))
            if fd is not None:
                cur_fd = fd
                f.section_marks.append((st.start, "FILE", fd.select_name))
            elif re.match(r"^(?:WORKING-STORAGE|LINKAGE|LOCAL-STORAGE)\s+SECTION", up):
                cur_fd = None
            elif cur_fd is not None:
                ml = _LEVEL_ENTRY.match(body)
                if ml and ml.group(1) == "01":
                    # The 01s under an FD are its records: WRITE names these.
                    cur_fd.fd_records.append(ml.group(2).upper())
                    if cur_fd.fd_record is None:
                        cur_fd.fd_record = ml.group(2).upper()
        guards: List[Tuple[int, Optional[str]]] = []
        if current_division == "PROCEDURE":
            _extract_entry(f, st)
            _extract_calls(f, st, literal_map)
            _extract_performs(f, st, here)
            _extract_file_ops(f, st)
            guards = _extract_field_and_literal_refs(f, st)
        elif current_division in ("DATA", None):
            _extract_value_literals(f, st)
        _extract_sql(f, st)
        _extract_cics(f, st, literal_map, guards)
        _extract_dli(f, st, literal_map, guards)
        _extract_mq(f, st, literal_map, guards)

    if para_open:
        para_open.end_line = len(lines)
    # A section spans every paragraph under it.
    for sec in f.paragraphs:
        if sec.kind == "section":
            ends = [p.end_line for p in f.paragraphs if p.kind == "paragraph" and p.section == sec.name]
            if ends:
                sec.end_line = max(ends)
    _fallthrough_edges(f, last_stmt)

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
    # 88 name -> (parent field, its literals): `SET WS-PGM-SUBA TO TRUE` then
    # `CALL WS-PGM` is statically knowable.
    conds: Dict[str, Tuple[str, List[str]]] = {}
    last_field: Optional[str] = None

    for st in stmts:
        body = st.text

        ml = _LEVEL_ENTRY.match(body)
        if ml:
            if ml.group(1) == "88":
                if last_field:
                    lits = [l.strip("'\"").upper() for l in _ALL_LITS.findall(ml.group(3) or "")
                            if l[:1] in ("'", '"')]
                    conds[ml.group(2).upper()] = (last_field, lits)
            else:
                last_field = ml.group(2).upper()

        mv = _SET_VALUE.match(body)
        if mv:
            out.setdefault(mv.group(1).upper(), set()).add(mv.group(3).strip().upper())

        for ms in _SET_TRUE.finditer(body):
            c = conds.get(ms.group(1).upper())
            if c:
                out.setdefault(c[0], set()).update(c[1])

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
    sorted_cands: Dict[str, List[str]] = {}
    # EXEC SQL/CICS/DLI text is not COBOL: `EXEC SQL CALL PROC(:X)` is a
    # stored-procedure call (recorded by _extract_sql), not a dynamic CALL.
    body = _EXEC_ANY.sub(lambda m: " " * len(m.group(0)), st.text)

    # DL/I and MQ calls are CALLs syntactically but are handled elsewhere.
    if _DLI_CALL.search(body) or _MQ_CALL.search(body):
        return

    # One sentence may hold several CALLs (IF ... CALL 'A' ... ELSE CALL WS-B
    # ... END-IF): every fragment is its own call with its own USING list.
    for verb, frag, off in _split_verbs(body):
        if verb != "CALL":
            continue
        line = st.line_at(off)
        using = _parse_using("CALL " + frag)
        args, returning = _parse_using_detail("CALL " + frag)
        ml = re.match(r"\s*(['\"])([^'\"]+)\1", frag)
        if ml:
            target = ml.group(2).strip().upper()
            f.calls.append(CallFact(kind="static", target=target, via_var=None,
                                    resolved=[target], resolution="literal",
                                    using_args=using, line=line, args=args, returning=returning))
            continue
        mv = re.match(rf"\s*({ID})", frag, re.IGNORECASE)
        if mv:
            var = mv.group(1).upper()
            cands = sorted_cands.get(var)
            if cands is None:
                cands = sorted(literal_map.get(var, []))
                if len(cands) > MAX_RESOLVED:          # a dispatcher moving 5,000 names to one variable
                    cands = cands[:MAX_RESOLVED] + [f"(+{len(cands) - MAX_RESOLVED} more)"]
                sorted_cands[var] = cands
            f.calls.append(CallFact(
                kind="dynamic", target=None, via_var=var,
                resolved=cands,
                resolution="move_literal" if cands else "unresolved",
                using_args=using, line=line, args=args, returning=returning))
            if not cands:
                f.unresolved.append((
                    "dynamic_call",
                    f"CALL {var} - target never assigned a literal in this program; "
                    f"it may come from a control card, a DB2 table, or LINKAGE",
                    line))

    for m in _CANCEL.finditer(body):
        f.calls.append(CallFact(kind="cancel", target=m.group(2).upper(),
                                via_var=None, resolved=[m.group(2).upper()],
                                resolution="literal", using_args=[], line=st.start))


_USING_STOP = re.compile(r"\bEND-[A-Z]+\b|\bELSE\b|\bWHEN\b|\bON\s+(?:EXCEPTION|OVERFLOW|SIZE)\b|"
                         r"\bNOT\s+ON\b|\bRETURNING\b|\bGIVING\b|" + B + r"(?:MOVE|PERFORM|IF|CALL|GO|DISPLAY|"
                         r"COMPUTE|ADD|SUBTRACT|EVALUATE|SET|READ|WRITE|OPEN|CLOSE|GOBACK|STOP|EXIT|CONTINUE)" + E,
                         re.IGNORECASE)


def _parse_using(body: str) -> List[str]:
    """Positional USING list: BY REFERENCE/CONTENT/VALUE dropped, LENGTH OF x
    and ADDRESS OF x reduced to x, subscripts stripped, `A OF B` -> A, and the
    list cut at the next verb / END-xxx / ELSE (a USING list never runs into
    the ELSE branch of the IF that contains it)."""
    m = _USING.search(body)
    if not m:
        return []
    raw = m.group(1)
    raw = re.split(r"\bEND-CALL\b|\.(?=\s|$)", raw)[0]
    ms = _USING_STOP.search(raw)
    if ms:
        raw = raw[:ms.start()]
    raw = re.sub(r"\b(?:LENGTH|ADDRESS)\s+OF\s+", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(?:OF|IN)\s+" + ID, " ", raw, flags=re.IGNORECASE)     # qualifier, not an argument
    raw = re.sub(r"\([^)]*\)", " ", raw)                                       # subscripts
    out = []
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip().upper()
        if not tok or tok in ("BY", "REFERENCE", "CONTENT", "VALUE", "OF", "IN", "USING"):
            continue
        if re.fullmatch(ID, tok, re.IGNORECASE):
            out.append(tok)
    return out


_RETURNING = re.compile(r"\bRETURNING\s+([A-Z0-9][A-Z0-9\-]*)", re.IGNORECASE)


def _returning_of(body: str) -> Optional[str]:
    """`... RETURNING x` on a CALL or on the PROCEDURE DIVISION header."""
    m = _RETURNING.search(body)
    return m.group(1).upper() if m else None


def _parse_using_detail(body: str) -> Tuple[List[ArgFact], Optional[str]]:
    """The USING list with HOW each argument is passed: BY REFERENCE (the
    default) / CONTENT / VALUE carries to the arguments after it, `LENGTH OF x`
    -> length_of and `ADDRESS OF x` -> address_of for that one argument, `A OF
    B` keeps its qualifier chain, a subscript stays with its argument, a
    literal or OMITTED keeps its position with name None. Same span and cut
    as _parse_using, which still feeds using_args. Returns (args, RETURNING)."""
    m = _USING.search(body)
    if not m:
        return [], _returning_of(body)
    raw = m.group(1)
    raw = re.split(r"\bEND-CALL\b|\.(?=\s|$)", raw)[0]
    ms = _USING_STOP.search(raw)
    if ms:
        raw = raw[:ms.start()]
    args: List[ArgFact] = []
    how = "reference"
    one_shot: Optional[str] = None                     # LENGTH OF / ADDRESS OF: this argument only
    toks = list(_OPTOKEN.finditer(raw))
    i = 0
    while i < len(toks):
        t = toks[i].group(0)
        i += 1
        if t[0] in ("'", '"'):
            args.append(ArgFact(pos=len(args) + 1, name=None, how=one_shot or how))
            one_shot = None
            continue
        head, groups = _split_groups(t)
        up = head.upper()
        if up == "BY":
            continue
        if up in ("REFERENCE", "CONTENT", "VALUE"):
            how = up.lower()
            continue
        if up in ("LENGTH", "ADDRESS") and i < len(toks) and toks[i].group(0).upper() == "OF":
            one_shot = "length_of" if up == "LENGTH" else "address_of"
            i += 1
            continue
        if up in ("OF", "IN"):
            # qualifier of the argument before it; a subscript written after the qualifier is the argument's
            if i < len(toks) and args and args[-1].name:
                qh, qg = _split_groups(toks[i].group(0))
                i += 1
                args[-1].quals.append(qh.upper())
                if qg and not args[-1].sub:
                    args[-1].sub = qg[0]
            continue
        if up == "OMITTED" or re.fullmatch(r"[+\-\d.]+", up):
            args.append(ArgFact(pos=len(args) + 1, name=None, how=one_shot or how))
            one_shot = None
            continue
        if not re.fullmatch(ID, up, re.IGNORECASE):
            continue
        args.append(ArgFact(pos=len(args) + 1, name=up, sub=groups[0] if groups else None, how=one_shot or how))
        one_shot = None
    return args, _returning_of(body)


# --------------------------------------------------------------------------
# other extractors
# --------------------------------------------------------------------------

_LIT_MASK = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def _extract_copy(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _SQL_INCLUDE.finditer(st.text):
        f.copies.append((m.group(1).upper(), None, None, st.start))
    # literals blanked, same length: DISPLAY 'COPY FAILED' is text, not a COPY - but the name itself may be a
    # literal (COPY 'NAME'), so the statement is matched on the text and checked against the mask
    masked = _LIT_MASK.sub(lambda mm: " " * len(mm.group(0)), st.text)
    pos = 0
    while True:
        m = _COPY.search(st.text, pos)
        if not m:
            break
        pos = m.end()
        if masked[m.start():m.start() + 4].upper() != st.text[m.start():m.start() + 4].upper() or (
                not m.group("q") and masked[m.start("name"):m.end("name")].upper() != m.group("name").upper()):
            pos = m.start() + 4                                    # the keyword or the bare name sits inside a literal:
            continue                                               # look again right after it (its REPLACING tail may
                                                                   # have swallowed a real COPY later in the sentence)
        rep = m.group("rep")
        f.copies.append((m.group("name").upper(),
                         m.group("lib").upper() if m.group("lib") else None,
                         rep.strip() if rep else None,
                         st.start))
        if rep:
            f.unresolved.append((
                "copy_replacing",
                f"COPY {m.group('name').upper()} REPLACING - field names in this "
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


def _extract_fd(f: ProgramFacts, st: LogicalLine, body: str) -> Optional[FileDeclFact]:
    """`FD name` / `SD name`: returns the file so the 01s that follow can be
    attached to it as its record(s)."""
    m = _FD.match(body)
    if not m:
        return None
    name = m.group(2).upper()
    for fd in f.files:
        if fd.select_name == name:
            fd.is_sd = m.group(1).upper() == "SD"
            return fd
    fd = FileDeclFact(select_name=name, assign_dd=None, organization="SORT" if m.group(1).upper() == "SD" else None,
                      access_mode=None, record_key=None, alt_keys=[], fd_record=None, line=st.start,
                      is_sd=m.group(1).upper() == "SD")
    f.files.append(fd)
    return fd


def _record_of(f: ProgramFacts, name: str) -> Optional[FileDeclFact]:
    """The file whose 01 record is `name` (WRITE/REWRITE/RELEASE name the record)."""
    up = name.upper()
    for fd in f.files:
        if up in fd.fd_records:
            return fd
    return None


def _extract_file_ops(f: ProgramFacts, st: LogicalLine) -> None:
    body = _EXEC_ANY.sub(lambda m: " " * len(m.group(0)), st.text)
    for m in _OPEN_CLOSE.finditer(body):
        verb = m.group(1).upper()
        mode = None
        for tok in re.findall(r"[A-Z0-9][A-Z0-9\-]*", m.group(2), re.IGNORECASE):
            up = tok.upper()
            if _OPEN_MODE.fullmatch(up):
                mode = up
            elif up in ("REVERSED", "WITH", "NO", "REWIND", "LOCK", "FOR", "REMOVAL"):
                continue
            elif verb == "OPEN" and mode:
                f.io_ops.append((up, "file", f"OPEN {mode}", st.line_at(m.start())))
            elif verb == "CLOSE":
                f.io_ops.append((up, "file", "CLOSE", st.line_at(m.start())))
    for m in _FILE_OP.finditer(body):
        op = m.group(1).upper()
        name = m.group(2).upper()
        if op in ("WRITE", "REWRITE", "RELEASE"):
            # These name the RECORD; the fact is about the FILE that owns it.
            fd = _record_of(f, name)
            f.io_ops.append((fd.select_name if fd else name, "file", op, st.line_at(m.start())))
        else:
            f.io_ops.append((name, "file", op, st.line_at(m.start())))
    # SORT/MERGE file USING a GIVING b: the sort reads a and writes b.
    if re.search(B + r"(?:SORT|MERGE)" + E, body, re.IGNORECASE):
        for m in _SORT_USING.finditer(body):
            op = "READ" if m.group(1).upper() == "USING" else "WRITE"
            for tok in re.findall(ID, m.group(2), re.IGNORECASE):
                f.io_ops.append((tok.upper(), "file", f"SORT {op}", st.line_at(m.start())))


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


# ---- column-level lineage --------------------------------------------------

_SQL_CLAUSE_END = r"(?:\s+WHERE\b|\s+GROUP\s+BY\b|\s+ORDER\s+BY\b|\s+FOR\b|\s+WITH\b|\s+FETCH\s+FIRST\b|\s+OPTIMIZE\b|$)"
_SQL_SELECT_INTO = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?(.*?)\s+INTO\s+(.*?)\s+FROM\s+(.*?)" + _SQL_CLAUSE_END,
                              re.IGNORECASE | re.S)
_SQL_DECLARE_CUR = re.compile(r"\bDECLARE\s+([A-Z0-9_\-]+)\s+(?:[A-Z]+\s+)*?CURSOR\b.*?\bFOR\s+SELECT\s+(?:DISTINCT\s+)?(.*?)\s+FROM\s+(.*?)"
                              + _SQL_CLAUSE_END, re.IGNORECASE | re.S)
# FETCH [NEXT|PRIOR|...] [ROWSET] [FROM] cursor [FOR n ROWS] INTO :a, :b
_SQL_FETCH = re.compile(r"\bFETCH\s+(?:(?:NEXT|PRIOR|FIRST|LAST|CURRENT|BEFORE|AFTER|ROWSET|FROM|ABSOLUTE|RELATIVE|"
                        r"STARTING|AT|[+-]?\d+)\s+)*([A-Z0-9_\-]+)\s+(?:FOR\s+\S+\s+ROWS\s+)?INTO\s+(.*)$",
                        re.IGNORECASE | re.S)
_SQL_UPDATE = re.compile(r"\bUPDATE\s+([A-Z0-9_$#@.]+)(?:\s+(?:AS\s+)?(?!SET\b)([A-Z][A-Z0-9_]*))?\s+SET\s+(.*?)(?:\s+WHERE\s+(.*))?$",
                         re.IGNORECASE | re.S)
_SQL_WHERE = re.compile(r"\bWHERE\s+(.*?)" + _SQL_CLAUSE_END.replace(r"\s+WHERE\b|", ""), re.IGNORECASE | re.S)
_SQL_PRED_HV = re.compile(r"\b([A-Z0-9_$#@]+(?:\.[A-Z0-9_$#@]+)?)\s*(?:=|<>|!=|>=|<=|>|<|\bLIKE\b|\bIN\s*\()\s*:([A-Z0-9\-_]+)",
                          re.IGNORECASE)
_SQL_DELETE_FROM = re.compile(r"\bDELETE\s+FROM\s+([A-Z0-9_$#@.]+)(?:\s+(?:AS\s+)?(?!WHERE\b)([A-Z][A-Z0-9_]*))?", re.IGNORECASE)
_SQL_HV_FIRST = re.compile(r":([A-Z0-9\-_]+)", re.IGNORECASE)
_SQL_IDENT = re.compile(r"^([A-Z0-9_$#@]+)(?:\.([A-Z0-9_$#@]+))?$", re.IGNORECASE)


def _split_top(s: str) -> List[str]:
    out, buf, depth = [], [], 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [x for x in out if x]


def _from_tables(from_clause: str) -> List[Tuple[str, Optional[str]]]:
    """'POLICY_TBL P INNER JOIN MEMBER_TBL M ON ...' -> [(POLICY_TBL,P),(MEMBER_TBL,M)]"""
    out: List[Tuple[str, Optional[str]]] = []
    parts = re.split(r"\s*,\s*|\s+(?:INNER\s+|LEFT\s+(?:OUTER\s+)?|RIGHT\s+(?:OUTER\s+)?|FULL\s+(?:OUTER\s+)?)?JOIN\s+",
                     from_clause, flags=re.IGNORECASE)
    for p in parts:
        p = re.split(r"\s+ON\s+", p, flags=re.IGNORECASE)[0].strip()
        toks = p.split()
        if not toks:
            continue
        tbl = toks[0].upper()
        alias = None
        if len(toks) >= 2:
            a = toks[-1].upper()
            if a not in ("AS",) and re.fullmatch(r"[A-Z][A-Z0-9_]*", a):
                alias = a
        out.append((tbl, alias))
    return out


def _col(expr: str, tables: List[Tuple[str, Optional[str]]]) -> Optional[Tuple[str, str]]:
    """A plain column reference -> (table, column); functions/literals -> None."""
    m = _SQL_IDENT.match(expr.strip())
    if not m:
        return None
    a, b = m.group(1).upper(), (m.group(2) or "").upper()
    if b:                                   # qualified: alias or table name
        for tbl, alias in tables:
            if a in (alias, tbl):
                return tbl, b
        return a, b
    if a in ("NULL", "CURRENT", "USER", "DEFAULT"):
        return None
    if len(tables) == 1:
        return tables[0][0], a
    return ("?" if tables else "?"), a


def _hv_list(s: str) -> List[Optional[str]]:
    """':A :A-IND, :B' -> ['A', 'B'] - the first host var per comma-separated part."""
    out: List[Optional[str]] = []
    for part in _split_top(s):
        m = _SQL_HV_FIRST.search(part)
        out.append(m.group(1).upper() if m else None)
    return out


def _predicates(f: ProgramFacts, where: str, tables, stmt: str, ln: int) -> None:
    for m in _SQL_PRED_HV.finditer(where or ""):
        c = _col(m.group(1), tables)
        if c:
            f.sql_cols.append((c[0], c[1], m.group(2).upper(), "predicate", stmt, ln))


def _sql_columns(f: ProgramFacts, inner: str, verb: str, ln: int) -> None:
    """Pair DB2 columns with COBOL host variables, positionally where SQL is
    positional (SELECT list <-> INTO list, INSERT columns <-> VALUES, cursor
    select list <-> FETCH INTO) and by name for SET and predicates."""
    v = verb.upper()
    if v == "SELECT":
        m = _SQL_SELECT_INTO.search(inner)
        if m:
            tables = _from_tables(m.group(3))
            cols, hvs = _split_top(m.group(1)), _hv_list(m.group(2))
            if len(hvs) == 1 and hvs[0] and (len(cols) > 1 or cols == ["*"]):
                # SELECT * INTO :DCLPOLICY / SELECT a,b,c INTO :GROUP - the
                # columns land in the group's children positionally; build
                # resolves them against the field tree.
                f.sql_group_intos.append((hvs[0], tables, [c.strip() for c in cols], "SELECT", ln))
            else:
                for c, h in zip(cols, hvs):
                    cc = _col(c, tables)
                    if cc and h:
                        f.sql_cols.append((cc[0], cc[1], h, "read", "SELECT", ln))
            mw = _SQL_WHERE.search(inner)
            if mw:
                _predicates(f, mw.group(1), tables, "SELECT", ln)
    elif v == "DECLARE":
        m = _SQL_DECLARE_CUR.search(inner)
        if m:
            tables = _from_tables(m.group(3))
            f.cursors[m.group(1).upper()] = (tables, _split_top(m.group(2)))
            mw = _SQL_WHERE.search(inner)
            if mw:
                _predicates(f, mw.group(1), tables, "DECLARE", ln)
    elif v == "FETCH":
        m = _SQL_FETCH.search(inner)
        if m:
            cur = m.group(1).upper()
            if cur in f.cursors:
                tables, cols = f.cursors[cur]
                hvs = _hv_list(m.group(2))
                if len(hvs) == 1 and hvs[0] and (len(cols) > 1 or cols == ["*"]):
                    f.sql_group_intos.append((hvs[0], tables, [c.strip() for c in cols], "FETCH", ln))
                else:
                    for c, h in zip(cols, hvs):
                        cc = _col(c, tables)
                        if cc and h:
                            f.sql_cols.append((cc[0], cc[1], h, "read", "FETCH", ln))
            else:
                f.unresolved.append(("sql_cursor", f"FETCH {cur}: cursor not declared in this program", ln))
    elif v == "INSERT":
        m = _SQL_INSERT.search(inner)
        if m:
            tbl = re.search(r"\bINSERT\s+INTO\s+([A-Z0-9_$#@.]+)", inner, re.IGNORECASE).group(1).upper()
            cols = [c.strip().upper() for c in m.group(1).split(",")]
            vals = _split_top(m.group(2))
            for c, val in zip(cols, vals):
                hv = _SQL_HV_FIRST.search(val)
                if hv:
                    f.sql_cols.append((tbl, c, hv.group(1).upper(), "write", "INSERT", ln))
    elif v == "UPDATE":
        m = _SQL_UPDATE.search(inner)
        if m:
            tables = [(m.group(1).upper(), (m.group(2) or "").upper() or None)]
            for assign in _split_top(m.group(3)):
                if "=" in assign:
                    c, _, rhs = assign.partition("=")
                    cc = _col(c, tables)
                    hv = _SQL_HV_FIRST.search(rhs)
                    if cc and hv:
                        f.sql_cols.append((cc[0], cc[1], hv.group(1).upper(), "write", "UPDATE", ln))
            if m.group(4):
                _predicates(f, m.group(4), tables, "UPDATE", ln)
    elif v == "DELETE":
        m = _SQL_DELETE_FROM.search(inner)
        if m:
            tables = [(m.group(1).upper(), (m.group(2) or "").upper() or None)]
            mw = _SQL_WHERE.search(inner)
            if mw:
                _predicates(f, mw.group(1), tables, "DELETE", ln)


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


def _parse_declared_columns(body: str) -> List[Tuple[str, str]]:
    """`POL_NO CHAR(12) NOT NULL, STATUS_CD CHAR(2), PREM_AMT DECIMAL(11,2)`
    -> [(POL_NO, CHAR(12) NOT NULL), ...] in declared order."""
    out: List[Tuple[str, str]] = []
    for item in _split_top(body):
        toks = item.strip().split(None, 1)
        if not toks or not re.fullmatch(r"[A-Z0-9_$#@]+", toks[0], re.IGNORECASE):
            continue
        out.append((toks[0].upper(), (toks[1] if len(toks) > 1 else "").strip().upper()))
    return out


def _extract_sql(f: ProgramFacts, st: LogicalLine) -> None:
    for m in _EXEC_SQL.finditer(st.text):
        inner = " ".join(m.group(1).split())
        verb = next((v for v in _SQL_VERBS
                     if re.match(rf"^{v}\b", inner, re.IGNORECASE)), "OTHER")
        if verb == "INCLUDE":
            continue                     # handled as a copy in _extract_copy
        cur = _SQL_CURSOR.search(inner)
        tables = sorted({t.upper() for t in _SQL_TABLE.findall(inner)})
        if verb == "FETCH":
            tables = []                  # `FETCH ... FROM cursor` names a CURSOR, not a table
        if verb == "DECLARE":
            mt = _SQL_DECLARE_TABLE.search(inner)
            if mt:
                # DCLGEN: the positional column list behind SELECT * / :GROUP
                f.declared_tables[mt.group(1).upper()] = _parse_declared_columns(mt.group(2))
                tables = []
        if verb == "CALL":
            mc = _SQL_CALL.match(inner)
            if mc:
                # A DB2 stored procedure - in a COBOL shop usually a COBOL
                # program of the estate, reached through DB2 rather than CALL.
                proc = mc.group(2).upper()
                f.calls.append(CallFact(kind="sql_call", target=proc, via_var=None, resolved=[proc],
                                        resolution="literal",
                                        using_args=[h.upper() for h in _SQL_HOSTVAR.findall(inner)],
                                        line=st.start))
                f.io_ops.append((proc, "db2", "CALL", st.start))
                tables = []
        hvars = sorted({h.upper() for h in _SQL_HOSTVAR.findall(inner)})
        _sql_host_modes(f, inner, st.start)
        _sql_literals(f, inner, verb.upper(), st.start)
        _sql_columns(f, inner, verb.upper(), st.start)
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


def _cics_value(arg: str, literal_map: Dict[str, Set[str]]) -> Tuple[Optional[str], List[str], str]:
    """(literal value or None, candidates, resolution) for a CICS option
    argument: 'MEMMAP' is literal; WS-NEXT-TRAN resolves through the
    VALUE / MOVE literal map like a dynamic CALL."""
    a = arg.strip()
    lit = re.fullmatch(r"['\"]([^'\"]+)['\"]", a)
    if lit:
        return lit.group(1).strip().upper(), [lit.group(1).strip().upper()], "literal"
    cands = sorted(literal_map.get(a.upper(), []))
    if len(cands) == 1:
        return cands[0], cands, "move_literal"
    return None, cands, ("move_literal" if cands else "unresolved")


def _extract_cics(f: ProgramFacts, st: LogicalLine,
                  literal_map: Dict[str, Set[str]],
                  guards: Optional[List[Tuple[int, Optional[str]]]] = None) -> None:
    for m in _EXEC_CICS.finditer(st.text):
        inner = " ".join(m.group(1).split())
        ln = st.line_at(m.start())
        guard = _guard_at(guards, m.start())
        verb = _CICS_VERB.match(inner)
        vb = verb.group(1).upper() if verb else "?"
        f.cics.append((vb, inner, ln))
        opts: Dict[str, str] = {}
        for mo in _CICS_OPT.finditer(inner):
            opts.setdefault(mo.group(1).upper(), mo.group(2).strip())
        commarea = _idents(opts["COMMAREA"])[:1] if opts.get("COMMAREA") and opts["COMMAREA"][:1] not in ("'", '"') else []
        arg_how = "commarea"
        if not commarea and vb == "START" and opts.get("FROM") and opts["FROM"][:1] not in ("'", '"'):
            commarea = _idents(opts["FROM"])[:1]          # START TRANSID ... FROM(data): the started task RETRIEVEs it
            arg_how = "start_from"
        # the COMMAREA / START FROM item is the call's one positional argument
        cics_args = [ArgFact(pos=1, name=commarea[0], how=arg_how)] if commarea else []

        mp = _CICS_PROG.search(inner)
        if mp:
            kind = f"cics_{mp.group(1).lower()}"
            val, cands, res = _cics_value(mp.group(2), literal_map)
            # COMMAREA is the positional argument of a LINK/XCTL: the callee
            # sees it as DFHCOMMAREA, so a value flows through it.
            f.calls.append(CallFact(kind=kind, target=val, via_var=None if res == "literal" else mp.group(2).strip().upper(),
                                    resolved=cands, resolution=res, using_args=commarea, line=ln, args=cics_args))
            if res == "unresolved":
                f.unresolved.append((
                    "dynamic_call",
                    f"EXEC CICS {mp.group(1).upper()} PROGRAM({mp.group(2).strip().upper()}) - target "
                    f"not resolvable from this source", ln))
            f.cics_cmds.append((vb, "program", val or mp.group(2).strip().upper(), "out", ln))

        # START TRANSID / RETURN TRANSID: the online call graph written in
        # code. Recorded as call edges to the TRANSACTION CODE; the CSD /
        # stage-1 turns the code into a program.
        if vb in ("START", "RETURN") and opts.get("TRANSID"):
            val, cands, res = _cics_value(opts["TRANSID"], literal_map)
            kind = "cics_start" if vb == "START" else "cics_return"
            f.calls.append(CallFact(kind=kind, target=val, via_var=None if res == "literal" else opts["TRANSID"].upper(),
                                    resolved=cands, resolution=res, using_args=commarea, line=ln, args=cics_args))
            f.cics_cmds.append((vb, "transid", val or opts["TRANSID"].upper(), "out", ln))
            if res == "unresolved":
                f.unresolved.append(("cics_transid", f"EXEC CICS {vb} TRANSID({opts['TRANSID'].upper()}) - "
                                                     f"transaction code not resolvable from this source", ln))

        if opts.get("MAP") or opts.get("MAPSET"):
            mapv, _c, _r = _cics_value(opts.get("MAP", ""), literal_map)
            setv, _c2, _r2 = _cics_value(opts.get("MAPSET", ""), literal_map)
            res_name = ".".join(x for x in (setv or (opts.get("MAPSET") or "").upper() or None,
                                            mapv or (opts.get("MAP") or "").upper() or None) if x)
            f.cics_cmds.append((vb, "map", res_name, "in" if vb == "RECEIVE" else "out" if vb == "SEND" else None, ln))

        mf = _CICS_FILE.search(inner)
        if mf and verb:
            val, cands, res = _cics_value(mf.group(1).strip(), literal_map)
            name = val or mf.group(1).strip().strip("'\"").upper()
            f.io_ops.append((name, "cics", vb, ln))
            f.cics_cmds.append((vb, "file", name, None, ln))
            if res == "unresolved":
                f.unresolved.append(("cics_file_var", f"EXEC CICS {vb} FILE({mf.group(1).strip().upper()}) - "
                                                      f"FCT name not resolvable from this source", ln))

        if opts.get("QUEUE"):
            qkind = "tdq" if re.search(r"\bTD\b", inner, re.IGNORECASE) else "tsq" if re.search(r"\bTS\b", inner, re.IGNORECASE) else "queue"
            val, _c, _r = _cics_value(opts["QUEUE"], literal_map)
            direction = "out" if vb.startswith("WRITEQ") else "in" if vb.startswith("READQ") else "delete" if vb.startswith("DELETEQ") else None
            name = val or opts["QUEUE"].upper()
            f.cics_cmds.append((vb, qkind, name, direction, ln))
            f.io_ops.append((name, "cics_" + qkind, vb, ln))

        if opts.get("CONTAINER"):
            val, _c, _r = _cics_value(opts["CONTAINER"], literal_map)
            chan, _c2, _r2 = _cics_value(opts.get("CHANNEL", ""), literal_map)
            f.cics_cmds.append((vb, "container", f"{chan or ''}/{val or opts['CONTAINER'].upper()}".lstrip("/"),
                                "out" if vb == "PUT" else "in" if vb == "GET" else None, ln))
        if opts.get("WEBSERVICE"):
            val, _c, _r = _cics_value(opts["WEBSERVICE"], literal_map)
            f.cics_cmds.append((vb, "webservice", val or opts["WEBSERVICE"].upper(), "out", ln))
        if vb == "WEB":
            f.cics_cmds.append((vb, "web", inner.split()[1].upper() if len(inner.split()) > 1 else "?", None, ln))

        # the data carrier: FROM(x) leaves the program (cics_out), INTO(x) / SET(p) arrive (cics_in),
        # noted "{verb} {resource kind} {resource}" so the walker can pair a WRITEQ with its READQ.
        # START FROM(x) is the started task's argument (above), not a carrier row.
        carried = [(vb, k, r) for (vb_, k, r, _d, ln_) in f.cics_cmds
                   if ln_ == ln and vb_ == vb and k in ("map", "file", "tsq", "tdq", "queue", "container")]
        if carried:
            ckind, cres = carried[0][1], carried[0][2]
        elif vb == "RETRIEVE":
            ckind, cres = "start", ""
        elif vb in ("SEND", "RECEIVE"):
            ckind, cres = "terminal", ""
        else:
            ckind, cres = "", ""
        note = " ".join(x for x in (vb, ckind, cres) if x)
        if vb != "START":
            for opt, fkind in (("FROM", "cics_out"), ("INTO", "cics_in"), ("SET", "cics_in")):
                v = opts.get(opt)
                if not v or v[:1] in ("'", '"'):
                    continue
                if opt == "SET":
                    v = _CICS_SET_ADDR.sub("", v)       # the area, not the pointer
                for op in _operands(v)[:1]:
                    if op.name:
                        f.flows.append(_flow(ln, "EXEC-CICS-" + vb, fkind,
                                             src=op if fkind == "cics_out" else None,
                                             dst=op if fkind == "cics_in" else None, note=note, guard=guard))


_DLI_IN_FUNCS = {"GU", "GN", "GHU", "GHN", "GNP", "GHNP"}
_DLI_OUT_FUNCS = {"ISRT", "REPL"}


def _dli_flow(f: ProgramFacts, ln: int, verb: str, func: Optional[str], io_area: Optional[str],
              guard: Optional[str], into: Optional[str] = None, frm: Optional[str] = None) -> None:
    """The I/O area of a DL/I call: a get fills it (dli_in), ISRT/REPL send it
    (dli_out); a function nobody resolved is recorded as a get and says so."""
    if func in _DLI_OUT_FUNCS:
        name, kind, note = frm or io_area, "dli_out", func
    elif func in _DLI_IN_FUNCS:
        name, kind, note = into or io_area, "dli_in", func
    elif func is None or func.startswith("*"):
        name, kind, note = into or io_area, "dli_in", "function unresolved"
    else:
        return                                              # DLET, CHKP, PCB ... move no data into the program
    if not name:
        return
    op = _operands(name)[:1]
    if op and op[0].name:
        f.flows.append(_flow(ln, verb, kind, src=op[0] if kind == "dli_out" else None,
                             dst=op[0] if kind == "dli_in" else None, note=note, guard=guard))


def _extract_dli(f: ProgramFacts, st: LogicalLine, literal_map: Optional[Dict[str, Set[str]]] = None,
                 guards: Optional[List[Tuple[int, Optional[str]]]] = None) -> None:
    literal_map = literal_map or {}
    m = _DLI_CALL.search(st.text)
    if m:
        iface = m.group(2).upper()
        args = _split_call_args(m.group(3))
        _record_dli(f, st, iface, args, literal_map, _guard_at(guards, m.start()))
        return

    for m2 in _EXEC_DLI.finditer(st.text):
        # EXEC DLI GHU USING PCB(2) SEGMENT(POLICY) INTO(WS-AREA) WHERE(...)
        inner = " ".join(m2.group(1).split())
        verb = _CICS_VERB.match(inner)
        func = verb.group(1).upper() if verb else None
        opts: Dict[str, str] = {}
        for mo in _CICS_OPT.finditer(inner):
            opts.setdefault(mo.group(1).upper(), mo.group(2).strip())
        mp = re.search(r"\bPCB\s*\(\s*(\d+)\s*\)", inner, re.IGNORECASE)
        pcb_n = int(mp.group(1)) if mp else None
        seg = (opts.get("SEGMENT") or "").upper() or None
        io = (opts.get("INTO") or opts.get("FROM") or "").upper() or None
        ssa = [seg] if seg else []
        if opts.get("WHERE"):
            ssa.append(opts["WHERE"])
        f.dli.append(DliFact(interface="EXEC DLI", func=func, pcb_arg=f"PCB({pcb_n})" if pcb_n else None,
                             ssa_args=ssa, io_area=io, line=st.line_at(m2.start()), pcb_index=pcb_n,
                             resolution="literal"))
        if seg:
            f.io_ops.append((seg, "ims", func or "?", st.line_at(m2.start())))
        _dli_flow(f, st.line_at(m2.start()), "EXEC-DLI", func, io, _guard_at(guards, m2.start()),
                  into=opts.get("INTO"), frm=opts.get("FROM"))


def _is_dli_func(tok: str, literal_map: Dict[str, Set[str]]) -> bool:
    return tok in _DLI_FUNCS or any(v.strip().upper() in _DLI_FUNCS for v in literal_map.get(tok, ()))


def _record_dli(f: ProgramFacts, st: LogicalLine, iface: str,
                args: List[str], literal_map: Dict[str, Set[str]], guard: Optional[str] = None) -> None:
    """CALL 'CBLTDLI' USING [parmcount] func, pcb, io-area, ssa...

    The PCB argument is positional in the PSB, which is why it is captured
    verbatim: resolving which database it refers to requires the PSB, and
    guessing is how an analysis ends up naming the wrong IMS database.
    The function is usually a VALUE-initialised field (`05 DLI-GU PIC X(4)
    VALUE 'GU  '`): resolved through the literal map like a dynamic CALL.
    """
    func = None
    pcb = None
    io_area = None
    ssas: List[str] = []
    resolution: Optional[str] = None

    # Older programs pass a parameter count first; every argument shifts by
    # one and the PCB (so the database) named for the call would be wrong.
    if len(args) >= 2:
        first = args[0].strip().strip("'\"").upper()
        second = args[1].strip().strip("'\"").upper()
        numeric = literal_map.get(first) and all(re.fullmatch(r"[+-]?\d+", v.strip()) for v in literal_map[first])
        if not _is_dli_func(first, literal_map) and (_PARMCOUNT_NAME.match(first) or numeric
                                                     or re.fullmatch(r"\d+", first)) \
                and _is_dli_func(second, literal_map):
            args = args[1:]
            resolution = "parmcount"

    if args:
        first = args[0].strip().strip("'\"").upper()
        if first in _DLI_FUNCS:
            func = first
            resolution = resolution or "literal"
        elif re.fullmatch(ID, first, re.IGNORECASE):
            cands = sorted({v.strip().upper() for v in literal_map.get(first, ()) if v.strip().upper() in _DLI_FUNCS})
            if len(cands) == 1:
                func = cands[0]
                resolution = resolution or "value_clause"     # keep 'parmcount' when both apply
            else:
                func = f"*{first}*"      # func held in a variable never given a single literal
                resolution = "unresolved"
                f.unresolved.append((
                    "dli_function",
                    f"DL/I call function held in variable {first} - "
                    + (f"candidates {', '.join(cands)}" if cands else
                       "never assigned a literal in this program; the operation (read vs update) "
                       "cannot be determined statically"),
                    st.start))
    if len(args) > 1:
        pcb = args[1].strip().upper()
    if len(args) > 2:
        io_area = args[2].strip().upper()
    if len(args) > 3:
        ssas = [a.strip().upper() for a in args[3:]]

    dest = None
    if func == "CHNG" and io_area:
        # CHNG sets the destination of an alternate PCB; the ISRT that follows
        # is a message switch to that transaction / LTERM.
        cands = sorted(literal_map.get(io_area, ()))
        dest = cands[0] if len(cands) == 1 else None
        f.calls.append(CallFact(kind="ims_switch", target=dest, via_var=None if dest else io_area,
                                resolved=cands, resolution="move_literal" if dest else ("move_literal" if cands else "unresolved"),
                                using_args=[], line=st.start))
        if not cands:
            f.unresolved.append(("ims_switch", f"CHNG destination in {io_area} never assigned a literal - "
                                               f"the transaction switched to is unknown", st.start))

    f.dli.append(DliFact(interface=iface, func=func, pcb_arg=pcb,
                         ssa_args=ssas, io_area=io_area, line=st.start,
                         resolution=resolution, dest=dest or (f"*{io_area}*" if func == "CHNG" else None)))
    if pcb:
        f.io_ops.append((pcb, "ims", func or "?", st.start))
    _dli_flow(f, st.start, "CALL-" + iface, func, io_area, guard)


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


def _mq_queue(struct: Optional[str], literal_map: Dict[str, Set[str]]) -> Optional[str]:
    """The queue name behind an MQOD structure: `MQOD-OBJECTNAME` (or
    `<struct>-OBJECTNAME`) initialised by VALUE or MOVEd a literal."""
    if not struct:
        return None
    keys = [f"{struct}-OBJECTNAME", "MQOD-OBJECTNAME"]
    keys += [k for k in literal_map if k.endswith("OBJECTNAME") and k not in keys]
    for k in keys:
        vals = sorted(v for v in literal_map.get(k, ()) if v.strip())
        if len(vals) == 1:
            return vals[0]
    return None


def _extract_mq(f: ProgramFacts, st: LogicalLine, literal_map: Optional[Dict[str, Set[str]]] = None,
                guards: Optional[List[Tuple[int, Optional[str]]]] = None) -> None:
    """MQOPEN/MQPUT/MQPUT1/MQGET with the queue name and the message layout.

    USING positions (MQI): MQOPEN hconn, MQOD, options, hobj, cc, rc
                          MQPUT  hconn, hobj, MQMD, PMO, buflen, BUFFER, cc, rc
                          MQPUT1 hconn, MQOD, MQMD, PMO, buflen, BUFFER, cc, rc
                          MQGET  hconn, hobj, MQMD, GMO, buflen, BUFFER, datalen, cc, rc
    The queue is the MQOD's OBJECTNAME; PUT/GET reach it through the HOBJ
    that MQOPEN filled. The BUFFER argument is the message layout - the
    interface contract with whatever reads the queue.
    """
    literal_map = literal_map or {}
    for m in _MQ_CALL.finditer(st.text):
        call = m.group(1).upper()
        args = _parse_using(st.text[m.start():])
        queue = layout = direction = None
        if call == "MQOPEN" and len(args) > 3:
            f.mq_handles[args[3]] = args[1]
            queue = _mq_queue(args[1], literal_map)
        elif call == "MQPUT1":
            queue = _mq_queue(args[1] if len(args) > 1 else None, literal_map)
            layout = args[5] if len(args) > 5 else None
            direction = "out"
        elif call in ("MQPUT", "MQGET"):
            od = f.mq_handles.get(args[1]) if len(args) > 1 else None
            queue = _mq_queue(od, literal_map)
            layout = args[5] if len(args) > 5 else None
            direction = "out" if call == "MQPUT" else "in"
        f.mq.append((call, queue, direction, layout, st.start))
        if direction and layout:
            # the BUFFER argument carries the message: MQPUT/MQPUT1 send it (mq_out), MQGET fills it (mq_in)
            op = _operands(layout)[:1]
            if op and op[0].name:
                kind = "mq_out" if direction == "out" else "mq_in"
                f.flows.append(_flow(st.start, "CALL-" + call, kind,
                                     src=op[0] if kind == "mq_out" else None, dst=op[0] if kind == "mq_in" else None,
                                     note=f"queue {queue}" if queue else "(queue not resolvable)",
                                     guard=_guard_at(guards, m.start())))


def _extract_entry(f: ProgramFacts, st: LogicalLine) -> None:
    """ENTRY 'name' USING ...: an alternate entry point (CALL 'name' reaches
    this program) and, for `ENTRY 'DLITCBL'`, the PCB list of an older IMS
    program that has no PROCEDURE DIVISION USING."""
    for m in _ENTRY.finditer(st.text):
        name = m.group(2).strip().upper()
        using = _parse_using(m.group(0)) if m.group(3) else []
        f.entries.append((name, using, st.start))
        if using and not f.linkage_using:
            f.linkage_using = using


def _extract_performs(f: ProgramFacts, st: LogicalLine, here: str) -> None:
    if st.area_a and _PARAGRAPH.match(st.text.strip()):
        return
    body = _EXEC_ANY.sub(lambda m: " " * len(m.group(0)), st.text)
    for m in _PERFORM.finditer(body):
        to = m.group(1).upper()
        thru = m.group(3).upper() if m.group(3) else None
        # `PERFORM UNTIL`, `PERFORM VARYING`, `PERFORM n TIMES`,
        # `PERFORM WS-CNT TIMES` are inline - no edge to '3' or 'WS-CNT'.
        if to in ("UNTIL", "VARYING", "WITH", "TEST", "FOREVER"):
            continue
        if re.fullmatch(r"\d+", to) or re.match(r"\s+TIMES\b", body[m.end():], re.IGNORECASE):
            continue
        # PERFORM WORK OF SUB-B: two paragraphs may share a name; the section
        # travels with the target as "WORK OF SUB-B"
        if m.group(2):
            to = f"{to} OF {m.group(2).upper()}"
        if thru and m.group(4):
            thru = f"{thru} OF {m.group(4).upper()}"
        f.performs.append((here, to, thru, st.line_at(m.start()), "perform"))
    # GO TO is control flow too: a paragraph reached only by GO TO is not dead.
    for m in _GO_TO.finditer(body):
        targets = [f"{a.upper()} OF {q.upper()}" if q else a.upper()
                   for a, q in _QUALIFIED_TARGET.findall(m.group(1))]
        kind = "goto_depending" if m.group(2) else "goto"
        for t in targets:
            f.performs.append((here, t, None, st.line_at(m.start()), kind))
    for m in _ALTER.finditer(body):
        f.performs.append((here, m.group(2).upper(), None, st.line_at(m.start()), "alter"))
    for m in _SORT_PROC.finditer(body):
        f.performs.append((here, m.group(2).upper(), m.group(3).upper() if m.group(3) else None,
                           st.line_at(m.start()), "sort_proc"))


_TERMINAL = re.compile(B + r"(?:GOBACK|STOP\s+RUN|EXIT\s+PROGRAM|GO\s+TO)" + E, re.IGNORECASE)


def _fallthrough_edges(f: ProgramFacts, last_stmt: Dict[str, str]) -> None:
    """A paragraph whose last statement does not leave (GOBACK / STOP RUN /
    EXIT PROGRAM / unconditional GO TO) runs straight into the next one.
    Recorded as its own edge kind so `dead` and `paragraph` can say
    "reached by fall-through" instead of "never performed"."""
    paras = [p for p in f.paragraphs if p.kind == "paragraph"]
    for a, b in zip(paras, paras[1:]):
        last = last_stmt.get(a.name, "")
        if last and _TERMINAL.search(last) and not re.search(B + r"IF|WHEN|ELSE" + E, last, re.IGNORECASE) \
                and not re.search(B + r"DEPENDING" + E, last, re.IGNORECASE):
            continue                                      # GO TO ... DEPENDING ON falls through out of range
        f.performs.append((a.name, b.name, None, a.end_line, "fallthrough"))


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
# ADD a b GIVING c has no TO: the keyword group is optional, so the GIVING
# target is a write and not one more read (LESSONS 174).
_ARITH = re.compile(r"^(.+?)(?:\s+" + B + r"(TO|FROM|BY|INTO)" + E + r"\s+(.+?))?"
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
    # `WS-KEY OF WS-REC` is ONE identifier (WS-KEY); the qualifier is not a
    # second reference.
    text = re.sub(r"\b(?:OF|IN)\s+[A-Z0-9][A-Z0-9\-]*", " ", text or "", flags=re.IGNORECASE)
    for m in _TOKEN.finditer(text):
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


# ---- operands, as written, for the flow rows ------------------------------
#
# _idents answers "which names does this fragment touch"; the flow rows need
# the operands themselves: `WS-Q OF REC-A` is not `WS-Q OF REC-B`, `WS-X (WS-I)`
# reads WS-X and WS-I is only the subscript, `WS-BUF(1:2)` is two bytes of
# WS-BUF. So a second tokenizer keeps up to two paren groups on a name.

_PAREN = r"\((?:[^()]|\([^()]*\))*\)"
_OPTOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[A-Z0-9][A-Z0-9\-]*(?:\s*" + _PAREN + r")?(?:\s*" + _PAREN + r")?",
                      re.I)
_FIGURATIVE = {"SPACE", "SPACES", "ZERO", "ZEROS", "ZEROES", "LOW-VALUE", "LOW-VALUES", "HIGH-VALUE",
               "HIGH-VALUES", "QUOTE", "QUOTES", "NULL", "NULLS"}


def _split_groups(tok: str) -> Tuple[str, List[str]]:
    """'WS-X (1) (2:3)' -> ('WS-X', ['(1)', '(2:3)'])."""
    i = tok.find("(")
    if i < 0:
        return tok, []
    return tok[:i].strip(), re.findall(_PAREN, tok[i:])


def _attach_groups(op: Operand, groups: List[str]) -> None:
    """A group with a top-level ':' is a ref-mod; the first other one the subscript."""
    for g in groups:
        depth, colon = 0, False
        for ch in g:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ":" and depth == 1:
                colon = True
        if colon:
            op.refmod = g
        elif op.sub is None:
            op.sub = g


def _operands(text: str) -> List[Operand]:
    """The operands of a fragment, in order: data-names with their OF/IN chain,
    subscript and ref-mod; literals, figurative constants and `ALL 'x'` as
    `lit`; the identifiers inside FUNCTION f(...) with `func` = f (a function
    without an identifier argument is one operand with only `func`). Reserved
    words are skipped and a subscript name is never an operand."""
    out: List[Operand] = []
    toks = list(_OPTOKEN.finditer(text or ""))
    i = 0
    while i < len(toks):
        t = toks[i].group(0)
        i += 1
        if t[0] in ("'", '"'):
            out.append(Operand(lit=t))
            continue
        head, groups = _split_groups(t)
        up = head.upper()
        if re.fullmatch(r"[+\-\d.]+", up):
            out.append(Operand(lit=up))
            continue
        if up in ("OF", "IN"):
            # qualifier of the operand before it; a subscript written after the qualifier is that operand's
            if i < len(toks):
                qh, qg = _split_groups(toks[i].group(0))
                i += 1
                if out and out[-1].name and re.fullmatch(ID, qh, re.I):
                    out[-1].quals.append(qh.upper())
                    if qg and out[-1].sub is None and out[-1].refmod is None:
                        _attach_groups(out[-1], qg)
            continue
        if up == "ALL":
            if i < len(toks) and (toks[i].group(0)[0] in ("'", '"') or toks[i].group(0).upper() in _FIGURATIVE):
                out.append(Operand(lit="ALL " + toks[i].group(0)))
                i += 1
            continue
        if up in _FIGURATIVE:
            out.append(Operand(lit=up))
            continue
        if up == "FUNCTION":
            if i < len(toks):
                fh, fg = _split_groups(toks[i].group(0))
                i += 1
                inner = [o for g in fg for o in _operands(g[1:-1]) if o.name]
                for o in inner:
                    o.func = fh.upper()
                out.extend(inner or [Operand(func=fh.upper())])
            continue
        if up in _RESERVED or not re.fullmatch(ID, up, re.I):
            continue
        op = Operand(name=up)
        _attach_groups(op, groups)
        out.append(op)
    return out


def _named(text: str) -> List[Operand]:
    return [o for o in _operands(text) if o.name]


def _lit_kind(op: Operand) -> str:
    """MOVE 'AB' / MOVE 1 are `literal`; SPACES, ZEROS, ALL 'x' are `figurative`."""
    return "literal" if op.lit and (op.lit[0] in ("'", '"') or op.lit[0] in "+-.0123456789") else "figurative"


def _flow(ln: int, verb: str, kind: str, src: Optional[Operand] = None, dst: Optional[Operand] = None,
          ordinal: int = 0, note: Optional[str] = None, guard: Optional[str] = None) -> FlowFact:
    return FlowFact(line=ln, verb=verb, kind=kind,
                    src_name=src.name if src else None,
                    src_qual=(" OF ".join(src.quals) or None) if src else None,
                    src_sub=src.sub if src else None, src_refmod=src.refmod if src else None,
                    src_lit=src.lit if src else None,
                    dst_name=dst.name if dst else None,
                    dst_qual=(" OF ".join(dst.quals) or None) if dst else None,
                    dst_sub=dst.sub if dst else None, dst_refmod=dst.refmod if dst else None,
                    ordinal=ordinal, note=note, guard=guard)


def _pair_flows(f: ProgramFacts, ln: int, verb: str, kind: str, srcs: List[Operand], dsts: List[Operand],
                guard: Optional[str], note: Optional[str] = None) -> None:
    """One row per source x target; the ordinal is the target's position."""
    for i, d in enumerate(dsts):
        for s in srcs:
            f.flows.append(_flow(ln, verb, kind, src=s, dst=d, ordinal=i, note=note or s.func, guard=guard))


def _cond_text(frag: str) -> Optional[str]:
    """An IF / WHEN condition as the guard stores it: one line, no THEN, 120 chars."""
    s = re.sub(r"\s+THEN$", "", " ".join((frag or "").split()), flags=re.I)
    return s[:120] or None


def _pop_to(stack: List[List], kind: str) -> None:
    """END-IF / END-EVALUATE closes the nearest open block of its kind and everything opened inside it."""
    if any(e[0] == kind for e in stack):
        while stack.pop()[0] != kind:
            pass


def _guard_at(guards: Optional[List[Tuple[int, Optional[str]]]], off: int) -> Optional[str]:
    """The guard in force at `off` of the statement: the state after the last verb before it."""
    g = None
    for o, cond in guards or ():
        if o >= off:
            break
        g = cond
    return g


_DELIM_BY = re.compile(B + r"DELIMITED\s+BY\s+(?:ALL\s+)?(?:" + LIT + r"|[A-Z0-9][A-Z0-9\-]*(?:\s*" + _PAREN + r")?)", re.I)
_DELIM_COUNT_IN = re.compile(B + r"(?:DELIMITER|COUNT)\s+IN\s+[A-Z0-9][A-Z0-9\-]*(?:\s*" + _PAREN + r")?", re.I)
_UNSTRING_END = re.compile(B + r"(?:WITH\s+POINTER|POINTER|TALLYING|(?:NOT\s+)?(?:ON\s+)?OVERFLOW)" + E, re.I)
_SET_ADDR_OF = re.compile(r"^ADDRESS\s+OF\s+(.+?)\s+TO\s+(.+)$", re.I | re.S)
_SET_TO_ADDR = re.compile(r"^(.+?)\s+TO\s+ADDRESS\s+OF\s+(.+)$", re.I | re.S)
_VARYING_FROM = re.compile(B + r"VARYING\s+(.+?)\s+FROM\s+(.+?)(?:\s+BY\s+|\s+UNTIL\s+|$)", re.I | re.S)
_FIRST_NAME = re.compile(r"([A-Z0-9][A-Z0-9\-]*)", re.I)


def _move_flows(f: ProgramFacts, raw: str, ln: int, guard: Optional[str]) -> None:
    m = _MOVE_TO.match(raw)
    if not m:
        return
    srcs, dsts = _operands(m.group(1)), _named(m.group(2))
    if re.match(r"CORR(?:ESPONDING)?" + E, raw, re.I):
        _pair_flows(f, ln, "MOVE", "move_corr", [s for s in srcs if s.name][:1], dsts, guard)
        return
    for i, d in enumerate(dsts):
        for s in srcs:
            kind = "function" if s.func else "move" if s.name else _lit_kind(s)
            f.flows.append(_flow(ln, "MOVE", kind, src=s, dst=d, ordinal=i, note=s.func, guard=guard))


def _set_flows(f: ProgramFacts, raw: str, ln: int, guard: Optional[str]) -> None:
    ma = _SET_ADDR_OF.match(raw)
    if ma:                      # SET ADDRESS OF a TO p: p's value becomes a's address
        _pair_flows(f, ln, "SET", "set_address", _named(ma.group(2))[:1], _named(ma.group(1)), guard)
        return
    mp = _SET_TO_ADDR.match(raw)
    if mp:                      # SET p TO ADDRESS OF x
        _pair_flows(f, ln, "SET", "set_address", _named(mp.group(2))[:1], _named(mp.group(1)), guard)
        return
    m = _SET.match(raw)
    if not m or re.fullmatch(r"TRUE|FALSE", m.group(2).strip(), re.I):
        return
    srcs, dsts = _operands(m.group(2))[:1], _named(m.group(1))
    if re.search(B + r"(?:UP|DOWN)\s+BY" + E, raw, re.I):
        _pair_flows(f, ln, "SET", "arith", [s for s in srcs if s.name], dsts, guard)
        return
    for i, d in enumerate(dsts):
        for s in srcs:
            f.flows.append(_flow(ln, "SET", "set" if s.name else _lit_kind(s), src=s, dst=d, ordinal=i, guard=guard))


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


def _exec_refs(f: ProgramFacts, kind: str, inner: str, ln: int) -> None:
    """Host fields inside EXEC CICS / EXEC DLI. INTO/SET/RESP receive data
    (the program WRITES them); FROM/RIDFLD/QUEUE/... are read; COMMAREA is
    both. SQL host variables are handled by _sql_host_modes."""
    if kind == "SQL":
        return
    if kind == "DLI":
        for m in _CICS_OPT.finditer(inner):
            k, v = m.group(1).upper(), m.group(2).strip()
            if k == "INTO":
                _refs(f, _idents(v), "write", "EXEC-DLI", ln)
            elif k == "FROM":
                _refs(f, _idents(v), "read", "EXEC-DLI", ln)
            elif k == "WHERE":
                _refs(f, _idents(v), "test", "EXEC-DLI", ln)
        return
    verb = _CICS_VERB.match(inner)
    stmt = "EXEC-CICS-" + (verb.group(1).upper() if verb else "?")
    for m in _CICS_OPT.finditer(inner):
        k, v = m.group(1).upper(), m.group(2).strip()
        if not v or v[0] in ("'", '"') or k in _CICS_NOT_FIELDS:
            continue
        names = _idents(v)
        if k in _CICS_WRITE_OPTS:
            _refs(f, names, "write", stmt, ln)
        elif k == "COMMAREA":
            _refs(f, names, "read", stmt, ln)
            _refs(f, names, "write", stmt, ln)     # the callee may change it
        elif k in _CICS_READ_OPTS:
            _refs(f, names, "read", stmt, ln)


def _extract_field_and_literal_refs(f: ProgramFacts, st: LogicalLine) -> List[Tuple[int, Optional[str]]]:
    """field_refs / literal_refs from the qualifier-blanked text (unchanged),
    and one FlowFact per (source, target) from the text as written, each with
    the IF / WHEN that encloses it as its guard. Returns (offset, guard) after
    every verb, so the EXEC CICS / DLI / MQ rows of the same statement - blanked
    here - get their guard too."""
    body = st.text.strip().rstrip(".")
    # EXEC CICS/DLI/SQL text is not COBOL: READ FILE('X') INTO(Y) is not a
    # COBOL READ, WHERE(POLNO = K) is not a WHEN. Their host fields are taken
    # by _exec_refs; the span is blanked (same length) so line attribution
    # of everything else is unchanged.
    for m in _EXEC_ANY.finditer(body):
        _exec_refs(f, m.group(1).upper(), " ".join(m.group(2).split()), st.line_at(m.start()))
    body = _EXEC_ANY.sub(lambda m: " " * len(m.group(0)), body)
    # the flow rows need the operands as written (`WS-Q OF REC-A` is not `WS-Q
    # OF REC-B`): this text is kept, the refs go on reading the blanked one
    body_raw = body
    # `WS-KEY OF WS-REC = 'B'` tests WS-KEY; the qualifier is blanked (same
    # length, so line attribution is unchanged) before verbs are read.
    body = re.sub(r"\b(?:OF|IN)\s+[A-Z0-9][A-Z0-9\-]*", lambda m: " " * len(m.group(0)), body, flags=re.IGNORECASE)
    # EVALUATE A ALSO B ... WHEN 3 ALSO 'M': one subject per ALSO position, and
    # each WHEN literal attaches to the subject at ITS position.
    eval_subjects: List[Optional[str]] = []

    verbs = _split_verbs(body)
    raws = _split_verbs(body_raw)
    if [(v, o) for v, _fr, o in verbs] != [(v, o) for v, _fr, o in raws]:
        # blanking a qualifier changed the verb split (not seen on any fixture): both from the blanked text
        raws = verbs
        f.unresolved.append(("operand_parse", "statement splits differently with its OF/IN qualifiers blanked; "
                                              "flow operands taken from the blanked text", st.start))
    # open IF / EVALUATE / SEARCH / WHEN blocks of this statement: [kind, text, negated]
    stack: List[List] = []
    guards: List[Tuple[int, Optional[str]]] = []

    def guard() -> Optional[str]:
        for kind, text, neg in reversed(stack):
            if kind in ("if", "when"):
                return f"NOT ({text})" if neg else text
        return None

    for (verb, frag, off), (_v, raw, _o) in zip(verbs, raws):
        ln = st.line_at(off)          # the verb's own physical line, not the IF's
        g = guard()
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
            _move_flows(f, raw, ln, g)

        elif verb == "COMPUTE":
            m = _COMPUTE.match(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)
                _refs(f, _idents(m.group(2)), "read", verb, ln)
                mr = _COMPUTE.match(raw)
                if mr:
                    _pair_flows(f, ln, verb, "arith", _named(mr.group(2)), _named(mr.group(1)), g)

        elif verb in ("ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"):
            m = _ARITH.match(frag)
            if m and (m.group(2) or m.group(4)):
                operands, target, giving, rem = m.group(1), m.group(3), m.group(4), m.group(5)
                _refs(f, _idents(operands), "read", verb, ln)
                if giving:
                    if target:
                        _refs(f, _idents(target), "read", verb, ln)
                    _refs(f, _idents(giving), "write", verb, ln)
                else:
                    _refs(f, _idents(target), "write", verb, ln)
                if rem:
                    _refs(f, _idents(rem), "write", verb, ln)
                mr = _ARITH.match(raw)
                if mr and (mr.group(2) or mr.group(4)):
                    # GIVING: every operand feeds each GIVING target (and the REMAINDER); else the
                    # operands feed the TO/FROM/BY/INTO target
                    srcs = _named(mr.group(1)) + (_named(mr.group(3)) if mr.group(4) and mr.group(3) else [])
                    dsts = (_named(mr.group(4)) + _named(mr.group(5) or "")) if mr.group(4) else _named(mr.group(3))
                    _pair_flows(f, ln, verb, "arith", srcs, dsts, g)
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
                if verb == "STRING":
                    tpl = _template(frag[:m.start()])
                    if tpl:
                        for t in _idents(m.group(1)):
                            f.literal_refs.append((tpl, "string_group", t, ln))
                mr = _INTO_TGT.search(raw)
                if mr and verb == "STRING":
                    # the sources, without their DELIMITED BY operands; the POINTER is cut by _INTO_TGT
                    _pair_flows(f, ln, verb, "string", _named(_DELIM_BY.sub(" ", raw[:mr.start()])),
                                _named(mr.group(1)), g)
                elif mr:
                    # the source before DELIMITED / INTO; every INTO target, minus DELIMITER IN / COUNT IN
                    head = re.split(B + r"(?:DELIMITED|INTO)" + E, raw, maxsplit=1, flags=re.I)[0]
                    tail = _UNSTRING_END.split(raw[mr.start(1):], maxsplit=1)[0]
                    _pair_flows(f, ln, verb, "unstring", _named(head)[:1], _named(_DELIM_COUNT_IN.sub(" ", tail)), g)
            else:
                _refs(f, _idents(frag), "read", verb, ln)

        elif verb == "INITIALIZE":
            head = re.split(B + r"(?:REPLACING|WITH)" + E, frag, maxsplit=1, flags=re.I)[0]
            _refs(f, _idents(head), "write", verb, ln)
            mr = re.search(B + r"((?:REPLACING|WITH)" + E + r".*)$", raw, re.I | re.S)
            rep = Operand(lit=" ".join(mr.group(1).split())) if mr else None
            for i, d in enumerate(_named(re.split(B + r"(?:REPLACING|WITH)" + E, raw, maxsplit=1, flags=re.I)[0])):
                f.flows.append(_flow(ln, verb, "initialize", src=rep, dst=d, ordinal=i, guard=g))

        elif verb == "SET":
            m = _SET.match(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)
                _refs(f, _idents(m.group(2)), "read", verb, ln)
            _set_flows(f, raw, ln, g)

        elif verb == "ACCEPT":
            ids = _idents(frag)
            if ids:
                _refs(f, ids[:1], "write", verb, ln)
            mf = re.search(B + r"FROM\s+([A-Z0-9][A-Z0-9\-]*)", raw, re.I)
            for d in _named(re.split(B + "FROM" + E, raw, maxsplit=1, flags=re.I)[0])[:1]:
                f.flows.append(_flow(ln, verb, "accept", dst=d, note=mf.group(1).upper() if mf else None, guard=g))

        elif verb in ("READ", "RETURN"):
            m = _INTO_TGT.search(frag)
            if m:
                _refs(f, _idents(m.group(1)), "write", verb, ln)
            mf = _FIRST_NAME.match(raw)
            if mf:
                # the file fills each of its 01 records (io_in); INTO copies the record on to x
                fname = mf.group(1).upper()
                fd = next((x for x in f.files if x.select_name == fname), None)
                recs = fd.fd_records if fd else []
                note = f"file {fname}" + (" multi-record" if len(recs) > 1 else "")
                for i, r in enumerate(recs):
                    f.flows.append(_flow(ln, verb, "io_in", dst=Operand(name=r), ordinal=i, note=note, guard=g))
                mr = _INTO_TGT.search(raw)
                if mr:
                    for d in _named(mr.group(1))[:1]:
                        if recs:
                            for i, r in enumerate(recs):
                                f.flows.append(_flow(ln, verb, "read_into", src=Operand(name=r), dst=d, ordinal=i,
                                                     note=note, guard=g))
                        else:
                            f.flows.append(_flow(ln, verb, "read_into", dst=d, note=note + " (records unknown)", guard=g))

        elif verb in ("WRITE", "REWRITE", "RELEASE"):
            m = _FROM_SRC.search(frag)
            if m:
                _refs(f, [m.group(1).upper()], "read", verb, ln)
            mf = _FIRST_NAME.match(raw)
            if mf:
                rec = mf.group(1).upper()
                fd = _record_of(f, rec)
                note = f"file {fd.select_name}" if fd else "file ?"
                f.flows.append(_flow(ln, verb, "io_out", src=Operand(name=rec), note=note, guard=g))
                mr = re.search(B + r"FROM\s+(.+)$", raw, re.I | re.S)
                if mr:
                    _pair_flows(f, ln, verb, "write_from", _named(mr.group(1))[:1], [Operand(name=rec)], g)

        elif verb == "INSPECT":
            ids = _idents(frag)
            if ids:
                if re.search(B + r"(?:REPLACING|CONVERTING)" + E, frag, re.I):
                    _refs(f, ids[:1], "write", verb, ln)
                    for op in _named(raw)[:1]:
                        f.flows.append(_flow(ln, verb, "inspect", src=op, dst=op, guard=g))
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
            tpl = _template(head)
            if tpl:
                f.literal_refs.append((tpl, "display_group", None, ln))

        elif verb == "CALL":
            # BY REFERENCE is the default: the callee may write every argument.
            # BY CONTENT / BY VALUE give the callee a copy, LENGTH OF x a number:
            # read only. From the text as written, so LENGTH and ADDRESS never
            # become fields of their own.
            args, returning = _parse_using_detail("CALL " + raw)
            for a in args:
                if not a.name:
                    continue
                if a.how not in ("content", "value", "length_of"):
                    f.field_refs.append((a.name, "write", "CALL-USING", ln))
                f.field_refs.append((a.name, "read", "CALL-USING", ln))
            if returning:
                f.field_refs.append((returning, "write", "CALL-RETURNING", ln))
                mt = re.match(r"\s*(?:(['\"])([^'\"]+)\1|([A-Z0-9][A-Z0-9\-]*))", raw, re.I)
                target = (mt.group(2) or mt.group(3)).strip().upper() if mt else None
                f.flows.append(_flow(ln, verb, "returning", dst=Operand(name=returning), note=target, guard=g))

        elif verb == "IF":
            _refs(f, _idents(frag), "test", verb, ln)
            _compare_literals(f, frag, "compare", ln)
            stack.append(["if", _cond_text(raw), False])

        elif verb == "ELSE":
            for e in reversed(stack):
                if e[0] == "if":
                    e[2] = True
                    break

        elif verb == "END-IF":
            _pop_to(stack, "if")

        elif verb == "EVALUATE":
            eval_subjects = []
            for part in re.split(B + "ALSO" + E, frag, flags=re.I):
                m = _EVAL_SUBJ.match(part.strip())
                eval_subjects.append(m.group(1).upper() if m and m.group(1) else None)
            _refs(f, _idents(frag), "test", verb, ln)
            stack.append(["eval", _cond_text(raw), False])

        elif verb == "WHEN":
            _refs(f, _idents(frag), "test", verb, ln)
            parts = re.split(B + "ALSO" + E, frag, flags=re.I)
            for i, part in enumerate(parts):
                subj = eval_subjects[i] if i < len(eval_subjects) else None
                _compare_literals(f, part, "when", ln, subject=subj)
            if stack and stack[-1][0] == "when":
                stack.pop()
            subject = next((e[1] for e in reversed(stack) if e[0] == "eval"), None)
            stack.append(["when", _cond_text((f"EVALUATE {subject} " if subject else "") + "WHEN " + raw), False])

        elif verb in ("END-EVALUATE", "END-SEARCH"):
            _pop_to(stack, "eval")

        elif verb == "PERFORM":
            mu = re.search(B + r"UNTIL\s+(.+)$", frag, re.I | re.S)
            if mu:
                _refs(f, _idents(mu.group(1)), "test", verb, ln)
                _compare_literals(f, mu.group(1), "compare", ln)
            mv = re.search(B + r"VARYING\s+([A-Z0-9][A-Z0-9\-]*)", frag, re.I)
            if mv:
                _refs(f, [mv.group(1).upper()], "write", verb, ln)
            mvr = _VARYING_FROM.search(raw)
            if mvr:
                # the index starts at FROM's value: a `set` row, src_lit when it is a number
                for d in _named(mvr.group(1))[:1]:
                    for s in _operands(mvr.group(2))[:1]:
                        f.flows.append(_flow(ln, verb, "set", src=s, dst=d, guard=g))

        elif verb in ("SEARCH", "START", "DELETE", "OPEN", "CLOSE"):
            _refs(f, _idents(frag), "read", verb, ln)
            if verb == "SEARCH":
                stack.append(["eval", None, False])

        guards.append((off, guard()))
    return guards


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


_TEMPLATE_SKIP = {"DELIMITED", "BY", "SIZE", "INTO", "WITH", "POINTER", "UPON", "NO", "ADVANCING",
                  "ON", "OVERFLOW", "NOT", "END-STRING", "END-DISPLAY", "OF", "IN", "CONSOLE",
                  "SYSOUT", "SYSPRINT", "LINE", "ALL"}


def _template(fragment: str) -> Optional[str]:
    """The message a STRING or DISPLAY builds, as a template.

        STRING 'INVALID GENDER ' DELIMITED BY SIZE WS-GENDER-CD DELIMITED BY SPACE
               ' FOR MEMBER ' DELIMITED BY SIZE WS-MEMBER-ID ... INTO WS-ERR-MSG
    -> 'INVALID GENDER <WS-GENDER-CD> FOR MEMBER <WS-MEMBER-ID>'

    Searching for the message text 'GENDER' must find this even though no
    single literal contains the whole sentence. Figurative constants (SPACE,
    ZERO) used as delimiters are not part of the text.
    """
    parts: List[str] = []
    has_literal = False
    toks = list(_TOKEN.finditer(fragment or ""))
    i = 0
    while i < len(toks):
        t = toks[i].group(0)
        up = t.upper()
        if t[0] in ("'", '"'):
            parts.append(_norm_lit(t))
            has_literal = True
        elif up in ("DELIMITED",):
            i += 1
            if i < len(toks) and toks[i].group(0).upper() == "BY":
                i += 1                              # skip the delimiter operand too
            i += 1
            continue
        elif up in _TEMPLATE_SKIP or up in ("SPACE", "SPACES", "ZERO", "ZEROS", "ZEROES",
                                             "LOW-VALUE", "LOW-VALUES", "HIGH-VALUE", "HIGH-VALUES"):
            pass
        elif re.fullmatch(ID, up.split("(")[0], re.I) and up.split("(")[0] not in _RESERVED:
            parts.append(f"<{up.split('(')[0]}>")
        i += 1
    if has_literal and len(parts) >= 2:
        return "".join(parts)
    return None
