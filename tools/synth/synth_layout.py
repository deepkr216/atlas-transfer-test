"""
synth_layout.py - data items, their byte layout, and their rendering as
fixed-format COBOL data description entries.

The byte rules here are the GENERATOR'S OWN, written from the COBOL standard
and the IBM Enterprise COBOL reference, never from atlas/copybook.py: the
ground truth must be an independent witness, so a wrong rule in the toolkit
shows up as a disagreement instead of being copied into the truth.

Rules applied (Enterprise COBOL, no SYNC anywhere in the generated estate):

  * DISPLAY: one byte per PICTURE symbol that occupies storage (X A 9 Z * B 0
    / . , + - $ and CR / DB as two); S V P take none; SIGN SEPARATE adds one.
  * COMP / COMP-4 / COMP-5 / BINARY: 1-4 digits 2 bytes, 5-9 4 bytes, 10-18
    8 bytes.
  * COMP-3 / PACKED-DECIMAL: (digits + 2) // 2 bytes (a sign nibble always).
  * COMP-1 4, COMP-2 8, POINTER / INDEX 4.
  * A group's length is the largest end of its subordinates; an item that
    REDEFINES a sibling starts at that sibling's offset and adds nothing
    (the generator never makes a redefining item longer than the original);
    the sibling after a REDEFINES follows the redefined item.
  * OCCURS n multiplies the item's length; OCCURS a TO b DEPENDING ON x is
    laid out at b (the maximum) and every item after it carries after_odo,
    meaning its offset is a maximum.
  * USAGE and SIGN are inherited from the group down to the elementary item.
  * 88-levels take no storage; they are attached to the item they follow.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

USAGE_WORDS = {
    "": "", "DISPLAY": "", "COMP": "COMP", "COMPUTATIONAL": "COMP", "BINARY": "COMP", "COMP-4": "COMP",
    "COMP-5": "COMP-5", "COMP-3": "COMP-3", "PACKED-DECIMAL": "COMP-3", "COMP-1": "COMP-1", "COMP-2": "COMP-2",
    "POINTER": "POINTER", "INDEX": "INDEX",
}


@dataclass
class Item:
    level: int
    name: str = ""                 # "" = anonymous (05  PIC X(3)); "FILLER" is a name too
    pic: str = ""
    usage: str = ""                # as written: COMP, COMP-3, COMP-5, COMP-1, COMP-2, POINTER, INDEX, ""
    occurs: int = 0                # maximum occurrences (0 = no OCCURS)
    occurs_min: int = -1           # >= 0 with DEPENDING ON
    odo: str = ""                  # DEPENDING ON name
    redefines: str = ""
    value: object = None           # str (a literal as written, quotes included, or ZEROS...) | list of str (88 values)
    sign_sep: str = ""             # "LEADING" / "TRAILING" (SIGN IS ... SEPARATE)
    conds: List[Tuple[str, List[str]]] = dc_field(default_factory=list)   # 88s: (name, [literals as written])
    conds_style: str = "VALUE"     # VALUE | VALUES ARE (render only)
    children: List["Item"] = dc_field(default_factory=list)
    comments: List[str] = dc_field(default_factory=list)                  # comment lines rendered before the entry
    # computed by layout()
    offset: int = 0
    length: int = 0
    digits: int = 0
    scale: int = 0
    after_odo: bool = False
    group: bool = False
    line: int = 0                  # member line of the entry's first record (set by the renderer)
    src: str = ""                  # copybook the entry came from ("" = the member's own)
    orig_name: str = ""            # the name before a COPY REPLACING rename ("" = unchanged)

    def clone(self) -> "Item":
        return copy.deepcopy(self)


_REPEAT = re.compile(r"([A-Z9ZB0/.,+\-$*])\((\d+)\)")


def expand_pic(pic: str) -> str:
    return _REPEAT.sub(lambda m: m.group(1) * int(m.group(2)), pic.upper().replace(" ", ""))


def pic_info(pic: str) -> Tuple[int, int, int, bool]:
    """(storage characters, digit positions, scale, numeric)."""
    if not pic:
        return 0, 0, 0, False
    p = expand_pic(pic)
    chars = 0
    digits = 0
    scale = 0
    after_v = False
    i = 0
    while i < len(p):
        two = p[i:i + 2]
        if two in ("CR", "DB"):
            chars += 2
            i += 2
            continue
        ch = p[i]
        if ch in ("S", "P"):
            pass
        elif ch == "V":
            after_v = True
        else:
            chars += 1
            if ch in "9Z*":
                digits += 1
                if after_v:
                    scale += 1
        i += 1
    numeric = bool(re.fullmatch(r"[S9VP]+", p)) or (digits > 0 and not any(c in p for c in "XA"))
    return chars, digits, scale, numeric


def storage_bytes(pic: str, usage: str, sign_sep: str = "") -> int:
    u = USAGE_WORDS.get(usage.upper(), usage.upper())
    chars, digits, _scale, _num = pic_info(pic)
    if u == "COMP-3":
        return (digits + 2) // 2 if digits else 0
    if u in ("COMP", "COMP-5"):
        if digits <= 4:
            return 2
        if digits <= 9:
            return 4
        return 8
    if u == "COMP-1":
        return 4
    if u == "COMP-2":
        return 8
    if u in ("POINTER", "INDEX"):
        return 4
    return chars + (1 if sign_sep else 0)


def layout(root: Item) -> None:
    """Set offset / length / digits / scale / after_odo / group on every item
    of one 01 or 77 tree. Offsets are 0-based within the root."""
    _walk(root, 0, "", "", False)


def _walk(item: Item, start: int, inh_usage: str, inh_sign: str, after_odo: bool) -> bool:
    """Returns True when the item's length is variable (an ODO inside it)."""
    item.offset = start
    item.after_odo = after_odo
    usage = item.usage or inh_usage
    sign = item.sign_sep or inh_sign
    variable = False
    if item.children:
        item.group = True
        cur = start
        end_max = start
        by_name: Dict[str, Item] = {}
        seen_variable = False
        for ch in item.children:
            if ch.redefines and ch.redefines in by_name:
                ch_start = by_name[ch.redefines].offset
            else:
                ch_start = cur
            ch_var = _walk(ch, ch_start, usage, sign, after_odo or seen_variable)
            total = ch.length * (ch.occurs or 1)
            end = ch_start + total
            if not ch.redefines:
                cur = end
                if ch_var or ch.odo:
                    seen_variable = True
            end_max = max(end_max, end)
            if ch.name:
                by_name[ch.name] = ch
        item.length = end_max - start
        variable = seen_variable
        item.digits = item.scale = 0
    else:
        item.group = False
        item.length = storage_bytes(item.pic, usage, sign)
        _c, item.digits, item.scale, _n = pic_info(item.pic)
    return variable or bool(item.odo)


