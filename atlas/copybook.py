"""
copybook.py - parse COBOL data descriptions into a field tree with real byte
offsets and lengths.

Why offsets matter more than they look:

  * "If I change this field, what breaks" is only answerable if you know the
    field's position and size. A PIC change from 9(5) to 9(7) moves every
    field after it, changes the record length, and breaks every downstream
    fixed-width reader, every VSAM definition, every SORT control card that
    references a column position, and every non-mainframe consumer of the
    extract. None of that is visible from the field name alone.

  * COMP-3 is where naive tools go wrong. PIC S9(9)V99 COMP-3 is 11 digits
    packed into 6 bytes, not 11 characters. An LLM asked to build test data
    without this will produce a file that is the wrong length and whose
    numeric fields are garbage - and it will look completely plausible.

  * REDEFINES does not advance the offset. Miss that and every subsequent
    offset in the record is wrong.

  * OCCURS DEPENDING ON means the record has no single fixed length. That is a
    fact the analysis must state, not paper over.

Deliberately NOT handled, and flagged instead of guessed:
  * SYNCHRONIZED / SYNC alignment slack bytes. Rare in insurance copybooks but
    when present the offsets after it are wrong, so any 01 containing SYNC is
    marked unreliable rather than silently mis-computed.
  * Compiler options that change defaults (TRUNC, NSYMBOL).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

from .reader import (Line, LogicalLine, cobol_statements, join_cobol_continuations,
                     read_cobol_lines)

# --------------------------------------------------------------------------
# PIC / USAGE analysis
# --------------------------------------------------------------------------

_USAGE_ALIASES = {
    "COMP": "COMP", "COMPUTATIONAL": "COMP",
    "COMP-1": "COMP-1", "COMPUTATIONAL-1": "COMP-1",
    "COMP-2": "COMP-2", "COMPUTATIONAL-2": "COMP-2",
    "COMP-3": "COMP-3", "COMPUTATIONAL-3": "COMP-3", "PACKED-DECIMAL": "COMP-3",
    "COMP-4": "COMP", "COMPUTATIONAL-4": "COMP", "BINARY": "COMP",
    "COMP-5": "COMP-5", "COMPUTATIONAL-5": "COMP-5",
    "DISPLAY": "DISPLAY", "DISPLAY-1": "DISPLAY-1",
    "INDEX": "INDEX", "POINTER": "POINTER",
    "NATIONAL": "NATIONAL",
}

# CR / DB are two-character editing symbols (two bytes each), not C+R.
_PIC_TOKEN = re.compile(r"(CR|DB|[9AXZSVPBE0/,.\-+*$])(?:\((\d+)\))?", re.IGNORECASE)
# `05 PIC X(3).` / `05 COMP-3 ...` : an entry with no name is an anonymous FILLER.
_CLAUSE_WORDS = {"PIC", "PICTURE", "REDEFINES", "OCCURS", "VALUE", "VALUES", "USAGE", "COMP", "COMP-1", "COMP-2",
                 "COMP-3", "COMP-4", "COMP-5", "COMPUTATIONAL", "COMPUTATIONAL-1", "COMPUTATIONAL-2",
                 "COMPUTATIONAL-3", "COMPUTATIONAL-4", "COMPUTATIONAL-5", "BINARY", "PACKED-DECIMAL", "DISPLAY",
                 "DISPLAY-1", "POINTER", "INDEX", "NATIONAL", "SIGN", "SYNC", "SYNCHRONIZED", "JUST", "JUSTIFIED",
                 "BLANK", "EXTERNAL", "GLOBAL", "LEADING", "TRAILING"}
# Words whose OPERAND is a data-name, never a usage: `DEPENDING ON WS-COMP-CNT`.
_OPERAND_WORDS = {"REDEFINES", "DEPENDING", "ON", "INDEXED", "BY", "KEY", "IS", "ASCENDING", "DESCENDING"}
_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


@dataclass
class PicInfo:
    digits: int = 0
    scale: int = 0
    chars: int = 0
    signed: bool = False
    is_numeric: bool = False
    is_edited: bool = False


def parse_pic(pic: str) -> PicInfo:
    """Expand a PICTURE string into digit/char counts."""
    info = PicInfo()
    if not pic:
        return info
    p = pic.upper().replace(" ", "")
    after_v = False

    i = 0
    while i < len(p):
        m = _PIC_TOKEN.match(p, i)
        if not m:
            i += 1
            continue
        sym, cnt = m.group(1), m.group(2)
        n = int(cnt) if cnt else 1
        i = m.end()

        if sym == "S":
            info.signed = True
            info.is_numeric = True
        elif sym == "V":
            after_v = True
            info.is_numeric = True
        elif sym == "P":
            # Scaling position: contributes to scale, not to storage.
            info.is_numeric = True
            if after_v:
                info.scale += n
        elif sym == "9":
            info.is_numeric = True
            info.digits += n
            info.chars += n
            if after_v:
                info.scale += n
        elif sym in ("X", "A"):
            info.chars += n
        elif sym in ("Z", "0", "B", "/", ",", ".", "*", "$", "+", "-", "E"):
            info.is_edited = True
            info.chars += n
            if sym == "Z" or sym == "*":
                info.digits += n
        elif sym in ("CR", "DB"):   # credit / debit symbols: two bytes each
            info.is_edited = True
            info.chars += 2 * n
    return info


def storage_bytes(pic: str, usage: str, sign_separate: bool = False) -> int:
    """Bytes occupied on the record. This is the number that must be right."""
    u = (usage or "DISPLAY").upper()
    info = parse_pic(pic)

    if u == "COMP-3":
        # Packed decimal: one nibble per digit plus a sign nibble, rounded up.
        return (info.digits + 1 + 1) // 2 if info.digits else 0
    if u in ("COMP", "COMP-5"):
        d = info.digits
        if d <= 4:
            return 2
        if d <= 9:
            return 4
        return 8
    if u == "COMP-1":
        return 4
    if u == "COMP-2":
        return 8
    if u == "INDEX":
        return 4
    if u == "POINTER":
        return 4
    if u in ("NATIONAL", "DISPLAY-1"):
        return info.chars * 2
    # DISPLAY
    n = info.chars
    if info.signed and sign_separate:
        n += 1              # SIGN IS SEPARATE occupies its own byte
    return n


# --------------------------------------------------------------------------
# data description parsing
# --------------------------------------------------------------------------

@dataclass
class Field:
    level: int
    name: str
    line: int
    pic: Optional[str] = None
    usage: Optional[str] = None
    sign_clause: Optional[str] = None
    occurs: Optional[int] = None
    occurs_max: Optional[int] = None
    odo_on: Optional[str] = None
    redefines: Optional[str] = None
    value_lit: Optional[str] = None
    is_group: bool = False
    offset: int = 0
    length: int = 0
    digits: int = 0
    scale: int = 0
    qualified: str = ""
    parent: Optional["Field"] = None
    children: List["Field"] = dc_field(default_factory=list)
    conds: List[Tuple[str, List[str], int]] = dc_field(default_factory=list)  # 88s
    warnings: List[str] = dc_field(default_factory=list)


# A data name - or one as a copybook written for COPY REPLACING writes it, with a :TAG: of pseudo-text in it
# (`:PCB:-STATUS`, `WS-:SFX:-AMT`, `PM-:SFX:`) that the including program's `REPLACING ==:PCB:== BY ==POL==` turns
# into its own name. Before, a tagged name was no name: the PCB mask every IMS program copies had no field row, no 88
# and no layout (`layout` said NOT FOUND) while each program's own view was right (tools/synth/repro/F07, ROADMAP
# re-parse item 27). The copybook's rows keep the tagged names; field_alias ties each program's name to them.
DATA_NAME = r"(?:[A-Z0-9]|:[A-Z0-9\-_]+:)(?:[A-Z0-9\-_]|:[A-Z0-9\-_]+:)*"
_TAG = re.compile(r":[A-Z0-9\-_]+:", re.IGNORECASE)
_LEVEL = re.compile(r"^\s*(\d{1,2})\s+(" + DATA_NAME + r")(?![A-Z0-9\-_:])(.*)$", re.IGNORECASE)
_LEVEL_ANON = re.compile(r"^\s*(\d{1,2})\s*\.?\s*$")
# A picture string may itself contain periods (ZZZ,ZZ9.99 / 99.99.99), so the
# match runs to the next blank; the statement terminator was already stripped
# by the caller, and a stray trailing '.' is removed defensively below.
_PIC_CLAUSE = re.compile(r"\b(?:PIC|PICTURE)\s+(?:IS\s+)?(\S+)", re.IGNORECASE)
_USAGE_CLAUSE = re.compile(
    r"\b(?:USAGE\s+(?:IS\s+)?)?(COMPUTATIONAL-[12345]|COMPUTATIONAL|COMP-[12345]|COMP|"
    r"PACKED-DECIMAL|BINARY|DISPLAY-1|DISPLAY|INDEX|POINTER|NATIONAL)\b", re.IGNORECASE)
# TIMES is optional, and so is the blank before DEPENDING only when TIMES is there: `\s*(?:TIMES)?` took the blank
# and the DEPENDING clause after it was never tried - `OCCURS 1 TO 10 DEPENDING ON WS-CNT` was a fixed table of 10
# with no word of its variable length (found while writing ROADMAP re-parse item 27; LESSONS 215)
_OCCURS = re.compile(
    r"\bOCCURS\s+(?:(\d+)\s+TO\s+)?(\d+)(?:\s+TIMES)?"
    r"(?:\s+DEPENDING\s+(?:ON\s+)?(" + DATA_NAME + r"))?", re.IGNORECASE)
_REDEFINES = re.compile(r"\bREDEFINES\s+(" + DATA_NAME + r")", re.IGNORECASE)
_VALUE = re.compile(r"\bVALUES?\s+(?:IS\s+|ARE\s+)?(.+?)(?:\s*\.\s*$|$)", re.IGNORECASE)
_SIGN = re.compile(r"\bSIGN\s+(?:IS\s+)?(LEADING|TRAILING)(\s+SEPARATE(\s+CHARACTER)?)?",
                   re.IGNORECASE)
_SYNC = re.compile(r"\bSYNC(HRONIZED)?\b", re.IGNORECASE)
_COND_VALUES = re.compile(r"""('(?:[^']|'')*'|"(?:[^"]|"")*"|[^\s,]+)""")


