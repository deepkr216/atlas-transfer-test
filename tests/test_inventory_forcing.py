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

The jobs follow the same rule for what THEY read (build.JOB_READ_KINDS: a
cataloged PROC, an INCLUDE member, a card member - which may be filed
unknown or sql): a member of those kinds that arrives, changes, goes or is
re-typed makes the build parse every job and PROC again. Before, only a new
or changed member filed proc, jcl or ctlcard did: a PROC that went from disk
left its job with the PROC's steps and datasets, and a card member arriving
'unknown' left the job without its cards (JobsFollowWhatTheyRead). Each
case is checked against a --rebuild of the same estate, fact for fact.
"""

import os
import shutil
import sqlite3
import sys
import unittest
from collections import Counter
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
# what the stand-ins say of a program that had expanded a copy which left the index after the parse (LESSONS 202)
LEFT_CELL = ("the copy the program had expanded left the index after the parse and the build that made this index did "
             "not parse it again: run recover, then the build")
LEFT_CONSOLE = ("1 program(s) were parsed with a copy of a copybook that has left the index since - the file went, its "
                "text changed, or it was recorded again under a new id - and the build that made this index did not parse "
                "them again (1 copybook, a member of that name in the index now): marked for the next build")


# a job that reads a cataloged PROC, a card member by DSN=LIB(MEMBER), a card taken by its sequential dataset's last
# qualifier, and an INCLUDE member
GCJOB1 = ("//GCJOB1   JOB (ACCT),'NIGHT',CLASS=A\n"
          "//STEP1    EXEC GCPROC\n"
          "//STEP2    EXEC PGM=IKJEFT01\n"
          "//SYSTSIN  DD DSN=PROD.GC.PARMS(GCRUN1),DISP=SHR\n"
          "//STEP3    EXEC PGM=SORT\n"
          "//SORTIN   DD DSN=PROD.GC.IN,DISP=SHR\n"
          "//SORTOUT  DD DSN=PROD.GC.SORTED,DISP=(NEW,CATLG)\n"
          "//SYSIN    DD DSN=PROD.GC.SRTCARD,DISP=SHR\n"
          "//STEP4    EXEC PGM=GCPGM2\n"
          "//         INCLUDE MEMBER=GCINC1\n")
GCPROC = "//GCPROC   PROC\n//RUN      EXEC PGM=GCPGM1\n//OUT      DD DSN=PROD.GC.OUT,DISP=SHR\n//         PEND\n"
GCINC1 = "//EXTRA    DD DSN=PROD.GC.EXTRA,DISP=SHR\n"
# card members with no signature in a dataset-named folder with no hint: filed 'unknown'
GCRUN1 = " DSN SYSTEM(DB2P)\n RUN PROGRAM(GCPGM3) PLAN(GCPLAN3)\n END\n"
SRTCARD = "  SORT FIELDS=(1,5,CH,A)\n"

# build bookkeeping and the search index: not facts about the estate
_NOT_FACTS = {"atlas_meta", "build_run", "src_fts", "src_fts_config", "src_fts_content", "src_fts_data",
              "src_fts_docsize", "src_fts_idx", "fts_span", "expand_run", "sqlite_sequence", "library"}
_WHEN = {"id", "scanned_at", "generated_at", "fetched_at", "started_at", "finished_at"}


def facts(db, skip=()):
    """Every row of the index with each id replaced by what it points at (the member's path under the estate root,
    the program, the field, the step), so an incremental build and a --rebuild of the same estate compare row for
    row: {table: Counter(rows)}."""
    c = sqlite3.connect(db)
    try:
        root = os.path.commonpath([r[0] for r in c.execute("SELECT path FROM member")] or [os.sep])
        mem = {i: os.path.relpath(p, root) for i, p in c.execute("SELECT id, path FROM member")}
        prog = {i: mem.get(m) for i, m in c.execute("SELECT id, member_id FROM program")}
        pf = {i: f"{prog.get(p)}:{n}@{o}" for i, p, n, o in c.execute("SELECT id, program_id, name, offset FROM pfield")}
        fld = {i: f"{mem.get(m)}:{n}" for i, m, n in c.execute("SELECT id, member_id, name FROM field")}
        step = {i: f"{j}/{n}/{o}/{pp}" for i, j, n, o, pp in c.execute(
            "SELECT s.id, COALESCE(j.job_name, pd.proc_name), s.step_name, s.ordinal, s.parent_step FROM step s "
            "LEFT JOIN job j ON j.id=s.job_id LEFT JOIN proc_def pd ON pd.id=s.proc_id")}
        maps = {"member_id": mem, "resolved_member_id": mem, "src_member": mem, "include_member": mem,
                "card_member": mem, "pfield": pf, "pfield_id": pf, "src_pfield": pf, "dst_pfield": pf, "root_id": pf,
                "copy_field_id": fld, "field_id": fld, "step_id": step,
                "job_id": dict(c.execute("SELECT id, job_name FROM job").fetchall()),
                "proc_id": dict(c.execute("SELECT id, proc_name FROM proc_def").fetchall())}
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            if t in _NOT_FACTS or t in skip:
                continue
            cols = [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
            rows = Counter()
            for r in c.execute(f'SELECT * FROM "{t}"'):
                vals = []
                for col, v in zip(cols, r):
                    if col in _WHEN:
                        continue
                    if col == "program_id" and t != "program":
                        v = prog.get(v, f"?prog{v}")
                    elif col == "parent_id":
                        v = (pf if t == "pfield" else fld).get(v, v)
                    elif col in maps and v is not None and isinstance(v, int):
                        v = maps[col].get(v, f"?{col}{v}")
                    elif col.endswith("_id") and isinstance(v, int):
                        v = "*"
                    if isinstance(v, str):
                        v = v.replace(root, "<ROOT>")
                    vals.append(f"{col}={v}")
                rows[tuple(vals)] += 1
            out[t] = rows
        return out
    finally:
        c.close()


def facts_differ(a, b):
    """The rows each index holds that the other does not, table by table ({} when they hold the same facts)."""
    diff = {}
    for t in sorted(set(a) | set(b)):
        ra, rb = a.get(t, Counter()), b.get(t, Counter())
        if ra != rb:
            diff[t] = (sorted(ra - rb)[:4], sorted(rb - ra)[:4])
    return diff


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

    def test_the_jobs_read_the_kinds_their_lookups_read(self):
        # _proc_facts, _include_text and _card_text read these kinds; the job forcing reads the same tuple
        self.assertEqual(build.PROC_KINDS, ("proc", "jcl"))
        self.assertEqual(build.INCLUDE_KINDS, ("jcl", "proc", "ctlcard", "unknown"))
        self.assertEqual(build.CARD_KINDS, ("ctlcard", "unknown", "sql", "jcl", "proc"))
        self.assertEqual(build.JOB_READ_KINDS, ("proc", "jcl", "ctlcard", "unknown", "sql"))

    def test_for_the_jobs_a_member_arriving_changing_going_or_re_typed_counts(self):
        J = build.JOB_READ_KINDS

        def moved(existing, found):
            return build.moved_names(existing, found, J, again=False)

        for kind in J:
            self.assertEqual(moved({}, [seen("p", kind)]), {"BOOK"}, kind)
            self.assertEqual(moved({"p": stored(kind)}, [seen("p", kind, sha="s2")]), {"BOOK"}, kind)
            self.assertEqual(moved({r"x\book.txt": stored(kind, name="BOOK")}, []), {"BOOK"}, kind)
        for kind in ("copybook", "cobol", "doc", "listing", "asm", "mfs", "bms", "dbd", "psb", "empty", "rexx"):
            self.assertEqual(moved({}, [seen("p", kind)]), set(), kind)
            self.assertEqual(moved({r"x\book.txt": stored(kind)}, []), set(), kind)
        # re-typed out of the kinds a job reads, and into them
        self.assertEqual(moved({"p": stored("proc")}, [seen("p", "doc", sha="s2")]), {"BOOK"})
        self.assertEqual(moved({"p": stored("doc")}, [seen("p", "unknown", sha="s2")]), {"BOOK"})
        self.assertEqual(moved({"p": stored("asm")}, [seen("p", "copybook", sha="s2")]), set())
        # recorded again with the same bytes: a job keeps the names of what it read, never an id - nothing changes
        for status, error in (("ok", None), ("pending", None), ("failed", "RuntimeError: boom")):
            self.assertEqual(moved({"p": stored("proc", status=status, error=error)}, [seen("p", "proc")]), set(), status)


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

    def full(self):
        """The same estate built from nothing (--rebuild) into another index: what an incremental build must give."""
        db = os.path.join(self.td, "full.db")
        if os.path.exists(db):
            os.remove(db)
        self.build(["--rebuild", "--db", db])
        return db

    def assert_as_full(self, skip=()):
        """The incremental index holds the facts a --rebuild of the same estate holds, row for row."""
        self.assertEqual(facts_differ(facts(self.db, skip), facts(self.full(), skip)), {})

    def stamp(self):
        """Mark every member row as it stands: a member the next build records again - parsed again - loses the
        mark (the build writes scanned_at only when it inserts a member), whatever id it gets (SQLite may give the
        same id back when the highest rows were removed)."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE member SET scanned_at='kept by the last build'")
            conn.commit()
        finally:
            conn.close()

    def recorded_again(self):
        """The members recorded since stamp(): new, or parsed again."""
        return {r[0] for r in self.q("SELECT name FROM member WHERE COALESCE(scanned_at, '') != 'kept by the last build'")}

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

    def assert_left_words(self, where, program="STPGM", book="STARTBK"):
        """On an index the build before item 21 left, the program HAD expanded a copy of the copybook that left the
        index after the parse (no NOT FOUND note), and a member of that name is in the index (`where`: its folder and
        kind): every stand-in says so - and none says it was parsed before the member arrived, nor why an arrival
        forced nothing (LESSONS 202). recover marks it with the words of that cause, and the build makes it whole."""
        self.assertEqual(self.not_found_note(program, book), [], "the program's own notes say no COPY NOT FOUND")
        cov, nf, prog, bk, _as_prog = self.outputs(program, book)
        self.assertIn(f"| {book} | 1 | yes: {where} - {LEFT_CELL} |", nf)
        self.assertIn("A copybook marked `yes` is not missing - a member with its name is in the index. Either the "
                      "programs had expanded a copy of it that left the index after their parse - the file went while "
                      "another copy of the name stays, its text changed, or it was recorded again under a new id with its "
                      "text unchanged (a copybook marked pending, a build stopped before it was parsed, a parser "
                      "exception) - and the build that made this index did not parse them again", cov)
        self.assertIn(f"| {book} | **no longer linked** - a member with this name exists: {where} - {LEFT_CELL} |", prog)
        self.assertIn(UNLINKED.replace("STARTBK", book) + ": the member it had expanded went out of the index since - its "
                      "text changed on disk and the classifier filed the new text as another kind, the file went, or it "
                      "was recorded again under a new id with its text unchanged - and the build that made this index did "
                      "not parse the program again", prog)
        self.assertIn("> 1 of the programs above was parsed with a copy of this copybook that has left the index since - "
                      "the file went while another copy of the name stays", bk)
        for text in (cov, prog, bk):
            self.assertNotIn("parsed before it arrived", text)
            self.assertNotIn("parsed before this member arrived", text)
            self.assertNotIn("only for a member filed copybook or cobol", text)
            self.assertNotIn("still say", text)
        self.assertNotIn("reported NOT FOUND has a member", cov)
        stats, said = self.recover(dry_run=True)
        self.assertEqual((stats["arrived"], stats["marked"]), (1, 0), said)
        self.assertIn(LEFT_CONSOLE.replace(": marked", ": would be marked"), said)
        self.assertNotIn("has arrived since", said)
        rep = self.report_text()
        self.assertIn("## Programs parsed with a copy that has left the index since", rep)
        self.assertIn(f"| {book} | {where.split(' (')[1].rstrip(')')} | {where.split(' (')[0]} | {program} |", rep)
        self.assertNotIn("arrived after the program was parsed", rep)
        self.assertNotIn("forced nothing", rep)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (1, 1), said)
        self.assertIn(LEFT_CONSOLE, said)
        self.assertEqual(self.status(program), "pending")
        self.build()
        self.assertEqual(self.status(program), "ok")
        self.stand_ins_say_nothing(program, book)

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
        self.assert_as_full()
        self.stamp()
        self.build()
        self.assertEqual(self.recorded_again(), set(), "settled: the next build parses nothing again")

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
        self.assert_as_full()

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

    def test_before_the_batch_the_other_copy_left_it_un_linked(self):
        # the copy the program expanded goes while the shared one stays: the build before the item parsed nothing
        # again, the program kept 'ok', the fields of the copy that went and a NULL row - the stand-ins say that copy
        # left, then recover and the build link it to the shared copy with its own offsets
        self.write("SHARED/PROD.CMN.COPYLIB/STARTBK.cpy", STARTBK_SHARED)
        self.build()
        self.remove("GC/PROD.GC.COPYLIB/STARTBK.cpy")
        with build_before_item_21():
            self.build()
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 5, 8)])
        self.assert_left_words("PROD.CMN.COPYLIB (copybook)")
        self.assert_ok_and_linked(library="PROD.CMN.COPYLIB")
        self.assertEqual([f[:3] for f in self.fields("STPGM", "START-DATE")], [("START-DATE", 15, 8)])

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
        self.assert_as_full()

    def test_an_unknown_member_going_forces_its_programs_too(self):
        self.write("GC/PROD.GC.SRC/VALPGM.cbl", value_program("VALPGM", "VALBK"))
        self.write("SHARED/PROD.GC.CPYLIB/VALBK.txt", VALBK)
        self.build()
        self.assert_ok_and_linked("VALPGM", "VALBK")
        self.remove("SHARED/PROD.GC.CPYLIB/VALBK.txt")
        self.build()
        self.assert_not_found("VALPGM", "VALBK")
        self.stand_ins_say_nothing("VALPGM", "VALBK")
        self.assert_as_full()


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
        self.assert_as_full()

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
        self.assert_as_full()

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
        self.assert_as_full()

    def test_before_the_batch_a_copybook_marked_pending_un_linked_its_programs(self):
        self.q_write("UPDATE member SET parse_status='pending' WHERE name='STARTBK'")
        with build_before_item_21():
            self.build()
        self.assertEqual((self.status("STPGM"), self.copy_use("STPGM", "STARTBK")), ("ok", [(None,)]))
        # the member was there all along: the stand-ins say the copy was recorded again, not that it arrived
        self.assert_left_words("PROD.GC.COPYLIB (copybook)")
        self.assert_ok_and_linked()

    def test_before_the_batch_both_causes_under_one_name(self):
        # NEWPGM as a program parsed while STARTBK was missing and not parsed again (its NOT FOUND note, set by SQL as
        # that build left it), STPGM un-linked by STARTBK recorded again: one name, each program with its own cause
        self.write("GC/PROD.GC.SRC/NEWPGM.cbl", data_program("NEWPGM", "STARTBK", "START-DATE"))
        self.build()
        new = self.q("SELECT id FROM member WHERE name='NEWPGM'")[0][0]
        self.q_write("UPDATE copy_use SET resolved_member_id=NULL WHERE member_id=?", new)
        self.q_write("INSERT INTO unresolved(member_id,kind,detail) VALUES(?,'expand',?)", new,
                     "L8: COPY STARTBK NOT FOUND - fields/code from it are missing from this program's facts")
        self.q_write("UPDATE member SET parse_status='partial' WHERE id=?", new)
        self.q_write("UPDATE member SET parse_status='pending' WHERE name='STARTBK'")
        with build_before_item_21():
            self.build()
        self.assertEqual((self.status("STPGM"), self.status("NEWPGM")), ("ok", "partial"))
        cov, nf, prog, bk, _as_prog = self.outputs("STPGM", "STARTBK")
        self.assertIn("| STARTBK | 2 | yes: PROD.GC.COPYLIB (copybook) - 1 program(s) parsed before it arrived, 1 parsed "
                      "with a copy that left the index after the parse: run recover, then the build |", nf)
        # both causes in the legend; no 'only for a member filed copybook or cobol' - the member IS a copybook
        self.assertIn("Either the programs that copy it were parsed before it arrived and nothing parsed them again: "
                      "`python -m atlas.recover --db atlas.db` marks them, then run your usual build; or the programs had "
                      "expanded a copy of it that left the index after their parse", cov)
        self.assertNotIn("only for a member filed copybook or cobol", cov)
        self.assertIn("| STARTBK | **no longer linked** - a member with this name exists: PROD.GC.COPYLIB (copybook) - "
                      + LEFT_CELL + " |", prog)
        newprog = self.outputs("NEWPGM", "STARTBK")[2]
        self.assertIn("| STARTBK | **NOT FOUND** - a member with this name exists: PROD.GC.COPYLIB (copybook) - parsed "
                      "before it arrived: run recover, then the build |", newprog)
        self.assertIn("> 1 of the programs above still says `COPY STARTBK NOT FOUND` (under 'Unresolved in scope' below): "
                      "parsed before this member arrived and nothing parsed them again. `python -m atlas.recover", bk)
        self.assertIn("> 1 of the programs above was parsed with a copy of this copybook that has left the index since", bk)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (1, 2), said)
        self.assertIn("1 program(s) copy a copybook that has arrived since they were parsed (1 copybook): marked for the "
                      "next build", said)
        self.assertIn(LEFT_CONSOLE, said)
        rep = self.report_text()
        late = rep.split("## Copybooks that arrived after the program was parsed")[1].split("\n## ")[0]
        gone = rep.split("## Programs parsed with a copy that has left the index since")[1].split("\n## ")[0]
        self.assertIn("| STARTBK | copybook | PROD.GC.COPYLIB | NEWPGM |", late)
        self.assertNotIn("forced nothing", late, "a copybook is a kind the earlier forcing did follow")
        self.assertIn("| STARTBK | copybook | PROD.GC.COPYLIB | STPGM |", gone)
        self.build()
        self.assert_ok_and_linked()
        self.assert_ok_and_linked("NEWPGM")
        self.stand_ins_say_nothing("NEWPGM", "STARTBK")

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
        self.stamp()
        self.write("SHARED/PROD.GC.CPYLIB/NOBODY.txt", VALBK)                     # 'unknown', copied by nobody
        self.write("SHARED/DOCS/RUNBOOK.txt", "Please run the job after the close.\n")
        self.write("GC/PROD.GC.SRC/NEWPGM.cbl", data_program("NEWPGM", "STARTBK", "START-DATE"))
        self.build()
        self.assertEqual(self.member("NOBODY")[0], "unknown")
        self.assertEqual(self.recorded_again(), {"NOBODY", "RUNBOOK", "NEWPGM"}, "STPGM and STARTBK kept as they were")
        self.assert_ok_and_linked("NEWPGM")
        self.stamp()
        self.remove("SHARED/PROD.GC.CPYLIB/NOBODY.txt")
        self.build()
        self.assertEqual(self.recorded_again(), set())
        self.assertEqual(self.q("SELECT COUNT(*) FROM member WHERE name='NOBODY'"), [(0,)])


