"""
Multi-line handling regression tests.

Every case here is something that produced a WRONG fact at some point. The
rule for this file: when a parsing mistake is found in real source, a fixture
line and an assertion land here before the fix does, so the mistake cannot
come back. Run with:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import cobol, copybook, reader  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _load(name):
    return reader.load(os.path.join(FIX, name))


class MultiLineCobol(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        text, data, enc = _load("MULTILN.cbl")
        cls.facts = cobol.parse_program(text, data, enc)
        lines, _ = reader.read_cobol_lines(text, data=data, enc=enc)
        cls.logical = reader.join_cobol_continuations(lines)
        cls.stmts = list(reader.cobol_statements(cls.logical))
        cls.roots, cls.warns = copybook.parse_data_division(cls.logical)
        cls.fields = {f.name: f for f in copybook.flatten(cls.roots)}

    # ---- statement assembly ----------------------------------------------

    def test_program_id_on_following_line(self):
        self.assertEqual(self.facts.program_id, "MULTILN")

    def test_definition_split_over_three_lines(self):
        f = self.fields["WS-SPLIT-DEFINITION"]
        self.assertEqual(f.pic, "X(10)")
        self.assertEqual(f.length, 10)
        self.assertEqual(f.value_lit, "'ABC'")

    def test_pic_with_embedded_period_is_not_split(self):
        self.assertEqual(self.fields["WS-EDITED-AMOUNT"].pic, "ZZZ,ZZ9.99")
        self.assertEqual(self.fields["WS-DOTTED-DATE"].pic, "99.99.99")
        self.assertEqual(self.fields["WS-DOTTED-DATE"].length, 8)

    def test_numeric_literal_with_decimal_point(self):
        self.assertEqual(self.fields["WS-RATE"].value_lit, "1.5")

    def test_period_inside_literal_does_not_terminate(self):
        self.assertEqual(self.fields["WS-END-FLAG"].value_lit, "'END.'")
        moves = [s for s in self.stmts if "MOVE 'END.'" in s.text]
        self.assertEqual(len(moves), 1)
        self.assertTrue(moves[0].text.endswith("WS-END-FLAG."))

    def test_88_value_list_across_lines(self):
        conds = self.fields["WS-STATUS"].conds
        self.assertEqual(len(conds), 1)
        name, values, _ = conds[0]
        self.assertEqual(name, "WS-OK")
        self.assertEqual(values, ["'OK'", "'AA'", "'BB'"])

    def test_occurs_depending_on_across_lines(self):
        f = self.fields["WS-ENTRY"]
        self.assertEqual(f.occurs_max, 50)
        self.assertEqual(f.odo_on, "WS-COUNT")
        self.assertTrue(any("VARIABLE length" in w for w in self.warns))

    def test_pic_and_usage_on_separate_lines(self):
        f = self.fields["WS-ENTRY-AMT"]
        self.assertEqual(f.pic, "S9(7)V99")
        self.assertEqual(f.usage, "COMP-3")
        self.assertEqual(f.length, 5)          # 9 digits packed -> 5 bytes

    def test_column7_hyphen_literal_continuation(self):
        f = self.fields["WS-MSG"]
        self.assertIn("SPLIT ACROSS TWO PHYSICAL LINES", f.value_lit)
        self.assertNotIn("ACR'", f.value_lit)

    # ---- procedure division ----------------------------------------------

    def test_call_using_list_spanning_lines_with_comment_inside(self):
        calls = [c for c in self.facts.calls if c.target == "SUBPGM1"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].using_args,
                         ["WS-SPLIT-DEFINITION", "WS-STATUS", "WS-COUNT"])
        self.assertNotIn("WS-OLD-ARGUMENT-COMMENTED-OUT", calls[0].using_args)

    def test_perform_thru_split_over_two_lines(self):
        self.assertIn(("0000-MAIN", "1000-PROCESS", "1000-EXIT", 40, "perform"), self.facts.performs)

    def test_paragraph_header_and_statement_on_same_line(self):
        names = [p.name for p in self.facts.paragraphs]
        self.assertIn("1000-EXIT", names)
        self.assertNotIn("EXIT", names)
        self.assertEqual(names, ["0000-MAIN", "1000-PROCESS", "1000-EXIT",
                                 "2000-NEVER-PERFORMED"])

    def test_paragraph_line_ranges_exact(self):
        by = {p.name: p for p in self.facts.paragraphs}
        self.assertEqual((by["1000-PROCESS"].start_line, by["1000-PROCESS"].end_line), (48, 49))
        self.assertEqual(by["1000-EXIT"].start_line, 50)

    def test_if_else_block_is_one_statement(self):
        ifs = [s for s in self.stmts if s.text.startswith("IF WS-COUNT")]
        self.assertEqual(len(ifs), 1)
        self.assertTrue(ifs[0].text.endswith("END-IF."))
        self.assertEqual((ifs[0].start, ifs[0].end), (30, 34))

    def test_exec_sql_block_whole_with_tables_and_hostvars(self):
        self.assertEqual(len(self.facts.sql), 1)
        s = self.facts.sql[0]
        self.assertEqual(s.stmt_type, "UPDATE")
        self.assertEqual(s.tables, ["CLAIM_TBL"])
        self.assertEqual(s.host_vars, ["WS-SPLIT-DEFINITION", "WS-STATUS"])
        self.assertEqual((s.start_line, s.end_line), (42, 46))


class RaggedMargins(unittest.TestCase):
    """PMASTREC.cpy is deliberately 79 columns wide with 80-column banners."""

    def test_identification_area_does_not_leak(self):
        text, data, enc = _load("PMASTREC.cpy")
        roots, _ = copybook.parse_copybook(text, data, enc)
        self.assertEqual(roots[0].length, 422)
        names = [f.name for f in copybook.flatten(roots)]
        self.assertIn("PM-FILLER", names)


class CommentedCode(unittest.TestCase):

    def test_commented_call_is_not_an_edge(self):
        text, data, enc = _load("SAMPPGM.cbl")
        f = cobol.parse_program(text, data, enc)
        targets = {c.target for c in f.calls if c.target}
        self.assertNotIn("OLDRATER", targets)
        self.assertIn("VALIDATE", targets)

    def test_dynamic_call_resolves_every_candidate(self):
        text, data, enc = _load("SAMPPGM.cbl")
        f = cobol.parse_program(text, data, enc)
        dyn = [c for c in f.calls if c.kind == "dynamic"]
        self.assertTrue(dyn)
        self.assertEqual(dyn[0].resolved, ["ADJUSTER", "RATECALC"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
