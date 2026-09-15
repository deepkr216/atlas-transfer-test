r"""
Shapes that made a build slow down as the estate grew, or held the whole
process inside a regular expression (LESSONS 148). Each test uses an input
the old code took many seconds - or minutes - on, with a budget the fixed
code meets comfortably, and checks the facts still come out.
"""

import contextlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, classify, cobol, docs, jcl, query, reader, screens  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def timed(fn, *a, **k):
    t0 = time.time()
    r = fn(*a, **k)
    return r, time.time() - t0


class Classifier(unittest.TestCase):

    def test_blank_card_padding_does_not_freeze_the_classifier(self):
        head = ((" " * 80) + "\n") * 101                                    # 8,181 bytes: 70 s before
        kind, took = timed(classify.classify, "X/PROD.GC.UTL/CARD1", head)
        self.assertLess(took, 0.5, f"{took:.1f} s")
        self.assertEqual(kind[0], "ctlcard")
        kind, took = timed(classify.classify, "X/CARD2.txt", "\r\n" * 4096)
        self.assertLess(took, 0.5, f"{took:.1f} s")

    def test_signatures_still_recognise_their_members(self):
        cases = [("X/A.txt", "MYPGM    CSECT\n         STM   14,12,12(13)\n", "asm"),
                 ("X/B.txt", "         CSECT\n", "asm"),
                 ("X/C.txt", "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. C.\n", "cobol"),
                 ("X/D.txt", "         DBD   NAME=CLMDBD,ACCESS=HIDAM\n", "dbd"),
                 ("X/E.txt", "         PCB   TYPE=DB,DBDNAME=CLMDBD\n", "psb"),
                 ("X/F.txt", " DEFINE TRANSACTION(MEMB) GROUP(G1)\n", "csd"),
                 ("X/G.txt", "         TRANSACT CODE=MEMB,PGMTYPE=TP\n", "imsgen"),
                 ("X/H.txt", "MEMFMT   FMT\n", "mfs"),
                 ("X/I.txt", "       01  WS-REC.\n           05  WS-A PIC X.\n", "copybook")]
        for path, head, want in cases:
            self.assertEqual(classify.classify(path, head)[0], want, path)


