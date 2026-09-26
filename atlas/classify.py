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
COBOL statements an Assembler or MFS member writes too (COPY, IF, CALL ...);
then the kind declared for the library in the UI's table (declared_wins),
the folder name and the extension (ROADMAP re-parse items 20 and 22,
LESSONS 183, 186, 200). A signature a comment line carries (a remark naming
DFHMDF, PROGRAM-ID, CREATE TABLE or the compiler, a REXX header behind a
slash in column 7) says nothing about the member (LESSONS 189).

The COBOL statements are words other texts use too, so the library says
how much they need to say (LESSONS 199, 200): an Easytrieve program or a
Connect:Direct process - read by the jobs through SYSIN - is neither a
copybook nor typed by them at all; in a library of documents (its folder
name, its declared kind, a document's extension other than .txt, or a .txt
with a line of prose) they count for nothing - a run book says PERFORM THE
FOLLOWING STEPS, a design note quotes a paragraph; in a library of control
cards (its folder name or its declared kind) two statement lines are
needed, one of a form no card language has; in a library of Assembler or
macro source (MFS, BMS, DBD, PSB, stage-1 - its declared kind, folder name
or extension) the statements an Assembler member writes too count for
nothing - an MFS member COPYs its device headers.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

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
# PROGRAM-ID as the paragraph's own word: never the tail of a data-name. `\b` matched after the hyphen of
# `05 CA-PROGRAM-ID PIC X(8).` - a field every CICS commarea and audit record carries - and the copybook was filed a
# program (LESSONS 248)
_SIG_PROGRAM_ID = re.compile(r"(?<![A-Z0-9\-])PROGRAM-ID(?![A-Z0-9\-])", re.I)
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
# CREATE TABLE where a statement begins - at the start of a line or after a `;` - never in the middle of one: an
# Assembler remark `BAL 14,BLDTAB  CREATE TABLE OF RATES` or a COBOL literal 'CREATE TABLE' is no DDL (LESSONS 199)
_SIG_SQL_DDL = re.compile(r"(?:^|;)[ \t]*CREATE\s+(?:(?:UNIQUE|LOB|AUX|AUXILIARY|LARGE|GLOBAL\s+TEMPORARY)\s+)?"
                          r"(TABLE|VIEW|INDEX|TABLESPACE|DATABASE)\b", re.I | re.M)
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
# A data-name, with a letter in it: `WS-ID`, and the tagged `WS-:XR:-ID` / `:XR:-REC` a program's COPY ... REPLACING
# ==:XR:== BY ==...== fills in (a tag is a word between two colons, so a prose `NOTE:` is none - LESSONS 200)
_DATA_NAME = r"(?=[A-Z0-9\-:@#$]*[A-Z])(?:[A-Z0-9]|:[A-Z0-9@#$\-]+:)(?:[A-Z0-9\-]|:[A-Z0-9@#$\-]+:)*"
# A data description entry: a level number where a COBOL line's code begins, then a data-name ended by a period,
# the end of the line or a clause - or a clause at once (`05 PIC X.`). A level number alone is not enough this
# early: `R12      EQU   12` (the register equates of nearly every Assembler member) and `LBL12345 DS F` carry
# a number after a label; the looser signature below, checked after the Assembler shape as before, takes the rest.
# A copybook written from column 1, with no sequence area (`05 WS-ID PIC X(10).`), is one by a level number in
# column 1 with a data-name and a PIC, VALUE, REDEFINES, OCCURS or USAGE clause - no card, prose or Assembler line
# starts so (an Assembler label never starts with a digit; `01 20260925` has no data-name) - LESSONS 200.
# A clause WITH its operand, as a data description entry writes it: a picture string after PIC (one with a 9, X, A,
# Z or N in it), a literal, a number or a figurative constant after VALUE, a data-name after REDEFINES, a number
# after OCCURS, a usage after USAGE. A prose heading `02 Premium values` (a level-like number, a word, then the word
# VALUES and nothing more) filed a document as a copybook (LESSONS 248): the column-1 form below, and every entry in
# a library of documents (_SIG_DATA_ENTRY), need the operand.
_CLAUSE_OPERAND = (r"(?:PIC(?:TURE)?(?:[ \t]+IS)?[ \t]+(?=[^ \t\n]*[9XAZN])[-+$*.,/()0-9ABEGNPSVXZ]+(?:[ \t.]|$)"
                   r"|VALUES?(?:[ \t]+(?:IS|ARE))?[ \t]+(?:'|\"|[-+]?\.?\d|(?:ZEROS?|ZEROES|SPACES?|HIGH-VALUES?|"
                   r"LOW-VALUES?|QUOTES?|NULLS?|ALL)(?![A-Z0-9\-]))"
                   r"|REDEFINES[ \t]+" + _DATA_NAME +
                   r"|OCCURS[ \t]+\d"
                   r"|USAGE(?:[ \t]+IS)?[ \t]+(?:COMP|BINARY|PACKED-DECIMAL|DISPLAY|INDEX|POINTER|NATIONAL))")
