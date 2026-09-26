"""
Which listing speaks for which program; a contradicted choice is marked for
the next build (ROADMAP re-parse item 19, the verifier's rounds).

atlas.recover keys the listing rows by the listing's file stem, so every
listing of one program NAME shares the key: two environments holding one
program name (GC and GC-TEST, each with its own copybook library and its own
compiler listing), a listing filed under a system of its own (SHARED, a
LISTINGS folder), a listing read from a --from folder the index does not
hold. The build (build.current_datasets, per (program, copybook)), the check
(recover.rows_of_system, per copybook) and `program`'s 'listing says' lines
(recover.rows_of_system, per program, grouped by copybook) apply ONE rule,
build.rows_that_count, per copybook: when another system holds a program of
that name, only the listings in the program's own system speak for it;
while no other system holds one, a current listing in its own system
decides every copybook it names, and for a copybook no current listing of
its own names, every listing of the name speaks wherever it is filed and
the current one decides. One environment's listing never decides for the
other's copy, a listing filed elsewhere never decides for either of two
same-named programs, a SHARED listing decides for the one CLAIMS program of
its name - the verifier's case, where derive_systems made the listing SHARED
and the rule of the earlier round threw it away - an older compile's
listing in the program's own folder hides neither a current one filed under
SHARED nor one read from --from (EveryListingOfAOneOfAKindProgram), and a
listing filed elsewhere never overrides the program's own current listing
(ItsOwnCurrentListingDecides). A twin's UNKNOWN reason says what its own
listing lacks - not read, no table, no row in a current or an older
compile's table - with a next step that changes the answer (ATwinsOwnListing).
A current listing naming two libraries for one copybook confirms the copy
used when it is one of them; a `library` row with an empty dataset leaves
the folder name to decide, in the build as in recover; and a choice a
CURRENT listing contradicts (the index holds the copy it names, with a
different text) marks the program for the next build - on an index this
toolkit built; on one another toolkit built the next build re-parses every
member anyway. A choice contradicted by a listing not yet dated marks
nothing: the build follows a current listing only, and a mark would parse
the program again for nothing on every run. Coverage says which choices the
listing decided and shows that word whole.
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
from test_copy_sources import DUPREC_CLAIMS, DUPREC_POLICY, listing, listing2, older_listing, program  # noqa: E402
from test_recover import ibm_listing  # noqa: E402
from test_listing_resolver import build_it, expanded_from, pick_of, write_estate  # noqa: E402

LISTING = "the program's compiler listing names "


def member_id(conn, system, name):
    return conn.execute("SELECT id FROM member WHERE name=? AND kind='cobol' AND system=?", (name, system)).fetchone()[0]


def pick_by_id(conn, mid):
    """(the 'ambiguous_copybook' note, the path the COPY DUPREC row resolved to, parse_status) of one member."""
    note = conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind='ambiguous_copybook'", (mid,)).fetchone()
    row = conn.execute("SELECT r.path FROM copy_use u JOIN member r ON r.id = u.resolved_member_id "
                       "WHERE u.member_id=? AND UPPER(u.copybook)='DUPREC'", (mid,)).fetchone()
    status = conn.execute("SELECT parse_status FROM member WHERE id=?", (mid,)).fetchone()[0]
    return (note[0] if note else None), (row[0] if row else None), status


def expanded_by_id(conn, mid):
    return {r[0] for r in conn.execute("SELECT s.path FROM expand_run r JOIN program p ON p.id = r.program_id "
                                       "JOIN member s ON s.id = r.src_member WHERE p.member_id=? AND r.depth > 0", (mid,))}


def statuses(db):
    conn = sqlite3.connect(db)
    try:
        return {(r[0], r[1]): r[2] for r in conn.execute("SELECT system, name, parse_status FROM member WHERE kind='cobol'")}
    finally:
        conn.close()


def set_fingerprint(db, fp):
    """An index another toolkit built: its last build carries that toolkit's fingerprint."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE build_run SET fingerprint=?", (fp,))
        conn.commit()
    finally:
        conn.close()


