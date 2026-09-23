"""
flow.py - where a VALUE goes (`flow FIELD`, docs/PLAN-value-flow.md section 4).

A node is bytes inside one 01 of one program: (program, root pfield, lo, hi).
Never a bare name - WS-STATUS is three unrelated fields in three programs.
From a node the walk follows, in a fixed order (cross-program edges first):
the file the record is written to and every step and program that reads
those bytes, CALL USING positions into the callee, LINKAGE positions back to
the callers, DB2 columns to their static readers, IMS segments by DBD, CICS
queues and maps, MQ queues, pointer aliases, and last the local copies.
Every place the walk widens (a group MOVE, an OCCURS, a REDEFINES) or stops
is LABELLED with a fixed string; nothing is followed silently.

Not a fact module: the walk reads data_flow / pfield / call_arg / param /
file_record that build.py stored, so a fix here never costs a re-parse.

On an index built before those tables existed (his index at work until the
next night) the same questions are answered from field_ref, call_edge and
sql_col_ref alone: MOVE pairs only from lines that hold exactly one MOVE
read and one MOVE write, every hop marked (reconstructed).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import cobol
from . import query as Q
from . import verify_citations as V

FOOTER = "Flow-insensitive: statement order and IF guards are not evaluated; a hop is a copy that CAN happen."
FALLBACK_HEADER = "index has no data_flow - reconstructed from field_ref; exact after the next re-parse"
FALLBACK_COPY = "copybook fields: not followed until re-parse"
FALLBACK_PARENT = "parent group not followed until re-parse"
FALLBACK_PARAM = "callee's fields under the parameter: not followed until re-parse"
FALLBACK_GROUP = "fields under the group: not followed until re-parse"

# Fixed end strings (plan section 5): each is a test assertion, so the words
# never change. `{}` slots are filled per hop.
END_TESTED = "only tested/displayed here"
END_NO_USE = "no further use in {p}"
END_UNNAMED = "group MOVE into unnamed bytes"
END_TRUNC = "truncated {n} -> {m}"
END_DERIVED = "derived - value not preserved (--derived)"
END_DYNAMIC = "dynamic CALL unresolved (via {x})"
END_NO_CALLEE = "callee not in index"
END_POS = "LINKAGE position out of range / count mismatch - HUMAN MUST VERIFY"
END_LENGTH_OF = "callee receives LENGTH OF, not the bytes"
END_NO_COMMAREA = "callee has no DFHCOMMAREA 01"
END_CONTENT = "BY CONTENT: one-way"
END_XCTL = "XCTL does not return"
END_NO_READER = "no indexed reader"
END_NO_WRITER = "no indexed writer"
END_SEEN_DSN = "dataset already followed in this flow"
END_NO_FIELD = "reader layout has no field at bytes {lo}-{hi}"
END_COPYBOOK = "copybook differs from the writer's: verify layout"
END_SORT = "sort step re-arranges bytes - mapping not indexed"
END_UTILITY = "utility step - bytes not modelled"
END_INTERFACE = "dataset leaves the mainframe (interfaces: {x})"
END_NO_DB2_READER = "DB2 column: no static reader"
END_NO_DB2_WRITER = "DB2 column: no static writer"
END_PCB = "PCB unresolved"
END_SEGMENT = "segment not resolved (RM-03)"
END_IO_PCB_OUT = "IMS message to the terminal / I/O PCB - not a database"
END_IO_PCB_IN = "IMS message from the terminal / I/O PCB - not a database"
END_RES_VAR = "{what} name not resolvable (variable {v})"
END_SCREEN = "screen field - a human sees it"
END_MQ = "MQ PUT to {q} (peer from manifest)"
END_ODO = "offset is a maximum (ODO)"
END_MISSING = "field not declared in this program (missing copybook {x})"
END_AMBIGUOUS = "ambiguous name - HUMAN MUST VERIFY"
END_HOPS = "hop limit {n}"
END_WIDTH = "width cap"
END_NODES = "node cap"

# The member whose line numbers a DD row carries. A step expanded from a PROC into a job keeps the
# PROC's lines (the step, its DDs, an FTP / NDM pseudo-DD on its EXEC) but belongs to the job; a
# //STEP.DD override and a JOBLIB DD are the job's own lines. The PROC is the in-stream one of the
# job's member first (JCL looks there first), else the one cataloged PROC of that name; with none
# or several the job's member stays, and a cite there fails the gate rather than pass on a
# statement it does not mean.
_DD_MEMBER = """COALESCE(CASE WHEN s.from_proc IS NOT NULL AND COALESCE(d.is_override, 0)=0
                                   AND COALESCE(d.mode_source, '')<>'joblib' THEN
                    COALESCE((SELECT MIN(mi.name) FROM proc_def pi JOIN member mi ON mi.id=pi.member_id
                              WHERE UPPER(pi.proc_name)=UPPER(s.from_proc) AND pi.instream=1 AND pi.member_id=j.member_id),
                             (SELECT CASE WHEN COUNT(DISTINCT pc.member_id)=1 THEN MIN(mc.name) END
                              FROM proc_def pc JOIN member mc ON mc.id=pc.member_id
                              WHERE UPPER(pc.proc_name)=UPPER(s.from_proc) AND pc.instream=0)) END,
                m.name)"""

# The parser stores the COMMAREA of EXEC CICS LINK / XCTL / RETURN as a write too (the program it goes to
# may change it), as it stores a CALL argument. Either is a hand-over, never a statement of this program
# that sets the bytes: a program that only hands its parameter on is no origin (--up) and wrote nothing
# (rule 3). A LINK is a way back only when passes_on finds one; XCTL and RETURN never come back. The row is
# matched to its own statement (same line, the name in its COMMAREA), so RESP(x) on a LINK stays a write.
_CA_HANDOVER = ("(field_ref.stmt IN ('EXEC-CICS-LINK','EXEC-CICS-XCTL','EXEC-CICS-RETURN') AND EXISTS ("
                "SELECT 1 FROM call_edge ce WHERE ce.program_id=field_ref.program_id AND ce.line=field_ref.line "
                "AND ce.kind IN ('cics_link','cics_xctl','cics_return') "
                "AND UPPER(ce.using_args) LIKE '%\"' || UPPER(field_ref.name) || '\"%'))")
# a field_ref write that sets the bytes here (the query reads field_ref under its own name)
_SETS_HERE = f"mode='write' AND stmt<>'CALL-USING' AND NOT {_CA_HANDOVER}"

_NO_CUT = 1 << 30                   # passes_on: no cut ran into an item being asked about

COPY_KINDS = ("move", "move_corr", "set", "read_into", "write_from")
DERIVED_KINDS = ("arith", "string", "unstring", "function", "inspect")
SET_KINDS = ("literal", "figurative", "initialize", "accept")
TOKEN_MAX = 40
_CALL_WORD = {"static": "CALL", "dynamic": "CALL", "cics_link": "LINK", "cics_xctl": "XCTL", "cics_start": "START",
              "cics_return": "RETURN", "proc_call": "CALL"}
# CICS carriers whose resource the parser may hold as a variable's name (QUEUE(x), FILE(x), CONTAINER(x))
_CICS_CARRIERS = {"tsq": "queue", "tdq": "queue", "queue": "queue", "file": "file", "container": "container"}
_REFMOD = re.compile(r"^\(\s*(\d+)\s*:\s*(\d+)\s*\)$")
_STOP_WORDS = {"ELSE", "END-IF", "END-EVALUATE", "END-PERFORM", "END-CALL", "END-STRING", "END-UNSTRING",
               "IF", "MOVE", "PERFORM", "GO", "CALL", "DISPLAY", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY",
               "DIVIDE", "STRING", "UNSTRING", "SET", "READ", "WRITE", "EXEC", "EVALUATE", "WHEN", "INITIALIZE",
               "ACCEPT", "OPEN", "CLOSE", "GOBACK", "STOP", "CONTINUE", "NEXT", "EXIT", "INSPECT", "ROUNDED",
               "ON", "SIZE", "NOT", "DELIMITED", "BY", "WITH", "POINTER", "OVERFLOW", "DELIMITER", "COUNT",
               "IN", "OF", "REFERENCE", "CONTENT", "VALUE", "LENGTH", "ADDRESS", "OMITTED", "RETURNING",
               "USING", "TO", "FROM", "GIVING", "INTO", "CORR", "CORRESPONDING", "REMAINDER", "AT", "END",
               "INVALID", "KEY", "AND", "OR", "THRU", "THROUGH", "UNTIL", "VARYING", "TIMES", "ALSO", "OTHER"}


class Opts:
    def __init__(self, up: bool = False, hops: int = 3, width: int = 12, nodes: int = 200, derived: bool = False,
                 show_all: bool = False, budget: Optional[int] = None, header: bool = True) -> None:
        self.up = up
        self.hops = max(0, hops)
        self.width = max(1, width)
        self.nodes = max(1, nodes)
        self.derived = derived
        self.show_all = show_all
        self.budget = budget
        self.header = header


def has_flow_tables(conn: sqlite3.Connection) -> bool:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                                        "('data_flow','pfield','call_arg','param','file_record')")}
    return {"data_flow", "pfield", "call_arg", "param", "file_record"} <= names


# ---------------------------------------------------------------------------
# shared: lines, ends, cites
# ---------------------------------------------------------------------------

class _Node:
    """`via_call`: the call_edge id when the node is a callee's parameter
    reached THROUGH that CALL (or, under --up, written back through it), and
    `via_root` that parameter's 01. The parameter then IS that caller's
    storage: the value cannot reach another invocation's argument, and back
    through the same CALL is the argument it came from - so the parameter
    itself has no LINKAGE-back / param-in edges. A copy inside the callee
    into LINKAGE or LOCAL-STORAGE keeps `via_call` (the same invocation):
    from there LINKAGE-back / RETURNING / param-in take that one CALL, never
    every caller. WORKING-STORAGE outlives the invocation, so a copy there
    drops it."""
    __slots__ = ("pid", "pname", "root", "lo", "hi", "pf", "hop", "unnamed", "kind", "data", "name", "via_call",
                 "via_root")

    def __init__(self, pid: int, pname: str, root: int, lo: int, hi: int, pf, hop: int,
                 unnamed: bool = False, kind: str = "field", data=None, name: Optional[str] = None) -> None:
        self.pid, self.pname, self.root, self.lo, self.hi, self.pf, self.hop = pid, pname, root, lo, hi, pf, hop
        self.unnamed, self.kind, self.data, self.name = unnamed, kind, data, name
        self.via_call: Optional[int] = None
        self.via_root: Optional[int] = None


class _Edge:
    """One thing the walk can print under a node: a hop to `child`, an end
    (`end` set, no child), or a table (DB2 readers). `rank` is the plan's
    fixed order; `prog` names the program a dropped branch would have led to."""
    __slots__ = ("rank", "prog", "line", "label", "cites", "guard", "child", "end", "table", "extra")

    def __init__(self, rank: int, prog: str, line: int, label: str, cites: str, guard: Optional[str] = None,
                 child: Optional[_Node] = None, end: Optional[str] = None, table: Optional[str] = None,
                 extra: Optional[List[str]] = None) -> None:
        self.rank, self.prog, self.line, self.label, self.cites, self.guard = rank, prog, line, label, cites, guard
        self.child, self.end, self.table, self.extra = child, end, table, extra or []


class _Leaf:
    """What a dataset hop reaches after the pass-through steps: a reader
    node, or an end. `path` is the pass-through lines printed before it."""
    __slots__ = ("path", "text", "cites", "node", "end")

    def __init__(self, path: List[Tuple[str, str]], text: str, cites: str, node: Optional[_Node] = None,
                 end: Optional[str] = None) -> None:
        self.path, self.text, self.cites, self.node, self.end = path, text, cites, node, end


class _Report:
    """The line buffer and the bookkeeping both walkers share: node numbers,
    the visited ranges (cycles), the ends, the caps, and the cite forms."""

    def __init__(self, conn: sqlite3.Connection, opts: Opts) -> None:
        self.conn = conn
        self.o = opts
        self.lines: List[Tuple[int, str]] = []          # (depth, text); depth 0 = the root's own lines
        self.ends: Dict[str, List[str]] = {}
        self.end_order: List[str] = []
        self.count = 0                                  # nodes printed, the root excluded
        self.derived = 0
        self.dropped_width = 0
        self.dropped_nodes = 0
        self.unlinked = 0
        self.members: Set[int] = set()
        self.programs: Set[int] = set()
        self.root_pid: int = 0
        self._member_tag: Dict[int, str] = {}
        self._raw: Dict[Tuple[str, int], str] = {}
        # passes_on (--up): the items being asked about (key -> depth), the answers kept, the
        # shallowest asked-about item a cut ran into, and the way back of the last `yes`
        self._passing: Dict[Tuple, int] = {}
        self._pass_memo: Dict[Tuple, Tuple[bool, Tuple]] = {}
        self._pass_low = _NO_CUT
        self._witness: Tuple = ()

    def pass_search(self, key: Tuple, left: int, edges: Callable[[], Iterator[Tuple[_Edge, Tuple]]]) -> bool:
        """passes_on's search, both walkers: is there a way back (an edge of
        arg_back other than an XCTL end) from the item `key` with `left` hops
        to go? It stops at the first. A way that comes back to an item being
        asked about is none (the cut). Each answer is kept per (key, left),
        so a lattice of callees is asked once per item, not once per path: a
        `no` only when no cut reached an item asked about further up (the
        same question from elsewhere may have its way through there), a
        `yes` with the items its way goes through - taken again only while
        none of them is being asked about, as the walk without the answers
        kept would find it."""
        if key in self._passing:
            self._pass_low = min(self._pass_low, self._passing[key])
            return False
        mkey = key + (left,)
        kept = self._pass_memo.get(mkey)
        if kept is not None and (not kept[0] or not any(k in self._passing for k in kept[1])):
            self._witness = kept[1]
            return kept[0]
        depth = len(self._passing)
        self._passing[key] = depth
        outer_low, self._pass_low = self._pass_low, _NO_CUT
        programs, members = set(self.programs), set(self.members)
        found, way = False, ()
        try:
            for e, w in edges():
                if e.end != END_XCTL:
                    found, way = True, (key,) + w
                    break
        finally:
            del self._passing[key]
            self.programs, self.members = programs, members     # asking prints nothing: touches nothing
            low, self._pass_low = self._pass_low, outer_low
        if low < depth and not found:
            self._pass_low = min(self._pass_low, low)       # a `yes` holds whatever was cut
        if found or low >= depth:
            self._pass_memo[mkey] = (found, way)
        self._witness = way
        return found

    # ---- output ----------------------------------------------------------
    def put(self, depth: int, text: str) -> None:
        self.lines.append((depth, text))

    def end(self, reason: str, num: str) -> str:
        if reason not in self.ends:
            self.ends[reason] = []
            self.end_order.append(reason)
        self.ends[reason].append(num)
        return f"   [end: {reason}]"

    @staticmethod
    def fmt(num: str, depth: int, text: str) -> str:
        return f"{num:<7}{'  ' * max(0, depth - 1)}{text}"

    @staticmethod
    def sub(depth: int, text: str) -> str:
        return f"{'':7}{'  ' * depth}{text}"

    def hop_prefix(self, node: _Node) -> str:
        return "" if node.pid == self.root_pid else f"{node.pname}."

    # ---- members / source --------------------------------------------------
    def member_tag(self, mid: Optional[int]) -> Optional[str]:
        if mid is None:
            return None
        if mid not in self._member_tag:
            r = self.conn.execute("SELECT name, system FROM member WHERE id=?", (mid,)).fetchone()
            self._member_tag[mid] = (f"{r['system']}/{r['name']}" if r and r["system"] else (r["name"] if r else None))
        return self._member_tag[mid]

    def raw(self, tag: str, line: int) -> str:
        key = (tag, line)
        if key not in self._raw:
            self._raw[key] = Q.source_line_raw(self.conn, tag, line) if line else ""
        return self._raw[key]

    # ---- cites ---------------------------------------------------------------
    @staticmethod
    def _tok(text: str) -> str:
        # the line's own spacing is kept ("01  WS-STATUS" reads as the source
        # does); the gate collapses whitespace, so a joined or cut token passes too
        tok = text.strip().rstrip(".").rstrip()
        if "\n" in tok or len(tok) > TOKEN_MAX:
            tok = re.sub(r"\s+", " ", tok)
        if len(tok) > TOKEN_MAX:
            cut = tok[-TOKEN_MAX:]
            sp = cut.find(" ")
            if 0 <= sp < TOKEN_MAX - 6:
                cut = cut[sp + 1:]
            tok = cut
        return tok.replace('"', '\\"')

    def cite_stmt(self, pid: int, exp_line: Optional[int], verb: Optional[str], operand: Optional[str]) -> str:
        """MEMBER:line "token" for a statement: the token runs from the verb
        keyword through the operand that carries the value (<= 40 chars);
        an operand on a continuation line makes the cite a line range."""
        m, ln, depth, via = Q.origin(self.conn, pid, exp_line)
        if not m or ln is None:
            return f"?:{exp_line}"
        via_tag = f" (via COPY {via})" if depth else ""
        raw0 = self.raw(m, ln)
        if not raw0.strip():
            return f"{m}:{ln}{via_tag}"
        vword = (verb or "").upper()
        if vword.startswith("EXEC-CICS-"):
            vword = vword[len("EXEC-CICS-"):]
        vrx = re.compile(r"(?<![\w-])" + re.escape(vword) + r"(?![\w-])", re.I) if vword else None
        orx = re.compile(r"(?<![\w-])" + re.escape(operand) + r"(?![\w-])", re.I) if operand else None
        vs = [x.start() for x in vrx.finditer(raw0)] if vrx else []
        start = vs[0] if vs else (len(raw0) - len(raw0.lstrip()))
        end_line = ln
        token = None
        if orx:
            om = orx.search(raw0, start)
            if om:
                v = max([x for x in vs if x <= om.start()] or [start])
                head = raw0[:v].rstrip().upper()
                if head.endswith("ELSE") or head.endswith("WHEN"):
                    v = raw0.upper().rfind(head[-4:], 0, v)
                token = raw0[v:om.end()]
            else:
                parts = [raw0[start:].strip()]
                for k in range(1, 7):
                    if self.raw(m, ln + k - 1).rstrip().endswith("."):
                        break                   # the statement ended: never cite into the next one
                    rawk = self.raw(m, ln + k)
                    if not rawk.strip():
                        break
                    omk = orx.search(rawk)
                    if omk:
                        parts.append(rawk[:omk.end()].strip())
                        end_line = ln + k
                        break
                    parts.append(rawk.strip())
                else:
                    parts = [raw0[start:].strip()]
                if end_line == ln:
                    parts = [raw0[start:].strip()]
                token = " ".join(parts)
        if token is None:
            token = raw0[start:].strip()
        tok = self._tok(token)
        if len(tok) < V.WEAK_TOKEN_CHARS:
            tok = self._tok(raw0.strip())
        if end_line - ln > 20:
            end_line = ln
            tok = self._tok(raw0[start:].strip())
        rng = f"{m}:{ln}" if end_line == ln else f"{m}:{ln}-{end_line}"
        return f'{rng}{via_tag} "{tok}"'

    def cite_def(self, row, pid: Optional[int] = None) -> str:
        """MEMBER:line "05  NAME" for a data item: the member that holds the
        line (the copybook for a copied item) without the via note, the
        level and the name as the token."""
        tag = self.member_tag(row["src_member"]) if "src_member" in row.keys() else None
        line = row["src_line"] if "src_line" in row.keys() else None
        if not tag or line is None:
            m, ln, _d, _v = Q.origin(self.conn, pid or 0, row["exp_line"] if "exp_line" in row.keys() else row["line"])
            tag, line = m, ln
        if not tag or line is None:
            return "?"
        raw = self.raw(tag, line)
        mm = re.search(r"(\d\d\s+" + re.escape(row["name"]) + r")(?![\w-])", raw, re.I)
        tok = self._tok(mm.group(1) if mm else (raw.strip() if raw.strip() else row["name"]))
        return f'{tag}:{line} "{tok}"'

    def cite_sql(self, pid: int, line: int, col: str, host_var: Optional[str] = None) -> str:
        """sql_stmt start-end with the column as the token; a FETCH names no
        column (the cursor's DECLARE does), so there the host variable is the
        token, on the line that holds it."""
        st = self.conn.execute("SELECT start_line, end_line FROM sql_stmt WHERE program_id=? AND start_line<=? "
                               "AND COALESCE(end_line,start_line)>=? ORDER BY start_line DESC LIMIT 1",
                               (pid, line, line)).fetchone()
        s, e = (st["start_line"], st["end_line"] or st["start_line"]) if st else (line, line)
        m1, l1, d1, v1 = Q.origin(self.conn, pid, s)
        m2, l2, _d2, _v2 = Q.origin(self.conn, pid, e)
        if not m1 or l1 is None:
            return f"?:{line}"
        if m2 != m1 or l2 is None or l2 < l1 or l2 - l1 > V.WIDE_RANGE_LINES:
            l2 = l1
        via = f" (via COPY {v1})" if d1 else ""
        rng = f"{m1}:{l1}" if l1 == l2 else f"{m1}:{l1}-{l2}"
        text = " ".join(self.raw(m1, ln) for ln in range(l1, l2 + 1)).upper()
        if not re.search(r"(?<![\w-])" + re.escape(col.upper()) + r"(?![\w-])", text) and host_var:
            for ln in range(l1, l2 + 1):
                raw = self.raw(m1, ln)
                hm = re.search(r":\s*" + re.escape(host_var) + r"(?![\w-])", raw, re.I)
                if hm:
                    tok = hm.group(0) if len(hm.group(0)) >= V.WEAK_TOKEN_CHARS else raw.strip()
                    return f'{m1}:{ln}{via} "{self._tok(tok)}"'
        tok = col if len(col) >= V.WEAK_TOKEN_CHARS else None
        if tok is None:
            hit = next((ln for ln in range(l1, l2 + 1) if col.upper() in self.raw(m1, ln).upper()), l1)
            t = self.raw(m1, hit).strip()
            tok = self._tok(t) if t else col
            rng = f"{m1}:{hit}"
        return f'{rng}{via} "{tok}"'

    def cite_dd(self, member: Optional[str], line: Optional[int], dd_name: str, with_dsn: bool,
                step: Optional[str] = None) -> str:
        """MEMBER:line "//NAME DD" for a DD. A pseudo-DD the JCL parser adds
        on the EXEC line (`*FTP*`, `*NDM*`: the dataset an FTP / NDM step
        sends) quotes the EXEC text there only when that EXEC is the step's
        own (its label is the last part of `step`). A concatenated dataset's
        line has no name field and quotes its own `// DD DSN=...`. Anything else quotes the
        statement the cite claims (`//NAME DD`, `//STEP EXEC`), so a line
        that does not hold it FAILS the citation gate visibly - never the
        text of whatever other statement is on that line, never no token."""
        if not member or not line:
            return f"{member or '?'}:{line or '?'}"
        raw = self.raw(member, line)
        dd_name = dd_name or ""
        if dd_name.startswith("*"):
            label = (step or "?").rpartition(".")[2].upper()
            ex = re.match(r"//(\S*)\s+EXEC\s+[^\s,]+", raw)
            if ex and ex.group(1).upper() == label:
                return f'{member}:{line} "{self._tok(ex.group(0))}"'
            return f'{member}:{line} "//{label} EXEC"'
        # a concatenated dataset (`//   DD DSN=B` under `//QIN DD DSN=A`) is stored under the
        # previous DD's name (concat_seq > 0) with its own line, which has no name field: quote
        # that line's own statement and operand, whitespace collapsed as the gate compares it
        cm = re.match(r"//\s+DD(?![\w-])(?:\s+([^,\s]+))?", raw)
        if cm:
            return f'{member}:{line} "{self._tok("// DD" + (" " + cm.group(1) if cm.group(1) else ""))}"'
        # the DD on the line must be this one (an override //S1.GIN DD is GIN): another DD there
        # would pass the gate with a name the flow never meant
        mm = re.match(r"//(\S*)\s+DD\b", raw)
        if mm and mm.group(1).upper().rpartition(".")[2] != dd_name.upper().rpartition(".")[2]:
            mm = None
        tok = f"//{mm.group(1) if mm else dd_name} DD"
        if mm and with_dsn:
            dm = re.search(r"DSN(?:AME)?=([^,\s(]+)", raw, re.I)
            if dm and len(tok) + 5 + len(dm.group(1)) <= TOKEN_MAX:
                tok += f" DSN={dm.group(1)}"
        return f'{member}:{line} "{tok}"'

    def cite_card(self, step_id: int, kind: str) -> str:
        r = self.conn.execute("""SELECT d.sysin_text, d.card_member, d.line, m.name AS mem
                                 FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                 LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                 LEFT JOIN member m ON m.id=COALESCE(j.member_id, pd.member_id)
                                 WHERE d.step_id=? AND d.sysin_text IS NOT NULL ORDER BY d.line""", (step_id,)).fetchall()
        for d in r:
            for i, txt in enumerate(d["sysin_text"].splitlines()):
                if re.search(r"(?<![\w-])" + re.escape(kind) + r"(?![\w-])", txt, re.I):
                    if d["card_member"]:
                        return f'{d["card_member"]}:{i + 1} "{self._tok(txt)}"'
                    return f'{d["mem"]}:{(d["line"] or 0) + i + 1} "{self._tok(txt)}"'
        return "?"

    # ---- sections ------------------------------------------------------------
    def budget_cut(self) -> None:
        """--budget: drop the deepest tree lines first; the end reasons of
        what stays are kept, and the cut is said."""
        b = self.o.budget
        if not b:
            return
        total = sum(len(t) + 1 for _d, t in self.lines)
        cut = 0
        while total > b and self.lines:
            deepest = max(d for d, _t in self.lines)
            if deepest <= 1:
                break
            kept = [(d, t) for (d, t) in self.lines if d < deepest]
            cut += len(self.lines) - len(kept)
            self.lines = kept
            total = sum(len(t) + 1 for _d, t in self.lines)
        if cut:
            self.put(1, f"... --budget {b}: {cut} deeper line(s) cut; their end reasons stay listed under Ends")

    def sections(self, root_pname: str, extra_not_followed: Sequence[str] = ()) -> List[str]:
        out = ["## Not followed"]
        if self.o.derived:
            out.append("- derived edges (COMPUTE/STRING/FUNCTION): followed (--derived)")
        else:
            out.append(f"- derived edges (COMPUTE/STRING/FUNCTION): {self.derived} (--derived follows them)")
        if self.unlinked:
            out.append(f"- {self.unlinked} statement(s) name an item with no pfield link (ambiguous or undeclared: "
                       "see Unresolved in scope)")
        out.extend(extra_not_followed)
        out.append("## Ends")
        any_end = False
        caps = {END_WIDTH: f"{self.dropped_width} branch(es) collapsed (--all shows them)",
                END_NODES: f"{self.dropped_nodes} branch(es) not printed (--nodes {self.o.nodes})"}
        for reason in self.end_order:
            nums = self.ends[reason]
            out.append(f"- {reason}: " + ", ".join(nums[:12]) + (f" (+{len(nums) - 12} more)" if len(nums) > 12 else "")
                       + (f" - {caps[reason]}" if reason in caps else ""))
            any_end = True
        if self.derived and not self.o.derived:
            out.append(f"- {END_DERIVED}: {self.derived} edge(s)")
            any_end = True
        if not any_end:
            out.append("- none")
        return out


# ---------------------------------------------------------------------------
# the walker over pfield / data_flow (the normal mode)
# ---------------------------------------------------------------------------

def _refmod(text: Optional[str]) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    if not text:
        return None, None
    m = _REFMOD.match(text.strip())
    if m:
        return (int(m.group(1)), int(m.group(2))), None
    return None, "(ref-mod with variable position: whole item)"


def _how_resolved(c) -> str:
    """call_edge.resolution in words: how a dynamic CALL's target was found."""
    how = c["resolution"] if "resolution" in c.keys() else None
    return {"move_literal": "MOVE literal", "value_clause": "VALUE clause"}.get(how or "", how or "?")


def _candidates(c) -> List[Tuple[str, str]]:
    """A dynamic CALL's targets, one labelled branch each (guard 19); a
    `(+N more)` entry is kept so the caller can end on it, never drop it."""
    return [(t, f"candidate: resolved via {_how_resolved(c)}") for t in Q._jl(c["resolved"])]


def _note_file(note: Optional[str]) -> Optional[str]:
    """The SELECT name of an io_in / io_out note: `file STAT-OUT multi-record` -> STAT-OUT."""
    parts = (note or "").split()
    return parts[1] if len(parts) > 1 and parts[0] == "file" else None


def _same_run(d: dict, s) -> bool:
    """A temp dataset (&&X) lives only inside one run: the same job, or -
    for the steps of a PROC definition, which have no job - the same PROC.
    Two PROCs (job_id NULL on both sides) are not one run (guard 17)."""
    if d.get("job_id") is not None:
        return s["job_id"] == d["job_id"]
    return s["job_id"] is None and d.get("proc_id") is not None and s["proc_id"] == d.get("proc_id")


def _numeric(row) -> bool:
    pic = (row["pic"] or "").upper()
    usage = (row["usage"] or "").upper()
    if usage.startswith("COMP") or usage in ("BINARY", "PACKED-DECIMAL", "INDEX"):
        return True
    return bool(pic) and "9" in pic and not any(c in pic for c in "XA")


def _picstr(row) -> str:
    pic = row["pic"]
    usage = row["usage"]
    if pic:
        return pic + (f" {usage}" if usage and usage.upper() != "DISPLAY" else "")
    if usage:
        return usage
    return f"group of {row['length']} bytes"


class _Walker(_Report):

    def __init__(self, conn: sqlite3.Connection, opts: Opts) -> None:
        super().__init__(conn, opts)
        self._pf: Dict[int, sqlite3.Row] = {}
        self._root_items: Dict[int, List[sqlite3.Row]] = {}
        self._prog: Dict[int, sqlite3.Row] = {}
        self._dup: Dict[Tuple[int, str], int] = {}
        self._aliases: Dict[int, Set[str]] = {}
        self.visited: Dict[Tuple[int, int], List[Tuple[int, int, str, Optional[int], int]]] = defaultdict(list)
        self._partial_shown: Set[int] = set()
        self._layout: Dict[int, Optional[str]] = {}
        self._names: Dict[int, Set[str]] = {}

    # ---- lookups -------------------------------------------------------------
    def pf(self, pf_id: Optional[int]):
        if pf_id is None:
            return None
        if pf_id not in self._pf:
            self._pf[pf_id] = self.conn.execute("SELECT * FROM pfield WHERE id=?", (pf_id,)).fetchone()
        return self._pf[pf_id]

    def root_items(self, root_id: int) -> List[sqlite3.Row]:
        if root_id not in self._root_items:
            rows = self.conn.execute("SELECT * FROM pfield WHERE root_id=? ORDER BY offset, id", (root_id,)).fetchall()
            self._root_items[root_id] = rows
            for r in rows:
                self._pf[r["id"]] = r
        return self._root_items[root_id]

    def prog(self, pid: int):
        if pid not in self._prog:
            self._prog[pid] = self.conn.execute(
                "SELECT p.*, m.name AS member_name, m.parse_status FROM program p JOIN member m ON m.id=p.member_id "
                "WHERE p.id=?", (pid,)).fetchone()
        return self._prog[pid]

    def aliases(self, pid: int) -> Set[str]:
        if pid not in self._aliases:
            self._aliases[pid] = {r[0].upper() for r in self.conn.execute(
                "SELECT alias FROM program_alias WHERE program_id=?", (pid,))}
        return self._aliases[pid]

    def dup(self, pid: int, name: str) -> int:
        key = (pid, name.upper())
        if key not in self._dup:
            self._dup[key] = self.conn.execute("SELECT COUNT(*) FROM pfield WHERE program_id=? AND UPPER(name)=?",
                                               key).fetchone()[0]
        return self._dup[key]

    @staticmethod
    def extent(row) -> Tuple[int, int]:
        n = row["occurs_max"] or 1
        return row["offset"], row["offset"] + row["length"] * max(1, n)

    def ancestors(self, row) -> List[sqlite3.Row]:
        out = []
        p = row["parent_id"]
        while p is not None:
            r = self.pf(p)
            if r is None:
                break
            out.append(r)
            p = r["parent_id"]
        return out

    def redefines_chain(self, row) -> bool:
        if row["redefines"]:
            return True
        return any(a["redefines"] for a in self.ancestors(row))

    def touch(self, pid: int) -> None:
        self.programs.add(pid)
        p = self.prog(pid)
        if p:
            self.members.add(p["member_id"])

    # ---- naming ----------------------------------------------------------------
    def base_name(self, pid: int, row) -> str:
        name = row["name"]
        if self.dup(pid, name) > 1 and row["qualified"] and "." in row["qualified"]:
            return f"{name} OF {row['qualified'].split('.')[-2]}"
        return name

    def nm(self, node: _Node) -> str:
        if node.name:
            return node.name
        pf = node.pf
        base = self.base_name(node.pid, pf)
        lo, hi = self.extent(pf)
        if node.unnamed:
            return f"unnamed bytes of {base} ({_picstr(pf)})"
        if (node.lo, node.hi) != (lo, hi):
            return f"{base} (bytes {node.lo - lo + 1}-{node.hi - lo} of {hi - lo})"
        return base

    def desc(self, node: _Node, full: bool = False) -> str:
        pf = node.pf
        if pf is None:
            return ""
        sec = pf["section"]
        root = self.pf(node.root)
        pic = _picstr(pf)
        notes = []
        if pf["after_odo"]:
            notes.append(END_ODO)
        p = self.prog(node.pid)
        if p and pf["src_member"] is not None and pf["src_member"] != p["member_id"] and sec in ("FILE", "LINKAGE"):
            notes.append(f"copybook {self.member_tag(pf['src_member'])}")
        if sec in ("FILE", "LINKAGE") or root is not None and root["id"] != pf["id"]:
            lw = self.layout_note(node.pid)
            if lw:
                notes.append(lw)
        tail = ("; " + "; ".join(notes)) if notes else ""
        if sec == "FILE":
            return f"(FILE {root['name']} bytes {node.lo + 1}-{node.hi}, {pic}{tail})"
        if sec == "LINKAGE":
            if root is not None and root["id"] != pf["id"]:
                return f"(LINKAGE {root['name']} bytes {node.lo + 1}-{node.hi}, {pic}{tail})"
            return f"(LINKAGE, {pic}{tail})"
        if full:
            return f"({sec}, {pic}{tail})"
        return f"({END_ODO})" if pf["after_odo"] else ""

    def layout_note(self, pid: int) -> Optional[str]:
        """A layout_warning on the program (REDEFINES larger than its object,
        SYNC slack): byte offsets there are not exact (guard 15)."""
        if pid not in self._layout:
            p = self.prog(pid)
            n = self.conn.execute("SELECT COUNT(*) FROM unresolved WHERE member_id=? AND kind='layout_warning'",
                                  (p["member_id"],)).fetchone()[0] if p else 0
            self._layout[pid] = (f"layout warning in {p['member_name']}: offsets HUMAN MUST VERIFY" if n else None)
        return self._layout[pid]

    def partial_note(self, pid: int) -> Optional[str]:
        p = self.prog(pid)
        if not p or p["parse_status"] != "partial":
            return None
        rows = self.conn.execute("""SELECT detail FROM unresolved WHERE member_id=? AND kind IN ('expand','missing_copybook')
                                    ORDER BY id LIMIT 4""", (p["member_id"],)).fetchall()
        det = "; ".join(r["detail"].split(" - ")[0].replace("]", ")") for r in rows) or "facts incomplete"
        return f"[program partial: {det}]"

    # ---- closure -----------------------------------------------------------------
    def closure(self, node: _Node) -> List[Tuple[sqlite3.Row, str]]:
        """Every item of the root whose bytes intersect the node's, with the
        reason it shares them (guard 7): printed before any edge is followed."""
        out = []
        anc = {a["id"] for a in self.ancestors(node.pf)} if node.pf is not None else set()
        self_id = node.pf["id"] if node.pf is not None else None
        for r in self.root_items(node.root):
            lo, hi = self.extent(r)
            if hi <= node.lo or lo >= node.hi:
                continue
            if r["id"] == self_id:
                reason = "self"
            elif r["id"] in anc:
                reason = "parent group"
            elif node.pf is not None and self_id in {a["id"] for a in self.ancestors(r)}:
                reason = "child"
            elif self.redefines_chain(r) or (node.pf is not None and self.redefines_chain(node.pf)):
                reason = "REDEFINES"
            else:
                reason = "overlaps"
            bits = [reason]
            if reason == "self":
                out.append((r, reason))
                continue
            if r["occurs_max"]:
                bits.append("OCCURS element, index unknown")
            if r["after_odo"]:
                bits.append(END_ODO)
            out.append((r, ", ".join(bits)))
        # nearest ancestor first, then by offset
        depth = {a["id"]: i for i, a in enumerate(self.ancestors(node.pf))} if node.pf is not None else {}
        out.sort(key=lambda x: (0 if x[1] == "self" else 1 if x[0]["id"] in depth else 2,
                                depth.get(x[0]["id"], 0), x[0]["offset"], x[0]["id"]))
        return out

    def closure_ids(self, node: _Node) -> Tuple[Set[int], List[Tuple[sqlite3.Row, str]]]:
        cl = self.closure(node)
        return {r["id"] for r, _why in cl}, cl

    def place(self, dst, lo: int, hi: int) -> List[Tuple[sqlite3.Row, int, int, str]]:
        """Which named items of the destination root the bytes [lo,hi) land
        in: the item itself when exact, its covering elementary children, or
        'unnamed' when nothing names those bytes (guard 6)."""
        d0, d1 = self.extent(dst)
        if not dst["is_group"]:
            if (lo, hi) == (d0, d1):
                return [(dst, lo, hi, "exact")]
            if dst["parent_id"] is None:
                return [(dst, lo, hi, "unnamed")]
            return [(dst, lo, hi, "partial")]
        under = {dst["id"]}
        items = []
        for r in self.root_items(dst["root_id"]):
            if r["parent_id"] in under:
                under.add(r["id"])
            if r["id"] == dst["id"] or r["id"] not in under or r["is_group"] or self.redefines_chain(r):
                continue
            a, b = self.extent(r)
            if b <= lo or a >= hi:
                continue
            xlo, xhi = max(lo, a), min(hi, b)
            items.append((r, xlo, xhi, "exact" if (xlo, xhi) == (a, b) else "partial"))
        if not items:
            return [(dst, lo, hi, "unnamed")]
        return items

    def child(self, pid: int, row, lo: int, hi: int, how: str, hop: int) -> _Node:
        return _Node(pid, self.prog(pid)["program_id"], row["root_id"], lo, hi, row, hop, unnamed=(how == "unnamed"))

    @staticmethod
    def carry(frm: _Node, ch: _Node) -> _Node:
        """A copy inside one invocation (LINKAGE, LOCAL-STORAGE) keeps the
        CALL the value came through; WORKING-STORAGE outlives it (_Node)."""
        if frm.via_call is not None and ch.pid == frm.pid and ch.pf is not None \
                and ch.pf["section"] in ("LINKAGE", "LOCAL-STORAGE"):
            ch.via_call, ch.via_root = frm.via_call, frm.via_root
        return ch

    def seen(self, node: _Node) -> Optional[str]:
        # a parameter walked as one CALL's storage (via_call) has fewer edges than the
        # same parameter reached by a copy inside the callee: it never stands in for that,
        # nor for the same parameter reached through ANOTHER call (its edges take that call).
        # A visit at a DEEPER hop was cut sooner by --hops: a shorter path re-expands the node
        for (vlo, vhi, num, via, vhop) in self.visited[(node.pid, node.root)]:
            if vlo <= node.lo and node.hi <= vhi and (via is None or via == node.via_call) and vhop <= node.hop:
                return num
        return None

    def visit(self, node: _Node, num: str) -> None:
        self.visited[(node.pid, node.root)].append((node.lo, node.hi, num, node.via_call, node.hop))

    # ---- byte mapping between a source item and a destination item ------------------
    def map_bytes(self, node: _Node, src, src_refmod: Optional[str], dst, dst_refmod: Optional[str],
                  reverse: bool = False, src_sub: bool = False,
                  dst_sub: bool = False) -> Optional[Tuple[int, int, List[str], Optional[str]]]:
        """(lo, hi, labels, end) of the node's bytes after a copy from `src`
        to `dst`; None when the copy does not touch the node's bytes. With
        `reverse` the node lies in `dst` and the result is where in `src`
        those bytes came from. A subscripted side is ANY element: its bytes
        are taken element-relative, and a subscripted target is the whole
        table (index unknown)."""
        def span(row, refmod, sub):
            a, b = self.extent(row)
            rm, lbl = _refmod(refmod)
            if rm:
                a, b = a + rm[0] - 1, min(b, a + rm[0] - 1 + rm[1])
            unit = row["length"] if sub and row["occurs_max"] else None
            return a, b, unit, lbl

        s0, s1, su, s_lbl = span(src, src_refmod, src_sub)
        d0, d1, du, d_lbl = span(dst, dst_refmod, dst_sub)
        labels = [x for x in (s_lbl, d_lbl) if x]
        if reverse:
            s0, s1, su, d0, d1, du = d0, d1, du, s0, s1, su
            src, dst = dst, src
        x0, x1 = max(node.lo, s0), min(node.hi, s1)
        if x0 >= x1:
            return None
        slen, dlen = su or (s1 - s0), du or (d1 - d0)
        if not src["is_group"] and not dst["is_group"] and (_numeric(src) or _numeric(dst))                 and (_picstr(src) != _picstr(dst)):
            a, b = (_picstr(dst), _picstr(src)) if reverse else (_picstr(src), _picstr(dst))
            labels.append(f"converted {a} -> {b}")
            return d0, d1, labels, None
        # an alphanumeric MOVE is left-justified: a shorter sender fills the first bytes of the
        # receiver and the rest is padding, a longer one is cut - the value keeps its place, both
        # ways (guard 6). Never the whole longer item: its padding / cut bytes never held the value
        rel0 = (x0 - s0) % su if su else x0 - s0
        rel1 = rel0 + min(x1 - x0, su - rel0) if su else x1 - s0
        # the copy runs original source -> original target: under --up that is dst -> src here
        if reverse:
            trunc = END_TRUNC.format(n=dlen, m=slen) if dlen > slen else None
        else:
            trunc = END_TRUNC.format(n=slen, m=dlen) if slen > dlen else None
        if rel0 >= dlen:
            return d0, d1, labels, trunc or END_TRUNC.format(n=slen, m=dlen)
        rel1 = min(rel1, dlen)
        if trunc:
            labels.append(trunc)
        if du:
            return d0, d1, labels, None
        return d0 + rel0, d0 + rel1, labels, None

    def overlay(self, node: _Node, frm, to) -> Optional[Tuple[int, int, List[str], Optional[str]]]:
        """(lo, hi, labels, end) in `to` of the node's bytes in `frm` when the
        two SHARE storage from their first byte (CALL BY REFERENCE, LINKAGE,
        COMMAREA, pointer alias) or a carrier moves the bytes as they are (a
        queue, a segment, START data): same relative place, never converted,
        never spread over the whole receiving item (guards 6, 16). None when
        the node's bytes are outside `frm`; bytes past the end of `to` are
        cut and labelled."""
        f0, f1 = self.extent(frm)
        t0, t1 = self.extent(to)
        x0, x1 = max(node.lo, f0), min(node.hi, f1)
        if x0 >= x1:
            return None
        rel0, rel1, tlen = x0 - f0, x1 - f0, t1 - t0
        cut = END_TRUNC.format(n=f1 - f0, m=tlen)
        if rel0 >= tlen:
            return t0, t1, [], cut
        if rel1 > tlen:
            return t0 + rel0, t1, [cut], None
        return t0 + rel0, t0 + rel1, [], None

    def place_ref(self, dst, lo: int, hi: int) -> List[Tuple[sqlite3.Row, int, int, str]]:
        """place() for shared storage: part of an elementary item is that
        item's bytes a-b, not a group MOVE's unnamed bytes."""
        return [(row, a, b, "partial" if how == "unnamed" and not row["is_group"] else how)
                for (row, a, b, how) in self.place(dst, lo, hi)]

    def place_copy(self, src, dst, into, lo: int, hi: int) -> List[Tuple[sqlite3.Row, int, int, str]]:
        """place() for a copy from `src` to `dst` landing in `into`: an
        elementary MOVE between items of different lengths leaves the value
        in bytes a-b of the named item (the rest padding or cut), never a
        group MOVE's unnamed bytes."""
        if not src["is_group"] and not dst["is_group"]:
            return self.place_ref(into, lo, hi)
        return self.place(into, lo, hi)

    # ---- edges: down ------------------------------------------------------------------
    def edges(self, node: _Node) -> List[_Edge]:
        ids, _cl = self.closure_ids(node)
        out = self.edges_up(node, ids) if self.o.up else self.edges_down(node, ids)
        out.sort(key=lambda e: (e.rank, e.prog, e.line))
        return out

    def edges_down(self, node: _Node, ids: Set[int]) -> List[_Edge]:
        out: List[_Edge] = []
        pid = node.pid
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"SELECT * FROM data_flow WHERE program_id=? AND src_pfield IN ({q}) ORDER BY line, id",
                                 (pid, *ids)).fetchall()
        for r in rows:
            k = r["kind"]
            if k in COPY_KINDS:
                out.extend(self.copy_edges(node, r))
            elif k in DERIVED_KINDS:
                if self.o.derived:
                    out.extend(self.copy_edges(node, r, derived=True))
                else:
                    self.derived += 1
            elif k == "set_address":
                out.extend(self.pointer_edges(node, r))
            elif k == "cics_out":
                out.extend(self.cics_out_edges(node, r))
            elif k == "dli_out":
                out.extend(self.dli_edges(node, r))
            elif k == "mq_out":
                out.extend(self.mq_edges(node, r))
            elif k == "io_out" and r["src_pfield"] == node.root:
                out.extend(self.file_edges(node, r))
        out.extend(self.call_edges(node, ids))
        out.extend(self.linkage_back(node, ids))
        out.extend(self.sql_edges(node, ids, "write"))
        return out

    def copy_edges(self, node: _Node, r, derived: bool = False) -> List[_Edge]:
        pid = node.pid
        verb = r["verb"] or "MOVE"
        src, dst = self.pf(r["src_pfield"]), self.pf(r["dst_pfield"])
        if r["kind"] == "move_corr":
            return self.corr_edges(node, r, src, dst)
        cites = self.cite_stmt(pid, r["line"], verb, r["dst_name"])
        lbl_verb = {"read_into": "READ INTO", "write_from": "WRITE FROM", "set": "SET"}.get(r["kind"], verb)
        if derived:
            lbl_verb = f"{verb}"
        if dst is None or src is None:
            self.unlinked += 1
            e = _Edge(9, node.pname, r["line"], f"{lbl_verb} -> {r['dst_name']}", cites, r["guard"])
            e.end = END_AMBIGUOUS
            return [e]
        mapped = self.map_bytes(node, src, r["src_refmod"], dst, r["dst_refmod"],
                                src_sub=bool(r["src_sub"]), dst_sub=bool(r["dst_sub"]))
        if mapped is None:
            return []
        n_lo, n_hi, labels, end = mapped
        subs = self._subs(r, src, dst)
        if derived:
            # the value is not preserved: the whole target is the next node, no byte mapping
            n_lo, n_hi = self.extent(dst)
            labels, end, lbl_verb = [], None, f"derived ({verb})"
        rank = 10 if derived else (1 if dst["section"] == "FILE" else 3 if dst["section"] == "LINKAGE" else 9)
        s0, s1 = self.extent(src)
        group_src = bool(src["is_group"]) and (node.lo > s0 or node.hi < s1) and not derived
        if end:
            label = (f"group {lbl_verb} {src['name']} -> {self.base_name(pid, dst)}: bytes {node.lo + 1}-{node.hi} "
                     f"fall outside it" if group_src else f"{lbl_verb} -> {self.base_name(pid, dst)}{subs}")
            return [_Edge(rank, node.pname, r["line"], label + self._lbls(labels), cites, r["guard"], end=end)]
        out = []
        for (row, plo, phi, how) in self.place_copy(src, dst, dst, n_lo, n_hi):
            ch = self.carry(node, self.child(pid, row, plo, phi, how, node.hop + 1))
            if group_src:
                label = (f"group {lbl_verb} {src['name']} -> {dst['name']}: bytes {n_lo + 1}-{n_hi} land in "
                         f"{self.nm(ch)}")
                if self.layout_note(pid):
                    labels = labels + [self.layout_note(pid)] if self.layout_note(pid) not in labels else labels
            else:
                label = f"{lbl_verb} -> {self.hop_prefix(ch)}{self.nm(ch)}{subs}"
                d = self.desc(ch)
                if d:
                    label += " " + d
            out.append(_Edge(rank, node.pname, r["line"], label + self._lbls(labels), cites, r["guard"], child=ch))
        return out

    @staticmethod
    def _subs(r, src, dst) -> str:
        """` (subscripted: any of N elements)` for a subscripted operand: the
        element is not known, so the whole table is the node (guard 7)."""
        out = ""
        for sub, row in ((r["src_sub"], src), (r["dst_sub"], dst)):
            if sub and row is not None:
                out += f" (subscripted: any of {row['occurs_max'] or '?'} elements)"
        return out

    @staticmethod
    def _lbls(labels: List[str]) -> str:
        return ("; " + "; ".join(labels)) if labels else ""

    def corr_edges(self, node: _Node, r, src, dst) -> List[_Edge]:
        """MOVE CORR pairs same-named immediate children by NAME, never by
        bytes; FILLER, REDEFINES and OCCURS children are not moved (guard 8)."""
        pid = node.pid
        cites = self.cite_stmt(pid, r["line"], r["verb"] or "MOVE", r["dst_name"])
        if src is None or dst is None:
            self.unlinked += 1
            return [_Edge(9, node.pname, r["line"], f"MOVE CORR -> {r['dst_name']}", cites, r["guard"], end=END_AMBIGUOUS)]
        out = []
        up = self.o.up
        arrow = "<-" if up else "->"

        def pairs(s, d):
            # s, d: the source and destination groups; the node lies in d under --up
            s_kids = {}
            for x in self.root_items(s["root_id"]):
                if x["parent_id"] == s["id"] and x["name"].upper() != "FILLER" and not x["redefines"] and not x["occurs_max"]:
                    s_kids.setdefault(x["name"].upper(), x)
            for y in self.root_items(d["root_id"]):
                if y["parent_id"] == d["id"] and y["name"].upper() in s_kids and not y["redefines"] and not y["occurs_max"]:
                    x = s_kids[y["name"].upper()]
                    if x["is_group"] and y["is_group"]:
                        pairs(x, y)
                        continue
                    if x["is_group"] != y["is_group"]:
                        continue
                    mine = y if up else x
                    a, b = self.extent(mine)
                    if b <= node.lo or a >= node.hi:
                        continue
                    mapped = self.map_bytes(node, x, None, y, None, reverse=up)
                    if mapped is None:
                        continue
                    n_lo, n_hi, labels, end = mapped
                    head = f"MOVE CORR {s['name']} {arrow} {d['name']}: {x['name']} by name" if not up else \
                        f"MOVE CORR {d['name']} {arrow} {s['name']}: {y['name']} by name"
                    if end:
                        out.append(_Edge(9, node.pname, r["line"], head + self._lbls(labels), cites, r["guard"], end=end))
                        continue
                    for (row, plo, phi, how) in self.place_copy(x, y, x if up else y, n_lo, n_hi):
                        ch = self.carry(node, self.child(pid, row, plo, phi, how, node.hop + 1))
                        out.append(_Edge(9, node.pname, r["line"], f"{head} {arrow} {self.hop_prefix(ch)}{self.nm(ch)}"
                                         + self._lbls(labels), cites, r["guard"], child=ch))

        pairs(src, dst)
        return out

    def pointer_edges(self, node: _Node, r) -> List[_Edge]:
        """SET p TO ADDRESS OF x (row x -> p) then SET ADDRESS OF a TO p
        (row p -> a): `a` overlays `x` (rule 8)."""
        pid = node.pid
        p = self.pf(r["dst_pfield"])
        x = self.pf(r["src_pfield"])
        if p is None or x is None or (p["usage"] or "").upper() != "POINTER":
            return []
        out = []
        for r2 in self.conn.execute("SELECT * FROM data_flow WHERE program_id=? AND kind='set_address' AND src_pfield=?",
                                    (pid, p["id"])):
            a = self.pf(r2["dst_pfield"])
            if a is None:
                self.unlinked += 1
                continue
            mapped = self.overlay(node, x, a)
            if mapped is None:
                continue
            n_lo, n_hi, labels, cut = mapped
            if cut:
                continue            # the node's bytes lie past the end of the other side: not shared
            cites = self.cite_stmt(pid, r["line"], "SET", p["name"]) + "; " + self.cite_stmt(pid, r2["line"], "SET", a["name"])
            for (row, plo, phi, how) in self.place_ref(a, n_lo, n_hi):
                ch = self.child(pid, row, plo, phi, how, node.hop + 1)
                out.append(_Edge(8, node.pname, r["line"], f"pointer alias {p['name']} -> {self.nm(ch)} {self.desc(ch)}".rstrip()
                                 + self._lbls(labels), cites, None, child=ch))
        return out

    # ---- rule 1: the file -----------------------------------------------------------------
    @staticmethod
    def dd_matches(assign: Optional[str], dd_name: Optional[str]) -> bool:
        if not assign or not dd_name:
            return False
        a, d = assign.upper(), dd_name.upper()
        d_last = d.split(".")[-1]
        return a == d or a == d_last or a.endswith("-" + d_last) or d_last.endswith("-" + a)

    def steps_of(self, pname: str) -> List[sqlite3.Row]:
        return self.conn.execute("""SELECT d.id AS dd_id, d.dd_name, d.dsn_resolved, d.mode, d.mode_source, d.gdg_rel,
                                           d.is_temp, d.line AS dd_line, s.id AS step_id, s.step_name, s.effective_pgm AS pgm,
                                           s.launcher, s.job_id, s.proc_id, j.job_name, pd.proc_name, m.name AS owner_name,
                                           """ + _DD_MEMBER + """ AS member_name
                                    FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                    LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                    LEFT JOIN member m ON m.id=COALESCE(j.member_id, pd.member_id)
                                    WHERE UPPER(s.effective_pgm)=? AND d.dsn_resolved IS NOT NULL
                                    ORDER BY j.job_name, s.ordinal, d.line""", (pname.upper(),)).fetchall()

    def dds_on(self, dsn: str) -> List[sqlite3.Row]:
        return self.conn.execute("""SELECT d.id AS dd_id, d.dd_name, d.dsn_resolved, d.mode, d.mode_source, d.gdg_rel,
                                           d.is_temp, d.line AS dd_line, s.id AS step_id, s.step_name, s.effective_pgm AS pgm,
                                           s.launcher, s.job_id, s.proc_id, j.job_name, pd.proc_name, m.name AS owner_name,
                                           """ + _DD_MEMBER + """ AS member_name
                                    FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                    LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                    LEFT JOIN member m ON m.id=COALESCE(j.member_id, pd.member_id)
                                    WHERE d.dsn_resolved=? ORDER BY j.job_name, pd.proc_name, s.ordinal, d.line""",
                                 (dsn,)).fetchall()

    def file_edges(self, node: _Node, r) -> List[_Edge]:
        """WRITE of the record root -> the DD -> the DSN, one hop that then
        fans out to every step reading it (rule 1). The dataset line is a
        child; the readers hang under it."""
        pid = node.pid
        select = _note_file(r["note"])
        fd = self.conn.execute("SELECT * FROM file_decl WHERE program_id=? AND UPPER(select_name)=?",
                               (pid, (select or "").upper())).fetchone() if select else None
        if fd is None:
            fd = self.conn.execute("""SELECT f.* FROM file_decl f JOIN file_record fr ON fr.file_id=f.id
                                      WHERE fr.program_id=? AND fr.pfield=?""", (pid, node.root)).fetchone()
        verb = r["verb"] or "WRITE"
        rec = self.pf(node.root)["name"]
        stmt_cite = self.cite_stmt(pid, r["line"], verb, rec)
        if fd is None or not fd["assign_dd"]:
            return [_Edge(1, node.pname, r["line"], f"{verb} {rec} -> (no SELECT/ASSIGN for it)", stmt_cite, end=END_NO_READER)]
        steps = [s for s in self.steps_of(node.pname) if self.dd_matches(fd["assign_dd"], s["dd_name"])]
        cf = self.conn.execute("SELECT dsname FROM cics_file WHERE UPPER(name)=? AND dsname IS NOT NULL",
                               (fd["assign_dd"].upper(),)).fetchone()
        if not steps and not cf:
            return [_Edge(1, node.pname, r["line"], f"{verb} {rec} -> DD {fd['assign_dd']}: no job step runs "
                          f"{node.pname} with this DD", stmt_cite, end=END_NO_READER)]
        out = []
        for s in steps:
            where = f"{s['job_name'] or ('PROC ' + (s['proc_name'] or '?'))} {s['step_name']} DD {s['dd_name']}"
            label = f"{verb} {rec} -> {s['dsn_resolved']} ({where}, {s['mode']}/{s['mode_source']})"
            cites = stmt_cite + "; " + self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], True, s["step_name"])
            ds = _Node(pid, node.pname, node.root, node.lo, node.hi, node.pf, node.hop, kind="dataset",
                       data={"dsn": s["dsn_resolved"], "job_id": s["job_id"], "proc_id": s["proc_id"], "is_temp": s["is_temp"],
                             "dd_id": s["dd_id"], "gdg": s["gdg_rel"], "writer_pf": node.pf})
            out.append(_Edge(1, node.pname, r["line"], label, cites, child=ds))
        if cf:
            ds = _Node(pid, node.pname, node.root, node.lo, node.hi, node.pf, node.hop, kind="dataset",
                       data={"dsn": cf["dsname"], "job_id": None, "proc_id": None, "is_temp": 0, "dd_id": None, "gdg": None,
                             "writer_pf": node.pf})
            out.append(_Edge(1, node.pname, r["line"], f"{verb} {rec} -> {cf['dsname']} (CICS FILE {fd['assign_dd']})",
                             stmt_cite, child=ds))
        return out

    def interfaces_on(self, dsn: str) -> List[Tuple[str, Optional[str], Optional[int]]]:
        """(kind, member, line) per interface on the DSN: an interface_edge
        row (its JCL member and line) or a manifest row (no source line)."""
        out: List[Tuple[str, Optional[str], Optional[int]]] = []
        for r in self.conn.execute("""SELECT i.kind, i.detail, i.peer_system, i.line, m.name AS mem FROM interface_edge i
                                      LEFT JOIN member m ON m.id=i.member_id WHERE UPPER(i.detail) LIKE ?""",
                                   (f"%{dsn.upper()}%",)):
            out.append((f"{r['kind']}" + (f" to {r['peer_system']}" if r["peer_system"] else ""), r["mem"], r["line"]))
        for r in self.conn.execute("SELECT kind, peer, direction FROM external_interface WHERE UPPER(target)=?",
                                   (dsn.upper(),)):
            out.append((f"{r['kind']}" + (f" {r['direction'] or ''} {r['peer'] or ''}".rstrip()), None, None))
        return out

    def step_cards(self, step_id: int) -> Set[str]:
        return {r[0].upper() for r in self.conn.execute("SELECT DISTINCT card_kind FROM card_field_ref WHERE step_id=?",
                                                        (step_id,)) if r[0]}

    def dataset_leaves(self, ds: _Node, reader_fn: Callable, seen: Optional[Set[str]] = None,
                       path: Optional[List[Tuple[str, str]]] = None) -> List[_Leaf]:
        """Every place the bytes go from a dataset (down) or came from (up):
        through plain sort / copy steps, into COBOL readers (reader_fn gives
        the nodes), stopping at reformatting sorts, utilities, interfaces."""
        up = self.o.up
        d = ds.data
        dsn = d["dsn"]
        seen = seen if seen is not None else set()
        path = path or []
        if dsn in seen:
            return []
        seen.add(dsn)
        leaves: List[_Leaf] = []
        iface_at: Set[Tuple[str, int]] = set()
        rows = self.dds_on(dsn)
        want = ("output", "mod", "unknown") if up else ("input", "mod", "unknown")
        # the JCL parser stores an FTP / NDM step's pseudo-DD twice when the step comes from a PROC
        # expanded into a job: once re-derived as `unknown`/`undetermined`, once with the direction
        # the cards give. The determined row is the fact; the other would be a second branch (and,
        # upstream, a PUT shown as a writer)
        determined = {(r["step_id"], r["dd_name"]) for r in rows
                      if (r["dd_name"] or "").startswith("*") and r["mode_source"] != "undetermined"}
        once: Set[Tuple] = set()
        for s in rows:
            if s["dd_id"] == d.get("dd_id"):
                continue
            if ((s["dd_name"] or "").startswith("*") and s["mode_source"] == "undetermined"
                    and (s["step_id"], s["dd_name"]) in determined):
                continue
            key = (s["step_id"], s["dd_name"], s["dd_line"], s["mode"])
            if key in once:
                continue            # the same DD of the same step stored twice is one branch
            once.add(key)
            if s["mode"] not in want:
                continue
            if (d.get("is_temp") or s["is_temp"]) and not _same_run(d, s):
                continue
            pgm = s["pgm"] or ""
            job = s["job_name"] or (f"PROC {s['proc_name']}" if s["proc_name"] else "?")
            here = f"{job} {s['step_name']} DD {s['dd_name']}"
            gdg = ""
            if d.get("gdg") is not None and s["gdg_rel"] is not None and d.get("gdg") != s["gdg_rel"]:
                gdg = f"; GDG {d.get('gdg')} vs {s['gdg_rel']}: generation not modelled"
            launcher = (s["launcher"] or "").upper()
            if pgm in ("*FTP*", "*NDM*", "*USSSH*"):
                # the step that sends (or, upstream, receives) the dataset: the bytes leave the estate here
                kind = pgm.strip("*").lower()
                iface_at.add((s["member_name"], s["dd_line"]))
                iface_at.add((s["owner_name"], s["dd_line"]))      # the interface_edge row sits on the job
                leaves.append(_Leaf(list(path), f"{job} {s['step_name']} {launcher or kind.upper()}",
                                    self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"]),
                                    end=END_INTERFACE.format(x=kind)))
                continue
            if pgm.startswith("*") or launcher in ("SORT", "ICETOOL", "SYNCSORT", "IEBGENER", "ICEGENER", "IDCAMS", "DFSORT"):
                kinds = self.step_cards(s["step_id"])
                is_sort = launcher in ("SORT", "ICETOOL", "SYNCSORT", "DFSORT") or "SORT" in pgm
                if is_sort and kinds & {"INREC", "OUTREC", "OUTFIL", "JOINKEYS"}:
                    k = sorted(kinds & {"INREC", "OUTREC", "OUTFIL", "JOINKEYS"})[0]
                    leaves.append(_Leaf(list(path), f"{job} {s['step_name']} SORT has {k}", self.cite_card(s["step_id"], k),
                                        end=END_SORT))
                    continue
                if is_sort and not kinds and not self.conn.execute(
                        "SELECT 1 FROM dd WHERE step_id=? AND sysin_text IS NOT NULL", (s["step_id"],)).fetchone():
                    leaves.append(_Leaf(list(path), f"{job} {s['step_name']} SORT: control cards not indexed",
                                        self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"]), end=END_UTILITY))
                    continue
                if not (is_sort or launcher in ("IEBGENER", "ICEGENER", "IDCAMS")):
                    leaves.append(_Leaf(list(path), f"{job} {s['step_name']} {launcher or pgm}",
                                        self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"]), end=END_UTILITY))
                    continue
                if launcher in ("IEBGENER", "ICEGENER"):
                    # SYSIN DD DUMMY is the plain copy; GENERATE / RECORD FIELD= moves fields around
                    gen = next((k for k in ("RECORD", "GENERATE") if self.conn.execute(
                        "SELECT 1 FROM dd WHERE step_id=? AND sysin_text IS NOT NULL AND UPPER(sysin_text) LIKE ?",
                        (s["step_id"], f"%{k}%")).fetchone()), None)
                    if gen:
                        leaves.append(_Leaf(list(path), f"{job} {s['step_name']} {launcher} has {gen} control statements",
                                            self.cite_card(s["step_id"], gen), end=END_UTILITY))
                        continue
                    # a SYSIN that is not DUMMY holds control statements; when their text is not indexed
                    # (DSN=LIB(MEMBER) not in the estate) the copy is not known to be plain - as SORT
                    blind = self.unindexed_sysin(s["step_id"])
                    if blind is not None:
                        leaves.append(_Leaf(list(path), f"{job} {s['step_name']} {launcher}: control cards not indexed",
                                            self.cite_dd(blind[0], blind[1], "SYSIN", True), end=END_UTILITY))
                        continue
                other = "input" if up else "output"
                outs = [x for x in self.dds_on_step(s["step_id"]) if x["mode"] == other and x["dsn_resolved"] != dsn]
                if not outs:
                    leaves.append(_Leaf(list(path), f"{job} {s['step_name']} {launcher or pgm}: no {other} DD",
                                        self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"]), end=END_UTILITY))
                    continue
                what = "SORT FIELDS only" if is_sort else f"{launcher} copy"
                for o in outs:
                    arrow = "<-" if up else "->"
                    line = (f"{s['step_name']} {what}{gdg} - bytes unchanged {arrow} {o['dsn_resolved']}",
                            self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], True, s["step_name"]))
                    if o["dsn_resolved"] in seen:
                        # a second copy into (or, upstream, out of) a dataset this walk already
                        # followed: the copy is said, the branch is the one shown above
                        leaves.append(_Leaf(list(path), f"{job} {line[0]}", line[1], end=END_SEEN_DSN))
                        continue
                    nxt = _Node(ds.pid, ds.pname, ds.root, ds.lo, ds.hi, ds.pf, ds.hop, kind="dataset",
                                data={"dsn": o["dsn_resolved"], "job_id": s["job_id"], "proc_id": s["proc_id"], "is_temp": o["is_temp"],
                                      "dd_id": o["dd_id"], "gdg": o["gdg_rel"], "writer_pf": d.get("writer_pf")})
                    sub = self.dataset_leaves(nxt, reader_fn, seen, path + [line])
                    if not sub:
                        # nothing reads the copy (or writes the copy's input): the copy step is still the
                        # reader (writer) of this dataset - its line stays, with the stop on the other side
                        sub = [_Leaf(list(path), f"{job} {line[0]}: no step {'writes' if up else 'reads'} {o['dsn_resolved']}",
                                     line[1], end=END_NO_WRITER if up else END_NO_READER)]
                    leaves.extend(sub)
                continue
            progs = Q.programs_named(self.conn, pgm) if pgm else []
            if not progs:
                leaves.append(_Leaf(list(path), f"{here} runs {pgm or '?'} (not in index)",
                                    self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"]),
                                    end=END_NO_WRITER if up else END_NO_READER))
                continue
            for lf in reader_fn(progs[0], s, here + gdg, ds):
                lf.path = list(path) + lf.path
                leaves.append(lf)
        shown: Set[Tuple[str, str]] = set()
        for (kind, mem, ln) in self.interfaces_on(dsn):
            if (mem, ln) in iface_at:
                continue            # the step above already ends there
            cite = self.cite_iface(mem, ln, dsn)
            if cite and (kind, cite) in shown:
                continue            # a PROC's step and its expansion in a job: one interface, one line
            shown.add((kind, cite))
            leaves.append(_Leaf(list(path), f"{dsn} {kind}", cite, end=END_INTERFACE.format(x=kind)))
        return leaves

    def cite_iface(self, mem: Optional[str], ln: Optional[int], dsn: str) -> str:
        """The cite of an interface_edge row on the DSN: the DSN where its
        line names it (a CSD TDQUEUE DSNAME), else the EXEC line of the step
        of that member at that line that carries the DSN as its pseudo-DD
        (for a step expanded from a PROC the row sits on the job with the
        PROC's line: the PROC member is cited). A row whose line cannot be
        tied to the DSN gets no cite - never the text of an unrelated
        statement that happens to be there."""
        if not mem or not ln:
            return ""
        if dsn.upper() in self.raw(mem, ln).upper():
            return f'{mem}:{ln} "{self._tok(dsn)}"'
        st = self.conn.execute("""SELECT s.step_name, d.dd_name, """ + _DD_MEMBER + """ AS member_name
                                  FROM step s JOIN dd d ON d.step_id=s.id
                                  LEFT JOIN job j ON j.id=s.job_id LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                  JOIN member m ON m.id=COALESCE(j.member_id, pd.member_id)
                                  WHERE m.name=? AND s.line=? AND d.line=s.line AND d.dsn_resolved=? AND d.dd_name LIKE '*%'
                                  ORDER BY s.from_proc IS NOT NULL, s.id LIMIT 1""", (mem, ln, dsn)).fetchone()
        return self.cite_dd(st["member_name"], ln, st["dd_name"], False, st["step_name"]) if st else ""

    def unindexed_sysin(self, step_id: int) -> Optional[Tuple[str, int]]:
        """(member, line) of the step's SYSIN DD (or a dataset concatenated
        to it) that is not DUMMY and whose control-card text is not indexed;
        None when every SYSIN is DUMMY or indexed, or there is none. In the
        order the step lists its DDs: a PROC step's lines and a job
        override's are in two members, so line order means nothing."""
        cur = ""
        for r in self.conn.execute("""SELECT d.dd_name, d.mode, d.sysin_text, d.line, """ + _DD_MEMBER + """ AS member_name
                                      FROM dd d JOIN step s ON s.id=d.step_id LEFT JOIN job j ON j.id=s.job_id
                                      LEFT JOIN proc_def pd ON pd.id=s.proc_id
                                      LEFT JOIN member m ON m.id=COALESCE(j.member_id, pd.member_id)
                                      WHERE d.step_id=? ORDER BY d.id""", (step_id,)):
            cur = (r["dd_name"] or cur).upper().split(".")[-1]
            if cur == "SYSIN" and r["mode"] != "dummy" and r["sysin_text"] is None:
                return r["member_name"], r["line"]
        return None

    def dds_on_step(self, step_id: int) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT id AS dd_id, dd_name, dsn_resolved, mode, gdg_rel, is_temp FROM dd "
                                 "WHERE step_id=? AND dsn_resolved IS NOT NULL ORDER BY line", (step_id,)).fetchall()

    def cobol_reader(self, prog, s, where: str, ds: _Node) -> List[_Leaf]:
        """The COBOL program on the other side of the DSN: its file on that DD,
        the 01s it READs (or WRITEs, upstream), the items at the same bytes."""
        up = self.o.up
        rpid = prog["id"]
        self.touch(rpid)
        fds = [f for f in self.conn.execute("SELECT * FROM file_decl WHERE program_id=?", (rpid,))
               if self.dd_matches(f["assign_dd"], s["dd_name"])]
        dd_cite = self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"])
        if not fds:
            return [_Leaf([], f"{prog['program_id']} runs in {where} but declares no file on that DD",
                          dd_cite, end=END_NO_FIELD.format(lo=ds.lo + 1, hi=ds.hi))]
        leaves = []
        for fd in fds:
            kind = "io_out" if up else "io_in"
            roots = [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT {'src_pfield' if up else 'dst_pfield'} FROM data_flow WHERE program_id=? AND kind=? "
                f"AND (note=? OR note LIKE ?) AND {'src_pfield' if up else 'dst_pfield'} IS NOT NULL",
                (rpid, kind, f"file {fd['select_name']}", f"file {fd['select_name']} %"))]
            if not roots:
                roots = [r[0] for r in self.conn.execute(
                    "SELECT pfield FROM file_record WHERE file_id=? AND pfield IS NOT NULL ORDER BY ordinal", (fd["id"],))]
            found = False
            for root in roots:
                for r in self.root_items(root):
                    if r["is_group"] or self.redefines_chain(r):
                        continue
                    a, b = self.extent(r)
                    if b <= ds.lo or a >= ds.hi:
                        continue
                    xlo, xhi = max(ds.lo, a), min(ds.hi, b)
                    how = "exact" if (xlo, xhi) == (a, b) else "partial"
                    ch = self.child(rpid, r, xlo, xhi, how, ds.hop + 1)
                    wpf = ds.data.get("writer_pf")
                    my_layout = self.member_tag(r["src_member"]) or prog["program_id"]
                    their_layout = self.member_tag(wpf["src_member"]) if wpf is not None and wpf["src_member"] is not None else None
                    if their_layout and their_layout != my_layout:
                        lay = f"layout {my_layout}, not {their_layout} - {END_COPYBOOK}"
                    else:
                        lay = f"layout {my_layout}" + (" (same as the other side)" if their_layout else "")
                    verb = "written by" if up else "read by"
                    lw = self.layout_note(rpid)
                    if lw:
                        lay += f"; {lw}"
                    text = (f"{verb} {prog['program_id']}.{self.base_name(rpid, r)} bytes {xlo + 1}-{xhi}"
                            + (f" (bytes {xlo - a + 1}-{xhi - a} of {b - a})" if how == "partial" else "")
                            + f" ({where}; {lay})")
                    leaves.append(_Leaf([], text, self.cite_def(r, rpid) + "; " + dd_cite, node=ch))
                    found = True
            if not found:
                leaves.append(_Leaf([], f"{prog['program_id']} {fd['select_name']} ({where}): no item at those bytes",
                                    dd_cite, end=END_NO_FIELD.format(lo=ds.lo + 1, hi=ds.hi)))
        return leaves

    # ---- rule 2: CALL out ----------------------------------------------------------------------
    def entry_for(self, callee, target: str) -> Optional[str]:
        t = target.upper()
        if t == (callee["program_id"] or "").upper() or t == (callee["member_name"] or "").upper():
            return None
        return t if t in self.aliases(callee["id"]) else None

    def call_edges(self, node: _Node, ids: Set[int]) -> List[_Edge]:
        pid = node.pid
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"""SELECT a.*, c.kind AS ckind, c.target, c.via_var, c.resolved, c.resolution,
                                            c.line AS cline, c.returning_item, c.id AS cid
                                     FROM call_arg a JOIN call_edge c ON c.id=a.call_id
                                     WHERE a.program_id=? AND a.pfield IN ({q}) ORDER BY c.line, a.pos""", (pid, *ids)).fetchall()
        out: List[_Edge] = []
        for a in rows:
            apf = self.pf(a["pfield"])
            a0, a1 = self.extent(apf)
            x0, x1 = max(node.lo, a0), min(node.hi, a1)
            if x0 >= x1:
                continue
            word = _CALL_WORD.get(a["ckind"], "CALL")
            cites = self.cite_stmt(pid, a["cline"], word, a["name"])
            if a["ckind"] in ("cics_start", "cics_return"):
                out.extend(self.tran_edges(node, a, apf, x0, x1, cites))
                continue
            targets = [(a["target"], None)] if a["target"] else _candidates(a)
            if not targets:
                out.append(_Edge(2, "?", a["cline"], f"{word} {a['via_var']} arg {a['pos']}", cites,
                                 end=END_DYNAMIC.format(x=a["via_var"])))
                continue
            for (t, cand) in targets:
                if t.startswith("(+"):
                    out.append(_Edge(2, "?", a["cline"], f"{word} {a['via_var']} arg {a['pos']}: {t} candidate(s) not listed",
                                     cites, end=END_DYNAMIC.format(x=a["via_var"])))
                    continue
                callname = f"{word} {t}" if a["target"] else f"{word} {a['via_var']} = {t} ({cand})"
                if a["how"] == "length_of":
                    out.append(_Edge(2, t.upper(), a["cline"], f"{callname} arg {a['pos']} LENGTH OF {a['name']}", cites,
                                     end=END_LENGTH_OF))
                    continue
                progs = Q.programs_named(self.conn, t)
                if not progs:
                    out.append(_Edge(2, t.upper(), a["cline"], f"{callname} arg {a['pos']}", cites, end=END_NO_CALLEE))
                    continue
                callee = progs[0]
                cname = callee["program_id"]
                extra: List[str] = []
                if a["returning_item"] and a["pos"] == min(x["pos"] for x in rows if x["cid"] == a["cid"]):
                    ret = self.conn.execute("SELECT name FROM param WHERE program_id=? AND pos=0", (callee["id"],)).fetchone()
                    extra.append(f"RETURNING -> {a['returning_item']}" + (f" (from {cname}.{ret['name']})" if ret else ""))
                if a["how"] in ("commarea", "start_from"):
                    prm = self.conn.execute("SELECT * FROM param WHERE program_id=? AND entry='DFHCOMMAREA' AND pos=1",
                                            (callee["id"],)).fetchone()
                    if prm is None:
                        out.append(_Edge(2, cname, a["cline"], f"{callname} COMMAREA arg {a['pos']}", cites, end=END_NO_COMMAREA,
                                         extra=extra))
                        continue
                    note = "by CICS convention"
                else:
                    entry = self.entry_for(callee, t)
                    prm = self.conn.execute("SELECT * FROM param WHERE program_id=? AND entry IS ? AND pos=?",
                                            (callee["id"], entry, a["pos"])).fetchone()
                    if prm is None:
                        n = self.conn.execute("SELECT COUNT(*) FROM param WHERE program_id=? AND entry IS ? AND pos>0",
                                              (callee["id"], entry)).fetchone()[0]
                        out.append(_Edge(2, cname, a["cline"], f"{callname} arg {a['pos']} (callee declares {n} parameter(s))",
                                         cites, end=END_POS, extra=extra))
                        continue
                    note = ""
                if prm["pfield"] is None:
                    out.append(_Edge(2, cname, a["cline"], f"{callname} arg {a['pos']} -> {cname}.{prm['name']}", cites,
                                     end=END_MISSING.format(x=self.missing_copy(callee)), extra=extra))
                    continue
                ppf = self.pf(prm["pfield"])
                if a["how"] == "address_of":
                    out.extend(self.address_of_edges(node, a, callee, ppf, cites, callname))
                    continue
                # the parameter IS the argument's storage (or, BY CONTENT, a copy of its bytes): no MOVE rules
                mapped = self.overlay(node, apf, ppf)
                if mapped is None:
                    continue
                n_lo, n_hi, labels, end = mapped
                if a["how"] in ("content", "value"):
                    labels.append(END_CONTENT)
                if note:
                    labels.append(note)
                if end:
                    out.append(_Edge(2, cname, a["cline"], f"{callname} arg {a['pos']} -> {cname}.{ppf['name']}"
                                     + self._lbls(labels), cites, end=end, extra=extra))
                    continue
                for (row, plo, phi, how) in self.place_ref(ppf, n_lo, n_hi):
                    ch = self.child(callee["id"], row, plo, phi, how, node.hop + 1)
                    ch.via_call, ch.via_root = a["cid"], ppf["root_id"]
                    self.touch(callee["id"])
                    label = f"{callname} arg {a['pos']} -> {cname}.{self.nm(ch)} {self.desc(ch)}".rstrip() + self._lbls(labels)
                    out.append(_Edge(2, cname, a["cline"], label, cites, child=ch, extra=extra))
                    extra = []
        return out

    def missing_copy(self, callee) -> str:
        """The COPY a callee's parameter would have come from (END_MISSING)."""
        miss = self.conn.execute("""SELECT detail FROM unresolved WHERE member_id=? AND kind IN ('expand','missing_copybook')
                                    LIMIT 1""", (callee["member_id"],)).fetchone()
        m = re.search(r"COPY\s+(\S+)", miss["detail"]) if miss else None
        return m.group(1) if m else "?"

    def address_of_edges(self, node: _Node, a, callee, ppf, cites: str, callname: str) -> List[_Edge]:
        """ADDRESS OF x passed: the callee's SET ADDRESS OF a TO param makes
        `a` overlay x."""
        out = []
        apf = self.pf(a["pfield"])
        for r2 in self.conn.execute("SELECT * FROM data_flow WHERE program_id=? AND kind='set_address' AND src_pfield=?",
                                    (callee["id"], ppf["id"])):
            tgt = self.pf(r2["dst_pfield"])
            if tgt is None:
                self.unlinked += 1
                continue
            mapped = self.overlay(node, apf, tgt)
            if mapped is None:
                continue
            n_lo, n_hi, labels, cut = mapped
            if cut:
                continue            # the node's bytes lie past the end of the other side: not shared
            for (row, plo, phi, how) in self.place_ref(tgt, n_lo, n_hi):
                ch = self.child(callee["id"], row, plo, phi, how, node.hop + 1)
                self.touch(callee["id"])
                out.append(_Edge(2, callee["program_id"], a["cline"],
                                 f"{callname} arg {a['pos']} ADDRESS OF -> {callee['program_id']}.{self.nm(ch)} "
                                 f"{self.desc(ch)}; pointer alias".rstrip() + self._lbls(labels),
                                 cites + "; " + self.cite_stmt(callee["id"], r2["line"], "SET", tgt["name"]), child=ch))
        if not out:
            out.append(_Edge(2, callee["program_id"], a["cline"], f"{callname} arg {a['pos']} ADDRESS OF {a['name']}: "
                             f"{callee['program_id']} never SETs an item to it", cites, end=END_NO_USE.format(p=callee["program_id"])))
        return out

    def tran_edges(self, node: _Node, a, apf, x0: int, x1: int, cites: str) -> List[_Edge]:
        """START TRANSID / RETURN TRANSID with data: the transaction's program
        RETRIEVEs it (START) or gets it as DFHCOMMAREA (RETURN)."""
        out = []
        t = a["target"] or ""
        progs = [r["program"] for r in self.conn.execute(
            "SELECT DISTINCT program FROM transaction_def WHERE UPPER(tran_code)=? AND program IS NOT NULL", (t.upper(),))]
        if not progs:
            return [_Edge(2, t.upper(), a["cline"], f"{_CALL_WORD.get(a['ckind'], 'START')} {t} arg {a['pos']}", cites,
                          end=END_NO_CALLEE)]
        for pg in progs:
            rows = Q.programs_named(self.conn, pg)
            if not rows:
                out.append(_Edge(2, pg.upper(), a["cline"], f"START {t} -> {pg}", cites, end=END_NO_CALLEE))
                continue
            callee = rows[0]
            targets = []
            if a["ckind"] == "cics_start":
                for r in self.conn.execute("SELECT * FROM data_flow WHERE program_id=? AND kind='cics_in' AND note LIKE 'RETRIEVE%'",
                                           (callee["id"],)):
                    if r["dst_pfield"] is not None:
                        targets.append((self.pf(r["dst_pfield"]), f"RETRIEVE in {callee['program_id']}"))
            else:
                prm = self.conn.execute("SELECT * FROM param WHERE program_id=? AND entry='DFHCOMMAREA'", (callee["id"],)).fetchone()
                if prm is not None and prm["pfield"] is not None:
                    targets.append((self.pf(prm["pfield"]), "by CICS convention"))
            if not targets:
                out.append(_Edge(2, callee["program_id"], a["cline"], f"START/RETURN {t} -> {callee['program_id']}", cites,
                                 end=END_NO_COMMAREA))
                continue
            for (tpf, note) in targets:
                mapped = self.overlay(node, apf, tpf)
                if mapped is None:
                    continue
                n_lo, n_hi, labels, cut = mapped
                if cut:
                    continue            # the node's bytes lie past the end of the other side: not shared
                for (row, plo, phi, how) in self.place_ref(tpf, n_lo, n_hi):
                    ch = self.child(callee["id"], row, plo, phi, how, node.hop + 1)
                    self.touch(callee["id"])
                    out.append(_Edge(2, callee["program_id"], a["cline"],
                                     f"{_CALL_WORD.get(a['ckind'], 'START')} {t} -> {callee['program_id']}.{self.nm(ch)} "
                                     f"{self.desc(ch)}; {note}".rstrip() + self._lbls(labels), cites, child=ch))
        return out

    # ---- rule 3: LINKAGE back ----------------------------------------------------------------------
    def params_of_root(self, node: _Node, returning: bool = False) -> List[sqlite3.Row]:
        # pos 0 is the RETURNING item: no caller passes it, the value goes back at GOBACK
        return self.conn.execute(f"""SELECT pr.* FROM param pr JOIN pfield f ON f.id=pr.pfield
                                     WHERE pr.program_id=? AND f.root_id=? AND pr.pos{'>=' if returning else '>'}0
                                     ORDER BY pr.pos, pr.id""", (node.pid, node.root)).fetchall()

    def written_here(self, node: _Node, ids: Set[int]) -> bool:
        q = ",".join("?" * len(ids))
        if self.conn.execute(f"SELECT 1 FROM data_flow WHERE program_id=? AND dst_pfield IN ({q}) LIMIT 1",
                             (node.pid, *ids)).fetchone():
            return True
        return bool(self.conn.execute(f"""SELECT 1 FROM field_ref WHERE program_id=? AND pfield_id IN ({q})
                                          AND {_SETS_HERE} LIMIT 1""", (node.pid, *ids)).fetchone())

    def callers_of(self, pname: str, entry: Optional[str], via_call: Optional[int] = None,
                   transid: bool = False) -> List[sqlite3.Row]:
        """The CALL / LINK / XCTL edges that reach this program (or ENTRY);
        `via_call`: that one edge only (the invocation the value is in);
        `transid`: also RETURN TRANSID edges to a transaction that runs it
        (its DFHCOMMAREA is that COMMAREA - --up only: the returning task is
        gone, nothing comes back to it)."""
        # DFHCOMMAREA is the program's own LINK / XCTL target, not an entry name
        name = (pname if entry in (None, "DFHCOMMAREA") else entry).upper()
        tran = ("""OR (c.kind='cics_return' AND UPPER(c.target) IN
                       (SELECT UPPER(tran_code) FROM transaction_def WHERE UPPER(program)=?))""" if transid else "")
        args: List = [name, name, f'%"{name}"%'] + ([name] if transid else [])
        rows = self.conn.execute(f"""SELECT c.*, q.program_id AS caller, q.id AS cpid, ? AS callee_name
                                     FROM call_edge c JOIN program q ON q.id=c.program_id
                                     WHERE (UPPER(c.target)=? OR c.resolved LIKE ? {tran}) ORDER BY q.program_id, c.line""",
                                 args).fetchall()
        return [c for c in rows if via_call is None or c["id"] == via_call]

    @staticmethod
    def pos_tag(prm, c) -> str:
        """`pos 2`, `pos 1 of ENTRY FLOWENT`, `COMMAREA` - and `(candidate)` when
        the caller's CALL is dynamic and this program is one of its targets."""
        if prm["entry"] == "DFHCOMMAREA":
            tag = "COMMAREA"
        else:
            tag = f"pos {prm['pos']}" + (f" of ENTRY {prm['entry']}" if prm["entry"] else "")
        return tag + ("" if c["target"] else f" (CALL {c['via_var']}: {_how_resolved(c)} candidate)")

    @staticmethod
    def call_tag(prm, c) -> str:
        """The caller's side of the same position: `CALL FLOWENT arg 1`."""
        if prm["entry"] == "DFHCOMMAREA":
            return f"{c['target'] or c['via_var']} COMMAREA"
        name = c["target"] or f"{c['via_var']} = {prm['entry'] or c['callee_name']} (candidate: resolved via {_how_resolved(c)})"
        return f"{name} arg {prm['pos']}"

    def linkage_back(self, node: _Node, ids: Set[int]) -> List[_Edge]:
        out: List[_Edge] = []
        # reached through one CALL: the parameter is that caller's storage, so the value cannot
        # reach another CALL's argument, and back at the same position is where it came from;
        # copied from there into another parameter: back through that CALL only (_Node);
        # every caller only when it got here by a copy of other storage (or is the start)
        if node.via_call is not None and node.root == node.via_root:
            return out
        params = self.params_of_root(node, returning=True)
        if not params:
            return out
        written = self.written_here(node, ids)
        for prm in params:
            ppf = self.pf(prm["pfield"])
            if prm["pos"] == 0:
                # RETURNING: a copy back to each caller's RETURNING item at GOBACK (rule 9 `returning`),
                # whoever wrote it - not shared storage, so no write is needed here
                out.extend(self.returning_back(node, prm, ppf))
                continue
            if not written:
                continue
            for c in self.callers_of(node.pname, prm["entry"], node.via_call):
                if prm["entry"] == "DFHCOMMAREA":
                    args = self.conn.execute("SELECT * FROM call_arg WHERE call_id=? AND how IN ('commarea')", (c["id"],)).fetchall()
                else:
                    args = self.conn.execute("SELECT * FROM call_arg WHERE call_id=? AND pos=?", (c["id"], prm["pos"])).fetchall()
                for a in args:
                    word = _CALL_WORD.get(c["kind"], "CALL")
                    cites = self.cite_stmt(c["cpid"], c["line"], word, a["name"])
                    tag = self.pos_tag(prm, c)
                    if a["how"] in ("content", "value"):
                        out.append(_Edge(3, c["caller"], c["line"], f"LINKAGE {tag} -> back to {c['caller']}.{a['name']}: "
                                         f"not returned", cites, end=END_CONTENT))
                        continue
                    if a["how"] in ("length_of", "address_of"):
                        continue
                    apf = self.pf(a["pfield"])
                    if apf is None:
                        self.unlinked += 1
                        continue
                    mapped = self.overlay(node, ppf, apf)
                    if mapped is None:
                        continue
                    n_lo, n_hi, labels, end = mapped
                    if c["kind"] == "cics_xctl":
                        # XCTL hands control over for good: the caller never sees what is written here
                        out.append(_Edge(3, c["caller"], c["line"], f"LINKAGE {tag}: {c['caller']}.{a['name']} was passed by XCTL",
                                         cites, end=END_XCTL))
                        continue
                    if end:
                        out.append(_Edge(3, c["caller"], c["line"], f"LINKAGE {tag} -> back to {c['caller']}.{a['name']}"
                                         + self._lbls(labels), cites, end=end))
                        continue
                    for (row, plo, phi, how) in self.place_ref(apf, n_lo, n_hi):
                        ch = self.child(c["cpid"], row, plo, phi, how, node.hop + 1)
                        self.touch(c["cpid"])
                        out.append(_Edge(3, c["caller"], c["line"], f"LINKAGE {tag} -> back to {c['caller']}.{self.nm(ch)} "
                                         f"{self.desc(ch)} (BY REFERENCE)".replace("  ", " ") + self._lbls(labels), cites, child=ch))
        return out

    def returning_back(self, node: _Node, prm, ppf) -> List[_Edge]:
        """The callee's RETURNING item -> every caller's `CALL .. RETURNING x`
        (the caller's `returning` data_flow row names x's pfield) - or, when
        the value came in through one CALL (via_call), that caller's only;
        the --up mirror is returning_up."""
        out: List[_Edge] = []
        for c in self.callers_of(node.pname, prm["entry"], node.via_call):
            if not c["returning_item"]:
                continue
            word = _CALL_WORD.get(c["kind"], "CALL")
            cites = self.cite_stmt(c["cpid"], c["line"], word, c["returning_item"])
            tag = "" if c["target"] else f" (CALL {c['via_var']}: {_how_resolved(c)} candidate)"
            r = self.conn.execute("SELECT dst_pfield FROM data_flow WHERE program_id=? AND line=? AND kind='returning'",
                                  (c["cpid"], c["line"])).fetchone()
            apf = self.pf(r["dst_pfield"]) if r else None
            if apf is None:
                self.unlinked += 1
                out.append(_Edge(3, c["caller"], c["line"], f"RETURNING{tag} -> back to {c['caller']}.{c['returning_item']}",
                                 cites, end=END_AMBIGUOUS))
                continue
            mapped = self.map_bytes(node, ppf, None, apf, None)
            if mapped is None:
                continue
            n_lo, n_hi, labels, end = mapped
            if end:
                out.append(_Edge(3, c["caller"], c["line"], f"RETURNING{tag} -> back to {c['caller']}.{c['returning_item']}"
                                 + self._lbls(labels), cites, end=end))
                continue
            for (row, plo, phi, how) in self.place_copy(ppf, apf, apf, n_lo, n_hi):
                ch = self.child(c["cpid"], row, plo, phi, how, node.hop + 1)
                self.touch(c["cpid"])
                out.append(_Edge(3, c["caller"], c["line"], f"RETURNING{tag} -> back to {c['caller']}.{self.nm(ch)} "
                                 f"{self.desc(ch)} (pos 0)".replace("  ", " ") + self._lbls(labels), cites, child=ch))
        return out

    # ---- rule 4: DB2 -----------------------------------------------------------------------------
    def sql_edges(self, node: _Node, ids: Set[int], mode: str) -> List[_Edge]:
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"""SELECT * FROM sql_col_ref WHERE program_id=? AND mode=? AND pfield_id IN ({q})
                                     ORDER BY line""", (node.pid, mode, *ids)).fetchall()
        out: List[_Edge] = []
        done = set()
        for r in rows:
            base = (r["tbl"] or "?").rpartition(".")[2].upper()
            key = (base, r["col"].upper())
            if key in done:
                continue
            done.add(key)
            cites = self.cite_sql(node.pid, r["line"], r["col"], r["host_var"])
            sep = " SET" if (r["stmt"] or "").upper() == "UPDATE" else ""
            label = f"EXEC SQL {r['stmt'] or ''} {r['tbl'] or '?'}{sep} {r['col']}".replace("  ", " ")
            if base == "?":
                out.append(_Edge(4, "DB2 ?", r["line"], label + " (table unresolved)", cites,
                                 end=END_NO_DB2_READER if mode == "write" else END_NO_DB2_WRITER))
                continue
            other = "read" if mode == "write" else "write"
            peers = self.conn.execute("""SELECT c.*, p.program_id AS pname FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                                         WHERE c.mode=? AND UPPER(c.col)=? AND (UPPER(c.tbl)=? OR UPPER(c.tbl) LIKE ?)
                                         ORDER BY p.program_id, c.line""", (other, key[1], base, f"%.{base}")).fetchall()
            if not peers:
                out.append(_Edge(4, f"DB2 {base}", r["line"], label, cites,
                                 end=END_NO_DB2_READER if mode == "write" else END_NO_DB2_WRITER))
                continue
            trows = []
            for p in peers:
                self.touch(p["program_id"])
                trows.append((p["pname"], p["host_var"] or "", self.cite_sql(p["program_id"], p["line"], p["col"], p["host_var"])))
            word = "readers" if mode == "write" else "writers"
            tbl = (f"via table {base}.{r['col']}: {len(peers)} static {word}, no ordering\n"
                   + Q.table(["program", "host variable", "cite"], trows).rstrip("\n"))
            out.append(_Edge(4, f"DB2 {base}", r["line"], label, cites, table=tbl))
        return out

    # ---- rules 5-7: IMS, CICS carriers, MQ --------------------------------------------------------------
    def dli_edges(self, node: _Node, r) -> List[_Edge]:
        up = self.o.up
        pid = node.pid
        call = self.conn.execute("SELECT * FROM dli_call WHERE program_id=? AND line=?", (pid, r["line"])).fetchone()
        func = (r["note"] or (call["func"] if call else "") or "DL/I").split(" ")[0]
        area = r["dst_name"] if up else r["src_name"]
        cites = self.cite_stmt(pid, r["line"], "CALL", area)
        if call is None or not call["dbd_name"]:
            return [_Edge(5, "IMS", r["line"], f"{func} via {call['pcb_arg'] if call else '?'}", cites, end=END_PCB)]
        dbd = call["dbd_name"]
        if dbd == "*IO-PCB*":
            # the build's marker for the I/O PCB: a reply to (or input from) the terminal, never
            # another program's GU / ISRT on its own I/O PCB
            return [_Edge(5, "IMS", r["line"], f"{func} via {call['pcb_arg'] or 'I/O PCB'} (I/O PCB)", cites,
                          end=END_IO_PCB_IN if up else END_IO_PCB_OUT)]
        other = "dli_out" if up else "dli_in"
        peers = self.conn.execute(f"""SELECT d.*, p.program_id AS pname, c.func FROM data_flow d JOIN program p ON p.id=d.program_id
                                      JOIN dli_call c ON c.program_id=d.program_id AND c.line=d.line
                                      WHERE d.kind=? AND UPPER(c.dbd_name)=? ORDER BY p.program_id, d.line""",
                                  (other, dbd.upper())).fetchall()
        mine = self.pf(r["dst_pfield"] if up else r["src_pfield"])
        out = []
        for p in peers:
            tpf = self.pf(p["src_pfield"] if up else p["dst_pfield"])
            if tpf is None or mine is None:
                self.unlinked += 1
                continue
            mapped = self.overlay(node, mine, tpf)
            if mapped is None:
                continue
            n_lo, n_hi, labels, cut = mapped
            if cut:
                continue            # the node's bytes lie past the end of the other side: not shared
            for (row, plo, phi, how) in self.place_ref(tpf, n_lo, n_hi):
                ch = self.child(p["program_id"], row, plo, phi, how, node.hop + 1)
                self.touch(p["program_id"])
                arrow = "<-" if up else "->"
                out.append(_Edge(5, p["pname"], r["line"], f"{func} {dbd} {arrow} {p['func']} {p['pname']}.{self.nm(ch)} "
                                 f"{self.desc(ch)} ({END_SEGMENT} - matched by DBD and offset)".replace("  ", " ")
                                 + self._lbls(labels), cites + "; " + self.cite_stmt(p["program_id"], p["line"], "CALL", tpf["name"]),
                                 child=ch))
        if not out:
            out.append(_Edge(5, "IMS", r["line"], f"{func} {dbd}", cites, end=END_NO_WRITER if up else END_NO_READER))
        return out

    def data_names(self, pid: int) -> Set[str]:
        if pid not in self._names:
            self._names[pid] = {x[0].upper() for x in self.conn.execute(
                "SELECT name FROM pfield WHERE program_id=? UNION SELECT name FROM field_ref WHERE program_id=?", (pid, pid))}
        return self._names[pid]

    def res_var(self, pid: int, kind: str, res: str) -> Optional[str]:
        """The data item a queue / file / container name really is: the
        parser keeps QUEUE(x)'s variable name when no literal reaches x, and
        two programs' WS-QNAME are two different queues (guard 1)."""
        if kind not in _CICS_CARRIERS:
            return None
        names = self.data_names(pid)
        return next((x for x in res.upper().split("/") if x in names), None)

    def cics_peers(self, pkind: str, kind: str, res: str) -> List[sqlite3.Row]:
        """The other side of a CICS carrier: the same resource kind and the
        whole resource name (never LIKE: `_` and a longer name are not it),
        and a resource that names a literal in the peer too."""
        rows = self.conn.execute("""SELECT d.*, p.program_id AS pname FROM data_flow d JOIN program p ON p.id=d.program_id
                                    WHERE d.kind=? AND UPPER(d.note) LIKE ? ORDER BY p.program_id, d.line""",
                                 (pkind, f"% {kind.upper()} %")).fetchall()
        out = []
        for p in rows:
            parts = (p["note"] or "").split(" ", 2)
            if len(parts) < 3 or parts[1].lower() != kind.lower() or parts[2].upper() != res.upper():
                continue
            if self.res_var(p["program_id"], kind, parts[2]):
                continue
            out.append(p)
        return out

    def cics_out_edges(self, node: _Node, r) -> List[_Edge]:
        pid = node.pid
        parts = (r["note"] or "").split(" ", 2)
        verb = parts[0] if parts else "SEND"
        kind = parts[1] if len(parts) > 1 else ""
        res = parts[2] if len(parts) > 2 else ""
        cites = self.cite_stmt(pid, r["line"], verb, r["src_name"])
        src = self.pf(r["src_pfield"])
        carrier = self.base_name(pid, node.pf) if node.pf is not None else r["src_name"]
        if kind in ("map", "terminal"):
            return [_Edge(6, "screen", r["line"], f"{verb} {kind.upper()} {res} FROM {r['src_name']} ({carrier} bytes "
                          f"{node.lo + 1}-{node.hi})", cites, end=END_SCREEN)]
        out: List[_Edge] = []
        var = self.res_var(pid, kind, res)
        if var:
            return [_Edge(6, "CICS", r["line"], f"{verb} {kind.upper()} {res} FROM {r['src_name']}", cites,
                          end=END_RES_VAR.format(what=_CICS_CARRIERS[kind], v=var))]
        if kind in ("tsq", "tdq", "container", "file", "channel"):
            peers = self.cics_peers("cics_in", kind, res)
            for p in peers:
                tpf = self.pf(p["dst_pfield"])
                if tpf is None or src is None:
                    self.unlinked += 1
                    continue
                mapped = self.overlay(node, src, tpf)
                if mapped is None:
                    continue
                n_lo, n_hi, labels, cut = mapped
                if cut:
                    continue            # the node's bytes lie past the end of the other side: not shared
                pverb = (p["note"] or "").split(" ")[0]
                for (row, plo, phi, how) in self.place_ref(tpf, n_lo, n_hi):
                    ch = self.child(p["program_id"], row, plo, phi, how, node.hop + 1)
                    self.touch(p["program_id"])
                    out.append(_Edge(6, p["pname"], r["line"], f"{verb} {kind.upper()} {res} -> {pverb} into {p['pname']}."
                                     f"{self.nm(ch)} {self.desc(ch)}".rstrip() + self._lbls(labels),
                                     cites + "; " + self.cite_stmt(p["program_id"], p["line"], pverb, tpf["name"]), child=ch))
            dsn = None
            if kind == "tdq":
                cr = self.conn.execute("SELECT attrs FROM cics_resource WHERE type LIKE 'TDQ%' AND UPPER(name)=?", (res.upper(),)).fetchone()
                attrs = Q._jl(cr["attrs"]) if cr and cr["attrs"] else {}
                dsn = (attrs.get("DSNAME") or attrs.get("DSN")) if isinstance(attrs, dict) else None
            if kind == "file":
                cf = self.conn.execute("SELECT dsname FROM cics_file WHERE UPPER(name)=? AND dsname IS NOT NULL", (res.upper(),)).fetchone()
                dsn = cf["dsname"] if cf else None
            if dsn:
                ds = _Node(pid, node.pname, node.root, node.lo, node.hi, node.pf, node.hop, kind="dataset",
                           data={"dsn": dsn, "job_id": None, "proc_id": None, "is_temp": 0, "dd_id": None, "gdg": None, "writer_pf": node.pf})
                out.append(_Edge(6, dsn, r["line"], f"{verb} {kind.upper()} {res} -> {dsn} (CICS {kind.upper()})", cites, child=ds))
        if not out:
            out.append(_Edge(6, "CICS", r["line"], f"{verb} {kind.upper()} {res} FROM {r['src_name']}", cites, end=END_NO_READER))
        return out

    def cics_in_edges(self, node: _Node, r) -> List[_Edge]:
        """--up: what fills a READQ / GET / RECEIVE target."""
        pid = node.pid
        parts = (r["note"] or "").split(" ", 2)
        verb = parts[0] if parts else "RECEIVE"
        kind = parts[1] if len(parts) > 1 else ""
        res = parts[2] if len(parts) > 2 else ""
        cites = self.cite_stmt(pid, r["line"], verb, r["dst_name"])
        dst = self.pf(r["dst_pfield"])
        if kind in ("map", "terminal") or verb in ("RECEIVE",):
            return [_Edge(6, "screen", r["line"], f"{verb} {kind.upper()} {res} INTO {r['dst_name']}", cites, end=END_SCREEN)]
        if verb in ("GETMAIN",):
            return []
        if verb == "RETRIEVE":
            out = []
            for c in self.conn.execute("""SELECT a.*, c.line AS cline, c.target, q.program_id AS caller, q.id AS cpid
                                          FROM call_arg a JOIN call_edge c ON c.id=a.call_id JOIN program q ON q.id=c.program_id
                                          JOIN transaction_def t ON UPPER(t.tran_code)=UPPER(c.target)
                                          WHERE a.how='start_from' AND UPPER(t.program)=?""", (node.pname.upper(),)):
                apf = self.pf(c["pfield"])
                if apf is None or dst is None:
                    continue
                mapped = self.overlay(node, dst, apf)
                if mapped is None:
                    continue
                n_lo, n_hi, labels, cut = mapped
                if cut:
                    continue            # the node's bytes lie past the end of the other side: not shared
                for (row, plo, phi, how) in self.place_ref(apf, n_lo, n_hi):
                    ch = self.child(c["cpid"], row, plo, phi, how, node.hop + 1)
                    self.touch(c["cpid"])
                    out.append(_Edge(2, c["caller"], r["line"], f"RETRIEVE <- START {c['target']} FROM {c['caller']}.{self.nm(ch)}"
                                     + self._lbls(labels), cites + "; " + self.cite_stmt(c["cpid"], c["cline"], "START", c["name"]),
                                     child=ch))
            return out
        out: List[_Edge] = []
        var = self.res_var(pid, kind, res)
        if var:
            return [_Edge(6, "CICS", r["line"], f"{verb} {kind.upper()} {res} INTO {r['dst_name']}", cites,
                          end=END_RES_VAR.format(what=_CICS_CARRIERS[kind], v=var))]
        peers = self.cics_peers("cics_out", kind, res) if kind else []
        for p in peers:
            spf = self.pf(p["src_pfield"])
            if spf is None or dst is None:
                self.unlinked += 1
                continue
            mapped = self.overlay(node, dst, spf)
            if mapped is None:
                continue
            n_lo, n_hi, labels, cut = mapped
            if cut:
                continue            # the node's bytes lie past the end of the other side: not shared
            pverb = (p["note"] or "").split(" ")[0]
            for (row, plo, phi, how) in self.place_ref(spf, n_lo, n_hi):
                ch = self.child(p["program_id"], row, plo, phi, how, node.hop + 1)
                self.touch(p["program_id"])
                out.append(_Edge(6, p["pname"], r["line"], f"{verb} {kind.upper()} {res} <- {pverb} from {p['pname']}."
                                 f"{self.nm(ch)} {self.desc(ch)}".rstrip() + self._lbls(labels),
                                 cites + "; " + self.cite_stmt(p["program_id"], p["line"], pverb, spf["name"]), child=ch))
        if kind == "file":
            cf = self.conn.execute("SELECT dsname FROM cics_file WHERE UPPER(name)=? AND dsname IS NOT NULL", (res.upper(),)).fetchone()
            if cf:
                ds = _Node(pid, node.pname, node.root, node.lo, node.hi, node.pf, node.hop, kind="dataset",
                           data={"dsn": cf["dsname"], "job_id": None, "proc_id": None, "is_temp": 0, "dd_id": None, "gdg": None, "writer_pf": node.pf})
                out.append(_Edge(6, cf["dsname"], r["line"], f"{verb} FILE {res} <- {cf['dsname']} (CICS FILE)", cites, child=ds))
        if not out:
            out.append(_Edge(6, "CICS", r["line"], f"{verb} {kind.upper()} {res} INTO {r['dst_name']}", cites, end=END_NO_WRITER))
        return out

    def mq_edges(self, node: _Node, r) -> List[_Edge]:
        up = self.o.up
        pid = node.pid
        note = r["note"] or ""
        queue = note.split("queue ", 1)[1].strip() if "queue " in note else "(queue not resolvable)"
        item = r["dst_name"] if up else r["src_name"]
        cites = self.cite_stmt(pid, r["line"], "CALL", item)
        other = "mq_out" if up else "mq_in"
        # the whole queue name: APP.REQ is not APP.REQ.BACKOUT
        peers = self.conn.execute("""SELECT d.*, p.program_id AS pname FROM data_flow d JOIN program p ON p.id=d.program_id
                                     WHERE d.kind=? AND UPPER(TRIM(d.note))=? ORDER BY p.program_id, d.line""",
                                  (other, f"QUEUE {queue.upper()}")).fetchall() if "(" not in queue else []
        mine = self.pf(r["dst_pfield"] if up else r["src_pfield"])
        out = []
        for p in peers:
            tpf = self.pf(p["src_pfield"] if up else p["dst_pfield"])
            if tpf is None or mine is None:
                self.unlinked += 1
                continue
            mapped = self.overlay(node, mine, tpf)
            if mapped is None:
                continue
            n_lo, n_hi, labels, cut = mapped
            if cut:
                continue            # the node's bytes lie past the end of the other side: not shared
            for (row, plo, phi, how) in self.place_ref(tpf, n_lo, n_hi):
                ch = self.child(p["program_id"], row, plo, phi, how, node.hop + 1)
                self.touch(p["program_id"])
                arrow = "<-" if up else "->"
                out.append(_Edge(7, p["pname"], r["line"], f"MQ {queue} {arrow} {p['pname']}.{self.nm(ch)} {self.desc(ch)}".rstrip()
                                 + self._lbls(labels), cites + "; " + self.cite_stmt(p["program_id"], p["line"], "CALL", tpf["name"]),
                                 child=ch))
        if not out:
            out.append(_Edge(7, "MQ", r["line"], f"MQ {'GET' if up else 'PUT'} {queue}", cites,
                             end=END_MQ.format(q=queue) if not up else END_NO_WRITER))
        return out

    # ---- edges: up (mirrors) ---------------------------------------------------------------------------
    def edges_up(self, node: _Node, ids: Set[int]) -> List[_Edge]:
        out: List[_Edge] = []
        pid = node.pid
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"SELECT * FROM data_flow WHERE program_id=? AND dst_pfield IN ({q}) ORDER BY line, id",
                                 (pid, *ids)).fetchall()
        for r in rows:
            k = r["kind"]
            if k in COPY_KINDS:
                out.extend(self.copy_edges_up(node, r))
            elif k in DERIVED_KINDS:
                if self.o.derived:
                    out.extend(self.copy_edges_up(node, r, derived=True))
                else:
                    self.derived += 1
            elif k == "io_in" and r["dst_pfield"] == node.root:
                out.extend(self.file_edges_up(node, r))
            elif k == "returning":
                out.extend(self.returning_up(node, r))
            elif k == "cics_in":
                out.extend(self.cics_in_edges(node, r))
            elif k == "dli_in":
                out.extend(self.dli_edges(node, r))
            elif k == "mq_in":
                out.extend(self.mq_edges(node, r))
        # pointer alias, both ways: an item SET ADDRESS OF to a pointer that holds x
        for r in self.conn.execute(f"SELECT * FROM data_flow WHERE program_id=? AND kind='set_address' AND dst_pfield IN ({q})",
                                   (pid, *ids)):
            p = self.pf(r["src_pfield"])
            if p is None or (p["usage"] or "").upper() != "POINTER":
                continue
            for r2 in self.conn.execute("SELECT * FROM data_flow WHERE program_id=? AND kind='set_address' AND dst_pfield=?",
                                        (pid, p["id"])):
                x = self.pf(r2["src_pfield"])
                a = self.pf(r["dst_pfield"])
                if x is None or a is None:
                    continue
                mapped = self.overlay(node, a, x)
                if mapped is None:
                    continue
                n_lo, n_hi, labels, cut = mapped
                if cut:
                    continue            # the node's bytes lie past the end of the other side: not shared
                for (row, plo, phi, how) in self.place_ref(x, n_lo, n_hi):
                    ch = self.child(pid, row, plo, phi, how, node.hop + 1)
                    out.append(_Edge(8, node.pname, r["line"], f"pointer alias {p['name']} <- {self.nm(ch)} {self.desc(ch)}".rstrip()
                                     + self._lbls(labels), self.cite_stmt(pid, r["line"], "SET", a["name"]), child=ch))
        out.extend(self.param_in(node))
        out.extend(self.arg_back(node, ids))
        out.extend(self.sql_edges(node, ids, "read"))
        return out

    def copy_edges_up(self, node: _Node, r, derived: bool = False) -> List[_Edge]:
        pid = node.pid
        verb = r["verb"] or "MOVE"
        src, dst = self.pf(r["src_pfield"]), self.pf(r["dst_pfield"])
        if r["kind"] == "move_corr":
            return self.corr_edges(node, r, src, dst)
        cites = self.cite_stmt(pid, r["line"], verb, r["dst_name"])
        lbl_verb = {"read_into": "READ INTO", "write_from": "WRITE FROM", "set": "SET"}.get(r["kind"], verb)
        if src is None or dst is None:
            self.unlinked += 1
            return [_Edge(9, node.pname, r["line"], f"{lbl_verb} <- {r['src_name']}", cites, r["guard"], end=END_AMBIGUOUS)]
        mapped = self.map_bytes(node, src, r["src_refmod"], dst, r["dst_refmod"], reverse=True,
                                src_sub=bool(r["src_sub"]), dst_sub=bool(r["dst_sub"]))
        if mapped is None:
            return []
        n_lo, n_hi, labels, end = mapped
        subs = self._subs(r, src, dst)
        if derived:
            n_lo, n_hi = self.extent(src)
            labels, end, lbl_verb = [], None, f"derived ({verb})"
        rank = 10 if derived else (1 if src["section"] == "FILE" else 3 if src["section"] == "LINKAGE" else 9)
        if end:
            return [_Edge(rank, node.pname, r["line"], f"{lbl_verb} <- {self.base_name(pid, src)}{subs}" + self._lbls(labels),
                          cites, r["guard"], end=end)]
        d0, d1 = self.extent(dst)
        group_dst = bool(dst["is_group"]) and (node.lo > d0 or node.hi < d1) and not derived
        out = []
        for (row, plo, phi, how) in self.place_copy(src, dst, src, n_lo, n_hi):
            ch = self.carry(node, self.child(pid, row, plo, phi, how, node.hop + 1))
            if group_dst:
                label = f"group {lbl_verb} {dst['name']} <- {src['name']}: bytes {n_lo + 1}-{n_hi} come from {self.nm(ch)}"
            else:
                label = f"{lbl_verb} <- {self.hop_prefix(ch)}{self.nm(ch)}{subs}"
                d = self.desc(ch)
                if d:
                    label += " " + d
            out.append(_Edge(rank, node.pname, r["line"], label + self._lbls(labels), cites, r["guard"], child=ch))
        return out

    def file_edges_up(self, node: _Node, r) -> List[_Edge]:
        pid = node.pid
        select = _note_file(r["note"])
        fd = self.conn.execute("SELECT * FROM file_decl WHERE program_id=? AND UPPER(select_name)=?",
                               (pid, (select or "").upper())).fetchone() if select else None
        verb = r["verb"] or "READ"
        rec = self.pf(node.root)["name"]
        stmt_cite = self.cite_stmt(pid, r["line"], verb, select or rec)
        if fd is None or not fd["assign_dd"]:
            return [_Edge(1, node.pname, r["line"], f"{verb} {rec} <- (no SELECT/ASSIGN for it)", stmt_cite, end=END_NO_WRITER)]
        steps = [s for s in self.steps_of(node.pname) if self.dd_matches(fd["assign_dd"], s["dd_name"])]
        cf = self.conn.execute("SELECT dsname FROM cics_file WHERE UPPER(name)=? AND dsname IS NOT NULL",
                               (fd["assign_dd"].upper(),)).fetchone()
        if not steps and not cf:
            return [_Edge(1, node.pname, r["line"], f"{verb} {rec} <- DD {fd['assign_dd']}: no job step runs {node.pname} "
                          f"with this DD", stmt_cite, end=END_NO_WRITER)]
        out = []
        for s in steps:
            where = f"{s['job_name'] or ('PROC ' + (s['proc_name'] or '?'))} {s['step_name']} DD {s['dd_name']}"
            label = f"{verb} {rec} <- {s['dsn_resolved']} ({where}, {s['mode']}/{s['mode_source']})"
            cites = stmt_cite + "; " + self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], True, s["step_name"])
            ds = _Node(pid, node.pname, node.root, node.lo, node.hi, node.pf, node.hop, kind="dataset",
                       data={"dsn": s["dsn_resolved"], "job_id": s["job_id"], "proc_id": s["proc_id"], "is_temp": s["is_temp"], "dd_id": s["dd_id"],
                             "gdg": s["gdg_rel"], "writer_pf": node.pf})
            out.append(_Edge(1, node.pname, r["line"], label, cites, child=ds))
        if cf:
            ds = _Node(pid, node.pname, node.root, node.lo, node.hi, node.pf, node.hop, kind="dataset",
                       data={"dsn": cf["dsname"], "job_id": None, "proc_id": None, "is_temp": 0, "dd_id": None, "gdg": None, "writer_pf": node.pf})
            out.append(_Edge(1, node.pname, r["line"], f"{verb} {rec} <- {cf['dsname']} (CICS FILE {fd['assign_dd']})", stmt_cite,
                             child=ds))
        return out

    def returning_up(self, node: _Node, r) -> List[_Edge]:
        pid = node.pid
        c = self.conn.execute("SELECT * FROM call_edge WHERE program_id=? AND line=? AND returning_item IS NOT NULL",
                              (pid, r["line"])).fetchone()
        cites = self.cite_stmt(pid, r["line"], "CALL", r["dst_name"])
        if c is None:
            return []
        targets = [c["target"]] if c["target"] else [t for t, _c in _candidates(c)]
        out = []
        for t in targets:
            if t.startswith("(+"):
                out.append(_Edge(2, "?", r["line"], f"RETURNING <- {c['via_var']}: {t} candidate(s) not listed", cites,
                                 end=END_DYNAMIC.format(x=c["via_var"])))
                continue
            progs = Q.programs_named(self.conn, t)
            if not progs:
                out.append(_Edge(2, t.upper(), r["line"], f"RETURNING <- {t}", cites, end=END_NO_CALLEE))
                continue
            callee = progs[0]
            prm = self.conn.execute("SELECT * FROM param WHERE program_id=? AND pos=0", (callee["id"],)).fetchone()
            if prm is None or prm["pfield"] is None:
                out.append(_Edge(2, callee["program_id"], r["line"], f"RETURNING <- {callee['program_id']} (no RETURNING item)",
                                 cites, end=END_POS))
                continue
            ppf = self.pf(prm["pfield"])
            ch = self.child(callee["id"], ppf, *self.extent(ppf), "exact", node.hop + 1)
            # the RETURNING item of THIS call: a parameter copied into it came from this caller only
            ch.via_call, ch.via_root = c["id"], ppf["root_id"]
            self.touch(callee["id"])
            out.append(_Edge(2, callee["program_id"], r["line"], f"RETURNING <- {callee['program_id']}.{self.nm(ch)} "
                             f"{self.desc(ch)} (pos 0)".replace("  ", " "), cites, child=ch))
        if not targets:
            out.append(_Edge(2, "?", r["line"], f"RETURNING <- {c['via_var']}", cites, end=END_DYNAMIC.format(x=c["via_var"])))
        return out

    def param_in(self, node: _Node) -> List[_Edge]:
        """--up mirror of rule 2: a LINKAGE parameter's bytes come from every
        caller's argument at that position (BY CONTENT included: the value
        does arrive)."""
        out: List[_Edge] = []
        if node.via_call is not None and node.root == node.via_root:
            return out          # written back through one CALL: its argument is where the walk came from
        for prm in self.params_of_root(node):
            ppf = self.pf(prm["pfield"])
            # RETURN TRANSID(t) COMMAREA(x): the next task's program gets x as DFHCOMMAREA (tran_edges)
            for c in self.callers_of(node.pname, prm["entry"], node.via_call, transid=prm["entry"] == "DFHCOMMAREA"):
                if prm["entry"] == "DFHCOMMAREA":
                    args = self.conn.execute("SELECT * FROM call_arg WHERE call_id=? AND how IN ('commarea','start_from')",
                                             (c["id"],)).fetchall()
                else:
                    args = self.conn.execute("SELECT * FROM call_arg WHERE call_id=? AND pos=?", (c["id"], prm["pos"])).fetchall()
                word = _CALL_WORD.get(c["kind"], "CALL")
                tag = self.call_tag(prm, c)
                for a in args:
                    cites = self.cite_stmt(c["cpid"], c["line"], word, a["name"])
                    if a["how"] == "length_of":
                        out.append(_Edge(2, c["caller"], c["line"], f"{word} {tag} <- LENGTH OF {c['caller']}.{a['name']}", cites,
                                         end=END_LENGTH_OF))
                        continue
                    apf = self.pf(a["pfield"])
                    if apf is None:
                        self.unlinked += 1
                        continue
                    mapped = self.overlay(node, ppf, apf)
                    if mapped is None:
                        continue
                    n_lo, n_hi, labels, end = mapped
                    if a["how"] in ("content", "value"):
                        labels.append("BY CONTENT")
                    if prm["entry"] == "DFHCOMMAREA":
                        labels.append("by CICS convention")
                    if end:
                        out.append(_Edge(2, c["caller"], c["line"], f"{word} {tag} <- {c['caller']}.{a['name']}" + self._lbls(labels),
                                         cites, end=end))
                        continue
                    for (row, plo, phi, how) in self.place_ref(apf, n_lo, n_hi):
                        ch = self.child(c["cpid"], row, plo, phi, how, node.hop + 1)
                        self.touch(c["cpid"])
                        out.append(_Edge(2, c["caller"], c["line"], f"{word} {tag} <- {c['caller']}.{self.nm(ch)} {self.desc(ch)}".rstrip()
                                         + self._lbls(labels), cites, child=ch))
        return out

    def arg_back(self, node: _Node, ids: Set[int]) -> List[_Edge]:
        """--up mirror of rule 3: a BY REFERENCE argument holds what the callee
        wrote into the parameter (only when the callee writes it)."""
        return [e for e, _w in self.arg_back_iter(node, ids)]

    def arg_back_iter(self, node: _Node, ids: Set[int]) -> Iterator[Tuple[_Edge, Tuple]]:
        """arg_back's edges one by one, each with the passes_on keys its way
        back goes through (passes_on stops at the first that qualifies)."""
        pid = node.pid
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"""SELECT a.*, c.kind AS ckind, c.target, c.via_var, c.resolved, c.resolution, c.line AS cline,
                                            c.id AS cid
                                     FROM call_arg a JOIN call_edge c ON c.id=a.call_id
                                     WHERE a.program_id=? AND a.pfield IN ({q}) AND a.how IN ('reference','commarea')
                                     ORDER BY c.line, a.pos""", (pid, *ids)).fetchall()
        for a in rows:
            if a["ckind"] in ("cics_return", "cics_start"):
                continue                # RETURN / START TRANSID: the next task gets a copy, nothing comes back
            apf = self.pf(a["pfield"])
            a0, a1 = self.extent(apf)
            if max(node.lo, a0) >= min(node.hi, a1):
                continue                # the argument does not hold the node's bytes
            targets = [(a["target"], None)] if a["target"] else _candidates(a)
            word = _CALL_WORD.get(a["ckind"], "CALL")
            cites = self.cite_stmt(pid, a["cline"], word, a["name"])
            tag = "COMMAREA" if a["how"] == "commarea" else f"arg {a['pos']}"
            # XCTL never comes back: a program it transfers to that cannot be followed is no origin here.
            # Any other callee that can write the bytes but cannot be followed is a labelled end, never a
            # silent drop - the origins listed would look complete (downstream call_edges ends each alike)
            xctl = a["ckind"] == "cics_xctl"
            may = "may set it (BY REFERENCE)"
            if not targets and not xctl:
                yield _Edge(3, "?", a["cline"], f"{word} {a['via_var']} {tag} <- ? {may}", cites,
                            end=END_DYNAMIC.format(x=a["via_var"])), ()
                continue
            for (t, cand) in targets:
                if t.startswith("(+"):
                    if not xctl:
                        yield _Edge(3, "?", a["cline"], f"{word} {a['via_var']} {tag} <- {t} candidate(s) not "
                                    f"listed, each {may}", cites, end=END_DYNAMIC.format(x=a["via_var"])), ()
                    continue
                callname = f"{word} {t}" + (f" ({cand})" if cand else "")
                progs = Q.programs_named(self.conn, t)
                if not progs:
                    if not xctl:
                        yield _Edge(3, t.upper(), a["cline"], f"{callname} {tag} <- {t.upper()} {may}", cites,
                                    end=END_NO_CALLEE), ()
                    continue
                callee = progs[0]
                cname = callee["program_id"]
                if a["how"] == "commarea":
                    prm = self.conn.execute("SELECT * FROM param WHERE program_id=? AND entry='DFHCOMMAREA'", (callee["id"],)).fetchone()
                else:
                    prm = self.conn.execute("SELECT * FROM param WHERE program_id=? AND entry IS ? AND pos=?",
                                            (callee["id"], self.entry_for(callee, t), a["pos"])).fetchone()
                if prm is None:
                    if xctl:
                        continue
                    if a["how"] == "commarea":
                        yield _Edge(3, cname, a["cline"], f"{callname} {tag} <- {cname}", cites, end=END_NO_COMMAREA), ()
                        continue
                    n = self.conn.execute("SELECT COUNT(*) FROM param WHERE program_id=? AND entry IS ? AND pos>0",
                                          (callee["id"], self.entry_for(callee, t))).fetchone()[0]
                    yield _Edge(3, cname, a["cline"], f"{callname} {tag} <- {cname} (callee declares {n} parameter(s))",
                                cites, end=END_POS), ()
                    continue
                if prm["pfield"] is None:
                    if xctl:
                        continue
                    yield _Edge(3, cname, a["cline"], f"{callname} {tag} <- {cname}.{prm['name']} {may}", cites,
                                end=END_MISSING.format(x=self.missing_copy(callee))), ()
                    continue
                ppf = self.pf(prm["pfield"])
                mapped = self.overlay(node, apf, ppf)
                if mapped is None:
                    continue
                n_lo, n_hi, labels, cut = mapped
                if cut:
                    continue            # the node's bytes lie past the end of the other side: not shared
                for (row, plo, phi, how) in self.place_ref(ppf, n_lo, n_hi):
                    ch = self.child(callee["id"], row, plo, phi, how, node.hop + 1)
                    ch.via_call, ch.via_root = a["cid"], ppf["root_id"]
                    # only the bytes the callee writes come back: CA-MESSAGE set there is not CA-STATUS
                    cids, _cl = self.closure_ids(ch)
                    # the callee may also hand the bytes on BY REFERENCE to a program that sets them (or
                    # that cannot be followed): that callee is on the path back, as the down walker's CALL
                    written = self.written_here(ch, cids)
                    if not written and not self.passes_on(ch, cids):
                        continue
                    way = () if written else self._witness
                    how_set = "written there" if written else "passed on BY REFERENCE there"
                    if a["ckind"] == "cics_xctl":
                        # written there, but XCTL never comes back: not an origin of this program's bytes
                        yield _Edge(3, callee["program_id"], a["cline"], f"{callname} {tag}: {callee['program_id']}."
                                    f"{self.nm(ch)} is {how_set}", cites, end=END_XCTL), ()
                        continue
                    self.touch(callee["id"])
                    yield _Edge(3, callee["program_id"], a["cline"], f"{callname} {tag} <- {callee['program_id']}.{self.nm(ch)} "
                                f"{self.desc(ch)} ({'written there, BY REFERENCE' if written else how_set})".replace("  ", " ")
                                + self._lbls(labels), cites, child=ch), way

    def passes_on(self, node: _Node, ids: Set[int]) -> bool:
        """Does the program pass the node's bytes BY REFERENCE to a callee
        that sets them, or that --up cannot follow (a labelled end there)?
        XCTL never comes back, so it is no way back. Past the hop limit the
        CALL alone counts: the node is printed there with its edges not
        followed. A CALL chain that comes back to a node being asked about
        is no origin. Each item is asked once per hop count (pass_search)."""
        q = ",".join("?" * len(ids))
        if not self.conn.execute(f"""SELECT 1 FROM call_arg a JOIN call_edge c ON c.id=a.call_id
                                     WHERE a.program_id=? AND a.pfield IN ({q}) AND a.how IN ('reference','commarea')
                                     AND c.kind NOT IN ('cics_return','cics_start','cics_xctl') LIMIT 1""",
                                 (node.pid, *ids)).fetchone():
            return False
        if node.hop > self.o.hops:
            self._witness = ()
            return True
        return self.pass_search((node.pid, node.root, node.lo, node.hi), self.o.hops - node.hop,
                                lambda: self.arg_back_iter(node, ids))

    # ---- per-node information: sets, uses, writers -------------------------------------------------------
    def sets(self, node: _Node, ids: Set[int]) -> List[str]:
        q = ",".join("?" * len(ids))
        out, seen = [], set()
        for r in self.conn.execute(f"""SELECT * FROM data_flow WHERE program_id=? AND dst_pfield IN ({q}) AND kind IN
                                       ('literal','figurative','initialize','accept') ORDER BY line, id""", (node.pid, *ids)):
            verb = r["verb"] or "MOVE"
            if r["kind"] in ("literal", "figurative"):
                what = f"{verb} {r['src_lit']}"
            elif r["kind"] == "accept":
                what = f"ACCEPT FROM {r['note'] or '?'}"
            else:
                what = verb
            cites = self.cite_stmt(node.pid, r["line"], verb, r["dst_name"])
            text = f"also set here: {what}   {cites}" + (f"   guard: {r['guard']}" if r["guard"] else "")
            if text not in seen:
                seen.add(text)
                out.append(text)
        return out

    def uses(self, node: _Node, ids: Set[int]) -> Optional[str]:
        """IF / EVALUATE / DISPLAY / a WHERE clause on the node's names: the
        value is looked at, not copied."""
        q = ",".join("?" * len(ids))
        rows = self.conn.execute(f"""SELECT * FROM field_ref WHERE program_id=? AND pfield_id IN ({q})
                                     AND (mode IN ('test','display') OR (mode='read' AND stmt='EXEC-SQL')) ORDER BY line, id""",
                                 (node.pid, *ids)).fetchall()
        texts, cites, seen = [], [], set()
        for r in rows:
            if r["mode"] == "read":
                if not self.conn.execute("SELECT 1 FROM sql_col_ref WHERE program_id=? AND line=? AND UPPER(host_var)=? AND mode='predicate'",
                                         (node.pid, r["line"], r["name"].upper())).fetchone():
                    continue
            pf = self.pf(r["pfield_id"])
            m, ln, depth, via = Q.origin(self.conn, node.pid, r["line"])
            if not m or ln is None:
                continue
            tag = f"{m}:{ln}" + (f" (via COPY {via})" if depth else "")
            if r["mode"] == "display":
                text, cite = "DISPLAY", tag
            elif r["mode"] == "read":
                text, cite = f"EXEC SQL WHERE :{r['name']}", tag
            else:
                raw = self.raw(m, ln)
                stmt = (r["stmt"] or "IF").upper()
                mm = re.search(r"(?<![\w-])" + re.escape(stmt) + r"(?![\w-])", raw, re.I)
                body = raw[mm.start():].strip() if mm else f"{stmt} {r['name']}"
                body = re.sub(r"\s+", " ", body).rstrip(".")
                if pf is not None and r["name"].upper() != pf["name"].upper():
                    c88 = self.conn.execute("SELECT values_lit FROM cond88 WHERE UPPER(name)=? LIMIT 1", (r["name"].upper(),)).fetchone()
                    if c88 is not None:
                        vals = " ".join(str(v) for v in Q._jl(c88["values_lit"]))
                        text = f"88 {r['name']} {vals} tested".replace("  ", " ")
                        om = re.search(r"(?<![\w-])" + re.escape(r["name"]) + r"(?![\w-])", raw, re.I)
                        tok = self._tok(raw[mm.start() if mm else 0:om.end()] if om else body)
                        cite = f'{tag} "{tok}"'
                        if (text, cite) not in seen:
                            seen.add((text, cite))
                            texts.append(text)
                            cites.append(cite)
                        continue
                text, cite = body[:TOKEN_MAX], tag
            if (text, cite) in seen:
                continue
            seen.add((text, cite))
            texts.append(text)
            cites.append(cite)
        if not texts:
            return None
        return "; ".join(texts) + "   " + "; ".join(cites)

    def writers(self, node: _Node, ids: Set[int], exclude: Set[int]) -> int:
        q = ",".join("?" * len(ids))
        lines = {r[0] for r in self.conn.execute(f"SELECT DISTINCT line FROM data_flow WHERE program_id=? AND dst_pfield IN ({q})",
                                                  (node.pid, *ids))}
        lines |= {r[0] for r in self.conn.execute(f"""SELECT DISTINCT line FROM field_ref WHERE program_id=? AND pfield_id IN ({q})
                                                       AND {_SETS_HERE}""", (node.pid, *ids))}
        return len(lines - exclude)

    def set_from(self, node: _Node, ids: Set[int]) -> List[Tuple[int, str]]:
        """The root's own sources (down mode): the copies into it, as context."""
        q = ",".join("?" * len(ids))
        out = []
        for r in self.conn.execute(f"""SELECT * FROM data_flow WHERE program_id=? AND dst_pfield IN ({q}) AND kind IN
                                       ('move','move_corr','set','read_into','write_from','returning','cics_in','dli_in','mq_in','io_in')
                                       ORDER BY line, id""", (node.pid, *ids)):
            src = self.pf(r["src_pfield"])
            verb = r["verb"] or "MOVE"
            if src is None:
                what = f"{verb} {r['note'] or ''}".strip() if r["src_name"] is None else f"{verb} {r['src_name']}"
                operand = _note_file(r["note"]) if r["kind"] == "io_in" else None
                out.append((r["line"], f"set from {what}   {self.cite_stmt(node.pid, r['line'], verb, operand or r['dst_name'])}"))
                continue
            sn = _Node(node.pid, node.pname, src["root_id"], *self.extent(src), src, 0)
            out.append((r["line"], f"set from {self.base_name(node.pid, src)} {self.desc(sn, True)[:-1]}, {self.cite_def(src, node.pid)})"
                        f"   {self.cite_stmt(node.pid, r['line'], verb, r['dst_name'])}"
                        + (f"   guard: {r['guard']}" if r["guard"] else "")))
        return out

    # ---- emission ------------------------------------------------------------------------------
    def emit_node(self, e: _Edge, num: str, depth: int) -> None:
        node = e.child
        self.touch(node.pid)
        guard = f"   guard: {e.guard}" if e.guard else ""
        head = f"{e.label}{guard}   {e.cites}"
        prev = self.seen(node)
        if prev is not None:
            self.put(depth, self.fmt(num, depth, f"{head}   (shown as {prev})"))
            return
        self.visit(node, num)
        self.count += 1
        ids, cl = self.closure_ids(node)
        edges = self.edges(node)
        uses = self.uses(node, ids)
        tail = ""
        at_limit = bool(edges) and node.hop >= self.o.hops
        if at_limit:
            tail = f"   ({len(edges)} edge(s) not followed)" + self.end(END_HOPS.format(n=self.o.hops), num)
            edges = []
        elif not edges and not uses:
            tail = self.end(END_UNNAMED if node.unnamed else END_NO_USE.format(p=node.pname), num)
        self.put(depth, self.fmt(num, depth, head + tail))
        part = self.partial_note(node.pid) if node.pid not in self._partial_shown else None
        if part:
            self._partial_shown.add(node.pid)
            self.put(depth, self.sub(depth, part))
        for x in e.extra:
            self.put(depth, self.sub(depth, x))
        if at_limit:
            # the node at the limit prints its edge count only: what sets or
            # tests it there is the next hop's business (--hops N+1)
            return
        also = [f"{self.base_name(node.pid, r)} ({why})" for (r, why) in cl if why != "self"]
        if also:
            self.put(depth, self.sub(depth, "also read as: " + ", ".join(also)))
        n = self.writers(node, ids, {e.line} if e.line else set())
        if n:
            self.put(depth, self.sub(depth, f"also written by {n} other statement{'s' if n != 1 else ''} (order not checked)"))
        for s in self.sets(node, ids):
            self.put(depth, self.sub(depth, s))
        if uses:
            self.put(depth, self.sub(depth, uses + ("" if edges else self.end(END_TESTED, num))))
        self.children(node, edges, num, depth + 1)

    def emit_dataset(self, e: _Edge, num: str, depth: int) -> None:
        ds = e.child
        self.put(depth, self.fmt(num, depth, f"{e.label}   {e.cites}"))
        leaves = self.dataset_leaves(ds, self.cobol_reader)
        if not leaves:
            self.put(depth, self.sub(depth, f"no step {'writes' if self.o.up else 'reads'} {ds.data['dsn']}"
                                            + self.end(END_NO_WRITER if self.o.up else END_NO_READER, num)))
            return
        printed: Set[str] = set()
        shown = leaves if self.o.show_all else leaves[:self.o.width]
        for i, leaf in enumerate(shown, 1):
            for (text, cites) in leaf.path:
                key = text + cites
                if key not in printed:
                    printed.add(key)
                    self.put(depth, self.sub(depth, f"{text}   {cites}"))
            cnum = f"{num}.{i}"
            if leaf.end:
                self.put(depth + 1, self.fmt(cnum, depth + 1, f"{leaf.text}   {leaf.cites}".rstrip() + self.end(leaf.end, cnum)))
                continue
            if self.count >= self.o.nodes:
                self.dropped_nodes += 1
                self.put(depth + 1, self.fmt(cnum, depth + 1, f"{leaf.text}   {leaf.cites}" + self.end(END_NODES, cnum)))
                continue
            self.emit_node(_Edge(1, leaf.node.pname, 0, leaf.text, leaf.cites, child=leaf.node), cnum, depth + 1)
        if len(leaves) > len(shown):
            rest = leaves[len(shown):]
            progs = defaultdict(int)
            for lf in rest:
                progs[lf.node.pname if lf.node else lf.text.split(" ")[0]] += 1
            self.dropped_width += len(rest)
            self.put(depth + 1, self.sub(depth, f"... {len(rest)} more reader(s) in {len(progs)} program(s): "
                                                + ", ".join(f"{p} {n}" for p, n in sorted(progs.items(), key=lambda x: -x[1]))
                                                + " (--all)" + self.end(END_WIDTH, num)))

    def children(self, node: _Node, edges: List[_Edge], prefix: str, depth: int) -> None:
        shown = edges if self.o.show_all else edges[:self.o.width]
        for i, e in enumerate(shown, 1):
            num = f"{prefix}.{i}" if prefix else str(i)
            if e.child is None:
                guard = f"   guard: {e.guard}" if e.guard else ""
                line = f"{e.label}{guard}   {e.cites}"
                if e.end:
                    line += self.end(e.end, num)
                self.put(depth, self.fmt(num, depth, line))
                for x in e.extra:
                    self.put(depth, self.sub(depth, x))
                if e.table:
                    tnum = f"{num}.1"
                    self.count += 1
                    tl = e.table.split("\n")
                    self.put(depth + 1, self.fmt(tnum, depth + 1, tl[0]))
                    for t in tl[1:]:
                        self.put(depth + 1, self.sub(depth + 1, t))
                continue
            if e.child.kind == "dataset":
                self.emit_dataset(e, num, depth)
                continue
            if self.count >= self.o.nodes:
                rest = shown[i - 1:]
                progs = defaultdict(int)
                for x in rest:
                    progs[x.prog] += 1
                self.dropped_nodes += len(rest)
                self.put(depth, self.sub(depth - 1, f"... {len(rest)} more branch(es) in {len(progs)} program(s): "
                                                    + ", ".join(f"{p} {n}" for p, n in sorted(progs.items(), key=lambda x: -x[1]))
                                                    + f" not printed (--nodes {self.o.nodes})" + self.end(END_NODES, prefix or "root")))
                break
            self.emit_node(e, num, depth)
        if len(edges) > len(shown):
            rest = edges[len(shown):]
            progs = defaultdict(int)
            for x in rest:
                progs[x.prog] += 1
            self.dropped_width += len(rest)
            self.put(depth, self.sub(depth - 1, f"... {len(rest)} more target(s) in {len(progs)} program(s): "
                                                + ", ".join(f"{p} {n}" for p, n in sorted(progs.items(), key=lambda x: (-x[1], x[0])))
                                                + " (--all)" + self.end(END_WIDTH, prefix or "root")))

    # ---- the root ------------------------------------------------------------------------------
    def run(self, pid: int, row, via88: Optional[str] = None) -> _Node:
        self.root_pid = pid
        self.touch(pid)
        p = self.prog(pid)
        lo, hi = self.extent(row)
        node = _Node(pid, p["program_id"], row["root_id"], lo, hi, row, 0)
        self.visit(node, "root")
        ids, cl = self.closure_ids(node)
        edges = self.edges(node)
        sf = self.set_from(node, ids) if not self.o.up else []
        n_writers = self.writers(node, ids, {ln for ln, _t in sf})
        defined = (f"- defined {self.cite_def(row, pid)} {self.desc(node, True)}"
                   + (f" (via 88 {via88})" if via88 else "")
                   + (f"; also written by {n_writers} other statement{'s' if n_writers != 1 else ''} (order not checked)"
                      if n_writers else ""))
        self.put(0, defined)
        part = self.partial_note(pid)
        if part:
            self._partial_shown.add(pid)
            self.put(0, f"- {part}")
        also = [f"{self.base_name(pid, r)} ({why})" for (r, why) in cl if why != "self"]
        if also:
            self.put(0, "- also read as: " + ", ".join(also))
        for _ln, t in sf:
            self.put(0, f"- {t}")
        for s in self.sets(node, ids):
            self.put(0, f"- {s}")
        uses = self.uses(node, ids)
        if uses:
            self.put(0, f"- {uses}" + ("" if edges else self.end(END_TESTED, "root")))
        elif not edges:
            self.put(0, f"- {END_NO_USE.format(p=p['program_id'])}" + self.end(END_NO_USE.format(p=p["program_id"]), "root"))
        self.children(node, edges, "", 1)
        return node


