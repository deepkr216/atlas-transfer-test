"""
reader.py - turn raw mainframe source files into clean, citable lines.

Nothing else in the toolkit reads a file directly. Everything goes through
here, because the single most common way a COBOL "analyser" produces wrong
answers is by treating a fixed-format member as plain text:

  * Columns 1-6 hold sequence numbers. Members downloaded from a PDS often
    still have them, and they are full of digits that look like data.
  * Column 7 is the indicator: '*' and '/' mean the line is a COMMENT (so any
    CALL/COPY/EXEC SQL on it is dead text, not a real edge), '-' means the
    line CONTINUES the previous one (so a literal or a statement is split
    across physical lines), and 'D' means a debugging line that only compiles
    under WITH DEBUGGING MODE.
  * Columns 73-80 are the identification area, ignored by the compiler, and
    frequently contain change-ticket stamps that look like identifiers.

A regex run over raw text will therefore happily report calls that are
commented out, miss calls whose target literal is split across a continuation,
and invent identifiers out of sequence numbers and change stamps. Every one of
those shows up later as a confident, wrong answer.

JCL has its own rules: statements start with '//', '//*' is a comment, column
72 (1-based) carries the continuation flag, and the significant text ends at
column 71.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field as dc_field
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

# Encodings tried in order. cp037 first only if the file looks like raw EBCDIC
# (see sniff_encoding); most desktop folder dumps have already been converted.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_EBCDIC_SPACE = 0x40  # EBCDIC blank; a real EBCDIC member is mostly 0x40


# --------------------------------------------------------------------------
# encoding / decoding
# --------------------------------------------------------------------------

def sniff_encoding(data: bytes) -> str:
    """Guess the encoding of a source member.

    Raw EBCDIC is easy to spot: it is dominated by 0x40 (blank) and contains
    very few ASCII printables. Everything else is treated as some flavour of
    ASCII/ANSI text.
    """
    if not data:
        return "utf-8"
    sample = data[:8192]
    space_ratio = sample.count(_EBCDIC_SPACE) / len(sample)
    # 0x40 is the EBCDIC blank and the ASCII '@': it says nothing either way,
    # so it counts for neither side (counted as ASCII, a genuine EBCDIC member
    # looked 60 % printable and was decoded as Windows text - LESSONS 146)
    ascii_printable = sum(1 for b in sample if (0x20 <= b <= 0x7E and b != 0x40) or b in (0x09, 0x0A, 0x0D))
    ascii_ratio = ascii_printable / (len(sample) - sample.count(_EBCDIC_SPACE) or 1)
    if space_ratio > 0.15 and ascii_ratio < 0.60:
        return "cp037"
    for enc in _TEXT_ENCODINGS:
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def decode_bytes(data: bytes, encoding: Optional[str] = None) -> Tuple[str, str]:
    """Decode member bytes to text. Returns (text, encoding_used)."""
    enc = encoding or sniff_encoding(data)
    try:
        return data.decode(enc), enc
    except (UnicodeDecodeError, LookupError):
        return data.decode("latin-1", errors="replace"), "latin-1"


def _split_records(text: str, data: bytes, enc: str) -> List[str]:
    """Split into records.

    Files unloaded from a PDS as fixed-block sometimes arrive with no line
    terminators at all - just 80-byte records concatenated. Detect that and
    split on width, otherwise split on newlines.
    """
    if "\n" in text or "\r" in text:
        return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    n = len(text)
    for width in (80, 133, 132, 121):
        if n >= width * 2 and n % width == 0:
            return [text[i:i + width] for i in range(0, n, width)]
    if n >= 160:
        # a stray trailing byte (an EOF mark, a lone CR) must not turn 5 MB of
        # 80-byte records into ONE record: cut at 80, the tail is the last one
        return [text[i:i + 80] for i in range(0, n, 80)]
    return [text]


_STUB_BLANKS = str.maketrans({"\x00": " ", "\x1a": " ", "\f": " "})
_STUB_DIGITS = " 0123456789"


def stub_count(text: str, data: bytes = b"", enc: str = "utf-8") -> int:
    """How many lines a STUB holds, or 0: a member whose every non-blank,
    non-comment record holds only digits and blanks in the columns the
    compiler reads - the indicator column 7 and the code area to column 72
    (ROADMAP re-parse item 23). `1234567` puts a digit in column 7, the
    indicator, which no compiler accepts; `12345678` puts one in column 8
    too, which the reader takes for code, and expanded into a program it
    runs the data entry before the COPY on into the PROCEDURE DIVISION and
    erases every paragraph (tools/synth/repro/F08). The compiler could
    compile neither text, so the program was compiled against another copy:
    a stub is never COBOL to expand.

    The sequence area (columns 1-6) and the identification area (73-80) are
    the compiler's to ignore, and so they are here: a record whose columns
    7-72 are blank is a BLANK line whatever those two areas hold - ISPF
    numbering puts a number on every blank line (NUM ON COBOL in 1-6, NUM
    ON STD in 73-80), and a retired copybook of comments and such numbered
    blank lines is `empty`, as it always was (LESSONS 205). The columns are
    the reader's: fixed or free format as reader.looks_fixed_format decides
    (free format: the whole record is code), the code area ending where
    reader.detect_code_end finds a shifted stamp, a tab four columns wide.
    A comment record is, in fixed format, '*' or '/' in column 7 - the
    reader's rule: a '*' in the sequence area (`*0000100`) leaves a digit
    in the indicator and one in column 8, which the reader takes for code,
    so such a record counts as numbers (LESSONS 206) - or a record whose
    first non-blank character is '*' with text other than digits where the
    compiler reads (a floating `*>` remark from column 1: never the text of
    a member); in free format '*' as the first non-blank character or '*'
    / '/' in column 7. NULs, an end-of-file mark and form feeds count as
    blanks. 0 for a member with no record holding a digit in those columns
    (blank or comments only: `empty`) and for one with any record holding
    anything else there. Records split as the reader splits them."""
    records = _split_records(text, data, enc)
    kept: List[str] = []
    for rec in records:
        rec = rec.replace("\t", "    ").translate(_STUB_BLANKS)
        if not rec.strip():
            continue
        starred = rec.lstrip().startswith("*") or (len(rec) > 6 and rec[6] in "*/")
        if not starred and rec[6:65].strip(_STUB_DIGITS):
            # text in columns 7-65, which the compiler reads in either format (detect_code_end never cuts before
            # column 65), on a record no rule reads as a comment: no stub - every real member stops here, at its
            # first code line, before the format is read
            return 0
        kept.append(rec)
    if not kept:
        return 0
    fixed = looks_fixed_format(records)
    code_end = detect_code_end(records) if fixed else None
    n = 0
    for rec in kept:
        if fixed and len(rec) > 6 and rec[6] in "*/":
            continue                     # the reader's comment: '*' or '/' in the indicator column
        if not fixed and (rec.lstrip().startswith("*") or (len(rec) > 6 and rec[6] in "*/")):
            continue
        area = rec[6:code_end] if fixed else rec
        if not area.strip():
            continue                     # a sequence number, a stamp - nothing the compiler reads: a blank line
        if area.strip(_STUB_DIGITS):
            if fixed and rec.lstrip().startswith("*"):
                continue                 # '*' first, words where the compiler reads: a remark, never the member's text
            return 0
        n += 1                           # digits where the compiler reads - `*0000100` too: the reader's code '0'
    return n


# --------------------------------------------------------------------------
# the Line record
# --------------------------------------------------------------------------

@dataclass
class Line:
    """One physical line of a member, with the fixed-format rules applied.

    `no`   is the 1-based physical line number. This is the number that appears
           in every citation, so it must always refer to the original member.
    `code` is the significant text only: sequence area, indicator and
           identification area stripped. Parsers see this and nothing else.
    """
    no: int
    raw: str
    code: str
    indicator: str = " "
    is_comment: bool = False
    is_continuation: bool = False
    is_debug: bool = False
    is_blank: bool = False

    @property
    def upper(self) -> str:
        return self.code.upper()


@dataclass
class LogicalLine:
    """A COBOL statement or clause assembled across continuations.

    `text`      is the joined, continuation-resolved text.
    `start`/`end` are physical line numbers, so a citation still points at real
                lines in the real member.
    `lines`     lists every contributing physical line number, which is what
                lets an impact report say "field referenced at 1420, 1421".
    """
    text: str
    start: int
    end: int
    lines: List[int] = dc_field(default_factory=list)
    # True when the entry began in Area A (cols 8-11). Division headers,
    # section headers, paragraph names and FD/01 entries live in Area A;
    # ordinary statements live in Area B. Without this flag a paragraph called
    # MOVE-TOTALS is indistinguishable from a MOVE statement, and the
    # paragraph list - and therefore the whole PERFORM graph - comes out wrong.
    area_a: bool = False
    # Physical line number for each character of `text`, when known. A single
    # statement can hold several verbs on different lines (IF ... MOVE ...
    # ELSE MOVE ... END-IF); this is what lets each MOVE be cited to ITS line
    # rather than to the line the IF started on.
    charmap: Optional[List[int]] = None
    # (SQL | CICS | DLI, the line of its EXEC keyword) when cobol_statements
    # closed this statement at an END-EXEC with no period after it outside the
    # PROCEDURE DIVISION - a missing period the parser reports, never a reason
    # to let the EXEC block swallow the entries and the division header after it
    no_period: Optional[Tuple[str, int]] = None

    def line_at(self, offset: int) -> int:
        if self.charmap and 0 <= offset < len(self.charmap):
            return self.charmap[offset]
        return self.start

    @property
    def upper(self) -> str:
        return self.text.upper()


# --------------------------------------------------------------------------
# format detection
# --------------------------------------------------------------------------

_FREE_FORMAT_HINTS = re.compile(
    r"^\s{0,6}(IDENTIFICATION|ID)\s+DIVISION", re.IGNORECASE | re.MULTILINE
)


def looks_fixed_format(records: Sequence[str]) -> bool:
    """Decide whether cols 1-6 / 7 / 73-80 rules apply.

    Heuristic, and deliberately biased toward 'fixed': in a shop like this,
    treating fixed source as free costs you comment detection (catastrophic),
    while treating free source as fixed costs you the first 6 characters of
    lines that are almost always indented anyway.
    """
    sample = [r for r in records[:400] if r.strip()]
    if not sample:
        return True

    seq_numeric = 0
    indicator_hits = 0
    long_lines = 0
    considered = 0

    for rec in sample:
        considered += 1
        head = rec[:6]
        if head.strip() and head.strip().isdigit():
            seq_numeric += 1
        if len(rec) >= 7 and rec[6] in "*/-D$":
            indicator_hits += 1
        if len(rec.rstrip()) > 80:
            long_lines += 1

    if considered == 0:
        return True
    # Sequence numbers present, or comments sitting exactly in column 7.
    if seq_numeric / considered > 0.5:
        return True
    if indicator_hits / considered > 0.02:
        return True
    # Free-format sources routinely exceed 80 columns; fixed ones rarely do.
    if long_lines / considered > 0.25:
        return False
    return True


# --------------------------------------------------------------------------
# COBOL reading
# --------------------------------------------------------------------------

def detect_code_end(records: Sequence[str]) -> int:
    """Find the column where significant text stops (0-based, exclusive).

    The compiler's rule is "columns 73-80 are the identification area", which
    assumes 80-column records. Real folder dumps rarely honour that: FTP with
    trailing-blank stripping, editors that trim, and conversions that drop a
    column all produce members where the change-ticket stamp sits at 72 or 71
    instead of 73.

    When that happens a hard-coded [7:72] slice leaks a fragment of the stamp
    onto the end of every line. That fragment then gets glued onto the last
    token of the statement - so a data description entry no longer ends in a
    period, entries run together, and the whole member parses as one giant
    field. Detect the margin empirically instead: if a stable tag block sits at
    the right edge of the widest records, cut there.
    """
    nb = [r.rstrip() for r in records if r.strip()]
    if not nb:
        return 72

    # Use the MODAL width, not the max. Comment banners and the occasional long
    # line are routinely a column or two wider than the body of the member, and
    # taking the max would pick a margin that fits the banner and leaks a
    # character of the change stamp onto every ordinary line.
    counts: dict = {}
    for r in nb:
        counts[len(r)] = counts.get(len(r), 0) + 1
    modal_w = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    if modal_w <= 72:
        return 72

    tails = [r[-8:] for r in nb if len(r) == modal_w]
    if len(tails) >= 5:
        prefixes: dict = {}
        for t in tails:
            p = t[:4].strip()
            if p:
                prefixes[p] = prefixes.get(p, 0) + 1
        if prefixes and max(prefixes.values()) / len(tails) >= 0.6:
            return max(min(72, modal_w - 8), 8)
    return 72


_DIRECTING = re.compile(r"^(?:EJECT|SKIP[123])\s*\.?\s*$|^TITLE\b")
_CBL_OPTS = re.compile(r"^(?:CBL|PROCESS)\s+\S")
_DIVISION_HDR = re.compile(r"^(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b")
_ID_COMMENT_ENTRY = re.compile(r"^(?:AUTHOR|INSTALLATION|DATE-WRITTEN|DATE-COMPILED|SECURITY|REMARKS)\b")


def read_cobol_lines(text: str, fixed: Optional[bool] = None,
                     data: bytes = b"", enc: str = "utf-8") -> Tuple[List[Line], bool]:
    """Return (lines, fixed_format_used) for a COBOL / copybook member."""
    records = _split_records(text, data, enc)
    if fixed is None:
        fixed = looks_fixed_format(records)
    code_end = detect_code_end(records) if fixed else None

    out: List[Line] = []
    in_id_comment = False        # inside AUTHOR. / REMARKS. ... (a comment-entry)
    in_sql = False               # inside EXEC SQL ... END-EXEC
    division = None
    for i, rec in enumerate(records, start=1):
        rec = rec.replace("\t", "    ")  # tabs in a column-sensitive format
        if fixed:
            indicator = rec[6] if len(rec) > 6 else " "
            body = rec[7:code_end] if len(rec) > 7 else ""
            # A line shorter than col 8 is blank as far as the compiler cares.
        else:
            indicator = " "
            body = rec
            stripped = rec.lstrip()
            if stripped.startswith("*>"):
                indicator = "*"
                body = stripped[2:]
            elif stripped[:1] in ("*", "/") and len(rec) - len(stripped) <= 6:
                indicator = stripped[0]
                body = stripped[1:]

        code = body.rstrip()
        up = code.strip().upper()
        # Compiler-directing lines are not statements: EJECT / SKIPn / TITLE
        # (and CBL / PROCESS options) would otherwise swallow the paragraph
        # header that follows them.
        if indicator == " " and (_DIRECTING.match(up) or _CBL_OPTS.match(rec.strip().upper())):
            indicator = "*"
        # SQL `--` comments live inside EXEC SQL ... END-EXEC. Once the lines
        # are joined into one statement their end-of-line is gone, so a
        # comment ending in a period (`-- get the policy.`) would terminate
        # the statement here and the SELECT would never be seen.
        if indicator == " " and in_sql:
            if code.lstrip().startswith("--"):
                indicator = "*"
            elif " --" in code:
                code = code.split(" --", 1)[0].rstrip()
        if indicator == " " and "EXEC" in up:
            # the words outside literals: `VALUE 'EXEC SQL'` opens no block - read as one, every later line holding
            # ' --' (a heading literal `' -- END -- '`) was cut there as an SQL comment, the literal left open ate
            # the rest of the program (LESSONS 217). A line without the word changes nothing: not masked at all
            words = code_outside_literals(up)
            if "EXEC SQL" in words and "END-EXEC" not in words:
                in_sql = True
            elif "END-EXEC" in words:
                in_sql = False
        # IDENTIFICATION DIVISION comment-entries (AUTHOR. PAT O'BRIEN.) are
        # free text: an apostrophe there is not a literal, and left as code it
        # opens a string that never closes and silently eats the program.
        md = _DIVISION_HDR.match(up)
        if md:
            division = md.group(1)
            in_id_comment = False
        elif division in ("IDENTIFICATION", "ID") and indicator == " ":
            if _ID_COMMENT_ENTRY.match(up):
                in_id_comment = True
            elif code[:4].strip():           # a new Area-A entry ends the comment-entry
                in_id_comment = False
            if in_id_comment:
                indicator = "*"
        out.append(Line(
            no=i,
            raw=rec,
            code=code,
            indicator=indicator,
            is_comment=indicator in ("*", "/"),
            is_continuation=indicator == "-",
            is_debug=indicator in ("D", "d"),
            is_blank=not code.strip() and indicator not in ("*", "/", "-"),
        ))
    return out, fixed


# Quote handling for continuation: a continued alphanumeric literal restarts
# with a quote in area B and the quote is NOT part of the literal.
def join_cobol_continuations(lines: Sequence[Line],
                             include_debug: bool = False) -> List[LogicalLine]:
    """Collapse continuation lines into single logical lines.

    Comments are dropped entirely - this is what stops a parser reporting a
    commented-out CALL as a live dependency. Debug lines are dropped by
    default for the same reason.
    """
    joined: List[LogicalLine] = []
    for ln in lines:
        if ln.is_comment:
            continue
        if ln.is_debug and not include_debug:
            continue
        if ln.is_blank and not ln.is_continuation:
            continue

        if ln.is_continuation and joined:
            prev = joined[-1]
            frag = ln.code.lstrip()
            if _ends_inside_literal(prev.text) and frag[:1] in ("'", '"'):
                frag = frag[1:]          # the reopening quote is not data
                prev.text = prev.text.rstrip()
                if prev.text.endswith(("'", '"')):
                    prev.text = prev.text[:-1]
                prev.text = prev.text + frag
            else:
                prev.text = prev.text.rstrip() + " " + frag
            prev.end = ln.no
            prev.lines.append(ln.no)
        else:
            if not ln.code.strip():
                continue
            # Area A is columns 8-11, i.e. the first four characters of `code`.
            indent = len(ln.code) - len(ln.code.lstrip())
            joined.append(LogicalLine(text=ln.code.strip(), start=ln.no,
                                      end=ln.no, lines=[ln.no],
                                      area_a=indent < 4))
    return joined


def _ends_inside_literal(text: str) -> bool:
    """True if `text` has an unterminated alphanumeric literal."""
    in_q = None
    i = 0
    while i < len(text):
        ch = text[i]
        if in_q:
            if ch == in_q:
                if i + 1 < len(text) and text[i + 1] == in_q:
                    i += 1          # doubled quote = escaped
                else:
                    in_q = None
        elif ch in ("'", '"'):
            in_q = ch
        i += 1
    return in_q is not None


def _scan_terminator(s: str, start: int, in_q: Optional[str], at_line_end: bool = True) -> Tuple[int, Optional[str]]:
    """find_terminator with the literal state carried in and out: (index or
    -1, the open quote at the end). Lets cobol_statements resume where it
    stopped instead of rescanning the whole sentence for every line added -
    a 1,000-line paragraph with one period at its end was quadratic (LESSONS 146)."""
    i, n = start, len(s)
    while i < n:
        ch = s[i]
        if in_q:
            if ch == in_q:
                if i + 1 < n and s[i + 1] == in_q:
                    i += 1              # doubled quote is an escaped quote
                else:
                    in_q = None
        elif ch in ("'", '"'):
            in_q = ch
        elif ch == ".":
            if i + 1 >= n:
                if at_line_end:
                    return i, in_q
            elif s[i + 1].isspace():
                return i, in_q
        i += 1
    return -1, in_q


def find_terminator(s: str, start: int = 0, at_line_end: bool = True) -> int:
    """Index of the first period that really ends a statement, or -1.

    A period terminates only when it is NOT inside an alphanumeric literal AND
    is followed by whitespace or end-of-text. That second condition is what
    keeps picture strings and numeric literals intact:

        05 WS-AMT  PIC ZZZ,ZZ9.99.     <- the '.' in ZZ9.99 is picture data,
                                          only the trailing one terminates
        MOVE 1.5 TO WS-RATE.           <- 1.5 is one literal
        MOVE 'END.' TO WS-FLAG.        <- the '.' is inside a literal

    Split on every period instead and a copybook turns into twice as many
    half-fields, each with a truncated PIC - and the byte offsets that come out
    of it are wrong in a way that looks entirely reasonable.
    """
    in_q = None
    i, n = start, len(s)
    while i < n:
        ch = s[i]
        if in_q:
            if ch == in_q:
                if i + 1 < n and s[i + 1] == in_q:
                    i += 1              # doubled quote is an escaped quote
                else:
                    in_q = None
        elif ch in ("'", '"'):
            in_q = ch
        elif ch == ".":
            if i + 1 >= n:
                if at_line_end:
                    return i
            elif s[i + 1].isspace():
                return i
        i += 1
    return -1


def cobol_statements(logical: Sequence[LogicalLine]) -> Iterator[LogicalLine]:
    """Split logical lines into real statements.

    Two things this must get right, both of which are routine in real source:

    1. ONE statement spanning MANY lines. A data item's PIC and VALUE clauses,
       a CALL and its USING list, an EXEC SQL block, an OCCURS DEPENDING ON -
       all commonly continue across lines with no continuation indicator,
       because the indicator is only needed to split a *literal*. Parse
       line-by-line and half of every such entry is silently discarded.

    2. MANY statements on ONE line. `1000-EXIT.  EXIT.` is a paragraph header
       and a statement on one physical line. Treating the line as one unit
       loses the paragraph - and with it a node of the PERFORM graph.

    So the text is accumulated into a buffer with a character-to-line map, and
    statements are carved out wherever a genuine terminator appears. Line
    numbers stay exact, because every citation depends on them.

    One place the text lacks its period and the statement ends anyway: an
    EXEC SQL / CICS / DLI block outside the PROCEDURE DIVISION (a DECLARE
    CURSOR or an INCLUDE in WORKING-STORAGE) whose END-EXEC has no period
    after it. Whether a compile accepted that depends on the step that read
    the block (the DB2 precompiler or the SQL coprocessor); read as one
    sentence here, the block swallowed the data entries after it and the PROCEDURE DIVISION
    header, and the program lost every paragraph while it read `parse: ok`
    (the synthetic reproduction F15). The statement is closed at the END-EXEC
    - before the PROCEDURE DIVISION header, or, with no division header yet
    (a copybook read alone), when the next line starts a data entry with its
    level number - and carries `no_period` for the parser's note. Inside the
    PROCEDURE DIVISION an END-EXEC without a period is ordinary: several
    statements make one sentence.
    """
    buf = ""
    lmap: List[int] = []          # line number for each character in buf
    area_a = False                # only the statement starting a logical line
    fresh = True                  # ... may be an Area B construct
    # scanner state, carried across lines: where the last scan stopped, the
    # open quote there, and the EXEC SQL/CICS/DLI state up to exec_pos. Every
    # character of the buffer is looked at once, whatever the sentence length.
    scan_pos = 0
    in_q: Optional[str] = None
    exec_open = False
    exec_pos = 0
    division: Optional[str] = None  # the division the statements yielded so far have entered

    def compact(cut: int) -> None:
        nonlocal buf, lmap, scan_pos, exec_pos
        buf = buf[cut:]
        lmap = lmap[cut:]
        scan_pos = max(0, scan_pos - cut)
        exec_pos = max(0, exec_pos - cut)

    for ll in logical:
        if (division != "PROCEDURE" and in_q is None and buf and not ll.text.startswith(".")
                and (division is not None or _LEVEL_START.match(ll.text))
                and _ENDS_AT_END_EXEC.search(buf[-40:])):          # the tail only: a long sentence stays linear
            opened = None
            for opened in _EXEC_KIND.finditer(blank_literals(buf)):     # not the words of a literal (LESSONS 217)
                pass
            if opened is not None:
                # END-EXEC with no period after it, before the PROCEDURE DIVISION:
                # the statement ends here all the same (see the docstring)
                lead = len(buf) - len(buf.lstrip())
                end = len(buf.rstrip())
                yield LogicalLine(text=buf.strip(), start=lmap[lead], end=lmap[end - 1],
                                  lines=sorted(set(lmap[:end])), area_a=area_a, charmap=lmap[lead:end],
                                  no_period=(opened.group(1).upper(), lmap[opened.start()]))
                buf, lmap, fresh = "", [], True
                scan_pos = exec_pos = 0
                in_q, exec_open = None, False
        if buf and not buf.endswith(" "):
            buf += " "
            lmap.append(lmap[-1] if lmap else ll.start)
        if fresh and not buf.strip():
            area_a = ll.area_a
        buf += ll.text
        lmap.extend([ll.start] * len(ll.text))
        # `ll.text` is one logical line, so a trailing period really does end
        # the statement - there is no more text coming on this line.
        while True:
            idx, in_q = _scan_terminator(buf, scan_pos, in_q, at_line_end=True)
            if idx < 0:
                scan_pos = len(buf)
                break
            # EXEC state up to this period, advanced incrementally - over the text outside literals: the words of
            # `VALUE 'EXEC CICS LINK FAILED'` opened a block no period closed, and every data item and paragraph
            # after it was lost while the member read `parse: ok` (LESSONS 217). exec_pos is always just after a
            # period outside a literal (or the buffer's start), so no literal is open where the slice starts
            seg = buf[exec_pos:idx + 1]
            if _EXEC_TOKEN.search(seg):                                  # most statements hold no EXEC word at all
                for m in _EXEC_TOKEN.finditer(blank_literals(seg)):
                    exec_open = not m.group(0).upper().startswith("END")
            exec_pos = idx + 1
            if exec_open:
                # `-- get the policy.` inside EXEC SQL ... END-EXEC is SQL
                # text: the statement ends after END-EXEC, not here.
                scan_pos = idx + 1
                continue
            raw_stmt = buf[:idx + 1]
            text = raw_stmt.strip()
            if text:
                division = _division_entered(text, division)
                lead = len(raw_stmt) - len(raw_stmt.lstrip())
                s_line = lmap[lead] if lmap and lead < len(lmap) else ll.start
                e_line = lmap[min(idx, len(lmap) - 1)] if lmap else ll.end
                yield LogicalLine(text=text, start=s_line, end=e_line,
                                  lines=sorted(set(lmap[:idx + 1])),
                                  area_a=area_a, charmap=lmap[lead:idx + 1])
            compact(idx + 1)
            scan_pos = 0
            in_q = None
            # Anything after the period sits in Area B by definition.
            area_a, fresh = False, False
        if not buf.strip():
            buf, lmap, fresh = "", [], True
            scan_pos = exec_pos = 0
            in_q, exec_open = None, False

    if buf.strip():
        lead = len(buf) - len(buf.lstrip())
        yield LogicalLine(text=buf.strip(), start=lmap[lead] if lmap and lead < len(lmap) else 0,
                          end=lmap[-1] if lmap else 0,
                          lines=sorted(set(lmap)), area_a=area_a, charmap=lmap[lead:])


_DECIMAL_DOT = re.compile(r"\d\.\d")
_EXEC_OPEN = re.compile(r"\bEXEC\s+(?:SQL|CICS|DLI)\b", re.IGNORECASE)
_EXEC_TOKEN = re.compile(r"\bEXEC\s+(?:SQL|CICS|DLI)\b|END-EXEC", re.IGNORECASE)
_EXEC_KIND = re.compile(r"(?<![A-Z0-9\-])EXEC\s+(SQL|CICS|DLI)(?![A-Z0-9\-])", re.IGNORECASE)
_ENDS_AT_END_EXEC = re.compile(r"(?<![A-Z0-9\-])END-EXEC\s*$", re.IGNORECASE)
# a data entry starts with its level number: 01, 05, 77, 88 (`05.` is a bare group level)
_LEVEL_START = re.compile(r"^\d{1,2}(?:\s|\.|$)")
_STATEMENT_DIVISION = re.compile(r"^(IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION(?![A-Z0-9\-])",
                                 re.IGNORECASE)
_PROCEDURE_DIVISION = re.compile(r"(?<![A-Z0-9\-])PROCEDURE\s+DIVISION(?![A-Z0-9\-])", re.IGNORECASE)
_LITERALS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def blank_literals(text: str) -> str:
    """`text` with every closed alphanumeric literal blanked, quotes and all,
    to the same length: a scan for keywords sees only the code, and a
    position found in it is the same position in `text`."""
    return _LITERALS.sub(lambda m: " " * len(m.group(0)), text)


def code_outside_literals(line: str) -> str:
    """One source line's code with its literals blanked, and cut where a
    literal opens that the line does not close (it goes on, on a
    continuation line): what is left is the line's own words."""
    t = blank_literals(line)
    cut = [i for i in (t.find("'"), t.find('"')) if i >= 0]
    return t[:min(cut)] if cut else t


