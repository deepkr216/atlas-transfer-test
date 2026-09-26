"""
A recovered copybook gives way to the real member it stands in for (ROADMAP re-parse item 11).

atlas.recover writes a copybook it rebuilt from the compiler listings under a
RECOVERED-COPYBOOKS folder (SHARED's, or a system's own when the systems'
texts differ). It stands in for a missing member. Before the item the
resolver's chain ranked it like any library: in the program's own system it
beat a real copy under SHARED ('same system'), in a folder that sorts first it
beat one that sorts after ('first found'), and a recovered copy whose text
differed from the real one made the choice 'among several' - an
'ambiguous_copybook' row for a stand-in. Coverage warned meanwhile
(query._recovered_shadowing, LESSONS 168). Now the build drops a recovered
copy once a real member of the name sits in the program's own system, in
SHARED, or in the system the copy was written for; a real member only
another system holds leaves the program's own recovered copy in the choice
(LESSONS 209, below). Among recovered copies alone the chain decides as
before. On an index this toolkit built the shadowing warning finds nothing,
and atlas.recover's removal marks no program; on an index built before the
item (written by hand below) both still work.
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

PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. {name}.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-OUT              PIC X(5).
       01  WS-REC.
           COPY {book}.
       PROCEDURE DIVISION.
{moves}
           GOBACK.
"""


def program(name, bk, *fields):
    """A program copying BK that MOVEs each of `fields` - the fields of every copy of BK, so its own data items
    (pfield) hold the ones of the copy it expanded."""
    return PROGRAM.format(name=name, book=bk, moves="\n".join(f"           MOVE {f} TO WS-OUT." for f in fields))


RECOVERED_HEAD = "      * RECOVERED by atlas.recover from the expanded text of a program\n"


def book(*fields):
    return "".join(f"           05  {f:<16}PIC X(5).\n" for f in fields)


FILES = {
    # the recovered copy sits in the program's own system: the chain's 'same system' would take it over SHARED's
    "GC/PROD.GC.SRC/RKPGM1.cbl": program("RKPGM1", "RKREC", "RK-OLD-FIELD", "RK-NEW-FIELD"),
    "GC/RECOVERED-COPYBOOKS/RKREC.cpy": RECOVERED_HEAD + book("RK-OLD-FIELD"),
    "SHARED/PROD.SHR.COPYLIB/RKREC.cpy": book("RK-NEW-FIELD"),
    # two real copies that differ and a recovered one: a choice between the two real copies, counted as two
    "GC/PROD.GC.SRC/RKPGM2.cbl": program("RKPGM2", "RKTWO", "RK-GC-FIELD", "RK-POL-FIELD", "RK-REC-FIELD"),
    "GC/PROD.GC.COPYLIB/RKTWO.cpy": book("RK-GC-FIELD"),
    "POLICY/PROD.POLICY.COPYLIB/RKTWO.cpy": book("RK-POL-FIELD"),
    "GC/RECOVERED-COPYBOOKS/RKTWO.cpy": RECOVERED_HEAD + book("RK-REC-FIELD"),
    # recovered copies alone, one per system (the systems' texts differ): the chain decides as before
    "GC/PROD.GC.SRC/RKPGM3.cbl": program("RKPGM3", "RKONLY", "RK-GC-ONLY", "RK-SHR-ONLY", "RK-REAL-ONLY"),
    "GC/RECOVERED-COPYBOOKS/RKONLY.cpy": RECOVERED_HEAD + book("RK-GC-ONLY"),
    "SHARED/RECOVERED-COPYBOOKS/RKONLY.cpy": RECOVERED_HEAD + book("RK-SHR-ONLY"),
    # the real member sorts after the recovered folder: the chain's 'first found' would take the recovered copy
    "GC/PROD.GC.SRC/RKPGM4.cbl": program("RKPGM4", "RKLATE", "RK-LATE-OLD", "RK-LATE-NEW"),
    "SHARED/RECOVERED-COPYBOOKS/RKLATE.cpy": RECOVERED_HEAD + book("RK-LATE-OLD"),
    "SHARED/ZZ.LATE.COPYLIB/RKLATE.cpy": book("RK-LATE-NEW"),
}


def make_estate(td, files=None):
    root = os.path.join(td, "estate")
    for rel, text in (files or FILES).items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    db = os.path.join(td, "t.db")
    build_it(root, db, "--rebuild")
    return root, db


