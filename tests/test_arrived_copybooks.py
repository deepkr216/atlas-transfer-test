r"""
A copybook that ARRIVED after the program was parsed, and one FILED AS
SOMETHING ELSE (LESSONS 183); a copybook copied from inside another
copybook is NOT one that arrived (LESSONS 184).

His line `A-100-BEGIN SECTION.  COPY PROCBOOK.` copies a PROCEDURE copybook:
paragraph names and statements, no level numbers, no DIVISION header - no
content signature at all before the re-parse batch, so classify typed it by
the FOLDER NAME. He fetched the library that holds it (members arrive as
.txt), built twice, and coverage still said 'COPY PROCBOOK NOT FOUND'
although the member was on disk. ROADMAP re-parse item 20 (this batch)
types a procedure copybook by its COBOL statements, before the folder name:
PROCBOOK arriving in a dataset-named folder or in a PROCS folder is now a
copybook and the build makes its programs whole at once (cases A and B,
the first test of each). A copybook with no signature even now - LITBOOK,
a literal copied into a VALUE clause - still takes its folder's kind, and
the two mechanisms below still hold for it (the other tests of cases A and
B); the first is ROADMAP re-parse item 21, still open:

  * in a dataset-named folder with no COPY hint the member is 'unknown' - a
    kind the resolver accepts - but the build re-parses the copiers of a NEW
    member only when it is filed as copybook or cobol, so nothing parsed the
    program again (case A: recover marks it now; and an 'unknown' member has
    no handler, so its own lines are not indexed until the folder is renamed
    - the note says so);
  * in a PROCS / CNTL / plain folder the member is a proc / ctlcard / doc -
    kinds the resolver never looks at - so no re-parse would find it (case
    B: recover names it with the fix, coverage / program / copybook say so).

A data copybook (level numbers) has a signature, is a copybook wherever it
sits, and the existing forcing handles it (case C: the control).

LESSONS 184: a copybook member's own COPY rows are recorded with no
resolved_member_id (the build parses a copybook for copies only, never
resolves them), so they are NULL for ever - reading them as 'unresolved'
made every copybook that copies another copybook 'arrived', marked the
COPYBOOK pending, and the next build re-inserted it under a new id and
nulled the links of every program copying it. Only a PROGRAM's row says
whether a COPY was found, and a program carries its own row for every
nested COPY, so marking programs alone is enough (cases D and E).

LESSONS 185: a PROGRAM's row is NULL for a third reason - the expander
SKIPPED the COPY (a copybook copying itself: 'recursive'; nesting deeper
than expand.MAX_DEPTH) without looking it up. The member exists and the
program was parsed after it, so nothing arrived: the program's own 'expand'
note holds the reason, and only a row whose name the program reports NOT
FOUND is a copybook not found (cases F, G and H).
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
SECTION_COPY = "       A-100-BEGIN SECTION.  COPY {book}.\n"
# a procedure copybook: paragraphs and statements, no level numbers, no DIVISION - no content signature
PROCBOOK = ("       A-110-DO.\n           MOVE 1 TO WS-X.\n       A-199-EXIT.\n           EXIT.\n")
# a data copybook: level numbers ARE a signature, whatever the folder is called
DATABOOK = ("           05  DB-FIELD-A          PIC X(5).\n           05  DB-FIELD-B          PIC 9(3).\n")
# a procedure copybook that copies another one (LESSONS 184)
OUTBOOK = ("       A-110-DO.\n           MOVE 1 TO WS-X.\n           COPY INBOOK.\n       A-199-EXIT.\n           EXIT.\n")
INBOOK = ("       A-150-SUB.\n           MOVE 2 TO WS-Y.\n")
# a copybook with no signature even after ROADMAP re-parse item 20: a literal copied into a VALUE clause - the
# folder name types it ('unknown' in a dataset-named folder with no hint, 'proc' in a PROCS folder)
LITBOOK = "      * THE STATE CODES, COPIED INTO A VALUE CLAUSE\n               'NYNJCTPAMA'.\n"
# a data copybook that copies LITBOOK into a VALUE clause (LESSONS 184 with a member of no signature)
OUTLIT = "           05  OUT-STATES          PIC X(10) VALUE\n           COPY LITBOOK.\n"
# a copybook copying itself: the expander skips the inner COPY as recursive (LESSONS 185)
RECBOOK = ("       R-110-DO.\n           MOVE 1 TO WS-X.\n           COPY RECBOOK.\n       R-199-EXIT.\n           EXIT.\n")


def chain(prefix, n, last=None):
    """[(path, text)] for copybooks P01..Pn where each copies the next, and
    the last copies `last` when given: with n = 13 the COPY in P12 sits at
    depth 12 = expand.MAX_DEPTH and is skipped as nested too deep."""
    files = []
    for k in range(1, n + 1):
        nxt = f"{prefix}{k + 1:02d}" if k < n else last
        body = f"       P-{prefix}{k:02d}.\n           MOVE 1 TO WS-X.\n" + (f"           COPY {nxt}.\n" if nxt else "")
        files.append((f"GC/PROD.GC.COPYLIB/{prefix}{k:02d}.cpy", body))
    return files

ARRIVED = "parsed before it arrived: run recover, then the build"
UNKNOWN_FIX = ("; and rename the folder to end in COPYLIB (or declare the library's kind in the UI's table) so the "
               "copybook's own lines are indexed and citable")
MISFILED = "not a kind the build expands: rename the folder to end in COPYLIB or declare its kind in the UI, then build"
MISFILED_FIX = ("the folder name decided the kind (its text carries no signature - no level numbers, no COBOL statements): "
                "rename the folder to end in COPYLIB, or declare the library's kind in the UI's table (the manifest kinds) "
                "and run the build")


def program(name="SECPGM", data="", book="PROCBOOK"):
    return HEAD.format(name=name, data=data) + SECTION_COPY.format(book=book) + TAIL


def value_program(name="VALPGM", book="LITBOOK", times=1):
    """`05 WS-STATESn PIC X(10) VALUE` with the literal in `book`, COPYed on the next line (`times` fields)."""
    data = "       01  WS-REC.\n" + "".join(f"           05  WS-STATES{k}          PIC X(10) VALUE\n"
                                            f"           COPY {book}.\n" for k in range(1, times + 1))
    return HEAD.format(name=name, data=data) + "       A-100-BEGIN SECTION.\n           MOVE 1 TO WS-X.\n" + TAIL


class _Estate(unittest.TestCase):
    """estate\\GC\\PROD.GC.SRC\\SECPGM.cbl built while PROCBOOK is absent."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        self.first_files()
        self.build(["--rebuild"])

    def first_files(self):
        self.write("GC/PROD.GC.SRC/SECPGM.cbl", program())

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

    def status(self, name="SECPGM", kind="cobol"):
        return self.q("SELECT parse_status FROM member WHERE UPPER(name)=? AND kind=?", name, kind)[0][0]

    def book(self, name="PROCBOOK"):
        return self.q("SELECT kind, library FROM member WHERE UPPER(name)=?", name)

    def copy_use(self, name="PROCBOOK", copier="SECPGM"):
        return self.q("SELECT c.resolved_member_id FROM copy_use c JOIN member m ON m.id=c.member_id "
                      "WHERE UPPER(m.name)=? AND UPPER(c.copybook)=? ORDER BY c.line", copier, name)

    def member_id(self, name, kind="cobol"):
        return self.q("SELECT id FROM member WHERE UPPER(name)=? AND kind=?", name, kind)[0][0]

    def ids(self):
        return self.q("SELECT name, kind, id, parse_status FROM member ORDER BY name, kind")

    def paragraphs(self, name="SECPGM"):
        return self.q("SELECT p.name, p.kind, p.section FROM paragraph p JOIN program g ON g.id=p.program_id "
                      "JOIN member m ON m.id=g.member_id WHERE UPPER(m.name)=? ORDER BY p.start_line", name)

    def outputs(self, name="SECPGM", book="PROCBOOK"):
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            return (cov, cov.split("### Copybooks not found")[1].split("\n###")[0],
                    query.cmd_program(conn, name), query.cmd_copybook(conn, book))
        finally:
            conn.close()

    def assert_parsed_without_the_book(self, name="SECPGM", bookname="PROCBOOK"):
        self.assertEqual(self.status(name), "partial")
        self.assertEqual(self.book(bookname), [])
        self.assertEqual(self.copy_use(bookname, name), [(None,)])
        cov, nf, prog, book = self.outputs(name, bookname)
        self.assertIn(f"| {bookname} | 1 | - |", nf)                                # no member: nothing to say
        self.assertIn(f"| {bookname} | **NOT FOUND** |", prog)
        self.assertIn("**NOT FOUND**\n", book)
        self.assertNotIn("a member with this name exists:", prog)
        self.assertNotIn("a member with this name exists,", book)
        self.assertNotIn("yes:", nf)
        self.assertNotIn("parsed before it arrived", cov)
        self.assertNotIn("A copybook marked `yes`", cov)