class TwoEnvironmentsOneProgramName(unittest.TestCase):
    """GC and GC-TEST each hold GCPGM1 and GCPGM2 with their own DUPREC; each
    GCPGM1 has its own listing, only GC-TEST's GCPGM2 has one."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "GC/PROD.GC.SRC/GCPGM1.cbl": program("GCPGM1", "DUPREC"),
            "GC-TEST/TEST.GC.SRC/GCPGM1.cbl": program("GCPGM1", "DUPREC"),
            "GC/PROD.GC.SRC/GCPGM2.cbl": program("GCPGM2", "DUPREC"),
            "GC-TEST/TEST.GC.SRC/GCPGM2.cbl": program("GCPGM2", "DUPREC"),
            "GC/PROD.GC.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "GC-TEST/TEST.GC.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "GC/PROD.GC.LISTING/GCPGM1.lst": listing("GCPGM1", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.GC.COPYLIB")]),
            "GC-TEST/TEST.GC.LISTING/GCPGM1.lst": listing("GCPGM1", ["DUPREC"], [("DUPREC", "SYSLIB", "TEST.GC.COPYLIB")]),
            "GC-TEST/TEST.GC.LISTING/GCPGM2.lst": listing("GCPGM2", ["DUPREC"], [("DUPREC", "SYSLIB", "TEST.GC.COPYLIB")]),
        })
        cls.prod = os.path.join(cls.root, "GC", "PROD.GC.COPYLIB", "DUPREC.cpy")
        cls.test = os.path.join(cls.root, "GC-TEST", "TEST.GC.COPYLIB", "DUPREC.cpy")
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        build_it(cls.root, cls.db, rebuild=True)
        cls.first_said = []
        cls.first_stats = recover.run(cls.db, log=cls.first_said.append, report=cls.report)
        cls.after_first = statuses(cls.db)
        recover._mark_programs(cls.db, ["DUPREC"])                       # the re-parse: every program parsed again with the rows in place
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

    def test_1_the_first_build_kept_each_environment_with_its_own_copy_and_recover_agreed(self):
        # the chain ('same system') picked each program's own copy; the check reads each program's own listing,
        # not the other environment's, so nothing was contradicted and nothing marked
        self.assertEqual(self.first_stats["checked"], (3, 0, 0, 0, 1), self.first_said)
        self.assertIn("copybook choices checked against the listings: 3 confirmed, 0 contradicted by a current listing, "
                      "0 named by an older listing, 0 name a library the index does not hold, 1 unknown", "\n".join(self.first_said))
        self.assertNotIn("marked for the next build", "\n".join(self.first_said))
        self.assertEqual(self.first_stats["marked"], 0)
        self.assertEqual(set(self.after_first.values()), {"ok"})                  # a chosen copybook: whole (item 18)

    def test_2_parsed_again_each_program_takes_its_own_listing(self):
        gc1, gt1 = member_id(self.conn, "GC", "GCPGM1"), member_id(self.conn, "GC-TEST", "GCPGM1")
        note, used, _s = pick_by_id(self.conn, gc1)
        self.assertEqual(used, self.prod)
        self.assertEqual(note, f"2 copies of DUPREC with different content; used {self.prod} ({LISTING}PROD.GC.COPYLIB)")
        self.assertEqual(expanded_by_id(self.conn, gc1), {self.prod})
        note, used, _s = pick_by_id(self.conn, gt1)
        self.assertEqual(used, self.test)
        self.assertEqual(note, f"2 copies of DUPREC with different content; used {self.test} ({LISTING}TEST.GC.COPYLIB)")
        self.assertEqual(expanded_by_id(self.conn, gt1), {self.test})

    def test_3_a_program_with_no_listing_of_its_own_stays_with_the_chain(self):
        gc2, gt2 = member_id(self.conn, "GC", "GCPGM2"), member_id(self.conn, "GC-TEST", "GCPGM2")
        note, used, _s = pick_by_id(self.conn, gc2)
        self.assertEqual(used, self.prod)                                 # GC-TEST's listing of GCPGM2 says nothing for GC's
        self.assertEqual(note, f"2 copies of DUPREC with different content; used {self.prod} (same system)")
        note, used, _s = pick_by_id(self.conn, gt2)
        self.assertEqual(used, self.test)
        self.assertTrue(note.endswith(f"({LISTING}TEST.GC.COPYLIB)"), note)

    def test_4_the_rows_are_loaded_with_the_listing_members_system(self):
        rows = build.load_listing_sources(self.conn)
        self.assertEqual(set(rows), {("GCPGM1", "DUPREC"), ("GCPGM2", "DUPREC")})
        self.assertEqual(set(rows[("GCPGM1", "DUPREC")]), {("PROD.GC.COPYLIB", "GC", True), ("TEST.GC.COPYLIB", "GC-TEST", True)})
        self.assertEqual(rows[("GCPGM2", "DUPREC")], (("TEST.GC.COPYLIB", "GC-TEST", True),))
        self.assertEqual(build.current_datasets(rows[("GCPGM1", "DUPREC")], "GC", {"GC-TEST"}), ["PROD.GC.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("GCPGM1", "DUPREC")], "GC-TEST", {"GC"}), ["TEST.GC.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("GCPGM2", "DUPREC")], "GC", {"GC-TEST"}), [])
        self.assertEqual(build.current_datasets(rows[("GCPGM2", "DUPREC")], "CLAIMS", {"GC", "GC-TEST"}), [])
        # the twins as the build reads them: every program member of the name in another system
        ctx = build.Ctx(self.conn, quiet=True)
        for sysname, name, twins in (("GC", "GCPGM1", {"GC-TEST"}), ("GC-TEST", "GCPGM2", {"GC"})):
            mems = [build.Mem(member_id(self.conn, s_, name), f"{s_}/{name}.cbl", name, "cobol", "L", "x", system=s_)
                    for s_ in ("GC", "GC-TEST")]
            ctx.by_name[name] = mems + [build.Mem(0, f"SHARED/{name}.lst", name, "listing", "L", "y", system="SHARED")]
            prog = next(m for m in mems if m.system == sysname)
            self.assertEqual(build.twin_systems(ctx, prog), twins)

    def test_5_recover_confirms_every_choice_and_says_whose_listing_it_lacks(self):
        self.assertEqual(self.stats["checked"], (3, 0, 0, 0, 1), self.said)
        self.assertEqual(self.stats["marked"], 0)
        by = {(v["system"], v["program"]): v for v in recover.check_choices(self.conn)}
        self.assertEqual({k: v["verdict"] for k, v in by.items()},
                         {("GC", "GCPGM1"): "CONFIRMED", ("GC-TEST", "GCPGM1"): "CONFIRMED",
                          ("GC", "GCPGM2"): "UNKNOWN", ("GC-TEST", "GCPGM2"): "CONFIRMED"})
        self.assertEqual(by[("GC", "GCPGM1")]["listing_datasets"], ["PROD.GC.COPYLIB"])
        self.assertEqual(by[("GC-TEST", "GCPGM1")]["listing_datasets"], ["TEST.GC.COPYLIB"])
        why = (r"the listing read is GC-TEST's, and GC-TEST holds a GCPGM2 of its own: no listing of the program in GC - "
               r"next: put GC's own listing of GCPGM2 in a folder under estate\GC, run the build, then run recover again")
        self.assertEqual(by[("GC", "GCPGM2")]["why"], why)
        with open(self.report, encoding="utf-8") as fh:
            sec = fh.read().split("## Copybook choices, checked against the listings")[1]
        self.assertIn("_no choice contradicted by a current listing_", sec)
        self.assertIn(f"(1: {why})", sec)
        self.assertIn(recover.LISTING_RULE, sec)
        self.assertIn("A program's own system's listings speak for it. When another system holds a program of the same name "
                      "(GC and GC-TEST), only those do: each environment keeps its own listing's word, and a listing filed "
                      "anywhere else cannot say which of them it is. While no other system holds one, the program's own current "
                      "listing decides every copybook it names, and a listing filed elsewhere never overrides it; for a "
                      "copybook no current listing of its own names (it has none, only an older compile's, or one not yet "
                      "dated), every listing of the program's name speaks, wherever it is filed - a SHARED listings folder, a "
                      "--from folder - and where two of them disagree the current one (its source is the program as indexed) "
                      "decides.", sec)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})

    def test_6_program_shows_its_own_listing_only(self):
        out = query.cmd_program(self.conn, "GC/GCPGM1")
        self.assertIn("- listing says: DUPREC came from PROD.GC.COPYLIB (SYSLIB) - confirms the copy the build used", out)
        self.assertNotIn("TEST.GC.COPYLIB", out.split("### Listing says")[1])
        out = query.cmd_program(self.conn, "GC-TEST/GCPGM1")
        self.assertIn("- listing says: DUPREC came from TEST.GC.COPYLIB (SYSLIB) - confirms the copy the build used", out)
        self.assertNotIn("PROD.GC.COPYLIB", out.split("### Listing says")[1])
        self.assertNotIn("listing says", query.cmd_program(self.conn, "GC/GCPGM2"))      # no listing of its own: no line
        self.assertEqual(query._listing_says(self.conn, member_id(self.conn, "GC", "GCPGM2")), "")

    def test_7_the_same_rule_in_both_readers(self):
        rows = [("DUPREC", "SYSLIB", "PROD.GC.COPYLIB", "gc.lst", True, 100),
                ("DUPREC", "SYSLIB", "TEST.GC.COPYLIB", "gt.lst", True, 100),
                ("DUPREC", "SYSLIB", "PROD.SHR.COPYLIB", "shared.lst", True, 100),
                ("DUPREC", "SYSLIB", "PROD.X.COPYLIB", "nowhere.lst", True, 100)]
        systems = {"gc.lst": "GC", "gt.lst": "GC-TEST", "shared.lst": "SHARED"}   # nowhere.lst: not indexed
        loaded = [("PROD.GC.COPYLIB", "GC", True), ("TEST.GC.COPYLIB", "GC-TEST", True), ("PROD.SHR.COPYLIB", "SHARED", True),
                  ("PROD.X.COPYLIB", None, True)]                           # as load_listing_sources gives them: all current
        every = ["PROD.GC.COPYLIB", "TEST.GC.COPYLIB", "PROD.SHR.COPYLIB", "PROD.X.COPYLIB"]
        for system, twins, want in (
                ("GC", {"GC-TEST"}, ["PROD.GC.COPYLIB"]),                   # a twin: its own system's listing, and only that
                ("GC-TEST", {"GC"}, ["TEST.GC.COPYLIB"]),
                ("GC", set(), ["PROD.GC.COPYLIB"]),                         # no twin, a current listing of its own: that one
                ("SHARED", set(), ["PROD.SHR.COPYLIB"]),
                ("CLAIMS", set(), every),                                   # no twin, none of its own: every listing of the name
                ("CLAIMS", {"GC"}, []),                                     # none of its own, a twin: none of them
                (None, set(), every),                                       # no system, no twin: every one
                (None, {"GC"}, [])):                                        # no system, a twin: none is its own
            with self.subTest(system=system, twins=twins):
                self.assertEqual([r[2] for r in recover.rows_of_system(rows, system, systems, twins)], want)
                self.assertEqual(build.current_datasets(loaded, system, twins), want)
        self.assertEqual(build.current_datasets(loaded, "gc", {"GC-TEST"}), ["PROD.GC.COPYLIB"])   # the system in any case
        # only a current listing's rows reach the resolver; recover checks every row that counts and dates them itself.
        # An own listing that is not current (older, not dated) hides no current one filed elsewhere
        dated = [("PROD.GC.COPYLIB", "GC", False), ("TEST.GC.COPYLIB", "GC-TEST", None), ("PROD.SHR.COPYLIB", "SHARED", True)]
        self.assertEqual(build.current_datasets(dated, "GC", set()), ["PROD.SHR.COPYLIB"])
        self.assertEqual(build.current_datasets(dated, "GC-TEST", set()), ["PROD.SHR.COPYLIB"])
        self.assertEqual(build.current_datasets(dated, "GC", {"GC-TEST"}), [])
        # the verifier's VNHDIFF: its own current listing names a staging library the index does not hold, a current one
        # filed under SHARED names POLICY's - its own listing's word stands, both ways round
        both = [("STG.NOTHELD.COPYLIB", "CLAIMS", True), ("PROD.POLICY.COPYLIB", "SHARED", True)]
        self.assertEqual(build.current_datasets(both, "CLAIMS", set()), ["STG.NOTHELD.COPYLIB"])
        self.assertEqual(build.current_datasets(both[::-1], "CLAIMS", set()), ["STG.NOTHELD.COPYLIB"])
        self.assertEqual(build.current_datasets([("PROD.POLICY.COPYLIB", "CLAIMS", True), ("PROD.CLAIMS.COPYLIB", "SHARED", True)],
                                                "CLAIMS", set()), ["PROD.POLICY.COPYLIB"])
        # per copybook: its own current listing names DUPREC only; FEEREC is named only by the one filed under SHARED
        prog = [("DUPREC", "SYSLIB", "STG.NOTHELD.COPYLIB", "own.lst", True, 100),
                ("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "shared.lst", True, 100),
                ("FEEREC", "SYSLIB", "PROD.POLICY.COPYLIB", "shared.lst", True, 100)]
        where = {"own.lst": "CLAIMS", "shared.lst": "SHARED"}
        self.assertEqual([(r[0], r[2]) for r in recover.rows_of_system(prog, "CLAIMS", where, set())],
                         [("DUPREC", "STG.NOTHELD.COPYLIB"), ("FEEREC", "PROD.POLICY.COPYLIB")])
        for copybook in ("DUPREC", "FEEREC"):                               # the same rows per copybook as per program
            with self.subTest(copybook=copybook):
                one = [r for r in prog if r[0] == copybook]
                self.assertEqual(recover.rows_of_system(one, "CLAIMS", where, set()),
                                 [r for r in recover.rows_of_system(prog, "CLAIMS", where, set()) if r[0] == copybook])
                self.assertEqual(build.current_datasets([(r[2], where[r[3]], r[4]) for r in one], "CLAIMS", set()),
                                 [r[2] for r in recover.rows_of_system(one, "CLAIMS", where, set())])


class AListingNamingTwoLibraries(unittest.TestCase):
    """The copybook read from two DDs: the listing names two datasets for it.
    The build takes the one the index holds; the check says CONFIRMED - the
    copy used is one the compiler read - whether the listing or the chain
    picked it."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLM.SRC/TWOLIB.cbl": program("TWOLIB", "DUPREC"),
            "CLAIMS/PROD.CLM.SRC/CHAIN2.cbl": program("CHAIN2", "DUPREC"),
            "CLAIMS/PROD.CLM.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POL.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
        })
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        conn = build.open_db(cls.db)
        try:
            recover.store_copy_sources(conn, {"TWOLIB": [("DUPREC", "SYSLIB", "PROD.NOTHERE.COPYLIB", "TWOLIB.lst", True, 100),
                                                         ("DUPREC", "MYLIB", "PROD.CLM.COPYLIB", "TWOLIB.lst", True, 100)]})
        finally:
            conn.close()
        build_it(cls.root, cls.db)
        conn = build.open_db(cls.db)                                     # CHAIN2's rows stored after its parse: the chain's pick
        try:
            recover.store_copy_sources(conn, {"CHAIN2": [("DUPREC", "SYSLIB", "PROD.NOTHERE.COPYLIB", "CHAIN2.lst", True, 100),
                                                         ("DUPREC", "MYLIB", "PROD.CLM.COPYLIB", "CHAIN2.lst", True, 100)]})
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_1_the_build_took_the_library_the_index_holds(self):
        note, used = pick_of(self.conn, "TWOLIB")
        self.assertEqual(used, os.path.join(self.root, "CLAIMS", "PROD.CLM.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith(f"({LISTING}PROD.CLM.COPYLIB)"), note)
        note, used = pick_of(self.conn, "CHAIN2")
        self.assertTrue(note.endswith("(same system)"), note)

    def test_2_confirmed_the_copy_used_is_one_of_them(self):
        by = {v["program"]: v for v in recover.check_choices(self.conn)}
        for name in ("TWOLIB", "CHAIN2"):
            self.assertEqual(by[name]["verdict"], "CONFIRMED", by[name])
            self.assertEqual(by[name]["why"], "the listing names 2 libraries for DUPREC (PROD.CLM.COPYLIB, PROD.NOTHERE.COPYLIB); "
                                              "the copy used is one of them")
            self.assertEqual(by[name]["marked"], "")
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["checked"], stats["marked"]), ((2, 0, 0, 0, 0), 0), said)
        self.assertNotIn("is a wrong fact in the index", "\n".join(said))
        self.assertEqual(set(statuses(self.db).values()), {"ok"})

    def test_3_program_names_both_libraries_and_which_one_was_used(self):
        out = query.cmd_program(self.conn, "TWOLIB")
        self.assertIn("- listing says: DUPREC came from PROD.CLM.COPYLIB (MYLIB) - confirms the copy the build used", out)
        self.assertIn("- listing says: DUPREC came from PROD.NOTHERE.COPYLIB (SYSLIB) - the build used PROD.CLM.COPYLIB: the "
                      "listing names 2 libraries for DUPREC (PROD.CLM.COPYLIB, PROD.NOTHERE.COPYLIB); the copy used is one of "
                      "them", out)
        self.assertNotIn("CONTRADICTS", out)


