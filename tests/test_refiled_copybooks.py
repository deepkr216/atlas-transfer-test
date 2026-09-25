r"""
A copybook the classifier filed as asm / listing / mfs by a LINE OF ITS
TEXT, in a folder whose name ends in COPYLIB (LESSONS 186).

His copybook sat in a .COPYLIB folder, yet `copybook NAME` said NOT FOUND
and `program NAME` said 'Not a program: indexed as a asm member': classify
checks _SIG_ASM before the level-number signature and before the folder
hint, and it fires on any line whose first or second word BEGINS with
START, CSECT or DSECT - `05 START-DATE PIC X(8).`, `PERFORM START-PARA.`.
Two sibling weak signatures do the same: a comment saying MODULE MAP makes
a listing, a line whose first word is MSG makes MFS source. The resolver
expands only copybook / cobol / sql / unknown, so every program copying
such a member says COPY X NOT FOUND while the file is in the estate - and
the folder fix the misfiled report gave (rename the folder, declare the
kind) changes nothing: the content decided, and a declared kind only
replaces 'unknown'. The classifier is a fact module (ROADMAP re-parse
item 22), so until the re-parse batch atlas.recover re-files such a
member as a copybook in the index and marks its programs; the build keeps
the stored kind of an unchanged, settled member, and the expander reads
the copybook's text from disk - the member's own field rows wait for the
re-parse.

Cases: (A) a data copybook with START-DATE, the whole cycle; (B) a
procedure copybook with PERFORM START-PARA on his `A-100-BEGIN SECTION.
COPY PROCBOOK.` line; (C) a MODULE MAP comment (listing) and a first word
MSG (mfs) re-filed in one run; (D) a genuine Assembler member in a COPYLIB
folder is NOT re-filed and the sentence is honest; (E) a procedure
copybook in a PROCS folder (the folder decided) keeps the rename sentence;
(F) --rebuild files it as asm again and recover re-files it again.

A round later (LESSONS 188): (G) a build that re-parses every member for
its own reasons - the manifest changed - reverts a re-file like --rebuild
does, and the docs say so after the pinned --rebuild sentence; (H) a name
carried by a re-filable COPYLIB copy AND a folder-typed PROCS copy sends
him to no folder on a dry run; (I) a re-filed copybook whose text changed
on disk leaves its program 'ok' with a NULL COPY row and the old fields -
`program` and coverage say so, and recover then the build heal it.

His real copybook (LESSONS 189): (J) the COBOL START verb on a line of its
own with KEY IS on the next read as an Assembler START to the shape guard,
so the re-file was refused; START now counts as Assembler only with a label
in column 1 or a numeric / quoted operand, and a strong signature that fires
only inside comment lines no longer refuses a re-file.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from atlas import build, query, recover  # noqa: E402
from test_recover import ibm_listing  # noqa: E402 - the Enterprise COBOL listing generator

HEAD = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {name}.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-X                PIC 9.\n       01  WS-Y                PIC 9.\n"
        "{data}"
        "       PROCEDURE DIVISION.\n       MAIN SECTION.\n{main}"
        "           PERFORM A-100-BEGIN.\n           GOBACK.\n")
TAIL = "       Z-900-END SECTION.\n           EXIT.\n"
SECTION_COPY = "       A-100-BEGIN SECTION.  COPY {book}.\n"
PLAIN_SECTION = "       A-100-BEGIN SECTION.\n           MOVE 1 TO WS-X.\n"

# a DATA copybook whose second field begins with START: _SIG_ASM reads `START` as an Assembler operation
STARTBK = ("           05  ST-ID               PIC X(5).\n"
           "           05  START-DATE          PIC X(8).\n"
           "           05  ST-AMT              PIC 9(3).\n")
# a PROCEDURE copybook (no level numbers) whose PERFORM names START-PARA - and the paragraph itself
PROCBOOK = ("       S-110-DO.\n           PERFORM START-PARA.\n       START-PARA.\n           MOVE 1 TO WS-X.\n"
            "       S-199-EXIT.\n           EXIT.\n")
# a comment naming the MODULE MAP: _SIG_LISTING
MMAPBK = ("      * OFFSETS AS IN THE MODULE MAP OF THE LOAD MODULE\n"
          "           05  MM-ID               PIC X(5).\n           05  MM-QTY              PIC 9(3).\n")
# a section named MSG: _SIG_MFS (first word MSG, then a blank)
MSGBK = "       MSG SECTION.\n       M-110-DO.\n           MOVE 2 TO WS-Y.\n"
# a REAL Assembler member: a label in column 1 before CSECT, DS with a type, no COBOL at all
ASMBK = ("ASMBK    CSECT\n         USING *,15\nSTID     DS    CL5\nSTDATE   DS    CL8\n         BR    14\n         END\n")
# a plain procedure copybook: no signature at all, the folder name types it (LESSONS 183)
PLAINBK = "       A-110-DO.\n           MOVE 1 TO WS-X.\n       A-199-EXIT.\n           EXIT.\n"
# three REAL Assembler members the first shape guard let through (LESSONS 187): a label longer than 8 characters
# before CSECT with only address constants, an unlabelled CSECT, START with a hexadecimal operand
LONGLBL = ("PLLONGLABEL CSECT\n         USING *,15\nTABLE    DC    A(TABLE)\n         DC    AL2(5)\n         BR    14\n"
           "         END\n")
NOLABEL = "         CSECT\n         USING *,12\n         LA    1,4(0,1)\n         BR    14\n         END\n"
HEXSTART = "PLHEX    START X'100'\n         LA    1,4\n         END\n"

ITEM = "ROADMAP re-parse item 22"
CONTENT = "filed as asm by its content"
SEEN = ("line 2 `START-DATE` reads as Assembler - a first or second word beginning with START, CSECT or DSECT, checked "
        "before the level numbers and the folder name; " + ITEM)
ASM_SEEN = ("reads as Assembler - a first or second word beginning with START, CSECT or DSECT, checked before the level "
            "numbers and the folder name; " + ITEM)
RECOVER_FIX = ("no folder change helps (the content decided): run `python -m atlas.recover --db atlas.db`, which re-files "
               "it as a copybook in the index, then the build")
MISFILED_FIX = ("the folder name decided the kind (a copybook with no level numbers has no signature): rename the folder to "
                "end in COPYLIB, or declare the library's kind in the UI's table (the manifest kinds) and run the build")
MISFILED_CELL = "not a kind the build expands: rename the folder to end in COPYLIB or declare its kind in the UI, then build"


def data_program(name, book, ref=None):
    """`01 WS-REC. COPY book.` - and a MOVE to `ref`, one of the copybook's fields, so the build keeps
    WS-REC's children in pfield (an unreferenced root is stored without them)."""
    return HEAD.format(name=name, data=f"       01  WS-REC.\n           COPY {book}.\n",
                       main=f"           MOVE SPACES TO {ref}.\n" if ref else "") + PLAIN_SECTION + TAIL


def section_program(name, book):
    return HEAD.format(name=name, data="", main="") + SECTION_COPY.format(book=book) + TAIL


class _Estate(unittest.TestCase):
    """estate\\GC\\PROD.GC.SRC\\<program>.cbl and estate\\SHARED\\PROD.GC.COPYLIB\\<copybook>.txt,
    built together with --rebuild (the copybook is in the estate from the first build: nothing arrived)."""

    files = ()
    manifest = None                     # a manifest.json path: every build runs with --manifest (DeclaredKindDecided)

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        for rel, text in self.files:
            self.write(rel, text)
        self.before_build()
        self.build(["--rebuild"])

    def before_build(self):
        """A hook for what the first build needs on disk besides the estate (a manifest)."""

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def build(self, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([self.root, "--db", self.db, "--quiet", *extra,
                              *(["--manifest", self.manifest] if self.manifest else [])])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def recover(self, **kw):
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report, **kw)
        return stats, "\n".join(said)

    def report_text(self):
        with open(self.report, encoding="utf-8") as fh:
            return fh.read()

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def member(self, name):
        """(kind, folder, parse_status, parse_error) - one row expected."""
        rows = self.q("SELECT kind, library, parse_status, parse_error FROM member WHERE UPPER(name)=?", name)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def status(self, name):
        return self.q("SELECT parse_status FROM member WHERE UPPER(name)=? AND kind='cobol'", name)[0][0]

    def member_id(self, name):
        return self.q("SELECT id FROM member WHERE UPPER(name)=?", name)[0][0]

    def copy_use(self, program, book):
        return self.q("SELECT c.resolved_member_id FROM copy_use c JOIN member m ON m.id=c.member_id "
                      "WHERE UPPER(m.name)=? AND UPPER(c.copybook)=? ORDER BY c.line", program, book)

    def not_found_note(self, program, book):
        return self.q("SELECT detail FROM unresolved u JOIN member m ON m.id=u.member_id WHERE UPPER(m.name)=? "
                      "AND u.kind='expand' AND detail LIKE ?", program, f"%COPY {book} NOT FOUND%")

    def paragraphs(self, program):
        return self.q("SELECT p.name, p.kind, p.section FROM paragraph p JOIN program g ON g.id=p.program_id "
                      "JOIN member m ON m.id=g.member_id WHERE UPPER(m.name)=? ORDER BY p.start_line", program)

    def outputs(self, program, book):
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            return (cov, cov.split("### Copybooks not found")[1].split("\n###")[0],
                    query.cmd_program(conn, program), query.cmd_copybook(conn, book), query.cmd_program(conn, book))
        finally:
            conn.close()

    def assert_the_bug(self, program, book, kind, folder="PROD.GC.COPYLIB"):
        """The build filed the copybook as `kind` and the program says NOT FOUND although the file is there."""
        self.assertEqual(self.member(book)[:2], (kind, folder))
        self.assertEqual(self.status(program), "partial")
        self.assertEqual(self.copy_use(program, book), [(None,)])
        self.assertEqual(len(self.not_found_note(program, book)), 1)


STARTVERB = ("       S-100-POSITION.\n           MOVE WS-KEY-IN TO CUST-KEY.\n           START CUSTFILE\n"
             "              KEY IS >= CUST-KEY\n              INVALID KEY MOVE 'N' TO WS-FOUND\n           END-START.\n"
             "       S-199-EXIT.\n           EXIT.\n")


