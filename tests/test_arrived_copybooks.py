r"""
A copybook that ARRIVED after the program was parsed, and one FILED AS
SOMETHING ELSE (LESSONS 183).

His line `A-100-BEGIN SECTION.  COPY PROCBOOK.` copies a PROCEDURE copybook:
paragraph names and statements, no level numbers, no DIVISION header - no
content signature at all, so classify types it by the FOLDER NAME. He
fetched the library that holds it (members arrive as .txt), built twice,
and coverage still said 'COPY PROCBOOK NOT FOUND' although the member was
on disk. Two mechanisms, both build-side (ROADMAP re-parse items 20, 21):

  * in a dataset-named folder with no COPY hint the member is 'unknown' - a
    kind the resolver accepts - but the build re-parses the copiers of a NEW
    member only when it is filed as copybook or cobol, so nothing parsed the
    program again (case A: recover marks it now);
  * in a PROCS / CNTL / plain folder the member is a proc / ctlcard / doc -
    kinds the resolver never looks at - so no re-parse would find it (case
    B: recover names it with the fix, coverage / program / copybook say so).

A data copybook (level numbers) has a signature, is a copybook wherever it
sits, and the existing forcing handles it (case C: the control).
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
        "       PROCEDURE DIVISION.\n       MAIN SECTION.\n           PERFORM A-100-BEGIN.\n"
        "           GOBACK.\n")
TAIL = "       Z-900-END SECTION.\n           EXIT.\n"
SECTION_COPY = "       A-100-BEGIN SECTION.  COPY PROCBOOK.\n"
# a procedure copybook: paragraphs and statements, no level numbers, no DIVISION - no content signature
PROCBOOK = ("       A-110-DO.\n           MOVE 1 TO WS-X.\n       A-199-EXIT.\n           EXIT.\n")
# a data copybook: level numbers ARE a signature, whatever the folder is called
DATABOOK = ("           05  DB-FIELD-A          PIC X(5).\n           05  DB-FIELD-B          PIC 9(3).\n")


def program(name="SECPGM", data=""):
    return HEAD.format(name=name, data=data) + SECTION_COPY + TAIL


class _Estate(unittest.TestCase):
    """estate\\GC\\PROD.GC.SRC\\SECPGM.cbl built while PROCBOOK is absent."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        self.write("GC/PROD.GC.SRC/SECPGM.cbl", program())
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

    def status(self, name="SECPGM"):
        return self.q("SELECT parse_status FROM member WHERE UPPER(name)=? AND kind='cobol'", name)[0][0]

    def book(self, name="PROCBOOK"):
        return self.q("SELECT kind, library FROM member WHERE UPPER(name)=?", name)

    def copy_use(self, name="PROCBOOK"):
        return self.q("SELECT c.resolved_member_id FROM copy_use c JOIN member m ON m.id=c.member_id "
                      "WHERE UPPER(m.name)='SECPGM' AND UPPER(c.copybook)=?", name)

    def paragraphs(self):
        return self.q("SELECT p.name, p.kind, p.section FROM paragraph p JOIN program g ON g.id=p.program_id "
                      "JOIN member m ON m.id=g.member_id WHERE UPPER(m.name)='SECPGM' ORDER BY p.start_line")

    def outputs(self):
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            return (cov, cov.split("### Copybooks not found")[1].split("\n###")[0],
                    query.cmd_program(conn, "SECPGM"), query.cmd_copybook(conn, "PROCBOOK"))
        finally:
            conn.close()

    def assert_parsed_without_the_book(self):
        self.assertEqual(self.status(), "partial")
        self.assertEqual(self.book(), [])
        self.assertEqual(self.copy_use(), [(None,)])
        cov, nf, prog, book = self.outputs()
        self.assertIn("| PROCBOOK | 1 | - |", nf)                                   # no member: nothing to say
        self.assertIn("| PROCBOOK | **NOT FOUND** |", prog)
        self.assertIn("**NOT FOUND**\n", book)
        self.assertNotIn("a member with this name exists:", prog)
        self.assertNotIn("a member with this name exists,", book)
        self.assertNotIn("yes:", nf)
        self.assertNotIn("parsed before it arrived", cov)
        self.assertNotIn("A copybook marked `yes`", cov)


