"""
The resolver reads the compiler listing first (ROADMAP re-parse item 19).

Where a copybook's name exists in several libraries with different content
the build used to GUESS which copy to expand (COPY..OF, then the program's
own system in its declared order, then authoritative, then the same folder,
then the first). atlas.recover reads each program's compiler listing for the
library dataset the compiler read the copybook from and stores it in
listing_copy_source, each row dated (is the listing the compile of the
program as indexed?). Now build.make_resolver reads that table with its
chain: where a CURRENT listing names a dataset whose held copy (a folder
tied to it by the `library` table, else named after it) has a DIFFERENT
text from the chain's pick, that copy is expanded and the
'ambiguous_copybook' row says 'the program's compiler listing names
DATASET'; where it names the chain's own dataset, the chain's copy with that
how; where the copy it names has the SAME text (his listings name the
staging library the compile ran against, the copybook promoted unchanged),
the chain's copy stays - production, not staging - and the how says
'confirmed by the program's listing (same text as DATASET)'. An older
compile's listing, one not yet dated, a library the index does not hold, no
listing: the chain decides as before. The table absent or empty changes
nothing, the build never creates it, and a table written before recover
dated listings (no `current` column) decides nothing.
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
        self.assertEqual(self.first_stats["checked"], (1, 2, 0, 0, 1), self.first_said)
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 2 contradicted by a current listing, 0 named by an older listing, 0 name a library the index does not hold, "
                      "1 unknown", "\n".join(self.first_said))

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
        self.assertEqual(recover.choice_counts(by.values()), (3, 0, 0, 0, 1))
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((3, 0, 0, 0, 1), 0), self.said)
        self.assertIn("copybook choices checked against the listings: 3 confirmed, 0 contradicted by a current listing, 0 named by an older listing, 0 name a library the index does not hold, "
                      "1 unknown", "\n".join(self.said))
        with open(self.report, encoding="utf-8") as fh:
            sec = fh.read().split("## Copybook choices, checked against the listings")[1]
        self.assertIn("_no choice contradicted by a current listing_", sec)
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
        self.assertIn("3 of these choices are confirmed by the program's listing, 0 contradicted by a current listing (see "
                      "work/recover.md), 0 named by an older listing, 0 name a library the index does not hold, 1 unknown", cov)
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
    hold, and a listing with no table, leave the chain to decide. The rows
    are stored as recover dates them: current."""

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
                "LISTED": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "LISTED.lst", True, 100)],
                "NESTED": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "NESTED.lst", True, 100)],
                "FETCHIT": [("DUPREC", "COPYLIB", "PROD.SHARED.COPYLIB", "FETCHIT.lst", True, 100)],
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
        self.assertEqual((v["verdict"], v["index_has"]), ("NOT HELD", "not a library the index holds"))     # the copy used stands

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


