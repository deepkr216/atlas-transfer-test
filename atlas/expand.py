"""
expand.py - materialise a program with every COPY / EXEC SQL INCLUDE resolved
inline, honouring REPLACING, with a line map back to the original members.

Why the expanded source is a first-class artifact and not a convenience:

  * A field brought in by `COPY POLREC REPLACING ==:PFX:== BY ==WS==` is
    called WS-STAT-CD in this program and :PFX:-STAT-CD in the copybook. No
    grep of either the program or the copybook finds the connection. Only the
    expanded text contains the name the compiler actually used.
  * Procedure-division copybooks contain PERFORMs, CALLs and SQL that belong
    to every program that includes them. Attributing those to the copybook
    member instead of the program makes the call graph wrong for each one.
  * The compiler listing is the authoritative version of this file. Where a
    listing is available it should be indexed instead; this module exists for
    the (usual) case where it is not.

Every expanded line carries (source member, source line, depth) so a
citation always points at a real line in a real member.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Callable, List, Optional, Sequence, Tuple

from .reader import Line, find_terminator

B = r"(?<![A-Z0-9\-])"
E = r"(?![A-Z0-9\-])"
_COPY_START = re.compile(B + r"(COPY|\+\+INCLUDE|-INC)\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})", re.I)
_SQL_INCLUDE_START = re.compile(B + r"EXEC\s+SQL\s+INCLUDE\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})", re.I)
_COPY_FULL = re.compile(
    B + r"COPY\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})(?:\s+(?:OF|IN)\s+([A-Z0-9@#$\-_]+))?"
    r"(?:\s+SUPPRESS)?(?:\s+REPLACING\s+(.*))?\s*\.?\s*$", re.I | re.S)
_PSEUDO = re.compile(r"==(.*?)==", re.S)
_SYSTEM_INCLUDES = {"SQLCA", "SQLDA"}

MAX_DEPTH = 12

# resolver(copybook_name, library_hint) -> (member_id, lines, note) or None
Resolver = Callable[[str, Optional[str]], Optional[Tuple[int, List[Line], Optional[str]]]]


@dataclass
class Run:
    """A contiguous run of expanded lines that all come from one member."""
    exp_start: int          # 1-based expanded line number
    exp_end: int
    src_member: int
    src_start: int
    depth: int
    via_copy: Optional[str] = None


@dataclass
class Expansion:
    lines: List[Line]
    runs: List[Run]
    warnings: List[str] = dc_field(default_factory=list)
    copies: List[Tuple[str, Optional[str], Optional[str], int, Optional[int]]] = dc_field(default_factory=list)
    # (copybook, library, replacing_text, line_in_including_member, resolved_member_id)
    # REPLACING renamed a copybook field: (copybook, name as stored, name in this program, copybook line).
    # Without this, every reference to LK-POLICY-STATUS is invisible to `field PM-POLICY-STATUS`.
    aliases: List[Tuple[str, str, str, int]] = dc_field(default_factory=list)

    def run_at(self, exp_line: int) -> Optional["Run"]:
        """The run holding an expanded line - by bisection over the runs'
        starts (they are emitted in ascending order and do not overlap).
        A linear scan per field made a 3,000-COPY program quadratic (LESSONS 146)."""
        import bisect
        starts = getattr(self, "_starts", None)
        if starts is None or len(starts) != len(self.runs):
            starts = [r.exp_start for r in self.runs]
            object.__setattr__(self, "_starts", starts)
        k = bisect.bisect_right(starts, exp_line) - 1
        if k >= 0:
            r = self.runs[k]
            if r.exp_start <= exp_line <= r.exp_end:
                return r
        return None

    def origin(self, exp_line: int) -> Tuple[Optional[int], Optional[int], int]:
        """(src_member, src_line, depth) for an expanded line number."""
        r = self.run_at(exp_line)
        if r is not None:
            return r.src_member, r.src_start + (exp_line - r.exp_start), r.depth
        return None, None, 0


# --------------------------------------------------------------------------
# REPLACING
# --------------------------------------------------------------------------

def parse_replacing(text: str) -> List[Tuple[str, ...]]:
    """'==:PFX:== BY ==WS==, OLD BY NEW, LEADING ==PM-== BY ==LK-=='
    -> [(':PFX:', 'WS'), ('OLD', 'NEW'), ('PM-', 'LK-', 'LEADING')]"""
    pairs: List[Tuple[str, ...]] = []
    if not text:
        return pairs
    # Tokenise into pseudo-text or words, then pair around BY.
    toks = re.findall(r"==.*?==|'[^']*'|\"[^\"]*\"|[^\s,]+", text, re.S)
    i = 0
    mode = ""
    while i + 2 < len(toks):
        a, by, b = toks[i], toks[i + 1], toks[i + 2]
        if a.upper() in ("LEADING", "TRAILING"):
            mode = a.upper()
            i += 1
            continue
        if by.upper() == "BY":
            # ==PM-== BY ==LK-== is PSEUDO-TEXT: it matches the START of
            # PM-POLICY-NO (the prefix-rename idiom). A bare word matches a
            # whole word only. The distinction must survive the parse.
            src, src_pseudo = _strip_pseudo(a)
            dst, _d = _strip_pseudo(b)
            pairs.append((src, dst, mode or ("PSEUDO" if src_pseudo else "")) if (mode or src_pseudo) else (src, dst))
            mode = ""
            i += 3
        else:
            i += 1
    return pairs


def _strip_pseudo(t: str) -> Tuple[str, bool]:
    m = _PSEUDO.fullmatch(t.strip())
    return (m.group(1).strip(), True) if m else (t.strip(), False)


_LIT_MASK = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def _mask_literals(code: str) -> str:
    """Same length, alphanumeric literals blanked: a COPY word inside
    DISPLAY 'COPY FAILED' is text, not a statement."""
    return _LIT_MASK.sub(lambda m: " " * len(m.group(0)), code)


def apply_replacing(code: str, pairs: Sequence[Tuple[str, ...]]) -> str:
    out = code
    for pair in pairs:
        src, dst = pair[0], pair[1]
        mode = pair[2] if len(pair) > 2 else ""
        if not src:
            continue
        rep = dst.replace("\\", "\\\\")
        if mode == "LEADING":
            out = re.sub(B + re.escape(src) + r"(?=[A-Z0-9\-])", rep, out, flags=re.I)
        elif mode == "TRAILING":
            out = re.sub(r"(?<=[A-Z0-9\-])" + re.escape(src) + E, rep, out, flags=re.I)
        elif re.fullmatch(r"\d+", src):
            # `==01== BY ==05==` renumbers LEVELS: only the first word of an
            # entry, never the 01 inside PIC X(01) or OCCURS 01 TIMES.
            out = re.sub(r"^(\s*)" + re.escape(src) + E, r"\g<1>" + rep, out)
        elif mode == "PSEUDO" or not re.fullmatch(r"[A-Z0-9][A-Z0-9\-_]*", src, re.I):
            # Pseudo-text: character-string replacement (==PM-== renames the
            # prefix of PM-POLICY-NO), case-insensitive, literals excluded.
            masked = _mask_literals(out)
            pieces, last = [], 0
            for mm in re.finditer(re.escape(src), masked, flags=re.I):
                pieces.append(out[last:mm.start()])
                pieces.append(dst)
                last = mm.end()
            pieces.append(out[last:])
            out = "".join(pieces)
        else:
            out = re.sub(B + re.escape(src) + E, rep, out, flags=re.I)
    return out


_LEVEL_NAME = re.compile(r"^\s*\d{1,2}\s+([A-Z0-9][A-Z0-9\-_]*)", re.I)


# --------------------------------------------------------------------------
# expansion
# --------------------------------------------------------------------------

def expand(lines: Sequence[Line], member_id: int, resolver: Resolver,
           depth: int = 0, stack: Tuple[str, ...] = (),
           replacing: Sequence[Tuple[str, ...]] = ()) -> Expansion:
    out: List[Line] = []
    runs: List[Run] = []
    warnings: List[str] = []
    copies: List[Tuple[str, Optional[str], Optional[str], int, Optional[int]]] = []
    aliases: List[Tuple[str, str, str, int]] = []

    def emit(src_line: Line, code: str, src_member: int, src_no: int, d: int,
             indicator: Optional[str] = None, via: Optional[str] = None) -> None:
        no = len(out) + 1
        ind = indicator if indicator is not None else src_line.indicator
        ln = Line(no=no, raw=src_line.raw, code=code, indicator=ind,
                  is_comment=ind in ("*", "/"), is_continuation=ind == "-",
                  is_debug=ind in ("D", "d"), is_blank=not code.strip() and ind == " ")
        out.append(ln)
        if runs and runs[-1].src_member == src_member and runs[-1].depth == d \
                and runs[-1].src_start + (runs[-1].exp_end - runs[-1].exp_start) + 1 == src_no \
                and runs[-1].exp_end == no - 1:
            runs[-1].exp_end = no
        else:
            runs.append(Run(exp_start=no, exp_end=no, src_member=src_member,
                            src_start=src_no, depth=d, via_copy=via))

    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        code = apply_replacing(ln.code, replacing) if replacing else ln.code
        if replacing and code != ln.code and not ln.is_comment:
            mo, mn = _LEVEL_NAME.match(ln.code), _LEVEL_NAME.match(code)
            if mo and mn and mo.group(1).upper() != mn.group(1).upper():
                aliases.append((stack[-1] if stack else "", mo.group(1).upper(), mn.group(1).upper(), ln.no))

        if ln.is_comment or not code.strip():
            emit(ln, code, member_id, ln.no, depth)
            i += 1
            continue

        masked = _mask_literals(code)
        m = _COPY_START.search(masked)
        msql = _SQL_INCLUDE_START.search(masked)
        if not m and not msql:
            emit(ln, code, member_id, ln.no, depth)
            i += 1
            continue

        # ---- gather the whole COPY statement (may span lines) --------------
        j = i
        stmt = code.strip()
        if m and m.group(1).upper() == "COPY":
            while find_terminator(stmt, 0, at_line_end=True) < 0 and j + 1 < n:
                j += 1
                nxt = lines[j]
                if nxt.is_comment:
                    continue
                stmt += " " + (apply_replacing(nxt.code, replacing) if replacing else nxt.code).strip()
        elif msql:
            while "END-EXEC" not in stmt.upper() and j + 1 < n:
                j += 1
                stmt += " " + lines[j].code.strip()

        if msql:
            name, lib, rep_text = msql.group(1).upper(), None, None
        else:
            mf = _COPY_FULL.search(stmt)
            if mf:
                name, lib, rep_text = mf.group(1).upper(), (mf.group(2) or None), (mf.group(3) or None)
            else:
                name, lib, rep_text = m.group(2).upper(), None, None

        # The COPY statement itself stays in the output as a comment line so
        # that line accounting for the including member is preserved.
        for k in range(i, j + 1):
            emit(lines[k], lines[k].code, member_id, lines[k].no, depth, indicator="*")

        if name in _SYSTEM_INCLUDES and msql:
            copies.append((name, lib, rep_text, ln.no, None))
            i = j + 1
            continue

        if depth >= MAX_DEPTH or name in stack:
            warnings.append(f"L{ln.no}: COPY {name} skipped - "
                            + ("recursive" if name in stack else f"nesting deeper than {MAX_DEPTH}"))
            copies.append((name, lib, rep_text, ln.no, None))
            i = j + 1
            continue

        resolved = resolver(name, lib)
        if not resolved:
            warnings.append(f"L{ln.no}: COPY {name} NOT FOUND - fields/code from it are missing "
                            f"from this program's facts")
            copies.append((name, lib, rep_text, ln.no, None))
            i = j + 1
            continue

        cb_member, cb_lines, note = resolved
        if note:
            warnings.append(f"L{ln.no}: COPY {name}: {note}")
        copies.append((name, lib, rep_text, ln.no, cb_member))

        inner_pairs = list(replacing) + parse_replacing(rep_text or "")
        sub = expand(cb_lines, cb_member, resolver, depth + 1, stack + (name,), inner_pairs)
        warnings.extend(f"(in COPY {name}) {w}" for w in sub.warnings)
        copies.extend(sub.copies)
        aliases.extend(sub.aliases)
        base = len(out)
        for r in sub.runs:
            runs.append(Run(exp_start=base + r.exp_start, exp_end=base + r.exp_end,
                            src_member=r.src_member, src_start=r.src_start,
                            depth=r.depth, via_copy=r.via_copy or name))
        for sl in sub.lines:
            out.append(Line(no=len(out) + 1, raw=sl.raw, code=sl.code, indicator=sl.indicator,
                            is_comment=sl.is_comment, is_continuation=sl.is_continuation,
                            is_debug=sl.is_debug, is_blank=sl.is_blank))
        i = j + 1

    return Expansion(lines=out, runs=runs, warnings=warnings, copies=copies, aliases=aliases)


def expanded_text(exp: Expansion) -> str:
    """Render expanded lines as fixed-format text (cols 8-72 = code)."""
    rows = []
    for ln in exp.lines:
        rows.append(f"{'':6}{ln.indicator}{ln.code}")
    return "\n".join(rows) + "\n"
