r"""
ROADMAP re-parse item 21: an incremental build parses again every program
whose COPY may now resolve differently (LESSONS 201).

The resolver expands a COPY from a member filed copybook, cobol, sql or
unknown (build.RESOLVER_KINDS). Before the batch the build forced the
copiers of a new or changed member only when it was filed copybook or cobol
(`changed_names`), so:

  * a copybook arriving 'unknown' - a .txt in a dataset-named folder with no
    COPY hint and no signature - or 'sql' forced nothing: its programs stayed
    'partial - COPY X NOT FOUND' with the member in the index (LESSONS 183;
    atlas.recover's arrived step marks them, tests/test_arrived_copybooks.py);
  * a copybook that went from disk, or whose new text was filed as another
    kind, forced nothing either: _forget_member set the programs' copy_use
    rows to NULL and they kept 'ok' with the fields of the earlier read
    (LESSONS 188's un-linked 'ok');
  * a copybook recorded again under a new id with its bytes unchanged - a
    copybook marked pending (LESSONS 184), a build stopped before it was
    parsed, a parser exception - did the same.

Now build.moved_names takes every member of those kinds that is new, whose
bytes changed, that is recorded again or that went, under its kind in the
last build and in this one, and build.copiers_to_parse finds every member
copying one of the names - one query per 500 names, each name asked once.
Each case is also built by the earlier rule (build_before_item_21), which
leaves the state the stand-ins are written for, and on an index this
toolkit built the stand-ins have nothing to say.
"""

import os
import sqlite3
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from atlas import build, query, recover  # noqa: E402
from test_arrived_copybooks import build_before_item_21  # noqa: E402
from test_refiled_copybooks import (ASMBK, HEAD, PLAIN_SECTION, STARTBK, TAIL, _Estate, data_program,  # noqa: E402
                                    section_program, value_program)

# a procedure copybook in upper case: a copybook by its COBOL statements (ROADMAP re-parse item 20) ...
PROCBK = "       A-110-DO.\n           MOVE 1 TO WS-X.\n       A-199-EXIT.\n           EXIT.\n"
# ... and its next version in lower case with a paragraph renamed: the statement signature reads upper case, so the
# new text has none and a dataset-named folder with no COPY hint files it 'unknown'
PROCBK_LOWER = "       a-120-new.\n           move 1 to ws-x.\n       a-199-exit.\n           exit.\n"
# a copybook with no signature: a literal copied into a VALUE clause
VALBK = "      * THE STATE CODES, COPIED INTO A VALUE CLAUSE\n               'NYNJCTPAMA'.\n"
# DDL: a member filed 'sql', which the resolver expands too
DDLBK = "  CREATE TABLE PROD.POLICY_TAB\n  ( POL_ID CHAR(10) NOT NULL\n  , POL_AMT DECIMAL(9,2)\n  );\n"
# STARTBK's twin in the shared library, with a field before START-DATE: START-DATE sits at offset 15 there, not 5
STARTBK_SHARED = ("           05  ST-ID               PIC X(5).\n           05  ST-REGION           PIC X(10).\n"
                  "           05  START-DATE          PIC X(8).\n           05  ST-AMT              PIC 9(3).\n")
UNLINKED = "but 1 COPY row is unresolved (STARTBK)"


def stored(kind, sha="s1", status="ok", error=None, name="BOOK"):
    """A member row as inventory() reads it: (id, sha256, parse_status, parse_error, kind, library, ext, norm_sha,
    lines, fixed_format, name)."""
    return (1, sha, status, error, kind, "PROD.GC.COPYLIB", ".cpy", "n", 3, 1, name)


def seen(path, kind, sha="s1", name="BOOK"):
    """A file as inventory() found it: (path, name, kind, library, ext, sha256, norm_sha, bytes, lines, fixed, note)."""
    return (path, name, kind, "PROD.GC.COPYLIB", ".cpy", sha, "n", 10, 3, 1, None)


