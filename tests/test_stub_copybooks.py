r"""
A copybook member that holds only numbers is a STUB - never expanded (ROADMAP re-parse item 23, LESSONS 204).

Two of his missing copybooks hold a 7-8 digit number and nothing else. Before the item a 7-digit number in column 1
made the member `empty` (columns 1-6 are the sequence area and column 7 the indicator of fixed-format COBOL: no code),
and every program copying it said COPY X NOT FOUND; an 8-digit one (column 8 holds a digit) was a copybook `ok` with
no fields, and expanded into `01 WS-STUB-AREA.` it ran that data entry on into the PROCEDURE DIVISION and erased every
paragraph, PERFORM and reference of the program while it read `parse: ok` (the synthetic reproduction
tools/synth/repro/F08-stub-8-digits). The compiler could compile neither text, so the program was compiled against
another copy. Now a member of a kind the resolver expands whose every non-blank, non-comment line holds only digits,
whatever the columns, is filed `stub`; the resolver never expands one - a real copy of the name in any library comes
first - and with none the program is `partial` with the note 'COPY X: the member in LIBRARY holds only numbers (N
lines) - a stub, not the copybook's text; the program was compiled against another copy (its listing, or another
library, holds it)', its own facts kept. recover's disk check, coverage, `copybook` and `program` say 'a stub'; the
stand-ins of this week (the arrived / misfiled / re-file steps) find nothing to do on such an index, and on an index
built before the item they say what they said (tests/test_disk_check.py TheStubFiledEmpty, aged).

Cases: TheReader (reader.stub_count, expand.stub_note and its pattern); SevenAndEightDigits (both stubs filed `stub`,
the programs partial with the note and their own paragraphs, every report, recover twice, a dry run, an incremental
build that parses nothing); ARealCopyBeatsTheStub (the real copy in another library expanded, `COPY ... OF` too;
removed, the stub note; back, whole again); AStubArrivesChangesAndGoes (the incremental build parses the copier again
each time); TheListingHoldsTheText (recover writes the copybook from the program's listing, the build expands it over
the stub); CardsHoldingOnlyNumbers (a date card filed `stub` is still a job's cards; a copybook stub never takes a
card member's place); TheReproduction (F08's estate, in process); AnIndexBuiltBeforeTheItem (aged: no stub word);
TheDocsSayIt.
"""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from atlas import build, expand, query, reader, recover  # noqa: E402
from test_disk_check import CHECKED, NEXT_FETCH, SECTION, _Estate, age_stubs  # noqa: E402
from test_recover import ibm_listing  # noqa: E402 - the Enterprise COBOL listing generator
from test_refiled_copybooks import data_program, section_program  # noqa: E402

BOOK = "           05  BK-ID               PIC X(5).\n           05  BK-AMT              PIC 9(3).\n"
STUB7 = "1234567\n"                                                  # column 1-7: no code to the fixed-format reader
STUB8 = "".join(f"{k * 100:08d}\n" for k in range(1, 7))            # 00000100 ... 00000600: a digit in column 8
COMPILED = "the program was compiled against another copy (its listing, or another library, holds it)"


def note(book, lib, n):
    """The program's 'expand' note for the COPY, after its line number."""
    return f"COPY {book}: {body(lib, n)}"


def body(lib, n):
    """The note's sentence itself - what `program`, `copybook` and coverage print."""
    return (f"the member in {lib} holds only numbers ({n} line{'' if n == 1 else 's'}) - a stub, not the copybook's "
            f"text; {COMPILED}")


def area_program(name, book):
    """R08PGM's shape: the COPY under `01 WS-STUB-AREA.`, then a PROCEDURE DIVISION with two paragraphs and a PERFORM
    - the facts the expanded 8-digit stub erased."""
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            "       01  WS-COUNT         PIC S9(04) COMP.\n"
            "       01  WS-STUB-AREA.\n"
            f"           COPY {book}.\n"
            "       PROCEDURE DIVISION.\n"
            "       0000-MAIN.\n"
            "           PERFORM 1000-COUNT\n"
            "           GOBACK.\n"
            "       1000-COUNT.\n"
            "           ADD 1 TO WS-COUNT.\n")