class ProcedureCopybookWithTheStartVerb(_Estate):
    """Case J (LESSONS 189): his procedure copybook - the COBOL START verb on a line of its own, its KEY IS
    clause on the next - copied on the `A-100-BEGIN SECTION.  COPY STARTVB.` line. The classifier files it
    asm; the first shape guard refused the re-file as 'the shape of an Assembler member'; now it is re-filed
    and the next build gives the program its section and the copybook's paragraphs."""

    files = (("GC/PROD.GC.SRC/SVPGM.cbl", section_program("SVPGM", "STARTVB")),
             ("SHARED/PROD.GC.COPYLIB/STARTVB.txt", STARTVERB))

    def test_re_filed_and_expanded(self):
        self.assert_the_bug("SVPGM", "STARTVB", "asm")
        stats, said = self.recover()
        self.assertEqual((stats["misfiled"], stats["refiled"], stats["marked"]), (0, 1, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index", said)
        self.assertNotIn("shape of an Assembler member", self.report_text())
        self.assertEqual(self.member("STARTVB")[:3], ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.build()
        self.assertEqual(self.status("SVPGM"), "ok")
        self.assertEqual(self.copy_use("SVPGM", "STARTVB"), [(self.member_id("STARTVB"),)])
        names = [(p[0], p[1]) for p in self.paragraphs("SVPGM")]
        self.assertIn(("A-100-BEGIN", "section"), names)
        self.assertIn(("S-100-POSITION", "paragraph"), names)
        self.assertIn(("S-199-EXIT", "paragraph"), names)
        cov, nf, _prog, _book, _as_prog = self.outputs("SVPGM", "STARTVB")
        self.assertNotIn("STARTVB", nf)
        stats, said = self.recover()
        self.assertEqual((stats["misfiled"], stats["refiled"], stats["marked"]), (0, 0, 0), said)


class WrittenThoughTheClassifierMisreadsIt(_Estate):
    """Case K (LESSONS 191): the copybook's library copy on disk is a bare number (the fetched member holds
    no COBOL: kind 'empty'), the program's compiler listing in the estate carries the copybook expanded
    under the `A-100-BEGIN SECTION.  COPY STARTVB.` line, and the text holds the COBOL START verb the
    classifier reads as Assembler. recover writes it all the same, says the build will misread it, the
    build files it asm, recover re-files it and marks the program, the build makes the program whole."""

    files = (("GC/PROD.GC.SRC/SVPGM.cbl", section_program("SVPGM", "STARTVB")),
             ("SHARED/PROD.GC.COPYLIB/STARTVB.txt", "1234567\n"),
             ("GC/PROD.GC.LISTING/SVPGM.lst", ibm_listing(section_program("SVPGM", "STARTVB").splitlines(),
                                                          {"STARTVB": STARTVERB.splitlines()})))

    def test_the_whole_cycle(self):
        # the bug: the member on disk is empty of code, the program says NOT FOUND, and the listing is not used
        self.assertEqual(self.member("STARTVB")[:3], ("empty", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.status("SVPGM"), "partial")
        # 1. recover writes the copybook from the listing, and says what the build will make of it
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["rejected"], stats["misread"], stats["refiled"]), (1, 0, 1, 0), said)
        self.assertIn("recovered: 1 of 1 missing copybooks", said)
        self.assertIn("  1 of them carries a line the build's classifier misreads (asm): after the build, run this tool once "
                      "more - it re-files them in the index - then build again (ROADMAP re-parse item 22)", said)
        self.assertIn("next: run your usual build command", said)
        rep = self.report_text()
        self.assertIn("## Written", rep)
        self.assertIn("the build's classifier reads it as asm by a line of its text (ROADMAP re-parse item 22): run this tool "
                      "again after the build - it re-files it - then build once more", rep)
        self.assertNotIn("the build would file this as asm", rep)
        recovered = os.path.join(self.root, "SHARED", "RECOVERED-COPYBOOKS", "STARTVB.cpy")
        self.assertTrue(os.path.isfile(recovered))
        with open(os.path.join(self.root, "SHARED", "RECOVERED-COPYBOOKS", recover.MARKER), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["copybooks"]["STARTVB"]["misread_as"], "asm")
        # 2. the build files the recovered copy as asm (item 22): the program is still partial
        self.build()
        kinds = sorted(k for (k,) in self.q("SELECT kind FROM member WHERE UPPER(name)='STARTVB'"))
        self.assertEqual(kinds, ["asm", "empty"])
        self.assertEqual(self.status("SVPGM"), "partial")
        # 3. recover re-files it, marks the program, and does not call it 'recovered earlier, still missing'
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["refiled"], stats["marked"]), (0, 1, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index", said)
        self.assertNotIn("recovered on an earlier run are still missing", said)
        self.assertNotIn("## Recovered on an earlier run, still missing in the index", self.report_text())
        self.assertEqual(self.status("SVPGM"), "pending")
        # 4. the build makes the program whole: the section and the copybook's paragraphs are its own
        self.build()
        self.assertEqual(self.status("SVPGM"), "ok")
        names = [(p[0], p[1]) for p in self.paragraphs("SVPGM")]
        self.assertIn(("A-100-BEGIN", "section"), names)
        self.assertIn(("S-100-POSITION", "paragraph"), names)
        resolved = self.copy_use("SVPGM", "STARTVB")[0][0]
        self.assertEqual(self.q("SELECT kind, library FROM member WHERE id=?", resolved), [("copybook", "RECOVERED-COPYBOOKS")])
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["refiled"], stats["marked"], stats["misread"]), (0, 0, 0, 0), said)

    def test_a_real_assembler_block_is_still_refused(self):
        # the listing carries an Assembler control section where the copybook should be: not written
        self.write("GC/PROD.GC.LISTING/SVPGM.lst", ibm_listing(section_program("SVPGM", "STARTVB").splitlines(),
                                                                {"STARTVB": ASMBK.splitlines()}))
        self.build()
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["rejected"], stats["misread"]), (0, 1, 0), said)
        rep = self.report_text()
        self.assertIn("## Rejected", rep)
        self.assertIn("| STARTVB |", rep.split("## Rejected")[1].split("\n## ")[0])
        self.assertFalse(os.path.exists(os.path.join(self.root, "SHARED", "RECOVERED-COPYBOOKS", "STARTVB.cpy")))


