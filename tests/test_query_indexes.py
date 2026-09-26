"""
The trimmed-name lookups use an index (ROADMAP re-parse item 12).

A file named `PLAN .docx` - a space before the extension - is member `PLAN `,
and nobody types the space: the gate, `doc`, `images` and `diff` look a
name up again with UPPER(TRIM(name)) (LESSONS 156, 158). The gate does it
for every cited name no document carries, so on 121k members each such
lookup scanned the member table: QUERY_INDEXES held an index on UPPER(name)
only. Now it holds one on UPPER(TRIM(name)) beside it. The helper
that decides whether a table has the columns an expression reads took only
UPPER( off, so it read `TRIM(name` for the new expression and would have
created nothing - `index_columns` reads every name that is not a function's.
An index built before the item opens and answers as before (by a scan, and
`doc` and a `diff` of one kind by the kind index), and its next build adds
the index.

The plans are taken of the statements as the commands run them, with their
values bound (LESSONS 209): the first delivery traced the SQL with the
values written in, and for `doc PLAN` that is `... ='PLAN' OR ... ='PLAN'`,
which SQLite folds into one equality - only that folded form used the new
index, while `doc` itself read the kind index. Each command is asked for a
name with an extension too, so its two values differ.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from atlas import build, query, verify_citations  # noqa: E402

TRIMMED = "ix_q_member_utname"

PROGRAM = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. PLAN.
       PROCEDURE DIVISION.
           GOBACK.
"""


def make_estate(td):
    """A program PLAN and, in a documents folder, `PLAN .txt` (member `PLAN `)."""
    root = os.path.join(td, "estate")
    docs = os.path.join(td, "docs")
    os.makedirs(os.path.join(root, "GC", "PROD.GC.SRC"))
    os.makedirs(docs)
    with open(os.path.join(root, "GC", "PROD.GC.SRC", "PLAN.cbl"), "w", encoding="utf-8") as fh:
        fh.write(PROGRAM)
    with open(os.path.join(docs, "PLAN .txt"), "w", encoding="utf-8") as fh:
        fh.write("The plan of the nightly claims run, step by step.\n")
    return root, docs


