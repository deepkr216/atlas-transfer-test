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
sys.path.insert(0, HERE)

from atlas import build, query, recover  # noqa: E402
from test_recover import ibm_listing  # noqa: E402

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
        # POLICY's program takes POLICY's real member; for the GC program both stay candidates, the chain takes the
        # estate's recovered copy - never POLICY's layout because POLICY's folder sorts first - and the choice is said
        self.assertEqual(self.where("POLICY", "XSPGM3", "XSONE"),
                         ("PROD.POLICY.COPYLIB", "POLICY/PROD.POLICY.COPYLIB/XSONE.cpy"))
        self.assertEqual(self.rows_in("POLICY", "XSPGM3"), [])
        self.assertEqual(self.where("GC", "XSPGM2", "XSONE"), (recover.FOLDER, "SHARED/RECOVERED-COPYBOOKS/XSONE.cpy"))
        self.assertIn("XS-SHR-FIELD", self.fields_in("GC", "XSPGM2"))
        self.assertNotIn("XS-POL-FIELD", self.fields_in("GC", "XSPGM2"))
        (row,) = self.rows_in("GC", "XSPGM2")
        self.assertTrue(row.startswith("2 copies of XSONE with different content; used "), row)
        self.assertTrue(row.endswith(f"({build.ESTATE_COPY_HOW})"), row)

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


# ---------------------------------------------------------------------------
# The verifier's second round on item 11 (LESSONS 210). (1) Where neither the program's own system nor SHARED held a
# real member, 'first found' chose between the estate's recovered copy and another system's real member by where their
# folders sort: a GC program took POLICY's layout when POLICY sorted before SHARED, and the recovered copy when the
# other system sorted after it, both 'FIRST FOUND'. The program's own system's recovered copy, else SHARED's, is now
# taken before another system's real member, and that is the one stand-in left in the choice. (2) A program of the
# name - a callee whose parameter copybook has its own name - was counted as the real member for every other program
# copying the name: the recovered copy gave way to the callee's source, the whole callee was expanded into the
# caller's WORKING-STORAGE ('skipped - recursive'), and atlas.recover kept the copy by its own test, so nothing ever
# changed. A program is never a copybook: it is a candidate only when no other member of the name is.

def stand_in_files(other):
    """OTHER is a system holding real members only; it sorts before SHARED or after it."""
    return {
        # the estate's recovered copy and a real member only OTHER holds, copied by a GC program and an OTHER program
        "GC/PROD.GC.SRC/SIPGM1.cbl": program("SIPGM1", "SIBOOK1", "SI-SHR-F1", "SI-OTH-F1"),
        f"{other}/{other}.SRC/SIPGM1T.cbl": program("SIPGM1T", "SIBOOK1", "SI-SHR-F1", "SI-OTH-F1"),
        "SHARED/RECOVERED-COPYBOOKS/SIBOOK1.cpy": RECOVERED_HEAD + book("SI-SHR-F1"),
        f"{other}/{other}.COPYLIB/SIBOOK1.cpy": book("SI-OTH-F1"),
        # GC's own recovered copy, the estate's and OTHER's real member: GC's own, counted beside OTHER's alone
        "GC/PROD.GC.SRC/SIPGM2.cbl": program("SIPGM2", "SIBOOK2", "SI-GC-F2", "SI-SHR-F2", "SI-OTH-F2"),
        "GC/RECOVERED-COPYBOOKS/SIBOOK2.cpy": RECOVERED_HEAD + book("SI-GC-F2"),
        "SHARED/RECOVERED-COPYBOOKS/SIBOOK2.cpy": RECOVERED_HEAD + book("SI-SHR-F2"),
        f"{other}/{other}.COPYLIB/SIBOOK2.cpy": book("SI-OTH-F2"),
        # a third system's recovered copy and OTHER's real member: the GC program takes the real member
        "GC/PROD.GC.SRC/SIPGM3.cbl": program("SIPGM3", "SIBOOK3", "SI-QX-F3", "SI-OTH-F3"),
        "QX/RECOVERED-COPYBOOKS/SIBOOK3.cpy": RECOVERED_HEAD + book("SI-QX-F3"),
        f"{other}/{other}.COPYLIB/SIBOOK3.cpy": book("SI-OTH-F3"),
        # COPY ... OF the library holding OTHER's real member: GC's own recovered copy is passed by
        "GC/PROD.GC.SRC/SIPGM4.cbl": program("SIPGM4", "SIBOOK4", "SI-GC-F4", "SI-OTH-F4").replace(
            "COPY SIBOOK4.", f"COPY SIBOOK4 OF {other}LIB."),
        "GC/RECOVERED-COPYBOOKS/SIBOOK4.cpy": RECOVERED_HEAD + book("SI-GC-F4"),
        f"{other}/{other}LIB/SIBOOK4.cpy": book("SI-OTH-F4"),
    }


