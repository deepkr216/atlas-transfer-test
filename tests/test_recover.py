r"""
Copybooks the estate lacks, rebuilt from the expanded text of the programs
that copy them (LESSONS 167, 168). A compiler listing flags each copied
line with a C; an expanded text lines up against the original program; an
expanded source carries the copybook's name in columns 73-80; an expander
may leave a comment naming it. The index says which copybooks are missing;
the recovered ones make the programs whole on the next build, and go away
when the real member arrives. A wrong copybook, written silently, is worse
than a missing one - so every block is checked before it is trusted.
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


def ibm_listing(program_records, copybooks, ruler=True, flag="C", prefix_lines=0, strip=False, flag_at=None):
    """What Enterprise COBOL prints: a line number per source line, a C after
    the number on every copied line, the 80-column record after that."""
    out = ["1PP 5655-S71 IBM Enterprise COBOL for z/OS  6.3.0                    SAMPPGM   Date 09/20/2026  Time 10:00:00   Page   2"]
    out += [f" some translator output line {i}" for i in range(prefix_lines)]
    if ruler:
        out.append("   LineID  PL SL  ----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7-|--+----8 Map and Cross Reference")
    n = 0
    sql = []
    for rec in program_records:
        n += 1
        out.append(f"   {n:06d}         {rec.ljust(80)}")
        code = rec[7:72] if len(rec) > 7 else ""
        found = recover.copy_in(code)
        if sql or ("EXEC SQL" in code.upper() and "END-EXEC" not in code.upper()):
            sql.append(code.strip())                                   # a three-line EXEC SQL INCLUDE
            if "END-EXEC" in code.upper():
                found = recover.copy_in(" ".join(sql))
                sql = []
            else:
                continue
        if found and found[0] in copybooks:
            for k, crec in enumerate(copybooks[found[0]]):
                n += 1
                f = flag_at.get((found[0], k), flag) if flag_at else flag
                out.append(f"   {n:06d}{f.ljust(9)}{crec.ljust(80)}")      # the flags sit in the PL/SL columns
    out.append("")
    out.append("   LineID  Message code  Message text")
    text = "\n".join(out) + "\n"
    return "\n".join(ln.rstrip() for ln in text.splitlines()) + "\n" if strip else text


class Formats(unittest.TestCase):

    def setUp(self):
        self.prog = list(PROG)
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        self.poldcl = POLDCL.splitlines()

    def test_a_compiler_listing_gives_every_copied_block_back_exactly(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        fmt, regions, stats = recover.extract(text, "TESTPGM.lst", set())
        self.assertEqual(fmt, "compiler listing")
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, [r.name for r in regions])
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast])
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl])
        self.assertFalse(by["PMASTREC"].replacing)
        self.assertTrue(by["PMASTREC"].trusted)
        self.assertEqual(stats["flagged lines"], len(self.pmast) + len(self.poldcl))

    def test_the_ruler_is_found_however_late_and_stripped_blank_lines_are_kept(self):
        pm = list(self.pmast) + ["000900                                                                SAMP0009"]
        text = ibm_listing(self.prog, {"PMASTREC": pm}, prefix_lines=900, strip=True)
        fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        r = [x for x in regions if x.name == "PMASTREC"][0]
        self.assertEqual(len(r.records), len(pm), "the blank copied record survives trailing-blank stripping")
        self.assertEqual(r.records[3].rstrip(), self.pmast[3].rstrip())

    def test_a_listing_without_the_ruler_still_finds_the_source_column_and_checks_it(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, ruler=False)
        fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        self.assertEqual(regions[0].records[3].rstrip(), self.pmast[3].rstrip())
        # the header one column to the right of where the guess assumes: the shape check still lands it
        shifted = [r[:7] + " " + r[7:] if "DIVISION" in r else r for r in self.prog]
        text = ibm_listing(shifted, {"PMASTREC": self.pmast}, ruler=False)
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        self.assertEqual(regions[0].records[3].rstrip(), self.pmast[3].rstrip())

    def test_an_unknown_flag_inside_a_block_makes_the_copy_suspect(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, flag_at={("PMASTREC", 5): "E"})
        fmt, regions, stats = recover.extract(text, "x.lst", set())
        r = regions[0]
        self.assertTrue(r.suspect, "one line carried a flag the tool does not know")
        self.assertEqual(stats.get("lines with an unknown flag 'E'"), 1)
        pick, how, why = recover.choose([r])
        self.assertIsNone(pick, "not written on its own")
        self.assertIn("flag 'E'", why)

    def test_ibm_out_of_sequence_asterisks_are_not_a_copy_mark_and_do_not_swallow_a_line(self):
        # IBM prints ** after the line number of a statement out of sequence - on a program line, and
        # C** on a copied one; neither may drop the line or merge two blocks
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl},
                           flag_at={("PMASTREC", 4): "C**"})
        text = text.replace("   000007         000700 01  WS-POL.", "   000007**       000700 01  WS-POL.")
        fmt, regions, stats = recover.extract(text, "x.lst", set())
        self.assertEqual(fmt, "compiler listing")
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"}, stats)
        self.assertEqual([r.rstrip() for r in by["PMASTREC"].records], [r.rstrip() for r in self.pmast],
                         "the C** line is still a copied line")
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl],
                         "the ** program line closed the first block")
        self.assertFalse(by["PMASTREC"].suspect)
        self.assertFalse(any(k.startswith("lines with an unknown flag") for k in stats), stats)

    def test_copy_after_code_on_the_same_line_and_a_three_line_sql_include(self):
        prog = [r for r in self.prog if "COPY" not in r]
        prog[4:4] = ["000450 01  WS-REC.  COPY PMASTREC.",
                     "000460     EXEC SQL", "000470          INCLUDE POLDCL", "000480     END-EXEC."]
        text = ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": self.poldcl})
        _fmt, regions, stats = recover.extract(text, "x.lst", set())
        self.assertEqual(sorted(r.name for r in regions), ["PMASTREC", "POLDCL"], stats)
        self.assertEqual(stats.get("flagged lines with no COPY before them", 0), 0)

    def test_a_replacing_clause_is_told_from_library_text_and_a_nested_copy_is_kept_inline(self):
        prog = [r.replace("COPY PMASTREC.", "COPY PMASTREC REPLACING ==PM-== BY ==WS-==.") for r in self.prog]
        inner = ["000100 05  PM-INNER-A            PIC X.                                 INNR0001"]
        pm = list(self.pmast[:3]) + ["000350     COPY INNERCPY.                                            SAMP0003"] + inner + list(self.pmast[3:])
        text = ibm_listing(prog, {"PMASTREC": pm, "INNERCPY": inner})
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        r = [x for x in regions if x.name == "PMASTREC"][0]
        self.assertTrue(r.replacing)
        self.assertEqual(r.nested, ["INNERCPY"])
        joined = "\n".join(r.records)
        self.assertNotIn("COPY INNERCPY", joined, "the nested COPY statement is gone, its text stays")
        self.assertIn("PM-INNER-A", joined)
        recover.resolve_replacing(r)
        self.assertFalse(r.replacing, "the listing shows PM- names, so it holds the library text: as good as any copy")
        post = [x.replace("PM-", "WS-") for x in pm]
        text = ibm_listing(prog, {"PMASTREC": post})
        _fmt, regions, _s = recover.extract(text, "x.lst", set())
        r = regions[0]
        recover.resolve_replacing(r)
        self.assertTrue(r.replacing, "the listing shows the replaced names: post-replacement text")

    def test_lined_up_with_the_program_needs_no_marks(self):
        expanded = []
        for rec in self.prog:
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                expanded.append(rec[:6] + "*" + rec[7:])                  # the expander comments the COPY out
                expanded += [f"{900000 + i:06d}{c[6:72]}" for i, c in enumerate(self.pmast)]   # and renumbers
            elif found and found[0] == "POLDCL":
                expanded.append(rec)                                      # or keeps it and adds the text after
                expanded += self.poldcl
            else:
                expanded.append(rec)
        fmt, regions, stats = recover.extract("\n".join(expanded), "TESTPGM.exp", set(), original=self.prog)
        self.assertEqual(fmt, "expanded source, lined up with the program", stats)
        by = {r.name: r for r in regions}
        self.assertEqual(set(by), {"PMASTREC", "POLDCL"})
        self.assertEqual([r[6:72].rstrip() for r in by["PMASTREC"].records], [r[6:72].rstrip() for r in self.pmast])
        self.assertEqual([r.rstrip() for r in by["POLDCL"].records], [r.rstrip() for r in self.poldcl])
        self.assertTrue(by["PMASTREC"].trusted)
        # a listing whose copied lines are not flagged: lined up too
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast}, flag=" ")
        fmt, regions, _s = recover.extract(text, "TESTPGM.lst", set(), original=self.prog)
        self.assertEqual(fmt, "compiler listing, copied lines not flagged, lined up with the program")
        self.assertEqual([r.name for r in regions], ["PMASTREC"])
        # an expanded text that differs elsewhere too is not a pure expansion: not trusted alone
        edited = [r.replace("MOVE 'X'", "MOVE 'Y'") for r in expanded]
        _fmt, regions, stats = recover.extract("\n".join(edited), "TESTPGM.exp", set(), original=self.prog)
        self.assertTrue(regions and not regions[0].trusted, stats)
        pick, _how, why = recover.choose([regions[0]])
        self.assertIsNone(pick)
        self.assertIn("differs from the program elsewhere", why)
        two = recover.Region("PMASTREC", regions[0].records, "OTHER.lst", "compiler listing")
        pick, how, _why = recover.choose([regions[0], two])
        self.assertIsNotNone(pick, "a second program agreeing makes it good")
        self.assertIn("identical in 2", how)

    def test_columns_73_80_expansion(self):
        recs = []
        for rec in self.prog:
            recs.append(rec.ljust(80))
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += [(c[:72].ljust(72) + "PMASTREC") for c in self.pmast]
        fmt, regions, _s = recover.extract("\n".join(recs), "TESTPGM.exp", {"PMASTREC"})
        self.assertEqual(fmt, "columns 73-80")
        self.assertEqual([r.name for r in regions], ["PMASTREC"])
        self.assertEqual([r[:72].rstrip() for r in regions[0].records], [r[:72].rstrip() for r in self.pmast])

    def test_marker_comments_are_trusted_only_with_an_end_marker(self):
        recs = []
        for rec in self.prog:
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += ["      *COPY PMASTREC"] + self.pmast + ["      *END COPY PMASTREC"]
            elif found and found[0] == "POLDCL":
                recs += ["      *++INCLUDE POLDCL"] + self.poldcl
            else:
                recs.append(rec)
        fmt, regions, _s = recover.extract("\n".join(recs), "TESTPGM.exp", set())
        self.assertEqual(fmt, "marker comments")
        by = {r.name: r for r in regions}
        self.assertTrue(by["PMASTREC"].trusted)
        self.assertFalse(by["POLDCL"].trusted, "no END marker: the end was guessed")
        self.assertIsNone(recover.choose([by["POLDCL"]])[0])
        two = recover.Region("POLDCL", by["POLDCL"].records, "OTHER.exp", "marker comments")
        self.assertIsNotNone(recover.choose([by["POLDCL"], two])[0])

    def test_no_marks_and_no_program_to_line_up_with_is_said_so(self):
        recs = []
        for rec in self.prog:
            recs.append(rec)
            found = recover.copy_in(rec[7:72] if len(rec) > 7 else "")
            if found and found[0] == "PMASTREC":
                recs += [c[:72] for c in self.pmast]
        fmt, regions, _s = recover.extract("\n".join(recs), "x.cbl", {"PMASTREC"})
        self.assertEqual(fmt, "expanded source, no copy marks and the program is not in the index to line up with")
        self.assertEqual(regions, [])
        self.assertEqual(recover.extract("hello world\nno cobol here\n", "x.txt", set())[0], "no COPY statements")

    def test_a_listing_unloaded_as_fixed_records_without_line_ends(self):
        text = ibm_listing(self.prog, {"PMASTREC": self.pmast})
        fixed = "".join(ln.ljust(133) for ln in text.splitlines())
        self.assertEqual(fixed.count("\n"), 0)
        _fmt, regions, _s = recover.extract(fixed, "x.lst", set())
        self.assertEqual([r.name for r in regions], ["PMASTREC"])

    def test_what_is_and_is_not_a_copybook(self):
        self.assertEqual(recover.looks_like_copybook(self.pmast), "data")
        self.assertEqual(recover.looks_like_copybook(["           MOVE A TO B.", "           PERFORM X."]), "procedure")
        self.assertEqual(recover.looks_like_copybook(["           SET WS-EOF TO TRUE.", "       9999-EXIT.", "           EXIT."]),
                         "procedure")
        self.assertEqual(recover.looks_like_copybook(["      * just a comment", "      * and another"]), "comments")
        self.assertEqual(recover.looks_like_copybook(["                              "]), "")
        shifted = [" " + r for r in self.pmast]                          # every record one column to the right
        self.assertEqual(recover.looks_like_copybook(shifted), "", "a shifted block fails the shape check")
        self.assertIn("first code line looks like", recover.shape_of(self.pmast))
        self.assertNotIn("PM-POLICY", recover.shape_of(self.pmast), "nothing from the estate in the shape")

    def test_choosing_between_programs(self):
        a = recover.Region("X", self.pmast, "A.lst", "compiler listing")
        b = recover.Region("X", self.pmast, "B.lst", "compiler listing")
        c = recover.Region("X", self.pmast[:-1], "C.lst", "compiler listing")
        pick, how, _why = recover.choose([c, a, b])
        self.assertIs(pick, a)
        self.assertIn("2 different texts across 3 program(s); the most common taken (2 program(s) agree)", how)
        rep = recover.Region("X", self.pmast[:-1], "R.lst", "compiler listing", replacing=True,
                             statement="COPY X REPLACING ==ZZ-== BY ==QQ-==.")
        pick, how, _why = recover.choose([rep, c])
        self.assertIs(pick, c, "the copy under a REPLACING clause that changed the text loses")


class EndToEnd(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.src = os.path.join(self.root, "GC", "PDS.SRC")
        self.lst = os.path.join(self.root, "GC", "PDS.LISTING")
        os.makedirs(self.src)
        os.makedirs(self.lst)
        os.makedirs(os.path.join(self.root, "SHARED", "COPYLIB"))
        shutil.copy(os.path.join(FIX, "SAMPPGM.cbl"), self.src)
        shutil.copy(os.path.join(FIX, "ERRPGM.cbl"), self.src)
        self.pmast = records_of(os.path.join(FIX, "PMASTREC.cpy"))
        for name in ("SAMPPGM", "ERRPGM"):
            prog = records_of(os.path.join(FIX, name + ".cbl"))
            with open(os.path.join(self.lst, name + ".lst"), "w", encoding="utf-8") as fh:
                fh.write(ibm_listing(prog, {"PMASTREC": self.pmast, "POLDCL": POLDCL.splitlines()}))
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        self.build(["--rebuild"])

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def build(self, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([self.root, "--db", self.db, "--quiet", *extra])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def status(self, name):
        conn = query.connect(self.db)
        try:
            return conn.execute("SELECT parse_status FROM member WHERE name=? AND kind='cobol'", (name,)).fetchone()[0]
        finally:
            conn.close()

    def test_missing_copybooks_are_recovered_and_the_programs_become_whole(self):
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("partial", "partial"))
        conn = query.connect(self.db)
        self.assertEqual(recover.missing_copybooks(conn), {"PMASTREC": 1, "POLDCL": 1})
        conn.close()
        said = []
        stats = recover.run(self.db, dry_run=True, log=said.append, report=self.report)
        self.assertEqual((stats["written"], stats["missing"]), (2, 2), said)
        out = os.path.join(self.root, "SHARED", recover.FOLDER)
        self.assertFalse(os.path.exists(out), "a dry run writes nothing")
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["written"], 2, said)
        self.assertEqual(sorted(os.listdir(out)), [recover.MARKER, "PMASTREC.cpy", "POLDCL.cpy"])
        with open(os.path.join(out, "PMASTREC.cpy"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("* RECOVERED by atlas.recover", body)
        self.assertIn("(compiler listing); seen in one program", body)
        self.assertIn("PM-POLICY-STATUS", body)
        self.assertTrue(any(s.startswith("recovered: 2 of 2 missing copybooks") for s in said), said)
        self.assertTrue(any("formats seen: compiler listing in 2" in s for s in said), said)
        self.assertTrue(any("used in 2 places" in s for s in said), said)
        with open(self.report, encoding="utf-8") as fh:
            self.assertIn("| PMASTREC | 1 |", fh.read())
        # run again before the build: nothing new, and it says what to do
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual((stats["written"], stats["kept"]), (0, 2), said)
        self.assertTrue(any("nothing new to write: 2 of 2" in s for s in said), said)
        # the next build, without --rebuild, re-expands the programs that copy them
        out_text = self.build()
        self.assertEqual((self.status("SAMPPGM"), self.status("ERRPGM")), ("ok", "ok"), out_text)
        conn = query.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM copy_use WHERE resolved_member_id IS NULL").fetchone()[0], 0)
            self.assertIn("PM-POLICY-STATUS", query.cmd_field(conn, "PM-POLICY-STATUS"))
            self.assertIn("### Members parsed only in part\n_none_", query.cmd_coverage(conn))
        finally:
            conn.close()
        # after the build nothing is missing any more
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["missing"], 0, said)
        self.assertTrue(any("nothing to recover: every copybook" in s for s in said), said)
        # the real copybook arrives: the recovered one goes, its programs are parsed again, the real one is the only one
        shutil.copy(os.path.join(FIX, "PMASTREC.cpy"), os.path.join(self.root, "SHARED", "COPYLIB"))
        self.build()
        conn = query.connect(self.db)
        before = conn.execute("SELECT id FROM member WHERE name='SAMPPGM' AND kind='cobol'").fetchone()[0]
        conn.close()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["removed"], 1, said)
        self.assertEqual(sorted(os.listdir(out)), [recover.MARKER, "POLDCL.cpy"])
        self.assertTrue(any("removed - the estate now holds the real member: PMASTREC" in s for s in said), said)
        self.assertTrue(any("1 program(s) that had expanded them are marked for the next build" in s for s in said), said)
        self.build()
        conn = query.connect(self.db)
        try:
            self.assertEqual(self.status("SAMPPGM"), "ok")
            self.assertNotEqual(conn.execute("SELECT id FROM member WHERE name='SAMPPGM' AND kind='cobol'").fetchone()[0],
                                before, "parsed again against the real copybook")
            paths = [r[0] for r in conn.execute("SELECT path FROM member WHERE name='PMASTREC'")]
            self.assertEqual(len(paths), 1, paths)
            self.assertIn("COPYLIB", paths[0])
            resolved = conn.execute("SELECT m.path FROM copy_use c JOIN member m ON m.id=c.resolved_member_id "
                                    "WHERE c.copybook='PMASTREC'").fetchone()[0]
            self.assertIn("COPYLIB", resolved)
        finally:
            conn.close()

    def test_coverage_warns_while_a_recovered_copy_shadows_the_real_member(self):
        recover.run(self.db, log=lambda s: None, report=self.report)
        self.build()
        conn = query.connect(self.db)
        try:
            self.assertNotIn("still expand a recovered copybook", query.cmd_coverage(conn))
            # the real member arrives in a library that sorts after RECOVERED-COPYBOOKS: the build keeps the
            # recovered copy until atlas.recover runs - and coverage says so
            late = os.path.join(self.root, "SHARED", "ZCOPYLIB")
            os.makedirs(late)
            shutil.copy(os.path.join(FIX, "PMASTREC.cpy"), late)
        finally:
            conn.close()
        self.build()
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
        finally:
            conn.close()
        self.assertIn("still expand a recovered copybook although the estate now holds the real member", cov)
        self.assertIn("PMASTREC", cov)
        recover.run(self.db, log=lambda s: None, report=self.report)
        self.build()
        conn = query.connect(self.db)
        try:
            self.assertNotIn("still expand a recovered copybook", query.cmd_coverage(conn))
            resolved = conn.execute("SELECT m.path FROM copy_use c JOIN member m ON m.id=c.resolved_member_id "
                                    "WHERE c.copybook='PMASTREC'").fetchone()[0]
            self.assertIn("ZCOPYLIB", resolved)
        finally:
            conn.close()

    def test_two_systems_with_different_texts_each_get_their_own_copy(self):
        test_src = os.path.join(self.root, "GC-TEST", "PDS.SRC")
        test_lst = os.path.join(self.root, "GC-TEST", "PDS.LISTING")
        os.makedirs(test_src)
        os.makedirs(test_lst)
        shutil.copy(os.path.join(FIX, "SAMPPGM.cbl"), test_src)
        newer = list(self.pmast) + ["001300 05  PM-NEW-IN-TEST         PIC X(05).                          SAMP0013"]
        with open(os.path.join(test_lst, "SAMPPGM.lst"), "w", encoding="utf-8") as fh:
            fh.write(ibm_listing(records_of(os.path.join(FIX, "SAMPPGM.cbl")), {"PMASTREC": newer}))
        self.build()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["per_system"], 2, said)
        gc = os.path.join(self.root, "GC", recover.FOLDER, "PMASTREC.cpy")
        gct = os.path.join(self.root, "GC-TEST", recover.FOLDER, "PMASTREC.cpy")
        self.assertTrue(os.path.isfile(gc) and os.path.isfile(gct), said)
        self.assertFalse(os.path.exists(os.path.join(self.root, "SHARED", recover.FOLDER, "PMASTREC.cpy")))
        with open(gct, encoding="utf-8") as fh:
            self.assertIn("PM-NEW-IN-TEST", fh.read())
        with open(gc, encoding="utf-8") as fh:
            self.assertNotIn("PM-NEW-IN-TEST", fh.read())
        self.build()
        conn = query.connect(self.db)
        try:
            for system, want in (("GC", "GC"), ("GC-TEST", "GC-TEST")):
                resolved = conn.execute(
                    "SELECT r.path FROM copy_use c JOIN member p ON p.id=c.member_id JOIN member r ON r.id=c.resolved_member_id "
                    "WHERE p.name='SAMPPGM' AND p.system=? AND c.copybook='PMASTREC'", (system,)).fetchone()[0]
                self.assertIn(os.sep + want + os.sep, resolved, "each program expands its own system's copy")
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
                rc = recover.main(["--db", self.db, "--dry-run", "--from", self.lst + " "])   # the same folder, typed with a space
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("expanded texts to read: 2 ", out.getvalue(), "the folder the index already holds is not read twice")
            self.assertIn("recovered: 2 of 2 missing copybooks (dry run - nothing written)", out.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(self.td, "work", "recover.md")))
        finally:
            os.chdir(cwd)

    def test_no_listings_and_a_relative_root_are_explained(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM member WHERE kind='listing'")
        conn.commit()
        conn.close()
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report)
        self.assertEqual(stats["sources"], 0)
        self.assertTrue(any('--from "FOLDER"' in s for s in said), said)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE build_run SET root='estate'")
        conn.commit()
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            recover.run(self.db, log=said.append, report=self.report)
        self.assertIn("no such folder here", str(cm.exception))

    def test_a_build_run_with_a_relative_root_works_from_the_same_folder(self):
        # his way at work: `python -m atlas.supervise estate --db atlas.db` from the toolkit's folder
        cwd = os.getcwd()
        os.chdir(self.td)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(build._main(["estate", "--db", "t.db", "--rebuild", "--quiet"]), 0, buf.getvalue())
            said = []
            stats = recover.run("t.db", log=said.append, report=self.report)
            self.assertEqual(stats["written"], 2, said)
            self.assertTrue(any("recorded the estate as `estate`: found at" in s for s in said), said)
            out = os.path.join(self.root, "SHARED", recover.FOLDER)
            self.assertTrue(os.path.isfile(os.path.join(out, "PMASTREC.cpy")), said)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(build._main(["estate", "--db", "t.db", "--quiet"]), 0, buf.getvalue())
            conn = query.connect("t.db")
            try:
                self.assertEqual(conn.execute("SELECT parse_status FROM member WHERE name='SAMPPGM' AND kind='cobol'").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM copy_use WHERE resolved_member_id IS NULL").fetchone()[0], 0)
            finally:
                conn.close()
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