class ArrivedAfterTheParse(_Estate):
    """Case A: a copybook lands in estate\\SHARED\\PROD.GC.CPYLIB - a dataset-named folder with no COPY hint -
    after the programs that copy it were parsed. PROCBOOK (COBOL statements) is a copybook by its content now
    and the build makes SECPGM whole at once (ROADMAP re-parse item 20); LITBOOK (no signature) is filed
    'unknown', which forces no re-parse (item 21): recover marks VALPGM."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/SECPGM.cbl", program())
        self.write("GC/PROD.GC.SRC/VALPGM.cbl", value_program())

    def test_a_procedure_copybook_arriving_is_a_copybook_and_the_build_makes_the_program_whole(self):
        self.assert_parsed_without_the_book()
        self.write("SHARED/PROD.GC.CPYLIB/PROCBOOK.txt", PROCBOOK)
        self.build()
        self.assertEqual(self.book(), [("copybook", "PROD.GC.CPYLIB")])
        self.assertEqual(self.status(), "ok")
        self.assertIsNotNone(self.copy_use()[0][0])
        paras = [(p[0], p[1], p[2]) for p in self.paragraphs()]
        self.assertIn(("A-110-DO", "paragraph", "A-100-BEGIN"), paras)
        self.assertIn(("A-199-EXIT", "paragraph", "A-100-BEGIN"), paras)
        # its own lines are indexed and citable: a copybook has a parser (an 'unknown' member had none)
        self.assertGreater(self.q("SELECT COUNT(*) FROM src_fts f JOIN member m ON m.id=f.member_id "
                                  "WHERE m.name='PROCBOOK'")[0][0], 0)
        cov, nf, prog, book = self.outputs()
        self.assertNotIn("PROCBOOK", nf)
        self.assertNotIn("NOT FOUND", prog + book)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("PROCBOOK", said)

    def test_the_incremental_build_leaves_the_program_partial_until_recover_marks_it(self):
        self.assert_parsed_without_the_book("VALPGM", "LITBOOK")
        self.write("SHARED/PROD.GC.CPYLIB/LITBOOK.txt", LITBOOK)
        self.build()
        # ROADMAP re-parse item 21: the member is in the index, of a kind the resolver accepts, and the program
        # that copies it was not parsed again - coverage still says NOT FOUND
        self.assertEqual(self.book("LITBOOK"), [("unknown", "PROD.GC.CPYLIB")])
        self.assertEqual(self.status("VALPGM"), "partial")
        self.assertEqual(self.copy_use("LITBOOK", "VALPGM"), [(None,)])
        cov, nf, prog, book = self.outputs("VALPGM", "LITBOOK")
        # the note says the whole fix: recover + build, AND the folder rename - an 'unknown' member has no
        # handler, so its own lines are not in the index until it is filed as a copybook (LESSONS 184)
        self.assertIn(f"| LITBOOK | 1 | yes: PROD.GC.CPYLIB (unknown) - {ARRIVED}{UNKNOWN_FIX} |", nf)
        self.assertIn("A copybook marked `yes` is not missing", cov)
        self.assertIn("1 of the copybooks reported NOT FOUND has a member with that name in the index now", cov)
        self.assertNotIn("0 exist", cov, "a zero-count clause with an instruction attached")
        self.assertIn(f"| LITBOOK | **NOT FOUND** - a member with this name exists: PROD.GC.CPYLIB (unknown) - {ARRIVED}"
                      f"{UNKNOWN_FIX} |", prog)
        self.assertIn("1 of the programs above still say", book)
        self.assertIn("parsed before this member arrived and nothing parsed them again", book)
        self.assertIn("`python -m atlas.recover --db atlas.db` marks them, then run your usual build" + UNKNOWN_FIX, book)
        # a dry run reports and marks nothing
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (1, 0, 0), said)
        self.assertIn("1 program(s) copy a copybook that has arrived since they were parsed (1 copybook): would be marked "
                      "for the next build", said)
        self.assertNotIn("next: run your usual build command", said)
        self.assertEqual(self.status("VALPGM"), "partial")
        self.assertIn("would be marked for the next build (dry run: nothing marked)", self.report_text())
        # the run marks the program, says so, and the report names copybook, kind, folder and program
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 program(s) copy a copybook that has arrived since they were parsed (1 copybook): marked for the "
                      "next build", said)
        self.assertIn("1 of them filed 'unknown' (a folder with no COPY hint): the build expands it but has no parser for "
                      "it, so its own lines are not indexed - rename the folder to end in COPYLIB", said)
        self.assertIn("next: run your usual build command", said)
        self.assertIn(f"every name: {self.report}", said)
        self.assertEqual(self.status("VALPGM"), "pending")
        rep = self.report_text()
        self.assertIn("## Copybooks that arrived after the program was parsed", rep)
        self.assertIn("| copybook | kind the index filed it as | folder | programs |", rep)
        self.assertIn("| LITBOOK | unknown | PROD.GC.CPYLIB | VALPGM |", rep)
        self.assertIn("so its own lines are not indexed", rep)
        self.assertNotIn("filed as something else", rep)
        # the next build, without --rebuild, parses the program again: whole
        self.build()
        self.assertEqual(self.status("VALPGM"), "ok")
        self.assertEqual(len(self.copy_use("LITBOOK", "VALPGM")), 1)
        self.assertIsNotNone(self.copy_use("LITBOOK", "VALPGM")[0][0])
        cov, nf, prog, book = self.outputs("VALPGM", "LITBOOK")
        self.assertNotIn("LITBOOK", nf)
        self.assertNotIn("NOT FOUND", prog)
        self.assertNotIn("still say", book)
        # the copybook's own lines are still outside the index (no handler for 'unknown'): the reason for the clause
        self.assertEqual(self.status("LITBOOK", "unknown"), "skipped")
        self.assertEqual(self.q("SELECT COUNT(*) FROM src_fts f JOIN member m ON m.id=f.member_id WHERE m.name='LITBOOK'"),
                         [(0,)])
        # nothing left for the step to say about it - and the earlier run's report does not stay in place saying 'marked'
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("has arrived since", said)
        rep = self.report_text()
        self.assertNotIn("arrived after the program was parsed", rep)
        self.assertNotIn("marked for the next build", rep)

    def test_a_program_whose_own_copy_resolved_is_left_alone(self):
        # OTHERPGM arrives together with the book and resolves it at its first parse; VALPGM, parsed
        # earlier, does not - the marking is by member, so only VALPGM is marked, OTHERPGM stays ok
        self.write("GC2/PROD.GC2.SRC/OTHERPGM.cbl", value_program("OTHERPGM"))
        self.write("GC2/PROD.GC2.CPYLIB/LITBOOK.txt", LITBOOK)
        self.build()
        self.assertEqual(self.book("LITBOOK"), [("unknown", "PROD.GC2.CPYLIB")])
        self.assertEqual((self.status("VALPGM"), self.status("OTHERPGM")), ("partial", "ok"))
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (1, 1), said)
        self.assertEqual((self.status("VALPGM"), self.status("OTHERPGM")), ("pending", "ok"))
        self.assertIn("| LITBOOK | unknown | PROD.GC2.CPYLIB | VALPGM |", self.report_text())
        self.build()
        self.assertEqual((self.status("VALPGM"), self.status("OTHERPGM")), ("ok", "ok"))

    def test_copybook_counts_programs_not_copy_sites(self):
        # TWICEPGM copies LITBOOK on two lines: `copybook LITBOOK` counts it once, with VALPGM twice in all
        self.write("GC/PROD.GC.SRC/TWICEPGM.cbl", value_program("TWICEPGM", times=2))
        self.build()
        self.assertEqual(self.copy_use("LITBOOK", "TWICEPGM"), [(None,), (None,)])
        self.write("SHARED/PROD.GC.CPYLIB/LITBOOK.txt", LITBOOK)
        self.build()
        _cov, _nf, _prog, book = self.outputs("VALPGM", "LITBOOK")
        self.assertIn("### Programs including it (3)", book)
        self.assertIn("> 2 of the programs above still say `COPY LITBOOK NOT FOUND`", book)
        self.assertNotIn("3 of the programs", book)

    def test_the_index_side_helpers(self):
        self.write("SHARED/PROD.GC.CPYLIB/LITBOOK.txt", LITBOOK)
        self.build()
        conn = query.connect(self.db)
        try:
            accepted, other = recover.members_named(conn, "litbook")
            self.assertEqual([(k, f) for _i, k, f, _p in accepted], [("unknown", "PROD.GC.CPYLIB")])
            self.assertEqual(other, [])
            self.assertEqual(recover.members_named(conn, "NOPE"), ([], []))
            arrived, misfiled = recover.arrived_copybooks(conn)
            self.assertEqual(misfiled, [])
            self.assertEqual(len(arrived), 1)
            self.assertEqual((arrived[0]["copybook"], arrived[0]["members"]), ("LITBOOK", [("unknown", "PROD.GC.CPYLIB")]))
            self.assertEqual([(n, k) for _i, n, k in arrived[0]["programs"]], [("VALPGM", "cobol")])
            self.assertEqual(recover.missing_copybooks(conn), {"PROCBOOK": 1}, "LITBOOK is not missing: the member exists")
            self.assertEqual(query.same_named_note(conn, "LITBOOK"), f"PROD.GC.CPYLIB (unknown) - {ARRIVED}{UNKNOWN_FIX}")
            self.assertEqual(query.same_named_note(conn, "NOPE"), "")
        finally:
            conn.close()


class FiledAsAnotherKind(_Estate):
    """Case B: a copybook lands in estate\\SHARED\\PROD.GC.PROCS. PROCBOOK (COBOL statements) is a copybook by its
    content now, before the folder name (ROADMAP re-parse item 20): the build makes SECPGM whole at once. LITBOOK
    (no signature) is still a proc by the folder name, which the resolver never expands: recover names it with the
    fix, and coverage / program / copybook say so."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/SECPGM.cbl", program())
        self.write("GC/PROD.GC.SRC/VALPGM.cbl", value_program())

    def test_a_procedure_copybook_in_a_procs_folder_is_a_copybook(self):
        self.assert_parsed_without_the_book()
        self.write("SHARED/PROD.GC.PROCS/PROCBOOK.txt", PROCBOOK)
        self.write("SHARED/downloads/PROCBOOK2.txt", PROCBOOK)
        self.build()
        self.assertEqual(self.book(), [("copybook", "PROD.GC.PROCS")])
        self.assertEqual(self.book("PROCBOOK2"), [("copybook", "downloads")])
        self.assertEqual(self.status(), "ok")
        self.assertIn(("A-110-DO", "paragraph", "A-100-BEGIN"), [(p[0], p[1], p[2]) for p in self.paragraphs()])
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
        self.assertNotIn("PROCBOOK", said)
        self.assertNotIn("next: rename the folder", said)

    def test_recover_names_it_with_the_fix_and_marks_nothing(self):
        self.assert_parsed_without_the_book("VALPGM", "LITBOOK")
        self.write("SHARED/PROD.GC.PROCS/LITBOOK.txt", LITBOOK)
        self.build()
        self.assertEqual(self.book("LITBOOK"), [("proc", "PROD.GC.PROCS")])
        self.assertEqual(self.status("VALPGM"), "partial")
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        # the console line carries the fix itself, and the run's own 'next:' line says it too - not only
        # the line that sends him to the compiler listings
        self.assertIn("1 copybook name(s) exist in the index only as a member of a kind the build does not expand (proc): "
                      "1 program(s) stay parsed only in part until the folder is renamed to end in COPYLIB or the library's "
                      "kind declared in the UI's table - the report names each", said)
        self.assertIn("next: rename the folder(s) the report names to end in COPYLIB (or declare the library's kind in the "
                      "UI's table), then run your usual build command", said)
        self.assertNotIn("has arrived since", said)
        self.assertNotIn("next: run your usual build command", said)
        self.assertIn("missing copybooks in the index: 2", said)             # LITBOOK and SECPGM's PROCBOOK
        self.assertEqual(self.status("VALPGM"), "partial", "a re-parse would find nothing: not marked")
        rep = self.report_text()
        self.assertIn("## A member with the copybook's name exists but is filed as something else", rep)
        self.assertIn("| copybook | filed as | folder | programs | what to do |", rep)
        self.assertIn(f"| LITBOOK | proc | PROD.GC.PROCS | VALPGM | {MISFILED_FIX} |", rep)
        self.assertNotIn("## Copybooks that arrived after the program was parsed", rep)
        # a further build changes nothing: the member is still a proc
        self.build()
        self.assertEqual(self.status("VALPGM"), "partial")

    def test_coverage_program_and_copybook_say_so(self):
        self.write("SHARED/PROD.GC.PROCS/LITBOOK.txt", LITBOOK)
        self.build()
        cov, nf, prog, book = self.outputs("VALPGM", "LITBOOK")
        self.assertIn(f"| LITBOOK | 1 | yes: PROD.GC.PROCS, filed as proc - {MISFILED} |", nf)
        self.assertIn("A copybook marked `yes` is not missing", cov)
        self.assertIn("1 exists only as a member of a kind the build does not expand", cov)
        self.assertNotIn("0 of the copybooks", cov, "a zero-count clause with an instruction attached")
        self.assertIn(f"| LITBOOK | **NOT FOUND** - a member with this name exists: PROD.GC.PROCS, filed as proc - {MISFILED} |",
                      prog)
        self.assertIn("**NOT FOUND** as a copybook - a member with this name exists, filed as proc (folder PROD.GC.PROCS): "
                      "not a kind the build expands, so every program that copies it is parsed only in part. "
                      f"{MISFILED_FIX[0].upper() + MISFILED_FIX[1:]}.", book)
        # the fix, applied: the folder renamed to end in COPYLIB - the build files it as a copybook and re-parses the program
        shutil.move(os.path.join(self.root, "SHARED", "PROD.GC.PROCS"), os.path.join(self.root, "SHARED", "PROD.GC.COPYLIB"))
        self.build()
        self.assertEqual(self.book("LITBOOK"), [("copybook", "PROD.GC.COPYLIB")])
        self.assertEqual(self.status("VALPGM"), "ok")
        cov, nf, prog, book = self.outputs("VALPGM", "LITBOOK")
        self.assertNotIn("LITBOOK", nf)
        self.assertNotIn("NOT FOUND", prog + book)

    def test_two_misfiled_copies_keep_each_folder_paired_with_its_kind(self):
        # the same .txt in a PROCS folder (proc) and in a plain folder (doc): every report says which folder is which
        self.write("SHARED/PROD.GC.PROCS/LITBOOK.txt", LITBOOK)
        self.write("SHARED/downloads/LITBOOK.txt", LITBOOK)
        self.build()
        self.assertEqual(sorted(self.book("LITBOOK")), [("doc", "downloads"), ("proc", "PROD.GC.PROCS")])
        cov, nf, prog, book = self.outputs("VALPGM", "LITBOOK")
        self.assertIn(f"| LITBOOK | 1 | yes: downloads, filed as doc; PROD.GC.PROCS, filed as proc - {MISFILED} |", nf)
        self.assertIn(f"a member with this name exists: downloads, filed as doc; PROD.GC.PROCS, filed as proc - {MISFILED} |",
                      prog)
        self.assertIn("filed as doc (folder downloads); filed as proc (folder PROD.GC.PROCS)", book)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        self.assertIn("(doc, proc)", said)
        self.assertIn("| LITBOOK | doc, proc | downloads, PROD.GC.PROCS | VALPGM |", self.report_text())


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