class DataCopybookWithStartDate(_Estate):
    """Case A: `05 START-DATE PIC X(8).` in estate\\SHARED\\PROD.GC.COPYLIB\\STARTBK.txt, copied
    by STPGM under `01 WS-REC.` - the whole cycle, dry run, run, build, run again."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK))

    def test_the_build_files_it_as_asm_and_every_report_says_by_its_content(self):
        # THE BUG (ROADMAP re-parse item 22), documented: a data copybook in a COPYLIB folder is an asm member
        self.assert_the_bug("STPGM", "STARTBK", "asm")
        cov, nf, prog, book, as_prog = self.outputs("STPGM", "STARTBK")
        cell = f"| STARTBK | 1 | yes: PROD.GC.COPYLIB, {CONTENT} ({SEEN}) - not a kind the build expands, and {RECOVER_FIX} |"
        self.assertIn(cell, nf)
        self.assertNotIn("rename the folder", nf.split("| STARTBK |")[1].split("\n")[0], "a folder fix for a content case")
        self.assertIn("Where the cell says `by its content`", cov)
        self.assertIn(f"1 exists only as a member the classifier typed by a line of its text (asm - a START- name, a MODULE "
                      f"MAP comment, a first word MSG; {ITEM}): no folder change helps - `python -m atlas.recover --db "
                      "atlas.db` re-files it as a copybook in the index, then build", cov)
        self.assertNotIn("of a kind the build does not expand (the folder name decided it)", cov)
        self.assertIn(f"| STARTBK | **NOT FOUND** - a member with this name exists: PROD.GC.COPYLIB, {CONTENT} ({SEEN}) - not a "
                      f"kind the build expands, and {RECOVER_FIX} |", prog)
        self.assertIn(f"**NOT FOUND** as a copybook - a member with this name exists, {CONTENT} (folder PROD.GC.COPYLIB; {SEEN}): "
                      "not a kind the build expands, so every program that copies it is parsed only in part. No folder change "
                      "helps (the content decided): run `python -m atlas.recover --db atlas.db`, which re-files it as a copybook "
                      "in the index, then the build.", book)
        self.assertNotIn("The folder name decided the kind", book)
        # `program STARTBK`: not a program, and what it is to the programs that copy it
        self.assertIn("**Not a program**: indexed as a `asm` member", as_prog)
        self.assertIn(f"> 1 program copies it as a copybook and says `COPY STARTBK NOT FOUND` (STPGM): in PROD.GC.COPYLIB it is "
                      f"{CONTENT} ({SEEN}) - {RECOVER_FIX}.", as_prog)

    def test_dry_run_re_files_nothing_and_says_what_it_would_do(self):
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["refiled"], stats["marked"]), (0, 1, 0, 0), said)
        self.assertIn("  1 misfiled copybook(s) would be re-filed as copybook in the index (the classifier had read them as asm "
                      "by a line of their text): 1 program(s) would be marked for the next build (dry run: nothing changed)", said)
        self.assertIn("missing copybooks in the index: 1", said)
        self.assertIn("every one of them is the name of a member this run would re-file as a copybook (above): run without "
                      "--dry-run first - the listings only if a program still says NOT FOUND after the build", said)
        self.assertNotIn("next: run again as", said)
        self.assertNotIn("next: rename the folder", said)
        self.assertNotIn("next: run your usual build command", said)
        self.assertNotIn("does not expand", said)
        self.assertEqual(self.member("STARTBK")[:3], ("asm", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.status("STPGM"), "partial")
        rep = self.report_text()
        self.assertIn("## A member with the copybook's name exists but is filed as something else", rep)
        self.assertIn("| copybook | filed as | folder | programs | what to do | why that kind |", rep)
        self.assertIn("| STARTBK | asm | PROD.GC.COPYLIB | STPGM | no folder change helps (the content decided): a run without "
                      f"--dry-run re-files it as a copybook in the index and marks the programs | {CONTENT} ({SEEN}) |", rep)
        self.assertIn("## Re-filed as copybook", rep)
        self.assertIn("a run without --dry-run sets their kind to copybook in the index and marks the programs that copy them "
                      "(dry run: nothing changed)", rep)
        self.assertIn(f"| STARTBK | asm | {SEEN} | PROD.GC.COPYLIB | STPGM |", rep)

    def test_the_run_re_files_it_and_the_next_build_makes_the_program_whole(self):
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["refiled"], stats["marked"]), (0, 0, 1, 1), said)
        self.assertIn("  1 misfiled copybook(s) re-filed as copybook in the index (the classifier had read them as asm by a "
                      "line of their text): 1 program(s) marked for the next build", said)
        self.assertIn("nothing to recover: every copybook the programs copy is in the index", said)
        self.assertIn("next: run your usual build command", said)
        self.assertNotIn("next: rename the folder", said)
        self.assertNotIn("does not expand", said)
        kind, folder, status, error = self.member("STARTBK")
        self.assertEqual((kind, folder, status), ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.assertTrue(error.startswith(recover.REFILED_MARK + " on 20"), error)
        self.assertTrue(error.endswith(f": the classifier filed it as asm by its content ({ITEM}); the programs copying it were "
                                       "marked"), error)
        self.assertEqual(self.status("STPGM"), "pending")
        self.assertEqual(self.q("SELECT COUNT(*) FROM unresolved u JOIN member m ON m.id=u.member_id WHERE m.name='STARTBK'"),
                         [(0,)])
        rep = self.report_text()
        self.assertIn("## Re-filed as copybook", rep)
        self.assertIn("| copybook | had been filed as | why (the signature that fired, in words) | folder | programs |", rep)
        self.assertIn(f"| STARTBK | asm | {SEEN} | PROD.GC.COPYLIB | STPGM |", rep)
        self.assertIn("this run set their kind to copybook in the index and marked the programs that copy them", rep)
        self.assertIn("Their own layout rows - fields, offsets - stay absent until the next full re-parse", rep)
        self.assertNotIn("filed as something else", rep)
        # before the build: every report says re-filed, none says NOT FOUND as a copybook
        cov, nf, prog, book, as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("| STARTBK | 1 | yes: PROD.GC.COPYLIB (copybook, re-filed by atlas.recover) - the programs copying it are "
                      "marked: run your usual build command |", nf)
        self.assertIn("- 1 copybook marked `skipped` was re-filed by atlas.recover (the classifier had read it as another kind "
                      f"by a line of its text - {ITEM}): not a gap in the parse", cov)
        self.assertIn("a member with this name exists: PROD.GC.COPYLIB (copybook, re-filed by atlas.recover) - the programs "
                      "copying it are marked: run your usual build command |", prog)
        self.assertIn("**Indexed as a copybook (re-filed by atlas.recover; its own layout rows arrive with the next full "
                      "re-parse)**: " + error + ".", book)
        self.assertNotIn("NOT FOUND** as a copybook", book)
        self.assertIn("> 1 of the programs above still says `COPY STARTBK NOT FOUND` (under 'Unresolved in scope' below): parsed "
                      "while this member was filed as `asm` by a line of its text; atlas.recover re-filed it and marked them - "
                      "run your usual build command.", book)
        self.assertIn("**Not a program**: indexed as a `copybook` member", as_prog)
        self.assertIn("> Indexed as a copybook (re-filed by atlas.recover; its own layout rows arrive with the next full "
                      "re-parse): " + error + ".", as_prog)
        # the next INCREMENTAL build: the member row is kept as it is (build.py keeps the stored kind of an unchanged,
        # settled member), the program is parsed again and expands the copybook's text from disk
        self.build()
        self.assertEqual(self.member("STARTBK")[:3], ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.member("STARTBK")[3], error)
        self.assertEqual(self.status("STPGM"), "ok")
        mid = self.member_id("STARTBK")
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(mid,)])
        self.assertEqual(self.not_found_note("STPGM", "STARTBK"), [])
        rows = self.q("SELECT f.name, f.offset, f.length, f.src_member FROM pfield f JOIN program p ON p.id=f.program_id "
                      "JOIN member m ON m.id=p.member_id WHERE m.name='STPGM' AND f.name IN ('ST-ID','START-DATE','ST-AMT') "
                      "ORDER BY f.offset")
        self.assertEqual(rows, [("ST-ID", 0, 5, mid), ("START-DATE", 5, 8, mid), ("ST-AMT", 13, 3, mid)])
        self.assertEqual(self.q("SELECT name, length FROM pfield WHERE name='WS-REC'"), [("WS-REC", 16)])
        # the copybook's OWN rows wait for the re-parse: said, not hidden
        self.assertEqual(self.q("SELECT COUNT(*) FROM field WHERE member_id=?", mid), [(0,)])
        cov, nf, prog, book, as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("_none_", nf)
        self.assertIn("### Members parsed only in part\n_none_", cov)
        self.assertNotIn("NOT FOUND", prog)
        self.assertNotIn("still say", book)
        self.assertIn("its own layout rows arrive with the next full re-parse", book)
        self.assertIn("- 1 copybook marked `skipped` was re-filed by atlas.recover", cov)
        # a further run has nothing to say about it
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["refiled"], stats["marked"]), (0, 0, 0, 0), said)
        self.assertNotIn("STARTBK", said)
        self.assertNotIn("re-filed", said)
        self.assertIn("Nothing to report on this run", self.report_text())

    def test_the_index_side_helpers(self):
        conn = query.connect(self.db)
        try:
            accepted, other = recover.members_named(conn, "startbk")
            self.assertEqual(accepted, [])
            self.assertEqual([(k, f) for _i, k, f, _p in other], [("asm", "PROD.GC.COPYLIB")])
            r = recover.member_readings(other)[0]
            self.assertEqual((r["kind"], r["by"], r["line"], r["word"], r["refile"], r["why_not"]),
                             ("asm", "content", 2, "START-DATE", True, ""))
            self.assertEqual(r["seen"], SEEN)
            self.assertEqual(recover.filed_phrase(r), f"{CONTENT} ({SEEN})")
            self.assertEqual(recover.content_fix(r), RECOVER_FIX)
            arrived, misfiled = recover.arrived_copybooks(conn)
            self.assertEqual(arrived, [])
            self.assertEqual(len(misfiled), 1)
            recover.read_misfiled(misfiled)
            self.assertFalse(recover.folder_decided(misfiled[0]))
            self.assertEqual(recover.misfiled_cells(misfiled[0]), (RECOVER_FIX, f"{CONTENT} ({SEEN})"))
            self.assertEqual(recover.refiled_members(conn), {})
            self.assertEqual(recover.missing_copybooks(conn), {"STARTBK": 1}, "missing to the resolver: no accepted member")
        finally:
            conn.close()
        self.recover()
        conn = query.connect(self.db)
        try:
            refiled = recover.refiled_members(conn)
            self.assertEqual(list(refiled), [self.member_id("STARTBK")])
            self.assertEqual(recover.refiled_kind_before(refiled[self.member_id("STARTBK")]), "asm")
            self.assertEqual(recover.arrived_copybooks(conn)[1], [], "re-filed: not misfiled again")
            self.assertEqual(recover.missing_copybooks(conn), {})
        finally:
            conn.close()


class ProcedureCopybookWithStartPara(_Estate):
    """Case B: PROCBOOK - paragraphs and statements, no level numbers - holds `PERFORM START-PARA` and the
    paragraph START-PARA, copied on his `A-100-BEGIN SECTION.  COPY PROCBOOK.` line; the COPYLIB folder is
    what lets this tool re-file it (no level numbers to go by)."""

    files = (("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PROCBOOK")),
             ("SHARED/PROD.GC.COPYLIB/PROCBOOK.txt", PROCBOOK))

    def test_re_filed_and_the_section_holds_the_copybook_paragraphs_after_the_build(self):
        self.assert_the_bug("SECPGM", "PROCBOOK", "asm")
        _cov, nf, _prog, book, _as_prog = self.outputs("SECPGM", "PROCBOOK")
        self.assertIn("| PROCBOOK | 1 | yes: PROD.GC.COPYLIB, filed as asm by its content (line 2 `START-PARA` reads as Assembler", nf)
        self.assertIn("filed as asm by its content (folder PROD.GC.COPYLIB; line 2 `START-PARA` reads as Assembler", book)
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index (the classifier had read them as asm by a line "
                      "of their text): 1 program(s) marked for the next build", said)
        self.assertEqual(self.member("PROCBOOK")[:3], ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.assertIn("| PROCBOOK | asm | line 2 `START-PARA` reads as Assembler", self.report_text())
        self.build()
        self.assertEqual(self.status("SECPGM"), "ok")
        self.assertEqual(self.copy_use("SECPGM", "PROCBOOK"), [(self.member_id("PROCBOOK"),)])
        paras = [(p[0], p[1], p[2]) for p in self.paragraphs("SECPGM")]
        self.assertIn(("A-100-BEGIN", "section", "A-100-BEGIN"), paras)
        self.assertIn(("S-110-DO", "paragraph", "A-100-BEGIN"), paras)
        self.assertIn(("START-PARA", "paragraph", "A-100-BEGIN"), paras)
        self.assertIn(("S-199-EXIT", "paragraph", "A-100-BEGIN"), paras)
        _cov, nf, prog, book, _as_prog = self.outputs("SECPGM", "PROCBOOK")
        self.assertIn("_none_", nf)
        self.assertNotIn("NOT FOUND", prog + book)


class ListingAndMfsSignatures(_Estate):
    """Case C: a comment naming the MODULE MAP files a data copybook as a compiler listing; a section
    named MSG files a procedure copybook as MFS source - both re-filed in one run, and the rows the MFS
    parser wrote for the second go with the re-file."""

    files = (("GC/PROD.GC.SRC/MMPGM.cbl", data_program("MMPGM", "MMAPBK", "MM-ID")),
             ("GC/PROD.GC.SRC/MSGPGM.cbl", section_program("MSGPGM", "MSGBK")),
             ("SHARED/PROD.GC.COPYLIB/MMAPBK.txt", MMAPBK),
             ("SHARED/PROD.GC.COPYLIB/MSGBK.txt", MSGBK))

    def test_both_re_filed_in_one_run(self):
        self.assert_the_bug("MMPGM", "MMAPBK", "listing")
        self.assert_the_bug("MSGPGM", "MSGBK", "mfs")
        mfs_id = self.member_id("MSGBK")
        self.assertGreaterEqual(self.q("SELECT COUNT(*) FROM unresolved WHERE member_id=?", mfs_id)[0][0], 0)
        cov, nf, _prog, _book, _as_prog = self.outputs("MMPGM", "MMAPBK")
        self.assertIn("| MMAPBK | 1 | yes: PROD.GC.COPYLIB, filed as listing by its content (line 1 `MODULE MAP` reads as a "
                      "compiler listing - a comment naming MODULE MAP or CROSS REFERENCE TABLE, checked before the level numbers "
                      f"and the folder name; {ITEM}) - not a kind the build expands, and {RECOVER_FIX} |", nf)
        self.assertIn("| MSGBK | 1 | yes: PROD.GC.COPYLIB, filed as mfs by its content (line 1 `MSG` reads as an MFS statement - "
                      "a line whose first word is MSG, FMT, DEV, DFLD or MFLD, checked before the level numbers and the folder "
                      f"name; {ITEM}) - not a kind the build expands, and {RECOVER_FIX} |", nf)
        self.assertIn("2 exist only as a member the classifier typed by a line of its text (listing, mfs - ", cov)
        self.assertIn("re-files them as a copybook in the index, then build", cov)
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (2, 0, 2), said)
        self.assertIn("  2 misfiled copybook(s) re-filed as copybook in the index (the classifier had read them as listing/mfs by "
                      "a line of their text): 2 program(s) marked for the next build", said)
        self.assertEqual(self.member("MMAPBK")[:3], ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.member("MSGBK")[:3], ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.assertIn("the classifier filed it as listing by its content", self.member("MMAPBK")[3])
        self.assertIn("the classifier filed it as mfs by its content", self.member("MSGBK")[3])
        # the MFS parser's rows for MSGBK (a 'no macros' note, or a screen) are void for a copybook
        self.assertEqual(self.q("SELECT COUNT(*) FROM unresolved WHERE member_id=?", mfs_id), [(0,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM screen WHERE member_id=?", mfs_id), [(0,)])
        rep = self.report_text()
        self.assertIn("| MMAPBK | listing | line 1 `MODULE MAP` reads as a compiler listing", rep)
        self.assertIn("| MSGBK | mfs | line 1 `MSG` reads as an MFS statement", rep)
        self.build()
        self.assertEqual((self.status("MMPGM"), self.status("MSGPGM")), ("ok", "ok"))
        self.assertEqual(self.q("SELECT name, offset, length FROM pfield WHERE name IN ('MM-ID','MM-QTY') ORDER BY offset"),
                         [("MM-ID", 0, 5), ("MM-QTY", 5, 3)])
        paras = [(p[0], p[1], p[2]) for p in self.paragraphs("MSGPGM")]
        self.assertIn(("MSG", "section", "MSG"), paras)
        self.assertIn(("M-110-DO", "paragraph", "MSG"), paras)
        cov, nf, _prog, _book, _as_prog = self.outputs("MMPGM", "MMAPBK")
        self.assertIn("_none_", nf)
        self.assertIn("- 2 copybooks marked `skipped` were re-filed by atlas.recover", cov)


class GenuineAssemblerIsNotRefiled(_Estate):
    """Case D: ASMBK is a real Assembler member (label in column 1, CSECT, DS with a type) in a COPYLIB
    folder, and a program copies that name: misfiled by its content, NOT re-filed, and the sentence says
    what that means - the copybook the program copies is another member."""

    files = (("GC/PROD.GC.SRC/ASMPGM.cbl", data_program("ASMPGM", "ASMBK")),
             ("SHARED/PROD.GC.COPYLIB/ASMBK.txt", ASMBK))

    def test_reported_by_its_content_with_an_honest_sentence(self):
        self.assert_the_bug("ASMPGM", "ASMBK", "asm")
        why_not = ("no folder change helps (the content decided); not re-filed: it has the shape of an Assembler member "
                   f"({recover.ASM_SHAPES}) - the copybook the programs copy is then another member, still to fetch; if this "
                   f"member IS the COBOL copybook, wait for {ITEM}")
        seen = ("line 1 `CSECT` reads as Assembler - a first or second word beginning with START, CSECT or DSECT, checked before "
                f"the level numbers and the folder name; {ITEM}")
        for dry in (True, False):
            stats, said = self.recover(dry_run=dry)
            self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
            self.assertIn("  1 copybook name(s) exist in the index only as a member the classifier typed by a line of its text "
                          "(asm) and this run could not re-file: 1 program(s) stay parsed only in part - no folder change helps; "
                          f"the report says why for each ({ITEM})", said)
            self.assertNotIn("re-filed as copybook", said)
            self.assertNotIn("does not expand", said)
            self.assertNotIn("next: rename the folder", said)
            # the listings may hold the real copybook: that path stays open (no 'folder fix comes first')
            self.assertIn("missing copybooks in the index: 1", said)
            self.assertIn("next: run again as", said)
            self.assertNotIn("comes first", said)
            self.assertEqual(self.member("ASMBK")[:3], ("asm", "PROD.GC.COPYLIB", "skipped"))
            self.assertEqual(self.status("ASMPGM"), "partial")
            rep = self.report_text()
            self.assertIn(f"| ASMBK | asm | PROD.GC.COPYLIB | ASMPGM | {why_not} | filed as asm by its content ({seen}) |", rep)
            self.assertNotIn("## Re-filed as copybook", rep)
        cov, nf, prog, book, as_prog = self.outputs("ASMPGM", "ASMBK")
        self.assertIn(f"| ASMBK | 1 | yes: PROD.GC.COPYLIB, filed as asm by its content ({seen}) - not a kind the build expands, "
                      f"and {why_not} |", nf)
        self.assertNotIn("rename the folder", nf.split("| ASMBK |")[1].split("\n")[0])
        self.assertIn("1 exists only as a member the classifier typed by a line of its text (asm - ", cov)
        self.assertIn("no folder change helps - atlas.recover cannot re-file it; the 'Copybooks not found' table says why", cov)
        self.assertIn(f"and {why_not} |", prog)
        self.assertIn(f"filed as asm by its content (folder PROD.GC.COPYLIB; {seen}): not a kind the build expands, so every "
                      f"program that copies it is parsed only in part. {why_not[0].upper() + why_not[1:]}.", book)
        self.assertIn(f"in PROD.GC.COPYLIB it is filed as asm by its content ({seen}) - {why_not}.", as_prog)


class ProcedureCopybookInProcsFolder(_Estate):
    """Case E: the folder decided (LESSONS 183): PROCBOOK.txt in a PROCS folder is a proc - not re-filed, the
    rename / declare sentence unchanged, and the report's last column says 'by its folder'."""

    files = (("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PLAINBK")),
             ("SHARED/PROD.GC.PROCS/PLAINBK.txt", PLAINBK))

    def test_the_rename_sentence_stays_and_nothing_is_re_filed(self):
        self.assert_the_bug("SECPGM", "PLAINBK", "proc", "PROD.GC.PROCS")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        self.assertIn("1 copybook name(s) exist in the index only as a member of a kind the build does not expand (proc): "
                      "1 program(s) stay parsed only in part until the folder is renamed to end in COPYLIB or the library's "
                      "kind declared in the UI's table - the report names each", said)
        self.assertIn("next: rename the folder(s) the report names to end in COPYLIB", said)
        self.assertIn("every one of them is the name of a member filed as a kind the build does not expand (above): the folder "
                      "fix comes first", said)
        self.assertNotIn("re-filed", said)
        self.assertNotIn("could not re-file", said)
        self.assertEqual(self.member("PLAINBK")[:3], ("proc", "PROD.GC.PROCS", "ok"))
        rep = self.report_text()
        self.assertIn(f"| PLAINBK | proc | PROD.GC.PROCS | SECPGM | {MISFILED_FIX} | filed as proc by its folder (the folder name "
                      "ends in PROCS) |", rep)
        self.assertNotIn("## Re-filed as copybook", rep)
        cov, nf, prog, book, as_prog = self.outputs("SECPGM", "PLAINBK")
        self.assertIn(f"| PLAINBK | 1 | yes: PROD.GC.PROCS, filed as proc - {MISFILED_CELL} |", nf)
        self.assertIn("1 exists only as a member of a kind the build does not expand (the folder name decided it)", cov)
        self.assertNotIn("by a line of its text", cov)
        self.assertIn(f"a member with this name exists: PROD.GC.PROCS, filed as proc - {MISFILED_CELL} |", prog)
        self.assertIn("filed as proc (folder PROD.GC.PROCS): not a kind the build expands, so every program that copies it is "
                      f"parsed only in part. {MISFILED_FIX[0].upper() + MISFILED_FIX[1:]}.", book)
        self.assertIn(f"> 1 program copies it as a copybook and says `COPY PLAINBK NOT FOUND` (SECPGM): in PROD.GC.PROCS it is "
                      f"filed as proc by its folder (the folder name ends in PROCS) - {MISFILED_FIX}.", as_prog)
        # a copy in a plain folder is a doc by its extension: the same rename sentence, said as an extension case
        self.write("SHARED/downloads/PLAINBK.txt", PLAINBK)
        self.build()
        conn = query.connect(self.db)
        try:
            rs = recover.member_readings(recover.members_named(conn, "PLAINBK")[1])
            self.assertEqual([(r["kind"], r["by"], r["refile"]) for r in rs], [("doc", "extension", False), ("proc", "folder", False)])
            self.assertEqual(recover.filed_phrase(rs[0]), "filed as doc by its extension (the extension .txt in a folder with "
                                                          "no library hint)")
            self.assertEqual(query.same_named_note(conn, "PLAINBK"),
                             f"downloads, filed as doc; PROD.GC.PROCS, filed as proc - {MISFILED_CELL}")
        finally:
            conn.close()