class TheRule(unittest.TestCase):
    """build.moved_names, case by case."""

    OTHER_KINDS = ("proc", "jcl", "ctlcard", "doc", "listing", "asm", "mfs", "bms", "dbd", "psb", "empty", "rexx")

    def test_the_resolver_and_recover_read_the_same_kinds(self):
        self.assertEqual(build.RESOLVER_KINDS, ("copybook", "cobol", "sql", "unknown"))
        self.assertEqual(recover.RESOLVER_KINDS, build.RESOLVER_KINDS)

    def test_a_new_member_counts_under_every_kind_the_resolver_expands(self):
        for kind in build.RESOLVER_KINDS:
            self.assertEqual(build.moved_names({}, [seen("p", kind)]), {"BOOK"}, kind)
        for kind in self.OTHER_KINDS:
            self.assertEqual(build.moved_names({}, [seen("p", kind)]), set(), kind)

    def test_changed_bytes_count_under_the_old_kind_and_the_new_one(self):
        for old, new, want in (("copybook", "copybook", {"BOOK"}), ("copybook", "asm", {"BOOK"}),
                               ("unknown", "proc", {"BOOK"}), ("asm", "unknown", {"BOOK"}), ("proc", "sql", {"BOOK"}),
                               ("asm", "proc", set()), ("doc", "doc", set())):
            self.assertEqual(build.moved_names({"p": stored(old)}, [seen("p", new, sha="s2")]), want, (old, new))

    def test_a_member_gone_from_disk_counts_under_its_stored_kind_and_name(self):
        for kind in build.RESOLVER_KINDS:
            self.assertEqual(build.moved_names({r"x\book.txt": stored(kind, name="BOOK")}, []), {"BOOK"}, kind)
        for kind in self.OTHER_KINDS:
            self.assertEqual(build.moved_names({r"x\book.txt": stored(kind)}, []), set(), kind)

    def test_the_same_bytes_count_only_when_recorded_again(self):
        # settled: kept with the same id - nothing moves, whatever the kind
        for status, error in (("ok", None), ("partial", None), ("skipped", None),
                              ("failed", "MemberTimeout: 900 s"), ("failed", "InventoryTimeout: skipped")):
            self.assertEqual(build.moved_names({"p": stored("copybook", status=status, error=error)}, [seen("p", "copybook")]),
                             set(), status)
        # not settled - a build stopped before it was parsed, a parser exception: recorded again under a new id
        for status, error in (("pending", None), ("failed", "RuntimeError: boom")):
            self.assertEqual(build.moved_names({"p": stored("copybook", status=status, error=error)}, [seen("p", "copybook")]),
                             {"BOOK"}, status)
        self.assertEqual(build.moved_names({"p": stored("proc", status="pending")}, [seen("p", "proc")]), set())

    def test_names_are_upper_case(self):
        self.assertEqual(build.moved_names({}, [seen("p", "copybook", name="Book")]), {"BOOK"})
        self.assertEqual(build.moved_names({"q": stored("unknown", name="Gone")}, []), {"GONE"})


