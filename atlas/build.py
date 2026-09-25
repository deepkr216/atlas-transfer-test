"""
build.py - walk the folder, extract facts, write atlas.db. No model calls.

    python -m atlas.build  C:/path/to/estate  --db atlas.db
    python -m atlas.build  C:/path/to/estate  --db atlas.db --manifest manifest.json
    python -m atlas.build  C:/path/to/estate  --db atlas.db --write-expanded out/expanded

Two passes: inventory every file (hash, classify), then parse by kind. Every
COBOL program is expanded (COPY / EXEC SQL INCLUDE resolved, REPLACING
applied) before it is parsed, so facts reflect what the compiler saw. Line
numbers on program facts are therefore EXPANDED line numbers; `expand_run`
maps them back to (member, line) and query.py does the translation.

Nothing that fails is dropped: a member that will not parse is recorded with
parse_status='failed' and the error, and every unresolved reference lands in
`unresolved`. The summary at the end is the coverage report; read it before
trusting any answer built on the index.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import faulthandler
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import AbstractSet, Callable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar

from . import classify, cobol, copybook, docs, expand, ims, jcl, reader, screens, txn
from .reader import Line

VERSION = "0.1.0"
MAX_MEMBER_BYTES = 300 * 1024 * 1024      # a file bigger than this is not a document to index, it is a dump
SLOW_MEMBER_SECONDS = 30                  # a member still parsing after this long is named on screen, and again every 30 s
MEMBER_TIME_LIMIT = 900                   # a member still parsing after this long is given up on (--member-limit)
STUCK_GRACE = 30                          # seconds a timed-out parse gets to notice the interruption
INVENTORY_TIME_LIMIT = 900                # reading + classifying ONE file may not take longer than this
BIG_TEXT_BYTES = 32 * 1024 * 1024         # nothing this big is a mainframe member: hash it, classify by its first 8 KB, move on


class MemberTimeout(Exception):
    """One member took longer than the limit: recorded as failed, skipped."""


class ParserStuck(Exception):
    """One member cannot even be interrupted (the parser is inside C code,
    a regular expression gone quadratic): the build stops and names it."""


def _stop_worker(t: threading.Thread, grace: float) -> bool:
    """Raise MemberTimeout inside a worker thread; True if it stopped."""
    if not t.is_alive():
        return True
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), ctypes.py_object(MemberTimeout))
    if n > 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident), None)
    t.join(grace)
    return not t.is_alive()


def run_with_limit(fn, args: tuple, limit: float, label: str, grace: Optional[float] = None) -> None:
    """Run fn(*args) in a worker thread and give up after `limit` seconds:
    a 121,000-member build must never sit on ONE member all night. The
    worker is interrupted with MemberTimeout (which fires between Python
    statements); a worker that does not stop inside `grace` seconds is
    stuck in C code and raises ParserStuck, which ends the build with the
    member's name."""
    grace = STUCK_GRACE if grace is None else grace
    box: Dict[str, BaseException] = {}

    def target():
        try:
            fn(*args)
        except BaseException as e:                                      # noqa: BLE001 - handed to the caller
            box["exc"] = e

    t = threading.Thread(target=target, daemon=True, name=f"parse {label}")
    t.start()
    deadline = time.time() + limit
    try:
        while t.is_alive() and time.time() < deadline:
            t.join(0.5)                                                 # short waits keep Ctrl+C working on Windows
    except KeyboardInterrupt:
        _stop_worker(t, 5)
        raise
    if t.is_alive():
        if _stop_worker(t, grace):
            raise MemberTimeout(f"exceeded the {int(limit)} s member limit ({label}) - facts of this member dropped, "
                                f"build continued; raise --member-limit or move the file out of the estate")
        raise ParserStuck(label)
    exc = box.get("exc")
    if isinstance(exc, MemberTimeout):
        raise MemberTimeout(f"exceeded the {int(limit)} s member limit ({label}) - facts of this member dropped, "
                            f"build continued; raise --member-limit or move the file out of the estate")
    if exc is not None:
        raise exc


def _clear_facts(conn: sqlite3.Connection, mid: int) -> None:
    """Drop every fact row of a member but keep its inventory row: a parse
    that failed or timed out must not leave half a program behind."""
    cur = conn.execute("SELECT * FROM member WHERE id=?", (mid,))
    row = cur.fetchone()
    if row is None:
        return
    cols = [d[0] for d in cur.description]
    _forget_member(conn, mid)
    conn.execute(f"INSERT INTO member({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row))


PROGRESS_SECONDS = 10.0                   # the status line's own clock
FOLDER_REPORT_FILES = 5                   # a folder is reported when done if it held this many files,
FOLDER_REPORT_BYTES = 5 * 1024 * 1024     # ... or this many bytes,
FOLDER_REPORT_SECONDS = 5.0               # ... or took this long; smaller ones are counted, not listed
BULK_FORGET = 200                         # more changed members than this: old facts are removed in bulk

KIND_LABELS = {"copybook": "copybooks", "cobol": "programs", "proc": "PROCs", "jcl": "jobs", "dbd": "DBDs",
               "psb": "PSBs", "doc": "documents", "ctlcard": "control cards", "bms": "BMS maps",
               "mfs": "MFS formats", "csd": "CSD extracts", "imsgen": "IMS stage-1 members",
               "listing": "compiler listings", "unknown": "unrecognised members", "empty": "empty members",
               "stub": "stubs (only numbers)",
               "sql": "SQL members", "sched": "scheduler exports", "asm": "assembler members", "rexx": "REXX members"}


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n / 1024 / 1024:.1f} MB"
CURRENT_FILE = "atlas-current.txt"        # beside the db: the member in hand (path + label) - read by atlas.supervise
SKIP_FILE = "atlas-skip.txt"              # beside the db: members that froze the parser - written by atlas.supervise
_FREEZE = os.environ.get("ATLAS_TEST_FREEZE")   # tests only: this member freezes the whole process, output included
_FAULT = os.environ.get("ATLAS_TEST_FAULT")     # tests only: "diskfull:<member file>" or "crash:post"
PROBLEMS_FILE = "atlas-problems.txt"            # beside the db: every problem of the run, one line each
PROBLEM_EXAMPLES = 5                            # names shown per kind of problem in the end-of-build list


class BuildFatal(Exception):
    """The index itself cannot be written (disk full, I/O error, read-only,
    locked): no further member can succeed, so the build stops once."""


_FATAL_DB = ("disk is full", "database or disk is full", "disk i/o error", "attempt to write a readonly database",
             "database is locked", "unable to open database", "no space left", "out of memory")


def fatal_db_error(e: BaseException) -> Optional[str]:
    """The message when `e` means the index cannot be written at all."""
    if isinstance(e, (sqlite3.OperationalError, sqlite3.DatabaseError, MemoryError)) or \
            (isinstance(e, OSError) and getattr(e, "errno", None) == 28):
        msg = str(e) or type(e).__name__
        if isinstance(e, MemoryError) or any(k in msg.lower() for k in _FATAL_DB) or getattr(e, "errno", None) == 28:
            return msg
    return None


def toolkit_where(e: BaseException) -> str:
    """`cobol.py:1234 in _extract_x` - the deepest toolkit frame, so a failure
    line can be fixed from the screen alone (no estate text in it)."""
    import traceback
    frames = [f for f in traceback.extract_tb(e.__traceback__)
              if "/atlas/" in f.filename.replace("\\", "/")]
    if not frames:
        return ""
    f = frames[-1]
    return f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}"


def _stamp() -> str:
    return time.strftime("%H:%M:%S")
STUCK_DUMP_SECONDS = 300                  # one item in hand this long: the exact place is written to STUCK_FILE
STUCK_FILE = "atlas-stuck.txt"


class Progress:
    """The build's status line on its OWN clock: a daemon thread prints
    every `every` seconds whatever the current step last reported plus the
    item in hand and how long it has been in hand. One Progress runs for the
    whole build and every step announces itself (`phase`), so there is no
    stretch of the build without a status - the silent removal of old facts
    after a `git pull` looked like a hang on the last control card read
    (LESSONS 142, 147)."""

    def __init__(self, say, every: Optional[float] = None, hint_after: float = SLOW_MEMBER_SECONDS,
                 limit: float = 0, dump_after: Optional[float] = None, dump_file: Optional[str] = None,
                 current_file: Optional[str] = None):
        self.say = say
        self.every = PROGRESS_SECONDS if every is None else every
        self.hint_after, self.limit = hint_after, limit
        self.dump_after = STUCK_DUMP_SECONDS if dump_after is None else dump_after
        self.dump_file = dump_file or STUCK_FILE
        self.current_file = current_file
        self.line = lambda: ""
        self.current: Optional[Tuple[str, float]] = None
        self.phase_name: Optional[str] = None
        self._dumped: Optional[Tuple[str, float]] = None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="progress")

    def dump(self, cur: Tuple[str, float]) -> None:
        """Where the build is stuck, as toolkit file names, line numbers and
        function names only - nothing from the estate. Shareable."""
        try:
            with open(self.dump_file, "w", encoding="utf-8") as fh:
                fh.write(f"atlas build: {cur[0]} in hand for {_hms(time.time() - cur[1])} at "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                         "Every thread's position (toolkit code only; the main thread is the one to read):\n\n")
                faulthandler.dump_traceback(file=fh, all_threads=True)
            self.say(f"  !!! {cur[0]} has been in hand for {_hms(time.time() - cur[1])}: the exact place is written "
                     f"to {self.dump_file} - send that file (it holds toolkit line numbers, nothing from the estate)")
        except OSError:
            pass

    def _write_current(self, first: str, second: str) -> None:
        """atlas-current.txt: the member in hand (its path) or PHASE and the
        step - what the outside supervisor reads when the build goes silent."""
        if not self.current_file:
            return
        try:
            with open(self.current_file, "w", encoding="utf-8") as fh:
                fh.write(f"{first}\n{second}\n")
        except OSError:
            pass

    def set(self, line) -> None:
        """`line` is called on the clock: it returns the counters part."""
        self.line = line

    def phase(self, name: str, limit: float = 0) -> None:
        """A step of the build, announced with the time and named on every
        status line until the next step starts."""
        t0 = time.time()
        self.phase_name, self.limit = name, limit
        self.current = None
        self.line = lambda: f"{name} for {_hms(time.time() - t0)}"
        self.say(f"{_stamp()} == {name}")
        self._write_current("PHASE", name)

    def now(self, label: Optional[str], path: Optional[str] = None) -> None:
        """What the step holds at the moment (None between items). With a
        path, the item is a member the supervisor can put on the skip list."""
        self.current = (label, time.time()) if label else None
        if path is not None:
            self._write_current(path, label or "")

    def text(self) -> str:
        try:
            head = self.line() or ""
        except Exception:                                               # noqa: BLE001 - a status line never breaks a build
            head = ""
        cur = self.current
        if cur:
            held = time.time() - cur[1]
            head += f" - now {cur[0]}"
            if held >= self.hint_after:
                head += (f" ({_hms(held)} on this one"
                         + (f"; the build gives up on it at {_hms(self.limit)}" if self.limit else "") + ")")
        return head

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            t = self.text()
            if t:
                self.say(f"{_stamp()}  ... " + t)
            cur = self.current
            if cur and self.dump_after and time.time() - cur[1] >= self.dump_after and self._dumped != cur:
                self._dumped = cur
                self.dump(cur)

    def start(self) -> "Progress":
        if not self._t.is_alive() and not self._stop.is_set():
            self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._t.is_alive():
            self._t.join(1.0)
HERE = os.path.dirname(os.path.abspath(__file__))

CODE_KINDS = {"cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs",
              "sql", "asm", "rexx"}
# the kinds a COPY is expanded from (make_resolver's candidates). What a COPY of a name resolves to changes whenever a
# member of that name and of one of these kinds arrives, changes, goes, is recorded again under a new id, or is
# re-typed into or out of them - so an incremental build parses again every program that copies such a name
# (moved_names, copiers_to_parse; ROADMAP re-parse item 21). atlas.recover keeps the same tuple (recover.RESOLVER_KINDS).
RESOLVER_KINDS = ("copybook", "cobol", "sql", "unknown")
# a member of one of those kinds whose every code line holds only digits is filed `stub` instead (reader.stub_count,
# _inventory_one): the resolver never expands one, and a program whose only members of a COPY's name are stubs says so
# in place of NOT FOUND (expand.stub_note, ROADMAP re-parse item 23). A stub arriving, going or re-typed changes that
# note, so the programs copying its name are parsed again too: COPY_KINDS is what moved_names reads for them
STUB_KIND = "stub"
COPY_KINDS = RESOLVER_KINDS + (STUB_KIND,)
# the kinds a job's expansion reads a member from: a cataloged PROC (_proc_facts), an `// INCLUDE MEMBER=` member
# (_include_text) and a control-card member, `DSN=LIB(MEMBER)` or a sequential dataset's last qualifier (_card_text).
# A member of one of these kinds that arrives, changes, goes or is re-typed into or out of them changes the facts of
# every job that reads it, and a job records only the NAME it looked for (a card taken by its last qualifier not even
# that, when nothing was found) - so an incremental build parses every job and PROC again (moved_names with
# again=False: a job holds no member id of what it read, so a member recorded again with the same bytes changes
# nothing in it; ROADMAP re-parse item 21)
# A stub is read by the jobs exactly where the member its classifier made of it was read before ROADMAP re-parse item
# 23 filed it `stub` (_stub_read_as): one read as 'unknown' - a date or a count card in a library with no hint - as an
# INCLUDE or cards, one read as 'sql' as cards, and one read as a copybook or a program never (it was `empty` or a
# copybook then) - on equal terms with the other members of the name in _pick_member's JCLLIB / department / manifest
# order, so a department's own card holding only numbers is still its job's cards (LESSONS 205). Only a COPY never
# expands a stub
PROC_KINDS = ("proc", "jcl")
INCLUDE_KINDS = ("jcl", "proc", "ctlcard", "unknown", STUB_KIND)
CARD_KINDS = ("ctlcard", "unknown", STUB_KIND, "sql", "jcl", "proc")
JOB_READ_KINDS = tuple(dict.fromkeys(PROC_KINDS + INCLUDE_KINDS + CARD_KINDS))
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".svn", "$RECYCLE.BIN"}

EXTRA_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS src_fts USING fts5(
    member_name, kind, member_id UNINDEXED, line_no UNINDEXED, text,
    tokenize="unicode61 tokenchars '-_#@$:'");
CREATE TABLE IF NOT EXISTS doc_section(
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    heading TEXT, text TEXT,
    ordinal INTEGER);                     -- 1.. = text sections; 1001.. = OCR'd images
CREATE TABLE IF NOT EXISTS doc_image(
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    name TEXT,
    extracted_path TEXT,                  -- where atlas.ocr wrote the image
    ocr_text TEXT,                        -- NULL = not read yet; '' = read, nothing found
    anchor TEXT);                         -- the sheet / slide / heading the picture sits in
CREATE TABLE IF NOT EXISTS expand_run(
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    exp_start INTEGER, exp_end INTEGER,
    src_member INTEGER REFERENCES member(id), src_start INTEGER,
    depth INTEGER, via_copy TEXT);
