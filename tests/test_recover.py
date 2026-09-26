r"""
Copybooks the estate lacks, rebuilt from the expanded text of the programs
that copy them (LESSONS 167, 168). A compiler listing flags each copied
line with a C; an expanded text lines up against the original program; an
expanded source carries the copybook's name in columns 73-80; an expander
may leave a comment naming it. The index says which copybooks are missing;
the recovered ones make the programs whole on the next build, and go away
when the real member arrives. A wrong copybook, written silently, is worse
than a missing one - so every block is checked before it is trusted.
"""

import contextlib
import io
import os
import shutil
import re
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, recover  # noqa: E402

FIX = os.path.join(HERE, "fixtures")

POLDCL = """000100 01  POLICY-DCL.                                                POLD0001
000200     05  POL-NUMBER              PIC X(12).                       POLD0002
000300     05  POL-STATUS              PIC X(02).                       POLD0003
000400     05  POL-PREMIUM             PIC S9(09)V99 COMP-3.            POLD0004
"""

PROG = [
    "000100 IDENTIFICATION DIVISION.",
    "000200 PROGRAM-ID. TESTPGM.",
    "000300 DATA DIVISION.",
    "000400 WORKING-STORAGE SECTION.",
    "000500 01  WS-REC.",
    "000600     COPY PMASTREC.",
    "000700 01  WS-POL.",
    "000800     COPY POLDCL.",
    "000900 PROCEDURE DIVISION.",
    "001000 0000-MAIN.",
    "001100     MOVE 'X' TO PM-POLICY-STATUS.",
    "001200     GOBACK.",
]