def parse_data_division(logical: Sequence[LogicalLine]) -> Tuple[List[Field], List[str]]:
    """Parse data description entries into 01-rooted trees with offsets.

    Operates on period-terminated STATEMENTS, not raw lines. A single data
    description entry routinely spans several lines without a continuation
    indicator:

        05  PM-COVERAGE-TBL   OCCURS 1 TO 20 TIMES
                              DEPENDING ON PM-COV-COUNT.

    Parse that line-by-line and the DEPENDING ON clause is silently discarded,
    turning a variable-length record into a fixed-length one in the index -
    a wrong fact that then propagates into every downstream answer.
    """
    roots: List[Field] = []
    stack: List[Field] = []
    warnings: List[str] = []
    last_field: Optional[Field] = None

    for ll in cobol_statements(logical):
        txt = ll.text.strip().rstrip(".")
        if not txt:
            continue

        m = _LEVEL.match(txt)
        if not m:
            ma = _LEVEL_ANON.match(txt)
            if not ma:
                continue
            level, name, rest = int(ma.group(1)), "FILLER", ""      # `05.` - a bare group level
        else:
            level = int(m.group(1))
            name = m.group(2).upper()
            rest = m.group(3) or ""
            if name in _CLAUSE_WORDS:
                # `05  PIC X(3).` - no data-name at all: an anonymous FILLER,
                # NOT a field called PIC with no picture (and length 0).
                rest = txt[m.start(2):]
                name = "FILLER"

        # ---- 88 condition names attach to the field above them ------------
        if level == 88:
            if last_field is not None:
                vals = _parse_88_values(rest)
                last_field.conds.append((name, vals, ll.start))
            continue

        # 66 RENAMES / 77 standalone
        if level == 66:
            warnings.append(f"L{ll.start}: level-66 RENAMES not modelled ({name})")
            continue

        fld = Field(level=level, name=name, line=ll.start)
        _apply_clauses(fld, rest, warnings, ll.start)

        # ---- place in tree -----------------------------------------------
        if level in (1, 77):
            stack.clear()                 # 01 and 77 always start a new root
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            fld.parent = stack[-1]
            stack[-1].children.append(fld)
        else:
            roots.append(fld)
        stack.append(fld)
        last_field = fld

    # A copybook is very often a FRAGMENT: a run of 05s with no 01 above them,
    # because the 01 lives in the program that COPYs it. Left as separate roots
    # every field would get offset 0 and the record length would be nonsense.
    # Wrap them under a synthetic 01 so offsets accumulate the way they will
    # when the compiler expands the copy.
    roots = _wrap_fragment(roots, warnings)

    # Groups are items with children and no PIC.
    for r in roots:
        _mark_groups(r)
        _compute_offsets(r, 0, warnings)
        _qualify(r, "")
        _check_redefines(r, warnings)
    return roots, warnings