class JobsFollowWhatTheyRead(_Forcing):
    """A member a job's expansion reads - a cataloged PROC, an INCLUDE member, a card member (filed ctlcard, or
    unknown / sql when its folder has no hint) - that arrives, goes or is re-typed: the build parses every job and
    PROC again, and the job holds what a --rebuild of the same estate gives it. Before ROADMAP re-parse item 21 only a
    new or changed member filed proc, jcl or ctlcard forced the jobs.

    The one table left out of the comparison is `dataset`, the registry of dataset names: a name stays in it after
    the last DD naming it is gone, until a --rebuild - on every incremental build, whatever changed the job (it is
    read only for an IDCAMS DEFINE's attributes)."""

    files = _Forcing.files + (("GC/PROD.GC.JCL/GCJOB1.jcl", GCJOB1),
                              ("GC/PROD.GC.PROCLIB/GCPROC.prc", GCPROC),
                              ("GC/PROD.GC.PROCLIB/GCINC1.prc", GCINC1))

    def steps(self):
        return self.q("SELECT s.step_name, s.effective_pgm, s.from_proc FROM step s JOIN job j ON j.id=s.job_id "
                      "WHERE j.job_name='GCJOB1' ORDER BY s.ordinal, s.id")

    def dds(self):
        return self.q("SELECT d.dd_name, d.dsn_resolved, d.card_member, d.sysin_text IS NOT NULL FROM dd d "
                      "JOIN step s ON s.id=d.step_id JOIN job j ON j.id=s.job_id WHERE j.job_name='GCJOB1' "
                      "ORDER BY s.ordinal, d.id")

    def job_notes(self):
        return self.q("SELECT u.kind, u.detail FROM unresolved u JOIN member m ON m.id=u.member_id "
                      "WHERE m.name='GCJOB1' ORDER BY u.kind, u.detail")

    def assert_as_full(self, skip=("dataset",)):
        super().assert_as_full(skip)

    def test_as_built_from_nothing(self):
        self.assertIn(("STEP1.RUN", "GCPGM1", "GCPROC"), self.steps())
        self.assertIn(("EXTRA", "PROD.GC.EXTRA", None, 0), self.dds())
        self.assert_as_full()

    def test_a_proc_gone_from_disk(self):
        self.stamp()
        self.remove("GC/PROD.GC.PROCLIB/GCPROC.prc")
        self.build()
        self.assertIn("GCJOB1", self.recorded_again())
        # the job no longer runs the PROC's step nor writes its dataset, and says the PROC is missing
        self.assertNotIn("STEP1.RUN", [s[0] for s in self.steps()])
        self.assertNotIn("PROD.GC.OUT", [d[1] for d in self.dds()])
        self.assertIn(("missing_proc", "STEP1: PROC GCPROC not found"), self.job_notes())
        self.assert_as_full()

    def test_before_the_batch_a_proc_gone_left_its_steps(self):
        self.remove("GC/PROD.GC.PROCLIB/GCPROC.prc")
        with build_before_item_21():
            self.build()
        # the verifier's case: 'to parse: nothing' - the job kept the step and the DD of a PROC no longer on disk
        self.assertIn(("STEP1.RUN", "GCPGM1", "GCPROC"), self.steps())
        self.assertIn("PROD.GC.OUT", [d[1] for d in self.dds()])
        self.assertNotIn(("missing_proc", "STEP1: PROC GCPROC not found"), self.job_notes())
        self.assertIn("step", facts_differ(facts(self.db, ("dataset",)), facts(self.full(), ("dataset",))))

    def test_an_include_member_gone_from_disk(self):
        self.remove("GC/PROD.GC.PROCLIB/GCINC1.prc")
        self.build()
        self.assertNotIn("EXTRA", [d[0] for d in self.dds()])
        self.assert_as_full()

    def test_card_members_arriving_unknown(self):
        # SYSTSIN names its card member; SORT's SYSIN is a sequential dataset, whose cards are looked up by its last
        # qualifier - a lookup the job records nothing of when it finds nothing, so only 'every job' can follow it
        self.assertIn(("STEP2", None, None), self.steps())
        self.write("GC/PROD.GC.PARMS/GCRUN1.txt", GCRUN1)
        self.write("GC/PROD.GC.PARMS/SRTCARD.txt", SRTCARD)
        self.build()
        self.assertEqual((self.member("GCRUN1")[0], self.member("SRTCARD")[0]), ("unknown", "unknown"))
        self.assertIn(("STEP2", "GCPGM3", None), self.steps())
        self.assertIn(("SYSTSIN", "PROD.GC.PARMS(GCRUN1)", "GCRUN1", 1), self.dds())
        self.assertIn(("SYSIN", "PROD.GC.SRTCARD", "SRTCARD", 1), self.dds())
        self.assert_as_full()
        # and when they go again, the job loses them again
        self.remove("GC/PROD.GC.PARMS/GCRUN1.txt")
        self.remove("GC/PROD.GC.PARMS/SRTCARD.txt")
        self.build()
        self.assertIn(("STEP2", None, None), self.steps())
        self.assert_as_full()

    def test_before_the_batch_cards_arriving_unknown_were_not_read(self):
        self.write("GC/PROD.GC.PARMS/GCRUN1.txt", GCRUN1)
        with build_before_item_21():
            self.build()
        self.assertIn(("STEP2", None, None), self.steps())

    def test_what_a_job_does_not_read_forces_no_job(self):
        self.stamp()
        # a data copybook and a document arrive; a PROC is recorded again with its bytes unchanged (marked pending)
        self.write("GC/PROD.GC.COPYLIB/NEWBK.cpy", STARTBK)
        self.write("SHARED/DOCS/RUNBOOK.txt", "Please run the job after the close.\n")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE member SET parse_status='pending' WHERE name='GCPROC'")
            conn.commit()
        finally:
            conn.close()
        self.build()
        self.assertEqual(self.recorded_again(), {"NEWBK", "RUNBOOK", "GCPROC"})
        self.assert_as_full()

    def test_copybook_gives_no_folder_fix_for_a_card_member(self):
        # a card member filed 'unknown' that the job reads and no program copies: `copybook` has no copy of it to
        # speak of - the folder fix it printed ('rename the folder to end in COPYLIB') would file the cards as a
        # copybook, which no card lookup reads (build.CARD_KINDS), and the job would lose them (LESSONS 203)
        self.write("GC/PROD.GC.PARMS/GCRUN1.txt", GCRUN1)
        self.build()
        self.assertEqual(self.member("GCRUN1")[0], "unknown")
        self.assertIn(("SYSTSIN", "PROD.GC.PARMS(GCRUN1)", "GCRUN1", 1), self.dds())
        conn = query.connect(self.db)
        try:
            book = query.cmd_copybook(conn, "GCRUN1")
        finally:
            conn.close()
        self.assertIn("### Programs including it (0)", book)
        self.assertNotIn("Filed `unknown`", book)
        self.assertNotIn("COPYLIB", book)
        # a program that copies it: the note is said, as for any copy expanded from a member filed 'unknown'
        self.write("GC/PROD.GC.SRC/VALPGM.cbl", value_program("VALPGM", "GCRUN1"))
        self.build()
        conn = query.connect(self.db)
        try:
            self.assertIn("**Filed `unknown`** (`", query.cmd_copybook(conn, "GCRUN1"))
        finally:
            conn.close()


