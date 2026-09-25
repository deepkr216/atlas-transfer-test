"""
A copybook chosen among several same-named ones is not a partial parse.

At his site (2026-09-24): coverage said 701 COBOL programs "parsed only in
part" while only ~60 copybooks were missing. The resolver's note ("N copies
of X with different content; used PATH (how)") comes back through the
expander as a COPY warning, and build.py stores every expander warning as an
'expand' note and marks the member partial - the same word as a program
whose copybook is missing. The build cannot change without a night's
re-parse (ROADMAP item 18), so the query side tells the two apart.
"""

import contextlib
import io
import os
import shutil
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


class ChosenCopybookIsNotPartial(unittest.TestCase):
    """estate\\SYSTEM\\LIBRARY\\member: the folder names the system, so the
    CLAIMS program takes the CLAIMS copy of DUPREC over POLICY's (same
    system) - a choice, recorded, and complete."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.root = os.path.join(cls.td, "estate")
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
            p = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([cls.root, "--db", cls.db, "--rebuild", "--quiet"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def setUp(self):
        self.conn = query.connect(self.db)

    def tearDown(self):
        self.conn.close()

    def member(self, name):
        return self.conn.execute("SELECT id, parse_status FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()

    def test_the_build_is_unchanged_the_chosen_program_is_marked_partial(self):
        mid, status = self.member("CHOSEN")
        self.assertEqual(status, "partial")           # build.py unchanged: the resolver's note is an 'expand' note
        notes = [r[0] for r in self.conn.execute("SELECT detail FROM unresolved WHERE member_id=? AND kind='expand'", (mid,))]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("COPY DUPREC: 2 copies of DUPREC with different content; used ", notes[0])
        self.assertIn("(same system)", notes[0])
        self.assertIn(query.AMBIGUOUS_PICK, notes[0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM unresolved WHERE member_id=? AND kind='ambiguous_copybook'",
                                           (mid,)).fetchone()[0], 1)
        self.assertEqual(self.member("MISSPGM")[1], "partial")
        self.assertEqual(self.member("PLAINPGM")[1], "ok")
        # no listing table (recover never ran on this index): the chain decided, and the build did not create the table
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_copy_source'").fetchone())

    def test_partial_kind_tells_the_two_apart(self):
        self.assertEqual(query.partial_kind(self.conn, self.member("CHOSEN")[0]), "chosen")
        self.assertEqual(query.partial_kind(self.conn, self.member("MISSPGM")[0]), "partial")
        self.assertIsNone(query.partial_kind(self.conn, self.member("PLAINPGM")[0]))
        self.assertFalse(query.is_truly_partial(self.conn, self.member("CHOSEN")[0]))
        self.assertTrue(query.is_truly_partial(self.conn, self.member("MISSPGM")[0]))
        self.assertFalse(query.is_truly_partial(self.conn, self.member("PLAINPGM")[0]))
        self.assertEqual(query.parse_label(self.conn, self.member("CHOSEN")[0], "partial"), query.PARSE_CHOSEN)
        self.assertEqual(query.parse_label(self.conn, self.member("MISSPGM")[0], "partial"), "partial")
        self.assertEqual(query.parse_label(self.conn, self.member("PLAINPGM")[0], "ok"), "ok")

    def test_coverage_lists_the_chosen_program_in_its_own_table(self):
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

    def test_the_counts_add_up(self):
        cov = query.cmd_coverage(self.conn)
        old_partial = self.conn.execute("SELECT COUNT(*) FROM member WHERE parse_status='partial'").fetchone()[0]
        self.assertEqual(old_partial, 2)
        self.assertIn("| cobol | partial | 2 |", cov)           # the index's own count, as stored
        self.assertIn("- of the 2 members marked `partial`, **1 is parsed only in part** and "
                      "**1 is complete with a copybook chosen among several**", cov)
        self.assertIn("### Members parsed only in part (1)", cov)
        self.assertIn("### Complete, with a copybook chosen among several: 1 member -", cov)
        # the unresolved-by-kind table counts the choice apart from a missing copybook
        self.assertIn("| expand | 1 | a COPY statement whose copybook is not in the index", cov)
        self.assertIn("| expand (copybook chosen among several) | 2 | the resolver's own choice", cov)

    def test_coverage_all_lists_every_member(self):
        cov = query.cmd_coverage(self.conn, everything=True)
        self.assertIn("| cobol | CHOSEN |", cov)
        self.assertIn("| cobol | MISSPGM |", cov)

    def test_program_header_says_complete_with_the_choice_and_keeps_the_note(self):
        out = query.cmd_program(self.conn, "CHOSEN")
        self.assertIn("parse: complete (copybook chosen among several - see notes)", out)
        self.assertNotIn("parse: partial", out)
        self.assertIn("ambiguous_copybook", out)                 # the note itself stays
        self.assertIn("2 copies of DUPREC with different content; used", out)
        # the 'expand' repeat of the same note is not listed a second time as a gap
        scope = out.split("### Unresolved in scope")[1]
        self.assertIn("(1)", scope.splitlines()[0])
        self.assertNotIn("| expand |", scope)
        miss = query.cmd_program(self.conn, "MISSPGM")
        self.assertIn("parse: partial", miss)
        self.assertIn("COPY NOPE NOT FOUND", miss)
        self.assertIn("| expand |", miss)
        self.assertIn("parse: ok", query.cmd_program(self.conn, "PLAINPGM"))

    def test_pack_header_carries_the_same_wording(self):
        out = query.cmd_pack(self.conn, "CHOSEN")
        self.assertIn("parse: complete (copybook chosen among several - see notes)", out)
        self.assertIn("parse: partial", query.cmd_pack(self.conn, "MISSPGM"))

    def test_flow_marks_only_the_truly_partial_program(self):
        chosen = query.cmd_flow(self.conn, "DUP-FIELD-A", program="CHOSEN")
        self.assertIn("WS-OUT", chosen)
        self.assertNotIn("[program partial:", chosen)
        miss = query.cmd_flow(self.conn, "DUP-FIELD-A", program="MISSPGM")
        self.assertIn("[program partial:", miss)
        self.assertIn("COPY NOPE NOT FOUND", miss)
        self.assertNotIn("different content", miss.split("[program partial:")[1].split("]")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