SYNTHETIC_ROOT = "*COPYBOOK-FRAGMENT*"


def _wrap_fragment(roots: List[Field], warnings: List[str]) -> List[Field]:
    if len(roots) <= 1:
        return roots
    top_levels = {r.level for r in roots}
    if top_levels == {1} or top_levels == {77}:
        return roots                      # proper 01s / standalone 77s
    if 1 in top_levels:
        # Mixed 01s and stray lower levels - ambiguous, do not guess.
        warnings.append("mixed 01 and lower-level items at top of member; "
                        "offsets for the stray items are unreliable")
        return roots

    level = min(top_levels)
    synth = Field(level=1, name=SYNTHETIC_ROOT, line=roots[0].line)
    for r in roots:
        r.parent = synth
        synth.children.append(r)
    warnings.append(
        f"member is a copybook FRAGMENT (top level {level:02d}, no 01) - offsets "
        f"computed under a synthetic 01; the real record may be longer if the "
        f"including program adds fields around the COPY")
    return [synth]


def _check_redefines(f: Field, warnings: List[str]) -> None:
    """A REDEFINES item must not be longer than what it redefines (below 01)."""
    by_name = {c.name: c for c in f.children}
    for c in f.children:
        if c.redefines:
            base = by_name.get(c.redefines.upper())
            if base is not None and c.level != 1 and c.length > base.length:
                warnings.append(
                    f"L{c.line}: {c.name} REDEFINES {base.name} but is larger "
                    f"({c.length} > {base.length} bytes) - this member will not "
                    f"compile as written, or the source is out of date")
    for c in f.children:
        _check_redefines(c, warnings)