def records_of(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [ln.rstrip("\r\n") for ln in fh]


BANNER = "1PP 5655-EC6 IBM Enterprise COBOL for z/OS  6.3.0 P231102                 SAMPPGM   Date 09/28/2024  Time 14:31:41   Page   {page}"
RULER = "   LineID  PL SL  ----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7-|--+----8 Map and Cross Reference"


def ibm_listing(program_records, copybooks, ruler=True, flag="C", prefix_lines=0, strip=False, flag_at=None, page_lines=0,
                copy_table=None):
    """What Enterprise COBOL prints: a line number per source line, a C after
    the number on every copied line, the 80-column record after that - and,
    with `page_lines`, a page break every so many lines, each new page
    starting with the banner and the ruler again, as the real thing does.
    `copy_table`: [(copybook, DD name, dataset)] - the copybook-source table
    Enterprise COBOL 6 prints after the source (which library each copybook
    was read from), under a heading, one row per copybook with a number and
    dates after the dataset."""
    out = [BANNER.format(page=1), "0Invocation parameters:", " TRUNC(BIN),DATA(24),XREF", "0Options in effect:", "    XREF(FULL)"]
    out += [f" some translator output line {i}" for i in range(prefix_lines)]
    page = 2
    out.append(BANNER.format(page=page))
    if ruler:
        out.append(RULER)
    n = 0
    sql = []
    on_page = 0

    def emit(line):
        nonlocal on_page, page
        if page_lines and on_page >= page_lines:
            page += 1
            out.append(BANNER.format(page=page))
            if ruler:
                out.append(RULER)
            on_page = 0
        out.append(line)
        on_page += 1

    def stmt_closed(stmt):
        # the generator's own reading (not the tool's): a period outside literals and outside ==pseudo-text==
        bare = re.sub(r"'[^']*'|\"[^\"]*\"", " ", stmt)
        if bare.count("==") % 2:
            return False
        return re.search(r"\.(\s|$)", re.sub(r"==.*?==", " ", bare)) is not None

    open_copy = None                                                   # a COPY statement spanning lines: gathered first
    for rec in program_records:
        n += 1
        emit(f"   {n:06d}         {rec.ljust(80)}")
        code = rec[7:72] if len(rec) > 7 else ""
        found = recover.copy_in(code)
        if open_copy and not found:
            open_copy = (open_copy[0], open_copy[1] + " " + code.strip())
            if not stmt_closed(open_copy[1]):
                continue
            found, open_copy = open_copy, None                         # the period reached: the copied lines follow
        elif found and not stmt_closed(found[1]):
            open_copy = found                                          # as the compiler prints it: the whole statement, then the copy
            continue
        if sql or ("EXEC SQL" in code.upper() and "END-EXEC" not in code.upper()):
            sql.append(code.strip())                                   # a three-line EXEC SQL INCLUDE
            if "END-EXEC" in code.upper():
                found = recover.copy_in(" ".join(sql))
                sql = []
            else:
                continue
        if found and found[0] in copybooks:
            for k, crec in enumerate(copybooks[found[0]]):
                n += 1
                f = flag_at.get((found[0], k), flag) if flag_at else flag
                emit(f"   {n:06d}{f.ljust(9)}{crec.ljust(80)}")           # the flags sit in the PL/SL columns
    out.append("")
    out.append("   LineID  Message code  Message text")
    if copy_table:
        out.append("")
        out.append("0Copybook  Ddname    Library dataset                                Text  Created     Last modified")
        for k, (cb, dd, dsn) in enumerate(copy_table, 1):
            out.append(f"  {cb:<8}  {dd:<8}  {dsn:<44}  {k:>4}  1997/01/29  2018/11/11 08:00:27")
    text = "\n".join(out) + "\n"
    return "\n".join(ln.rstrip() for ln in text.splitlines()) + "\n" if strip else text


OSVS_BANNER = "1PP 5740-CB1 RELEASE 2.4          IBM OS/VS COBOL  JULY 1, 1982                          14.31.19       DATE FEB  5,1992"
OSVS_PAGE = "1  {page}        TESTPGM         14.31.19        FEB  5,1992"


def osvs_listing(program_records, copybooks, mark="apart", page_lines=0, banner=True, headers=True, seq=None, start=1, asa=False):
    """What the older compilers (OS/VS COBOL) print: a FIVE-digit count of
    every line read, the copy mark C apart from it (or attached, or none), the
    80-column record from column 17, a page header of page number, program,
    time and date - and after the source a data map, the diagnostics (which
    cite earlier line numbers, one with a C after it) and the cross-reference
    dictionary. `seq` rewrites columns 1-6 of every record (blank, a tag)."""
    out = ([OSVS_BANNER] if banner else []) + ([OSVS_PAGE.format(page=1)] if headers else [])
    n = start - 1
    on_page = 0
    page = 1

    def emit(num, flag, rec):
        nonlocal on_page, page
        if seq is not None:
            rec = seq(rec)
        if page_lines and on_page >= page_lines:
            page += 1
            if headers:
                out.append(OSVS_PAGE.format(page=page))
            on_page = 0
        if asa:                                                       # carriage control in column 1, the number from column 2:
            out.append(f"{'0' if on_page == 0 else ' '}{num:05d} {flag:<8}{rec}")   # '0' (skip a line) glued to the first number
        elif mark == "attached":
            out.append(f"  {num:05d}{flag:<9}{rec}")
        else:
            out.append(f"  {num:05d} {flag:<8}{rec}")
        on_page += 1

    for rec in program_records:
        n += 1
        emit(n, "", rec)
        found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
        if found and found[0] in copybooks:
            for crec in copybooks[found[0]]:
                n += 1
                emit(n, "" if mark == "none" else "C", crec)
    if headers:
        out.append(OSVS_PAGE.format(page=page + 1))
    out.append("   INTRNL NAME   LVL  SOURCE NAME          BASE    DISPL  INTRNL NAME   DEFINITION   USAGE")
    out += [f"   DNM=1-{100 + i:03d}     05   AAAA-FLD-{i:02d}          BL=1    {i * 4:03X}    DNM=1-{100 + i:03d}     DS 4C        DISP"
            for i in range(30)]
    out.append("  CARD   ERROR MESSAGE")
    out.append(f"  {start + 4:05d}C  IKF2133I-W   A COPY STATEMENT ... WORDS IN A MESSAGE")   # cites an earlier line, a C after it
    out += [f"  {start + 5 + i:05d}   IKF2133I-W   COPY WORD SEEN IN A MESSAGE {i}" for i in range(5)]
    out.append("   CROSS-REFERENCE DICTIONARY")
    out.append("   DATA NAMES                      DEFN    REFERENCE")
    return "\n".join(out) + "\n"


def blank_seq(rec):
    return "      " + rec[6:]


def tag_seq(rec):
    return "CHG001" + rec[6:]


def translator_front(prog, width=18):
    """A translator's or precompiler's own listing, printed in front: five-digit
    numbers of its own, its record at another column, its own heading."""
    pad = " " * (width - 7)
    return [f"  {i:05d}{pad}{r}" for i, r in enumerate(prog * 3, 1)] + ["   Diagnostic Messages", "   none"]


class OlderListings(unittest.TestCase):
    """His line (2026-09-23), names changed: 03008   00854  01  XXXX-SEG   COPY  'XXXXX'. - many programs, the
    copybook expanded under it, and the report said 'not in any expanded text': the tool read only six-digit line
    numbers, and a listing from an older compiler gave it no lines at all (LESSONS 176). The review of the first
    fix showed how easily a wider pattern takes a plain expanded source for a listing, or reads a listing one
    column off - so each case here either IS an older listing and must come out exact, or is NOT one and must
    come out as it did before."""

    def setUp(self):
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        self.poldcl = POLDCL.splitlines()
        self.prog = list(PROG)
        self.prog[6] = "000700 01  WS-POL-SEG     COPY 'POLDCL'."           # the level-01 form, name in quotes
        del self.prog[7]
        self.books = {"PMASTREC": self.pmast, "POLDCL": self.poldcl}

    def exact(self, regions, stats, label, seq=None):
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, (label, stats))
        fix = seq or (lambda r: r)
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [fix(r).rstrip() for r in self.pmast], label)
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [fix(r).rstrip() for r in self.poldcl], label)
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, (label, stats))
        self.assertFalse(any(r.suspect for r in regions), (label, [r.suspect for r in regions]))

    def test_an_older_compiler_listing_with_five_digit_line_numbers(self):
        for mark in ("apart", "attached"):
            text = osvs_listing(self.prog, self.books, mark=mark, page_lines=9, start=3001)
            self.assertIn("1  2        TESTPGM", text, "page headers of the older layout between the lines")
            fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
            self.assertEqual(fmt, "older compiler listing", (mark, stats))
            self.exact(regions, stats, mark)
            self.assertEqual(stats["source column"], 17, (mark, stats))
            self.assertEqual(stats["flagged lines"], len(self.pmast) + len(self.poldcl),
                             "the diagnostics after the source, one with a C, add nothing")

    def test_no_banner_no_numeric_sequence_areas_no_page_headers(self):
        # the page headers alone say 'older listing'; without them the copy marks do
        for label, kw in (("blank columns 1-6", dict(banner=False, seq=blank_seq)),
                          ("a tag in columns 1-6", dict(banner=False, seq=tag_seq)),
                          ("no banner, no headers, marks only", dict(banner=False, headers=False))):
            text = osvs_listing(self.prog, self.books, page_lines=9, **kw)
            fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
            self.assertEqual(fmt, "older compiler listing", (label, stats))
            self.exact(regions, stats, label, seq=kw.get("seq"))

    def test_no_copy_marks_lines_up_with_the_program(self):
        text = osvs_listing(self.prog, self.books, mark="none")
        fmt, regions, _stats = recover.extract(text, "TESTPGM.lst", set(), original=self.prog)
        self.assertEqual(fmt, "older compiler listing, copied lines not flagged, lined up with the program")
        self.assertEqual({r.name for r in regions}, {"PMASTREC", "POLDCL"})

    def test_a_translator_listing_in_front_never_sets_the_column(self):
        # a job's print output: the translator's listing first (its own numbers, its record elsewhere), then
        # the compiler's (record at column 17) - read one column late, a copybook's comments turn into code and
        # still pass every check, so the column must come from the compiler's own lines
        for width in (18, 16, 25):
            for mark in ("apart", "none"):
                text = "\n".join(translator_front(self.prog, width)) + "\n" + osvs_listing(self.prog, self.books, mark=mark, page_lines=50)
                fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set(), original=self.prog if mark == "none" else None)
                if mark == "apart":
                    self.assertEqual(fmt, "older compiler listing", (width, stats))
                    self.assertEqual(stats["source column"], 17, (width, stats))
                    self.exact(regions, stats, f"translator at {width}")
                else:
                    self.assertEqual(fmt, "older compiler listing, copied lines not flagged, lined up with the program", (width, stats))
                    by = {r.name: r for r in regions}
                    self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, width)
                    self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl], width)

    def test_a_header_one_column_off_is_corrected_by_the_comment_lines(self):
        # IDENTIFICATION DIVISION written in column 9, blank columns 1-6: the header's guess is one column late,
        # and the shape check passes that too (column 7 of every record is then a blank column 8) - the comment
        # indicators in column 7 decide
        prog = [blank_seq(r) for r in self.prog]
        prog[0] = "        IDENTIFICATION DIVISION."
        books = {k: [blank_seq(r) for r in v] for k, v in self.books.items()}
        text = osvs_listing(prog, books, banner=False)
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual((fmt, stats["source column"]), ("older compiler listing", 17), stats)
        by = {r.name: r for r in regions}
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [blank_seq(r).rstrip() for r in self.pmast])

    def test_an_editor_ruler_comment_inside_an_older_listing(self):
        # the editor's COLS line kept as a comment in the program prints after the line number, where a listing's
        # ruler would be: the current-layout path finds no six-digit line, and the older layout is tried next
        prog = list(self.prog)
        prog.insert(4, "000450*----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7--")
        text = osvs_listing(prog, self.books, page_lines=9)
        self.assertTrue(any("----+-*A" in ln for ln in text.splitlines()))
        self.assertFalse(any(recover._ruler_at(ln) for ln in text.splitlines()), "no LineID heading: not a listing's ruler")
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "older compiler listing", stats)
        self.assertEqual(stats["ruler line"], 0, stats)
        self.exact(regions, stats, "ruler comment")

    def test_numbers_that_do_not_count_up_by_one_are_not_a_listing(self):
        # a print that numbers its lines in steps (a compare report, a sequence-numbered print): read as it was before
        text = osvs_listing(self.prog, self.books)
        stepped = []
        k = 0
        for ln in text.splitlines():
            m = recover._OLD_LINE.match(ln)
            if m:
                k += 10
                ln = ln[:m.start(1)] + f"{k:05d}" + ln[m.end(1):]
            stepped.append(ln)
        self.assertIsNone(recover.older_layout(stepped))
        self.assertIsNotNone(recover.older_layout(text.splitlines()))

    def never_shifted(self, text, label, books=None):
        """Either exact, or nothing: a block read at another column than 17 must never come out."""
        books = books or self.books
        _fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        for r in regions:
            self.assertEqual([x.rstrip() for x in r.records], [x.rstrip() for x in books[r.name]], (label, r.name, stats))
        if regions:
            self.assertEqual(stats.get("source column"), 17, (label, stats))
        return regions

    def test_sequence_numbers_decide_the_column_whatever_the_header(self):
        for col in (9, 10, 11):
            prog = list(self.prog)
            prog[0] = "000100" + " " * (col - 7) + "IDENTIFICATION DIVISION."
            text = osvs_listing(prog, self.books, banner=False)
            old = recover.older_layout(text.splitlines())
            self.assertEqual((old["off"], old["how"]), (16, "sequence numbers in columns 1-6"), col)
            fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
            self.assertEqual(fmt, "older compiler listing", (col, stats))
            self.exact(regions, stats, f"header in column {col}")

    def test_a_six_digit_column_that_is_not_the_record_proves_nothing(self):
        # a listing printing its own six-digit statement number between the line number and the record: those digits
        # line up with an indicator after them on every line, but the headers sit nine columns further - the sequence
        # numbers must not decide there; the comment lines and headers find the record, nine columns later
        books = {k: [blank_seq(r) for r in v] for k, v in self.books.items()}     # the only six digits: the extra column
        text = osvs_listing([blank_seq(r) for r in self.prog], books, banner=False)
        extra = []
        for k, ln in enumerate(text.splitlines(), 1):
            m = recover._OLD_LINE.match(ln)
            extra.append(ln[:m.end()] + f"  {k:06d} " + ln[m.end():] if m else ln)
        old = recover.older_layout(extra)
        self.assertEqual((old["off"], old["how"]), (25, "comment lines and division headers agree"))
        fmt, regions, stats = recover.extract("\n".join(extra) + "\n", "TESTPGM.lst", set())
        self.assertEqual((fmt, stats["source column"]), ("older compiler listing", 26), stats)
        self.exact(regions, stats, "an extra six-digit column", seq=blank_seq)
        # without comment lines, nothing can prove the column: nothing is read
        bare = [ln for ln in extra if not re.match(r"^  \d{5}\s+(C\s+)?\d{6}\*", ln[:40]) and "*" not in ln[25:32]]
        self.assertIsNone(recover.older_layout(bare))

    def test_comment_borders_and_change_flags_never_shift_the_column(self):
        # round two: borders starting in column 4-6 and a '*' change flag in column 6 pulled the comment count
        # one to three columns early; the count now takes comment TEXT only, and must agree with the headers
        base = [blank_seq(r) for r in self.prog]
        books = {k: [blank_seq(r) for r in v] for k, v in self.books.items()}
        for start in (4, 5, 6):
            border = " " * (start - 1) + "*" * (72 - start + 1)
            box = [border, "      *  POLICY UPDATE - READS THE MASTER ONCE", border] * 5    # more border lines than text
            text = osvs_listing(base[:4] + box + base[4:], books, banner=False)
            regions = self.never_shifted(text, f"borders from column {start}", books)
            self.assertEqual({r.name for r in regions}, {"PMASTREC", "POLDCL"}, f"borders from column {start}")
        # '**' comments: the asterisk in column 8 as well - read one column late they look like comment text. With
        # almost nothing in column 8 (fields in area B), that later column passes the shape check too
        stars = ["      **  POLICY UPDATE - STEP " + str(k) for k in range(10)]
        real = ["      *  POLICY UPDATE - NOTE " + str(k) for k in range(3)]
        filler = ["           DISPLAY 'FILLER LINE'." for _ in range(200)]
        only = {"POLDCL": books["POLDCL"]}
        text = osvs_listing(base[:4] + stars + real + base[4:] + filler, only, banner=False)
        late = [ln[17:97] for ln in text.splitlines() if recover._OLD_LINE.match(ln)]
        self.assertGreaterEqual(recover.shaped(late), recover.FALLBACK_OK, "one column late passes the shape check")
        regions = self.never_shifted(text, "'**' comments", only)
        self.assertEqual({r.name for r in regions}, {"POLDCL"}, "'**' comments")
        flagged = [r[:5] + "*" + r[6:] if r[6:7] == " " and r[7:].strip() and i % 2 else r for i, r in enumerate(base)]
        self.never_shifted(osvs_listing(flagged, books, banner=False), "change flags in column 6", books)

    def test_without_sequence_numbers_or_comments_nothing_is_read_and_the_report_says_why(self):
        # round three: headers alone agreed with each other one to three columns off when a program writes all of
        # area A in column 9-11 - so headers alone prove nothing; the copybooks are named as unreadable, not missing
        base = [blank_seq(r) for r in self.prog]
        books = {k: [blank_seq(r) for r in v if not r[6:7] == "*"] for k, v in self.books.items()}
        for label, prog in (("area A in column 8", base),
                            ("area A in column 9", [" " + r if r.strip() and r[7:8] != " " else r for r in base])):
            text = osvs_listing(prog, books, banner=False)
            self.assertIsNone(recover.older_layout(text.splitlines()), label)
            fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
            self.assertEqual((fmt, regions), (recover.UNPROVABLE, []), label)
            self.assertEqual(stats["named"], ["PMASTREC", "POLDCL"], label)
            self.assertEqual(stats["why"], "no sequence numbers in columns 1-6 and too few comment lines to prove the column")

    def test_a_letter_touching_the_record_is_no_copy_mark(self):
        m = recover._OLD_LINE.match("  00007        CHG001     05  X  PIC X.")
        self.assertEqual(recover._old_flag("  00007        CHG001     05  X  PIC X.", m, 16), "",
                         "one column late, the C of CHG001 sits in the gap - but it touches the record")
        m = recover._OLD_LINE.match("  00007 C      CHG001     05  X  PIC X.")
        self.assertEqual(recover._old_flag("  00007 C      CHG001     05  X  PIC X.", m, 15), "C")

    def test_a_short_run_says_so_in_the_trace(self):
        short = osvs_listing(list(PROG[:4]) + ["000500 01  WS-POL.", "000600     COPY POLDCL."] + list(PROG[8:]), {"POLDCL": self.poldcl})
        self.assertIsNone(recover.older_layout(short.splitlines()))
        self.assertRegex(recover.older_why_not(short.splitlines()), r"^the numbering run is \d+ lines, fewer than 20$")

    def test_headers_all_indented_the_same_are_no_proof(self):
        # every division and section header in column 10, the level numbers in column 8, no sequence numbers, no
        # comments: the headers agree with each other, but the records do not look like source at their column
        base = [blank_seq(r) for r in self.prog]
        base = ["         " + r.strip() if recover._HEADER_WORD.search(r) else r for r in base]
        books = {k: [blank_seq(r) for r in v if not r[6:7] == "*"] for k, v in self.books.items()}
        text = osvs_listing(base, books, banner=False)
        self.assertIsNone(recover.older_layout(text.splitlines()))
        self.never_shifted(text, "headers all in column 10", books)

    def test_carriage_control_glued_to_the_number(self):
        # '0' (skip a line) in column 1 straight before the five-digit number reads as a SIX-digit line number; on
        # enough pages those few lines made the text a current-layout listing and the copybook came out cut
        text = osvs_listing(self.prog, self.books, page_lines=2, asa=True)
        self.assertGreaterEqual(sum(1 for ln in text.splitlines() if recover._LISTING_SHAPE.match(ln)), 5)
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "older compiler listing", stats)
        self.assertEqual(stats["source column"], 16, stats)
        by = {r.name: r for r in regions}
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])

    def test_a_line_printed_again_does_not_cut_the_copybook(self):
        text = osvs_listing(self.prog, self.books, page_lines=9).splitlines()
        k = next(i for i, ln in enumerate(text) if ln.startswith("  ") and " C " in ln[:16] and "PM-POLICY-STATUS" in ln)
        text.insert(k + 1, text[k])                                    # a page eject prints the line again
        fmt, regions, stats = recover.extract("\n".join(text) + "\n", "TESTPGM.lst", set())
        self.assertEqual(fmt, "older compiler listing", stats)
        self.exact(regions, stats, "line printed twice")

    def test_a_comment_or_literal_naming_a_division_is_no_header(self):
        # enough comments naming divisions, at one column, to outvote the four real headers if they counted
        prog = (["000050*  RC 12 = INVALID DIVISION CODE ON THE POLICY", "000060*  THE PROCEDURE DIVISION READS POLMAST ONCE",
                 "000070*  THE DATA DIVISION HOLDS NO TABLES", "000080*  THE LINKAGE SECTION IS EMPTY",
                 "000090*  THE FILE SECTION NAMES TWO FILES", "000095*  THE ENVIRONMENT DIVISION NAMES NO DEVICES"]
                + list(self.prog))
        for p in (prog, [blank_seq(r) for r in prog]):
            books = self.books if p is prog else {k: [blank_seq(r) for r in v] for k, v in self.books.items()}
            text = osvs_listing(p, books, banner=False)
            fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
            self.assertEqual(fmt, "older compiler listing", stats)
            self.exact(regions, stats, "a comment naming a division", seq=None if p is prog else blank_seq)

    def test_two_compile_units_the_one_with_more_copy_marks_is_read(self):
        prog_b = (list(PROG[:5]) + ["000700 01  WS-POL.", "000800     COPY POLDCL."] + list(PROG[8:])
                  + [f"0013{k:02d}     DISPLAY 'B'." for k in range(16)])         # long enough to count, fewer marks
        prog_a = [r for r in PROG if "POLDCL" not in r]
        a = osvs_listing(prog_a, {"PMASTREC": self.pmast})
        b = osvs_listing(prog_b, {"POLDCL": self.poldcl})
        for text in (a + b, b + a):
            _fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
            self.assertEqual([r.name for r in regions], ["PMASTREC"], stats)

    def test_a_precompiler_listing_in_front_of_a_current_listing_without_a_ruler(self):
        # the current layout keeps reading six-digit lines only: a five-digit listing in front changes nothing
        text = ibm_listing(list(PROG), self.books, ruler=False)
        text = text.replace("    XREF(FULL)\n", "    XREF(FULL)\n" + "\n".join(translator_front(PROG, 11)) + "\n", 1)
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        self.assertEqual(stats["source column"], 19, stats)
        self.assertEqual([r.rstrip() for r in {r.name: r for r in regions}["PMASTREC"].records], [r.rstrip() for r in self.pmast])

    def test_the_banner_and_page_header_shapes(self):
        self.assertTrue(recover._OLD_PAGE.match("1  17        PROGANNM        14.31.19        FEB  5,1923"), "his header, names changed")
        self.assertTrue(recover._OLD_BANNER.match(OSVS_BANNER))
        self.assertTrue(recover._OLD_BANNER.match("1PP NO. 5746-CB1 RELEASE 3.0   IBM DOS/VS COBOL"))
        self.assertFalse(recover._OLD_BANNER.match("           PP 5740-CB1 OS/VS COBOL, CONVERTED TO VS COBOL II."), "a REMARKS line")
        self.assertFalse(recover._OLD_PAGE.match("000100 01  WS-TIME   PIC X(8) VALUE '14.31.19'."))


