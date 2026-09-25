r"""
A copybook the classifier filed as asm / listing / mfs by a LINE OF ITS
TEXT (LESSONS 186-189), and what ROADMAP re-parse items 20 and 22 changed.

His copybook sat in a .COPYLIB folder, yet `copybook NAME` said NOT FOUND
and `program NAME` said 'Not a program: indexed as a asm member': classify
checked _SIG_ASM before the level-number signature and before the folder
hint, and it fired on any line whose first or second word BEGAN with START,
CSECT or DSECT - `05 START-DATE PIC X(8).`, `PERFORM START-PARA.`. Two
sibling weak signatures did the same: a comment saying MODULE MAP made a
listing, a line whose first word was MSG made MFS source. The resolver
expands only copybook / cobol / sql / unknown, so every program copying such
a member said COPY X NOT FOUND while the file was in the estate. Until the
re-parse batch, atlas.recover re-filed such a member as a copybook in the
index and marked its programs.

The batch (this branch): the classifier reads a COBOL copybook's own lines
first - level numbers, COBOL statements with no DIVISION header - and types
a member asm / listing / mfs only by the shape a real one has (the shapes the
re-file proved, now in classify.py); the kind declared for a library in the
UI's table wins over a shape, the folder name and the extension. So:

  * a fresh build files every copybook of cases A (START-DATE), B
    (PERFORM START-PARA), C (MODULE MAP comment, a section named MSG) and J
    (the COBOL START verb alone on its line) as a copybook, the programs are
    whole, and recover finds nothing to do (TheBuildFilesThemRight);
  * the Assembler, listing and MFS shapes still classify their members, and
    recover says honestly why it does not re-file one - declare the library
    copybook if the member IS the copybook (ClassifierShapes,
    GenuineAssemblerIsNotRefiled, RealAssemblerShapesAreNotRefiled,
    DeclaredKinds);
  * on an index built BEFORE the batch - aged here by setting member.kind by
    SQL as the old classifier did and parsing the programs against it -
    recover still re-files the member, marks the programs, and the build
    makes them whole; the first build after the toolkit changed re-parses
    every member and files it a copybook itself (AnIndexBuiltBeforeTheBatch,
    SecondRunBeforeTheBuild, DryRunWithAFolderTypedCopyToo,
    OkWithAnUnlinkedCopyRow);
  * the stand-in's own rules, on hand-made readings (TheVerdict).
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from atlas import build, classify, query, recover  # noqa: E402
from test_recover import ibm_listing  # noqa: E402 - the Enterprise COBOL listing generator

HEAD = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {name}.\n"
        "       DATA DIVISION.\n       WORKING-STORAGE SECTION.\n"
        "       01  WS-X                PIC 9.\n       01  WS-Y                PIC 9.\n"
        "{data}"
        "       PROCEDURE DIVISION.\n       MAIN SECTION.\n{main}"
        "           PERFORM A-100-BEGIN.\n           GOBACK.\n")
TAIL = "       Z-900-END SECTION.\n           EXIT.\n"
SECTION_COPY = "       A-100-BEGIN SECTION.  COPY {book}.\n"
PLAIN_SECTION = "       A-100-BEGIN SECTION.\n           MOVE 1 TO WS-X.\n"

# a DATA copybook whose second field begins with START: the old _SIG_ASM read `START` as an Assembler operation
STARTBK = ("           05  ST-ID               PIC X(5).\n"
           "           05  START-DATE          PIC X(8).\n"
           "           05  ST-AMT              PIC 9(3).\n")
# a PROCEDURE copybook (no level numbers) whose PERFORM names START-PARA - and the paragraph itself
PROCBOOK = ("       S-110-DO.\n           PERFORM START-PARA.\n       START-PARA.\n           MOVE 1 TO WS-X.\n"
            "       S-199-EXIT.\n           EXIT.\n")
# a comment naming the MODULE MAP: the old _SIG_LISTING
MMAPBK = ("      * OFFSETS AS IN THE MODULE MAP OF THE LOAD MODULE\n"
          "           05  MM-ID               PIC X(5).\n           05  MM-QTY              PIC 9(3).\n")
# a section named MSG: the old _SIG_MFS (first word MSG, then a blank)
MSGBK = "       MSG SECTION.\n       M-110-DO.\n           MOVE 2 TO WS-Y.\n"
# a REAL Assembler member: a label in column 1 before CSECT, DS with a type, no COBOL at all
ASMBK = ("ASMBK    CSECT\n         USING *,15\nSTID     DS    CL5\nSTDATE   DS    CL8\n         BR    14\n         END\n")
# a plain procedure copybook: before the batch it had no signature and the folder name typed it (LESSONS 183)
PLAINBK = "       A-110-DO.\n           MOVE 1 TO WS-X.\n       A-199-EXIT.\n           EXIT.\n"
# a copybook with no signature at all even now - a literal copied into a VALUE clause: the folder name types it
VALBK = "      * THE STATE CODES, COPIED INTO A VALUE CLAUSE\n               'NYNJCTPAMA'.\n"
# three REAL Assembler members the first shape guard let through (LESSONS 187): a label longer than 8 characters
# before CSECT with only address constants, an unlabelled CSECT, START with a hexadecimal operand
LONGLBL = ("PLLONGLABEL CSECT\n         USING *,15\nTABLE    DC    A(TABLE)\n         DC    AL2(5)\n         BR    14\n"
           "         END\n")
NOLABEL = "         CSECT\n         USING *,12\n         LA    1,4(0,1)\n         BR    14\n         END\n"
HEXSTART = "PLHEX    START X'100'\n         LA    1,4\n         END\n"
# his procedure copybook (LESSONS 189): the COBOL START verb on a line of its own, its KEY IS clause on the next
STARTVERB = ("       S-100-POSITION.\n           MOVE WS-KEY-IN TO CUST-KEY.\n           START CUSTFILE\n"
             "              KEY IS >= CUST-KEY\n              INVALID KEY MOVE 'N' TO WS-FOUND\n           END-START.\n"
             "       S-199-EXIT.\n           EXIT.\n")

ITEMS = "ROADMAP re-parse items 20 and 22"
AGED_SEEN = ("line 2 `START-DATE` read as Assembler (a first or second word beginning with START, CSECT or DSECT) to the "
             "classifier this index was built with, which checked it before the level numbers and the folder name; the "
             f"classifier of this toolkit reads it as a copybook (COBOL level numbers with no PROCEDURE DIVISION) - {ITEMS}")
AGED_FIX = recover.EARLIER_FIX
ASM_SEEN = ("line 1 `CSECT` has the shape of an Assembler member, which the classifier checks after the level numbers and "
            "the COBOL statements")
SHAPE_WHY = ("no folder change helps (the content decided); not re-filed: it has the shape of an Assembler member "
             f"({classify.ASM_SHAPES}) - the copybook the programs copy is then another member, still to fetch; if this "
             "member IS the COBOL copybook, declare its library copybook in the UI's table and run the build (a declared "
             "kind wins over the shape)")
MISFILED_FIX = ("the folder name decided the kind (its text carries no signature - no level numbers, no COBOL statements): "
                "rename the folder to end in COPYLIB, or declare the library's kind in the UI's table (the manifest kinds) "
                "and run the build")
MISFILED_CELL = "not a kind the build expands: rename the folder to end in COPYLIB or declare its kind in the UI, then build"
PRE_BATCH = "0000prebatch0000"                  # a fingerprint no toolkit of this batch has: the index predates it


def data_program(name, book, ref=None):
    """`01 WS-REC. COPY book.` - and a MOVE to `ref`, one of the copybook's fields, so the build keeps
    WS-REC's children in pfield (an unreferenced root is stored without them)."""
    return HEAD.format(name=name, data=f"       01  WS-REC.\n           COPY {book}.\n",
                       main=f"           MOVE SPACES TO {ref}.\n" if ref else "") + PLAIN_SECTION + TAIL


def section_program(name, book):
    return HEAD.format(name=name, data="", main="") + SECTION_COPY.format(book=book) + TAIL


def value_program(name, book):
    """`05 WS-STATES PIC X(10) VALUE` with the literal in `book`, COPYed on the next line."""
    return HEAD.format(name=name, data="       01  WS-REC.\n           05  WS-STATES           PIC X(10) VALUE\n"
                                       f"           COPY {book}.\n", main="") + PLAIN_SECTION + TAIL