class CopiersToParse(_Estate):
    """build.copiers_to_parse: every member copying a name, then every member copying one of THEIRS, each name
    asked once, 500 to a query."""

    files = (("GC/PROD.GC.SRC/PGMONE.cbl", section_program("PGMONE", "BOOKA")),
             ("GC/PROD.GC.SRC/PGMTWO.cbl", section_program("PGMTWO", "BOOKC")),
             ("GC/PROD.GC.COPYLIB/BOOKA.cpy", "       A-110-DO.\n           MOVE 1 TO WS-X.\n           COPY BOOKB.\n"),
             ("GC/PROD.GC.COPYLIB/BOOKB.cpy", "       A-150-SUB.\n           MOVE 2 TO WS-Y.\n"),
             ("GC/PROD.GC.COPYLIB/BOOKC.cpy", "       A-160-SUB.\n           MOVE 3 TO WS-Y.\n"))

    def test_one_query_per_500_names_each_name_once(self):
        conn = sqlite3.connect(self.db)
        asked = []
        conn.set_trace_callback(lambda sql: asked.append(sql) if "FROM copy_use c JOIN member m" in sql else None)
        try:
            names = {"BOOKB"} | {f"NOBODY{i:04d}" for i in range(1200)}
            forced = build.copiers_to_parse(conn, names)
        finally:
            conn.close()
        # PGMONE carries its own row for the nested COPY BOOKB; BOOKA copies it; PGMTWO copies nothing that moved
        self.assertEqual({os.path.basename(p) for p in forced}, {"PGMONE.cbl", "BOOKA.cpy"})
        # 1,201 names in three queries (500, 500, 201); then BOOKA and PGMONE, recorded again, in one; nothing after
        self.assertEqual(len(asked), 4, asked)

    def test_nothing_asked_nothing_forced(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(build.copiers_to_parse(conn, set()), set())
            self.assertEqual(build.copiers_to_parse(conn, {"NOBODY"}), set())
        finally:
            conn.close()


class _Forcing(_Estate):
    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("GC/PROD.GC.COPYLIB/STARTBK.cpy", STARTBK))

    def remove(self, rel):
        os.remove(os.path.join(self.root, *rel.split("/")))

    def age_fingerprint(self):
        """The index's last build recorded as an older toolkit's, as his index is before the re-parse night: the
        first build of this toolkit parses every member again."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE build_run SET fingerprint='an older toolkit'")
            conn.commit()
        finally:
            conn.close()

    def ids(self):
        return self.q("SELECT name, kind, id, parse_status FROM member ORDER BY name, kind")

    def stand_ins_say_nothing(self, program, book):
        """On an index this toolkit built nothing is left for the stand-ins: no program 'ok' with an un-linked row,
        nothing arrived or waiting, recover marks nothing, and coverage / program say neither."""
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
            arrived, _misfiled, waiting = recover.arrival_scan(conn)
            self.assertEqual((arrived, waiting), ([], []))
        finally:
            conn.close()
        cov, _nf, prog, bk, _as_prog = self.outputs(program, book)
        self.assertNotIn("Not counted above", cov)
        self.assertNotIn("COPY row is unresolved", prog)
        self.assertNotIn("parsed before it arrived", cov + prog + bk)
        self.assertNotIn("still say", bk)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["waiting"], stats["refiled"], stats["marked"]), (0, 0, 0, 0), said)
        self.assertNotIn("has arrived since", said)
        self.assertNotIn("marked for the next build", said)
        return cov, prog

    def assert_ok_and_linked(self, program="STPGM", book="STARTBK", library=None):
        self.assertEqual(self.status(program), "ok")
        self.assertEqual(self.copy_use(program, book), [(self.member_id(book, library),)])

    def assert_not_found(self, program="STPGM", book="STARTBK"):
        self.assertEqual(self.status(program), "partial")
        self.assertEqual(self.copy_use(program, book), [(None,)])
        self.assertTrue(self.not_found_note(program, book))


class ArrivingUnderEveryKind(_Forcing):
    """A copybook arriving 'unknown' or 'sql' after its program was parsed: the build parses the program again."""

    files = (("GC/PROD.GC.SRC/VALPGM.cbl", value_program("VALPGM", "VALBK")),
             ("GC/PROD.GC.SRC/SQLPGM.cbl", HEAD.format(name="SQLPGM", data="           EXEC SQL INCLUDE POLTAB END-EXEC.\n",
                                                        main="") + PLAIN_SECTION + TAIL))

    def test_unknown_and_sql_members_force_their_programs(self):
        self.assert_not_found("VALPGM", "VALBK")
        self.assert_not_found("SQLPGM", "POLTAB")
        self.write("SHARED/PROD.GC.CPYLIB/VALBK.txt", VALBK)
        self.write("SHARED/PROD.GC.CPYLIB/POLTAB.txt", DDLBK)
        self.build()
        self.assertEqual((self.member("VALBK")[0], self.member("POLTAB")[0]), ("unknown", "sql"))
        self.assert_ok_and_linked("VALPGM", "VALBK")
        self.assert_ok_and_linked("SQLPGM", "POLTAB")
        self.stand_ins_say_nothing("VALPGM", "VALBK")
        before = self.ids()
        self.build()
        self.assertEqual(self.ids(), before, "settled: the next build parses nothing again")

    def test_before_the_batch_they_forced_nothing(self):
        self.write("SHARED/PROD.GC.CPYLIB/VALBK.txt", VALBK)
        self.write("SHARED/PROD.GC.CPYLIB/POLTAB.txt", DDLBK)
        with build_before_item_21():
            self.build()
        self.assert_not_found("VALPGM", "VALBK")
        self.assert_not_found("SQLPGM", "POLTAB")
        # recover's arrived step still finds both on such an index, marks them, and the build makes them whole
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (2, 2), said)
        self.build()
        self.assert_ok_and_linked("VALPGM", "VALBK")
        self.assert_ok_and_linked("SQLPGM", "POLTAB")


class RemovedFromDisk(_Forcing):
    """A copybook that goes from disk: its programs are parsed again and say COPY X NOT FOUND - not 'ok' with a
    NULL row and the fields of the earlier read (LESSONS 188's un-linked 'ok')."""

    def test_its_programs_are_parsed_again_and_say_not_found(self):
        self.assert_ok_and_linked()
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 5, 8)])
        self.remove("GC/PROD.GC.COPYLIB/STARTBK.cpy")
        self.build()
        self.assert_not_found()
        self.assertEqual(self.fields("STPGM", "START-DATE"), [], "no field of a copybook the program no longer has")
        cov, prog = self.stand_ins_say_nothing("STPGM", "STARTBK")
        self.assertIn("| STARTBK | **NOT FOUND** |", prog)
        self.assertIn("| STARTBK | 1 | - |", cov.split("### Copybooks not found")[1].split("\n###")[0])
        self.assertIn("| cobol | STPGM |", cov.split("### Members parsed only in part")[1].split("\n###")[0])

    def test_before_the_batch_the_program_kept_ok_with_a_null_row(self):
        self.remove("GC/PROD.GC.COPYLIB/STARTBK.cpy")
        with build_before_item_21():
            self.build()
        # LESSONS 188's state: 'ok', the row NULL, the fields of the earlier read - and the stand-in says so
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 5, 8)])
        pid = self.q("SELECT id FROM member WHERE name='STPGM'")[0][0]
        conn = query.connect(self.db)
        try:
            self.assertEqual(recover.unlinked_ok_programs(conn), {pid: ("STPGM", ["STARTBK"])})
        finally:
            conn.close()
        _cov, _nf, prog, _bk, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn(UNLINKED, prog)
        # the re-parse night: the first build of this toolkit parses every member again - partial, NOT FOUND
        self.age_fingerprint()
        self.build()
        self.assert_not_found()
        self.stand_ins_say_nothing("STPGM", "STARTBK")

    def test_the_other_copy_takes_over(self):
        # STARTBK in the program's own system and in the shared library, with different text: the chain picks the
        # program's system; that copy goes, and the build parses the program again with the shared one
        self.write("SHARED/PROD.CMN.COPYLIB/STARTBK.cpy", STARTBK_SHARED)
        self.build()
        gc = self.member_id("STARTBK", "PROD.GC.COPYLIB")
        self.assertEqual(self.copy_use("STPGM", "STARTBK"), [(gc,)])
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 5, 8)])
        self.remove("GC/PROD.GC.COPYLIB/STARTBK.cpy")
        self.build()
        self.assert_ok_and_linked(library="PROD.CMN.COPYLIB")
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 15, 8)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM unresolved WHERE kind='ambiguous_copybook'"), [(0,)],
                         "one copy left: no choice to record")
        self.stand_ins_say_nothing("STPGM", "STARTBK")

    def test_an_unknown_member_going_forces_its_programs_too(self):
        self.write("GC/PROD.GC.SRC/VALPGM.cbl", value_program("VALPGM", "VALBK"))
        self.write("SHARED/PROD.GC.CPYLIB/VALBK.txt", VALBK)
        self.build()
        self.assert_ok_and_linked("VALPGM", "VALBK")
        self.remove("SHARED/PROD.GC.CPYLIB/VALBK.txt")
        self.build()
        self.assert_not_found("VALPGM", "VALBK")
        self.stand_ins_say_nothing("VALPGM", "VALBK")


