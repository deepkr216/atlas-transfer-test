"""
The listing says which library each copybook came from; the build's choice
is checked against it.

Where a copybook's name exists in several libraries with different content
the build's resolver GUESSES which copy to expand (COPY..OF, then the
program's own system in its declared order, then authoritative, then the
same folder, then the first) and records an 'ambiguous_copybook' row. His
compiler listings end with a table naming, per copybook, the DD name and the
LIBRARY DATASET the compiler read it from - the compiler's record.
atlas.recover reads that table from every such program's listing, stores it
(listing_copy_source, each row dated: is the listing the compile of the
program as indexed?) and gives each choice a verdict. A name is not a fact
about content (LESSONS 193: his listings' SYSLIB is the staging library the
compile ran against, the copybooks promoted to production unchanged), so
the listing's library is looked up in the index and its copy's text
compared before anything is called wrong: CONFIRMED (the same dataset, or a
held copy with the same text - promoted), NOT HELD (a library the index
does not hold: the copy used stands), OLDER (different text, but an older
compile's listing), CONTRADICTED (a current listing, a held copy, different
text: the wrong fact), UNKNOWN. The fact tables do not change; the build
reads the rows before its chain since ROADMAP re-parse item 19
(tests/test_listing_resolver.py, tests/test_listing_systems.py), and this
file pins the check on an index built before that - the stand-in must keep
working there.
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
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from atlas import build, query, recover  # noqa: E402
from test_recover import ibm_listing, osvs_listing  # noqa: E402

PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. {name}.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-OUT              PIC X(5).
       01  WS-REC.
{copies}
       PROCEDURE DIVISION.
           MOVE DUP-FIELD-A TO WS-OUT.
           GOBACK.
"""
DUPREC_CLAIMS = ["           05  DUP-FIELD-A   PIC X(5)."]
DUPREC_POLICY = ["           05  DUP-FIELD-B   PIC X(9)."]
NOPE = ["           05  NOPE-FIELD    PIC X(3)."]


def program(name, *copybooks):
    return PROGRAM.format(name=name, copies="\n".join(f"           COPY {c}." for c in copybooks))


def listing(name, copybooks, table):
    return ibm_listing(program(name, *copybooks).splitlines(),
                       {"DUPREC": DUPREC_CLAIMS, "NOPE": NOPE}, copy_table=table)