class AnEmptyDatasetMarker(unittest.TestCase):
    """A `.atlas-library.json` whose dataset is empty: the folder's own name
    decides in the build exactly as in recover.dataset_of, so a listing that
    names it is followed and then confirmed."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLM.SRC/EDGEPGM.cbl": program("EDGEPGM", "DUPREC"),
            "CLAIMS/PROD.CLM.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.EDGE.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "POLICY/PROD.EDGE.COPYLIB/.atlas-library.json": json.dumps({
                "dataset": "", "folder": "x", "rc": 0, "expected": 1, "present": 1, "complete": True, "missing": [], "stale": []}),
        })
        cls.db = os.path.join(cls.td, "t.db")
        conn = build.open_db(cls.db)
        try:
            recover.store_copy_sources(conn, {"EDGEPGM": [("DUPREC", "SYSLIB", "PROD.EDGE.COPYLIB", "EDGEPGM.lst", True, 100)]})
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

    def test_1_the_folder_name_decides_in_both_readers(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE library (id INTEGER PRIMARY KEY, dataset TEXT NOT NULL, folder TEXT NOT NULL)")
        empty = os.path.join(self.root, "POLICY", "PROD.EDGE.COPYLIB")
        conn.execute("INSERT INTO library(dataset, folder) VALUES(?,?)", ("", empty))
        conn.execute("INSERT INTO library(dataset, folder) VALUES(?,?)", ("  ", os.path.join(self.root, "POLICY", "BLANK")))
        self.assertEqual(build.load_folder_datasets(conn), {})
        ctx = build.Ctx(conn, quiet=True)
        ctx.folder_dataset = build.load_folder_datasets(conn)
        self.assertEqual(build.folder_dataset(ctx, os.path.join(empty, "DUPREC.cpy")), "PROD.EDGE.COPYLIB")
        self.assertIsNone(build.folder_dataset(ctx, os.path.join(self.root, "POLICY", "BLANK", "X.cpy")))
        self.assertEqual(recover.dataset_of(os.path.join(empty, "DUPREC.cpy"), {recover._fkey(empty): ""}), "PROD.EDGE.COPYLIB")

    def test_2_the_listing_is_followed_and_confirmed(self):
        note, used = pick_of(self.conn, "EDGEPGM")
        self.assertEqual(used, os.path.join(self.root, "POLICY", "PROD.EDGE.COPYLIB", "DUPREC.cpy"))
        self.assertTrue(note.endswith(f"({LISTING}PROD.EDGE.COPYLIB)"), note)
        self.assertEqual(expanded_from(self.conn, "EDGEPGM"), {used})
        v = recover.check_choices(self.conn)[0]
        self.assertEqual((v["program"], v["verdict"], v["used_dataset"]), ("EDGEPGM", "CONFIRMED", "PROD.EDGE.COPYLIB"))

    def test_3_coverage_every_choice_the_compilers_own(self):
        cov = query.cmd_coverage(self.conn)
        chosen = cov.split("### Complete, with a copybook chosen among several")[1].split("\n###")[0]
        self.assertTrue(chosen.startswith(": 1 member - 1 choice decided by the program's compiler listing, 0 picked by system "
                                          "and library order; the 'ambiguous_copybook' rows name the copy used"), chosen[:160])
        self.assertIn("| cobol | EDGEPGM | PROD.CLM.SRC | DUPREC: 2 copies, used POLICY/PROD.EDGE.COPYLIB/DUPREC.cpy "
                      "(listing: PROD.EDGE.COPYLIB) |", chosen)
        self.assertIn("Every choice is the compiler's own: the program's listing names the library the copybook was read from, "
                      "and that copy was expanded (`listing: DATASET` in the table) - nothing to declare", chosen)
        self.assertNotIn("- the build picked by system and library order", chosen)
        self.assertNotIn("a choice is wrong only where the manifest's system or copybook order is", chosen)
        kinds = cov.split("### Unresolved by kind")[1].split("\n###")[0]
        self.assertIn("| ambiguous_copybook (decided by the listing) | 1 | two copies of one copybook with different content; the "
                      "program's compiler listing names the library the compiler read it from, and that copy was expanded | "
                      "nothing - the compiler's own record, not a guess; nothing to declare |", kinds)
        self.assertNotIn("| expand (copybook chosen among several", kinds)     # no COPY warning repeats the choice (item 18)
        self.assertNotIn("| ambiguous_copybook | ", kinds)
        self.assertNotIn("copylib_order", kinds)


class ListingsFiledOutsideTheProgramsSystem(unittest.TestCase):
    """The verifier's case: WRONGPK lives in CLAIMS and its listing was
    fetched into estate\\SHARED\\PROD.LISTINGS, so derive_systems makes the
    listing SHARED. SHARED holds no program WRONGPK and no other system does
    either: the listing speaks for the CLAIMS program - recover marks it
    (the index holds the POLICY copy the listing names) and the next build
    expands that copy, the note naming the listing. FROMPGM's listing is read
    from a --from folder the index does not hold: the same. TWINPGM lives in
    GC and in GC-TEST, and its only listing is filed under SHARED; TWIN2's
    only listing is in the --from folder: each could be either environment's
    compile, so neither program takes it - each keeps its own system's copy,
    and recover says UNKNOWN with why and the next step."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        cls.from_dir = os.path.join(cls.td, "listings-pulled")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/WRONGPK.cbl": program("WRONGPK", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/FROMPGM.cbl": program("FROMPGM", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "GC/PROD.GC.SRC/TWINPGM.cbl": program("TWINPGM", "DUPREC"),
            "GC-TEST/TEST.GC.SRC/TWINPGM.cbl": program("TWINPGM", "DUPREC"),
            "GC/PROD.GC.SRC/TWIN2.cbl": program("TWIN2", "DUPREC"),
            "GC-TEST/TEST.GC.SRC/TWIN2.cbl": program("TWIN2", "DUPREC"),
            "GC/PROD.GC.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "GC-TEST/TEST.GC.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "SHARED/PROD.LISTINGS/WRONGPK.lst": listing("WRONGPK", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "SHARED/PROD.LISTINGS/TWINPGM.lst": listing("TWINPGM", ["DUPREC"], [("DUPREC", "SYSLIB", "TEST.GC.COPYLIB")]),
        })
        write_estate(cls.from_dir, {
            "FROMPGM.lst": listing("FROMPGM", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "TWIN2.lst": listing("TWIN2", ["DUPREC"], [("DUPREC", "SYSLIB", "TEST.GC.COPYLIB")]),
        })
        cls.claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        cls.policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")
        cls.gc = os.path.join(cls.root, "GC", "PROD.GC.COPYLIB", "DUPREC.cpy")
        cls.gctest = os.path.join(cls.root, "GC-TEST", "TEST.GC.COPYLIB", "DUPREC.cpy")
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        build_it(cls.root, cls.db, rebuild=True)
        cls.first_said = []
        cls.first_stats = recover.run(cls.db, [cls.from_dir], log=cls.first_said.append, report=cls.report)
        cls.after_first = statuses(cls.db)
        with open(cls.report, encoding="utf-8") as fh:
            cls.first_report = fh.read()
        build_it(cls.root, cls.db)                                        # incremental: the marked programs parsed again
        cls.said = []
        cls.stats = recover.run(cls.db, [cls.from_dir], log=cls.said.append, report=cls.report)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def test_1_the_listing_is_a_shared_member(self):
        row = self.conn.execute("SELECT kind, system FROM member WHERE name='WRONGPK' AND kind='listing'").fetchone()
        self.assertEqual(tuple(row), ("listing", "SHARED"))
        self.assertEqual(self.conn.execute("SELECT system FROM member WHERE name='WRONGPK' AND kind='cobol'").fetchone()[0], "CLAIMS")

    def test_2_recover_took_the_shared_and_the_from_listing_and_marked_both(self):
        text = "\n".join(self.first_said)
        self.assertEqual((self.first_stats["checked"], self.first_stats["marked"]), ((0, 2, 0, 0, 4), 2), text)
        self.assertIn("  2 programs whose current listing names a held copy with different text are marked for the next build - "
                      "it follows the listing", text)
        self.assertEqual(self.after_first[("CLAIMS", "WRONGPK")], "pending")
        self.assertEqual(self.after_first[("CLAIMS", "FROMPGM")], "pending")
        self.assertEqual({k: v for k, v in self.after_first.items() if k[1] in ("TWINPGM", "TWIN2")},
                         {("GC", "TWINPGM"): "ok", ("GC-TEST", "TWINPGM"): "ok", ("GC", "TWIN2"): "ok", ("GC-TEST", "TWIN2"): "ok"})
        self.assertIn("| WRONGPK | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | yes | CONTRADICTED - marked for the next build | a current listing names "
                      "PROD.POLICY.COPYLIB, whose text differs from the copy the build used |", self.first_report)

    def test_3_the_shared_listing_decides_for_the_claims_program(self):
        note, used = pick_of(self.conn, "WRONGPK")
        self.assertEqual(used, self.policy)
        self.assertEqual(note, f"4 copies of DUPREC with different content; used {self.policy} ({LISTING}PROD.POLICY.COPYLIB)")
        self.assertEqual(expanded_from(self.conn, "WRONGPK"), {self.policy})
        note, used = pick_of(self.conn, "FROMPGM")                        # the --from listing: no twin, so it counts
        self.assertEqual(used, self.policy)
        self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
        out = query.cmd_program(self.conn, "WRONGPK")
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used", out)

    def test_4_a_listing_filed_elsewhere_decides_for_neither_twin(self):
        for sysname, name, own in (("GC", "TWINPGM", self.gc), ("GC-TEST", "TWINPGM", self.gctest),
                                   ("GC", "TWIN2", self.gc), ("GC-TEST", "TWIN2", self.gctest)):
            with self.subTest(system=sysname, program=name):
                note, used, status = pick_by_id(self.conn, member_id(self.conn, sysname, name))
                self.assertEqual(used, own)                               # the chain: its own system's copy
                self.assertTrue(note.endswith("(same system)"), note)
                self.assertEqual(status, "ok")
                self.assertEqual(query._listing_says(self.conn, member_id(self.conn, sysname, name)), "")

    def test_5_recover_says_why_and_what_next(self):
        text = "\n".join(self.said)
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((2, 0, 0, 0, 4), 0), text)
        self.assertNotIn("marked for the next build", text)
        by = {(v["system"], v["program"]): v for v in recover.check_choices(self.conn)}
        self.assertEqual(by[("CLAIMS", "WRONGPK")]["verdict"], "CONFIRMED")
        self.assertEqual(by[("CLAIMS", "FROMPGM")]["verdict"], "CONFIRMED")
        self.assertEqual(by[("GC", "TWINPGM")]["why"],
                         r"the listing filed under SHARED cannot say which TWINPGM it is while GC-TEST holds one too: no "
                         r"listing of the program in GC - next: put GC's own listing of TWINPGM in a folder under estate\GC, "
                         r"run the build, then run recover again")
        self.assertEqual(by[("GC-TEST", "TWIN2")]["why"],
                         r"the listing not in the index (a --from folder) cannot say which TWIN2 it is while GC holds one too: "
                         r"no listing of the program in GC-TEST - next: put GC-TEST's own listing of TWIN2 in a folder under "
                         r"estate\GC-TEST, run the build, then run recover again")
        self.assertEqual({v["verdict"] for k, v in by.items() if k[1] in ("TWINPGM", "TWIN2")}, {"UNKNOWN"})
        self.assertEqual(set(statuses(self.db).values()), {"ok"})

    def test_6_the_rows_of_one_pair_in_both_readers(self):
        rows = build.load_listing_sources(self.conn)
        self.assertEqual(rows[("WRONGPK", "DUPREC")], (("PROD.POLICY.COPYLIB", "SHARED", True),))     # dated by recover
        self.assertEqual(rows[("FROMPGM", "DUPREC")], (("PROD.POLICY.COPYLIB", None, True),))
        self.assertEqual(build.current_datasets(rows[("WRONGPK", "DUPREC")], "CLAIMS", set()), ["PROD.POLICY.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("TWINPGM", "DUPREC")], "GC", {"GC-TEST"}), [])
        self.assertEqual(recover.program_systems(self.conn, ["twinpgm", "WRONGPK"]),
                         {"TWINPGM": {"GC", "GC-TEST"}, "WRONGPK": {"CLAIMS"}})


FEEREC_CLAIMS = ["           05  FEE-AMT-A     PIC X(4)."]
FEEREC_POLICY = ["           05  FEE-AMT-B     PIC X(8)."]


class EveryListingOfAOneOfAKindProgram(unittest.TestCase):
    """The verifier's next round (LESSONS 196). While no other system holds a
    program of its name, every listing of that name is the program's own,
    wherever it is filed, and a current one decides over an older one - for
    every copybook no current listing in the program's own system names
    (LESSONS 197: ItsOwnCurrentListingDecides); the rule is decided per
    copybook, so the build (per copybook) and recover and `program` (per
    program, grouped by copybook) read the same rows.

    - MIXPGM copies DUPREC and FEEREC. Its listing in CLAIMS's own listing
      folder is an older compile, from before COPY FEEREC was added, so it
      names DUPREC only; the current one, filed under SHARED, names FEEREC
      -> PROD.POLICY.COPYLIB, whose text differs. The build expanded
      POLICY's FEEREC (its rows are keyed per copybook: the SHARED row
      alone) while recover took the CLAIMS listing for the whole program and
      said 'the listing's table has no row for FEEREC', and `program` showed
      no FEEREC line.
    - HIDEPGM: an older listing in CLAIMS names CLAIMS's copy, the current
      one under SHARED names POLICY's. The own-system rule hid the current
      one: the chain's copy stayed and recover said CONFIRMED - main's rule,
      the current listing decides, lost in the merge. FROMOLD: the same with
      the current listing in a --from folder.
    - TWINOLD lives in GC and GC-TEST: GC's own listing is older, a current
      one under SHARED names GC-TEST's library. A twin: only GC's own rows
      count, so the SHARED listing decides for neither."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        cls.from_dir = os.path.join(cls.td, "listings-pulled")
        texts = {"DUPREC": DUPREC_CLAIMS, "FEEREC": FEEREC_POLICY}
        own = lambda dsn: [("DUPREC", "SYSLIB", dsn)]                       # noqa: E731
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/MIXPGM.cbl": program("MIXPGM", "DUPREC", "FEEREC"),
            "CLAIMS/PROD.CLAIMS.SRC/HIDEPGM.cbl": program("HIDEPGM", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/FROMOLD.cbl": program("FROMOLD", "DUPREC"),
            "GC/PROD.GC.SRC/TWINOLD.cbl": program("TWINOLD", "DUPREC"),
            "GC-TEST/TEST.GC.SRC/TWINOLD.cbl": program("TWINOLD", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "CLAIMS/PROD.CLAIMS.COPYLIB/FEEREC.cpy": FEEREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/FEEREC.cpy": FEEREC_POLICY[0] + "\n",
            "GC/PROD.GC.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "GC-TEST/TEST.GC.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            # MIXPGM: the older compile (no COPY FEEREC yet) in its own folder, the current one under SHARED
            "CLAIMS/PROD.CLAIMS.LISTING/MIXPGM.lst": listing2("MIXPGM", ["DUPREC"], texts, own("PROD.CLAIMS.COPYLIB")),
            "SHARED/PROD.LISTINGS/MIXPGM.lst": listing2("MIXPGM", ["DUPREC", "FEEREC"], texts,
                                                        [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB"),
                                                         ("FEEREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/HIDEPGM.lst": older_listing("HIDEPGM", ["DUPREC"], own("PROD.CLAIMS.COPYLIB")),
            "SHARED/PROD.LISTINGS/HIDEPGM.lst": listing("HIDEPGM", ["DUPREC"], own("PROD.POLICY.COPYLIB")),
            "CLAIMS/PROD.CLAIMS.LISTING/FROMOLD.lst": older_listing("FROMOLD", ["DUPREC"], own("PROD.CLAIMS.COPYLIB")),
            "GC/PROD.GC.LISTING/TWINOLD.lst": older_listing("TWINOLD", ["DUPREC"], own("PROD.GC.COPYLIB")),
            "SHARED/PROD.LISTINGS/TWINOLD.lst": listing("TWINOLD", ["DUPREC"], own("TEST.GC.COPYLIB")),
        })
        write_estate(cls.from_dir, {"FROMOLD.lst": listing("FROMOLD", ["DUPREC"], own("PROD.POLICY.COPYLIB"))})
        cls.claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        cls.policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")
        cls.fee_claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "FEEREC.cpy")
        cls.fee_policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "FEEREC.cpy")
        cls.gc = os.path.join(cls.root, "GC", "PROD.GC.COPYLIB", "DUPREC.cpy")
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        build_it(cls.root, cls.db, rebuild=True)
        cls.first_said = []
        cls.first_stats = recover.run(cls.db, [cls.from_dir], log=cls.first_said.append, report=cls.report)
        cls.after_first = statuses(cls.db)
        with open(cls.report, encoding="utf-8") as fh:
            cls.first_report = fh.read()
        conn = query.connect(cls.db)
        try:
            cls.first_checks = recover.check_choices(conn)
            cls.first_program = {n: query.cmd_program(conn, n) for n in ("MIXPGM", "HIDEPGM")}
        finally:
            conn.close()
        build_it(cls.root, cls.db)                                        # incremental: the marked programs parsed again
        cls.said = []
        cls.stats = recover.run(cls.db, [cls.from_dir], log=cls.said.append, report=cls.report)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def copy_of(self, name, copybook):
        """(the 'ambiguous_copybook' note naming the copybook, the path its COPY resolved to) of the CLAIMS program."""
        mid = member_id(self.conn, "CLAIMS", name)
        note = next(d for (d,) in self.conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND "
                                                    "kind='ambiguous_copybook'", (mid,)) if f" of {copybook} " in d)
        row = self.conn.execute("SELECT r.path FROM copy_use u JOIN member r ON r.id = u.resolved_member_id "
                                "WHERE u.member_id=? AND UPPER(u.copybook)=?", (mid, copybook)).fetchone()
        return note, row[0]

    def test_1_the_listings_are_dated_against_the_one_program_of_the_name(self):
        rows = build.load_listing_sources(self.conn)
        self.assertEqual(rows[("MIXPGM", "DUPREC")], (("PROD.CLAIMS.COPYLIB", "CLAIMS", False),
                                                     ("PROD.CLAIMS.COPYLIB", "SHARED", True)))
        self.assertEqual(rows[("MIXPGM", "FEEREC")], (("PROD.POLICY.COPYLIB", "SHARED", True),))
        self.assertEqual(rows[("HIDEPGM", "DUPREC")], (("PROD.CLAIMS.COPYLIB", "CLAIMS", False),
                                                      ("PROD.POLICY.COPYLIB", "SHARED", True)))
        self.assertEqual(set(rows[("FROMOLD", "DUPREC")]), {("PROD.CLAIMS.COPYLIB", "CLAIMS", False),
                                                           ("PROD.POLICY.COPYLIB", None, True)})

    def test_2_recover_contradicted_the_three_and_marked_them(self):
        text = "\n".join(self.first_said)
        self.assertEqual((self.first_stats["checked"], self.first_stats["marked"]), ((2, 3, 0, 0, 1), 3), text)
        self.assertIn("copybook choices checked against the listings: 2 confirmed, 3 contradicted by a current listing, 0 named "
                      "by an older listing, 0 name a library the index does not hold, 1 unknown", text)
        self.assertIn("  3 programs whose current listing names a held copy with different text are marked for the next build - "
                      "it follows the listing", text)
        self.assertEqual({k: v for k, v in self.after_first.items() if k[0] == "CLAIMS"},
                         {("CLAIMS", "MIXPGM"): "pending", ("CLAIMS", "HIDEPGM"): "pending", ("CLAIMS", "FROMOLD"): "pending"})
        by = {(v["program"], v["copybook"], v["system"]): v for v in self.first_checks}
        v = by[("MIXPGM", "FEEREC", "CLAIMS")]                            # it read 'the listing's table has no row for FEEREC'
        self.assertEqual((v["verdict"], v["named"], v["current"]), ("CONTRADICTED", "PROD.POLICY.COPYLIB", True))
        self.assertEqual(by[("MIXPGM", "DUPREC", "CLAIMS")]["verdict"], "CONFIRMED")
        v = by[("HIDEPGM", "DUPREC", "CLAIMS")]                           # it read CONFIRMED: the older own listing hid it
        self.assertEqual((v["verdict"], v["named"], v["current"]), ("CONTRADICTED", "PROD.POLICY.COPYLIB", True))
        self.assertEqual(v["listing_datasets"], ["PROD.CLAIMS.COPYLIB", "PROD.POLICY.COPYLIB"])
        v = by[("FROMOLD", "DUPREC", "CLAIMS")]
        self.assertEqual((v["verdict"], v["named"], v["current"]), ("CONTRADICTED", "PROD.POLICY.COPYLIB", True))
        self.assertIn("| HIDEPGM | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.CLAIMS.COPYLIB, PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at "
                      "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build chose the other | yes | CONTRADICTED - marked for the "
                      "next build | a current listing names PROD.POLICY.COPYLIB, whose text differs from the copy the build "
                      "used |", self.first_report)
        self.assertIn("| MIXPGM | FEEREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/FEEREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/FEEREC.cpy - "
                      "the build chose the other | yes | CONTRADICTED - marked for the next build |", self.first_report)
        # `program`, before the parse: the FEEREC line is there, and says what recover says
        out = self.first_program["MIXPGM"].split("### Listing says")[1]
        self.assertIn("- listing says: FEEREC came from PROD.POLICY.COPYLIB (SYSLIB) - CONTRADICTS the copy the build used "
                      "(PROD.CLAIMS.COPYLIB; the index holds that copy at POLICY/PROD.POLICY.COPYLIB/FEEREC.cpy - the build "
                      "chose the other; a current listing: its source matches this program as indexed) - see work/recover.md", out)
        self.assertIn("- listing says: DUPREC came from PROD.CLAIMS.COPYLIB (SYSLIB) - confirms the copy the build used", out)
        out = self.first_program["HIDEPGM"].split("### Listing says")[1]
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - CONTRADICTS the copy the build used", out)

    def test_3_the_next_build_takes_the_current_listings_copy_and_recover_agrees(self):
        note, used = self.copy_of("MIXPGM", "FEEREC")
        self.assertEqual(used, self.fee_policy)
        self.assertEqual(note, f"2 copies of FEEREC with different content; used {self.fee_policy} ({LISTING}PROD.POLICY.COPYLIB)")
        note, used = self.copy_of("MIXPGM", "DUPREC")                     # the current listing names the chain's own copy
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith(f"({LISTING}PROD.CLAIMS.COPYLIB)"), note)
        for name in ("HIDEPGM", "FROMOLD"):
            with self.subTest(program=name):
                note, used = self.copy_of(name, "DUPREC")
                self.assertEqual(used, self.policy)
                self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
                self.assertEqual(expanded_from(self.conn, name), {self.policy})
        text = "\n".join(self.said)
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((5, 0, 0, 0, 1), 0), text)
        self.assertNotIn("marked for the next build", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        by = {(v["program"], v["copybook"], v["system"]): v for v in recover.check_choices(self.conn)}
        self.assertEqual(by[("MIXPGM", "FEEREC", "CLAIMS")]["verdict"], "CONFIRMED")
        self.assertEqual((by[("HIDEPGM", "DUPREC", "CLAIMS")]["verdict"], by[("HIDEPGM", "DUPREC", "CLAIMS")]["why"]),
                         ("CONFIRMED", "a current listing names the dataset the build used; another listing names "
                                       "PROD.CLAIMS.COPYLIB and is not the current compile's"))
        out = query.cmd_program(self.conn, "MIXPGM").split("### Listing says")[1]
        self.assertIn("- listing says: DUPREC came from PROD.CLAIMS.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
        self.assertIn("- listing says: FEEREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
        out = query.cmd_program(self.conn, "HIDEPGM").split("### Listing says")[1]
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
        self.assertIn("- listing says: DUPREC came from PROD.CLAIMS.COPYLIB (SYSLIB) - the build used PROD.POLICY.COPYLIB: a "
                      "current listing names the dataset the build used; another listing names PROD.CLAIMS.COPYLIB and is not "
                      "the current compile's", out)

    def test_4_a_twin_takes_only_its_own_systems_listing_older_or_not(self):
        gc, gt = member_id(self.conn, "GC", "TWINOLD"), member_id(self.conn, "GC-TEST", "TWINOLD")
        note, used, status = pick_by_id(self.conn, gc)
        self.assertEqual((used, status), (self.gc, "ok"))                 # the current SHARED listing decides nothing
        self.assertTrue(note.endswith("(same system)"), note)
        by = {(v["program"], v["system"]): v for v in recover.check_choices(self.conn)}
        self.assertEqual(by[("TWINOLD", "GC")]["listing_datasets"], ["PROD.GC.COPYLIB"])
        self.assertEqual(by[("TWINOLD", "GC-TEST")]["why"],
                         "the listing read is GC's, and GC holds a TWINOLD of its own; the listing filed under SHARED cannot "
                         "say which TWINOLD it is while GC holds one too: no listing of the program in GC-TEST - next: put "
                         r"GC-TEST's own listing of TWINOLD in a folder under estate\GC-TEST, run the build, then run recover "
                         "again")
        self.assertNotIn("TEST.GC.COPYLIB", query._listing_says(self.conn, gc))
        self.assertEqual(query._listing_says(self.conn, gt), "")

    def test_5_the_same_rows_per_copybook_and_per_program(self):
        # what the build reads per (program, copybook) is exactly what recover and `program` read per program, then
        # per copybook - for every key in the index, both shapes of the index's twins
        loaded = build.load_listing_sources(self.conn)
        stored = recover.stored_copy_sources(self.conn)
        systems = recover.listing_systems(self.conn, stored)
        held = recover.program_systems(self.conn)
        for (program_, copybook), rows in loaded.items():
            for system in sorted(held.get(program_, set()), key=str):
                twins = held[program_] - {system}
                with self.subTest(program=program_, copybook=copybook, system=system):
                    per_program = recover.rows_of_system(stored[program_], system, systems, twins)
                    self.assertEqual(build.current_datasets(rows, system, twins),
                                     list(dict.fromkeys(r[2] for r in per_program if r[0] == copybook and r[4])))
        # the verifier's function-level case: an older own listing naming RATEREC, a current SHARED one naming both
        rows = [("RATEREC", "SYSLIB", "PROD.CLAIMS.COPYLIB", "own.lst", False, 95),
                ("RATEREC", "SYSLIB", "PROD.CLAIMS.COPYLIB", "shared.lst", True, 100),
                ("FEEREC", "SYSLIB", "PROD.POLICY.COPYLIB", "shared.lst", True, 100)]
        where = {"own.lst": "CLAIMS", "shared.lst": "SHARED"}
        fee = [(r[2], where[r[3]], r[4]) for r in rows if r[0] == "FEEREC"]
        self.assertEqual(build.current_datasets(fee, "CLAIMS", set()), ["PROD.POLICY.COPYLIB"])
        self.assertEqual([r[2] for r in recover.rows_of_system(rows, "CLAIMS", where, set()) if r[0] == "FEEREC"],
                         ["PROD.POLICY.COPYLIB"])
        self.assertEqual(build.current_datasets(fee, "CLAIMS", {"POLICY"}), [])       # a twin: the SHARED row counts for neither
        self.assertEqual([r[2] for r in recover.rows_of_system(rows, "CLAIMS", where, {"POLICY"}) if r[0] == "FEEREC"], [])

    def test_6_a_twins_own_listing_without_the_copybooks_row(self):
        # not_counted_why: what GC's own listing lacks decides the next step - never 'put the listing there' when it is
        # there (the verifier's E2: a current own listing, asked for again)
        theirs = [("FEEREC", "SYSLIB", "TEST.GC.COPYLIB", "gt.lst", True, 100)]
        where = {"gt.lst": "GC-TEST", "gc.lst": "GC"}
        head = "the listing read is GC-TEST's, and GC-TEST holds a TWINFEE of its own: "

        def why(own):
            return recover.not_counted_why("TWINFEE", "GC", theirs, where, {"GC-TEST"}, "FEEREC", own)

        self.assertEqual(why([("RATEREC", "SYSLIB", "PROD.GC.COPYLIB", "gc.lst", True, 100)]),
                         head + "GC's own listing of TWINFEE is current and its table has no row for FEEREC, so no listing "
                                "of this TWINFEE names the library FEEREC came from - the copy the build chose stands; "
                                "nothing to do")
        self.assertEqual(why([("RATEREC", "SYSLIB", "PROD.GC.COPYLIB", "gc.lst", False, 90)]),
                         head + "GC's own listing of TWINFEE is an older compile's and its table has no row for FEEREC - "
                                r"next: put GC's current listing of TWINFEE in a folder under estate\GC, run the build, then "
                                "run recover again")
        self.assertEqual(why([("RATEREC", "SYSLIB", "PROD.GC.COPYLIB", "gc.lst", None, None)]),
                         head + "GC's own listing of TWINFEE has no row for FEEREC in its table and is not yet dated - next: "
                                "run recover again: it dates the listing, and says whether a current one has the row")
        self.assertEqual(why([("", "", "", "gc.lst", None, None)]),
                         head + "GC's own listing of TWINFEE has no copybook-source table (an older compiler's listing prints "
                                "none), so it cannot say which library FEEREC came from - the copy the build chose stands; "
                                "next: only a listing of TWINFEE from a compiler that prints the table can decide - if there "
                                r"is one, put it in a folder under estate\GC, run the build, then run recover again")
        self.assertEqual(why([]), head + r"no listing of the program in GC - next: put GC's own listing of TWINFEE in a folder "
                                         r"under estate\GC, run the build, then run recover again")
        # one current own listing is enough: an older one beside it does not make the step 'put the current one there'
        self.assertIn("is current and its table has no row for FEEREC",
                      why([("RATEREC", "SYSLIB", "PROD.GC.COPYLIB", "gc.lst", True, 100),
                           ("RATEREC", "SYSLIB", "PROD.GC.COPYLIB", "gc2.lst", False, 80)]))
        # nothing else read: the reason starts with what its own listing lacks
        self.assertTrue(recover.not_counted_why("TWINFEE", "GC", [], where, {"GC-TEST"}, "FEEREC", [])
                        .startswith("no listing of the program in GC - next:"))


class ContradictedChoicesMarkedForTheNextBuild(unittest.TestCase):
    """The night's order reversed: the build ran, then the listings were read
    (his routine fetches them later). WRONGPK's current listing names
    POLICY's copy, which the index holds with a different text: CONTRADICTED;
    FETCHIT's a library the index does not hold: NOT HELD, the copy used
    stands; CHOSEN's agrees. On an index this toolkit built, WRONGPK is
    marked and the next build gives it the listing's copy; on one another
    toolkit built nothing is marked - that build re-parses every member
    anyway. Rows not yet dated contradict, but mark nothing: the build
    follows a current listing only."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/CHOSEN.cbl": program("CHOSEN", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/WRONGPK.cbl": program("WRONGPK", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/FETCHIT.cbl": program("FETCHIT", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "CLAIMS/PROD.CLAIMS.LISTING/CHOSEN.lst": listing("CHOSEN", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.CLAIMS.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/WRONGPK.lst": listing("WRONGPK", ["DUPREC"], [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            "CLAIMS/PROD.CLAIMS.LISTING/FETCHIT.lst": listing("FETCHIT", ["DUPREC"], [("DUPREC", "COPYLIB", "PROD.SHARED.COPYLIB")]),
        })
        cls.built = os.path.join(cls.td, "built.db")
        build_it(cls.root, cls.built, rebuild=True)
        cls.claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        cls.policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.case = tempfile.mkdtemp(dir=self.td)
        self.db = os.path.join(self.case, "t.db")
        shutil.copy(self.built, self.db)
        self.report = os.path.join(self.case, "work", "recover.md")

    def run_it(self, **kw):
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report, **kw)
        return stats, "\n".join(said)

    def section(self):
        with open(self.report, encoding="utf-8") as fh:
            return fh.read().split("## Copybook choices, checked against the listings")[1]

    def test_1_marked_then_parsed_again_with_the_listings_copy(self):
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 1, 0, 1, 0), 1), text)
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 1 contradicted by a current listing, 0 named "
                      "by an older listing, 1 name a library the index does not hold, 0 unknown - every choice contradicted by "
                      "a current listing is a wrong fact in the index", text)
        self.assertIn("  1 program whose current listing names a held copy with different text is marked for the next build - it "
                      "follows the listing", text)
        self.assertIn("next: run your usual build command - the programs marked re-expand by themselves", text)
        self.assertEqual(statuses(self.db), {("CLAIMS", "CHOSEN"): "ok", ("CLAIMS", "WRONGPK"): "pending", ("CLAIMS", "FETCHIT"): "ok"})
        sec = self.section()
        self.assertIn("- 1 of the contradicted is named by a current listing - the 1 program is marked for the next build, which "
                      "follows that listing: run your usual build command", sec)
        self.assertIn("| WRONGPK | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | yes | CONTRADICTED - marked for the next build | a current listing names "
                      "PROD.POLICY.COPYLIB, whose text differs from the copy the build used |", sec)
        self.assertIn("| FETCHIT | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      f"PROD.SHARED.COPYLIB (COPYLIB) - not a library the index holds | yes | NOT HELD | {recover.NOT_HELD_WHY} |",
                      sec)
        # the next build, incremental: WRONGPK alone is parsed again and takes the listing's copy
        build_it(self.root, self.db)
        conn = query.connect(self.db)
        try:
            note, used = pick_of(conn, "WRONGPK")
            self.assertEqual(used, self.policy)
            self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
            self.assertEqual(expanded_from(conn, "WRONGPK"), {self.policy})
            # CHOSEN's listing names the copy the chain had picked: parsed again (build.picks_moved), its note says
            # what a full parse says - before LESSONS 249 it kept '(same system)', so the index hung on the order the
            # listings arrived in
            note, used = pick_of(conn, "CHOSEN")
            self.assertEqual(used, self.claims)
            self.assertTrue(note.endswith(f"({LISTING}PROD.CLAIMS.COPYLIB)"), note)
        finally:
            conn.close()
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((2, 0, 0, 1, 0), 0), text)
        self.assertNotIn("marked for the next build", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        self.assertIn("| FETCHIT | DUPREC |", self.section())
        self.assertNotIn("| WRONGPK |", self.section())

    def test_2_a_dry_run_says_would_be_marked_and_marks_nothing(self):
        stats, text = self.run_it(dry_run=True)
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 1, 0, 1, 0), 0), text)
        self.assertIn("  1 program whose current listing names a held copy with different text would be marked for the next "
                      "build (dry run: nothing changed)", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        self.assertIn("- 1 of the contradicted is named by a current listing - the 1 program would be marked for the next build "
                      "(dry run: nothing changed)", self.section())
        self.assertIn("| CONTRADICTED - would be marked for the next build (dry run) |", self.section())

    def test_3_an_index_another_toolkit_built_is_left_alone(self):
        set_fingerprint(self.db, "0123456789abcdef")
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 1, 0, 1, 0), 0), text)
        self.assertIn("  1 program whose current listing names a held copy with different text: the next build re-parses every "
                      "member (the toolkit changed since the index was built) and reads the listing first", text)
        self.assertNotIn("marked for the next build", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        sec = self.section()
        self.assertIn("- 1 of the contradicted is named by a current listing - the next build re-parses every member (the "
                      "toolkit changed since the index was built) and reads the listing first", sec)
        self.assertIn("| CONTRADICTED - re-parsed by the next build (toolkit changed) |", sec)
        # and it does: every member parsed again, the rows in place, WRONGPK takes the listing's copy
        build_it(self.root, self.db)
        conn = query.connect(self.db)
        try:
            note, used = pick_of(conn, "WRONGPK")
            self.assertEqual(used, self.policy)
            self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
        finally:
            conn.close()

    def test_4_no_listing_in_the_index_the_stored_rows_still_mark(self):
        # the rows stored on an earlier run (or given with --from then), the listing members gone from the index
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM member WHERE kind='listing'")
            recover.store_copy_sources(conn, {"WRONGPK": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "WRONGPK.lst", True, 100)]})
            conn.commit()
        finally:
            conn.close()
        stats, text = self.run_it()
        self.assertIn("expanded texts to read: 0 - the index holds no compiler listings and no --from folder was given", text)
        self.assertEqual((stats["checked"], stats["marked"]), ((0, 1, 0, 0, 2), 1), text)
        self.assertIn("  1 program whose current listing names a held copy with different text is marked for the next build - it "
                      "follows the listing", text)
        self.assertIn("next: run your usual build command - the programs marked re-expand by themselves", text)
        self.assertEqual(statuses(self.db)[("CLAIMS", "WRONGPK")], "pending")

    def test_6_a_listing_not_yet_dated_marks_nothing_and_decides_nothing(self):
        # rows stored before recover dated listings (the table of an earlier run), the listing members gone from the index
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM member WHERE kind='listing'")
            recover.store_copy_sources(conn, {"WRONGPK": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "WRONGPK.lst", None, None)]})
            conn.commit()
        finally:
            conn.close()
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((0, 1, 0, 0, 2), 0), text)
        # never counted as contradicted by a CURRENT listing (the verifier: the count line said so, the line under it not)
        self.assertIn("copybook choices checked against the listings: 0 confirmed, 0 contradicted by a current listing, 1 "
                      "contradicted by a listing not yet dated, 0 named by an older listing, 0 name a library the index does "
                      "not hold, 2 unknown", text)
        self.assertIn("  1 program contradicted by a listing not yet dated: not marked - the build follows a current listing only: "
                      "run this tool again with --from FOLDER (the folder the listings came from) so it dates them", text)
        self.assertNotIn("marked for the next build -", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        conn = query.connect(self.db)                                    # no listing read: the report section, as it would read
        try:
            checks = recover.check_choices(conn)
        finally:
            conn.close()
        self.assertEqual(recover.mark_contradicted(self.db, checks, dry_run=False), (0, False))
        sec = "".join(recover.choice_report(checks, self.root))
        self.assertIn("- 3 choices checked: 0 confirmed, 0 contradicted by a current listing, 1 contradicted by a listing not "
                      "yet dated, 0 named by an older listing, 0 name a library the index does not hold, 2 unknown", sec)
        self.assertIn("- 1 of the contradicted is named by a listing not yet dated - not marked - the build follows a current "
                      "listing only", sec)
        self.assertIn("_no choice contradicted by a current listing_", sec)
        self.assertNotIn("named by a current listing -", sec)
        self.assertIn("| not dated | CONTRADICTED | the listing names PROD.POLICY.COPYLIB, whose text differs from the copy the "
                      "build used; " + recover.NOT_YET_DATED + " |", sec)
        stats, text = self.run_it()                                       # it settles: a second run marks nothing either
        self.assertEqual(stats["marked"], 0, text)
        # parsed again all the same (a full re-parse): the build does not follow a listing not yet dated
        recover._mark_programs(self.db, ["DUPREC"])
        build_it(self.root, self.db)
        conn = query.connect(self.db)
        try:
            note, used = pick_of(conn, "WRONGPK")
            self.assertEqual(used, self.claims)
            self.assertTrue(note.endswith("(same system)"), note)
        finally:
            conn.close()

    def test_7_current_and_undated_counted_apart_in_every_output(self):
        # the verifier's run: rows a pre-batch recover stored (not dated) beside rows this run reads and dates. WRONGPK's
        # listing is read and current; CHOSEN's listing member is gone from the index and its stored row, naming POLICY's
        # copy, was never dated. Every output said '2 contradicted by a current listing' where one of them is not dated
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM member WHERE kind='listing' AND name='CHOSEN'")
            recover.store_copy_sources(conn, {"CHOSEN": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "CHOSEN.lst", None, None)]})
            conn.commit()
        finally:
            conn.close()
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((0, 2, 0, 1, 0), 1), text)
        self.assertIn("copybook choices checked against the listings: 0 confirmed, 1 contradicted by a current listing, 1 "
                      "contradicted by a listing not yet dated, 0 named by an older listing, 1 name a library the index does "
                      "not hold, 0 unknown - every choice contradicted by a current listing is a wrong fact in the index", text)
        self.assertIn("  1 program whose current listing names a held copy with different text is marked for the next build - it "
                      "follows the listing", text)
        self.assertIn("  1 program contradicted by a listing not yet dated: " + recover.UNDATED_NEXT, text)
        self.assertEqual(statuses(self.db), {("CLAIMS", "CHOSEN"): "ok", ("CLAIMS", "WRONGPK"): "pending", ("CLAIMS", "FETCHIT"): "ok"})
        sec = self.section()
        self.assertIn("- 3 choices checked: 0 confirmed, 1 contradicted by a current listing, 1 contradicted by a listing not yet "
                      "dated, 0 named by an older listing, 1 name a library the index does not hold, 0 unknown\n", sec)
        self.assertIn("- 1 of the contradicted is named by a current listing - the 1 program is marked for the next build", sec)
        self.assertIn("- 1 of the contradicted is named by a listing not yet dated - " + recover.UNDATED_NEXT, sec)
        self.assertNotIn("_no choice contradicted by a current listing_", sec)
        conn = query.connect(self.db)
        try:
            self.assertIn("0 of these choices are confirmed by the program's listing, 1 contradicted by a current listing, 1 "
                          "contradicted by a listing not yet dated (see work/recover.md), 0 named by an older listing, 1 name a "
                          "library the index does not hold, 0 unknown", query.cmd_coverage(conn))
            self.assertIn("CONTRADICTS the copy the build used (PROD.CLAIMS.COPYLIB; the index holds that copy at "
                          "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build chose the other; " + recover.NOT_YET_DATED + ")",
                          query.cmd_program(conn, "CHOSEN"))
            self.assertIn("CONTRADICTS the copy the build used (PROD.CLAIMS.COPYLIB; the index holds that copy at "
                          "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - the build chose the other; a current listing: its source "
                          "matches this program as indexed)", query.cmd_program(conn, "WRONGPK"))
            self.assertEqual(recover.contradicted_split(recover.check_choices(conn)), (1, 1))
        finally:
            conn.close()

    def test_5_coverage_says_who_decided_and_shows_the_word_whole(self):
        self.run_it()
        build_it(self.root, self.db)
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        chosen = cov.split("### Complete, with a copybook chosen among several")[1].split("\n###")[0]
        # CHOSEN is parsed again by the build that follows recover (build.picks_moved): its listing names the copy the
        # chain had picked, and the note says so as a full parse does (LESSONS 249)
        self.assertTrue(chosen.startswith(": 3 members - 2 choices decided by the program's compiler listing, 1 picked by system "
                                          "and library order; the 'ambiguous_copybook' rows name the copy used"), chosen[:160])
        self.assertIn("| cobol | WRONGPK | PROD.CLAIMS.SRC | DUPREC: 2 copies, used POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy "
                      "(listing: PROD.POLICY.COPYLIB) |", chosen)
        self.assertIn("| cobol | CHOSEN | PROD.CLAIMS.SRC | DUPREC: 2 copies, used CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy "
                      "(listing: PROD.CLAIMS.COPYLIB) |", chosen)
        self.assertIn("| cobol | FETCHIT | PROD.CLAIMS.SRC | DUPREC: 2 copies, used CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy "
                      "(same system) |", chosen)
        self.assertIn("2 of these choices are confirmed by the program's listing, 0 contradicted by a current listing (see "
                      "work/recover.md), 0 named by an older listing, 1 name a library the index does not hold, 0 unknown", chosen)
        self.assertIn("2 of the choices are the compiler's own: the program's listing names the library the copybook was read from, "
                      "and that copy was expanded (`listing: DATASET` in the table) - nothing to declare for those. The other 1 "
                      "follows `COPY ... OF`, then the member's own system in its declared copybook order, then the manifest's "
                      "authoritative copy, and one of those is wrong only where the manifest's system or copybook order is; "
                      "1 copybook name(s) are involved - `ambiguous` lists them per department.", chosen)
        kinds = cov.split("### Unresolved by kind")[1].split("\n###")[0]
        self.assertIn("| ambiguous_copybook (decided by the listing) | 2 |", kinds)
        self.assertIn("| ambiguous_copybook | 1 | two copies of one copybook with different content; one was chosen | declare the "
                      "department's copybook order (manifest `copylib_order`) or remove the stale copy |", kinds)
        self.assertNotIn("| expand (copybook chosen among several", kinds)     # no COPY warning repeats the choice (item 18)