class _Estate(unittest.TestCase):
    """estate\\GC\\PROD.GC.SRC\\<program>.cbl and estate\\SHARED\\<library>\\<copybook>.txt, built together
    with --rebuild (the copybook is in the estate from the first build: nothing arrived)."""

    files = ()
    manifest = None                     # a manifest.json path: every build runs with --manifest

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root = os.path.join(self.td, "estate")
        self.db = os.path.join(self.td, "t.db")
        self.report = os.path.join(self.td, "work", "recover.md")
        for rel, text in self.files:
            self.write(rel, text)
        self.before_build()
        self.build(["--rebuild"])

    def before_build(self):
        """A hook for what the first build needs on disk besides the estate (a manifest)."""

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def build(self, extra=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = build._main([self.root, "--db", self.db, "--quiet", *extra,
                              *(["--manifest", self.manifest] if self.manifest else [])])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def recover(self, **kw):
        said = []
        stats = recover.run(self.db, log=said.append, report=self.report, **kw)
        return stats, "\n".join(said)

    def report_text(self):
        with open(self.report, encoding="utf-8") as fh:
            return fh.read()

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def member(self, name, library=None):
        """(kind, folder, parse_status, parse_error) - one row expected."""
        rows = self.q("SELECT kind, library, parse_status, parse_error FROM member WHERE UPPER(name)=?"
                      + (" AND library=?" if library else ""), name, *([library] if library else []))
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def status(self, name):
        return self.q("SELECT parse_status FROM member WHERE UPPER(name)=? AND kind='cobol'", name)[0][0]

    def member_id(self, name, library=None):
        return self.q("SELECT id FROM member WHERE UPPER(name)=?" + (" AND library=?" if library else ""),
                      name, *([library] if library else []))[0][0]

    def copy_use(self, program, book):
        return self.q("SELECT c.resolved_member_id FROM copy_use c JOIN member m ON m.id=c.member_id "
                      "WHERE UPPER(m.name)=? AND UPPER(c.copybook)=? ORDER BY c.line", program, book)

    def not_found_note(self, program, book):
        return self.q("SELECT detail FROM unresolved u JOIN member m ON m.id=u.member_id WHERE UPPER(m.name)=? "
                      "AND u.kind='expand' AND detail LIKE ?", program, f"%COPY {book} NOT FOUND%")

    def paragraphs(self, program):
        return self.q("SELECT p.name, p.kind, p.section FROM paragraph p JOIN program g ON g.id=p.program_id "
                      "JOIN member m ON m.id=g.member_id WHERE UPPER(m.name)=? ORDER BY p.start_line", program)

    def fields(self, program, *names):
        return self.q("SELECT f.name, f.offset, f.length, f.src_member FROM pfield f JOIN program p ON p.id=f.program_id "
                      f"JOIN member m ON m.id=p.member_id WHERE m.name=? AND f.name IN ({','.join('?' * len(names))}) "
                      "ORDER BY f.offset", program, *names)

    def outputs(self, program, book):
        conn = query.connect(self.db)
        try:
            cov = query.cmd_coverage(conn)
            return (cov, cov.split("### Copybooks not found")[1].split("\n###")[0],
                    query.cmd_program(conn, program), query.cmd_copybook(conn, book), query.cmd_program(conn, book))
        finally:
            conn.close()

    def assert_not_found(self, program, book, kind, folder="PROD.GC.COPYLIB"):
        """The index holds the copybook as `kind` and the program says NOT FOUND although the file is there."""
        self.assertEqual(self.member(book, folder)[:2], (kind, folder))
        self.assertEqual(self.status(program), "partial")
        self.assertEqual(self.copy_use(program, book), [(None,)])
        self.assertEqual(len(self.not_found_note(program, book)), 1)

    def age(self, book, kind, library=None, fingerprint=None):
        """The index as the classifier before ROADMAP re-parse items 20 and 22 built it: the member's kind set by
        SQL as that classifier filed it (settled, `skipped`, the rows its parse as a copybook wrote gone), the
        programs copying it parsed again by an incremental build (the build keeps the stored kind of an unchanged,
        settled member) - they say COPY NOT FOUND, as they did. With `fingerprint`, the last build is recorded as
        made by an older toolkit too, so the next build re-parses every member, as the first one after this batch
        does on his estate."""
        conn = sqlite3.connect(self.db)
        try:
            mid = conn.execute("SELECT id FROM member WHERE UPPER(name)=?" + (" AND library=?" if library else ""),
                               (book, *([library] if library else []))).fetchone()[0]
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                if "member_id" in cols and table not in ("member", "fts_span") and not table.startswith("src_fts"):
                    conn.execute(f"DELETE FROM {table} WHERE member_id=?", (mid,))
            conn.execute("UPDATE member SET kind=?, parse_status='skipped', parse_error=NULL WHERE id=?", (kind, mid))
            conn.execute("UPDATE member SET parse_status='pending' WHERE kind='cobol' AND id IN "
                         "(SELECT member_id FROM copy_use WHERE UPPER(copybook)=?)", (book,))
            conn.commit()
        finally:
            conn.close()
        self.build()
        if fingerprint:
            conn = sqlite3.connect(self.db)
            try:
                conn.execute("UPDATE build_run SET fingerprint=? WHERE id=(SELECT MAX(id) FROM build_run)", (fingerprint,))
                conn.commit()
            finally:
                conn.close()

    def assert_nothing_to_do(self):
        """recover on an index this toolkit built: nothing arrived, nothing misfiled, nothing re-filed or marked."""
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["refiled"], stats["waiting"], stats["marked"]),
                         (0, 0, 0, 0, 0), said)
        for word in ("re-filed", "misfiled", "does not expand", "could not re-file", "wait for the build"):
            self.assertNotIn(word, said)
        return said


