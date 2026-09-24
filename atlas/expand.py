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
# the text-name may be a literal - COPY 'NAME' is legal and some shops write every COPY that way
# (LESSONS 173): groups are (keyword, quote, name); the quote, if any, must close
_COPY_START = re.compile(B + r"(COPY|\+\+INCLUDE|-INC)\s+(['\"]?)([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\2", re.I)
_SQL_INCLUDE_START = re.compile(B + r"EXEC\s+SQL\s+INCLUDE\s+([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})", re.I)
_COPY_FULL = re.compile(
    B + r"COPY\s+(['\"]?)([A-Z0-9@#$][A-Z0-9@#$\-_]{0,9})\1(?:\s+(?:OF|IN)\s+(['\"]?)([A-Z0-9@#$\-_]+)\3)?"
    r"(?:\s+SUPPRESS)?(?:\s+REPLACING\s+(.*))?\s*\.?\s*$", re.I | re.S)


def find_copy_start(code: str, masked: str):
    """The COPY / ++INCLUDE / -INC statement in `code`, its name bare or in
    quotes. `masked` is `code` with its literals blanked: a bare name is found
    there (DISPLAY 'COPY FAILED' is text, not a statement); a quoted name is a
    literal and is blanked too, so it is looked for in `code` itself - and
    taken only when the keyword before it was not inside a literal."""
    for m in _COPY_START.finditer(code):
        if masked[m.start(1):m.end(1)].upper() != m.group(1).upper():
            continue                                                   # the keyword sits inside a literal
        if not m.group(2) and masked[m.start(3):m.end(3)].upper() != m.group(3).upper():
            continue                                                   # a bare "name" that is the inside of a literal
        return m
    return None
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
    # expanded lines of a copied 01/77 renamed by "01 X COPY Y." (OS/VS): the
    # entry carries the program's name, so it is the program's field
    renamed: List[int] = dc_field(default_factory=list)

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

# OS/VS COBOL "01 data-name COPY text." (also 77, FD, SD): the compiler copies the
# library text and puts data-name in place of the library's own 01 / 77 / FD / SD
# name. Group 1 is the level or FD/SD, group 2 the name; the text must end there
# (the COPY follows it, on this line or the next).
_OSVS_HEAD = re.compile(r"(?:^|\.\s)\s*(0?1|77|FD|SD)\s+([A-Z0-9@#$][A-Z0-9@#$\-_]*)\s*$", re.I)
_ENTRY_HEAD = re.compile(r"^(\s*)(\d{1,2}|FD|SD)(\s+)([A-Z0-9@#$][A-Z0-9@#$\-_]*)", re.I)


def _entry_kind(word: str) -> str:
    """'01'/'1' -> '01', '77', 'FD', 'SD'; anything else as written."""
    w = word.upper()
    return "01" if w in ("1", "01") else w