class ArrivedAfterTheParse(_Estate):
    """Case A: PROCBOOK.txt lands in estate\\SHARED\\PROD.GC.CPYLIB - a
    dataset-named folder with no COPY hint - so the build files it 'unknown'."""

    def test_the_incremental_build_leaves_the_program_partial_until_recover_marks_it(self):
        self.assert_parsed_without_the_book()
        self.write("SHARED/PROD.GC.CPYLIB/PROCBOOK.txt", PROCBOOK)
        self.build()
        # THE BUG (ROADMAP re-parse item 21): the member is in the index, of a kind the resolver accepts,
        # and the program that copies it was not parsed again - coverage still says NOT FOUND
        self.assertEqual(self.book(), [("unknown", "PROD.GC.CPYLIB")])
        self.assertEqual(self.status(), "partial")
        self.assertEqual(self.copy_use(), [(None,)])
        cov, nf, prog, _book = self.outputs()
        self.assertIn("| PROCBOOK | 1 | yes: PROD.GC.CPYLIB (unknown) - parsed before it arrived: run recover, then the build |", nf)
        self.assertIn("A copybook marked `yes` is not missing", cov)
        self.assertIn("1 of the copybooks reported NOT FOUND has a member with that name in the index now", cov)
        self.assertIn("| PROCBOOK | **NOT FOUND** - a member with this name exists: PROD.GC.CPYLIB (unknown) - parsed before "
                      "it arrived: run recover, then the build |", prog)
        # a dry run reports and marks nothing
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (1, 0, 0), said)
        self.assertIn("1 program(s) copy a copybook that has arrived since they were parsed (1 copybook): would be marked "
                      "for the next build", said)
        self.assertNotIn("next: run your usual build command", said)
        self.assertEqual(self.status(), "partial")
        self.assertIn("would be marked for the next build (dry run: nothing marked)", self.report_text())
        # the run marks the program, says so, and the report names copybook, kind, folder and program
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 program(s) copy a copybook that has arrived since they were parsed (1 copybook): marked for the "
                      "next build", said)
        self.assertIn("next: run your usual build command", said)
        self.assertIn(f"every name: {self.report}", said)
        self.assertEqual(self.status(), "pending")
        rep = self.report_text()
        self.assertIn("## Copybooks that arrived after the program was parsed", rep)
        self.assertIn("| copybook | kind the index filed it as | folder | programs |", rep)
        self.assertIn("| PROCBOOK | unknown | PROD.GC.CPYLIB | SECPGM |", rep)
        self.assertNotIn("filed as something else", rep)
        # the next build, without --rebuild, parses the program again: whole, the section holding the book's paragraphs
        self.build()
        self.assertEqual(self.status(), "ok")
        self.assertEqual(len(self.copy_use()), 1)
        self.assertIsNotNone(self.copy_use()[0][0])
        paras = [(p[0], p[1], p[2]) for p in self.paragraphs()]
        self.assertIn(("A-100-BEGIN", "section", "A-100-BEGIN"), paras)
        self.assertIn(("A-110-DO", "paragraph", "A-100-BEGIN"), paras)
        self.assertIn(("A-199-EXIT", "paragraph", "A-100-BEGIN"), paras)
        cov, nf, prog, _book = self.outputs()
        self.assertIn("_none_", nf)
        self.assertNotIn("NOT FOUND", prog)
        self.assertIn("### Members parsed only in part\n_none_", cov)
        # nothing left for the step to say
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("has arrived since", said)

    def test_a_program_whose_own_copy_resolved_is_left_alone(self):
        # OTHERPGM arrives together with the book and resolves it at its first parse; SECPGM, parsed
        # earlier, does not - the marking is by member, so only SECPGM is marked, OTHERPGM stays ok
        self.write("GC2/PROD.GC2.SRC/OTHERPGM.cbl", program("OTHERPGM"))
        self.write("GC2/PROD.GC2.CPYLIB/PROCBOOK.txt", PROCBOOK)
        self.build()
        self.assertEqual(self.book(), [("unknown", "PROD.GC2.CPYLIB")])
        self.assertEqual((self.status("SECPGM"), self.status("OTHERPGM")), ("partial", "ok"))
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (1, 1), said)
        self.assertEqual((self.status("SECPGM"), self.status("OTHERPGM")), ("pending", "ok"))
        self.assertIn("| PROCBOOK | unknown | PROD.GC2.CPYLIB | SECPGM |", self.report_text())
        self.build()
        self.assertEqual((self.status("SECPGM"), self.status("OTHERPGM")), ("ok", "ok"))

    def test_the_index_side_helpers(self):
        self.write("SHARED/PROD.GC.CPYLIB/PROCBOOK.txt", PROCBOOK)
        self.build()
        conn = query.connect(self.db)
        try:
            accepted, other = recover.members_named(conn, "procbook")
            self.assertEqual([(k, f) for _i, k, f, _p in accepted], [("unknown", "PROD.GC.CPYLIB")])
            self.assertEqual(other, [])
            self.assertEqual(recover.members_named(conn, "NOPE"), ([], []))
            arrived, misfiled = recover.arrived_copybooks(conn)
            self.assertEqual(misfiled, [])
            self.assertEqual(len(arrived), 1)
            self.assertEqual((arrived[0]["copybook"], arrived[0]["members"]), ("PROCBOOK", [("unknown", "PROD.GC.CPYLIB")]))
            self.assertEqual([(n, k) for _i, n, k in arrived[0]["programs"]], [("SECPGM", "cobol")])
            self.assertEqual(recover.missing_copybooks(conn), {}, "not missing: the member exists - that is why it fell through")
            self.assertEqual(query.same_named_note(conn, "PROCBOOK"),
                             "PROD.GC.CPYLIB (unknown) - parsed before it arrived: run recover, then the build")
            self.assertEqual(query.same_named_note(conn, "NOPE"), "")
        finally:
            conn.close()