_SIG_DATA_LEVEL = re.compile(_CODE_AT + _LEVEL_NO + r"[ \t]+(?:" + _DATA_CLAUSE + r"|" + _DATA_NAME +
                             r"(?:[ \t]*\.|[ \t]*$|[ \t]+" + _DATA_CLAUSE + r"))"
                             r"|^" + _LEVEL_NO + r"[ \t]+" + _DATA_NAME + r"[ \t]+" + _CLAUSE_OPERAND, re.I | re.M)
# ... and in a library of documents: a level number, a data-name and a clause with its operand, where a COBOL line's
# code begins or in column 1 - an indented numbered heading (`   01 Overview`, `   02 Premium values`) is prose
_SIG_DATA_ENTRY = re.compile(r"(?:" + _CODE_AT + r"|^)" + _LEVEL_NO + r"[ \t]+" + _DATA_NAME + r"[ \t]+" +
                             _CLAUSE_OPERAND, re.I | re.M)
# Any level 01-49 plus 66/77/88. Level 49 is the DCLGEN VARCHAR structure and
# 02/03/04/06/07/15/20 are all common; testing only 01/05/10 misfiles them.
_SIG_LEVEL_NUMBER = re.compile(r"^.{0,6}.?[ \t]*(0[1-9]|[1-4]\d|66|77|88)[ \t]+[A-Z0-9][A-Z0-9\-]*", re.I | re.M)
# COBOL procedure statements no Assembler, listing or MFS member carries (upper case, as mainframe COBOL is
# written): MOVE ... TO, ADD ... TO and the other arithmetic verbs with their preposition, SET ... TO, PERFORM,
# GOBACK, GO TO, EVALUATE, STOP RUN, EXIT., NEXT SENTENCE, CONTINUE, COMPUTE, INITIALIZE, ACCEPT ... FROM,
# STRING / UNSTRING ... DELIMITED, INSPECT ... TALLYING, OPEN INPUT, the COBOL forms of READ / WRITE (an Assembler
# OPEN / READ / WRITE macro takes parentheses or commas, a CLIST WRITE no period), a scope terminator (END-IF ...)
# - and a paragraph or section name in area A (columns 8-11) ended by a period (`S-110-DO.`, `A-100-BEGIN
# SECTION.  COPY X.`). CLOSE is no such statement: the Assembler CLOSE macro takes one DCB name bare (`CLOSE
# INFILE`) and a remark may follow it - it is one of the statements an Assembler member writes too (LESSONS 200).
_NAME = r"[A-Z][A-Z0-9\-]*"
_COBOL_VERB = (r"(?:MOVE[ \t][^\n]*?[ \t]TO(?:[ \t]|$)"
               r"|(?:ADD|SUBTRACT|MULTIPLY|DIVIDE)[ \t][^\n]*?[ \t](?:TO|FROM|BY|INTO|GIVING)(?:[ \t]|$)"
               r"|SET[ \t][^\n]*?[ \t]TO(?:[ \t]|$)|ACCEPT[ \t][^\n]*?[ \t]FROM(?:[ \t]|$)"
               r"|(?:UN)?STRING[ \t][^\n]*?[ \t]DELIMITED(?:[ \t]|$)"
               r"|INSPECT[ \t][^\n]*?[ \t](?:TALLYING|REPLACING|CONVERTING)(?:[ \t]|$)"
               r"|PERFORM(?:[ \t.]|$)|GOBACK(?:[ \t.]|$)|GO[ \t]+TO(?:[ \t.]|$)|EVALUATE[ \t]|STOP[ \t]+RUN(?:[ \t.]|$)"
               r"|EXIT(?:[ \t]+(?:PROGRAM|PARAGRAPH|SECTION|PERFORM(?:[ \t]+CYCLE)?))?[ \t]*\.(?:[ \t]|$)"
               r"|NEXT[ \t]+SENTENCE(?:[ \t.]|$)|CONTINUE(?:[ \t.]|$)|COMPUTE[ \t][^\n]*=|INITIALIZE[ \t]+[A-Z0-9]"
               r"|OPEN[ \t]+(?:INPUT|OUTPUT|I-O|EXTEND)(?:[ \t]|$)"
               r"|READ[ \t]+" + _NAME + r"(?:[ \t]+(?:NEXT|INTO|RECORD|KEY|AT|INVALID)(?:[ \t]|$)|[ \t]*\.?[ \t]*$)"
               r"|(?:RE)?WRITE[ \t]+" + _NAME + r"(?:[ \t]+(?:FROM|AFTER|BEFORE|INVALID)(?:[ \t]|$)|[ \t]*\.)"
               r"|END-(?:IF|PERFORM|EVALUATE|READ|WRITE|REWRITE|START|DELETE|CALL|EXEC|COMPUTE|STRING|UNSTRING|SEARCH|"
               r"RETURN|ADD|SUBTRACT|MULTIPLY|DIVIDE)(?:[ \t.]|$))")
