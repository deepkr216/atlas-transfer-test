r"""
Copybooks the estate lacks, rebuilt from the expanded text of the programs
that copy them (LESSONS 167). A compiler listing flags each copied line
with a C; an expanded source carries the copybook's name in columns
73-80; an expander may leave a comment naming it. The index says which
copybooks are missing; the recovered ones make the programs whole on the
next build, and go away when the real member arrives.
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

from atlas import build, query, recover  # noqa: E402

FIX = os.path.join(HERE, "fixtures")

POLDCL = """000100 01  POLICY-DCL.                                                POLD0001
000200     05  POL-NUMBER              PIC X(12).                       POLD0002
000300     05  POL-STATUS              PIC X(02).                       POLD0003
000400     05  POL-PREMIUM             PIC S9(09)V99 COMP-3.            POLD0004
"""


PROG = [
    "000100 IDENTIFICATION DIVISION.",
    "000200 PROGRAM-ID. TESTPGM.",
    "000300 DATA DIVISION.",
    "000400 WORKING-STORAGE SECTION.",
    "000500 01  WS-REC.",
    "000600     COPY PMASTREC.",
    "000700 01  WS-POL.",
    "000800     COPY POLDCL.",
    "000900 PROCEDURE DIVISION.",
    "001000 0000-MAIN.",
    "001100     MOVE 'X' TO PM-POLICY-STATUS.",
    "001200     GOBACK.",
]


def records_of(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [ln.rstrip("\r\n") for ln in fh]


def ibm_listing(program_records, copybooks, ruler=True, flag="C"):
    """What Enterprise COBOL prints: a line number per source line, a C after
    the number on every copied line, the 80-column record after that."""
    out = ["1PP 5655-S71 IBM Enterprise COBOL for z/OS  6.3.0                    SAMPPGM   Date 09/20/2026  Time 10:00:00   Page   2"]
    if ruler:
        out.append("   LineID  PL SL  ----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7-|--+----8 Map and Cross Reference")
    n = 0
    for rec in program_records:
        n += 1
        out.append(f"   {n:06d}         {rec.ljust(80)}")
        found = recover._copy_name(rec[7:72] if len(rec) > 7 else "")
        if found and found[0] in copybooks:
            for crec in copybooks[found[0]]:
                n += 1
                out.append(f"   {n:06d}{flag}        {crec.ljust(80)}")
    out.append("")
    out.append("   LineID  Message code  Message text")
    return "\n".join(out) + "\n"


class Formats(unittest.TestCase):

    def setUp(self):
        self.prog = list(PROG)
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        self.poldcl = POLDCL.splitlines()

    def test_a_compiler_listing_gives_every_copied_block_back_exactly(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        fmt, regions, stats = recover.extract(text, "SAMPPGM.lst", set())
        self.assertEqual(fmt, "compiler listing")
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, [r.name for r in regions])
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl])
        self.assertFalse(by["PMASTREC"].replacing)
        self.assertEqual(stats["flagged lines"], len(self.pmast) + len(self.poldcl))

    def test_a_listing_without_the_ruler_still_finds_the_source_column(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, ruler=False)
        fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        self.assertEqual([r.name for r in regions], ["PMASTREC"])
        self.assertEqual(regions[0].records[3].rstrip(), self.pmast[3].rstrip())

    def test_a_replacing_clause_and_a_nested_copy_are_noted(self):
        prog = [r.replace("COPY PMASTREC.", "COPY PMASTREC REPLACING ==PM-== BY ==WS-==.") for r in self.prog]
        inner = ["000100 05  PM-INNER-A            PIC X.                                 INNR0001"]
        pm = list(self.pmast[:3]) + ["000350     COPY INNERCPY.                                            SAMP0003"] + inner + list(self.pmast[3:])
        text = ibm_listing(prog, {"PMASTREC": pm, "INNERCPY": inner})
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        r = [x for x in regions if x.name == "PMASTREC"][0]
        self.assertTrue(r.replacing)
        self.assertEqual(r.nested, ["INNERCPY"])
        joined = "\n".join(r.records)
        self.assertIn("      *    COPY INNERCPY.", joined.replace("000350*", "      *"), "the nested COPY is a comment now")
        self.assertIn("PM-INNER-A", joined, "its text stays inline, so the fields are there once")

    def test_columns_73_80_expansion(self):
        recs = []
        for rec in self.prog:
            recs.append(rec.ljust(80))
            found = recover._copy_name(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += [(c[:72].ljust(72) + "PMASTREC") for c in self.pmast]
        fmt, regions, _s = recover.extract("\n".join(recs), "SAMPPGM.exp", {"PMASTREC"})
        self.assertEqual(fmt, "columns 73-80")
        self.assertEqual([r.name for r in regions], ["PMASTREC"])
        self.assertEqual([r[:72].rstrip() for r in regions[0].records], [r[:72].rstrip() for r in self.pmast])

    def test_marker_comments_with_and_without_an_end(self):
        recs = []
        for rec in self.prog:
            found = recover._copy_name(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs.append("      *COPY PMASTREC")
                recs += self.pmast
                recs.append("      *END COPY PMASTREC")
            elif found and found[0] == "POLDCL":
                recs.append("      *++INCLUDE POLDCL")
                recs += self.poldcl
            else:
                recs.append(rec)
        fmt, regions, _s = recover.extract("\n".join(recs), "SAMPPGM.exp", set())
        self.assertEqual(fmt, "marker comments")
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"})
        self.assertFalse(by["PMASTREC"].end_guessed)
        self.assertTrue(by["POLDCL"].end_guessed, "no END marker: the block ran to the next header")
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl])

    def test_an_expanded_source_with_no_marks_is_said_so(self):
        recs = []
        for rec in self.prog:
            recs.append(rec)
            found = recover._copy_name(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += [c[:72] for c in self.pmast]
        fmt, regions, _s = recover.extract("\n".join(recs), "x.cbl", {"PMASTREC"})
        self.assertEqual((fmt, regions), ("no copy marks", []))
        self.assertEqual(recover.extract("hello world\nno cobol here\n", "x.txt", set())[0], "no COPY statements")

    def test_choosing_between_programs(self):
        a = recover.Region("X", self.pmast, "A.lst", "compiler listing")
        b = recover.Region("X", self.pmast, "B.lst", "compiler listing")
        c = recover.Region("X", self.pmast[:-1], "C.lst", "compiler listing")
        pick, how = recover.choose([c, a, b])
        self.assertIs(pick, a)
        self.assertIn("2 different texts across 3 program(s); the most common taken (2 program(s) agree)", how)
        rep = recover.Region("X", self.pmast[:-1], "R.lst", "compiler listing", replacing=True)
        pick, how = recover.choose([rep, c])
        self.assertIs(pick, c, "the copy without REPLACING wins")
        pick, how = recover.choose([rep])
        self.assertIn("post-replacement", how)
        self.assertEqual(recover.looks_like_copybook(self.pmast), "data")
        self.assertEqual(recover.looks_like_copybook(["           MOVE A TO B.", "           PERFORM X."]), "procedure")
        self.assertEqual(recover.looks_like_copybook(["      * just a comment", "                              "]), "")


class EndToEnd(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.src = os.path.join(self.td, "estate", "GC", "PDS.SRC")
        self.lst = os.path.join(self.td, "estate", "GC", "PDS.LISTING")
        os.makedirs(self.src)
        os.makedirs(self.lst)
        os.makedirs(os.path.join(self.td, "estate", "SHARED", "COPYLIB"))
        shutil.copy(os.path.join(FIX, "SAMPPGM.cbl"), self.src)
        shutil.copy(os.path.join(FIX, "ERRPGM.cbl"), self.src)
        pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        for name in ("SAMPPGM", "ERRPGM"):
            prog = records_of(os.path.join(FIX, name + ".cbl"))
            with open(os.path.join(self.lst, name + ".lst"), "w", encoding="utf-8") as fh:
                fh.write(ibm_listing(prog, {"PMASTREC": pmast, "POLDCL": POLDCL.splitlines()}))
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        self.build(["--rebuild"])

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def build(self, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([os.path.join(self.td, "estate"), "--db", self.db, "--quiet", *extra])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def status(self, name):
        conn = query.connect(self.db)
        try:
            return conn.execute("SELECT parse_status FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()[0]
        finally:
            conn.close()

    def test_missing_copybooks_are_recovered_from_the_listing_and_the_program_becomes_whole(self):
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("partial", "partial"))
        conn = query.connect(self.db)
        self.assertEqual(recover.missing_copybooks(conn), {"PMASTREC": 1, "POLDCL": 1})
        conn.close()
        said = []
        stats = recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertEqual((stats["written"], stats["missing"]), (2, 2), said)
        out = os.path.join(self.td, "estate", "SHARED", recover.FOLDER)
        self.assertFalse(os.path.exists(out), "a dry run writes nothing")
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["written"], 2, said)
        self.assertEqual(sorted(os.listdir(out)), [recover.MARKER, "PMASTREC.cpy", "POLDCL.cpy"])
        with open(os.path.join(out, "PMASTREC.cpy"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("* RECOVERED by atlas.recover", body)
        self.assertIn("SAMPPGM (compiler listing); seen in one program", body)
        self.assertIn("PM-POLICY-STATUS", body)
        self.assertTrue(any("recovered: 2 of 2 missing copybooks" in s for s in said), said)
        self.assertTrue(any("formats seen: compiler listing in 2" in s for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            self.assertIn("| PMASTREC | 1 |", fh.read())
        # the next build, without --rebuild, re-expands the program that copies them
        out_text = self.build()
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("ok", "ok"), out_text)
        conn = query.connect(self.db)
        try:
            kinds = dict(conn.execute("SELECT name, kind FROM member WHERE name IN ('PMASTREC','POLDCL')").fetchall())
            self.assertEqual(kinds, {"PMASTREC": "copybook", "POLDCL": "copybook"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM copy_use WHERE resolved_member_id IS NULL").fetchone()[0], 0)
            self.assertIn("PM-POLICY-STATUS", query.cmd_field(conn, "PM-POLICY-STATUS"))
            self.assertIn("### Members parsed only in part\n_none_", query.cmd_coverage(conn))
        finally:
            conn.close()
        # a second run has nothing to do
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["written"], stats["kept"]), (0, 0), said)
        # the real copybook arrives: the recovered one goes, the real one is the only one
        shutil.copy(os.path.join(FIX, "PMASTREC.cpy"), os.path.join(self.td, "estate", "SHARED", "COPYLIB"))
        self.build()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["removed"], 1, said)
        self.assertEqual(sorted(os.listdir(out)), [recover.MARKER, "POLDCL.cpy"])
        self.assertTrue(any("removed - the estate now holds the real member: PMASTREC" in s for s in said), said)
        self.build()
        self.assertEqual(self.status("SAMPPGM"), "ok")
        conn = query.connect(self.db)
        try:
            paths = [r[0] for r in conn.execute("SELECT path FROM member WHERE name='PMASTREC'")]
            self.assertEqual(len(paths), 1, paths)
            self.assertIn("COPYLIB", paths[0])
        finally:
            conn.close()

    def test_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = recover.main(["--db", os.path.join(self.td, "no.db")])
        self.assertEqual(rc, 2)
        self.assertIn("not found", out.getvalue())
        cwd = os.getcwd()
        os.chdir(self.td)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = recover.main(["--db", self.db, "--dry-run"])
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("recovered: 2 of 2 missing copybooks (dry run - nothing written)", out.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(self.td, "work", "recover.md")))
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