class RebuildRefilesAgain(_Estate):
    """Case F: --rebuild after a re-file starts from an empty index, so the classifier files STARTBK as asm
    again (expected until ROADMAP item 22) and the program is partial again; recover re-files it again."""

    files = DataCopybookWithStartDate.files

    def test_the_cycle_works_and_the_report_says_so(self):
        stats, _said = self.recover()
        self.assertEqual(stats["refiled"], 1)
        self.build()
        self.assertEqual((self.member("STARTBK")[0], self.status("STPGM")), ("copybook", "ok"))
        self.build(["--rebuild"])
        self.assert_the_bug("STPGM", "STARTBK", "asm")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index", said)
        self.assertIn("a --rebuild before that files them as before, and this tool re-files them again", self.report_text())
        self.build()
        self.assertEqual((self.member("STARTBK")[:3], self.status("STPGM")), (("copybook", "PROD.GC.COPYLIB", "skipped"), "ok"))
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(self.member_id("STARTBK"),)])


class RealAssemblerShapesAreNotRefiled(_Estate):
    """LESSONS 187 (1): three real Assembler members in a COPYLIB folder that the first shape guard judged
    'no Assembler shape' and re-filed - their copiers then went 'ok' with Assembler text expanded as COBOL. A
    label of any length (or none) before CSECT, START with a quoted operand, DC with an address constant: refused,
    with the honest sentence, and the build after it expands nothing."""

    files = (("GC/PROD.GC.SRC/TRK1P.cbl", data_program("TRK1P", "LONGLBL")),
             ("GC/PROD.GC.SRC/TRK2P.cbl", data_program("TRK2P", "NOLABEL")),
             ("GC/PROD.GC.SRC/TRK3P.cbl", data_program("TRK3P", "HEXSTART")),
             ("SHARED/PROD.GC.COPYLIB/LONGLBL.txt", LONGLBL),
             ("SHARED/PROD.GC.COPYLIB/NOLABEL.txt", NOLABEL),
             ("SHARED/PROD.GC.COPYLIB/HEXSTART.txt", HEXSTART))
    pairs = (("TRK1P", "LONGLBL"), ("TRK2P", "NOLABEL"), ("TRK3P", "HEXSTART"))

    def test_the_three_shapes_are_refused_and_nothing_is_expanded(self):
        for prog, book in self.pairs:
            self.assert_the_bug(prog, book, "asm")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 3, 0), said)
        self.assertIn("  3 copybook name(s) exist in the index only as a member the classifier typed by a line of its text (asm) "
                      "and this run could not re-file: 3 program(s) stay parsed only in part - no folder change helps; the "
                      f"report says why for each ({ITEM})", said)
        self.assertNotIn("re-filed as copybook", said)
        rep = self.report_text()
        self.assertNotIn("## Re-filed as copybook", rep)
        for prog, book in self.pairs:
            self.assertEqual(self.member(book)[:3], ("asm", "PROD.GC.COPYLIB", "skipped"))
            self.assertIn(f"| {book} | asm | PROD.GC.COPYLIB | {prog} | no folder change helps (the content decided); not re-filed: "
                          f"it has the shape of an Assembler member ({recover.ASM_SHAPES}) - the copybook the programs copy is "
                          f"then another member, still to fetch; if this member IS the COBOL copybook, wait for {ITEM} | "
                          f"filed as asm by its content (line 1 ", rep)
        # the build after the run: the programs stay partial, no Assembler line was expanded as COBOL
        self.build()
        for prog, book in self.pairs:
            self.assertEqual(self.status(prog), "partial")
            self.assertEqual(self.copy_use(prog, book), [(None,)])
            self.assertEqual(self.q("SELECT COUNT(*) FROM pfield f JOIN program p ON p.id=f.program_id JOIN member m ON "
                                    "m.id=p.member_id WHERE m.name=? AND f.name IN ('TABLE','PLHEX','PLLONGLABEL')", prog), [(0,)])
        cov, nf, _prog, _book, _as_prog = self.outputs("TRK1P", "LONGLBL")
        self.assertIn("3 exist only as a member the classifier typed by a line of its text (asm - ", cov)
        self.assertIn("no folder change helps - atlas.recover cannot re-file them; the 'Copybooks not found' table says why", cov)
        for _prog, book in self.pairs:
            self.assertIn(f"| {book} | 1 | yes: PROD.GC.COPYLIB, filed as asm by its content (line 1 ", nf)