class TheBuildFilesThemRight(_Estate):
    """Cases A, B, C and J on a fresh --rebuild with the classifier of this batch: `05 START-DATE`, `PERFORM
    START-PARA`, a comment naming MODULE MAP, a section named MSG and the COBOL START verb alone on its line are
    copybooks in the COPYLIB folder; so is a procedure copybook in a dataset-named folder with no hint (his
    hand-fetched libraries) and in a PROCS folder (ROADMAP item 20). Every program is whole at once, the copybooks
    carry their own layout rows, and recover finds nothing to do."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PROCBOOK")),
             ("GC/PROD.GC.SRC/MMPGM.cbl", data_program("MMPGM", "MMAPBK", "MM-ID")),
             ("GC/PROD.GC.SRC/MSGPGM.cbl", section_program("MSGPGM", "MSGBK")),
             ("GC/PROD.GC.SRC/SVPGM.cbl", section_program("SVPGM", "STARTVB")),
             ("GC/PROD.GC.SRC/PLPGM.cbl", section_program("PLPGM", "PLIBBK")),
             ("GC/PROD.GC.SRC/PRPGM.cbl", section_program("PRPGM", "PLAINBK")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK),
             ("SHARED/PROD.GC.COPYLIB/PROCBOOK.txt", PROCBOOK),
             ("SHARED/PROD.GC.COPYLIB/MMAPBK.txt", MMAPBK),
             ("SHARED/PROD.GC.COPYLIB/MSGBK.txt", MSGBK),
             ("SHARED/PROD.GC.COPYLIB/STARTVB.txt", STARTVERB),
             ("SHARED/PROD.GC.PLIB/PLIBBK.txt", PROCBOOK),
             ("SHARED/PROD.GC.PROCS/PLAINBK.txt", PLAINBK))

    def test_every_copybook_is_a_copybook_and_every_program_is_whole(self):
        for book, folder in (("STARTBK", "PROD.GC.COPYLIB"), ("PROCBOOK", "PROD.GC.COPYLIB"), ("MMAPBK", "PROD.GC.COPYLIB"),
                             ("MSGBK", "PROD.GC.COPYLIB"), ("STARTVB", "PROD.GC.COPYLIB"), ("PLIBBK", "PROD.GC.PLIB"),
                             ("PLAINBK", "PROD.GC.PROCS")):
            self.assertEqual(self.member(book)[:3], ("copybook", folder, "ok"), book)
        for prog, book in (("STPGM", "STARTBK"), ("SECPGM", "PROCBOOK"), ("MMPGM", "MMAPBK"), ("MSGPGM", "MSGBK"),
                           ("SVPGM", "STARTVB"), ("PLPGM", "PLIBBK"), ("PRPGM", "PLAINBK")):
            self.assertEqual(self.status(prog), "ok", prog)
            self.assertEqual(self.copy_use(prog, book), [(self.member_id(book),)], prog)
            self.assertEqual(self.not_found_note(prog, book), [], prog)
        # the data copybooks' fields, in the programs and in their own rows
        mid = self.member_id("STARTBK")
        self.assertEqual(self.fields("STPGM", "ST-ID", "START-DATE", "ST-AMT"),
                         [("ST-ID", 0, 5, mid), ("START-DATE", 5, 8, mid), ("ST-AMT", 13, 3, mid)])
        self.assertEqual(self.q("SELECT name, offset, length FROM field WHERE member_id=? ORDER BY offset", mid),
                         [("ST-ID", 0, 5), ("START-DATE", 5, 8), ("ST-AMT", 13, 3)])
        self.assertEqual([(n, o) for n, o, _l, _m in self.fields("MMPGM", "MM-ID", "MM-QTY")], [("MM-ID", 0), ("MM-QTY", 5)])
        # the procedure copybooks' paragraphs sit in the section that copies them
        paras = [(p[0], p[1], p[2]) for p in self.paragraphs("SECPGM")]
        for want in (("A-100-BEGIN", "section", "A-100-BEGIN"), ("S-110-DO", "paragraph", "A-100-BEGIN"),
                     ("START-PARA", "paragraph", "A-100-BEGIN"), ("S-199-EXIT", "paragraph", "A-100-BEGIN")):
            self.assertIn(want, paras)
        self.assertIn(("MSG", "section", "MSG"), [(p[0], p[1], p[2]) for p in self.paragraphs("MSGPGM")])
        self.assertIn(("S-100-POSITION", "paragraph"), [(p[0], p[1]) for p in self.paragraphs("SVPGM")])
        self.assertIn(("START-PARA", "paragraph"), [(p[0], p[1]) for p in self.paragraphs("PLPGM")])
        self.assertIn(("A-110-DO", "paragraph"), [(p[0], p[1]) for p in self.paragraphs("PRPGM")])
        # no MFS parser row for the section named MSG, no screen
        self.assertEqual(self.q("SELECT COUNT(*) FROM screen WHERE member_id=?", self.member_id("MSGBK")), [(0,)])

    def test_every_report_says_so_and_recover_finds_nothing(self):
        cov, nf, prog, book, as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("_none_", nf)
        self.assertIn("### Members parsed only in part\n_none_", cov)
        self.assertNotIn("re-filed", cov)
        self.assertNotIn("NOT FOUND", prog + book)
        self.assertIn("**Not a program**: indexed as a `copybook` member", as_prog)
        said = self.assert_nothing_to_do()
        self.assertIn("nothing to recover: every copybook the programs copy is in the index", said)
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.members_named(conn, "startbk")[1], [])
            self.assertEqual(recover.arrival_scan(conn), ([], [], []))
            self.assertEqual(recover.missing_copybooks(conn), {})
            self.assertEqual(recover.refiled_members(conn), {})
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
        finally:
            conn.close()

    def test_the_classifier_says_why(self):
        for book, text, reason in (("STARTBK", STARTBK, classify.REASON_DATA), ("PROCBOOK", PROCBOOK, classify.REASON_PROC),
                                   ("MMAPBK", MMAPBK, classify.REASON_DATA), ("MSGBK", MSGBK, classify.REASON_PROC),
                                   ("STARTVB", STARTVERB, classify.REASON_PROC), ("PLAINBK", PLAINBK, classify.REASON_PROC)):
            for folder in ("PROD.GC.COPYLIB", "PROD.GC.PROCS", "PROD.GC.CNTL", "downloads", "PROD.GC.PLIB"):
                self.assertEqual(classify.classify(f"C:/estate/SHARED/{folder}/{book}.txt", text), ("copybook", reason),
                                 (book, folder))

    def test_a_manifest_change_or_a_rebuild_keeps_them(self):
        # a build that re-parses every member files them the same: nothing for recover to re-file after it (the
        # stand-in had to re-file after every such build - LESSONS 188)
        self.manifest = os.path.join(self.td, "manifest.json")
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": {"PROD.GC.PROCS": "proc", "PROD.GC.PLIB": "proc"}}, fh)
        out = self.build()
        self.assertIn("changed 14  unchanged 0", out)                   # the manifest changed: every member re-parsed
        self.build(["--rebuild"])
        for book in ("STARTBK", "PROCBOOK", "PLIBBK", "PLAINBK"):
            self.assertEqual(self.member(book)[0], "copybook", book)
        self.assertEqual((self.status("STPGM"), self.status("PLPGM"), self.status("PRPGM")), ("ok", "ok", "ok"))
        self.assert_nothing_to_do()


class ClassifierShapes(unittest.TestCase):
    """classify.classify directly: the shapes a real Assembler, listing or MFS member has still decide it (the
    guard the re-file proved, LESSONS 187-189), a COBOL line never trips them, and a signature on a comment line
    says nothing."""

    def kind(self, text, folder="PROD.GC.COPYLIB", name="X.txt"):
        return classify.classify(f"C:/estate/SHARED/{folder}/{name}", text)[0]

    def test_real_members_keep_their_kind(self):
        for text in (ASMBK, LONGLBL, NOLABEL, HEXSTART, "STARTBK  START 0\n         END\n", "PGM      START\n         END\n",
                     "         START 0\n", "PGM      START 0   BEGIN HERE\n", "PGM      START SYM\n",
                     "STID     DS    CL5\n         B     START\n", "V        DC    V(SUBPGM)\n", "         DFHEIENT\n",
                     "D        DSECT\n", "         USING *,12\n", "R        EQU   *\n",
                     "MYPGM    CSECT\nR12      EQU   12\nR1       EQU   1\n",                 # register equates: no level
                     "PGM      CSECT\n         COPY  REGEQU\n         CALL  SUB,(A,B),VL\n",   # shared verbs after the shape
                     "PGM      DFHEIENT CODEREG=(12)\n         EXEC CICS RETURN\n",
                     "         CSECT\r\n         BR    14\r\n",                                   # CR LF
                     # the CLOSE macro with a bare DCB name, a remark after it (LESSONS 200)
                     "PGMCL    CSECT\n         USING *,15\n         OPEN  (INFILE,(INPUT))\n         CLOSE INFILE\n"
                     "         CLOSE OUTFILE          CLOSE THE REPORT\n         BR    14\n"
                     "INFILE   DCB   DDNAME=INFILE,DSORG=PS,MACRF=GM\n         END\n"):
            self.assertEqual(self.kind(text), "asm", text)
        for text in ("MYFMT    FMT\n         DEV   TYPE=3270-A2\n", "         MSG   TYPE=INPUT,SOR=(MYFMT,IGNORE)\n",
                     "         DFLD  POS=(1,2),LTH=8\n", "MEMMSG   MSG   TYPE=INPUT\n         MFLD  'MEMB'\n         MSGEND\n",
                     # the MFS COPY of a device header, and an operator control table's IF (LESSONS 200)
                     "PLMSG2   MSG   TYPE=OUTPUT,SOR=(PLFMT2,IGNORE)\n         SEG\n         MFLD  PLFLD2,LTH=10\n"
                     "         MSGEND\nPLFMT2   FMT\n         COPY  PLDEVHD\n         DIV   TYPE=INOUT\n"
                     "PLFLD2   DFLD  POS=(1,2),LTH=10\n         FMTEND\n         END\n",
                     "PLTAB    TABLE\n         IF    DATA=' ',NOFLDEXIT\n         IF    LENGTH=,NOFLDEXIT\n         TABLEEND\n",
                     "         IF    DATA>'5',PLEXIT\n"):
            for folder in ("PROD.GC.COPYLIB", "PROD.GC.MFS", "downloads"):
                self.assertEqual(self.kind(text, folder), "mfs", (text, folder))
        banner = "1PP 5655-EC6 IBM Enterprise COBOL for z/OS  6.3.0\n" + MMAPBK
        numbered = "".join(f"  {n:06d}         {n:06d}     05  A{n}  PIC X.\n" for n in range(1, 5))
        for text in (banner, numbered, "   LineID  PL SL  ----+-*A-1-B--+----2\n   000002         PROGRAM-ID. X.\n",
                     " MODULE MAP\n", "1   DATA DIVISION MAP\n"):
            self.assertEqual(self.kind(text), "listing", text)
        self.assertEqual(self.kind("anything\n", name="X.lst"), "listing")

    def test_cobol_lines_never_trip_a_shape(self):
        for text in (STARTBK, PROCBOOK, MMAPBK, MSGBK, STARTVERB, PLAINBK,
                     "       S-100.\n           START CUST-FILE KEY IS > CUST-KEY.\n",
                     "       S-100.\n           START CUSTFILE\n",
                     "           05  CSECT-NAME   PIC X(8).\n           PERFORM CSECT-PARA.\n",
                     "           MOVE DS TO WS-X.\n           PERFORM DC-PARA.\n",
                     "CR1234     START CUSTFILE\nCR1234         KEY IS >= K\nCR1234     END-START.\n",   # change tags
                     "000100     05  WS-A                                                 00010000\n",
                     "           ADD 1 TO WS-X.\n", "           SET WS-FLAG TO TRUE.\n", "           OPEN INPUT CUSTFILE.\n",
                     "           WRITE OUT-REC FROM WS-REC.\n", "           CONTINUE.\n",
                     "           EXEC SQL INCLUDE SQLCA END-EXEC.\n", "           COPY CLMHDR.\n           COPY CLMDTL.\n",
                     "      * COMPILED WITH IBM Enterprise COBOL 6.3\n" + STARTBK,
                     "      * BMS FIELD DFHMDF MAPPED HERE\n" + PLAINBK,
                     "      /* REXX */\n" + PLAINBK,
                     "      * PROGRAM-ID. OLDPGM - SEE CREATE TABLE CLAIM\n" + PLAINBK,
                     # the COBOL START verb alone - its KEY IS on the next line, no paragraph name (LESSONS 189, 200)
                     "           START CUSTFILE\n               KEY IS NOT LESS THAN CUST-KEY.\n",
                     "           START CUSTFILE KEY IS > CUST-KEY\n",
                     # a CLOSE with its period, a tagged data copybook, a name that only begins with an MFS word
                     "           CLOSE CUSTFILE.\n", "       01  WS-:XR:-REC.\n           05  WS-:XR:-ID  PIC X(10).\n",
                     "           05  :XR:-KEY                PIC X(8).\n",
                     "CR1234     IF WS-A = 1\nCR1234        DISPLAY\nCR1234          DEV-CODE\n",
                     "           DISPLAY 'THE END OF THE RUN'.\n           CALL 'PLSUB' USING WS-A.\n"):
            for folder in ("PROD.GC.COPYLIB", "PROD.GC.PROCS", "downloads"):
                self.assertEqual(self.kind(text, folder), "copybook", (text, folder))

    def test_what_is_not_cobol_keeps_its_kind(self):
        for text, folder, want in (("  SET MAXCC = 0\n", "PROD.GC.CNTL", "ctlcard"),
                                   ("  DELETE PROD.X.Y\n  IF LASTCC = 8 THEN SET MAXCC = 0\n", "PROD.GC.CNTL", "ctlcard"),
                                   ("  SORT FIELDS=(1,10,CH,A)\n", "PROD.GC.CNTL", "ctlcard"),
                                   ("  COPY FROM(IN) TO(OUT)\n  DISPLAY FROM(IN) LIST(RPT) ON(1,10,CH)\n", "PROD.GC.CNTL",
                                    "ctlcard"),
                                   ("  COPY OUTDD=OUT,INDD=IN\n", "PROD.GC.CNTL", "ctlcard"),
                                   ("  CALL 'PROD.LOAD(PGM1)' 'PARM'\n", "PROD.GC.CNTL", "ctlcard"),
                                   ("WRITE DONE\n", "PROD.GC.CNTL", "ctlcard"),
                                   ("         OPEN  (INFILE,(INPUT))\n         READ  DECB1,SF,INDCB,AREA\n", "PROD.GC.PROCS",
                                    "proc"),
                                   (VALBK, "PROD.GC.PROCS", "proc"),
                                   ("       PROCEDURE DIVISION USING LK-A.\n           PERFORM A-100.\n", "PROD.GC.PROCS",
                                    "proc"),
                                   ("//SYSOUT   DD SYSOUT=*\n", "PROD.GC.PROCS", "proc"),
                                   ("//STEP1   EXEC PGM=X\n" + STARTBK, "PROD.GC.COPYLIB", "jcl"),
                                   ("Meeting notes\nWe will move the file to the archive.\n       Please review.\n", "DOCS", "doc"),
                                   ("MAP1     DFHMDF POS=(1,1),LENGTH=8\n" + PLAINBK, "PROD.GC.COPYLIB", "bms"),
                                   ("/* REXX */\nsay hi\n", "PROD.GC.EXEC", "rexx"),
                                   ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. X.\n", "PROD.GC.COPYLIB", "cobol")):
            self.assertEqual(self.kind(text, folder), want, (text, folder))

    def test_the_declared_kind(self):
        # it replaces 'unknown', a shape, the folder name and the extension; a strong signature and a COBOL copybook's
        # own lines win over it; 'cobol' replaces 'unknown' only
        def k(text, folder, declared, name="X.txt"):
            return classify.classify(f"C:/estate/SHARED/{folder}/{name}", text, declared=declared)[0]
        self.assertEqual(k(VALBK, "PROD.GC.MISC", "copybook"), "copybook")                  # unknown
        self.assertEqual(k(VALBK, "PROD.GC.MISC", "cobol"), "cobol")                        # unknown, even for cobol
        self.assertEqual(k(ASMBK, "PROD.GC.COPYLIB", "copybook"), "copybook")               # over a shape
        self.assertEqual(k(ASMBK, "PROD.GC.SRC", "cobol"), "asm")                           # cobol never over a shape
        self.assertEqual(k("anything\n", "PROD.GC.LISTING", "cobol", "X.lst"), "listing")   # nor over a listing
        self.assertEqual(k(VALBK, "PROD.GC.PROCS", "copybook"), "copybook")                 # over the folder name
        self.assertEqual(k(VALBK, "downloads", "copybook", "X.txt"), "copybook")            # over the extension
        self.assertEqual(k(VALBK, "PROD.GC.SRC", "proc"), "proc")
        self.assertEqual(k(PLAINBK, "PROD.GC.MISC", "proc"), "copybook")                    # COBOL lines win
        self.assertEqual(k(STARTBK, "PROD.GC.MISC", "proc"), "copybook")
        self.assertEqual(k("//J JOB A\n//S EXEC PGM=X\n", "PROD.GC.COPYLIB", "copybook"), "jcl")   # strong wins
        self.assertEqual(k("MAP1     DFHMDF POS=(1,1)\n", "PROD.GC.COPYLIB", "copybook"), "bms")
        self.assertEqual(k(VALBK, "PROD.GC.MISC", "asm"), "unknown")                        # not a declarable kind
        self.assertEqual(classify.classify("C:/estate/SHARED/PROD.GC.COPYLIB/X.txt", ASMBK, declared="copybook")[1],
                         "the kind declared for library PROD.GC.COPYLIB in the UI's table, over asm (the shape of an "
                         "Assembler member)")

    def test_blank_card_padding_stays_fast(self):
        import time
        t0 = time.time()
        for head in (((" " * 80) + "\n") * 101, "\r\n" * 4096, ("\t" * 70 + "\n") * 115, (" " * 67 + "\n") * 120):
            classify.classify("X/PROD.GC.UTL/CARD1", head)
        # one long blank line: recover reads 64 KB of a member stored as a listing (the banner's two runs of blanks
        # took 15 s over it - LESSONS 200)
        for head in (" " * 65536, "\t" * 65536, " " * 65536 + "1"):
            classify.listing_hit(head)
            classify.classify("X/PROD.GC.UTL/CARD1", head[:8192])
        self.assertLess(time.time() - t0, 0.5)


class GenuineAssemblerIsNotRefiled(_Estate):
    """Case D: ASMBK is a real Assembler member (label in column 1, CSECT, DS with a type) in a COPYLIB folder, and
    a program copies that name: the build files it asm by its shape, recover does not re-file it, and every
    sentence says why - the copybook the program copies is another member; declare the library copybook if this
    member IS it."""

    files = (("GC/PROD.GC.SRC/ASMPGM.cbl", data_program("ASMPGM", "ASMBK")),
             ("SHARED/PROD.GC.COPYLIB/ASMBK.txt", ASMBK))

    def test_reported_by_its_shape_with_an_honest_sentence(self):
        self.assert_not_found("ASMPGM", "ASMBK", "asm")
        for dry in (True, False):
            stats, said = self.recover(dry_run=dry)
            self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
            self.assertIn("  1 copybook name(s) exist in the index only as a member the classifier typed by a line of its text "
                          "(asm) and this run could not re-file: 1 program(s) stay parsed only in part - no folder change helps; "
                          "the report says why for each", said)
            self.assertNotIn("re-filed as copybook", said)
            self.assertNotIn("next: rename the folder", said)
            self.assertNotIn("ROADMAP re-parse item 22", said)
            self.assertIn("next: run again as", said)                   # the listings may hold the real copybook
            self.assertEqual(self.member("ASMBK")[:3], ("asm", "PROD.GC.COPYLIB", "skipped"))
            rep = self.report_text()
            self.assertIn(f"| ASMBK | asm | PROD.GC.COPYLIB | ASMPGM | {SHAPE_WHY} | filed as asm by its content ({ASM_SEEN}) |",
                          rep)
            self.assertNotIn("## Re-filed as copybook", rep)
            self.assertNotIn("wait for", rep)
        cov, nf, prog, book, as_prog = self.outputs("ASMPGM", "ASMBK")
        self.assertIn(f"| ASMBK | 1 | yes: PROD.GC.COPYLIB, filed as asm by its content ({ASM_SEEN}) - not a kind the build "
                      f"expands, and {SHAPE_WHY} |", nf)
        self.assertIn("1 exists only as a member the classifier typed by a line of its text (asm): no folder change helps - "
                      "atlas.recover cannot re-file it; the 'Copybooks not found' table says why", cov)
        self.assertIn(f"and {SHAPE_WHY} |", prog)
        self.assertIn(f"filed as asm by its content (folder PROD.GC.COPYLIB; {ASM_SEEN}): not a kind the build expands, so "
                      f"every program that copies it is parsed only in part. {SHAPE_WHY[0].upper() + SHAPE_WHY[1:]}.", book)
        self.assertIn(f"in PROD.GC.COPYLIB it is filed as asm by its content ({ASM_SEEN}) - {SHAPE_WHY}.", as_prog)
        for text in (nf.split("\n>")[0], prog, book, as_prog):             # (coverage's legend names the older index's cases)
            self.assertNotIn("START-", text)
            self.assertNotIn("wait for", text)


class RealAssemblerShapesAreNotRefiled(_Estate):
    """LESSONS 187 (1): three real Assembler members in a COPYLIB folder that the first shape guard judged 'no
    Assembler shape' - their copiers then went 'ok' with Assembler text expanded as COBOL. A label of any length
    (or none) before CSECT, START with a quoted operand, DC with an address constant: filed asm by the classifier,
    refused by recover with the honest sentence, and the build expands nothing."""

    files = (("GC/PROD.GC.SRC/TRK1P.cbl", data_program("TRK1P", "LONGLBL")),
             ("GC/PROD.GC.SRC/TRK2P.cbl", data_program("TRK2P", "NOLABEL")),
             ("GC/PROD.GC.SRC/TRK3P.cbl", data_program("TRK3P", "HEXSTART")),
             ("SHARED/PROD.GC.COPYLIB/LONGLBL.txt", LONGLBL),
             ("SHARED/PROD.GC.COPYLIB/NOLABEL.txt", NOLABEL),
             ("SHARED/PROD.GC.COPYLIB/HEXSTART.txt", HEXSTART))
    pairs = (("TRK1P", "LONGLBL"), ("TRK2P", "NOLABEL"), ("TRK3P", "HEXSTART"))

    def test_the_three_shapes_are_refused_and_nothing_is_expanded(self):
        for prog, book in self.pairs:
            self.assert_not_found(prog, book, "asm")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 3, 0), said)
        self.assertIn("  3 copybook name(s) exist in the index only as a member the classifier typed by a line of its text (asm) "
                      "and this run could not re-file: 3 program(s) stay parsed only in part - no folder change helps; the "
                      "report says why for each", said)
        rep = self.report_text()
        self.assertNotIn("## Re-filed as copybook", rep)
        for prog, book in self.pairs:
            self.assertEqual(self.member(book)[:3], ("asm", "PROD.GC.COPYLIB", "skipped"))
            self.assertIn(f"| {book} | asm | PROD.GC.COPYLIB | {prog} | {SHAPE_WHY} | filed as asm by its content (line 1 ", rep)
        self.build()
        for prog, book in self.pairs:
            self.assertEqual(self.status(prog), "partial")
            self.assertEqual(self.copy_use(prog, book), [(None,)])
            self.assertEqual(self.q("SELECT COUNT(*) FROM pfield f JOIN program p ON p.id=f.program_id JOIN member m ON "
                                    "m.id=p.member_id WHERE m.name=? AND f.name IN ('TABLE','PLHEX','PLLONGLABEL')", prog), [(0,)])

    def test_a_real_member_in_a_folder_with_no_hint_is_refused_as_what_it_is(self):
        # the shape is checked before the folder: the refusal says what the member IS - never 'a folder ending in
        # COPYLIB would let this tool re-file it', which the next run, refusing on the shape, would make false
        os.rename(os.path.join(self.root, "SHARED", "PROD.GC.COPYLIB"), os.path.join(self.root, "SHARED", "PROD.GC.ASMLIB"))
        self.build()
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"]), (0, 3), said)
        self.assertNotIn(recover.FOLDER_LETS, self.report_text())
        self.assertNotIn("next: rename the folder", said)
        self.assertIn(SHAPE_WHY, self.report_text())


class DeclaredKinds(_Estate):
    """LESSONS 187 (3) and ROADMAP 22 (c): the kind declared for a library in the UI's table (manifest kinds).
    VALBK (no signature at all) in SHARED\\PROD.GC.MISC declared 'proc': the reading says 'by its declared kind'
    with 'declare it copybook there' - never 'the file now reads as unknown ... run the build first'; declared
    copybook, the build makes the program whole. A declaration now also wins over a shape and a folder name: ASMBK
    in a library declared copybook is a copybook (the instruction the shape refusal gives), VALBK in a PROCS folder
    declared copybook too; a COBOL copybook's own lines win over a declaration."""

    files = (("GC/PROD.GC.SRC/VALPGM.cbl", value_program("VALPGM", "VALBK")),
             ("SHARED/PROD.GC.MISC/VALBK.txt", VALBK),
             ("GC/PROD.GC.SRC/ASMPGM.cbl", data_program("ASMPGM", "ASMBK")),
             ("SHARED/PROD.GC.ASMCPY/ASMBK.txt", ASMBK),
             ("GC/PROD.GC.SRC/PRPGM.cbl", value_program("PRPGM", "PRVALBK")),
             ("SHARED/PROD.GC.PROCS/PRVALBK.txt", VALBK),
             ("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PLAINBK")),
             ("SHARED/PROD.GC.PMISC/PLAINBK.txt", PLAINBK))
    SEEN = ("the kind declared for library PROD.GC.MISC in the UI's table (sources.json, the manifest kinds) - the classifier "
            "itself read no signature and no folder hint (unrecognised member of library PROD.GC.MISC (extension .txt ignored))")

    def before_build(self):
        self.manifest = os.path.join(self.td, "manifest.json")
        self.declare({"PROD.GC.MISC": "proc", "PROD.GC.PMISC": "proc"})

    def declare(self, kinds):
        with open(self.manifest, "w", encoding="utf-8") as fh:
            json.dump({"kinds": kinds}, fh)

    def test_by_its_declared_kind_never_run_the_build_first(self):
        self.assert_not_found("VALPGM", "VALBK", "proc", "PROD.GC.MISC")
        # a COBOL copybook's own lines win over the declaration
        self.assertEqual(self.member("PLAINBK")[:2], ("copybook", "PROD.GC.PMISC"))
        self.assertEqual(self.status("SECPGM"), "ok")
        conn = query.connect(self.db)
        try:
            other = recover.members_named(conn, "VALBK")[1]
            rs = recover.member_readings(other, conn)
            self.assertEqual([(r["kind"], r["by"], r["over"], r["refile"], r["why_not"]) for r in rs],
                             [("proc", "declared", "", False, "")])
            self.assertEqual(rs[0]["seen"], self.SEEN)
            self.assertEqual(recover.filed_phrase(rs[0]), f"filed as proc by its declared kind ({self.SEEN})")
            self.assertEqual(recover.folder_fix(rs[0]), recover.DECLARED_FIX)
            self.assertEqual(recover.member_readings(other)[0]["by"], "changed", "without the stored sha nothing tells")
        finally:
            conn.close()
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 3, 0), said)    # VALBK, ASMBK, PRVALBK
        rep = self.report_text()
        self.assertIn(f"| VALBK | proc | PROD.GC.MISC | VALPGM | {recover.DECLARED_FIX} | filed as proc by its declared kind "
                      f"({self.SEEN}) |", rep)
        self.assertNotIn("run the build first", rep)
        cov, nf, prog, book, as_prog = self.outputs("VALPGM", "VALBK")
        self.assertIn(f"| VALBK | 1 | yes: PROD.GC.MISC, filed as proc by its declared kind - not a kind the build expands: "
                      f"{recover.DECLARED_CELL} |", nf)
        for text in (cov, prog, book, as_prog):
            self.assertNotIn("run the build first", text)
            self.assertNotIn("the folder name decided the kind", text)
        # the instruction, followed: declared copybook - the build (the manifest changed) files it so
        self.declare({"PROD.GC.MISC": "copybook", "PROD.GC.PMISC": "proc"})
        self.build()
        self.assertEqual((self.member("VALBK")[:3], self.status("VALPGM")), (("copybook", "PROD.GC.MISC", "ok"), "ok"))
        self.assertEqual(self.copy_use("VALPGM", "VALBK"), [(self.member_id("VALBK"),)])

    def test_a_declaration_wins_over_a_shape_and_a_folder_name(self):
        # before: the declared kind replaced 'unknown' only, so 'declare the library's kind' never helped a member a
        # signature or a folder name had typed (LESSONS 186)
        self.assert_not_found("ASMPGM", "ASMBK", "asm", "PROD.GC.ASMCPY")
        self.assert_not_found("PRPGM", "PRVALBK", "proc", "PROD.GC.PROCS")
        stats, said = self.recover()
        rep = self.report_text()
        self.assertIn(f"| PRVALBK | proc | PROD.GC.PROCS | PRPGM | {MISFILED_FIX} | filed as proc by its folder (the folder "
                      "name ends in PROCS) |", rep)
        cov, nf, _prog, _book, _as_prog = self.outputs("PRPGM", "PRVALBK")
        self.assertIn(f"| PRVALBK | 1 | yes: PROD.GC.PROCS, filed as proc - {MISFILED_CELL} |", nf)
        # the instruction, followed: both libraries declared copybook in the UI's table
        self.declare({"PROD.GC.MISC": "proc", "PROD.GC.PMISC": "proc", "PROD.GC.ASMCPY": "copybook",
                      "PROD.GC.PROCS": "copybook"})
        self.build()
        self.assertEqual(self.member("ASMBK")[:2], ("copybook", "PROD.GC.ASMCPY"))
        self.assertEqual(self.member("PRVALBK")[:2], ("copybook", "PROD.GC.PROCS"))
        self.assertEqual((self.status("ASMPGM"), self.status("PRPGM")), ("ok", "ok"))
        # a declaration over what the classifier reads: the reading says so, and the fix drops the rename
        self.declare({"PROD.GC.MISC": "proc", "PROD.GC.PMISC": "proc", "PROD.GC.ASMCPY": "proc"})
        self.build()
        conn = query.connect(self.db)
        try:
            r = recover.member_readings(recover.members_named(conn, "ASMBK")[1], conn)[0]
            self.assertEqual((r["kind"], r["by"], r["over"]), ("proc", "declared", "weak"))
            self.assertEqual(r["seen"], "the kind declared for library PROD.GC.ASMCPY in the UI's table (sources.json, the "
                                        "manifest kinds), which the build puts before what the classifier itself reads: asm "
                                        "(the shape of an Assembler member)")
            self.assertEqual(recover.folder_fix(r), recover.DECLARED_OVER_FIX)
            self.assertIn(f"filed as proc by its declared kind - not a kind the build expands: {recover.DECLARED_OVER_CELL}",
                          query.same_named_note(conn, "ASMBK"))
        finally:
            conn.close()


