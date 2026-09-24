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
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, recover  # noqa: E402

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

ITEM = "ROADMAP re-parse item 22"
CONTENT = "filed as asm by its content"
SEEN = ("line 2 `START-DATE` reads as Assembler - a first or second word beginning with START, CSECT or DSECT, checked "
        "before the level numbers and the folder name; " + ITEM)
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

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        for rel, text in self.files:
            self.write(rel, text)
        self.build(["--rebuild"])

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
            rc = build._main([self.root, "--db", self.db, "--quiet", *extra])
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
        why_not = ("no folder change helps (the content decided); not re-filed: it has the shape of an Assembler member (a label "
                   "in column 1 before CSECT or DSECT, START alone or with a numeric operand, DFHEIENT, or DS / DC with a type) - "
                   "the copybook the programs copy is then another member, still to fetch; if this member IS the COBOL copybook, "
                   f"wait for {ITEM}")
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
        for text in (ASMBK, "STARTBK  START 0\n         END\n", "         START\n         END\n",
                     "STID     DS    CL5\n         B     START\n", "STDC     DC    F'0'\n         B     START\n",
                     "         DFHEIENT\n"):
            r, ok, why = self.reading("PROD.GC.COPYLIB", text)
            self.assertEqual((r["kind"], ok), ("asm", False), text)
            self.assertIn("it has the shape of an Assembler member", why)
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
        # unreadable
        r = recover.how_classified(os.path.join(self.td, "nope", "X.txt"), "asm")
        self.assertEqual((r["kind"], r["by"]), ("asm", "unreadable"))
        self.assertEqual(recover.refile_verdict(r, "PROD.GC.COPYLIB"), (False, ""))
        self.assertEqual(recover.filed_phrase(r), "filed as asm (the file could not be read from disk to say what decided its "
                                                  "kind - is the estate where the build saw it?)")


if __name__ == "__main__":
    unittest.main()