class CopySourcesParser(unittest.TestCase):
    """Rows are known by shape: a member, a DD name, a dotted dataset, then
    numbers and dates only. Nothing depends on the heading's words."""

    def test_his_layout(self):
        rows = recover.copy_sources([
            "0Copybook  Ddname    Library dataset                                Text  Created     Last modified",
            "  COPYBOOK  SYSLIB    AB.ABCC.COPYLIB                                3     1997/01/29 2018/11/11 08:00:27",
            "  SECOND    SYSLIB    AB.ABCC.COPYLIB2                               12    2001/03/04 2019/12/31 23:59:59",
            "0THIRD     MYLIB     AB.OTHER.LIB(THIRD)                            1     1997/01/29 2018/11/11 08:00:27",
            "  LONGDSN   SYSLIB    A2345678.B2345678.C2345678.D2345678.E2345678     1",     # 44 characters: the longest allowed
        ])
        self.assertEqual([r[:3] for r in rows], [("COPYBOOK", "SYSLIB", "AB.ABCC.COPYLIB"),
                                                  ("SECOND", "SYSLIB", "AB.ABCC.COPYLIB2"),
                                                  ("THIRD", "MYLIB", "AB.OTHER.LIB"),
                                                  ("LONGDSN", "SYSLIB", "A2345678.B2345678.C2345678.D2345678.E2345678")])
        self.assertEqual(rows[0][3], "3 1997/01/29 2018/11/11 08:00:27")
        self.assertEqual(rows[2][3], "1 1997/01/29 2018/11/11 08:00:27")     # a `0` carriage control glued to the name
        self.assertEqual(rows[3][3], "1")

    def test_the_heading_and_anything_else_is_ignored(self):
        self.assertEqual(recover.copy_sources([
            "  COPY/BASIS  Statistics",
            "  Copybook  DDNAME  Dataset name  Text  Created  Last modified",
            "  Cross-reference of programs",
            "  ABC       SYSLIB    AB.ABCC.COPYLIB   NOTANUMBER",              # words after the dataset: not a row
            "  ABC       TOOLONGDD AB.ABCC.COPYLIB   3",                       # a DD name of 9 characters
            "  ABC       SYSLIB    NODOTS            3",                       # a dataset has at least one dot
            "  ABC       SYSLIB    A2345678.B2345678.C2345678.D2345678.E2345678.F  1",   # over 44 characters
            "  ABC       SYSLIB    TOOLONGQUAL.X     3",                       # a qualifier of 9 characters
            "  A-B       SYSLIB    AB.ABCC.COPYLIB   3",                       # a hyphen is no member-name character
            "",
        ]), [])

    def test_a_numbered_source_line_is_never_a_row(self):
        # a listing's source line: the line number, the copy mark, then the record - never mistaken for a row
        self.assertEqual(recover.copy_sources([
            "   000120         COPYBOOK SYSLIB AB.ABCC.COPYLIB",
            "   000120C        SYSLIB   AB.ABCC.COPYLIB   3",
            "   000600     COPY PMASTREC.",
            " 00120           SYSLIB   AB.ABCC.COPYLIB   3",                 # an older compiler's five-digit line
            "   000120   1     MOVE PROD.X.Y TO WS",
        ]), [])

    def test_a_listing_without_the_table_gives_nothing(self):
        prog = program("TESTPGM", "DUPREC").splitlines()
        self.assertEqual(recover.copy_sources(ibm_listing(prog, {"DUPREC": DUPREC_CLAIMS}).splitlines()), [])
        self.assertEqual(recover.copy_sources(osvs_listing(prog, {"DUPREC": DUPREC_CLAIMS}).splitlines()), [])

    def test_a_listing_with_the_table(self):
        text = listing("TESTPGM", ["DUPREC", "NOPE"], [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB"), ("NOPE", "SYSLIB", "PROD.X.COPYLIB")])
        self.assertEqual([r[:3] for r in recover.copy_sources(text.splitlines())],
                         [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB"), ("NOPE", "SYSLIB", "PROD.X.COPYLIB")])


class ChoicesCheckedAgainstTheListings(unittest.TestCase):
    """estate\\SYSTEM\\LIBRARY\\member. DUPREC exists in CLAIMS, POLICY and a
    third library with different content; every CLAIMS program takes the
    CLAIMS copy (same system). The listings say which copy was right. The
    index is one ANOTHER toolkit built (its last build carries another
    fingerprint - what an index built before ROADMAP re-parse item 19 looks
    like to this tool): the next build re-parses every member and reads the
    listing first, so recover marks nothing and changes no parse_status."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        files = {
            "CLAIMS/PROD.CLAIMS.SRC/CHOSEN.cbl": program("CHOSEN", "DUPREC"),      # the listing agrees
            "CLAIMS/PROD.CLAIMS.SRC/WRONGPK.cbl": program("WRONGPK", "DUPREC"),    # the listing names POLICY's library
            "CLAIMS/PROD.CLAIMS.SRC/LIBTBL.cbl": program("LIBTBL", "DUPREC"),      # ... a library known through the `library` table
            "CLAIMS/PROD.CLAIMS.SRC/FETCHIT.cbl": program("FETCHIT", "DUPREC"),    # ... a library the index does not hold
            "CLAIMS/PROD.CLAIMS.SRC/NOTABLE.cbl": program("NOTABLE", "DUPREC"),    # the listing has no table
            "CLAIMS/PROD.CLAIMS.SRC/NOLIST.cbl": program("NOLIST", "DUPREC"),      # no listing at all
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "POLICY/POLCOPY2/DUPREC.cpy": "           05  DUP-FIELD-C   PIC X(2).\n",
            "POLICY/POLCOPY2/.atlas-library.json": json.dumps({"dataset": "PROD.POLICY.COPYLIB2", "folder": "x", "rc": 0,
                                                              "expected": 1, "present": 1, "complete": True,
                                                              "missing": [], "stale": []}),
            "CLAIMS/PROD.CLAIMS.LISTING/CHOSEN.lst": listing("CHOSEN", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/WRONGPK.lst": listing("WRONGPK", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/LIBTBL.lst": listing("LIBTBL", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB2")]),
            "CLAIMS/PROD.CLAIMS.LISTING/FETCHIT.lst": listing("FETCHIT", ["DUPREC"], [("DUPREC", "COPYLIB", "PROD.SHARED.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/NOTABLE.lst": listing("NOTABLE", ["DUPREC"], None),
        }
        for rel, text in files.items():
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        assert rc == 0
        conn = sqlite3.connect(cls.db)
        try:
            conn.execute("UPDATE build_run SET fingerprint='0000000000000000'")     # built by another toolkit
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def run_it(self, **kw):
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report, **kw)
        return stats, said

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT program, copybook, ddname, dataset FROM listing_copy_source ORDER BY program, copybook").fetchall()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()

    def report_text(self):
        with open(self.report, encoding="utf-8") as fh:
            return fh.read()

    def test_1_the_build_chose_the_claims_copy_everywhere(self):
        conn = query.connect(self.db)
        try:
            picks = recover.chosen_picks(conn)
            self.assertEqual(sorted(p[0] for p in picks), ["CHOSEN", "FETCHIT", "LIBTBL", "NOLIST", "NOTABLE", "WRONGPK"])
            self.assertTrue(all(p[1] == "DUPREC" and "PROD.CLAIMS.COPYLIB" in p[2] and p[3] == "same system" for p in picks), picks)
            self.assertEqual(recover.missing_copybooks(conn), {})
            libs = recover.library_datasets(conn)
            self.assertEqual(list(libs.values()), ["PROD.POLICY.COPYLIB2"])
            self.assertEqual(recover.dataset_of(os.path.join(self.root, "POLICY", "POLCOPY2", "DUPREC.cpy"), libs), "PROD.POLICY.COPYLIB2")
            self.assertEqual(recover.dataset_of(picks[0][2], libs), "PROD.CLAIMS.COPYLIB")      # the folder is named after the dataset
            self.assertIsNone(recover.dataset_of(os.path.join(self.root, "POLICY", "NOTADSN", "X.cpy"), libs))
        finally:
            conn.close()

    LINE = ("copybook choices checked against the listings: 1 confirmed, 2 contradicted by a current listing, 0 named by an "
            "older listing, 1 name a library the index does not hold, 2 unknown")

    def test_2_dry_run_checks_and_stores_nothing(self):
        stats, said = self.run_it(dry_run=True)
        self.assertEqual(stats["checked"], (1, 2, 0, 1, 2), said)
        self.assertIsNone(self.rows(), "a dry run writes nothing")
        self.assertIn(self.LINE, "\n".join(said))

    def test_3_the_verdicts_the_line_and_the_report(self):
        stats, said = self.run_it()
        text = "\n".join(said)
        self.assertEqual(stats["written"], 0)
        self.assertIn("nothing to recover: every copybook the programs copy is in the index", text)
        self.assertIn("expanded texts to read: 5 of 5 found", text)
        self.assertIn("the 6 with a copybook chosen among several (to check the choice against the listing)", text)
        self.assertIn(self.LINE + " - every choice contradicted by a current listing is a wrong fact in the index: the report "
                      "names each one with the library the listing says", text)
        # an index another toolkit built: the next build re-parses every member and follows the listing - nothing marked
        self.assertIn("  2 programs whose current listing names a held copy with different text: the next build re-parses every "
                      "member (the toolkit changed since the index was built) and reads the listing first", text)
        self.assertNotIn("marked for the next build", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertNotIn("recovered: 0 of 0", text)
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 2, 0, 1, 2), 0))
        conn = query.connect(self.db)
        try:
            by = {v["program"]: v for v in recover.check_choices(conn)}
        finally:
            conn.close()
        self.assertEqual((by["CHOSEN"]["verdict"], by["CHOSEN"]["why"], by["CHOSEN"]["current"]), ("CONFIRMED", "", True))
        self.assertEqual((by["WRONGPK"]["verdict"], by["WRONGPK"]["listing_datasets"], by["WRONGPK"]["used_dataset"]),
                         ("CONTRADICTED", ["PROD.POLICY.COPYLIB"], "PROD.CLAIMS.COPYLIB"))
        # the listing is current (the source it echoes is the program as indexed), the index holds POLICY's copy and its
        # text differs from the CLAIMS copy the build used: the real wrong fact
        self.assertEqual((by["WRONGPK"]["current"], by["WRONGPK"]["matched"], by["WRONGPK"]["why"]),
                         (True, 100, "a current listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used"))
        self.assertIn("the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build chose the other", by["WRONGPK"]["index_has"])
        self.assertEqual(by["LIBTBL"]["verdict"], "CONTRADICTED")
        self.assertIn("the index holds that copy at POLICY/POLCOPY2/DUPREC.cpy", by["LIBTBL"]["index_has"])     # known through `library`
        # a library the index does not hold says nothing against the copy used: the build could only choose among its own
        self.assertEqual((by["FETCHIT"]["verdict"], by["FETCHIT"]["index_has"], by["FETCHIT"]["why"]),
                         ("NOT HELD", "not a library the index holds", recover.NOT_HELD_WHY))
        self.assertIn("the copy it used stands", recover.NOT_HELD_WHY)
        self.assertIn("a staging library, or one gone from the host", recover.NOT_HELD_WHY)
        self.assertEqual((by["NOTABLE"]["verdict"], by["NOTABLE"]["why"]), ("UNKNOWN", "the program's listing has no copybook-source table"))
        self.assertEqual((by["NOLIST"]["verdict"], by["NOLIST"]["why"]), ("UNKNOWN", "no listing of the program was read"))
        rep = self.report_text()
        sec = rep.split("## Copybook choices, checked against the listings")[1]
        self.assertIn("- 6 choices checked: 1 confirmed, 2 contradicted by a current listing, 0 named by an older listing, 1 name a "
                      "library the index does not hold, 2 unknown (1: no listing of the program was read; "
                      "1: the program's listing has no copybook-source table)", sec)
        self.assertIn("A name is not a fact about content", sec)
        self.assertIn("| program | copybook | the index used | the listing says | listing current? | verdict | why |", sec)
        self.assertIn("- 2 of the contradicted are named by a current listing - the next build re-parses every member (the "
                      "toolkit changed since the index was built) and reads the listing first", sec)
        self.assertIn("| WRONGPK | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | yes | CONTRADICTED - re-parsed by the next build (toolkit changed) | a "
                      "current listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used |", sec)
        self.assertIn("| LIBTBL | DUPREC | ", sec)
        self.assertIn("| FETCHIT | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      f"PROD.SHARED.COPYLIB (COPYLIB) - not a library the index holds | yes | NOT HELD | {recover.NOT_HELD_WHY} |", sec)
        self.assertLess(sec.index("| WRONGPK |"), sec.index("| FETCHIT |"), "contradicted first, then not held")
        self.assertNotIn("| CHOSEN |", sec)
        self.assertNotIn("| NOTABLE |", sec)
        self.assertNotIn("_no choice contradicted", sec)
        self.assertIn("ROADMAP re-parse item 19", sec)
        # the stored rows: one per (program, copybook) named; a listing without a table leaves an empty row; every listing
        # here echoes the program as indexed, so every row is dated current
        rows = self.rows()
        self.assertEqual([r for r in rows if r[1]], [("CHOSEN", "DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB"),
                                                     ("FETCHIT", "DUPREC", "COPYLIB", "PROD.SHARED.COPYLIB"),
                                                     ("LIBTBL", "DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB2"),
                                                     ("WRONGPK", "DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")])
        self.assertEqual([r for r in rows if not r[1]], [("NOTABLE", "", None, None)])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT program, current, matched FROM listing_copy_source WHERE copybook<>'' "
                                          "ORDER BY program").fetchall(),
                             [("CHOSEN", 1, 100), ("FETCHIT", 1, 100), ("LIBTBL", 1, 100), ("WRONGPK", 1, 100)])
        finally:
            conn.close()
        # the build's facts are untouched: no parse_status changed (nothing marked on an index another toolkit built),
        # every 'ambiguous_copybook' row still there
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='ambiguous_copybook'").fetchone()[0], 6)
            # the six chosen programs are `ok` (ROADMAP re-parse item 18): whole for the copy named, none partial
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM member WHERE kind='cobol' AND parse_status='ok'").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM member WHERE parse_status='partial'").fetchone()[0], 0)
        finally:
            conn.close()

    def test_4_a_second_run_replaces_the_rows(self):
        self.run_it()
        first = self.rows()
        self.run_it()
        self.assertEqual(self.rows(), first, "rows are replaced per program, never added twice")
        self.assertEqual(len(first), 5)

    def test_5_coverage_program_and_copybook_say_what_the_listings_say(self):
        self.run_it()
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            chosen = cov.split("### Complete, with a copybook chosen among several")[1].split("\n###")[0]
            self.assertIn("1 of these choices is confirmed by the program's listing, 2 contradicted by a current listing (see "
                          "work/recover.md), 0 named by an older listing, 1 name a library the index does not hold, 2 unknown - "
                          "the listing names the library the compiler read the copybook from; a name is not a fact about "
                          "content, so the listing's copy is compared by text with the copy used before a choice is called "
                          "wrong.", chosen)
            out = query.cmd_program(conn, "CHOSEN")
            self.assertIn("- listing says: DUPREC came from PROD.CLAIMS.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
            out = query.cmd_program(conn, "WRONGPK")
            self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - CONTRADICTS the copy the build used "
                          "(PROD.CLAIMS.COPYLIB; the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build "
                          "chose the other; a current listing: its source matches this program as indexed) - see work/recover.md", out)
            self.assertIn("parse: complete (copybook chosen among several - see notes)", out)      # unchanged
            out = query.cmd_program(conn, "FETCHIT")
            self.assertIn("- listing says: DUPREC came from PROD.SHARED.COPYLIB (COPYLIB) - names PROD.SHARED.COPYLIB, a library "
                          "the index does not hold - the copy the build used stands", out)
            self.assertNotIn("CONTRADICTS", out)
            self.assertNotIn("listing says", query.cmd_program(conn, "NOLIST"))
            self.assertNotIn("listing says", query.cmd_program(conn, "NOTABLE"))
            out = query.cmd_copybook(conn, "DUPREC")
            self.assertIn("The programs' compiler listings say this copybook came from: PROD.CLAIMS.COPYLIB (1 program), "
                          "PROD.POLICY.COPYLIB (1 program), PROD.POLICY.COPYLIB2 (1 program), PROD.SHARED.COPYLIB (1 program)", out)
        finally:
            conn.close()

    def test_6_nothing_is_said_before_recover_has_run(self):
        fresh = os.path.join(self.td, "fresh.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([self.root, "--db", fresh, "--rebuild", "--quiet"])
        conn = query.connect(fresh)
        try:
            self.assertNotIn("confirmed by the program's listing", query.cmd_coverage(conn))
            self.assertNotIn("listing says", query.cmd_program(conn, "CHOSEN"))
            self.assertNotIn("compiler listings say", query.cmd_copybook(conn, "DUPREC"))
        finally:
            conn.close()


class TwoListingsOfOneProgram(unittest.TestCase):
    """A program with two listings - one current naming the dataset the build used, one older naming
    another library the index holds with different text: the current listing decides (CONFIRMED); the
    other way round (the older one confirms, the current one names the other copy) is CONTRADICTED."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        for rel, text in {"CLAIMS/PROD.CLAIMS.SRC/CHOSEN.cbl": program("CHOSEN", "DUPREC"),
                          "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
                          "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n"}.items():
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            assert build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"]) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def verdict(self, rows):
        conn = query.connect(self.db)
        try:
            checks = recover.check_choices(conn, {"CHOSEN": rows})
        finally:
            conn.close()
        self.assertEqual([c["copybook"] for c in checks], ["DUPREC"])
        return checks[0]

    def test_the_current_listing_decides(self):
        v = self.verdict([("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB", "cur.lst", 1, 100),
                          ("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "old.lst", 0, 90)])
        self.assertEqual((v["verdict"], v["current"], v["named"]), ("CONFIRMED", True, "PROD.CLAIMS.COPYLIB"))
        self.assertIn("a current listing names the dataset the build used; another listing names PROD.POLICY.COPYLIB", v["why"])
        v = self.verdict([("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB", "old.lst", 0, 90),
                          ("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "cur.lst", 1, 100)])
        self.assertEqual((v["verdict"], v["named"]), ("CONTRADICTED", "PROD.POLICY.COPYLIB"))


class CheckedWhileRecovering(unittest.TestCase):
    """A program that copies a missing copybook AND took a chosen one: its
    listing is read for the missing copybook, and the choice is checked on
    the same read - the recovery path, not the check-only one. The index is
    this toolkit's own build, so the contradicted program (the index holds
    the listing's copy) is marked for the next build."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        files = {
            "CLAIMS/PROD.CLAIMS.SRC/MISSPGM.cbl": program("MISSPGM", "DUPREC", "NOPE"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "CLAIMS/PROD.CLAIMS.LISTING/MISSPGM.lst": listing("MISSPGM", ["DUPREC", "NOPE"],
                                                              [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB"), ("NOPE", "SYSLIB", "PROD.X.COPYLIB")]),
        }
        for rel, text in files.items():
            p = os.path.join(self.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build._main([self.root, "--db", self.db, "--rebuild", "--quiet"]), 0)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_recovered_and_checked_on_one_read(self):
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        text = "\n".join(said)
        self.assertEqual((stats["written"], stats["missing"], stats["checked"]), (1, 1, (0, 1, 0, 0, 0)), text)
        self.assertIn("recovered: 1 of 1 missing copybooks", text)
        self.assertIn("copybook choices checked against the listings: 0 confirmed, 1 contradicted by a current listing, 0 named "
                      "by an older listing, 0 name a library the index does not hold, 0 unknown", text)
        self.assertIn("expanded texts to read: 1 of 1 found", text)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertIn("| NOPE | 1 |", rep)
        self.assertIn("| MISSPGM | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | yes | CONTRADICTED - marked for the next build | a current listing names "
                      "PROD.POLICY.COPYLIB, whose text differs from the copy the build used |", rep)
        self.assertIn("  1 program whose current listing names a held copy with different text is marked for the next build - it "
                      "follows the listing", text)
        self.assertIn("next: run your usual build command - the programs that copy them re-expand by themselves (and the programs "
                      "marked above)", text)
        self.assertEqual(stats["marked"], 1)
        # the recovered copybook is not in the listing's table for anything the index chose: NOPE's row is stored all the same
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT copybook, dataset FROM listing_copy_source WHERE program='MISSPGM' ORDER BY 1").fetchall()
            self.assertEqual(conn.execute("SELECT parse_status FROM member WHERE name='MISSPGM' AND kind='cobol'").fetchone()[0], "pending")
        finally:
            conn.close()
        self.assertEqual(rows, [("DUPREC", "PROD.POLICY.COPYLIB"), ("NOPE", "PROD.X.COPYLIB")])


STG_COMMENT = "      * promoted to production unchanged: the same code as the CLAIMS copy\n"


def older_listing(name, copybooks, table, change=("PIC X(5).", "PIC X(6).")):
    """A listing from an older compile: the program's source as it was then,
    one line different from the member as indexed."""
    src = program(name, *copybooks).replace(change[0], change[1], 1)
    return ibm_listing(src.splitlines(), {"DUPREC": DUPREC_CLAIMS, "NOPE": NOPE}, copy_table=table)


class TheListingIsDated(unittest.TestCase):
    """listing_is_current: the listing's own program lines (the unflagged
    records - a copied line carries a mark) against the member's, both read
    the way the parser reads a member. Equal: current. Else older, with how
    much of the source still matches."""

    PROG = program("DATED", "DUPREC").splitlines()
    TABLE = [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB")]

    def records(self, text):
        return recover.listing_records(text.splitlines())[0]

    def test_the_same_source_is_current(self):
        recs = self.records(listing("DATED", ["DUPREC"], self.TABLE))
        self.assertTrue(any(f == "C" for f, _r, _n in recs), "the copied lines are there, flagged")
        self.assertEqual(recover.listing_is_current(recs, self.PROG), (True, 100))

    def test_one_changed_line_is_older_with_the_match(self):
        recs = self.records(older_listing("DATED", ["DUPREC"], self.TABLE))
        self.assertEqual(recover.listing_is_current(recs, self.PROG), (False, 90))      # 9 of 10 code lines the same

    def test_comments_blank_lines_and_the_columns_outside_8_72_do_not_date(self):
        # the member as indexed gained sequence numbers, change stamps, a comment and a blank line: the same program
        member = ["000100" + ln[6:].ljust(66) + "CHG00001" for ln in self.PROG]
        member.insert(2, "000150* a comment the listing does not show")
        member.insert(5, "")
        recs = self.records(listing("DATED", ["DUPREC"], self.TABLE))
        self.assertEqual(recover.listing_is_current(recs, member), (True, 100))

    def test_nothing_to_date_against(self):
        recs = self.records(listing("DATED", ["DUPREC"], self.TABLE))
        self.assertEqual(recover.listing_is_current(recs, None), (None, None))          # the program is not in the index
        self.assertEqual(recover.listing_is_current([], self.PROG), (None, None))       # no source column read
        self.assertEqual(recover.listing_is_current([("C", "       01  X PIC X.", 1)], self.PROG), (None, None))

    def test_the_dating_cell(self):
        self.assertEqual(recover.dated_cell(True, 100), "yes")
        self.assertEqual(recover.dated_cell(False, 97), "older (97%)")
        self.assertEqual(recover.dated_cell(None, None), "not dated")


def staging_estate(td):
    """The owner's estate (LESSONS 193), written under td/estate, with one
    listing outside it under td/from. DUPREC exists in CLAIMS (the copy the
    build uses: same system), in PROD.STG.COPYLIB - the staging library the
    compiles ran against, promoted to production unchanged (a comment
    differs, the code does not) - and in POLICY with different code."""
    table = lambda dsn: [("DUPREC", "SYSLIB", dsn)]   # noqa: E731
    files = {
        "estate/CLAIMS/PROD.CLAIMS.SRC/PROMOTED.cbl": program("PROMOTED", "DUPREC"),   # the listing names staging: same text
        "estate/CLAIMS/PROD.CLAIMS.SRC/STGGONE.cbl": program("STGGONE", "DUPREC"),     # ... a staging library the index does not hold
        "estate/CLAIMS/PROD.CLAIMS.SRC/OLDLIST.cbl": program("OLDLIST", "DUPREC"),     # an older compile's listing names POLICY's
        "estate/CLAIMS/PROD.CLAIMS.SRC/CURLIST.cbl": program("CURLIST", "DUPREC"),     # a current listing names POLICY's
        "estate/CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
        "estate/STAGING/PROD.STG.COPYLIB/DUPREC.cpy": STG_COMMENT + DUPREC_CLAIMS[0] + "\n",
        "estate/POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        "estate/CLAIMS/PROD.CLAIMS.LISTING/PROMOTED.lst": listing("PROMOTED", ["DUPREC"], table("PROD.STG.COPYLIB")),
        "estate/CLAIMS/PROD.CLAIMS.LISTING/STGGONE.lst": listing("STGGONE", ["DUPREC"], table("PROD.STG2.COPYLIB")),
        "estate/CLAIMS/PROD.CLAIMS.LISTING/OLDLIST.lst": older_listing("OLDLIST", ["DUPREC"], table("PROD.POLICY.COPYLIB")),
        "estate/CLAIMS/PROD.CLAIMS.LISTING/CURLIST.lst": listing("CURLIST", ["DUPREC"], table("PROD.POLICY.COPYLIB")),
        "from/LISTONLY.lst": listing("LISTONLY", ["DUPREC"], table("PROD.POLICY.COPYLIB")),   # a program the index lacks
    }
    for rel, text in files.items():
        p = os.path.join(td, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return os.path.join(td, "estate"), os.path.join(td, "from")


USED = "PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system)"
POLICY_HELD = "the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build chose the other"


class StagingOlderAndUndatedListings(unittest.TestCase):
    """His correction: the listing's SYSLIB is the staging library the compile
    ran against, the copybooks were promoted to production unchanged, and
    the build's own choice was right - a name is not a fact about content.
    His listings may also be a mix of older and current compiles. The five
    verdicts on the estate of staging_estate()."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root, cls.from_dir = staging_estate(cls.td)
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        assert rc == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def run_it(self, **kw):
        said = []
        stats = recover.run(self.db, folders=[self.from_dir], log=said.append, report=self.report, **kw)
        return stats, "\n".join(said)

    def verdicts(self):
        conn = query.connect(self.db)
        try:
            return {v["program"]: v for v in recover.check_choices(conn)}
        finally:
            conn.close()

    def test_1_the_premise(self):
        conn = sqlite3.connect(self.db)
        try:
            picks = recover.chosen_picks(conn)
            self.assertEqual(sorted(p[0] for p in picks), ["CURLIST", "OLDLIST", "PROMOTED", "STGGONE"])
            self.assertTrue(all("PROD.CLAIMS.COPYLIB" in p[2] and p[3] == "same system" for p in picks), picks)
            shas = dict(conn.execute("SELECT path, norm_sha FROM member WHERE name='DUPREC'").fetchall())
            by = {os.path.basename(os.path.dirname(p)): s for p, s in shas.items()}
            self.assertEqual(by["PROD.CLAIMS.COPYLIB"], by["PROD.STG.COPYLIB"], "the same code: the comment does not count")
            self.assertNotEqual(by["PROD.CLAIMS.COPYLIB"], by["PROD.POLICY.COPYLIB"])
        finally:
            conn.close()

    def test_2_the_verdicts_the_console_and_the_report(self):
        stats, text = self.run_it()
        self.assertEqual(stats["checked"], (1, 1, 1, 1, 0), text)
        # this toolkit built the index: the one choice a current listing contradicts is marked for the next build, which
        # follows that listing; the older, the not-held and the promoted choices mark nothing
        self.assertEqual(stats["marked"], 1, text)
        self.assertIn("  1 program whose current listing names a held copy with different text is marked for the next build - it "
                      "follows the listing", text)
        self.assertIn("expanded texts to read: 5 of 5 found (listings + the folders given)", text)
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 1 contradicted by a current listing, 1 named "
                      "by an older listing, 1 name a library the index does not hold, 0 unknown - every choice contradicted by a "
                      "current listing is a wrong fact in the index: the report names each one with the library the listing "
                      "says", text)
        by = self.verdicts()
        # staging, promoted unchanged: the listing names PROD.STG.COPYLIB, the index holds that copy, same text as the copy used
        v = by["PROMOTED"]
        self.assertEqual((v["verdict"], v["why"], v["index_has"], v["current"], v["named"]),
                         ("CONFIRMED", "same text as the copy the listing names in PROD.STG.COPYLIB - promoted",
                          "the index holds that copy at STAGING/PROD.STG.COPYLIB/DUPREC.cpy", True, "PROD.STG.COPYLIB"))
        # the staging library is not in the estate at all: the copy the build used stands
        v = by["STGGONE"]
        self.assertEqual((v["verdict"], v["why"], v["index_has"], v["current"]),
                         ("NOT HELD", recover.NOT_HELD_WHY, "not a library the index holds", True))
        # an older compile's listing (one source line differs from the member): what it names is not a wrong fact
        v = by["OLDLIST"]
        self.assertEqual((v["verdict"], v["current"], v["matched"], v["index_has"]), ("OLDER", False, 90, POLICY_HELD))
        self.assertEqual(v["why"], "an older listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used; "
                                   "the program as indexed may use another copy now; not counted as a wrong fact")
        # the real wrong fact: current listing, held copy, different text
        v = by["CURLIST"]
        self.assertEqual((v["verdict"], v["current"], v["matched"], v["dated"]), ("CONTRADICTED", True, 100, ""))
        self.assertEqual(v["why"], "a current listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used")
        self.assertNotIn("LISTONLY", by, "no choice to check for a program the index does not hold")
        # the report: the counts, then contradicted first, then older, not held, promoted - each dated, each with its why
        with open(self.report, encoding="utf-8") as fh:
            sec = fh.read().split("## Copybook choices, checked against the listings")[1]
        self.assertIn("- 4 choices checked: 1 confirmed, 1 contradicted by a current listing, 1 named by an older listing, 1 name a "
                      "library the index does not hold, 0 unknown\n", sec)
        rows = [ln for ln in sec.splitlines() if ln.startswith("| ") and not ln.startswith("| program")]
        self.assertEqual(rows, [
            f"| CURLIST | DUPREC | {USED} | PROD.POLICY.COPYLIB (SYSLIB) - {POLICY_HELD} | yes | CONTRADICTED - marked for "
            "the next build | a current listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used |",
            f"| OLDLIST | DUPREC | {USED} | PROD.POLICY.COPYLIB (SYSLIB) - {POLICY_HELD} | older (90%) | OLDER | "
            "an older listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used; the program as "
            "indexed may use another copy now; not counted as a wrong fact |",
            f"| STGGONE | DUPREC | {USED} | PROD.STG2.COPYLIB (SYSLIB) - not a library the index holds | yes | NOT HELD | "
            f"{recover.NOT_HELD_WHY} |",
            f"| PROMOTED | DUPREC | {USED} | PROD.STG.COPYLIB (SYSLIB) - the index holds that copy at "
            "STAGING/PROD.STG.COPYLIB/DUPREC.cpy | yes | CONFIRMED | same text as the copy the listing names in "
            "PROD.STG.COPYLIB - promoted |",
        ])
        # the stored rows carry the dating; the listing-only program's row is undated
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT program, dataset, current, matched FROM listing_copy_source ORDER BY program").fetchall(),
                             [("CURLIST", "PROD.POLICY.COPYLIB", 1, 100), ("LISTONLY", "PROD.POLICY.COPYLIB", None, None),
                              ("OLDLIST", "PROD.POLICY.COPYLIB", 0, 90), ("PROMOTED", "PROD.STG.COPYLIB", 1, 100),
                              ("STGGONE", "PROD.STG2.COPYLIB", 1, 100)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='ambiguous_copybook'").fetchone()[0], 4)
        finally:
            conn.close()

    def test_3_no_wrong_fact_no_trailing_clause(self):
        # only the promoted, the not-held and the older programs: the line ends at the counts
        self.run_it()
        conn = sqlite3.connect(self.db)
        try:
            checks = [v for v in recover.check_choices(conn) if v["program"] != "CURLIST"]
        finally:
            conn.close()
        self.assertEqual(recover.choice_counts(checks), (1, 0, 1, 1, 0))
        self.assertEqual(recover.choice_line(checks), "copybook choices checked against the listings: 1 confirmed, 0 contradicted "
                                                      "by a current listing, 1 named by an older listing, 1 name a library the "
                                                      "index does not hold, 0 unknown")
        rep = "".join(recover.choice_report(checks, self.root))
        self.assertIn("_no choice contradicted by a current listing_", rep)
        self.assertIn("| OLDLIST | DUPREC |", rep)                      # the table follows all the same
        self.assertIn("| PROMOTED | DUPREC |", rep)

    def test_4_coverage_program_and_copybook(self):
        self.run_it()
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            self.assertIn("1 of these choices is confirmed by the program's listing, 1 contradicted by a current listing (see "
                          "work/recover.md), 1 named by an older listing, 1 name a library the index does not hold, 0 unknown", cov)
            out = query.cmd_program(conn, "PROMOTED")
            self.assertIn("- listing says: DUPREC came from PROD.STG.COPYLIB (SYSLIB) - confirms the copy the build used (same text, "
                          "promoted from PROD.STG.COPYLIB)", out)
            out = query.cmd_program(conn, "STGGONE")
            self.assertIn("- listing says: DUPREC came from PROD.STG2.COPYLIB (SYSLIB) - names PROD.STG2.COPYLIB, a library the index "
                          "does not hold - the copy the build used stands", out)
            out = query.cmd_program(conn, "OLDLIST")
            self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - an older listing names PROD.POLICY.COPYLIB "
                          "(its source differs from this program as indexed; the text differs from the copy used)", out)
            self.assertNotIn("CONTRADICTS", out)
            out = query.cmd_program(conn, "CURLIST")
            self.assertIn(f"- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - CONTRADICTS the copy the build used "
                          f"(PROD.CLAIMS.COPYLIB; {POLICY_HELD}; a current listing: its source matches this program as indexed) - "
                          "see work/recover.md", out)
            out = query.cmd_copybook(conn, "DUPREC")
            self.assertIn("The programs' compiler listings say this copybook came from: PROD.POLICY.COPYLIB (3 programs; 1 by an "
                          "older listing; 1 whose program is not in the index to date the listing against), PROD.STG.COPYLIB "
                          "(1 program), PROD.STG2.COPYLIB (1 program)", out)
        finally:
            conn.close()

    def test_5_a_choice_on_a_program_the_index_holds_only_as_a_listing(self):
        # the branch for a program that cannot be dated because no COBOL member of its name is in the index: the check
        # says so rather than 'not yet dated' (a hand-made row on a copybook member stands in for it)
        db2 = os.path.join(self.td, "branch.db")
        shutil.copyfile(self.db, db2)
        conn = query.connect(db2)
        try:
            used = conn.execute("SELECT path FROM member WHERE name='DUPREC' AND path LIKE '%CLAIMS%'").fetchone()[0]
            mid = conn.execute("SELECT id FROM member WHERE name='DUPREC' AND path LIKE '%STAGING%'").fetchone()[0]
            conn.execute("INSERT INTO unresolved(member_id, kind, detail, line) VALUES(?,?,?,0)",
                         (mid, "ambiguous_copybook", f"3 copies of DUPREC with different content; used {used} (same system)"))
            conn.commit()
            srcs = {"DUPREC": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "x.lst", None, None)]}
            v = {c["program"]: c for c in recover.check_choices(conn, srcs)}["DUPREC"]
            self.assertEqual((v["verdict"], v["dated"]), ("CONTRADICTED", recover.NOT_IN_INDEX_TO_DATE))
            self.assertTrue(v["why"].endswith("; the program itself is not in the index to date the listing against"), v["why"])
            self.assertEqual(recover.dated_cell(v["current"], v["matched"]), "not dated")
        finally:
            conn.close()

    def test_6_the_copy_used_gone_from_the_index_is_unknown_not_contradicted(self):
        # no text to compare: the verdict says so rather than call the listing's copy different
        db2 = os.path.join(self.td, "gone.db")
        shutil.copyfile(self.db, db2)
        conn = query.connect(db2)
        try:
            conn.execute("DELETE FROM member WHERE name='DUPREC' AND path LIKE '%CLAIMS%'")
            conn.commit()
            srcs = {"CURLIST": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "CURLIST.lst", True, 100)]}
            v = {c["program"]: c for c in recover.check_choices(conn, srcs)}["CURLIST"]
            self.assertEqual(v["verdict"], "UNKNOWN")
            self.assertIn("the copy the build used is no longer a member of the index", v["why"])
        finally:
            conn.close()


class TheReparseNightOnTheStagingEstate(unittest.TestCase):
    """His sequence on the estate of staging_estate(): pull, recover with
    --from (which dates every listing), then the build night - here the
    build ran first (the index of the day), recover dated the listings and
    marked CURLIST, and the re-parse parses every program that copies DUPREC
    again with the rows in place (ROADMAP re-parse item 19). The resolver
    follows a listing only where recover called the choice contradicted by a
    current listing: CURLIST takes POLICY's copy; PROMOTED keeps the CLAIMS
    copy - production, not the staging copy - with 'confirmed by the
    program's listing (same text as PROD.STG.COPYLIB)'; OLDLIST (an older
    compile's listing) and STGGONE (a library not held) keep the chain's.
    Then recover finds nothing to do and says nothing wrong."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root, cls.from_dir = staging_estate(cls.td)
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        with contextlib.redirect_stdout(io.StringIO()):
            assert build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"]) == 0
        cls.first = recover.run(cls.db, folders=[cls.from_dir], log=lambda *_a: None, report=cls.report)
        recover._mark_programs(cls.db, ["DUPREC"])                        # the re-parse night: every copier parsed again
        with contextlib.redirect_stdout(io.StringIO()):
            assert build._main([cls.root, "--db", cls.db, "--quiet"]) == 0
        cls.said = []
        cls.stats = recover.run(cls.db, folders=[cls.from_dir], log=cls.said.append, report=cls.report)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def pick(self, name):
        mid = self.conn.execute("SELECT id FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()[0]
        note = self.conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind='ambiguous_copybook'",
                                 (mid,)).fetchone()[0]
        used = self.conn.execute("SELECT r.path FROM copy_use u JOIN member r ON r.id = u.resolved_member_id "
                                 "WHERE u.member_id=?", (mid,)).fetchone()[0]
        return note, used

    def test_1_the_first_run_marked_the_one_real_contradiction(self):
        self.assertEqual((self.first["checked"], self.first["marked"]), ((1, 1, 1, 1, 0), 1))

    def test_2_what_the_resolver_expanded(self):
        claims = os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        policy = os.path.join(self.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")
        for name, path, how in (
                ("CURLIST", policy, "the program's compiler listing names PROD.POLICY.COPYLIB"),
                ("PROMOTED", claims, "confirmed by the program's listing (same text as PROD.STG.COPYLIB)"),
                ("OLDLIST", claims, "same system"),
                ("STGGONE", claims, "same system")):
            with self.subTest(program=name):
                note, used = self.pick(name)
                self.assertEqual(used, path)
                self.assertEqual(note, f"3 copies of DUPREC with different content; used {path} ({how})")

    def test_3_recover_finds_nothing_to_do_and_says_nothing_wrong(self):
        text = "\n".join(self.said)
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((2, 0, 1, 1, 0), 0), text)
        self.assertIn("copybook choices checked against the listings: 2 confirmed, 0 contradicted by a current listing, 1 named "
                      "by an older listing, 1 name a library the index does not hold, 0 unknown", text)
        self.assertNotIn("is a wrong fact in the index", text)
        self.assertNotIn("marked for the next build", text)
        self.assertNotIn("next: run your usual build command", text)
        by = {v["program"]: v for v in recover.check_choices(self.conn)}
        self.assertEqual((by["CURLIST"]["verdict"], by["CURLIST"]["why"]), ("CONFIRMED", ""))
        self.assertEqual((by["PROMOTED"]["verdict"], by["PROMOTED"]["why"]),
                         ("CONFIRMED", "same text as the copy the listing names in PROD.STG.COPYLIB - promoted"))
        self.assertEqual(by["PROMOTED"]["how"], "confirmed by the program's listing (same text as PROD.STG.COPYLIB)")
        self.assertEqual(set(r[0] for r in self.conn.execute("SELECT parse_status FROM member WHERE kind='cobol'")), {"ok"})
        with open(self.report, encoding="utf-8") as fh:
            sec = fh.read().split("## Copybook choices, checked against the listings")[1]
        self.assertIn("_no choice contradicted by a current listing_", sec)
        self.assertIn("| PROMOTED | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; confirmed by the "
                      "program's listing (same text as PROD.STG.COPYLIB)) | PROD.STG.COPYLIB (SYSLIB) - the index holds that "
                      "copy at STAGING/PROD.STG.COPYLIB/DUPREC.cpy | yes | CONFIRMED | same text as the copy the listing names "
                      "in PROD.STG.COPYLIB - promoted |", sec)
        self.assertNotIn("| CURLIST |", sec)

    def test_4_coverage_and_program(self):
        cov = query.cmd_coverage(self.conn)
        self.assertIn("| cobol | PROMOTED | PROD.CLAIMS.SRC | DUPREC: 3 copies, used CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy "
                      "(listing: same text as PROD.STG.COPYLIB) |", cov)
        self.assertIn("| cobol | CURLIST | PROD.CLAIMS.SRC | DUPREC: 3 copies, used POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy "
                      "(listing: PROD.POLICY.COPYLIB) |", cov)
        self.assertIn("2 of these choices are confirmed by the program's listing, 0 contradicted by a current listing", cov)
        out = query.cmd_program(self.conn, "CURLIST")
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used", out)
        self.assertNotIn("CONTRADICTS", out)


class RowsFromBeforeTheToolDatedListings(unittest.TestCase):
    """A listing_copy_source table the tool wrote before the two dating
    columns existed: read as undated (the query side never alters it), a
    would-be contradiction says 'not yet dated', and the next recover run
    adds the columns and dates the rows."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root, self.from_dir = staging_estate(self.td)
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build._main([self.root, "--db", self.db, "--rebuild", "--quiet"]), 0)
        conn = sqlite3.connect(self.db)
        try:                                                           # the table as the tool created it before this change
            conn.execute("CREATE TABLE listing_copy_source (program TEXT NOT NULL, copybook TEXT NOT NULL, ddname TEXT, "
                         "dataset TEXT, listing TEXT, seen TEXT)")
            conn.executemany("INSERT INTO listing_copy_source VALUES(?,?,?,?,?,?)",
                             [("CURLIST", "DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "CURLIST.lst", "2026-09-01 08:00:00"),
                              ("PROMOTED", "DUPREC", "SYSLIB", "PROD.STG.COPYLIB", "PROMOTED.lst", "2026-09-01 08:00:00")])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def columns(self):
        conn = sqlite3.connect(self.db)
        try:
            return [r[1] for r in conn.execute("PRAGMA table_info(listing_copy_source)")]
        finally:
            conn.close()

    def test_1_read_as_undated(self):
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.stored_copy_sources(conn),
                             {"CURLIST": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "CURLIST.lst", None, None)],
                              "PROMOTED": [("DUPREC", "SYSLIB", "PROD.STG.COPYLIB", "PROMOTED.lst", None, None)]})
            by = {v["program"]: v for v in recover.check_choices(conn)}
            v = by["CURLIST"]
            self.assertEqual((v["verdict"], v["current"], v["dated"]), ("CONTRADICTED", None, recover.NOT_YET_DATED))
            self.assertTrue(v["why"].endswith("; " + recover.NOT_YET_DATED), v["why"])
            self.assertIn("not yet dated: run recover again with --from", v["why"])
            self.assertEqual((by["PROMOTED"]["verdict"], by["PROMOTED"]["current"]), ("CONFIRMED", None))   # the same text needs no date
            rep = "".join(recover.choice_report(recover.check_choices(conn), self.root))
            self.assertIn(f"| CURLIST | DUPREC | {USED} | PROD.POLICY.COPYLIB (SYSLIB) - {POLICY_HELD} | not dated | CONTRADICTED | "
                          "the listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build used; "
                          f"{recover.NOT_YET_DATED} |", rep)
            out = query.cmd_program(conn, "CURLIST")
            self.assertIn(f"CONTRADICTS the copy the build used (PROD.CLAIMS.COPYLIB; {POLICY_HELD}; {recover.NOT_YET_DATED}) - "
                          "see work/recover.md", out)
            self.assertIn("PROD.POLICY.COPYLIB (1 program; 1 not yet dated)", query.cmd_copybook(conn, "DUPREC"))
            self.assertIn("1 contradicted by a current listing (see work/recover.md)", query.cmd_coverage(conn))
            self.assertEqual(self.columns(), ["program", "copybook", "ddname", "dataset", "listing", "seen"], "nothing altered")
        finally:
            conn.close()

    def test_2_the_next_run_adds_the_columns_and_dates_the_rows(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(recover.ensure_copy_source_columns(conn), ["current", "matched"])
            self.assertEqual(recover.ensure_copy_source_columns(conn), [])
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.columns(), ["program", "copybook", "ddname", "dataset", "listing", "seen", "current", "matched"])
        said = []
        stats = recover.run(self.db, folders=[self.from_dir], log=said.append, report=self.report)
        self.assertEqual(stats["checked"], (1, 1, 1, 1, 0), "\n".join(said))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT program, current, matched FROM listing_copy_source WHERE program IN "
                                          "('CURLIST', 'PROMOTED') ORDER BY program").fetchall(), [("CURLIST", 1, 100), ("PROMOTED", 1, 100)])
        finally:
            conn.close()

    def test_3_the_run_alone_alters_the_table(self):
        said = []
        recover.run(self.db, folders=[self.from_dir], log=said.append, report=self.report)
        self.assertEqual(self.columns(), ["program", "copybook", "ddname", "dataset", "listing", "seen", "current", "matched"])
        conn = query.connect(self.db)
        try:
            v = {c["program"]: c for c in recover.check_choices(conn)}["CURLIST"]
            self.assertEqual((v["verdict"], v["current"], v["dated"]), ("CONTRADICTED", True, ""))
        finally:
            conn.close()