def _osvs_rename(sub: Expansion, kind: str, new_name: str, copybook: str,
                 aliases: List[Tuple[str, str, str, int]]) -> Optional[int]:
    """Rename the first entry of the copied text to new_name when it is at
    `kind`'s level: its line in `sub` (1-based). 0 when the text starts with
    something else (a fragment); None when it holds no entry at all."""
    for k, sl in enumerate(sub.lines):
        if sl.is_comment or not sl.code.strip():
            continue
        m = _ENTRY_HEAD.match(sl.code)
        if not m or _entry_kind(m.group(2)) != kind:
            return 0
        old = m.group(4).upper()
        if old != new_name.upper():
            sl.code = sl.code[:m.start(4)] + new_name + sl.code[m.end(4):]
            r = sub.run_at(k + 1)
            _mem, src_no, _d = sub.origin(k + 1)
            aliases.append(((r.via_copy if r and r.via_copy else copybook), old, new_name.upper(), src_no or 0))
        return k + 1
    return None


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
    renamed: List[int] = []

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

    last_live: Optional[int] = None      # index in `out` of the last program line emitted as code
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
        m = find_copy_start(code, masked)
        msql = _SQL_INCLUDE_START.search(masked)
        if not m and not msql:
            emit(ln, code, member_id, ln.no, depth)
            last_live = len(out) - 1
            i += 1
            continue

        # "01 X COPY Y." (OS/VS): the text before COPY in the same statement is
        # a level 01/77 or FD/SD and one name - on this line, or alone on the
        # last code line before it ("01 X" / "COPY Y.")
        osvs = None                      # (kind, program's name, index in out of a live "01 X" line)
        # Anything else before COPY on its line is a complete statement of the
        # program's own - "A-100-BEGIN SECTION.  COPY PROCBOOK.", a paragraph
        # name, "MOVE A TO B.", "01 X." - and the compiler keeps it: only the
        # COPY statement is replaced. It stays live on its own line number, the
        # COPY part blanked, so the line map is unchanged (LESSONS 178).
        prefix: Optional[str] = None
        if m and not msql and m.group(1).upper() == "COPY":
            before = code[:m.start(1)]
            if before.strip():
                h = _OSVS_HEAD.search(before)
                if h:
                    osvs = (_entry_kind(h.group(1)), h.group(2), None, before[:h.end(2)])
                else:
                    prefix = before.rstrip()
            elif last_live is not None:
                h = _OSVS_HEAD.search(out[last_live].code)
                if h:
                    osvs = (_entry_kind(h.group(1)), h.group(2), last_live, None)
        last_live = None

        # ---- gather the whole COPY statement (may span lines) --------------
        # from the COPY keyword: a period in the text before it ("SECTION.")
        # is not the statement's end
        j = i
        stmt = code.strip()
        if m and m.group(1).upper() == "COPY":
            stmt = code[m.start(1):].strip()
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
                name, lib, rep_text = mf.group(2).upper(), (mf.group(4) or None), (mf.group(5) or None)
            else:
                name, lib, rep_text = m.group(3).upper(), None, None

        # The COPY statement itself stays in the output as a comment line so
        # that line accounting for the including member is preserved. With a
        # live prefix the first line is the prefix (no extra line is invented);
        # the statement's continuation lines, if any, are the comment echo.
        first = i
        if prefix is not None:
            emit(ln, prefix, member_id, ln.no, depth)
            first = i + 1
        for k in range(first, j + 1):
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
        at = None
        if osvs:
            kind, pname, prev, head = osvs
            at = _osvs_rename(sub, kind, pname, name, aliases)
            if at:
                # the library's own 01 now carries the program's name and is the
                # definition (cited in the copybook); the program's "01 X" line goes
                if prev is not None:
                    pl = out[prev]
                    pl.indicator, pl.is_comment, pl.is_blank = "*", True, False
            else:
                # a fragment (starts at 05, or with clauses): the program's own entry
                # stays as code and the fragment hangs under it. Clauses (PIC ...,
                # an FD's RECORDING MODE) continue the entry, so no period then.
                first = next((sl.code for sl in sub.lines if not sl.is_comment and sl.code.strip()), "")
                period = "" if first and not re.match(r"\s*\d{1,2}\s", first) else "."
                if prev is not None:
                    out[prev].code = out[prev].code.rstrip() + period
                else:
                    emit(ln, head + period, member_id, ln.no, depth, indicator=" ")
        base = len(out)
        renamed.extend(base + x for x in sub.renamed)
        if at and kind in ("01", "77"):
            renamed.append(base + at)
        for r in sub.runs:
            runs.append(Run(exp_start=base + r.exp_start, exp_end=base + r.exp_end,
                            src_member=r.src_member, src_start=r.src_start,
                            depth=r.depth, via_copy=r.via_copy or name))
        for sl in sub.lines:
            out.append(Line(no=len(out) + 1, raw=sl.raw, code=sl.code, indicator=sl.indicator,
                            is_comment=sl.is_comment, is_continuation=sl.is_continuation,
                            is_debug=sl.is_debug, is_blank=sl.is_blank))
        i = j + 1

    return Expansion(lines=out, runs=runs, warnings=warnings, copies=copies, aliases=aliases, renamed=renamed)


def expanded_text(exp: Expansion) -> str:
    """Render expanded lines as fixed-format text (cols 8-72 = code)."""
    rows = []
    for ln in exp.lines:
        rows.append(f"{'':6}{ln.indicator}{ln.code}")
    return "\n".join(rows) + "\n"