_PARAGRAPH = (r"^(?!//|/\*|\*|\.\*)[^\n]{6}[ Dd] {0,3}(?=[A-Z0-9\-]*[A-Z])[A-Z0-9][A-Z0-9\-]*(?:[ \t]+SECTION)?[ \t]*\."
              r"(?:[ \t]|$)")
_SIG_COBOL_PROC = re.compile(_CODE_AT + _COBOL_VERB + "|" + _PARAGRAPH, re.M)
# COBOL statements an Assembler or MFS member writes as well - the CALL and COPY instructions (an MFS member COPYs
# its device headers), the structured-programming IF and MFS's IF in an operator control table, EXEC CICS / EXEC
# SQL in a CICS or DB2 Assembler program, the CLOSE macro with a DCB name, START with a symbol - count only where
# neither the Assembler nor the MFS shape fired, and only in a library that says nothing (library_says). Neither the
# IDCAMS `IF LASTCC` / `IF MAXCC`, a CLIST `IF &RC`, ICETOOL's `DISPLAY FROM(`, the IEBCOPY and ICETOOL `COPY
# OUTDD=` / `COPY FROM(`, nor a TSO `CALL 'LIB(PGM)'` has the COBOL form. The COBOL START verb is here, not among
# the statements above: unlabelled with a file name (`START CUSTFILE`, its KEY IS on the next line) it is the
# Assembler shape's own exception, and a label of six characters or fewer before an Assembler `START SYM` sits
# where a COBOL member's sequence area does (LESSONS 189, 200).
_SHARED_VERB = (r"(?:IF[ \t]+(?!LASTCC\b|MAXCC\b|&)[^\n;]*$|DISPLAY[ \t]+(?!FROM\()[^\n;]*$"
                r"|CALL[ \t]+(?:'[A-Z0-9@#$\-]{1,30}'|\"[A-Z0-9@#$\-]{1,30}\"|[A-Z][A-Z0-9\-]*)"
                r"(?:[ \t]*\.?[ \t]*$|[ \t]+(?:USING|RETURNING|ON|END-CALL)\b)"
                r"|EXEC[ \t]+(?:CICS|SQL|SQLIMS|DLI)\b"
                r"|COPY[ \t]+['\"]?[A-Z0-9@#$][A-Z0-9@#$\-]*['\"]?(?:[ \t]*\.|[ \t]*$|[ \t]+(?:OF|IN|REPLACING|SUPPRESS)\b)"
                r"|CLOSE[ \t]+" + _NAME + r"(?:[ \t]*\.?[ \t]*$|[ \t]+[A-Z])"
                r"|START[ \t]+" + _NAME + r"(?:[ \t]*\.?[ \t]*$|[ \t]+(?:KEY|INVALID)(?:[ \t]|$)))")
_SIG_COBOL_SHARED = re.compile(_CODE_AT + _SHARED_VERB, re.M)
# a DIVISION header on a code line: such a member is a program's part, not a procedure copybook
_SIG_DIVISION = re.compile(_CODE_AT + r"(?:IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)[ \t]+DIVISION\b", re.I | re.M)

