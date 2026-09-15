"""
Shapes that held the build for hours (LESSONS 146), each now linear or
bounded: a paragraph with one period at its end, an unterminated literal
or EXEC block, a line cut at column 72, a 5 MB record with no line ends,
an EBCDIC member, an unterminated GO TO, a dispatcher moving thousands of
names to one CALL variable, thousands of COPY statements.
"""

import os
import re
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, cobol, expand, query, reader  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _prog(body_lines, ws=""):
    head = ["       IDENTIFICATION DIVISION.", "       PROGRAM-ID. BIGONE.", "       DATA DIVISION.",
            "       WORKING-STORAGE SECTION.", "       01  WS-A                 PIC X(10).", "       01  WS-B                 PIC X(10).",
            "       01  WS-IDX               PIC 9(02)."]
    if ws:
        head.append(ws)
    head += ["       PROCEDURE DIVISION.", "       0000-MAIN."]
    return "\n".join(head + body_lines + ["           GOBACK."]) + "\n"


def _statements(text):
    lines, _fixed = reader.read_cobol_lines(text)
    return list(reader.cobol_statements(reader.join_cobol_continuations(lines)))


class Scanner(unittest.TestCase):

    def test_one_period_per_paragraph_is_linear(self):
        body = ["           MOVE WS-A TO WS-B"] * 20000
        body[-1] += "."
        t0 = time.time()
        stmts = _statements(_prog(body))
        took = time.time() - t0
        self.assertLess(took, 4, f"20,000-line sentence took {took:.1f} s")
        big = max(stmts, key=lambda s: len(s.text))
        self.assertEqual(big.text.count("MOVE WS-A TO WS-B"), 20000)
        self.assertEqual((big.start, big.end), (10, 20009))

    def test_unterminated_literal_is_linear(self):
        body = ["           MOVE 'OOPS TO WS-B."] + ["           MOVE 'X' TO WS-B."] * 8000
        t0 = time.time()
        _statements(_prog(body))
        self.assertLess(time.time() - t0, 4)

    def test_exec_without_end_exec_is_linear(self):
        body = ["           EXEC SQL SELECT A INTO :WS-A FROM T"] + ["           MOVE 'X' TO WS-B."] * 8000
        t0 = time.time()
        stmts = _statements(_prog(body))
        self.assertLess(time.time() - t0, 4)
        self.assertTrue(any(s.text.startswith("EXEC SQL") and s.text.count("MOVE 'X'") == 8000 for s in stmts))
        # a closed block still ends where it should, and a period inside SQL text does not end it
        body = ["           EXEC SQL SELECT A INTO :WS-A FROM T", "              -- the policy. really", "           END-EXEC",
                "           MOVE 'Y' TO WS-B."]
        stmts = _statements(_prog(body))
        sql = [s for s in stmts if s.text.startswith("EXEC SQL")]
        self.assertEqual(len(sql), 1)
        self.assertIn("END-EXEC", sql[0].text)
        self.assertIn("MOVE 'Y'", sql[0].text)                                 # no period after END-EXEC: same sentence
        self.assertTrue(any(s.text == "GOBACK." for s in stmts))

    def test_statement_boundaries_unchanged(self):
        text = _prog(["           MOVE 1.5 TO WS-A.  MOVE 'END.' TO WS-B.", "           DISPLAY 'A'", "              'B'.",
                      "       1000-EXIT.  EXIT."])
        texts = [s.text for s in _statements(text)]
        self.assertIn("MOVE 1.5 TO WS-A.", texts)
        self.assertIn("MOVE 'END.' TO WS-B.", texts)
        self.assertIn("DISPLAY 'A' 'B'.", texts)
        self.assertIn("1000-EXIT.", texts)
        self.assertIn("EXIT.", texts)

    def test_huge_record_without_line_ends_is_cut_at_80(self):
        text = ("       MOVE WS-A TO WS-B." + " " * 55) * 60000 + "\x1a"          # 4.8 MB, one stray byte
        recs = reader._split_records(text, text.encode("latin-1"), "latin-1")
        self.assertEqual(len(recs), 60001)
        self.assertEqual(len(recs[0]), 80)

    def test_ebcdic_member_is_recognised(self):
        src = _prog(["           MOVE WS-A TO WS-B."])
        data = "".join(ln.ljust(80) for ln in src.split("\n")).encode("cp037")
        self.assertEqual(reader.sniff_encoding(data), "cp037")
        text, enc = reader.decode_bytes(data)
        self.assertEqual(enc, "cp037")
        self.assertIn("PROGRAM-ID. BIGONE.", text)
        self.assertNotEqual(reader.sniff_encoding(b"       MOVE '@@@@' TO WS-B.\n" * 50), "cp037")   # '@' in ASCII text


