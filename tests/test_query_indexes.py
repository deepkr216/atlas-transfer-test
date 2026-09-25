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
created nothing - `index_columns` takes every call off. An index built
before the item opens and answers as before (by a scan), and its next build
adds the index.
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


def trimmed_lookups(conn, root):
    """Run the trimmed-name lookups of the gate, `doc`, `images` and `diff`;
    return (what each found, every statement they ran that tests
    TRIM(name))."""
    ran = []
    conn.set_trace_callback(ran.append)
    try:
        found = {
            "gate": verify_citations._resolve("PLAN", root, conn)[1],
            "gate, a kind that is part of the name": verify_citations._resolve("PLANX(draft)", root, conn)[1],
            "doc": [r["name"] for r in query._doc_members(conn, "PLAN")],
            "diff": [r["name"] for r in query._members_named(conn, None, "PLAN", "doc", None, trim=True)],
            "images": query.cmd_images(conn, "PLAN"),
        }
    finally:
        conn.set_trace_callback(None)
    return found, [s for s in ran if "TRIM(" in s.upper() and "MEMBER" in s.upper()]


def plan_of(conn, sql):
    args = ("PLAN",) * sql.count("?")
    return [str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]


class IndexColumns(unittest.TestCase):

    def test_every_call_is_taken_off(self):
        self.assertEqual(build.index_columns("UPPER(TRIM(name))"), {"name"})
        self.assertEqual(build.index_columns("UPPER(name)"), {"name"})
        self.assertEqual(build.index_columns("program_id, target"), {"program_id", "target"})

    def test_the_trimmed_expression_sits_beside_the_plain_one(self):
        entries = {name: (table, expr) for table, name, expr in build.QUERY_INDEXES}
        self.assertEqual(entries["ix_q_member_uname"], ("member", "UPPER(name)"))
        self.assertEqual(entries[TRIMMED], ("member", "UPPER(TRIM(name))"))

    def test_an_older_schema_gets_no_index_and_no_error(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE member(id INTEGER PRIMARY KEY, path TEXT)")      # no name column
        self.assertEqual(build.ensure_query_indexes(conn), 0)
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
            found, sqls = trimmed_lookups(conn, self.root)
            return found, sqls, {s: plan_of(conn, s) for s in sqls}, indexes(conn)
        finally:
            conn.close()

    def check_found(self, found):
        """What the lookups find - the same with the index and without it."""
        self.assertIn("AMBIGUOUS", found["gate"])                  # the program PLAN and the document `PLAN `
        self.assertEqual(found["gate, a kind that is part of the name"], "member PLANX not in index")
        self.assertEqual(found["doc"], ["PLAN "])
        self.assertEqual(found["diff"], ["PLAN "])
        self.assertIn("_no images recorded_", found["images"])    # a text document holds no picture


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
        found, sqls, plans, _have = self.lookups()
        self.check_found(found)
        # the gate three times (PLAN; PLANX(DRAFT) whole, then PLANX), doc, diff, images
        self.assertEqual(len(sqls), 6, sqls)
        for sql, plan in plans.items():
            self.assertTrue(any(TRIMMED in step for step in plan), (sql, plan))
            self.assertFalse(any(step.startswith("SCAN") and ("member" in step or " m" in step) for step in plan),
                             (sql, plan))


class AnIndexBuiltBeforeTheItem(_Built):
    """The index as a build before the item left it - no trimmed-name index: it opens, every lookup answers as
    before (scanning the member table), and the next build adds the index."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(f"DROP INDEX {TRIMMED}")
            conn.commit()
        finally:
            conn.close()

    def test_it_opens_and_answers_by_a_scan(self):
        found, sqls, plans, have = self.lookups()
        self.assertNotIn(TRIMMED, have)
        self.check_found(found)
        self.assertTrue(sqls)
        for sql, plan in plans.items():
            self.assertFalse(any(TRIMMED in step for step in plan), (sql, plan))

    def test_the_next_build_adds_it_once(self):
        out = build_it(self.root, self.docs, self.db)
        self.assertNotIn("Traceback", out)
        found, _sqls, plans, have = self.lookups()
        self.assertIn(TRIMMED, have)
        self.check_found(found)
        self.assertTrue(all(any(TRIMMED in step for step in plan) for plan in plans.values()), plans)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(build.ensure_query_indexes(conn), 0, "a second call finds nothing missing")
            conn.execute(f"DROP INDEX {TRIMMED}")
            self.assertEqual(build.ensure_query_indexes(conn), 1, "an older index gets it back")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