def build_it(root, docs, db, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = build._main([root, "--db", db, "--also", docs, *extra])
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


class Recording:
    """A connection that keeps every statement it runs with the values it
    binds - what the planner sees when the command runs."""

    def __init__(self, conn):
        self._conn = conn
        self.ran = []

    def execute(self, sql, params=()):
        self.ran.append((sql, tuple(params)))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def trimmed_lookups(conn, root):
    """Run the trimmed-name lookups of the gate, `doc`, `images` and `diff`;
    return (what each found, [(label, statement, values)] for every
    statement they ran that tests TRIM(name))."""
    ran = []
    found = {}
    for label, call in (
            ("gate", lambda c: verify_citations._resolve("PLAN", root, c)[1]),
            ("gate, a kind that is part of the name", lambda c: verify_citations._resolve("PLANX(draft)", root, c)[1]),
            ("doc", lambda c: [r["name"] for r in query._doc_members(c, "PLAN")]),
            ("doc, with the extension", lambda c: [r["name"] for r in query._doc_members(c, "PLAN.txt")]),
            ("diff", lambda c: [r["name"] for r in query._members_named(c, None, "PLAN", "doc", None, trim=True)]),
            ("images", lambda c: query.cmd_images(c, "PLAN")),
            ("images, with the extension", lambda c: query.cmd_images(c, "PLAN.txt"))):
        rec = Recording(conn)
        found[label] = call(rec)
        ran += [(label, sql, args) for sql, args in rec.ran if "TRIM(" in sql.upper() and "MEMBER" in sql.upper()]
    return found, ran


def plan_of(conn, sql, args):
    return [str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]


class IndexColumns(unittest.TestCase):

    def test_every_call_is_taken_off(self):
        self.assertEqual(build.index_columns("UPPER(TRIM(name))"), {"name"})
        self.assertEqual(build.index_columns("UPPER(name)"), {"name"})
        self.assertEqual(build.index_columns("program_id, target"), {"program_id", "target"})

    def test_other_shapes_of_expression(self):
        # split at every comma first, COALESCE(system, '') read as 'COALESCE(system' and "'')"; a lower-case call was
        # not taken off - either one read as an older schema and skipped without a word (LESSONS 209)
        self.assertEqual(build.index_columns("COALESCE(system, '')"), {"system"})
        self.assertEqual(build.index_columns("lower(name)"), {"name"})
        self.assertEqual(build.index_columns("UPPER(name) COLLATE NOCASE, kind DESC"), {"name", "kind"})
        self.assertEqual(build.index_columns("substr(Name, 1, 3), 'a,b(c)'"), {"name"})
        self.assertEqual(build.index_columns('"library", upper ( TRIM( name ) )'), {"library", "name"})

    def test_every_entry_reads_columns_its_table_has(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with open(os.path.join(os.path.dirname(HERE), "atlas", "schema.sql"), encoding="utf-8") as fh:
            conn.executescript(fh.read())
        for table, name, expr in build.QUERY_INDEXES:
            cols = {str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info('{table}')")}
            self.assertTrue(cols, (name, table))
            self.assertLessEqual(build.index_columns(expr), cols, name)

    def test_the_trimmed_expression_sits_beside_the_plain_one(self):
        entries = {name: (table, expr) for table, name, expr in build.QUERY_INDEXES}
        self.assertEqual(entries["ix_q_member_uname"], ("member", "UPPER(name)"))
        self.assertEqual(entries[TRIMMED], ("member", "UPPER(TRIM(name))"))
        self.assertEqual(query.TRIMMED_NAME_INDEX, TRIMMED)

    def test_an_older_schema_gets_no_index_and_no_error_and_says_so(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE member(id INTEGER PRIMARY KEY, path TEXT)")      # no name column
        said = []
        self.assertEqual(build.ensure_query_indexes(conn, said.append), 0)
        self.assertEqual(indexes(conn), set())
        # the two entries on member, each said once; a table the schema lacks is not an entry skipped
        self.assertEqual(said, [f"  lookup index {n} not added: table member has no column name (an older schema)"
                                for n in ("ix_q_member_uname", TRIMMED)])
        self.assertEqual(build.ensure_query_indexes(conn), 0)                      # and quiet with no one to tell

    def test_a_column_the_reading_misses_is_said_too(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE member(id INTEGER PRIMARY KEY, path TEXT)")
        said = []
        with mock.patch.object(build, "index_columns", return_value=set()):
            self.assertEqual(build.ensure_query_indexes(conn, said.append), 0)
        self.assertEqual(len(said), 2, said)
        self.assertTrue(said[0].startswith("  lookup index ix_q_member_uname not added: table member: no such column: "
                                           "name"), said)
        self.assertEqual(indexes(conn), set())


class _Built(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.root, self.docs = make_estate(self.td)
        self.db = os.path.join(self.td, "t.db")
        build_it(self.root, self.docs, self.db, "--rebuild", "--quiet")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def lookups(self):
        conn = query.connect(self.db)
        try:
            found, ran = trimmed_lookups(conn, self.root)
            return found, [(label, sql, args, plan_of(conn, sql, args)) for label, sql, args in ran], indexes(conn)
        finally:
            conn.close()

    def check_found(self, found):
        """What the lookups find - the same with the index and without it."""
        self.assertIn("AMBIGUOUS", found["gate"])                  # the program PLAN and the document `PLAN `
        self.assertEqual(found["gate, a kind that is part of the name"], "member PLANX not in index")
        self.assertEqual(found["doc"], ["PLAN "])
        self.assertEqual(found["doc, with the extension"], ["PLAN "])
        self.assertEqual(found["diff"], ["PLAN "])
        for label in ("images", "images, with the extension"):
            self.assertIn("_no images recorded_", found[label])  # a text document holds no picture

    def check_ran(self, ran):
        # the gate three times (PLAN; PLANX(DRAFT) whole, then PLANX), doc and images twice each, diff once
        self.assertEqual(sorted(label for label, *_rest in ran),
                         ["diff", "doc", "doc, with the extension", "gate", "gate, a kind that is part of the name",
                          "gate, a kind that is part of the name", "images", "images, with the extension"], ran)
        # with the extension the two values differ: the plan is not the one of a single folded equality
        (doc_ext,) = [args for label, _sql, args, _plan in ran if label == "doc, with the extension"]
        self.assertEqual(doc_ext, ("PLAN.TXT", "PLAN"))


class TheLookupsUseTheIndex(_Built):

    def test_a_build_creates_every_query_index(self):
        conn = sqlite3.connect(self.db)
        try:
            have = indexes(conn)
            for _table, name, _expr in build.QUERY_INDEXES:
                self.assertIn(name, have)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIMMED,)).fetchone()[0]
            self.assertIn("UPPER(TRIM(name))", sql)
        finally:
            conn.close()

    def test_the_gate_doc_images_and_diff_search_it(self):
        found, ran, _have = self.lookups()
        self.check_found(found)
        self.check_ran(ran)
        for label, sql, args, plan in ran:
            self.assertTrue(any(TRIMMED in step for step in plan), (label, sql, args, plan))
            self.assertFalse(any(step.startswith("SCAN") and ("member" in step or " m" in step) for step in plan),
                             (label, sql, args, plan))


class DocsExactNameLookup(_Built):
    """`doc`'s first statement looks the name up as typed (UPPER(name), the name or its stem). It read the kind index
    (every document) with its values bound, although the name's index was there; it writes +kind when that index is
    there, as the trimmed statement does (LESSONS 210)."""

    def exact_plans(self, conn):
        out = []
        for name in ("PLAN", "PLAN.txt"):
            rec = Recording(conn)
            self.assertEqual([r["name"] for r in query._doc_members(rec, name)], ["PLAN "])
            out += [(name, sql, args, plan_of(conn, sql, args)) for sql, args in rec.ran
                    if "UPPER(NAME)=" in sql.upper().replace(" ", "") and "TRIM(" not in sql.upper()]
        self.assertEqual(len(out), 2, out)
        return out

    def test_it_searches_the_names_index(self):
        conn = query.connect(self.db)
        try:
            for name, sql, args, plan in self.exact_plans(conn):
                self.assertTrue(any(query.NAME_INDEX in step for step in plan), (name, sql, args, plan))
                self.assertFalse(any("ix_member_kind" in step for step in plan), (name, sql, args, plan))
        finally:
            conn.close()

    def test_without_the_names_index_it_reads_the_kind_index_as_before(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(f"DROP INDEX {query.NAME_INDEX}")
            conn.execute(f"DROP INDEX {TRIMMED}")
            conn.commit()
        finally:
            conn.close()
        conn = query.connect(self.db)
        try:
            for name, sql, args, plan in self.exact_plans(conn):
                self.assertTrue(any("ix_member_kind" in step for step in plan), (name, sql, args, plan))
        finally:
            conn.close()


class AnIndexBuiltBeforeTheItem(_Built):
    """The index as a build before the item left it - no trimmed-name index: it opens, every lookup answers as
    before (the gate and images scanning the member table, doc and a diff of one kind reading the kind index),
    and the next build adds the index."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(f"DROP INDEX {TRIMMED}")
            conn.commit()
        finally:
            conn.close()

    def test_it_opens_and_answers_as_before(self):
        found, ran, have = self.lookups()
        self.assertNotIn(TRIMMED, have)
        self.check_found(found)
        self.check_ran(ran)
        for label, sql, args, plan in ran:
            self.assertFalse(any(TRIMMED in step for step in plan), (label, sql, args, plan))
            if label.startswith("doc") or label == "diff":
                self.assertTrue(any("ix_member_kind" in step for step in plan), (label, sql, args, plan))

    def test_the_next_build_adds_it_once(self):
        out = build_it(self.root, self.docs, self.db)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("not added", out)
        found, ran, have = self.lookups()
        self.assertIn(TRIMMED, have)
        self.check_found(found)
        self.assertTrue(all(any(TRIMMED in step for step in plan) for _l, _s, _a, plan in ran), ran)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(build.ensure_query_indexes(conn), 0, "a second call finds nothing missing")
            conn.execute(f"DROP INDEX {TRIMMED}")
            self.assertEqual(build.ensure_query_indexes(conn), 1, "an older index gets it back")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