class PlainSourcesStayPlain(unittest.TestCase):
    """Expanded sources that are NOT listings: what the review of the first fix found a wider pattern would
    misread (a copybook's lines dropped, a shortened copybook written, or nothing recovered)."""

    POLSEG = [body.ljust(72) + "POLSEG" for body in (       # the copybook's name in columns 73-80
        "000100     05  PS-KEY              PIC X(10).",
        "           05  PS-STATUS           PIC X(02).",
        "               88  PS-ACTIVE       VALUE 'AC'.",
        "000400     05  PS-AMT              PIC S9(7)V99 COMP-3.")]

    def program(self, extra, body):
        return (["000100 IDENTIFICATION DIVISION.", "000200 PROGRAM-ID. POLUPD."] + extra +
                ["000900 DATA DIVISION.", "001000 WORKING-STORAGE SECTION.", "001100 01  WS-POL.", "001200     COPY POLSEG."]
                + body + ["001300 PROCEDURE DIVISION."])

    def test_change_logs_paragraph_numbers_and_add_statements(self):
        cases = {
            "change log CR 12345": [f"00{3 + i}00*  CR {12345 + i * 1111}  JDOE  2019-01-15  CHANGE {i}" for i in range(5)],
            "change log 12345": [f"00{3 + i}00*  {12345 + i * 1111}  JDOE  2019-01-15  CHANGE {i}" for i in range(5)],
            "ADD 10000": [f"00{3 + i}00     ADD 10000 TO WS-TOTAL-{i}." for i in range(5)],
            "paragraphs 10000-INIT": [f"00{3 + i}00 {i}0000-PARA-{i}." for i in range(6)],
        }
        for label, extra in cases.items():
            text = "\n".join(self.program(extra, self.POLSEG)) + "\n"
            fmt, regions, _stats = recover.extract(text, "POLUPD.exp", {"POLSEG"})
            self.assertEqual(fmt, "columns 73-80", label)
            self.assertEqual([r.rstrip() for r in regions[0].records], [r.rstrip() for r in self.POLSEG], label)

    def test_consecutive_five_digit_sequence_numbers_are_a_source_not_a_listing(self):
        # the program's own lines numbered 00001, 00002, ... in columns 1-5, the copied lines unnumbered: the number
        # is part of the record, so this is no listing - read as one, the unnumbered copybook lines would be lost
        book = ["      " + r[6:] for r in self.POLSEG]
        lines = self.program([], [])
        numbered = [f"{k:05d} " + ln[6:] for k, ln in enumerate(lines, 1)]
        at = next(i for i, ln in enumerate(numbered) if "COPY POLSEG" in ln) + 1
        text = "\n".join(numbered[:at] + book + numbered[at:] + [f"{k:05d}     DISPLAY 'X'." for k in range(len(lines) + 1, 30)]) + "\n"
        self.assertIsNone(recover.older_layout(text.splitlines()))
        fmt, regions, _stats = recover.extract(text, "POLUPD.exp", {"POLSEG"})
        self.assertEqual(fmt, "columns 73-80")
        self.assertEqual([r.rstrip() for r in regions[0].records], [r.rstrip() for r in book])

    def test_a_remarks_line_naming_the_compiler_and_a_column_ruler_comment(self):
        body = ["      *COPY POLDCL"] + ["      " + r[6:] for r in POLDCL.splitlines()] + ["      *END COPY POLDCL"]
        prog = ["       IDENTIFICATION DIVISION.", "       PROGRAM-ID. POLUPD.", "       REMARKS.",
                "           PP 5740-CB1 OS/VS COBOL, CONVERTED TO VS COBOL II.", "       DATA DIVISION.",
                "       WORKING-STORAGE SECTION.", "       01  WS-POL."] + body + ["       PROCEDURE DIVISION.", "           GOBACK."]
        fmt, regions, _stats = recover.extract("\n".join(prog) + "\n", "POLUPD.exp", set())
        self.assertEqual(fmt, "marker comments")
        self.assertEqual(len(regions[0].records), 4)
        # the editor's column ruler kept as a comment inside a copybook
        cols = "----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7-|--"[:72] + "POLSEG"   # '*' in column 7
        book = [self.POLSEG[0], cols] + self.POLSEG[1:]
        fmt, regions, _stats = recover.extract("\n".join(self.program([], book)) + "\n", "POLUPD.exp", {"POLSEG"})
        self.assertEqual(fmt, "columns 73-80")
        # before: the ruler made the source pass for a listing, only its numbered lines were read, and the copybook
        # came out with 2 of its 5 lines - trusted, and written
        self.assertEqual([r.rstrip() for r in regions[0].records], [r.rstrip() for r in book], "every line of the copybook")
        self.assertIsNone(recover._ruler_at(cols))
        self.assertIsNotNone(recover._ruler_at(RULER), "a listing's ruler after the line number still counts")

    def test_a_numbered_procedure_copybook_inside_a_plain_source(self):
        # round two: a procedure copybook carrying its own consecutive numbers 00001.. and a comment and a literal
        # naming a division made a plain source pass for an older listing, and every copybook was lost
        errdiv = [(f"{k:05d}      " + body).ljust(72)[:72] + "ERRDIV" for k, body in enumerate(
            ["9000-ERROR-RTN."] + [f"    DISPLAY 'POLUPD STEP {k}'." for k in range(1, 18)]
            + ["    DISPLAY 'POLICY REJECTED: INVALID DIVISION CODE'.", "*   PERFORMED FROM THE PROCEDURE DIVISION",
               "    MOVE 16 TO RETURN-CODE.", "    GOBACK."], 1)]
        lines = self.program([], self.POLSEG) + ["001400     COPY ERRDIV."] + errdiv
        self.assertIsNone(recover.older_layout(lines))
        fmt, regions, _stats = recover.extract("\n".join(lines) + "\n", "POLUPD.exp", {"POLSEG", "ERRDIV"})
        self.assertEqual(fmt, "columns 73-80")
        by = {r.name: r for r in regions}
        self.assertEqual([r.rstrip() for r in by["POLSEG"].records], [r.rstrip() for r in self.POLSEG])
        self.assertEqual(len(by["ERRDIV"].records), len(errdiv))

    def test_a_numbered_print_of_a_copybook_inside_a_plain_source_is_no_listing(self):
        # a copybook kept as a numbered print (five-digit number, then its 80-column record) inside a plain
        # expanded source: its column is provable, but without a copy mark, page header or banner it is a small
        # part of the text, not a listing - read as one, the rest of the source would be lost
        printed = [f"{k:05d}   " + (f"{k * 10:06d}     05  LK-FIELD-{k:02d}          PIC X(4).").ljust(72)[:72] + "LKPRINT"
                   for k in range(1, 23)]
        printed[0] = "00001   " + "000010 LINKAGE SECTION.".ljust(72) + "LKPRINT"
        lines = self.program([], self.POLSEG) + printed
        self.assertIsNone(recover.older_layout(lines))
        fmt, regions, _stats = recover.extract("\n".join(lines) + "\n", "POLUPD.exp", {"POLSEG"})
        self.assertEqual(fmt, "columns 73-80")
        self.assertEqual([r.rstrip() for r in {r.name: r for r in regions}["POLSEG"].records], [r.rstrip() for r in self.POLSEG])

    def test_a_ruler_in_a_comment_at_any_column(self):
        for lead in ("000350*", "      * ", "      *  ", "000350*   "):
            cols = (lead + "----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7-|--")[:72] + "POLSEG"
            book = [self.POLSEG[0], cols] + self.POLSEG[1:]
            fmt, regions, _stats = recover.extract("\n".join(self.program([], book)) + "\n", "POLUPD.exp", {"POLSEG"})
            self.assertEqual(fmt, "columns 73-80", lead)
            self.assertEqual([r.rstrip() for r in regions[0].records], [r.rstrip() for r in book], lead)

    def test_a_six_digit_change_log_in_a_plain_source_keeps_every_copybook_line(self):
        # round three (and the committed version before it): five change-log comments with six-digit numbers made the
        # text a listing, the column was guessed as 1 - no room for a line number - and only the numbered copybook
        # lines were kept. A guessed column must leave room for the number; else the text is read as source
        log = [f"000{3 + i}00*  {102340 + i}  01/05/1998  CHANGE {i}" for i in range(5)]
        text = "\n".join(self.program(log, self.POLSEG)) + "\n"
        self.assertEqual(recover.reading(text.splitlines())["kind"], "source")
        fmt, regions, _stats = recover.extract(text, "POLUPD.exp", {"POLSEG"})
        self.assertEqual(fmt, "columns 73-80")
        self.assertEqual([r.rstrip() for r in regions[0].records], [r.rstrip() for r in self.POLSEG])

    def test_a_listing_only_by_the_shape_of_some_lines_falls_back_to_source(self):
        # five change-log comments with six-digit numbers look like listing lines to the shape test; with no ruler
        # and no banner, and no readable source column, the text is read as the source it is
        pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        log = [f"0000{2 + i}0*  {102340 + i}  01/05/1998  CHANGE {i}" for i in range(5)]
        prog = ["       IDENTIFICATION DIVISION.", "       PROGRAM-ID. TESTPGM.", "       DATA DIVISION.",
                "       WORKING-STORAGE SECTION.", "       01  WS-REC.", "           COPY PMASTREC.",
                "       PROCEDURE DIVISION.", "           GOBACK."]
        expanded = prog[:6] + log + pmast + prog[6:]
        fmt, regions, _stats = recover.extract("\n".join(expanded) + "\n", "TESTPGM.exp", {"PMASTREC"}, original=prog)
        self.assertEqual(fmt, "expanded source, lined up with the program")
        self.assertEqual(len(regions[0].records), len(log) + len(pmast))