CREATE INDEX IF NOT EXISTS ix_exprun ON expand_run(program_id, exp_start);
-- the rowid range of each member's rows in src_fts: its member_id column
-- cannot be indexed, so without this every delete and every line lookup
-- scanned the whole search index (LESSONS 148)
CREATE TABLE IF NOT EXISTS fts_span(
    member_id INTEGER NOT NULL REFERENCES member(id) ON DELETE CASCADE,
    lo INTEGER NOT NULL, hi INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fts_span_mem ON fts_span(member_id);
CREATE TABLE IF NOT EXISTS atlas_meta(key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Mem:
    id: int
    path: str
    name: str
    kind: str
    library: str
    norm_sha: str
    authoritative: int = 0
    skip: bool = False        # unchanged since the last build: facts kept, not re-parsed
    system: str = ""          # department, from the manifest (systems / system_of)
    nbytes: int = 0           # size on disk, shown on the status line


class Ctx:
    def __init__(self, conn: sqlite3.Connection, quiet: bool = False):
        self.conn = conn
        self.quiet = quiet
        self.members: List[Mem] = []
        self.by_name: Dict[str, List[Mem]] = {}
        self._lines: Dict[int, List[Line]] = {}
        self.stats: Dict[str, int] = {}
        self.write_expanded: Optional[str] = None
        self.proc_cache: Dict[str, Optional[jcl.JclFacts]] = {}
        self.copylib_order: Dict[str, List[str]] = {}   # SYSTEM -> copybook library folder names, SYSLIB order
        self.kind_of: Dict[str, str] = {}               # library folder name -> kind declared in sources.json
        # path -> (the shape's kind, its line) for a member the kind declared for its library typed copybook over the
        # shape of an Assembler, listing or MFS member: kept beside the member as a 'declared_kind' row, so `program`
        # and `copybook` say it (LESSONS 199); written by the inventory's workers
        self.shape_over: Dict[str, Tuple[str, int]] = {}
        self.progress: Optional["Progress"] = None      # the status line's clock, stopped by whoever ends the build
        self.problems: List[Tuple[str, str, str]] = []   # (kind of problem, path, detail) - listed at the end of the build
        self.problems_file: Optional[str] = None
        # what the compiler listings say (atlas.recover's listing_copy_source, read once): (PROGRAM, COPYBOOK) ->
        # (the library dataset the listing names, the system of the listing member that names it, whether the
        # listing is current); and the dataset each folder holds (the `library` table, else the folder's own name),
        # cached per folder - make_resolver reads both before its precedence chain
        self.listing_sources: Dict[Tuple[str, str], Tuple[ListingRow, ...]] = {}
        self.folder_dataset: Dict[str, Optional[str]] = {}
        self.stub_lines: Dict[int, Optional[int]] = {}  # member id -> lines holding numbers, for a stub a COPY names
        self.stub_kinds: Dict[int, str] = {}            # member id -> the classifier's kind of a stub a job names

    def problem(self, kind: str, path: str, detail: str, line: Optional[str] = None) -> None:
        """A member or file that could not be indexed: shown now (with the time),
        written to atlas-problems.txt, and listed again at the end of the build -
        after 121,000 members the line printed at 02:00 is long gone."""
        self.problems.append((kind, path, detail))
        self.say(line if line is not None else f"{_stamp()}  PROBLEM {kind}: {path} - {detail}")
        if self.problems_file:
            try:
                with open(self.problems_file, "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t{path}\t{detail}\n")
            except OSError:
                pass

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def lines_for(self, mem: Mem) -> List[Line]:
        if mem.id not in self._lines:
            text, data, enc = reader.load(mem.path)
            lines, _ = reader.read_cobol_lines(text, data=data, enc=enc)
            self._lines[mem.id] = lines
            if len(self._lines) > 4000:            # bounded cache
                self._lines.pop(next(iter(self._lines)))
        return self._lines[mem.id]

    def say(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)


# --------------------------------------------------------------------------
# db
# --------------------------------------------------------------------------

def open_db(path: str, rebuild: bool = False) -> sqlite3.Connection:
    if rebuild and os.path.exists(path):
        os.remove(path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
    conn = sqlite3.connect(path, check_same_thread=False)   # one member at a time is parsed on a worker thread
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Views are re-created from the schema every time (their column lists
    # grow); tables are evolved in place below.
    conn.execute("DROP VIEW IF EXISTS v_dataset_flow")
    conn.execute("DROP VIEW IF EXISTS v_ambiguous_member")
    # Columns added after a db was first built: add them in place rather than
    # forcing a rebuild of a 40,000-member index. Done BEFORE the schema runs
    # so a view over a new column can be created on an old database.
    for table, col, decl in (("doc_section", "ordinal", "INTEGER"), ("doc_image", "extracted_path", "TEXT"),
                             ("doc_image", "ocr_text", "TEXT"), ("doc_image", "anchor", "TEXT"),
                             ("member", "system", "TEXT"),
                             ("step", "from_proc", "TEXT"), ("step", "parent_step", "TEXT"),
                             ("dd", "mode_source", "TEXT"), ("transaction_def", "group_name", "TEXT"),
                             ("transaction_def", "detail", "TEXT"), ("dd", "card_member", "TEXT"),
                             ("dd", "is_temp", "INTEGER DEFAULT 0"), ("step", "guard", "TEXT"),
                             ("job", "joblib", "TEXT"), ("job", "job_cond", "TEXT"), ("job", "jcllib", "TEXT"),
                             ("perform_edge", "kind", "TEXT DEFAULT 'perform'"),
                             ("paragraph", "kind", "TEXT DEFAULT 'paragraph'"),
                             ("dli_call", "pcb_index", "INTEGER"), ("dli_call", "resolution", "TEXT"),
                             ("dli_call", "dest", "TEXT"), ("dli_call", "psb_name", "TEXT"),
                             ("dli_call", "dbd_name", "TEXT"), ("dli_call", "procopt", "TEXT"),
                             ("dli_call", "pcb_source", "TEXT"), ("ims_pcb", "list_no", "INTEGER DEFAULT 0"),
                             ("ims_pcb", "procseq", "TEXT"), ("ims_dbd", "dd1", "TEXT"), ("ims_dbd", "dd2", "TEXT"),
                             ("dataset", "recordsize_max", "INTEGER"), ("dataset", "key_len", "INTEGER"),
                             ("dataset", "key_off", "INTEGER"), ("dataset", "gdg_limit", "INTEGER"),
                             ("dataset", "relates_to", "TEXT"), ("build_run", "fingerprint", "TEXT"),
                             ("build_run", "manifest_sha", "TEXT"), ("build_run", "declared_kinds", "TEXT"),
                             ("field_ref", "pfield_id", "INTEGER"), ("call_edge", "returning_item", "TEXT"),
                             ("sql_col_ref", "pfield_id", "INTEGER")):
        _ensure_column(conn, table, col, decl)
    with open(os.path.join(HERE, "schema.sql"), "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    had_spans = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fts_span'").fetchone() is not None
    had_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='src_fts'").fetchone() is not None
    conn.executescript(EXTRA_SCHEMA)
    if not had_spans and had_fts and conn.execute("SELECT rowid FROM src_fts LIMIT 1").fetchone() is not None:
        # search rows written before spans were recorded: deletes fall back to a scan until they are gone
        conn.execute("INSERT OR REPLACE INTO atlas_meta(key, value) VALUES('fts_legacy', '1')")
        conn.commit()
    return conn


def fts_legacy(conn: sqlite3.Connection) -> bool:
    try:
        r = conn.execute("SELECT value FROM atlas_meta WHERE key='fts_legacy'").fetchone()
    except sqlite3.DatabaseError:
        return True
    return bool(r and r[0] == "1")


def fts_insert(conn: sqlite3.Connection, member_id: int, rows: Sequence[tuple]) -> None:
    """Rows into the search index, and the rowid range they took (rowids of
    one insert are contiguous: each new row gets the largest rowid + 1)."""
    if not rows:
        return
    conn.executemany("INSERT INTO src_fts(member_name,kind,member_id,line_no,text) VALUES(?,?,?,?,?)", rows)
    hi = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO fts_span(member_id, lo, hi) VALUES(?,?,?)", (member_id, hi - len(rows) + 1, hi))


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if cols and col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _j(v) -> Optional[str]:
    return json.dumps(v) if v not in (None, [], {}) else None


# --------------------------------------------------------------------------
# pass 1 - inventory
# --------------------------------------------------------------------------

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The modules whose code decides what a member's facts ARE. A change to one
# of them makes the index stale and forces a re-parse of every member. The
# query / gate / UI / fetch / convert / OCR modules read the index or write
# beside it; changing them must not cost a 121,000-member estate hours.
FACT_MODULES = ("build.py", "schema.sql", "reader.py", "classify.py", "cobol.py", "copybook.py", "expand.py",
                "jcl.py", "ims.py", "screens.py", "txn.py", "docs.py")


def tool_fingerprint(where: str = HERE) -> str:
    """sha256 of the fact-producing source (FACT_MODULES). A `git pull` that
    changes a parser must re-parse every member, or the index keeps facts an
    older parser made; a pull that only changes queries or prompts does not."""
    h = hashlib.sha256()
    for fn in FACT_MODULES:
        p = os.path.join(where, fn)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                h.update(fn.encode() + fh.read())
    return h.hexdigest()[:16]


def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def code_line_count(kind: str, text: str, data: bytes, enc: str) -> int:
    """Lines that carry code: comment-only and empty members become
    programs with no facts otherwise, and outrank the real one."""
    if kind in ("cobol", "copybook"):
        lines, _ = reader.read_cobol_lines(text, data=data, enc=enc)
        return sum(1 for l in lines if not l.is_comment and l.code.strip())
    recs = reader._split_records(text, data, enc)
    if kind in ("jcl", "proc"):
        return sum(1 for r in recs if r.strip() and not r.startswith("//*") and r.strip() != "//")
    return sum(1 for r in recs if r.strip() and not r.lstrip().startswith("*"))


OUTPUT_MARKER = ".atlas-output"


def norm_hash(kind: str, text: str, data: bytes, enc: str) -> Tuple[str, int, int]:
    """(norm_sha, line_count, fixed_format)"""
    if kind in ("cobol", "copybook"):
        lines, fixed = reader.read_cobol_lines(text, data=data, enc=enc)
        payload = "\n".join(l.code.rstrip().upper() for l in lines if not l.is_comment)
        return sha(payload.encode("utf-8", "replace")), len(lines), int(fixed)
    recs = reader._split_records(text, data, enc)
    if kind in ("jcl", "proc", "ctlcard"):
        payload = "\n".join(r[:71].rstrip().upper() for r in recs if not r.startswith("//*"))
    else:
        payload = "\n".join(r.rstrip().upper() for r in recs)
    return sha(payload.encode("utf-8", "replace")), len(recs), 0


def _forget_member(conn: sqlite3.Connection, mid: int) -> None:
    """Remove a member and everything derived from it - including rows in
    OTHER members that only reference it, which a cascade cannot reach."""
    conn.execute("UPDATE copy_use SET resolved_member_id=NULL WHERE resolved_member_id=?", (mid,))
    conn.execute("DELETE FROM expand_run WHERE src_member=?", (mid,))
    conn.execute("DELETE FROM db2_object WHERE member_id=?", (mid,))
    if fts_legacy(conn):
        conn.execute("DELETE FROM src_fts WHERE member_id=?", (mid,))
    else:
        conn.executemany("DELETE FROM src_fts WHERE rowid BETWEEN ? AND ?",
                         conn.execute("SELECT lo, hi FROM fts_span WHERE member_id=?", (mid,)).fetchall())
    conn.execute("DELETE FROM member WHERE id=?", (mid,))


def _forget_members(conn: sqlite3.Connection, mids: Sequence[int], progress: Optional["Progress"] = None) -> None:
    """Remove many members at once. One by one, each removal scanned the
    search index and every table without an index on its member column:
    quadratic - 2,000 members took 68 s, 121,000 would take days, silently,
    after every `git pull` that changed a parser (LESSONS 147). In bulk, each
    table is visited once."""
    if len(mids) <= BULK_FORGET:
        for mid in mids:
            _forget_member(conn, mid)
        if fts_legacy(conn) and conn.execute("SELECT 1 FROM member LIMIT 1").fetchone() is None:
            conn.execute("DELETE FROM atlas_meta WHERE key='fts_legacy'")     # every old row is gone
        return
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS forget_ids(id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM forget_ids")
    conn.executemany("INSERT OR IGNORE INTO forget_ids(id) VALUES(?)", [(m,) for m in mids])
    ids = "(SELECT id FROM forget_ids)"
    legacy = fts_legacy(conn)
    steps = [("copybook references", f"UPDATE copy_use SET resolved_member_id=NULL WHERE resolved_member_id IN {ids}"),
             ("copybook expansion runs", f"DELETE FROM expand_run WHERE src_member IN {ids}"),
             ("DB2 objects", f"DELETE FROM db2_object WHERE member_id IN {ids}")]
    for label, sql in steps:
        if progress:
            progress.now(f"removing {label}")
        conn.execute(sql)
    if progress:
        progress.now("removing search index rows")
    if legacy:
        conn.execute(f"DELETE FROM src_fts WHERE member_id IN {ids}")          # one scan, rows from before spans
    else:
        conn.executemany("DELETE FROM src_fts WHERE rowid BETWEEN ? AND ?",
                         conn.execute(f"SELECT lo, hi FROM fts_span WHERE member_id IN {ids}").fetchall())
    if progress:
        progress.now("removing members and every fact derived from them")
    conn.execute(f"DELETE FROM member WHERE id IN {ids}")
    if legacy and conn.execute("SELECT 1 FROM member LIMIT 1").fetchone() is None:
        conn.execute("DELETE FROM atlas_meta WHERE key='fts_legacy'")         # every old row is gone
    conn.execute("DELETE FROM forget_ids")
    if progress:
        progress.now(None)


def ensure_fk_indexes(conn: sqlite3.Connection, say=None) -> int:
    """An index on every foreign-key column. Without one, deleting a member
    makes SQLite scan the whole child table for rows to cascade or check -
    44 tables had none (LESSONS 147). Idempotent; a large index built by an
    older toolkit gets them once, which takes a while and is announced."""
    missing = []
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        try:
            fks = conn.execute(f"PRAGMA foreign_key_list('{t}')").fetchall()
        except sqlite3.DatabaseError:
            continue
        if not fks:
            continue
        firsts = set()
        for idx in conn.execute(f"PRAGMA index_list('{t}')").fetchall():
            cols = conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()
            if cols:
                firsts.add(cols[0][2])
        for fk in fks:
            col = fk[3]
            if col not in firsts and (t, col) not in missing:
                missing.append((t, col))
    if missing:
        big = conn.execute("SELECT COUNT(*) FROM member").fetchone()[0] > 5000
        if say and big:
            say(f"  adding {len(missing)} index(es) this index was missing (one time; a few minutes on a large index)")
        for t, col in missing:
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_fk_{t}_{col} ON {t}({col})")
        conn.commit()
    return len(missing)


# Lookups the builder and the reports make in loops: an index on the exact
# expression each one tests (UPPER(name) cannot use an index on name).
QUERY_INDEXES = [
    ("io_op", "ix_q_ioop_prog_target", "program_id, target"),
    ("member", "ix_q_member_uname", "UPPER(name)"),
    ("program", "ix_q_program_upid", "UPPER(program_id)"),
    ("program_alias", "ix_q_alias_ualias", "UPPER(alias)"),
    ("call_edge", "ix_q_call_utarget", "UPPER(target)"),
    ("copy_use", "ix_q_copy_ubook", "UPPER(copybook)"),
    ("step", "ix_q_step_upgm", "UPPER(effective_pgm)"),
    ("job", "ix_q_job_uname", "UPPER(job_name)"),
    ("transaction_def", "ix_q_tran_uprogram", "UPPER(program)"),
    ("transaction_def", "ix_q_tran_ucode", "UPPER(tran_code)"),
    ("ims_psb", "ix_q_psb_uname", "UPPER(name)"),
    ("ims_dbd", "ix_q_dbd_uname", "UPPER(name)"),
]


def ensure_query_indexes(conn: sqlite3.Connection, say=None) -> int:
    made = 0
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for table, name, expr in QUERY_INDEXES:
        if name in existing:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
        needed = {c.strip().replace("UPPER(", "").rstrip(")") for c in expr.split(",")}
        if not cols or not needed <= cols:
            continue
        if say and made == 0 and conn.execute("SELECT COUNT(*) FROM member").fetchone()[0] > 5000:
            say("  adding lookup indexes this index was missing (one time)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({expr})")
        made += 1
    if made:
        conn.commit()
    return made


# `[ \t]` not `\s`: a `\s+` at every line start ran across a whole block of blank lines (quadratic)
_IMSGEN_IN_SYSIN = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?[ \t]+(?:APPLCTN|TRANSACT)[ \t]+(?:PSB|GPSB|CODE)=", re.I | re.M)


def load_inventory_skips(path: Optional[str]) -> set:
    """Files the supervisor recorded while they were being CLASSIFIED (the
    comment carries `classifying`): skipped by the inventory itself."""
    out: set = set()
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            item, _, comment = line.partition("\t")
            item = item.split("#", 1)[0].strip()
            if item and "classifying" in comment:
                out.add(os.path.normcase(os.path.abspath(item)))
    return out


def _scan_files(root: str, limit: Optional[int] = None, on_dir=None):
    """Every file under `root`, folder by folder; `on_dir(text)` is told which
    folder is being listed, so a folder that will not list (a dead network
    path, a junction) is named on the status line, not hidden behind the
    last file read."""
    count = 0
    if on_dir:
        on_dir(f"listing folder {root}")
    for dirpath, dirs, files in os.walk(root):
        if on_dir:
            on_dir(f"listing folder {dirpath} ({len(files)} files, {len(dirs)} subfolders)")
        # Folders the toolkit itself wrote (expanded sources, extracted
        # images) carry a marker: indexing them again would create a second
        # copy of every program from its own expansion.
        if OUTPUT_MARKER in files:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        lower = {f.lower() for f in files}
        for fn in sorted(files):
            if fn.startswith(".") or fn.lower().endswith(".exp.cbl"):
                continue
            stem, ext = os.path.splitext(fn)
            # a legacy Office file whose converted copy sits beside it
            # (atlas.convert): the copy is readable, the original is not
            if ext.lower() in docs.LEGACY_TO_MODERN and (stem + docs.LEGACY_TO_MODERN[ext.lower()]).lower() in lower:
                continue
            # an archive atlas.convert already extracted into <name>.unzipped beside it
            if ext.lower() == ".zip" and os.path.isdir(os.path.join(dirpath, stem + ".unzipped")):
                continue
            yield dirpath, fn
        if on_dir:
            on_dir(f"listing the next folder after {dirpath}")
            count += 1
            if limit and count >= limit:
                return


def _load_library_markers(ctx: Ctx, roots) -> None:
    """`.atlas-library.json` written by the fetcher: what the host listed
    versus what arrived. Loaded into `library` so coverage can say
    'PROD.CLAIMS.SRC 3912/4100 INCOMPLETE' and `program X` can say NOT
    FETCHED instead of NOT FOUND."""
    conn = ctx.conn
    conn.execute("DELETE FROM library")
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            if ".atlas-library.json" not in files:
                continue
            try:
                with open(os.path.join(dirpath, ".atlas-library.json"), "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            conn.execute("INSERT INTO library(dataset,folder,fetched_at,rc,expected,present,complete,missing,stale) "
                         "VALUES(?,?,?,?,?,?,?,?,?)",
                         (rec.get("dataset"), os.path.normpath(dirpath), rec.get("fetched_at"), rec.get("rc"),
                          rec.get("expected"), rec.get("present"), int(bool(rec.get("complete"))),
                          _j(rec.get("missing")), _j(rec.get("stale"))))
            if not rec.get("complete"):
                ctx.say(f"  INCOMPLETE library {rec.get('dataset')}: {rec.get('present')}/{rec.get('expected')} members")


def _settled(status: Optional[str], error: Optional[str]) -> bool:
    """A member whose last outcome stands until its bytes or the parser
    change: parsed (ok / partial / skipped), or given up on with a reason."""
    return status in ("ok", "partial", "skipped") or (
        status == "failed" and (error or "").startswith(("MemberTimeout", "ParserStuck", "InventoryTimeout")))


def moved_names(existing: Dict[str, tuple], found: Sequence[tuple], kinds: Sequence[str] = COPY_KINDS,
                again: bool = True) -> Set[str]:
    """The names whose COPY statements may resolve differently after this
    inventory: every member of a kind the resolver expands (RESOLVER_KINDS),
    or a stub of the name (COPY_KINDS: the resolver never expands one, but
    the program's note names it in place of NOT FOUND - ROADMAP re-parse
    item 23), that is new, whose bytes changed, that is recorded again under
    a new id although its bytes did not change (its last outcome did not
    settle - a build stopped before it was parsed, a parser exception), or
    that went from disk - taken under its kind in the last build AND in this
    one, so a member re-typed into or out of those kinds counts too.

    With `kinds` = JOB_READ_KINDS and `again` False: the names of the members
    a job's expansion reads (a PROC, an INCLUDE, a card member) that arrived,
    changed, went or were re-typed - one of them makes inventory() parse
    every job and PROC again. A member recorded again with the same bytes is
    left out there: a job keeps the names of what it read, never its id, so
    its facts do not change. Before this, only a new or changed member filed
    proc, jcl or ctlcard forced the jobs: a PROC that went from disk left its
    jobs with the steps and datasets of the PROC it no longer has, and a card
    member arriving 'unknown' or 'sql' left them without its cards.

    Before ROADMAP re-parse item 21 only a new or changed member filed
    copybook or cobol counted: a copybook arriving 'unknown' (a dataset-named
    folder with no COPY hint) forced nothing, and every program that copied
    it stayed 'partial - COPY X NOT FOUND' until atlas.recover marked it
    (LESSONS 183); a copybook that went, whose new text was filed as another
    kind, or that was recorded again under a new id, left its programs 'ok'
    with a copy_use row _forget_member had set to NULL and the fields of the
    earlier read (LESSONS 184, 188). `existing`: path -> the stored row as
    inventory() reads it (id, sha256, parse_status, parse_error, kind, ...,
    name last); `found`: inventory()'s tuples (path, name, kind, library,
    ext, sha256, ...). Linear in the members."""
    names: Set[str] = set()
    here: Set[str] = set()
    for f in found:
        path, name, kind, sha_ = f[0], f[1], f[2], f[5]
        here.add(path)
        ex = existing.get(path)
        if ex is not None and ex[1] == sha_ and (not again or _settled(ex[2], ex[3])):
            continue                          # kept: the same bytes, the same kind, the same id
        if kind in kinds:
            names.add(str(name).upper())
        if ex is not None and ex[4] in kinds:
            names.add(str(ex[-1] or name).upper())
    for path, ex in existing.items():
        if path not in here and ex[4] in kinds:
            names.add(str(ex[-1] or os.path.splitext(os.path.basename(path))[0]).upper())    # gone from disk
    return names


def copiers_to_parse(conn: sqlite3.Connection, names: AbstractSet[str]) -> Set[str]:
    """The paths of every member whose COPY statements name one of `names`
    (a program carries its own row for every nested COPY, so a program
    reaches the name however deep it sits), and - each of those being
    recorded again under a new id, which un-links every row that resolved to
    it (_forget_member) - of every member copying one of THEIR names, until
    nothing new turns up. Each name is asked once, 500 to a query: linear in
    the names, however long the chain."""
    forced: Set[str] = set()
    pending = {n.upper() for n in names}
    asked: Set[str] = set()
    while pending:
        batch = sorted(pending)
        asked |= pending
        pending = set()
        for k in range(0, len(batch), 500):
            chunk = batch[k:k + 500]
            q = ",".join("?" * len(chunk))
            for path, name, kind in conn.execute(f"SELECT DISTINCT m.path, m.name, m.kind FROM copy_use c "
                                                 f"JOIN member m ON m.id=c.member_id WHERE UPPER(c.copybook) IN ({q})",
                                                 tuple(chunk)):
                forced.add(path)
                n = str(name or "").upper()
                if kind in RESOLVER_KINDS and n not in asked:
                    pending.add(n)
    return forced


def program_names_moved(existing: Dict[str, tuple], found: Sequence[tuple]) -> Set[str]:
    """The names whose PROGRAM members changed: a member filed cobol that
    arrived or went, or that was re-typed into or out of cobol. Which
    systems hold a program of a name decides which compiler listings speak
    for each of them (twin_systems, rows_that_count - ROADMAP re-parse item
    19): while one system holds the name, every current listing of it
    speaks, wherever it is filed; once another holds one too, only the
    listings in the program's own system do. So such a change may change
    what a COPY expands in the OTHER programs of the name, which copy
    nothing that moved - twins_to_parse finds them. A program member whose
    bytes changed, or that is recorded again, leaves every system holding
    what it held: not counted. `existing` / `found` as for moved_names."""
    names: Set[str] = set()
    here: Set[str] = set()
    for f in found:
        path, name, kind = f[0], f[1], f[2]
        here.add(path)
        ex = existing.get(path)
        if (kind == "cobol") != (ex is not None and ex[4] == "cobol"):
            names.add(str(ex[-1] if ex is not None and ex[-1] else name).upper())
    for path, ex in existing.items():
        if path not in here and ex[4] == "cobol":
            names.add(str(ex[-1] or os.path.splitext(os.path.basename(path))[0]).upper())    # gone from disk
    return names


def twins_to_parse(conn: sqlite3.Connection, names: AbstractSet[str]) -> Set[str]:
    """The paths of the program members (kind cobol) named one of `names`
    (program_names_moved) that a CURRENT compiler listing speaks of - a row
    of atlas.recover's listing_copy_source for the name that names a
    copybook and a dataset and is dated current, the only rows
    current_datasets follows. Without such a row, which systems hold the
    name decides nothing and nothing is parsed again. Before this, a
    program of the name arriving in GC-TEST, or going from it, left GC's
    program with the copy a listing filed outside GC had named (or without
    it) until a full re-parse (LESSONS 203). One query per 500 names; a
    program copied by other members is parsed again with the rest of them,
    since its name is one moved_names takes too."""
    if not names or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_copy_source'"
                                 ).fetchone() is None:
        return set()
    if "current" not in {str(r[1]) for r in conn.execute("PRAGMA table_info(listing_copy_source)")}:
        return set()                                                    # written before recover dated listings
    forced: Set[str] = set()
    batch = sorted({n.upper() for n in names})
    for k in range(0, len(batch), 500):
        chunk = tuple(batch[k:k + 500])
        q = ",".join("?" * len(chunk))
        for (path,) in conn.execute(
                f"SELECT m.path FROM member m WHERE m.kind = 'cobol' AND UPPER(m.name) IN ({q}) "
                f"AND UPPER(m.name) IN (SELECT UPPER(TRIM(s.program)) FROM listing_copy_source s "
                f"WHERE UPPER(TRIM(s.program)) IN ({q}) AND s.copybook IS NOT NULL AND s.copybook <> '' "
                f"AND s.dataset IS NOT NULL AND s.dataset <> '' AND s.current)", chunk + chunk):
            forced.add(path)
    return forced


def _inventory_one(ctx: Ctx, path: str, fn: str, dirpath: str, data: bytes) -> tuple:
    """Classify and fingerprint one file. Pure: no database, so it can run
    on a worker thread under a time limit."""
    ext = os.path.splitext(fn)[1].lower()
    if ext in classify.BINARY_EXTS or ext in docs.LEGACY or ext in (".pdf", ".docx", ".xlsx", ".pptx", ".vsdx"):
        kind, _why = classify.classify(path, "", None)
        if kind == "binary":
            kind = "doc"
        norm, nlines, fixed = sha(data), 0, 0
    elif len(data) > BIG_TEXT_BYTES:
        # a 40 MB text export is a document (or a dump), never a member:
        # decoding and line-splitting it as COBOL is where a build sits for hours
        text, enc = reader.decode_bytes(data[:8192])
        kind, _why = classify.classify(path, text)
        if kind in CODE_KINDS or kind == "unknown":
            kind = "doc" if kind in ("unknown", "listing") or ext in (".txt", ".md", ".html", ".htm", ".csv", ".xml") else "listing"
        norm, nlines, fixed = sha(data), data.count(b"\n"), 0
    else:
        text, enc = reader.decode_bytes(data)
        # the kind the user declared for that library in sources.json (the
        # manifest kinds) replaces 'unknown', and - unless it is 'cobol' - a
        # shape (asm / listing / mfs), the folder name and the extension;
        # a strong signature and a COBOL copybook's own lines win over it
        # (classify.declared_wins, ROADMAP re-parse item 22)
        kind, _why, over, at = classify.decide(path, text[:8192], ctx.kind_of.get(os.path.basename(dirpath).upper()))
        if over and kind == "copybook":
            ctx.shape_over[path] = (over, at)
        norm, nlines, fixed = norm_hash(kind, text, data, enc)
        if kind in RESOLVER_KINDS and reader.stub_count(text, data, enc):
            # only numbers - a 7-digit one in column 1 or an 8-digit one reaching column 8: no text a compiler could
            # compile, so never COBOL to expand (ROADMAP re-parse item 23; before, the 7-digit stub was filed empty
            # and the 8-digit one a copybook whose expansion erased the copying program's procedure division)
            kind = STUB_KIND
        elif kind in CODE_KINDS and code_line_count(kind, text, data, enc) == 0:
            kind = "empty"           # comments and blanks only, or a retired member: never a program row
    return (path, os.path.splitext(fn)[0].upper(), kind, os.path.basename(dirpath), ext,
            sha(data), norm, len(data), nlines, fixed, None)


def inventory(ctx: Ctx, roots, limit: Optional[int] = None, force_all: bool = False) -> None:
    """Hash and classify every file under each root; re-index only what changed.

    `roots` is the estate folder plus any extra folders (`--also`): the
    documentation folder is usually NOT a mainframe dataset and lives elsewhere.

    Incremental by default: a member whose bytes are unchanged keeps its facts.
    A member of a kind the resolver expands (copybook, cobol, sql, unknown)
    that arrives, changes, goes or is re-typed forces every program that
    copies its name (and every copybook that copies it) to be re-parsed
    (moved_names, copiers_to_parse); a program member that arrives, goes or
    is re-typed changes which compiler listings speak for the other programs
    of its name, and forces those a current listing speaks of
    (program_names_moved, twins_to_parse); a member of a kind a job reads (a PROC,
    an INCLUDE, a card member: JOB_READ_KINDS) that arrives, changes, goes
    or is re-typed forces every job and PROC. `force_all` (parser or manifest
    changed) re-parses everything. Members that vanished are pruned.
    `--rebuild` starts from an empty db.

    Three steps, each announced on the status line: reading every file
    (a line per finished folder), comparing with the last build (and
    removing the old facts of what changed), recording the members.
    """
    conn = ctx.conn
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing = {r[0]: tuple(r[1:]) for r in
                conn.execute("SELECT path, id, sha256, parse_status, parse_error, kind, library, ext, norm_sha, "
                             "lines, fixed_format, name FROM member")}
    if isinstance(roots, str):
        roots = [roots]
    own = ctx.progress is None
    progress = ctx.progress or Progress(ctx.say).start()
    ctx.progress = progress
    skip_classify = getattr(ctx, "inventory_skips", set())

    found: List[tuple] = []
    # members whose stored classification stands (same bytes, settled): not classified again, so ctx.shape_over has
    # nothing for them - a forced re-parse re-inserts them with their stored kind and their 'declared_kind' row
    stored: set = set()
    progress.phase("inventory: reading and fingerprinting every file", limit=INVENTORY_TIME_LIMIT)
    ctx.say(f"  a line every {int(PROGRESS_SECONDS)} s says how far and which file is in hand; a line when a folder "
            f"is done; a file still in hand after {SLOW_MEMBER_SECONDS} s is flagged, and given up on after "
            f"{_hms(INVENTORY_TIME_LIMIT)} (a document folder on OneDrive is downloaded now - minutes of disk work)")
    t_start = time.time()
    n_read = bytes_read = 0
    box: List[tuple] = []
    progress.set(lambda: f"{n_read:,} files read ({bytes_read // 1024 // 1024:,} MB) in {_hms(time.time() - t_start)}")
    folder = {"dir": None, "path": None, "files": 0, "bytes": 0, "t0": time.time(), "small": 0, "small_files": 0}

    def folder_done() -> None:
        if folder["path"] is None:
            return
        secs = time.time() - folder["t0"]
        if folder["files"] >= FOLDER_REPORT_FILES or folder["bytes"] >= FOLDER_REPORT_BYTES or secs >= FOLDER_REPORT_SECONDS:
            extra = (f" (and {folder['small']} small folder(s) with {folder['small_files']} file(s) before it)"
                     if folder["small"] else "")
            ctx.say(f"{_stamp()}  folder done: {folder['path']} - {folder['files']:,} file(s), "
                    f"{_size(folder['bytes'])}, {_hms(secs)}{extra}")
            folder["small"] = folder["small_files"] = 0
        elif folder["files"]:
            folder["small"] += 1
            folder["small_files"] += folder["files"]

    try:
        for root in roots:
            root_label = os.path.basename(os.path.normpath(root)) or root
            for dirpath, fn in _scan_files(root, limit, progress.now):
                if dirpath != folder["dir"]:
                    folder_done()
                    rel = os.path.relpath(dirpath, root)
                    folder.update(dir=dirpath, path=root_label if rel == "." else os.path.join(root_label, rel),
                                  files=0, bytes=0, t0=time.time())
                path = os.path.normpath(os.path.join(dirpath, fn))
                n_read += 1
                fold = os.path.basename(dirpath) or dirpath
                progress.now(f"reading {fn} in {fold}")
                try:
                    st = os.stat(docs.long_path(path))
                    progress.now(f"reading {fn} ({_size(st.st_size)}) in {fold}")
                    if st.st_size > MAX_MEMBER_BYTES:
                        ctx.bump("too_large")
                        ctx.problem("too large to index", path, f"{st.st_size // 1024 // 1024} MB (limit "
                                    f"{MAX_MEMBER_BYTES // 1024 // 1024} MB)",
                                    f"{_stamp()}  skipped, too large to index: {path} ({st.st_size // 1024 // 1024} MB)")
                        continue
                    with open(docs.long_path(path), "rb") as fh:
                        data = fh.read()
                    bytes_read += len(data)
                    folder["files"] += 1
                    folder["bytes"] += len(data)
                except OSError as e:
                    ctx.bump("unreadable")
                    hint = ""
                    try:
                        attrs = getattr(os.stat(docs.long_path(path)), "st_file_attributes", 0)
                        if attrs & 0x400000 or attrs & 0x1000:
                            hint = " - a OneDrive placeholder not downloaded to this laptop: right-click the folder > Always keep on this device"
                    except OSError:
                        pass
                    ctx.problem("unreadable", path, f"{e}{hint}", f"{_stamp()}  unreadable: {path} ({e}){hint}")
                    continue
                sha_ = sha(data)
                ex = existing.get(path)
                if ex and ex[1] == sha_ and not force_all and _settled(ex[2], ex[3]):
                    # same bytes, same parser, settled outcome: the stored classification
                    # stands - no decode, no classify, no line split (and a file the
                    # inventory once gave up on is not read again for another limit)
                    found.append((path, os.path.splitext(fn)[0].upper(), ex[4], ex[5], ex[6], sha_, ex[7], len(data),
                                  ex[8] or 0, ex[9] or 0, None))
                    stored.add(path)
                    continue
                label = f"file {fn} ({len(data) // 1024} KB) in {fold}"
                if os.path.normcase(os.path.abspath(path)) in skip_classify:
                    ctx.bump("too_slow")
                    ctx.problem("skipped: froze the classifier on an earlier run", path, "skip list",
                                f"{_stamp()}  SKIPPED {label}: froze the classifier on an earlier run (skip list)")
                    found.append((path, os.path.splitext(fn)[0].upper(), "unknown", os.path.basename(dirpath),
                                  os.path.splitext(fn)[1].lower(), sha_, sha_, len(data), 0, 0,
                                  "InventoryTimeout: froze the classifier on an earlier run - skipped (skip list); "
                                  "report its extension, size and what it is"))
                    continue
                progress.now(f"classifying {fn} ({_size(len(data))}) in {fold}", path=path)
                try:
                    box.clear()
                    run_with_limit(lambda: box.append(_inventory_one(ctx, path, fn, dirpath, data)), (),
                                   INVENTORY_TIME_LIMIT, label)
                    found.append(box[0])
                except ParserStuck:
                    ctx.say(f"\nSTOPPED: {label} has been read for {_hms(INVENTORY_TIME_LIMIT)} and cannot be interrupted - "
                            f"the classifier is stuck inside it. Move that one file out of the folder and run the same build "
                            f"command again; then report its extension, size and what it is so the classifier can be fixed.")
                    raise
                except MemberTimeout as e:
                    # recorded, not indexed: the name is what matters
                    ctx.bump("too_slow")
                    ctx.problem("classifier time limit", path, str(e), f"{_stamp()}  gave up on {label}: {e}")
                    found.append((path, os.path.splitext(fn)[0].upper(), "unknown", os.path.basename(dirpath),
                                  os.path.splitext(fn)[1].lower(), sha(data), sha(data), len(data), 0, 0,
                                  f"InventoryTimeout: reading and classifying took longer than {INVENTORY_TIME_LIMIT} s - "
                                  f"not indexed; move the file out of the folder, or report its extension, size and what it is"))
        folder_done()
        if folder["small"]:
            ctx.say(f"{_stamp()}  and {folder['small']} small folder(s) with {folder['small_files']} file(s)")
        ctx.say(f"{_stamp()}  inventory done: {n_read:,} files, {bytes_read // 1024 // 1024:,} MB, "
                f"{_hms(time.time() - t_start)}")

        progress.phase("comparing with the last build")
        found_by = {f[0]: f for f in found}
        _load_library_markers(ctx, roots)

        forced: set = set()
        moved = moved_names(existing, found) if existing and not force_all else set()
        if moved:
            # every program whose COPY may now resolve differently - a member of a kind the resolver expands arrived,
            # changed, went, is recorded again or was re-typed (ROADMAP re-parse item 21) - and, transitively, the
            # members copying what is recorded again: an incremental build gives what a full one would
            progress.now(f"finding the programs to parse again: {len(moved):,} name(s) of members that arrived, changed, "
                         f"went or were re-typed since the last build")
            forced |= copiers_to_parse(conn, moved)
        twins = program_names_moved(existing, found) if existing and not force_all else set()
        if twins:
            # a program of a name arriving in another system, going from it or re-typed changes which listings speak
            # for the other programs of the name (item 19's twin rule): those a current listing speaks of are parsed
            # again too - they copy nothing that moved (LESSONS 203)
            forced |= twins_to_parse(conn, twins)
        if existing and not force_all and moved_names(existing, found, JOB_READ_KINDS, again=False):
            # a PROC, INCLUDE or card member arrived, changed, went or was re-typed (a card member may be filed
            # unknown or sql): every job that expands it carries its facts, and a job keeps only the names it looked
            # for - re-parse all JCL (cheap next to COBOL). Before ROADMAP re-parse item 21 only a new or changed
            # member filed proc, jcl or ctlcard did: a PROC gone from disk left its jobs with its steps and datasets
            forced |= {r[0] for r in conn.execute("SELECT path FROM member WHERE kind IN ('jcl','proc')")}
        if force_all:
            forced |= set(existing)

        kept: Dict[str, int] = {}
        to_forget: List[int] = []
        for path, (mid, ex_sha, ex_status, ex_error, *_rest) in existing.items():
            f = found_by.get(path)
            settled = _settled(ex_status, ex_error)     # a member that hit a limit is not retried until the parser changes
            if f is None:
                to_forget.append(mid)
                ctx.bump("pruned")
            elif f[5] == ex_sha and settled and path not in forced:
                kept[path] = mid
            else:
                to_forget.append(mid)
                ctx.bump("changed")
        ctx.say(f"{_stamp()}  {len(kept):,} member(s) unchanged and kept, {len(to_forget):,} to redo "
                f"({ctx.stats.get('pruned', 0):,} gone from disk), {len(found) - len(kept) - ctx.stats.get('changed', 0):,} new")
        # a member re-parsed because a member it copies changed keeps its stored classification (the same bytes, the
        # same toolkit and declared kinds: a change of either re-parses every member and classifies it again), so
        # the 'declared_kind' row its classification wrote is carried over, not lost with its old facts (LESSONS 200)
        carried: Dict[str, List[tuple]] = {}
        again = {existing[p][0]: p for p in stored if p not in kept}
        if again:
            for mid, detail, line in conn.execute("SELECT member_id, detail, line FROM unresolved WHERE kind='declared_kind'"):
                if mid in again:
                    carried.setdefault(again[mid], []).append((detail, line))
        if to_forget:
            progress.phase(f"removing the old facts of {len(to_forget):,} member(s)")
            _forget_members(conn, to_forget, progress)
            conn.commit()

        progress.phase(f"recording {len(found):,} member(s)")
        k = 0
        progress.set(lambda: f"{k:,}/{len(found):,} recorded")
        for f in found:
            path, name, kind, library, ext, sha_, norm, nbytes, nlines, fixed, note = f
            if path in kept:
                mem = Mem(kept[path], path, name, kind, library, norm, skip=True, nbytes=nbytes)
                ctx.bump("unchanged")
            else:
                cur = conn.execute(
                    "INSERT INTO member(path,name,kind,library,ext,sha256,norm_sha,bytes,lines,"
                    "fixed_format,parse_status,parse_error,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, name, kind, library, ext, sha_, norm, nbytes, nlines, fixed,
                     "failed" if note else "pending", note, now))
                mem = Mem(cur.lastrowid, path, name, kind, library, norm, skip=bool(note), nbytes=nbytes)
                if path not in existing:
                    ctx.bump("new")
                over = ctx.shape_over.get(path) if kind == "copybook" else None
                if over:
                    conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                                 (mem.id, "declared_kind", declared_note(library, *over), over[1] or None))
                elif kind == "copybook":
                    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                                     [(mem.id, "declared_kind", detail, line) for detail, line in carried.get(path, ())])
            ctx.members.append(mem)
            ctx.by_name.setdefault(name, []).append(mem)
            ctx.bump(f"kind:{kind}")
            k += 1
    finally:
        if own:
            progress.stop()
            ctx.progress = None


def _parse_one(ctx: Ctx, mem: Mem, handler) -> None:
    if _FAULT and _FAULT.startswith("diskfull:") and os.path.basename(mem.path) == _FAULT[9:]:
        raise sqlite3.OperationalError("database or disk is full")          # tests only
    if _FREEZE and os.path.basename(mem.path) == _FREEZE:
        if ctx.progress:                 # what a regular expression gone wrong looks like from outside:
            ctx.progress.stop()          # no status line, no limit, nothing - until the supervisor acts
        time.sleep(3600)
    if handler:
        handler(ctx, mem)
    if mem.kind in CODE_KINDS:
        index_fts_code(ctx, mem)


def load_skip_list(path: Optional[str]) -> Tuple[set, set]:
    """Members that froze the parser on an earlier run (atlas.supervise writes
    them): full paths, or bare file names. (paths, names)"""
    paths: set = set()
    names: set = set()
    if not path or not os.path.isfile(path):
        return paths, names
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            item = line.split("\t", 1)[0].split("#", 1)[0].strip()
            if not item:
                continue
            if os.sep in item or "/" in item:
                paths.add(os.path.normcase(os.path.abspath(item)))
            else:
                names.add(item.upper())
    return paths, names


def load_sched(ctx: Ctx, csv_path: str) -> None:
    """Scheduler export (CA-7 / Control-M / TWS) as CSV.

    Columns (header row required, any order, extra columns ignored):
      job_name, depends_on, kind          -> one predecessor/trigger edge per row
      job_name, system, schedule, calendar -> one job definition per row
    A row with a non-empty depends_on is an edge; otherwise a definition.
    The scheduler is where the real batch flow lives; without it "is this job
    dead" and "what runs before this" cannot be answered.
    """
    import csv
    n_dep = n_job = 0
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        head = fh.readline()
        fh.seek(0)
        cols = {c.strip().lower() for c in head.split(",")}
        if not cols & {"job_name", "job", "jobname"}:
            # Not the CSV form this loader understands (a Zeke / CA-7 / Control-M
            # native export, most likely). Say so where it will be seen: the
            # coverage report reads `unresolved`, and a silent "0 loaded" would
            # look exactly like "no dependencies exist".
            msg = (f"scheduler file {os.path.basename(csv_path)} is not a CSV with a job_name column "
                   f"(first line: {head.strip()[:80]!r}). Its native format needs a loader - send its "
                   f"SHAPE (20 lines, fake job names) per REPORTING.md, or export it as job_name,depends_on.")
            ctx.conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(NULL,'scheduler_format',?,NULL)",
                             (msg,))
            ctx.conn.commit()
            ctx.say("scheduler: " + msg)
            return
        for row in csv.DictReader(fh):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            job = r.get("job_name") or r.get("job") or r.get("jobname")
            if not job:
                continue
            if r.get("depends_on") or r.get("predecessor"):
                ctx.conn.execute("INSERT INTO sched_dep(job_name,depends_on,kind) VALUES(?,?,?)",
                                 (job.upper(), (r.get("depends_on") or r.get("predecessor")).upper(),
                                  r.get("kind") or "predecessor"))
                n_dep += 1
            else:
                ctx.conn.execute("INSERT INTO sched_job(job_name,system,schedule,calendar,source) VALUES(?,?,?,?,?)",
                                 (job.upper(), r.get("system"), r.get("schedule"), r.get("calendar"),
                                  os.path.basename(csv_path)))
                n_job += 1
    ctx.conn.commit()
    ctx.say(f"scheduler: {n_job} job definition(s), {n_dep} dependency edge(s) loaded")


