"""
Value-domain change scenario (new gender code, relationship codes merged):
the queries must inventory every place a VALUE is assumed, including the
undocumented one, the 88-level tests, the SQL predicates, the cross-field
rules and the multi-line message literals. Runs end-to-end through build.py.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class ValueDomainScenario(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        cls.db = os.path.join(cls.td, "t.db")
        build._main([FIX, "--db", cls.db, "--rebuild", "--quiet"])
        cls.conn = query.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.td, ignore_errors=True)

    def test_values_finds_the_undocumented_relationship_5(self):
        out = query.cmd_values(self.conn, "WS-REL-CD")
        self.assertRegex(out, r"NOT DOCUMENTED BY ANY 88-LEVEL:\*\* [^\n]*\b5\b")
        self.assertIn("WS-REL-SON", out)                 # 3 documented by its 88
        self.assertIn("MEMBRVAL:21", out)                # WHEN 3
        self.assertIn("MEMBRVAL:33", out)                # MOVE 2 TO WS-REL-CD
        # value 1 is tested only in CONDLOGX: uses aggregate across programs
        self.assertIn("| 1 | WS-REL-MAIN | 0 | 1 |", out)
        self.assertIn("CONDLOGX:29", out)

    def test_values_counts_tests_through_88_names_and_sql_columns(self):
        out = query.cmd_values(self.conn, "WS-GENDER-CD")
        self.assertIn("via 88 WS-GENDER-MALE", out)       # IF NOT WS-GENDER-MALE never mentions 'M'
        self.assertIn("MEMBRVAL:27", out)                 # WS-GENDER-CD NOT = 'F'
        self.assertIn("MEMBRVAL:37", out)                 # = 'U'
        self.assertIn("GENDER_CD", out)                   # SQL column inferred from the field name

    def test_sql_predicate_literals_are_indexed(self):
        rows = {tuple(r) for r in self.conn.execute(
            "SELECT literal, context, field FROM literal_ref WHERE context LIKE 'sql_%'")}
        self.assertIn(("3", "sql_predicate", "REL_CD"), rows)
        self.assertIn(("M", "sql_predicate", "GENDER_CD"), rows)
        self.assertIn(("F", "sql_predicate", "GENDER_CD"), rows)

    def test_pair_finds_every_cross_field_rule(self):
        out = query.cmd_pair(self.conn, "WS-REL-CD", "WS-GENDER-CD")
        for ln in ("MEMBRVAL:20", "MEMBRVAL:22", "MEMBRVAL:27", "MEMBRVAL:37"):
            self.assertIn(ln, out)
        self.assertIn("WS-GENDER-MALE test", out)

    def test_messages_finds_the_multi_line_message(self):
        out = query.cmd_messages(self.conn, "GENDER")
        self.assertIn("GENDER MUST BE MALE FOR SON", out)
        self.assertIn("GENDER MUST BE FEMALE FOR DAUGHTER", out)
        self.assertIn("MEMBRVAL:29", out)                 # the MOVE that spans lines 29-30
        self.assertIn("-> WS-ERR-MSG", out)

    def test_literal_field_filter_removes_noise(self):
        out = query.cmd_literal(self.conn, "3", field="WS-REL-CD")
        self.assertIn("MEMBRVAL:21", out)
        self.assertNotIn("SAMPPGM", out)
        wide = query.cmd_literal(self.conn, "E10", like=True)
        self.assertIn("E101", wide)
        self.assertIn("E103", wide)


if __name__ == "__main__":
    unittest.main(verbosity=2)
