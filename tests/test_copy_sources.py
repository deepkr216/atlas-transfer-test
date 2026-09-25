"""
The listing says which library each copybook came from; the build's choice
is checked against it.

Where a copybook's name exists in several libraries with different content
the build's resolver GUESSES which copy to expand (COPY..OF, then the
program's own system in its declared order, then authoritative, then the
same folder, then the first) and records an 'ambiguous_copybook' row. His
compiler listings end with a table naming, per copybook, the DD name and the
LIBRARY DATASET the compiler read it from - the truth. atlas.recover reads
that table from every such program's listing, stores it
(listing_copy_source) and gives each choice a verdict: CONFIRMED,
CONTRADICTED (a wrong fact: the report names it), UNKNOWN. The fact tables
do not change; the build's use of the rows is ROADMAP re-parse item 19.
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
    CLAIMS copy (same system). The listings say which copy was right."""

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

    def test_2_dry_run_checks_and_stores_nothing(self):
        stats, said = self.run_it(dry_run=True)
        self.assertEqual(stats["checked"], (1, 3, 2), said)
        self.assertIsNone(self.rows(), "a dry run writes nothing")
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 3 contradicted, 2 unknown", "\n".join(said))

    def test_3_the_verdicts_the_line_and_the_report(self):
        stats, said = self.run_it()
        text = "\n".join(said)
        self.assertEqual(stats["written"], 0)
        self.assertIn("nothing to recover: every copybook the programs copy is in the index", text)
        self.assertIn("expanded texts to read: 5 of 5 found", text)
        self.assertIn("the 6 with a copybook chosen among several (to check the choice against the listing)", text)
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 3 contradicted, 2 unknown - every contradicted "
                      "choice is a wrong fact in the index", text)
        self.assertNotIn("recovered: 0 of 0", text)
        self.assertEqual(stats["checked"], (1, 3, 2))
        conn = query.connect(self.db)
        try:
            by = {v["program"]: v for v in recover.check_choices(conn)}
        finally:
            conn.close()
        self.assertEqual(by["CHOSEN"]["verdict"], "CONFIRMED")
        self.assertEqual((by["WRONGPK"]["verdict"], by["WRONGPK"]["listing_datasets"], by["WRONGPK"]["used_dataset"]),
                         ("CONTRADICTED", ["PROD.POLICY.COPYLIB"], "PROD.CLAIMS.COPYLIB"))
        self.assertIn("the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build chose the other", by["WRONGPK"]["index_has"])
        self.assertIn("the index holds that copy at POLICY/POLCOPY2/DUPREC.cpy", by["LIBTBL"]["index_has"])     # known through `library`
        self.assertEqual(by["FETCHIT"]["index_has"], "not a library the index holds - fetch it")
        self.assertEqual((by["NOTABLE"]["verdict"], by["NOTABLE"]["why"]), ("UNKNOWN", "the program's listing has no copybook-source table"))
        self.assertEqual((by["NOLIST"]["verdict"], by["NOLIST"]["why"]), ("UNKNOWN", "no listing of the program was read"))
        rep = self.report_text()
        sec = rep.split("## Copybook choices, checked against the listings")[1]
        self.assertIn("- 6 choices checked: 1 confirmed, 3 contradicted, 2 unknown (1: no listing of the program was read; "
                      "1: the program's listing has no copybook-source table)", sec)
        self.assertIn("| program | copybook | the index used | the listing says | verdict |", sec)
        self.assertIn("| WRONGPK | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | CONTRADICTED |", sec)
        self.assertIn("| LIBTBL | DUPREC | ", sec)
        self.assertIn("| FETCHIT | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.SHARED.COPYLIB (COPYLIB) - not a library the index holds - fetch it | CONTRADICTED |", sec)
        self.assertNotIn("| CHOSEN |", sec)
        self.assertNotIn("| NOTABLE |", sec)
        self.assertIn("ROADMAP re-parse item 19", sec)
        # the stored rows: one per (program, copybook) named; a listing without a table leaves an empty row
        rows = self.rows()
        self.assertEqual([r for r in rows if r[1]], [("CHOSEN", "DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB"),
                                                     ("FETCHIT", "DUPREC", "COPYLIB", "PROD.SHARED.COPYLIB"),
                                                     ("LIBTBL", "DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB2"),
                                                     ("WRONGPK", "DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")])
        self.assertEqual([r for r in rows if not r[1]], [("NOTABLE", "", None, None)])
        # the build's facts are untouched: no parse_status changed, every 'ambiguous_copybook' row still there
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
            self.assertIn("1 of these choices is confirmed by the program's listing, 3 contradicted (see work/recover.md), "
                          "2 unknown", chosen)
            out = query.cmd_program(conn, "CHOSEN")
            self.assertIn("- listing says: DUPREC came from PROD.CLAIMS.COPYLIB (SYSLIB) - confirms the copy the build used", out)
            out = query.cmd_program(conn, "WRONGPK")
            self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - CONTRADICTS the copy the build used "
                          "(PROD.CLAIMS.COPYLIB; the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build "
                          "chose the other) - see work/recover.md", out)
            self.assertIn("parse: complete (copybook chosen among several - see notes)", out)      # unchanged
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


class CheckedWhileRecovering(unittest.TestCase):
    """A program that copies a missing copybook AND took a chosen one: its
    listing is read for the missing copybook, and the choice is checked on
    the same read - the recovery path, not the check-only one."""

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
        self.assertEqual((stats["written"], stats["missing"], stats["checked"]), (1, 1, (0, 1, 0)), text)
        self.assertIn("recovered: 1 of 1 missing copybooks", text)
        self.assertIn("copybook choices checked against the listings: 0 confirmed, 1 contradicted, 0 unknown", text)
        self.assertIn("expanded texts to read: 1 of 1 found", text)
        with open(self.report, encoding="utf-8") as fh:
            rep = fh.read()
        self.assertIn("| NOPE | 1 |", rep)
        self.assertIn("| MISSPGM | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | CONTRADICTED |", rep)
        # the recovered copybook is not in the listing's table for anything the index chose: NOPE's row is stored all the same
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT copybook, dataset FROM listing_copy_source WHERE program='MISSPGM' ORDER BY 1").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("DUPREC", "PROD.POLICY.COPYLIB"), ("NOPE", "PROD.X.COPYLIB")])


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