def build_it(root, db, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = build._main([root, "--db", db, "--quiet", *extra])
    assert rc == 0, buf.getvalue()


def age_index(db):
    """The shape a build before ROADMAP re-parse item 11 left where the chain
    took the recovered copy: RKPGM1's and RKPGM4's COPY rows point at the
    recovered member although the real one is in the index."""
    conn = sqlite3.connect(db)
    try:
        for prog, bk, folder in (("RKPGM1", "RKREC", "GC"), ("RKPGM4", "RKLATE", "SHARED")):
            rec = [r[0] for r in conn.execute("SELECT id, path FROM member WHERE name=? AND UPPER(library)=?",
                                              (bk, build.RECOVERED_FOLDER))
                   if os.sep + folder + os.sep in r[1]]
            assert len(rec) == 1, rec
            conn.execute("UPDATE copy_use SET resolved_member_id=? WHERE copybook=? AND member_id=(SELECT id FROM member "
                         "WHERE name=? AND kind='cobol')", (rec[0], bk, prog))
        conn.commit()
    finally:
        conn.close()


class _Estate(unittest.TestCase):
    files = FILES

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root, self.db = make_estate(self.td, self.files)
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.td, ignore_errors=True)

    def resolved(self, prog, bk):
        """(library, path relative to the estate) of the member PROG's COPY BK expanded."""
        row = self.conn.execute("SELECT r.library, r.path FROM copy_use c JOIN member m ON m.id=c.member_id "
                                "JOIN member r ON r.id=c.resolved_member_id WHERE m.name=? AND m.kind='cobol' "
                                "AND c.copybook=?", (prog, bk)).fetchone()
        return row[0], os.path.relpath(row[1], self.root).replace(os.sep, "/")

    def rows(self, prog, kind="ambiguous_copybook"):
        return [r[0] for r in self.conn.execute("SELECT u.detail FROM unresolved u JOIN member m ON m.id=u.member_id "
                                                "WHERE m.name=? AND m.kind='cobol' AND u.kind=?", (prog, kind))]

    def status(self, prog):
        return self.conn.execute("SELECT parse_status FROM member WHERE name=? AND kind='cobol'", (prog,)).fetchone()[0]

    def fields(self, prog):
        return {r[0] for r in self.conn.execute("SELECT f.name FROM pfield f JOIN program p ON p.id=f.program_id "
                                                "JOIN member m ON m.id=p.member_id WHERE m.name=?", (prog,))}

    def recover_run(self):
        said = []
        stats = recover.run(self.db, log=said.append, report=os.path.join(self.td, "work", "recover.md"))
        return stats, said