class NestedCopybookIsNotArrived(_Estate):
    """Case D (LESSONS 184): NESTPGM copies OUTBOOK, which copies INBOOK, both
    present in a COPYLIB folder from the first build - nothing is wrong. The
    copybook's own COPY INBOOK row is recorded with no resolved_member_id
    (the build never resolves a copybook's copies), and reading it as
    'unresolved' called INBOOK 'arrived', marked OUTBOOK pending, and the next
    build re-inserted OUTBOOK under a new id and nulled the program's link."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/NESTPGM.cbl", program("NESTPGM", book="OUTBOOK"))
        self.write("GC/PROD.GC.COPYLIB/OUTBOOK.cpy", OUTBOOK)
        self.write("GC/PROD.GC.COPYLIB/INBOOK.cpy", INBOOK)

    def ids(self):
        return self.q("SELECT name, id, parse_status FROM member WHERE name IN ('NESTPGM','OUTBOOK','INBOOK') ORDER BY name")

    def test_nothing_is_reported_marked_or_broken(self):
        self.assertEqual(self.status("NESTPGM"), "ok")
        # the program carries its own row for the nested COPY, resolved; the copybook's own row is NULL by construction
        self.assertIsNotNone(self.copy_use("OUTBOOK", "NESTPGM")[0][0])
        self.assertIsNotNone(self.copy_use("INBOOK", "NESTPGM")[0][0])
        self.assertEqual(self.copy_use("INBOOK", "OUTBOOK"), [(None,)])
        before = self.ids()
        cov, nf, prog, book = self.outputs("NESTPGM", "INBOOK")
        self.assertIn("_none_", nf, "a copybook every program found is not 'not found'")
        self.assertNotIn("INBOOK", nf)
        self.assertNotIn("yes:", nf)
        self.assertNotIn("NOT FOUND", prog + book)
        self.assertIn("### Members parsed only in part\n_none_", cov)
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.arrived_copybooks(conn), ([], []))
        finally:
            conn.close()
        # recover, build, recover, build: nothing is ever reported, marked or re-inserted - it settles at once
        for _round in range(2):
            stats, said = self.recover()
            self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)
            self.assertNotIn("has arrived since", said)
            self.assertNotIn("next: run your usual build command", said)
            self.assertEqual(self.ids(), before, "no member marked pending")
            self.assertFalse(os.path.exists(self.report) and "arrived after" in self.report_text())
            self.build()
            self.assertEqual(self.ids(), before, "no member re-inserted under a new id")
            self.assertIsNotNone(self.copy_use("OUTBOOK", "NESTPGM")[0][0], "the program's link to OUTBOOK kept")
            self.assertIsNotNone(self.copy_use("INBOOK", "NESTPGM")[0][0])
            cov, nf, prog, _book = self.outputs("NESTPGM", "INBOOK")
            self.assertIn("_none_", nf)
            self.assertNotIn("NOT FOUND", prog)


class NestedCopybookArrivesLater(_Estate):
    """Case E: OUTBOOK is present, INBOOK absent: the program is partial and carries its own unresolved row for
    INBOOK. INBOOK (COBOL statements) arrives in a folder with no COPY hint: a copybook by its content now, so the
    build parses the program again and it is whole (ROADMAP re-parse item 20), OUTBOOK untouched."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/NESTPGM.cbl", program("NESTPGM", book="OUTBOOK"))
        self.write("GC/PROD.GC.COPYLIB/OUTBOOK.cpy", OUTBOOK)

    def test_the_build_makes_the_program_whole_and_leaves_the_copybook_alone(self):
        self.assertEqual(self.status("NESTPGM"), "partial")
        self.assertEqual(self.copy_use("INBOOK", "NESTPGM"), [(None,)])
        self.assertEqual(self.copy_use("INBOOK", "OUTBOOK"), [(None,)])
        cov, nf, _prog, _book = self.outputs("NESTPGM", "INBOOK")
        self.assertIn("| INBOOK | 1 | - |", nf, "one use: the program's - the copybook's own row is not a use")
        outbook_id = self.q("SELECT id FROM member WHERE name='OUTBOOK'")
        self.write("SHARED/PROD.GC.CPYLIB/INBOOK.txt", INBOOK)
        self.build()
        self.assertEqual(self.book("INBOOK"), [("copybook", "PROD.GC.CPYLIB")])
        self.assertEqual(self.status("NESTPGM"), "ok")
        self.assertIsNotNone(self.copy_use("INBOOK", "NESTPGM")[0][0])
        self.assertEqual(self.q("SELECT id FROM member WHERE name='OUTBOOK'"), outbook_id, "the copybook was not touched")
        self.assertIn(("A-150-SUB", "paragraph", "A-100-BEGIN"), [(p[0], p[1], p[2]) for p in self.paragraphs("NESTPGM")])
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)