def of_program(name, book, lib):
    """`COPY book OF lib.` under `01 WS-REC.`, and a MOVE to one of the copybook's fields."""
    return ("       IDENTIFICATION DIVISION.\n"
            f"       PROGRAM-ID. {name}.\n"
            "       DATA DIVISION.\n"
            "       WORKING-STORAGE SECTION.\n"
            "       01  WS-REC.\n"
            f"           COPY {book} OF {lib}.\n"
            "       PROCEDURE DIVISION.\n"
            "       0000-MAIN.\n"
            "           MOVE SPACES TO BK-ID\n"
            "           GOBACK.\n")


class _Stubs(_Estate):
    """test_disk_check's estate, with the lookups these cases need."""

    def notes(self, program):
        return [r[0] for r in self.q("SELECT u.detail FROM unresolved u JOIN member m ON m.id=u.member_id "
                                     "WHERE UPPER(m.name)=? AND u.kind='expand' ORDER BY u.id", program)]

    def member(self, name, library=None):
        rows = self.q("SELECT kind, library, parse_status FROM member WHERE UPPER(name)=?"
                      + (" AND library=?" if library else ""), name, *([library] if library else []))
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def resolved(self, program, book):
        return self.q("SELECT rm.kind, rm.library FROM copy_use c JOIN member m ON m.id=c.member_id "
                      "LEFT JOIN member rm ON rm.id=c.resolved_member_id WHERE UPPER(m.name)=? AND UPPER(c.copybook)=?",
                      program, book)

    def paragraph_names(self, program):
        return [r[0] for r in self.q("SELECT p.name FROM paragraph p JOIN program g ON g.id=p.program_id "
                                     "JOIN member m ON m.id=g.member_id WHERE UPPER(m.name)=? ORDER BY p.start_line",
                                     program)]

    def page(self, cmd, name):
        conn = query.connect(self.db)
        try:
            return cmd(conn, name)
        finally:
            conn.close()

    def coverage(self):
        conn = query.connect(self.db)
        try:
            return query.cmd_coverage(conn)
        finally:
            conn.close()

    def ids(self):
        return dict(self.q("SELECT path, id FROM member"))