# ---------------------------------------------------------------------------
# the fallback walker over field_ref / call_edge / sql_col_ref
# ---------------------------------------------------------------------------

class _FNode:
    __slots__ = ("pid", "pname", "name", "frow", "hop", "kind", "via_call", "via_name")

    def __init__(self, pid: int, pname: str, name: str, frow, hop: int) -> None:
        self.pid, self.pname, self.name, self.frow, self.hop = pid, pname, name, frow, hop
        self.kind = "field"
        self.via_call: Optional[int] = None        # as _Node.via_call
        self.via_name: Optional[str] = None        # as _Node.via_root: the parameter the CALL reached


class _Fallback(_Report):
    """Before the re-parse: MOVE pairs only from lines with exactly one MOVE
    read and one MOVE write (a line with more cannot say which read fed
    which write - guard 2), CALL / LINKAGE by position from call_edge and
    linkage_using, DB2 from sql_col_ref, file bytes from the program's own
    field rows. Every hop says (reconstructed)."""

    def __init__(self, conn: sqlite3.Connection, opts: Opts) -> None:
        super().__init__(conn, opts)
        self.unpaired: Dict[int, int] = {}
        self.pairs: Dict[int, List[Tuple[int, str, str]]] = {}
        # (num, via_call, hop) per visit, as _Walker.seen: a CALL's storage never stands in for
        # the item reached by a copy, and a visit cut sooner by --hops never hides a shorter path
        self.visited: Dict[Tuple[int, str], List[Tuple[str, Optional[int], int]]] = defaultdict(list)
        self._prog: Dict[int, sqlite3.Row] = {}
        self._partial_shown: Set[int] = set()

    def prog(self, pid: int):
        if pid not in self._prog:
            self._prog[pid] = self.conn.execute(
                "SELECT p.*, m.name AS member_name, m.parse_status FROM program p JOIN member m ON m.id=p.member_id "
                "WHERE p.id=?", (pid,)).fetchone()
        return self._prog[pid]

    def touch(self, pid: int) -> None:
        self.programs.add(pid)
        self.members.add(self.prog(pid)["member_id"])

    def move_pairs(self, pid: int) -> List[Tuple[int, str, str]]:
        if pid in self.pairs:
            return self.pairs[pid]
        by_line: Dict[int, Tuple[Set[str], Set[str]]] = defaultdict(lambda: (set(), set()))
        for r in self.conn.execute("SELECT line, name, mode FROM field_ref WHERE program_id=? AND stmt='MOVE' AND mode IN ('read','write')",
                                   (pid,)):
            by_line[r["line"]][0 if r["mode"] == "read" else 1].add(r["name"].upper())
        pairs, bad = [], 0
        for ln, (reads, writes) in sorted(by_line.items()):
            if len(reads) == 1 and len(writes) == 1:
                pairs.append((ln, next(iter(reads)), next(iter(writes))))
            elif len(reads) == 0:
                continue                       # a literal / figurative MOVE: a set, not a pair
            else:
                bad += 1
        self.pairs[pid] = pairs
        self.unpaired[pid] = bad
        return pairs

    def decls(self, pid: int, name: str) -> List[sqlite3.Row]:
        """Every declaration of the name in the program's own text."""
        p = self.prog(pid)
        return self.conn.execute("SELECT * FROM field WHERE member_id=? AND UPPER(name)=? ORDER BY id",
                                 (p["member_id"], name.upper())).fetchall()

    def field_row(self, pid: int, name: str):
        # a twice-declared name is none of them: field_ref cannot say which one a statement names (guard 4)
        rows = self.decls(pid, name)
        return rows[0] if len(rows) == 1 else None

    def params(self, pid: int) -> Set[str]:
        """The program's USING parameters (PROCEDURE DIVISION and every ENTRY)."""
        out = {a.upper() for a in Q._jl(self.prog(pid)["linkage_using"])}
        for r in self.conn.execute("SELECT linkage_using FROM program_alias WHERE program_id=?", (pid,)):
            out |= {a.upper() for a in Q._jl(r["linkage_using"])}
        return out

    def carry(self, frm: _FNode, ch: _FNode) -> _FNode:
        """As _Walker.carry: a copy into another parameter stays in the
        invocation the value came through (the fallback cannot see
        LOCAL-STORAGE: parameters only)."""
        if frm.via_call is not None and ch.pid == frm.pid and ch.name in self.params(ch.pid):
            ch.via_call, ch.via_name = frm.via_call, frm.via_name
        return ch

    def start(self, pid: int, name: str) -> Tuple[Optional[sqlite3.Row], Optional[str], bool]:
        """(field row, problem, follow) for a start name with its OF/IN
        qualifiers: a twice-declared name lists its declarations and follows
        none, as the exact walker does (guard 4); a qualified one is defined
        at the right line but still not followed - field_ref drops the
        qualifier, so its statements are both declarations' statements."""
        base, quals = _split_name(name)
        rows = self.decls(pid, base)
        pname = self.prog(pid)["program_id"]
        cands = [r for r in rows if all(q in (r["qualified"] or "").upper().split(".") for q in quals)] if quals else rows
        if quals and not cands and rows:
            return None, f"{base} in {pname}: no declaration matches the qualifier {' OF '.join(quals)}", False
        if len(cands) > 1:
            return None, (f"{base} is declared {len(cands)} times in {pname}: "
                          + ", ".join(f"{c['qualified']} ({self.cite_def(c, pid)})" for c in cands)
                          + " - HUMAN MUST VERIFY which one; run `flow \"" + base + " OF <group>\"`"), False
        if len(rows) > 1:
            return cands[0], (f"{base} is declared {len(rows)} times in {pname}; before the re-parse field_ref names it "
                              "without the qualifier, so their statements cannot be told apart - not followed until "
                              "re-parse - HUMAN MUST VERIFY"), False
        return (cands[0] if cands else None), None, True

    def root_of(self, frow):
        r = frow
        while r is not None and r["parent_id"]:
            r = self.conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
        return r

    def copy_decls(self, pid: int, name: str) -> List[sqlite3.Row]:
        """The name's rows in the copybooks this program COPYs (a copied
        item has no field row of the program's own)."""
        return self.conn.execute("""SELECT f.* FROM field f JOIN member m ON m.id=f.member_id
                                    WHERE UPPER(f.name)=? AND m.kind='copybook' AND UPPER(m.name) IN
                                    (SELECT UPPER(copybook) FROM copy_use WHERE member_id=?) ORDER BY f.id""",
                                 (name.upper(), self.prog(pid)["member_id"])).fetchall()

    def ancestor_names(self, node: _FNode) -> List[str]:
        """The groups above the item: its own text's, or - a copied item - the
        copybook's (the 01 the COPY sits under is not known before the
        re-parse: FALLBACK_COPY says so)."""
        out: List[str] = []
        for r in ([node.frow] if node.frow is not None else self.copy_decls(node.pid, node.name)):
            while r is not None and r["parent_id"]:
                r = self.conn.execute("SELECT * FROM field WHERE id=?", (r["parent_id"],)).fetchone()
                if r is not None and r["name"].upper() not in out and r["name"].upper() != "FILLER":
                    out.append(r["name"].upper())
        return out

    def cond_names(self, node: _FNode) -> List[str]:
        """The 88 names on the item: `IF IR-LAPSED` tests IR-STAT."""
        rows = [node.frow] if node.frow is not None else self.copy_decls(node.pid, node.name)
        ids = [r["id"] for r in rows]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return sorted({r[0].upper() for r in self.conn.execute(f"SELECT name FROM cond88 WHERE field_id IN ({q})", ids)})

    def parent_edges(self, node: _FNode) -> List[_Edge]:
        """A group MOVE, a CALL argument or a COMMAREA that names a group above
        the item carries its bytes too (guard 7). Before the re-parse field_ref
        has no offsets to place them, so each is a labelled end - never a
        silent `no further use`."""
        anc = self.ancestor_names(node)
        if not anc:
            return []
        q = ",".join("?" * len(anc))
        up = self.o.up
        out: List[_Edge] = []
        seen: Set[Tuple[int, str]] = set()
        for r in self.conn.execute(f"""SELECT line, name, stmt FROM field_ref WHERE program_id=? AND UPPER(name) IN ({q})
                                       AND mode=? ORDER BY line, id""", (node.pid, *anc, "write" if up else "read")):
            key = (r["line"], r["name"].upper())
            if key in seen:
                continue
            seen.add(key)
            stmt = (r["stmt"] or "?").upper()
            verb = "CALL" if stmt.startswith("CALL-") else stmt
            words = {"CALL-USING": "CALL USING", "CALL-RETURNING": "CALL RETURNING"}.get(
                stmt, stmt.replace("EXEC-CICS-", "EXEC CICS ").replace("EXEC-SQL", "EXEC SQL"))
            rank = 2 if stmt.startswith(("CALL-", "EXEC-CICS-")) else 9
            label = (f"parent group {key[1]} {'set' if up else 'used'} by {words}: its bytes include {node.name} "
                     f"(reconstructed)")
            out.append(_Edge(rank, node.pname, r["line"], label, self.cite_stmt(node.pid, r["line"], verb, r["name"]),
                             end=FALLBACK_PARENT))
        return out

    def node(self, pid: int, name: str, hop: int) -> _FNode:
        return _FNode(pid, self.prog(pid)["program_id"], name.upper(), self.field_row(pid, name), hop)

    def pfx(self, node: _FNode) -> str:
        return "" if node.pid == self.root_pid else f"{node.pname}."

    def edges(self, node: _FNode) -> List[_Edge]:
        out = self.edges_up(node) if self.o.up else self.edges_down(node)
        out.sort(key=lambda e: (e.rank, e.prog, e.line))
        return out

    def edges_down(self, node: _FNode) -> List[_Edge]:
        out: List[_Edge] = []
        pid, name = node.pid, node.name
        # 1. the file: a program-owned record field written to a file
        out.extend(self.file_edges(node))
        # 2. CALL USING by position
        for c in self.conn.execute("SELECT * FROM call_edge WHERE program_id=? AND using_args IS NOT NULL ORDER BY line", (pid,)):
            args = [a.upper() for a in Q._jl(c["using_args"])]
            for pos in [i + 1 for i, a in enumerate(args) if a == name]:
                word = _CALL_WORD.get(c["kind"], "CALL")
                cites = self.cite_stmt(pid, c["line"], word, name)
                targets = [(c["target"], None)] if c["target"] else _candidates(c)
                if not targets:
                    out.append(_Edge(2, "?", c["line"], f"{word} {c['via_var']} arg {pos} (reconstructed)", cites,
                                     end=END_DYNAMIC.format(x=c["via_var"])))
                for (t, cand) in targets:
                    if t.startswith("(+"):
                        out.append(_Edge(2, "?", c["line"], f"{word} {c['via_var']} arg {pos}: {t} candidate(s) not listed "
                                         f"(reconstructed)", cites, end=END_DYNAMIC.format(x=c["via_var"])))
                        continue
                    callname = f"{word} {t}" if c["target"] else f"{word} {c['via_var']} = {t} ({cand})"
                    progs = Q.programs_named(self.conn, t)
                    if not progs:
                        out.append(_Edge(2, t.upper(), c["line"], f"{callname} arg {pos} (reconstructed)", cites, end=END_NO_CALLEE))
                        continue
                    callee = progs[0]
                    lk = self.linkage_of(callee, t)
                    if not lk and c["kind"] in ("cics_link", "cics_xctl"):
                        # the COMMAREA arrives as the DFHCOMMAREA 01, USING or not (as edges_up)
                        if not self.decls(callee["id"], "DFHCOMMAREA"):
                            out.append(_Edge(2, callee["program_id"], c["line"], f"{callname} COMMAREA -> {callee['program_id']} "
                                             f"(reconstructed)", cites, end=END_NO_COMMAREA))
                            continue
                        lk = ["DFHCOMMAREA"]
                    if pos > len(lk):
                        out.append(_Edge(2, callee["program_id"], c["line"], f"{callname} arg {pos} (callee declares {len(lk)} "
                                         f"parameter(s)) (reconstructed)", cites, end=END_POS))
                        continue
                    ch = self.node(callee["id"], lk[pos - 1], node.hop + 1)
                    ch.via_call, ch.via_name = c["id"], ch.name
                    out.append(_Edge(2, callee["program_id"], c["line"], f"{callname} arg {pos} -> {callee['program_id']}.{ch.name} "
                                     f"(reconstructed){self.call_modes(pid, c['line'])}", cites, child=ch))
        # 3. LINKAGE back (never for a parameter reached through a CALL: _Node.via_call)
        lk = [a.upper() for a in Q._jl(self.prog(pid)["linkage_using"])]
        entries = [(None, lk)] + [(r["alias"], [a.upper() for a in Q._jl(r["linkage_using"])]) for r in
                                  self.conn.execute("SELECT alias, linkage_using FROM program_alias WHERE program_id=?", (pid,))]
        if node.via_call is not None and node.name == node.via_name:
            entries = []
        written = bool(self.conn.execute(f"SELECT 1 FROM field_ref WHERE program_id=? AND UPPER(name)=? AND {_SETS_HERE} "
                                         "LIMIT 1", (pid, name)).fetchone())
        for (entry, using) in entries:
            if name not in using or not written:
                continue
            pos = using.index(name) + 1
            cname = (entry or node.pname).upper()
            for c in self.conn.execute("""SELECT c.*, q.program_id AS caller, q.id AS cpid FROM call_edge c JOIN program q ON q.id=c.program_id
                                          WHERE (UPPER(c.target)=? OR c.resolved LIKE ?) ORDER BY q.program_id, c.line""",
                                       (cname, f'%"{cname}"%')):
                args = Q._jl(c["using_args"])
                if pos > len(args) or (node.via_call is not None and c["id"] != node.via_call):
                    continue
                # a dynamic CALL says so, as _Walker.pos_tag: never printed as a static one
                dyn = ((f" of ENTRY {entry.upper()}" if entry else "")
                       + ("" if c["target"] else f" (CALL {c['via_var']}: {_how_resolved(c)} candidate)"))
                if c["kind"] == "cics_xctl":
                    out.append(_Edge(3, c["caller"], c["line"], f"LINKAGE pos {pos}{dyn}: {c['caller']}.{args[pos - 1]} was passed by XCTL "
                                     f"(reconstructed)", self.cite_stmt(c["cpid"], c["line"], "XCTL", args[pos - 1]), end=END_XCTL))
                    continue
                ch = self.node(c["cpid"], args[pos - 1], node.hop + 1)
                out.append(_Edge(3, c["caller"], c["line"], f"LINKAGE pos {pos}{dyn} -> back to {c['caller']}.{ch.name} (reconstructed)"
                                 + self.call_modes(c["cpid"], c["line"]),
                                 self.cite_stmt(c["cpid"], c["line"], _CALL_WORD.get(c["kind"], "CALL"), args[pos - 1]),
                                 child=ch))
        # 4. DB2
        out.extend(self.sql_edges(node, "write"))
        # 9. local copies
        for (ln, rd, wr) in self.move_pairs(pid):
            if rd == name:
                ch = self.carry(node, self.node(pid, wr, node.hop + 1))
                out.append(_Edge(9, node.pname, ln, f"MOVE -> {self.pfx(ch)}{ch.name} (reconstructed)",
                                 self.cite_stmt(pid, ln, "MOVE", wr), child=ch))
        out.extend(self.parent_edges(node))
        return out

    def edges_up(self, node: _FNode) -> List[_Edge]:
        out: List[_Edge] = []
        pid, name = node.pid, node.name
        out.extend(self.file_edges(node))
        lk = [a.upper() for a in Q._jl(self.prog(pid)["linkage_using"])]
        entries = [(None, lk)] + [(r["alias"], [a.upper() for a in Q._jl(r["linkage_using"])]) for r in
                                  self.conn.execute("SELECT alias, linkage_using FROM program_alias WHERE program_id=?", (pid,))]
        if node.via_call is not None and node.name == node.via_name:
            entries = []
        for (entry, using) in entries:
            if name not in using:
                continue
            pos = using.index(name) + 1
            cname = (entry or node.pname).upper()
            for c in self.conn.execute("""SELECT c.*, q.program_id AS caller, q.id AS cpid FROM call_edge c JOIN program q ON q.id=c.program_id
                                          WHERE (UPPER(c.target)=? OR c.resolved LIKE ?) ORDER BY q.program_id, c.line""",
                                       (cname, f'%"{cname}"%')):
                args = Q._jl(c["using_args"])
                if pos > len(args) or (node.via_call is not None and c["id"] != node.via_call):
                    continue
                ch = self.node(c["cpid"], args[pos - 1], node.hop + 1)
                word = _CALL_WORD.get(c["kind"], "CALL")
                # a dynamic CALL says so, as _Walker.call_tag: never printed as a static one
                tag = (f"{word} {cname}" if c["target"] else
                       f"{word} {c['via_var']} = {cname} (candidate: resolved via {_how_resolved(c)})")
                out.append(_Edge(2, c["caller"], c["line"], f"{tag} arg {pos} <- {c['caller']}.{ch.name} (reconstructed)"
                                 + self.call_modes(c["cpid"], c["line"]),
                                 self.cite_stmt(c["cpid"], c["line"], word, args[pos - 1]), child=ch))
        out.extend(self.arg_back(node))
        out.extend(self.sql_edges(node, "read"))
        for (ln, rd, wr) in self.move_pairs(pid):
            if wr == name:
                ch = self.carry(node, self.node(pid, rd, node.hop + 1))
                out.append(_Edge(9, node.pname, ln, f"MOVE <- {self.pfx(ch)}{ch.name} (reconstructed)",
                                 self.cite_stmt(pid, ln, "MOVE", rd), child=ch))
        out.extend(self.parent_edges(node))
        return out

    def arg_back(self, node: _FNode) -> List[_Edge]:
        """--up: what a callee the node is passed to BY REFERENCE writes into
        it (as _Walker.arg_back), or a labelled end where that cannot be
        followed."""
        return [e for e, _w in self.arg_back_iter(node)]

    def arg_back_iter(self, node: _FNode) -> Iterator[Tuple[_Edge, Tuple]]:
        """arg_back's edges one by one, with their way back (_Walker.arg_back_iter)."""
        pid, name = node.pid, node.name
        for c in self.conn.execute("SELECT * FROM call_edge WHERE program_id=? AND using_args IS NOT NULL ORDER BY line", (pid,)):
            if c["kind"] in ("cics_return", "cics_start"):
                continue            # RETURN / START TRANSID: the next task gets a copy, nothing comes back
            args = [a.upper() for a in Q._jl(c["using_args"])]
            xctl = c["kind"] == "cics_xctl"
            word = _CALL_WORD.get(c["kind"], "CALL")
            hows = self.arg_hows(pid, c, args) if name in args else None
            for pos in [i + 1 for i, a in enumerate(args) if a == name]:
                if hows is not None and hows[pos - 1] != "reference":
                    continue        # BY CONTENT / VALUE, LENGTH OF, ADDRESS OF: never a way back (as _Walker.arg_back)
                # a callee that may write the position but cannot be followed is a labelled end (as
                # _Walker.arg_back); XCTL never comes back, so there it is no origin at all
                modes = self.call_modes(pid, c["line"])
                may = f"may set it (reconstructed){modes}"
                cites = self.cite_stmt(pid, c["line"], word, name)
                targets = [c["target"]] if c["target"] else [t for t, _c in _candidates(c)]
                if not targets and not xctl:
                    yield _Edge(3, "?", c["line"], f"{word} {c['via_var']} arg {pos} <- ? {may}", cites,
                                end=END_DYNAMIC.format(x=c["via_var"])), ()
                for t in targets:
                    if t.startswith("(+"):
                        if not xctl:
                            yield _Edge(3, "?", c["line"], f"{word} {c['via_var']} arg {pos} <- {t} candidate(s) not "
                                        f"listed, each {may}", cites, end=END_DYNAMIC.format(x=c["via_var"])), ()
                        continue
                    callname = f"{word} {t}" + ("" if c["target"] else f" (candidate: resolved via {_how_resolved(c)})")
                    progs = Q.programs_named(self.conn, t)
                    if not progs:
                        if not xctl:
                            yield _Edge(3, t.upper(), c["line"], f"{callname} arg {pos} <- {t.upper()} {may}", cites,
                                        end=END_NO_CALLEE), ()
                        continue
                    callee = progs[0]
                    lk2 = self.linkage_of(callee, t)
                    if not lk2 and c["kind"] == "cics_link":
                        # a CICS program gets its COMMAREA as DFHCOMMAREA with or without a PROCEDURE DIVISION
                        # USING (usually without): the 01 declared is the parameter, only no 01 is none
                        if not self.decls(callee["id"], "DFHCOMMAREA"):
                            yield _Edge(3, callee["program_id"], c["line"], f"{callname} COMMAREA <- {callee['program_id']} "
                                        f"(reconstructed)", cites, end=END_NO_COMMAREA), ()
                            continue
                        lk2 = ["DFHCOMMAREA"]
                    if pos > len(lk2):
                        if not xctl:
                            yield _Edge(3, callee["program_id"], c["line"], f"{callname} arg {pos} <- {callee['program_id']} "
                                        f"(callee declares {len(lk2)} parameter(s)) (reconstructed){modes}", cites,
                                        end=END_POS), ()
                        continue
                    pname = lk2[pos - 1]
                    ch = self.node(callee["id"], pname, node.hop + 1)
                    ch.via_call, ch.via_name = c["id"], ch.name
                    how_set, way = "written there", ()
                    if not self.conn.execute(f"SELECT 1 FROM field_ref WHERE program_id=? AND UPPER(name)=? AND {_SETS_HERE} "
                                             "LIMIT 1", (callee["id"], pname.upper())).fetchone():
                        if self.passes_on(ch):
                            way = self._witness
                            # handed on BY REFERENCE by its own name to a program that sets it (or that cannot
                            # be followed): the callee is on the path back, as the down walker's CALL
                            how_set = "passed on BY REFERENCE there"
                        else:
                            # not written by its own name: a field under it may be (or be passed on), which
                            # field_ref cannot place in the argument's bytes before the re-parse - a labelled
                            # end, never a silent drop
                            under = None if xctl else self.used_under(callee["id"], pname, True)
                            passed = not xctl and under is False and bool(self.used_under(callee["id"], pname, True, passed=True))
                            if not xctl and (under is not False or passed):
                                what = "COMMAREA" if c["kind"] == "cics_link" else f"arg {pos}"
                                how = ("a field under it is written there" if under else
                                       "a field under it is passed on BY REFERENCE there" if passed else
                                       f"its fields are not all in {callee['program_id']}'s own text")
                                yield _Edge(3, callee["program_id"], c["line"], f"{callname} {what} <- "
                                            f"{callee['program_id']}.{pname.upper()}: {how}, {may}", cites,
                                            end=FALLBACK_PARAM), ()
                            continue
                    if c["kind"] == "cics_xctl":
                        # written there, but XCTL never comes back (as _Walker.arg_back)
                        yield _Edge(3, callee["program_id"], c["line"], f"{callname} arg {pos}: {callee['program_id']}.{pname.upper()} "
                                    f"is {how_set} (reconstructed)", self.cite_stmt(pid, c["line"], "XCTL", name), end=END_XCTL), ()
                        continue
                    yield _Edge(3, callee["program_id"], c["line"], f"{callname} arg {pos} <- {callee['program_id']}.{ch.name} "
                                f"({how_set}) (reconstructed){modes}", cites, child=ch), way

    def passes_on(self, node: _FNode) -> bool:
        """As _Walker.passes_on, by name: is the item an argument of a CALL /
        LINK in its program whose callee sets it or cannot be followed?"""
        if not any(node.name in [a.upper() for a in Q._jl(c["using_args"])] for c in self.conn.execute(
                "SELECT using_args FROM call_edge WHERE program_id=? AND using_args IS NOT NULL "
                "AND kind NOT IN ('cics_return','cics_start','cics_xctl')", (node.pid,))):
            return False
        if node.hop > self.o.hops:
            self._witness = ()
            return True
        return self.pass_search((node.pid, node.name), self.o.hops - node.hop, lambda: self.arg_back_iter(node))

    def arg_hows(self, pid: int, c, args: List[str]) -> Optional[List[str]]:
        """How a CALL passes each of its call_edge.using_args (reference,
        content, value, length_of, address_of), read from the statement's own
        text as the parser reads it - an index built before the re-parse
        stores a BY CONTENT argument as a write, as it does a BY REFERENCE
        one. None when that is not a CALL, or its text does not give the
        same names in the same order: then every position may be BY
        REFERENCE (call_modes says so on the hop)."""
        if c["kind"].startswith("cics_"):
            return None
        m, ln, _d, _v = Q.origin(self.conn, pid, c["line"])
        if not m or ln is None:
            return None
        text = []
        for k in range(12):
            raw = self.raw(m, ln + k)
            text.append(raw)
            if raw.rstrip().endswith("."):
                break
        t = " ".join(text)
        mc = re.search(r"(?<![\w-])CALL(?![\w-])", t, re.I)
        if not mc:
            return None
        named = [a for a in cobol._parse_using_detail(t[mc.start():])[0] if a.name]
        if [a.name.upper() for a in named] != args:
            return None
        return [a.how for a in named]

    def call_modes(self, pid: int, exp_line: int) -> str:
        """call_edge.using_args has no BY CONTENT / LENGTH OF: when the CALL's
        own text holds one, a position may be one-way or a length - said on
        the hop, never assumed away (guards 9, 10)."""
        m, ln, _d, _v = Q.origin(self.conn, pid, exp_line)
        if not m or ln is None:
            return ""
        text = []
        for k in range(8):
            raw = self.raw(m, ln + k)
            text.append(raw)
            if raw.rstrip().endswith("."):
                break
        t = " ".join(text).upper()
        if re.search(r"(?<![\w-])(?:CONTENT|VALUE|LENGTH\s+OF)(?![\w-])", t):
            return " (this CALL passes BY CONTENT / VALUE / LENGTH OF: the position may be one-way or a length - HUMAN MUST VERIFY)"
        return ""

    def used_under(self, pid: int, name: str, up: bool, passed: bool = False) -> Optional[bool]:
        """Is a field under the item written (up) or read, tested or shown
        (down) in the program - or, with `passed`, named in a CALL USING
        there? True: one in its own text is; False: it is
        elementary, or a group none of whose own-text fields is; None: the
        fallback cannot tell (declared in a copybook or twice, or a group
        whose fields come from a COPY)."""
        frow = self.field_row(pid, name)
        if frow is None:
            return None
        if not frow["is_group"]:
            return False
        # a COPY between the group and the next item at its level or above puts fields under it
        # that the program's own text does not declare
        nxt = self.conn.execute("SELECT MIN(line) FROM field WHERE member_id=? AND line>? AND level<=? AND level<>88",
                                (frow["member_id"], frow["line"], frow["level"])).fetchone()[0]
        if self.conn.execute("SELECT 1 FROM copy_use WHERE member_id=? AND line>? AND line<? LIMIT 1",
                             (frow["member_id"], frow["line"], nxt if nxt is not None else 1 << 30)).fetchone():
            return None
        subs = [r[0].upper() for r in self.conn.execute(
            """WITH RECURSIVE sub(id) AS (SELECT id FROM field WHERE parent_id=? UNION ALL
                                          SELECT f.id FROM field f JOIN sub ON f.parent_id=sub.id)
               SELECT f.name FROM field f JOIN sub ON sub.id=f.id""", (frow["id"],))
                if r[0] and r[0].upper() != "FILLER"]
        if not subs:
            return None
        q = ",".join("?" * len(subs))
        # a CALL argument is recorded as a write too (BY REFERENCE): up, only a statement that sets it counts
        # passed on: a BY REFERENCE argument (the parser stores BY CONTENT as a read only) or a LINK COMMAREA
        where = (f"((stmt='CALL-USING' AND mode='write') OR (stmt='EXEC-CICS-LINK' AND {_CA_HANDOVER}))" if passed else
                 _SETS_HERE if up else "mode IN ('read','test','display')")
        return self.conn.execute(f"SELECT 1 FROM field_ref WHERE program_id=? AND UPPER(name) IN ({q}) AND {where} LIMIT 1",
                                 (pid, *subs)).fetchone() is not None

    def linkage_of(self, callee, target: str) -> List[str]:
        t = target.upper()
        if t != (callee["program_id"] or "").upper() and t != (callee["member_name"] or "").upper():
            r = self.conn.execute("SELECT linkage_using FROM program_alias WHERE program_id=? AND UPPER(alias)=?",
                                  (callee["id"], t)).fetchone()
            if r:
                return Q._jl(r["linkage_using"])
        return Q._jl(callee["linkage_using"])

    def sql_edges(self, node: _FNode, mode: str) -> List[_Edge]:
        out: List[_Edge] = []
        done = set()
        for r in self.conn.execute("SELECT * FROM sql_col_ref WHERE program_id=? AND mode=? AND UPPER(host_var)=? ORDER BY line",
                                   (node.pid, mode, node.name)):
            base = (r["tbl"] or "?").rpartition(".")[2].upper()
            key = (base, r["col"].upper())
            if key in done:
                continue
            done.add(key)
            cites = self.cite_sql(node.pid, r["line"], r["col"], r["host_var"])
            sep = " SET" if (r["stmt"] or "").upper() == "UPDATE" else ""
            label = f"EXEC SQL {r['stmt'] or ''} {r['tbl'] or '?'}{sep} {r['col']} (reconstructed)".replace("  ", " ")
            other = "read" if mode == "write" else "write"
            peers = self.conn.execute("""SELECT c.*, p.program_id AS pname FROM sql_col_ref c JOIN program p ON p.id=c.program_id
                                         WHERE c.mode=? AND UPPER(c.col)=? AND (UPPER(c.tbl)=? OR UPPER(c.tbl) LIKE ?)
                                         ORDER BY p.program_id, c.line""", (other, key[1], base, f"%.{base}")).fetchall() \
                if base != "?" else []
            if not peers:
                out.append(_Edge(4, f"DB2 {base}", r["line"], label, cites,
                                 end=END_NO_DB2_READER if mode == "write" else END_NO_DB2_WRITER))
                continue
            trows = [(p["pname"], p["host_var"] or "", self.cite_sql(p["program_id"], p["line"], p["col"], p["host_var"]))
                     for p in peers]
            for p in peers:
                self.touch(p["program_id"])
            word = "readers" if mode == "write" else "writers"
            out.append(_Edge(4, f"DB2 {base}", r["line"], label, cites,
                             table=f"via table {base}.{r['col']}: {len(peers)} static {word}, no ordering\n"
                                   + Q.table(["program", "host variable", "cite"], trows).rstrip("\n")))
        return out

    def file_edges(self, node: _FNode) -> List[_Edge]:
        """Program-owned record fields only: a copybook's items sit at the
        copybook's offsets, one byte off in a program with a prefix byte."""
        pid = node.pid
        fr = node.frow
        if fr is None:
            return []
        root = self.root_of(fr)
        if root is None:
            return []
        fd = self.conn.execute("SELECT * FROM file_decl WHERE program_id=? AND UPPER(fd_record)=?", (pid, root["name"].upper())).fetchone()
        if fd is None or not fd["assign_dd"]:
            return []
        up = self.o.up
        op = "READ" if up else "WRITE"
        io = self.conn.execute("SELECT line FROM io_op WHERE program_id=? AND target_kind='file' AND UPPER(target)=? AND op=? "
                               "ORDER BY line LIMIT 1", (pid, fd["select_name"].upper(), op)).fetchone()
        if io is None:
            return []
        lo, hi = fr["offset"], fr["offset"] + fr["length"]
        stmt_cite = self.cite_stmt(pid, io["line"], op, fd["select_name"] if up else root["name"])
        steps = [s for s in _Walker.steps_of(self, node.pname) if _Walker.dd_matches(fd["assign_dd"], s["dd_name"])]
        out = []
        arrow = "<-" if up else "->"
        for s in steps:
            where = f"{s['job_name'] or ('PROC ' + (s['proc_name'] or '?'))} {s['step_name']} DD {s['dd_name']}"
            label = f"{op} {root['name']} {arrow} {s['dsn_resolved']} ({where}, {s['mode']}/{s['mode_source']}) (reconstructed)"
            cites = stmt_cite + "; " + self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], True, s["step_name"])
            ds = _Node(pid, node.pname, root["id"], lo, hi, None, node.hop, kind="dataset",
                       data={"dsn": s["dsn_resolved"], "job_id": s["job_id"], "proc_id": s["proc_id"], "is_temp": s["is_temp"], "dd_id": s["dd_id"],
                             "gdg": s["gdg_rel"], "writer_pf": None})
            out.append(_Edge(1, node.pname, io["line"], label, cites, child=ds))
        if not steps:
            out.append(_Edge(1, node.pname, io["line"], f"{op} {root['name']} {arrow} DD {fd['assign_dd']}: no job step runs "
                             f"{node.pname} with this DD (reconstructed)", stmt_cite, end=END_NO_WRITER if up else END_NO_READER))
        return out

    def cobol_reader(self, prog, s, where: str, ds: _Node) -> List[_Leaf]:
        rpid = prog["id"]
        self.touch(rpid)
        fds = [f for f in self.conn.execute("SELECT * FROM file_decl WHERE program_id=?", (rpid,))
               if _Walker.dd_matches(f["assign_dd"], s["dd_name"])]
        dd_cite = self.cite_dd(s["member_name"], s["dd_line"], s["dd_name"], False, s["step_name"])
        if not fds:
            return [_Leaf([], f"{prog['program_id']} runs in {where} but declares no file on that DD", dd_cite,
                          end=END_NO_FIELD.format(lo=ds.lo + 1, hi=ds.hi))]
        leaves = []
        verb = "written by" if self.o.up else "read by"
        for fd in fds:
            if not fd["fd_record"]:
                continue
            root = self.conn.execute("SELECT * FROM field WHERE member_id=? AND UPPER(name)=? AND parent_id IS NULL",
                                     (prog["member_id"], fd["fd_record"].upper())).fetchone()
            if root is None:
                # the 01 itself comes from a copybook: field holds the program's own text only
                leaves.append(_Leaf([], f"{prog['program_id']} {fd['fd_record']} ({where}): record from a copybook", dd_cite,
                                    end=FALLBACK_COPY))
                continue
            # an elementary 01 is its own item; a group's items are its program-owned elementary children
            items = self.conn.execute("""WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT f.id FROM field f JOIN sub ON f.parent_id=sub.id)
                                         SELECT f.* FROM field f JOIN sub ON sub.id=f.id WHERE f.is_group=0
                                         AND f.offset<? AND f.offset+f.length>? ORDER BY f.offset""",
                                      (root["id"], ds.hi, ds.lo)).fetchall()
            if not items:
                if ds.lo >= root["offset"] + root["length"]:
                    leaves.append(_Leaf([], f"{prog['program_id']}.{root['name']} ({where}): record is {root['length']} bytes",
                                        dd_cite, end=END_NO_FIELD.format(lo=ds.lo + 1, hi=ds.hi)))
                else:
                    # bytes inside the record that no line of the program's own text names: a COPY's items
                    leaves.append(_Leaf([], f"{prog['program_id']}.{root['name']} bytes {ds.lo + 1}-{min(ds.hi, root['offset'] + root['length'])} "
                                            f"({where}): not in the program's own text", dd_cite, end=FALLBACK_COPY))
                continue
            for r in items:
                ch = _FNode(rpid, prog["program_id"], r["name"].upper(), r, ds.hop + 1)
                xlo, xhi = max(ds.lo, r["offset"]), min(ds.hi, r["offset"] + r["length"])
                leaves.append(_Leaf([], f"{verb} {prog['program_id']}.{r['name']} bytes {xlo + 1}-{xhi} ({where}) (reconstructed)",
                                    self.cite_def(r, rpid) + "; " + dd_cite, node=ch))
        return leaves

    def uses(self, node: _FNode) -> Optional[str]:
        texts, cites = [], []
        names = [node.name] + self.cond_names(node)
        q = ",".join("?" * len(names))
        for r in self.conn.execute(f"SELECT * FROM field_ref WHERE program_id=? AND UPPER(name) IN ({q}) AND mode IN ('test','display') "
                                   "ORDER BY line", (node.pid, *names)):
            m, ln, depth, via = Q.origin(self.conn, node.pid, r["line"])
            if not m or ln is None:
                continue
            tag = f"{m}:{ln}" + (f" (via COPY {via})" if depth else "")
            if r["mode"] == "display":
                text = "DISPLAY"
            else:
                raw = self.raw(m, ln)
                stmt = (r["stmt"] or "IF").upper()
                mm = re.search(r"(?<![\w-])" + re.escape(stmt) + r"(?![\w-])", raw, re.I)
                text = re.sub(r"\s+", " ", raw[mm.start():].strip() if mm else f"{stmt} {r['name']}").rstrip(".")[:TOKEN_MAX]
            if (text, tag) in zip(texts, cites):
                continue
            texts.append(text)
            cites.append(tag)
        return ("; ".join(texts) + "   " + "; ".join(cites)) if texts else None

    def sets(self, node: _FNode) -> List[str]:
        out = []
        for r in self.conn.execute("""SELECT literal, line FROM literal_ref WHERE program_id=? AND UPPER(field)=? AND context='move_to'
                                      ORDER BY line""", (node.pid, node.name)):
            reads = self.conn.execute("SELECT COUNT(*) FROM field_ref WHERE program_id=? AND line=? AND stmt='MOVE' AND mode='read'",
                                      (node.pid, r["line"])).fetchone()[0]
            if reads:
                continue
            out.append(f"also set here: MOVE '{r['literal']}' (reconstructed)   {self.cite_stmt(node.pid, r['line'], 'MOVE', node.name)}")
        return out

    def emit_node(self, e: _Edge, num: str, depth: int) -> None:
        node = e.child
        self.touch(node.pid)
        head = f"{e.label}   {e.cites}"
        key = (node.pid, node.name)
        prev = next((n for (n, via, hop) in self.visited[key]
                     if (via is None or via == node.via_call) and hop <= node.hop), None)
        if prev is not None:
            self.put(depth, self.fmt(num, depth, f"{head}   (shown as {prev})"))
            return
        self.visited[key].append((num, node.via_call, node.hop))
        self.count += 1
        n_decl = len(self.decls(node.pid, node.name))
        if n_decl > 1:
            # field_ref drops OF/IN: the statements on this name are every declaration's (guard 4)
            self.put(depth, self.fmt(num, depth, f"{head}   ({node.name} is declared {n_decl} times in {node.pname})"
                                     + self.end(END_AMBIGUOUS, num)))
            self.show_partial(node.pid, depth)
            return
        edges = self.edges(node)
        uses = self.uses(node)
        tail = ""
        if edges and node.hop >= self.o.hops:
            tail = f"   ({len(edges)} edge(s) not followed)" + self.end(END_HOPS.format(n=self.o.hops), num)
            edges = []
        elif not edges and not uses:
            # a group's own name unused is not its bytes unused: the fields under it (a callee's
            # parameter, a DFHCOMMAREA) are not placed in the value's bytes before the re-parse
            under = (self.used_under(node.pid, node.name, self.o.up)
                     if node.frow is not None and node.frow["is_group"] else False)
            tail = self.end(FALLBACK_COPY if node.frow is None else FALLBACK_GROUP if under is not False
                            else END_NO_USE.format(p=node.pname), num)
        self.put(depth, self.fmt(num, depth, head + tail))
        self.show_partial(node.pid, depth)
        for s in self.sets(node):
            self.put(depth, self.sub(depth, s))
        if uses:
            self.put(depth, self.sub(depth, uses + ("" if edges else self.end(FALLBACK_COPY if node.frow is None else END_TESTED,
                                                                              num))))
        self.children(node, edges, num, depth + 1)

    partial_note = _Walker.partial_note

    def show_partial(self, pid: int, depth: int) -> None:
        """[program partial: ...] once per program, under its first node (guard 23), as _Walker does:
        a program with a missing copybook never looks complete in the tree."""
        part = self.partial_note(pid) if pid not in self._partial_shown else None
        if part:
            self._partial_shown.add(pid)
            self.put(depth, f"- {part}" if depth == 0 else self.sub(depth, part))

    emit_dataset = _Walker.emit_dataset
    children = _Walker.children
    dataset_leaves = _Walker.dataset_leaves
    dds_on = _Walker.dds_on
    dds_on_step = _Walker.dds_on_step
    unindexed_sysin = _Walker.unindexed_sysin
    cite_iface = _Walker.cite_iface
    step_cards = _Walker.step_cards
    interfaces_on = _Walker.interfaces_on
    cite_card = _Report.cite_card

    def run(self, pid: int, name: str, frow, problem: Optional[str] = None, follow: bool = True) -> None:
        self.root_pid = pid
        self.touch(pid)
        p = self.prog(pid)
        node = _FNode(pid, p["program_id"], name.upper(), frow, 0)
        self.visited[(pid, node.name)].append(("root", None, 0))
        if frow is not None:
            self.put(0, f"- defined {self.cite_def(frow, pid)} ({_picstr(frow)}; offsets as this program's own text declares them)")
        self.show_partial(pid, 0)
        if not follow:
            # guard 4: candidates listed, none followed
            self.put(0, f"- {problem}" + self.end(END_AMBIGUOUS, "root"))
            return
        if frow is None:
            cb = self.conn.execute("""SELECT m.name, f.offset, f.length, f.pic FROM field f JOIN member m ON m.id=f.member_id
                                      WHERE UPPER(f.name)=? AND m.kind='copybook' LIMIT 1""", (node.name,)).fetchone()
            if cb:
                self.put(0, f"- defined in copybook {cb['name']} at the copybook's offset {cb['offset']} ({cb['pic'] or 'group'}); "
                            f"{FALLBACK_COPY}")
            else:
                self.put(0, f"- not declared in {p['program_id']}'s own text; {FALLBACK_COPY}")
        edges = self.edges(node)
        for s in self.sets(node):
            self.put(0, f"- {s}")
        uses = self.uses(node)
        # a copied item: its own text's statements are not all it takes part in (FALLBACK_COPY)
        if uses:
            self.put(0, f"- {uses}" + ("" if edges else self.end(FALLBACK_COPY if frow is None else END_TESTED, "root")))
        elif not edges and frow is None:
            self.put(0, f"- nothing more found in {p['program_id']}'s own statements" + self.end(FALLBACK_COPY, "root"))
        elif not edges:
            self.put(0, f"- {END_NO_USE.format(p=p['program_id'])}" + self.end(END_NO_USE.format(p=p["program_id"]), "root"))
        self.children(node, edges, "", 1)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def _split_name(name: str) -> Tuple[str, List[str]]:
    parts = re.split(r"\s+(?:OF|IN)\s+", name.strip().upper())
    return parts[0], parts[1:]