# ---- statement languages that are not COBOL, and what the library says -----
#
# The COBOL statements are words other texts use too (LESSONS 199). His CNTL / PARMLIB libraries hold, beside the
# sort and IDCAMS cards, two statement languages that share them: an Easytrieve program - read by EZTPA00 through
# its SYSIN - with JOB INPUT, MOVE ... TO, PERFORM, IF ... END-IF; and a Connect:Direct process - submitted by
# DMBATCH `SUBMIT PROC=` - with its label and PROCESS SNODE=, COPY FROM (DSN=...), IF (SENDIT = 0) THEN ... EIF,
# whose label `STEP01` the loose level-number signature read as level 01. Filed a copybook, the job never read it
# (build._card_text reads a control-card member, never a copybook): the Easytrieve files and fields, the
# Connect:Direct datasets and the step's cards were gone from `job` and `dataset`. A member with either shape is
# typed by the kind declared for its library, its folder name or its extension - never by a COBOL signature.
_EZT_ACTIVITY = re.compile(r"^[ \t]*JOB(?:[ \t]+(?:INPUT|NAME|START|FINISH|ENVIRONMENT)\b|[ \t]*$)"
                           r"|^[ \t]*END-PROC\b|^[ \t]*[A-Z0-9@#$\-]{1,40}\.[ \t]+PROC\b"
                           r"|^[ \t]*SORT[ \t]+[A-Z0-9@#$\-]{1,8}[ \t]+TO[ \t]+[A-Z0-9@#$\-]{1,8}\b", re.I | re.M)
_EZT_FILE = re.compile(r"^[ \t]*FILE[ \t]+[A-Z0-9@#$\-]{1,8}(?:[ \t]|$)", re.I | re.M)          # FILE POLIN ...
_EZT_FIELD = re.compile(r"^[ \t]+[A-Z0-9@#$:\-]{1,40}[ \t]+\d{1,5}[ \t]+\d{1,5}[ \t]+[ANPBUK]\b", re.I | re.M)  # IN-ID 1 10 A
_NDM_PROCESS = re.compile(r"^[A-Z@#$][A-Z0-9@#$]{0,7}[ \t]+PROCESS(?:[ \t]|$)", re.I | re.M)     # QZNDM1 PROCESS ...
_NDM_NODE = re.compile(r"\b[SP]NODE[ \t]*=", re.I)                                              # ... SNODE=QZREMOTE
_NDM_STMT = re.compile(r"^(?:[A-Z@#$][A-Z0-9@#$]{0,7})?[ \t]+(?:COPY[ \t]+FROM[ \t]*\([ \t]*(?:DSN|PNODE|SNODE|FILE)\b"
                       r"|RUN[ \t]+(?:TASK|JOB)[ \t]*\(|EIF[ \t]*$)", re.I | re.M)
# PL/I: a procedure's label and PROC (`XQPLI: PROC OPTIONS(MAIN);`) or a DECLARE with its structure level or its
# attributes - its GO TO, IF, READ FILE(...) and CLOSE FILE(...) are no COBOL statements (LESSONS 248)
_PLI_PROC = re.compile(r"^[ \t]*[A-Z@#$_][A-Z0-9@#$_]*[ \t]*:[ \t]*PROC(?:EDURE)?[ \t]*(?:OPTIONS|RECURSIVE|REORDER|"
                       r"RETURNS|\(|;|$)", re.I | re.M)
_PLI_DCL = re.compile(r"^[ \t]*(?:DCL|DECLARE)[ \t]+(?:\d{1,2}[ \t]+)?[A-Z@#$_][A-Z0-9@#$_]*[ \t]*(?:[,;(]|[ \t]+(?:"
                      r"CHAR(?:ACTER)?|FIXED|FLOAT|BIN(?:ARY)?|DEC(?:IMAL)?|BIT|FILE|POINTER|PTR|ENTRY|BASED|STATIC|"
                      r"AUTOMATIC|AUTO|EXTERNAL|EXT|BUILTIN|LIKE|PIC(?:TURE)?|INIT(?:IAL)?|CONTROLLED|CTL|LABEL|"
                      r"AREA|OFFSET|VARYING|VAR)\b)", re.I | re.M)