def _division_entered(text: str, division: Optional[str]) -> Optional[str]:
    """The division after a statement: its own header's, PROCEDURE when the
    words PROCEDURE DIVISION sit anywhere in it outside a literal (a data
    entry that ran on into the header - the division is entered all the
    same, so nothing after it is taken for WORKING-STORAGE), else unchanged."""
    m = _STATEMENT_DIVISION.match(text)
    if m:
        return "IDENTIFICATION" if m.group(1).upper() == "ID" else m.group(1).upper()
    if division != "PROCEDURE" and _PROCEDURE_DIVISION.search(_LITERALS.sub(" ", text)):
        return "PROCEDURE"
    return division


def _inside_exec(buf: str, idx: int) -> bool:
    """True when position idx lies inside an EXEC SQL/CICS/DLI ... END-EXEC
    block that has not been closed yet."""
    head = buf[:idx + 1]
    last = None
    for m in _EXEC_OPEN.finditer(head):
        last = m
    if last is None:
        return False
    return "END-EXEC" not in head[last.end():].upper()


def _terminates(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped.endswith("."):
        return False
    if _ends_inside_literal(stripped):
        return False
    return True


# --------------------------------------------------------------------------
# JCL reading
# --------------------------------------------------------------------------

@dataclass
class JclStatement:
    """One JCL statement, continuations already joined."""
    name: str          # step name / dd name / job name, '' if unnamed
    op: str            # JOB | EXEC | DD | PROC | PEND | SET | INCLUDE | IF | OUTPUT
    operands: str
    start: int
    end: int
    lines: List[int] = dc_field(default_factory=list)
    inline_data: List[str] = dc_field(default_factory=list)   # SYSIN control cards
    comment: str = ""                                          # text after the operand field


def _cut_operands(rest: str) -> Tuple[str, str]:
    """(operands, comment): the operand field ends at the first blank that is
    outside quotes and parentheses. Inside an open quote the whole remainder
    (trailing blanks included, up to col 71) belongs to the string."""
    depth = 0
    in_quote = False
    for i, ch in enumerate(rest):
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch in " \t" and depth == 0:
                return rest[:i], rest[i:].strip()
    return (rest if in_quote else rest.rstrip()), ""


# The name field may be qualified: //PROCSTEP.DDNAME DD ... overrides a DD
# inside a called PROC. A pattern limited to 8 characters silently drops every
# such override - and with it the datasets a job actually uses.
_JCL_STMT = re.compile(r"^//(?P<name>[A-Z0-9@#$]{0,8}(?:\.[A-Z0-9@#$]{1,8})?)\s+(?P<op>[A-Z]+)\s*(?P<rest>.*)$",
                       re.IGNORECASE)
_JCL_NAMELESS = re.compile(r"^//\s+(?P<op>[A-Z]+)\s*(?P<rest>.*)$", re.IGNORECASE)
_JCL_CONT_ONLY = re.compile(r"^//\s+(?P<rest>\S.*)$")


def read_jcl(text: str, data: bytes = b"", enc: str = "utf-8") -> List[JclStatement]:
    """Parse JCL into statements, joining continuations and capturing inline data.

    Two things here matter more than they look:

    1. Inline data (`DD *` / `DD DATA`) is captured onto the DD statement. Those
       are the control cards, and for a huge fraction of batch steps the control
       card - not the COBOL - determines what the step actually does. A model
       asked "what does this job do" without the control cards will guess.

    2. Significant text stops at column 71 and column 72 is the continuation
       flag. Reading to end-of-line instead swallows the flag and any trailing
       comment text into the operands.
    """
    records = _split_records(text, data, enc)
    stmts: List[JclStatement] = []
    i = 0
    n = len(records)

    while i < n:
        rec = records[i].replace("\t", "    ")
        lineno = i + 1

        if rec.startswith("//*") or rec.startswith("//\x00"):
            i += 1
            continue
        if rec.startswith("/*") and not rec.startswith("//"):
            i += 1          # end-of-data delimiter
            continue
        if not rec.startswith("//"):
            i += 1
            continue
        if rec[2:].strip() == "":
            i += 1          # null statement: end of job
            continue

        body = rec[:71]
        m = _JCL_STMT.match(body) or _JCL_NAMELESS.match(body)
        if not m:
            i += 1
            continue

        gd = m.groupdict()
        name = (gd.get("name") or "").strip().upper()
        op = gd["op"].upper()
        # The operand field ends at the first blank outside quotes and
        # parentheses; the rest of the line is a comment. Keeping the comment
        # glued on turns `DSN=PROD.X   INPUT MASTER` into a dataset that does
        # not exist and hides the real one from every lineage query.
        operands, comment = _cut_operands(gd["rest"].lstrip())
        if op in ("IF", "ELSE", "ENDIF"):
            operands, comment = gd["rest"].strip(), ""     # relational expressions contain blanks
        lines = [lineno]
        start = lineno

        # ---- join continuations -------------------------------------------
        # A statement continues when the operand field ends with a comma, when
        # a quoted string is still open at column 71, or when column 72 holds
        # a non-blank. The next record must start with '//' and, for a comma
        # continuation, resume in cols 4-16; inside a quote it resumes at
        # column 16 verbatim.
        quotes = operands.count("'")                 # kept up to date as text is appended (recounting was quadratic)
        while i + 1 < n:
            in_quote = quotes % 2 == 1
            col72 = len(rec) > 71 and rec[71] not in " \t"
            if not (in_quote or col72 or operands.rstrip().endswith(",")):
                break
            nxt = records[i + 1].replace("\t", "    ")
            if not nxt.startswith("//") or nxt.startswith("//*"):
                break
            if in_quote:
                if nxt[2:15].strip():
                    break
                piece = nxt[15:71].rstrip()
                operands = operands + piece
                quotes += piece.count("'")
            else:
                cont = _JCL_CONT_ONLY.match(nxt[:71])
                if not cont:
                    break
                more, c2 = _cut_operands(cont.group("rest").lstrip())
                operands = operands.rstrip() + more
                quotes += more.count("'")
                if c2:
                    comment = (comment + " " + c2).strip()
            i += 1
            lines.append(i + 1)
            rec = nxt

        stmt = JclStatement(name=name, op=op, operands=operands.rstrip(),
                            start=start, end=i + 1, lines=lines, comment=comment)

        # ---- capture inline data for DD * / DD DATA ------------------------
        if op == "DD" and _is_inline_dd(operands):
            delim = _inline_delimiter(operands)
            j = i + 1
            while j < n:
                drec = records[j]
                if drec.startswith(delim) or (delim == "/*" and drec.startswith("/*")):
                    j += 1
                    break
                if delim == "/*" and drec.startswith("//") and not _is_dlm_data(operands):
                    break
                stmt.inline_data.append(drec[:80].rstrip())
                j += 1
            i = j - 1
            stmt.end = j

        stmts.append(stmt)
        i += 1

    return stmts


def _is_inline_dd(operands: str) -> bool:
    head = operands.split(",")[0].strip().upper()
    return head in ("*", "DATA") or head.startswith("*") or head.startswith("DATA")


def _is_dlm_data(operands: str) -> bool:
    return "DATA" in operands.split(",")[0].strip().upper()


def _inline_delimiter(operands: str) -> str:
    m = re.search(r"\bDLM=(?:'([^']{1,2})'|([^,\s]{1,2}))", operands, re.IGNORECASE)
    if m:
        return m.group(1) or m.group(2)
    return "/*"


# --------------------------------------------------------------------------
# operand parsing shared by JCL / PROC / control cards
# --------------------------------------------------------------------------

def split_operands(operands: str) -> List[str]:
    """Split a JCL operand field on commas that are not inside parens or quotes."""
    out, buf, depth, quote = [], [], 0, None
    for ch in operands:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [o for o in out if o]


def keyword_operands(operands: str) -> dict:
    """Return the KEY=VALUE operands as a dict (positional ones are skipped)."""
    d = {}
    for tok in split_operands(operands):
        if "=" in tok:
            k, _, v = tok.partition("=")
            d[k.strip().upper()] = v.strip()
    return d


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------

def load(path: str) -> Tuple[str, bytes, str]:
    """Read a file and return (text, raw_bytes, encoding)."""
    with open(path, "rb") as fh:
        data = fh.read()
    text, enc = decode_bytes(data)
    return text, data, enc


def numbered(lines: Sequence[Line], start: int, end: int) -> str:
    """Render a physical line range with line numbers, for citation display."""
    buf = io.StringIO()
    for ln in lines:
        if start <= ln.no <= end:
            buf.write(f"{ln.no:6d} | {ln.raw.rstrip()}\n")
    return buf.getvalue()