class AnIndexBuiltBeforeTheBatch(_Estate):
    """recover's re-file keeps working on an index the classifier before ROADMAP re-parse items 20 and 22 built:
    STARTBK aged to asm (a START- name), PLAINBK in a PROCS folder aged to proc (the folder name typed it). The
    reading quotes how that classifier filed each, recover re-files both and marks the programs, and the build
    makes them whole; recover then finds nothing."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PLAINBK")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK),
             ("SHARED/PROD.GC.PROCS/PLAINBK.txt", PLAINBK))
    PROCS_SEEN = ("the folder name ends in PROCS - the classifier this index was built with read no content signature; the "
                  "classifier of this toolkit reads it as a copybook (COBOL procedure statements with no level numbers and no "
                  f"DIVISION header) - {ITEMS}")

    def setUp(self):
        super().setUp()
        self.age("STARTBK", "asm")
        self.age("PLAINBK", "proc")

    def test_every_report_says_how_it_was_filed_and_what_to_do(self):
        self.assert_not_found("STPGM", "STARTBK", "asm")
        self.assert_not_found("SECPGM", "PLAINBK", "proc", "PROD.GC.PROCS")
        cov, nf, prog, book, as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn(f"| STARTBK | 1 | yes: PROD.GC.COPYLIB, filed as asm by its content ({AGED_SEEN}) - not a kind the build "
                      f"expands, and {AGED_FIX} |", nf)
        self.assertIn(f"| PLAINBK | 1 | yes: PROD.GC.PROCS, filed as proc by its folder ({self.PROCS_SEEN}) - not a kind the "
                      f"build expands, and {AGED_FIX} |", nf)
        self.assertIn(f"2 exist only as a member the classifier this index was built with filed as another kind (asm, proc) "
                      f"and the classifier of this toolkit reads as a copybook ({ITEMS}): no folder change helps - `python -m "
                      "atlas.recover --db atlas.db` re-files them as a copybook in the index, then build", cov)
        self.assertIn(f"| STARTBK | **NOT FOUND** - a member with this name exists: PROD.GC.COPYLIB, filed as asm by its content "
                      f"({AGED_SEEN}) - not a kind the build expands, and {AGED_FIX} |", prog)
        self.assertIn(f"**NOT FOUND** as a copybook - a member with this name exists, filed as asm by its content (folder "
                      f"PROD.GC.COPYLIB; {AGED_SEEN}): not a kind the build expands, so every program that copies it is parsed "
                      f"only in part. {AGED_FIX[0].upper() + AGED_FIX[1:]}.", book)
        self.assertIn(f"in PROD.GC.COPYLIB it is filed as asm by its content ({AGED_SEEN}) - {AGED_FIX}.", as_prog)
        conn = query.connect(self.db)
        try:
            self.assertIn(f"filed as proc by its folder (folder PROD.GC.PROCS; {self.PROCS_SEEN})",
                          query.cmd_copybook(conn, "PLAINBK"))
            accepted, other = recover.members_named(conn, "startbk")
            r = recover.member_readings(other, conn)[0]
            self.assertEqual((r["kind"], r["by"], r["earlier"], r["line"], r["word"], r["refile"]),
                             ("asm", "content", "content", 2, "START-DATE", True))
            self.assertEqual(recover.content_fix(r), AGED_FIX)
            self.assertEqual(recover.content_fix(r, dry_run=True), recover.EARLIER_FIX_DRY)
            self.assertEqual(recover.member_readings(other)[0]["by"], "changed", "without the stored sha nothing tells")
        finally:
            conn.close()

    def test_dry_run_then_the_run_then_the_build(self):
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["misfiled"], stats["refiled"], stats["marked"]), (2, 0, 0), said)
        self.assertIn("  2 misfiled copybook(s) would be re-filed as copybook in the index (the classifier this index was "
                      "built with had filed them as asm/proc; the classifier of this toolkit reads them as copybooks - "
                      f"{ITEMS}): 2 program(s) would be marked for the next build (dry run: nothing changed)", said)
        self.assertNotIn("next: rename the folder", said)
        self.assertIn(f"| STARTBK | asm | PROD.GC.COPYLIB | STPGM | {recover.EARLIER_FIX_DRY} | filed as asm by its content "
                      f"({AGED_SEEN}) |", self.report_text())
        self.assertEqual(self.member("STARTBK")[0], "asm")
        stats, said = self.recover()
        self.assertEqual((stats["misfiled"], stats["refiled"], stats["marked"]), (0, 2, 2), said)
        self.assertIn("  2 misfiled copybook(s) re-filed as copybook in the index (the classifier this index was built with had "
                      f"filed them as asm/proc; the classifier of this toolkit reads them as copybooks - {ITEMS}): 2 "
                      "program(s) marked for the next build", said)
        self.assertIn("next: run your usual build command", said)
        kind, _f, status, error = self.member("STARTBK")
        self.assertEqual((kind, status), ("copybook", "skipped"))
        self.assertTrue(error.startswith(recover.REFILED_MARK + " on 20"), error)
        self.assertTrue(error.endswith(": the classifier this index was built with filed it as asm by its content, the "
                                       f"classifier of this toolkit reads it as a copybook ({ITEMS}); the programs copying "
                                       "it were marked"), error)
        self.assertIn("by its folder, the classifier of this toolkit", self.member("PLAINBK")[3])
        self.assertEqual((self.status("STPGM"), self.status("SECPGM")), ("pending", "pending"))
        rep = self.report_text()
        self.assertIn("## Re-filed as copybook", rep)
        self.assertIn(f"| STARTBK | asm | {AGED_SEEN} | PROD.GC.COPYLIB | STPGM |", rep)
        self.assertIn(f"| PLAINBK | proc | {self.PROCS_SEEN} | PROD.GC.PROCS | SECPGM |", rep)
        self.assertIn(recover.REPARSE_FILES, rep)
        self.assertNotIn("--rebuild before that files them as before", rep)
        # before the build: every report says re-filed, the programs marked
        cov, nf, prog, book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("| STARTBK | 1 | yes: PROD.GC.COPYLIB (copybook, re-filed by atlas.recover) - the programs copying it are "
                      "marked: run your usual build command |", nf)
        self.assertIn(f"- 2 copybooks marked `skipped` were re-filed by atlas.recover (the classifier had filed them as another "
                      f"kind - {ITEMS}): not a gap in the parse", cov)
        self.assertIn("parsed while this member was filed as `asm` by a line of its text; atlas.recover re-filed it and marked "
                      "them - run your usual build command.", book)
        conn = query.connect(self.db)
        try:
            self.assertIn("parsed while this member was filed as `proc` by its folder; atlas.recover re-filed it",
                          query.cmd_copybook(conn, "PLAINBK"))
        finally:
            conn.close()
        # the next INCREMENTAL build (the toolkit unchanged since the aged build): the member row kept, the programs
        # parsed again against the copybook's text on disk
        self.build()
        self.assertEqual(self.member("STARTBK")[:3], ("copybook", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual((self.status("STPGM"), self.status("SECPGM")), ("ok", "ok"))
        mid = self.member_id("STARTBK")
        self.assertEqual(self.fields("STPGM", "ST-ID", "START-DATE", "ST-AMT"),
                         [("ST-ID", 0, 5, mid), ("START-DATE", 5, 8, mid), ("ST-AMT", 13, 3, mid)])
        self.assertIn(("A-110-DO", "paragraph", "A-100-BEGIN"), [(p[0], p[1], p[2]) for p in self.paragraphs("SECPGM")])
        cov, nf, prog, book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("_none_", nf)
        self.assertNotIn("NOT FOUND", prog)
        self.assert_nothing_to_do()
        # a build that re-parses every member (a --rebuild): the classifier files both copybooks itself, with rows
        self.build(["--rebuild"])
        self.assertEqual((self.member("STARTBK")[:3], self.member("PLAINBK")[:3]),
                         (("copybook", "PROD.GC.COPYLIB", "ok"), ("copybook", "PROD.GC.PROCS", "ok")))
        self.assertEqual(self.q("SELECT COUNT(*) FROM field WHERE member_id=?", self.member_id("STARTBK")), [(3,)])
        self.assert_nothing_to_do()


class OneProgramCopyingTwoRefiled(_Estate):
    """LESSONS 200, on an index built before the batch: STARTBK (aged asm) and MMAPBK (aged listing) are re-filed;
    TWOPGM copies both, STPGM copies STARTBK. The dry run said 2 programs would be marked and the run said 3 - it
    counted a program once for each re-filed copybook it copies. Both say 2 now, and 2 are pending."""

    files = (("GC/PROD.GC.SRC/TWOPGM.cbl",
              HEAD.format(name="TWOPGM", data="       01  WS-REC.\n           COPY STARTBK.\n       01  WS-MM.\n"
                                              "           COPY MMAPBK.\n",
                          main="           MOVE SPACES TO START-DATE.\n           MOVE SPACES TO MM-ID.\n")
              + PLAIN_SECTION + TAIL),
             ("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK), ("SHARED/PROD.GC.COPYLIB/MMAPBK.txt", MMAPBK))

    def setUp(self):
        super().setUp()
        self.age("STARTBK", "asm")
        self.age("MMAPBK", "listing")

    def test_each_program_counted_once(self):
        self.assert_not_found("TWOPGM", "MMAPBK", "listing")
        stats, said = self.recover(dry_run=True)
        self.assertIn("): 2 program(s) would be marked for the next build (dry run: nothing changed)", said)
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["marked"]), (2, 2), said)
        self.assertIn("  2 misfiled copybook(s) re-filed as copybook in the index (", said)
        self.assertIn("): 2 program(s) marked for the next build", said)
        self.assertEqual(self.q("SELECT name FROM member WHERE parse_status='pending' ORDER BY name"),
                         [("STPGM",), ("TWOPGM",)])
        self.build()
        self.assertEqual((self.status("TWOPGM"), self.status("STPGM")), ("ok", "ok"))
        self.assert_nothing_to_do()


class TheFirstBuildAfterTheBatch(_Estate):
    """His re-parse night on an index built before the batch (ROADMAP 19's sequence: pull, recover, build): the
    index's last build recorded an older toolkit's fingerprint. recover re-files the member; the build re-parses
    every member ('toolkit changed since the last build') and the classifier files it a copybook with its own rows -
    and the same build without recover first does it too."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK))

    def test_recover_then_the_build(self):
        self.age("STARTBK", "asm", fingerprint=PRE_BATCH)
        self.assert_not_found("STPGM", "STARTBK", "asm")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["marked"]), (1, 1), said)
        out = self.build()
        self.assertIn("changed 2  unchanged 0", out)                    # the toolkit changed: every member re-parsed
        self.assertEqual(self.member("STARTBK")[:3], ("copybook", "PROD.GC.COPYLIB", "ok"))
        self.assertIsNone(self.member("STARTBK")[3])
        self.assertEqual(self.status("STPGM"), "ok")
        self.assertEqual(self.q("SELECT name, offset FROM field WHERE member_id=? ORDER BY offset", self.member_id("STARTBK")),
                         [("ST-ID", 0), ("START-DATE", 5), ("ST-AMT", 13)])
        self.assert_nothing_to_do()

    def test_the_build_alone(self):
        self.age("STARTBK", "asm", fingerprint=PRE_BATCH)
        self.build()
        self.assertEqual((self.member("STARTBK")[:3], self.status("STPGM")), (("copybook", "PROD.GC.COPYLIB", "ok"), "ok"))
        self.assert_nothing_to_do()


