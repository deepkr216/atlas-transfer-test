r"""
The missing copybooks looked for ON DISK (LESSONS 192).

His run said 'missing copybooks in the index: 27' while the copybooks sat in
the COPYLIB folders he had fetched that day. Only two of the names had a
member row at all - both filed 'empty': a 7-digit number in column 1 and
nothing else, which the fixed-format reader takes for the sequence area and
the indicator column, so it sees no code - and the misfiled scan
(recover.arrival_scan) reads the index only, so the other 25 - on disk under
another name, arrived after the build, or skipped by it - got no word, and
he spent an evening on it. Now recover walks the estate root once, before
any listing is read (a dry run too), and says per missing name what the
disk holds; coverage's 'Copybooks not found' table and `copybook NAME`
carry the same verdict.

Cases: (A) the copybook arrived after the build - on disk, not in the index:
run the build; (B) the folder holds BOOKB-V2 and BOOKBX only - no file,
the near names; (C) the file holds `1234567` in column 1 - an index built
before ROADMAP re-parse item 23 filed it empty (age_stubs gives a build of
the item that shape), and every report says where the text sits; the build
of the item files it `stub` and every report says 'a stub'
(tests/test_stub_copybooks.py); (D) nothing near anywhere - the fetch
sentence; (E) coverage and `copybook` carry the verdicts (on the older
index); (F) an estate with nothing missing prints no
new line and no new section - the console and the report are those of the
toolkit at aba1a2d; (G) a file there at the last build that the build did
not record; (H) 20,000 files and 5 names in under 5 s.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import re
import tarfile
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from atlas import build, query, reader, recover  # noqa: E402
from test_refiled_copybooks import data_program, section_program  # noqa: E402

BOOK = "           05  BK-ID               PIC X(5).\n           05  BK-AMT              PIC 9(3).\n"
ITEM = "ROADMAP re-parse item 23"
STUB_1 = ("the file holds 1 line(s) whose text sits in columns 1-7 - the sequence area and the indicator column of "
          "fixed-format COBOL - so the reader sees no code (ROADMAP re-parse item 23)")
SECTION = "## Missing copybooks, checked on disk"
HEADER = "| copybook | programs copying it | on disk? | what the file is | what to do |"
CHECKED = "  missing copybooks checked on disk: "
NO_FILE = ("no file with this name under the estate: fetch the library the listings name (the fetch list) - or the "
           "library is gone from the host")
NEAR_B = ("no file with this name under the estate; nearest names in the copybook folders: "
          r"BOOKB-V2 (SHARED\PROD.GC.COPYLIB\BOOKB-V2.txt), BOOKBX (SHARED\PROD.GC.COPYLIB\BOOKBX.txt) "
          "(is the COPY statement's name the member's name?)")
NEAR_B_E = NEAR_B.replace(r"BOOKBX (SHARED\PROD.GC.COPYLIB\BOOKBX.txt) ",
                          r"BOOKBX (SHARED\PROD.GC.COPYLIB\BOOKBX.txt), BOOKC (SHARED\PROD.GC.COPYLIB\BOOKC.txt), "
                          r"BOOKA (SHARED\PROD.GC.COPYLIB\BOOKA.txt) ")
NEXT_BUILD = ("  next: run your usual build command - it indexes the 1 copybook(s) that arrived after the last build, and "
              "the programs copying them re-expand by themselves")
NEXT_FETCH = ("  next: fetch the libraries that hold these copybooks - the report's fetch list names the datasets the "
              r"listings say (work\fetch-list.txt)")
NEXT_TABLE = ("  next: the report's table says per file why the build did not index it as a copybook - a member filed "
              "empty (its text in columns 1-7), a skipped file (atlas-problems.txt)")
# no member filed empty holds a number on disk (an index this toolkit built): comments and blank lines only (LESSONS 205)
NEXT_TABLE_BLANK = NEXT_TABLE.replace("(its text in columns 1-7)", "(comments and blank lines only)")


def age_stubs(db):
    """Give the index the shape a build before ROADMAP re-parse item 23 left for a 7-digit stub: the member filed
    'empty' (the fixed-format reader saw no code in columns 1-7) and every program copying it carrying 'COPY X NOT
    FOUND' in place of the stub note - what his index holds until the re-parse night. Only a stub within columns 1-7
    is aged: the 8-digit one was a copybook then, expanded into its programs."""
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT id, path FROM member WHERE kind = 'stub'").fetchall()
        assert rows, "the estate must hold a stub to age"
        for mid, path in rows:
            text, data, enc = reader.load(path)
            # what that build did: no code the reader sees in columns 8-72 - a number within columns 1-7, with or without
            # a sequence number in 73-80 beside it (LESSONS 205) - and the member was filed empty
            assert build.code_line_count("copybook", text, data, enc) == 0, f"{path}: only a stub the reader saw no code in"
            conn.execute("UPDATE member SET kind = 'empty' WHERE id = ?", (mid,))
        for uid, detail in conn.execute("SELECT id, detail FROM unresolved WHERE kind = 'expand' "
                                        "AND detail LIKE '%only numbers%'").fetchall():
            m = re.match(r"(.*?COPY \S+): the member", detail)
            conn.execute("UPDATE unresolved SET detail = ? WHERE id = ?",
                         (f"{m.group(1)} NOT FOUND - fields/code from it are missing from this program's facts", uid))
        conn.commit()
    finally:
        conn.close()


class _Estate(unittest.TestCase):
    """estate\\GC\\PROD.GC.SRC\\<program>.cbl and estate\\SHARED\\PROD.GC.COPYLIB\\<copybook>.txt, built with --rebuild;
    `aged`: then given the shape an index built before ROADMAP re-parse item 23 has (age_stubs)."""

    files = ()
    build_args = ()
    aged = False

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        for rel, text in self.files:
            self.write(rel, text)
        self.build(["--rebuild", *self.build_args])
        if self.aged:
            age_stubs(self.db)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

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

    def status(self, name):
        return self.q("SELECT parse_status FROM member WHERE UPPER(name)=? AND kind='cobol'", name)[0][0]

    def outputs(self, book):
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            return cov, cov.split("### Copybooks not found")[1].split("\n###")[0], query.cmd_copybook(conn, book)
        finally:
            conn.close()

    def disk_row(self, name):
        rep = self.report_text()
        self.assertIn(SECTION, rep)
        sec = rep.split(SECTION)[1].split("\n## ")[0]
        self.assertIn(HEADER, sec)
        rows = [ln for ln in sec.splitlines() if ln.startswith(f"| {name} | ")]
        self.assertEqual(len(rows), 1, sec)
        return rows[0]


class ArrivedAfterTheBuild(_Estate):
    """Case A: PGMA copies BOOKA; BOOKA.txt lands in the COPYLIB folder after the build."""

    files = (("GC/PROD.GC.SRC/PGMA.cbl", data_program("PGMA", "BOOKA", "BK-ID")),)

    def setUp(self):
        super().setUp()
        self.path = self.write("SHARED/PROD.GC.COPYLIB/BOOKA.txt", BOOK)

    def test_on_disk_arrived_after_the_last_build_and_the_build_settles_it(self):
        self.assertEqual(self.status("PGMA"), "partial")
        for dry in (True, False):                                   # a dry run looks too
            stats, said = self.recover(dry_run=dry)
            self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"]), (1, 1, 1, 0), said)
            self.assertIn(CHECKED + "1 on disk (1 arrived after the last build - not in the index yet)", said)
            self.assertIn(NEXT_BUILD, said)
            self.assertLess(said.index("missing copybooks in the index: 1"), said.index(CHECKED))
            self.assertLess(said.index(CHECKED), said.index(NEXT_BUILD))
            row = self.disk_row("BOOKA")
            self.assertIn(r"| BOOKA | 1 (PGMA:8) | yes: SHARED\PROD.GC.COPYLIB\BOOKA.txt - not in the index | copybook (COBOL "
                          "level numbers with no PROCEDURE DIVISION), 2 code line(s) | arrived after the last build (file dated ",
                          row)
            self.assertIn(", last build started ", row)
            self.assertTrue(row.endswith("): run your usual build command |"), row)
        rep = self.report_text()
        # right after the summary lines, before everything else
        self.assertLess(rep.index("- missing in the index: 1"), rep.index(SECTION))
        self.assertNotIn("## Written", rep)
        self.assertIn("a --from folder is read for expanded programs, not for copybooks", rep)
        # the build indexes it, and the name is not missing any more: no line, no section
        self.build()
        self.assertEqual(self.status("PGMA"), "ok")
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"]), (0, 0, 0, 0), said)
        self.assertNotIn("checked on disk", said)
        self.assertNotIn(SECTION, self.report_text())

    def test_a_copied_file_keeps_the_original_date_and_the_creation_time_says_it_arrived(self):
        old = time.time() - 30 * 24 * 3600
        os.utime(self.path, (old, old))                             # Explorer keeps the modification time of the original
        self.assertGreater(recover.file_dated(self.path), old + 1)
        stats, said = self.recover()
        self.assertEqual((stats["arrived_late"], stats["no_file"]), (1, 0), said)
        self.assertIn("arrived after the last build", self.disk_row("BOOKA"))


class NearNamesOnly(_Estate):
    """Case B: PGMB copies BOOKB; the folder holds BOOKB-V2.txt and BOOKBX.txt only."""

    files = (("GC/PROD.GC.SRC/PGMB.cbl", data_program("PGMB", "BOOKB", "BK-ID")),
             ("SHARED/PROD.GC.COPYLIB/BOOKB-V2.txt", BOOK),
             ("SHARED/PROD.GC.COPYLIB/BOOKBX.txt", BOOK))

    def test_no_file_the_nearest_names_listed(self):
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"]), (1, 0, 0, 1), said)
        self.assertIn(CHECKED + "1 with no file under the estate (1 with a near name)", said)
        self.assertIn(NEXT_FETCH + "; the 1 near name(s) the report lists may be the members under another name: check them "
                      "against the COPY statements", said)
        self.assertEqual(self.disk_row("BOOKB"), f"| BOOKB | 1 (PGMB:8) | no | - | {NEAR_B} |")


class TheStubFiledEmpty(_Estate):
    """Case C, on an index built before ROADMAP re-parse item 23: PGMC copies BOOKC on its section line; BOOKC.txt
    holds `1234567` in column 1 - columns 1-6 are the sequence area and column 7 the indicator, so the reader saw no
    code and that build filed the member empty. The stand-ins keep saying where the text sits there; the build of the
    item files it `stub` (tests/test_stub_copybooks.py)."""

    aged = True

    files = (("GC/PROD.GC.SRC/PGMC.cbl", section_program("PGMC", "BOOKC")),
             ("SHARED/PROD.GC.COPYLIB/BOOKC.txt", "1234567\n"))

    def test_every_report_says_where_the_text_sits(self):
        self.assertEqual(self.q("SELECT kind, parse_status FROM member WHERE name='BOOKC'"), [("empty", "skipped")])
        self.assertEqual(self.status("PGMC"), "partial")
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"], stats["misfiled"]),
                         (1, 1, 0, 0, 1), said)
        self.assertIn(CHECKED + "1 on disk (1 in the index filed empty - no code lines the reader sees)", said)
        self.assertIn(NEXT_TABLE, said)
        row = self.disk_row("BOOKC")
        self.assertIn(r"| BOOKC | 1 (PGMC:11) | yes: SHARED\PROD.GC.COPYLIB\BOOKC.txt - in the index, filed as empty | "
                      + STUB_1 + " | open the file: if the number is all it holds, the library copy is a stub and the "
                      "copybook's text is in the listings of the programs copying it (this run reads them) or on the host "
                      f"(fetch the member again); the build of {ITEM} files such a member `stub` and never expands it |", row)
        rep = self.report_text()
        misfiled = rep.split("## A member with the copybook's name exists but is filed as something else")[1]
        cell = [ln for ln in misfiled.splitlines() if ln.startswith("| BOOKC | empty | PROD.GC.COPYLIB | PGMC | ")][0]
        self.assertIn(f"| no folder change helps (the content decided); not re-filed: {STUB_1} | filed as empty by its content "
                      f"(the classifier read it as copybook (library folder PROD.GC.COPYLIB) and the build filed it empty: {STUB_1}) |",
                      cell)
        self.assertNotIn("no code lines, nothing a program could copy", rep)
        self.assertNotIn("comments and blanks only", rep)
        cov, nf, book = self.outputs("BOOKC")
        self.assertIn(f"| BOOKC | 1 | yes: PROD.GC.COPYLIB, filed as empty by its content (the classifier read it as copybook "
                      f"(library folder PROD.GC.COPYLIB) and the build filed it empty: {STUB_1}) - not a kind the build expands, "
                      f"and no folder change helps (the content decided); not re-filed: {STUB_1} | in the index (the column "
                      "before says as what) |", nf)
        self.assertIn(f"**NOT FOUND** as a copybook - a member with this name exists, filed as empty by its content (folder "
                      f"PROD.GC.COPYLIB; the classifier read it as copybook (library folder PROD.GC.COPYLIB) and the build filed "
                      f"it empty: {STUB_1})", book)
        self.assertIn(f"No folder change helps (the content decided); not re-filed: {STUB_1}.", book)
        self.assertNotIn("Looked for on disk", book)                # the member note says it: nothing to add

    def test_a_member_of_comments_only_keeps_the_old_sentence(self):
        p = self.write("SHARED/PROD.GC.COPYLIB/BOOKC.txt", "      * A COMMENT AND NOTHING ELSE\n\n")
        r = recover.how_classified(p, "empty")
        self.assertEqual(r["stub"], 0)
        self.assertEqual(r["seen"], "the classifier read it as copybook (library folder PROD.GC.COPYLIB) and the build filed it "
                                    "empty: no code lines, nothing a program could copy")
        self.assertEqual(recover.refile_verdict(r, "PROD.GC.COPYLIB"),
                         (False, "it has no code lines (comments and blanks only): nothing a program could copy"))

    def test_two_stub_lines_and_the_verdict(self):
        p = self.write("SHARED/PROD.GC.COPYLIB/BOOKC.txt", "1234567\n7654321\n")
        r = recover.how_classified(p, "empty")
        self.assertEqual(r["stub"], 2)
        self.assertTrue(str(r["seen"]).endswith("the file holds 2 line(s) whose text sits in columns 1-7 - the sequence area and "
                                                "the indicator column of fixed-format COBOL - so the reader sees no code "
                                                f"({ITEM})"), r["seen"])
        ok, why = recover.refile_verdict(r, "PROD.GC.COPYLIB")
        self.assertFalse(ok)
        self.assertIn("holds 2 line(s) whose text sits in columns 1-7", why)


class TheNumberedStubFiledEmpty(_Estate):
    """The verifier's round (LESSONS 205), on an index built before ROADMAP re-parse item 23: BOOKN holds `1234567`
    with an ISPF sequence number in columns 73-80, BOOKM two comments and a blank line numbered there. That build filed
    both empty. BOOKN's row said 'no code lines (comments and blanks only)' - stub_lines() counts text within columns
    1-7 only - and BOOKM's 'open the file: if the number is all it holds'. Now each says what the file holds, and the
    'what to do' comes from the file on disk: EMPTY_TODO for the number, the comments-and-blank-lines one for BOOKM."""

    aged = True

    files = (("GC/PROD.GC.SRC/PGMN.cbl", section_program("PGMN", "BOOKN")),
             ("GC/PROD.GC.SRC/PGMM.cbl", section_program("PGMM", "BOOKM")),
             ("SHARED/PROD.GC.COPYLIB/BOOKN.txt", "1234567".ljust(72) + "00010000\n"),
             # filed empty by this build too (no number where the compiler reads): not aged, the same row either way
             ("SHARED/PROD.GC.COPYLIB/BOOKM.txt", "      * RETIRED".ljust(72) + "00010000\n"
              + "      * NO FIELDS".ljust(72) + "00020000\n" + " " * 72 + "00030000\n"))

    def test_what_the_file_holds_and_what_to_do(self):
        self.assertEqual(self.q("SELECT name, kind FROM member WHERE name IN ('BOOKN', 'BOOKM') ORDER BY name"),
                         [("BOOKM", "empty"), ("BOOKN", "empty")])
        stats, said = self.recover()
        self.assertIn(CHECKED + "2 on disk (2 in the index filed empty - no code lines the reader sees)", said)
        self.assertIn(NEXT_TABLE, said)                                 # BOOKN holds a number: columns 1-7, as before
        numbers = ("the file holds only numbers (1 line) and no code the reader sees - a stub, not the copybook's text; "
                   f"the build of {ITEM} files such a member `stub` and never expands it")
        self.assertEqual(recover.numbers_sentence(1), numbers)
        self.assertEqual(self.disk_row("BOOKN"), r"| BOOKN | 1 (PGMN:11) | yes: SHARED\PROD.GC.COPYLIB\BOOKN.txt - in the "
                                                 f"index, filed as empty | {numbers} | {recover.EMPTY_TODO} |")
        self.assertEqual(self.disk_row("BOOKM"), r"| BOOKM | 1 (PGMM:11) | yes: SHARED\PROD.GC.COPYLIB\BOOKM.txt - in the "
                                                 "index, filed as empty | no code lines (comments and blanks only): nothing "
                                                 f"a program could copy | {recover.EMPTY_BLANK_TODO} |")
        rep = self.report_text()
        misfiled = rep.split("## A member with the copybook's name exists but is filed as something else")[1]
        cell = [ln for ln in misfiled.splitlines() if ln.startswith("| BOOKN | empty | ")][0]
        self.assertIn(f"not re-filed: {numbers} |", cell)
        self.assertNotIn("comments and blanks only", cell)
        r = recover.how_classified(os.path.join(self.root, "SHARED", "PROD.GC.COPYLIB", "BOOKN.txt"), "empty")
        self.assertEqual((r["stub"], r["numbers"]), (0, 1))
        self.assertTrue(str(r["seen"]).endswith("the build filed it empty: " + numbers), r["seen"])
        _cov, nf, book = self.outputs("BOOKN")
        self.assertIn(numbers, nf)
        self.assertIn(numbers, book)
        self.assertNotIn("comments and blanks only", book)


class NothingNearAnywhere(_Estate):
    """Case D: PGMD copies QZXWVUTS, a name no file comes near."""

    files = (("GC/PROD.GC.SRC/PGMD.cbl", data_program("PGMD", "QZXWVUTS", "BK-ID")),
             ("SHARED/PROD.GC.COPYLIB/OTHERBK.txt", BOOK))

    def test_the_fetch_sentence(self):
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["no_file"]), (1, 0, 1), said)
        self.assertIn(CHECKED + "1 with no file under the estate\n", said + "\n")
        self.assertNotIn("with a near name", said)
        self.assertIn(NEXT_FETCH + "\n", said + "\n")
        self.assertEqual(self.disk_row("QZXWVUTS"), f"| QZXWVUTS | 1 (PGMD:8) | no | - | {NO_FILE} |")


class CoverageAndCopybookCarryTheVerdicts(_Estate):
    """Case E: cases A-D in one estate, on an index built before ROADMAP re-parse item 23 (BOOKC filed empty);
    coverage's table and `copybook NAME` say what the disk says."""

    aged = True

    files = (("GC/PROD.GC.SRC/PGMA.cbl", data_program("PGMA", "BOOKA", "BK-ID")),
             ("GC/PROD.GC.SRC/PGMB.cbl", data_program("PGMB", "BOOKB", "BK-ID")),
             ("GC/PROD.GC.SRC/PGMC.cbl", section_program("PGMC", "BOOKC")),
             ("GC/PROD.GC.SRC/PGMD.cbl", data_program("PGMD", "QZXWVUTS", "BK-ID")),
             ("SHARED/PROD.GC.COPYLIB/BOOKB-V2.txt", BOOK),
             ("SHARED/PROD.GC.COPYLIB/BOOKBX.txt", BOOK),
             ("SHARED/PROD.GC.COPYLIB/BOOKC.txt", "1234567\n"))

    def setUp(self):
        super().setUp()
        self.write("SHARED/PROD.GC.COPYLIB/BOOKA.txt", BOOK)

    def test_coverage_and_copybook(self):
        cov, nf, _b = self.outputs("BOOKA")
        self.assertIn("| copybook | uses | a member with this name exists? | on disk? |", nf)
        self.assertIn(r"| BOOKA | 1 | - | on disk at SHARED\PROD.GC.COPYLIB\BOOKA.txt, not in the index - arrived after the last "
                      "build (file dated ", nf)
        # the near names here: the two that start with the name first, then the close ones (BOOKC and BOOKA are 4 of 5
        # characters in order: 0.8) - the other estate's own files, not noise from elsewhere
        self.assertIn(f"| BOOKB | 1 | - | {NEAR_B_E} |", nf)
        self.assertIn(f"| BOOKC | 1 | yes: PROD.GC.COPYLIB, filed as empty by its content (the classifier read it as copybook "
                      f"(library folder PROD.GC.COPYLIB) and the build filed it empty: {STUB_1})", nf)
        self.assertIn("| in the index (the column before says as what) |", nf)
        self.assertIn(f"| QZXWVUTS | 1 | - | {NO_FILE} |", nf)
        conn = query.connect(self.db)
        try:
            a, b, c, d = (query.cmd_copybook(conn, n) for n in ("BOOKA", "BOOKB", "BOOKC", "QZXWVUTS"))
            prog = query.cmd_program(conn, "PGMA")
        finally:
            conn.close()
        head = f"**NOT FOUND**\n\nLooked for on disk under the estate root `{self.root}`: "
        self.assertIn(head + r"on disk at SHARED\PROD.GC.COPYLIB\BOOKA.txt, not in the index - arrived after the last build "
                      "(file dated ", a)
        self.assertIn(head + NEAR_B_E + ".\n", b)
        self.assertIn(STUB_1, c)
        self.assertNotIn("Looked for on disk", c)
        self.assertIn(head + NO_FILE + ".\n", d)
        self.assertIn("| BOOKA | **NOT FOUND** |", prog)               # `program` is unchanged
        # the run's console counts every case once
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"]), (4, 2, 1, 2), said)
        self.assertIn(CHECKED + "2 on disk (1 arrived after the last build - not in the index yet, 1 in the index filed "
                      "empty - no code lines the reader sees), 2 with no file under the estate (1 with a near name)", said)
        self.assertIn(NEXT_BUILD, said)                              # the build first: a file arrived after it
        self.assertNotIn(NEXT_FETCH, said)

    def test_the_verdicts_are_computed_for_the_names_with_no_member_only(self):
        conn = query.connect(self.db)
        try:
            root, cells = query.disk_verdicts(conn, ["BOOKA", "QZXWVUTS"])
        finally:
            conn.close()
        self.assertEqual(root, self.root)
        self.assertEqual(sorted(cells), ["BOOKA", "QZXWVUTS"])
        self.assertEqual(cells["QZXWVUTS"], NO_FILE)


