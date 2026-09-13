"""
txn.py - transaction -> program routing from the CICS CSD and the IMS stage-1
system definition.

Without this, "which transaction runs CLMUPDT" and "is this online program
dead" are unanswerable: the routing is not in any COBOL, BMS or MFS member.

CICS: a DFHCSDUP extract / upload deck, in either form it comes in.
  DEFINE TRANSACTION(MEMB) GROUP(MEMGRP)
         PROGRAM(CONDLOGX) TWASIZE(0) STATUS(ENABLED)
  DEFINE PROGRAM(CONDLOGX) GROUP(MEMGRP) LANGUAGE(COBOL)
  DEFINE FILE(POLMAST) GROUP(MEMGRP) DSNAME(PROD.POLICY.MASTER.KSDS)
or the LIST report form
  TRANSACTION(MEMB)            GROUP(MEMGRP)
     PROGRAM     : CONDLOGX
FILE definitions matter too: EXEC CICS READ FILE('POLMAST') names the FCT
entry, and only the CSD says which VSAM dataset that is.

IMS: stage-1 SYSGEN macros (HLASM format, continuation in column 72).
  APPLCTN PSB=MEMBRVAL,PGMTYPE=TP,SCHDTYP=PARALLEL
  TRANSACT CODE=MEMB,PRTY=(7,10,2),MSGTYPE=(SNGLSEG,RESPONSE,1)
  DATABASE DBD=POLDBD,ACCESS=UP
A TRANSACT belongs to the APPLCTN above it. The load module name of an IMS TP
program is, by convention, the PSB name - recorded as an assumption, because
APPLCTN GPSB= (generated PSB) names the program directly instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from .ims import _kw, _list, parse_macros


@dataclass
class TxnDef:
    tran_code: str
    system: str                 # cics | ims_dc
    program: Optional[str]
    psb: Optional[str] = None
    group: Optional[str] = None
    detail: Optional[str] = None
    line: int = 0


@dataclass
class CicsProgram:
    name: str
    group: Optional[str]
    language: Optional[str]
    line: int


@dataclass
class CicsFile:
    name: str
    dsname: Optional[str]
    group: Optional[str]
    line: int


@dataclass
class RoutingFacts:
    transactions: List[TxnDef] = dc_field(default_factory=list)
    programs: List[CicsProgram] = dc_field(default_factory=list)
    files: List[CicsFile] = dc_field(default_factory=list)
    databases: List[Tuple[str, str, int]] = dc_field(default_factory=list)   # (dbd, access, line)
    warnings: List[str] = dc_field(default_factory=list)
    # every CSD block: (type, name, attrs without the _ keys, line) - TDQUEUE,
    # DB2ENTRY/DB2TRAN, URIMAP, WEBSERVICE... are facts too
    resources: List[Tuple[str, str, Dict[str, str], int]] = dc_field(default_factory=list)


# --------------------------------------------------------------------------
# CICS CSD
# --------------------------------------------------------------------------

_CSD_VERBS = ("DEFINE", "ALTER", "USERDEFINE", "ADD", "DELETE", "REMOVE", "LIST", "EXTRACT",
              "UPGRADE", "INITIALIZE", "MIGRATE", "SERVICE", "VERIFY", "COPY", "APPEND", "SCAN")
_RES_TYPES = ("TRANSACTION", "PROGRAM", "FILE", "MAPSET", "TDQUEUE", "TSMODEL", "DB2ENTRY",
              "DB2TRAN", "URIMAP", "PIPELINE", "WEBSERVICE", "CONNECTION", "SESSIONS",
              "TERMINAL", "TYPETERM", "PROFILE", "PARTITIONSET", "JOURNALMODEL", "ENQMODEL",
              "DOCTEMPLATE", "TCPIPSERVICE", "LIBRARY", "BUNDLE", "ATOMSERVICE")
# `DEFINE TRANSACTION (MEMX)` with a blank before the parenthesis is accepted too.
_ATTR = re.compile(r"\b([A-Z][A-Z0-9]*)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", re.I)
_REPORT_HEAD = re.compile(r"^\s*(" + "|".join(_RES_TYPES) + r")\(([^)]+)\)", re.I)
_REPORT_ATTR = re.compile(r"^\s*([A-Z][A-Z0-9]*)\s*:\s*(.*?)\s*$", re.I)


def parse_csd(text: str) -> RoutingFacts:
    f = RoutingFacts()
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: List[Tuple[int, Dict[str, str]]] = []   # (line, attrs incl. _TYPE/_NAME)
    cur: Optional[Dict[str, str]] = None
    cur_line = 0
    buf: List[str] = []

    def flush_define():
        nonlocal buf, cur, cur_line
        if buf:
            joined = " ".join(b.strip() for b in buf)
            attrs: Dict[str, str] = {}
            first = True
            for m in _ATTR.finditer(joined):
                k, v = m.group(1).upper(), m.group(2).strip()
                if first and k in _RES_TYPES:
                    attrs["_TYPE"], attrs["_NAME"] = k, v.upper()
                    first = False
                elif first and k in ("DEFINE",):
                    continue
                else:
                    attrs.setdefault(k, v)
                    first = False
            if "_TYPE" in attrs:
                blocks.append((cur_line, attrs))
            buf = []

    for i, raw in enumerate(lines, 1):
        s = raw[:72].rstrip()          # DFHCSDUP decks are card images
        if not s.strip() or s.lstrip().startswith("*"):
            continue
        head = s.strip().split()[0].upper()
        if head in _CSD_VERBS:
            flush_define()
            if cur is not None:
                blocks.append((cur_line, cur))
                cur = None
            if head in ("DEFINE", "ALTER", "USERDEFINE"):
                buf = [s.strip()[len(head):]]
                cur_line = i
            continue
        if buf:                        # continuation of a DEFINE
            buf.append(s)
            continue
        mh = _REPORT_HEAD.match(s)
        if mh:                         # LIST report form
            if cur is not None:
                blocks.append((cur_line, cur))
            cur = {"_TYPE": mh.group(1).upper(), "_NAME": mh.group(2).strip().upper()}
            cur_line = i
            for m in _ATTR.finditer(s[mh.end():]):
                cur.setdefault(m.group(1).upper(), m.group(2).strip())
            continue
        if cur is not None:
            ma = _REPORT_ATTR.match(s)
            if ma:
                cur.setdefault(ma.group(1).upper(), ma.group(2))
    flush_define()
    if cur is not None:
        blocks.append((cur_line, cur))

    for line, a in blocks:
        t, name, grp = a["_TYPE"], a["_NAME"], (a.get("GROUP") or "").upper() or None
        f.resources.append((t, name, {k: v for k, v in a.items() if not k.startswith("_")}, line))
        if t == "TRANSACTION":
            prog = (a.get("PROGRAM") or "").upper() or None
            if not prog:
                f.warnings.append(f"L{line}: TRANSACTION {name} has no PROGRAM (remote/dynamic?)")
            f.transactions.append(TxnDef(tran_code=name, system="cics", program=prog, group=grp,
                                         detail=("REMOTESYSTEM " + a["REMOTESYSTEM"]) if a.get("REMOTESYSTEM") else None,
                                         line=line))
        elif t == "PROGRAM":
            f.programs.append(CicsProgram(name=name, group=grp, language=(a.get("LANGUAGE") or "").upper() or None,
                                          line=line))
        elif t == "FILE":
            f.files.append(CicsFile(name=name, dsname=(a.get("DSNAME") or "").upper() or None, group=grp, line=line))
    if not blocks:
        f.warnings.append("no DEFINE/LIST resource blocks recognised")
    return f


# --------------------------------------------------------------------------
# IMS stage-1
# --------------------------------------------------------------------------

def parse_imsgen(text: str) -> RoutingFacts:
    f = RoutingFacts()
    cur_psb: Optional[str] = None
    cur_prog: Optional[str] = None
    cur_type: Optional[str] = None
    for st in parse_macros(text):
        kw = _kw(st.operands)
        if st.op == "APPLCTN":
            psb = (kw.get("PSB") or "").upper() or None
            gpsb = (kw.get("GPSB") or "").upper() or None
            pt = _list(kw.get("PGMTYPE"))
            cur_type = pt[0] if pt else None
            if gpsb:
                cur_psb, cur_prog = None, gpsb
            else:
                cur_psb, cur_prog = psb, psb
            if cur_prog:
                f.programs.append(CicsProgram(name=cur_prog, group=None, language=(kw.get("LANG") or "").upper() or None,
                                              line=st.start))
        elif st.op in ("TRANSACT", "RTCODE"):
            codes = _list(kw.get("CODE"))
            if not codes:
                f.warnings.append(f"L{st.start}: {st.op} without CODE=")
                continue
            extra = []
            spa = _list(kw.get("SPA"))
            if spa:
                extra.append(f"conversational SPA {spa[0]}")
            if (kw.get("INQUIRY") or "").upper().startswith("YES"):
                extra.append("inquiry-only")
            mt = _list(kw.get("MSGTYPE"))
            if mt:
                extra.append("MSGTYPE " + "/".join(mt))
            for code in codes:
                f.transactions.append(TxnDef(
                    tran_code=code, system="ims_dc", program=cur_prog, psb=cur_psb,
                    detail=(f"{st.op} under APPLCTN {cur_psb or cur_prog or '?'}; PGMTYPE={cur_type or '?'}; "
                            f"program name assumed = PSB name" if cur_psb else f"{st.op} under APPLCTN GPSB {cur_prog}")
                           + ("; " + "; ".join(extra) if extra else ""),
                    line=st.start))
            if cur_prog is None:
                f.warnings.append(f"L{st.start}: {st.op} {codes[0]} before any APPLCTN")
        elif st.op == "DATABASE":
            dbd = (kw.get("DBD") or "").upper()
            if dbd:
                f.databases.append((dbd, (kw.get("ACCESS") or "").upper(), st.start))
    if not f.transactions and not f.programs:
        f.warnings.append("no APPLCTN/TRANSACT macros recognised")
    return f