class SecondRunBeforeTheBuild(_Estate):
    """LESSONS 187 (4), on an index built before the batch: a second run of recover before the build finds the member
    it re-filed a run earlier as a copybook-kind member and the program's COPY still unresolved - not 'a copybook
    that has arrived since they were parsed': it waits for the build, and says so."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK))

    def test_waiting_for_the_build_not_arrived(self):
        self.age("STARTBK", "asm")
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["waiting"]), (1, 0), said)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["misfiled"], stats["refiled"], stats["waiting"], stats["marked"]),
                         (0, 0, 0, 1, 0), said)
        self.assertIn("  1 copybook(s) re-filed on an earlier run wait for the build (1 program(s) marked already): run your "
                      "usual build command", said)
        self.assertNotIn("has arrived since", said)
        self.assertIn("next: run your usual build command", said)
        self.assertEqual(self.status("STPGM"), "pending")
        rep = self.report_text()
        self.assertIn("## Re-filed on an earlier run - waiting for the build", rep)
        self.assertIn("| copybook | folder | programs |\n|---|---|---|\n| STARTBK | PROD.GC.COPYLIB | STPGM |", rep)
        self.assertNotIn("## Copybooks that arrived after the program was parsed", rep)
        cov, nf, prog, book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("| STARTBK | 1 | yes: PROD.GC.COPYLIB (copybook, re-filed by atlas.recover) - the programs copying it are "
                      "marked: run your usual build command |", nf)
        for text in (nf.split("\n>")[0], prog, book):
            self.assertNotIn("parsed before it arrived", text)
        conn = query.connect(self.db)
        try:
            arrived, misfiled, waiting = recover.arrival_scan(conn)
            self.assertEqual((arrived, misfiled, [e["copybook"] for e in waiting]), ([], [], ["STARTBK"]))
        finally:
            conn.close()
        self.build()
        self.assertEqual(self.status("STPGM"), "ok")
        self.assert_nothing_to_do()


class DryRunWithAFolderTypedCopyToo(_Estate):
    """Case H (LESSONS 188 (2)), on an index built before the batch: STARTBK.txt in PROD.GC.COPYLIB (aged to asm,
    re-filable) and STARTBK.txt in PROD.GC.PROCS (a literal with no signature: proc by its folder, now as then), one
    program. The dry run says 'run without --dry-run first' and no folder line; the run re-files the COPYLIB copy,
    leaves the PROCS copy alone, and the build resolves the program to the copybook member."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK),
             ("SHARED/PROD.GC.PROCS/STARTBK.txt", VALBK))

    def members(self):
        return self.q("SELECT kind, library, parse_status FROM member WHERE name='STARTBK' ORDER BY library")

    def test_the_dry_run_sends_him_to_no_folder_and_the_run_re_files_the_one_copy(self):
        # the build of this batch: the COPYLIB copy is a copybook, the program whole, nothing for recover
        self.assertEqual([(k, f) for k, f, _s in self.members()], [("copybook", "PROD.GC.COPYLIB"), ("proc", "PROD.GC.PROCS")])
        self.assertEqual(self.status("STPGM"), "ok")
        self.age("STARTBK", "asm", library="PROD.GC.COPYLIB")
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("partial", [(None,)]))
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (0, 1, 0), said)
        self.assertIn("every one of them is the name of a member this run would re-file as a copybook (above): run without "
                      "--dry-run first - the listings only if a program still says NOT FOUND after the build", said)
        for wrong in ("does not expand", "next: rename the folder", "the folder fix comes first", "next: run your usual build"):
            self.assertNotIn(wrong, said)
        rep = self.report_text()
        row = [ln for ln in rep.splitlines() if ln.startswith("| STARTBK | ") and " STPGM | " in ln and "PROCS" in ln]
        self.assertEqual(len(row), 1, rep)
        self.assertIn(MISFILED_FIX, row[0])
        self.assertIn("the copy filed by its content: " + recover.EARLIER_FIX_DRY, row[0])
        self.assertIn("filed as proc by its folder (the folder name ends in PROCS)", row[0])
        self.assertIn(f"filed as asm by its content ({AGED_SEEN})", row[0])
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        for wrong in ("does not expand", "next: rename the folder", "the folder fix comes first"):
            self.assertNotIn(wrong, said)
        self.assertEqual(self.members(), [("copybook", "PROD.GC.COPYLIB", "skipped"), ("proc", "PROD.GC.PROCS", "ok")])
        self.build()
        self.assertEqual(self.status("STPGM"), "ok")
        cid = self.q("SELECT id FROM member WHERE name='STARTBK' AND kind='copybook'")[0][0]
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(cid,)])
        self.assert_nothing_to_do()


