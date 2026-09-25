"""
Which listing speaks for which program; a contradicted choice is marked for
the next build (ROADMAP re-parse item 19, the verifier's rounds).

atlas.recover keys the listing rows by the listing's file stem, so every
listing of one program NAME shares the key: two environments holding one
program name (GC and GC-TEST, each with its own copybook library and its own
compiler listing), a listing filed under a system of its own (SHARED, a
LISTINGS folder), a listing read from a --from folder the index does not
hold. The build (build.listing_rows_for) and the check
(recover.rows_of_system) apply ONE rule, build.rows_that_count: the rows of
the program's own system's listing; when that system has none, every other
listing of the name, but only while no other system holds a program of that
name. One environment's listing never decides for the other's copy, a
listing filed elsewhere never decides for either of two same-named programs,
and a SHARED listing decides for the one CLAIMS program of its name - the
verifier's case, where derive_systems made the listing SHARED and the rule
of the earlier round threw it away.
A listing naming two libraries for one copybook confirms the copy used when
it is one of them; a `library` row with an empty dataset leaves the folder
name to decide, in the build as in recover; and a choice the listing
contradicts while the index holds the listing's copy marks the program for
the next build - on an index this toolkit built; on one another toolkit
built the next build re-parses every member anyway. Coverage says which
choices the listing decided and shows that word whole.
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
        self.assertEqual(self.first_stats["checked"], (3, 0, 1), self.first_said)
        self.assertIn("copybook choices checked against the listings: 3 confirmed, 0 contradicted, 1 unknown", "\n".join(self.first_said))
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
        self.assertEqual(set(rows[("GCPGM1", "DUPREC")]), {("PROD.GC.COPYLIB", "GC"), ("TEST.GC.COPYLIB", "GC-TEST")})
        self.assertEqual(rows[("GCPGM2", "DUPREC")], (("TEST.GC.COPYLIB", "GC-TEST"),))
        self.assertEqual(build.listing_rows_for(rows[("GCPGM1", "DUPREC")], "GC", {"GC-TEST"}), ["PROD.GC.COPYLIB"])
        self.assertEqual(build.listing_rows_for(rows[("GCPGM1", "DUPREC")], "GC-TEST", {"GC"}), ["TEST.GC.COPYLIB"])
        self.assertEqual(build.listing_rows_for(rows[("GCPGM2", "DUPREC")], "GC", {"GC-TEST"}), [])
        self.assertEqual(build.listing_rows_for(rows[("GCPGM2", "DUPREC")], "CLAIMS", {"GC", "GC-TEST"}), [])
        # the twins as the build reads them: every program member of the name in another system
        ctx = build.Ctx(self.conn, quiet=True)
        for sysname, name, twins in (("GC", "GCPGM1", {"GC-TEST"}), ("GC-TEST", "GCPGM2", {"GC"})):
            mems = [build.Mem(member_id(self.conn, s_, name), f"{s_}/{name}.cbl", name, "cobol", "L", "x", system=s_)
                    for s_ in ("GC", "GC-TEST")]
            ctx.by_name[name] = mems + [build.Mem(0, f"SHARED/{name}.lst", name, "listing", "L", "y", system="SHARED")]
            prog = next(m for m in mems if m.system == sysname)
            self.assertEqual(build.twin_systems(ctx, prog), twins)

    def test_5_recover_confirms_every_choice_and_says_whose_listing_it_lacks(self):
        self.assertEqual(self.stats["checked"], (3, 0, 1), self.said)
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
        self.assertIn("_no contradicted choice_", sec)
        self.assertIn(f"(1: {why})", sec)
        self.assertIn(recover.LISTING_RULE, sec)
        self.assertIn("A listing speaks for the program in its own system. When that system has no listing of it, any other "
                      "listing of that name - a SHARED listings folder, a --from folder - speaks for it only while no other "
                      "system holds a program of that name", sec)
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
        rows = [("DUPREC", "SYSLIB", "PROD.GC.COPYLIB", "gc.lst"), ("DUPREC", "SYSLIB", "TEST.GC.COPYLIB", "gt.lst"),
                ("DUPREC", "SYSLIB", "PROD.SHR.COPYLIB", "shared.lst"), ("DUPREC", "SYSLIB", "PROD.X.COPYLIB", "nowhere.lst")]
        systems = {"gc.lst": "GC", "gt.lst": "GC-TEST", "shared.lst": "SHARED"}   # nowhere.lst: not indexed
        pairs = [("PROD.GC.COPYLIB", "GC"), ("TEST.GC.COPYLIB", "GC-TEST"), ("PROD.SHR.COPYLIB", "SHARED"),
                 ("PROD.X.COPYLIB", None)]
        every = ["PROD.GC.COPYLIB", "TEST.GC.COPYLIB", "PROD.SHR.COPYLIB", "PROD.X.COPYLIB"]
        for system, twins, want in (
                ("GC", {"GC-TEST"}, ["PROD.GC.COPYLIB"]),                   # its own system's listing, and only that
                ("GC-TEST", {"GC"}, ["TEST.GC.COPYLIB"]),
                ("GC", set(), ["PROD.GC.COPYLIB"]),                         # own rows win even with no twin
                ("CLAIMS", set(), every),                                   # none of its own, no twin: every listing
                ("CLAIMS", {"GC"}, []),                                     # none of its own, a twin: none of them
                ("SHARED", set(), ["PROD.SHR.COPYLIB"]),
                (None, set(), every),                                       # no system: no own rows; no twin: every one
                (None, {"GC"}, [])):
            with self.subTest(system=system, twins=twins):
                self.assertEqual([r[2] for r in recover.rows_of_system(rows, system, systems, twins)], want)
                self.assertEqual(build.listing_rows_for(pairs, system, twins), want)
        self.assertEqual(build.listing_rows_for(pairs, "gc", {"GC-TEST"}), ["PROD.GC.COPYLIB"])   # the system in any case


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
            recover.store_copy_sources(conn, {"TWOLIB": [("DUPREC", "SYSLIB", "PROD.NOTHERE.COPYLIB", "TWOLIB.lst"),
                                                         ("DUPREC", "MYLIB", "PROD.CLM.COPYLIB", "TWOLIB.lst")]})
        finally:
            conn.close()
        build_it(cls.root, cls.db)
        conn = build.open_db(cls.db)                                     # CHAIN2's rows stored after its parse: the chain's pick
        try:
            recover.store_copy_sources(conn, {"CHAIN2": [("DUPREC", "SYSLIB", "PROD.NOTHERE.COPYLIB", "CHAIN2.lst"),
                                                         ("DUPREC", "MYLIB", "PROD.CLM.COPYLIB", "CHAIN2.lst")]})
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
            self.assertFalse(by[name]["holds_copy"])
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["checked"], stats["marked"]), ((2, 0, 0), 0), said)
        self.assertNotIn("contradicted choice is a wrong fact", "\n".join(said))
        self.assertEqual(set(statuses(self.db).values()), {"ok"})

    def test_3_program_names_both_libraries_and_which_one_was_used(self):
        out = query.cmd_program(self.conn, "TWOLIB")
        self.assertIn("- listing says: DUPREC came from PROD.CLM.COPYLIB (MYLIB) - confirms the copy the build used", out)
        self.assertIn("- listing says: DUPREC came from PROD.NOTHERE.COPYLIB (SYSLIB) - a second library the listing names for "
                      "this copybook; the build used PROD.CLM.COPYLIB", out)
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
            recover.store_copy_sources(conn, {"EDGEPGM": [("DUPREC", "SYSLIB", "PROD.EDGE.COPYLIB", "EDGEPGM.lst")]})
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
        self.assertEqual((self.first_stats["checked"], self.first_stats["marked"]), ((0, 2, 4), 2), text)
        self.assertIn("  2 programs whose listing names a copy the index holds are marked for the next build - it reads the "
                      "listing first", text)
        self.assertEqual(self.after_first[("CLAIMS", "WRONGPK")], "pending")
        self.assertEqual(self.after_first[("CLAIMS", "FROMPGM")], "pending")
        self.assertEqual({k: v for k, v in self.after_first.items() if k[1] in ("TWINPGM", "TWIN2")},
                         {("GC", "TWINPGM"): "ok", ("GC-TEST", "TWINPGM"): "ok", ("GC", "TWIN2"): "ok", ("GC-TEST", "TWIN2"): "ok"})
        self.assertIn("| WRONGPK | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | CONTRADICTED - marked for the next build |", self.first_report)

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
        self.assertEqual((self.stats["checked"], self.stats["marked"]), ((2, 0, 4), 0), text)
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
        self.assertEqual(rows[("WRONGPK", "DUPREC")], (("PROD.POLICY.COPYLIB", "SHARED"),))
        self.assertEqual(rows[("FROMPGM", "DUPREC")], (("PROD.POLICY.COPYLIB", None),))
        self.assertEqual(build.listing_rows_for(rows[("WRONGPK", "DUPREC")], "CLAIMS", set()), ["PROD.POLICY.COPYLIB"])
        self.assertEqual(build.listing_rows_for(rows[("TWINPGM", "DUPREC")], "GC", {"GC-TEST"}), [])
        self.assertEqual(recover.program_systems(self.conn, ["twinpgm", "WRONGPK"]),
                         {"TWINPGM": {"GC", "GC-TEST"}, "WRONGPK": {"CLAIMS"}})


class ContradictedChoicesMarkedForTheNextBuild(unittest.TestCase):
    """The night's order reversed: the build ran, then the listings were read
    (his routine fetches them later). WRONGPK's listing names POLICY's copy,
    which the index holds; FETCHIT's a library it does not; CHOSEN's agrees.
    On an index this toolkit built, WRONGPK is marked and the next build
    gives it the listing's copy; on one another toolkit built nothing is
    marked - that build re-parses every member anyway."""

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
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 2, 0), 1), text)
        self.assertIn("copybook choices checked against the listings: 1 confirmed, 2 contradicted, 0 unknown - every contradicted "
                      "choice is a wrong fact in the index", text)
        self.assertIn("  1 program whose listing names a copy the index holds is marked for the next build - it reads the listing first", text)
        self.assertIn("next: run your usual build command - the programs marked re-expand by themselves", text)
        self.assertEqual(statuses(self.db), {("CLAIMS", "CHOSEN"): "ok", ("CLAIMS", "WRONGPK"): "pending", ("CLAIMS", "FETCHIT"): "ok"})
        sec = self.section()
        self.assertIn("- 2 of the contradicted: the index holds the copy the listing names - the 1 program is marked for the next build, "
                      "which reads the listing first: run your usual build command".replace("2 of", "1 of"), sec)
        self.assertIn("| WRONGPK | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.POLICY.COPYLIB (SYSLIB) - the index holds that copy at POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy - "
                      "the build chose the other | CONTRADICTED - marked for the next build |", sec)
        self.assertIn("| FETCHIT | DUPREC | PROD.CLAIMS.COPYLIB (CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy; same system) | "
                      "PROD.SHARED.COPYLIB (COPYLIB) - not a library the index holds - fetch it | CONTRADICTED |", sec)
        # the next build, incremental: WRONGPK alone is parsed again and takes the listing's copy
        build_it(self.root, self.db)
        conn = query.connect(self.db)
        try:
            note, used = pick_of(conn, "WRONGPK")
            self.assertEqual(used, self.policy)
            self.assertTrue(note.endswith(f"({LISTING}PROD.POLICY.COPYLIB)"), note)
            self.assertEqual(expanded_from(conn, "WRONGPK"), {self.policy})
            note, used = pick_of(conn, "CHOSEN")
            self.assertTrue(note.endswith("(same system)"), note)              # not parsed again: nothing to correct
        finally:
            conn.close()
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((2, 1, 0), 0), text)
        self.assertNotIn("marked for the next build", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        self.assertIn("| FETCHIT | DUPREC |", self.section())
        self.assertNotIn("| WRONGPK |", self.section())

    def test_2_a_dry_run_says_would_be_marked_and_marks_nothing(self):
        stats, text = self.run_it(dry_run=True)
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 2, 0), 0), text)
        self.assertIn("  1 program whose listing names a copy the index holds would be marked for the next build (dry run: nothing changed)", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        self.assertIn("- 1 of the contradicted: the index holds the copy the listing names - the 1 program would be marked for the "
                      "next build (dry run: nothing changed)", self.section())
        self.assertIn("| CONTRADICTED - would be marked for the next build (dry run) |", self.section())

    def test_3_an_index_another_toolkit_built_is_left_alone(self):
        set_fingerprint(self.db, "0123456789abcdef")
        stats, text = self.run_it()
        self.assertEqual((stats["checked"], stats["marked"]), ((1, 2, 0), 0), text)
        self.assertIn("  1 program whose listing names a copy the index holds: the next build re-parses every member (the toolkit "
                      "changed since the index was built) and reads the listing first", text)
        self.assertNotIn("marked for the next build", text)
        self.assertNotIn("next: run your usual build command", text)
        self.assertEqual(set(statuses(self.db).values()), {"ok"})
        sec = self.section()
        self.assertIn("- 1 of the contradicted: the index holds the copy the listing names - the next build re-parses every member "
                      "(the toolkit changed since the index was built) and reads the listing first", sec)
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
            recover.store_copy_sources(conn, {"WRONGPK": [("DUPREC", "SYSLIB", "PROD.POLICY.COPYLIB", "WRONGPK.lst")]})
            conn.commit()
        finally:
            conn.close()
        stats, text = self.run_it()
        self.assertIn("expanded texts to read: 0 - the index holds no compiler listings and no --from folder was given", text)
        self.assertEqual((stats["checked"], stats["marked"]), ((0, 1, 2), 1), text)
        self.assertIn("  1 program whose listing names a copy the index holds is marked for the next build - it reads the listing first", text)
        self.assertIn("next: run your usual build command - the programs marked re-expand by themselves", text)
        self.assertEqual(statuses(self.db)[("CLAIMS", "WRONGPK")], "pending")

    def test_5_coverage_says_who_decided_and_shows_the_word_whole(self):
        self.run_it()
        build_it(self.root, self.db)
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        chosen = cov.split("### Complete, with a copybook chosen among several")[1].split("\n###")[0]
        self.assertTrue(chosen.startswith(": 3 members - 1 choice decided by the program's compiler listing, 2 picked by system "
                                          "and library order; the 'ambiguous_copybook' rows name the copy used"), chosen[:160])
        self.assertIn("| cobol | WRONGPK | PROD.CLAIMS.SRC | DUPREC: 2 copies, used POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy "
                      "(listing: PROD.POLICY.COPYLIB) |", chosen)
        self.assertIn("| cobol | CHOSEN | PROD.CLAIMS.SRC | DUPREC: 2 copies, used CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy (same system) |", chosen)
        self.assertIn("2 of these choices are confirmed by the program's listing, 1 contradicted (see work/recover.md), 0 unknown", chosen)
        self.assertIn("1 of the choices is the compiler's own: the program's listing names the library the copybook was read from, "
                      "and that copy was expanded (`listing: DATASET` in the table) - nothing to declare for those. The other 2 "
                      "follow `COPY ... OF`, then the member's own system in its declared copybook order, then the manifest's "
                      "authoritative copy, and one of those is wrong only where the manifest's system or copybook order is; "
                      "1 copybook name(s) are involved - `ambiguous` lists them per department.", chosen)
        kinds = cov.split("### Unresolved by kind")[1].split("\n###")[0]
        self.assertIn("| ambiguous_copybook (decided by the listing) | 1 |", kinds)
        self.assertIn("| ambiguous_copybook | 2 | two copies of one copybook with different content; one was chosen | declare the "
                      "department's copybook order (manifest `copylib_order`) or remove the stale copy |", kinds)
        self.assertNotIn("| expand (copybook chosen among several", kinds)     # no COPY warning repeats the choice (item 18)


if __name__ == "__main__":
    unittest.main()