def _resolve_starts(conn: sqlite3.Connection, name: str, program: Optional[str]) -> Tuple[List[Tuple[int, sqlite3.Row, Optional[str]]], List[str]]:
    """(pid, pfield row, via88) per declaring program, and the problems: an
    ambiguous name lists its candidates and picks none (guard 4)."""
    base, quals = _split_name(name)
    problems: List[str] = []
    pids: Optional[List[int]] = None
    if program:
        progs = Q.programs_named(conn, program)
        if not progs:
            return [], [f"program {program.upper()} not in index"]
        pids = [p["id"] for p in progs]
    names = {base}
    for r in conn.execute("SELECT DISTINCT new_name FROM field_alias WHERE UPPER(orig_name)=?", (base,)):
        names.add(r[0].upper())
    for r in conn.execute("SELECT DISTINCT orig_name FROM field_alias WHERE UPPER(new_name)=?", (base,)):
        names.add(r[0].upper())
    q = ",".join("?" * len(names))
    rows = conn.execute(f"SELECT * FROM pfield WHERE UPPER(name) IN ({q}) ORDER BY program_id, id", tuple(names)).fetchall()
    via88: Dict[int, str] = {}
    if not rows:
        # an 88 name: its parent field, in the programs that declare it
        for c in conn.execute("""SELECT c.name AS cname, f.id AS fid, f.name, f.member_id, f.line FROM cond88 c JOIN field f ON f.id=c.field_id
                                 WHERE UPPER(c.name)=?""", (base,)):
            for r in conn.execute("""SELECT * FROM pfield WHERE UPPER(name)=? AND (copy_field_id=? OR (src_member=? AND src_line=?))
                                     ORDER BY program_id, id""", (c["name"].upper(), c["fid"], c["member_id"], c["line"])):
                rows.append(r)
                via88[r["id"]] = c["cname"]
    by_prog: Dict[int, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        if pids is not None and r["program_id"] not in pids:
            continue
        by_prog[r["program_id"]].append(r)
    out = []
    for pid, cands in sorted(by_prog.items(), key=lambda x: x[0]):
        if quals:
            cands = [c for c in cands if all(qq in (c["qualified"] or "").upper().split(".") for qq in quals)]
        pname = conn.execute("SELECT program_id FROM program WHERE id=?", (pid,)).fetchone()[0]
        if len(cands) == 1:
            out.append((pid, cands[0], via88.get(cands[0]["id"])))
        elif len(cands) > 1:
            problems.append(f"{base} is declared {len(cands)} times in {pname}: "
                            + ", ".join(f"{c['qualified']} (line {c['src_line']})" for c in cands)
                            + " - HUMAN MUST VERIFY which one; run `flow \"" + base + " OF <group>\"`")
        else:
            problems.append(f"{base} in {pname}: no declaration matches the qualifier {' OF '.join(quals)}")
    if not out and not problems:
        if program:
            pname = program.upper()
            miss = conn.execute("""SELECT u.detail FROM unresolved u JOIN program p ON p.member_id=u.member_id
                                   WHERE p.id IN ({}) AND u.kind IN ('expand','missing_copybook')""".format(",".join("?" * len(pids))),
                                tuple(pids)).fetchall() if pids else []
            if miss:
                x = re.search(r"COPY\s+(\S+)", miss[0]["detail"])
                problems.append(f"{base}: {END_MISSING.format(x=x.group(1) if x else '?')} in {pname}")
            else:
                pruned = conn.execute(f"""SELECT 1 FROM field f JOIN member m ON m.id=f.member_id
                                          JOIN copy_use cu ON UPPER(cu.copybook)=UPPER(m.name)
                                          JOIN program p ON p.member_id=cu.member_id
                                          WHERE UPPER(f.name)=? AND p.id IN ({",".join("?" * len(pids))}) LIMIT 1""",
                                      (base, *pids)).fetchone() if pids else None
                if pruned:
                    problems.append(f"{base} is in a copybook {pname} copies but nothing in {pname} references it (root stored pruned): "
                                    f"no flow starts there")
                else:
                    problems.append(f"{base} is NOT DEFINED in {pname}")
        else:
            problems.append(f"{base} is NOT DEFINED in any indexed program (check spelling, REPLACING renames, or try `field`)")
    return out, problems


def render(conn: sqlite3.Connection, name: str, program: Optional[str] = None, up: bool = False, hops: int = 3,
           width: int = 12, nodes: int = 200, derived: bool = False, show_all: bool = False,
           budget: Optional[int] = None, header: bool = True) -> str:
    opts = Opts(up=up, hops=hops, width=width, nodes=nodes, derived=derived, show_all=show_all, budget=budget, header=header)
    if not has_flow_tables(conn):
        return _render_fallback(conn, name, program, opts)
    starts, problems = _resolve_starts(conn, name, program)
    base, _q = _split_name(name)
    direction = "up" if up else "down"
    title_tail = ("upstream; where the value comes from" if up
                  else "downstream; copies only - --derived adds COMPUTE/STRING")
    bodies: List[str] = []
    members: Set[int] = set()
    total_nodes = 0
    shown = starts if (show_all or program) else starts[:width]
    for (pid, row, via88) in shown:
        w = _Walker(conn, opts)
        w.run(pid, row, via88)
        w.budget_cut()
        pname = w.prog(pid)["program_id"]
        body = [f"# Flow of {pname}.{base} ({title_tail})"]
        body.extend(t for _d, t in w.lines)
        body.extend(w.sections(pname))
        bodies.append("\n".join(body))
        members |= w.members
        total_nodes += w.count
    if len(starts) > len(shown):
        rest = starts[len(shown):]
        names = [conn.execute("SELECT program_id FROM program WHERE id=?", (pid,)).fetchone()[0] for (pid, _r, _v) in rest]
        bodies.append(f"... {len(rest)} more program(s) declare {base}: " + ", ".join(names) + " (--all, or --program P)")
    for pr in problems:
        bodies.append(f"# Flow of {base}\n- {pr}")
    text = "\n\n".join(bodies) + "\n"
    text += Q.unresolved_for(conn, sorted(members)) if members else "\n### Unresolved in scope\n_none in scope_\n"
    text += FOOTER + "\n"
    if header:
        label = f"{program.upper()}." if program else ""
        est = len(text) // 4
        text = (f"<!-- flow {label}{base}: ~{est} tokens ({len(text)} chars); {direction}, hops {hops}, {total_nodes} nodes; "
                f"{Q.index_header(conn)} -->\n") + text
    return text


def _render_fallback(conn: sqlite3.Connection, name: str, program: Optional[str], opts: Opts) -> str:
    base, _q = _split_name(name)
    if program:
        progs = Q.programs_named(conn, program)
        if not progs:
            return f"# Flow of {base}\n- program {program.upper()} not in index\n{FOOTER}\n"
    else:
        progs = conn.execute("""SELECT DISTINCT p.*, m.name AS member_name FROM program p JOIN member m ON m.id=p.member_id
                                WHERE p.id IN (SELECT program_id FROM field_ref WHERE UPPER(name)=?)
                                ORDER BY p.program_id""", (base,)).fetchall()
    direction = "up" if opts.up else "down"
    bodies: List[str] = []
    members: Set[int] = set()
    total = 0
    unpaired_total = 0
    shown = progs if (opts.show_all or program) else progs[:opts.width]
    for p in shown:
        w = _Fallback(conn, opts)
        frow, problem, follow = w.start(p["id"], name)
        w.run(p["id"], base, frow, problem, follow)
        w.budget_cut()
        body = [f"# Flow of {p['program_id']}.{base} ({'upstream' if opts.up else 'downstream'}; reconstructed)",
                f"- {FALLBACK_HEADER}"]
        body.extend(t for _d, t in w.lines)
        for pid in w.programs:
            w.move_pairs(pid)
        n = sum(w.unpaired.values())
        unpaired_total += n
        body.extend(w.sections(p["program_id"], [f"- {n} MOVE statement{'s' if n != 1 else ''} could not be paired "
                                                 f"(index predates data_flow)"]))
        bodies.append("\n".join(body))
        members |= w.members
        total += w.count
    if not progs:
        bodies.append(f"# Flow of {base}\n- {FALLBACK_HEADER}\n- {base} is referenced by no indexed program")
    if len(progs) > len(shown):
        bodies.append(f"... {len(progs) - len(shown)} more program(s) reference {base}: "
                      + ", ".join(p["program_id"] for p in progs[len(shown):]) + " (--all, or --program P)")
    text = "\n\n".join(bodies) + "\n"
    text += Q.unresolved_for(conn, sorted(members)) if members else "\n### Unresolved in scope\n_none in scope_\n"
    text += FOOTER + "\n"
    if opts.header:
        label = f"{program.upper()}." if program else ""
        text = (f"<!-- flow {label}{base}: ~{len(text) // 4} tokens ({len(text)} chars); {direction}, hops {opts.hops}, "
                f"{total} nodes, reconstructed; {Q.index_header(conn)} -->\n") + text
    return text


def lines_from(conn: sqlite3.Connection, pid: int, name: str, hops: int = 2, width: int = 6, nodes: int = 40) -> List[str]:
    """The hop lines of `flow NAME --program <pid>` for another report
    (`literal`, `diff`; `flow_from` before it): where the value goes, no
    header, no footer - the tree in a fenced block so its indentation
    survives markdown. A name that is ambiguous or not declared here says
    so instead of an empty answer."""
    opts = Opts(hops=hops, width=width, nodes=nodes, header=False)
    if not has_flow_tables(conn):
        w = _Fallback(conn, opts)
        frow, problem, follow = w.start(pid, name)
        if not follow:
            return [f"- {problem}"]
        w.run(pid, _split_name(name)[0], frow)
        lines = [t for d, t in w.lines if d > 0]
        return ([f"- {FALLBACK_HEADER}", "```"] + lines + ["```"]) if lines else []
    pname = conn.execute("SELECT program_id FROM program WHERE id=?", (pid,)).fetchone()
    if not pname:
        return []
    starts, problems = _resolve_starts(conn, name, pname[0])
    starts = [s for s in starts if s[0] == pid]
    if not starts:
        return [f"- {p}" for p in problems]
    w = _Walker(conn, opts)
    w.run(pid, starts[0][1], starts[0][2])
    lines = [t for d, t in w.lines if d > 0]
    return (["```"] + lines + ["```"]) if lines else []


_TARGET_STOP = {"ELSE", "END-IF", "END-EVALUATE", "END-PERFORM", "END-CALL", "END-STRING", "END-UNSTRING", "END-ADD",
                "END-COMPUTE", "IF", "MOVE", "PERFORM", "GO", "CALL", "DISPLAY", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY",
                "DIVIDE", "STRING", "UNSTRING", "SET", "READ", "WRITE", "EXEC", "EVALUATE", "WHEN", "INITIALIZE", "ACCEPT",
                "OPEN", "CLOSE", "GOBACK", "STOP", "CONTINUE", "EXIT", "INSPECT", "ROUNDED", "ON", "SIZE", "NOT", "DELIMITED",
                "WITH", "POINTER", "OVERFLOW", "DELIMITER", "COUNT", "RETURNING", "REMAINDER", "AT", "END", "INVALID",
                "UNTIL", "VARYING", "THRU", "THROUGH", "TALLYING", "REPLACING", "GIVING", "TO", "FROM", "INTO", "USING",
                "AND", "OR", "ALSO", "OTHER", "NEXT", "SENTENCE", "TIMES", "BEFORE", "AFTER"}
_SKIP_ARG = {"BY", "REFERENCE", "CONTENT", "VALUE", "LENGTH", "OF", "ADDRESS", "OMITTED", "IN"}


# a line whose first word is one of these starts a statement (or a clause that ends the one before)
_STMT_START = {"MOVE", "COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "STRING", "UNSTRING", "CALL", "IF", "ELSE",
               "EVALUATE", "WHEN", "PERFORM", "DISPLAY", "SET", "READ", "WRITE", "REWRITE", "DELETE", "START", "RETURN",
               "RELEASE", "SORT", "MERGE", "SEARCH", "EXEC", "INITIALIZE", "ACCEPT", "OPEN", "CLOSE", "GOBACK", "STOP",
               "CONTINUE", "EXIT", "INSPECT", "GO", "NEXT", "ENTRY", "AT", "INVALID", "NOT", "ON", "END-IF", "END-EVALUATE",
               "END-PERFORM", "END-CALL", "END-STRING", "END-UNSTRING", "END-ADD", "END-SUBTRACT", "END-MULTIPLY",
               "END-DIVIDE", "END-COMPUTE", "END-READ", "END-WRITE", "END-REWRITE", "END-SEARCH", "END-EXEC"}


def _code_of(s: str, fixed: bool) -> Optional[str]:
    """The code area of a source line; None for a comment line."""
    if fixed:
        if len(s) > 6 and s[6] in "*/":
            return None
        return s[7:72]
    return None if s.lstrip().startswith("*>") else s


def _statement_spans(lines: Sequence[str], changed: Sequence[int], fixed: bool) -> List[List[str]]:
    """Each changed line widened to its whole logical statement: back to
    the line whose first word starts a statement, on to the line that ends
    it (a period, or the next line starting a statement). A MOVE whose TO
    sits on the next line, a changed `TO X` line alone and a CALL's USING
    continuation all carry their targets this way."""
    code = [_code_of(x, fixed) for x in lines]

    def first(i: int) -> str:
        m = re.match(r"\s*([A-Za-z][\w-]*)", code[i] or "")
        return m.group(1).upper() if m else ""

    def ends(i: int) -> bool:
        return (code[i] or "").rstrip().endswith(".")

    def blank(i: int) -> bool:
        return code[i] is None or not code[i].strip()

    spans: List[Tuple[int, int]] = []
    for i in sorted(set(changed)):
        if i < 0 or i >= len(code) or blank(i):
            continue
        a = i
        for _k in range(20):
            if first(a) in _STMT_START:
                break
            j = a - 1
            while j >= 0 and blank(j):
                j -= 1
            if j < 0 or ends(j):
                break
            a = j
        b = i
        for _k in range(20):
            if ends(b):
                break
            j = b + 1
            while j < len(code) and blank(j):
                j += 1
            if j >= len(code) or first(j) in _STMT_START:
                break
            b = j
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    return [[code[k] for k in range(a, b + 1) if code[k] is not None] for (a, b) in spans]


def changed_targets(lines: Sequence[str], fixed: bool, changed: Optional[Sequence[int]] = None) -> List[str]:
    """The data items the changed lines set: MOVE / COMPUTE / STRING / CALL
    USING (and the arithmetic verbs) - the starts of `diff`'s flow. With
    `changed` (indexes into `lines`, the whole new text) each changed line
    is read as part of its whole statement; without it `lines` is the text."""
    if changed is None:
        groups = [[c for c in (_code_of(x, fixed) for x in lines) if c is not None]]
    else:
        groups = _statement_spans(lines, changed, fixed)
    out: List[str] = []
    for code in groups:
        out.extend(_targets_in(re.sub(r"'[^']*'|\"[^\"]*\"", "'L'", " ".join(code).upper())))
    seen: Set[str] = set()
    uniq = []
    for n in out:
        if n in seen or n in _TARGET_STOP or n in _SKIP_ARG:
            continue
        if "-" in n or (n.isalpha() and len(n) > 2):
            seen.add(n)
            uniq.append(n)
    return uniq[:8]


def _targets_in(text: str) -> List[str]:
    """Target names of the MOVE / COMPUTE / STRING / arithmetic / CALL
    statements in one upper-cased, literal-blanked piece of code."""
    out: List[str] = []

    def names_after(m_end: int, limit: int = 8, src: Optional[str] = None) -> List[str]:
        got = []
        skip_next = False
        depth = 0
        for tok in re.findall(r"[A-Z0-9][\w-]*|\(|\)|\.", (text if src is None else src)[m_end:]):
            if tok == ".":
                break
            # a name inside a subscript or ref-mod parenthesis is an index, never a target (guard 3)
            if tok == "(":
                depth += 1
                continue
            if tok == ")":
                depth = max(0, depth - 1)
                continue
            if depth:
                continue
            if tok == "ROUNDED" and src is not None:
                continue                    # ADD A TO B ROUNDED C ROUNDED: each target may carry it
            if tok in _TARGET_STOP and tok not in _SKIP_ARG:
                break
            if skip_next:
                skip_next = False
                continue
            if tok in ("OF", "IN"):
                skip_next = True
                continue
            if tok in _SKIP_ARG or tok[0].isdigit():
                continue
            got.append(tok)
            if len(got) >= limit:
                break
        return got

    # `\b` matches after a hyphen: WS-BILL-TO holds a `\bTO\b`, so every keyword is a whole COBOL word (as cobol.py's B)
    def kw(words: str) -> str:
        return r"(?<![\w-])(?:" + words + r")(?![\w-])"

    for m in re.finditer(kw("MOVE") + r".*?" + kw("TO") + r"\s", text):
        out.extend(names_after(m.end()))
    for m in re.finditer(kw("COMPUTE") + r"\s+([A-Z0-9][\w-]*)", text):
        out.append(m.group(1))
    for m in re.finditer(kw("STRING|UNSTRING") + r".*?" + kw("INTO") + r"\s", text):
        out.extend(names_after(m.end()))
    # the arithmetic verbs, one statement at a time: GIVING replaces the TO / FROM / BY / INTO operand as the
    # target (ADD A TO B GIVING C changes C, never B), and REMAINDER is one more target
    ends = re.compile(r"\.(?:\s|$)|" + kw("|".join(sorted(_STMT_START))))
    keyword = {"ADD": "TO", "SUBTRACT": "FROM", "MULTIPLY": "BY", "DIVIDE": "INTO"}
    for m in re.finditer(kw("ADD|SUBTRACT|MULTIPLY|DIVIDE"), text):
        e = ends.search(text, m.end())
        stmt = text[m.end():e.start() if e else len(text)]
        giving = re.search(kw("GIVING") + r"\s", stmt)
        into = giving or re.search(kw(keyword[m.group(0)]) + r"\s", stmt)
        if into:
            out.extend(names_after(into.end(), src=stmt))
        rem = re.search(kw("REMAINDER") + r"\s", stmt)
        if rem:
            out.extend(names_after(rem.end(), 1, src=stmt))
    for m in re.finditer(kw("CALL") + r".*?" + kw("USING") + r"\s", text):
        out.extend(names_after(m.end(), 12))
    return out
