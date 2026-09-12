"""
screens.py - BMS (CICS) and MFS (IMS DC) screen definitions.

Screens matter for a value-domain change because the FIRST place a gender or
relationship code is validated is usually the online program reading the
screen field, and the screen definition is where the field's length, picture
and default value live.

BMS (DFHMSD / DFHMDI / DFHMDF): the map field label becomes a family of COBOL
symbolic names in the generated copybook - <name>L (length), <name>F (flag),
<name>A (attribute), <name>I (input data), <name>O (output data). A program
that validates gender therefore references GENDERI, never GENDER. The query
layer joins those symbolic names back to the map field.

MFS (FMT / DEV / DIV / DPAGE / DFLD ... MSG / SEG / MFLD): the MSG (MID for
input, MOD for output) defines the message layout as an ordered list of
MFLDs with lengths. The program's I/O area copybook mirrors that order, so the
BYTE OFFSET of each MFLD within the segment data (after the 4-byte LL ZZ
prefix) is what links a screen field to a copybook field. Offsets are
computed here; shops that name their MIDs/MODs with conventions such as
...FIP (input) / ...FOP (output) get the type inferred from the label when
TYPE= is missing, and the assumption is recorded.

Both formats are HLASM macro source: label in column 1, continuation in
column 72 (parse_macros handles a trailing comma too).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

from .ims import _int, _kw, _list, parse_macros
from .reader import split_operands


@dataclass
class ScreenField:
    name: Optional[str]           # DFHMDF label / DFLD name referenced by an MFLD; None = unnamed constant
    ordinal: int
    row: Optional[int] = None
    col: Optional[int] = None
    length: Optional[int] = None
    offset: Optional[int] = None  # MFS only: byte offset inside the segment data
    seg: Optional[int] = None     # MFS only: segment number within the message
    attrb: Optional[str] = None
    initial: Optional[str] = None # BMS INITIAL= / MFS default literal
    picin: Optional[str] = None
    picout: Optional[str] = None
    literal: Optional[str] = None # MFS constant MFLD ('TRANCODE ')
    line: int = 0


@dataclass
class Screen:
    kind: str                     # bms_map | mfs_fmt | mfs_msg
    name: str
    parent: Optional[str] = None  # BMS: mapset ; MFS msg: FMT (from SOR=)
    mode: Optional[str] = None    # BMS: IN|OUT|INOUT ; MFS msg: INPUT|OUTPUT ; MFS fmt: DIV TYPE
    next_msg: Optional[str] = None
    lang: Optional[str] = None
    size: Optional[str] = None
    line: int = 0
    fields: List[ScreenField] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)


def _unq(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = v.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].replace(s[0] * 2, s[0])
    return s


# --------------------------------------------------------------------------
# BMS
# --------------------------------------------------------------------------

def parse_bms(text: str) -> List[Screen]:
    screens: List[Screen] = []
    mapset = mode = lang = None
    cur: Optional[Screen] = None
    ordinal = 0
    for st in parse_macros(text):
        kw = _kw(st.operands)
        if st.op == "DFHMSD":
            if (kw.get("TYPE") or "").upper() == "FINAL":
                continue
            mapset = st.label or mapset
            mode = (kw.get("MODE") or mode or "").upper() or None
            lang = (kw.get("LANG") or lang or "").upper() or None
        elif st.op == "DFHMDI":
            cur = Screen(kind="bms_map", name=st.label or f"MAP{len(screens) + 1}", parent=mapset,
                         mode=mode, lang=lang, size=kw.get("SIZE"), line=st.start)
            screens.append(cur)
            ordinal = 0
        elif st.op == "DFHMDF":
            if cur is None:
                cur = Screen(kind="bms_map", name=mapset or "MAP1", parent=mapset, mode=mode,
                             lang=lang, line=st.start)
                screens.append(cur)
                cur.warnings.append("DFHMDF before any DFHMDI; synthetic map")
            ordinal += 1
            pos = _list(kw.get("POS"))
            cur.fields.append(ScreenField(
                name=st.label or None, ordinal=ordinal,
                row=_int(pos[0]) if pos else None, col=_int(pos[1]) if len(pos) > 1 else None,
                length=_int(kw.get("LENGTH")), attrb=kw.get("ATTRB"),
                initial=_unq(kw.get("INITIAL")), picin=_unq(kw.get("PICIN")),
                picout=_unq(kw.get("PICOUT")), line=st.start))
    return screens


BMS_SUFFIXES = ("I", "O", "L", "F", "A")


def bms_symbolic_names(field_name: str) -> List[str]:
    """GENDER -> GENDERI, GENDERO, GENDERL, GENDERF, GENDERA (the generated copybook)."""
    return [field_name.upper() + s for s in BMS_SUFFIXES]


# --------------------------------------------------------------------------
# MFS
# --------------------------------------------------------------------------

_NAME_HINTS = (("FIP", "INPUT"), ("MID", "INPUT"), ("IN", "INPUT"),
               ("FOP", "OUTPUT"), ("MOD", "OUTPUT"), ("OUT", "OUTPUT"))


def _msg_type_from_label(label: str) -> Optional[str]:
    up = (label or "").upper()
    for suffix, kind in _NAME_HINTS:
        if up.endswith(suffix):
            return kind
    return None


def _mfld_operands(operands: str) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
    """MFLD positional operand -> (dfld name, literal). Forms:
       'LITERAL'   NAME   (NAME,'default')   (NAME,SCA)   or nothing (filler)."""
    toks = split_operands(operands)
    kw = {k.upper(): v for k, v in (t.partition("=")[::2] for t in toks if "=" in t)}
    pos = [t for t in toks if "=" not in t]
    if not pos:
        return None, None, kw
    p = pos[0].strip()
    if p.startswith("("):
        inner = split_operands(p.strip("()"))
        name = inner[0].strip().upper() if inner else None
        lit = next((_unq(x) for x in inner[1:] if x.strip().startswith(("'", '"'))), None)
        return (name if name and not name.startswith(("'", '"')) else None), lit, kw
    if p.startswith(("'", '"')):
        return None, _unq(p), kw
    return p.upper(), None, kw


def parse_mfs(text: str) -> List[Screen]:
    screens: List[Screen] = []
    cur: Optional[Screen] = None
    ordinal = offset = seg = 0
    do_count: Optional[int] = None
    do_buf: List[ScreenField] = []

    def add_field(f: ScreenField) -> None:
        nonlocal offset, ordinal
        ordinal += 1
        f.ordinal = ordinal
        if cur.kind == "mfs_msg":
            f.offset = offset
            f.seg = seg
            offset += (f.length or 0)
        cur.fields.append(f)

    for st in parse_macros(text):
        kw = _kw(st.operands)
        op = st.op
        if op == "FMT":
            cur = Screen(kind="mfs_fmt", name=st.label or f"FMT{len(screens) + 1}", line=st.start)
            screens.append(cur)
            ordinal = 0
        elif op == "DEV" and cur is not None:
            cur.size = kw.get("TYPE")
        elif op == "DIV" and cur is not None:
            cur.mode = (kw.get("TYPE") or "").upper() or None
        elif op == "DFLD" and cur is not None:
            pos = _list(kw.get("POS"))
            f = ScreenField(name=st.label or None, ordinal=0,
                            row=_int(pos[0]) if pos else None, col=_int(pos[1]) if len(pos) > 1 else None,
                            length=_int(kw.get("LTH")), attrb=kw.get("ATTR"), line=st.start)
            toks = [t for t in split_operands(st.operands) if "=" not in t]
            if toks and toks[0].startswith(("'", '"')):
                f.literal = _unq(toks[0])          # DFLD 'GENDER:',POS=... is a screen label
            add_field(f)
        elif op == "MSG":
            mtype = (kw.get("TYPE") or "").upper() or None
            sor = _list(kw.get("SOR"))
            cur = Screen(kind="mfs_msg", name=st.label or f"MSG{len(screens) + 1}",
                         parent=sor[0] if sor else None, mode=mtype,
                         next_msg=(kw.get("NXT") or "").upper() or None, line=st.start)
            if mtype is None:
                inferred = _msg_type_from_label(cur.name)
                cur.mode = inferred
                cur.warnings.append(f"MSG {cur.name}: no TYPE=; {'inferred ' + inferred + ' from the label' if inferred else 'type UNKNOWN'}")
            screens.append(cur)
            ordinal = offset = seg = 0
        elif op == "SEG" and cur is not None:
            seg += 1
            offset = 0
        elif op == "DO" and cur is not None:
            toks = [t for t in split_operands(st.operands) if "=" not in t]
            do_count = _int(toks[0]) if toks else 1
            do_buf = []
        elif op == "ENDDO" and cur is not None:
            n = do_count or 1
            first = True
            for i in range(1, n + 1):
                for f in do_buf:
                    if first:
                        continue
                    add_field(ScreenField(name=f"{f.name}{i:02d}" if f.name else None, ordinal=0,
                                          length=f.length, attrb=f.attrb, initial=f.initial,
                                          literal=f.literal, line=f.line))
                first = False
            # rename the first occurrence to match MFS's 2-digit suffix convention
            for f in do_buf:
                if f.name and f in cur.fields:
                    f.name = f"{f.name}01"
            do_count, do_buf = None, []
        elif op == "MFLD" and cur is not None:
            name, lit, mk = _mfld_operands(st.operands)
            length = _int(mk.get("LTH")) or (len(lit) if lit else None)
            attr = mk.get("ATTR")
            if attr and attr.upper().startswith("YES"):
                length = (length or 0) + 2       # attribute bytes precede the data
            f = ScreenField(name=name, ordinal=0, length=length, attrb=attr,
                            initial=lit if name else None, literal=lit if not name else None,
                            line=st.start)
            add_field(f)
            if do_count is not None:
                do_buf.append(f)
        elif op in ("MSGEND", "FMTEND"):
            cur = None
    return screens