# a DB2 program whose SQLCA the precompiler supplies (`EXEC SQL INCLUDE SQLCA` on one line: expand.py writes a
# copy_use row with no member and no note, on purpose) - and a SQLCA copybook in a library, which a program COPYs
DB2PGM = HEAD.format(name="DB2PGM", data="           EXEC SQL INCLUDE SQLCA END-EXEC.\n"
                                         "           EXEC SQL INCLUDE SQLDA END-EXEC.\n", main="") + PLAIN_SECTION + TAIL
SQLCABK = "           05  SQLCAID             PIC X(8).\n           05  SQLCODE             PIC S9(9) COMP.\n"
PRECOMPILER = ("**supplied by the DB2 precompiler** - `EXEC SQL INCLUDE {0}` is written into the program by the "
               "precompiler, not copied from a library: no member is expanded for it and none is missing")


class PrecompilerIncludes(_Forcing):
    """`EXEC SQL INCLUDE SQLCA` / `SQLDA`: the row carries no member and no NOT FOUND note because the DB2 precompiler
    supplies the area - not because a copy left the index. coverage, recover and the un-linked note leave the two
    names out (expand._SYSTEM_INCLUDES); `program`, `pack` and `copybook` read the row by its note alone and told him
    to run recover, then the build, on every DB2 program - on an index this toolkit built too, where no run of either
    changes the row (LESSONS 203)."""

    files = (("GC/PROD.GC.SRC/DB2PGM.cbl", DB2PGM),)

    def assert_precompiler_cells(self, text):
        for book in ("SQLCA", "SQLDA"):
            self.assertIn(f"| {book} | {PRECOMPILER.format(book)} |", text)
        for words in ("no longer linked", "run recover", "left the index", "NOT FOUND", "a member with this name"):
            self.assertNotIn(words, text)

    def test_program_and_pack_say_the_precompiler_supplies_it(self):
        self.assertEqual(self.status("DB2PGM"), "ok")
        self.assertEqual(self.copy_use("DB2PGM", "SQLCA"), [(None,)])
        self.assertEqual(self.not_found_note("DB2PGM", "SQLCA"), [])
        conn = query.connect(self.db)
        try:
            self.assert_precompiler_cells(query.cmd_program(conn, "DB2PGM"))
            self.assert_precompiler_cells(query.cmd_pack(conn, "DB2PGM"))
            self.assertEqual(recover.unlinked_ok_programs(conn), {})
        finally:
            conn.close()
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (0, 0), said)

    def test_with_a_sqlca_copybook_in_the_estate(self):
        # the verifier's second estate: a SQLCA member in a COPYLIB, COPYed by another program - the DB2 program's
        # row is still the precompiler's, and `copybook SQLCA` groups it apart, with no word of a copy that left
        self.write("GC/PROD.GC.COPYLIB/SQLCA.cpy", SQLCABK)
        self.write("GC/PROD.GC.SRC/CPYPGM.cbl", data_program("CPYPGM", "SQLCA", "SQLCODE"))
        self.build()
        self.assert_ok_and_linked("CPYPGM", "SQLCA")
        self.assertEqual((self.status("DB2PGM"), self.copy_use("DB2PGM", "SQLCA")), ("ok", [(None,)]))
        conn = query.connect(self.db)
        try:
            self.assert_precompiler_cells(query.cmd_program(conn, "DB2PGM"))
            book = query.cmd_copybook(conn, "SQLCA")
        finally:
            conn.close()
        self.assertIn("- supplied by the DB2 precompiler (`EXEC SQL INCLUDE SQLCA`), not this member: "
                      "DB2PGM @DB2PGM:", book)
        self.assertIn("CPYPGM @CPYPGM:", book)
        self.assertIn("Grouped by the copy each program actually expanded:\n", book)       # one copy: no skew
        for words in ("left the index", "no longer linked", "still say", "recover", "NOT FOUND", "version skew"):
            self.assertNotIn(words, book)
        stats, said = self.recover()
        self.assertEqual((stats["arrived"], stats["marked"]), (0, 0), said)
        # on an index the build before ROADMAP re-parse item 21 left, the same words: the row is the precompiler's
        self.stamp()
        with build_before_item_21():
            self.build()
        conn = query.connect(self.db)
        try:
            self.assert_precompiler_cells(query.cmd_program(conn, "DB2PGM"))
            self.assertNotIn("left the index", query.cmd_copybook(conn, "SQLCA"))
        finally:
            conn.close()

    def test_a_copy_of_sqlca_not_found_is_still_not_found(self):
        # `COPY SQLCA` is a COBOL COPY the resolver looks up: with no member of the name it says NOT FOUND, as before
        self.write("GC/PROD.GC.SRC/CPYPGM.cbl", data_program("CPYPGM", "SQLCA"))
        self.build()
        self.assert_not_found("CPYPGM", "SQLCA")
        conn = query.connect(self.db)
        try:
            self.assertIn("| SQLCA | **NOT FOUND** |", query.cmd_program(conn, "CPYPGM"))
        finally:
            conn.close()