def flatten(root: Item) -> List[Item]:
    out: List[Item] = []

    def rec(it: Item) -> None:
        out.append(it)
        for ch in it.children:
            rec(ch)
    rec(root)
    return out


def find(root: Item, name: str) -> Optional[Item]:
    for it in flatten(root):
        if it.name == name:
            return it
    return None


def leaf_names(root: Item) -> List[str]:
    return [it.name for it in flatten(root) if it.name and it.name != "FILLER" and not it.children]


# --------------------------------------------------------------------------
# COPY REPLACING, applied to the generator's own item trees
# --------------------------------------------------------------------------

@dataclass
class Replacing:
    """One REPLACING pair as the generator means it.

    kind: tag      - ==:TAG:== BY ==NEW==   (the :TAG: inside every name)
          leading  - LEADING ==OLD-== BY ==NEW-==
          name     - ==OLDNAME== BY ==NEWNAME== (a whole word, every occurrence)
          name_dot - ==OLDNAME.== BY ==NEWNAME.== (the 01 name with its period)
    """
    kind: str
    src: str
    dst: str

    def rename(self, name: str, is_root: bool = False) -> str:
        if not name or name == "FILLER":
            return name
        if self.kind == "tag":
            return name.replace(self.src, self.dst)
        if self.kind == "leading":
            return self.dst + name[len(self.src):] if name.startswith(self.src) else name
        if self.kind == "name":
            return self.dst if name == self.src else name
        if self.kind == "name_dot":
            return self.dst if (is_root and name == self.src) else name
        return name

    def text(self) -> str:
        if self.kind == "tag":
            return f"=={self.src}== BY =={self.dst}=="
        if self.kind == "leading":
            return f"LEADING =={self.src}== BY =={self.dst}=="
        if self.kind == "name":
            return f"=={self.src}== BY =={self.dst}=="
        return f"=={self.src}.== BY =={self.dst}.=="