class NoHintFolderStartPara(_Estate):
    """LESSONS 187 (2): PROCBOOK (PERFORM START-PARA, no level numbers) in SHARED\\PROD.GC.PLIB - a dataset-named
    folder with no COPY hint, as his hand-fetched libraries land (LESSONS 183). Asm by its content, and nothing
    says copybook, so this tool does not re-file it - the instruction is the folder fix and a second run, never
    'no folder change helps' beside it; and the instruction, followed, makes the program whole."""

    files = (("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PROCBOOK")),
             ("SHARED/PROD.GC.PLIB/PROCBOOK.txt", PROCBOOK))

    def test_the_folder_fix_then_a_second_run_and_the_instruction_proves_true(self):
        self.assert_the_bug("SECPGM", "PROCBOOK", "asm", "PROD.GC.PLIB")
        fix = recover.FOLDER_THEN_RECOVER
        self.assertTrue(fix.startswith("the content decided the kind, and neither the folder name (no COPYLIB) nor the text "
                                       "(no level numbers) says copybook, so this tool did not re-file it: rename the folder to "
                                       "end in COPYLIB"), fix)
        for dry in (True, False):
            stats, said = self.recover(dry_run=dry)
            self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
            self.assertIn("  1 copybook name(s) exist in the index only as a member the classifier typed by a line of its text "
                          "(asm) in a folder with no COPY hint and with no level numbers to go by: 1 program(s) stay parsed only "
                          "in part until the folder is renamed to end in COPYLIB and this tool is run again - it then re-files "
                          f"the member ({ITEM})", said)
            self.assertIn(recover.NEXT_FOLDER_REFILE, said)
            self.assertIn("every one of them is the name of a member a COPYLIB folder would let this tool re-file (above): the "
                          "folder fix and a second run come first - the listings only if a program still says NOT FOUND after "
                          "the build", said)
            self.assertNotIn("next: run again as", said)
            self.assertNotIn("no folder change helps", said)
            self.assertNotIn("could not re-file", said)
            self.assertNotIn("next: rename the folder(s) the report names to end in COPYLIB (or declare", said)
            rep = self.report_text()
            self.assertIn(f"| PROCBOOK | asm | PROD.GC.PLIB | SECPGM | {fix} | filed as asm by its content (line 2 `START-PARA` "
                          f"{ASM_SEEN}) |", rep)
            self.assertNotIn("no folder change helps", rep.split("| PROCBOOK |")[1])
            self.assertIn("needs the folder renamed to end in COPYLIB first, then a second run", rep)
        cov, nf, prog, book, as_prog = self.outputs("SECPGM", "PROCBOOK")
        cell = (f"| PROCBOOK | 1 | yes: PROD.GC.PLIB, filed as asm by its content (line 2 `START-PARA` {ASM_SEEN}) - not a kind "
                f"the build expands, and {fix} |")
        self.assertIn(cell, nf)
        self.assertIn(f"1 exists only as a member the classifier typed by a line of its text (asm - a START- name, a MODULE MAP "
                      f"comment, a first word MSG; {ITEM}): 1 sits in a folder with no COPY hint and has no level numbers to go "
                      "by: rename the folder to end in COPYLIB, then `python -m atlas.recover --db atlas.db` re-files it; then "
                      "build", cov)
        self.assertNotIn("no folder change helps - atlas.recover cannot re-file", cov)
        self.assertIn(f"| PROCBOOK | **NOT FOUND** - a member with this name exists: PROD.GC.PLIB, filed as asm by its content "
                      f"(line 2 `START-PARA` {ASM_SEEN}) - not a kind the build expands, and {fix} |", prog)
        self.assertIn(f"filed as asm by its content (folder PROD.GC.PLIB; line 2 `START-PARA` {ASM_SEEN}): not a kind the build "
                      f"expands, so every program that copies it is parsed only in part. {fix[0].upper() + fix[1:]}.", book)
        self.assertIn(f"in PROD.GC.PLIB it is filed as asm by its content (line 2 `START-PARA` {ASM_SEEN}) - {fix}.", as_prog)
        # the instruction, followed: the folder renamed to end in COPYLIB, the build (the classifier still reads the
        # content: asm, until item 22), this tool re-files it, the build makes the program whole
        os.rename(os.path.join(self.root, "SHARED", "PROD.GC.PLIB"), os.path.join(self.root, "SHARED", "PROD.GC.COPYLIB"))
        self.build()
        self.assert_the_bug("SECPGM", "PROCBOOK", "asm")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index", said)
        self.build()
        self.assertEqual(self.status("SECPGM"), "ok")
        self.assertEqual(self.copy_use("SECPGM", "PROCBOOK"), [(self.member_id("PROCBOOK"),)])
        paras = [(p[0], p[1], p[2]) for p in self.paragraphs("SECPGM")]
        self.assertIn(("START-PARA", "paragraph", "A-100-BEGIN"), paras)
        _cov, nf, prog, _book, _as_prog = self.outputs("SECPGM", "PROCBOOK")
        self.assertIn("_none_", nf)
        self.assertNotIn("NOT FOUND", prog)