SHAPE_OF = {"asm": "an Assembler member", "listing": "a compiler listing", "mfs": "MFS source"}


def declared_note(library: str, over: str, line: int) -> str:
    """The 'declared_kind' row of a member the kind declared for its library
    typed copybook over the shape of an Assembler, listing or MFS member."""
    return (f"filed copybook by the kind declared for library {library} in the UI's table, over the shape of "
            f"{SHAPE_OF.get(over, over)}" + (f" on line {line}" if line else "") + ": every program copying it expands this "
            "text - if it is not the COBOL copybook they copy, correct the library's kind in the UI's table and run the build")


def load_declared_kinds(ctx: Ctx, manifest_path: Optional[str]) -> None:
    """`kinds` from the manifest (written from the sources table): library
    folder name -> kind. Read BEFORE the inventory, because that is where a
    member gets typed: the declared kind replaces 'unknown' and - unless it
    is 'cobol' - an Assembler / listing / MFS shape, the folder name and the
    extension (classify.declared_wins)."""
    ctx.kind_of = {}
    if not manifest_path or not os.path.isfile(manifest_path):
        return
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        return
    ctx.kind_of = {str(k).upper(): str(v).lower() for k, v in (man.get("kinds") or {}).items()
                   if str(v).lower() in classify.DECLARABLE}