class FiledAsAnotherKind(_Estate):
    """Case B: PROCBOOK.txt lands in estate\\SHARED\\PROD.GC.PROCS - the folder
    name makes it a proc (a JCL PROC), which the resolver never expands."""

    def test_recover_names_it_with_the_fix_and_marks_nothing(self):
        self.assert_parsed_without_the_book()
        self.write("SHARED/PROD.GC.PROCS/PROCBOOK.txt", PROCBOOK)
        self.build()
        self.assertEqual(self.book(), [("proc", "PROD.GC.PROCS")])
        self.assertEqual(self.status(), "partial")
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        self.assertIn("1 copybook name(s) exist in the index only as a member of a kind the build does not expand (proc): "
                      "1 program(s) stay parsed only in part until the folder is renamed or the library's kind declared - "
                      "the report says what to do", said)
        self.assertNotIn("has arrived since", said)
        self.assertNotIn("next: run your usual build command", said)
        self.assertEqual(self.status(), "partial", "a re-parse would find nothing: not marked")
        rep = self.report_text()
        self.assertIn("## A member with the copybook's name exists but is filed as something else", rep)
        self.assertIn("| copybook | filed as | folder | programs | what to do |", rep)
        self.assertIn("| PROCBOOK | proc | PROD.GC.PROCS | SECPGM | the folder name decided the kind (a copybook with no level "
                      "numbers has no signature): rename the folder to end in COPYLIB, or declare the library's kind in the "
                      "UI's table (the manifest kinds) and run the build |", rep)
        self.assertNotIn("## Copybooks that arrived after the program was parsed", rep)
        # a further build changes nothing: the member is still a proc
        self.build()
        self.assertEqual(self.status(), "partial")

    def test_coverage_program_and_copybook_say_so(self):
        self.write("SHARED/PROD.GC.PROCS/PROCBOOK.txt", PROCBOOK)
        self.build()
        cov, nf, prog, book = self.outputs()
        self.assertIn("| PROCBOOK | 1 | yes: PROD.GC.PROCS, filed as proc - not a kind the build expands: rename the folder "
                      "to end in COPYLIB or declare its kind in the UI, then build |", nf)
        self.assertIn("A copybook marked `yes` is not missing", cov)
        self.assertIn("1 exists only as a member of a kind the build does not expand", cov)
        self.assertIn("| PROCBOOK | **NOT FOUND** - a member with this name exists: PROD.GC.PROCS, filed as proc - not a kind "
                      "the build expands: rename the folder to end in COPYLIB or declare its kind in the UI, then build |", prog)
        self.assertIn("**NOT FOUND** as a copybook - a member with this name exists, filed as proc (folder PROD.GC.PROCS): "
                      "not a kind the build expands, so every program that copies it is parsed only in part. The folder name "
                      "decided the kind (a copybook with no level numbers has no signature): rename the folder to end in "
                      "COPYLIB, or declare the library's kind in the UI's table (the manifest kinds) and run the build.", book)
        # the fix, applied: the folder renamed to end in COPYLIB - the build files it as a copybook and re-parses the program
        shutil.move(os.path.join(self.root, "SHARED", "PROD.GC.PROCS"), os.path.join(self.root, "SHARED", "PROD.GC.COPYLIB"))
        self.build()
        self.assertEqual(self.book(), [("copybook", "PROD.GC.COPYLIB")])
        self.assertEqual(self.status(), "ok")
        cov, nf, prog, book = self.outputs()
        self.assertIn("_none_", nf)
        self.assertNotIn("NOT FOUND", prog + book)