class DeclaredKindDecided(_Estate):
    """LESSONS 187 (3): PLAINBK (no signature) in SHARED\\PROD.GC.MISC, a folder with no hint, and the UI's table
    declares that library 'proc' (manifest kinds): the build files it proc. The reading says 'by its declared
    kind' with 'declare it copybook there' - never 'the file now reads as unknown ... run the build first', which
    the build would not change; declared copybook, the build makes the program whole."""

    files = (("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PLAINBK")),
             ("SHARED/PROD.GC.MISC/PLAINBK.txt", PLAINBK))
    SEEN = ("the kind declared for library PROD.GC.MISC in the UI's table (sources.json, the manifest kinds) - the classifier "
            "itself read no signature and no folder hint (unrecognised member of library PROD.GC.MISC (extension .txt ignored))")

    def before_build(self):
        self.manifest = os.path.join(self.td, "manifest.json")
        self.declare("proc")

    def declare(self, kind):
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": {"PROD.GC.MISC": kind}}, fh)

    def test_by_its_declared_kind_never_run_the_build_first(self):
        self.assert_the_bug("SECPGM", "PLAINBK", "proc", "PROD.GC.MISC")
        conn = query.connect(self.db)
        try:
            other = recover.members_named(conn, "PLAINBK")[1]
            rs = recover.member_readings(other, conn)
            self.assertEqual([(r["kind"], r["by"], r["refile"], r["why_not"]) for r in rs], [("proc", "declared", False, "")])
            self.assertEqual(rs[0]["seen"], self.SEEN)
            self.assertEqual(recover.filed_phrase(rs[0]), f"filed as proc by its declared kind ({self.SEEN})")
            self.assertEqual(recover.folder_fix(rs[0]), recover.DECLARED_FIX)
            self.assertFalse(recover.folder_helps(rs[0]))
            # without the stored sha the reading cannot tell a declared kind from a file changed since the build
            self.assertEqual(recover.member_readings(other)[0]["by"], "changed")
            arrived, misfiled = recover.arrived_copybooks(conn)
            recover.read_misfiled(misfiled, conn)
            self.assertTrue(recover.folder_decided(misfiled[0]))
            self.assertEqual(recover.misfiled_cells(misfiled[0]),
                             (recover.DECLARED_FIX, f"filed as proc by its declared kind ({self.SEEN})"))
        finally:
            conn.close()
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        self.assertIn("1 copybook name(s) exist in the index only as a member of a kind the build does not expand (proc): "
                      "1 program(s) stay parsed only in part until the folder is renamed to end in COPYLIB or the library's "
                      "kind declared in the UI's table - the report names each", said)
        self.assertIn("next: rename the folder(s) the report names to end in COPYLIB", said)
        rep = self.report_text()
        self.assertIn(f"| PLAINBK | proc | PROD.GC.MISC | SECPGM | {recover.DECLARED_FIX} | filed as proc by its declared kind "
                      f"({self.SEEN}) |", rep)
        self.assertNotIn("run the build first", rep)
        self.assertNotIn("the folder name decided", rep.split("| PLAINBK |")[1])
        self.assertIn("takes the kind declared for its library in the UI's table: declare it copybook there", rep)
        cov, nf, prog, book, as_prog = self.outputs("SECPGM", "PLAINBK")
        self.assertIn(f"| PLAINBK | 1 | yes: PROD.GC.MISC, filed as proc by its declared kind - not a kind the build expands: "
                      f"{recover.DECLARED_CELL} |", nf)
        self.assertIn("1 exists only as a member of a kind the build does not expand (the kind declared for the library in the "
                      "UI's table decided it): rename the folder to end in COPYLIB or declare the library's kind in the UI, "
                      "then build", cov)
        self.assertIn(f"a member with this name exists: PROD.GC.MISC, filed as proc by its declared kind - not a kind the build "
                      f"expands: {recover.DECLARED_CELL} |", prog)
        self.assertIn(f"filed as proc by its declared kind (folder PROD.GC.MISC; {self.SEEN}): not a kind the build expands, so "
                      f"every program that copies it is parsed only in part. "
                      f"{recover.DECLARED_FIX[0].upper() + recover.DECLARED_FIX[1:]}.", book)
        self.assertIn(f"> 1 program copies it as a copybook and says `COPY PLAINBK NOT FOUND` (SECPGM): in PROD.GC.MISC it is "
                      f"filed as proc by its declared kind ({self.SEEN}) - {recover.DECLARED_FIX}.", as_prog)
        for text in (cov, prog, book, as_prog):
            self.assertNotIn("run the build first", text)
            self.assertNotIn("the folder name decided the kind", text)
        # the instruction, followed: the library declared copybook in the table - the build (the manifest changed:
        # every member re-parsed) files it as a copybook and the program is whole
        self.declare("copybook")
        self.build()
        self.assertEqual((self.member("PLAINBK")[:3], self.status("SECPGM")), (("copybook", "PROD.GC.MISC", "ok"), "ok"))
        self.assertEqual(self.copy_use("SECPGM", "PLAINBK"), [(self.member_id("PLAINBK"),)])


class SecondRunBeforeTheBuild(_Estate):
    """LESSONS 187 (4): a second run of this tool before the build finds the member it re-filed a run earlier as
    a copybook-kind member and the program's COPY still unresolved - not 'a copybook that has arrived since they
    were parsed' (nothing arrived; nothing parsed them, by design): it waits for the build, and says so."""

    files = DataCopybookWithStartDate.files

    def test_waiting_for_the_build_not_arrived(self):
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["waiting"]), (1, 0), said)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["refiled"], stats["waiting"], stats["marked"]),
                         (0, 0, 0, 1, 0), said)
        self.assertIn("  1 copybook(s) re-filed on an earlier run wait for the build (1 program(s) marked already): run your "
                      "usual build command", said)
        self.assertNotIn("has arrived since", said)
        self.assertNotIn("re-filed as copybook in the index", said)
        self.assertIn("next: run your usual build command", said)
        self.assertEqual(self.status("STPGM"), "pending")
        rep = self.report_text()
        self.assertIn("## Re-filed on an earlier run - waiting for the build", rep)
        self.assertIn("| copybook | folder | programs |\n|---|---|---|\n| STARTBK | PROD.GC.COPYLIB | STPGM |", rep)
        self.assertNotIn("## Copybooks that arrived after the program was parsed", rep)
        self.assertNotIn("## Re-filed as copybook", rep)
        self.assertNotIn("Nothing to report", rep)
        cov, nf, prog, book, _as_prog = self.outputs("STPGM", "STARTBK")
        # the program is pending, not partial, so coverage's partial-members note is '_none_' here; its not-found
        # cell and its Members line carry the instruction (the note's own waiting clause is for a mixed estate)
        self.assertIn("| STARTBK | 1 | yes: PROD.GC.COPYLIB (copybook, re-filed by atlas.recover) - the programs copying it are "
                      "marked: run your usual build command |", nf)
        self.assertIn("### Members parsed only in part\n_none_", cov)
        self.assertIn("- 1 copybook marked `skipped` was re-filed by atlas.recover", cov)
        # (the legend under coverage's not-found table describes the arrived case in general; the ROWS, the program
        # and the copybook must not say it of this member)
        for text in (nf.split("\n>")[0], prog, book):
            self.assertNotIn("parsed before it arrived", text)
        self.assertNotIn("has a member with that name in the index now", cov)
        conn = query.connect(self.db)
        try:
            arrived, misfiled, waiting = recover.arrival_scan(conn)
            self.assertEqual((arrived, misfiled, [e["copybook"] for e in waiting]), ([], [], ["STARTBK"]))
            self.assertEqual(recover.arrived_copybooks(conn), ([], []))
        finally:
            conn.close()
        self.build()
        self.assertEqual(self.status("STPGM"), "ok")
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["waiting"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("STARTBK", said)
        self.assertNotIn("wait for the build", said)


class ManifestChangeRefilesAgain(_Estate):
    """Case G (LESSONS 188 (1)): --rebuild is not the one build that undoes a re-file. The stored kind survives only
    while the build keeps every unchanged member's kind, and a build re-parses every member when the manifest
    changed (the UI rewrites manifest.json from the sources table on every build, so every library he adds or
    re-kinds in its table changes it - his routine of LESSONS 183) or a parser module changed. An UNRELATED library
    declared in the manifest after the re-file: the build files STARTBK asm again and un-links the program; recover
    re-files it again and the build makes the program whole - and the report says so after the --rebuild sentence."""

    files = DataCopybookWithStartDate.files + (("SHARED/PROD.GC.MISC/PLAINBK.txt", PLAINBK),)

    def before_build(self):
        self.manifest = os.path.join(self.td, "manifest.json")
        self.declare({})

    def declare(self, kinds):
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": kinds}, fh)

    def test_a_library_re_kinded_in_the_table_reverts_the_re_file_and_recover_re_files_it(self):
        self.assert_the_bug("STPGM", "STARTBK", "asm")
        self.assertEqual(self.member("PLAINBK")[:2], ("unknown", "PROD.GC.MISC"))
        stats, _said = self.recover()
        self.assertEqual(stats["refiled"], 1)
        # the sentence: the pinned --rebuild one stays, the manifest / parser one follows it
        self.assertIn("a --rebuild before that files them as before, and this tool re-files them again. "
                      + recover.REPARSE_UNDOES + " If a re-filed member's text changes on disk", self.report_text())
        self.assertIn("So does any build that re-parses every member - the manifest changed (a library added or re-kinded "
                      "in the UI's table rewrites it) or a parser module changed: run this tool after such a build and it "
                      "re-files them again.", recover.REPARSE_UNDOES)
        self.build()
        self.assertEqual((self.member("STARTBK")[:3], self.status("STPGM")), (("copybook", "PROD.GC.COPYLIB", "skipped"), "ok"))
        # an unrelated library re-kinded in the UI's table: nothing about STARTBK changed, yet the build re-parses
        # every member and the classifier reads it asm again - the program is partial again, its COPY row NULL
        self.declare({"PROD.GC.MISC": "proc"})
        self.build()
        self.assertEqual(self.member("PLAINBK")[:2], ("proc", "PROD.GC.MISC"))
        self.assert_the_bug("STPGM", "STARTBK", "asm")
        self.assertIsNone(self.member("STARTBK")[3])
        cov, nf, prog, _book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn(f"| STARTBK | 1 | yes: PROD.GC.COPYLIB, {CONTENT} ({SEEN}) - not a kind the build expands, and "
                      f"{RECOVER_FIX} |", nf)
        # the instruction, followed: recover re-files it again and the build makes the program whole
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index", said)
        self.assertIn("## Re-filed as copybook", self.report_text())
        self.build()
        self.assertEqual((self.member("STARTBK")[:3], self.status("STPGM")), (("copybook", "PROD.GC.COPYLIB", "skipped"), "ok"))
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(self.member_id("STARTBK"),)])
        self.assertEqual(self.not_found_note("STPGM", "STARTBK"), [])