class OkWithAnUnlinkedCopyRow(_Estate):
    """Case I (LESSONS 188 (3)): an 'ok' program whose COPY row no member resolves any more - the state the build
    before the batch left when a re-filed copybook's text changed on disk and the classifier filed the new text asm
    again. `program` says so beside 'parse: ok', coverage counts it apart; recover re-files the member and the build
    makes the program whole. On an index this toolkit built the text change heals itself: the new text is a
    copybook, and the build parses the program again with it."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK))
    NOTE = ("parse: ok, but 1 COPY row is unresolved (STARTBK): the member it had expanded went out of the index since - "
            "its text changed on disk and the classifier filed the new text as another kind, the file went, or it was "
            "recorded again under a new id with its text unchanged - and the build that made this index did not parse "
            "the program again, so its fields are those of the earlier read of that copybook; run "
            "`python -m atlas.recover --db atlas.db`, then the build (the Copybooks table below says what happened to each)")
    CLAUSE = ("### Members parsed only in part\n_none_\n\n> Not counted above: 1 program marked `ok` has a COPY row no "
              "member resolves any more (STPGM; copybook STARTBK): the member it had expanded went out of the index since "
              "- its text changed on disk and the classifier filed the new text as another kind, the file went, or it was "
              "recorded again under a new id with its text unchanged - and the build that made this index did not parse "
              "the program again, so its fields are those of the earlier read. `program NAME` says so "
              "beside `parse: ok`; run `python -m atlas.recover --db atlas.db`, then the build - the 'Copybooks not "
              "found' table names each copybook with what to do.\n")

    def test_on_this_batch_the_changed_text_heals_itself(self):
        self.write("SHARED/PROD.GC.COPYLIB/STARTBK.txt", STARTBK + "           05  ST-NEW              PIC X(4).\n")
        self.build()
        new = self.member_id("STARTBK")
        self.assertEqual((self.member("STARTBK")[0], self.status("STPGM"), self.copy_use("STPGM", "STARTBK")),
                         ("copybook", "ok", [(new,)]))
        self.assertEqual(self.q("SELECT name, length FROM pfield WHERE name='WS-REC'"), [("WS-REC", 20)])
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
        finally:
            conn.close()
        self.assert_nothing_to_do()

    def test_the_older_state_is_said_and_healed(self):
        # the state the build before the batch left: the program ok with the old fields, its COPY row NULL, the member
        # filed asm (set by SQL here, as that build did)
        old = self.member_id("STARTBK")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE copy_use SET resolved_member_id=NULL WHERE resolved_member_id=?", (old,))
            conn.execute("DELETE FROM field WHERE member_id=?", (old,))
            conn.execute("UPDATE member SET kind='asm', parse_status='skipped' WHERE id=?", (old,))
            conn.commit()
        finally:
            conn.close()
        pid = self.member_id("STPGM")
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {pid: ("STPGM", ["STARTBK"])})
            self.assertEqual(query.unlinked_ok_note(conn, pid, "ok"), ", but" + self.NOTE.split(", but")[1])
        finally:
            conn.close()
        cov, nf, prog, _book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn(self.NOTE, prog)
        self.assertIn(self.CLAUSE, cov)
        stats, said = self.recover()
        self.assertEqual((stats["refiled"], stats["misfiled"], stats["marked"]), (1, 0, 1), said)
        self.assertEqual(self.status("STPGM"), "pending")
        self.build()
        self.assertEqual((self.member("STARTBK")[:3], self.status("STPGM")), (("copybook", "PROD.GC.COPYLIB", "skipped"), "ok"))
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(old,)])
        cov, nf, prog, _book, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertNotIn("but 1 COPY row", prog)
        self.assertNotIn("Not counted above", cov)

    def test_a_system_include_is_never_counted(self):
        """A program copying SQLCA (the compiler supplies it: a NULL row by design) is not an un-linked one."""
        self.write("GC/PROD.GC.SRC/SQLPGM.cbl", HEAD.format(name="SQLPGM", data="           EXEC SQL INCLUDE SQLCA END-EXEC.\n",
                                                                    main="") + PLAIN_SECTION + TAIL)
        self.build()
        self.assertEqual(self.status("SQLPGM"), "ok")
        self.assertEqual(self.copy_use("SQLPGM", "SQLCA"), [(None,)])
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
            self.assertNotIn("but 1 COPY row", query.cmd_program(conn, "SQLPGM"))
            self.assertNotIn("Not counted above", query.cmd_coverage(conn))
        finally:
            conn.close()


class WrittenCopybookIsFiledRight(_Estate):
    """Case K (LESSONS 191): the copybook's library copy on disk is a bare number (kind 'empty'), the program's
    compiler listing carries the copybook expanded under the `A-100-BEGIN SECTION.  COPY STARTVB.` line, and its
    text holds the COBOL START verb. recover writes it; before the batch the build then filed it asm and it took a
    second recover and a third build - now the build files the written copybook as a copybook at once."""

    files = (("GC/PROD.GC.SRC/SVPGM.cbl", section_program("SVPGM", "STARTVB")),
             ("SHARED/PROD.GC.COPYLIB/STARTVB.txt", "1234567\n"),
             ("GC/PROD.GC.LISTING/SVPGM.lst", ibm_listing(section_program("SVPGM", "STARTVB").splitlines(),
                                                          {"STARTVB": STARTVERB.splitlines()})))

    def test_written_then_one_build(self):
        self.assertEqual(self.member("STARTVB")[:3], ("empty", "PROD.GC.COPYLIB", "skipped"))
        self.assertEqual(self.status("SVPGM"), "partial")
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["rejected"], stats["misread"], stats["refiled"]), (1, 0, 0, 0), said)
        self.assertIn("recovered: 1 of 1 missing copybooks", said)
        self.assertNotIn("misreads", said)
        self.assertNotIn("the build's classifier reads it as", self.report_text())
        self.build()
        self.assertEqual(self.status("SVPGM"), "ok")
        names = [(p[0], p[1]) for p in self.paragraphs("SVPGM")]
        self.assertIn(("A-100-BEGIN", "section"), names)
        self.assertIn(("S-100-POSITION", "paragraph"), names)
        resolved = self.copy_use("SVPGM", "STARTVB")[0][0]
        self.assertEqual(self.q("SELECT kind, library FROM member WHERE id=?", resolved), [("copybook", "RECOVERED-COPYBOOKS")])
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["refiled"], stats["marked"], stats["misread"]), (0, 0, 0, 0), said)

    def test_a_real_assembler_block_is_still_refused(self):
        # the listing carries an Assembler control section where the copybook should be: not written
        self.write("GC/PROD.GC.LISTING/SVPGM.lst", ibm_listing(section_program("SVPGM", "STARTVB").splitlines(),
                                                                {"STARTVB": ASMBK.splitlines()}))
        self.build()
        stats, said = self.recover()
        self.assertEqual((stats["written"], stats["rejected"], stats["misread"]), (0, 1, 0), said)
        rep = self.report_text()
        self.assertIn("## Rejected", rep)
        self.assertIn("| STARTVB |", rep.split("## Rejected")[1].split("\n## ")[0])
        self.assertFalse(os.path.exists(os.path.join(self.root, "SHARED", "RECOVERED-COPYBOOKS", "STARTVB.cpy")))


class TheDocsSayIt(unittest.TestCase):
    """ROADMAP items 20 and 22 in delivered wording, README's classification sentence, the Field Manual, and the
    LESSONS rows: 186-189 keep the history, the batch's own row names the tests."""

    def read(self, *path):
        with open(os.path.join(os.path.dirname(HERE), *path), encoding="utf-8") as fh:
            return fh.read()

    def test_roadmap_20_and_22_are_delivered(self):
        roadmap = self.read("ROADMAP.md")
        item20 = roadmap.split("\n20. ")[1].split("\n21. ")[0]
        item22 = roadmap.split("\n22. ")[1].split("\n23. ")[0]
        self.assertTrue(item20.startswith("**Delivered in the batch - "), item20[:80])
        self.assertTrue(item22.startswith("**Delivered in the batch - "), item22[:80])
        self.assertIn("recover._ASM_SHAPE", item22)
        self.assertIn("declared_wins", item22)
        self.assertIn("tests/test_refiled_copybooks.py", item22)

    def test_readme_and_the_field_manual(self):
        readme = " ".join(self.read("README.md").split())                   # the sentences, not the line breaks
        manual = " ".join(self.read("docs", "FieldManual.html").split())
        for text in (readme, manual):
            self.assertIn("A COBOL copybook is typed by its own lines first", text)
            self.assertIn("A declared kind wins over a shape, a folder name and an extension", text)
            self.assertNotIn("files such a member as before", text)
            self.assertNotIn("weak signatures checked before the level numbers", text)

    def test_lessons(self):
        lessons = self.read("LESSONS.md")
        for n in (186, 187, 188, 189):
            self.assertIn(f"\n| {n} | ", lessons)
        self.assertIn("TheBuildFilesThemRight", lessons)