def _usage_in(rest: str) -> Optional[str]:
    """The USAGE keyword as a WORD of the clause text - not `COMP` inside
    WS-COMP-CNT, not inside a VALUE literal, and not the operand of
    REDEFINES / DEPENDING ON / INDEXED BY (those are data-names)."""
    text = _TAG.sub(lambda m: "X" * len(m.group(0)), _LITERAL.sub(" ", rest))   # `REDEFINES :P:-COMP` names no usage
    skip_next = False
    for tok in re.findall(r"[A-Z0-9][A-Z0-9\-]*", text, re.IGNORECASE):
        up = tok.upper()
        if skip_next:
            skip_next = up in ("ON", "BY", "IS")      # DEPENDING ON x / INDEXED BY x / KEY IS x
            continue
        if up in _OPERAND_WORDS:
            skip_next = True
            continue
        if up in _USAGE_ALIASES:
            return _USAGE_ALIASES[up]
        if up in ("DISPLAY", "DISPLAY-1", "INDEX", "POINTER", "NATIONAL"):
            return up
    return None


def _apply_clauses(fld: Field, rest: str, warnings: List[str], line: int) -> None:
    m = _PIC_CLAUSE.search(rest)
    if m:
        fld.pic = m.group(1).rstrip(".")

    u = _usage_in(rest)
    if u:
        fld.usage = u

    m = _OCCURS.search(rest)
    if m:
        lo, hi, odo = m.group(1), m.group(2), m.group(3)
        fld.occurs = int(lo) if lo else int(hi)
        fld.occurs_max = int(hi)
        if odo:
            fld.odo_on = odo.upper()

    m = _REDEFINES.search(rest)
    if m:
        fld.redefines = m.group(1).upper()

    m = _SIGN.search(rest)
    if m:
        fld.sign_clause = m.group(0).upper()

    m = _VALUE.search(rest)
    if m:
        fld.value_lit = m.group(1).strip()

    if _SYNC.search(rest):
        fld.warnings.append("SYNCHRONIZED present - offsets after this item may be "
                            "wrong (alignment slack not modelled)")
        warnings.append(f"L{line}: SYNC on {fld.name} - offsets below it are unreliable")


