"""
DB2 column-level lineage (SELECT INTO / cursor FETCH / INSERT / UPDATE /
predicates) and run-time message templates built by STRING and DISPLAY.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, cobol, query, reader  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


class ColumnLineage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        text, data, enc = reader.load(os.path.join(FIX, "SQLCOLS.cbl"))
        cls.f = cobol.parse_program(text, data, enc)
        cls.cols = {(t, c, h, m, s) for (t, c, h, m, s, _ln) in cls.f.sql_cols}

    def test_select_into_pairs_positionally(self):
        self.assertIn(("POLICY_TBL", "PREM_AMT", "WS-PREM", "read", "SELECT"), self.cols)
        self.assertIn(("POLICY_TBL", "STATUS_CD", "WS-STATUS", "read", "SELECT"), self.cols)
        self.assertIn(("POLICY_TBL", "POL_NO", "WS-POL-NO", "predicate", "SELECT"), self.cols)

    def test_cursor_fetch_pairs_with_declare_select_list(self):
        self.assertIn("POLCUR", self.f.cursors)
        self.assertIn(("POLICY_TBL", "POL_NO", "WS-POL-NO", "read", "FETCH"), self.cols)
        self.assertIn(("POLICY_TBL", "STATUS_CD", "WS-STATUS", "read", "FETCH"), self.cols)
        self.assertIn(("POLICY_TBL", "STATUS_CD", "WS-STATUS", "predicate", "DECLARE"), self.cols)   # alias P resolved

    def test_update_and_insert_are_writes(self):
        self.assertIn(("MEMBER_TBL", "GENDER_CD", "WS-GENDER-CD", "write", "UPDATE"), self.cols)
        self.assertIn(("MEMBER_TBL", "MEMBER_ID", "WS-MEMBER-ID", "predicate", "UPDATE"), self.cols)
        self.assertIn(("AUDIT_TBL", "MEMBER_ID", "WS-MEMBER-ID", "write", "INSERT"), self.cols)
        self.assertIn(("AUDIT_TBL", "GENDER_CD", "WS-GENDER-CD", "write", "INSERT"), self.cols)

    def test_string_and_display_templates(self):
        lits = {(l, c, f) for (l, c, f, _ln) in self.f.literal_refs}
        self.assertIn(("INVALID GENDER <WS-GENDER-CD> FOR MEMBER <WS-MEMBER-ID>", "string_group", "WS-ERR-MSG"), lits)
        self.assertIn(("AUDIT <WS-MEMBER-ID> STATUS <WS-STATUS>", "display_group", None), lits)
        # the pieces are still there for a plain literal search
        self.assertIn(("INVALID GENDER ", "string", "WS-ERR-MSG"), lits)


class ColumnQueries(unittest.TestCase):

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

    def test_column_report(self):
        out = query.cmd_column(self.conn, "MEMBER_TBL.GENDER_CD")
        self.assertIn("Written (INSERT / UPDATE", out)
        self.assertIn("SQLCOLS", out)
        self.assertIn("WS-GENDER-CD", out)
        self.assertIn("SQLCOLS:28", out)                 # the EXEC SQL statement's first line
        bare = query.cmd_column(self.conn, "STATUS_CD")
        self.assertIn("Read (SELECT INTO / FETCH INTO", bare)
        self.assertIn("FETCH", bare)
        self.assertIn("display DISPLAY", bare)          # WS-STATUS is displayed afterwards
        self.assertIn("No static SQL references", query.cmd_column(self.conn, "NO_SUCH_COL"))

    def test_field_shows_db2_columns(self):
        out = query.cmd_field(self.conn, "WS-GENDER-CD")
        self.assertIn("DB2 columns", out)
        self.assertIn("| MEMBER_TBL | GENDER_CD | write | UPDATE |", out)

    def test_messages_finds_runtime_templates(self):
        out = query.cmd_messages(self.conn, "GENDER")
        self.assertIn("INVALID GENDER <WS-GENDER-CD> FOR MEMBER <WS-MEMBER-ID>", out)
        self.assertIn("string_group -> WS-ERR-MSG", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