def apply_manifest(ctx: Ctx, manifest_path: str) -> None:
    """manifest.json (generated by fetch.write_manifest, or hand-written):
      authoritative : folders / library names holding PRODUCTION copies
      systems       : {"CLAIMS": ["C:/estate/CLAIMS/PROD.CLAIMS.SRC", ...]}   folder -> department
      system_of     : {"PROD.CLAIMS.SRC": "CLAIMS"}                            library name -> department
      copylib_order : {"CLAIMS": [copybook folders in SYSLIB order]}         which copy wins
    The manifest is the whole truth for these: flags are reset before applying."""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        man = json.load(fh)
    prefixes = [p.replace("\\", "/").upper() for p in man.get("authoritative", [])]
    system_of = {k.upper(): str(v).upper() for k, v in (man.get("system_of") or {}).items()}
    sys_paths = [(p.replace("\\", "/").upper().rstrip("/"), str(s).upper())
                 for s, lst in (man.get("systems") or {}).items() for p in lst]
    ctx.copylib_order = {str(s).upper(): [os.path.basename(p.replace("\\", "/").rstrip("/")).upper() for p in lst]
                         for s, lst in (man.get("copylib_order") or {}).items()}
    ctx.conn.execute("UPDATE member SET authoritative=0, system=NULL")
    n_auth = n_sys = 0
    auth_by_dir: Dict[Tuple[str, str], bool] = {}           # members x libraries was quadratic: decide per folder
    for m in ctx.members:
        p = m.path.replace("\\", "/").upper()
        folder_up, _, _fname = p.rpartition("/")
        dkey = (folder_up, m.library.upper())
        if dkey not in auth_by_dir:
            # a declared prefix is a folder or a library name: it cannot split one folder's files
            auth_by_dir[dkey] = any((folder_up + "/").startswith(x.rstrip("/") + "/") or folder_up == x.rstrip("/")
                                    or f"/{x}/" in (folder_up + "/") or dkey[1] == x for x in prefixes)
        if auth_by_dir[dkey]:
            m.authoritative = 1
            ctx.conn.execute("UPDATE member SET authoritative=1 WHERE id=?", (m.id,))
            n_auth += 1
        sysname = next((s for sp, s in sys_paths if p == sp or p.startswith(sp + "/")), None)
        if not sysname:
            sysname = system_of.get(m.library.upper())
        if not sysname:
            sysname = next((v for k, v in system_of.items() if f"/{k}/" in p), None)
        if sysname:
            m.system = sysname
            ctx.conn.execute("UPDATE member SET system=? WHERE id=?", (sysname, m.id))
            n_sys += 1
    # What crosses the mainframe boundary is declared, not derivable:
    #   "external_interfaces": [{"kind":"ndm","peer":"REINSURER-X","direction":"out","dataset":"PROD.POLICY.EXTRACT"},
    #                           {"kind":"ddf","peer":"CLAIMS-WEB","direction":"in","table":"PRD.POLICY_TBL"}]
    ctx.conn.execute("DELETE FROM external_interface")
    n_ext = 0
    for e in man.get("external_interfaces") or []:
        if not isinstance(e, dict):
            continue
        tk = next((k for k in ("dataset", "queue", "table", "transaction", "program", "path") if e.get(k)), None)
        if not tk:
            continue
        ctx.conn.execute("INSERT INTO external_interface(kind,peer,direction,target_kind,target,note) VALUES(?,?,?,?,?,?)",
                         ((e.get("kind") or "other").lower(), e.get("peer"), (e.get("direction") or "").lower() or None,
                          tk, str(e[tk]).upper() if tk != "path" else str(e[tk]), e.get("note")))
        n_ext += 1
    ctx.say(f"manifest: {n_auth} authoritative, {n_sys} assigned to a system, "
            f"copybook order declared for {len(ctx.copylib_order)} system(s), {n_ext} external interface(s)")


def derive_systems(ctx: Ctx, root: str) -> int:
    """`estate\\<SYSTEM>\\<LIBRARY>\\member`: the folder between the estate root
    and the library folder names the system (GC, SHARED, GC-TEST) for every
    member the manifest did not assign. Two environments of one system go in
    two folders - `GC` for production, `GC-TEST` for the release under test:
    copybook resolution then keeps each program with its own environment's
    copybooks, and `diff GC/PGM GC-TEST/PGM` compares the two."""
    root_n = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    n = 0
    for m in ctx.members:
        if m.system:
            continue
        p = os.path.abspath(m.path)
        if not os.path.normcase(p).startswith(root_n + os.sep):
            continue
        parts = p[len(root_n) + 1:].split(os.sep)
        if len(parts) < 3:
            continue
        m.system = parts[0].upper()
        ctx.conn.execute("UPDATE member SET system=? WHERE id=?", (m.system, m.id))
        n += 1
    if n:
        ctx.say(f"  {n} member(s) assigned to a system from the folder layout (estate\\SYSTEM\\LIBRARY)")
    return n


# --------------------------------------------------------------------------
# copybook resolution
# --------------------------------------------------------------------------

def _pick_member(ctx: Ctx, cands: List[Mem], name: str, job_mem: Optional[Mem] = None,
                 jcllib: Sequence[str] = ()) -> Tuple[Optional[Mem], Optional[str]]:
    """Choose among same-named members the way the system would: a library
    named in the job's JCLLIB ORDER first, then the job's own department
    (member.system), then the manifest's authoritative copy. Two departments
    each owning a PROC called NIGHTLY is normal; picking the wrong one
    credits one department's datasets to the other's job. When nothing
    separates two DIFFERENT copies, the choice is reported, not silent."""
    if not cands:
        return None, None
    order = [x.upper() for x in jcllib]

    def score(m: Mem) -> Tuple[int, int, int]:
        lib = (m.library or "").upper()
        return (order.index(lib) if lib in order else len(order),
                0 if (job_mem is not None and m.system and m.system == job_mem.system) else 1,
                0 if m.authoritative else 1)

    cands = sorted(cands, key=score)
    best = cands[0]
    note = None
    if len(cands) > 1 and score(cands[1]) == score(best) and cands[1].norm_sha != best.norm_sha:
        note = (f"{name.upper()}: {len(cands)} different copies and no JCLLIB / department / manifest rule "
                f"picks one - used {best.path}")
    return best, note


def _stub_read_as(ctx: Ctx, m: Mem) -> str:
    """The kind the classifier gives a member filed `stub` - the kind the
    build filed it before ROADMAP re-parse item 23 (a stub is one of
    RESOLVER_KINDS holding only numbers): 'unknown' for a date or a count
    card in a library with no hint, 'sql', or a copybook / program by its
    folder or extension. Read again as _inventory_one read it (the same
    bytes, the same kinds declared for the libraries: a change of either
    re-parses every job), once per build; 'copybook' when the file cannot
    be read now, which no job reads."""
    cache = ctx.stub_kinds
    if m.id not in cache:
        try:
            text, _data, _enc = reader.load(m.path)
            cache[m.id] = classify.decide(m.path, text[:8192], ctx.kind_of.get((m.library or "").upper()))[0]
        except OSError:
            cache[m.id] = "copybook"
    return cache[m.id]


def _member_text(ctx: Ctx, name: str, kinds: Tuple[str, ...], job_mem: Optional[Mem] = None,
                 jcllib: Sequence[str] = ()) -> Optional[str]:
    # a stub (only numbers - ROADMAP re-parse item 23) is a candidate where the member its classifier made of it was
    # one before the item: a card read as 'unknown' or 'sql' on equal terms with the other members of the name - its
    # own department's card comes first by _pick_member, as before - and a copybook's or a program's stub (`empty` or
    # a copybook then) never, so it takes no card member's place (LESSONS 205)
    cands = [m for m in ctx.by_name.get(name.upper(), [])
             if m.kind in kinds and (m.kind != STUB_KIND or _stub_read_as(ctx, m) in kinds)]
    m, _note = _pick_member(ctx, cands, name, job_mem, jcllib)
    if m is None:
        return None
    text, _d, _e = reader.load(m.path)
    return text


def _include_text(ctx: Ctx, name: str, job_mem: Optional[Mem] = None) -> Optional[str]:
    """`// INCLUDE MEMBER=X`: the member's records, spliced in by parse_jcl."""
    return _member_text(ctx, name, INCLUDE_KINDS, job_mem)


def _card_text(ctx: Ctx, name: str, job_mem: Optional[Mem] = None) -> Optional[str]:
    """`//SYSIN DD DSN=PROD.PARMLIB(SRTCLM)`: the card member's text. Never a
    COBOL/copybook member of the same name - PARMLIB(CLMRPT2) is cards for
    CLMRPT2, not the program."""
    return _member_text(ctx, name, CARD_KINDS, job_mem)


def _parsed_proc(ctx: Ctx, m: Mem) -> Optional[jcl.JclFacts]:
    """Parsed PROC member (cached per member): None when the member is a job."""
    if m.id not in ctx.proc_cache:
        text, data, enc = reader.load(m.path)
        f = jcl.parse_jcl(text, data, enc, include_lookup=lambda n: _include_text(ctx, n, m),
                          member_lookup=lambda n: _card_text(ctx, n, m))
        ctx.proc_cache[m.id] = f if (f.is_proc or m.kind == "proc") else None
    return ctx.proc_cache[m.id]


def _proc_facts(ctx: Ctx, name: str, job_mem: Optional[Mem] = None,
                job: Optional[jcl.JclFacts] = None) -> Optional[jcl.JclFacts]:
    """Cataloged PROC by name for expand_job, chosen by the calling job's
    JCLLIB ORDER and department; an undecidable choice is recorded on the
    job as `ambiguous_proc`."""
    cands = [m for m in ctx.by_name.get(name.upper(), []) if m.kind in PROC_KINDS
             and _parsed_proc(ctx, m) is not None]
    m, note = _pick_member(ctx, cands, name, job_mem, job.jcllib if job else ())
    if m is None:
        return None
    if note and job is not None and ("ambiguous_proc", note, 0) not in job.unresolved:
        job.unresolved.append(("ambiguous_proc", note, 0))
    return _parsed_proc(ctx, m)


# A library dataset's shape: dotted qualifiers of 1-8 characters, 44 at most
# (the same shape atlas.recover reads a listing's copybook-source row by).
_LIB_DSN = re.compile(r"^[A-Z0-9@#$][A-Z0-9@#$\-]{0,7}(?:\.[A-Z0-9@#$][A-Z0-9@#$\-]{0,7})+$")
LIB_DSN_MAX = 44
LISTING_HOW = "the program's compiler listing names "          # the 'how' of a choice the listing decided
# the 'how' of the chain's pick kept because the copy the listing names has the same text:
# f"{LISTING_SAME}{DATASET})" - 'confirmed by the program's listing (same text as DATASET)'
LISTING_SAME = "confirmed by the program's listing (same text as "


def _folder_key(folder: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(folder)))


# one row of what a compiler listing says: (library dataset, the listing member's system, the listing current?)
ListingRow = Tuple[str, Optional[str], Optional[bool]]


def load_listing_sources(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Tuple[ListingRow, ...]]:
    """{(PROGRAM, COPYBOOK): ((library dataset, system, current), ...)} - the
    datasets the program's compiler listings name for that copybook, in
    stored order, each with the SYSTEM of the listing member that names it
    and whether that listing is CURRENT (recover's `current` column: its
    source is the program as indexed; None when not dated, or on a table
    written before recover dated listings - such a row is never followed,
    make_resolver follows a current listing only): atlas.recover
    keys the rows by the listing's file stem, so every listing of one program
    NAME shares the key - GC's and GC-TEST's listings of GCPGM1, and one filed
    under estate\\SHARED\\PROD.LISTINGS - and rows_that_count decides which
    of them speak for the program being parsed (a twin in another system:
    its own system's only; else its own system's when one of them is
    current for that copybook, every one otherwise). The
    system is the listing member's own (member.path = the stored listing
    path, member.system as derive_systems set it); a listing the index does
    not hold (a folder given to recover with --from) has none.
    atlas.recover's `listing_copy_source`,
    read ONCE for the whole build (one dict lookup per COPY afterwards). The
    table is recover's own: absent on an index recover never ran on, or
    empty - then nothing is known, and the build never creates it. A row with
    no copybook (a listing read with no table) says nothing."""
    out: Dict[Tuple[str, str], Tuple[ListingRow, ...]] = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_copy_source'").fetchone() is None:
        return out
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(listing_copy_source)")}
    current = "s.current" if "current" in cols else "NULL"             # a table written before recover dated listings
    for program, copybook, dataset, system, cur in conn.execute(
            f"SELECT s.program, s.copybook, s.dataset, m.system, {current} FROM listing_copy_source s "
            "LEFT JOIN member m ON m.path = s.listing "
            "WHERE s.copybook IS NOT NULL AND s.copybook <> '' AND s.dataset IS NOT NULL AND s.dataset <> '' "
            "ORDER BY s.rowid"):
        key = (str(program).strip().upper(), str(copybook).strip().upper())
        row = (str(dataset).strip().upper(), system_key(system), None if cur is None else bool(cur))
        have = out.get(key, ())
        if row not in have:
            out[key] = have + (row,)
    return out


def system_key(system: Optional[str]) -> Optional[str]:
    """A member's system as the listing rule compares it: upper case, None
    for none (an empty string included)."""
    return (str(system).strip().upper() or None) if system else None


_R = TypeVar("_R")


def rows_that_count(rows: Sequence[_R], system: Optional[str], twins: AbstractSet[Optional[str]],
                    listing_system: Callable[[_R], Optional[str]], is_current: Callable[[_R], Optional[bool]],
                    copybook: Optional[Callable[[_R], str]] = None) -> List[_R]:
    """The listing rows that speak for one program - ONE rule, applied by the
    resolver (current_datasets) and by atlas.recover's check
    (recover.rows_of_system) and `program`'s 'listing says' lines, so what
    the build expanded and what recover checks it against never differ.
    Every listing of a program NAME shares the rows' key; each row carries
    the system of the listing member that names it (derive_systems: the
    top-level folder under the estate root - where the file was put, not
    whose it is). The rule is decided per (program, copybook):

    - Another system holds a program member of that name (a twin: GC and
      GC-TEST each hold GCPGM1): only the rows of listings in the program's
      own system count. GC-TEST's listing is its own program's, and a
      listing filed anywhere else cannot say which of the two it is -
      recover dates it against one program of the name only, and a test
      compile's SYSLIB must never decide the production program's copy. A
      program with no system has no own rows then.
    - No twin, and a CURRENT listing in the program's own system names the
      copybook: the rows of the program's own system's listings, and only
      those - its own current compile's word stands, and a listing filed
      elsewhere (under SHARED, in a --from folder) never overrides it.
    - No twin, and no current listing of its own system names the copybook
      (none there, or only an older compile's or one not yet dated, or one
      whose table has no row for it): every listing of the name speaks,
      wherever it is filed - its own system's, estate\\SHARED\\PROD.LISTINGS,
      a folder outside any system, a --from folder the index does not hold -
      and recover dates each against this one program, so where two
      disagree the CURRENT one decides (current_datasets keeps only a
      current listing's rows; recover.check_choices ranks a current
      listing's findings first). An older compile's listing in the program's
      own folder never hides a current one filed elsewhere.

    The rule looks at one copybook's rows together (does a current listing
    of the program's own system name it?), so the rows are grouped by
    `copybook(row)` first: applied to one copybook's rows (the build:
    load_listing_sources is keyed by (program, copybook); `copybook` None)
    or to all of a program's rows (recover, `program`), it gives the same
    rows for each copybook.

    `system`: the program's (system_key); `twins`: the systems (None for a
    member with none) of the program members of that name in systems OTHER
    than the program's; `listing_system(row)`: the row's listing system (None
    for none); `is_current(row)`: the listing's `current` (True, False, or
    None when not dated)."""
    if twins:
        return [r for r in rows if system and listing_system(r) == system]
    key: Callable[[_R], str] = copybook or (lambda _r: "")
    own_word = {key(r) for r in rows if system and listing_system(r) == system and is_current(r)}
    return [r for r in rows if key(r) not in own_word or listing_system(r) == system]


def current_datasets(rows: Sequence[ListingRow], system: Optional[str], twins: AbstractSet[Optional[str]]) -> List[str]:
    """The datasets a CURRENT listing that speaks for the program names, in
    stored order, once each: rows_that_count first (whose listing it is -
    `rows` are one (program, copybook)'s), then only a listing whose source
    is the program as indexed - an older compile's listing or one not yet
    dated never decides."""
    out: List[str] = []
    for dsn, _lsys, cur in rows_that_count(rows, system_key(system), twins, lambda r: r[1], lambda r: r[2]):
        if cur and dsn not in out:
            out.append(dsn)
    return out


def twin_systems(ctx: "Ctx", prog: "Mem") -> Set[Optional[str]]:
    """The systems, other than the program's own, that hold a program member
    (kind cobol) of the same name - GC-TEST for GC's GCPGM2 when GC-TEST holds
    one too. None stands for a member with no system."""
    mine = system_key(prog.system)
    return {system_key(m.system) for m in ctx.by_name.get(prog.name.upper(), ())
            if m.kind == "cobol" and m.id != prog.id} - {mine}


def load_folder_datasets(conn: sqlite3.Connection) -> Dict[str, Optional[str]]:
    """{folder (normalised): dataset} from the `library` table - the fetcher's
    `.atlas-library.json` ties each fetched folder to the dataset it holds.
    The seed of ctx.folder_dataset; a folder not in it is judged by its own
    name (folder_dataset) - a row whose dataset is empty is left out, so the
    folder's name decides exactly as atlas.recover.dataset_of judges it."""
    out: Dict[str, Optional[str]] = {}
    for folder, dataset in conn.execute("SELECT folder, dataset FROM library WHERE folder IS NOT NULL AND dataset IS NOT NULL"):
        dsn = str(dataset).strip().upper()
        if dsn:
            out[_folder_key(str(folder))] = dsn
    return out


def folder_dataset(ctx: Ctx, path: str) -> Optional[str]:
    """The library dataset a member's folder holds: the `library` table first
    (the fetcher's marker), else the folder's own name when it is shaped like
    a dataset (the fetcher names each folder after its dataset; a hand-made
    folder such as `downloads` or `RECOVERED-COPYBOOKS` names none). Cached
    per folder: 121k members sit in a few hundred folders."""
    folder = os.path.dirname(path)
    key = _folder_key(folder)
    if key not in ctx.folder_dataset:
        base = os.path.basename(folder).upper()
        ctx.folder_dataset[key] = base if "." in base and len(base) <= LIB_DSN_MAX and _LIB_DSN.match(base) else None
    return ctx.folder_dataset[key]


def _chain_pick(ctx: Ctx, prog: Mem, cands: List[Mem], lib: Optional[str]) -> Tuple[Mem, str]:
    """The precedence chain over same-named candidates: COPY x OF lib > the
    program's own department in its declared SYSLIB order > same department
    > authoritative > same folder > first. A CLAIMS program must never
    silently expand a POLICY department's copy of a same-named copybook.
    Returns the pick and how it was picked."""
    by_lib = [c for c in cands if lib and c.library.upper() == lib.upper()]
    same_sys = [c for c in cands if prog.system and c.system == prog.system]
    order = ctx.copylib_order.get(prog.system, [])
    if order and same_sys:
        same_sys.sort(key=lambda c: order.index(c.library.upper()) if c.library.upper() in order else 999)
    auth = [c for c in cands if c.authoritative]
    same_lib = [c for c in cands if c.library == prog.library]
    how = ("COPY ... OF" if by_lib else "same system" + (" + declared order" if order and same_sys else "")
           if same_sys else "authoritative" if auth else "same folder" if same_lib else "FIRST FOUND")
    return (by_lib or same_sys or auth or same_lib or cands)[0], how


def listing_pick(ctx: Ctx, prog: Mem, cands: List[Mem], lib: Optional[str], pick: Mem, how: str,
                 named: Sequence[str]) -> Tuple[Mem, str]:
    """The chain's (pick, how) after the program's CURRENT listing, which
    names the library datasets in `named` (current_datasets). The listing
    changes the pick only where the compiler's record and the chain's guess
    differ in CONTENT - a name is not a fact about content (LESSONS 193):

    - the listing names the dataset of the chain's pick: that copy, and the
      how says the listing names it;
    - it names a dataset whose held copies all differ in text from the
      chain's pick: the copy in that dataset is expanded (two folders tied to
      it: the chain breaks the tie) - LISTING_HOW + DATASET;
    - it names a dataset holding a copy with the SAME text as the chain's
      pick (his listings name the staging library the compile ran against;
      the copybook was promoted to production unchanged): the chain's pick
      stays - production, not the staging copy - and the how says
      'confirmed by the program's listing (same text as DATASET)';
    - it names only datasets the index does not hold: the chain decides.

    A different text beats a same text, in the listing's order: the same
    precedence atlas.recover.check_choices gives its verdicts, so a program
    this resolver parsed is never called contradicted, and one recover marks
    is changed by the next parse."""
    own = folder_dataset(ctx, pick.path)
    if own is not None and own in named:
        return pick, LISTING_HOW + own
    same_as: Optional[str] = None
    for dsn in named:
        in_dsn = [c for c in cands if folder_dataset(ctx, c.path) == dsn]
        if not in_dsn:
            continue                                                    # a library the index does not hold
        if any(c.norm_sha == pick.norm_sha for c in in_dsn):
            same_as = same_as or dsn                                    # promoted unchanged: the chain's copy stays
            continue
        chosen, _tie = _chain_pick(ctx, prog, in_dsn, lib)
        return chosen, LISTING_HOW + dsn
    if same_as is not None:
        return pick, f"{LISTING_SAME}{same_as})"
    return pick, how


def _stub_lines(ctx: Ctx, mem: Mem) -> Optional[int]:
    """The lines holding numbers in a member filed `stub` (reader.stub_count),
    read once per build; None when the file cannot be read now."""
    cache = ctx.stub_lines
    if mem.id not in cache:
        try:
            cache[mem.id] = reader.stub_count(*reader.load(mem.path)) or None
        except OSError:
            cache[mem.id] = None
    return cache[mem.id]