class TheResolverRanksARecoveredCopyLast(_Estate):

    def test_the_folder_name_is_recovers_own(self):
        self.assertEqual(build.RECOVERED_FOLDER, recover.FOLDER)

    def test_a_real_copy_in_another_system_beats_the_recovered_copy_in_the_programs_own(self):
        self.assertEqual(self.resolved("RKPGM1", "RKREC"), ("PROD.SHR.COPYLIB", "SHARED/PROD.SHR.COPYLIB/RKREC.cpy"))
        self.assertIn("RK-NEW-FIELD", self.fields("RKPGM1"))
        self.assertNotIn("RK-OLD-FIELD", self.fields("RKPGM1"))
        # a stand-in is not a second library: no choice among several, and the program is ok with no note
        self.assertEqual(self.rows("RKPGM1"), [])
        self.assertEqual(self.rows("RKPGM1", "expand"), [])
        self.assertEqual(self.status("RKPGM1"), "ok")

    def test_a_real_copy_that_sorts_after_the_recovered_folder_beats_it(self):
        self.assertEqual(self.resolved("RKPGM4", "RKLATE"), ("ZZ.LATE.COPYLIB", "SHARED/ZZ.LATE.COPYLIB/RKLATE.cpy"))
        self.assertIn("RK-LATE-NEW", self.fields("RKPGM4"))
        self.assertEqual(self.rows("RKPGM4"), [])
        self.assertEqual(self.status("RKPGM4"), "ok")

    def test_two_real_copies_are_a_choice_between_the_two(self):
        self.assertEqual(self.resolved("RKPGM2", "RKTWO"), ("PROD.GC.COPYLIB", "GC/PROD.GC.COPYLIB/RKTWO.cpy"))
        (row,) = self.rows("RKPGM2")
        self.assertTrue(row.startswith("2 copies of RKTWO with different content; used "), row)
        self.assertIn("PROD.GC.COPYLIB", row)
        self.assertTrue(row.endswith("(same system)"), row)
        self.assertNotIn(recover.FOLDER, row)
        self.assertEqual(self.status("RKPGM2"), "ok")

    def test_recovered_copies_alone_are_chosen_by_the_chain_as_before(self):
        # one per system, their texts differ: the program's own system's copy, and the choice recorded
        self.assertEqual(self.resolved("RKPGM3", "RKONLY"), ("RECOVERED-COPYBOOKS", "GC/RECOVERED-COPYBOOKS/RKONLY.cpy"))
        (row,) = self.rows("RKPGM3")
        self.assertTrue(row.startswith("2 copies of RKONLY with different content; used "), row)
        self.assertTrue(row.endswith("(same system)"), row)
        self.assertIn("RK-GC-ONLY", self.fields("RKPGM3"))

    def test_coverage_and_program_have_nothing_to_warn_of(self):
        self.assertEqual(query._recovered_shadowing(self.conn), "")
        cov = query.cmd_coverage(self.conn)
        self.assertNotIn("still expand a recovered copybook", cov)
        for prog in ("RKPGM1", "RKPGM4"):
            text = query.cmd_program(self.conn, prog)
            self.assertNotIn("chosen among several", text)
            self.assertNotIn("RECOVERED-COPYBOOKS", text)

    def test_recover_removes_the_stand_ins_and_marks_no_program(self):
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (3, 0), said)
        self.assertTrue(any("3 recovered copybook(s) removed - the estate now holds the real member: RKLATE, RKREC, RKTWO"
                            in s for s in said), said)
        self.assertIn("  " + recover.REMOVED_NONE_EXPANDS, said)
        self.assertIn(recover.REMOVED_NEXT, said)
        self.assertFalse(any("that had expanded them" in s for s in said), said)
        self.assertFalse(os.path.exists(os.path.join(self.root, "GC", "RECOVERED-COPYBOOKS", "RKREC.cpy")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "GC", "RECOVERED-COPYBOOKS", "RKONLY.cpy")))
        for prog in ("RKPGM1", "RKPGM2", "RKPGM3", "RKPGM4"):
            self.assertEqual(self.status(prog), "ok", prog)
        # the next build drops them; every program keeps the copy it had
        self.conn.close()
        build_it(self.root, self.db)
        self.conn = query.connect(self.db)
        self.assertEqual(self.resolved("RKPGM1", "RKREC")[0], "PROD.SHR.COPYLIB")
        self.assertEqual(self.resolved("RKPGM2", "RKTWO")[0], "PROD.GC.COPYLIB")
        self.assertEqual(self.resolved("RKPGM4", "RKLATE")[0], "ZZ.LATE.COPYLIB")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM member WHERE name IN ('RKREC', 'RKTWO', 'RKLATE') AND "
                                           "UPPER(library)=?", (recover.FOLDER,)).fetchone()[0], 0)
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (0, 0), said)
        self.assertNotIn(recover.REMOVED_NEXT, said)

    def test_the_real_member_arriving_later_is_taken_at_that_build(self):
        # the real RKONLY arrives under SHARED: the incremental build parses RKPGM3 again and takes it at once
        p = os.path.join(self.root, "SHARED", "PROD.SHR.COPYLIB", "RKONLY.cpy")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(book("RK-REAL-ONLY"))
        self.conn.close()
        build_it(self.root, self.db)
        self.conn = query.connect(self.db)
        self.assertEqual(self.resolved("RKPGM3", "RKONLY"), ("PROD.SHR.COPYLIB", "SHARED/PROD.SHR.COPYLIB/RKONLY.cpy"))
        self.assertIn("RK-REAL-ONLY", self.fields("RKPGM3"))
        self.assertEqual(self.rows("RKPGM3"), [])
        self.assertEqual(query._recovered_shadowing(self.conn), "")