EZT_WORDS = "an Easytrieve program"
NDM_WORDS = "a Connect:Direct process"
PLI_WORDS = "a PL/I program"
# A line of prose or Markdown: a letter in column 1 and, in column 7, neither a blank nor a COBOL indicator - no line
# of a COBOL member in reference format has that (columns 1-6 are its sequence area, column 7 its indicator) - or a
# Markdown heading or code fence. It makes a .txt a document for the COBOL statements (a .md, .html or .csv is one
# by its extension alone). So does a run book's numbered line - a list number in column 1 (`2.`, `3)`), where a COBOL
# member holds a sequence number or blanks - and an indented one, by an English word no COBOL statement carries
# (THE, YOU, PLEASE ...) outside a literal on a line that is not a comment: `PERFORM THE RESTART FROM STEP S020.`
# (LESSONS 200).
_PROSE = re.compile(r"^[A-Za-z][^\n]{5}[^ \t*/\-Dd\n]|^#{1,6}[ \t]|^```|^\d{1,3}[.)][ \t]+[A-Za-z]", re.M)
_PROSE_WORD = re.compile(r"(?<![A-Z0-9\-])(?:THE|YOU|YOUR|PLEASE|THIS|THESE|THOSE|THAT|WHICH|SHOULD|MUST)(?![A-Z0-9\-])",
                         re.I)
_LITERAL = re.compile(r"'[^'\n]*'?|\"[^\"\n]*\"?|\*>.*")         # a literal (to the line's end when open), a *> remark
# the document extensions a COBOL member never carries: every one but .txt, the extension his downloads arrive with
DOC_ONLY_EXTS = tuple(e for e, k in EXT_HINTS.items() if k == "doc" and e != ".txt")
# the kinds of Assembler or macro source - whose members write COPY, IF, CALL, EXEC, CLOSE and START as well
MACRO_KINDS = ("asm", "mfs", "bms", "dbd", "psb", "imsgen")


def _is_prose(head: str) -> bool:
    """Whether `head` (normalized) has a line of prose (_PROSE, _PROSE_WORD)."""
    if _PROSE.search(head):
        return True
    for line in head.split("\n"):
        if line.strip() and not _is_comment_line(line) and _PROSE_WORD.search(_LITERAL.sub(" ", line)):
            return True
    return False


def not_cobol(head: str) -> str:
    """'an Easytrieve program' / 'a Connect:Direct process' / 'a PL/I
    program' when `head` (normalized) has the shape of one - statements
    whose words are COBOL's in a language that is not - or ''."""
    if _EZT_ACTIVITY.search(head) or (_EZT_FILE.search(head) and _EZT_FIELD.search(head)):
        return EZT_WORDS
    if (_NDM_PROCESS.search(head) and _NDM_NODE.search(head)) or _NDM_STMT.search(head):
        return NDM_WORDS
    if _PLI_PROC.search(head) or _PLI_DCL.search(head):
        return PLI_WORDS
    return ""


def library_says(path: str, head: str, declared: Optional[str] = None) -> str:
    """What the member's library says it holds, for how much the COBOL
    statements must say (LESSONS 199, 200): 'doc' - the kind declared for
    the library, else its folder name, is doc; or, neither saying a kind,
    the file has a document's extension other than .txt (.md, .html,
    .csv ...), or is a .txt outside a dataset-named folder with a line of
    prose (_is_prose) - where a run book's PERFORM THE FOLLOWING STEPS or a
    design note's quoted paragraph count for nothing; 'cards' - declared or
    named ctlcard (or declared sched) - where two statement lines are
    needed, one of a form no card language has; 'macro' - declared or named
    Assembler or macro source (MACRO_KINDS: MFS, BMS, DBD, PSB, stage-1),
    or so by its extension (.asm, .mfs ...) - where the statements an
    Assembler or MFS member writes too (COPY, IF, CALL ...) count for
    nothing; '' otherwise. 'cobol', the UI table's default, says nothing."""
    parent = os.path.basename(os.path.dirname(path))
    ext = os.path.splitext(path)[1].lower()
    said = declared if declared in DECLARABLE and declared != "cobol" else None
    if said is None:
        said = next((k for rx, k in DIR_HINTS if rx.search(parent)), None)
    if said is None:
        if ext in DOC_ONLY_EXTS or (ext == ".txt" and not _DATASET_FOLDER.match(parent.upper()) and _is_prose(head)):
            said = "doc"
        elif EXT_HINTS.get(ext) in MACRO_KINDS:
            said = EXT_HINTS[ext]
    return ("doc" if said == "doc" else "cards" if said in ("ctlcard", "sched") else "macro" if said in MACRO_KINDS
            else "")


