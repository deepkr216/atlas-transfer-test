"""
classify.py - decide what each file in the folder actually is.

Extensions cannot be trusted. Members downloaded from a PDS frequently arrive
with no extension at all (the library name carried the type), or with an
extension that reflects the download tool rather than the content. Misfiling a
copybook as a program, or a PROC as a job, corrupts the index quietly - so
every file gets a content sniff and the extension is only a hint.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence, Tuple

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
    ".txt": "doc", ".md": "doc", ".doc": "doc", ".docx": "doc", ".pdf": "doc",
    ".xls": "doc", ".xlsx": "doc", ".ppt": "doc", ".pptx": "doc", ".csv": "doc",
    ".htm": "doc", ".html": "doc", ".vsd": "doc", ".vsdx": "doc",
}

# Library-name hints: a folder called COPYLIB or PROCLIB tells you the type of
# everything inside it, and is usually more reliable than the file extension.
DIR_HINTS = [
    (re.compile(r"COPY(LIB|BOOK)?S?$", re.I), "copybook"),
    (re.compile(r"PROCLIB$|PROCS?$", re.I), "proc"),
    (re.compile(r"JCL(LIB)?$|JOBS?$|CNTL$", re.I), "jcl"),
    (re.compile(r"SRC$|SOURCE$|COBOL$|PGM(LIB)?$", re.I), "cobol"),
    (re.compile(r"DBD(LIB|SRC)?$", re.I), "dbd"),
    (re.compile(r"PSB(LIB|SRC)?$", re.I), "psb"),
    (re.compile(r"BMS(LIB|SRC)?$|MAPS?$", re.I), "bms"),
    (re.compile(r"MFS(LIB|SRC)?$", re.I), "mfs"),
    (re.compile(r"CTL(CARDS?)?$|PARM(LIB)?$|SYSIN$", re.I), "ctlcard"),
    (re.compile(r"DOCS?$|DOCUMENT.*$|SPECS?$|DESIGN$", re.I), "doc"),
]

BINARY_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".vsdx", ".zip", ".gz",
               ".png", ".jpg", ".jpeg", ".gif", ".bin", ".load", ".obj"}

# ---- content signatures ---------------------------------------------------

_SIG_COBOL = re.compile(r"^\s*(IDENTIFICATION|ID)\s+DIVISION", re.I | re.M)
_SIG_PROGRAM_ID = re.compile(r"\bPROGRAM-ID\b", re.I)
_SIG_JOB = re.compile(r"^//\S{1,8}\s+JOB\b", re.M)
_SIG_PROC = re.compile(r"^//\S{0,8}\s+PROC\b", re.M)
_SIG_PEND = re.compile(r"^//\S{0,8}\s+PEND\b", re.M)
_SIG_EXEC = re.compile(r"^//\S{0,8}\s+EXEC\b", re.M)
# HLASM macro format: an optional label in column 1, then the operation. A
# pattern anchored on leading whitespace misses every labelled statement -
# which is most of them in DBD/PSB source.
_SIG_DBD = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?\s+DBD\s+NAME=", re.I | re.M)
_SIG_SEGM = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?\s+SEGM\s+NAME=", re.I | re.M)
_SIG_PSB = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?\s+PSBGEN\b|^(?:[A-Z0-9@#$]{1,8})?\s+PCB\s+TYPE=",
                      re.I | re.M)
_SIG_BMS = re.compile(r"\bDFHMSD\b|\bDFHMDI\b|\bDFHMDF\b", re.I)
# CICS CSD extract/upload deck (DEFINE form) or DFHCSDUP LIST report form.
_SIG_CSD = re.compile(r"^\s*(?:DEFINE|ALTER|USERDEFINE)\s+(?:TRANSACTION|PROGRAM|FILE|MAPSET|TDQUEUE)\("
                      r"|^\s*(?:TRANSACTION|PROGRAM|FILE)\([A-Z0-9@#$]+\)\s+GROUP\(", re.I | re.M)
# IMS stage-1 system definition: APPLCTN / TRANSACT macros (label optional in col 1).
_SIG_IMSGEN = re.compile(r"^(?:[A-Z0-9@#$]{1,8})?\s+(?:APPLCTN|TRANSACT)\s+(?:PSB|GPSB|CODE)=", re.I | re.M)
_SIG_MFS = re.compile(r"^\s*(MSG|FMT|DEV|DFLD|MFLD)\s", re.I | re.M)
_SIG_SQL_DDL = re.compile(r"\bCREATE\s+(TABLE|VIEW|INDEX|TABLESPACE|DATABASE)\b", re.I)
# Any level 01-49 plus 66/77/88. Level 49 is the DCLGEN VARCHAR structure and
# 02/03/04/06/07/15/20 are all common; testing only 01/05/10 misfiles them.
_SIG_DATA_LEVEL = re.compile(r"^.{0,6}.?\s*(0[1-9]|[1-4]\d|66|77|88)\s+[A-Z0-9][A-Z0-9\-]*",
                             re.I | re.M)
_SIG_ASM = re.compile(r"^\s*\w*\s+(CSECT|DSECT|START|DFHEIENT)\b", re.I | re.M)
_SIG_REXX = re.compile(r"^\s*/\*\s*REXX", re.I)


def classify(path: str, head: str, ext_hint: Optional[str] = None) -> Tuple[str, str]:
    """Return (kind, reason). `head` is the first ~8KB of decoded text."""
    ext = os.path.splitext(path)[1].lower()
    parent = os.path.basename(os.path.dirname(path))

    if ext in BINARY_EXTS:
        return EXT_HINTS.get(ext, "binary"), f"binary extension {ext}"

    # ---- content signatures win, in specificity order ---------------------
    if _SIG_JOB.search(head):
        return "jcl", "contains a JOB card"
    if _SIG_PROC.search(head) and _SIG_PEND.search(head):
        return "proc", "instream PROC with PEND"
    if _SIG_PROC.search(head):
        return "proc", "PROC statement"
    if _SIG_IMSGEN.search(head):
        return "imsgen", "APPLCTN/TRANSACT macro (IMS stage-1)"
    if _SIG_CSD.search(head):
        return "csd", "CICS CSD DEFINE / LIST resource block"
    if _SIG_DBD.search(head) or _SIG_SEGM.search(head):
        return "dbd", "DBD/SEGM macro"
    if _SIG_PSB.search(head):
        return "psb", "PSBGEN/PCB macro"
    if _SIG_BMS.search(head):
        return "bms", "DFHMSD/DFHMDI macro"
    if _SIG_MFS.search(head):
        return "mfs", "MFS statement"
    if _SIG_COBOL.search(head) or _SIG_PROGRAM_ID.search(head):
        return "cobol", "IDENTIFICATION DIVISION / PROGRAM-ID"
    if _SIG_ASM.search(head):
        return "asm", "CSECT/DSECT"
    if _SIG_REXX.search(head):
        return "rexx", "REXX comment header"
    if _SIG_SQL_DDL.search(head):
        return "sql", "CREATE TABLE/VIEW"
    if _SIG_EXEC.search(head):
        return "jcl", "EXEC statement without a JOB card (JCL fragment)"
    if _SIG_DATA_LEVEL.search(head):
        return "copybook", "COBOL level numbers with no PROCEDURE DIVISION"

    # ---- fall back to the library folder name -----------------------------
    for rx, kind in DIR_HINTS:
        if rx.search(parent):
            return kind, f"library folder {parent}"

    # ---- last resort: the extension ---------------------------------------
    if ext in EXT_HINTS:
        return EXT_HINTS[ext], f"extension {ext}"

    return "unknown", "no signature, no library hint, no extension"