class AnIndexBuiltBeforeTheItem(_Estate):
    """RKPGM1 and RKPGM4 expanded the recovered copy although the real member was in the index - what a build before
    the item left: coverage warns, recover marks exactly those programs, and the build makes them whole."""

    def setUp(self):
        super().setUp()
        self.conn.close()
        age_index(self.db)
        self.conn = query.connect(self.db)

    def test_coverage_warns(self):
        shadow = query._recovered_shadowing(self.conn)
        self.assertIn("**2 COPY statement(s) still expand a recovered copybook although the estate now holds the real "
                      "member** (2 copybook(s): ", shadow)
        self.assertIn("RKREC", shadow)
        self.assertIn("RKLATE", shadow)
        self.assertIn(shadow, query.cmd_coverage(self.conn))

    def test_recover_marks_the_programs_that_expanded_them_and_the_build_heals_them(self):
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (3, 2), said)
        self.assertIn("  2 program(s) that had expanded them are marked for the next build", said)
        self.assertIn("next: run your usual build command - the programs marked re-expand by themselves", said)
        self.assertNotIn(recover.REMOVED_NEXT, said)
        self.assertEqual((self.status("RKPGM1"), self.status("RKPGM4")), ("pending", "pending"))
        self.assertEqual((self.status("RKPGM2"), self.status("RKPGM3")), ("ok", "ok"))
        self.conn.close()
        build_it(self.root, self.db)
        self.conn = query.connect(self.db)
        self.assertEqual(self.resolved("RKPGM1", "RKREC")[0], "PROD.SHR.COPYLIB")
        self.assertEqual(self.resolved("RKPGM4", "RKLATE")[0], "ZZ.LATE.COPYLIB")
        self.assertIn("RK-NEW-FIELD", self.fields("RKPGM1"))
        self.assertEqual(query._recovered_shadowing(self.conn), "")