class DryRunWithAFolderTypedCopyToo(_Estate):
    """Case H (LESSONS 188 (2)): STARTBK.txt in PROD.GC.COPYLIB (asm by its content, re-filable) and STARTBK.txt
    in PROD.GC.PROCS (a plain procedure copybook: proc by its folder), one program. The dry run counted the name
    among the folder-decided - the entry stays in `misfiled` on a dry run, and folder_decided() is true through
    the PROCS reading - and sent him to rename a folder no fix needs. Now it says 'run without --dry-run first'
    and no folder line; the run re-files the COPYLIB copy, leaves the PROCS copy alone, and the build resolves the
    program to the copybook member."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK),
             ("SHARED/PROD.GC.PROCS/STARTBK.txt", PLAINBK))

    def members(self):
        return self.q("SELECT kind, library, parse_status FROM member WHERE name='STARTBK' ORDER BY library")

    def test_the_dry_run_sends_him_to_no_folder_and_the_run_re_files_the_one_copy(self):
        self.assertEqual([(k, f) for k, f, _s in self.members()], [("asm", "PROD.GC.COPYLIB"), ("proc", "PROD.GC.PROCS")])
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("partial", [(None,)]))
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        self.assertIn("1 misfiled copybook(s) would be re-filed as copybook in the index (the classifier had read them as asm "
                      "by a line of their text): 1 program(s) would be marked for the next build (dry run: nothing changed)",
                      said)
        self.assertIn("every one of them is the name of a member this run would re-file as a copybook (above): run without "
                      "--dry-run first - the listings only if a program still says NOT FOUND after the build", said)
        for wrong in ("does not expand", "next: rename the folder", "the folder fix comes first", "next: run your usual build"):
            self.assertNotIn(wrong, said)
        # the report's cell says both: the rename for the PROCS copy, the re-file for the COPYLIB one (dry run)
        rep = self.report_text()
        row = [ln for ln in rep.splitlines() if ln.startswith("| STARTBK | ") and " STPGM | " in ln and "PROCS" in ln]
        self.assertEqual(len(row), 1, rep)
        self.assertIn(MISFILED_FIX, row[0])
        self.assertIn("the copy filed by its content: " + recover.CONTENT_FIX_DRY, row[0])
        self.assertIn("filed as proc by its folder (the folder name ends in PROCS)", row[0])
        self.assertIn(f"{CONTENT} ({SEEN})", row[0])
        self.assertEqual(self.members()[0][:2], ("asm", "PROD.GC.COPYLIB"))                   # a dry run changes nothing
        # the run: the COPYLIB copy re-filed, the PROCS copy left alone, no folder advice anywhere
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 misfiled copybook(s) re-filed as copybook in the index", said)
        self.assertIn("next: run your usual build command", said)
        for wrong in ("does not expand", "next: rename the folder", "the folder fix comes first"):
            self.assertNotIn(wrong, said)
        self.assertEqual(self.members(), [("copybook", "PROD.GC.COPYLIB", "skipped"), ("proc", "PROD.GC.PROCS", "ok")])
        self.assertNotIn("filed as something else", self.report_text())
        self.build()
        self.assertEqual(self.status("STPGM"), "ok")
        cid = self.q("SELECT id FROM member WHERE name='STARTBK' AND kind='copybook'")[0][0]
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(cid,)])
        self.assertEqual(self.q("SELECT name, offset FROM pfield WHERE name='START-DATE'"), [("START-DATE", 5)])
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("STARTBK", said)


class OkWithAnUnlinkedCopyRow(_Estate):
    """Case I (LESSONS 188 (3)): the window ROADMAP 22 names, said on the query side. After the re-file and the
    build (STPGM ok), STARTBK's text changes on disk (a field ST-NEW added) and the build files the new text asm
    again under a new member id, un-links STPGM's COPY row and parses nothing again: STPGM reads 'ok' with the old
    text's fields. `program` says so beside 'parse: ok', coverage counts it apart from the partial members; recover
    re-files the new member and the build makes the program whole with ST-NEW at offset 16."""

    files = DataCopybookWithStartDate.files
    NOTE = ("parse: ok, but 1 COPY row is unresolved (STARTBK): the member it had expanded went out of the index since - "
            "its text changed on disk and the classifier filed the new text as another kind, or the file went - and "
            "nothing parsed the program again, so its fields are those of the earlier read of that copybook; run "
            "`python -m atlas.recover --db atlas.db`, then the build (the Copybooks table below says what happened to each)")
    CLAUSE = ("### Members parsed only in part\n_none_\n\n> Not counted above: 1 program marked `ok` has a COPY row no "
              "member resolves any more (STPGM; copybook STARTBK): the member it had expanded went out of the index since "
              "- its text changed on disk and the classifier filed the new text as another kind, or the file went - and "
              "nothing parsed the program again, so its fields are those of the earlier read. `program NAME` says so "
              "beside `parse: ok`; run `python -m atlas.recover --db atlas.db`, then the build - the 'Copybooks not "
              "found' table names each copybook with what to do.\n")

    def fields(self):
        return self.q("SELECT f.name, f.offset, f.length, f.src_member FROM pfield f JOIN program p ON p.id=f.program_id "
                      "JOIN member m ON m.id=p.member_id WHERE m.name='STPGM' AND f.name IN ('ST-ID','START-DATE','ST-AMT',"
                      "'ST-NEW') ORDER BY f.offset")

    def test_program_and_coverage_say_it_and_recover_then_the_build_heal_it(self):
        self.recover()
        self.build()
        old = self.member_id("STARTBK")
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(old,)]))
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
        finally:
            conn.close()
        # the copybook re-fetched with a new field: the new text is filed asm again, the program's row un-linked
        self.write("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK + "           05  ST-NEW              PIC X(4).\n")
        self.build()
        kind, _folder, _status, error = self.member("STARTBK")
        new = self.member_id("STARTBK")
        self.assertEqual((kind, error), ("asm", None))
        self.assertNotEqual(new, old)
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))
        self.assertEqual(self.not_found_note("STPGM", "STARTBK"), [])
        self.assertEqual(self.fields(), [("ST-ID", 0, 5, old), ("START-DATE", 5, 8, old), ("ST-AMT", 13, 3, old)])
        self.assertEqual(self.q("SELECT name, length FROM pfield WHERE name='WS-REC'"), [("WS-REC", 16)])
        pid = self.member_id("STPGM")
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {pid: ("STPGM", ["STARTBK"])})
            self.assertEqual(recover.unlinked_ok_programs(conn, pid), {pid: ("STPGM", ["STARTBK"])})
            self.assertEqual(recover.unlinked_ok_programs(conn, new), {})
            self.assertEqual(query.unlinked_ok_note(conn, pid, "ok"), ", but" + self.NOTE.split(", but")[1])
            self.assertEqual(query.unlinked_ok_note(conn, pid, "partial"), "")
            self.assertEqual(query.unlinked_ok_clause({}), "")
        finally:
            conn.close()
        cov, nf, prog, book, as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn(self.NOTE, prog)
        self.assertIn(f"| STARTBK | **NOT FOUND** - a member with this name exists: PROD.GC.COPYLIB, {CONTENT} ({SEEN}) - not a "
                      f"kind the build expands, and {RECOVER_FIX} |", prog)
        self.assertIn(self.CLAUSE, cov)
        self.assertIn(f"| STARTBK | 1 | yes: PROD.GC.COPYLIB, {CONTENT} ({SEEN}) - not a kind the build expands, and "
                      f"{RECOVER_FIX} |", nf)
        self.assertIn(f"{CONTENT} (folder PROD.GC.COPYLIB; {SEEN})", book)
        self.assertIn("**Not a program**: indexed as a `asm` member", as_prog)
        # the heal: recover re-files the new member and marks the program by name; the build makes it whole
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertEqual(self.status("STPGM"), "pending")
        self.build()
        self.assertEqual((self.member("STARTBK")[:3], self.status("STPGM")), (("copybook", "PROD.GC.COPYLIB", "skipped"), "ok"))
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(new,)])
        self.assertEqual(self.fields(), [("ST-ID", 0, 5, new), ("START-DATE", 5, 8, new), ("ST-AMT", 13, 3, new),
                                         ("ST-NEW", 16, 4, new)])
        self.assertEqual(self.q("SELECT name, length FROM pfield WHERE name='WS-REC'"), [("WS-REC", 20)])
        cov, nf, prog, _book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertNotIn("but 1 COPY row", prog)
        self.assertNotIn("NOT FOUND", prog)
        self.assertNotIn("Not counted above", cov)
        self.assertIn("### Members parsed only in part\n_none_", cov)
        self.assertIn("_none_", nf)

    def test_a_system_include_is_never_counted(self):
        """A program copying SQLCA (the compiler supplies it: a NULL row by design) is not an un-linked one."""
        self.write("GC/PROD.GC.SRC/SQLPGM.cbl", HEAD.format(name="SQLPGM", data="           EXEC SQL INCLUDE SQLCA END-EXEC.\n",
                                                                    main="") + PLAIN_SECTION + TAIL)
        self.build()
        self.assertEqual(self.status("SQLPGM"), "ok")
        self.assertEqual(self.copy_use("SQLPGM", "SQLCA"), [(None,)])
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
            self.assertNotIn("but 1 COPY row", query.cmd_program(conn, "SQLPGM"))
            self.assertNotIn("Not counted above", query.cmd_coverage(conn))
        finally:
            conn.close()


class TheDocsSayIt(unittest.TestCase):
    """ROADMAP item 22 names the window a re-filed member's changed text opens (LESSONS 187 (5)) and the builds
    that undo a re-file (LESSONS 188 (1)); LESSONS has the rows; README and the Field Manual carry the clause."""

    def test_roadmap_22_and_lessons_187(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "ROADMAP.md"), encoding="utf-8") as fh:
            item = fh.read().split("\n22. ")[1].split("\n###")[0]
        self.assertIn("changes on disk", item)
        self.assertIn("recover._ASM_SHAPE", item)
        with open(os.path.join(root, "LESSONS.md"), encoding="utf-8") as fh:
            self.assertIn("\n| 187 | ", fh.read())

    def test_the_manifest_and_parser_builds_are_named_after_the_rebuild_sentence(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "ROADMAP.md"), encoding="utf-8") as fh:
            item = fh.read().split("\n22. ")[1].split("\n###")[0]
        self.assertIn("a `--rebuild` files it as before and recover re-files it again (so does any build that\n"
                      "   re-parses every member: the manifest changed", item)
        self.assertIn("or a parser module changed", item)
        with open(os.path.join(root, "LESSONS.md"), encoding="utf-8") as fh:
            lessons = fh.read()
        self.assertIn("\n| 188 | ", lessons)
        row_186 = [ln for ln in lessons.splitlines() if ln.startswith("| 186 | ")][0]
        self.assertIn("a --rebuild files it as asm again until item 22 and recover re-files it again; so does any build that "
                      "re-parses every member - the manifest changed (a library added or re-kinded in the UI's table rewrites "
                      "it) or a parser module changed", row_186)
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            self.assertIn("the rename. A build that re-parses every member - `--rebuild`, the\nmanifest changed (every library "
                          "added or re-kinded in the UI's table\nrewrites it), a parser module changed - files such a member "
                          "as before\nand parses its programs again without it, so they read partial with\nCOPY NOT FOUND: "
                          "run recover after such a build and it re-files them.",
                          fh.read())
        with open(os.path.join(root, "docs", "FieldManual.html"), encoding="utf-8") as fh:
            self.assertIn("instead of the rename. A build that re-parses every member — <code>--rebuild</code>, the manifest "
                          "changed (every library added or re-kinded in the UI's table rewrites it), a parser module changed "
                          "— files such a member as before and parses its programs again without it, so they read partial "
                          "with COPY NOT FOUND: run recover after such a build and it re-files them. Such a member", fh.read())


class TheVerdict(unittest.TestCase):
    """refile_verdict() line by line: what is re-filed and what is not, with the reason."""

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def reading(self, folder, text, name="X.txt", kind=None):
        p = os.path.join(self.td, folder, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        r = recover.how_classified(p, kind or recover.classify.classify(p, text)[0])
        ok, why = recover.refile_verdict(r, folder)
        return r, ok, why

    def test_what_is_re_filed(self):
        r, ok, why = self.reading("PROD.GC.COPYLIB", STARTBK)
        self.assertEqual((r["kind"], r["by"], ok, why), ("asm", "content", True, ""))
        # level numbers say copybook wherever the folder has no hint
        r, ok, why = self.reading("PROD.GC.OTHER", STARTBK)
        self.assertEqual((r["kind"], ok), ("asm", True))
        # the COBOL START verb in a procedure copybook (COPYLIB says copybook)
        r, ok, why = self.reading("PROD.GC.COPYLIB", "       S-100.\n           START CUST-FILE KEY IS > CUST-KEY.\n")
        self.assertEqual((r["kind"], r["word"], ok), ("asm", "START", True))
        r, ok, why = self.reading("PROD.GC.COPYLIB", MMAPBK)
        self.assertEqual((r["kind"], r["word"], ok), ("listing", "MODULE MAP", True))
        r, ok, why = self.reading("PROD.GC.COPYLIB", MSGBK)
        self.assertEqual((r["kind"], r["word"], ok), ("mfs", "MSG", True))

    def test_what_is_not(self):
        # a procedure copybook in a folder with no hint: nothing says copybook - the sentence names the folder fix
        # that would let THIS tool re-file it (the classifier itself still reads it as asm)
        r, ok, why = self.reading("PROD.GC.CPYSRC", PROCBOOK)
        self.assertEqual((r["kind"], r["by"], ok), ("asm", "content", False))
        self.assertIn("a folder ending in COPYLIB would let this tool re-file it", why)
        r, ok, why = self.reading("PROD.GC.COPYLIB", STARTBK + "//SYSIN DD *\n")
        self.assertEqual(ok, False)
        self.assertIn("it holds a JCL line (//)", why)
        # a PROGRAM whose comment names the MODULE MAP is a listing to the classifier (that signature runs before the
        # IDENTIFICATION DIVISION one): a program is not re-filed as a copybook
        r, ok, why = self.reading("PROD.GC.COPYLIB", "      * SEE THE MODULE MAP\n       IDENTIFICATION DIVISION.\n"
                                  "       PROGRAM-ID. X.\n       PROCEDURE DIVISION.\n       START-PARA.\n           GOBACK.\n")
        self.assertEqual((r["kind"], ok, "a program, not a copybook" in why), ("listing", False, True))
        # (a DS / DC line alone trips no signature, and neither does START in column 1: the `B START` line does,
        # and the DS / DC shape then refuses)
        for text in (ASMBK, "STARTBK  START 0\n         END\n", "PGM      START\n         END\n", "         START 0\n",
                     "STID     DS    CL5\n         B     START\n", "STDC     DC    F'0'\n         B     START\n",
                     "         DFHEIENT\n"):
            r, ok, why = self.reading("PROD.GC.COPYLIB", text)
            self.assertEqual((r["kind"], ok), ("asm", False), text)
            self.assertIn("it has the shape of an Assembler member", why)
        # the shapes the first guard let through (LESSONS 187): a label of any length or none before CSECT, START
        # with a quoted operand or a remark, a labelled START with a symbol, address constants, USING * - and never
        # a COBOL line
        for text in (LONGLBL, NOLABEL, HEXSTART, "PGM      START 0   BEGIN HERE\n", "PGM      START SYM\n",
                     "      * 05 START-DATE\nX        CSECT\n", "D        DSECT\n         B     START\n",
                     "         USING *,12\n         B     START\n", "V        DC    V(SUBPGM)\n         B     START\n"):
            r, ok, why = self.reading("PROD.GC.COPYLIB", text)
            self.assertEqual((r["kind"], ok), ("asm", False), text)
            self.assertIn("it has the shape of an Assembler member", why)
        # the COBOL START verb with a file name that carries no hyphen, its KEY clause on the same line: no shape
        r, ok, why = self.reading("PROD.GC.COPYLIB", "       S-100.\n           START CUSTFILE KEY IS > CUST-KEY.\n")
        self.assertEqual((r["kind"], r["word"], ok), ("asm", "START", True))
        # ... and alone on its line, the KEY IS or INVALID KEY clause on the next (his procedure copybook, LESSONS
        # 189): a COBOL line never begins in column 1, an Assembler START names its control section there or gives
        # a self-defining term - an unlabelled START with a bare symbol is the COBOL verb, re-filed
        for text in ("       S-100.\n           START CUSTFILE\n               KEY IS > K.\n",
                     "       S-100.\n           START INFILE\n              INVALID KEY MOVE 'N' TO WS-OK\n           END-START.\n",
                     "       S-100.\n           START CUSTFILE\n"):
            r, ok, why = self.reading("PROD.GC.COPYLIB", text)
            self.assertEqual((r["kind"], r["word"], ok, why), ("asm", "START", True, ""), text)
        # a STRONG signature that fires only inside comment lines says nothing about the member: a BMS macro name in
        # a remark, a REXX header behind a slash in column 7 - re-filed; the same word on a code line refuses
        r, ok, why = self.reading("PROD.GC.COPYLIB", "      * BMS FIELD DFHMDF MAPPED HERE\n" + PLAINBK)
        self.assertEqual((r["kind"], r["by"], ok, why), ("bms", "content", True, ""))
        r, ok, why = self.reading("PROD.GC.COPYLIB", "      /* REXX */\n" + PLAINBK)
        self.assertEqual((r["kind"], ok, why), ("rexx", True, ""))
        r, ok, why = self.reading("PROD.GC.COPYLIB", "MAP1     DFHMDF POS=(1,1),LENGTH=8\n" + PLAINBK)
        self.assertEqual((r["kind"], ok), ("bms", False))
        self.assertIn("its text carries a bms signature", why)
        for text in ("           05  CSECT-NAME   PIC X(8).\n           PERFORM CSECT-PARA.\n           B START\n",
                     "           MOVE DS TO WS-X.\n           PERFORM DC-PARA.\n           B START\n"):
            self.assertIsNone(recover._ASM_SHAPE.search(text), text)
        for text in ("MYFMT    FMT\n         DEV   TYPE=3270-A2\n", "         MSG   TYPE=INPUT,SOR=(MYFMT,IGNORE)\n",
                     "         DFLD  POS=(1,2),LTH=8\n"):
            r, ok, why = self.reading("PROD.GC.COPYLIB", text)
            self.assertEqual((r["kind"], ok), ("mfs", False), text)
            self.assertIn("it has the shape of MFS source", why)
        banner = "1PP 5655-EC6 IBM Enterprise COBOL for z/OS  6.3.0\n" + MMAPBK
        r, ok, why = self.reading("PROD.GC.COPYLIB", banner)
        self.assertEqual((r["kind"], ok), ("listing", False))
        self.assertIn("it has the shape of a compiler listing", why)
        # a strong signature is never overridden (a JOB card is checked first; an EXEC without one is checked AFTER the
        # Assembler signature, so that text is asm to the classifier and the JCL line refuses the re-file)
        r, ok, why = self.reading("PROD.GC.COPYLIB", "//MYJOB   JOB (ACCT)\n" + STARTBK)
        self.assertEqual((r["kind"], r["by"], ok), ("jcl", "content", False))
        self.assertIn("its text carries a jcl signature", why)
        r, ok, why = self.reading("PROD.GC.COPYLIB", "//STEP1   EXEC PGM=X\n" + STARTBK)
        self.assertEqual((r["kind"], ok), ("asm", False))
        self.assertIn("it holds a JCL line (//)", why)
        # the folder decided: not a content case at all
        r, ok, why = self.reading("PROD.GC.PROCS", PLAINBK)
        self.assertEqual((r["kind"], r["by"], r["seen"], ok, why), ("proc", "folder", "the folder name ends in PROCS", False, ""))
        # the file changed on disk since the build: say so, re-file nothing
        r, ok, why = self.reading("PROD.GC.COPYLIB", STARTBK, kind="proc")
        self.assertEqual((r["kind"], r["by"], ok), ("proc", "changed", False))
        self.assertIn("run the build first", r["seen"])
        # the folder fix is the instruction only for a content case nothing but a COPYLIB folder is missing for
        r, ok, why = self.reading("PROD.GC.CPYSRC", PROCBOOK)
        r.update(refile=ok, why_not=why)
        self.assertTrue(recover.folder_helps(r))
        self.assertEqual(recover.content_fix(r), recover.FOLDER_THEN_RECOVER)
        r, ok, why = self.reading("PROD.GC.COPYLIB", ASMBK)
        r.update(refile=ok, why_not=why)
        self.assertFalse(recover.folder_helps(r))
        self.assertTrue(recover.content_fix(r).startswith("no folder change helps (the content decided); not re-filed: "))

    def test_declared_kind_against_a_changed_file(self):
        # the same bytes the build read, the classifier says 'unknown', the index holds a kind the UI's table can
        # declare: the declared kind decided - with a different sha the file changed; without a sha nothing can tell
        p = os.path.join(self.td, "PROD.GC.MISC", "PLAINBK.txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(PLAINBK)
        with open(p, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        r = recover.how_classified(p, "proc", sha)
        self.assertEqual((r["kind"], r["by"]), ("proc", "declared"))
        self.assertEqual(r["seen"], DeclaredKindDecided.SEEN)
        self.assertEqual(recover.refile_verdict(r, "PROD.GC.MISC"), (False, ""))
        self.assertEqual(recover.how_classified(p, "proc", "0" * 64)["by"], "changed")
        self.assertEqual(recover.how_classified(p, "proc")["by"], "changed")
        # the same bytes, a kind the table cannot declare: the build's own rule, not the file
        r = recover.how_classified(p, "binary", sha)
        self.assertEqual(r["by"], "build")
        self.assertIn("the file unchanged since", r["seen"])
        # a kind the classifier itself gives is never 'declared', sha or not
        self.assertEqual(recover.how_classified(p, "unknown", sha)["by"], "extension")
        # unreadable
        r = recover.how_classified(os.path.join(self.td, "nope", "X.txt"), "asm")
        self.assertEqual((r["kind"], r["by"]), ("asm", "unreadable"))
        self.assertEqual(recover.refile_verdict(r, "PROD.GC.COPYLIB"), (False, ""))
        self.assertEqual(recover.filed_phrase(r), "filed as asm (the file could not be read from disk to say what decided its "
                                                  "kind - is the estate where the build saw it?)")


if __name__ == "__main__":
    unittest.main()