def make_resolver(ctx: Ctx, prog: Mem, notes: List[Tuple[str, str, int]]):
    def resolve(name: str, lib: Optional[str]):
        cands = [c for c in ctx.by_name.get(name.upper(), [])
                 if c.kind in RESOLVER_KINDS and c.id != prog.id]
        if not cands:
            # a stub of the name is never a candidate: any real copy in another library came first (ROADMAP
            # re-parse item 23). With none, nothing is expanded and the note says what the member holds - the
            # program is partial, its own lines kept
            stubs = [c for c in ctx.by_name.get(name.upper(), []) if c.kind == STUB_KIND and c.id != prog.id]
            if stubs:
                return None, [], expand.stub_note([(c.library, _stub_lines(ctx, c)) for c in stubs])
            return None
        pick = cands[0]
        if len(cands) > 1:
            # The precedence chain's pick, then the program's compiler
            # listing: it names the library dataset the compiler read this
            # copybook from (atlas.recover's listing_copy_source). Only the
            # rows that speak for THIS program count (rows_that_count): when
            # another system holds a program of its name, its own system's
            # listing only - GC and GC-TEST each hold GCPGM1 with its own
            # listing, and one's listing must not decide for the other's copy;
            # else its own system's listings when one of them is current for
            # this copybook, and every listing of its name, wherever filed,
            # when none is. Only a
            # CURRENT listing (its source is the program as indexed) decides,
            # and only where the copy it names is held with a different text
            # from the chain's pick (listing_pick). Everywhere else - none
            # read, no table, no row, an older or undated listing, a library
            # not held - the chain decides as before.
            pick, how = _chain_pick(ctx, prog, cands, lib)
            named = current_datasets(ctx.listing_sources.get((prog.name.upper(), name.upper()), ()), prog.system,
                                     twin_systems(ctx, prog))
            if named:
                pick, how = listing_pick(ctx, prog, cands, lib, pick, how, named)
            if len({c.norm_sha for c in cands}) > 1:
                # A choice, recorded once: the 'ambiguous_copybook' row names the
                # copy and says how. It is NOT handed back to the expander as a COPY
                # warning - a warning is a gap (NOT FOUND, skipped), and the repeat
                # made every such program 'partial' although every COPY expanded
                # (his 701 'partial' programs with ~60 copybooks missing - LESSONS
                # 181, ROADMAP re-parse item 18).
                notes.append(("ambiguous_copybook",
                              f"{len(cands)} copies of {name} with different content; used {pick.path} ({how})", 0))
        return pick.id, ctx.lines_for(pick), None
    return resolve


# --------------------------------------------------------------------------
# pass 2 - per-kind indexing
# --------------------------------------------------------------------------

def index_cobol(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    lines = ctx.lines_for(mem)
    notes: List[Tuple[str, str, int]] = []
    exp = expand.expand(lines, mem.id, make_resolver(ctx, mem, notes))
    exp_text = expand.expanded_text(exp)
    facts = cobol.parse_program(exp_text)

    if facts.program_id is None and not re.search(r"\b(?:IDENTIFICATION|ID|DATA|PROCEDURE)\s+DIVISION\b",
                                                   exp_text, re.IGNORECASE):
        # Not a program at all (a copybook or a card deck filed in the source
        # library): no program row, and the member says why.
        conn.execute("UPDATE member SET parse_status='skipped', parse_error=? WHERE id=?",
                     ("no PROGRAM-ID and no DIVISION header - not a program", mem.id))
        ctx.bump("not_a_program")
        return

    pid_name = facts.program_id or mem.name
    cur = conn.execute(
        "INSERT INTO program(member_id,program_id,uses_sql,uses_cics,uses_dli,uses_mq,linkage_using,"
        "src_lines,exp_lines) VALUES(?,?,?,?,?,?,?,?,?)",
        (mem.id, pid_name, int(facts.uses_sql), int(facts.uses_cics), int(facts.uses_dli),
         int(facts.uses_mq), _j(facts.linkage_using), len(lines), len(exp.lines)))
    pid = cur.lastrowid
    if facts.program_id is None:
        notes.append(("no_program_id", f"no PROGRAM-ID found; using member name {mem.name}", 0))

    conn.executemany(
        "INSERT INTO expand_run(program_id,exp_start,exp_end,src_member,src_start,depth,via_copy) "
        "VALUES(?,?,?,?,?,?,?)",
        [(pid, r.exp_start, r.exp_end, r.src_member, r.src_start, r.depth, r.via_copy) for r in exp.runs])

    # Fields: keep this program's own data items, plus any copybook brought in
    # with REPLACING (its names are program-specific). Plain copybook fields
    # are reached through copy_use -> the copybook's own field rows. A copied
    # 01/77 renamed by "01 X COPY Y." (OS/VS) is the program's own name.
    replaced = {name for (name, _l, rep, _ln, _r) in exp.copies if rep}
    renamed = set(exp.renamed)
    logical = reader.join_cobol_continuations(exp.lines)
    roots, warns = copybook.parse_data_division(logical)
    keep: List[copybook.Field] = []
    for fld in copybook.flatten(roots):
        if fld.name == copybook.SYNTHETIC_ROOT:
            continue
        run = exp.run_at(fld.line)
        depth = run.depth if run else 0
        if depth == 0 or (run and run.via_copy in replaced) or fld.line in renamed:
            keep.append(fld)
    _insert_fields(conn, mem.id, keep)
    conn.executemany(
        "INSERT INTO literal_ref(member_id,program_id,literal,context,field,line) VALUES(?,?,?,?,?,?)",
        [(mem.id, pid, lit, ctxt, fld, ln) for (lit, ctxt, fld, ln)
         in _group_value_literals(copybook.flatten(roots))])
    # The same tree at THIS program's offsets, so every reference below can
    # link to the item it names (docs/PLAN-value-flow.md section 3).
    scope = _index_pfields(conn, pid, exp, facts, roots, _flow_names(facts))

    conn.executemany(
        "INSERT INTO paragraph(program_id,section,name,start_line,end_line,ordinal,kind) VALUES(?,?,?,?,?,?,?)",
        [(pid, p.section, p.name, p.start_line, p.end_line, p.ordinal, p.kind) for p in facts.paragraphs])
    conn.executemany(
        "INSERT INTO perform_edge(program_id,from_para,to_para,thru_para,line,kind) VALUES(?,?,?,?,?,?)",
        [(pid, a, b, c, ln, kind) for (a, b, c, ln, kind) in facts.performs])
    conn.executemany(
        "INSERT INTO program_alias(program_id,alias,linkage_using,line) VALUES(?,?,?,?)",
        [(pid, name, _j(using), ln) for (name, using, ln) in facts.entries])
    conn.executemany(
        "INSERT INTO cics_cmd(program_id,verb,resource_kind,resource,direction,line) VALUES(?,?,?,?,?,?)",
        [(pid, v, k, r, d, ln) for (v, k, r, d, ln) in facts.cics_cmds])
    # REPLACING renamed copybook fields: keep the link both ways.
    conn.executemany(
        "INSERT INTO field_alias(program_id,copybook,orig_name,new_name,line) VALUES(?,?,?,?,?)",
        [(pid, cb, o, n, ln) for (cb, o, n, ln) in exp.aliases])
    # DCLGEN DECLARE TABLE: the table's columns in declared order.
    for tbl, cols in facts.declared_tables.items():
        qual, _, name = tbl.rpartition(".")
        conn.execute("INSERT OR IGNORE INTO db2_object(kind,qualifier,name,source,member_id) VALUES('table',?,?,'dclgen',?)",
                     (qual or None, name, mem.id))
        oid = conn.execute("SELECT id FROM db2_object WHERE kind='table' AND name=? AND COALESCE(qualifier,'')=?",
                           (name, qual or "")).fetchone()[0]
        if not conn.execute("SELECT 1 FROM db2_column WHERE object_id=? LIMIT 1", (oid,)).fetchone():
            conn.executemany("INSERT INTO db2_column(object_id,ordinal,name,type) VALUES(?,?,?,?)",
                             [(oid, i, c, t) for i, (c, t) in enumerate(cols, 1)])
    # field_ref keeps no qualifier (its names come from the OF/IN-blanked text):
    # a twice-declared name it holds is picked by the data_flow / call_arg row
    # of the same line and mode, which was resolved WITH the qualifier
    picked: Dict[Tuple[int, str, str], int] = {}
    for c in facts.calls:
        cur = conn.execute(
            "INSERT INTO call_edge(program_id,kind,target,via_var,resolved,resolution,using_args,line,returning_item) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, c.kind, c.target, c.via_var, _j(c.resolved), c.resolution, _j(c.using_args), c.line, c.returning))
        if c.args:
            # the caller side of each argument: position, how it is passed, the item
            rows = []
            for a in c.args:
                pf = scope.resolve(a.name, a.quals, c.line)
                if pf is not None and a.quals:
                    picked.setdefault((c.line, a.name, "read"), pf)
                    picked.setdefault((c.line, a.name, "write"), pf)
                rows.append((cur.lastrowid, pid, a.pos, a.name, " OF ".join(a.quals) or None, pf, a.sub, a.how))
            conn.executemany(
                "INSERT INTO call_arg(call_id,program_id,pos,name,qual,pfield,sub,how) VALUES(?,?,?,?,?,?,?,?)", rows)
        ctx.bump("calls:" + ("unresolved" if c.resolution == "unresolved" else c.kind))
    # the callee side: PROCEDURE DIVISION USING (entry NULL), RETURNING at pos 0,
    # each ENTRY under its alias, and a LINKAGE 01 DFHCOMMAREA as the CICS convention
    params: List[Tuple[Optional[str], int, str]] = [(None, i, n) for i, n in enumerate(facts.linkage_using, 1)]
    if facts.returning:
        params.append((None, 0, facts.returning))
    for (alias, using, _ln) in facts.entries:
        params.extend((alias, i, n) for i, n in enumerate(using, 1))
    if facts.uses_cics and "DFHCOMMAREA" in scope.linkage_roots:
        params.append(("DFHCOMMAREA", 1, "DFHCOMMAREA"))
    conn.executemany(
        "INSERT INTO param(program_id,entry,pos,name,pfield) VALUES(?,?,?,?,?)",
        [(pid, entry, pos, n, scope.resolve(n)) for (entry, pos, n) in params])
    flow_rows = []
    for fl in facts.flows:
        src = scope.resolve(fl.src_name, fl.src_qual, fl.line)
        dst = scope.resolve(fl.dst_name, fl.dst_qual, fl.line)
        if src is not None and fl.src_qual:
            picked.setdefault((fl.line, fl.src_name, "read"), src)
        if dst is not None and fl.dst_qual:
            picked.setdefault((fl.line, fl.dst_name, "write"), dst)
        flow_rows.append((pid, fl.line, fl.verb, fl.kind, fl.src_name, fl.src_qual, src, fl.src_sub, fl.src_refmod,
                          fl.src_lit, fl.dst_name, fl.dst_qual, dst, fl.dst_sub, fl.dst_refmod, fl.ordinal, fl.note,
                          fl.guard))
    conn.executemany(
        "INSERT INTO data_flow(program_id,line,verb,kind,src_name,src_qual,src_pfield,src_sub,src_refmod,src_lit,"
        "dst_name,dst_qual,dst_pfield,dst_sub,dst_refmod,ordinal,note,guard) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", flow_rows)

    conn.executemany(
        "INSERT INTO copy_use(member_id,copybook,of_library,replacing,line,resolved_member_id) "
        "VALUES(?,?,?,?,?,?)",
        [(mem.id, name, lib, rep, ln, rid) for (name, lib, rep, ln, rid) in exp.copies])

    for f in facts.files:
        cur = conn.execute(
            "INSERT INTO file_decl(program_id,select_name,assign_dd,organization,access_mode,record_key,"
            "alt_keys,fd_record,line) VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, f.select_name, f.assign_dd, f.organization, f.access_mode, f.record_key,
             _j(f.alt_keys), f.fd_record, f.line))
        # every 01 under the FD, in order - fd_record keeps only the first
        conn.executemany(
            "INSERT INTO file_record(file_id,program_id,ordinal,name,pfield) VALUES(?,?,?,?,?)",
            [(cur.lastrowid, pid, i, n, scope.resolve(n, None, f.line)) for i, n in enumerate(f.fd_records, 1)])
    conn.executemany(
        "INSERT INTO io_op(program_id,target,target_kind,op,line) VALUES(?,?,?,?,?)",
        [(pid, t, k, op, ln) for (t, k, op, ln) in facts.io_ops])
    conn.executemany(
        "INSERT INTO sql_stmt(member_id,program_id,stmt_type,cursor_name,tables,columns,host_vars,"
        "is_dynamic,start_line,end_line,text) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(mem.id, pid, s.stmt_type, s.cursor_name, _j(s.tables), None, _j(s.host_vars),
          int(s.is_dynamic), s.start_line, s.end_line, s.text[:4000]) for s in facts.sql])
    conn.executemany(
        "INSERT INTO dli_call(program_id,interface,func,pcb_arg,pcb_ordinal,ssa_args,io_area,line,pcb_index,"
        "resolution,dest) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(pid, d.interface, d.func, d.pcb_arg, None, _j(d.ssa_args), d.io_area, d.line, d.pcb_index,
          d.resolution, d.dest) for d in facts.dli])
    # MQ: the queue and the message layout are the interface contract.
    conn.executemany(
        "INSERT INTO interface_edge(member_id,kind,detail,direction,line) VALUES(?,?,?,?,?)",
        [(mem.id, "mq",
          (f"{call} queue {queue}" if queue else f"{call} (queue not resolvable from this source)")
          + (f" layout {layout}" if layout else ""),
          direction, ln)
         for (call, queue, direction, layout, ln) in facts.mq if call in ("MQPUT", "MQPUT1", "MQGET", "MQOPEN")])
    # IMS message switch: CHNG to a destination = this program reaches that transaction.
    conn.executemany(
        "INSERT INTO interface_edge(member_id,kind,detail,direction,line) VALUES(?,?,?,?,?)",
        [(mem.id, "ims_msw", f"CHNG destination {d.dest}", "out", d.line) for d in facts.dli if d.dest])
    conn.executemany(
        "INSERT INTO field_ref(program_id,name,mode,stmt,line,pfield_id) VALUES(?,?,?,?,?,?)",
        [(pid, n, mode, stmt, ln, picked.get((ln, n, mode)) or scope.resolve(n, None, ln))
         for (n, mode, stmt, ln) in facts.field_refs])
    conn.executemany(
        "INSERT INTO literal_ref(member_id,program_id,literal,context,field,line) VALUES(?,?,?,?,?,?)",
        [(mem.id, pid, lit, ctxt, fld, ln) for (lit, ctxt, fld, ln) in facts.literal_refs])
    conn.executemany(
        "INSERT INTO sql_col_ref(program_id,tbl,col,host_var,mode,stmt,line,pfield_id) VALUES(?,?,?,?,?,?,?,?)",
        [(pid, t, c, h, m, s, ln, scope.resolve_host(h, ln)) for (t, c, h, m, s, ln) in facts.sql_cols])

    # SELECT * INTO :DCLPOLICY / SELECT a,b,c INTO :GROUP: the columns land in
    # the group's elementary children in order (the DCLGEN idiom). Without
    # this, `column POLICY_TBL.STATUS_CD` says "no static SQL references" for
    # every program that reads the whole row.
    all_fields = copybook.flatten(roots)
    by_name: Dict[str, copybook.Field] = {}
    for fld in all_fields:
        by_name.setdefault(fld.name.upper(), fld)
    for hv, tables, cols, stmt, ln in facts.sql_group_intos:
        grp = by_name.get(hv.upper())
        if grp is None or not grp.children:
            notes.append(("sql_group_into", f"{stmt} INTO :{hv} - group not found in this program's data division; "
                                            f"column-to-field mapping skipped", ln))
            continue
        # Direct children only (a level-49 VARCHAR pair stays one column).
        targets = [c.name.upper() for c in grp.children if c.name.upper() != "FILLER"]
        if cols == ["*"]:
            tbl_names = [t[0] for t in tables]
            decl = None
            for t in tbl_names:
                decl = facts.declared_tables.get(t.upper()) or next(
                    (v for k, v in facts.declared_tables.items() if k.upper().rpartition(".")[2] == t.upper().rpartition(".")[2]), None)
                if decl:
                    break
            if not decl:
                notes.append(("sql_group_into", f"{stmt} * INTO :{hv} - no DECLARE TABLE (DCLGEN) for "
                                                f"{', '.join(tbl_names)} in this program; columns unknown", ln))
                continue
            col_names = [c for c, _t in decl]
            tbl = (tbl_names[0] if tbl_names else "?").upper()
            pairs = [((tbl, c), h) for c, h in zip(col_names, targets)]
        else:
            pairs = []
            for c, h in zip(cols, targets):
                cc = cobol._col(c, tables)
                if cc:
                    pairs.append((cc, h))
        conn.executemany(
            "INSERT INTO sql_col_ref(program_id,tbl,col,host_var,mode,stmt,line,pfield_id) VALUES(?,?,?,?,?,?,?,?)",
            [(pid, t, c, h, "read", stmt + " (positional via group)", ln, scope.resolve(h, None, ln))
             for ((t, c), h) in pairs])
        conn.executemany(
            "INSERT INTO field_ref(program_id,name,mode,stmt,line,pfield_id) VALUES(?,?,?,?,?,?)",
            [(pid, h, "write", "EXEC-SQL", ln, scope.resolve(h, None, ln)) for (_tc, h) in pairs])

    for name, (ln, decls) in scope.ambiguous.items():
        notes.append(("ambiguous_field",
                      f"{name} is declared {len(decls)} times in this program ({', '.join(decls)}); a reference "
                      f"without a qualifier that picks one gets no pfield link and `flow` does not follow it", ln))
    hit = scope.pruned_hits | (scope.used & scope.pruned)
    if hit:
        # a reference named a root stored without its children, or one of the
        # children it was stored without: the pruning rule and _flow_names
        # disagree, and `flow` would stop there
        names = sorted(scope.name_of[i] for i in hit)
        notes.append(("flow_pruned", f"{len(hit)} pruned root(s) are referenced ({', '.join(names[:8])}): "
                                     "the pruning rule missed a reference source - fix _flow_names", 0))
    if facts.uses_cics and "DFHCOMMAREA" not in scope.linkage_roots \
            and any(n in ("DFHCOMMAREA", "EIBCALEN") for (n, _m, _s, _l) in facts.field_refs):
        notes.append(("no_commarea", "CICS program tests EIBCALEN / DFHCOMMAREA but declares no 01 DFHCOMMAREA in "
                                     "LINKAGE: a LINK or XCTL into it cannot be followed by bytes", 0))
    for w in warns:
        if "SYNC" in w or "not compile" in w:
            notes.append(("layout_warning", w, 0))
    for (kind, detail, ln) in facts.unresolved:
        notes.append((kind, detail, ln))
    for w in exp.warnings:          # each one a gap: a COPY NOT FOUND, or skipped (recursive, nested too deep)
        notes.append(("expand", w, 0))
    conn.executemany(
        "INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
        [(mem.id, k, d, ln or None) for (k, d, ln) in notes])
    ctx.bump("unresolved", len(notes))

    if ctx.write_expanded:
        os.makedirs(ctx.write_expanded, exist_ok=True)
        with open(os.path.join(ctx.write_expanded, f"{pid_name}.exp.cbl"), "w", encoding="utf-8") as fh:
            fh.write(exp_text)

    # 'partial' only for a gap the expander reported; a copybook chosen among several is its own
    # 'ambiguous_copybook' row and leaves the program 'ok' (ROADMAP re-parse item 18)
    status = "partial" if any(k in ("expand",) for (k, _d, _l) in notes) else "ok"
    conn.execute("UPDATE member SET parse_status=? WHERE id=?", (status, mem.id))


def _group_value_literals(fields: List[copybook.Field]) -> List[Tuple[str, str, str, int]]:
    """A long message assembled from consecutive FILLER VALUEs:

        01  WS-MSG-TABLE.
            05  FILLER  PIC X(15)  VALUE 'RELATIONSHIP/GE'.
            05  FILLER  PIC X(15)  VALUE 'NDER MISMATCH'.

    Each piece is already a literal_ref; the WHOLE text ('RELATIONSHIP/GENDER
    MISMATCH') is what a search for 'GENDER' must find, so the group's
    concatenated value (each piece padded to its PIC length, as storage would
    be) is emitted as one more literal on the group item.
    """
    out: List[Tuple[str, str, str, int]] = []
    for g in fields:
        if not g.is_group:
            continue
        pieces = []
        for c in g.children:
            v = c.value_lit or ""
            if c.is_group or not (v.startswith("'") or v.startswith('"')):
                pieces = []
                break
            s = cobol._norm_lit(v)
            pieces.append(s.ljust(c.length)[:c.length] if c.length else s)
        if len(pieces) >= 2:
            out.append(("".join(pieces).rstrip(), "value_group", g.name, g.line))
    return out