class JclScanners(unittest.TestCase):

    def test_ims_stage1_check_on_a_sysin_of_blank_lines(self):
        td = tempfile.mkdtemp()
        try:
            root = os.path.join(td, "estate", "JCLLIB")
            os.makedirs(root)
            with open(os.path.join(root, "BIGJOB.jcl"), "w") as fh:
                fh.write("//BIGJOB   JOB (A),'X'\n//S1       EXEC PGM=MYPGM\n//SYSIN    DD *\n" + "\n" * 40000 + "/*\n")
            t0 = time.time()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = build._main([os.path.join(td, "estate"), "--db", os.path.join(td, "t.db"), "--rebuild"])
            took = time.time() - t0
            self.assertEqual(rc, 0)
            self.assertLess(took, 8, f"{took:.1f} s (33 s before)")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_sort_deck_with_thousands_of_outfil(self):
        cards = "\n".join(f"  OUTFIL FILES={i:02d},INCLUDE=(1,2,CH,EQ,C'{i:02d}')" for i in range(6000))
        cards += "\n  OUTFIL FNAMES=(OUT1,OUT2),INCLUDE=(1,2,CH,EQ,C'ZZ')\n  JOINKEYS F1=IN1,FIELDS=(1,8,A)\n"
        roles, took = timed(jcl.sort_dd_roles, cards)
        self.assertLess(took, 2, f"{took:.1f} s (29 s before)")
        self.assertEqual(roles.get("OUT1"), "output")
        self.assertEqual(roles.get("OUT2"), "output")
        self.assertEqual(roles.get("IN1"), "input")
        # an OUTFIL without FNAMES does not borrow the next one's
        self.assertEqual(jcl.sort_dd_roles("  OUTFIL FILES=01\n  OUTFIL FNAMES=A1\n"), {"A1": "output"})
        # ICETOOL: FROM/TO of one operator, not the next operator's TO
        r = jcl.sort_dd_roles("  COPY FROM(IN1) USING(C1)\n  SORT FROM(IN2) TO(OUT2) USING(C2)\n")
        self.assertEqual(r, {"IN1": "input", "IN2": "input", "OUT2": "output"})
        ops, took = timed(jcl.sort_dd_roles, "COPY FROM(IN1) USING(CTL1) " * 4000)
        self.assertLess(took, 2, f"{took:.1f} s")

    def test_easytrieve_and_ftp_blank_blocks(self):
        text = "\n" * 40000 + "FILE CLMIN\n  CLM-STAT  25  2  A\nJOB INPUT CLMIN\n  PUT CLMOUT\n"
        (dds, t1) = timed(jcl.easytrieve_dds, text)
        (fields, t2) = timed(jcl.easytrieve_fields, text)
        self.assertLess(t1 + t2, 2, f"{t1 + t2:.1f} s (52 s before)")
        self.assertIn("CLMIN", dds)
        self.assertTrue(any("CLM-STAT" in str(f) for f in fields), fields)
        jcl_text = ("//FTPJOB   JOB (A),'X'\n//S1       EXEC PGM=FTP,PARM='HOST1'\n//INPUT    DD *\n"
                    + "\n" * 20000 + "binary\nput 'PROD.CLM.EXTRACT' remote.dat\nquit\n/*\n")
        facts, took = timed(jcl.parse_jcl_all, jcl_text)
        self.assertLess(took, 3, f"{took:.1f} s (15 s before)")

    def test_idcams_define_on_a_padded_card_member(self):
        text = "  DEFINE CLUSTER -\n" + ((" " * 80) + "\n") * 2000 + "   (NAME(PROD.CLM.KSDS) INDEXED)\n"
        ops, took = timed(jcl.idcams_ops, text)
        self.assertLess(took, 2, f"{took:.1f} s (102 s before)")
        self.assertTrue(any("PROD.CLM.KSDS" in o[1] for o in ops), ops)
        self.assertTrue(any("PROD.A.B" in o[1] for o in jcl.idcams_ops("DEFINE CLUSTER (NAME(PROD.A.B) -\n  INDEXED)")))

    def test_long_jcl_continuations_and_referbacks(self):
        stmt = "//S1       EXEC PGM=X,PARM=(A,\n" + "".join("//             B,\n" for _ in range(40000)) + "//             C)\n"
        (stmts, took) = timed(reader.read_jcl, stmt)
        self.assertLess(took, 3, f"{took:.1f} s (8.6 s before)")
        self.assertEqual(len(stmts), 1)
        dds0 = "".join(f"//D{i:07d} DD DSN=PROD.T{i:05d},DISP=(NEW,PASS)\n" for i in range(3000))
        dds1 = "".join(f"//R{i:07d} DD DSN=*.S0.D{i:07d},DISP=SHR\n" for i in range(3000))
        text = "//J        JOB (A),'X'\n//S0       EXEC PGM=A\n" + dds0 + "//S1       EXEC PGM=B\n" + dds1
        facts, took = timed(jcl.parse_jcl, text)
        self.assertLess(took, 4, f"{took:.1f} s")
        s1 = [s for s in facts.steps if s.step_name == "S1"][0]
        self.assertEqual(s1.dds[0].dsn_resolved, "PROD.T00000")
        self.assertEqual(s1.dds[-1].dsn_resolved, "PROD.T02999")