class Formats(unittest.TestCase):

    def test_a_translator_listing_before_the_ruler_does_not_end_the_reading(self):
        # a job's print output often holds the CICS translator's or the precompiler's listing first, with
        # five-digit line numbers and its own headings: the current layout reads six-digit lines only
        pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        text = ibm_listing(list(PROG), {"PMASTREC": pmast, "POLDCL": POLDCL.splitlines()})
        text = text.replace("    XREF(FULL)\n", "    XREF(FULL)\n" + "\n".join(translator_front(PROG)) + "\n", 1)
        self.assertIn("Diagnostic Messages", text.split("----+-*A")[0], "the heading sits before the ruler")
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        self.assertEqual({r.name for r in regions}, {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual([r.rstrip() for r in {r.name: r for r in regions}["PMASTREC"].records], [r.rstrip() for r in pmast])

    def test_a_listing_without_a_readable_column_says_so(self):
        books = {"PMASTREC": records_of(os.path.join(FIX, "PMASTREC.cpy")), "POLDCL": POLDCL.splitlines()}
        # with its banner, no division header to guess the column from
        fmt, regions, _stats = recover.extract(ibm_listing(PROG[2:8], books, ruler=False), "TESTPGM.lst", set())
        self.assertEqual((fmt, regions), ("compiler listing without a readable source column", []))
        # no banner and no ruler, but copy marks: still a listing, and the report must not say 'no copy marks'
        prog = ["000100 ID DIVISION.", "000200 PROGRAM-ID. TESTPGM.", "000250*  CALLED BY POLDRV. THE PROCEDURE DIVISION READS IT."] + PROG[2:]
        text = "\n".join(ln for ln in ibm_listing(prog, books, ruler=False).splitlines() if "IBM Enterprise" not in ln) + "\n"
        fmt, regions, _stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual((fmt, regions), ("compiler listing without a readable source column", []))

    def test_an_unflagged_current_listing_with_a_translator_in_front_lines_up(self):
        books = {"PMASTREC": records_of(os.path.join(FIX, "PMASTREC.cpy")), "POLDCL": POLDCL.splitlines()}
        text = ibm_listing(list(PROG), books, flag="")
        text = text.replace("    XREF(FULL)\n", "    XREF(FULL)\n" + "\n".join(translator_front(PROG)) + "\n", 1)
        fmt, regions, _stats = recover.extract(text, "TESTPGM.lst", set(), original=list(PROG))
        self.assertEqual(fmt, "compiler listing, copied lines not flagged, lined up with the program")
        self.assertEqual({r.name: len(r.records) for r in regions}, {"PMASTREC": len(books["PMASTREC"]), "POLDCL": 4})

    def test_a_program_comment_naming_an_old_compiler_is_no_listing_banner(self):
        prog = ["000100* CONVERTED FROM IBM OS/VS COBOL - COMPILED WITH VS COBOL II"] + list(PROG)
        fmt, _regions, _stats = recover.extract("\n".join(prog) + "\n", "TESTPGM.exp", set())
        self.assertTrue(fmt.startswith("expanded source") or fmt == "no COPY statements", fmt)

    def setUp(self):
        self.prog = list(PROG)
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        self.poldcl = POLDCL.splitlines()

    def test_a_compiler_listing_gives_every_copied_block_back_exactly(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing")
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, [r.name for r in regions])
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl])
        self.assertFalse(by["PMASTREC"].replacing)
        self.assertTrue(by["PMASTREC"].trusted)
        self.assertEqual(stats["flagged lines"], len(self.pmast) + len(self.poldcl))

    def test_the_ruler_is_found_however_late_and_stripped_blank_lines_are_kept(self):
        pm = list(self.pmast) + ["000900                                                                SAMP0009"]
        text = ibm_listing(self.prog, {"PMASTREC": pm}, prefix_lines=900, strip=True)
        fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        r = [x for x in regions if x.name == "PMASTREC"][0]
        self.assertEqual(len(r.records), len(pm), "the blank copied record survives trailing-blank stripping")
        self.assertEqual(r.records[3].rstrip(), self.pmast[3].rstrip())

    def test_the_maps_and_cross_reference_after_the_source_do_not_spoil_the_source_column(self):
        # a real listing goes on after the source: a data division map and cross-reference tables whose
        # lines start with a line number too but are not source records - thousands of them in a big program
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        tail = ["   Data Division Map", "   Source   Hierarchy and                          Base       Hex-Displacement"]
        tail += [f"   {100 + i:06d}   {i % 3 + 1}  WS-FIELD-{i:04d} . . . . . . . . BLW=00000  {i * 8:03X}   DS 0CL55"
                 for i in range(2000)]
        tail += ["   Cross-reference of data names   References"]
        tail += [f"   {200 + i:06d}   FIELD-{i:04d} . . . . . . . . . . . {300 + i} {400 + i}" for i in range(2000)]
        text += "\n".join(tail) + "\n"
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"})
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])
        # the same listing without its ruler: the guess is checked on the source lines only
        no_ruler = "\n".join(ln for ln in text.splitlines() if "----+-*A" not in ln) + "\n"
        fmt, regions, stats = recover.extract(no_ruler, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        self.assertEqual({r.name for r in regions}, {"PMASTREC", "POLDCL"})

    def test_a_page_break_in_the_source_is_not_the_end_of_the_program(self):
        # what his photo showed: every source page starts with the banner and the ruler again, and the
        # ruler ends with the words "Map and Cross Reference"; the COPY sat on page three (line 718)
        filler = [f"{i:06d}     05  WS-FILLER-{i:03d}          PIC X(10)." for i in range(1, 121)]
        prog = self.prog[:5] + filler + self.prog[5:]
        text = ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl}, page_lines=50)
        self.assertGreater(text.count("Map and Cross Reference"), 3, "several pages")
        self.assertGreater(text.count("Options in effect"), 0)
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, stats)
        # the ASA carriage control the download kept: '0' before the first line of a page
        text2 = "\n".join(("0" + ln[1:] if ln.startswith("   000001") else ln) for ln in text.splitlines()) + "\n"
        _fmt, regions, _s = recover.extract(text2, "TESTPGM.lst", set())
        self.assertEqual({r.name for r in regions}, {"PMASTREC", "POLDCL"})

    def test_a_blank_or_comment_line_printed_inside_a_block_without_the_mark_does_not_split_it(self):
        # his listing (2026-09-21): a copybook that opens with two hundred comment lines, page headers among
        # them - and somewhere a numbered blank or comment line without the C. The block must run on to the
        # fields after it; before, that line closed the block and every copied line after it was untied
        book = ["000100*  DESCRIPTION : DIS", "000200*", "000300", "000400*", "000500 01  WS-DIS.",
                "000600     05  WS-DIS-A          PIC X(10).", "000700     05  WS-DIS-B          PIC 9(05)."]
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": book},
                           flag_at={("POLDCL", 1): " ", ("POLDCL", 2): " ", ("POLDCL", 3): " "}, page_lines=12)
        self.assertRegex(text, r"\n   \d{6}         000300\s*\n", "the blank line is printed without the mark")
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, stats)
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [book[0]] + book[4:], "the unmarked lines are not kept")
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])

    def test_a_copy_statement_after_an_exec_sql_that_never_closed_is_still_a_copy(self):
        # an EXEC SQL whose END-EXEC the tool never sees (here: on a comment line) must not swallow every
        # COPY statement after it
        prog = self.prog[:5] + ["000510     EXEC SQL", "000520*      END-EXEC"] + self.prog[5:]
        text = ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        _fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual({r.name for r in regions}, {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, stats)
        # and an INCLUDE whose copied lines are printed before its END-EXEC is tied to them
        lines = [BANNER.format(page=1), RULER,
                 "   000001         000100 IDENTIFICATION DIVISION.",
                 "   000002         000200 PROGRAM-ID. TESTPGM.",
                 "   000003         000300 DATA DIVISION.",
                 "   000004         000400 WORKING-STORAGE SECTION.",
                 "   000005         000500     EXEC SQL INCLUDE POLDCL",
                 "   000006C        000100 01  POLICY-DCL.",
                 "   000007C        000200     05  POL-NUMBER              PIC X(12).",
                 "   000008         000600     END-EXEC.",
                 "   000009         000700 PROCEDURE DIVISION."]
        regions, stats = recover.from_ibm_listing(lines, "X.lst", None)
        self.assertEqual([(r.name, len(r.records)) for r in regions], [("POLDCL", 2)], stats)

    def test_a_commented_out_copy_with_a_quoted_name_is_a_marker(self):
        # found in review: the marker-comment method bypassed copy_in and still took only a bare name
        for echo in ("000200*          COPY PMASTREC.", "000200*          COPY 'PMASTREC'.", '000200*          COPY "PMASTREC".',
                     "000200*          BEGIN COPY 'PMASTREC'"):
            records = ["000100 01  WS-REC.", echo,
                       "000300     05  PM-POLICY-NO            PIC X(12).",
                       "000400     05  PM-POLICY-STATUS        PIC X(02).",
                       "000500* END COPY 'PMASTREC'",
                       "000600 01  WS-AFTER                   PIC X."]
            regions = recover.from_markers(records, "X.exp", None)
            self.assertEqual([(r.name, len(r.records), r.end_guessed) for r in regions], [("PMASTREC", 2, False)], echo)

    def test_a_lone_carriage_control_line_before_or_after_the_copy_is_ignored(self):
        # 'in some cases the line just before the COPY statement has only a number like 1 or 0 in the first
        # column' (2026-09-21): the ASA carriage control the download kept, printed on a line of its own - and
        # a page header of another shape he saw between the copied lines: page number, program, time, date
        header = "1  17        PROGANNM        14.31.19        FEB  5,1923"
        for before, after in (("0", "0"), ("1", "1"), ("1", "0"), ("0", ""), (header, header)):
            lines = [BANNER.format(page=1), RULER,
                     "   000001         000100 IDENTIFICATION DIVISION.",
                     "   000002         000200 PROGRAM-ID. TESTPGM.",
                     "   000003         000300 DATA DIVISION.",
                     "   000004         000400 WORKING-STORAGE SECTION.",
                     before,
                     "   000005  FRAUDM        COPY 'POLDCL'.",                 # a change tag in columns 1-6, the name in quotes
                     after,
                     "   000006C        000100 01  POLICY-DCL.",
                     "0" if before != header else header,
                     "   000007C        000200     05  POL-NUMBER              PIC X(12).",
                     "   000008         000700 PROCEDURE DIVISION."]
            regions, stats = recover.from_ibm_listing(lines, "X.lst", None)
            self.assertEqual([(r.name, len(r.records)) for r in regions], [("POLDCL", 2)], (before, after, stats))
            self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, (before, after, stats))

    def test_an_untied_line_says_why_the_tool_had_no_copy_in_hand(self):
        lines = [BANNER.format(page=1), RULER,
                 "   000001         000100 IDENTIFICATION DIVISION.",
                 "   000002         000200 PROGRAM-ID. TESTPGM.",
                 "   000003         000300 DATA DIVISION.",
                 "   000004         000400 WORKING-STORAGE SECTION.",
                 "   000005                    COPY POLDCL.",
                 "   000006C        000100 01  POLICY-DCL.",
                 "   000007         000500 01  WS-X                        PIC X.",
                 "   000008C        000200     05  POL-NUMBER              PIC X(12).",
                 "   000009         000700 PROCEDURE DIVISION."]
        regions, stats = recover.from_ibm_listing(lines, "X.lst", None)
        self.assertEqual([(r.name, len(r.records)) for r in regions], [("POLDCL", 1)], stats)
        (at, before, _shape, _recent, why), = stats["untied"]
        self.assertEqual((at, before), (10, 9))
        self.assertEqual(why, "the last program line, 9, holds no COPY statement the tool recognises; the copied block "
                              "before it was closed by program line 9, shape `999999 99  AA-A                        AAA A.`")

    def test_reading_stops_where_the_program_ends(self):
        # what he found: after the source, the cross-reference tables carry line numbers too, some followed
        # by a letter, and the copybook names appear again - the tool must not read them as copied lines
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        tail = ["", "Defined   Cross-reference of data names   References"]
        tail += [f"   {100 + i:06d}D  PM-FIELD-{i:04d} . . . . . . . . . . {300 + i} {400 + i}" for i in range(40)]
        tail += ["", "Defined   Cross-reference of COPY/BASIS statements   References",
                 "   000006C  PMASTREC . . . . . . . . . . . . . . 7", "   000021C  POLDCL . . . . . . . . . . . . . . 22",
                 "", "   LineID  Message code  Message text", "   000020  IGYPS2015-I  A period was assumed."]
        text += "\n".join(tail) + "\n"
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        self.assertEqual(sorted(r.name for r in regions), ["PMASTREC", "POLDCL"])
        self.assertEqual([r.rstrip() for r in regions[0].records], [r.rstrip() for r in self.pmast])
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, stats)
        self.assertFalse(any(k.startswith("lines with an unknown flag") for k in stats), stats)
        self.assertEqual(stats["flagged lines"], len(self.pmast) + len(self.poldcl))

    def test_a_listing_without_the_ruler_still_finds_the_source_column_and_checks_it(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, ruler=False)
        fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        self.assertEqual(regions[0].records[3].rstrip(), self.pmast[3].rstrip())
        # the header one column to the right of where the guess assumes: the shape check still lands it
        shifted = [r[:7] + " " + r[7:] if "DIVISION" in r else r for r in self.prog]
        text = ibm_listing(shifted, {"PMASTREC": self.pmast}, ruler=False)
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(regions[0].records[3].rstrip(), self.pmast[3].rstrip())

    def test_an_unknown_flag_inside_a_block_makes_the_copy_suspect(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, flag_at={("PMASTREC", 5): "E"})
        fmt, regions, stats = recover.extract(text, "x.lst", set())
        r = regions[0]
        self.assertTrue(r.suspect, "one line carried a flag the tool does not know")
        self.assertEqual(stats.get("lines with an unknown flag 'E'"), 1)
        pick, how, why = recover.choose([r])
        self.assertIsNone(pick, "not written on its own")
        self.assertIn("flag 'E'", why)

    def test_ibm_out_of_sequence_asterisks_are_not_a_copy_mark_and_do_not_swallow_a_line(self):
        # IBM prints ** after the line number of a statement out of sequence - on a program line, and
        # C** on a copied one; neither may drop the line or merge two blocks
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl},
                           flag_at={("PMASTREC", 4): "C**"})
        text = text.replace("   000007         000700 01  WS-POL.", "   000007**       000700 01  WS-POL.")
        fmt, regions, stats = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast],
                         "the C** line is still a copied line")
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl],
                         "the ** program line closed the first block")
        self.assertFalse(by["PMASTREC"].suspect)
        self.assertFalse(any(k.startswith("lines with an unknown flag") for k in stats), stats)

    def test_copy_after_code_on_the_same_line_and_a_three_line_sql_include(self):
        prog = [r for r in self.prog if "COPY" not in r]
        prog[4:4] = ["000450 01  WS-REC.  COPY PMASTREC.",
                     "000460     EXEC SQL", "000470          INCLUDE POLDCL", "000480     END-EXEC."]
        text = ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        _fmt, regions, stats = recover.extract(text, "x.lst", set())
        self.assertEqual(sorted(r.name for r in regions), ["PMASTREC", "POLDCL"], stats)
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0)

    def test_a_replacing_clause_is_told_from_library_text_and_a_nested_copy_is_kept_inline(self):
        prog = [r.replace("COPY PMASTREC.", "COPY PMASTREC REPLACING ==PM-== BY ==WS-==.") for r in self.prog]
        inner = ["000100 05  PM-INNER-A            PIC X.                                 INNR0001"]
        pm = list(self.pmast[:3]) + ["000350     COPY INNERCPY.                                            SAMP0003"] + inner + list(self.pmast[3:])
        text = ibm_listing(prog, {"PMASTREC": pm, "INNERCPY": inner})
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        r = [x for x in regions if x.name == "PMASTREC"][0]
        self.assertTrue(r.replacing)
        self.assertEqual(r.nested, ["INNERCPY"])
        joined = "\n".join(r.records)
        self.assertNotIn("COPY INNERCPY", joined, "the nested COPY statement is gone, its text stays")
        self.assertIn("PM-INNER-A", joined)
        recover.resolve_replacing(r)
        self.assertFalse(r.replacing, "the listing shows PM- names, so it holds the library text: as good as any copy")
        post = [x.replace("PM-", "WS-") for x in pm]
        text = ibm_listing(prog, {"PMASTREC": post})
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        r = regions[0]
        recover.resolve_replacing(r)
        self.assertTrue(r.replacing, "the listing shows the replaced names: post-replacement text")

    def test_lined_up_with_the_program_needs_no_marks(self):
        expanded = []
        for rec in self.prog:
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                expanded.append(rec[:6] + "*" + rec[7:])                  # the expander comments the COPY out
                expanded += [f"{900000 + i:06d}{c[6:72]}" for i, c in enumerate(self.pmast)]   # and renumbers
            elif found and found[0] == "POLDCL":
                expanded.append(rec)                                      # or keeps it and adds the text after
                expanded += self.poldcl
            else:
                expanded.append(rec)
        fmt, regions, stats = recover.extract("\n".join(expanded), "TESTPGM.exp", set(), original=self.prog)
        self.assertEqual(fmt, "expanded source, lined up with the program", stats)
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"})
        self.assertEqual([r[6:72].rstrip() for r in by["PMASTREC"].records], [r[6:72].rstrip() for r in self.pmast])
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl])
        self.assertTrue(by["PMASTREC"].trusted)
        # a listing whose copied lines are not flagged: lined up too
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, flag=" ")
        fmt, regions, _s = recover.extract(text, "TESTPGM.lst", set(), original=self.prog)
        self.assertEqual(fmt, "compiler listing, copied lines not flagged, lined up with the program")
        self.assertEqual([r.name for r in regions], ["PMASTREC"])
        # an expanded text that differs elsewhere too is not a pure expansion: not trusted alone
        edited = [r.replace("MOVE 'X'", "MOVE 'Y'") for r in expanded]
        _fmt, regions, stats = recover.extract("\n".join(edited), "TESTPGM.exp", set(), original=self.prog)
        self.assertTrue(regions and not regions[0].trusted, stats)
        pick, _how, why = recover.choose([regions[0]])
        self.assertIsNone(pick)
        self.assertIn("differs from the program elsewhere", why)
        two = recover.Region("PMASTREC", regions[0].records, "OTHER.lst", "compiler listing")
        pick, how, _why = recover.choose([regions[0], two])
        self.assertIsNotNone(pick, "a second program agreeing makes it good")
        self.assertIn("identical in 2", how)

    def test_columns_73_80_expansion(self):
        recs = []
        for rec in self.prog:
            recs.append(rec.ljust(80))
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += [(c[:72].ljust(72) + "PMASTREC") for c in self.pmast]
        fmt, regions, _s = recover.extract("\n".join(recs), "TESTPGM.exp", {"PMASTREC"})
        self.assertEqual(fmt, "columns 73-80")
        self.assertEqual([r.name for r in regions], ["PMASTREC"])
        self.assertEqual([r[:72].rstrip() for r in regions[0].records], [r[:72].rstrip() for r in self.pmast])

    def test_marker_comments_are_trusted_only_with_an_end_marker(self):
        recs = []
        for rec in self.prog:
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += ["      *COPY PMASTREC"] + self.pmast + ["      *END COPY PMASTREC"]
            elif found and found[0] == "POLDCL":
                recs += ["      *++INCLUDE POLDCL"] + self.poldcl
            else:
                recs.append(rec)
        fmt, regions, _s = recover.extract("\n".join(recs), "TESTPGM.exp", set())
        self.assertEqual(fmt, "marker comments")
        by = {r.name: r for r in regions}
        self.assertTrue(by["PMASTREC"].trusted)
        self.assertFalse(by["POLDCL"].trusted, "no END marker: the end was guessed")
        self.assertIsNone(recover.choose([by["POLDCL"]])[0])
        two = recover.Region("POLDCL", by["POLDCL"].records, "OTHER.exp", "marker comments")
        self.assertIsNotNone(recover.choose([by["POLDCL"], two])[0])

    def test_no_marks_and_no_program_to_line_up_with_is_said_so(self):
        recs = []
        for rec in self.prog:
            recs.append(rec)
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += [c[:72] for c in self.pmast]
        fmt, regions, _s = recover.extract("\n".join(recs), "x.cbl", {"PMASTREC"})
        self.assertEqual(fmt, "expanded source, no copy marks and the program is not in the index to line up with")
        self.assertEqual(regions, [])
        self.assertEqual(recover.extract("hello world\nno cobol here\n", "x.txt", set())[0], "no COPY statements")

    def test_a_listing_unloaded_as_fixed_records_without_line_ends(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast})
        fixed = "".join(ln.ljust(133) for ln in text.splitlines())
        self.assertEqual(fixed.count("\n"), 0)
        _fmt, regions, _s = recover.extract(fixed, "x.lst", set())
        self.assertEqual([r.name for r in regions], ["PMASTREC"])

    def test_what_is_and_is_not_a_copybook(self):
        self.assertEqual(recover.looks_like_copybook(self.pmast), "data")
        self.assertEqual(recover.looks_like_copybook(["           MOVE A TO B.", "           PERFORM X."]), "procedure")
        self.assertEqual(recover.looks_like_copybook(["           SET WS-EOF TO TRUE.", "       9999-EXIT.", "           EXIT."]),
                         "procedure")
        self.assertEqual(recover.looks_like_copybook(["      * just a comment", "      * and another"]), "comments")
        self.assertEqual(recover.looks_like_copybook(["                              "]), "")
        shifted = [" " + r for r in self.pmast]                          # every record one column to the right
        self.assertEqual(recover.looks_like_copybook(shifted), "", "a shifted block fails the shape check")
        self.assertIn("column 7 is", recover.copybook_check(shifted)[1])
        tagged = ["FR-1.2" + r[6:] for r in self.pmast]                  # anything printable may sit in columns 1-6
        self.assertEqual(recover.looks_like_copybook(tagged), "data", "the compiler ignores columns 1-6")
        self.assertIn("data item(s)", recover.copybook_check(self.pmast)[1])
        self.assertIn("first code line looks like", recover.shape_of(self.pmast))
        self.assertNotIn("PM-POLICY", recover.shape_of(self.pmast), "nothing from the estate in the shape")

    def test_choosing_between_programs(self):
        a = recover.Region("X", self.pmast, "A.lst", "compiler listing")
        b = recover.Region("X", self.pmast, "B.lst", "compiler listing")
        c = recover.Region("X", self.pmast[:-1], "C.lst", "compiler listing")
        pick, how, _why = recover.choose([c, a, b])
        self.assertIs(pick, a)
        self.assertIn("2 different texts across 3 program(s); the most common taken (2 program(s) agree)", how)
        rep = recover.Region("X", self.pmast[:-1], "R.lst", "compiler listing", replacing=True,
                             statement="COPY X REPLACING ==ZZ-== BY ==QQ-==.")
        pick, how, _why = recover.choose([rep, c])
        self.assertIs(pick, c, "the copy under a REPLACING clause that changed the text loses")