class ASameNamedProgramArrivesOrGoes(_Forcing):
    """ROADMAP re-parse item 19's rule reads which systems hold a program of the name (build.twin_systems): while
    only GC holds TWPGM, every current listing of the name speaks for it, wherever it is filed; once GC-TEST holds a
    TWPGM too, only the listings in GC's own system do. So a program of that name arriving in another system, or
    going from it, changes which copy of STARTBK GC's TWPGM expands - and moved_names forced only the members that
    COPY the name, never the members that carry it: GC's TWPGM kept the copy the listing had named, with its fields
    and offsets, until a full re-parse (LESSONS 203). A --rebuild deletes the listing rows, so each case is compared
    with the programs parsed again instead."""

    files = (("GC/PROD.GC.SRC/TWPGM.cbl", data_program("TWPGM", "STARTBK", "START-DATE")),
             ("GC/PROD.GC.COPYLIB/STARTBK.cpy", STARTBK),
             ("GC-TEST/TEST.GC.COPYLIB/STARTBK.cpy", STARTBK_SHARED))
    TWIN = "GC-TEST/TEST.GC.SRC/TWPGM.cbl"

    def listing_row(self, current=1):
        """A listing of TWPGM read from a --from folder the index does not hold - filed outside both systems - naming
        TEST.GC.COPYLIB for STARTBK, stored as atlas.recover stores it; the programs of the name marked, as recover
        marks a contradicted choice."""
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(recover.COPY_SOURCE_TABLE)
            conn.execute("INSERT INTO listing_copy_source VALUES('TWPGM','STARTBK','SYSLIB','TEST.GC.COPYLIB',?,"
                         "'2026-09-25',?,100)", (os.path.join(self.td, "listings", "TWPGM.lst"), current))
            conn.execute("UPDATE member SET parse_status='pending' WHERE name='TWPGM'")
            conn.commit()
        finally:
            conn.close()

    def pick(self, system="GC"):
        """(the library the system's TWPGM expanded STARTBK from, START-DATE's offset, how the copy was chosen)"""
        rows = self.q("SELECT r.library, (SELECT f.offset FROM pfield f JOIN program p ON p.id=f.program_id "
                      "WHERE p.member_id=m.id AND f.name='START-DATE'), (SELECT u.detail FROM unresolved u "
                      "WHERE u.member_id=m.id AND u.kind='ambiguous_copybook') FROM member m "
                      "JOIN copy_use c ON c.member_id=m.id LEFT JOIN member r ON r.id=c.resolved_member_id "
                      "WHERE m.name='TWPGM' AND m.kind='cobol' AND m.system=?", system)
        self.assertEqual(len(rows), 1, rows)
        lib, offset, how = rows[0]
        return lib, offset, (how or "").rsplit(" (", 1)[-1].rstrip(")")

    def kept(self, system="GC"):
        """Whether the system's TWPGM was kept by the last build (not parsed again) - see stamp()."""
        return self.q("SELECT scanned_at FROM member WHERE name='TWPGM' AND kind='cobol' AND system=?",
                      system)[0][0] == "kept by the last build"

    def as_parsed_again(self):
        """The index holds what parsing every program of the name again gives."""
        before = facts(self.db)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE member SET parse_status='pending' WHERE name='TWPGM'")
            conn.commit()
        finally:
            conn.close()
        self.build()
        self.assertEqual(facts_differ(before, facts(self.db)), {})

    def test_a_program_of_the_name_arrives_in_another_system(self):
        self.listing_row()
        self.build()
        # the one TWPGM: the listing filed elsewhere speaks for it and names GC-TEST's copy, held with another text
        self.assertEqual(self.pick(), ("TEST.GC.COPYLIB", 15, build.LISTING_HOW + "TEST.GC.COPYLIB"))
        self.stamp()
        self.write(self.TWIN, data_program("TWPGM", "STARTBK", "START-DATE"))
        self.build()
        # GC-TEST holds one too: only GC's own listings speak for GC's, and it has none - its own system's copy
        self.assertFalse(self.kept("GC"), "GC's TWPGM is parsed again")
        self.assertEqual(self.pick(), ("PROD.GC.COPYLIB", 5, "same system"))
        self.assertEqual(self.pick("GC-TEST"), ("TEST.GC.COPYLIB", 15, "same system"))
        self.as_parsed_again()

    def test_a_program_of_the_name_goes_from_another_system(self):
        self.write(self.TWIN, data_program("TWPGM", "STARTBK", "START-DATE"))
        self.build()
        self.listing_row()
        self.build()
        self.assertEqual(self.pick(), ("PROD.GC.COPYLIB", 5, "same system"))
        self.stamp()
        self.remove(self.TWIN)
        self.build()
        # the one TWPGM again: the listing filed elsewhere speaks for it
        self.assertFalse(self.kept("GC"), "GC's TWPGM is parsed again")
        self.assertEqual(self.pick(), ("TEST.GC.COPYLIB", 15, build.LISTING_HOW + "TEST.GC.COPYLIB"))
        self.as_parsed_again()

    def test_without_the_rule_the_program_kept_the_listings_copy(self):
        # the verifier's case: 'to parse: programs 1' - the new member only; GC's TWPGM kept GC-TEST's copy
        self.listing_row()
        self.build()
        self.write(self.TWIN, data_program("TWPGM", "STARTBK", "START-DATE"))
        with mock.patch.object(build, "twins_to_parse", lambda conn, names: set()):
            self.build()
        self.assertEqual(self.pick(), ("TEST.GC.COPYLIB", 15, build.LISTING_HOW + "TEST.GC.COPYLIB"))

    def test_no_current_listing_of_the_name_nothing_else_is_parsed_again(self):
        # with no listing row, or an older compile's only, which systems hold the name decides nothing: the program
        # that arrived or went is the only one parsed
        for current in (None, 0):
            with self.subTest(current=current):
                if current is not None:
                    self.listing_row(current)
                    self.build()
                self.stamp()
                self.write(self.TWIN, data_program("TWPGM", "STARTBK", "START-DATE"))
                self.build()
                self.assertTrue(self.kept("GC"))
                self.assertFalse(self.kept("GC-TEST"))
                self.assertEqual(self.pick(), ("PROD.GC.COPYLIB", 5, "same system"))
                self.stamp()
                self.remove(self.TWIN)
                self.build()
                self.assertTrue(self.kept("GC"))


