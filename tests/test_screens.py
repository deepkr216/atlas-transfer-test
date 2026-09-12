"""
BMS / MFS screens and the condition-logic shapes that were not yet covered:
bracketed multi-line AND/OR, abbreviated `OR 'F'` after an AND (must not be
attributed to the earlier field), EVALUATE ... ALSO ..., a message assembled
from FILLER VALUEs, a VALUE split by a column-7 hyphen, PIC on its own line.
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, classify, cobol, copybook, query, reader, screens  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _load(name):
    return reader.load(os.path.join(FIX, name))


class BmsMfsParsing(unittest.TestCase):

    def test_bms_map_fields_defaults_and_symbolic_names(self):
        text, _, _ = _load("MEMSCRN.bms")
        scr = screens.parse_bms(text)
        self.assertEqual([s.name for s in scr], ["MEMMAP"])
        m = scr[0]
        self.assertEqual((m.parent, m.mode, m.lang), ("MEMSET", "INOUT", "COBOL"))
        by = {f.name: f for f in m.fields if f.name}
        g = by["GENDER"]
        self.assertEqual((g.row, g.col, g.length, g.initial, g.picin, g.picout), (3, 10, 1, "U", "X", "X"))
        self.assertEqual(by["RELCD"].picin, "9")
        self.assertEqual(by["ERRMSG"].length, 40)
        self.assertIn("GENDER:", [f.initial for f in m.fields if not f.name])     # screen label
        self.assertEqual(screens.bms_symbolic_names("GENDER")[:2], ["GENDERI", "GENDERO"])

    def test_mfs_offsets_and_type_inferred_from_fip_fop_label(self):
        text, _, _ = _load("MEMMFS.mfs")
        scr = screens.parse_mfs(text)
        by = {(s.kind, s.name): s for s in scr}
        fmt = by[("mfs_fmt", "MEMFMT")]
        self.assertEqual(fmt.mode, "INOUT")
        self.assertIn("GENDER", [f.name for f in fmt.fields])
        self.assertIn("GENDER:", [f.literal for f in fmt.fields if f.literal])
        mid = by[("mfs_msg", "MEMFIP")]
        self.assertEqual((mid.mode, mid.parent, mid.next_msg), ("INPUT", "MEMFMT", "MEMFOP"))
        self.assertEqual(mid.fields[0].literal, "MEMB ")                         # trancode constant
        off = {f.name: f.offset for f in mid.fields if f.name}
        self.assertEqual((off["GENDER"], off["RELCD"]), (5, 6))
        mod = by[("mfs_msg", "MEMFOP")]
        self.assertEqual(mod.mode, "OUTPUT")                                     # no TYPE=; from ...FOP
        self.assertTrue(any("inferred OUTPUT" in w for w in mod.warnings))
        offm = {f.name: (f.offset, f.length) for f in mod.fields if f.name}
        self.assertEqual(offm["GENDER"], (0, 3))                                 # ATTR=YES adds 2 bytes
        self.assertEqual(offm["RELCD"], (3, 1))
        self.assertEqual(offm["ERRMSG"], (4, 40))
        self.assertEqual(next(f.initial for f in mod.fields if f.name == "GENDER"), "U")

    def test_classify_screens(self):
        for name, kind in (("MEMSCRN.bms", "bms"), ("MEMMFS.mfs", "mfs")):
            text, _, _ = _load(name)
            self.assertEqual(classify.classify("x/" + name.split(".")[0], text)[0], kind)


class ConditionLogic(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        text, data, enc = _load("CONDLOGX.cbl")
        cls.f = cobol.parse_program(text, data, enc)
        cls.lits = {(l, c, fld) for (l, c, fld, _ln) in cls.f.literal_refs}
        cls.refs = {(n, m, s) for (n, m, s, _ln) in cls.f.field_refs}
        lines, _ = reader.read_cobol_lines(text, data=data, enc=enc)
        cls.roots, _ = copybook.parse_data_division(reader.join_cobol_continuations(lines))
        cls.fields = {x.name: x for x in copybook.flatten(cls.roots)}

    def test_bracketed_multiline_and_or_not(self):
        for lit, fld in (("3", "WS-REL-CD"), ("4", "WS-REL-CD"), ("M", "WS-GENDER-CD"), ("F", "WS-GENDER-CD")):
            self.assertIn((lit, "compare", fld), self.lits)
        self.assertIn(("WS-GENDER-UNK", "test", "IF"), self.refs)

    def test_abbreviated_or_after_and_is_not_over_attributed(self):
        at29 = {(l, fld) for (l, c, fld, ln) in self.f.literal_refs if ln == 29}
        self.assertEqual(at29, {("1", "WS-REL-CD"), ("M", "WS-GENDER-CD"), ("F", "WS-GENDER-CD")})

    def test_evaluate_also_attaches_when_values_by_position(self):
        for lit, fld in (("3", "WS-REL-CD"), ("M", "WS-GENDER-CD"), ("4", "WS-REL-CD"),
                         ("F", "WS-GENDER-CD"), ("2", "WS-REL-CD")):
            self.assertIn((lit, "when", fld), self.lits)
        self.assertNotIn(("M", "when", "WS-REL-CD"), self.lits)
        self.assertFalse(any(l == "ANY" for (l, _c, _f) in self.lits))

    def test_message_assembled_from_fillers(self):
        got = build._group_value_literals(copybook.flatten(self.roots))
        self.assertIn(("RELATIONSHIP/GENDER MISMATCH", "value_group", "WS-MSG-TABLE", 11), got)

    def test_value_continued_with_hyphen_and_pic_on_its_own_line(self):
        f = self.fields["WS-LONG-VALUE"]
        self.assertEqual(f.pic, "X(50)")
        self.assertIn("CONTINUATION HYPHEN", f.value_lit)

    def test_bms_symbolic_input_field_is_validated_here(self):
        self.assertIn(("N", "compare", "GENDERI"), self.lits)
        self.assertIn(("GENDERI", "test", "IF"), self.refs)


class ScreensEndToEnd(unittest.TestCase):

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

    def test_screen_dossier_links_bms_field_to_validating_program(self):
        out = query.cmd_screen(self.conn, "MEMMAP")
        self.assertIn("| GENDER |", out)
        self.assertIn("CONDLOGX", out)
        self.assertIn("GENDERI", out)
        self.assertIn("CONDLOGX:40", out)

    def test_screen_dossier_mfs_offsets(self):
        out = query.cmd_screen(self.conn, "MEMFIP")
        self.assertIn("next MEMFOP", out)
        self.assertIn("| 5/1 |", out)                    # GENDER at offset 5, segment 1
        self.assertIn("inferred OUTPUT", query.cmd_screen(self.conn, "MEMFOP"))

    def test_values_on_a_screen_field_shows_default_and_validator(self):
        out = query.cmd_values(self.conn, "GENDER")
        self.assertIn("Screen fields", out)
        self.assertIn("| U |", out)                      # default from the map / MOD
        self.assertIn("CONDLOGX", out)

    def test_messages_finds_filler_assembled_text(self):
        out = query.cmd_messages(self.conn, "GENDER")
        self.assertIn("RELATIONSHIP/GENDER MISMATCH", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