class GoTo(unittest.TestCase):

    def test_unterminated_go_to_is_fast(self):
        text = "GO TO ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 DEPENDING ON WS-IDX MOVE 'A' TO WS-B"
        t0 = time.time()
        m = cobol._GO_TO.search(text)
        self.assertLess(time.time() - t0, 0.5)
        self.assertIsNone(m)
        text = "GO TO PARA-ONE-LONG-NAME-HERE, PARA-TWO-LONG-NAME-HERE DEPENDING ON WS-IDX MOVE X(1) TO Y"
        t0 = time.time()
        cobol._GO_TO.search(text)
        self.assertLess(time.time() - t0, 0.5)

    def test_targets_still_parsed(self):
        m = cobol._GO_TO.search("GO TO P1, P2 P3 DEPENDING ON WS-IDX.")
        self.assertIsNotNone(m)
        self.assertEqual(re.split(r"[\s,]+", m.group(1)), ["P1", "P2", "P3"])
        self.assertEqual(m.group(2), "WS-IDX")
        m = cobol._GO_TO.search("GO TO 9999-EXIT.")
        self.assertEqual((m.group(1), m.group(2)), ("9999-EXIT", None))
        m = cobol._SORT_USING.search("USING IN-FILE, OTHER-FILE GIVING OUT-FILE.")
        self.assertEqual(re.split(r"[\s,]+", m.group(2)), ["IN-FILE", "OTHER-FILE"])


class BuildScale(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate", "SRC")
        os.makedirs(self.root)
        self.db = os.path.join(self.td, "t.db")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def _build(self):
        import contextlib
        import io
        buf = io.StringIO()
        t0 = time.time()
        with contextlib.redirect_stdout(buf):
            rc = build._main([os.path.join(self.td, "estate"), "--db", self.db, "--rebuild", "--member-limit", "120"])
        return rc, time.time() - t0, buf.getvalue()

    def test_dispatcher_calls_are_bounded(self):
        body = [f"           MOVE 'SUB{i:05d}' TO WS-PGM" + ("." if True else "") for i in range(3000)]
        body += ["           CALL WS-PGM USING WS-A WS-B."] * 1500
        with open(os.path.join(self.root, "DISPATCH.cbl"), "w") as fh:
            fh.write(_prog(body, ws="       01  WS-PGM               PIC X(08)."))
        rc, took, out = self._build()
        self.assertEqual(rc, 0, out)
        self.assertLess(took, 20, f"{took:.1f} s")
        conn = query.connect(self.db)
        n, longest = conn.execute("SELECT COUNT(*), MAX(LENGTH(resolved)) FROM call_edge WHERE kind='dynamic'").fetchone()
        conn.close()
        self.assertEqual(n, 1500)
        self.assertLess(longest, 2000, "thousands of candidate names must not be stored per call")

    def test_thousands_of_copies_are_linear(self):
        shutil.copy(os.path.join(FIX, "PMASTREC.cpy"), self.root)
        ws = "\n".join(f"       01  WS-REC-{i:04d}.\n           COPY PMASTREC." for i in range(400))
        with open(os.path.join(self.root, "MANYCOPY.cbl"), "w") as fh:
            fh.write(_prog(["           MOVE WS-A TO WS-B."], ws=ws))
        rc, took, out = self._build()
        self.assertEqual(rc, 0, out)
        self.assertLess(took, 25, f"{took:.1f} s")
        conn = query.connect(self.db)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM field WHERE member_id=(SELECT id FROM member WHERE name='MANYCOPY')").fetchone()[0], 400)
        conn.close()

    def test_run_at_matches_the_linear_scan(self):
        runs = [expand.Run(exp_start=1, exp_end=10, src_member=1, src_start=1, depth=0, via_copy=None),
                expand.Run(exp_start=11, exp_end=15, src_member=2, src_start=1, depth=1, via_copy="CPY"),
                expand.Run(exp_start=16, exp_end=30, src_member=1, src_start=11, depth=0, via_copy=None)]
        e = expand.Expansion(lines=[], runs=runs)
        for ln in (1, 10, 11, 15, 16, 30):
            lin = next(r for r in runs if r.exp_start <= ln <= r.exp_end)
            self.assertIs(e.run_at(ln), lin, ln)
        self.assertIsNone(e.run_at(31))
        self.assertIsNone(e.run_at(0))
        self.assertEqual(e.origin(12), (2, 2, 1))


if __name__ == "__main__":
    unittest.main()