class DataCopybookIsTheControl(_Estate):
    """Case C: a DATA copybook in the same PROD.GC.CPYLIB folder has a
    signature (level numbers): classified copybook by content, and the
    build's own forcing re-parses its copiers - the new step has nothing to say."""

    def setUp(self):
        super().setUp()
        self.write("GC/PROD.GC.SRC/DATAPGM.cbl", HEAD.format(name="DATAPGM", data="       01  WS-REC.\n           COPY DATABOOK.\n")
                   + "       A-100-BEGIN SECTION.\n           MOVE 1 TO WS-X.\n" + TAIL)
        self.build()

    def test_the_existing_forcing_handles_it_and_nothing_is_reported(self):
        self.assertEqual(self.status("DATAPGM"), "partial")
        self.assertEqual(self.q("SELECT resolved_member_id FROM copy_use WHERE UPPER(copybook)='DATABOOK'"), [(None,)])
        self.write("SHARED/PROD.GC.CPYLIB/DATABOOK.txt", DATABOOK)
        self.build()
        self.assertEqual(self.book("DATABOOK"), [("copybook", "PROD.GC.CPYLIB")])
        self.assertEqual(self.status("DATAPGM"), "ok", "a new member filed 'copybook' forces its copiers today")
        self.assertEqual(len(self.q("SELECT resolved_member_id FROM copy_use WHERE UPPER(copybook)='DATABOOK' "
                                    "AND resolved_member_id IS NOT NULL")), 1)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("has arrived since", said)
        self.assertNotIn("does not expand", said)
        self.assertEqual(self.status("DATAPGM"), "ok")
        # SECPGM's PROCBOOK is still plainly missing - no member at all - and the not-found table says nothing more
        cov, nf, _prog, _book = self.outputs()
        self.assertIn("| PROCBOOK | 1 | - |", nf)
        self.assertNotIn("DATABOOK", nf)
        self.assertNotIn("A copybook marked `yes`", cov)
        self.assertNotIn("has a member with that name in the index now", cov)


if __name__ == "__main__":
    unittest.main()
