"""
ims.py - DBD and PSB source (HLASM macro format) into segments, fields and
PCB lists.

The fact that matters most here is PCB ORDER. A COBOL program addresses IMS
databases by the POSITION of the PCB in its ENTRY 'DLITCBL' USING /
PROCEDURE DIVISION USING list, and that list is defined by the order of PCB
statements in the PSB - plus one trap:

  * In an MPP/BMP, and in a batch PSB generated with CMPAT=YES, the I/O PCB
    is inserted in FRONT of the coded PCBs. So the program's first USING
    argument is the I/O PCB and the first DB PCB is the SECOND argument.
  * In a DLI batch PSB with CMPAT=NO there is no I/O PCB, and the first USING
    argument is the first coded PCB.

Get that off by one and the analysis names the wrong database for every
DL/I call in the program, while looking completely consistent. The PSB alone
does not always settle it (a PSB with no TP PCB and no CMPAT could run in
either kind of region), so `io_pcb_first` is True/False when the PSB decides
it and None when the region type from the JCL has to decide it.

The AIB interface (AIBTDLI) addresses PCBs by NAME (AIBRSNM1 = the PCB's
label/NAME=), not position; `Pcb.name` is captured for that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from .reader import split_operands


# --------------------------------------------------------------------------
# HLASM macro statements
# --------------------------------------------------------------------------

@dataclass
class MacroStmt:
    label: str
    op: str
    operands: str
    start: int
    end: int


def parse_macros(text: str) -> List[MacroStmt]:
    """Statements from assembler macro source.

    Label in column 1 (optional), operation, operands. `*` in column 1 is a
    comment. A non-blank in column 72 continues the statement on the next
    line, whose text begins in column 16. Members that have been reformatted
    on the way to a desktop folder frequently lose column 72, so a trailing
    comma is also accepted as a continuation signal - in this grammar a
    trailing comma without continuation is an error anyway.
    """
    records = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[MacroStmt] = []
    i, n = 0, len(records)
    while i < n:
        rec = records[i].replace("\t", " ")
        if not rec.strip() or rec.startswith("*") or rec.startswith(".*"):
            i += 1
            continue
        start = i + 1
        body = rec[:71].rstrip()
        cont = len(rec) > 71 and rec[71] != " "
        while (cont or body.endswith(",")) and i + 1 < n:
            i += 1
            nxt = records[i].replace("\t", " ")
            if nxt.startswith("*"):
                continue
            nb = nxt[:71]
            cont = len(nxt) > 71 and nxt[71] != " "
            body = body + nb.strip()
        m = re.match(r"^(\S+)?\s+(\S+)\s*(.*)$", body)
        if m:
            out.append(MacroStmt(label=(m.group(1) or "").upper(),
                                 op=m.group(2).upper(),
                                 operands=(m.group(3) or "").strip(),
                                 start=start, end=i + 1))
        i += 1
    return out


def _kw(operands: str) -> Dict[str, str]:
    d: Dict[str, str] = {}
    for tok in split_operands(operands):
        if "=" in tok:
            k, _, v = tok.partition("=")
            d[k.strip().upper()] = v.strip()
    return d


def _list(v: Optional[str]) -> List[str]:
    """'(A,B,C)' -> ['A','B','C'];  'A' -> ['A'];  '((A,SNGL))' -> ['A','SNGL']."""
    if v is None:
        return []
    s = v.strip()
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    return [p.strip().strip("()").upper() for p in split_operands(s) if p.strip()]


def _int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"\d+", v)
    return int(m.group(0)) if m else None


# --------------------------------------------------------------------------
# DBD
# --------------------------------------------------------------------------

@dataclass
class Segment:
    name: str
    parent: Optional[str]
    bytes_max: Optional[int]
    bytes_min: Optional[int]
    seq_field: Optional[str]
    fields: List[Tuple[str, Optional[int], Optional[int], bool]] = dc_field(default_factory=list)
    line: int = 0


@dataclass
class DbdFacts:
    name: Optional[str] = None
    access: Optional[str] = None
    segments: List[Segment] = dc_field(default_factory=list)
    lchild: List[Tuple[str, str, str, int]] = dc_field(default_factory=list)   # (seg, dbd, in_seg, line)
    xdfld: List[Tuple[str, str, List[str], int]] = dc_field(default_factory=list)  # (name, seg, srch, line)
    warnings: List[str] = dc_field(default_factory=list)
    dd1: Optional[str] = None      # DATASET DD1= : for GSAM, the JCL DD the program writes/reads
    dd2: Optional[str] = None
    line: int = 0


def parse_dbd(text: str) -> DbdFacts:
    """The first DBD in the member (a member normally holds one)."""
    return parse_dbd_all(text)[0]


def parse_dbd_all(text: str) -> List[DbdFacts]:
    """One DbdFacts per DBD macro. A member holding two DBDs must not keep
    the last name with the first segments."""
    out: List[DbdFacts] = []
    f = DbdFacts()
    cur: Optional[Segment] = None
    for st in parse_macros(text):
        kw = _kw(st.operands)
        if st.op == "DBD":
            if f.name or f.segments:
                out.append(f)
                f = DbdFacts()
                cur = None
            f.name = (kw.get("NAME") or st.label or "").upper() or None
            acc = _list(kw.get("ACCESS"))
            f.access = acc[0] if acc else None
            f.line = st.start
        elif st.op == "DATASET":
            f.dd1 = (kw.get("DD1") or "").upper() or f.dd1
            f.dd2 = (kw.get("DD2") or "").upper() or f.dd2
        elif st.op == "SEGM":
            parent = _list(kw.get("PARENT"))
            b = _list(kw.get("BYTES"))
            cur = Segment(
                name=(kw.get("NAME") or st.label or "").upper(),
                parent=None if (not parent or parent[0] == "0") else parent[0],
                bytes_max=_int(b[0]) if b else None,
                bytes_min=_int(b[1]) if len(b) > 1 else None,
                seq_field=None, line=st.start)
            f.segments.append(cur)
            if cur.bytes_min is not None and cur.bytes_min != cur.bytes_max:
                f.warnings.append(f"L{st.start}: segment {cur.name} is VARIABLE length "
                                  f"({cur.bytes_min}-{cur.bytes_max})")
        elif st.op == "FIELD":
            nm = _list(kw.get("NAME"))
            if not nm:
                continue
            is_seq = len(nm) > 1 and nm[1] == "SEQ"
            entry = (nm[0], _int(kw.get("START")), _int(kw.get("BYTES")), is_seq)
            if cur is None:
                f.warnings.append(f"L{st.start}: FIELD {nm[0]} before any SEGM")
                continue
            cur.fields.append(entry)
            if is_seq:
                cur.seq_field = nm[0]
        elif st.op == "LCHILD":
            nm = _list(kw.get("NAME"))
            if nm and cur is not None:
                f.lchild.append((nm[0], nm[1] if len(nm) > 1 else f.name or "",
                                 cur.name, st.start))
        elif st.op == "XDFLD":
            f.xdfld.append(((kw.get("NAME") or "").upper(),
                            (kw.get("SEGMENT") or (cur.name if cur else "")).upper(),
                            _list(kw.get("SRCH")), st.start))
    out.append(f)
    for d in out:
        if not d.name:
            d.warnings.append("no DBD NAME= found")
    return out


# --------------------------------------------------------------------------
# PSB
# --------------------------------------------------------------------------

@dataclass
class Pcb:
    ordinal: int                   # 1-based order of PCB statements in the PSB
    pcb_type: str                  # DB|TP|GSAM
    dbd_name: Optional[str]
    procopt: Optional[str]
    keylen: Optional[int]
    name: Optional[str]            # label or NAME=/PCBNAME= - used by AIBTDLI
    sensegs: List[Tuple[str, Optional[str], Optional[str]]] = dc_field(default_factory=list)
    line: int = 0
    list_no: bool = False          # LIST=NO: not in the PCB address list the program receives
    procseq: Optional[str] = None  # PROCSEQ=: the database is accessed through this secondary index


@dataclass
class PsbFacts:
    name: Optional[str] = None
    lang: Optional[str] = None
    cmpat: Optional[str] = None
    io_pcb_first: Optional[bool] = None
    pcbs: List[Pcb] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)


def parse_psb(text: str, fallback_name: Optional[str] = None) -> PsbFacts:
    f = PsbFacts()
    cur: Optional[Pcb] = None
    ordinal = 0
    for st in parse_macros(text):
        kw = _kw(st.operands)
        if st.op == "PCB":
            ordinal += 1
            ptype = (kw.get("TYPE") or "DB").upper()
            cur = Pcb(ordinal=ordinal, pcb_type=ptype,
                      dbd_name=(kw.get("DBDNAME") or "").upper() or None,
                      procopt=(kw.get("PROCOPT") or "").upper() or None,
                      keylen=_int(kw.get("KEYLEN")),
                      name=(kw.get("PCBNAME") or kw.get("NAME") or st.label or "").upper() or None,
                      line=st.start,
                      list_no=(kw.get("LIST") or "").upper() == "NO",
                      procseq=(kw.get("PROCSEQ") or "").upper() or None)
            f.pcbs.append(cur)
        elif st.op == "SENSEG" and cur is not None:
            parent = _list(kw.get("PARENT"))
            cur.sensegs.append(((kw.get("NAME") or "").upper(),
                                None if (not parent or parent[0] == "0") else parent[0],
                                (kw.get("PROCOPT") or "").upper() or None))
        elif st.op == "PSBGEN":
            f.name = (kw.get("PSBNAME") or st.label or fallback_name or "").upper() or None
            f.lang = (kw.get("LANG") or "").upper() or None
            f.cmpat = (kw.get("CMPAT") or "").upper() or None

    if not f.name:
        f.name = (fallback_name or "").upper() or None
        f.warnings.append("PSBGEN PSBNAME= not found; name taken from member")

    has_tp = any(p.pcb_type == "TP" for p in f.pcbs)
    if f.cmpat == "YES" or has_tp:
        f.io_pcb_first = True
    elif f.cmpat == "NO":
        f.io_pcb_first = False
    else:
        f.io_pcb_first = None
        f.warnings.append(
            "no CMPAT= and no TP PCB: whether the program's first PCB argument is "
            "the I/O PCB depends on the region type (BMP/MPP: yes; DLI batch: no) - "
            "take it from the DFSRRC00 PARM in the JCL before mapping positions")
    return f


def program_positions(psb: PsbFacts, region_type: Optional[str] = None
                      ) -> List[Tuple[int, str, Optional[Pcb]]]:
    """(program_position, label, pcb) as the COBOL program sees the USING list.

    Position 1 is the I/O PCB when the PSB (or the region type) says so, and
    the coded PCBs follow in order. This is the map to use when turning a
    `CALL 'CBLTDLI' USING GU PCB-2 ...` into a database name.
    """
    io_first = psb.io_pcb_first
    if io_first is None and region_type:
        io_first = region_type.upper() in ("BMP", "MPP", "IFP", "JBP", "JMP")
    out: List[Tuple[int, str, Optional[Pcb]]] = []
    pos = 1
    if io_first:
        out.append((1, "IO-PCB", None))
        pos = 2
    for p in psb.pcbs:
        if p.list_no:
            continue                 # LIST=NO: addressed by name (AIB), not in the list
        out.append((pos, p.name or f"PCB{p.ordinal}", p))
        pos += 1
    return out
