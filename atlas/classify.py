"""
classify.py - decide what each file in the folder actually is.

Extensions cannot be trusted. Members downloaded from a PDS frequently arrive
with no extension at all (the library name carried the type), or with an
extension that reflects the download tool rather than the content. Misfiling a
copybook as a program, or a PROC as a job, corrupts the index quietly - so
every file gets a content sniff and the extension is only a hint.

The order: the strong signatures (a JOB card, a PROC, stage-1, CSD, DBD,
PSB, BMS), a compiler listing (it echoes a program), IDENTIFICATION
DIVISION / PROGRAM-ID, REXX, CREATE TABLE, a JCL EXEC; then a COBOL copybook
by its own lines - level numbers, or COBOL procedure statements with no
DIVISION header; then the shape of an Assembler or MFS member; then the
kind declared for the library in the UI's table (declared_wins), the folder
name and the extension (ROADMAP re-parse items 20 and 22, LESSONS 183, 186).
A signature a comment line carries (a remark naming DFHMDF, PROGRAM-ID,
CREATE TABLE or the compiler, a REXX header behind a slash in column 7)
says nothing about the member (LESSONS 189).
"""

from __future__ import annotations

import os
import re
from typing import Optional, Tuple

EXT_HINTS = {
    ".cbl": "cobol", ".cob": "cobol", ".cobol": "cobol", ".ccp": "cobol",
    ".cpy": "copybook", ".copy": "copybook", ".cpy": "copybook", ".inc": "copybook",
    ".jcl": "jcl", ".job": "jcl", ".jcls": "jcl",
    ".prc": "proc", ".proc": "proc",
    ".dbd": "dbd", ".psb": "psb",
    ".bms": "bms", ".mfs": "mfs",
    ".csd": "csd", ".csdup": "csd", ".imsgen": "imsgen", ".stage1": "imsgen",
    ".sql": "sql", ".ddl": "sql", ".dcl": "copybook",
    ".ctl": "ctlcard", ".card": "ctlcard", ".parm": "ctlcard", ".sysin": "ctlcard",
    ".asm": "asm", ".mac": "asm",
    ".rex": "rexx", ".rexx": "rexx",
    ".lst": "listing", ".listing": "listing", ".sysprint": "listing",
    ".txt": "doc", ".md": "doc", ".doc": "doc", ".docx": "doc", ".pdf": "doc",
    ".xls": "doc", ".xlsx": "doc", ".ppt": "doc", ".pptx": "doc", ".csv": "doc",
    ".htm": "doc", ".html": "doc", ".vsd": "doc", ".vsdx": "doc",
}

# Library-name hints: a folder called COPYLIB or PROCLIB tells you the type of
# everything inside it, and is usually more reliable than the file extension.
DIR_HINTS = [
    (re.compile(r"COPY(LIB|BOOK)?S?$", re.I), "copybook"),
    (re.compile(r"PROCLIB$|PROCS?$", re.I), "proc"),
    (re.compile(r"JCL(LIB)?$|JOBS?$", re.I), "jcl"),
    # CNTL / CARDLIB hold control cards; a real job inside one is caught by
    # its JOB/EXEC statements before the folder hint is consulted.
    (re.compile(r"CNTL(LIB)?$|CARD(LIB|S)?$|UTL$", re.I), "ctlcard"),
    # the specific libraries before the generic SRC / SOURCE rule: DBDSRC,
    # PSBSOURCE, BMSSRC and MFSSRC are not COBOL libraries
    (re.compile(r"DBD(LIB|SRC|SOURCE)?$", re.I), "dbd"),
    (re.compile(r"PSB(LIB|SRC|SOURCE)?$", re.I), "psb"),
    (re.compile(r"BMS(LIB|SRC|SOURCE)?$|MAPS?$", re.I), "bms"),
    (re.compile(r"MFS(LIB|SRC|SOURCE|GEN)?$|(^|\.)FORMATS?$|FMTLIB$|MSGLIB$", re.I), "mfs"),
    (re.compile(r"SRC$|SOURCE$|COBOL$|PGM(LIB)?$", re.I), "cobol"),
    (re.compile(r"CTL(CARDS?)?$|PARM(LIB)?$|SYSIN$", re.I), "ctlcard"),
    (re.compile(r"DOCS?$|DOCUMENT.*$|SPECS?$|DESIGN$", re.I), "doc"),
]