class ReTyped(_Forcing):
    """A copybook whose new text is filed as another kind: its programs are parsed again - NOT FOUND for a kind
    the resolver never expands, the new text for one it does."""

    files = (("GC/PROD.GC.SRC/STPGM.cbl", data_program("STPGM", "STARTBK", "START-DATE")),
             ("GC/PROD.GC.COPYLIB/STARTBK.cpy", STARTBK),
             ("GC/PROD.GC.SRC/SECPGM.cbl", section_program("SECPGM", "PROCBK")),
             ("SHARED/PROD.GC.CPYLIB/PROCBK.txt", PROCBK))

    def paras(self):
        return [p[0].upper() for p in self.paragraphs("SECPGM")]

    def test_a_new_text_of_another_kind_leaves_the_program_not_found(self):
        # the member's new text is a real Assembler member (its shape): filed asm, a kind the resolver never expands
        self.write("GC/PROD.GC.COPYLIB/STARTBK.cpy", ASMBK.replace("ASMBK ", "STARTBK", 1))
        self.build()
        self.assertEqual(self.member("STARTBK")[0], "asm")
        self.assert_not_found()
        self.assertEqual(self.fields("STPGM", "START-DATE"), [])
        self.stand_ins_say_nothing("STPGM", "STARTBK")
        nf = self.outputs("STPGM", "STARTBK")[1]
        self.assertIn("filed as asm", nf)

    def test_before_the_batch_it_left_the_program_ok_with_the_old_fields(self):
        self.write("GC/PROD.GC.COPYLIB/STARTBK.cpy", ASMBK.replace("ASMBK ", "STARTBK", 1))
        with build_before_item_21():
            self.build()
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 5, 8)])
        self.assertIn(UNLINKED, self.outputs("STPGM", "STARTBK")[2])

    def test_a_new_text_filed_unknown_is_expanded_in_place_of_the_old(self):
        # the procedure copybook rewritten in lower case with a paragraph renamed: 'unknown' now - the program is
        # parsed again with the new text, linked to the member as it is recorded now
        self.assertEqual(self.member("PROCBK")[0], "copybook")
        self.assertIn("A-110-DO", self.paras())
        self.write("SHARED/PROD.GC.CPYLIB/PROCBK.txt", PROCBK_LOWER)
        self.build()
        self.assertEqual(self.member("PROCBK")[0], "unknown")
        self.assert_ok_and_linked("SECPGM", "PROCBK")
        self.assertIn("A-120-NEW", self.paras())
        self.assertNotIn("A-110-DO", self.paras())
        self.stand_ins_say_nothing("SECPGM", "PROCBK")

    def test_before_the_batch_the_old_paragraphs_stayed(self):
        self.write("SHARED/PROD.GC.CPYLIB/PROCBK.txt", PROCBK_LOWER)
        with build_before_item_21():
            self.build()
        # 'ok', the row NULL, and the paragraphs of a text that is no longer on disk
        self.assertEqual((self.status("SECPGM"), self.copy_use("SECPGM", "PROCBK")), ("ok", [(None,)]))
        self.assertIn("A-110-DO", self.paras())
        self.assertNotIn("A-120-NEW", self.paras())