def _mark_groups(f: Field) -> None:
    f.is_group = bool(f.children) and not f.pic
    for c in f.children:
        _mark_groups(c)


def _compute_offsets(f: Field, start: int, warnings: List[str],
                     usage: Optional[str] = None, sign: Optional[str] = None) -> int:
    """Set f.offset/f.length. Returns bytes consumed by f (one occurrence * occurs).

    `usage` / `sign` are inherited from the group: `01 WS-AMOUNTS COMP-3.`
    makes every child packed - an explicit clause on the child wins.
    """
    f.offset = start
    eff_usage = f.usage or usage
    eff_sign = f.sign_clause or sign

    if f.is_group:
        cursor = start
        redefine_base: Dict[str, int] = {}
        for c in f.children:
            if c.redefines:
                # A REDEFINES starts where the redefined item started and does
                # NOT advance the cursor.
                base = redefine_base.get(c.redefines.upper())
                if base is None:
                    sib = next((s for s in f.children
                                if s.name == c.redefines.upper()), None)
                    base = sib.offset if sib else cursor
                _compute_offsets(c, base, warnings, eff_usage, eff_sign)
                continue
            used = _compute_offsets(c, cursor, warnings, eff_usage, eff_sign)
            redefine_base[c.name] = cursor
            cursor += used
        f.length = cursor - start
    else:
        if usage and not f.usage:
            f.usage = usage                  # record the usage that applies
        sign_sep = bool(eff_sign and "SEPARATE" in eff_sign)
        f.length = storage_bytes(f.pic or "", f.usage or "DISPLAY", sign_sep)
        info = parse_pic(f.pic or "")
        f.digits, f.scale = info.digits, info.scale

    if f.odo_on:
        warnings.append(
            f"L{f.line}: {f.name} has OCCURS DEPENDING ON {f.odo_on} - the record "
            f"has a VARIABLE length; offsets after it are only valid for the "
            f"maximum ({f.occurs_max}) occurrence count")

    total = f.length * (f.occurs_max or 1)
    return total


def _qualify(f: Field, prefix: str) -> None:
    f.qualified = f"{prefix}.{f.name}" if prefix else f.name
    for c in f.children:
        _qualify(c, f.qualified)


def _parse_88_values(rest: str) -> List[str]:
    m = _VALUE.search(rest)
    if not m:
        return []
    body = m.group(1)
    body = re.sub(r"\bTHRU\b|\bTHROUGH\b", " THRU ", body, flags=re.IGNORECASE)
    return [v.strip() for v in _COND_VALUES.findall(body) if v.strip()]


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------

def parse_copybook(text: str, data: bytes = b"", enc: str = "utf-8"
                   ) -> Tuple[List[Field], List[str]]:
    lines, _ = read_cobol_lines(text, data=data, enc=enc)
    logical = join_cobol_continuations(lines)
    return parse_data_division(logical)


def flatten(roots: Sequence[Field]) -> List[Field]:
    out: List[Field] = []

    def walk(f: Field):
        out.append(f)
        for c in f.children:
            walk(c)

    for r in roots:
        walk(r)
    return out


def render_layout(root: Field) -> str:
    """Human-readable record map - the thing to paste into a design document."""
    rows = []
    rows.append(f"{'OFF':>6} {'LEN':>4} {'LVL':>3}  {'FIELD':<32} {'PIC':<18} {'USAGE':<9}")
    rows.append("-" * 82)

    def walk(f: Field, depth: int):
        ind = "  " * depth
        occ = f"  OCCURS {f.occurs_max}" if f.occurs_max else ""
        odo = f" DEPENDING ON {f.odo_on}" if f.odo_on else ""
        red = f"  REDEFINES {f.redefines}" if f.redefines else ""
        rows.append(
            f"{f.offset:>6} {f.length:>4} {f.level:>3}  "
            f"{ind + f.name:<32} {(f.pic or ''):<18} {(f.usage or ''):<9}"
            f"{occ}{odo}{red}")
        for name, vals, _ln in f.conds:
            rows.append(f"{'':>6} {'':>4}  88  {ind + '  ' + name:<32} "
                        f"VALUE {' '.join(vals)}")
        for c in f.children:
            walk(c, depth + 1)

    walk(root, 0)
    rows.append("-" * 82)
    rows.append(f"record length: {root.length} bytes")
    return "\n".join(rows)