class OnlyACurrentListingWithADifferentTextDecides(unittest.TestCase):
    """The rows stored before the build, as recover dates them. DUPREC has
    three copies: CLAIMS's (the chain's pick for every CLAIMS program),
    STAGING's with the SAME text (a comment added - comments do not count)
    and POLICY's with a different text.

    - FOLLOWS: a current listing names POLICY's library: POLICY's copy.
    - PROMOTED: a current listing names the staging library, whose copy has
      the same text: the chain's CLAIMS copy stays, 'confirmed by the
      program's listing (same text as PROD.STG.COPYLIB)'.
    - OWNLIB: a current listing names CLAIMS's own library.
    - TWONAMED: a current listing names the staging library and POLICY's:
      the different text wins, as recover's verdict order has it.
    - OLDLIST / UNDATED: an older compile's listing, one not yet dated, both
      naming POLICY's: the chain decides.
    - NOTHELD: a current listing names a library the index does not hold.

    Then recover's check on the index the build made: nothing contradicted
    by a current listing, nothing marked - the stand-in finds nothing to do
    on an index built by this batch - and the words agree with the notes."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        names = ("FOLLOWS", "PROMOTED", "OWNLIB", "TWONAMED", "OLDLIST", "UNDATED", "NOTHELD")
        files = {f"CLAIMS/PROD.CLAIMS.SRC/{n}.cbl": program(n, "DUPREC") for n in names}
        files.update({
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "STAGING/PROD.STG.COPYLIB/DUPREC.cpy": "      * promoted unchanged\n" + DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        })
        write_estate(cls.root, files)
        cls.claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        cls.stg = os.path.join(cls.root, "STAGING", "PROD.STG.COPYLIB", "DUPREC.cpy")
        cls.policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")

        def row(dsn, cur=True, matched=100, lst=None):
            return ("DUPREC", "SYSLIB", dsn, lst or "x.lst", cur, matched)

        cls.db = os.path.join(cls.td, "t.db")
        conn = build.open_db(cls.db)
        try:
            recover.store_copy_sources(conn, {
                "FOLLOWS": [row("PROD.POLICY.COPYLIB", lst="FOLLOWS.lst")],
                "PROMOTED": [row("PROD.STG.COPYLIB", lst="PROMOTED.lst")],
                "OWNLIB": [row("PROD.CLAIMS.COPYLIB", lst="OWNLIB.lst")],
                "TWONAMED": [row("PROD.STG.COPYLIB", lst="TWONAMED.lst"), row("PROD.POLICY.COPYLIB", lst="TWONAMED.lst")],
                "OLDLIST": [row("PROD.POLICY.COPYLIB", False, 90, "OLDLIST.lst")],
                "UNDATED": [row("PROD.POLICY.COPYLIB", None, None, "UNDATED.lst")],
                "NOTHELD": [row("PROD.GONE.COPYLIB", lst="NOTHELD.lst")],
            })
        finally:
            conn.close()
        build_it(cls.root, cls.db)
        cls.said = []
        cls.report = os.path.join(cls.td, "work", "recover.md")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_0_the_premise(self):
        shas = {os.path.basename(os.path.dirname(p)): s for p, s in
                self.conn.execute("SELECT path, norm_sha FROM member WHERE name='DUPREC'").fetchall()}
        self.assertEqual(shas["PROD.CLAIMS.COPYLIB"], shas["PROD.STG.COPYLIB"], "the same code: the comment does not count")
        self.assertNotEqual(shas["PROD.CLAIMS.COPYLIB"], shas["PROD.POLICY.COPYLIB"])

    def test_1_what_the_resolver_expanded(self):
        want = {
            "FOLLOWS": (self.policy, "the program's compiler listing names PROD.POLICY.COPYLIB"),
            "PROMOTED": (self.claims, "confirmed by the program's listing (same text as PROD.STG.COPYLIB)"),
            "OWNLIB": (self.claims, "the program's compiler listing names PROD.CLAIMS.COPYLIB"),
            "TWONAMED": (self.policy, "the program's compiler listing names PROD.POLICY.COPYLIB"),
            "OLDLIST": (self.claims, "same system"),
            "UNDATED": (self.claims, "same system"),
            "NOTHELD": (self.claims, "same system"),
        }
        for name, (path, how) in want.items():
            with self.subTest(program=name):
                note, used = pick_of(self.conn, name)
                self.assertEqual(used, path)
                self.assertEqual(note, f"3 copies of DUPREC with different content; used {path} ({how})")
                self.assertEqual(expanded_from(self.conn, name), {path})

    def test_2_both_regexes_read_the_note_with_its_parentheses(self):
        note, _used = pick_of(self.conn, "PROMOTED")
        m = recover._PICK.search(note)
        self.assertEqual((m.group(2), m.group(3), m.group(4)),
                         ("DUPREC", self.claims, "confirmed by the program's listing (same text as PROD.STG.COPYLIB)"))
        m = query._PICK_RE.search(note)
        self.assertEqual((m.group(3), m.group(4)), (self.claims, "confirmed by the program's listing (same text as PROD.STG.COPYLIB)"))
        # a note written before this batch, and a path holding parentheses, read as before
        old = r"2 copies of X with different content; used C:\e\A (copy)\X.cpy (same system)"
        for rx in (recover._PICK, query._PICK_RE):
            m = rx.search(old)
            self.assertEqual((m.group(3), m.group(4)), (r"C:\e\A (copy)\X.cpy", "same system"))

    def test_3_recover_agrees_and_marks_nothing(self):
        by = {v["program"]: v for v in recover.check_choices(self.conn)}
        self.assertEqual({p: v["verdict"] for p, v in by.items()}, {
            "FOLLOWS": "CONFIRMED", "PROMOTED": "CONFIRMED", "OWNLIB": "CONFIRMED", "TWONAMED": "CONFIRMED",
            "OLDLIST": "OLDER", "UNDATED": "CONTRADICTED", "NOTHELD": "NOT HELD"})
        self.assertEqual(by["PROMOTED"]["why"], "same text as the copy the listing names in PROD.STG.COPYLIB - promoted")
        self.assertEqual(by["UNDATED"]["current"], None)
        self.assertEqual(recover.mark_contradicted(self.db, list(by.values()), dry_run=True), (0, False))
        self.assertEqual([p for p, v in by.items() if recover.marks(v)], [])

    def test_4_program_says_what_the_listing_says(self):
        out = query.cmd_program(self.conn, "PROMOTED")
        self.assertIn("- listing says: DUPREC came from PROD.STG.COPYLIB (SYSLIB) - confirms the copy the build used (same text, "
                      "promoted from PROD.STG.COPYLIB)", out)
        out = query.cmd_program(self.conn, "TWONAMED")
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used", out)
        self.assertIn("- listing says: DUPREC came from PROD.STG.COPYLIB (SYSLIB) - the build used PROD.POLICY.COPYLIB: the "
                      "listing names 2 libraries for DUPREC (PROD.POLICY.COPYLIB, PROD.STG.COPYLIB); the copy used is one of "
                      "them", out)

    def test_5_coverage_counts_the_confirmed_apart(self):
        cov = query.cmd_coverage(self.conn)
        chosen = cov.split("### Complete, with a copybook chosen among several")[1].split("\n###")[0]
        self.assertTrue(chosen.startswith(": 7 members - 3 choices decided by the program's compiler listing, 1 picked by "
                                          "system and library order and confirmed by the listing (the same text as the library "
                                          "it names), 3 picked by system and library order alone; the 'ambiguous_copybook' "
                                          "rows name the copy used"), chosen[:300])
        self.assertIn("| cobol | PROMOTED | PROD.CLAIMS.SRC | DUPREC: 3 copies, used CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy "
                      "(listing: same text as PROD.STG.COPYLIB) |", chosen)
        self.assertIn("3 of the choices are the compiler's own: the program's listing names the library the copybook was read "
                      "from, and that copy was expanded (`listing: DATASET` in the table) - nothing to declare for those. 1 is "
                      "confirmed by the listing: it names a library whose copy has the same text as the copy expanded - a "
                      "copybook compiled against staging and promoted unchanged (`listing: same text as DATASET`) - nothing "
                      "to declare for those either. The other 3 follow `COPY ... OF`", chosen)
        kinds = cov.split("### Unresolved by kind")[1].split("\n###")[0]
        self.assertIn("| ambiguous_copybook (decided by the listing) | 3 |", kinds)
        self.assertIn("| ambiguous_copybook (confirmed by the listing) | 1 | two copies of one copybook with different content; "
                      "system and library order picked one, and the program's compiler listing names a library whose copy has "
                      "the same text (compiled against staging, promoted unchanged) | nothing - the compiler's own record "
                      "agrees with the copy expanded; nothing to declare |", kinds)
        self.assertIn("| ambiguous_copybook | 3 |", kinds)


class SeveralListingsTheCurrentOneDecides(unittest.TestCase):
    """Found while merging main's five verdicts into the resolver: two
    listings of one program (his --from folders mix older and current
    compiles), or two folders holding one dataset.

    - check_choices took the first differing dataset BY NAME: an older
      listing naming PROD.APOL.COPYLIB outranked a current one naming
      PROD.POLICY.COPYLIB, so a choice the current compile contradicts read
      OLDER and was never marked - while the resolver follows the current
      listing. Now a current listing's findings decide, as in the resolver.
    - `program` printed '(same text, promoted from DATASET)' beside the
      library the build USED whenever a confirmed verdict had a why - the
      two-listings case (scenario A2) included, where nothing was promoted.
    - The same-text test compared the FIRST held copy in the dataset only:
      with two folders holding one dataset, one copy with the chosen text and
      one without, the verdict depended on member order and could read
      CONTRADICTED while the resolver kept its pick - marked, parsed again,
      marked again. Now any held copy with the text of the copy used makes it
      the same, in both.

    DUPREC: CLAIMS's (the chain's pick), STAGING's (same text), POLICY's and
    APOL's (each different); PROD.TWIN.COPYLIB held twice, POLICY's text in
    the first folder and CLAIMS's in the second."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        names = ("MIXED1", "MIXED2", "MIXED3", "TWOFOLD", "CHAINPGM")
        files = {f"CLAIMS/PROD.CLAIMS.SRC/{n}.cbl": program(n, "DUPREC") for n in names}
        twin_marker = json.dumps({"dataset": "PROD.TWIN.COPYLIB", "folder": "x", "rc": 0, "expected": 1, "present": 1,
                                  "complete": True, "missing": [], "stale": []})
        files.update({
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "STAGING/PROD.STG.COPYLIB/DUPREC.cpy": "      * promoted unchanged\n" + DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "POLICY/PROD.APOL.COPYLIB/DUPREC.cpy": DUPREC_THIRD,
            "SYSA/PROD.TWIN.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",     # the first held copy differs ...
            "SYSB/TWINCOPY/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",            # ... the second has the chosen text
            "SYSB/TWINCOPY/.atlas-library.json": twin_marker,
        })
        write_estate(cls.root, files)
        cls.claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        cls.policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")
        cls.db = os.path.join(cls.td, "t.db")
        conn = build.open_db(cls.db)
        try:
            recover.store_copy_sources(conn, {n: rows for n, rows in cls.rows().items() if n != "CHAINPGM"})
        finally:
            conn.close()
        build_it(cls.root, cls.db)

    @staticmethod
    def rows():
        old = ("DUPREC", "SYSLIB", "PROD.APOL.COPYLIB", "old.lst", False, 90)       # an older compile's listing
        return {
            "MIXED1": [old, ("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "cur.lst", True, 100)],
            "MIXED2": [old, ("DUPREC", "SYSLIB", "PROD.STG.COPYLIB", "cur.lst", True, 100)],
            "MIXED3": [old, ("DUPREC", "SYSLIB", "PROD.GONE.COPYLIB", "cur.lst", True, 100)],
            "TWOFOLD": [("DUPREC", "SYSLIB", "PROD.TWIN.COPYLIB", "cur.lst", True, 100)],
            "CHAINPGM": [old, ("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "cur.lst", True, 100)],
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_1_before_the_reparse_the_current_listing_decides_the_verdict(self):
        # CHAINPGM was parsed with no rows (the chain's CLAIMS copy); checked against both listings now
        v = {c["program"]: c for c in recover.check_choices(self.conn, {"CHAINPGM": self.rows()["CHAINPGM"]})}["CHAINPGM"]
        self.assertEqual((v["verdict"], v["named"], v["current"]), ("CONTRADICTED", "PROD.POLICY.COPYLIB", True))
        self.assertTrue(recover.marks(v))
        note, used = pick_of(self.conn, "CHAINPGM")
        self.assertTrue(note.endswith("(same system)"), note)

    def test_2_what_the_resolver_expanded(self):
        note, used = pick_of(self.conn, "MIXED1")
        self.assertEqual(used, self.policy)
        self.assertTrue(note.endswith("(the program's compiler listing names PROD.POLICY.COPYLIB)"), note)
        note, used = pick_of(self.conn, "MIXED2")
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith("(confirmed by the program's listing (same text as PROD.STG.COPYLIB))"), note)
        note, used = pick_of(self.conn, "MIXED3")
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith("(same system)"), note)
        note, used = pick_of(self.conn, "TWOFOLD")                       # one of the two copies has the chosen text
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith("(confirmed by the program's listing (same text as PROD.TWIN.COPYLIB))"), note)

    def test_3_recover_agrees_with_every_one(self):
        by = {v["program"]: v for v in recover.check_choices(self.conn)}
        self.assertEqual((by["MIXED1"]["verdict"], by["MIXED1"]["why"]),
                         ("CONFIRMED", "a current listing names the dataset the build used; another listing names "
                                       "PROD.APOL.COPYLIB and is not the current compile's"))
        self.assertEqual((by["MIXED2"]["verdict"], by["MIXED2"]["named"]), ("CONFIRMED", "PROD.STG.COPYLIB"))
        self.assertEqual((by["MIXED3"]["verdict"], by["MIXED3"]["named"]), ("NOT HELD", "PROD.GONE.COPYLIB"))
        self.assertEqual((by["TWOFOLD"]["verdict"], by["TWOFOLD"]["why"]),
                         ("CONFIRMED", "same text as the copy the listing names in PROD.TWIN.COPYLIB - promoted"))
        self.assertEqual([p for p, v in by.items() if recover.marks(v)], [])

    def test_4_program_says_which_library_confirms_and_why(self):
        out = query.cmd_program(self.conn, "MIXED1")
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
        self.assertIn("- listing says: DUPREC came from PROD.APOL.COPYLIB (SYSLIB) - the build used PROD.POLICY.COPYLIB: a current "
                      "listing names the dataset the build used; another listing names PROD.APOL.COPYLIB and is not the current "
                      "compile's", out)
        self.assertNotIn("promoted", out)
        out = query.cmd_program(self.conn, "MIXED2")
        self.assertIn("- listing says: DUPREC came from PROD.STG.COPYLIB (SYSLIB) - confirms the copy the build used (same text, "
                      "promoted from PROD.STG.COPYLIB)", out)
        self.assertIn("- listing says: DUPREC came from PROD.APOL.COPYLIB (SYSLIB) - the build used PROD.CLAIMS.COPYLIB: same "
                      "text as the copy the listing names in PROD.STG.COPYLIB - promoted", out)


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
        conn.executemany("INSERT INTO listing_copy_source(program, copybook, ddname, dataset, listing, seen, current, matched) "
                         "VALUES(?,?,?,?,?,?,?,?)", [
                             ("PGMF", "DUPREC", "SYSLIB", "PROD.X.COPYLIB", "a.lst", "x", 1, 100),     # current
                             ("PGMF", "DUPREC", "SYSLIB", "PROD.Y.COPYLIB", "a.lst", "x", 0, 90),      # an older compile's
                         ])
        self.assertEqual(build.load_listing_sources(conn), {
            ("PGMA", "DUPREC"): (("PROD.POLICY.COPYLIB", "CLAIMS", None), ("PROD.OTHER.COPYLIB", "CLAIMS", None)),
            ("PGMA", "OTHER"): (("PROD.CLAIMS.COPYLIB", "CLAIMS", None),),
            ("PGMB", "DUPREC"): (("PROD.X.COPYLIB", None, None),),
            ("PGMC", "DUPREC"): (("PROD.X.COPYLIB", None, None),),
            ("PGMD", "DUPREC"): (("PROD.X.COPYLIB", None, None),),
            ("PGME", "DUPREC"): (("PROD.X.COPYLIB", "CLAIMS", None), ("PROD.X.COPYLIB", None, None)),
            ("PGMF", "DUPREC"): (("PROD.X.COPYLIB", "CLAIMS", True), ("PROD.Y.COPYLIB", "CLAIMS", False)),
        })
        rows = build.load_listing_sources(conn)
        self.assertEqual(build.current_datasets(rows[("PGMF", "DUPREC")], "CLAIMS", set()), ["PROD.X.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("PGMA", "DUPREC")], "CLAIMS", set()), [])      # not dated: none
        self.assertEqual(build.current_datasets(rows[("PGMF", "DUPREC")], "POLICY", {"CLAIMS"}), [])  # another system's

    def test_a_table_written_before_recover_dated_listings(self):
        # the columns current / matched absent (recover of an earlier toolkit wrote it): every row reads not dated
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE listing_copy_source (program TEXT NOT NULL, copybook TEXT NOT NULL, ddname TEXT, "
                     "dataset TEXT, listing TEXT, seen TEXT)")
        conn.execute("CREATE TABLE member (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, system TEXT)")
        conn.execute("INSERT INTO listing_copy_source VALUES('PGMA', 'DUPREC', 'SYSLIB', 'PROD.X.COPYLIB', 'a.lst', 'x')")
        self.assertEqual(build.load_listing_sources(conn), {("PGMA", "DUPREC"): (("PROD.X.COPYLIB", None, None),)})
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(listing_copy_source)")],
                         ["program", "copybook", "ddname", "dataset", "listing", "seen"])      # the build never alters it

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