def _insert_fields(conn: sqlite3.Connection, member_id: int, fields: List[copybook.Field]) -> None:
    ids: Dict[int, int] = {}
    for f in fields:
        parent_id = ids.get(id(f.parent)) if f.parent else None
        cur = conn.execute(
            "INSERT INTO field(member_id,parent_id,level,name,qualified,pic,usage,sign_clause,occurs,"
            "occurs_max,odo_on,redefines,value_lit,offset,length,digits,scale,is_group,line) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (member_id, parent_id, f.level, f.name, f.qualified, f.pic, f.usage, f.sign_clause,
             f.occurs, f.occurs_max, f.odo_on, f.redefines, f.value_lit, f.offset, f.length,
             f.digits, f.scale, int(f.is_group), f.line))
        ids[id(f)] = cur.lastrowid
        for (name, vals, ln) in f.conds:
            conn.execute("INSERT INTO cond88(field_id,name,values_lit,line) VALUES(?,?,?,?)",
                         (cur.lastrowid, name, _j(vals), ln))


_QUAL_SPLIT = re.compile(r"\s+(?:OF|IN)\s+", re.IGNORECASE)


def _chain_has(chain: List[str], quals: List[str]) -> bool:
    """Every qualifier, in order, somewhere up the ancestor chain (nearest first)."""
    i = 0
    for q in quals:
        try:
            i = chain.index(q, i) + 1
        except ValueError:
            return False
    return True


class _PfieldScope:
    """This program's stored pfield rows by name, so a reference (data_flow,
    call_arg, param, file_record, field_ref, sql_col_ref) links to ONE of
    them. One row: it. Several: the OF/IN chain must pick one, else nobody
    is picked and the name is noted once as ambiguous_field - an unqualified
    twice-declared name is never guessed (guard 4). An 88 name is its parent.
    A name dropped with its pruned root has no row; a reference to it is a
    miss of the pruning rule and is collected in pruned_hits (guard 21)."""

    def __init__(self) -> None:
        self.rows: Dict[str, List[Tuple[int, List[str], str]]] = {}   # name -> [(id, ancestors nearest first, path)]
        self.name_of: Dict[int, str] = {}
        self.linkage_roots: Dict[str, int] = {}
        self.pruned: Set[int] = set()
        self.pruned_names: Dict[str, Set[int]] = {}                     # dropped item / 88 name -> pruned root ids
        self.pruned_hits: Set[int] = set()                               # pruned roots a reference named a child of
        self.used: Set[int] = set()
        self.ambiguous: Dict[str, Tuple[int, List[str]]] = {}           # name -> (first line, declared paths)

    def add(self, name: str, pf_id: int, ancestors: List[str], path: str) -> None:
        self.rows.setdefault(name.upper(), []).append((pf_id, ancestors, path))
        self.name_of.setdefault(pf_id, name.upper())

    def resolve(self, name: Optional[str], quals=None, line: int = 0) -> Optional[int]:
        if not name:
            return None
        key = name.upper()
        if key in self.pruned_names:
            # the name was dropped with its root, yet something refers to it:
            # the pruning rule missed this reference source
            self.pruned_hits.update(self.pruned_names[key])
        cands = self.rows.get(key)
        if not cands:
            return None
        if isinstance(quals, str):
            quals = _QUAL_SPLIT.split(quals.strip())
        quals = [q.upper() for q in (quals or []) if q]
        if len(cands) > 1 and quals:
            cands = [c for c in cands if _chain_has(c[1], quals)]
        if len(cands) == 1:
            self.used.add(cands[0][0])
            return cands[0][0]
        if len(cands) > 1 and key not in self.ambiguous:
            self.ambiguous[key] = (line, [c[2] for c in cands])
        return None

    def resolve_host(self, host_var: Optional[str], line: int = 0) -> Optional[int]:
        """`:GRP.ITEM` is ITEM OF GRP in SQL host-variable syntax."""
        if not host_var:
            return None
        parts = host_var.lstrip(":").split(".")
        return self.resolve(parts[-1], list(reversed(parts[:-1])), line)


def _flow_names(facts: cobol.ProgramFacts) -> Set[str]:
    """Every data name the program's facts refer to. A WORKING-STORAGE root
    holding none of them is stored pruned: no reference can lead the walk
    into it, and unused copybooks are most of a large program's bytes."""
    named: Set[str] = {"DFHCOMMAREA"}
    named.update(n for (n, _m, _s, _l) in facts.field_refs)
    for fl in facts.flows:
        for n in (fl.src_name, fl.dst_name):
            if n:
                named.add(n)
    for c in facts.calls:
        named.update(c.using_args)
        named.update(a.name for a in c.args if a.name)
        if c.returning:
            named.add(c.returning)
    named.update(facts.linkage_using)
    if facts.returning:
        named.add(facts.returning)
    for (_alias, using, _ln) in facts.entries:
        named.update(using)
    for fd in facts.files:
        named.update(fd.fd_records)
    for d in facts.dli:
        if d.io_area:
            named.add(d.io_area)
        named.update(d.ssa_args)
    for (_call, _queue, _direction, layout, _ln) in facts.mq:
        if layout:
            named.add(layout.strip())
    for s in facts.sql:
        named.update(h.lstrip(":").split(".")[-1] for h in s.host_vars)
    for (_t, _c, h, _m, _s, _ln) in facts.sql_cols:
        if h:
            named.add(h.lstrip(":").split(".")[-1])
    for (hv, _tables, _cols, _stmt, _ln) in facts.sql_group_intos:
        named.add(hv)
    named.update(t for (t, _k, _op, _ln) in facts.io_ops)
    return {n.upper() for n in named if n}


def _index_pfields(conn: sqlite3.Connection, pid: int, exp: expand.Expansion, facts: cobol.ProgramFacts,
                   roots: List[copybook.Field], named: Set[str]) -> _PfieldScope:
    """pfield rows for one program: every 01/77 of the expanded program at
    THIS program's offsets (a byte before a COPY moves every copied item by
    one), placed in its section and FD by the parser's section marks, with
    the copybook's own `field` row beside it (version skew shows as a
    different offset there). FILE and LINKAGE roots are the program's
    interfaces and are stored whole; a WORKING-STORAGE / LOCAL-STORAGE root
    none of `named` falls in is stored as its root row alone, pruned=1."""
    scope = _PfieldScope()
    marks = sorted(facts.section_marks)
    mark_lines = [m[0] for m in marks]
    copy_rows: Dict[int, Tuple[Dict[Tuple[int, str], int], Dict[int, List[int]]]] = {}

    def copy_field_id(member_id: int, line: int, name: str) -> Optional[int]:
        got = copy_rows.get(member_id)
        if got is None:
            by_key: Dict[Tuple[int, str], int] = {}
            by_line: Dict[int, List[int]] = {}
            for (fid, ln, nm) in conn.execute("SELECT id, line, name FROM field WHERE member_id=?", (member_id,)):
                by_key[(ln, nm)] = fid
                by_line.setdefault(ln, []).append(fid)
            got = copy_rows[member_id] = (by_key, by_line)
        fid = got[0].get((line, name))
        if fid is None and len(got[1].get(line, ())) == 1:       # REPLACING renamed it: same line, one item
            fid = got[1][line][0]
        return fid

    ins = ("INSERT INTO pfield(program_id,parent_id,root_id,section,fd_select,level,name,qualified,pic,usage,"
           "occurs_max,odo_on,redefines,offset,length,digits,scale,is_group,after_odo,exp_line,src_member,src_line,"
           "copy_field_id,pruned) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    tops: List[copybook.Field] = []
    for r in roots:
        tops.extend(r.children if r.name == copybook.SYNTHETIC_ROOT else [r])
    for root in tops:
        k = bisect.bisect_right(mark_lines, root.line) - 1
        section, fd = (marks[k][1], marks[k][2]) if k >= 0 else ("UNKNOWN", None)
        items = copybook.flatten([root])
        whole = section in ("FILE", "LINKAGE") or any(
            f.name in named or any(c[0] in named for c in f.conds) for f in items)
        dropped: List[copybook.Field] = []
        if not whole:
            dropped = items[1:]                 # flatten is pre-order: the root first, then its subtree
            items = [root]
        ids: Dict[int, int] = {}
        root_id: Optional[int] = None
        odo_seen: List[copybook.Field] = []
        for f in items:
            chain: List[str] = []
            p = f.parent
            while p is not None and id(p) in ids:
                chain.append(p.name)
                p = p.parent
            parent_id = ids.get(id(f.parent)) if f.parent is not None else None
            after_odo = 0
            for o in odo_seen:
                # an earlier OCCURS DEPENDING ON that is not this item's own
                # table: the offset holds only for the maximum count
                a = f.parent
                while a is not None and a is not o:
                    a = a.parent
                if a is None:
                    after_odo = 1
                    break
            src_member, src_line, depth = exp.origin(f.line)
            cfid = copy_field_id(src_member, src_line, f.name) if depth > 0 and src_member is not None else None
            cur = conn.execute(ins, (pid, parent_id, root_id, section, fd, f.level, f.name, f.qualified, f.pic,
                                     f.usage, f.occurs_max, f.odo_on, f.redefines, f.offset, f.length, f.digits,
                                     f.scale, int(f.is_group), after_odo, f.line, src_member, src_line, cfid,
                                     int(not whole)))
            rid = cur.lastrowid
            ids[id(f)] = rid
            if root_id is None:
                root_id = rid
                conn.execute("UPDATE pfield SET root_id=? WHERE id=?", (rid, rid))
                if section == "LINKAGE":
                    scope.linkage_roots[f.name] = rid
                if not whole:
                    scope.pruned.add(rid)
                    for d in dropped:
                        # the children have no rows; remember their names so a
                        # reference to one is caught by the self-check
                        scope.pruned_names.setdefault(d.name.upper(), set()).add(rid)
                        for (cname, _vals, _ln) in d.conds:
                            scope.pruned_names.setdefault(cname.upper(), set()).add(rid)
            scope.add(f.name, rid, chain, f.qualified)
            for (cname, _vals, _ln) in f.conds:
                scope.add(cname, rid, [f.name] + chain, f.qualified + "." + cname)
            if f.odo_on:
                odo_seen.append(f)
    return scope


def index_copybook(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    lines = ctx.lines_for(mem)
    logical = reader.join_cobol_continuations(lines)
    roots, warns = copybook.parse_data_division(logical)
    fields = [f for f in copybook.flatten(roots) if f.name != copybook.SYNTHETIC_ROOT]
    _insert_fields(conn, mem.id, fields)

    lits: List[Tuple[str, str, str, int]] = list(_group_value_literals(fields))
    for f in fields:
        if f.value_lit:
            lits.append((cobol._norm_lit(f.value_lit), "value", f.name, f.line))
        for (name, vals, ln) in f.conds:
            for v in vals:
                if v.upper() not in ("THRU", "THROUGH"):
                    lits.append((cobol._norm_lit(v), "cond88", f"{f.name}/{name}", ln))
    conn.executemany(
        "INSERT INTO literal_ref(member_id,program_id,literal,context,field,line) VALUES(?,NULL,?,?,?,?)",
        [(mem.id, lit, c, fld, ln) for (lit, c, fld, ln) in lits])

    # Nested COPY / procedure code inside a copybook: parse for copies only.
    text = "\n".join(f"{'':6}{l.indicator}{l.code}" for l in lines)
    facts = cobol.parse_program(text)
    conn.executemany(
        "INSERT INTO copy_use(member_id,copybook,of_library,replacing,line) VALUES(?,?,?,?,?)",
        [(mem.id, n, lib, rep, ln) for (n, lib, rep, ln) in facts.copies])
    # A DCLGEN's DECLARE TABLE is data, not procedure code: it is the
    # table's column list (positional, behind SELECT *), recorded here so
    # `table X` knows the columns even before any program includes it.
    for tbl, cols in facts.declared_tables.items():
        qual, _, name = tbl.rpartition(".")
        conn.execute("INSERT OR IGNORE INTO db2_object(kind,qualifier,name,source,member_id) VALUES('table',?,?,'dclgen',?)",
                     (qual or None, name, mem.id))
        oid = conn.execute("SELECT id FROM db2_object WHERE kind='table' AND name=? AND COALESCE(qualifier,'')=?",
                           (name, qual or "")).fetchone()[0]
        if not conn.execute("SELECT 1 FROM db2_column WHERE object_id=? LIMIT 1", (oid,)).fetchone():
            conn.executemany("INSERT INTO db2_column(object_id,ordinal,name,type) VALUES(?,?,?,?)",
                             [(oid, i, c, t) for i, (c, t) in enumerate(cols, 1)])
    proc_sql = [s for s in facts.sql if s.stmt_type not in ("DECLARE", "INCLUDE", "WHENEVER")]
    if facts.calls or facts.performs or proc_sql:
        conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     (mem.id, "procedure_copybook",
                      f"copybook contains procedure code ({len(facts.calls)} CALL, "
                      f"{len(facts.performs)} PERFORM, {len(proc_sql)} SQL); its facts are attributed "
                      f"to each including program via expansion", None))
    for w in warns:
        if "SYNC" in w or "not compile" in w or "mixed" in w:
            conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                         (mem.id, "layout_warning", w, None))
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_jcl(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    text, data, enc = reader.load(mem.path)
    # One member may hold several JOB cards (a job stream): one job row each.
    for facts in jcl.parse_jcl_all(text, data, enc, include_lookup=lambda n: _include_text(ctx, n, mem),
                                   member_lookup=lambda n: _card_text(ctx, n, mem)):
        _index_jcl_facts(ctx, mem, facts)
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def _index_jcl_facts(ctx: Ctx, mem: Mem, facts: jcl.JclFacts) -> None:
    conn = ctx.conn
    job_id = proc_id = None
    if not facts.steps and not facts.job_name and not facts.is_proc and not facts.instream_procs and mem.kind != "proc":
        # A member in a JCL-named folder with no JOB/EXEC/PROC statement is a
        # control-card member, not a job: re-typed so it is looked up as cards
        # and never gets a phantom job row.
        conn.execute("UPDATE member SET kind='ctlcard' WHERE id=?", (mem.id,))
        mem.kind = "ctlcard"
        ctx.bump("retyped:ctlcard")
        return
    if facts.is_proc or mem.kind == "proc":
        cur = conn.execute(
            "INSERT INTO proc_def(member_id,proc_name,symbolics,instream,line) VALUES(?,?,?,?,?)",
            (mem.id, facts.proc_name or mem.name, _j(facts.symbolics), 0, facts.job_line or 1))
        proc_id = cur.lastrowid
    else:
        cur = conn.execute(
            "INSERT INTO job(member_id,job_name,line,joblib,job_cond,jcllib) VALUES(?,?,?,?,?,?)",
            (mem.id, facts.job_name or mem.name, facts.job_line or 1,
             _j([d.dsn_resolved or d.dsn for d in facts.job_dds if d.dd_name.upper() == "JOBLIB"]),
             facts.job_cond, _j(facts.jcllib)))
        job_id = cur.lastrowid
    conn.executemany("INSERT INTO include_use(member_id,include_member) VALUES(?,?)",
                     [(mem.id, inc) for inc in sorted(set(facts.includes))])

    def insert_step(s: jcl.StepFact, ordinal: int, owner: Optional[Tuple[Optional[int], Optional[int]]] = None) -> int:
        jid, pid = owner if owner else (job_id, proc_id)
        cur = conn.execute(
            "INSERT INTO step(job_id,proc_id,ordinal,step_name,pgm,proc_called,effective_pgm,launcher,"
            "parm,cond,from_proc,parent_step,guard,line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, pid, ordinal, s.step_name, s.pgm, s.proc_called, s.effective_pgm,
             s.launcher, s.parm, s.cond, s.from_proc, s.parent_step, s.guard, s.line))
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO dd(step_id,dd_name,concat_seq,dsn,dsn_resolved,gdg_rel,disp,mode,mode_source,"
            "sysin_text,is_override,card_member,is_temp,line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, d.dd_name, d.concat_seq, d.dsn, d.dsn_resolved, d.gdg_rel, d.disp, d.mode,
              d.mode_source, d.sysin_text, int(d.is_override), d.card_member, int(d.is_temp), d.line)
             for d in s.dds])
        # Dataset rows come from JOB-level steps only. A bare PROC's rows carry
        # its DEFAULT symbolics (TEST.CLM.MASTER) - names nobody runs - and
        # &&TEMP is not a dataset of the estate: it cannot link two jobs.
        if pid is None:
            for d in s.dds:
                if d.dsn_resolved and not d.is_temp:
                    base = d.dsn_resolved
                    conn.execute("INSERT OR IGNORE INTO dataset(dsn,is_gdg) VALUES(?,?)",
                                 (base, int(d.gdg_rel is not None)))
                    if d.gdg_rel is not None:
                        conn.execute("UPDATE dataset SET is_gdg=1 WHERE dsn=?", (base,))
        kind = jcl.LAUNCHERS.get(s.launcher or "")
        ctl = "\n".join(d.sysin_text for d in s.dds if d.sysin_text)
        if kind == "sort":
            conn.executemany(
                "INSERT INTO card_field_ref(step_id,card_kind,pos,length,fmt,raw) VALUES(?,?,?,?,?,?)",
                [(sid, k, p, ln, fmt, raw) for (k, p, ln, fmt, raw) in jcl.sort_card_fields(ctl)])
        if kind == "easytrieve":
            # Easytrieve field definitions are byte positions: a copybook
            # offset change must find them like sort cards.
            conn.executemany(
                "INSERT INTO card_field_ref(step_id,card_kind,pos,length,fmt,raw) VALUES(?,?,?,?,?,?)",
                [(sid, k, p, ln, fmt, raw) for (k, p, ln, fmt, raw) in jcl.easytrieve_fields(ctl)])
        # SYSOUT=(x,INTRDR): a scheduling edge written in JCL.
        for sub in s.submits:
            conn.execute("INSERT INTO sched_dep(job_name,depends_on,kind) VALUES(?,?,?)",
                         (sub, facts.job_name or mem.name, "intrdr"))
        # DB2 utility / DSNTIAUL / DSNTEP2: the table <-> flat-file lineage.
        eff = (s.effective_pgm or "").strip("*")
        if kind == "db2util" or eff in ("DSNTIAUL", "DSNTEP2", "DSNTEP4") or s.launcher == "DSNUTILB":
            is_sql = eff in ("DSNTIAUL", "DSNTEP2", "DSNTEP4")
            sysin = "\n".join(d.sysin_text for d in s.dds if d.sysin_text and d.dd_name.upper().endswith("SYSIN"))
            for op, tbl, via_dd, direction in jcl.db2util_ops(sysin, is_sql=is_sql):
                conn.execute("INSERT INTO step_table(step_id,op,tbl,via_dd,direction) VALUES(?,?,?,?,?)",
                             (sid, op, tbl, via_dd, direction))
                if via_dd:
                    # LOAD reads the DD (INDDN); UNLOAD writes it (UNLDDN).
                    conn.execute("""UPDATE dd SET mode=?, mode_source='db2util_card' WHERE step_id=? AND UPPER(dd_name)=?
                                    AND mode_source NOT IN ('open_verb')""",
                                 ("input" if op == "LOAD" else "output", sid, via_dd.upper()))
            if is_sql:
                # DSNTIAUL writes SYSRECnn; DSNTEP2 prints. Every SYSREC DD is output.
                conn.execute("""UPDATE dd SET mode='output', mode_source='db2util_card' WHERE step_id=?
                                AND UPPER(dd_name) LIKE 'SYSREC%' AND mode_source NOT IN ('open_verb')""", (sid,))
        if kind == "idcams":
            # DEFINE attributes: RECORDSIZE / KEYS / LIMIT / RELATE - the numbers
            # a copybook or a rerun must agree with.
            for name, a in jcl.idcams_attrs(ctl).items():
                conn.execute("INSERT OR IGNORE INTO dataset(dsn) VALUES(?)", (name,))
                conn.execute("""UPDATE dataset SET vsam_type=COALESCE(?, vsam_type), recordsize_max=COALESCE(?, recordsize_max),
                                key_len=COALESCE(?, key_len), key_off=COALESCE(?, key_off), gdg_limit=COALESCE(?, gdg_limit),
                                relates_to=COALESCE(?, relates_to) WHERE dsn=?""",
                             (a.get("vsam_type"), a.get("recordsize_max"), a.get("key_len"), a.get("key_off"),
                              a.get("gdg_limit"), a.get("relates_to"), name))
            # A VSAM file is born (DEFINE) or dies (DELETE) here; REPRO copies it.
            for op, name, mode in jcl.idcams_ops(ctl):
                if op == "REPRO DD":
                    conn.execute("UPDATE dd SET mode=?, mode_source='idcams_card' WHERE step_id=? AND UPPER(dd_name)=?",
                                 (mode, sid, name))
                else:
                    conn.execute(
                        "INSERT INTO dd(step_id,dd_name,concat_seq,dsn,dsn_resolved,mode,mode_source,is_override,line) "
                        "VALUES(?,?,0,?,?,?,'idcams_card',0,?)", (sid, f"*{op}*", name, name, mode, s.line))
                    conn.execute("INSERT OR IGNORE INTO dataset(dsn) VALUES(?)", (name,))
                    if op.startswith(("DEFINE CLUSTER", "DEFINE AIX", "DEFINE ALTERNATEINDEX", "DEFINE PATH")):
                        conn.execute("UPDATE dataset SET is_vsam=1 WHERE dsn=?", (name,))
                    if op == "DEFINE GDG":
                        conn.execute("UPDATE dataset SET is_gdg=1 WHERE dsn=?", (name,))
        if kind in ("ftp", "ndm", "usssh"):
            notes = "; ".join(s.notes)
            direction = ("both" if "(out)" in notes and "(in)" in notes
                         else "out" if "(out)" in notes else "in" if "(in)" in notes else None)
            conn.execute(
                "INSERT INTO interface_edge(member_id,kind,detail,direction,line) VALUES(?,?,?,?,?)",
                (mem.id, kind, notes[:500], direction, s.line))
        if s.notes:
            conn.execute("UPDATE step SET parm=COALESCE(parm,'') || ' /* ' || ? || ' */' WHERE id=?",
                         ("; ".join(s.notes)[:300], sid))
        # A second RUN PROGRAM(...) in the same SYSTSIN is a second program
        # run by this step: its own row, same DDs, so `program X` finds it.
        for k, extra in enumerate(s.also_runs, 2):
            insert_step(replace(s, step_name=f"{s.step_name}#{k}", effective_pgm=extra, also_runs=[],
                                notes=[f"{k}. RUN PROGRAM in the SYSTSIN of {s.step_name}"]), ordinal, owner)
        ctx.bump("steps:proc" if s.proc_called else "steps:launcher" if s.launcher else "steps:pgm")
        return sid

    # Expand BEFORE inserting the job's own steps: a DSN=*.STEP.PROCSTEP.DD
    # referback in a plain job step resolves only once the PROC steps exist,
    # and expand_job writes that resolution back onto the job's step objects.
    effective: List[jcl.StepFact] = []
    if job_id is not None:
        effective = jcl.expand_job(facts, lambda n: _proc_facts(ctx, n, mem, facts),
                                   member_lookup=lambda n: _card_text(ctx, n, mem))

    for s in facts.steps:
        insert_step(s, s.ordinal)

    # Instream PROCs (// PROC ... // PEND) are PROCs of this member only.
    for pname, pf in facts.instream_procs.items():
        cur = conn.execute(
            "INSERT INTO proc_def(member_id,proc_name,symbolics,instream,line) VALUES(?,?,?,1,?)",
            (mem.id, pname, _j(pf.symbolics), pf.job_line or 1))
        ipid = cur.lastrowid
        for s in pf.steps:
            insert_step(s, s.ordinal, owner=(None, ipid))
        ctx.bump("procs:instream")

    # Effective steps: every EXEC PROC= expanded with THIS job's symbolics and
    # //STEP.DD overrides. These rows carry the datasets the job really uses.
    parent_ord = {s.step_name: s.ordinal for s in facts.steps}
    for i, e in enumerate(effective, 1):
        if e.from_proc:
            top = (e.parent_step or "").split(".")[0]
            insert_step(e, parent_ord.get(top, 0) * 100 + i)
            ctx.bump("steps:expanded")

    # A DFHCSDUP deck or an IMS stage-1 deck is usually the SYSIN of a JCL
    # step: harvest it in place.
    for s in facts.steps:
        for d in s.dds:
            if not d.sysin_text:
                continue
            if re.search(r"\b(?:DEFINE|ALTER)\s+(?:TRANSACTION|TDQUEUE|DB2ENTRY|URIMAP|FILE)\s*\(", d.sysin_text, re.I):
                _insert_routing(ctx, mem, txn.parse_csd(d.sysin_text))
            elif _IMSGEN_IN_SYSIN.search(d.sysin_text):
                _insert_routing(ctx, mem, txn.parse_imsgen(d.sysin_text))

    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     [(mem.id, k, d, ln) for (k, d, ln) in facts.unresolved])
    ctx.bump("unresolved", len(facts.unresolved))
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_dbd(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    text, _d, _e = reader.load(mem.path)
    for f in ims.parse_dbd_all(text):
        cur = conn.execute("INSERT INTO ims_dbd(member_id,name,access,line,dd1,dd2) VALUES(?,?,?,?,?,?)",
                           (mem.id, f.name or mem.name, f.access, f.line or 1, f.dd1, f.dd2))
        did = cur.lastrowid
        for s in f.segments:
            sc = conn.execute(
                "INSERT INTO ims_segment(dbd_id,name,parent,bytes,seq_field,line) VALUES(?,?,?,?,?,?)",
                (did, s.name, s.parent, s.bytes_max, s.seq_field, s.line))
            # FIELDs are what an SSA can qualify on and where a copybook
            # field maps into the segment.
            conn.executemany(
                "INSERT INTO ims_field(segment_id,name,start,bytes,is_seq,line) VALUES(?,?,?,?,?,?)",
                [(sc.lastrowid, n, st, by, int(seq), s.line) for (n, st, by, seq) in s.fields])
        conn.executemany(
            "INSERT INTO ims_xdfld(dbd_id,name,segment,srch,line) VALUES(?,?,?,?,?)",
            [(did, n, seg, _j(srch), ln) for (n, seg, srch, ln) in f.xdfld])
        conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                         [(mem.id, "ims_dbd", w, None) for w in f.warnings])
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_psb(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    text, _d, _e = reader.load(mem.path)
    f = ims.parse_psb(text, fallback_name=mem.name)
    has_tp = any(p.pcb_type == "TP" for p in f.pcbs)
    cur = conn.execute(
        "INSERT INTO ims_psb(member_id,name,psb_type,lang,cmpat,io_pcb_first,line) VALUES(?,?,?,?,?,?,?)",
        (mem.id, f.name or mem.name, "TP" if has_tp else "DB", f.lang, f.cmpat,
         None if f.io_pcb_first is None else int(f.io_pcb_first), 1))
    psid = cur.lastrowid
    conn.executemany(
        "INSERT INTO ims_pcb(psb_id,ordinal,pcb_type,dbd_name,procopt,keylen,sensegs,line,list_no,procseq) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(psid, p.ordinal, p.pcb_type, p.dbd_name, p.procopt, p.keylen,
          _j([s[0] for s in p.sensegs]), p.line, int(p.list_no), p.procseq) for p in f.pcbs])
    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     [(mem.id, "ims_psb", w, None) for w in f.warnings])
    conn.execute("UPDATE member SET parse_status='ok' WHERE id=?", (mem.id,))