class TheReader(unittest.TestCase):

    def test_stub_count(self):
        sc = reader.stub_count
        self.assertEqual(sc(STUB7), 1)                                  # within columns 1-7
        self.assertEqual(sc(STUB8), 6)                                  # a digit in column 8
        self.assertEqual(sc("1234567"), 1)                              # no line end at all
        self.assertEqual(sc("  0001  0002\n\n   \n00000300\n"), 2)       # blanks between digits, blank lines skipped
        self.assertEqual(sc(" " * 72 + "00010000\n"), 1)                 # only the sequence in columns 73-80
        self.assertEqual(sc("\t0001\n"), 1)                              # a tab is a blank
        self.assertEqual(sc("00000100\x1a"), 1)                          # an end-of-file mark
        self.assertEqual(sc("      * A STUB LEFT BY THE PROMOTION\n00000100\n000200* A REMARK\n"), 1)   # comments skipped
        self.assertEqual(sc("*> FREE-FORM REMARK\n1234567\n"), 1)
        self.assertEqual(sc("00000100\n" + BOOK), 0)                    # a real line: not a stub
        self.assertEqual(sc("000100 05 X PIC X.\n"), 0)
        self.assertEqual(sc("0001-0002\n"), 0)                          # a hyphen is not a digit
        self.assertEqual(sc("           00125.\n"), 0)                  # a literal copied into a VALUE clause ends in its period
        self.assertEqual(sc("      * COMMENTS ONLY\n"), 0)             # comments and blanks only: empty, as before
        self.assertEqual(sc(""), 0)
        self.assertEqual(sc("\n\n   \n"), 0)
        self.assertEqual(sc("0" * 80 + "1" * 80), 2)                     # 80-byte records with no line ends

    def test_the_note_and_its_pattern(self):
        one = expand.stub_note([("PROD.POL.COPYLIB", 6)])
        self.assertEqual(one, "the member in PROD.POL.COPYLIB holds only numbers (6 lines) - a stub, not the copybook's "
                              f"text; {COMPILED}")
        self.assertEqual(expand.stub_note([("A.COPYLIB", 1)]),
                         f"the member in A.COPYLIB holds only numbers (1 line) - a stub, not the copybook's text; {COMPILED}")
        self.assertEqual(expand.stub_note([("A.COPYLIB", None)]),
                         f"the member in A.COPYLIB holds only numbers - a stub, not the copybook's text; {COMPILED}")
        two = expand.stub_note([("A.COPYLIB", 1), ("B.COPYLIB", 3)])
        self.assertEqual(two, "the members in A.COPYLIB (1 line), B.COPYLIB (3 lines) hold only numbers - stubs, not the "
                              f"copybook's text; {COMPILED}")
        for text in (one, two):
            m = expand.STUB_NOTE_RE.search(f"(in COPY OUTER) L7: COPY R08STUB: {text}")
            self.assertEqual((m.group(1), m.group(2)), ("R08STUB", text))
        self.assertIsNone(expand.STUB_NOTE_RE.search("L7: COPY R08STUB NOT FOUND - fields/code from it are missing"))