class NestedLiteralArrivesLater(_Estate):
    """Case E with a member of no signature (ROADMAP re-parse item 21, still open): the data copybook OUTLIT copies
    LITBOOK into a VALUE clause; LITBOOK arrives 'unknown' and forces no re-parse. recover marks the PROGRAM only -
    never the copybook - and the next build makes the program whole (LESSONS 184)."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/NESTPGM.cbl", HEAD.format(name="NESTPGM", data="       01  WS-REC.\n           COPY OUTLIT.\n")
                   + "       A-100-BEGIN SECTION.\n           MOVE 1 TO WS-X.\n" + TAIL)
        self.write("GC/PROD.GC.COPYLIB/OUTLIT.cpy", OUTLIT)

    def test_the_program_is_marked_and_the_copybook_is_left_alone(self):
        self.assertEqual(self.status("NESTPGM"), "partial")
        self.assertEqual(self.copy_use("LITBOOK", "NESTPGM"), [(None,)])
        self.assertEqual(self.copy_use("LITBOOK", "OUTLIT"), [(None,)])
        self.write("SHARED/PROD.GC.CPYLIB/LITBOOK.txt", LITBOOK)
        self.build()
        self.assertEqual(self.book("LITBOOK"), [("unknown", "PROD.GC.CPYLIB")])
        self.assertEqual(self.status("NESTPGM"), "partial")
        outlit_id = self.q("SELECT id FROM member WHERE name='OUTLIT'")
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertIn("1 program(s) copy a copybook that has arrived", said)
        self.assertIn("| LITBOOK | unknown | PROD.GC.CPYLIB | NESTPGM |", self.report_text())
        self.assertNotIn("OUTLIT (copybook)", self.report_text())
        self.assertEqual((self.status("NESTPGM"), self.status("OUTLIT", "copybook")), ("pending", "ok"))
        self.build()
        self.assertEqual(self.status("NESTPGM"), "ok")
        self.assertIsNotNone(self.copy_use("LITBOOK", "NESTPGM")[0][0])
        self.assertEqual(self.q("SELECT id FROM member WHERE name='OUTLIT'"), outlit_id, "the copybook was not touched")
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0), said)


class _SkippedCopy(_Estate):
    """LESSONS 185: expand.py writes a program's NULL copy_use row for a
    system include, a COPY it SKIPPED and a COPY NOT FOUND alike; the arrived
    scan and every note built on it read the NULL as 'not found' and told him
    to run recover for a member the program had found, marked the program on
    every run, and each build left the row NULL: it never settled."""

    PROG = BOOK = WHY = ""

    def assert_skipped_not_arrived(self):
        prog, book, why = self.PROG, self.BOOK, self.WHY
        self.assertEqual(self.status(prog), "partial", "a skipped COPY makes the program truly partial (LESSONS 181)")
        mid = self.member_id(prog)
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.skipped_copies(conn), {(mid, book): why})
            self.assertEqual(recover.skipped_copies(conn, mid), {(mid, book): why})
            self.assertEqual(recover.arrived_copybooks(conn), ([], []), "the member exists and the program was parsed after it")
            self.assertEqual(recover.missing_copybooks(conn), {}, "a copybook copying its own name is not a missing copybook")
        finally:
            conn.close()
        cov, nf, prog_out, book_out = self.outputs(prog, book)
        # coverage: not a copybook not found - the skip is said under the table, and the reason stands
        # beside the program under 'Members parsed only in part'
        self.assertIn("_none_", nf)
        self.assertNotIn(f"| {book} |", nf)
        self.assertNotIn("yes:", nf)
        self.assertIn(f"`COPY {book}` skipped - {why} in {prog}", nf)
        self.assertIn("A skipped COPY is not a copybook not found", nf)
        self.assertNotIn("parsed before it arrived", cov)
        self.assertNotIn("A copybook marked `yes`", cov)
        self.assertNotIn("has a member with that name in the index now", cov)
        self.assertIn(f"| cobol | {prog} | PROD.GC.SRC | expand: ", cov.split("### Members parsed only in part")[1].split("\n###")[0])
        # program: the cell says the skip, never NOT FOUND
        self.assertIn(f"| {book} | **COPY skipped - {why}** - not a copybook not found: the member is in the index and the "
                      "expander stopped there (the notes below say so) |", prog_out)
        self.assertNotIn("NOT FOUND", prog_out)
        # copybook: no 'still says NOT FOUND', no instruction
        self.assertNotIn("still say", book_out)
        self.assertNotIn("NOT FOUND", book_out)
        self.assertIn(f"COPY skipped - {why}", book_out)
        # recover, build, recover, build: nothing reported, nothing marked, nothing re-inserted - it settles at once
        before = self.ids()
        for _round in range(2):
            stats, said = self.recover()
            self.assertEqual((stats["missing"], stats["arrived"], stats["misfiled"], stats["marked"]), (0, 0, 0, 0), said)
            self.assertNotIn("has arrived since", said)
            self.assertNotIn("next:", said)
            self.assertEqual(self.ids(), before, "no member marked pending")
            self.assertFalse(os.path.exists(self.report) and "arrived after" in self.report_text())
            self.build()
            self.assertEqual(self.ids(), before, "no member re-inserted under a new id, no status changed")


class RecursiveCopyIsNotArrived(_SkippedCopy):
    """Case F: RECPGM copies RECBOOK, RECBOOK copies itself - both present
    from the first build; the inner COPY is skipped as recursive."""

    PROG, BOOK, WHY = "RECPGM", "RECBOOK", "recursive"

    def first_files(self):
        self.write("GC/PROD.GC.SRC/RECPGM.cbl", program("RECPGM", book="RECBOOK"))
        self.write("GC/PROD.GC.COPYLIB/RECBOOK.cpy", RECBOOK)

    def test_nothing_arrived_and_nothing_is_marked(self):
        # the program's own rows: the outer COPY resolved, the inner one (inside the copybook) skipped
        rows = self.copy_use("RECBOOK", "RECPGM")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[0] is None for r in rows], [True, False])
        self.assertEqual(self.q("SELECT detail FROM unresolved WHERE kind='expand' AND member_id=?", self.member_id("RECPGM")),
                         [("(in COPY RECBOOK) L3: COPY RECBOOK skipped - recursive",)])
        self.assert_skipped_not_arrived()
        self.assertIsNotNone(self.copy_use("RECBOOK", "RECPGM")[1][0], "the resolved row kept through the rounds")
        # the resolved row still shows in `program`, and coverage's partial table carries the reason (LESSONS 181)
        cov, _nf, prog_out, _book = self.outputs("RECPGM", "RECBOOK")
        self.assertIn("| RECBOOK | RECBOOK |", prog_out)
        self.assertIn("| cobol | RECPGM | PROD.GC.SRC | expand: (in COPY RECBOOK) L3: COPY RECBOOK skipped - recursive |", cov)


class DeepNestingIsNotArrived(_SkippedCopy):
    """Case G: DEEPPGM copies N01, N01 copies N02 ... N12 copies N13, all
    present; COPY N13 sits at depth 12 and is skipped as nested too deep."""

    PROG, BOOK, WHY = "DEEPPGM", "N13", "nesting deeper than 12"

    def first_files(self):
        self.write("GC/PROD.GC.SRC/DEEPPGM.cbl", program("DEEPPGM", book="N01"))
        for rel, text in chain("N", 13):
            self.write(rel, text)

    def test_nothing_arrived_and_nothing_is_marked(self):
        self.assertEqual(self.copy_use("N13", "DEEPPGM"), [(None,)])
        self.assertEqual(self.book("N13"), [("copybook", "PROD.GC.COPYLIB")])
        self.assert_skipped_not_arrived()


class NotFoundWinsOverSkipped(_Estate):
    """Case H: XPGM copies XBOOK on its section line (no member: NOT FOUND)
    and M01, whose chain ends with M12 copying XBOOK at depth 12 (skipped
    without a lookup). The program reports both for the same name; NOT FOUND
    is the fact that holds - XBOOK stays a copybook not found."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/XPGM.cbl", HEAD.format(name="XPGM", data="") + SECTION_COPY.format(book="XBOOK")
                   + "       B-200-MORE SECTION.\n           COPY M01.\n" + TAIL)
        for rel, text in chain("M", 12, last="XBOOK"):
            self.write(rel, text)

    def test_the_name_stays_not_found(self):
        mid = self.member_id("XPGM")
        notes = [r[0] for r in self.q("SELECT detail FROM unresolved WHERE kind='expand' AND member_id=? ORDER BY id", mid)]
        self.assertTrue(any("COPY XBOOK NOT FOUND" in n for n in notes), notes)
        self.assertTrue(any("COPY XBOOK skipped - nesting deeper than 12" in n for n in notes), notes)
        self.assertEqual(self.copy_use("XBOOK", "XPGM"), [(None,), (None,)])
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.skipped_copies(conn), {})
            # two places: the program's own COPY and M12's (a copybook copying a missing one counts, as ever)
            self.assertEqual(recover.missing_copybooks(conn), {"XBOOK": 2})
            self.assertEqual(recover.arrived_copybooks(conn), ([], []))
        finally:
            conn.close()
        cov, nf, prog, book = self.outputs("XPGM", "XBOOK")
        self.assertIn("| XBOOK | 2 | - |", nf)
        self.assertNotIn("skipped", nf)
        self.assertIn("| XBOOK | **NOT FOUND** |", prog)
        self.assertNotIn("COPY skipped", prog)
        self.assertIn("**NOT FOUND**\n", book)