def apply_replacing(root: Item, pairs: Sequence[Replacing]) -> Tuple[Item, List[Tuple[str, str]]]:
    """A renamed copy of the tree and the (orig, new) aliases."""
    new = root.clone()
    aliases: List[Tuple[str, str]] = []

    def rec(it: Item, is_root: bool) -> None:
        if it.name and it.name != "FILLER":
            nm = it.name
            for p in pairs:
                nm = p.rename(nm, is_root)
            if nm != it.name:
                aliases.append((it.name, nm))
                it.orig_name = it.name
                it.name = nm
        for k, (cname, vals) in enumerate(it.conds):
            nm = cname
            for p in pairs:
                nm = p.rename(nm, False)
            if nm != cname:
                aliases.append((cname, nm))
                it.conds[k] = (nm, vals)
        for ref in ("redefines", "odo"):
            v = getattr(it, ref)
            if v:
                nm = v
                for p in pairs:
                    nm = p.rename(nm, False)
                setattr(it, ref, nm)
        for ch in it.children:
            rec(ch, False)
    rec(new, True)
    return new, aliases


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

NAME_W = 22


class Emitter:
    """Fixed-format records: 72 columns, columns 1-6 blank (a sequence
    number may be written there at the end), column 7 the indicator."""

    def __init__(self) -> None:
        self.lines: List[str] = []

    @property
    def n(self) -> int:
        return len(self.lines)

    def raw(self, rec: str) -> int:
        if len(rec) > 72:
            raise ValueError(f"record longer than 72 columns: {rec!r}")
        self.lines.append(rec)
        return self.n

    def at(self, col: int, text: str) -> int:
        return self.raw(" " * (col - 1) + text)

    def a(self, text: str) -> int:
        return self.at(8, text)

    def b(self, text: str) -> int:
        return self.at(12, text)

    def comment(self, text: str = "") -> int:
        return self.raw(("      *" + text)[:72])

    def cont(self, text: str) -> int:
        return self.raw("      -    " + text)

    def blank(self) -> int:
        return self.raw("")

    def wrapped(self, col: int, cont_col: int, text: str) -> int:
        """A statement broken at blanks so every record ends by column 72;
        returns the line of its first record."""
        first = None
        words = text.split(" ")
        line = ""
        c = col
        for w in words:
            if not w:
                continue
            cand = w if not line else line + " " + w
            if c - 1 + len(cand) > 72:
                n = self.at(c, line)
                first = first or n
                line = w
                c = cont_col
            else:
                line = cand
        if line:
            n = self.at(c, line)
            first = first or n
        return first or self.n


def render_value(val: str) -> str:
    return val