class TheVerdict(unittest.TestCase):
    """How a member was filed and whether recover re-files it, member by member on hand-made readings."""

    def setUp(self):
        self.td = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def path(self, folder, text, name="X.txt"):
        p = os.path.join(self.td, folder, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def reading(self, folder, text, kind=None, aged=False, name="X.txt"):
        """The reading of a member the index holds as `kind` (by default what the classifier reads); `aged`: the
        stored sha is the file's (an index built with the same bytes)."""
        p = self.path(folder, text, name)
        sha = None
        if aged:
            with open(p, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
        r = recover.how_classified(p, kind or classify.classify(p, text)[0], sha)
        ok, why = recover.refile_verdict(r, folder)
        return r, ok, why

    def test_an_older_index_is_re_filed(self):
        # the classifier of this toolkit reads each as a copybook and the index holds the older kind for the same
        # bytes: the reading quotes how the older classifier filed it, and the member is re-filed
        for folder, text, old, by, word in (("PROD.GC.COPYLIB", STARTBK, "asm", "content", "START-DATE"),
                                            ("PROD.GC.OTHER", STARTBK, "asm", "content", "START-DATE"),
                                            ("PROD.GC.CPYSRC", PROCBOOK, "asm", "content", "START-PARA"),
                                            ("PROD.GC.COPYLIB", MMAPBK, "listing", "content", "MODULE MAP"),
                                            ("PROD.GC.COPYLIB", MSGBK, "mfs", "content", "MSG"),
                                            ("PROD.GC.COPYLIB", STARTVERB, "asm", "content", "START"),
                                            ("PROD.GC.COPYLIB", "      * BMS FIELD DFHMDF MAPPED HERE\n" + PLAINBK, "bms",
                                             "content", "DFHMDF"),
                                            ("PROD.GC.PROCS", PLAINBK, "proc", "folder", ""),
                                            ("downloads", PLAINBK, "doc", "extension", ""),
                                            ("PROD.GC.MISC", PLAINBK, "proc", "declared kind", ""),
                                            ("PROD.GC.COPYLIB", STARTBK + "//SYSIN DD *\n", "asm", "content", "START-DATE")):
            r, ok, why = self.reading(folder, text, old, aged=True)
            self.assertEqual((r["kind"], r["by"], r["earlier"], r["word"], ok, why), (old, "content", by, word, True, ""),
                             (folder, text))
            self.assertIn(f"the classifier of this toolkit reads it as a copybook (", r["seen"])
            self.assertTrue(r["seen"].endswith(ITEMS), r["seen"])
            self.assertEqual(recover.filed_phrase(r), f"filed as {old} by its {by} ({r['seen']})")
        r, ok, why = self.reading("PROD.GC.COPYLIB", STARTBK, "asm", aged=True)
        self.assertEqual((r["line"], r["seen"]), (2, AGED_SEEN))
        # without the stored sha nothing tells an older index from a file changed since the build
        r, ok, why = self.reading("PROD.GC.COPYLIB", STARTBK, "asm")
        self.assertEqual((r["by"], ok), ("changed", False))
        self.assertIn("run the build first", r["seen"])

    def test_what_is_not(self):
        # the shapes: refused as what they are, wherever they sit - never sent to a COPYLIB folder
        for folder in ("PROD.GC.COPYLIB", "PROD.GC.ASMSRC"):
            for text in (ASMBK, LONGLBL, NOLABEL, HEXSTART, "PGM      START SYM\n", "V        DC    V(SUBPGM)\n",
                         "         DFHEIENT\n"):
                r, ok, why = self.reading(folder, text)
                self.assertEqual((r["kind"], r["by"], ok), ("asm", "content", False), text)
                self.assertTrue(why.startswith("it has the shape of an Assembler member"), why)
                self.assertNotIn(recover.FOLDER_LETS, why)
        for text in ("MYFMT    FMT\n         DEV   TYPE=3270-A2\n", "         DFLD  POS=(1,2),LTH=8\n"):
            r, ok, why = self.reading("PROD.GC.COPYLIB", text)
            self.assertEqual((r["kind"], ok), ("mfs", False), text)
            self.assertIn("it has the shape of MFS source", why)
        r, ok, why = self.reading("PROD.GC.COPYLIB", "1PP 5655-EC6 IBM Enterprise COBOL for z/OS  6.3.0\n" + MMAPBK)
        self.assertEqual((r["kind"], r["word"], ok), ("listing", "1PP 5655-", False))
        self.assertIn("it has the shape of a compiler listing", why)
        # a JCL line in a member of a shape: the JCL line says it first
        r, ok, why = self.reading("PROD.GC.COPYLIB", ASMBK + "//SYSIN DD *\n")
        self.assertEqual((r["kind"], ok), ("asm", False))
        self.assertTrue(why.startswith("it holds a JCL line (//) - if it is the copybook after all, declare its library "
                                       "copybook in the UI's table"), why)
        # a strong signature is never overridden
        r, ok, why = self.reading("PROD.GC.COPYLIB", "//MYJOB   JOB (ACCT)\n" + STARTBK)
        self.assertEqual((r["kind"], r["by"], ok), ("jcl", "content", False))
        self.assertIn("its text carries a jcl signature", why)
        r, ok, why = self.reading("PROD.GC.COPYLIB", "MAP1     DFHMDF POS=(1,1),LENGTH=8\n" + PLAINBK)
        self.assertEqual((r["kind"], ok), ("bms", False))
        self.assertIn("its text carries a bms signature", why)
        # a program with a comment naming the MODULE MAP is a program now, not a listing
        r, ok, why = self.reading("PROD.GC.COPYLIB", "      * SEE THE MODULE MAP\n       IDENTIFICATION DIVISION.\n"
                                  "       PROGRAM-ID. X.\n       PROCEDURE DIVISION.\n       START-PARA.\n           GOBACK.\n")
        self.assertEqual((r["kind"], ok), ("cobol", False))
        # the folder decided (no signature at all): not a content case
        r, ok, why = self.reading("PROD.GC.PROCS", VALBK)
        self.assertEqual((r["kind"], r["by"], r["seen"], ok, why), ("proc", "folder", "the folder name ends in PROCS", False, ""))
        self.assertEqual(recover.folder_fix(r), MISFILED_FIX)
        # the file changed on disk since the build: say so, re-file nothing
        r, ok, why = self.reading("PROD.GC.COPYLIB", STARTBK, kind="proc")
        self.assertEqual((r["kind"], r["by"], ok), ("proc", "changed", False))
        self.assertIn("run the build first", r["seen"])
        # the folder fix is the instruction only for a content case nothing but a COPYLIB folder is missing for
        r, ok, why = self.reading("PROD.GC.COPYLIB", ASMBK)
        r.update(refile=ok, why_not=why)
        self.assertFalse(recover.folder_helps(r))
        self.assertTrue(recover.content_fix(r).startswith("no folder change helps (the content decided); not re-filed: "))
        r = {"by": "content", "kind": "listing", "refile": False,
             "why_not": f"neither ... - {recover.FOLDER_LETS}; the classifier itself reads it as listing"}
        self.assertTrue(recover.folder_helps(r))
        self.assertEqual(recover.content_fix(r), recover.FOLDER_THEN_RECOVER)

    def test_declared_kind_against_a_changed_file(self):
        # the same bytes the build read, a kind the declaration replaces (unknown, or a shape), the index holding a
        # kind the UI's table can declare: the declared kind decided - with a different sha the file changed
        p = self.path("PROD.GC.MISC", VALBK, "VALBK.txt")
        with open(p, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        r = recover.how_classified(p, "proc", sha)
        self.assertEqual((r["kind"], r["by"], r["over"]), ("proc", "declared", ""))
        self.assertEqual(r["seen"], DeclaredKinds.SEEN.replace("PROD.GC.MISC", "PROD.GC.MISC"))
        self.assertEqual(recover.refile_verdict(r, "PROD.GC.MISC"), (False, ""))
        self.assertEqual(recover.how_classified(p, "proc", "0" * 64)["by"], "changed")
        self.assertEqual(recover.how_classified(p, "proc")["by"], "changed")
        # 'cobol' declared replaces only 'unknown'
        self.assertEqual(recover.how_classified(p, "cobol", sha)["by"], "declared")
        a = self.path("PROD.GC.MISC", ASMBK, "ASMBK.txt")
        with open(a, "rb") as fh:
            asha = hashlib.sha256(fh.read()).hexdigest()
        # over a shape: 'declared' only when the kinds the index was built with say so (LESSONS 199) - with none
        # declared, or with the declared kinds unknown, the build's own rule (or an older classifier) did it
        r = recover.how_classified(a, "proc", asha, {"PROD.GC.MISC": "proc"})
        self.assertEqual((r["by"], r["over"], r["declared_known"]), ("declared", "weak", True))
        for declared in ({}, {"PROD.GC.OTHER": "proc"}, None):
            r = recover.how_classified(a, "proc", asha, declared)
            self.assertEqual((r["by"], r["declared_known"]), ("build", declared is not None), declared)
        self.assertEqual(recover.how_classified(a, "cobol", asha)["by"], "build")
        # over 'unknown' with the declared kinds known: 'declared' only for the library declared so
        self.assertEqual(recover.how_classified(p, "proc", sha, {"PROD.GC.MISC": "proc"})["by"], "declared")
        self.assertEqual(recover.how_classified(p, "proc", sha, {})["by"], "build")
        # the same bytes, a kind the table cannot declare: a rule of the build's own
        r = recover.how_classified(p, "binary", sha)
        self.assertEqual(r["by"], "build")
        self.assertIn("the file unchanged since", r["seen"])
        # a kind the classifier itself gives is never 'declared', sha or not
        self.assertEqual(recover.how_classified(p, "unknown", sha)["by"], "extension")
        # unreadable
        r = recover.how_classified(os.path.join(self.td, "nope", "X.txt"), "asm")
        self.assertEqual((r["kind"], r["by"]), ("asm", "unreadable"))
        self.assertEqual(recover.refile_verdict(r, "PROD.GC.COPYLIB"), (False, ""))
        self.assertEqual(recover.filed_phrase(r), "filed as asm (the file could not be read from disk to say what decided its "
                                                  "kind - is the estate where the build saw it?)")

    def test_refiled_kind_and_how(self):
        old = "re-filed as copybook by atlas.recover on 2026-09-01: the classifier filed it as asm by its content (ROADMAP " \
              "re-parse item 22); the programs copying it were marked"
        new = ("re-filed as copybook by atlas.recover on 2026-09-25: the classifier this index was built with filed it as proc "
               f"by its folder, the classifier of this toolkit reads it as a copybook ({ITEMS}); the programs copying it were "
               "marked")
        self.assertEqual((recover.refiled_kind_before(old), recover.refiled_how_before(old)), ("asm", "by a line of its text"))
        self.assertEqual((recover.refiled_kind_before(new), recover.refiled_how_before(new)), ("proc", "by its folder"))
        self.assertEqual((recover.refiled_kind_before(None), recover.refiled_how_before(None)), ("?", "by a line of its text"))


if __name__ == "__main__":
    unittest.main()