class RemovedCopiesAreNotNothingToReport(_Estate):
    """A run whose only work is removing recovered copies (the real member
    arrived) and marking their programs wrote 'Nothing to report on this run'
    over the report and printed no 'next:' line, while the console had just
    said the programs were marked."""

    def first_files(self):
        self.write("GC/PROD.GC.SRC/DATAPGM.cbl", HEAD.format(name="DATAPGM", data="       01  WS-REC.\n           COPY DATABOOK.\n")
                   + "       A-100-BEGIN SECTION.\n           MOVE 1 TO WS-X.\n" + TAIL)
        # a copy an earlier atlas.recover run wrote, with its marker
        rec = os.path.join(self.root, "SHARED", recover.FOLDER)
        self.write(f"SHARED/{recover.FOLDER}/DATABOOK.cpy", DATABOOK)
        recover._save_marker(rec, {"DATABOOK": {"from": "DATAPGM", "how": "test fixture"}})
        os.makedirs(os.path.dirname(self.report), exist_ok=True)
        with open(self.report, "w", encoding="utf-8") as fh:
            fh.write("# Recovered copybooks - an earlier run\n\n## Written\n\n| DATABOOK | 1 | ... |\n")

    def test_the_report_and_the_next_line_say_what_the_console_said(self):
        self.assertEqual(self.status("DATAPGM"), "ok")
        self.assertIn(recover.FOLDER, self.q("SELECT path FROM member WHERE name='DATABOOK'")[0][0])
        self.write("GC/PROD.GC.COPYLIB/DATABOOK.cpy", DATABOOK)
        self.build()
        self.assertEqual(len(self.book("DATABOOK")), 2)
        stats, said = self.recover()
        self.assertEqual((stats["removed"], stats["arrived"], stats["misfiled"], stats["marked"]), (1, 0, 0, 1), said)
        self.assertIn("1 recovered copybook(s) removed - the estate now holds the real member: DATABOOK", said)
        self.assertIn("1 program(s) that had expanded them are marked for the next build", said)
        self.assertIn("next: run your usual build command", said)
        self.assertEqual(self.status("DATAPGM"), "pending")
        rep = self.report_text()
        self.assertNotIn("Nothing to report on this run", rep)
        self.assertNotIn("an earlier run", rep)
        self.assertIn("## Recovered copies removed - the real member arrived", rep)
        self.assertIn("DATABOOK", rep)
        self.assertIn("1 program(s) that had expanded them are marked for the next build: run your usual build command", rep)
        self.assertFalse(os.path.exists(os.path.join(self.root, "SHARED", recover.FOLDER, "DATABOOK.cpy")))
        self.build()
        self.assertEqual(self.status("DATAPGM"), "ok")
        self.assertEqual(self.book("DATABOOK"), [("copybook", "PROD.GC.COPYLIB")])
        # and a run with nothing left says so, replacing this one's report
        stats, said = self.recover()
        self.assertEqual((stats["removed"], stats["marked"]), (0, 0), said)
        self.assertNotIn("next:", said)
        self.assertIn("Nothing to report on this run", self.report_text())


if __name__ == "__main__":
    unittest.main()