MISSING1 = ["           05  MISS1-FIELD   PIC X(4)."]
PLAIN = ["           05  PLAIN-FIELD   PIC X(6)."]


def listing2(name, copybooks, texts, table):
    return ibm_listing(program(name, *copybooks).splitlines(), texts, copy_table=table)


def fetch_estate(root):
    """The estate of FetchListFromTheListings (its docstring says who copies
    what and which library each listing names), written under `root`."""
    texts = {"DUPREC": DUPREC_CLAIMS, "MISSING1": MISSING1, "PLAIN": PLAIN}
    files = {
        "CLAIMS/PROD.CLAIMS.SRC/MISSPGM.cbl": program("MISSPGM", "MISSING1", "DUPREC"),
        "CLAIMS/PROD.CLAIMS.SRC/OTHER1.cbl": program("OTHER1", "DUPREC", "PLAIN"),
        "CLAIMS/PROD.CLAIMS.SRC/OTHER2.cbl": program("OTHER2", "DUPREC"),
        "CLAIMS/PROD.CLAIMS.SRC/NOTBL.cbl": program("NOTBL", "NOTNAMED"),
        "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
        "CLAIMS/PROD.CLAIMS.COPYLIB/PLAIN.cpy": PLAIN[0] + "\n",
        "POLICY/POLCOPY2/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        "POLICY/POLCOPY2/.atlas-library.json": json.dumps({"dataset": "PROD.POLICY.COPYLIB2", "folder": "x", "rc": 0,
                                                          "expected": 1, "present": 1, "complete": True,
                                                          "missing": [], "stale": []}),
        "CLAIMS/PROD.CLAIMS.LISTING/MISSPGM.lst": listing2("MISSPGM", ["MISSING1", "DUPREC"], texts,
                                                           [("MISSING1", "SYSLIB", "PROD.NEW.COPYLIB"),
                                                            ("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB2")]),
        "CLAIMS/PROD.CLAIMS.LISTING/OTHER1.lst": listing2("OTHER1", ["DUPREC", "PLAIN"], texts,
                                                          [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB"),
                                                           ("PLAIN", "SYSLIB", "PROD.OTHER.COPYLIB")]),
        "CLAIMS/PROD.CLAIMS.LISTING/OTHER2.lst": listing2("OTHER2", ["DUPREC"], texts,
                                                          [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB")]),
        "CLAIMS/PROD.CLAIMS.LISTING/NOTBL.lst": listing2("NOTBL", ["NOTNAMED"], texts, None),
    }
    for rel, text in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)