class ExpandingPrograms(unittest.TestCase):
    """recover.expanding_programs: by the COPY row's member, never by the copybook's name."""

    def test_by_the_row(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE member(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, path TEXT, library TEXT);
            CREATE TABLE copy_use(member_id INTEGER, copybook TEXT, resolved_member_id INTEGER);""")
        rec = os.path.join(os.sep + "e", "SHARED", "RECOVERED-COPYBOOKS", "BK.cpy")
        real = os.path.join(os.sep + "e", "SHARED", "COPYLIB", "BK.cpy")
        conn.executemany("INSERT INTO member VALUES(?,?,?,?,?)", [
            (1, "BK", "copybook", rec, "RECOVERED-COPYBOOKS"), (2, "BK", "copybook", real, "COPYLIB"),
            (3, "P1", "cobol", "p1", "SRC"), (4, "P2", "cobol", "p2", "SRC"), (5, "OUTER", "copybook", "o", "COPYLIB")])
        conn.executemany("INSERT INTO copy_use VALUES(?,?,?)", [
            (3, "BK", 1), (4, "BK", 2), (5, "BK", 1)])
        self.assertEqual(recover.expanding_programs(conn, [rec]), [3])      # a copybook's row is not a program's
        self.assertEqual(recover.expanding_programs(conn, [real]), [4])
        self.assertEqual(recover.expanding_programs(conn, []), [])
        self.assertEqual(recover.expanding_programs(conn, [os.path.join(os.sep + "e", "X", "BK.cpy")]), [])


# ---------------------------------------------------------------------------
# The verifier's round on item 11 (LESSONS 209). The first delivery dropped every recovered copy as soon as ANY real
# member of the name was a candidate, in any system: atlas.recover writes a copy under a system's own folder exactly
# when the systems' listings show different texts, so where the only real member that had arrived was another
# system's (GC-TEST's for GC), GC's program expanded GC-TEST's layout, 'ok', with no row and no warning anywhere - and
# recover removed GC's copy the same way. A recovered copy stands in for the member of the system it was written for
# (SHARED's for the estate): it gives way to a real member in the program's own system, in SHARED, or in that system,
# and recover removes it only once no program copying the name would still expand it. A program that copies a
# copybook of its own name never expands itself: its recovered copy was counted as replaced by the program's own
# member - coverage warned on a new index, and recover removed the copy, the build said NOT FOUND, the next run wrote
# it again, every run.

OWN_FILES = {
    # GC and GC-TEST each hold XSPGM1 copying XSREC; each system has its own recovered copy (their listings showed
    # different texts), and only GC-TEST's real library has arrived
    "GC/PROD.GC.SRC/XSPGM1.cbl": program("XSPGM1", "XSREC", "XS-PROD-FIELD", "XS-TEST-FIELD"),
    "GC-TEST/TEST.GC.SRC/XSPGM1.cbl": program("XSPGM1", "XSREC", "XS-PROD-FIELD", "XS-TEST-FIELD"),
    "GC/RECOVERED-COPYBOOKS/XSREC.cpy": RECOVERED_HEAD + book("XS-PROD-FIELD"),
    "GC-TEST/RECOVERED-COPYBOOKS/XSREC.cpy": RECOVERED_HEAD + book("XS-TEST-FIELD"),
    "GC-TEST/TEST.GC.COPYLIB/XSREC.cpy": book("XS-TEST-FIELD"),
    # the estate's recovered copy (the listings showed one text) and a real member POLICY alone holds, copied by a
    # GC program and a POLICY program
    "GC/PROD.GC.SRC/XSPGM2.cbl": program("XSPGM2", "XSONE", "XS-SHR-FIELD", "XS-POL-FIELD"),
    "POLICY/PROD.POLICY.SRC/XSPGM3.cbl": program("XSPGM3", "XSONE", "XS-SHR-FIELD", "XS-POL-FIELD"),
    "SHARED/RECOVERED-COPYBOOKS/XSONE.cpy": RECOVERED_HEAD + book("XS-SHR-FIELD"),
    "POLICY/PROD.POLICY.COPYLIB/XSONE.cpy": book("XS-POL-FIELD"),
    # a program that copies a copybook of its own name: its COPY takes the recovered copy
    "GC/PROD.GC.SRC/XSSELF.cbl": program("XSSELF", "XSSELF", "XS-SELF-FIELD"),
    "GC/RECOVERED-COPYBOOKS/XSSELF.cpy": RECOVERED_HEAD + book("XS-SELF-FIELD"),
}


class _OwnSystem(_Estate):
    files = OWN_FILES

    def where(self, system, prog, bk):
        """(library, path relative to the estate) of the member SYSTEM's PROG's COPY BK expanded, NOT FOUND as None."""
        row = self.conn.execute("SELECT r.library, r.path FROM copy_use c JOIN member m ON m.id=c.member_id "
                                "LEFT JOIN member r ON r.id=c.resolved_member_id WHERE m.name=? AND m.kind='cobol' "
                                "AND m.system=? AND c.copybook=?", (prog, system, bk)).fetchone()
        return (row[0], os.path.relpath(row[1], self.root).replace(os.sep, "/")) if row[1] else None

    def rows_in(self, system, prog):
        return [r[0] for r in self.conn.execute("SELECT u.detail FROM unresolved u JOIN member m ON m.id=u.member_id "
                                                "WHERE m.name=? AND m.system=? AND m.kind='cobol' AND "
                                                "u.kind='ambiguous_copybook'", (prog, system))]

    def status_in(self, system, prog):
        return self.conn.execute("SELECT parse_status FROM member WHERE name=? AND system=? AND kind='cobol'",
                                 (prog, system)).fetchone()[0]

    def fields_in(self, system, prog):
        return {r[0] for r in self.conn.execute("SELECT f.name FROM pfield f JOIN program p ON p.id=f.program_id "
                                                "JOIN member m ON m.id=p.member_id WHERE m.name=? AND m.system=?",
                                                (prog, system))}

    def on_disk(self, rel):
        return os.path.exists(os.path.join(self.root, *rel.split("/")))

    def rebuild(self):
        self.conn.close()
        build_it(self.root, self.db)
        self.conn = query.connect(self.db)


class ARecoveredCopyStandsForItsOwnSystem(_OwnSystem):

    def test_another_systems_real_copy_does_not_replace_the_programs_own_recovered_copy(self):
        self.assertEqual(self.where("GC", "XSPGM1", "XSREC"), (recover.FOLDER, "GC/RECOVERED-COPYBOOKS/XSREC.cpy"))
        self.assertIn("XS-PROD-FIELD", self.fields_in("GC", "XSPGM1"))
        self.assertNotIn("XS-TEST-FIELD", self.fields_in("GC", "XSPGM1"))
        # a choice, said: GC-TEST's real copy against GC's own recovered one - GC-TEST's recovered copy, which
        # GC-TEST's real member replaced, is not counted
        (row,) = self.rows_in("GC", "XSPGM1")
        self.assertTrue(row.startswith("2 copies of XSREC with different content; used "), row)
        self.assertIn(os.path.join("GC", recover.FOLDER, "XSREC.cpy"), row)
        self.assertTrue(row.endswith("(same system)"), row)
        self.assertIn("XSREC: 2 copies, used GC/RECOVERED-COPYBOOKS/XSREC.cpy (same system)", query.cmd_coverage(self.conn))
        # GC-TEST's program takes its own system's real member, with nothing to choose
        self.assertEqual(self.where("GC-TEST", "XSPGM1", "XSREC"), ("TEST.GC.COPYLIB", "GC-TEST/TEST.GC.COPYLIB/XSREC.cpy"))
        self.assertIn("XS-TEST-FIELD", self.fields_in("GC-TEST", "XSPGM1"))
        self.assertEqual(self.rows_in("GC-TEST", "XSPGM1"), [])
        self.assertEqual(self.status_in("GC-TEST", "XSPGM1"), "ok")

    def test_the_estates_recovered_copy_stays_a_choice_beside_another_systems_real_one(self):
        # POLICY's program takes POLICY's real member; for the GC program both stay candidates and the choice is said
        self.assertEqual(self.where("POLICY", "XSPGM3", "XSONE"),
                         ("PROD.POLICY.COPYLIB", "POLICY/PROD.POLICY.COPYLIB/XSONE.cpy"))
        self.assertEqual(self.rows_in("POLICY", "XSPGM3"), [])
        (row,) = self.rows_in("GC", "XSPGM2")
        self.assertTrue(row.startswith("2 copies of XSONE with different content; used "), row)

    def test_a_program_copying_its_own_name_expands_the_recovered_copy(self):
        self.assertEqual(self.where("GC", "XSSELF", "XSSELF"), (recover.FOLDER, "GC/RECOVERED-COPYBOOKS/XSSELF.cpy"))
        self.assertEqual(self.status_in("GC", "XSSELF"), "ok")
        self.assertEqual(self.rows_in("GC", "XSSELF"), [])
        # the program's own member is not the real member its recovered copy waits for
        self.assertNotIn("XSSELF", query._recovered_shadowing(self.conn))

    def test_coverage_has_nothing_to_warn_of(self):
        self.assertEqual(query._recovered_shadowing(self.conn), "")
        self.assertNotIn("still expand a recovered copybook", query.cmd_coverage(self.conn))

    def test_recover_removes_only_the_copy_a_real_member_replaced_and_nothing_loops(self):
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (1, 0), said)
        self.assertIn("  1 recovered copybook(s) removed - the estate now holds the real member: XSREC", said)
        self.assertIn("  " + recover.REMOVED_NONE_EXPANDS, said)
        self.assertFalse(self.on_disk("GC-TEST/RECOVERED-COPYBOOKS/XSREC.cpy"))
        for kept in ("GC/RECOVERED-COPYBOOKS/XSREC.cpy", "SHARED/RECOVERED-COPYBOOKS/XSONE.cpy",
                     "GC/RECOVERED-COPYBOOKS/XSSELF.cpy"):
            self.assertTrue(self.on_disk(kept), kept)
        # XSSELF is held only as a recovered copy: its library stays on the fetch list
        self.assertTrue(any(s.startswith("  held only as recovered copies: 1 ") for s in said), said)
        for _round in range(2):
            self.rebuild()
            self.assertEqual(self.where("GC", "XSPGM1", "XSREC")[1], "GC/RECOVERED-COPYBOOKS/XSREC.cpy")
            self.assertTrue(self.rows_in("GC", "XSPGM1")[0].startswith("2 copies of XSREC "))
            self.assertEqual(self.where("GC-TEST", "XSPGM1", "XSREC")[0], "TEST.GC.COPYLIB")
            self.assertEqual(self.where("GC", "XSSELF", "XSSELF")[1], "GC/RECOVERED-COPYBOOKS/XSSELF.cpy")
            self.assertEqual(self.status_in("GC", "XSSELF"), "ok")
            self.assertEqual(query._recovered_shadowing(self.conn), "")
            stats, said = self.recover_run()
            self.assertEqual((stats["missing"], stats["removed"], stats["marked"]), (0, 0, 0), said)
            self.assertTrue(any(s.startswith("missing copybooks in the index: 0,") for s in said), said)

    def test_the_programs_own_real_member_replaces_it_when_it_arrives(self):
        p = os.path.join(self.root, "GC", "PROD.GC.COPYLIB", "XSREC.cpy")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(book("XS-PROD-FIELD"))
        self.rebuild()
        self.assertEqual(self.where("GC", "XSPGM1", "XSREC"), ("PROD.GC.COPYLIB", "GC/PROD.GC.COPYLIB/XSREC.cpy"))
        # two real copies whose texts differ: a choice between them, the recovered copies not counted
        (row,) = self.rows_in("GC", "XSPGM1")
        self.assertTrue(row.startswith("2 copies of XSREC with different content; used "), row)
        self.assertNotIn(recover.FOLDER, row)
        self.assertEqual(query._recovered_shadowing(self.conn), "")
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (2, 0), said)
        self.assertFalse(self.on_disk("GC/RECOVERED-COPYBOOKS/XSREC.cpy"))


class AnIndexBuiltBeforeTheItemWithAnotherSystemsRealCopy(_OwnSystem):
    """GC-TEST's XSPGM1 expanded GC-TEST's recovered copy although GC-TEST's real member was in the index - the chain's
    'first found' before the item (written by hand). Only that COPY statement is a stale one: GC's program expanding
    GC's own recovered copy is not, nor XSSELF expanding the copy of its own name."""

    def setUp(self):
        super().setUp()
        rec = self.conn.execute("SELECT id FROM member WHERE name='XSREC' AND system='GC-TEST' AND UPPER(library)=?",
                                (build.RECOVERED_FOLDER,)).fetchone()[0]
        self.conn.execute("UPDATE copy_use SET resolved_member_id=? WHERE copybook='XSREC' AND member_id=(SELECT id "
                          "FROM member WHERE name='XSPGM1' AND system='GC-TEST' AND kind='cobol')", (rec,))
        self.conn.commit()

    def test_coverage_warns_of_that_statement_alone(self):
        shadow = query._recovered_shadowing(self.conn)
        self.assertIn("**1 COPY statement(s) still expand a recovered copybook although the estate now holds the real "
                      "member** (1 copybook(s): XSREC)", shadow)

    def test_recover_marks_that_program_alone_and_the_build_heals_it(self):
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (1, 1), said)
        self.assertIn("  1 program(s) that had expanded them are marked for the next build", said)
        self.assertEqual((self.status_in("GC-TEST", "XSPGM1"), self.status_in("GC", "XSPGM1")), ("pending", "ok"))
        self.rebuild()
        self.assertEqual(self.where("GC-TEST", "XSPGM1", "XSREC")[0], "TEST.GC.COPYLIB")
        self.assertEqual(self.where("GC", "XSPGM1", "XSREC")[1], "GC/RECOVERED-COPYBOOKS/XSREC.cpy")
        self.assertEqual(query._recovered_shadowing(self.conn), "")