BINARY_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".vsdx", ".zip", ".gz",
               ".png", ".jpg", ".jpeg", ".gif", ".bin", ".load", ".obj"}

# ---- content signatures ---------------------------------------------------

# Line-anchored signatures use `[ \t]`, never `\s`: with re.M a `\s` runs
# across blank lines, and a card deck padded with blank 80-column lines
# made the assembler signature cubic - 101 blank cards held the whole
# process for 70 s (LESSONS 148).
_SIG_COBOL = re.compile(r"^[ \t]*(IDENTIFICATION|ID)[ \t]+DIVISION", re.I | re.M)
_SIG_PROGRAM_ID = re.compile(r"\bPROGRAM-ID\b", re.I)
_SIG_JOB = re.compile(r"^//\S{1,8}\s+JOB\b", re.M)
_SIG_PROC = re.compile(r"^//\S{0,8}\s+PROC\b", re.M)
_SIG_PEND = re.compile(r"^//\S{0,8}\s+PEND\b", re.M)
_SIG_EXEC = re.compile(r"^//\S{0,8}\s+EXEC\b", re.M)
# HLASM macro format: an optional label in column 1, then the operation. A
# pattern anchored on leading whitespace misses every labelled statement -
# which is most of them in DBD/PSB source.
_SIG_DBD = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?[ \t]+DBD[ \t]+NAME=", re.I | re.M)
_SIG_SEGM = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?[ \t]+SEGM[ \t]+NAME=", re.I | re.M)
_SIG_PSB = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?[ \t]+PSBGEN\b|^(?:[A-Z0-9@#$]{1,8})?[ \t]+PCB[ \t]+TYPE=",
                      re.I | re.M)
_SIG_BMS = re.compile(r"\bDFHMSD\b|\bDFHMDI\b|\bDFHMDF\b", re.I)
# CICS CSD extract/upload deck (DEFINE form) or DFHCSDUP LIST report form.
_SIG_CSD = re.compile(r"^[ \t]*(?:DEFINE|ALTER|USERDEFINE)[ \t]+(?:TRANSACTION|PROGRAM|FILE|MAPSET|TDQUEUE)\("
                      r"|^[ \t]*(?:TRANSACTION|PROGRAM|FILE)\([A-Z0-9@#$]+\)[ \t]+GROUP\(", re.I | re.M)
# IMS stage-1 system definition: APPLCTN / TRANSACT macros (label optional in col 1).
_SIG_IMSGEN = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?[ \t]+(?:APPLCTN|TRANSACT)[ \t]+(?:PSB|GPSB|CODE)=", re.I | re.M)
_SIG_SQL_DDL = re.compile(r"\bCREATE\s+(TABLE|VIEW|INDEX|TABLESPACE|DATABASE)\b", re.I)
_SIG_REXX = re.compile(r"^\s*/\*\s*REXX", re.I)
# A folder named like a dataset (PROD.CLAIMS.SRC) holds mainframe members:
# an unrecognised member there is UNKNOWN, never a document.
_DATASET_FOLDER = re.compile(r"^[A-Z0-9$#@]{1,8}(?:\.[A-Z0-9$#@]{1,8})+$")

# ---- a COBOL copybook by its own lines -------------------------------------
#
# A COBOL copybook is typed by its own lines before the Assembler and MFS
# shapes and before the folder name (ROADMAP re-parse items 20 and 22,
# LESSONS 183 and 186): level numbers (a data copybook) or COBOL procedure
# statements (a procedure copybook - his `A-100-BEGIN SECTION.  COPY
# PROCBOOK.` copies paragraphs and statements, no level numbers, no DIVISION
# header). Before, a procedure copybook had no signature and the FOLDER NAME
# typed it (PROCS made it a proc, CNTL a control card, a plain folder a
# document); and three weak signatures ran before the level numbers - a line
# whose first or second word BEGAN with START / CSECT / DSECT read as
# Assembler (`05 START-DATE PIC X(8).`, `PERFORM START-PARA.`), a comment
# naming MODULE MAP as a compiler listing, a line whose first word was MSG as
# MFS source - so the resolver never saw the copybook and every program
# copying it said COPY X NOT FOUND with the member on disk.
#
# Where a COBOL line's code begins: columns 1-6 hold the sequence area (a
# number, blanks or a change tag), column 7 the indicator - blank, or D for a
# debugging line - and the code starts in column 8; a member with no sequence
# area starts it after blanks. Never on a JCL line (`//`, `/*`), an Assembler
# comment (`*` in column 1) or a macro comment (`.*`); and never in column 1,
# where a prose line or an Assembler label starts.
_CODE_AT = r"^(?!//|/\*|\*|\.\*)(?:[^\n]{6}[ Dd][ \t]*|[ \t]+)"
_LEVEL_NO = r"(?:0[1-9]|[1-4][0-9]|66|77|88)"
_DATA_CLAUSE = (r"(?:PIC|PICTURE|VALUES?|REDEFINES|OCCURS|USAGE|COMP(?:UTATIONAL)?(?:-[1-6X])?|BINARY|PACKED-DECIMAL|"
                r"DISPLAY(?:-1)?|NATIONAL|INDEX|POINTER|SIGN|JUST(?:IFIED)?|SYNC(?:HRONIZED)?|BLANK|RENAMES|EXTERNAL|"
                r"GLOBAL|COPY)(?![A-Z0-9\-])")
# A data description entry: a level number where a COBOL line's code begins, then a data-name ended by a period,
# the end of the line or a clause - or a clause at once (`05 PIC X.`). A level number alone is not enough this
# early: `R12      EQU   12` (the register equates of nearly every Assembler member) and `LBL12345 DS F` carry
# a number after a label; the looser signature below, checked after the Assembler shape as before, takes the rest.
_SIG_DATA_LEVEL = re.compile(_CODE_AT + _LEVEL_NO + r"[ \t]+(?:" + _DATA_CLAUSE + r"|(?=[A-Z0-9\-]*[A-Z])[A-Z0-9][A-Z0-9\-]*"
                             r"(?:[ \t]*\.|[ \t]*$|[ \t]+" + _DATA_CLAUSE + r"))", re.I | re.M)
# Any level 01-49 plus 66/77/88. Level 49 is the DCLGEN VARCHAR structure and
# 02/03/04/06/07/15/20 are all common; testing only 01/05/10 misfiles them.
_SIG_LEVEL_NUMBER = re.compile(r"^.{0,6}.?[ \t]*(0[1-9]|[1-4]\d|66|77|88)[ \t]+[A-Z0-9][A-Z0-9\-]*", re.I | re.M)
# COBOL procedure statements no Assembler, listing or MFS member carries (upper case, as mainframe COBOL is
# written): MOVE ... TO, ADD ... TO and the other arithmetic verbs with their preposition, SET ... TO, PERFORM,
# GOBACK, GO TO, EVALUATE, STOP RUN, EXIT., NEXT SENTENCE, CONTINUE, COMPUTE, INITIALIZE, ACCEPT ... FROM,
# STRING / UNSTRING ... DELIMITED, INSPECT ... TALLYING, OPEN INPUT, the COBOL forms of CLOSE / READ / WRITE (an
# Assembler OPEN / CLOSE / READ / WRITE macro takes parentheses or commas, a CLIST WRITE no period), a scope
# terminator (END-IF ...) - and a paragraph or section name in area A (columns 8-11) ended by a period
# (`S-110-DO.`, `A-100-BEGIN SECTION.  COPY X.`).
_NAME = r"[A-Z][A-Z0-9\-]*"
_COBOL_VERB = (r"(?:MOVE[ \t][^\n]*?[ \t]TO(?:[ \t]|$)"
               r"|(?:ADD|SUBTRACT|MULTIPLY|DIVIDE)[ \t][^\n]*?[ \t](?:TO|FROM|BY|INTO|GIVING)(?:[ \t]|$)"
               r"|SET[ \t][^\n]*?[ \t]TO(?:[ \t]|$)|ACCEPT[ \t][^\n]*?[ \t]FROM(?:[ \t]|$)"
               r"|(?:UN)?STRING[ \t][^\n]*?[ \t]DELIMITED(?:[ \t]|$)"
               r"|INSPECT[ \t][^\n]*?[ \t](?:TALLYING|REPLACING|CONVERTING)(?:[ \t]|$)"
               r"|PERFORM(?:[ \t.]|$)|GOBACK(?:[ \t.]|$)|GO[ \t]+TO(?:[ \t.]|$)|EVALUATE[ \t]|STOP[ \t]+RUN(?:[ \t.]|$)"
               r"|EXIT(?:[ \t]+(?:PROGRAM|PARAGRAPH|SECTION|PERFORM(?:[ \t]+CYCLE)?))?[ \t]*\.(?:[ \t]|$)"
               r"|NEXT[ \t]+SENTENCE(?:[ \t.]|$)|CONTINUE(?:[ \t.]|$)|COMPUTE[ \t][^\n]*=|INITIALIZE[ \t]+[A-Z0-9]"
               r"|OPEN[ \t]+(?:INPUT|OUTPUT|I-O|EXTEND)(?:[ \t]|$)|CLOSE[ \t]+" + _NAME + r"(?:[ \t]*\.?[ \t]*$|[ \t]+[A-Z])"
               r"|READ[ \t]+" + _NAME + r"(?:[ \t]+(?:NEXT|INTO|RECORD|KEY|AT|INVALID)(?:[ \t]|$)|[ \t]*\.?[ \t]*$)"
               r"|(?:RE)?WRITE[ \t]+" + _NAME + r"(?:[ \t]+(?:FROM|AFTER|BEFORE|INVALID)(?:[ \t]|$)|[ \t]*\.)"
               r"|END-(?:IF|PERFORM|EVALUATE|READ|WRITE|REWRITE|START|DELETE|CALL|EXEC|COMPUTE|STRING|UNSTRING|SEARCH|"
               r"RETURN|ADD|SUBTRACT|MULTIPLY|DIVIDE)(?:[ \t.]|$))")
_PARAGRAPH = (r"^(?!//|/\*|\*|\.\*)[^\n]{6}[ Dd] {0,3}(?=[A-Z0-9\-]*[A-Z])[A-Z0-9][A-Z0-9\-]*(?:[ \t]+SECTION)?[ \t]*\."
              r"(?:[ \t]|$)")
_SIG_COBOL_PROC = re.compile(_CODE_AT + _COBOL_VERB + "|" + _PARAGRAPH, re.M)
# COBOL statements an Assembler member uses as well - the CALL and COPY instructions, the structured-programming
# IF, EXEC CICS / EXEC SQL in a CICS or DB2 Assembler program - count only where the Assembler shape did not fire.
# Neither the IDCAMS `IF LASTCC` / `IF MAXCC`, a CLIST `IF &RC`, ICETOOL's `DISPLAY FROM(`, the IEBCOPY and
# ICETOOL `COPY OUTDD=` / `COPY FROM(`, nor a TSO `CALL 'LIB(PGM)'` has the COBOL form.
_SHARED_VERB = (r"(?:IF[ \t]+(?!LASTCC\b|MAXCC\b|&)[^\n;]*$|DISPLAY[ \t]+(?!FROM\()[^\n;]*$"
                r"|CALL[ \t]+(?:'[A-Z0-9@#$\-]{1,30}'|\"[A-Z0-9@#$\-]{1,30}\"|[A-Z][A-Z0-9\-]*)"
                r"(?:[ \t]*\.?[ \t]*$|[ \t]+(?:USING|RETURNING|ON|END-CALL)\b)"
                r"|EXEC[ \t]+(?:CICS|SQL|SQLIMS|DLI)\b"
                r"|COPY[ \t]+['\"]?[A-Z0-9@#$][A-Z0-9@#$\-]*['\"]?(?:[ \t]*\.|[ \t]*$|[ \t]+(?:OF|IN|REPLACING|SUPPRESS)\b))")
_SIG_COBOL_SHARED = re.compile(_CODE_AT + _SHARED_VERB, re.M)
# a DIVISION header on a code line: such a member is a program's part, not a procedure copybook
_SIG_DIVISION = re.compile(_CODE_AT + r"(?:IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)[ \t]+DIVISION\b", re.I | re.M)

# ---- the Assembler, listing and MFS shapes ---------------------------------
#
# The shape a REAL member of each of these kinds has and a COBOL copybook
# never has, proven by atlas.recover's re-file on his estate before it moved
# here (LESSONS 186-189): CSECT / DSECT as the operation after a label of any
# length or none (HLASM labels run to 63 characters: `PLLONGLABEL CSECT`, an
# unlabelled `CSECT`); START with a label in column 1 (the Assembler START
# names its control section there) or with a numeric or quoted operand
# (` START X'100'`) - never unlabelled with a bare symbol or alone, which is
# the COBOL verb (`START CUSTFILE`, its KEY IS on the next line - LESSONS
# 189); DFHEIENT; DS / DC with a type, the address constants A, V, S, Q and
# AL2 / VL4 included; `USING *`, `EQU *`, `BR 14`.
_ASM_LABEL = r"(?:[A-Z@#$_][A-Z0-9@#$_]*)?"                   # an HLASM label in column 1: any length, or none
_ASM_LABEL_REQ = r"[A-Z@#$_][A-Z0-9@#$_]*"                     # ... required: the CSECT name before START
_ASM_SHAPE = re.compile(
    rf"^{_ASM_LABEL}[ \t]+(?:CSECT|DSECT)(?:[ \t]|$)"                                                   # the exact word: not CSECT-NAME
    rf"|^{_ASM_LABEL_REQ}[ \t]+START(?:[ \t]+[^ \t\n].*)?[ \t]*$"
    rf"|^[ \t]+START[ \t]+(?:\d+|[XBC]'[^']*')(?:[ \t]+.*)?[ \t]*$"
    rf"|^{_ASM_LABEL}[ \t]+DFHEIENT\b"
    rf"|^{_ASM_LABEL}[ \t]+D[SC][ \t]+\d*[ABCDEFHPXZVSQ]L?\d*(?:'|\(|[ \t]|$)"
    rf"|^{_ASM_LABEL}[ \t]+(?:USING|EQU)[ \t]+\*"
    rf"|^{_ASM_LABEL}[ \t]+(?:BR|BALR|BASR)[ \t]+R?1[45]\b", re.I | re.M)
ASM_SHAPES = ("CSECT or DSECT as the operation, with a label of any length or none; START with a label in column 1, or with a "
              "numeric or quoted operand; DFHEIENT; DS / DC with a type, address constants included; USING * or EQU *; BR 14")
# MFS: a statement with a label in column 1, TYPE= / POS= / LTH= operands, MSGEND / FMTEND - never a line whose
# first word is MSG alone (a COBOL section named MSG)
_MFS_SHAPE = re.compile(r"^[A-Z@#$][A-Z0-9@#$]{0,7}[ \t]+(?:MSG|FMT|DEV|DFLD|MFLD)\b"
                        r"|[ \t](?:MSG|DEV)[ \t]+TYPE=|[ \t]DFLD[ \t]+(?:POS|LTH)=|^[ \t]+(?:MSGEND|FMTEND)\b", re.I | re.M)
MFS_SHAPES = "a labelled MSG / FMT / DEV / DFLD / MFLD, TYPE= / POS= / LTH= operands, MSGEND / FMTEND"
# A compiler listing echoes the source: without this it is filed as a second copy of the program, with line-number
# prefixes as fields - so it is looked for BEFORE the COBOL signatures. Its shape: the compiler's banner on a line
# that is not a COBOL comment (a copybook's comment may name the compiler), a map heading at the start of a line
# (a comment naming MODULE MAP is not one), or numbered source lines - the listing's line number, then the source
# record with its OWN sequence number in columns 1-6: two numbers, where a plain source record has one (one line
# at a time).
_LISTING_SHAPE = re.compile(r"^[ 01\-+]?\s*\d{6}[^\s\d]*\s+\d{6}[ *\-/D]")
_LISTING_HEAD = re.compile(r"^[ \t\f]*LineID[ \t]+PL[ \t]+SL\b|IBM Enterprise COBOL|^1?PP[ \t]+5655-", re.I | re.M)
_LISTING_MAP = re.compile(r"^1?[ \t]*(?:DATA DIVISION MAP|MODULE MAP|CROSS REFERENCE TABLE)\b", re.I | re.M)
LISTING_LINES = 3                                               # numbered source lines that make a listing
LISTING_EXTS = (".lst", ".listing", ".sysprint")
LISTING_SHAPES = "the compiler's banner, a map heading, or numbered source lines"

# The kinds a SHAPE decides: the only kinds a line of a COBOL copybook could once trip by a weak signature
# (LESSONS 186). The kind declared for a library in the UI's table wins over them (declared_wins).
WEAK_KINDS = ("asm", "listing", "mfs")
# what build.load_declared_kinds accepts from the manifest kinds (the UI's table)
DECLARABLE = ("cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs", "csd", "imsgen", "sql",
              "listing", "doc", "sched")

REASON_DATA = "COBOL level numbers with no PROCEDURE DIVISION"
REASON_PROC = "COBOL procedure statements with no level numbers and no DIVISION header"
REASON_ASM = "the shape of an Assembler member"
REASON_MFS = "the shape of MFS source"
REASON_LISTING = "compiler listing (source echo, not the source)"


def _is_comment_line(line: str) -> bool:
    """A COBOL comment - an asterisk or a slash in column 7, after a
    sequence area that holds neither (`     /* REXX */` is not one) - or a
    line beginning with an asterisk."""
    return line.lstrip().startswith("*") or (len(line) > 6 and line[6] in "*/" and not any(c in "*/" for c in line[:6]))


def _code_hit(rx: "re.Pattern", head: str):
    """The first match of `rx` that is not on a comment line, or None: a
    signature a comment carries says nothing about the member - a COBOL
    copybook whose remark names DFHMDF, PROGRAM-ID or CREATE TABLE, or a
    REXX header behind a slash in column 7 (LESSONS 189)."""
    for m in rx.finditer(head):
        at = m.start() + len(m.group(0)) - len(m.group(0).lstrip())     # `^\s*/\*` may begin on a blank line above
        if not _is_comment_line(_line_of(head, at)):
            return m
    return None


def _line_of(text: str, pos: int) -> str:
    end = text.find("\n", pos)
    return text[text.rfind("\n", 0, pos) + 1:end if end >= 0 else len(text)]


def normalized(head: str) -> str:
    """The text with CR LF and CR line ends made LF: the signatures are
    line-anchored, and a member downloaded with CR LF must not slip past a
    `$`. Line numbers count the same."""
    return head.replace("\r\n", "\n").replace("\r", "\n")


def listing_hit(head: str) -> Optional[Tuple[int, str]]:
    """(offset, words) of what makes `head` (normalized) a compiler listing -
    the compiler's banner on a line that is not a comment, a map heading at
    the start of a line, or the first of LISTING_LINES numbered source
    lines - or None."""
    m = _code_hit(_LISTING_HEAD, head)
    if m:
        return m.start(), m.group(0).strip()
    m = _LISTING_MAP.search(head)
    if m:
        return m.start(), m.group(0).strip().lstrip("1").strip()
    first, n, pos = -1, 0, 0
    for line in head.split("\n"):
        if _LISTING_SHAPE.match(line):
            n += 1
            if first < 0:
                first = pos
            if n >= LISTING_LINES:
                return first, "numbered source lines"
        pos += len(line) + 1
    return None


def cobol_proc_hit(head: str, shared: bool = False):
    """The first COBOL procedure statement or paragraph name in `head`
    (normalized) as a match - with `shared`, one of the forms an Assembler
    member uses as well - or None; None too when the text carries a DIVISION
    header (a program's part, not a procedure copybook)."""
    m = (_SIG_COBOL_SHARED if shared else _SIG_COBOL_PROC).search(head)
    if m and _SIG_DIVISION.search(head):
        return None
    return m


def reading(path: str, head: str) -> Tuple[str, str, str]:
    """(kind, reason, basis): what the text, the folder name and the
    extension say, before any declared kind. `basis`: 'binary'; 'strong' (a
    JOB card, a PROC, stage-1, CSD, DBD, PSB, BMS, IDENTIFICATION DIVISION /
    PROGRAM-ID, a REXX header, CREATE TABLE, a JCL EXEC); 'cobol' (level
    numbers or COBOL statements: a copybook); 'weak' (the shape of an
    Assembler, listing or MFS member, or a listing's extension); 'folder';
    'extension'; 'none' (unknown)."""
    ext = os.path.splitext(path)[1].lower()
    parent = os.path.basename(os.path.dirname(path))

    if ext in BINARY_EXTS:
        return EXT_HINTS.get(ext, "binary"), f"binary extension {ext}", "binary"
    head = normalized(head)

    # ---- the strong content signatures, in specificity order --------------
    if _SIG_JOB.search(head):
        return "jcl", "contains a JOB card", "strong"
    if _SIG_PROC.search(head) and _SIG_PEND.search(head):
        return "proc", "instream PROC with PEND", "strong"
    if _SIG_PROC.search(head):
        return "proc", "PROC statement", "strong"
    if _SIG_IMSGEN.search(head):
        return "imsgen", "APPLCTN/TRANSACT macro (IMS stage-1)", "strong"
    if _SIG_CSD.search(head):
        return "csd", "CICS CSD DEFINE / LIST resource block", "strong"
    if _SIG_DBD.search(head) or _SIG_SEGM.search(head):
        return "dbd", "DBD/SEGM macro", "strong"
    if _SIG_PSB.search(head):
        return "psb", "PSBGEN/PCB macro", "strong"
    if _code_hit(_SIG_BMS, head):
        return "bms", "DFHMSD/DFHMDI macro", "strong"
    # a listing echoes the program - its PROGRAM-ID, its level numbers: before every COBOL signature
    if ext in LISTING_EXTS or listing_hit(head):
        return "listing", REASON_LISTING, "weak"
    if _SIG_COBOL.search(head) or _code_hit(_SIG_PROGRAM_ID, head):
        return "cobol", "IDENTIFICATION DIVISION / PROGRAM-ID", "strong"
    if _code_hit(_SIG_REXX, head):
        return "rexx", "REXX comment header", "strong"
    if _code_hit(_SIG_SQL_DDL, head):
        return "sql", "CREATE TABLE/VIEW", "strong"
    if _SIG_EXEC.search(head):
        return "jcl", "EXEC statement without a JOB card (JCL fragment)", "strong"

    # ---- a COBOL copybook by its own lines (ROADMAP re-parse items 20, 22) -
    if _SIG_DATA_LEVEL.search(head):
        return "copybook", REASON_DATA, "cobol"
    if cobol_proc_hit(head):
        return "copybook", REASON_PROC, "cobol"

    # ---- the Assembler and MFS shapes -------------------------------------
    if _ASM_SHAPE.search(head):
        return "asm", REASON_ASM, "weak"
    if cobol_proc_hit(head, shared=True):
        return "copybook", REASON_PROC, "cobol"
    if _MFS_SHAPE.search(head):
        return "mfs", REASON_MFS, "weak"
    if _SIG_LEVEL_NUMBER.search(head):
        return "copybook", REASON_DATA, "cobol"

    # ---- fall back to the library folder name -----------------------------
    for rx, kind in DIR_HINTS:
        if rx.search(parent):
            return kind, f"library folder {parent}", "folder"

    # ---- last resort: the extension ---------------------------------------
    if ext in EXT_HINTS:
        if EXT_HINTS[ext] == "doc" and _DATASET_FOLDER.match(parent.upper()):
            # A .txt in PROD.CLAIMS.BIND is a mainframe member the toolkit
            # cannot type (BIND cards, DBRM, PL/I ...) - not a specification.
            return "unknown", f"unrecognised member of library {parent} (extension {ext} ignored)", "none"
        return EXT_HINTS[ext], f"extension {ext}", "extension"

    return "unknown", "no signature, no library hint, no extension", "none"


def declared_wins(kind: str, basis: str, declared: Optional[str]) -> bool:
    """Whether the kind declared for the member's library in the UI's table
    (sources.json -> the manifest kinds) replaces what reading() said. It
    replaces 'unknown' always and - unless it is 'cobol' - a shape (asm /
    listing / mfs), the folder name and the extension: 'declare the
    library's kind' must help a member any of those typed (ROADMAP re-parse
    item 22). A strong signature and a COBOL copybook's own lines win over
    any declaration. 'cobol' replaces 'unknown' only: every COBOL program
    carries a PROGRAM-ID, which is checked first, so a member a shape, a
    folder name or an extension typed is never one - and 'cobol' is the kind
    the UI's table gives a library by default (a new row, every ...SRC
    name), so a mixed source library's Assembler members, or a listing
    library left at the default, would be filed as programs."""
    if not declared or declared == kind or declared not in DECLARABLE:
        return False
    if basis == "none":
        return True
    return declared != "cobol" and basis in ("weak", "folder", "extension")


def classify(path: str, head: str, ext_hint: Optional[str] = None, declared: Optional[str] = None) -> Tuple[str, str]:
    """Return (kind, reason). `head` is the first ~8KB of decoded text;
    `declared` the kind declared for the member's library in the UI's table
    (the build passes it; declared_wins() says when it counts)."""
    kind, reason, basis = reading(path, head)
    if declared_wins(kind, basis, declared):
        parent = os.path.basename(os.path.dirname(path))
        return declared, (f"the kind declared for library {parent} in the UI's table"
                          + ("" if basis == "none" else f", over {kind} ({reason})"))
    return kind, reason