class FetchListFromTheListings(unittest.TestCase):
    """The listings name the library each copybook came from: the run turns
    those rows into a fetch list - the datasets to pull members from, the
    ones holding a missing copybook first, the ones already held marked so -
    in the report and as work/fetch-list.txt (datasets only, for Bulk add).

    - MISSPGM copies MISSING1 (no copy anywhere) and DUPREC (chosen among
      several); its listing says MISSING1 came from PROD.NEW.COPYLIB (not
      fetched) and DUPREC from PROD.POLICY.COPYLIB2 (held, via `library`);
    - OTHER1 and OTHER2 copy DUPREC; their listings say PROD.CLAIMS.COPYLIB
      (held: the folder is named after the dataset) - two programs, no
      missing copybook, so it sorts after PROD.NEW.COPYLIB all the same;
    - OTHER1 also copies PLAIN (one copy, no choice); its listing says
      PROD.OTHER.COPYLIB (not fetched, nothing missing in it);
    - NOTBL copies NOTNAMED (missing) and its listing has no table."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        fetch_estate(cls.root)
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        cls.list_file = os.path.join(cls.td, "work", "fetch-list.txt")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])
        assert rc == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def run_it(self, **kw):
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report, **kw)
        return stats, "\n".join(said)

    def report_text(self):
        with open(self.report, encoding="utf-8") as fh:
            return fh.read()

    def list_lines(self):
        with open(self.list_file, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_1_the_table_the_file_and_the_console(self):
        stats, text = self.run_it()
        self.assertEqual((stats["written"], stats["fetch"], stats["unnamed"]), (1, (4, 2), 1), text)
        self.assertIn("libraries the listings name: 4 datasets, 2 not fetched yet - the report's fetch list names them", text)
        self.assertIn("fetch-list.txt holds the 2 to fetch, one dataset per line, for the UI's Bulk add", text)
        self.assertIn("1 copybook to fetch for is named by no listing's table", text)
        self.assertNotIn("held only as recovered copies", text)                # nothing recovered yet
        rep = self.report_text()
        # placed right after the not-found section, before the choices
        self.assertLess(rep.index("## Not in any expanded text"), rep.index("## Libraries the listings name (fetch list)"))
        self.assertLess(rep.index("## Libraries the listings name (fetch list)"), rep.index("## Copybook choices"))
        sec = rep.split("## Libraries the listings name (fetch list)")[1].split("\n## ")[0]
        rows = [ln for ln in sec.splitlines() if ln.startswith("| PROD.")]
        self.assertEqual(rows, [
            "| PROD.NEW.COPYLIB | not fetched | MISSING1 | - | 1 |",                       # a missing copybook: first
            "| PROD.CLAIMS.COPYLIB | held as CLAIMS/PROD.CLAIMS.COPYLIB | - | DUPREC | 2 |",   # most programs next
            "| PROD.OTHER.COPYLIB | not fetched | - | - | 1 |",
            "| PROD.POLICY.COPYLIB2 | held as POLICY/POLCOPY2 | - | DUPREC | 1 |",
        ])
        self.assertIn("| dataset | held? | copybooks to fetch for (missing, or held only as a recovered copy) | "
                      "copybooks the build chose among several | programs |", sec)
        self.assertIn("Fetch a dataset with the UI's Bulk add (paste the dataset names) or your zowe command; then run the build; "
                      "the missing copybooks it holds resolve, and the recovered copies of them are removed on the next recover run.", sec)
        self.assertIn("fetch-list.txt` holds the 2 not-fetched datasets, one per line", sec)
        self.assertIn("- 1 copybook to fetch for is named by no listing's table: NOTNAMED", sec)
        self.assertNotIn("recovered cop", "\n".join(rows))                # no copybook is held as a copy yet
        # the file: not-fetched datasets only, the one with a missing copybook first, nothing else in it
        self.assertEqual(self.list_lines(), ["PROD.NEW.COPYLIB", "PROD.OTHER.COPYLIB"])
        for ln in self.list_lines():
            self.assertRegex(ln, r"^[A-Z0-9@#$]+(\.[A-Z0-9@#$]+)+$", "datasets only, never a member name")
        for name in ("MISSING1", "DUPREC", "PLAIN", "NOTNAMED", "MISSPGM", "OTHER1"):
            self.assertNotIn(name, self.list_lines())
        self.assertNotIn("PROD.POLICY.COPYLIB2", self.list_lines(), "a held library is not fetched again")
        self.assertNotIn("PROD.CLAIMS.COPYLIB", self.list_lines())

    def test_2_a_second_run_rewrites_the_file(self):
        self.run_it()
        first = self.list_lines()
        stats, text = self.run_it()
        self.assertEqual(self.list_lines(), first, "rewritten, not appended to")
        self.assertEqual(len(first), len(set(first)), "no duplicates")
        self.assertEqual(stats["fetch"], (4, 2))
        self.assertIn("libraries the listings name: 4 datasets, 2 not fetched yet", text)

    def test_3_copybook_names_the_library_for_a_missing_copybook(self):
        self.run_it()
        conn = query.connect(self.db)
        try:
            out = query.cmd_copybook(conn, "MISSING1")
            self.assertIn("**NOT FOUND**", out)
            self.assertIn("the listings say it came from: PROD.NEW.COPYLIB (1 program) - fetch that library", out)
            self.assertIn("work/fetch-list.txt", out)
            out = query.cmd_copybook(conn, "NOTNAMED")
            self.assertIn("**NOT FOUND**", out)
            self.assertNotIn("listings say", out)                              # no table names it: nothing claimed
            out = query.cmd_copybook(conn, "DUPREC")                            # a present copybook: the old sentence, unchanged
            self.assertIn("The programs' compiler listings say this copybook came from: PROD.CLAIMS.COPYLIB (2 programs), "
                          "PROD.POLICY.COPYLIB2 (1 program)", out)
        finally:
            conn.close()

    def test_4_dry_run_still_writes_the_list(self):
        try:
            os.remove(self.list_file)
        except OSError:
            pass
        stats, text = self.run_it(dry_run=True)
        self.assertEqual(stats["fetch"], (4, 2))
        self.assertEqual(self.list_lines(), ["PROD.NEW.COPYLIB", "PROD.OTHER.COPYLIB"])


class FetchListAfterTheBuild(unittest.TestCase):
    """The same estate after recover wrote MISSING1 and the build read the
    copy: MISSING1 is no longer missing to the build (every COPY of it
    resolves to the recovered copy), but the real member is still on the
    host, so PROD.NEW.COPYLIB must stay first in the table and in
    fetch-list.txt, MISSING1 shown as a recovered copy - after an incremental
    build (the stored listing rows survive) and after --rebuild (a fresh
    index: MISSPGM's listing must be read again for its table)."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        fetch_estate(cls.root)
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        cls.list_file = os.path.join(cls.td, "work", "fetch-list.txt")
        cls.build("--rebuild")
        stats, _text = cls.run_it()                                    # writes MISSING1 under RECOVERED-COPYBOOKS
        assert stats["written"] == 1, stats
        cls.build()                                                    # incremental: the build reads the copy

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    @classmethod
    def build(cls, *flags):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = build._main([cls.root, "--db", cls.db, "--quiet", *flags])
        assert rc == 0

    @classmethod
    def run_it(cls, **kw):
        said = []
        stats = recover.run(cls.db, log=said.append, report=cls.report, **kw)
        return stats, "\n".join(said)

    def section(self):
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        return rep.split("## Libraries the listings name (fetch list)")[1].split("\n## ")[0]

    def list_lines(self):
        with open(self.list_file, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def rows(self):
        return [ln for ln in self.section().splitlines() if ln.startswith("| PROD.")]

    EXPECTED = [
        "| PROD.NEW.COPYLIB | not fetched | recovered copy: MISSING1 | - | 1 |",          # still first: its member is on the host
        "| PROD.CLAIMS.COPYLIB | held as CLAIMS/PROD.CLAIMS.COPYLIB | - | DUPREC | 2 |",
        "| PROD.OTHER.COPYLIB | not fetched | - | - | 1 |",
        "| PROD.POLICY.COPYLIB2 | held as POLICY/POLCOPY2 | - | DUPREC | 1 |",
    ]

    def check(self, stats, text):
        self.assertEqual((stats["missing"], stats["written"], stats["fetch"], stats["unnamed"]), (1, 0, (4, 2), 1), text)
        self.assertIn("held only as recovered copies: 1 - the build has read them, so they are not missing any more; "
                      "their libraries stay on the fetch list until the real members arrive", text)
        self.assertIn("libraries the listings name: 4 datasets, 2 not fetched yet", text)
        self.assertIn("1 copybook to fetch for is named by no listing's table", text)      # NOTNAMED, still missing
        self.assertEqual(self.rows(), self.EXPECTED)
        self.assertIn("- 1 copybook to fetch for is named by no listing's table: NOTNAMED", self.section())
        self.assertEqual(self.list_lines(), ["PROD.NEW.COPYLIB", "PROD.OTHER.COPYLIB"])

    def test_1_after_an_incremental_build_the_library_is_still_the_one_to_fetch(self):
        conn = sqlite3.connect(self.db)
        try:                                                           # the premise: the build resolved MISSING1 to the copy
            self.assertEqual(recover.missing_copybooks(conn), {"NOTNAMED": 1})
        finally:
            conn.close()
        stats, text = self.run_it()
        self.check(stats, text)

    def test_2_after_a_rebuild_the_listing_is_read_again_for_its_table(self):
        self.build("--rebuild")                                        # a fresh index: no stored listing rows
        conn = sqlite3.connect(self.db)
        try:
            self.assertFalse(recover.has_copy_sources(conn))
        finally:
            conn.close()
        stats, text = self.run_it()
        self.check(stats, text)
        self.assertIn("only the 2 programs that copy a missing copybook (or one held only as a recovered copy)", text)

    def test_3_copybook_says_held_only_as_a_recovered_copy_and_names_the_library(self):
        self.run_it()
        conn = query.connect(self.db)
        try:
            out = query.cmd_copybook(conn, "MISSING1")
            self.assertNotIn("NOT FOUND", out)
            self.assertIn("The index holds this copybook only as a recovered copy", out)
            self.assertIn("the listings say it came from: PROD.NEW.COPYLIB (1 program) - fetch that library", out)
            self.assertIn("work/fetch-list.txt", out)
            out = query.cmd_copybook(conn, "DUPREC")                            # a real member: the plain sentence
            self.assertNotIn("recovered copy", out)
            self.assertIn("The programs' compiler listings say this copybook came from:", out)
        finally:
            conn.close()

    def test_4_nothing_missing_at_all_still_reads_the_listings_for_the_fetch_list(self):
        # NOTNAMED arrives for real: nothing is missing any more, yet MISSING1 is held only as a copy
        p = os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "NOTNAMED.cpy")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("           05  NOTNAMED-F    PIC X(2).\n")
        try:
            self.build("--rebuild")
            stats, text = self.run_it()
            self.assertEqual((stats["missing"], stats["written"], stats["fetch"], stats["unnamed"]), (0, 0, (4, 2), 0), text)
            self.assertIn("nothing to recover: every copybook the programs copy is in the index", text)
            self.assertEqual(self.rows(), self.EXPECTED)
            self.assertEqual(self.list_lines(), ["PROD.NEW.COPYLIB", "PROD.OTHER.COPYLIB"])
            self.assertNotIn("recovered: 0 of 0", text)
        finally:
            os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