def statement_lines(head: str) -> Tuple[int, int]:
    """(lines with a COBOL statement no other language has, lines with any
    COBOL statement) in `head` (normalized, and read through code_view by
    the caller) - every match starts a line."""
    strong = {m.start() for m in _SIG_COBOL_PROC.finditer(head)}
    if not strong:
        return 0, 0
    return len(strong), len(strong | {m.start() for m in _SIG_COBOL_SHARED.finditer(head)})

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
# MFS: a statement with a label in column 1, TYPE= / POS= / LTH= operands, MSGEND / FMTEND / TABLEEND, an
# operator control table's `IF DATA=` / `IF LENGTH=` (DATA and LENGTH are COBOL reserved words: no COBOL IF reads
# so) - never a line whose first word is MSG alone (a COBOL section named MSG), nor a name that only begins with
# one of the words (`DEV-CODE` on a continuation line behind a change tag). Checked before the COBOL statements an
# MFS member writes too - its COPY of a device header, its IF (LESSONS 200).
_MFS_SHAPE = re.compile(r"^[A-Z@#$][A-Z0-9@#$]{0,7}[ \t]+(?:MSG|FMT|DEV|DFLD|MFLD)(?=[ \t]|$)"
                        r"|[ \t](?:MSG|DEV)[ \t]+TYPE=|[ \t]DFLD[ \t]+(?:POS|LTH)=|^[ \t]+(?:MSGEND|FMTEND|TABLEEND)(?=[ \t]|$)"
                        r"|^[ \t]+IF[ \t]+(?:DATA|LENGTH)[=<>^\u00ac]", re.I | re.M)
MFS_SHAPES = ("a labelled MSG / FMT / DEV / DFLD / MFLD, TYPE= / POS= / LTH= operands, MSGEND / FMTEND / TABLEEND, IF DATA= "
              "/ IF LENGTH=")
# A compiler listing echoes the source: without this it is filed as a second copy of the program, with line-number
# prefixes as fields - so it is looked for BEFORE the COBOL signatures. Its shape: the compiler's banner at the
# start of a line (`1PP 5655-EC6 IBM Enterprise COBOL for z/OS ...`, the carriage control optional) or the source
# heading `LineID PL SL`, a map heading at the start of a line (a comment naming MODULE MAP is not one), or
# numbered source lines - the listing's line number, then the source record with its OWN sequence number in
# columns 1-6: two numbers, where a plain source record has one (one line at a time). The compiler's name alone is
# no banner: a program's `DISPLAY 'BUILT WITH IBM ENTERPRISE COBOL'` or a copybook's VALUE literal names it too
# (LESSONS 199).
_LISTING_SHAPE = re.compile(r"^[ 01\-+]?\s*(\d{6})[^\s\d]*\s+\d{6}[ *\-/D]")
# (one run of blanks before the carriage control and one after it: two stars over the same blanks made a 64 KB blank
# line take 15 s - LESSONS 148, 200)
_LISTING_HEAD = re.compile(r"^[ \t\f]*LineID[ \t]+PL[ \t]+SL\b|^[ \t\f]*(?:1[ \t]*)?PP[ \t]+5655-", re.I | re.M)
# ... and, in a text already known to be a listing (atlas.recover reading one: its page headers, its sections),
# the compiler's name anywhere on a line counts as the banner as well
_LISTING_BANNER = re.compile(r"^[ \t\f]*LineID[ \t]+PL[ \t]+SL\b|IBM Enterprise COBOL|^1?PP[ \t]+5655-", re.I | re.M)
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