class ItsOwnCurrentListingDecides(unittest.TestCase):
    """The verifier's next round (LESSONS 197): the per-row rule of row 196
    let a listing filed outside the program's system override the program's
    own CURRENT listing. The rule, per copybook: a current listing in the
    program's own system decides every copybook it names; a listing filed
    elsewhere counts only for a copybook no current listing of its own names.

    - VNHDIFF (CLAIMS, no twin): its own current listing names
      STG.NOTHELD.COPYLIB, a staging library the index does not hold - the
      owner's usual case; a second current listing under SHARED names
      PROD.POLICY.COPYLIB, held with a different text. It read CONTRADICTED,
      was marked, and the next build expanded POLICY's copy; now NOT HELD,
      and CLAIMS's copy stays, parsed again or not. VFROMNH: the same with
      the other listing in a --from folder.
    - VREVERSE: its own current listing names POLICY's copy, the one under
      SHARED names CLAIMS's. It read CONFIRMED against its own listing; now
      CONTRADICTED, marked, and the next build expands POLICY's copy.
    - VSPLIT: its own current listing names DUPREC only (-> the staging
      library); the one under SHARED names DUPREC and FEEREC -> POLICY's.
      DUPREC is its own listing's word (NOT HELD), FEEREC the SHARED one's
      (contradicted, then POLICY's FEEREC) - in the build, in recover and in
      `program` alike.
    - VOWNSHO: its own current listing names CLAIMS's copy, an older one
      under SHARED names POLICY's: CONFIRMED, nothing marked."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        cls.from_dir = os.path.join(cls.td, "listings-pulled")
        texts = {"DUPREC": DUPREC_CLAIMS, "FEEREC": FEEREC_POLICY}
        one = lambda dsn: [("DUPREC", "SYSLIB", dsn)]                       # noqa: E731
        own, shared = "CLAIMS/PROD.CLAIMS.LISTING", "SHARED/PROD.LISTINGS"
        write_estate(cls.root, {
            "CLAIMS/PROD.CLAIMS.SRC/VNHDIFF.cbl": program("VNHDIFF", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/VFROMNH.cbl": program("VFROMNH", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/VREVERSE.cbl": program("VREVERSE", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.SRC/VSPLIT.cbl": program("VSPLIT", "DUPREC", "FEEREC"),
            "CLAIMS/PROD.CLAIMS.SRC/VOWNSHO.cbl": program("VOWNSHO", "DUPREC"),
            "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "CLAIMS/PROD.CLAIMS.COPYLIB/FEEREC.cpy": FEEREC_CLAIMS[0] + "\n",
            "POLICY/PROD.POLICY.COPYLIB/FEEREC.cpy": FEEREC_POLICY[0] + "\n",
            f"{own}/VNHDIFF.lst": listing("VNHDIFF", ["DUPREC"], one("STG.NOTHELD.COPYLIB")),
            f"{shared}/VNHDIFF.lst": listing("VNHDIFF", ["DUPREC"], one("PROD.POLICY.COPYLIB")),
            f"{own}/VFROMNH.lst": listing("VFROMNH", ["DUPREC"], one("STG.NOTHELD.COPYLIB")),
            f"{own}/VREVERSE.lst": listing("VREVERSE", ["DUPREC"], one("PROD.POLICY.COPYLIB")),
            f"{shared}/VREVERSE.lst": listing("VREVERSE", ["DUPREC"], one("PROD.CLAIMS.COPYLIB")),
            f"{own}/VSPLIT.lst": listing2("VSPLIT", ["DUPREC", "FEEREC"], texts, one("STG.NOTHELD.COPYLIB")),
            f"{shared}/VSPLIT.lst": listing2("VSPLIT", ["DUPREC", "FEEREC"], texts,
                                             [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB"),
                                              ("FEEREC", "SYSLIB", "PROD.POLICY.COPYLIB")]),
            f"{own}/VOWNSHO.lst": listing("VOWNSHO", ["DUPREC"], one("PROD.CLAIMS.COPYLIB")),
            f"{shared}/VOWNSHO.lst": older_listing("VOWNSHO", ["DUPREC"], one("PROD.POLICY.COPYLIB")),
        })
        write_estate(cls.from_dir, {"VFROMNH.lst": listing("VFROMNH", ["DUPREC"], one("PROD.POLICY.COPYLIB"))})
        cls.claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        cls.policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "DUPREC.cpy")
        cls.fee_claims = os.path.join(cls.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "FEEREC.cpy")
        cls.fee_policy = os.path.join(cls.root, "POLICY", "PROD.POLICY.COPYLIB", "FEEREC.cpy")
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        build_it(cls.root, cls.db, rebuild=True)
        cls.first_said = []
        cls.first_stats = recover.run(cls.db, [cls.from_dir], log=cls.first_said.append, report=cls.report)
        cls.after_first = statuses(cls.db)
        with open(cls.report, encoding="utf-8") as fh:
            cls.first_report = fh.read()
        conn = query.connect(cls.db)
        try:
            cls.first_checks = recover.check_choices(conn)
        finally:
            conn.close()
        recover._mark_programs(cls.db, ["DUPREC"])                       # the re-parse night: every program, the rows in place
        build_it(cls.root, cls.db)
        cls.said = []
        cls.stats = recover.run(cls.db, [cls.from_dir], log=cls.said.append, report=cls.report)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def copy_of(self, name, copybook):
        mid = member_id(self.conn, "CLAIMS", name)
        note = next(d for (d,) in self.conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND "
                                                    "kind='ambiguous_copybook'", (mid,)) if f" of {copybook} " in d)
        row = self.conn.execute("SELECT r.path FROM copy_use u JOIN member r ON r.id = u.resolved_member_id "
                                "WHERE u.member_id=? AND UPPER(u.copybook)=?", (mid, copybook)).fetchone()
        return note, row[0]

    def test_1_recover_takes_its_own_current_listings_word(self):
        text = "\n".join(self.first_said)
        self.assertEqual((self.first_stats["checked"], self.first_stats["marked"]), ((1, 2, 0, 3, 0), 2), text)
        self.assertIn("  2 programs whose current listing names a held copy with different text are marked for the next build - "
                      "it follows the listing", text)
        self.assertEqual(self.after_first, {("CLAIMS", "VNHDIFF"): "ok", ("CLAIMS", "VFROMNH"): "ok",
                                            ("CLAIMS", "VREVERSE"): "pending", ("CLAIMS", "VSPLIT"): "pending",
                                            ("CLAIMS", "VOWNSHO"): "ok"})
        by = {(v["program"], v["copybook"]): v for v in self.first_checks}
        for name in ("VNHDIFF", "VFROMNH"):                               # it read CONTRADICTED and was marked
            with self.subTest(program=name):
                v = by[(name, "DUPREC")]
                self.assertEqual((v["verdict"], v["named"], v["listing_datasets"], v["marked"]),
                                 ("NOT HELD", "STG.NOTHELD.COPYLIB", ["STG.NOTHELD.COPYLIB"], ""))
                self.assertEqual(v["why"], recover.NOT_HELD_WHY)
        v = by[("VREVERSE", "DUPREC")]                                    # it read CONFIRMED against its own listing
        self.assertEqual((v["verdict"], v["named"], v["current"], v["listing_datasets"]),
                         ("CONTRADICTED", "PROD.POLICY.COPYLIB", True, ["PROD.POLICY.COPYLIB"]))
        self.assertEqual((by[("VSPLIT", "DUPREC")]["verdict"], by[("VSPLIT", "DUPREC")]["named"]),
                         ("NOT HELD", "STG.NOTHELD.COPYLIB"))
        self.assertEqual((by[("VSPLIT", "FEEREC")]["verdict"], by[("VSPLIT", "FEEREC")]["named"]),
                         ("CONTRADICTED", "PROD.POLICY.COPYLIB"))
        self.assertEqual((by[("VOWNSHO", "DUPREC")]["verdict"], by[("VOWNSHO", "DUPREC")]["listing_datasets"]),
                         ("CONFIRMED", ["PROD.CLAIMS.COPYLIB"]))
        self.assertIn("| VNHDIFF | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "STG.NOTHELD.COPYLIB (SYSLIB) - not a library the index holds | yes | NOT HELD | "
                      f"{recover.NOT_HELD_WHY} |", self.first_report)
        self.assertNotIn("| VNHDIFF | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                         "PROD.POLICY.COPYLIB", self.first_report)

    def test_2_the_build_reads_the_same_rows(self):
        rows = build.load_listing_sources(self.conn)
        self.assertEqual(rows[("VNHDIFF", "DUPREC")], (("STG.NOTHELD.COPYLIB", "CLAIMS", True),
                                                      ("PROD.POLICY.COPYLIB", "SHARED", True)))
        self.assertEqual(build.current_datasets(rows[("VNHDIFF", "DUPREC")], "CLAIMS", set()), ["STG.NOTHELD.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("VFROMNH", "DUPREC")], "CLAIMS", set()), ["STG.NOTHELD.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("VREVERSE", "DUPREC")], "CLAIMS", set()), ["PROD.POLICY.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("VSPLIT", "DUPREC")], "CLAIMS", set()), ["STG.NOTHELD.COPYLIB"])
        self.assertEqual(build.current_datasets(rows[("VSPLIT", "FEEREC")], "CLAIMS", set()), ["PROD.POLICY.COPYLIB"])
        # every key of the index: what the build reads per (program, copybook) is what recover reads per program
        stored = recover.stored_copy_sources(self.conn)
        systems = recover.listing_systems(self.conn, stored)
        for (program_, copybook), rows_ in rows.items():
            with self.subTest(program=program_, copybook=copybook):
                per_program = recover.rows_of_system(stored[program_], "CLAIMS", systems, set())
                self.assertEqual(build.current_datasets(rows_, "CLAIMS", set()),
                                 list(dict.fromkeys(r[2] for r in per_program if r[0] == copybook and r[4])))

    def test_3_parsed_again_its_own_listing_decides(self):
        for name in ("VNHDIFF", "VFROMNH"):                               # it expanded POLICY's copy
            with self.subTest(program=name):
                note, used = self.copy_of(name, "DUPREC")
                self.assertEqual(used, self.claims)
                self.assertEqual(note, f"2 copies of DUPREC with different content; used {self.claims} (same system)")
                self.assertEqual(expanded_from(self.conn, name), {self.claims})
        note, used = self.copy_of("VREVERSE", "DUPREC")
        self.assertEqual(used, self.policy)
        self.assertEqual(note, f"2 copies of DUPREC with different content; used {self.policy} ({LISTING}PROD.POLICY.COPYLIB)")
        note, used = self.copy_of("VSPLIT", "DUPREC")
        self.assertEqual((used, note.endswith("(same system)")), (self.claims, True))
        note, used = self.copy_of("VSPLIT", "FEEREC")
        self.assertEqual(used, self.fee_policy)
        self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
        note, used = self.copy_of("VOWNSHO", "DUPREC")
        self.assertEqual(used, self.claims)
        self.assertTrue(note.endswith(f"({LISTING}PROD.CLAIMS.COPYLIB)"), note)

    def test_4_recover_agrees_and_marks_nothing(self):
        text = "\n".join(self.said)
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((3, 0, 0, 3, 0), 0), text)
        self.assertNotIn("marked for the next build", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        by = {(v["program"], v["copybook"]): v["verdict"] for v in recover.check_choices(self.conn)}
        self.assertEqual(by, {("VNHDIFF", "DUPREC"): "NOT HELD", ("VFROMNH", "DUPREC"): "NOT HELD",
                              ("VREVERSE", "DUPREC"): "CONFIRMED", ("VSPLIT", "DUPREC"): "NOT HELD",
                              ("VSPLIT", "FEEREC"): "CONFIRMED", ("VOWNSHO", "DUPREC"): "CONFIRMED"})
        self.assertIn("3 of these choices are confirmed by the program's listing, 0 contradicted by a current listing (see "
                      "work/recover.md), 0 named by an older listing, 3 name a library the index does not hold, 0 unknown",
                      query.cmd_coverage(self.conn))

    def test_5_program_shows_the_rows_that_speak_for_it(self):
        out = query.cmd_program(self.conn, "VNHDIFF").split("### Listing says")[1]
        self.assertIn("- listing says: DUPREC came from STG.NOTHELD.COPYLIB (SYSLIB) - names STG.NOTHELD.COPYLIB, a library "
                      "the index does not hold - the copy the build used stands\n", out)
        self.assertNotIn("PROD.POLICY.COPYLIB", out)
        out = query.cmd_program(self.conn, "VREVERSE").split("### Listing says")[1]
        self.assertIn("- listing says: DUPREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
        self.assertNotIn("PROD.CLAIMS.COPYLIB", out)
        out = query.cmd_program(self.conn, "VSPLIT").split("### Listing says")[1]
        self.assertIn("- listing says: DUPREC came from STG.NOTHELD.COPYLIB (SYSLIB) - names STG.NOTHELD.COPYLIB", out)
        self.assertIn("- listing says: FEEREC came from PROD.POLICY.COPYLIB (SYSLIB) - confirms the copy the build used\n", out)
        self.assertNotIn("DUPREC came from PROD.POLICY.COPYLIB", out)


class ATwinsOwnListing(unittest.TestCase):
    """The verifier's next round (LESSONS 197), a twin's UNKNOWN reason.
    GC and GC-TEST each hold TWF, TWC and TWO.

    - TWF: GC's own listing has no copybook-source table (an older
      compiler's), GC-TEST's has one. The rows were gathered per program
      stem and the 'no table' marker was written only when no listing of
      the name had a table, so nothing showed GC's listing was read: the
      reason said 'no listing of the program in GC - next: put GC's own
      listing of TWF in a folder under estate\\GC', where it already was.
      Now each listing read with no table is a row with no copybook and its
      path: 'GC's own listing of TWF has no copybook-source table'.
    - TWC: GC's own CURRENT listing names DUPREC only; one under SHARED names
      FEEREC -> GC-TEST's library. The reason asked for 'GC's current
      listing of TWC', which is there; now it says the current listing's
      table has no row for FEEREC and the chain's pick stands.
    - TWO: the same with GC's own listing an older compile's: then the next
      step is GC's current listing."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
        texts = {"DUPREC": DUPREC_CLAIMS, "FEEREC": FEEREC_CLAIMS}
        files = {
            "GC/PROD.GC.COPYLIB/DUPREC.cpy": DUPREC_CLAIMS[0] + "\n",
            "GC-TEST/TEST.GC.COPYLIB/DUPREC.cpy": DUPREC_POLICY[0] + "\n",
            "GC/PROD.GC.COPYLIB/FEEREC.cpy": FEEREC_CLAIMS[0] + "\n",
            "GC-TEST/TEST.GC.COPYLIB/FEEREC.cpy": FEEREC_POLICY[0] + "\n",
            "GC/PROD.GC.LISTING/TWF.lst": listing("TWF", ["DUPREC"], None),
            "GC-TEST/TEST.GC.LISTING/TWF.lst": listing("TWF", ["DUPREC"], [("DUPREC", "SYSLIB", "TEST.GC.COPYLIB")]),
            "GC/PROD.GC.LISTING/TWC.lst": listing2("TWC", ["DUPREC", "FEEREC"], texts, [("DUPREC", "SYSLIB", "PROD.GC.COPYLIB")]),
            "SHARED/PROD.LISTINGS/TWC.lst": listing2("TWC", ["DUPREC", "FEEREC"], texts,
                                                     [("DUPREC", "SYSLIB", "PROD.GC.COPYLIB"),
                                                      ("FEEREC", "SYSLIB", "TEST.GC.COPYLIB")]),
            "GC/PROD.GC.LISTING/TWO.lst": ibm_listing(program("TWO", "DUPREC", "FEEREC").replace("PIC X(5).", "PIC X(6).", 1)
                                                      .splitlines(), texts, copy_table=[("DUPREC", "SYSLIB", "PROD.GC.COPYLIB")]),
            "SHARED/PROD.LISTINGS/TWO.lst": listing2("TWO", ["DUPREC", "FEEREC"], texts,
                                                     [("FEEREC", "SYSLIB", "TEST.GC.COPYLIB")]),
        }
        for name, copybooks in (("TWF", ("DUPREC",)), ("TWC", ("DUPREC", "FEEREC")), ("TWO", ("DUPREC", "FEEREC"))):
            files[f"GC/PROD.GC.SRC/{name}.cbl"] = program(name, *copybooks)
            files[f"GC-TEST/TEST.GC.SRC/{name}.cbl"] = program(name, *copybooks)
        write_estate(cls.root, files)
        cls.gc_twf = os.path.join(cls.root, "GC", "PROD.GC.LISTING", "TWF.lst")
        cls.db = os.path.join(cls.td, "t.db")
        cls.report = os.path.join(cls.td, "work", "recover.md")
        build_it(cls.root, cls.db, rebuild=True)
        cls.dry_said = []
        cls.dry_stats = recover.run(cls.db, dry_run=True, log=cls.dry_said.append, report=cls.report)
        with open(cls.report, encoding="utf-8") as fh:
            cls.dry_report = fh.read()
        conn = query.connect(cls.db)
        try:
            cls.before_stored = recover.has_copy_sources(conn)
        finally:
            conn.close()
        cls.said = []
        cls.stats = recover.run(cls.db, log=cls.said.append, report=cls.report)
        with open(cls.report, encoding="utf-8") as fh:
            cls.section = fh.read().split("## Copybook choices, checked against the listings")[1]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    TWF_WHY = ("the listing read is GC-TEST's, and GC-TEST holds a TWF of its own: GC's own listing of TWF has no "
               "copybook-source table (an older compiler's listing prints none), so it cannot say which library DUPREC "
               "came from - the copy the build chose stands; next: only a listing of TWF from a compiler that prints the "
               r"table can decide - if there is one, put it in a folder under estate\GC, run the build, then run recover "
               "again")
    TWC_WHY = ("the listing filed under SHARED cannot say which TWC it is while GC-TEST holds one too: GC's own listing of "
               "TWC is current and its table has no row for FEEREC, so no listing of this TWC names the library FEEREC came "
               "from - the copy the build chose stands; nothing to do")
    TWO_WHY = ("the listing filed under SHARED cannot say which TWO it is while GC-TEST holds one too: GC's own listing of "
               "TWO is an older compile's and its table has no row for FEEREC - next: put GC's current listing of TWO in a "
               r"folder under estate\GC, run the build, then run recover again")

    def test_1_a_listing_read_with_no_table_is_stored_with_its_path(self):
        rows = [tuple(r) for r in self.conn.execute("SELECT copybook, dataset, listing FROM listing_copy_source "
                                                    "WHERE program='TWF' ORDER BY listing")]
        self.assertEqual(sorted(rows, key=lambda r: r[0]),
                         [("", None, self.gc_twf),
                          ("DUPREC", "TEST.GC.COPYLIB", os.path.join(self.root, "GC-TEST", "TEST.GC.LISTING", "TWF.lst"))])
        stored = recover.stored_copy_sources(self.conn, "twf")
        self.assertEqual(sorted(stored), ["TWF"])
        self.assertIn(("", "", "", self.gc_twf, None, None), stored["TWF"])
        self.assertEqual(recover.listing_systems(self.conn, stored)[self.gc_twf], "GC")
        self.assertNotIn(("TWF", ""), build.load_listing_sources(self.conn))       # the build reads no marker

    def test_2_the_twins_reasons_say_what_its_own_listing_lacks(self):
        by = {(v["system"], v["program"], v["copybook"]): v for v in recover.check_choices(self.conn)}
        self.assertEqual((by[("GC", "TWF", "DUPREC")]["verdict"], by[("GC", "TWF", "DUPREC")]["why"]), ("UNKNOWN", self.TWF_WHY))
        self.assertEqual(by[("GC-TEST", "TWF", "DUPREC")]["verdict"], "CONFIRMED")
        self.assertEqual((by[("GC", "TWC", "FEEREC")]["verdict"], by[("GC", "TWC", "FEEREC")]["why"]), ("UNKNOWN", self.TWC_WHY))
        self.assertEqual(by[("GC", "TWC", "DUPREC")]["verdict"], "CONFIRMED")
        self.assertEqual((by[("GC", "TWO", "FEEREC")]["verdict"], by[("GC", "TWO", "FEEREC")]["why"]), ("UNKNOWN", self.TWO_WHY))
        self.assertEqual((by[("GC", "TWO", "DUPREC")]["verdict"], by[("GC", "TWO", "DUPREC")]["current"]), ("CONFIRMED", False))
        self.assertEqual(by[("GC-TEST", "TWC", "FEEREC")]["why"],
                         "the listing read is GC's, and GC holds a TWC of its own; the listing filed under SHARED cannot say "
                         "which TWC it is while GC holds one too: no listing of the program in GC-TEST - next: put GC-TEST's "
                         r"own listing of TWC in a folder under estate\GC-TEST, run the build, then run recover again")
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((3, 0, 0, 0, 7), 0), "\n".join(self.said))
        self.assertIn(self.TWF_WHY, self.section)
        self.assertIn(self.TWC_WHY, self.section)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})

    def test_3_a_dry_run_gives_the_same_reasons_from_the_rows_it_read(self):
        self.assertFalse(self.before_stored)                               # nothing stored on the dry run
        self.assertEqual(self.dry_stats["checked"], self.stats["checked"], "\n".join(self.dry_said))
        self.assertIn(self.TWF_WHY, self.dry_report)
        self.assertIn(self.TWC_WHY, self.dry_report)
        self.assertNotIn(r"no listing of the program in GC - next: put GC's own listing of TWF", self.dry_report)

    def test_4_markers_written_before_they_carried_a_path_still_read_as_no_table(self):
        conn = sqlite3.connect(":memory:")
        try:
            recover.store_copy_sources(conn, {"OLDMARK": [], "NEWMARK": [("", "", "", "x.lst", None, None)]})
            conn.execute("INSERT INTO listing_copy_source(program, copybook, ddname, dataset, listing, seen) "
                         "VALUES('PRE', '', NULL, NULL, NULL, '2026-09-01 08:00:00')")    # an older toolkit's marker
            self.assertEqual([tuple(r) for r in conn.execute("SELECT program, copybook, dataset, listing FROM "
                                                             "listing_copy_source ORDER BY program")],
                             [("NEWMARK", "", None, "x.lst"), ("OLDMARK", "", None, None), ("PRE", "", None, None)])
            self.assertEqual(recover.stored_copy_sources(conn),
                             {"NEWMARK": [("", "", "", "x.lst", None, None)], "OLDMARK": [], "PRE": []})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