class RecordedAgain(_Forcing):
    """A copybook recorded again under a new id with its bytes unchanged: its programs are parsed again, linked to
    the member as it is recorded now."""

    def test_a_copybook_marked_pending(self):
        # LESSONS 184's mechanism: a copybook marked pending (as a build stopped before it was parsed leaves it)
        self.q_write("UPDATE member SET parse_status='pending' WHERE name='STARTBK'")
        self.build()
        self.assertEqual(self.member("STARTBK")[2], "ok")
        self.assert_ok_and_linked()
        self.stand_ins_say_nothing("STPGM", "STARTBK")

    def test_before_the_batch_a_copybook_marked_pending_un_linked_its_programs(self):
        self.q_write("UPDATE member SET parse_status='pending' WHERE name='STARTBK'")
        with build_before_item_21():
            self.build()
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))

    def test_a_copybook_the_parser_fails_on(self):
        # a parser exception is not a settled outcome: the member is parsed again - and recorded again - on every
        # build until the parser is fixed, and its programs with it (the problem list names the member each time)
        real = build.HANDLERS["copybook"]

        def failing(ctx, mem):
            if mem.name == "STARTBK":
                raise RuntimeError("a copybook the parser cannot read")
            return real(ctx, mem)

        with mock.patch.dict(build.HANDLERS, {"copybook": failing}):
            self.build(["--rebuild"])
            status, error = self.member("STARTBK")[2:]
            self.assertEqual(status, "failed")
            self.assertTrue(error.startswith("RuntimeError: a copybook the parser cannot read"), error)
            self.assert_ok_and_linked()
            self.build()
            self.assertEqual(self.member("STARTBK")[2], "failed")
            self.assert_ok_and_linked()
        self.stand_ins_say_nothing("STPGM", "STARTBK")

    def q_write(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()


class NothingElseIsParsedAgain(_Forcing):
    """The control: a member nobody copies, a document, or a new program forces no other member."""

    def test_unrelated_arrivals_and_departures_force_nothing(self):
        before = self.ids()
        self.write("SHARED/PROD.GC.CPYLIB/NOBODY.txt", VALBK)                     # 'unknown', copied by nobody
        self.write("SHARED/DOCS/RUNBOOK.txt", "Please run the job after the close.\n")
        self.write("GC/PROD.GC.SRC/NEWPGM.cbl", data_program("NEWPGM", "STARTBK", "START-DATE"))
        self.build()
        self.assertEqual(self.member("NOBODY")[0], "unknown")
        after = {r[0]: r for r in self.ids()}
        for row in before:
            self.assertEqual(after[row[0]], row, "kept with the same id and status")
        self.assert_ok_and_linked("NEWPGM")
        kept = self.ids()
        self.remove("SHARED/PROD.GC.CPYLIB/NOBODY.txt")
        self.build()
        self.assertEqual([r for r in self.ids() if r[0] != "NOBODY"], [r for r in kept if r[0] != "NOBODY"])


if __name__ == "__main__":
    unittest.main()