def render_items(em: Emitter, root: Item, depth0: int = 0, style: str = "std") -> None:
    """Render one tree. `style` 'std' aligns the PIC column; 'osvs' writes
    the older shop's compact style (the OS/VS-era members)."""

    def rec(it: Item, depth: int) -> None:
        for c in it.comments:
            em.comment(c)
        col = 8 if it.level in (1, 77) else 12 + 4 * (depth - 1)
        col = min(col, 36)
        lead = " " * (col - 1)
        head = f"{it.level:02d}  {it.name}" if it.name else f"{it.level:02d}"
        clauses: List[str] = []
        if it.redefines:
            clauses.append(f"REDEFINES {it.redefines}")
        if it.pic:
            clauses.append(f"PIC {it.pic}")
        if it.usage:
            clauses.append(it.usage if style == "osvs" or it.usage in ("COMP", "COMP-3") else f"USAGE {it.usage}")
        if it.sign_sep:
            clauses.append(f"SIGN {it.sign_sep} SEPARATE")
        occ = None
        if it.occurs:
            if it.odo:
                occ = f"OCCURS {it.occurs_min} TO {it.occurs} TIMES"
            else:
                occ = f"OCCURS {it.occurs} TIMES"
        # first record: level, name padded, then as many clauses as fit
        base = lead + head
        if it.name and clauses + ([occ] if occ else []):
            base = lead + f"{it.level:02d}  {it.name.ljust(NAME_W)}"
        cur = base
        first_line = None
        cont_lead = " " * (min(len(base), 44))
        if isinstance(it.value, str) and it.value.startswith("'") and len(it.value) > 40:
            # a long literal: VALUE and the opening quote on the entry's line, the literal
            # continued with a hyphen in column 7 (the shop's report headings)
            for cl in clauses:
                cur = cur + " " + cl
            cur = cur.rstrip() + " VALUE "
            body = it.value[1:-1]
            room = 72 - len(cur) - 1
            if room < 12:
                first_line = em.raw(cur.rstrip())
                cur = cont_lead + "VALUE "
                room = 72 - len(cur) - 1
            if len(body) < room:
                n = em.raw(cur + "'" + body + "'.")
                first_line = first_line or n
            else:
                piece = body[:room]                                  # fills the record to column 72 exactly:
                n = em.raw(cur + "'" + piece)                       # the literal continues on the next record
                first_line = first_line or n
                body = body[room:]
                while body:
                    piece = body[:60]
                    body = body[60:]
                    em.cont("'" + piece + ("'." if not body else ""))
        else:
            allc = list(clauses)
            if occ:
                allc.append(occ)
                if it.odo:
                    allc.append(f"DEPENDING ON {it.odo}")
            if it.value is not None:
                allc.append(f"VALUE {it.value}")
            pieces = allc
            for k, cl in enumerate(pieces):
                tail = "." if k == len(pieces) - 1 else ""
                if len(cur) + 1 + len(cl) + len(tail) > 72:
                    n = em.raw(cur.rstrip())
                    first_line = first_line or n
                    cur = cont_lead + cl + tail
                else:
                    cur = cur + (" " if cur.strip() else "") + cl + tail
            if not pieces:
                cur = cur + "."
            n = em.raw(cur.rstrip())
            first_line = first_line or n
        it.line = first_line
        for cname, vals in it.conds:
            c88 = " " * (col + 3)
            kw = "VALUES ARE" if it.conds_style == "VALUES ARE" else "VALUE"
            head88 = f"{c88}88  {cname.ljust(NAME_W - 4)} {kw}"
            line = head88
            vcol = min(len(head88) + 1, 48)                       # the values of a wrapped 88 line up under the first
            for k, v in enumerate(vals):
                tail = "." if k == len(vals) - 1 else ""
                if len(line) + 1 + len(v) + len(tail) > 72:
                    em.raw(line)
                    line = " " * vcol + v + tail
                else:
                    line = line + " " + v + tail
            em.raw(line)
        for ch in it.children:
            rec(ch, depth + 1)

    rec(root, depth0)


def truth_rows(root: Item, section: str, root_name: Optional[str] = None,
               member: str = "", fd: str = "") -> List[list]:
    """[level, name, offset, length, pic, usage, occurs, odo, redefines, after_odo, root, section, src_member, line, orig_name]"""
    rows = []
    rn = root_name or root.name
    for it in flatten(root):
        rows.append([it.level, it.name or "", it.offset, it.length, it.pic, it.usage, it.occurs, it.odo,
                     it.redefines, 1 if it.after_odo else 0, rn, section, it.src or member, it.line,
                     it.orig_name, fd])
    return rows


TRUTH_FIELD_COLUMNS = ["level", "name", "offset", "length", "pic", "usage", "occurs", "odo", "redefines",
                       "after_odo", "root", "section", "src_member", "line", "orig_name", "fd"]