class NothingMissingChangesNothing(_Estate):
    """Case F: a whole estate prints no new line and no new section - and the console and the report are those of
    the toolkit at aba1a2d, to the line (the report's date line apart)."""

    files = (("GC/PROD.GC.SRC/WHOLE.cbl", data_program("WHOLE", "WHOLEBK", "BK-ID")),
             ("SHARED/PROD.GC.COPYLIB/WHOLEBK.txt", BOOK))

    def test_no_new_line_and_no_new_section(self):
        self.assertEqual(self.status("WHOLE"), "ok")
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"]), (0, 0, 0, 0), said)
        self.assertNotIn("checked on disk", said)
        self.assertNotIn("looked for on disk", said)
        self.assertFalse(os.path.exists(self.report), "nothing to report: no report is written, as before")

    def test_the_same_console_and_report_as_aba1a2d(self):
        old = _old_toolkit(self.td)
        if not old:
            self.skipTest("git, or the commit aba1a2d, is not available here")
        stats, said = self.recover()
        old_report = os.path.join(self.td, "work", "recover-old.md")
        code = ("import json, sys\nsys.path.insert(0, sys.argv[1])\nfrom atlas import recover\nsaid = []\n"
                "s = recover.run(sys.argv[2], log=said.append, report=sys.argv[3])\n"
                "print(json.dumps({'said': said, 'stats': s}))\n")
        r = subprocess.run([sys.executable, "-c", code, old, self.db, old_report], capture_output=True, text=True, cwd=old)
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got["said"], said.split("\n"))
        for k, v in got["stats"].items():
            self.assertEqual(stats[k], v if not isinstance(v, list) else tuple(v), k)
        self.assertFalse(os.path.exists(old_report))
        self.assertFalse(os.path.exists(self.report))
        # and with something missing, the console differs by the two new lines alone; the old toolkit wrote no
        # report for an estate with no listings (nothing to say), this one writes the disk section
        self.write("GC/PROD.GC.SRC/PGMD.cbl", data_program("PGMD", "QZXWVUTS", "BK-ID"))
        self.build()
        stats, said = self.recover()
        r = subprocess.run([sys.executable, "-c", code, old, self.db, old_report], capture_output=True, text=True, cwd=old)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        new_lines = [ln for ln in said.split("\n") if ln.startswith(("  missing copybooks checked on disk", "  next: "))]
        self.assertEqual(len(new_lines), 2, said)
        rest = [ln for ln in said.split("\n") if ln not in new_lines]
        # the old toolkit had nothing to write and said nothing about the report; this one wrote the disk section
        self.assertEqual(rest[-1], f"every name: {self.report}")
        self.assertEqual(rest[:-1], got["said"], said)
        self.assertFalse(os.path.exists(old_report))
        new_text = self.report_text()
        self.assertIn(SECTION, new_text)
        self.assertNotIn("\n## ", new_text.split(SECTION)[1])         # the disk section is the only section


