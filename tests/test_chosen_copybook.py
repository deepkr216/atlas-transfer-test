"""
A copybook chosen among several same-named ones is not a partial parse.

At his site (2026-09-24): coverage said 701 COBOL programs "parsed only in
part" while only ~60 copybooks were missing. The resolver's note ("N copies
of X with different content; used PATH (how)") came back through the
expander as a COPY warning, and build.py stored every expander warning as an
'expand' note and marked the member partial - the same word as a program
whose copybook is missing. Since ROADMAP re-parse item 18 the resolver
records the choice once, as the 'ambiguous_copybook' row, and hands the
expander no note: the program is `ok`. An index built before the batch still
carries the 'expand' repeat and the `partial` word - the query side tells the
two apart there (partial_kind / is_truly_partial, LESSONS 181), and that
shape is written by hand below so the stand-in stays pinned.
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

from atlas import build, query  # noqa: E402

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


def program(name, *copybooks):
    return PROGRAM.format(name=name, copies="\n".join(f"           COPY {c}." for c in copybooks))


def make_estate(td):
    """estate\\SYSTEM\\LIBRARY\\member: the folder names the system, so the
    CLAIMS program takes the CLAIMS copy of DUPREC over POLICY's (same
    system) - a choice, recorded, and complete. Returns (root, db) with the
    index built."""
    root = os.path.join(td, "estate")
    files = {
        # expands completely: DUPREC exists twice with different content, CLAIMS' copy is chosen
        "CLAIMS/PROD.CLAIMS.SRC/CHOSEN.cbl": program("CHOSEN", "DUPREC"),
        # truly partial: NOPE is in no library - and DUPREC is chosen as well
        "CLAIMS/PROD.CLAIMS.SRC/MISSPGM.cbl": program("MISSPGM", "DUPREC", "NOPE"),
        # complete: DUPONE exists once
        "CLAIMS/PROD.CLAIMS.SRC/PLAINPGM.cbl": program("PLAINPGM", "DUPONE"),
        "CLAIMS/PROD.CLAIMS.COPYLIB/DUPREC.cpy": "           05  DUP-FIELD-A   PIC X(5).\n",
        "CLAIMS/PROD.CLAIMS.COPYLIB/DUPONE.cpy": "           05  DUP-FIELD-A   PIC X(5).\n",
        "POLICY/PROD.POLICY.COPYLIB/DUPREC.cpy": "           05  DUP-FIELD-B   PIC X(9).\n",
    }
    for rel, text in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    db = os.path.join(td, "t.db")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = build._main([root, "--db", db, "--rebuild", "--quiet"])
    assert rc == 0
    return root, db


def age_index(db):
    """Give the index the shape a build before ROADMAP re-parse item 18 left:
    every 'ambiguous_copybook' row repeated as an 'expand' note in the
    expander's words ("L7: COPY DUPREC: 2 copies of DUPREC with different
    content; used PATH (same system)") and the member marked `partial`. What
    his index holds until the re-parse night."""
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT u.member_id, u.detail FROM unresolved u JOIN member m ON m.id = u.member_id "
                            "WHERE u.kind = 'ambiguous_copybook' AND m.kind = 'cobol'").fetchall()
        assert rows, "the estate must hold a chosen copybook to age"
        for mid, detail in rows:
            book = detail.split(" copies of ")[1].split(" with different content")[0]
            line = conn.execute("SELECT line FROM copy_use WHERE member_id=? AND UPPER(copybook)=? LIMIT 1",
                                (mid, book.upper())).fetchone()
            conn.execute("INSERT INTO unresolved(member_id, kind, detail, line) VALUES(?,?,?,NULL)",
                         (mid, "expand", f"L{line[0] if line and line[0] else 0}: COPY {book}: {detail}"))
            conn.execute("UPDATE member SET parse_status='partial' WHERE id=?", (mid,))
        conn.commit()
    finally:
        conn.close()


class _Estate(unittest.TestCase):
    aged = False

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root, cls.db = make_estate(cls.td)
        if cls.aged:
            age_index(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def member(self, name):
        return self.conn.execute("SELECT id, parse_status FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()

    def rows(self, name, kind):
        return [r[0] for r in self.conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind=? ORDER BY id",
                                                (self.member(name)[0], kind))]

    # the assertions that hold on BOTH shapes of the index: the same words on the same facts
    def check_partial_kind_tells_the_two_apart(self):
        self.assertEqual(query.partial_kind(self.conn, self.member("MISSPGM")[0]), "partial")
        self.assertIsNone(query.partial_kind(self.conn, self.member("PLAINPGM")[0]))
        self.assertFalse(query.is_truly_partial(self.conn, self.member("CHOSEN")[0]))
        self.assertTrue(query.is_truly_partial(self.conn, self.member("MISSPGM")[0]))
        self.assertFalse(query.is_truly_partial(self.conn, self.member("PLAINPGM")[0]))
        self.assertTrue(query.chose_a_copybook(self.conn, self.member("CHOSEN")[0]))
        self.assertTrue(query.chose_a_copybook(self.conn, self.member("MISSPGM")[0]))
        self.assertFalse(query.chose_a_copybook(self.conn, self.member("PLAINPGM")[0]))
        chosen_id, chosen_status = self.member("CHOSEN")
        self.assertEqual(query.parse_label(self.conn, chosen_id, chosen_status), query.PARSE_CHOSEN)
        self.assertEqual(query.parse_label(self.conn, self.member("MISSPGM")[0], "partial"), "partial")
        self.assertEqual(query.parse_label(self.conn, self.member("PLAINPGM")[0], "ok"), "ok")

    def check_coverage_tables(self):
        cov = query.cmd_coverage(self.conn)
        partial = cov.split("### Members parsed only in part")[1].split("\n###")[0]
        chosen = cov.split("### Complete, with a copybook chosen among several")[1].split("\n###")[0]
        self.assertIn("### Members parsed only in part (1)", cov)
        self.assertIn("| cobol | 1 | expand (1) |", partial)
        self.assertNotIn("CHOSEN", partial)
        miss = [ln for ln in partial.splitlines() if ln.startswith("| cobol | MISSPGM |")]
        self.assertEqual(len(miss), 1, partial)
        self.assertIn("COPY NOPE NOT FOUND", miss[0])          # the reason that MADE it partial, not the choice
        self.assertNotIn("different content", miss[0])
        self.assertTrue(chosen.startswith(": 1 member - the build picked by system and library order; "
                                          "the 'ambiguous_copybook' rows name the copy used"), chosen[:120])
        self.assertIn("| cobol | 1 | DUPREC (1) |", chosen)
        row = [ln for ln in chosen.splitlines() if ln.startswith("| cobol | CHOSEN |")]
        self.assertEqual(len(row), 1, chosen)
        self.assertIn("DUPREC: 2 copies, used ", row[0])
        self.assertIn("PROD.CLAIMS.COPYLIB", row[0])
        self.assertIn("(same system)", row[0])
        self.assertNotIn("MISSPGM", chosen)
        self.assertNotIn("PLAINPGM", chosen)
        self.assertIn("NOT parsed only in part", chosen)
        self.assertIn("| expand | 1 | a COPY statement whose copybook is not in the index", cov)
        self.assertIn("| ambiguous_copybook | 2 | two copies of one copybook with different content; one was chosen", cov)
        return cov, chosen

    def check_program_pack_and_flow(self):
        out = query.cmd_program(self.conn, "CHOSEN")
        self.assertIn("parse: complete (copybook chosen among several - see notes)", out)
        self.assertNotIn("parse: partial", out)
        self.assertNotIn("parse: ok", out)
        self.assertIn("ambiguous_copybook", out)                 # the choice itself is printed
        self.assertIn("2 copies of DUPREC with different content; used", out)
        scope = out.split("### Unresolved in scope")[1]
        self.assertIn("(1)", scope.splitlines()[0])              # the choice once, never a second time as a gap
        self.assertNotIn("| expand |", scope)
        miss = query.cmd_program(self.conn, "MISSPGM")
        self.assertIn("parse: partial", miss)
        self.assertIn("COPY NOPE NOT FOUND", miss)
        self.assertIn("| expand |", miss)
        self.assertIn("parse: ok", query.cmd_program(self.conn, "PLAINPGM"))
        self.assertIn("parse: complete (copybook chosen among several - see notes)", query.cmd_pack(self.conn, "CHOSEN"))
        self.assertIn("parse: partial", query.cmd_pack(self.conn, "MISSPGM"))
        chosen = query.cmd_flow(self.conn, "DUP-FIELD-A", program="CHOSEN")
        self.assertIn("WS-OUT", chosen)
        self.assertNotIn("[program partial:", chosen)
        miss = query.cmd_flow(self.conn, "DUP-FIELD-A", program="MISSPGM")
        self.assertIn("[program partial:", miss)
        self.assertIn("COPY NOPE NOT FOUND", miss)
        self.assertNotIn("different content", miss.split("[program partial:")[1].split("]")[0])


class ChosenCopybookIsNotPartial(_Estate):
    """The build of this batch: the choice is one 'ambiguous_copybook' row,
    the program is `ok`; a missing copybook still makes a program partial."""

    def test_the_choice_is_recorded_once_and_the_program_is_ok(self):
        mid, status = self.member("CHOSEN")
        self.assertEqual(status, "ok")
        self.assertEqual(self.rows("CHOSEN", "expand"), [])           # no COPY warning repeats the choice
        picks = self.rows("CHOSEN", "ambiguous_copybook")
        self.assertEqual(len(picks), 1, picks)
        used = os.path.join(self.root, "CLAIMS", "PROD.CLAIMS.COPYLIB", "DUPREC.cpy")
        self.assertEqual(picks[0], f"2 copies of DUPREC with different content; used {used} (same system)")
        self.assertIn(query.AMBIGUOUS_PICK, picks[0])
        # a missing copybook is still a gap: the program is partial for THAT, with its choice recorded beside it
        self.assertEqual(self.member("MISSPGM")[1], "partial")
        self.assertEqual([n.split(" - ")[0] for n in self.rows("MISSPGM", "expand")], ["L8: COPY NOPE NOT FOUND"])
        self.assertEqual(len(self.rows("MISSPGM", "ambiguous_copybook")), 1)
        self.assertEqual(self.member("PLAINPGM")[1], "ok")
        self.assertEqual(self.rows("PLAINPGM", "ambiguous_copybook") + self.rows("PLAINPGM", "expand"), [])
        # no listing table (recover never ran on this index): the chain decided, and the build did not create the table
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_copy_source'").fetchone())

    def test_partial_kind_tells_the_two_apart(self):
        self.assertIsNone(query.partial_kind(self.conn, self.member("CHOSEN")[0]))      # not marked partial at all
        self.check_partial_kind_tells_the_two_apart()

    def test_coverage_lists_the_chosen_program_in_its_own_table(self):
        cov, chosen = self.check_coverage_tables()
        self.assertIn("| cobol | partial | 1 |", cov)            # the index's own count: MISSPGM only
        self.assertIn("| cobol | ok | 2 |", cov)
        self.assertIn("- 1 member marked `ok` took a copybook chosen among several same-named ones: complete for the "
                      "copy named, and listed in a table of its own below.", cov)
        self.assertNotIn("members marked `partial`, **", cov)
        self.assertNotIn("a copybook was chosen among several same-named ones: complete, its own table", cov)
        self.assertIn("They are marked `ok`: the build records the choice once, as the 'ambiguous_copybook' row "
                      "(ROADMAP re-parse item 18).", chosen)
        self.assertNotIn("`partial`", chosen)
        self.assertNotIn("expand (copybook chosen among several)", cov)     # no such row exists to count

    def test_coverage_all_lists_every_member(self):
        cov = query.cmd_coverage(self.conn, everything=True)
        self.assertIn("| cobol | CHOSEN |", cov)
        self.assertIn("| cobol | MISSPGM |", cov)

    def test_program_pack_and_flow_say_the_same_as_before_the_batch(self):
        self.check_program_pack_and_flow()


class AnIndexBuiltBeforeTheBatch(_Estate):
    """The shape his index has until the re-parse night: the choice repeated
    as an 'expand' note and the member `partial`. The query side's stand-in
    (LESSONS 181) keeps telling the two apart, with the same words."""
    aged = True

    def test_the_aged_index_has_the_earlier_shape(self):
        mid, status = self.member("CHOSEN")
        self.assertEqual(status, "partial")
        notes = self.rows("CHOSEN", "expand")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("COPY DUPREC: 2 copies of DUPREC with different content; used ", notes[0])
        self.assertIn("(same system)", notes[0])
        self.assertIn(query.AMBIGUOUS_PICK, notes[0])
        self.assertEqual(len(self.rows("CHOSEN", "ambiguous_copybook")), 1)
        self.assertEqual(self.member("MISSPGM")[1], "partial")
        self.assertEqual(len(self.rows("MISSPGM", "expand")), 2)
        self.assertEqual(self.member("PLAINPGM")[1], "ok")

    def test_partial_kind_tells_the_two_apart(self):
        self.assertEqual(query.partial_kind(self.conn, self.member("CHOSEN")[0]), "chosen")
        self.check_partial_kind_tells_the_two_apart()

    def test_coverage_lists_the_chosen_program_in_its_own_table(self):
        cov, chosen = self.check_coverage_tables()
        self.assertIn("The index marks them `partial` only because the build that made it repeated the resolver's "
                      "choice as a COPY warning - an index built before ROADMAP re-parse item 18; the next full "
                      "re-parse marks them `ok`.", chosen)
        self.assertNotIn("They are marked `ok`", chosen)

    def test_the_counts_add_up(self):
        cov = query.cmd_coverage(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM member WHERE parse_status='partial'").fetchone()[0], 2)
        self.assertIn("| cobol | partial | 2 |", cov)           # the index's own count, as stored
        self.assertIn("- of the 2 members marked `partial`, **1 is parsed only in part** and "
                      "**1 is complete with a copybook chosen among several**", cov)
        self.assertIn("a copybook was chosen among several same-named ones: complete, its own table", cov)
        self.assertNotIn("marked `ok` took a copybook chosen among several", cov)
        self.assertIn("### Members parsed only in part (1)", cov)
        self.assertIn("### Complete, with a copybook chosen among several: 1 member -", cov)
        # the unresolved-by-kind table counts the repeat apart from a missing copybook
        self.assertIn("| expand (copybook chosen among several) | 2 | the resolver's own choice", cov)

    def test_coverage_all_lists_every_member(self):
        cov = query.cmd_coverage(self.conn, everything=True)
        self.assertIn("| cobol | CHOSEN |", cov)
        self.assertIn("| cobol | MISSPGM |", cov)

    def test_program_pack_and_flow_keep_their_words(self):
        self.check_program_pack_and_flow()


if __name__ == "__main__":
    unittest.main(verbosity=2)