def index_doc(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    d = docs.extract(mem.path)
    # every section small enough to hand to a model whole and to cite precisely
    fts_rows = []
    for i, (h, t) in enumerate(docs.chunk_sections(d.sections), 1):
        conn.execute("INSERT INTO doc_section(member_id,heading,text,ordinal) VALUES(?,?,?,?)", (mem.id, h, t, i))
        if t:
            fts_rows.append((mem.name, "doc", mem.id, i, (h + "\n" + t)[:20000]))
    fts_insert(conn, mem.id, fts_rows)
    # table rows are part of their section's text (a sheet IS its rows), so
    # they are searched and cited section-precisely - no separate line-0 rows
    conn.executemany("INSERT INTO doc_image(member_id,name,anchor) VALUES(?,?,?)",
                     [(mem.id, n, d.image_anchor.get(n)) for n in d.images])
    ctx.bump("doc_images", len(d.images))
    # a converted copy whose legacy original (.doc/.xls/.ppt beside it) was
    # changed after the conversion: the index would describe the old text
    stem, ext = os.path.splitext(mem.path)
    for legacy, modern in docs.LEGACY_TO_MODERN.items():
        old = stem + legacy
        if ext.lower() == modern and os.path.exists(old):
            try:
                if os.path.getmtime(old) > os.path.getmtime(mem.path) + 1:
                    d.ok = False
                    d.notes.append(f"STALE copy: {os.path.basename(old)} changed after this conversion - "
                                   "run atlas.convert --refresh")
            except OSError:
                pass
    conn.execute("UPDATE member SET parse_status=?, parse_error=? WHERE id=?",
                 ("ok" if d.ok else "partial", "; ".join(d.notes) or None, mem.id))


def index_fts_code(ctx: Ctx, mem: Mem) -> None:
    conn = ctx.conn
    if mem.kind in ("cobol", "copybook"):
        rows = [(mem.name, mem.kind, mem.id, l.no, l.code) for l in ctx.lines_for(mem) if l.code.strip()]
    else:
        text, data, enc = reader.load(mem.path)
        rows = [(mem.name, mem.kind, mem.id, i, r.rstrip())
                for i, r in enumerate(reader._split_records(text, data, enc), 1) if r.strip()]
    fts_insert(conn, mem.id, rows)


def _insert_screens(ctx: Ctx, mem: Mem, scr: List[screens.Screen]) -> None:
    conn = ctx.conn
    for s in scr:
        cur = conn.execute(
            "INSERT INTO screen(member_id,kind,name,parent,mode,next_msg,lang,size,line) VALUES(?,?,?,?,?,?,?,?,?)",
            (mem.id, s.kind, s.name, s.parent, s.mode, s.next_msg, s.lang, s.size, s.line))
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO screen_field(screen_id,name,ordinal,row,col,length,offset,seg,attrb,initial,picin,"
            "picout,literal,line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, f.name, f.ordinal, f.row, f.col, f.length, f.offset, f.seg, f.attrb, f.initial,
              f.picin, f.picout, f.literal, f.line) for f in s.fields])
        # Defaults and on-screen constants are literals too: a default 'U' for
        # gender, a label 'GENDER:', a fixed transaction code.
        conn.executemany(
            "INSERT INTO literal_ref(member_id,program_id,literal,context,field,line) VALUES(?,NULL,?,?,?,?)",
            [(mem.id, f.initial if f.initial is not None else f.literal,
              "screen_initial" if f.initial is not None else "screen_literal",
              f.name or f"{s.name}#{f.ordinal}", f.line)
             for f in s.fields if (f.initial is not None or f.literal is not None)])
        conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                         [(mem.id, "screen", w, None) for w in s.warnings])
    conn.execute("UPDATE member SET parse_status=? WHERE id=?", ("ok" if scr else "partial", mem.id))
    if not scr:
        conn.execute("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     (mem.id, "screen", "no map/format/message macros recognised", None))


def index_bms(ctx: Ctx, mem: Mem) -> None:
    text, _d, _e = reader.load(mem.path)
    _insert_screens(ctx, mem, screens.parse_bms(text))


def index_mfs(ctx: Ctx, mem: Mem) -> None:
    text, _d, _e = reader.load(mem.path)
    _insert_screens(ctx, mem, screens.parse_mfs(text))


def _insert_routing(ctx: Ctx, mem: Mem, facts: txn.RoutingFacts) -> None:
    conn = ctx.conn
    conn.executemany(
        "INSERT INTO transaction_def(member_id,tran_code,system,program,psb,group_name,detail,line) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(mem.id, t.tran_code, t.system, t.program, t.psb, t.group, t.detail, t.line) for t in facts.transactions])
    conn.executemany(
        "INSERT INTO cics_program(member_id,name,group_name,language,line) VALUES(?,?,?,?,?)",
        [(mem.id, p.name, p.group, p.language, p.line) for p in facts.programs])
    conn.executemany(
        "INSERT INTO cics_file(member_id,name,dsname,group_name,line) VALUES(?,?,?,?,?)",
        [(mem.id, f.name, f.dsname, f.group, f.line) for f in facts.files])
    for dsn in {f.dsname for f in facts.files if f.dsname}:
        # The JCL may have created the row already; the CSD is the authority
        # that this dataset is VSAM, so update rather than ignore.
        conn.execute("INSERT OR IGNORE INTO dataset(dsn,is_vsam) VALUES(?,1)", (dsn,))
        conn.execute("UPDATE dataset SET is_vsam=1 WHERE dsn=?", (dsn,))
    conn.executemany(
        "INSERT INTO cics_resource(member_id,type,name,group_name,attrs,line) VALUES(?,?,?,?,?,?)",
        [(mem.id, t, n, (a.get("GROUP") or "").upper() or None, _j(a), ln) for (t, n, a, ln) in facts.resources])
    entries: Dict[str, str] = {}
    for t, n, a, ln in facts.resources:
        if t == "TDQUEUE" and (a.get("DSNAME") or "").strip():
            # An extrapartition TD queue IS a dataset: the classic online ->
            # batch hand-off.
            dsn = a["DSNAME"].strip().upper()
            conn.execute("INSERT OR IGNORE INTO dataset(dsn) VALUES(?)", (dsn,))
            direction = "out" if (a.get("TYPEFILE") or "").upper().startswith("OUT") else \
                        "in" if (a.get("TYPEFILE") or "").upper().startswith("IN") else None
            conn.execute("INSERT INTO interface_edge(member_id,kind,detail,direction,line) VALUES(?,?,?,?,?)",
                         (mem.id, "cics_tdq", f"TD queue {n} -> {dsn}", direction, ln))
        elif t == "DB2ENTRY":
            entries[n] = (a.get("PLAN") or "").upper()
        elif t == "URIMAP" and (a.get("PROGRAM") or "").strip():
            # An inbound web request routed straight to a program: routing
            # without a transaction code.
            conn.execute(
                "INSERT INTO transaction_def(member_id,tran_code,system,program,psb,group_name,detail,line) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (mem.id, n, "cics_web", a["PROGRAM"].strip().upper(), None,
                 (a.get("GROUP") or "").upper() or None,
                 f"URIMAP PATH({a.get('PATH', '?')}) USAGE({a.get('USAGE', '?')})", ln))
    for t, n, a, ln in facts.resources:
        if t == "DB2TRAN" and a.get("TRANSID") and a.get("ENTRY"):
            # DB2TRAN -> DB2ENTRY PLAN(): the plan a CICS transaction runs under
            plan = entries.get(a["ENTRY"].strip().upper())
            if plan:
                conn.execute("UPDATE transaction_def SET detail=COALESCE(detail,'') || ? WHERE UPPER(tran_code)=? AND system='cics'",
                             (f"; DB2 plan {plan} (DB2ENTRY {a['ENTRY'].strip().upper()})", a["TRANSID"].strip().upper()))
        elif t == "DB2ENTRY" and a.get("TRANSID") and a.get("PLAN"):
            conn.execute("UPDATE transaction_def SET detail=COALESCE(detail,'') || ? WHERE UPPER(tran_code)=? AND system='cics'",
                         (f"; DB2 plan {a['PLAN'].strip().upper()} (DB2ENTRY {n})", a["TRANSID"].strip().upper()))
    conn.executemany("INSERT INTO ims_online_db(member_id,dbd,access,line) VALUES(?,?,?,?)",
                     [(mem.id, d, acc or None, ln) for (d, acc, ln) in facts.databases])
    conn.executemany("INSERT INTO unresolved(member_id,kind,detail,line) VALUES(?,?,?,?)",
                     [(mem.id, "routing", w, None) for w in facts.warnings])
    ctx.bump("transactions", len(facts.transactions))


def index_csd(ctx: Ctx, mem: Mem) -> None:
    text, _d, _e = reader.load(mem.path)
    facts = txn.parse_csd(text)
    _insert_routing(ctx, mem, facts)
    ctx.conn.execute("UPDATE member SET parse_status=? WHERE id=?",
                     ("ok" if facts.transactions or facts.programs or facts.files else "partial", mem.id))


def index_imsgen(ctx: Ctx, mem: Mem) -> None:
    text, _d, _e = reader.load(mem.path)
    facts = txn.parse_imsgen(text)
    _insert_routing(ctx, mem, facts)
    ctx.conn.execute("UPDATE member SET parse_status=? WHERE id=?",
                     ("ok" if facts.transactions or facts.programs else "partial", mem.id))


HANDLERS = {
    "cobol": index_cobol, "copybook": index_copybook, "jcl": index_jcl, "proc": index_jcl,
    "dbd": index_dbd, "psb": index_psb, "doc": index_doc,
    "bms": index_bms, "mfs": index_mfs, "csd": index_csd, "imsgen": index_imsgen,
}


# --------------------------------------------------------------------------
# post passes
# --------------------------------------------------------------------------

def post_open_modes(ctx: Ctx) -> int:
    """The program's OPEN verb decides direction, overriding the JCL guess."""
    conn = ctx.conn
    n = 0
    rows = conn.execute("""
        SELECT p.id, p.program_id, f.assign_dd, f.select_name
        FROM program p JOIN file_decl f ON f.program_id = p.id
        WHERE f.assign_dd IS NOT NULL""").fetchall()
    for pid, pname, dd_name, sel in rows:
        opens = conn.execute(
            "SELECT op FROM io_op WHERE program_id=? AND target=? AND op LIKE 'OPEN%'",
            (pid, sel)).fetchall()
        if not opens:
            continue
        modes = {o[0].split()[-1] for o in opens}
        mode = ("both" if len(modes) > 1 or "I-O" in modes else
                {"INPUT": "input", "OUTPUT": "output", "EXTEND": "mod"}.get(next(iter(modes)), "unknown"))
        cur = conn.execute("""
            UPDATE dd SET mode=?, mode_source='open_verb'
            WHERE UPPER(dd_name) = ? AND step_id IN (SELECT id FROM step WHERE effective_pgm=?)""",
            (mode, dd_name.upper(), pname))
        n += cur.rowcount
    return n


def post_pcb_positions(ctx: Ctx) -> int:
    """Turn every DL/I call's PCB argument into a database name.

    The PSB is found from the DFSRRC00 PARM of a step running the program,
    else the stage-1 APPLCTN, else a PSB named like the program. The
    program's USING list gives the argument's position; the PSB's PCB order
    (I/O PCB first when CMPAT=YES / a TP PCB / BMP-MPP region; LIST=NO PCBs
    skipped) gives the PCB at that position. This is the off-by-one that
    names the wrong database when done by hand (LESSONS 23), so it is done
    here, once, and the reasoning is stored on the row (`pcb_source`).
    """
    conn = ctx.conn
    n = 0
    progs = conn.execute("""SELECT DISTINCT p.id, p.program_id, p.linkage_using FROM program p
                            JOIN dli_call d ON d.program_id=p.id""").fetchall()
    for p in progs:
        pid, pname = p[0], p[1].upper()
        using = [u.upper() for u in json.loads(p[2] or "[]")]
        cands: List[Tuple[str, Optional[str], str]] = []
        for (parm,) in conn.execute("SELECT parm FROM step WHERE UPPER(effective_pgm)=? AND parm LIKE '%PSB %'", (pname,)):
            m = re.search(r"\bPSB ([A-Z0-9@#$]+)", parm or "")
            r = re.search(r"IMS region type ([A-Z]+)", parm or "")
            if m:
                cands.append((m.group(1).upper(), r.group(1).upper() if r else None, "DFSRRC00 PARM in JCL"))
        for (psb,) in conn.execute("SELECT DISTINCT psb FROM transaction_def WHERE UPPER(program)=? AND psb IS NOT NULL", (pname,)):
            cands.append((psb.upper(), "MPP", "IMS stage-1 APPLCTN"))
        if conn.execute("SELECT 1 FROM ims_psb WHERE UPPER(name)=?", (pname,)).fetchone():
            cands.append((pname, None, "PSB named as the program (convention)"))
        chosen = None
        for psb_name, region, how in cands:
            row = conn.execute("SELECT id, io_pcb_first FROM ims_psb WHERE UPPER(name)=? ORDER BY id", (psb_name,)).fetchone()
            if row:
                chosen = (psb_name, region, how, row)
                break
        if not chosen:
            why = ("no PSB found: no DFSRRC00 step runs it, no stage-1 APPLCTN names it, no PSB member named like it"
                   if not cands else f"PSB {cands[0][0]} ({cands[0][2]}) is not indexed")
            conn.execute("UPDATE dli_call SET pcb_source=? WHERE program_id=?", (why, pid))
            continue
        psb_name, region, how, row = chosen
        others = sorted({c[0] for c in cands if c[0] != psb_name})
        pcbs = conn.execute("SELECT ordinal, pcb_type, dbd_name, procopt, list_no FROM ims_pcb WHERE psb_id=? ORDER BY ordinal",
                            (row[0],)).fetchall()
        io_first = row[1]
        if io_first is None and region:
            io_first = region in ("BMP", "MPP", "IFP", "JBP", "JMP")
        positions: Dict[int, Optional[sqlite3.Row]] = {}
        pos = 1
        if io_first:
            positions[1] = None
            pos = 2
        for pcb in pcbs:
            if pcb["list_no"]:
                continue
            positions[pos] = pcb
            pos += 1
        by_ordinal = {pcb["ordinal"]: pcb for pcb in pcbs}
        tail = f" ({how}{', I/O PCB first' if io_first else ''}{'; other PSBs seen: ' + ', '.join(others) if others else ''})"
        for d in conn.execute("SELECT id, pcb_arg, pcb_index, func FROM dli_call WHERE program_id=?", (pid,)).fetchall():
            pcb = None
            src = None
            if d["pcb_index"]:
                pcb = by_ordinal.get(d["pcb_index"])
                src = f"EXEC DLI PCB({d['pcb_index']}) = PSB {psb_name} PCB #{d['pcb_index']}" + tail
            elif d["pcb_arg"] and d["pcb_arg"] in using:
                p_pos = using.index(d["pcb_arg"]) + 1
                if p_pos in positions:
                    pcb = positions[p_pos]
                    src = (f"USING position {p_pos} = " + ("the I/O PCB" if pcb is None else f"PSB {psb_name} PCB #{pcb['ordinal']}")
                           + tail)
                else:
                    src = f"USING position {p_pos} is beyond the {len(positions)} PCB(s) of PSB {psb_name}" + tail
            elif d["pcb_arg"]:
                src = f"{d['pcb_arg']} is not in PROCEDURE DIVISION / ENTRY USING - position unknown (AIB by name?)" + tail
            else:
                src = f"no PCB argument" + tail
            if pcb is not None:
                conn.execute("UPDATE dli_call SET pcb_ordinal=?, psb_name=?, dbd_name=?, procopt=?, pcb_source=? WHERE id=?",
                             (pcb["ordinal"], psb_name, pcb["dbd_name"], pcb["procopt"], src, d["id"]))
                n += 1
                if pcb["pcb_type"] == "GSAM" and pcb["dbd_name"] and d["func"] in ("ISRT", "GN", "GHN", "GU"):
                    # A GSAM PCB is a sequential file: the DBD's DD1 (or the
                    # DBD name) is the JCL DD the program writes / reads.
                    dd = conn.execute("SELECT dd1 FROM ims_dbd WHERE UPPER(name)=?", (pcb["dbd_name"],)).fetchone()
                    ddname = (dd[0] if dd and dd[0] else pcb["dbd_name"]).upper()
                    mode = "output" if d["func"] == "ISRT" else "input"
                    conn.execute("""UPDATE dd SET mode=?, mode_source=? WHERE UPPER(dd_name)=? AND mode_source<>'open_verb'
                                    AND step_id IN (SELECT id FROM step WHERE UPPER(effective_pgm)=?)""",
                                 (mode, "gsam_isrt" if mode == "output" else "gsam_gn", ddname, pname))
            elif src and "I/O PCB" in src and pcb is None and d["pcb_arg"] and d["pcb_arg"] in using:
                conn.execute("UPDATE dli_call SET psb_name=?, dbd_name='*IO-PCB*', pcb_source=? WHERE id=?",
                             (psb_name, src, d["id"]))
            else:
                conn.execute("UPDATE dli_call SET psb_name=?, pcb_source=? WHERE id=?", (psb_name, src, d["id"]))
    return n


def summary(ctx: Ctx, db_path: str, root: str, t0: float) -> None:
    conn = ctx.conn
    kinds = conn.execute("SELECT kind, parse_status, COUNT(*) FROM member GROUP BY 1,2 ORDER BY 1,2").fetchall()
    print("\n== members by kind / parse status ==")
    for k, s, c in kinds:
        print(f"  {k:<10} {s:<8} {c:>7}")
    n_prog = conn.execute("SELECT COUNT(*) FROM program").fetchone()[0]
    n_calls = conn.execute("SELECT resolution, COUNT(*) FROM call_edge GROUP BY 1").fetchall()
    n_jobs = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    n_steps = conn.execute("SELECT COUNT(*) FROM step").fetchone()[0]
    n_unres = conn.execute("SELECT kind, COUNT(*) FROM unresolved GROUP BY 1 ORDER BY 2 DESC").fetchall()
    n_amb = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member WHERE distinct_content > 1").fetchone()[0]
    n_dup = conn.execute("SELECT COUNT(*) FROM v_ambiguous_member").fetchone()[0]
    st = ctx.stats
    print(f"\n== this run: new {st.get('new', 0)}  changed {st.get('changed', 0)}  "
          f"unchanged {st.get('unchanged', 0)}  pruned {st.get('pruned', 0)} ==")
    n_tx = conn.execute("SELECT COUNT(*) FROM transaction_def").fetchone()[0]
    print(f"\n== programs {n_prog}   jobs {n_jobs}   steps {n_steps}   transactions {n_tx} ==")
    print("  call edges: " + ", ".join(f"{r}={c}" for r, c in n_calls))
    print(f"\n== unresolved ({sum(c for _k, c in n_unres)}) - these are the known blind spots ==")
    for k, c in n_unres[:15]:
        print(f"  {k:<22} {c:>7}")
    print(f"\n== duplicate member names: {n_dup}  (with DIFFERENT content: {n_amb}) ==")
    if n_amb:
        print("  -> run `python -m atlas.query ambiguous` and declare production libraries in a manifest")
    print(f"\ndb: {db_path}   root: {root}   {time.time() - t0:.1f}s")


def _problems_report(ctx: Ctx) -> None:
    """The end-of-build list: every kind of problem with its count and the
    first names, so nothing printed hours ago is missed."""
    probs = ctx.problems
    if not probs:
        ctx.say("\n== problems in this run: none ==")
        return
    by_kind: Dict[str, List[Tuple[str, str]]] = {}
    for kind, path, detail in probs:
        by_kind.setdefault(kind, []).append((path, detail))
    ctx.say(f"\n== problems in this run: {len(probs):,} member(s) or file(s) not indexed ==")
    for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        ctx.say(f"  {kind}: {len(items):,}")
        for path, detail in items[:PROBLEM_EXAMPLES]:
            ctx.say(f"    {os.path.basename(path)}  ({os.path.basename(os.path.dirname(path))})  {detail[:160]}")
        if len(items) > PROBLEM_EXAMPLES:
            ctx.say(f"    ... and {len(items) - PROBLEM_EXAMPLES:,} more")
    if ctx.problems_file:
        ctx.say(f"  every one of them, with its folder and reason: {ctx.problems_file}")
    ctx.say("  send the problem and detail lines (the toolkit's messages - no content from the estate) to get a fix")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build atlas.db from a mainframe source/document folder.")
    ap.add_argument("root")
    ap.add_argument("--db", default="atlas.db")
    ap.add_argument("--manifest", help="JSON declaring authoritative (production) libraries")
    ap.add_argument("--sched", action="append",
                    help="scheduler export CSV (job_name,depends_on,kind | job_name,system,schedule); repeatable")
    ap.add_argument("--also", action="append", metavar="DIR",
                    help="extra folder to index (documents that are not mainframe datasets); repeatable")
    ap.add_argument("--rebuild", action="store_true", help="delete the db first")
    ap.add_argument("--write-expanded", help="directory to write expanded COBOL sources into")
    ap.add_argument("--limit", type=int, help="index only the first N files (smoke test)")
    ap.add_argument("--skip-list", metavar="FILE", help="members that froze the parser on an earlier run (one path or "
                                                         "file name per line): recorded as failed, not parsed - "
                                                         "atlas.supervise maintains it")
    ap.add_argument("--no-current-file", action="store_true", help="do not write atlas-current.txt beside the db")
    ap.add_argument("--member-limit", type=float, default=MEMBER_TIME_LIMIT, metavar="SECONDS",
                    help=f"give up on one member after this long (default {MEMBER_TIME_LIMIT} s): it is recorded as "
                         "failed and the build goes on")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    say = (lambda msg: None) if args.quiet else (lambda msg: print(msg, flush=True))
    current_file = None if args.no_current_file else os.path.join(os.path.dirname(os.path.abspath(args.db)), CURRENT_FILE)
    progress = Progress(say, current_file=current_file).start()
    try:
        return _build(args, progress, time.time())
    finally:
        progress.stop()


def _build(args: argparse.Namespace, progress: Progress, t0: float) -> int:
    progress.phase("opening the index")
    conn = open_db(args.db, rebuild=args.rebuild)
    ctx = Ctx(conn, quiet=args.quiet)
    ctx.progress = progress
    ensure_fk_indexes(conn, ctx.say)
    ensure_query_indexes(conn, ctx.say)
    ctx.write_expanded = args.write_expanded
    if ctx.write_expanded:
        os.makedirs(ctx.write_expanded, exist_ok=True)
        with open(os.path.join(ctx.write_expanded, OUTPUT_MARKER), "w") as fh:
            fh.write("written by atlas.build --write-expanded; never indexed\n")
    fp = tool_fingerprint()
    man_sha = None
    if args.manifest and os.path.isfile(args.manifest):
        with open(args.manifest, "rb") as fh:
            man_sha = sha(fh.read())[:16]
    # the LATEST run, finished or not: members parsed by an interrupted run
    # were parsed by that run's toolkit, and must be redone if it changed
    last = conn.execute("SELECT fingerprint, manifest_sha, declared_kinds FROM build_run ORDER BY id DESC LIMIT 1").fetchone()
    # the kinds declared per library in the UI's table (the manifest kinds): read BEFORE the inventory types a
    # member, and recorded with the run, so atlas.recover and `coverage` tell a declared kind from a rule of the
    # build's own without the manifest file (LESSONS 199)
    load_declared_kinds(ctx, args.manifest)
    kinds_json = json.dumps(ctx.kind_of, sort_keys=True)
    force_all = False
    if last is not None and not args.rebuild:
        if last["fingerprint"] and last["fingerprint"] != fp:
            force_all = True
            ctx.say("toolkit changed since the last build: every member is re-parsed")
        elif (last["manifest_sha"] or None) != man_sha:
            force_all = True
            ctx.say("manifest changed since the last build: every member is re-parsed")
    # Until the old facts are removed and every member is recorded again, this
    # run carries the PREVIOUS fingerprint: stopped or crashed during the
    # inventory, the next run must still see the toolkit as changed.
    run = conn.execute("INSERT INTO build_run(started_at,root,tool_version,fingerprint,manifest_sha,declared_kinds) "
                       "VALUES(?,?,?,?,?,?)",
                       (time.strftime("%Y-%m-%dT%H:%M:%S"), args.root, VERSION,
                        last["fingerprint"] if force_all else fp, last["manifest_sha"] if force_all else man_sha,
                        last["declared_kinds"] if force_all else kinds_json))
    run_id = run.lastrowid
    conn.commit()                              # a crash leaves a run with no finished_at: visible in `coverage`

    ctx.problems_file = os.path.join(os.path.dirname(os.path.abspath(args.db)), PROBLEMS_FILE)
    try:
        with open(ctx.problems_file, "w", encoding="utf-8") as fh:
            fh.write(f"# atlas build started {time.strftime('%Y-%m-%d %H:%M:%S')}: every member or file that could "
                     "not be indexed, one per line (time, problem, path, detail)\n")
    except OSError:
        ctx.problems_file = None
    skip_paths, skip_names = load_skip_list(args.skip_list)
    ctx.inventory_skips = load_inventory_skips(args.skip_list)
    roots = [args.root, *(args.also or [])]
    ctx.say("inventory: " + ", ".join(roots))
    try:
        inventory(ctx, roots, args.limit, force_all=force_all)
    except ParserStuck:
        conn.rollback()
        return 3
    except KeyboardInterrupt:
        conn.rollback()                        # nothing half-removed is kept; the next run starts the inventory again
        ctx.say("\nstopped by Ctrl+C during the inventory - nothing was changed; run the same command again")
        return 130
    conn.execute("UPDATE build_run SET fingerprint=?, manifest_sha=?, declared_kinds=? WHERE id=?",
                 (fp, man_sha, kinds_json, run_id))
    conn.commit()
    ctx.say(f"  {len(ctx.members)} files: " + ", ".join(
        f"{k[5:]}={v}" for k, v in sorted(ctx.stats.items()) if k.startswith("kind:")))
    progress.phase("manifest and systems")
    if args.manifest:
        apply_manifest(ctx, args.manifest)
    else:
        conn.execute("UPDATE member SET system=NULL, authoritative=0")     # nothing declared
    derive_systems(ctx, args.root)
    for sched_path in args.sched or []:
        load_sched(ctx, sched_path)
    conn.commit()
    # what the compiler listings say each copybook came from (atlas.recover's
    # table, read once) and which dataset each fetched folder holds: the
    # resolver reads both before it guesses (ROADMAP re-parse item 19)
    ctx.listing_sources = load_listing_sources(conn)
    ctx.folder_dataset = load_folder_datasets(conn)
    if ctx.listing_sources:
        ctx.say(f"  copybook libraries named by the compiler listings: {len(ctx.listing_sources):,} (program, copybook) "
                f"row(s) for {len({p for p, _c in ctx.listing_sources}):,} program(s) - read before the resolver guesses")

    # Copybooks first so field rows exist; programs; then everything else -
    # grouped by kind, so the screen says when one kind is done.
    order = {"copybook": 0, "cobol": 1, "proc": 2, "jcl": 3, "dbd": 4, "psb": 5, "doc": 9}
    members = sorted(ctx.members, key=lambda m: (order.get(m.kind, 6), m.kind))
    ok = partial = failed = 0
    slow: List[Tuple[float, str, str]] = []
    member_limit = float(args.member_limit or MEMBER_TIME_LIMIT)
    n_parse = sum(1 for m in members if not m.skip)
    todo_by_kind: Dict[str, int] = {}
    for m in members:
        if not m.skip:
            todo_by_kind[m.kind] = todo_by_kind.get(m.kind, 0) + 1
    progress.phase(f"parsing {n_parse:,} member(s)", limit=member_limit)
    ctx.say(f"  {len(members) - n_parse:,} unchanged and kept; to parse: "
            + (", ".join(f"{KIND_LABELS.get(k, k)} {v:,}" for k, v in todo_by_kind.items()) or "nothing")
            + f". A line every {int(PROGRESS_SECONDS)} s with the time left at the current rate and the member in hand "
              "with its size; a line when a kind is done; Ctrl+C stops cleanly and the same command continues later")
    t_commit = t_start = time.time()
    done = 0
    i = 0
    stopped = False
    cur_kind: Optional[str] = None
    kind_done = 0
    kind_t0 = time.time()

    def status() -> str:
        elapsed = time.time() - t_start
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = _hms((n_parse - done) / rate) if rate > 0 else "?"
        return (f"{i}/{len(members)} - {done}/{n_parse} parsed in {_hms(elapsed)}, {rate:.1f}/s, "
                f"about {eta} left at this rate")

    def kind_line(nxt: Optional[str]) -> None:
        if cur_kind is None:
            ctx.say(f"{_stamp()}  starting with {KIND_LABELS.get(nxt, nxt)} ({todo_by_kind.get(nxt, 0):,})")
            return
        secs = time.time() - kind_t0
        msg = (f"{_stamp()}  {KIND_LABELS.get(cur_kind, cur_kind)} done: {kind_done:,} in {_hms(secs)}"
               + (f" ({kind_done / secs:.1f}/s)" if secs >= 1 else ""))
        if nxt:
            msg += f"; next: {KIND_LABELS.get(nxt, nxt)} ({todo_by_kind.get(nxt, 0):,})"
        ctx.say(msg)

    if skip_paths or skip_names:
        ctx.say(f"skip list: {len(skip_paths) + len(skip_names)} member(s) that froze the parser on an earlier run are "
                f"recorded as failed, not parsed ({args.skip_list})")
    progress.set(status)
    for i, mem in enumerate(members, 1):
        if mem.skip:
            continue                       # unchanged since last build; facts kept
        if mem.kind != cur_kind:
            kind_line(mem.kind)
            cur_kind, kind_done, kind_t0 = mem.kind, 0, time.time()
        if time.time() - t_commit >= 10:
            t_commit = time.time()
            conn.commit()
        handler = HANDLERS.get(mem.kind)
        label = f"{mem.kind} {os.path.basename(mem.path)}"
        if os.path.normcase(os.path.abspath(mem.path)) in skip_paths or os.path.basename(mem.path).upper() in skip_names:
            _clear_facts(conn, mem.id)
            conn.execute("UPDATE member SET parse_status='failed', parse_error=? WHERE id=?",
                         ("ParserStuck: froze the parser on an earlier run - skipped (skip list); report its kind, "
                          "size and shape so the parser can be fixed", mem.id))
            ctx.problem("skipped: froze the parser on an earlier run", mem.path, "skip list",
                        f"{_stamp()}  SKIPPED {label}: froze the parser on an earlier run (skip list)")
            failed += 1
            done += 1
            kind_done += 1
            continue
        progress.now(f"{label} ({_size(mem.nbytes)})", path=mem.path)
        t_member = time.time()
        try:
            run_with_limit(_parse_one, (ctx, mem, handler), member_limit, label)
            held = time.time() - t_member
            if held >= SLOW_MEMBER_SECONDS:
                slow.append((held, mem.kind, os.path.basename(mem.path)))
            if not handler:
                conn.execute("UPDATE member SET parse_status='skipped' WHERE id=?", (mem.id,))
            st = conn.execute("SELECT parse_status FROM member WHERE id=?", (mem.id,)).fetchone()[0]
            ok += st == "ok"
            partial += st == "partial"
        except KeyboardInterrupt:
            # what is parsed is committed and kept; the member being parsed
            # stays 'pending' (its half-written rows go with it) and is
            # parsed again by the next run
            progress.stop()
            conn.commit()
            stopped = True
            ctx.say(f"\nstopped by Ctrl+C at {done}/{n_parse} parsed ({_hms(time.time() - t_start)}). Run the same "
                    f"build command again (without --rebuild) to continue: parsed members are kept, "
                    f"{n_parse - done} remain")
            break
        except ParserStuck:
            # the worker thread is still running inside C code: nothing more
            # can be written safely; what the last tick committed is kept
            progress.stop()
            ctx.say(f"\nSTOPPED: {label} has been parsing for {_hms(member_limit)} and cannot be interrupted - the "
                    f"parser is stuck inside it. Parsed members are kept ({done}/{n_parse}). Move that one file out "
                    f"of the estate (or into a folder holding an `.atlas-output` marker) and run the same build "
                    f"command again; then report its kind, size in KB and line count so the parser can be fixed.")
            return 3
        except Exception as e:                                          # noqa: BLE001
            fatal = fatal_db_error(e)
            if fatal:
                progress.stop()
                try:
                    conn.rollback()
                except sqlite3.DatabaseError:
                    pass
                ctx.say(f"\n{_stamp()}  STOPPED: the index cannot be written ({fatal}) while parsing {label}.\n"
                        f"  Nothing more can be parsed until that is fixed: free space on the drive holding "
                        f"{os.path.abspath(args.db)}, close any other program using it, then run the same command "
                        f"again - members parsed so far are kept ({done:,}/{n_parse:,}).")
                ctx.problems.append(("index cannot be written", mem.path, fatal))
                _problems_report(ctx)
                return 4
            failed += 1
            where = toolkit_where(e)
            _clear_facts(conn, mem.id)
            conn.execute("UPDATE member SET parse_status='failed', parse_error=? WHERE id=?",
                         (f"{type(e).__name__}: {e}"[:480] + (f" [{where}]" if where else ""), mem.id))
            kind_of_problem = "time limit" if isinstance(e, MemberTimeout) else "parse failed"
            ctx.problem(kind_of_problem, mem.path, f"{type(e).__name__}: {e}" + (f" [{where}]" if where else ""),
                        f"{_stamp()}  FAILED {mem.kind} {mem.path}: {type(e).__name__}: {e}"
                        + (f"  [{where}]" if where and not isinstance(e, MemberTimeout) else ""))
        done += 1
        kind_done += 1
        if i % 500 == 0:
            conn.commit()
    conn.commit()
    if stopped:
        conn.close()
        return 130
    if cur_kind is not None:
        kind_line(None)
    ctx.say(f"{_stamp()}  parsing done: {done:,} member(s) in {_hms(time.time() - t_start)}")
    if slow:
        slow.sort(reverse=True)
        ctx.say("  slowest members: " + "; ".join(f"{k} {n} {int(s)} s" for s, k, n in slow[:5]))

    progress.phase("post: DD directions from OPEN verbs")
    if _FREEZE == "PHASE:post":                    # tests only: a step that goes silent
        progress.stop()
        time.sleep(3600)
    if _FAULT == "crash:post":                     # tests only: a step that fails outright
        raise RuntimeError("injected failure in the post pass")
    n = post_open_modes(ctx)
    ctx.say(f"post: {n} DD direction(s) set from OPEN verbs")
    progress.phase("post: DL/I calls mapped to databases through the PSB")
    n = post_pcb_positions(ctx)
    ctx.say(f"post: {n} DL/I call(s) mapped to a database through the PSB")
    conn.execute("UPDATE build_run SET finished_at=?, members=?, ok=?, partial=?, failed=? WHERE id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), len(ctx.members), ok, partial, failed, run_id))
    conn.commit()
    progress.phase("summary")
    summary(ctx, args.db, args.root, t0)
    _problems_report(ctx)
    progress.phase("finished")
    conn.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Any crash becomes a short, redacted atlas-crash.txt you can share."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(argv)
    except Exception as e:                                              # noqa: BLE001
        from .diag import write_crash
        db = "atlas.db"
        for i, a in enumerate(args):
            if a == "--db" and i + 1 < len(args):
                db = args[i + 1]
        crash_path = os.path.join(os.path.dirname(os.path.abspath(db)), "atlas-crash.txt")
        text = write_crash(e, [sys.argv[0], *args], path=crash_path)
        fatal = fatal_db_error(e)
        print(f"\n{_stamp()}  BUILD STOPPED BY AN ERROR: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if fatal:
            print(f"  The index cannot be written ({fatal}): free space on the drive holding {os.path.abspath(db)}, "
                  "close any other program using it, then run the same command again - parsed members are kept.",
                  file=sys.stderr, flush=True)
        else:
            print("  Members parsed so far are kept; the same command continues from there once it is fixed.",
                  file=sys.stderr, flush=True)
        print("\n" + text, file=sys.stderr, flush=True)
        print(f"\nSaved to {crash_path} - paste it to get a fix.", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