CARD_LINES = 2                                                  # COBOL statement lines a card library needs
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
    lines - or None. The numbered lines are the listing's own: each one's
    line number is the one before it plus one (the compiler numbers every
    line it prints, copied lines too; a page heading or a message between
    them changes nothing). Three lines of two six-digit numbers each were
    enough before - and a data copybook's 88-level VALUES list of six-digit
    codes run over three lines (`236118 236210 236220`) is that: the
    copybook was filed a listing and every program copying it said COPY X
    NOT FOUND (LESSONS 248)."""
    m = _code_hit(_LISTING_HEAD, head)
    if m:
        return m.start(), m.group(0).strip()
    m = _LISTING_MAP.search(head)
    if m:
        return m.start(), m.group(0).strip().lstrip("1").strip()
    first, n, pos, prev = -1, 0, 0, -1
    for line in head.split("\n"):
        hit = _LISTING_SHAPE.match(line)
        if hit:
            no = int(hit.group(1))
            if n and no == prev + 1:
                n += 1
            else:
                first, n = pos, 1                                 # a run starts here
            prev = no
            if n >= LISTING_LINES:
                return first, "numbered source lines"
        pos += len(line) + 1
    return None


def _open_quote(code: str) -> str:
    """The quote a literal in `code` leaves open at its end, or ''."""
    q = ""
    for ch in code:
        if q:
            if ch == q:
                q = ""
        elif ch in "'\"":
            q = ch
    return q


def code_view(head: str) -> str:
    """`head` (normalized) with the text of a literal CONTINUED from the line
    before blanked, as the compiler or the assembler reads it: text, never
    a statement. An Assembler or MFS statement runs on to the next line when
    column 72 holds a character, and a literal left open there goes on from
    the continuation line's first column up to its closing quote; a COBOL
    literal left open goes on after the quote that reopens it on a line with
    '-' in column 7, up to its closing quote. Before this, the COBOL
    statement signature read an MFS field's text `MOVE THE CURSOR TO THE
    ACTION FIELD'` on its continuation line as a MOVE statement, filed the
    format a copybook, and `screen` said NOT FOUND (LESSONS 248). Comment
    lines are kept as they are; line numbers and lengths do not change."""
    out: List[str] = []
    carry, runs_on = "", False            # the quote the line before left open; its column 72 held a character
    for line in head.split("\n"):
        text = line
        if carry and not _is_comment_line(line):
            if len(line) > 6 and line[6] == "-":
                reopen = line.find(carry, 7)
                close = line.find(carry, reopen + 1) if reopen >= 0 else -1
            elif runs_on:
                close = line.find(carry)
            else:
                close = -2                                  # neither form of continuation: the literal was not continued
            if close != -2:
                end = close if close >= 0 else len(line) - 1
                text = " " * (end + 1) + line[end + 1:]
        if _is_comment_line(text) or text.lstrip().startswith(".*"):
            out.append(text)
            carry, runs_on = "", False
            continue
        runs_on = len(text) >= 72 and text[71] not in " \t"
        carry = _open_quote(text[:71] if runs_on else text[:72])
        out.append(text)
    return "\n".join(out)


def cobol_proc_hit(head: str, shared: bool = False):
    """The first COBOL procedure statement or paragraph name in `head`
    (normalized) as a match - with `shared`, one of the forms an Assembler
    member uses as well - or None; None too when the text carries a DIVISION
    header (a program's part, not a procedure copybook)."""
    m = (_SIG_COBOL_SHARED if shared else _SIG_COBOL_PROC).search(head)
    if m and _SIG_DIVISION.search(head):
        return None
    return m


def _cobol_statements(head: str, says: str) -> bool:
    """Whether the COBOL statements in `head` (normalized) make it a
    procedure copybook, before the Assembler shape, where the library says
    `says` (library_says): never in a library of documents; in a library of
    control cards, CARD_LINES lines of statements, one of a form no card
    language has; elsewhere (Assembler or macro source too) one statement of
    such a form."""
    if says == "doc":
        return False
    if says != "cards":
        return cobol_proc_hit(head) is not None
    strong, lines = statement_lines(head)
    return strong >= 1 and lines >= CARD_LINES and not _SIG_DIVISION.search(head)


def reading(path: str, head: str, declared: Optional[str] = None) -> Tuple[str, str, str]:
    """(kind, reason, basis): what the text, the folder name and the
    extension say, before any declared kind - which says only how much the
    COBOL statements must say (library_says). `basis`: 'binary'; 'strong' (a
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
    # the REXX header opens the text; six blanks before it put the slash in column 7, where a COBOL comment has it:
    # there the COBOL copybook's own lines decide first (a banner comment of a copybook), then it is the exec's header
    rexx_after_cobol = False
    rx = _SIG_REXX.search(head)
    if rx:
        if not _is_comment_line(_line_of(head, rx.start() + len(rx.group(0)) - len(rx.group(0).lstrip()))):
            return "rexx", "REXX comment header", "strong"
        rexx_after_cobol = True
    if _code_hit(_SIG_SQL_DDL, head):
        return "sql", "CREATE TABLE/VIEW", "strong"
    if _SIG_EXEC.search(head):
        return "jcl", "EXEC statement without a JOB card (JCL fragment)", "strong"

    # ---- a COBOL copybook by its own lines (ROADMAP re-parse items 20, 22) -
    # never an Easytrieve program, a Connect:Direct process or a PL/I program; the COBOL statements say as much as
    # the library lets them (library_says, LESSONS 199), and a literal continued from the line before is text
    # (code_view, LESSONS 248); in a library of documents a data entry needs a clause with its operand
    other = not_cobol(head)
    says = "" if other else library_says(path, head, declared)
    code = code_view(head)
    if not other:
        if (_SIG_DATA_ENTRY if says == "doc" else _SIG_DATA_LEVEL).search(code):
            return "copybook", REASON_DATA, "cobol"
        if _cobol_statements(code, says):
            return "copybook", REASON_PROC, "cobol"
    if rexx_after_cobol:
        return "rexx", "REXX comment header", "strong"

    # ---- the Assembler and MFS shapes, then the COBOL statements they write too (LESSONS 200) ----------------
    if _ASM_SHAPE.search(head):
        return "asm", REASON_ASM, "weak"
    if _MFS_SHAPE.search(head):
        return "mfs", REASON_MFS, "weak"
    if not other and not says and cobol_proc_hit(code, shared=True):
        return "copybook", REASON_PROC, "cobol"
    if not other and not says and _SIG_LEVEL_NUMBER.search(code):
        return "copybook", REASON_DATA, "cobol"
    also = f" - {other}, whose statements are not COBOL" if other else ""

    # ---- fall back to the library folder name -----------------------------
    for rx, kind in DIR_HINTS:
        if rx.search(parent):
            return kind, f"library folder {parent}{also}", "folder"

    # ---- last resort: the extension ---------------------------------------
    if ext in EXT_HINTS:
        if EXT_HINTS[ext] == "doc" and _DATASET_FOLDER.match(parent.upper()):
            # A .txt in PROD.CLAIMS.BIND is a mainframe member the toolkit
            # cannot type (BIND cards, DBRM, PL/I ...) - not a specification.
            return "unknown", f"unrecognised member of library {parent} (extension {ext} ignored){also}", "none"
        return EXT_HINTS[ext], f"extension {ext}{also}", "extension"

    return "unknown", f"no signature, no library hint, no extension{also}", "none"


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


def decide(path: str, head: str, declared: Optional[str] = None) -> Tuple[str, str, str, int]:
    """(kind, reason, over, line): classify() and, when the kind declared
    for the library replaced the shape of an Assembler, listing or MFS
    member, that kind (`over`) and the line its shape is on - the build
    keeps it beside the member (a 'declared_kind' row), so no page says a
    program is whole over an Assembler member without saying so (LESSONS
    199)."""
    kind, reason, basis = reading(path, head, declared)
    if declared_wins(kind, basis, declared):
        parent = os.path.basename(os.path.dirname(path))
        return (declared, (f"the kind declared for library {parent} in the UI's table"
                           + ("" if basis == "none" else f", over {kind} ({reason})")),
                kind if basis == "weak" else "", shape_line(kind, normalized(head)) if basis == "weak" else 0)
    return kind, reason, "", 0


def shape_line(kind: str, head: str) -> int:
    """The line (1-based) of `head` (normalized) whose shape makes it an
    Assembler, MFS or listing member, or 0."""
    if kind == "listing":
        hit = listing_hit(head)
        return head.count("\n", 0, hit[0]) + 1 if hit else 0
    rx = _ASM_SHAPE if kind == "asm" else _MFS_SHAPE if kind == "mfs" else None
    m = rx.search(head) if rx else None
    return head.count("\n", 0, m.start()) + 1 if m else 0


def classify(path: str, head: str, ext_hint: Optional[str] = None, declared: Optional[str] = None) -> Tuple[str, str]:
    """Return (kind, reason). `head` is the first ~8KB of decoded text;
    `declared` the kind declared for the member's library in the UI's table
    (the build passes it; declared_wins() says when it counts)."""
    return decide(path, head, declared)[:2]
