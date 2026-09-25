"""
synth_listing.py - an Enterprise COBOL 6 compiler listing of a program, the
way the compiler prints it: the banner and the ruler at the top of every
page, a six-digit line number per source line, a C after the number on
every copied line, the whole of a multi-line COPY statement printed before
the copied lines, a nested COPY's lines flagged inside the outer block, and
after the source the message table and the copybook-source table (which
library each copybook was read from). Written from the shape in LESSONS
171-172 and 182, not from atlas/recover.py.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

BANNER = ("1PP 5655-EC6 IBM Enterprise COBOL for z/OS  6.3.0 P231102                 {pgm:<8}  Date 09/24/2026  "
          "Time 21:14:07   Page   {page}")
RULER = ("   LineID  PL SL  ----+-*A-1-B--+----2----+----3----+----4----+----5----+----6----+----7-|--+----8 "
         "Map and Cross Reference")

_COPY = re.compile(r"\bCOPY\s+(?:'([A-Z0-9@#$-]+)'|\"([A-Z0-9@#$-]+)\"|([A-Z0-9@#$-]+))", re.I)
_INCLUDE = re.compile(r"\bINCLUDE\s+([A-Z0-9@#$-]+)", re.I)


def _mask(code: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: " " * len(m.group(0)), code)


def copy_in(code: str) -> Optional[Tuple[str, str]]:
    """(copybook, statement text) when the code holds a COPY statement outside a literal."""
    if re.match(r"^\s*\*", code):
        return None
    masked = _mask(code)
    m = _COPY.search(masked)
    if not m:
        return None
    name = (m.group(1) or m.group(2) or m.group(3)).upper()
    return name, code[m.start():].strip()


def _closed(stmt: str) -> bool:
    bare = _mask(stmt)
    if bare.count("==") % 2:
        return False
    return re.search(r"\.(\s|$)", re.sub(r"==.*?==", " ", bare)) is not None


def ibm_listing(program: str, program_records: Sequence[str], copybooks: Dict[str, Sequence[str]],
                page_lines: int = 50, copy_table: Optional[Sequence[Tuple[str, str, str]]] = None) -> str:
    out = [BANNER.format(pgm=program, page=1), "0Invocation parameters:", " TRUNC(BIN),DATA(24),XREF", "0Options in effect:",
           "    XREF(FULL)", "    NOSEQ"]
    page = 2
    out.append(BANNER.format(pgm=program, page=page))
    out.append(RULER)
    n = 0
    on_page = 0
    sql: List[str] = []

    def emit(line: str) -> None:
        nonlocal on_page, page
        if page_lines and on_page >= page_lines:
            page += 1
            out.append(BANNER.format(pgm=program, page=page))
            out.append(RULER)
            on_page = 0
        out.append(line)
        on_page += 1

    def emit_copy(name: str, depth: int) -> None:
        nonlocal n
        for crec in copybooks.get(name, []):
            n += 1
            emit(f"   {n:06d}C        {crec.ljust(80)}")
            inner = copy_in(crec[7:72] if len(crec) > 7 else "")
            if inner and inner[0] in copybooks and depth < 3:
                emit_copy(inner[0], depth + 1)

    open_copy: Optional[Tuple[str, str]] = None
    for rec in program_records:
        n += 1
        emit(f"   {n:06d}         {rec.ljust(80)}")
        code = rec[7:72] if len(rec) > 7 else ""
        found = copy_in(code)
        if open_copy and not found:
            open_copy = (open_copy[0], open_copy[1] + " " + code.strip())
            if not _closed(open_copy[1]):
                continue
            found, open_copy = open_copy, None
        elif found and not _closed(found[1]):
            open_copy = found
            continue
        up = code.upper()
        if sql or ("EXEC SQL" in up and "END-EXEC" not in up):
            sql.append(code.strip())
            if "END-EXEC" in up:
                m = _INCLUDE.search(_mask(" ".join(sql)))
                sql = []
                found = (m.group(1).upper(), " ".join(sql)) if m else None
            else:
                continue
        if found and found[0] in copybooks:
            emit_copy(found[0], 1)
    out.append("")
    out.append("   LineID  Message code  Message text")
    out.append(f"   {min(n, 12):06d}  IGYPS2015-I   A period was assumed after the paragraph name.")
    if copy_table:
        out.append("")
        out.append("0Copybook  Ddname    Library dataset                                Text  Created     Last modified")
        for k, (cb, dd, dsn) in enumerate(copy_table, 1):
            out.append(f"  {cb:<8}  {dd:<8}  {dsn:<44}  {k:>4}  2011/03/09  2024/11/11 08:00:27")
    out.append("")
    out.append("0End of compilation 1,  program " + program + ",  no statements flagged.")
    return "\n".join(out) + "\n"