def _mem(i, system, recovered=False, kind="copybook"):
    return build.Mem(id=i, path=f"{system}/{i}", name="BK", kind=kind, norm_sha=str(i), system=system,
                     library=build.RECOVERED_FOLDER if recovered else f"{system or 'NONE'}.COPYLIB")


class RecoveredGivesWay(unittest.TestCase):
    """build.recovered_gives_way, one list of candidates at a time; recover.replaced, the same rule over every program
    copying the name."""

    def ids(self, prog_system, *cands):
        prog = _mem(99, prog_system, kind="cobol")
        return [c.id for c in build.recovered_gives_way(list(cands), prog)]

    def test_recovered_home(self):
        self.assertEqual([build.recovered_home(s) for s in ("SHARED", "shared", "", None, "gc-test")],
                         ["", "", "", "", "GC-TEST"])

    def test_the_resolvers_rule(self):
        gc_rec, test_rec, test_real = _mem(1, "GC", True), _mem(2, "GC-TEST", True), _mem(3, "GC-TEST")
        shr_rec, shr_real, pol_real, none_real = _mem(4, "SHARED", True), _mem(5, "SHARED"), _mem(6, "POLICY"), _mem(7, "")
        # another system's real member: the program's own recovered copy stays, the one it replaced goes
        self.assertEqual(self.ids("GC", gc_rec, test_rec, test_real), [1, 3])
        # the program's own system holds the real member: every recovered copy goes
        self.assertEqual(self.ids("GC-TEST", gc_rec, test_rec, test_real), [3])
        # SHARED's real member - or one with no system - replaces every recovered copy, for every program
        self.assertEqual(self.ids("GC", gc_rec, shr_rec, shr_real, pol_real), [5, 6])
        self.assertEqual(self.ids("GC", gc_rec, none_real), [7])
        # the estate's recovered copy beside a real member POLICY alone holds: both stay for a GC program
        self.assertEqual(self.ids("GC", shr_rec, pol_real), [4, 6])
        self.assertEqual(self.ids("POLICY", shr_rec, pol_real), [6])
        # a program with no system: SHARED's rule
        self.assertEqual(self.ids("", gc_rec, pol_real), [1, 6])
        self.assertEqual(self.ids("", shr_rec, none_real), [7])
        # recovered copies alone, or real members alone: as they are
        self.assertEqual(self.ids("GC", gc_rec, shr_rec), [1, 4])
        self.assertEqual(self.ids("GC", test_real, pol_real), [3, 6])

    def test_recovers_rule(self):
        self.assertFalse(recover.replaced(set(), "GC", {"GC"}))              # no real member anywhere
        self.assertFalse(recover.replaced(None, ""))
        self.assertTrue(recover.replaced({""}, "GC", {"GC"}))                # SHARED's real member
        self.assertTrue(recover.replaced({"GC"}, "GC", {"GC", "POLICY"}))    # the system it was written for
        self.assertFalse(recover.replaced({"GC-TEST"}, "GC", {"GC", "GC-TEST"}))   # GC's program still expands it
        self.assertTrue(recover.replaced({"GC-TEST"}, "GC", {"GC-TEST"}))    # no GC program copies it
        self.assertFalse(recover.replaced({"POLICY"}, "", {"GC", "POLICY"}))  # the estate's copy: a GC program
        self.assertTrue(recover.replaced({"GC", "POLICY"}, "", {"GC", "POLICY"}))   # every copier has its own
        self.assertTrue(recover.replaced({"POLICY"}, "GC"))                  # no program copies the name

    def test_folder_home(self):
        root = os.path.join(os.sep + "e", "estate")
        self.assertEqual(recover.folder_home(os.path.join(root, "GC-TEST", recover.FOLDER), root), "GC-TEST")
        self.assertEqual(recover.folder_home(os.path.join(root, "SHARED", recover.FOLDER), root), "")
        self.assertEqual(recover.folder_home(os.path.join(root, recover.FOLDER), root), "")
        self.assertEqual(recover.folder_home(os.path.join(os.sep + "elsewhere", "OUT", "COPIES"), root), "")
        self.assertEqual(recover.folder_home(os.path.join(root, "GC", recover.FOLDER), None), "")


