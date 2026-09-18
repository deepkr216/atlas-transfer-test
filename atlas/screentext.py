r"""
atlas.screentext - what the OCR engine read off a recorded screen, checked
against the estate (LESSONS 166).

A shared mainframe screen inside a meeting recording is small and blurred,
and the OCR engine does not fail quietly on it: it invents. `000100` comes
back as `eeeløø`, `WS-RESTART-FLAG` as `KS-RESTART-FLAG` - a field name that
looks real and is not. Nothing in the picture tells the two apart. The index
does: every program, paragraph, field, dataset, job and screen name the
estate has is in atlas.db. So each name-shaped piece the engine read is
checked:

  * known   - a name in the index, a COBOL / JCL / ISPF / SQL keyword, or a
              plain word: kept (an index name is written in its own case);
  * snapped - one or two characters away from exactly one index name, with
              the engine's usual confusions (O for 0, I for 1, S for 5)
              allowed for: written as that name;
  * unsure  - shaped like a name but in no index and no keyword list: kept
              with a trailing `?` when an index was given, so nobody mistakes
              it for a fact;
  * junk    - a letter repeated three times, capitals and small letters mixed
              the way no name is, small letters with digits inside: written
              as `?`.

A line with no known or snapped piece is dropped; so is a line that is
mostly junk. The transcript says how many lines were kept, dropped and
snapped, so the reader knows what was done.
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
from typing import Dict, Iterable, List, Optional, Set, Tuple

# The words a mainframe screen is made of, beyond the estate's own names.
KEYWORDS = set("""
IDENTIFICATION DIVISION PROGRAM-ID AUTHOR ENVIRONMENT CONFIGURATION INPUT-OUTPUT FILE-CONTROL SELECT ASSIGN
ORGANIZATION ACCESS RECORD KEY STATUS DATA FILE WORKING-STORAGE LOCAL-STORAGE LINKAGE SECTION FD SD COPY REPLACING
PIC PICTURE VALUE VALUES COMP COMP-3 COMP-5 BINARY PACKED-DECIMAL DISPLAY OCCURS DEPENDING INDEXED REDEFINES FILLER
USAGE SIGN LEADING TRAILING SEPARATE JUSTIFIED BLANK ZERO ZEROS ZEROES SPACE SPACES HIGH-VALUES LOW-VALUES QUOTE
PROCEDURE USING RETURNING PERFORM VARYING UNTIL THRU THROUGH TIMES END-PERFORM MOVE TO FROM INTO ADD SUBTRACT
MULTIPLY DIVIDE COMPUTE GIVING ROUNDED IF ELSE END-IF THEN EVALUATE WHEN OTHER END-EVALUATE ALSO TRUE FALSE
CALL CANCEL GOBACK STOP RUN EXIT GO CONTINUE NEXT SENTENCE OPEN CLOSE INPUT OUTPUT I-O EXTEND READ WRITE REWRITE
DELETE START AT END NOT INVALID AFTER BEFORE ADVANCING ACCEPT INITIALIZE INSPECT TALLYING CONVERTING STRING
UNSTRING DELIMITED SEARCH ALL SET UP DOWN BY AND OR NOT EQUAL GREATER LESS THAN LENGTH OF FUNCTION SORT MERGE
RELEASE RETURN ASCENDING DESCENDING EXEC SQL CICS DLI END-EXEC INCLUDE SQLCA SQLCODE DECLARE CURSOR FETCH
INSERT UPDATE WHERE ORDER GROUP HAVING COMMIT ROLLBACK TABLE COLUMN VIEW INDEX HOST VARIABLE NULL
JOB EXEC PGM PROC DD DSN DISP SHR OLD NEW MOD CATLG UNCATLG KEEP PASS SYSOUT SYSIN SYSPRINT SYSTSIN SYSTSPRT
SYSUDUMP SYSABEND STEPLIB JOBLIB JCLLIB MEMBER COND PARM REGION TIME CLASS MSGCLASS MSGLEVEL NOTIFY USER
UNIT VOL SER SPACE CYL TRK DCB LRECL RECFM BLKSIZE DSORG DUMMY NULLFILE INTRDR ENDIF PEND LIKE REFDD
OUTLIM DEST HOLD RESTART TYPRUN SCAN RETURN CODE RC MAXCC ABEND ABENDED SYSTEM COMPLETION
MENU UTILITIES COMPILERS OPTIONS HELP EDIT VIEW BROWSE COMMAND SCROLL CSR PAGE HALF MAX COLUMNS COLS COLUMN ROW
TOP BOT BOTTOM LIST DSLIST DATASET DATASETS VOLUME VOLSER MEMBERS PRIMARY OPTION PANEL ISPF TSO SDSF DA I O H ST
JOBNAME JOBID OWNER PRTY QUEUE POS C CLASSES ACTIVE HELD PRINT LOG ENTER END EXIT CANCEL SAVE SUBMIT SUB SUBMITTED
LINE LINES CHANGE FIND LOCATE RESET COPY MOVE REPEAT INSERT NUMBER NUMBERS PROFILE HEX CAPS NULLS RECOVERY
TERMINAL ONLINE BATCH PROGRAM PROGRAMS JOBS STEP STEPS DDNAME DDNAMES ERROR ERRORS MESSAGE MESSAGES WARNING
COBOL JCL DB2 IMS VSAM GDG QSAM KSDS ESDS RRDS CICS MQ FTP NDM SORT SYNCSORT DFSORT IDCAMS IEBGENER IEFBR14
IKJEFT01 DFSRRC00 DSNUTILB DEFINE CLUSTER REPRO LISTCAT PRINT COUNT SKIP INFILE OUTFILE OUTREC INREC OUTFIL
FIELDS FORMAT CH ZD PD BI EQ NE GT LT GE LE OMIT INCLUDE
PSB PCB DBD SEGMENT SEGM FIELD LCHILD XDFLD PROCOPT GHU GN GNP GU ISRT REPL DLET CHKP XRST ROLB
MAP MAPSET DFHMDF DFHMDI DFHMSD FMT MSG DFLD MFLD DEV DIV DPAGE LPAGE SEG
TRANSACTION TRANS TRANSID TRAN APPLID TERMID USERID USERNAME PASSWORD DATE TIME ACCT
""".split())

_WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "words.txt")
_NAME_COLUMNS = ("name", "program_id", "dsn", "dd_name", "job_name", "step_name", "pgm", "effective_pgm", "target",
                 "proc_name", "proc_called", "qualified", "section")
_NAME_TABLES = ("member", "program", "paragraph", "field", "dataset", "dd", "job", "step", "proc_def", "screen",
                "screen_field", "transaction_def", "db2_object", "db2_column", "ims_dbd", "ims_psb", "ims_segment",
                "ims_field", "call_edge", "cics_program", "cics_file", "sched_job", "program_alias", "field_alias")
MAX_NAMES = 3_000_000
SNAP_RATIO = 0.85          # this close to exactly one index name is that name
# what the engine confuses on a small screen; both sides of a comparison are folded the same way
_FOLD = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "|": "1", "S": "5", "Z": "2", "B": "8", "G": "6"})


def _fold(s: str) -> str:
    return s.upper().translate(_FOLD)


def _words_from_file() -> Set[str]:
    try:
        with open(_WORDS_FILE, encoding="utf-8") as fh:
            return {w.strip().lower() for w in fh if w.strip()}
    except OSError:
        return set()


class Vocabulary:
    """The names the estate has (from the index), the keywords a screen is
    made of, and plain words."""

    def __init__(self, names: Iterable[str] = (), words: Optional[Iterable[str]] = None, from_index: bool = False):
        self.names: Set[str] = {n.upper() for n in names if n}
        self.words: Set[str] = {w.lower() for w in (words if words is not None else _words_from_file())}
        self.from_index = from_index
        self._shapes: Dict[Tuple[int, str], List[str]] = {}
        for n in self.names | KEYWORDS:
            f = _fold(n)
            self._shapes.setdefault((len(f), f[0]), []).append(n)
            if len(f) > 1:
                self._shapes.setdefault((len(f), "$" + f[-1]), []).append(n)

    @classmethod
    def from_db(cls, db_path: str, log=None) -> "Vocabulary":
        names: Set[str] = set()
        conn = sqlite3.connect(db_path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in _NAME_TABLES:
                if table not in tables:
                    continue
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] in _NAME_COLUMNS]
                for col in cols:
                    for (v,) in conn.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"):
                        v = str(v).strip().upper()
                        if not v or len(v) > 60:
                            continue
                        names.add(v)
                        if "." in v or "(" in v:                       # a dataset: its qualifiers and member too
                            names.update(p for p in re.split(r"[.()]", v) if p)
                        if len(names) >= MAX_NAMES:
                            break
        finally:
            conn.close()
        if log:
            log(f"  screens are checked against {len(names):,} names from the index")
        return cls(names, from_index=True)

    def known(self, piece: str) -> bool:
        return piece.upper() in self.names or piece.upper() in KEYWORDS or piece.lower() in self.words

    def snap(self, piece: str) -> Optional[str]:
        """The one index name (or keyword) this piece is a misread of, else None."""
        f = _fold(piece)
        if len(f) < 4:
            return None
        cands: List[str] = []
        for n in (len(f) - 1, len(f), len(f) + 1):
            cands += self._shapes.get((n, f[0]), [])
            cands += self._shapes.get((n, "$" + f[-1]), [])
        best: List[Tuple[float, str]] = []
        seen: Set[str] = set()
        for c in cands:
            if c in seen:
                continue
            seen.add(c)
            sm = difflib.SequenceMatcher(None, f, _fold(c))
            if sm.real_quick_ratio() < SNAP_RATIO or sm.quick_ratio() < SNAP_RATIO:
                continue
            r = sm.ratio()
            if r >= SNAP_RATIO:
                best.append((r, c))
        if not best:
            return None
        best.sort(reverse=True)
        if len(best) > 1 and best[0][0] - best[1][0] < 0.05 and best[0][1] != best[1][1]:
            return None                                                    # two names equally close: no guess
        return best[0][1]


# a name-shaped piece: letters/digits with - _ # @ $ inside, dotted parts allowed (PROD.CLAIMS.SRC)
_PIECE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_#@$]*(?:\.[A-Za-z0-9][A-Za-z0-9\-_#@$]*)*")
_REPEAT = re.compile(r"([A-Za-z])\1\1")


def _junk(piece: str) -> bool:
    if _REPEAT.search(_fold(piece)):
        return True                                                        # eeelee (but OOO is 000: folded first)
    letters = [ch for ch in piece if ch.isalpha()]
    if not letters:
        return False
    upper = sum(ch.isupper() for ch in letters)
    lower = len(letters) - upper
    capitalised = piece[0].isupper() and "".join(letters[1:]).islower()
    if upper and lower and not capitalised:
        return True                                                        # CLmposT, CLA1ms
    if piece.islower() and any(ch.isdigit() for ch in piece) and len(letters) >= 2:
        return True                                                        # el.e3, eee7ee
    return False


class Stats:
    def __init__(self) -> None:
        self.lines_in = self.lines_kept = self.snapped = self.junk = self.unsure = 0

    def __str__(self) -> str:
        return (f"{self.lines_kept} of {self.lines_in} screen lines kept, {self.lines_in - self.lines_kept} dropped as "
                f"unreadable; {self.snapped} misread name(s) snapped to the index, {self.junk} piece(s) unreadable"
                + (f", {self.unsure} name(s) in no index marked ?" if self.unsure else ""))


def clean_line(line: str, vocab: Vocabulary, stats: Optional[Stats] = None) -> Optional[str]:
    """The line as it can be trusted, or None when it cannot."""
    stats = stats or Stats()
    known = names = junk = pieces = 0
    out: List[str] = []
    pos = 0
    for m in _PIECE.finditer(line):
        out.append(line[pos:m.start()])
        pos = m.end()
        piece = m.group(0)
        pieces += 1
        if piece.isdigit():
            out.append(piece)
            continue
        if vocab.known(piece):
            known += 1
            if piece.upper() in vocab.names:
                names += 1
                if not piece.islower() and piece.upper() != piece:
                    piece = piece.upper()
            out.append(piece)
            continue
        up = piece.upper()
        if up in vocab.names or up in KEYWORDS:                           # CLmposT is CLMPOST
            known += 1
            names += up in vocab.names
            out.append(up)
            continue
        hit = vocab.snap(piece) if any(ch.isalpha() for ch in piece) else None
        if hit:                                                            # KS-RESTART-FLAG, IOOO-OPEN-FILES, PM-POLlCY
            stats.snapped += 1
            known += 1
            names += 1
            out.append(hit)
            continue
        if _junk(piece):
            stats.junk += 1
            junk += 1
            out.append("?")
            continue
        if any(ch.isalpha() for ch in piece) and vocab.from_index and len(piece) >= 4 and piece == up:
            stats.unsure += 1
            out.append(piece + "?")                                        # shaped like a name, in no index
            continue
        out.append(piece)
    out.append(line[pos:])
    if not pieces:
        return None
    if junk * 2 >= pieces or (names == 0 and known < 2):
        return None
    return "".join(out)


def clean_screen(text: str, vocab: Vocabulary, stats: Optional[Stats] = None) -> str:
    """Every line of one screen, checked; the lines that cannot be trusted are gone."""
    stats = stats if stats is not None else Stats()
    kept: List[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        stats.lines_in += 1
        good = clean_line(line, vocab, stats)
        if good is not None:
            stats.lines_kept += 1
            kept.append(good)
    return "\n".join(kept)