class TwinsToParse(_Estate):
    """build.program_names_moved and build.twins_to_parse: the names whose program members changed, and the program
    members of those names a current listing speaks for - one query per 500 names."""

    files = (("GC/PROD.GC.SRC/TWPGM.cbl", data_program("TWPGM", "STARTBK")),
             ("GC-TEST/TEST.GC.SRC/TWPGM.cbl", data_program("TWPGM", "STARTBK")),
             ("GC/PROD.GC.SRC/LONEPGM.cbl", data_program("LONEPGM", "STARTBK")))

    def test_the_names_whose_program_members_changed(self):
        moved = build.program_names_moved
        self.assertEqual(moved({}, [seen("p", "cobol")]), {"BOOK"})                                  # arrived
        self.assertEqual(moved({r"x\book.cbl": stored("cobol", name="BOOK")}, []), {"BOOK"})         # went
        self.assertEqual(moved({"p": stored("cobol")}, [seen("p", "asm", sha="s2")]), {"BOOK"})      # re-typed out
        self.assertEqual(moved({"p": stored("unknown")}, [seen("p", "cobol", sha="s2")]), {"BOOK"})  # re-typed in
        # the same program member: new bytes, or recorded again - no system gains or loses one
        self.assertEqual(moved({"p": stored("cobol")}, [seen("p", "cobol", sha="s2")]), set())
        self.assertEqual(moved({"p": stored("cobol", status="pending")}, [seen("p", "cobol")]), set())
        for kind in ("copybook", "unknown", "sql", "proc", "jcl", "asm"):
            self.assertEqual(moved({}, [seen("p", kind)]), set(), kind)
            self.assertEqual(moved({r"x\book.cbl": stored(kind)}, []), set(), kind)

    def test_only_the_names_a_current_listing_speaks_for_one_query_per_500(self):
        conn = sqlite3.connect(self.db)
        try:
            names = {"TWPGM", "LONEPGM"} | {f"NOBODY{i:04d}" for i in range(1200)}
            self.assertEqual(build.twins_to_parse(conn, names), set(), "no listing table: nothing")
            conn.execute(recover.COPY_SOURCE_TABLE)
            conn.executemany("INSERT INTO listing_copy_source VALUES(?,?,'SYSLIB',?,'x.lst','2026-09-25',?,100)",
                             [("twpgm ", "STARTBK", "TEST.GC.COPYLIB", 1), ("LONEPGM", "STARTBK", "TEST.GC.COPYLIB", 0),
                              ("LONEPGM", "STARTBK", "TEST.GC.COPYLIB", None), ("LONEPGM", "", "TEST.GC.COPYLIB", 1),
                              ("LONEPGM", "STARTBK", "", 1)])
            asked = []
            conn.set_trace_callback(lambda sql: asked.append(sql) if "FROM member m" in sql else None)
            forced = build.twins_to_parse(conn, names)
        finally:
            conn.close()
        # both TWPGM members (the row's program name read as load_listing_sources reads it); LONEPGM's rows are an
        # older compile's, undated, or name no copybook or no dataset: none of them decides
        self.assertEqual({os.path.relpath(p, self.root) for p in forced},
                         {os.path.join("GC", "PROD.GC.SRC", "TWPGM.cbl"), os.path.join("GC-TEST", "TEST.GC.SRC", "TWPGM.cbl")})
        self.assertEqual(len(asked), 3, asked)


if __name__ == "__main__":
    unittest.main()