def _old_toolkit(td):
    """The atlas package at aba1a2d, extracted under td (git archive); None when that cannot be had."""
    try:
        r = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", "aba1a2d", "atlas"], capture_output=True)
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    out = os.path.join(td, "old")
    os.makedirs(out, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(out)
    return out if os.path.isfile(os.path.join(out, "atlas", "recover.py")) else None


class ThereAtTheLastBuild(_Estate):
    """Case G: BOOKE.txt was on disk when the build ran, and the build stopped before it listed the SHARED folder
    (--limit 3: the root, GC and GC\\PROD.GC.SRC): not in the index, there at the last build."""

    files = (("GC/PROD.GC.SRC/PGME.cbl", data_program("PGME", "BOOKE", "BK-ID")),
             ("SHARED/PROD.GC.COPYLIB/BOOKE.txt", BOOK))
    build_args = ("--limit", "3")

    def build(self, extra=()):
        # the build's start is recorded to the second: a file written in the same second reads as arrived after it
        # (the harmless side - 'run the build'), so the file here is a second older than the build
        if "--rebuild" in extra:
            time.sleep(1.1)
        return super().build(extra)

    def test_there_at_the_last_build_and_the_next_build_indexes_it(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM member WHERE name='BOOKE'"), [(0,)])
        stats, said = self.recover()
        self.assertEqual((stats["missing"], stats["on_disk"], stats["arrived_late"], stats["no_file"]), (1, 1, 0, 0), said)
        self.assertIn(CHECKED + "1 on disk (1 there at the last build yet not in the index)", said)
        self.assertIn(NEXT_TABLE_BLANK, said)
        row = self.disk_row("BOOKE")
        self.assertIn(r"| BOOKE | 1 (PGME:8) | yes: SHARED\PROD.GC.COPYLIB\BOOKE.txt - not in the index | copybook (COBOL level "
                      "numbers with no PROCEDURE DIVISION), 2 code line(s) | was there at the last build (file dated ", row)
        self.assertIn("): the build did not index it - its kind would be copybook; see atlas-problems.txt for a skip (a time "
                      "limit, unreadable, too large) and the coverage report's 'failed' table, or a build that stopped before "
                      "it recorded every file |", row)
        self.build()
        self.assertEqual(self.status("PGME"), "ok")
        stats, said = self.recover()
        self.assertEqual(stats["missing"], 0, said)
        self.assertNotIn("checked on disk", said)


class RootNotOnDisk(_Estate):
    """The estate moved since the build: nothing is walked, and the console and coverage say so."""

    files = (("GC/PROD.GC.SRC/PGMD.cbl", data_program("PGMD", "GONEBK", "BK-ID")),)

    def test_said_not_walked(self):
        moved = self.root + "-moved"
        os.rename(self.root, moved)
        try:
            stats, said = self.recover(out_dir=os.path.join(self.td, "out"))
            self.assertEqual((stats["missing"], stats["on_disk"], stats["no_file"]), (1, 0, 0), said)
            self.assertIn(f"  the estate root {self.root} is not on disk from here: the missing names were not looked for on "
                          "disk", said)
            self.assertNotIn("checked on disk", said)
            self.assertFalse(os.path.exists(self.report))           # nothing else to report: no report, as before
            cov, nf, book = self.outputs("GONEBK")
            self.assertIn(f"| GONEBK | 1 | - | the estate root {self.root} is not on disk from here: not looked for on disk |", nf)
            self.assertIn(f"**NOT FOUND**\n\nLooked for on disk: the estate root {self.root} is not on disk from here: not looked "
                          "for on disk.\n", book)
        finally:
            os.rename(moved, self.root)


class TheHelpers(unittest.TestCase):

    def test_stub_lines(self):
        self.assertEqual(recover.stub_lines("1234567\n"), 1)
        self.assertEqual(recover.stub_lines("1234567"), 1)          # no line end at all
        self.assertEqual(recover.stub_lines("1234567\n7654321\n\n"), 2)
        # a comment mark alone in column 7 is a comment line, not the number of ROADMAP re-parse item 23 (LESSONS 206)
        self.assertEqual(recover.stub_lines("      *\n"), 0)
        self.assertEqual(recover.stub_lines("000100*\n1234567\n"), 0)
        self.assertEqual(recover.stub_lines("12345678\n"), 0)        # column 8 is code
        self.assertEqual(recover.stub_lines("1234567\n       05 X PIC X.\n"), 0)
        self.assertEqual(recover.stub_lines("      * A COMMENT\n"), 0)
        self.assertEqual(recover.stub_lines(""), 0)
        self.assertEqual(recover.stub_lines("\n\n   \n"), 0)
        self.assertEqual(recover.stub_lines("\t  1\n"), 1)           # a tab is four blanks to the reader: the 1 in column 7
        self.assertEqual(recover.stub_lines("1234567\n000200\n"), 2)  # a number in column 7 beside a sequence number alone
        # only a sequence number, in columns 1-6: a blank line to the compiler - never the columns-1-7 sentence, which
        # points at ROADMAP re-parse item 23 (LESSONS 206); sequence_lines counts it
        for text, n in (("123456\n", 1), ("000100\n000200\n000300\n", 3), ("\t1\n", 1), (" " * 72 + "00010000\n", 1),
                        ("000100".ljust(72) + "00010000\n", 1)):
            self.assertEqual((recover.stub_lines(text), recover.sequence_lines(text)), (0, n), text)
        for text in ("1234567\n", "000100* RETIRED\n000200\n", "      *\n", "", "\n\n", "000100\n       05 X PIC X.\n"):
            self.assertEqual(recover.sequence_lines(text), 0, text)
        self.assertEqual(recover.stub_sentence(3), "the file holds 3 line(s) whose text sits in columns 1-7 - the sequence area "
                                                    "and the indicator column of fixed-format COBOL - so the reader sees no "
                                                    f"code ({ITEM})")
        self.assertEqual(recover.sequence_sentence(1), "the file holds only sequence numbers (1 line: text in columns 1-6 or "
                                                       "73-80 alone) - blank lines to the compiler, so no code: nothing a "
                                                       "program could copy")
        self.assertIn("(3 lines: text", recover.sequence_sentence(3))
        self.assertEqual(recover.empty_sentence(0, 0, 0, "none"), "none")
        self.assertEqual(recover.empty_sentence(2, 2, 0, "none"), recover.stub_sentence(2))
        self.assertEqual(recover.empty_sentence(0, 1, 0, "none"), recover.numbers_sentence(1))
        self.assertEqual(recover.empty_sentence(0, 0, 3, "none"), recover.sequence_sentence(3))

    def test_near_names_order_and_cap(self):
        stems = sorted(["BOOKB-V2", "BOOKBX", "BOOKA", "BOOKC", "BOOK", "B", "ZZZ", "BOOKB.CPY"])
        files = {s: [os.path.join("r", "SHARED", "LIB", s + ".txt")] for s in stems}
        near = recover.near_names("BOOKB", stems, files, "r")
        names = [s for s, _p in near]
        # the stems that start with the name first (an extension or a suffix in the stem), then the stems the name
        # starts with, then the close ones; `B` is too short to count as a stem the name starts with
        self.assertEqual(names[:4], ["BOOKB-V2", "BOOKB.CPY", "BOOKBX", "BOOK"])
        self.assertNotIn("B", names)
        self.assertNotIn("ZZZ", names)
        self.assertEqual(near[0][1], os.path.join("SHARED", "LIB", "BOOKB-V2.txt"))
        many = sorted(f"BOOKB{k:02d}" for k in range(10))
        near = recover.near_names("BOOKB", many, {s: ["r/" + s] for s in many}, "r")
        self.assertEqual(len(near), recover.NEAR_MAX + 1)
        self.assertEqual(near[-1], ("+4 more", ""))

    def test_console_line_and_next(self):
        def v(status):
            return {"status": status}
        checked = {"A": v("late"), "B": v("late"), "C": v("indexed"), "D": v("there"), "E": v("near"), "F": v("none")}
        self.assertEqual(recover.disk_line(checked),
                         CHECKED + "4 on disk (2 arrived after the last build - not in the index yet, 1 in the index filed "
                                   "empty - no code lines the reader sees, 1 there at the last build yet not in the index), "
                                   "2 with no file under the estate (1 with a near name)")
        self.assertEqual(recover.disk_counts(checked), {"on_disk": 4, "arrived_late": 2, "no_file": 2, "there": 1,
                                                        "indexed": 1, "near": 1, "stub": 0})
        self.assertTrue(recover.disk_next(checked).startswith("  next: run your usual build command - it indexes the 2 copybook(s)"))
        self.assertEqual(recover.disk_next({"E": v("near"), "F": v("none")}),
                         NEXT_FETCH + "; the 1 near name(s) the report lists may be the members under another name: check "
                                      "them against the COPY statements")
        self.assertEqual(recover.disk_next({"F": v("none")}), NEXT_FETCH)
        self.assertEqual(recover.disk_next({"C": v("indexed"), "D": v("there")}), NEXT_TABLE_BLANK)
        numbered = {"status": "indexed", "numbers": 1}                  # a number on disk: an index built before item 23
        self.assertEqual(recover.disk_next({"C": numbered, "D": v("there")}), NEXT_TABLE)
        self.assertEqual(recover.disk_next({"D": v("there")}), NEXT_TABLE_BLANK)
        self.assertEqual(recover.disk_line({"F": v("none")}), CHECKED + "1 with no file under the estate")
        self.assertEqual(recover.disk_counts({}), {"on_disk": 0, "arrived_late": 0, "no_file": 0, "there": 0, "indexed": 0,
                                                   "near": 0, "stub": 0})
        # a stub (ROADMAP re-parse item 23): counted on disk, said as one, its own 'next:' when nothing comes first
        self.assertEqual(recover.disk_counts({"S": v("stub")})["on_disk"], 1)
        self.assertEqual(recover.disk_line({"S": v("stub"), "F": v("none")}),
                         CHECKED + "1 on disk (1 in the index as a stub - only numbers, not the copybook's text), 1 with no "
                                   "file under the estate")
        self.assertEqual(recover.disk_next({"S": v("stub")}), recover.STUB_NEXT)
        self.assertEqual(recover.disk_next({"S": v("stub"), "F": v("none")}), NEXT_FETCH)
        self.assertEqual(recover.disk_next({"S": v("stub"), "A": v("late")})[:40], NEXT_BUILD[:40])
        self.assertEqual(recover.disk_next({"S": v("stub"), "C": v("indexed")}), NEXT_TABLE_BLANK)

    def test_last_build_started(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE build_run (id INTEGER PRIMARY KEY, started_at TEXT, root TEXT)")
        self.assertIsNone(recover.last_build_started(conn))
        conn.execute("INSERT INTO build_run(started_at) VALUES ('2026-09-24T21:05:03')")
        self.assertEqual(recover.last_build_started(conn), time.mktime((2026, 9, 24, 21, 5, 3, 0, 0, -1)))
        conn.execute("INSERT INTO build_run(started_at) VALUES ('not a date')")
        self.assertIsNone(recover.last_build_started(conn))
        conn.close()

    def test_estate_files_lists_what_the_build_lists(self):
        td = tempfile.mkdtemp()
        try:
            def touch(rel):
                p = os.path.join(td, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write("x")
            touch("SHARED/PROD.GC.COPYLIB/A.txt")
            touch("SHARED/PROD.GC.COPYLIB/a.cpy")                    # the same stem, twice
            touch("GC/PROD.GC.CPYSRC/B.txt")                         # no COPY hint: a copybook folder only by the index
            touch("GC/PROD.GC.SRC/C.cbl")
            touch("GC/PROD.GC.SRC/C.exp.cbl")                        # never listed
            touch("GC/PROD.GC.SRC/.hidden.txt")
            touch(".git/D.txt")
            touch("out/.atlas-output")
            touch("out/E.txt")
            touch("SHARED/RECOVERED-COPYBOOKS/F.cpy")
            files, copy_stems = recover.estate_files(td, ["PROD.GC.CPYSRC"])
            self.assertEqual(sorted(files), ["A", "B", "C", "F"])
            self.assertEqual(len(files["A"]), 2)
            self.assertEqual(copy_stems, {"A", "B", "F"})           # RECOVERED-COPYBOOKS ends in COPYBOOKS: a copybook folder
        finally:
            shutil.rmtree(td, ignore_errors=True)


class TwentyThousandFiles(unittest.TestCase):
    """Case H: the walk is one pass over the estate and the map lives in a local - 20,000 files and 5 names in
    under 5 s (his estate holds 121k)."""

    def test_under_five_seconds(self):
        td = tempfile.mkdtemp()
        try:
            root = os.path.join(td, "estate")
            for k in range(20):
                d = os.path.join(root, "SYS", f"PROD.LIB{k:02d}.COPYLIB")
                os.makedirs(d)
                for j in range(1000):
                    fd = os.open(os.path.join(d, f"CPY{k:02d}{j:03d}.txt"), os.O_WRONLY | os.O_CREAT, 0o644)
                    os.close(fd)
            db = os.path.join(td, "t.db")
            conn = sqlite3.connect(db)
            with open(os.path.join(ROOT, "atlas", "schema.sql"), encoding="utf-8") as fh:
                conn.executescript(fh.read())
            conn.execute("INSERT INTO build_run(started_at, root) VALUES (?, ?)",
                         (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 5)), root))   # after every file
            conn.commit()
            names = ["CPY00001", "CPY19999", "CPY99999", "NOTHERE1", "CPY0"]
            t0 = time.time()
            checked = recover.disk_check(names, root, conn)
            took = time.time() - t0
            conn.close()
            self.assertLess(took, 5.0, f"{took:.1f} s")
            self.assertEqual({n: v["status"] for n, v in checked.items()},
                             {"CPY00001": "there", "CPY19999": "there", "CPY99999": "near", "NOTHERE1": "none", "CPY0": "near"})
            self.assertEqual(checked["CPY00001"]["path"], os.path.join("SYS", "PROD.LIB00.COPYLIB", "CPY00001.txt"))
        finally:
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