class SevenAndEightDigits(_Stubs):
    """PGM7 copies STUB7 (`1234567`) on its `A-100-BEGIN SECTION.` line; PGM8 copies STUB8 (six 8-digit numbers)
    under `01 WS-STUB-AREA.` with a PROCEDURE DIVISION after it - R08PGM's shape."""

    files = (("GC/PROD.GC.SRC/PGM7.cbl", section_program("PGM7", "STUB7")),
             ("GC/PROD.GC.SRC/PGM8.cbl", area_program("PGM8", "STUB8")),
             ("SHARED/PROD.GC.COPYLIB/STUB7.txt", STUB7),
             ("SHARED/PROD.GC.COPYLIB/STUB8.txt", STUB8))

    def test_filed_stub_never_expanded_the_program_kept(self):
        self.assertEqual(self.member("STUB7"), ("stub", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.member("STUB8"), ("stub", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.status("PGM7"), "partial")
        self.assertEqual(self.status("PGM8"), "partial")
        self.assertEqual(self.notes("PGM7"), ["L11: " + note("STUB7", "PROD.GC.COPYLIB", 1)])
        self.assertEqual(self.notes("PGM8"), ["L7: " + note("STUB8", "PROD.GC.COPYLIB", 6)])
        self.assertEqual(self.resolved("PGM8", "STUB8"), [(None, None)])
        # the program's own facts: nothing of the stub ran its data entry on into the procedure division
        self.assertEqual(self.paragraph_names("PGM8"), ["0000-MAIN", "1000-COUNT"])
        self.assertEqual(self.q("SELECT e.from_para, e.to_para FROM perform_edge e JOIN program p ON p.id=e.program_id "
                                "WHERE p.program_id='PGM8'"), [("0000-MAIN", "1000-COUNT")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM field_ref r JOIN program p ON p.id=r.program_id "
                                "WHERE p.program_id='PGM8' AND r.name='WS-COUNT'"), [(1,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM src_fts WHERE member_name IN ('STUB7', 'STUB8')"), [(0,)])
        conn = query.connect(self.db)
        try:
            for name in ("PGM7", "PGM8"):
                mid = self.q("SELECT id FROM member WHERE name=?", name)[0][0]
                self.assertEqual(query.partial_kind(conn, mid), "partial")
                self.assertTrue(query.is_truly_partial(conn, mid))
            # the stand-ins of this week find nothing to do on an index this build made
            self.assertEqual(recover.arrival_scan(conn), ([], [], []))
            self.assertEqual(recover.skipped_copies(conn), {})
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
            self.assertEqual(recover.refiled_members(conn), {})
            self.assertEqual(recover.members_named(conn, "STUB8"), ([], []))
            self.assertEqual([f for _i, f, _p in recover.stubs_named(conn, "STUB8")], ["PROD.GC.COPYLIB"])
            ids = {n: self.q("SELECT id FROM member WHERE name=?", n)[0][0] for n in ("PGM7", "PGM8")}
            self.assertEqual(recover.not_found_copies(conn), {(ids["PGM7"], "STUB7"), (ids["PGM8"], "STUB8")})
            self.assertEqual(recover.stub_copies(conn), {(ids["PGM7"], "STUB7"): body("PROD.GC.COPYLIB", 1),
                                                         (ids["PGM8"], "STUB8"): body("PROD.GC.COPYLIB", 6)})
            self.assertEqual(recover.missing_copybooks(conn), {"STUB7": 1, "STUB8": 1})
        finally:
            conn.close()

    def test_recover_says_a_stub(self):
        for dry in (True, False):
            stats, said = self.recover(dry_run=dry)
            self.assertEqual((stats["missing"], stats["on_disk"], stats["no_file"], stats["arrived"], stats["misfiled"],
                              stats["refiled"], stats["waiting"], stats["marked"]), (2, 2, 0, 0, 0, 0, 0, 0), said)
            self.assertIn(CHECKED + "2 on disk (2 in the index as a stub - only numbers, not the copybook's text)", said)
            self.assertIn(recover.STUB_NEXT, said)
            for word in ("misfiled", "re-filed", "arrived since", "rename the folder", "filed empty", "columns 1-7",
                         "marked for the next build"):
                self.assertNotIn(word, said)
        rep = self.report_text()
        self.assertNotIn("## A member with the copybook's name exists but is filed as something else", rep)
        self.assertNotIn("## Re-filed as copybook", rep)
        self.assertIn("A member the build filed `stub` holds only numbers, whatever the columns - not the copybook's text, "
                      "which no compiler could compile: the program was compiled against another copy, and the build never "
                      "expands a stub.", rep)
        todo = ("never expanded: the programs copying it were compiled against another copy - this tool writes the copybook "
                "from their compiler listings (--from FOLDER), or fetch the library the listings name")
        self.assertEqual(self.disk_row("STUB7"), r"| STUB7 | 1 (PGM7:11) | yes: SHARED\PROD.GC.COPYLIB\STUB7.txt - in the "
                                                 "index as a stub | a stub: it holds only numbers (1 line), not the "
                                                 f"copybook's text | {todo} |")
        self.assertEqual(self.disk_row("STUB8"), r"| STUB8 | 1 (PGM8:7) | yes: SHARED\PROD.GC.COPYLIB\STUB8.txt - in the "
                                                 "index as a stub | a stub: it holds only numbers (6 lines), not the "
                                                 f"copybook's text | {todo} |")
        # nothing was changed: the build after it parses nothing and the programs keep their note
        ids = self.ids()
        self.build()
        self.assertEqual(self.ids(), ids)
        self.assertEqual(self.notes("PGM8"), ["L7: " + note("STUB8", "PROD.GC.COPYLIB", 6)])

    def test_coverage_says_a_stub(self):
        cov = self.coverage()
        nf = cov.split("### Copybooks not found")[1].split("\n### ")[0]
        for book, n in (("STUB7", 1), ("STUB8", 6)):
            self.assertIn(f"| {book} | 1 | yes: {body('PROD.GC.COPYLIB', n)} | in the index (the column "
                          "before says as what) |", nf)
        self.assertIn("> A copybook whose cell says `a stub` is held only as a member of numbers - no text a compiler could "
                      "compile - so the build never expands it and the programs copying it are parsed only in part: they "
                      "were compiled against another copy. `python -m atlas.recover --db atlas.db --from FOLDER` writes the "
                      "copybook from their compiler listings; or fetch the library the listings name.", nf)
        # the misfiled / arrived legend is for rows of those kinds: none here
        self.assertNotIn("A copybook marked `yes` is not missing", nf)
        part = cov.split("### Members parsed only in part")[1].split("\n### ")[0]
        self.assertIn("| cobol | PGM8 | PROD.GC.SRC | expand: L7: COPY STUB8: the member in PROD.GC.COPYLIB holds only "
                      "numbers (6 lines) - a stub,", part)
        self.assertIn("2 of the copybooks are stubs - only numbers, which the build never expands: the programs were "
                      "compiled against another copy, which `python -m atlas.recover --db atlas.db --from FOLDER` writes "
                      "from their compiler listings (or fetch the library the listings name)", part)
        for word in ("rename the folder", "re-files", "parsed before it arrived", "filed as stub"):
            self.assertNotIn(word, nf + part)
        self.assertIn("| stub | 2 | NO | only numbers - never expanded: the programs copying it were compiled against "
                      "another copy |", cov)

    def test_copybook_and_program_say_a_stub(self):
        book = self.page(query.cmd_copybook, "STUB8")
        self.assertIn("**NOT FOUND** as a copybook - " + body("PROD.GC.COPYLIB", 6) + ".\n", book)
        self.assertIn("1 program copies it and is parsed only in part: PGM8.", book)
        self.assertIn("A stub is never expanded: the programs copying it were compiled against another copy", book)
        self.assertNotIn("Looked for on disk", book)
        prog = self.page(query.cmd_program, "PGM8")
        self.assertIn("parse: partial", prog)
        self.assertIn("| STUB8 | **a stub - not expanded**: " + body("PROD.GC.COPYLIB", 6) + " |  | PGM8:7 |", prog)
        self.assertNotIn("no longer linked", prog)
        self.assertNotIn("**NOT FOUND**", prog)
        as_prog = self.page(query.cmd_program, "STUB8")
        self.assertIn("**Not a program**: indexed as a `stub` member", as_prog)
        self.assertIn("> 1 program copies it as a copybook and is parsed only in part (PGM8): " + body("PROD.GC.COPYLIB", 6)
                      + ".", as_prog)


class ARealCopyBeatsTheStub(_Stubs):
    """REALBK is a stub in PROD.GC.COPYLIB and a copybook in PROD.OTHER.COPYLIB: the real one is expanded - also for
    `COPY REALBK OF PRODGC`; with the real one gone, the stub note; back, whole again."""

    files = (("GC/PROD.GC.SRC/PGMR.cbl", data_program("PGMR", "REALBK", "BK-ID")),
             ("GC/PROD.GC.SRC/PGMOF.cbl", of_program("PGMOF", "REALBK", "PRODGC")),
             ("SHARED/PROD.GC.COPYLIB/REALBK.txt", STUB8),
             ("SHARED/PROD.OTHER.COPYLIB/REALBK.txt", BOOK))

    def test_the_real_copy_is_expanded(self):
        self.assertEqual(self.member("REALBK", "PROD.GC.COPYLIB"), ("stub", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.member("REALBK", "PROD.OTHER.COPYLIB"), ("copybook", "PROD.OTHER.COPYLIB", "ok"))
        for pg in ("PGMR", "PGMOF"):
            self.assertEqual(self.status(pg), "ok")
            self.assertEqual(self.resolved(pg, "REALBK"), [("copybook", "PROD.OTHER.COPYLIB")])
            self.assertEqual(self.notes(pg), [])
            self.assertEqual(self.q("SELECT COUNT(*) FROM unresolved u JOIN member m ON m.id=u.member_id WHERE m.name=?", pg),
                             [(0,)])                                   # no choice among several either: one candidate
            self.assertEqual(self.q("SELECT f.offset, f.length FROM pfield f JOIN program p ON p.id=f.program_id "
                                    "WHERE p.program_id=? AND f.name='BK-AMT'", pg), [(5, 3)])
        book = self.page(query.cmd_copybook, "REALBK")
        self.assertIn("> Also a stub of this name in PROD.GC.COPYLIB: it holds only numbers - the build never expands it, "
                      "and the programs below expand the copy above.", book)
        stats, said = self.recover()
        self.assertEqual(stats["missing"], 0, said)
        self.assertNotIn("stub", said)

    def test_the_real_copy_goes_and_comes_back(self):
        real = os.path.join(self.root, "SHARED", "PROD.OTHER.COPYLIB", "REALBK.txt")
        os.remove(real)
        self.build()
        for pg in ("PGMR", "PGMOF"):
            self.assertEqual(self.status(pg), "partial")
            self.assertEqual(self.resolved(pg, "REALBK"), [(None, None)])
        self.assertEqual(self.notes("PGMR"), ["L8: " + note("REALBK", "PROD.GC.COPYLIB", 6)])
        self.assertEqual(self.paragraph_names("PGMOF"), ["0000-MAIN"])
        self.write("SHARED/PROD.OTHER.COPYLIB/REALBK.txt", BOOK)
        self.build()
        for pg in ("PGMR", "PGMOF"):
            self.assertEqual(self.status(pg), "ok")
            self.assertEqual(self.resolved(pg, "REALBK"), [("copybook", "PROD.OTHER.COPYLIB")])


class AStubArrivesChangesAndGoes(_Stubs):
    """PGMS copies ARRBK, which no member carries; then a stub of it arrives, its text becomes a real copybook, and the
    file goes - each incremental build parses PGMS again and it says what the index holds (COPY_KINDS)."""

    files = (("GC/PROD.GC.SRC/PGMS.cbl", data_program("PGMS", "ARRBK", "BK-ID")),)

    def test_each_build_says_what_the_index_holds(self):
        nf = "L8: COPY ARRBK NOT FOUND - fields/code from it are missing from this program's facts"
        self.assertEqual(self.notes("PGMS"), [nf])
        self.write("SHARED/PROD.GC.COPYLIB/ARRBK.txt", STUB7 + STUB7)
        self.build()
        self.assertEqual(self.member("ARRBK"), ("stub", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.notes("PGMS"), ["L8: " + note("ARRBK", "PROD.GC.COPYLIB", 2)])
        self.assertEqual(self.status("PGMS"), "partial")
        self.write("SHARED/PROD.GC.COPYLIB/ARRBK.txt", BOOK)
        self.build()
        self.assertEqual(self.member("ARRBK"), ("copybook", "PROD.GC.COPYLIB", "ok"))
        self.assertEqual((self.status("PGMS"), self.notes("PGMS")), ("ok", []))
        self.write("SHARED/PROD.GC.COPYLIB/ARRBK.txt", STUB8)
        self.build()
        self.assertEqual((self.status("PGMS"), self.notes("PGMS")), ("partial", ["L8: " + note("ARRBK", "PROD.GC.COPYLIB", 6)]))
        os.remove(os.path.join(self.root, "SHARED", "PROD.GC.COPYLIB", "ARRBK.txt"))
        self.build()
        self.assertEqual((self.status("PGMS"), self.notes("PGMS")), ("partial", [nf]))


class TheListingHoldsTheText(_Stubs):
    """The stub is all the library holds; PGML's compiler listing carries the copybook's text, expanded under its COPY:
    recover writes it, and the next build expands it over the stub - 'its listing holds it'."""

    files = (("GC/PROD.GC.SRC/PGML.cbl", data_program("PGML", "LSTBK", "BK-ID")),
             ("SHARED/PROD.GC.COPYLIB/LSTBK.txt", STUB8),
             ("GC/PROD.GC.LISTING/PGML.lst", ibm_listing(data_program("PGML", "LSTBK", "BK-ID").splitlines(),
                                                         {"LSTBK": BOOK.splitlines()})))

    def test_recovered_then_expanded(self):
        self.assertEqual(self.status("PGML"), "partial")
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["rejected"], stats["misfiled"], stats["refiled"]), (1, 0, 0, 0), said)
        self.assertIn(CHECKED + "1 on disk (1 in the index as a stub - only numbers, not the copybook's text)", said)
        self.assertIn("recovered: 1 of 1 missing copybooks", said)
        self.build()
        self.assertEqual(self.status("PGML"), "ok")
        self.assertEqual(self.resolved("PGML", "LSTBK"), [("copybook", "RECOVERED-COPYBOOKS")])
        self.assertEqual(self.member("LSTBK", "PROD.GC.COPYLIB"), ("stub", "PROD.GC.COPYLIB", "skipped"))
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["written"], stats["marked"]), (0, 0, 0), said)


JOB9 = ("//GCJOB9   JOB (ACCT),'NIGHT',CLASS=A\n"
        "//STEP1    EXEC PGM=GCDATE\n"
        "//SYSIN    DD DSN=PROD.GC.PARMS(DATECARD),DISP=SHR\n"
        "//STEP2    EXEC PGM=SORT\n"
        "//SORTIN   DD DSN=PROD.GC.IN,DISP=SHR\n"
        "//SORTOUT  DD DSN=PROD.GC.SORTED,DISP=(NEW,CATLG)\n"
        "//SYSIN    DD DSN=PROD.GC.CNTL(SRTCARD),DISP=SHR\n")


class CardsHoldingOnlyNumbers(_Stubs):
    """A date card in a PARMS folder with no hint was filed 'unknown' and read as its job's cards; filed `stub` now, the
    job still reads it. A copybook stub named like a card member never takes that card member's place."""

    files = (("GC/PROD.GC.JCL/GCJOB9.jcl", JOB9),
             ("GC/PROD.GC.PARMS/DATECARD.txt", "20260925\n"),
             ("GC/PROD.GC.CNTL/SRTCARD.txt", "  SORT FIELDS=(1,5,CH,A)\n"),
             ("SHARED/PROD.GC.COPYLIB/SRTCARD.txt", "00000100\n"))

    def test_the_job_reads_its_cards(self):
        self.assertEqual(self.member("DATECARD"), ("stub", "PROD.GC.PARMS", "skipped"))
        self.assertEqual(self.member("SRTCARD", "PROD.GC.CNTL")[0], "ctlcard")
        self.assertEqual(self.member("SRTCARD", "PROD.GC.COPYLIB")[0], "stub")
        dds = self.q("SELECT s.step_name, d.dd_name, d.card_member, d.sysin_text FROM dd d JOIN step s ON s.id=d.step_id "
                     "JOIN job j ON j.id=s.job_id WHERE j.job_name='GCJOB9' AND d.dd_name='SYSIN' ORDER BY s.ordinal")
        self.assertEqual([(s, dd, cm) for s, dd, cm, _t in dds], [("STEP1", "SYSIN", "DATECARD"), ("STEP2", "SYSIN", "SRTCARD")])
        self.assertIn("20260925", dds[0][3] or "")
        self.assertIn("SORT FIELDS=(1,5,CH,A)", dds[1][3] or "")
        self.assertNotIn("00000100", dds[1][3] or "")


class TheReproduction(unittest.TestCase):
    """tools/synth/repro/F08-stub-8-digits, built in process: its expect.json truths hold (verify.py says FIXED)."""

    def test_f08(self):
        src = os.path.join(ROOT, "tools", "synth", "repro", "F08-stub-8-digits")
        td = tempfile.mkdtemp()
        try:
            shutil.copytree(os.path.join(src, "estate"), os.path.join(td, "estate"))
            db = os.path.join(td, "t.db")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build._main([os.path.join(td, "estate"), "--db", db, "--rebuild", "--quiet"]), 0)
            with open(os.path.join(src, "expect.json"), encoding="utf-8") as fh:
                spec = json.load(fh)
            conn = sqlite3.connect(db)
            try:
                for ck in spec["checks"]:
                    self.assertEqual(conn.execute(ck["sql"]).fetchone()[0], ck["truth"], ck["sql"])
            finally:
                conn.close()
            self.assertEqual([c["truth"] for c in spec["checks"]], ["stub", 2, "partial"])
        finally:
            shutil.rmtree(td, ignore_errors=True)


class AnIndexBuiltBeforeTheItem(_Stubs):
    """The 7-digit stub on an index built before the item (age_stubs: filed empty, the program's NOT FOUND): nothing
    says 'a stub' of the index's own - the stand-ins say what they said (tests/test_disk_check.py TheStubFiledEmpty)."""

    files = (("GC/PROD.GC.SRC/PGM7.cbl", section_program("PGM7", "STUB7")),
             ("SHARED/PROD.GC.COPYLIB/STUB7.txt", STUB7))
    aged = True

    def test_the_older_words(self):
        self.assertEqual(self.member("STUB7"), ("empty", "PROD.GC.COPYLIB", "skipped"))
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.stub_copies(conn), {})
            self.assertEqual(recover.stubs_named(conn, "STUB7"), [])
            self.assertEqual(len(recover.arrival_scan(conn)[1]), 1)             # misfiled, as the stand-in has it
        finally:
            conn.close()
        cov = self.coverage()
        self.assertNotIn("`a stub`", cov)
        self.assertNotIn("are stubs", cov)
        self.assertIn("| STUB7 | **NOT FOUND** - a member with this name exists: PROD.GC.COPYLIB, filed as empty by its "
                      "content", self.page(query.cmd_program, "PGM7"))
        stats, said = self.recover()
        self.assertIn(CHECKED + "1 on disk (1 in the index filed empty - no code lines the reader sees)", said)
        self.assertNotIn("as a stub", said)
        self.assertIn("the build of ROADMAP re-parse item 23 files such a member `stub` and never expands it",
                      self.disk_row("STUB7"))


class TheDocsSayIt(unittest.TestCase):

    def read(self, *path):
        with open(os.path.join(ROOT, *path), encoding="utf-8") as fh:
            return fh.read()

    def test_roadmap_23_is_the_stub_rule(self):
        item = self.read("ROADMAP.md").split("\n23. ")[1].split("\n24. ")[0]
        self.assertTrue(item.startswith("**Delivered in the batch - "), item[:80])
        flat = " ".join(item.split())
        self.assertIn("never expanded", flat)
        self.assertIn("tests/test_stub_copybooks.py", flat)
        self.assertNotIn("read as free format", flat)

    def test_readme_manual_and_lessons(self):
        readme = " ".join(self.read("README.md").split())
        manual = " ".join(self.read("docs", "FieldManual.html").split())
        for text in (readme, manual):
            self.assertIn("A member holding only numbers is a stub: whatever the columns its digits sit in, no compiler could "
                          "compile it", text)
            self.assertIn("'COPY X: the member in LIBRARY holds only numbers (N lines) - a stub, not the copybook's text; the "
                          f"program was compiled against another copy (its listing, or another library, holds it)'", text)
            self.assertNotIn("filed `empty` because its text sits in columns 1-7", text)
            self.assertNotIn("filed <code>empty</code> because its text sits in columns 1-7", text)
        lessons = self.read("LESSONS.md")
        row = [ln for ln in lessons.splitlines() if ln.startswith("| 204 | ")]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0].count(" | "), 4)                      # four cells: saw / why / changed / tests
        self.assertIn("test_stub_copybooks.py", row[0])


if __name__ == "__main__":
    unittest.main()