class ScreensCobolDocs(unittest.TestCase):

    def test_mfs_do_group_with_many_fields(self):
        body = "".join(f"F{i:05d}   MFLD  LTH=1\n" for i in range(8000))
        text = "MEMMSG   MSG   TYPE=INPUT,SOR=(MEMFMT,IGNORE)\n         SEG\n         DO    20\n" + body + "         ENDDO\n         MSGEND\n"
        scr, took = timed(screens.parse_mfs, text)
        self.assertLess(took, 3, f"{took:.1f} s")
        self.assertTrue(scr)

    def test_open_after_a_long_exec_sql_block(self):
        cols = "\n".join(f"                  COL{i:04d}," for i in range(800))
        src = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. OPENSQL.\n       ENVIRONMENT DIVISION.\n"
               "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n           SELECT CLMFILE ASSIGN TO CLMIN.\n"
               "       DATA DIVISION.\n       FILE SECTION.\n       FD  CLMFILE.\n       01  CLM-REC PIC X(80).\n"
               "       WORKING-STORAGE SECTION.\n       01  WS-N PIC 9(4).\n       PROCEDURE DIVISION.\n       0000-MAIN.\n"
               "           OPEN INPUT CLMFILE\n           EXEC SQL DECLARE C1 CURSOR FOR SELECT\n" + cols +
               "\n                  COLZZZZ FROM T1\n           END-EXEC\n           ADD 1 TO WS-N.\n           GOBACK.\n")
        facts, took = timed(cobol.parse_program, src)
        self.assertLess(took, 2, f"{took:.1f} s (6.4 s before)")
        self.assertIn(("CLMFILE", "file", "OPEN INPUT"), [(a, b, c) for (a, b, c, _l) in facts.io_ops])

    def test_cics_option_with_an_unclosed_parenthesis(self):
        t0 = time.time()
        cobol._CICS_FILE.search("FILE(" + " " * 3200)
        list(cobol._CICS_OPT.finditer("X(" + " " * 3200))
        self.assertLess(time.time() - t0, 0.2)
        self.assertEqual(cobol._CICS_FILE.search("READ FILE( 'CLMFILE' ) INTO(X)").group(1).strip(), "'CLMFILE'")

    def _pdf(self, streams):
        td = tempfile.mkdtemp()
        p = os.path.join(td, "t.pdf")
        with open(p, "wb") as fh:
            fh.write(b"%PDF-1.4\n")
            for n, body in enumerate(streams, 1):
                fh.write(b"%d 0 obj\n<< /Length %d >>\nstream\n" % (n, len(body)) + body + b"\nendstream\nendobj\n")
            fh.write(b"trailer\n<< >>\n%%EOF\n")
        return td, p

    def test_pdf_text_from_tj_arrays_is_linear_and_in_order(self):
        content = b"BT /F1 12 Tf " + b"[(Claim) -250 (status)] TJ " * 4000 + b"(Final line) Tj ET"
        td, p = self._pdf([zlib.compress(content)])
        try:
            d, took = timed(docs.extract, p)
        finally:
            shutil.rmtree(td, ignore_errors=True)
        self.assertLess(took, 3, f"{took:.1f} s (21 s before)")
        text = d.sections[0][1]
        self.assertTrue(text.startswith("Claimstatus Claimstatus"), text[:60])     # a TJ array's strings join, as before
        self.assertTrue(text.endswith("Final line"), text[-40:])
        self.assertEqual(docs._pdf_text_pieces(b"BT (a\\) b) Tj [(c) 3 (d)] TJ (e) ' ET"), ["a) b", "cd", "e"])

    def test_pdf_streams_without_eol_before_endstream(self):
        td = tempfile.mkdtemp()
        p = os.path.join(td, "noeol.pdf")
        with open(p, "wb") as fh:
            fh.write(b"%PDF-1.4\n")
            for n in range(2000):
                fh.write(b"%d 0 obj\n<< >>\nstream\nBT (Word%d) Tj ETendstream\nendobj\n" % (n, n))
        try:
            d, took = timed(docs.extract, p)
        finally:
            shutil.rmtree(td, ignore_errors=True)
        self.assertLess(took, 3, f"{took:.1f} s (14 s before)")
        self.assertIn("Word1999", " ".join(t for _h, t in d.sections))

    def test_html_with_unclosed_style_tags(self):
        td = tempfile.mkdtemp()
        p = os.path.join(td, "t.html")
        with open(p, "w") as fh:
            fh.write("<html><style>a{}</style><p>Keep this</p><script>var x;</script>" + "<style>a " * 16000)
        try:
            d, took = timed(docs.extract, p)
        finally:
            shutil.rmtree(td, ignore_errors=True)
        self.assertLess(took, 2, f"{took:.1f} s (14 s before)")
        text = " ".join(t for _h, t in d.sections)
        self.assertIn("Keep this", text)
        self.assertNotIn("var x", text)