class _StandIn:
    """The checks, for each OTHER (a mixin: the classes below run them)."""
    other = "APOL"

    @property
    def files(self):
        return stand_in_files(self.other)

    def check_picks(self, recovered=False):
        """`recovered`: after atlas.recover removed the copies no program expands."""
        o = self.other
        # the estate's copy for the GC program, OTHER's real member for OTHER's - whatever order the folders sort in
        self.assertEqual(self.where("GC", "SIPGM1", "SIBOOK1"), (recover.FOLDER, "SHARED/RECOVERED-COPYBOOKS/SIBOOK1.cpy"))
        self.assertEqual(self.fields_in("GC", "SIPGM1") & {"SI-SHR-F1", "SI-OTH-F1"}, {"SI-SHR-F1"})
        (row,) = self.rows_in("GC", "SIPGM1")
        self.assertTrue(row.startswith("2 copies of SIBOOK1 with different content; used "), row)
        self.assertTrue(row.endswith(f"({build.ESTATE_COPY_HOW})"), row)
        self.assertNotIn("FIRST FOUND", row)
        self.assertEqual(self.where(o, "SIPGM1T", "SIBOOK1"), (f"{o}.COPYLIB", f"{o}/{o}.COPYLIB/SIBOOK1.cpy"))
        self.assertEqual(self.rows_in(o, "SIPGM1T"), [])
        # GC's own copy; the estate's is not counted beside it
        self.assertEqual(self.where("GC", "SIPGM2", "SIBOOK2"), (recover.FOLDER, "GC/RECOVERED-COPYBOOKS/SIBOOK2.cpy"))
        self.assertEqual(self.fields_in("GC", "SIPGM2") & {"SI-GC-F2", "SI-SHR-F2", "SI-OTH-F2"}, {"SI-GC-F2"})
        (row,) = self.rows_in("GC", "SIPGM2")
        self.assertTrue(row.startswith("2 copies of SIBOOK2 with different content; used "), row)
        self.assertTrue(row.endswith("(same system)"), row)
        # QX's copy stands in for QX's programs: the GC program takes OTHER's real member, with nothing to choose
        self.assertEqual(self.where("GC", "SIPGM3", "SIBOOK3"), (f"{o}.COPYLIB", f"{o}/{o}.COPYLIB/SIBOOK3.cpy"))
        self.assertEqual(self.rows_in("GC", "SIPGM3"), [])
        # the library the COPY names, GC's own copy beside it until recover removes it - no program expands it
        self.assertEqual(self.where("GC", "SIPGM4", "SIBOOK4"), (f"{o}LIB", f"{o}/{o}LIB/SIBOOK4.cpy"))
        rows = self.rows_in("GC", "SIPGM4")
        self.assertTrue(rows == [] if recovered else rows[0].endswith("(COPY ... OF)"), rows)
        for prog in ("SIPGM1", "SIPGM2", "SIPGM3", "SIPGM4"):
            self.assertEqual(self.status_in("GC", prog), "ok", prog)

    def test_the_program_takes_its_stand_in_before_another_systems_real_member(self):
        self.check_picks()
        self.assertEqual(query._recovered_shadowing(self.conn), "")

    def test_recover_removes_the_copies_no_program_expands_and_keeps_the_rest(self):
        stats, said = self.recover_run()
        # SHARED's SIBOOK2 (the one GC program copying it has its own), QX's SIBOOK3 (no QX program copies it) and
        # GC's SIBOOK4 (its one GC program names the library holding the real member)
        self.assertEqual((stats["removed"], stats["marked"]), (3, 0), said)
        self.assertIn("  3 recovered copybook(s) removed - the estate now holds the real member: SIBOOK2, SIBOOK3, SIBOOK4",
                      said)
        self.assertIn("  " + recover.REMOVED_NONE_EXPANDS, said)
        for gone in ("SHARED/RECOVERED-COPYBOOKS/SIBOOK2.cpy", "QX/RECOVERED-COPYBOOKS/SIBOOK3.cpy",
                     "GC/RECOVERED-COPYBOOKS/SIBOOK4.cpy"):
            self.assertFalse(self.on_disk(gone), gone)
        for kept in ("SHARED/RECOVERED-COPYBOOKS/SIBOOK1.cpy", "GC/RECOVERED-COPYBOOKS/SIBOOK2.cpy"):
            self.assertTrue(self.on_disk(kept), kept)
        for _round in range(2):
            self.rebuild()
            self.check_picks(recovered=True)
            self.assertEqual(query._recovered_shadowing(self.conn), "")
            stats, said = self.recover_run()
            self.assertEqual((stats["removed"], stats["marked"]), (0, 0), said)

    def test_on_an_index_built_before_another_systems_copy_is_warned_of_and_healed(self):
        # what a build before the item could leave ('first found' where QX sorts first): the GC program expanded QX's
        # recovered copy although OTHER's real member was in the index
        rec = self.conn.execute("SELECT id FROM member WHERE name='SIBOOK3' AND system='QX'").fetchone()[0]
        self.conn.execute("UPDATE copy_use SET resolved_member_id=? WHERE copybook='SIBOOK3' AND member_id=(SELECT id "
                          "FROM member WHERE name='SIPGM3' AND kind='cobol')", (rec,))
        self.conn.commit()
        self.assertIn("**1 COPY statement(s) still expand a recovered copybook although the estate now holds the real "
                      "member** (1 copybook(s): SIBOOK3)", query._recovered_shadowing(self.conn))
        stats, said = self.recover_run()
        self.assertEqual((stats["removed"], stats["marked"]), (3, 1), said)
        self.assertEqual(self.status_in("GC", "SIPGM3"), "pending")
        self.rebuild()
        self.check_picks(recovered=True)
        self.assertEqual(query._recovered_shadowing(self.conn), "")


