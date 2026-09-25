"""
The resolver reads the compiler listing first (ROADMAP re-parse item 19).

Where a copybook's name exists in several libraries with different content
the build used to GUESS which copy to expand (COPY..OF, then the program's
own system in its declared order, then authoritative, then the same folder,
then the first). atlas.recover reads each program's compiler listing for the
library dataset the compiler read the copybook from and stores it in
listing_copy_source. Now build.make_resolver looks that table up before its
chain: a candidate in a folder tied to the dataset the listing names (the
`library` table, else the folder named after its dataset) is the copy
expanded, and the 'ambiguous_copybook' row says 'the program's compiler
listing names DATASET' as its how. Only where no listing says does the chain
decide as before; the table absent or empty changes nothing and the build
never creates it.
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
from test_copy_sources import DUPREC_CLAIMS, DUPREC_POLICY, listing, program  # noqa: E402

DUPREC_THIRD = "           05  DUP-FIELD-C   PIC X(2).\n"
MARKER = json.dumps({"dataset": "PROD.POLICY.COPYLIB2", "folder": "x", "rc": 0, "expected": 1, "present": 1,
                     "complete": True, "missing": [], "stale": []})


def write_estate(root, files):
    for rel, text in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)


def build_it(root, db, rebuild=False):
    with contextlib.redirect_stdout(io.StringIO()):
        rc = build._main([root, "--db", db, "--quiet", *(["--rebuild"] if rebuild else [])])
    assert rc == 0, rc


def pick_of(conn, name):
    """(the 'ambiguous_copybook' note, the member the COPY DUPREC row resolved to, its path)."""
    mid = conn.execute("SELECT id FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()[0]
    note = conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind='ambiguous_copybook'", (mid,)).fetchone()
    row = conn.execute("SELECT r.path FROM copy_use u JOIN member r ON r.id = u.resolved_member_id "
                       "WHERE u.member_id=? AND UPPER(u.copybook)='DUPREC'", (mid,)).fetchone()
    return (note[0] if note else None), (row[0] if row else None)


def expanded_from(conn, name):
    """The members whose lines were expanded into the program (expand_run):
    which copy's fields the program's every answer rests on."""
    return {r[0] for r in conn.execute("SELECT s.path FROM expand_run r JOIN program p ON p.id = r.program_id "
                                       "JOIN member m ON m.id = p.member_id JOIN member s ON s.id = r.src_member "
                                       "WHERE m.name=? AND m.kind='cobol' AND r.depth > 0", (name,))}


def table_exists(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_copy_source'").fetchone() is not None
    finally:
        conn.close()


class ListingDecidesTheCopy(unittest.TestCase):
    """estate\\SYSTEM\\LIBRARY\\member. DUPREC exists in CLAIMS, POLICY and a
    third library with different content; the chain picks CLAIMS's copy for
    every CLAIMS program (same system). WRONGPK's listing names POLICY's
    library and LIBTBL's a library known through the `library` table: parsed
    again with the rows stored, they take the listing's copy."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/CHOSEN.cbl": program("CHOSEN", "DUPREC"),      # the listing agrees with the chain
            "CLAIMS/PROD.CLAIMS.SRC/WRONGPK.cbl": program("WRONGPK", "DUPREC"),    # the listing names POLICY's library
            "CLAIMS/PROD.CLAIMS.SRC/LIBTBL.cbl": program("LIBTBL", "DUPREC"),      # ... a library known through `library`
            "CLAIMS/PROD.CLAIMS.SRC/NOLIST.cbl": program("NOLIST", "DUPREC"),      # no listing at all
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "POLICY/POLCOPY2/DUPREC.cpy": DUPREC_THIRD,
            "POLICY/POLCOPY2/.atlas-library.json": MARKER,
            "CLAIMS/PROD.CLAIMS.LISTING/CHOSEN.lst": listing("CHOSEN", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/WRONGPK.lst": listing("WRONGPK", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/LIBTBL.lst": listing("LIBTBL", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB2")]),
        })
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        build_it(cls.root, cls.db, rebuild=True)
        cls.first_said = []
        cls.first_stats = recover.run(cls.db, log=cls.first_said.append, report=cls.report)
        # the re-parse: every program copying DUPREC parsed again, the rows now stored
        recover._mark_programs(cls.db, ["DUPREC"])
        build_it(cls.root, cls.db)
        cls.said = []
        cls.stats = recover.run(cls.db, log=cls.said.append, report=cls.report)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_1_the_first_build_guessed_and_recover_said_so(self):
        self.assertEqual(self.first_stats["checked"], (1, 2, 1), self.first_said)
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 2 contradicted, 1 unknown", "\n".join(self.first_said))

    def test_2_parsed_again_the_program_takes_the_copy_its_listing_names(self):
        note, used = pick_of(self.conn, "WRONGPK")
        self.assertEqual(used, os.path.join(self.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy"))
        self.assertEqual(note, f"3 copies of DUPREC with different content; used {used} "
                               "(the program's compiler listing names PROD.POLICY.COPYLIB)")
        self.assertEqual(expanded_from(self.conn, "WRONGPK"), {used})            # POLICY's text, not CLAIMS's

    def test_3_a_library_known_through_the_library_table(self):
        note, used = pick_of(self.conn, "LIBTBL")
        self.assertEqual(used, os.path.join(self.root, "POLICY", "POLCOPY2", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(the program's compiler listing names PROD.POLICY.COPYLIB2)"), note)
        self.assertEqual(expanded_from(self.conn, "LIBTBL"), {used})

    def test_4_a_listing_that_agrees_says_the_listing_decided(self):
        note, used = pick_of(self.conn, "CHOSEN")
        self.assertEqual(used, os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(the program's compiler listing names PROD.CLAIMS.COPYLIB)"), note)
        self.assertEqual(expanded_from(self.conn, "CHOSEN"), {used})

    def test_5_no_listing_the_chain_as_before(self):
        note, used = pick_of(self.conn, "NOLIST")
        self.assertEqual(used, os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(same system)"), note)

    def test_6_recover_confirms_every_choice_the_listing_made(self):
        by = {v["program"]: v for v in recover.check_choices(self.conn)}
        self.assertEqual({p: v["verdict"] for p, v in by.items()},
                         {"CHOSEN": "CONFIRMED", "WRONGPK": "CONFIRMED", "LIBTBL": "CONFIRMED", "NOLIST": "UNKNOWN"})
        self.assertEqual(recover.choice_counts(by.values()), (3, 0, 1))
        self.assertEqual(self.stats["checked"], (3, 0, 1), self.said)
        self.assertIn("copybook choices checked against the listings: 3 confirmed, 0 contradicted, 1 unknown", "\n".join(self.said))
        with open(self.report, encoding="utf-8") as fh:
            sec = fh.read().split("## Copybook choices, checked against the listings")[1]
        self.assertIn("_no contradicted choice_", sec)
        self.assertIn("the build reads this table first", sec)
        self.assertNotIn("does not read this table", sec)

    def test_7_the_stand_ins_say_nothing_wrong(self):
        for name in ("CHOSEN", "WRONGPK", "LIBTBL", "NOLIST"):
            mid = self.conn.execute("SELECT id FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()[0]
            self.assertFalse(query.is_truly_partial(self.conn, mid), name)
        out = query.cmd_program(self.conn, "WRONGPK")
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used", out)
        self.assertNotIn("CONTRADICTS", out)
        cov = query.cmd_coverage(self.conn)
        self.assertIn("3 of these choices are confirmed by the program's listing, 0 contradicted (see work/recover.md), 1 unknown", cov)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM unresolved WHERE kind='ambiguous_copybook'").fetchone()[0], 4)
        # `copybook DUPREC` groups the programs by the copy each one expanded: WRONGPK under POLICY's, alone
        groups = [l for l in query.cmd_copybook(self.conn, "DUPREC").splitlines() if l.startswith("- `")]
        by_copy = {l.split("`")[1]: l.split("): ", 1)[1] for l in groups}
        self.assertEqual(by_copy[os.path.join(self.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")], "WRONGPK @WRONGPK:7")
        self.assertEqual(by_copy[os.path.join(self.root, "POLICY", "POLCOPY2", "DUPREC.cpy")], "LIBTBL @LIBTBL:7")
        self.assertEqual(by_copy[os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")], "CHOSEN @CHOSEN:7, NOLIST @NOLIST:7")


class FilledBeforeTheFirstBuild(unittest.TestCase):
    """The rows in place before the index is built (recover ran on the last
    index; the re-parse night keeps the table): a first build reads them. A
    nested COPY (the program copies OUTER, which copies DUPREC) is looked up
    by the program's name; a listing naming a library the index does not
    hold, and a listing with no table, leave the chain to decide."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/LISTED.cbl": program("LISTED", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/NESTED.cbl": program("NESTED", "OUTER"),
            "CLAIMS/PROD.CLAIMS.SRC/FETCHIT.cbl": program("FETCHIT", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/NOTABLE.cbl": program("NOTABLE", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "CLAIMS/PROD.CLAIMS.COPYLIB/OUTER.cpy": "           COPY DUPREC.\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        })
        cls.db = os.path.join(cls.td, "t.db")
        conn = build.open_db(cls.db)
        try:
            recover.store_copy_sources(conn, {
                "LISTED": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "LISTED.lst")],
                "NESTED": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "NESTED.lst")],
                "FETCHIT": [("DUPREC", "COPYLIB", "PROD.SHARED.COPYLIB", "FETCHIT.lst")],
                "NOTABLE": [],
            })
        finally:
            conn.close()
        build_it(cls.root, cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_1_the_listing_named_copy_at_the_first_build(self):
        note, used = pick_of(self.conn, "LISTED")
        self.assertEqual(used, os.path.join(self.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(the program's compiler listing names PROD.POLICY.COPYLIB)"), note)
        self.assertEqual(expanded_from(self.conn, "LISTED"), {used})

    def test_2_a_nested_copy_is_looked_up_by_the_program(self):
        note, used = pick_of(self.conn, "NESTED")
        self.assertEqual(used, os.path.join(self.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(the program's compiler listing names PROD.POLICY.COPYLIB)"), note)
        self.assertEqual(expanded_from(self.conn, "NESTED"), {os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "OUTER.cpy"), used})

    def test_3_a_library_the_index_does_not_hold_leaves_the_chain(self):
        note, used = pick_of(self.conn, "FETCHIT")
        self.assertEqual(used, os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(same system)"), note)
        v = {c["program"]: c for c in recover.check_choices(self.conn)}["FETCHIT"]
        self.assertEqual((v["verdict"], v["index_has"]), ("CONTRADICTED", "not a library the index holds - fetch it"))

    def test_4_a_listing_with_no_table_leaves_the_chain(self):
        note, used = pick_of(self.conn, "NOTABLE")
        self.assertEqual(used, os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith("(same system)"), note)
        v = {c["program"]: c for c in recover.check_choices(self.conn)}["NOTABLE"]
        self.assertEqual((v["verdict"], v["why"]), ("UNKNOWN", "the program's listing has no copybook-source table"))

    def test_5_the_rows_are_kept_as_stored(self):
        rows = self.conn.execute("SELECT program, copybook, dataset FROM listing_copy_source ORDER BY program").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("FETCHIT", "DUPREC", "PROD.SHARED.COPYLIB"), ("LISTED", "DUPREC", "PROD.POLICY.COPYLIB"),
                                                    ("NESTED", "DUPREC", "PROD.POLICY.COPYLIB"), ("NOTABLE", "", None)])


class TheTableAbsentOrEmpty(unittest.TestCase):
    """An index recover never ran on has no table; one it ran on with nothing
    to store has an empty one. Either way the chain decides as before, and the
    build neither creates nor drops the table."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/GUESSED.cbl": program("GUESSED", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        })

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def check_chain(self, db):
        conn = query.connect(db)
        try:
            note, used = pick_of(conn, "GUESSED")
            self.assertEqual(used, os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy"))
            self.assertEqual(note, f"2 copies of DUPREC with different content; used {used} (same system)")
            self.assertEqual(expanded_from(conn, "GUESSED"), {used})
        finally:
            conn.close()

    def test_absent(self):
        db = os.path.join(self.td, "absent.db")
        build_it(self.root, db, rebuild=True)
        self.check_chain(db)
        self.assertFalse(table_exists(db), "the build never creates recover's table")

    def test_empty(self):
        db = os.path.join(self.td, "empty.db")
        conn = build.open_db(db)
        try:
            conn.execute(recover.COPY_SOURCE_TABLE)
            conn.commit()
        finally:
            conn.close()
        build_it(self.root, db)
        self.check_chain(db)
        self.assertTrue(table_exists(db), "the build never drops recover's table")
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM listing_copy_source").fetchone()[0], 0)
        finally:
            conn.close()


class LoadedOnce(unittest.TestCase):
    """The table is read once into a dict keyed (PROGRAM, COPYBOOK), each row
    tied to the system of the listing member that names it; the
    folder-to-dataset tie is cached per folder."""

    def test_absent_table_gives_nothing_and_is_not_created(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(build.load_listing_sources(conn), {})
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='listing_copy_source'").fetchone())

    def test_rows_keyed_upper_deduped_and_empty_rows_skipped(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(recover.COPY_SOURCE_TABLE)
        conn.execute("CREATE TABLE member (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, system TEXT)")
        conn.executemany("INSERT INTO member(path, system) VALUES(?,?)", [("a.lst", "CLAIMS"), ("c.lst", None), ("d.lst", "")])
        conn.executemany("INSERT INTO listing_copy_source(program, copybook, ddname, dataset, listing, seen) VALUES(?,?,?,?,?,?)", [
            ("pgma", "dupreC", "SYSLIB", "prod.policy.copylib", "a.lst", "x"),
            ("PGMA", "DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "a.lst", "x"),         # the same row twice: once
            ("PGMA", "DUPREC", "MYLIB", "PROD.OTHER.COPYLIB", "a.lst", "x"),           # a second dataset named: kept, in order
            ("PGMA", "OTHER", "SYSLIB", "PROD.CLAIMS.COPYLIB", "a.lst", "x"),
            ("NOTABLE", "", None, None, None, "x"),                                    # a listing read with no table
            ("NODSN", "DUPREC", "SYSLIB", "", "b.lst", "x"),
            ("PGMB", "DUPREC", "SYSLIB", "PROD.X.COPYLIB", "b.lst", "x"),              # a listing the index does not hold: no system
            ("PGMC", "DUPREC", "SYSLIB", "PROD.X.COPYLIB", "c.lst", "x"),              # held, no system assigned
            ("PGMD", "DUPREC", "SYSLIB", "PROD.X.COPYLIB", "d.lst", "x"),              # held, an empty system: none
            ("PGME", "DUPREC", "SYSLIB", "PROD.X.COPYLIB", "a.lst", "x"),              # the same dataset from two listings: both
            ("PGME", "DUPREC", "SYSLIB", "PROD.X.COPYLIB", "b.lst", "x"),
        ])
        self.assertEqual(build.load_listing_sources(conn), {
            ("PGMA", "DUPREC"): (("PROD.POLICY.COPYLIB", "CLAIMS"), ("PROD.OTHER.COPYLIB", "CLAIMS")),
            ("PGMA", "OTHER"): (("PROD.CLAIMS.COPYLIB", "CLAIMS"),),
            ("PGMB", "DUPREC"): (("PROD.X.COPYLIB", None),),
            ("PGMC", "DUPREC"): (("PROD.X.COPYLIB", None),),
            ("PGMD", "DUPREC"): (("PROD.X.COPYLIB", None),),
            ("PGME", "DUPREC"): (("PROD.X.COPYLIB", "CLAIMS"), ("PROD.X.COPYLIB", None)),
        })

    def test_folder_dataset_by_the_library_table_then_by_the_folder_name(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE library (id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, folder TEXT NOT NULL)")
        marked = os.path.join(tempfile.gettempdir(), "estate", "POLICY", "POLCOPY2")
        conn.execute("INSERT INTO library(dataset, folder) VALUES(?,?)", ("prod.policy.copylib2", marked))
        ctx = build.Ctx(conn, quiet=True)
        ctx.folder_dataset = build.load_folder_datasets(conn)
        self.assertEqual(build.folder_dataset(ctx, os.path.join(marked, "DUPREC.cpy")), "PROD.POLICY.COPYLIB2")
        self.assertEqual(build.folder_dataset(ctx, os.path.join(marked.lower(), "DUPREC.cpy")), "PROD.POLICY.COPYLIB2"
                         if os.path.normcase("A") == "a" else None)                  # the same folder, however spelt, on Windows
        est = os.path.join(tempfile.gettempdir(), "estate")
        self.assertEqual(build.folder_dataset(ctx, os.path.join(est, "CLAIMS", "prod.claims.copylib", "X.cpy")), "PROD.CLAIMS.COPYLIB")
        self.assertIsNone(build.folder_dataset(ctx, os.path.join(est, "CLAIMS", "COPYLIB", "X.cpy")))            # no dot
        self.assertIsNone(build.folder_dataset(ctx, os.path.join(est, "RECOVERED-COPYBOOKS", "X.cpy")))
        self.assertIsNone(build.folder_dataset(ctx, os.path.join(est, "TOOLONGQUAL.X", "X.cpy")))                # a qualifier of 9
        self.assertIsNone(build.folder_dataset(ctx, os.path.join(est, "A2345678.B2345678.C2345678.D2345678.E2345678.F", "X.cpy")))
        self.assertEqual(build.folder_dataset(ctx, os.path.join(est, "A2345678.B2345678.C2345678.D2345678.E2345678", "X.cpy")),
                         "A2345678.B2345678.C2345678.D2345678.E2345678")                                          # 44: the longest
        self.assertIn(build._folder_key(os.path.join(est, "CLAIMS", "COPYLIB")), ctx.folder_dataset)             # cached, None too


if __name__ == "__main__":
    unittest.main()
