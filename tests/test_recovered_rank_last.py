"""
A recovered copybook ranks after every other candidate (ROADMAP re-parse item 11).

atlas.recover writes a copybook it rebuilt from the compiler listings under a
RECOVERED-COPYBOOKS folder (SHARED's, or a system's own when the systems'
texts differ). It stands in for a missing member. Before the item the
resolver's chain ranked it like any library: in the program's own system it
beat a real copy under SHARED ('same system'), in a folder that sorts first it
beat one that sorts after ('first found'), and a recovered copy whose text
differed from the real one made the choice 'among several' - an
'ambiguous_copybook' row for a stand-in. Coverage warned meanwhile
(query._recovered_shadowing, LESSONS 168). Now the build takes a recovered
copy only while no other member of the name is a candidate; among recovered
copies alone the chain decides as before. On an index this toolkit built the
shadowing warning finds nothing, and atlas.recover's removal marks no program;
on an index built before the item (written by hand below) both still work.
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


def make_estate(td):
    root = os.path.join(td, "estate")
    for rel, text in FILES.items():
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
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root, self.db = make_estate(self.td)
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


if __name__ == "__main__":
    unittest.main()