class TheEstatesCopyBesideASystemSortingBeforeShared(_StandIn, _OwnSystem):
    other = "APOL"


class TheEstatesCopyBesideASystemSortingAfterShared(_StandIn, _OwnSystem):
    other = "TPOL"


CALLEE = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. {name}.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-X                PIC X(5).
       LINKAGE SECTION.
       01  LK-PARM.
           COPY {name}.
       PROCEDURE DIVISION USING LK-PARM.
           MOVE {field} TO WS-X.
           GOBACK.
"""


def callee_files(where):
    """A callee whose parameter copybook has its own name, in SHARED's source library; its recovered copy under
    WHERE (the caller's system, or SHARED)."""
    return {
        "QA/QA.PROD.SRC/QAPGM20.cbl": program("QAPGM20", "QACALC20", "QA-C-F20"),
        f"{where}/RECOVERED-COPYBOOKS/QACALC20.cpy": RECOVERED_HEAD + book("QA-C-F20"),
        "SHARED/SH.PROD.SRC/QACALC20.cbl": CALLEE.format(name="QACALC20", field="QA-C-F20"),
        # a program copying its own name, and another program of its system copying that name: the estate's copy
        "QD/QD.PROD.SRC/QDPGM05.cbl": program("QDPGM05", "QDPGM05", "QD-OWN-F5"),
        "QD/QD.PROD.SRC/QDPGM5B.cbl": program("QDPGM5B", "QDPGM05", "QD-OWN-F5"),
        "SHARED/RECOVERED-COPYBOOKS/QDPGM05.cpy": RECOVERED_HEAD + book("QD-OWN-F5"),
        # no recovered copy at all: the callee in the caller's own system, its copybook under SHARED
        "GC/PROD.GC.SRC/GCPGM30.cbl": program("GCPGM30", "GCCALC30", "GC-C-F30"),
        "GC/PROD.GC.SRC/GCCALC30.cbl": CALLEE.format(name="GCCALC30", field="GC-C-F30"),
        "SHARED/SH.PROD.COPYLIB/GCCALC30.cpy": book("GC-C-F30"),
    }


class _Callee:
    """The checks, for each folder of the copy (a mixin: the classes below run them)."""
    copy_in = "QA"

    @property
    def files(self):
        return callee_files(self.copy_in)

    def check_picks(self):
        rel = f"{self.copy_in}/RECOVERED-COPYBOOKS/QACALC20.cpy"
        # the caller expands the recovered copy, never the callee's source: ok, nothing skipped, no choice
        self.assertEqual(self.where("QA", "QAPGM20", "QACALC20"), (recover.FOLDER, rel))
        self.assertIn("QA-C-F20", self.fields_in("QA", "QAPGM20"))
        self.assertEqual(self.status_in("QA", "QAPGM20"), "ok")
        self.assertEqual(self.notes("QAPGM20"), [])
        # the callee's own COPY takes it too
        self.assertEqual(self.where("SHARED", "QACALC20", "QACALC20"), (recover.FOLDER, rel))
        self.assertEqual(self.status_in("SHARED", "QACALC20"), "ok")
        # a program of the name in the caller's own system is not a copy of it either
        for prog in ("QDPGM05", "QDPGM5B"):
            self.assertEqual(self.where("QD", prog, "QDPGM05"),
                             (recover.FOLDER, "SHARED/RECOVERED-COPYBOOKS/QDPGM05.cpy"), prog)
            self.assertEqual(self.status_in("QD", prog), "ok", prog)
            self.assertEqual(self.notes(prog), [], prog)
        # no recovered copy: the copybook, not the callee in the caller's own system ('same system' took it before)
        self.assertEqual(self.where("GC", "GCPGM30", "GCCALC30"), ("SH.PROD.COPYLIB", "SHARED/SH.PROD.COPYLIB/GCCALC30.cpy"))
        self.assertEqual(self.status_in("GC", "GCPGM30"), "ok")
        self.assertEqual(self.notes("GCPGM30"), [])
        self.assertEqual(self.where("GC", "GCCALC30", "GCCALC30")[1], "SHARED/SH.PROD.COPYLIB/GCCALC30.cpy")

    def notes(self, prog):
        return [r[0] for r in self.conn.execute("SELECT u.kind || ': ' || u.detail FROM unresolved u JOIN member m "
                                                "ON m.id=u.member_id WHERE m.name=? AND m.kind='cobol' AND u.kind IN "
                                                "('expand', 'ambiguous_copybook')", (prog,))]

    def test_the_callers_expand_the_recovered_copy(self):
        self.check_picks()
        self.assertEqual(query._recovered_shadowing(self.conn), "")
        self.assertNotIn("still expand a recovered copybook", query.cmd_coverage(self.conn))

    def test_recover_keeps_it_and_nothing_loops(self):
        for _round in range(2):
            stats, said = self.recover_run()
            self.assertEqual((stats["missing"], stats["removed"], stats["marked"]), (0, 0, 0), said)
            self.assertTrue(self.on_disk(f"{self.copy_in}/RECOVERED-COPYBOOKS/QACALC20.cpy"))
            self.assertTrue(self.on_disk("SHARED/RECOVERED-COPYBOOKS/QDPGM05.cpy"))
            self.rebuild()
            self.check_picks()
            self.assertEqual(query._recovered_shadowing(self.conn), "")


class ACalleeOfTheNameWithTheCopyInTheCallersSystem(_Callee, _OwnSystem):
    copy_in = "QA"


class ACalleeOfTheNameWithTheCopyUnderShared(_Callee, _OwnSystem):
    copy_in = "SHARED"


def listing_rows(*fields):
    return [f"           05  {f:<16}PIC X(5)." for f in fields]


LISTED_FILES = {
    # QA and QA-TEST each hold QAPGM11 copying QABOOK11, each system its own recovered copy, QA-TEST's real library
    # arrived; QA's listing names the staging library QA was compiled against, which the estate does not hold
    "QA/QA.PROD.SRC/QAPGM11.cbl": program("QAPGM11", "QABOOK11", "QA-PROD-F11", "QA-TEST-F11"),
    "QA-TEST/QA-TEST.SRC/QAPGM11.cbl": program("QAPGM11", "QABOOK11", "QA-PROD-F11", "QA-TEST-F11"),
    "QA/RECOVERED-COPYBOOKS/QABOOK11.cpy": RECOVERED_HEAD + book("QA-PROD-F11"),
    "QA-TEST/RECOVERED-COPYBOOKS/QABOOK11.cpy": RECOVERED_HEAD + book("QA-TEST-F11"),
    "QA-TEST/QA-TEST.COPYLIB/QABOOK11.cpy": book("QA-TEST-F11"),
    "QA/QA.PROD.LISTING/QAPGM11.lst": ibm_listing(
        program("QAPGM11", "QABOOK11", "QA-PROD-F11", "QA-TEST-F11").splitlines(),
        {"QABOOK11": listing_rows("QA-PROD-F11")}, copy_table=[("QABOOK11", "SYSLIB", "QA.STAGE.COPYLIB")]),
    "QA-TEST/QA-TEST.LISTING/QAPGM11.lst": ibm_listing(
        program("QAPGM11", "QABOOK11", "QA-PROD-F11", "QA-TEST-F11").splitlines(),
        {"QABOOK11": listing_rows("QA-TEST-F11")}, copy_table=[("QABOOK11", "SYSLIB", "QA-TEST.COPYLIB")]),
}


class AStandInTheListingNames(_OwnSystem):
    """QA's program expands QA's recovered copy on purpose (only QA-TEST's real member has arrived), and its listing
    names QA.STAGE.COPYLIB. The check of choices said UNKNOWN with advice about a `library` row and the folder name,
    the fetch list put the library under 'chosen among several' with nothing to fetch for, and coverage advised
    removing the copy - which would give QA's program QA-TEST's layout (LESSONS 210)."""
    files = LISTED_FILES

    def setUp(self):
        super().setUp()
        self.stats, self.said = self.recover_run()
        with open(os.path.join(self.td, "work", "recover.md"), encoding="utf-8") as fh:
            self.report = fh.read()

    def qa_check(self):
        (v,) = [v for v in recover.check_choices(self.conn) if v["system"] == "QA"]
        return v

    def test_the_build_keeps_the_stand_in_and_says_the_choice(self):
        self.assertEqual(self.where("QA", "QAPGM11", "QABOOK11"), (recover.FOLDER, "QA/RECOVERED-COPYBOOKS/QABOOK11.cpy"))
        self.assertTrue(self.rows_in("QA", "QAPGM11")[0].endswith("(same system)"))
        self.assertEqual(self.stats["removed"], 1, self.said)                # QA-TEST's copy: its real member arrived
        self.assertTrue(self.on_disk("QA/RECOVERED-COPYBOOKS/QABOOK11.cpy"))

    def test_the_check_names_the_library_to_fetch(self):
        v = self.qa_check()
        self.assertEqual((v["verdict"], v["recovered"], v["named"]), ("NOT HELD", True, "QA.STAGE.COPYLIB"))
        self.assertIn("the copy the build used is a recovered copy", v["why"])
        self.assertIn("QA.STAGE.COPYLIB, a library the index does not hold - fetch it", v["why"])
        self.assertNotIn("`library` row", v["why"])
        self.assertIn("copybook choices checked against the listings: 0 confirmed, 0 contradicted by a current listing, "
                      "0 named by an older listing, 1 name a library the index does not hold, 0 unknown", self.said)
        self.assertIn("| QAPGM11 | QABOOK11 | a recovered copy (QA/RECOVERED-COPYBOOKS/QABOOK11.cpy; same system) | "
                      "QA.STAGE.COPYLIB (SYSLIB) - not a library the index holds | yes | NOT HELD | the copy the build "
                      "used is a recovered copy", self.report)
        self.assertNotIn("library dataset is not known", self.report)

    def test_the_fetch_list_has_it_to_fetch_for(self):
        self.assertIn("| QA.STAGE.COPYLIB | not fetched | recovered copy: QABOOK11 | - | 1 |", self.report)
        self.assertIn("| dataset | held? | copybooks to fetch for (missing, or expanded from a recovered copy) |", self.report)
        with open(os.path.join(self.td, "work", recover.FETCH_LIST), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "QA.STAGE.COPYLIB\n")

    def test_coverage_and_program_say_to_fetch_it_not_to_remove_the_copy(self):
        cov = query.cmd_coverage(self.conn)
        self.assertIn("| ambiguous_copybook (a recovered copy used) | 1 |", cov)
        self.assertIn("Do not remove the recovered copy by hand", cov)
        self.assertNotIn("or remove the stale copy", cov)
        self.assertIn("1 name a library the index does not hold", cov)
        mid = self.conn.execute("SELECT id FROM member WHERE name='QAPGM11' AND system='QA' AND kind='cobol'").fetchone()[0]
        says = query._listing_says(self.conn, mid)
        self.assertIn("- listing says: QABOOK11 came from QA.STAGE.COPYLIB (SYSLIB) - names QA.STAGE.COPYLIB (not a "
                      "library the index holds); the build used a recovered copy, a stand-in written from the listings: "
                      "fetch the real member from there and build - it replaces the recovered copy", says)

    def test_the_real_member_arriving_replaces_the_copy(self):
        p = os.path.join(self.root, "QA", "QA.STAGE.COPYLIB", "QABOOK11.cpy")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(book("QA-PROD-F11"))
        self.rebuild()
        self.assertEqual(self.where("QA", "QAPGM11", "QABOOK11"), ("QA.STAGE.COPYLIB", "QA/QA.STAGE.COPYLIB/QABOOK11.cpy"))
        self.assertEqual(self.qa_check()["verdict"], "CONFIRMED")
        stats, said = self.recover_run()
        self.assertEqual(stats["removed"], 1, said)
        self.assertFalse(self.on_disk("QA/RECOVERED-COPYBOOKS/QABOOK11.cpy"))
        self.assertNotIn("ambiguous_copybook (a recovered copy used)", query.cmd_coverage(self.conn))


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
        # a program with no system: SHARED's rule; GC's recovered copy stands in for GC's programs, not for it
        self.assertEqual(self.ids("", gc_rec, pol_real), [6])
        self.assertEqual(self.ids("", shr_rec, gc_rec, pol_real), [4, 6])
        self.assertEqual(self.ids("", shr_rec, none_real), [7])
        # beside another system's real member one stand-in stays: the program's own system's, else SHARED's; another
        # system's recovered copy gives way to any real member (LESSONS 210)
        self.assertEqual(self.ids("GC", gc_rec, shr_rec, pol_real), [1, 6])
        self.assertEqual(self.ids("GC", test_rec, pol_real), [6])
        self.assertEqual(self.ids("GC", test_rec, shr_rec, pol_real), [4, 6])
        self.assertEqual(self.ids("SHARED", gc_rec, shr_rec, pol_real), [4, 6])
        # recovered copies alone, or real members alone: as they are
        self.assertEqual(self.ids("GC", gc_rec, shr_rec), [1, 4])
        self.assertEqual(self.ids("GC", test_rec, gc_rec), [2, 1])
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
        # another system's copy stands in for that system's programs alone (LESSONS 210)
        self.assertTrue(recover.replaced({"POLICY"}, "QX", {"GC"}))           # a GC program takes POLICY's real member
        self.assertFalse(recover.replaced({"POLICY"}, "QX", {"GC", "QX"}))
        # the estate's copy: a program whose system holds a recovered copy of its own takes that one
        self.assertTrue(recover.replaced({"POLICY"}, "", {"GC"}, {"GC", ""}))
        self.assertFalse(recover.replaced({"POLICY"}, "", {"GC", "QX"}, {"GC", ""}))
        self.assertFalse(recover.replaced({"POLICY"}, "", {""}, {"GC", ""}))   # a SHARED program takes SHARED's copy
        self.assertFalse(recover.replaced(set(), "", {"GC"}, {"GC"}))         # no real member anywhere

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
            CREATE TABLE member(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, path TEXT, library TEXT, system TEXT,
                                parse_status TEXT);
            CREATE TABLE copy_use(member_id INTEGER, copybook TEXT, resolved_member_id INTEGER, of_library TEXT);""")
        e = os.path.join(os.sep + "e", "estate")
        conn.executemany("INSERT INTO member VALUES(?,?,?,?,?,?,?)", [
            (1, "BK", "copybook", os.path.join(e, "GC-TEST", "TEST.GC.COPYLIB", "BK.cpy"), "TEST.GC.COPYLIB", "GC-TEST", "ok"),
            (2, "BK", "copybook", os.path.join(e, "GC", recover.FOLDER, "BK.cpy"), recover.FOLDER, "GC", "ok"),
            (3, "SH", "copybook", os.path.join(e, "SHARED", "COPYLIB", "SH.cpy"), "COPYLIB", "SHARED", "ok"),
            (4, "NO", "copybook", os.path.join(e, "COPYLIB", "NO.cpy"), "COPYLIB", None, "ok"),
            (5, "SELF", "cobol", os.path.join(e, "GC", "SRC", "SELF.cbl"), "SRC", "GC", "ok"),
            (6, "LOOP", "copybook", os.path.join(e, "GC", "COPYLIB", "LOOP.cpy"), "COPYLIB", "GC", "ok"),
            (7, "PGM", "cobol", os.path.join(e, "POLICY", "SRC", "PGM.cbl"), "SRC", "POLICY", "partial"),
            (8, "BK", "stub", os.path.join(e, "GC", "COPYLIB", "BK.cpy"), "COPYLIB", "GC", "skipped"),
            # a copybook filed in a source library: kind cobol, 'skipped' as not a program - a real member
            (9, "INSRC", "cobol", os.path.join(e, "QA", "SRC", "INSRC.cbl"), "SRC", "QA", "skipped"),
            # a program not parsed to the end is taken for a program
            (10, "HALF", "cobol", os.path.join(e, "QA", "SRC", "HALF.cbl"), "SRC", "QA", "failed"),
            (11, "WAIT", "cobol", os.path.join(e, "QA", "SRC", "WAIT.cbl"), "SRC", "QA", "pending"),
            # COPY BK OF the library holding the real member; OF a library holding none; one of each
            (12, "OFPGM1", "cobol", os.path.join(e, "QB", "SRC", "OFPGM1.cbl"), "SRC", "QB", "ok"),
            (13, "OFPGM2", "cobol", os.path.join(e, "QC", "SRC", "OFPGM2.cbl"), "SRC", "QC", "ok"),
            (14, "OFPGM3", "cobol", os.path.join(e, "QD", "SRC", "OFPGM3.cbl"), "SRC", "QD", "ok")])
        conn.executemany("INSERT INTO copy_use VALUES(?,?,?,?)", [
            (5, "self", 2, None), (6, "LOOP", None, None), (7, "BK", 1, None), (5, "BK", 2, None),
            (12, "BK", 1, "test.gc.copylib"), (13, "BK", 2, "NOLIB"), (14, "BK", 1, "TEST.GC.COPYLIB"), (14, "BK", 2, None)])
        real = recover.real_copies(conn, [os.path.join(e, "SHARED", recover.FOLDER)])
        # a program is never the real member of its name - SELF, which copies its own name, and PGM, which does not
        # (LESSONS 209, 210); LOOP, a copybook copying itself, is (LESSONS 185); a stub never is
        self.assertEqual(real, {"BK": {"GC-TEST"}, "SH": {""}, "NO": {""}, "LOOP": {"GC"}, "INSRC": {"QA"}})
        # OFPGM1 names the library holding the real member for its one COPY BK: it never takes a recovered copy
        # (LESSONS 210); OFPGM2 names a library holding none, OFPGM3 copies BK a second time without OF
        self.assertEqual(recover.copier_homes(conn, ["bk", "SELF", "LOOP", "NONE"]),
                         {"BK": {"GC", "POLICY", "QC", "QD"}, "SELF": {"GC"}})


if __name__ == "__main__":
    unittest.main()