class RealCopiesAndCopiers(unittest.TestCase):
    """recover.real_copies by system, never a program copying its own name; recover.copier_homes, programs only."""

    def test_by_system(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE member(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, path TEXT, library TEXT, system TEXT);
            CREATE TABLE copy_use(member_id INTEGER, copybook TEXT, resolved_member_id INTEGER);""")
        e = os.path.join(os.sep + "e", "estate")
        conn.executemany("INSERT INTO member VALUES(?,?,?,?,?,?)", [
            (1, "BK", "copybook", os.path.join(e, "GC-TEST", "TEST.GC.COPYLIB", "BK.cpy"), "TEST.GC.COPYLIB", "GC-TEST"),
            (2, "BK", "copybook", os.path.join(e, "GC", recover.FOLDER, "BK.cpy"), recover.FOLDER, "GC"),
            (3, "SH", "copybook", os.path.join(e, "SHARED", "COPYLIB", "SH.cpy"), "COPYLIB", "SHARED"),
            (4, "NO", "copybook", os.path.join(e, "COPYLIB", "NO.cpy"), "COPYLIB", None),
            (5, "SELF", "cobol", os.path.join(e, "GC", "SRC", "SELF.cbl"), "SRC", "GC"),
            (6, "LOOP", "copybook", os.path.join(e, "GC", "COPYLIB", "LOOP.cpy"), "COPYLIB", "GC"),
            (7, "PGM", "cobol", os.path.join(e, "POLICY", "SRC", "PGM.cbl"), "SRC", "POLICY"),
            (8, "BK", "stub", os.path.join(e, "GC", "COPYLIB", "BK.cpy"), "COPYLIB", "GC")])
        conn.executemany("INSERT INTO copy_use VALUES(?,?,?)", [
            (5, "self", 2), (6, "LOOP", None), (7, "BK", 1), (5, "BK", 2)])
        real = recover.real_copies(conn, [os.path.join(e, "SHARED", recover.FOLDER)])
        # SELF copies its own name: not the real member of it. LOOP, a copybook copying itself, is (LESSONS 185);
        # a stub never is
        self.assertEqual(real, {"BK": {"GC-TEST"}, "SH": {""}, "NO": {""}, "LOOP": {"GC"}, "PGM": {"POLICY"}})
        self.assertEqual(recover.copier_homes(conn, ["bk", "SELF", "LOOP", "NONE"]), {"BK": {"GC", "POLICY"}, "SELF": {"GC"}})


if __name__ == "__main__":
    unittest.main()