class EndToEnd(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.src = os.path.join(self.root, "GC", "PDS.SRC")
        self.lst = os.path.join(self.root, "GC", "PDS.LISTING")
        os.makedirs(self.src)
        os.makedirs(self.lst)
        os.makedirs(os.path.join(self.root, "SHARED", "COPYLIB"))
        shutil.copy(os.path.join(FIX, "SAMPPGM.cbl"), self.src)
        shutil.copy(os.path.join(FIX, "ERRPGM.cbl"), self.src)
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        for name in ("SAMPPGM", "ERRPGM"):
            prog = records_of(os.path.join(FIX, name + ".cbl"))
            with open(os.path.join(self.lst, name + ".lst"), "w", encoding="utf-8") as fh:
                fh.write(ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": POLDCL.splitlines()}))
        # a program whose copybooks are all in the estate: its listing is not worth reading
        for fn in ("WALKPGM.cbl", "WALKREC.cpy", "WALKPROC.cpy"):
            shutil.copy(os.path.join(FIX, fn), self.src)
        with open(os.path.join(self.lst, "WALKPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write(ibm_listing(records_of(os.path.join(FIX, "WALKPGM.cbl")),
                                 {"WALKREC": records_of(os.path.join(FIX, "WALKREC.cpy"))}))
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        self.build(["--rebuild"])

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def build(self, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([self.root, "--db", self.db, "--quiet", *extra])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def status(self, name):
        conn = query.connect(self.db)
        try:
            return conn.execute("SELECT parse_status FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()[0]
        finally:
            conn.close()

    def test_missing_copybooks_are_recovered_and_the_programs_become_whole(self):
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("partial", "partial"))
        conn = query.connect(self.db)
        self.assertEqual(recover.missing_copybooks(conn), {"PMASTREC": 1, "POLDCL": 1})
        conn.close()
        said = []
        stats = recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertEqual((stats["written"], stats["missing"]), (2, 2), said)
        out = os.path.join(self.root, "SHARED", recover.FOLDER)
        self.assertFalse(os.path.exists(out), "a dry run writes nothing")
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["written"], 2, said)
        self.assertEqual(sorted(os.listdir(out)), [recover.MARKER, "PMASTREC.cpy", "POLDCL.cpy"])
        with open(os.path.join(out, "PMASTREC.cpy"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("* RECOVERED by atlas.recover", body)
        self.assertIn("(compiler listing); seen in one program", body)
        self.assertIn("PM-POLICY-STATUS", body)
        self.assertTrue(any(s.startswith("recovered: 2 of 2 missing copybooks") for s in said), said)
        self.assertTrue(any("formats seen: compiler listing in 2" in s for s in said), said)
        self.assertTrue(any(s.startswith("expanded texts to read: 2 of 3 found") and "only the 2 programs that copy" in s
                            for s in said), "the listing of a program that needs nothing is not read: " + str(said))
        self.assertTrue(any("used in 2 places" in s for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            self.assertIn("| PMASTREC | 1 |", fh.read())
        # run again before the build: nothing new, and it says what to do
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["written"], stats["kept"]), (0, 2), said)
        self.assertTrue(any("nothing new to write: 2 of 2" in s for s in said), said)
        # his k=4 (2026-09-21): recovered earlier, still missing - the report names the file and says why
        self.assertTrue(any("2 recovered on an earlier run are still missing in the index" in s for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertIn("## Recovered on an earlier run, still missing in the index", rep)
        self.assertIn("PMASTREC.cpy", rep)
        # the next build, without --rebuild, re-expands the programs that copy them
        out_text = self.build()
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("ok", "ok"), out_text)
        conn = query.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM copy_use WHERE resolved_member_id IS NULL").fetchone()[0], 0)
            self.assertIn("PM-POLICY-STATUS", query.cmd_field(conn, "PM-POLICY-STATUS"))
            self.assertIn("### Members parsed only in part\n_none_", query.cmd_coverage(conn))
        finally:
            conn.close()
        # after the build nothing is missing any more
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["missing"], 0, said)
        self.assertTrue(any("nothing to recover: every copybook" in s for s in said), said)
        # the real copybook arrives under SHARED: the build expands it at once - a recovered copy gives way to a real
        # member in SHARED (ROADMAP re-parse item 11) - with no choice among several; recover then removes the
        # recovered copy and marks nothing, for no program expands it; the next build drops it and the real one is
        # the only one
        shutil.copy(os.path.join(FIX, "PMASTREC.cpy"), os.path.join(self.root, "SHARED", "COPYLIB"))
        self.build()
        conn = query.connect(self.db)
        try:
            resolved = conn.execute("SELECT m.path FROM copy_use c JOIN member m ON m.id=c.resolved_member_id "
                                    "WHERE c.copybook='PMASTREC'").fetchone()[0]
            self.assertIn("COPYLIB", resolved)
            self.assertNotIn(recover.FOLDER, resolved)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='ambiguous_copybook'").fetchone()[0], 0)
        finally:
            conn.close()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["removed"], stats["marked"]), (1, 0), said)
        self.assertEqual(sorted(os.listdir(out)), [recover.MARKER, "POLDCL.cpy"])
        self.assertTrue(any("removed - the estate now holds the real member: PMASTREC" in s for s in said), said)
        self.assertIn("  " + recover.REMOVED_NONE_EXPANDS, said)
        self.assertFalse(any("that had expanded them" in s for s in said), said)
        self.assertIn(recover.REMOVED_NEXT, said)
        with open(self.report, encoding="utf-8") as fh:
            self.assertIn("Removed: PMASTREC. No program expands them, so none is marked; the next build drops them from "
                          "the index.", fh.read())
        self.build()
        conn = query.connect(self.db)
        try:
            self.assertEqual(self.status("SAMPPGM"), "ok")
            paths = [r[0] for r in conn.execute("SELECT path FROM member WHERE name='PMASTREC'")]
            self.assertEqual(len(paths), 1, paths)
            self.assertIn("COPYLIB", paths[0])
            resolved = conn.execute("SELECT m.path FROM copy_use c JOIN member m ON m.id=c.resolved_member_id "
                                    "WHERE c.copybook='PMASTREC'").fetchone()[0]
            self.assertIn("COPYLIB", resolved)
        finally:
            conn.close()

    def resolved_path(self, book):
        conn = query.connect(self.db)
        try:
            return conn.execute("SELECT m.path FROM copy_use c JOIN member m ON m.id=c.resolved_member_id "
                                "WHERE c.copybook=?", (book,)).fetchone()[0]
        finally:
            conn.close()

    def coverage(self):
        conn = query.connect(self.db)
        try:
            return query.cmd_coverage(conn), query._recovered_shadowing(conn)
        finally:
            conn.close()

    def real_member_arrives_late(self):
        """PMASTREC recovered and expanded, then the real member arrives in a library that sorts after
        RECOVERED-COPYBOOKS - the chain's 'first found' would keep the recovered copy."""
        recover.run(self.db, log=lambda s: None, report=self.report)
        self.build()
        self.assertIn(recover.FOLDER, self.resolved_path("PMASTREC"))
        self.assertNotIn("still expand a recovered copybook", self.coverage()[0])
        late = os.path.join(self.root, "SHARED", "ZCOPYLIB")
        os.makedirs(late)
        shutil.copy(os.path.join(FIX, "PMASTREC.cpy"), late)
        self.build()

    def test_the_real_member_beats_a_recovered_copy_the_moment_it_arrives(self):
        # ROADMAP re-parse item 11: a recovered copy gives way to a real member in SHARED, so the real member is
        # expanded at the build it arrives in, with no choice among several; coverage has nothing to warn of
        self.real_member_arrives_late()
        self.assertIn("ZCOPYLIB", self.resolved_path("PMASTREC"))
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("ok", "ok"))
        cov, shadow = self.coverage()
        self.assertEqual(shadow, "")
        self.assertNotIn("still expand a recovered copybook", cov)
        conn = query.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='ambiguous_copybook'").fetchone()[0], 0)
            self.assertNotIn("chosen among several", query.cmd_program(conn, "SAMPPGM"))
        finally:
            conn.close()
        # a dry run reads the listings and says what it would remove, from the report's first lines (LESSONS 209)
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report, dry_run=True)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertIn("; would be removed (real member arrived): 1", rep)
        self.assertNotIn("; removed (real member arrived)", rep)
        self.assertIn("expanded texts read: ", rep)
        self.assertNotIn("expanded texts read: 0;", rep)
        # recover removes the stand-in and marks nothing - no program expands it; the next build drops it
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        with open(self.report, encoding="utf-8") as fh:
            self.assertIn("; removed (real member arrived): 1", fh.read())
        self.assertEqual((stats["removed"], stats["marked"]), (1, 0), said)
        self.assertIn("  " + recover.REMOVED_NONE_EXPANDS, said)
        self.assertIn(recover.REMOVED_NEXT, said)
        self.assertNotIn("next: run your usual build command - the programs marked re-expand by themselves", said)
        self.build()
        self.assertIn("ZCOPYLIB", self.resolved_path("PMASTREC"))
        self.assertEqual(self.coverage()[1], "")

    def test_coverage_warns_while_a_recovered_copy_shadows_the_real_member_on_an_older_index(self):
        # an index built before ROADMAP re-parse item 11: the build kept the recovered copy after the real member
        # arrived (the chain's 'first found'). Written by hand - the program's COPY row pointed back at the recovered
        # member - since no build of this toolkit leaves it. Coverage warns, and recover marks that program
        self.real_member_arrives_late()
        conn = sqlite3.connect(self.db)
        try:
            rec = conn.execute("SELECT id FROM member WHERE name='PMASTREC' AND UPPER(library)=?",
                               (recover.FOLDER,)).fetchone()[0]
            conn.execute("UPDATE copy_use SET resolved_member_id=? WHERE copybook='PMASTREC'", (rec,))
            conn.commit()
        finally:
            conn.close()
        cov, shadow = self.coverage()
        self.assertIn("still expand a recovered copybook although the estate now holds the real member", cov)
        self.assertIn("PMASTREC", shadow)
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["removed"], stats["marked"]), (1, 1), said)
        self.assertIn("  1 program(s) that had expanded them are marked for the next build", said)
        self.assertIn("next: run your usual build command - the programs marked re-expand by themselves", said)
        self.assertNotIn("  " + recover.REMOVED_NONE_EXPANDS, said)
        self.assertNotIn(recover.REMOVED_NEXT, said)
        with open(self.report, encoding="utf-8") as fh:
            self.assertIn("Removed: PMASTREC. 1 program(s) that had expanded them are marked for the next build: run your "
                          "usual build command.", fh.read())
        self.assertEqual(self.status("SAMPPGM"), "pending")
        self.build()
        self.assertNotIn("still expand a recovered copybook", self.coverage()[0])
        self.assertIn("ZCOPYLIB", self.resolved_path("PMASTREC"))
        self.assertEqual(self.status("SAMPPGM"), "ok")

    def test_a_copybook_copied_only_from_inside_another_copybook_is_named_as_such(self):
        # his z=71 (2026-09-21): after the re-parse, 71 of 80 missing copybooks were 'not in any expanded text'.
        # A copybook that only another copybook copies has no COPY statement in any program; in the listing its
        # lines sit inside the outer copybook's block. The tool reads the programs that copy the OUTER copybook,
        # and the report says where the inner one's lines are
        outer = ["000100 01  OUT-REC.", "000200     05  OUT-A                 PIC X(5).", "000300     COPY 'INNER'.",
                 "000400     05  OUT-Z                 PIC X(1)."]
        inner = ["000100     05  IN-B                  PIC X(3).", "000200     05  IN-C                  PIC 9(2)."]
        with open(os.path.join(self.root, "SHARED", "COPYLIB", "OUTER.cpy"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(outer) + "\n")
        prog = ["000100 IDENTIFICATION DIVISION.", "000200 PROGRAM-ID. NESTPGM.", "000300 DATA DIVISION.",
                "000400 WORKING-STORAGE SECTION.", "000500     COPY OUTER.", "000600 PROCEDURE DIVISION.",
                "000700     MOVE 'X' TO OUT-A.", "000800     GOBACK."]
        with open(os.path.join(self.src, "NESTPGM.cbl"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(prog) + "\n")
        with open(os.path.join(self.lst, "NESTPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write(ibm_listing(prog, {"OUTER": outer[:3] + inner + outer[3:]}))     # the compiler prints INNER inline
        self.build(["--rebuild"])
        conn = query.connect(self.db)
        try:
            missing = recover.missing_copybooks(conn)
            # the expander records the nested COPY under the program too, so INNER has two copiers: the program
            # (through OUTER) and the copybook OUTER itself
            self.assertEqual(missing.get("INNER"), 2, missing)
            self.assertIn("NESTPGM", recover.needing_programs(conn, missing), "the program copying the OUTER copybook is read")
            self.assertEqual(recover.copiers_by_kind(conn, missing)["INNER"], (1, 1))
            self.assertRegex(recover.copier_names(conn, missing)["INNER"], r"^NESTPGM:\d+, OUTER \(copybook\):\d+$",
                             "each user with the line of its COPY statement, so a name that is no copybook can be looked at")
        finally:
            conn.close()
        said = []
        recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertTrue(any("1 of the 1 not found are copied from INSIDE another copybook" in s for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertRegex(rep, r"\| INNER \| 1 program, 1 copybook \| NESTPGM:\d+, OUTER \(copybook\):\d+ \| OUTER \(1 listing\) \|")

    def test_two_systems_with_different_texts_each_get_their_own_copy(self):
        test_src = os.path.join(self.root, "GC-TEST", "PDS.SRC")
        test_lst = os.path.join(self.root, "GC-TEST", "PDS.LISTING")
        os.makedirs(test_src)
        os.makedirs(test_lst)
        shutil.copy(os.path.join(FIX, "SAMPPGM.cbl"), test_src)
        newer = list(self.pmast) + ["001300 05  PM-NEW-IN-TEST         PIC X(05).                          SAMP0013"]
        with open(os.path.join(test_lst, "SAMPPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write(ibm_listing(records_of(os.path.join(FIX, "SAMPPGM.cbl")), {"PMASTREC": newer}))
        self.build()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["per_system"], 2, said)
        gc = os.path.join(self.root, "GC", recover.FOLDER, "PMASTREC.cpy")
        gct = os.path.join(self.root, "GC-TEST", recover.FOLDER, "PMASTREC.cpy")
        self.assertTrue(os.path.isfile(gc) and os.path.isfile(gct), said)
        self.assertFalse(os.path.exists(os.path.join(self.root, "SHARED", recover.FOLDER, "PMASTREC.cpy")))
        with open(gct, encoding="utf-8") as fh:
            self.assertIn("PM-NEW-IN-TEST", fh.read())
        with open(gc, encoding="utf-8") as fh:
            self.assertNotIn("PM-NEW-IN-TEST", fh.read())
        self.build()
        conn = query.connect(self.db)
        try:
            for system, want in (("GC", "GC"), ("GC-TEST", "GC-TEST")):
                resolved = conn.execute(
                    "SELECT r.path FROM copy_use c JOIN member p ON p.id=c.member_id JOIN member r ON r.id=c.resolved_member_id "
                    "WHERE p.name='SAMPPGM' AND p.system=? AND c.copybook='PMASTREC'", (system,)).fetchone()[0]
                self.assertIn(os.sep + want + os.sep, resolved, "each program expands its own system's copy")
        finally:
            conn.close()

    def test_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = recover.main(["--db", os.path.join(self.td, "no.db")])
        self.assertEqual(rc, 2)
        self.assertIn("not found", out.getvalue())
        cwd = os.getcwd()
        os.chdir(self.td)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = recover.main(["--db", self.db, "--dry-run", "--from", self.lst + " "])   # the same folder, typed with a space
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("expanded texts to read: 2 ", out.getvalue(), "the folder the index already holds is not read twice")
            self.assertIn("recovered: 2 of 2 missing copybooks (dry run - nothing written)", out.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(self.td, "work", "recover.md")))
        finally:
            os.chdir(cwd)

    def test_the_report_names_a_listing_and_a_line_to_look_at_when_copied_lines_are_untied(self):
        # a listing where the copied lines follow no COPY statement the tool can read: every copied line is untied
        text = ibm_listing(records_of(os.path.join(FIX, "SAMPPGM.cbl")), {"PMASTREC": self.pmast, "POLDCL": POLDCL.splitlines()})
        text = text.replace("COPY PMASTREC.", "CONTINUE.     ").replace("COPY POLDCL.", "CONTINUE.   ")
        odd = os.path.join(self.lst, "ODDPGM.lst")
        with open(odd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.build()
        said = []
        recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertTrue(any("could not be tied to a COPY statement - the report's 'Please look'" in s for s in said), said)
        self.assertTrue(any(s.startswith("  source column: 19 (ruler) in ") for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertIn("## Please look", rep)
        self.assertIn("ODDPGM.lst", rep)
        m = re.search(r"- line (\d+): the first copied line", rep)
        self.assertIsNotNone(m, rep)
        with open(odd, encoding="utf-8") as fh:
            file_lines = fh.read().splitlines()
        self.assertRegex(file_lines[int(m.group(1)) - 1], r"^\s*\d{6}C\s", "the line named is a copied line")
        m2 = re.search(r"- line (\d+): the last program line before it; its shape \(letters A, digits 9\): `(.*)`", rep)
        self.assertIsNotNone(m2, rep)
        self.assertIn("AAAAAAAA.", m2.group(2), "the line before the block, masked")
        self.assertNotIn("PMASTREC", m2.group(2))
        self.assertIn("the numbered lines just before it, as the tool read them", rep)
        self.assertRegex(rep, r"- why the tool had no COPY in hand: the last program line, \d+, holds no COPY statement the tool recognises")
        marks = re.findall(r"    - line \d+: mark `(.*?)`  record `(.*?)`", rep)
        self.assertGreaterEqual(len(marks), 1, rep)
        self.assertIn(("(none)", "999999     AAAAAAAA."), [(m, r.rstrip()) for m, r in marks], marks)

    def test_a_copybook_name_in_quotes_ties_the_copied_lines(self):
        # 'If the copybook after the COPY statement is in single quotes, are you handling that?' (2026-09-21) - no,
        # and that was his 40,797 untied lines: the literal mask blanked the name and no COPY was seen
        pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        text = ibm_listing(list(PROG), {"PMASTREC": pmast, "POLDCL": POLDCL.splitlines()})
        text = text.replace("COPY PMASTREC.", "COPY 'PMASTREC'.").replace("COPY POLDCL.", 'COPY "POLDCL".')
        self.assertIn("COPY 'PMASTREC'.", text)
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing", stats)
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, stats)
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in pmast])
        self.assertEqual(by["POLDCL"].statement.strip(), 'COPY "POLDCL".')
        # the keyword inside a literal is still text; an unclosed quote is not a name
        self.assertIsNone(recover.copy_in("           DISPLAY 'COPY PMASTREC FAILED'."))
        self.assertIsNone(recover.copy_in("           COPY 'PMASTREC."))
        self.assertEqual(recover.copy_in("           DISPLAY 'X' COPY 'PMASTREC'."), ("PMASTREC", "COPY 'PMASTREC'."))

    def test_trace_shows_what_the_tool_sees_in_one_listing_as_numbers_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = recover.main(["--db", self.db, "--trace", "sampPGM"])
        text = out.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertIn("program in the index: yes; copies a missing copybook: yes", text)
        self.assertIn("listing banner in the first 400 lines: yes; ruler in the first 400 lines: yes", text)
        self.assertRegex(text, r"ruler: line \d+; source column: 19")
        self.assertRegex(text, r"COPY statements found: 1 \(first at file lines \d+\); lines with a mark after the line number: \d+ \(marks: 'C' x")
        self.assertIn("result: compiler listing; blocks: 1 - PMASTREC (", text)
        self.assertRegex(text, r"block PMASTREC: missing in the index; parses as data \(\d+ data item\(s\)\); trusted")
        self.assertIn("record 1 masked: `999999*====", text)
        self.assertNotIn("PM-POLICY", text, "nothing from the estate")
        self.assertIn("first source record, masked: `999999 AAAAAAAAAAAAAA AAAAAAAA.", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = recover.main(["--db", self.db, "--trace", "NOSUCH"])
        self.assertEqual(rc, 2)
        self.assertIn("no expanded text named NOSUCH", out.getvalue())

    def test_an_older_listing_among_current_ones_is_recovered_and_traced(self):
        # SAMPPGM's listing from the older compiler, banner-less, with the translator's listing in front
        prog = records_of(os.path.join(FIX, "SAMPPGM.cbl"))
        text = ("\n".join(translator_front(prog)) + "\n"
                + osvs_listing(prog, {"PMASTREC": self.pmast, "POLDCL": POLDCL.splitlines()}, banner=False, page_lines=20))
        with open(os.path.join(self.lst, "SAMPPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write(text)
        said = []
        stats = recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertEqual(stats["written"], 2, said)
        self.assertTrue(any(s.startswith("formats seen: ") and "older compiler listing in 1" in s and "compiler listing in 1" in s
                            for s in said), said)
        self.assertTrue(any("17 (older layout, proven) in 1" in s for s in said), said)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = recover.main(["--db", self.db, "--trace", "SAMPPGM"])
        t = out.getvalue()
        self.assertEqual(rc, 0, t)
        self.assertIn("read as: an older compiler listing", t)
        self.assertRegex(t, r"older compiler listing: file lines \d+-\d+ \(\d+ numbered lines in that run; 3 numbering run\(s\) in "
                            r"the file\); source column: 17 \(sequence numbers in columns 1-6\); copy marks: 30")
        self.assertRegex(t, r"the reading ends where the numbering run ends, file line \d+ of \d+")
        self.assertNotIn("read to the end of the file", t)
        self.assertIn("result: older compiler listing; blocks: 1 - PMASTREC (", t)
        self.assertNotIn("PM-POLICY", t, "nothing from the estate")

    def test_an_older_listing_whose_column_cannot_be_proven_is_named_not_missing(self):
        # no sequence numbers and no comments: nothing is read - but the copybook is IN the listing, and the report
        # must not send him to fetch a library for it (the very symptom LESSONS 176 started from)
        prog = [blank_seq(r) for r in records_of(os.path.join(FIX, "SAMPPGM.cbl")) if r[6:7] != "*"]
        book = [blank_seq(r) for r in self.pmast if r[6:7] != "*"]
        with open(os.path.join(self.lst, "SAMPPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write(osvs_listing(prog, {"PMASTREC": book}, banner=False, page_lines=20))
        said = []
        stats = recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertEqual(stats["written"], 1, said)
        self.assertTrue(any(s.startswith("recovered: 1 of 2") and "0 in no expanded text" in s
                            and "1 in older listings whose source column could not be proven" in s for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertIn("## In an older listing whose source column could not be proven", rep)
        self.assertIn("| PMASTREC | 1 | SAMPPGM.lst | no sequence numbers in columns 1-6 and too few comment lines", rep)
        self.assertNotIn("## Not in any expanded text", rep)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            recover.main(["--db", self.db, "--trace", "SAMPPGM"])
        t = out.getvalue()
        self.assertIn("not taken as an older listing: no sequence numbers in columns 1-6 and too few comment lines", t)
        self.assertIn(f"result: {recover.UNPROVABLE}; blocks: 0", t)

    def test_the_trace_reads_a_text_as_the_reader_does(self):
        # a text only the shape of some lines calls a listing, with no column: read as source - and traced as source
        log = [f"0000{2 + i}0*  {102340 + i}  01/05/1998  CHANGE {i}" for i in range(5)]
        prog = records_of(os.path.join(FIX, "SAMPPGM.cbl"))
        at = next(i for i, r in enumerate(prog) if "COPY PMASTREC" in r) + 1
        text = [("      " + r[6:]) for r in prog[:at]] + log + ["      " + r[6:] for r in self.pmast] + ["      " + r[6:] for r in prog[at:]]
        with open(os.path.join(self.lst, "SAMPPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(text) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            recover.main(["--db", self.db, "--trace", "SAMPPGM"])
        t = out.getvalue()
        self.assertIn("read as: an expanded source", t)
        self.assertRegex(t, r"result: expanded source, lined up with the program; blocks: 1 - PMASTREC")

    def test_no_listings_and_a_relative_root_are_explained(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM member WHERE kind='listing'")
        conn.commit()
        conn.close()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["sources"], 0)
        self.assertTrue(any('--from "FOLDER"' in s for s in said), said)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE build_run SET root='estate'")
        conn.commit()
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            recover.run(self.db, log=said.append, report=self.report)
        self.assertIn("no such folder here", str(cm.exception))

    def test_a_build_run_with_a_relative_root_works_from_the_same_folder(self):
        # his way at work: `python -m atlas.supervise estate --db atlas.db` from the toolkit's folder
        cwd = os.getcwd()
        os.chdir(self.td)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(build._main(["estate", "--db", "t.db", "--rebuild", "--quiet"]), 0, buf.getvalue())
            said = []
            stats = recover.run("t.db", log=said.append, report=self.report)
            self.assertEqual(stats["written"], 2, said)
            self.assertTrue(any("recorded the estate as `estate`: found at" in s for s in said), said)
            out = os.path.join(self.root, "SHARED", recover.FOLDER)
            self.assertTrue(os.path.isfile(os.path.join(out, "PMASTREC.cpy")), said)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(build._main(["estate", "--db", "t.db", "--quiet"]), 0, buf.getvalue())
            conn = query.connect("t.db")
            try:
                self.assertEqual(conn.execute("SELECT parse_status FROM member WHERE name='SAMPPGM' AND kind='cobol'").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM copy_use WHERE resolved_member_id IS NULL").fetchone()[0], 0)
            finally:
                conn.close()
        finally:
            os.chdir(cwd)


class CopyStatementsOverLines(unittest.TestCase):
    """His 'Please look' case (LESSONS 190): a COPY whose REPLACING clause runs over several lines, with a period
    INSIDE the pseudo-text (`==XXXX-XXXX.==`) and the BY pseudo-text broken across two lines. Read as closed at
    the first period, the statement was forgotten at the next line and 61,909 copied lines were 'not tied to a
    COPY statement'."""

    def setUp(self):
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        self.poldcl = POLDCL.splitlines()

    def test_closed_only_at_a_period_outside_literals_and_pseudo_text(self):
        closed, open_ = recover._copy_closed, lambda s: not recover._copy_closed(s)
        self.assertTrue(closed("COPY PMASTREC."))
        self.assertTrue(closed("COPY 'PM.REC'."))
        self.assertTrue(closed("COPY PMASTREC REPLACING ==PM-REC.== BY ==WS-REC.==."))
        self.assertTrue(closed("COPY PMASTREC REPLACING ==PM-== BY ==WS-==.   REMARK"))
        self.assertTrue(open_("COPY PMASTREC"))
        self.assertTrue(open_("COPY PMASTREC REPLACING ==PM-REC.== BY ==WS-REC.=="))
        self.assertTrue(open_("COPY PMASTREC REPLACING ==PM-REC.== BY ==WS-"))            # the pseudo-text goes on
        self.assertTrue(open_("COPY PMASTREC REPLACING ==PM-== BY 'A.B'"))                 # a period inside a literal
        self.assertTrue(open_("COPY PMASTREC REPLACING ==PM-== BY ==WS-== ==XX.== BY"))

    def test_a_replacing_clause_over_three_lines_ties_the_copied_lines(self):
        prog = list(PROG)
        prog[5:6] = ["000600     COPY PMASTREC REPLACING ==PM-REC.== BY ==WS-REC.==",
                     "000610                             ==PM-== BY ==WS-123-ABCDE-",
                     "000620     FGHIJ-KL==."]
        text = ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        _fmt, regions, stats = recover.extract(text, "x.lst", set())
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0, stats)
        by = {r.name: r for r in regions}
        self.assertEqual(sorted(by), ["PMASTREC", "POLDCL"])
        self.assertEqual(len(by["PMASTREC"].records), len(self.pmast))
        self.assertTrue(by["PMASTREC"].replacing)
        self.assertIn("FGHIJ-KL==.", by["PMASTREC"].statement, "the whole clause was gathered")
        self.assertEqual(len(by["POLDCL"].records), len(self.poldcl), "the next COPY is untouched")


if __name__ == "__main__":
    unittest.main()