class IndexLookups(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        root = os.path.join(cls.td, "estate", "SRC")
        os.makedirs(root)
        for fn in ("SAMPPGM.cbl", "WALKPGM.cbl", "ERRPGM.cbl", "PMASTREC.cpy", "WALKREC.cpy", "WALKPROC.cpy"):
            shutil.copy(os.path.join(FIX, fn), root)
        cls.db = os.path.join(cls.td, "t.db")
        with contextlib.redirect_stdout(io.StringIO()):
            build._main([os.path.join(cls.td, "estate"), "--db", cls.db, "--rebuild"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.td, ignore_errors=True)

    def _plan(self, conn, sql, args=()):
        return " | ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))

    def test_post_pass_and_report_lookups_use_indexes(self):
        conn = sqlite3.connect(self.db)
        try:
            for sql, args in [("SELECT op FROM io_op WHERE program_id=? AND target=? AND op LIKE 'OPEN%'", (1, "X")),
                              ("SELECT id FROM dli_call WHERE program_id=?", (1,)),
                              ("SELECT parm FROM step WHERE UPPER(effective_pgm)=? AND parm LIKE '%PSB %'", ("X",)),
                              ("SELECT DISTINCT psb FROM transaction_def WHERE UPPER(program)=?", ("X",)),
                              ("SELECT 1 FROM ims_psb WHERE UPPER(name)=?", ("X",)),
                              ("SELECT id FROM member WHERE UPPER(name)=?", ("X",)),
                              ("SELECT id FROM program WHERE member_id=?", (1,)),
                              ("SELECT 1 FROM call_edge WHERE UPPER(target)=?", ("X",)),
                              ("SELECT 1 FROM copy_use WHERE UPPER(copybook)=?", ("X",))]:
                plan = self._plan(conn, sql, args)
                self.assertIn("USING", plan, f"{sql}: {plan}")
                self.assertNotRegex(plan, r"^SCAN \w+$", f"{sql}: {plan}")
        finally:
            conn.close()

    def test_every_member_records_where_its_search_rows_are(self):
        conn = sqlite3.connect(self.db)
        try:
            n_fts = conn.execute("SELECT COUNT(*) FROM src_fts").fetchone()[0]
            n_span = conn.execute("SELECT COALESCE(SUM(hi - lo + 1), 0) FROM fts_span").fetchone()[0]
            self.assertEqual(n_fts, n_span)
            for mid, lo, hi in conn.execute("SELECT member_id, lo, hi FROM fts_span").fetchall():
                got = {r[0] for r in conn.execute("SELECT member_id FROM src_fts WHERE rowid BETWEEN ? AND ?", (lo, hi))}
                self.assertEqual(got, {mid})
        finally:
            conn.close()

    def test_line_lookups_read_a_range_not_the_whole_index(self):
        conn = query.connect(self.db)
        try:
            seen = []
            conn.set_trace_callback(seen.append)
            walk = query.cmd_walk(conn, "WALKPGM", budget=20000)
            conn.set_trace_callback(None)
            self.assertIn("MOVE 0 TO WS-READ-COUNT", walk)
            scans = [s for s in seen if "FROM src_fts WHERE member_name" in s or "FROM src_fts WHERE member_id" in s]
            self.assertEqual(scans, [], scans[:3])
        finally:
            conn.close()

    def test_an_index_from_before_spans_still_works_and_is_cleared_by_a_full_reparse(self):
        old = os.path.join(self.td, "old.db")
        shutil.copy(self.db, old)
        conn = sqlite3.connect(old)
        conn.execute("DROP TABLE fts_span")
        conn.execute("DELETE FROM atlas_meta")
        conn.commit()
        conn.close()
        conn = build.open_db(old)                       # sees search rows and no spans: legacy
        self.assertTrue(build.fts_legacy(conn))
        conn.close()
        qc = query.connect(old)
        try:
            self.assertIn("MOVE 0 TO WS-READ-COUNT", query.cmd_walk(qc, "WALKPGM", budget=20000))   # scan fallback
        finally:
            qc.close()
        with mock.patch.object(build, "tool_fingerprint", return_value="feedfacefeedface"), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = build._main([os.path.join(self.td, "estate"), "--db", old])
        self.assertEqual(rc, 0)
        conn = build.open_db(old)
        try:
            self.assertFalse(build.fts_legacy(conn), "every old row is gone after the full re-parse")
            n_fts = conn.execute("SELECT COUNT(*) FROM src_fts").fetchone()[0]
            n_span = conn.execute("SELECT COALESCE(SUM(hi - lo + 1), 0) FROM fts_span").fetchone()[0]
            self.assertEqual(n_fts, n_span)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
