"""
synth_domain.py - the insurance vocabulary the synthetic estate is built
from: record layouts, work areas, communication areas, DCLGENs, PCB masks,
symbolic maps and message tables, as item trees (synth_layout.Item).

Every name here is fictional (POLICY / CLAIMS / BILLING). Nothing is copied
from any real estate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

from synth_layout import Item

SYSTEMS = ("POLICY", "CLAIMS", "BILLING")
SYS3 = {"POLICY": "POL", "CLAIMS": "CLM", "BILLING": "BIL"}
HLQ = {"POLICY": "PROD.POL", "CLAIMS": "PROD.CLM", "BILLING": "PROD.BIL"}
STG_HLQ = {"POLICY": "STG.POL", "CLAIMS": "STG.CLM", "BILLING": "STG.BIL"}
DB2_QUAL = "PRD"


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# elementary item helpers
# --------------------------------------------------------------------------

def x(level: int, name: str, n: int, value: Optional[str] = None, **kw) -> Item:
    return Item(level, name, pic=f"X({n:02d})" if n < 100 else f"X({n})", value=value, **kw)


def n9(level: int, name: str, n: int, value: Optional[str] = None, **kw) -> Item:
    return Item(level, name, pic=f"9({n:02d})", value=value, **kw)


def packed(level: int, name: str, digits: int, scale: int = 2, value: Optional[str] = None, **kw) -> Item:
    pic = f"S9({digits:02d})V9{'(' + str(scale) + ')' if scale > 1 else ''}" if scale else f"S9({digits:02d})"
    if scale == 2:
        pic = f"S9({digits:02d})V99"
    return Item(level, name, pic=pic, usage="COMP-3", value=value, **kw)


def binary(level: int, name: str, digits: int = 4, value: Optional[str] = None, usage: str = "COMP", **kw) -> Item:
    return Item(level, name, pic=f"S9({digits:02d})", usage=usage, value=value, **kw)


def group(level: int, name: str, children: Sequence[Item], **kw) -> Item:
    return Item(level, name, children=list(children), **kw)


def flag(level: int, name: str, conds: Sequence[Tuple[str, Sequence[str]]], n: int = 1,
         value: Optional[str] = None, style: str = "VALUE") -> Item:
    it = x(level, name, n, value=value)
    it.conds = [(c, [lit(v) if not v.startswith("'") and not v.upper().startswith(("THRU", "SPACE", "ZERO", "LOW", "HIGH")) else v
                     for v in vals]) for c, vals in conds]
    it.conds_style = style
    return it


# --------------------------------------------------------------------------
# domain pieces
# --------------------------------------------------------------------------

PROVINCES = ["ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "PE", "NL", "YT", "NT", "NU"]
STATUS_SETS = {
    "POLICY": [("ACTIVE", ["AC"]), ("LAPSED", ["LP"]), ("CANCELLED", ["CN", "CX", "CR"]), ("PENDING", ["PN"]),
               ("INFORCE", ["'AC' THRU 'AZ'"])],
    "CLAIMS": [("OPEN", ["OP"]), ("CLOSED", ["CL"]), ("REOPENED", ["RO"]), ("DENIED", ["DN", "DX"]),
               ("SUSPENDED", ["SP"])],
    "BILLING": [("CURRENT", ["CU"]), ("OVERDUE", ["OD"]), ("PAID", ["PD"]), ("WRITTEN-OFF", ["WO", "WX"]),
                ("SUSPENSE", ["SU"])],
}
KEY_FIELD = {"POLICY": ("POLICY-NO", 12), "CLAIMS": ("CLAIM-NO", 10), "BILLING": ("ACCOUNT-NO", 12)}
AMOUNTS = {"POLICY": ["PREMIUM-AMT", "COMMISSION-AMT", "TAX-AMT", "TOTAL-AMT", "REFUND-AMT"],
           "CLAIMS": ["RESERVE-AMT", "PAID-AMT", "EXPENSE-AMT", "RECOVERY-AMT", "DEDUCTIBLE-AMT"],
           "BILLING": ["AMOUNT-DUE", "AMOUNT-PAID", "BALANCE-AMT", "LATE-FEE-AMT", "CREDIT-AMT"]}
DATES = {"POLICY": ["EFFECTIVE-DT", "EXPIRY-DT", "ISSUE-DT", "LAST-CHG-DT"],
         "CLAIMS": ["LOSS-DT", "REPORT-DT", "CLOSE-DT", "LAST-CHG-DT"],
         "BILLING": ["BILL-DT", "DUE-DT", "PAID-DT", "LAST-CHG-DT"]}
CODES = {"POLICY": [("LINE-CD", 2, [("AUTO", ["AU"]), ("PROPERTY", ["PR"]), ("LIFE", ["LF"])]),
                    ("PAY-PLAN", 1, [("ANNUAL", ["A"]), ("MONTHLY", ["M"]), ("QUARTERLY", ["Q"])]),
                    ("BILL-TYPE", 1, [("DIRECT", ["D"]), ("AGENCY", ["A"])])],
         "CLAIMS": [("CAUSE-CD", 3, [("FIRE", ["FIR"]), ("THEFT", ["THF"]), ("COLLISION", ["COL"]), ("WATER", ["WTR"])]),
                    ("SEVERITY", 1, [("LOW", ["1"]), ("MEDIUM", ["2"]), ("HIGH", ["3"])]),
                    ("LITIGATION", 1, [("YES", ["Y"]), ("NO", ["N"])])],
         "BILLING": [("PAY-METHOD", 1, [("CHEQUE", ["C"]), ("EFT", ["E"]), ("CARD", ["K"])]),
                     ("CYCLE-CD", 2, [("MONTHLY", ["01"]), ("QUARTERLY", ["03"]), ("ANNUAL", ["12"])]),
                     ("DUNNING", 1, [("NONE", ["0"]), ("FIRST", ["1"]), ("FINAL", ["9"])])]}
NAME_PARTS = ["LAST-NAME", "FIRST-NAME", "MID-INIT"]
EXTRA_WORDS = ["REGION", "BRANCH", "AGENT", "BROKER", "PRODUCT", "RIDER", "TERM", "SOURCE", "CHANNEL", "UNIT",
               "SEGMENT", "CLASS", "GROUP", "TIER", "ZONE", "BATCH", "USER", "TERMINAL", "REASON", "OVERRIDE"]


@dataclass
class RecordMeta:
    """What a record layout offers a program: the fields it can move, test and compute with."""
    key: str = ""
    status: str = ""
    status_conds: List[Tuple[str, List[str]]] = dc_field(default_factory=list)
    amounts: List[str] = dc_field(default_factory=list)
    dates: List[str] = dc_field(default_factory=list)
    alnums: List[Tuple[str, int]] = dc_field(default_factory=list)
    nums: List[str] = dc_field(default_factory=list)
    flags: List[Tuple[str, List[Tuple[str, List[str]]]]] = dc_field(default_factory=list)
    tables: List[Tuple[str, int, str]] = dc_field(default_factory=list)     # (table item, occurs, odo field or "")
    table_items: List[Tuple[str, str, str]] = dc_field(default_factory=list)  # (item, table, kind)
    groups: List[str] = dc_field(default_factory=list)
    corr_groups: List[str] = dc_field(default_factory=list)   # groups with YY/MM/DD children (MOVE CORRESPONDING)
    counters: List[str] = dc_field(default_factory=list)


def _collect(root: Item, meta: RecordMeta, prefix: str) -> RecordMeta:
    """Fill the meta from the tree (fields inside OCCURS go to table_items)."""

    def rec(it: Item, in_table: str) -> None:
        for ch in it.children:
            nm = ch.name
            tbl = in_table or (ch.name if ch.occurs else "")
            if ch.children:
                if not tbl and nm:
                    meta.groups.append(nm)
                    if any(c.name.endswith("-YY") for c in ch.children):
                        meta.corr_groups.append(nm)
                rec(ch, tbl if ch.occurs or in_table else "")
                continue
            if not nm or nm == "FILLER":
                continue
            if tbl:
                kind = "packed" if ch.usage == "COMP-3" else "num" if ch.pic.startswith("9") or ch.pic.startswith("S9") else "alnum"
                meta.table_items.append((nm, tbl, kind))
                continue
            if ch.conds:
                meta.flags.append((nm, [(c, v) for c, v in ch.conds]))
            if ch.usage == "COMP-3" and ch.scale == 2:
                meta.amounts.append(nm)
            elif ch.pic.startswith("9(08)"):
                meta.dates.append(nm)
            elif ch.pic.startswith("X"):
                n = int(ch.pic[2:].rstrip(")")) if "(" in ch.pic else 1
                meta.alnums.append((nm, n))
            elif ch.pic.startswith(("9", "S9")):
                (meta.counters if ch.usage in ("COMP", "COMP-5") or ch.name.endswith(("-CNT", "-COUNT", "-CTR")) else meta.nums).append(nm)
    rec(root, "")
    for it in _flat(root):
        if it.occurs and it.name:
            meta.tables.append((it.name, it.occurs, it.odo))
    return meta


def _flat(root: Item) -> List[Item]:
    out = []

    def rec(it):
        out.append(it)
        for c in it.children:
            rec(c)
    rec(root)
    return out


def date_group(level: int, name: str, pfx: str) -> Item:
    return group(level, name, [n9(level + 5, f"{pfx}-YY", 4), n9(level + 5, f"{pfx}-MM", 2), n9(level + 5, f"{pfx}-DD", 2)])


def master_record(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    """The system's master record as 05-level items (a fragment copybook)."""
    key, klen = KEY_FIELD[system]
    st = STATUS_SETS[system]
    items: List[Item] = [x(5, f"{pfx}-{key}", klen)]
    items.append(flag(5, f"{pfx}-STATUS", [(f"{pfx}-{c}", v) for c, v in st], n=2))
    for d in DATES[system][:3]:
        items.append(n9(5, f"{pfx}-{d}", 8))
    for a in AMOUNTS[system][:3 + variant % 2]:
        items.append(packed(5, f"{pfx}-{a}", 9))
    items.append(binary(5, f"{pfx}-COVERAGE-CNT", 4))
    items.append(group(5, f"{pfx}-INSURED-NAME", [x(10, f"{pfx}-LAST-NAME", 20), x(10, f"{pfx}-FIRST-NAME", 15),
                                                  x(10, f"{pfx}-MID-INIT", 1)]))
    addr = group(5, f"{pfx}-ADDRESS", [x(10, f"{pfx}-ADDR-LINE", 30, occurs=3), x(10, f"{pfx}-CITY", 20),
                                       flag(10, f"{pfx}-PROV", [(f"{pfx}-VALID-PROV", PROVINCES)], n=2, style="VALUES ARE"),
                                       x(10, f"{pfx}-POSTAL-CD", 9)])
    items.append(addr)
    code, cl, conds = CODES[system][variant % 3]
    items.append(flag(5, f"{pfx}-{code}", [(f"{pfx}-{c}", v) for c, v in conds], n=cl))
    items.append(group(5, f"{pfx}-AGENT-DATA", [x(10, f"{pfx}-AGENT-ID", 6), packed(10, f"{pfx}-COMMISSION-PCT", 3, 4)]))
    items.append(group(5, f"{pfx}-LEGACY-AREA", [x(10, f"{pfx}-OLD-AGENT-KEY", 9)], redefines=f"{pfx}-AGENT-DATA"))
    items.append(packed(5, f"{pfx}-ITEM-COUNT", 3, 0))
    items.append(group(5, f"{pfx}-ITEM-TBL", [x(10, f"{pfx}-ITEM-CODE", 4), packed(10, f"{pfx}-ITEM-LIMIT", 11),
                                              packed(10, f"{pfx}-ITEM-PREM", 7)],
                       occurs=12 + 2 * variant, occurs_min=1, odo=f"{pfx}-ITEM-COUNT"))
    items.append(x(5, f"{pfx}-FILLER", 25 + variant))
    root = Item(1, "", children=items)
    for it in _flat(root):
        it.src = ""
    return items, _collect(root, RecordMeta(key=f"{pfx}-{key}", status=f"{pfx}-STATUS",
                                            status_conds=[(f"{pfx}-{c}", v) for c, v in st]), pfx)


def trans_record(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    key, klen = KEY_FIELD[system]
    items = [flag(5, f"{pfx}-TRAN-TYPE", [(f"{pfx}-ADD", ["A"]), (f"{pfx}-CHANGE", ["C"]), (f"{pfx}-DELETE", ["D"]),
                                          (f"{pfx}-INQUIRY", ["I"])], n=1),
             x(5, f"{pfx}-{key}", klen),
             n9(5, f"{pfx}-TRAN-DT", 8),
             x(5, f"{pfx}-TRAN-TIME", 6),
             x(5, f"{pfx}-USER-ID", 8),
             packed(5, f"{pfx}-TRAN-AMT", 9),
             flag(5, f"{pfx}-SOURCE-CD", [(f"{pfx}-ONLINE", ["O"]), (f"{pfx}-BATCH", ["B"]), (f"{pfx}-EXTERNAL", ["X"])]),
             group(5, f"{pfx}-DETAIL", [x(10, f"{pfx}-DETAIL-TEXT", 60), x(10, f"{pfx}-REASON-CD", 3)]),
             group(5, f"{pfx}-DETAIL-AMTS", [packed(10, f"{pfx}-DET-AMT", 9, occurs=6), x(10, f"{pfx}-DET-FILL", 6)],
                   redefines=f"{pfx}-DETAIL"),
             x(5, f"{pfx}-FILLER", 20 + variant * 3)]
    root = Item(1, "", children=items)
    return items, _collect(root, RecordMeta(key=f"{pfx}-{key}"), pfx)


def extract_record(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    key, klen = KEY_FIELD[system]
    items = [x(5, f"{pfx}-{key}", klen), x(5, f"{pfx}-STATUS", 2), n9(5, f"{pfx}-AS-OF-DT", 8),
             x(5, f"{pfx}-REGION-CD", 3), x(5, f"{pfx}-AGENT-ID", 6),
             packed(5, f"{pfx}-{AMOUNTS[system][0]}", 9), packed(5, f"{pfx}-{AMOUNTS[system][1]}", 9),
             n9(5, f"{pfx}-{DATES[system][0]}", 8),
             x(5, f"{pfx}-NAME-TEXT", 36), x(5, f"{pfx}-POSTAL-CD", 9),
             Item(5, f"{pfx}-COUNT-1", pic="9(05)"), Item(5, f"{pfx}-COUNT-2", pic="9(05)"),
             group(5, f"{pfx}-RUN-DATE", [n9(10, f"{pfx}-RUN-YY", 4), n9(10, f"{pfx}-RUN-MM", 2), n9(10, f"{pfx}-RUN-DD", 2)]),
             x(5, f"{pfx}-FILLER", 30 + variant * 5)]
    root = Item(1, "", children=items)
    return items, _collect(root, RecordMeta(key=f"{pfx}-{key}", status=f"{pfx}-STATUS"), pfx)


def history_record(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    key, klen = KEY_FIELD[system]
    items = [group(5, f"{pfx}-HIST-KEY", [x(10, f"{pfx}-{key}", klen), n9(10, f"{pfx}-SEQ-NO", 5)]),
             n9(5, f"{pfx}-CHG-DT", 8), x(5, f"{pfx}-CHG-USER", 8),
             flag(5, f"{pfx}-CHG-TYPE", [(f"{pfx}-CHG-NEW", ["N"]), (f"{pfx}-CHG-UPD", ["U"]), (f"{pfx}-CHG-DEL", ["D"])]),
             x(5, f"{pfx}-BEFORE-IMAGE", 80), x(5, f"{pfx}-AFTER-IMAGE", 80),
             packed(5, f"{pfx}-OLD-AMT", 9), packed(5, f"{pfx}-NEW-AMT", 9),
             x(5, f"{pfx}-FILLER", 10 + variant)]
    root = Item(1, "", children=items)
    return items, _collect(root, RecordMeta(key=f"{pfx}-{key}"), pfx)


def control_record(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    items = [x(5, f"{pfx}-CTL-ID", 4, value=lit("CTL1")), n9(5, f"{pfx}-RUN-DATE", 8), n9(5, f"{pfx}-CYCLE-NO", 3),
             flag(5, f"{pfx}-RUN-MODE", [(f"{pfx}-FULL-RUN", ["F"]), (f"{pfx}-DELTA-RUN", ["D"]), (f"{pfx}-REPORT-ONLY", ["R"])]),
             x(5, f"{pfx}-ROUTINE-NAME", 8), x(5, f"{pfx}-NEXT-ROUTINE", 8),
             group(5, f"{pfx}-LIMITS", [packed(10, f"{pfx}-MAX-AMT", 11), packed(10, f"{pfx}-MIN-AMT", 11),
                                        binary(10, f"{pfx}-MAX-ERRORS", 4)]),
             x(5, f"{pfx}-FILLER", 20)]
    root = Item(1, "", children=items)
    return items, _collect(root, RecordMeta(), pfx)


def generic_record(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    """A plausible record of 8-16 fields, different every time."""
    key, klen = KEY_FIELD[system]
    items: List[Item] = [x(5, f"{pfx}-{key}", klen)]
    n = 6 + rng.randrange(9)
    words = rng.sample(EXTRA_WORDS, min(n, len(EXTRA_WORDS)))
    for k, w in enumerate(words):
        r = rng.random()
        if r < 0.35:
            items.append(x(5, f"{pfx}-{w}-CD", rng.choice([1, 2, 3, 4, 6, 8])))
        elif r < 0.55:
            items.append(packed(5, f"{pfx}-{w}-AMT", rng.choice([7, 9, 11])))
        elif r < 0.7:
            items.append(n9(5, f"{pfx}-{w}-DT", 8))
        elif r < 0.8:
            items.append(binary(5, f"{pfx}-{w}-CNT", rng.choice([4, 9]), usage=rng.choice(["COMP", "COMP-5"])))
        elif r < 0.9:
            items.append(x(5, f"{pfx}-{w}-TEXT", rng.choice([20, 30, 40])))
        else:
            items.append(group(5, f"{pfx}-{w}-TBL", [x(10, f"{pfx}-{w}-KEY", 4), packed(10, f"{pfx}-{w}-VAL", 7)],
                               occurs=rng.choice([5, 10, 12])))
    if rng.random() < 0.5:
        items.append(Item(5, f"{pfx}-SIGNED-QTY", pic="S9(05)", sign_sep="LEADING"))
    items.append(x(5, f"{pfx}-FILLER", 10 + rng.randrange(30)))
    root = Item(1, "", children=items)
    return items, _collect(root, RecordMeta(key=f"{pfx}-{key}"), pfx)


def report_line(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    key, klen = KEY_FIELD[system]
    items = [x(5, f"{pfx}-CC", 1), x(5, f"{pfx}-{key}", klen), x(5, "FILLER", 2), x(5, f"{pfx}-NAME", 30),
             x(5, "FILLER", 2), Item(5, f"{pfx}-AMT-1", pic="ZZ,ZZZ,ZZ9.99-"), x(5, "FILLER", 2),
             Item(5, f"{pfx}-AMT-2", pic="ZZ,ZZZ,ZZ9.99-"), x(5, "FILLER", 2), x(5, f"{pfx}-STATUS", 2),
             x(5, "FILLER", 2), Item(5, f"{pfx}-DATE", pic="9(04)/99/99"), x(5, "FILLER", 40)]
    root = Item(1, "", children=items)
    return items, _collect(root, RecordMeta(), pfx)


RECORD_BUILDERS = {"master": master_record, "trans": trans_record, "extract": extract_record, "history": history_record,
                   "control": control_record, "generic": generic_record, "report": report_line}


# --------------------------------------------------------------------------
# work areas, comm areas, common areas
# --------------------------------------------------------------------------

def work_area(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    """`01 xxx-WORK-AREA.` with counters, switches, dates and subscripts."""
    kids = [group(5, f"{pfx}-COUNTERS", [packed(10, f"{pfx}-READ-CNT", 7, 0, value="ZERO"),
                                          packed(10, f"{pfx}-WRITE-CNT", 7, 0, value="ZERO"),
                                          packed(10, f"{pfx}-ERROR-CNT", 7, 0, value="ZERO"),
                                          binary(10, f"{pfx}-PAGE-CNT", 4, value="ZERO"),
                                          binary(10, f"{pfx}-LINE-CNT", 4, value="+60")]),
            group(5, f"{pfx}-SWITCHES", [flag(10, f"{pfx}-EOF-SW", [(f"{pfx}-EOF", ["Y"]), (f"{pfx}-NOT-EOF", ["N"])], value=lit("N")),
                                          flag(10, f"{pfx}-ERROR-SW", [(f"{pfx}-ERROR-FOUND", ["Y"]), (f"{pfx}-NO-ERROR", ["N"])], value=lit("N")),
                                          flag(10, f"{pfx}-FIRST-SW", [(f"{pfx}-FIRST-TIME", ["Y"])], value=lit("Y"))]),
            group(5, f"{pfx}-WORK-DATE", [n9(10, f"{pfx}-WD-YY", 4), n9(10, f"{pfx}-WD-MM", 2), n9(10, f"{pfx}-WD-DD", 2)]),
            group(5, f"{pfx}-SAVE-DATE", [n9(10, f"{pfx}-WD-YY", 4), n9(10, f"{pfx}-WD-MM", 2), n9(10, f"{pfx}-WD-DD", 2)]),
            group(5, f"{pfx}-SUBSCRIPTS", [binary(10, f"{pfx}-SUB", 4, value="ZERO"), binary(10, f"{pfx}-SUB2", 4, value="ZERO"),
                                            binary(10, f"{pfx}-MAX-SUB", 4, value="+12")]),
            packed(5, f"{pfx}-WORK-AMT", 11, value="ZERO"), packed(5, f"{pfx}-TOTAL-AMT", 13, value="ZERO"),
            x(5, f"{pfx}-WORK-TEXT", 40, value="SPACES"), x(5, f"{pfx}-MESSAGE", 80, value="SPACES"),
            binary(5, f"{pfx}-STR-PTR", 4, value="+1"),
            x(5, f"{pfx}-RETURN-CD", 2, value=lit("00"))]
    if variant % 2:
        kids.append(group(5, f"{pfx}-RATE-TBL", [x(10, f"{pfx}-RATE-CD", 2), packed(10, f"{pfx}-RATE-PCT", 3, 4)],
                          occurs=10))
    root = Item(1, f"{pfx}-WORK-AREA", children=kids)
    return [root], _collect(root, RecordMeta(), pfx)


def comm_area(rng: random.Random, system: str, pfx: str, variant: int = 0) -> Tuple[List[Item], RecordMeta]:
    """`01 xxx-COMMAREA.`: what a caller passes to a subroutine (or CICS COMMAREA)."""
    key, klen = KEY_FIELD[system]
    kids = [x(5, f"{pfx}-FUNCTION", 1),
            x(5, f"{pfx}-{key}", klen),
            x(5, f"{pfx}-RETURN-CODE", 2),
            x(5, f"{pfx}-MESSAGE", 60),
            packed(5, f"{pfx}-AMOUNT", 9),
            n9(5, f"{pfx}-EFFECTIVE-DT", 8),
            binary(5, f"{pfx}-ITEM-CNT", 4),
            x(5, f"{pfx}-USER-ID", 8)]
    if variant % 2:
        kids.append(group(5, f"{pfx}-ITEMS", [x(10, f"{pfx}-ITEM-CD", 4), packed(10, f"{pfx}-ITEM-AMT", 7)], occurs=5))
    kids.append(x(5, f"{pfx}-FILLER", 10 + variant))
    root = Item(1, f"{pfx}-COMMAREA", children=kids)
    return [root], _collect(root, RecordMeta(key=f"{pfx}-{key}"), pfx)


def date_area_with_start_date() -> Tuple[List[Item], RecordMeta]:
    """The common date work area: START-DATE trips a weak Assembler signature (LESSONS 186)."""
    kids = [n9(5, "START-DATE", 8), n9(5, "END-DATE", 8), n9(5, "DAYS-BETWEEN", 5),
            group(5, "DATE-IN", [n9(10, "DI-YY", 4), n9(10, "DI-MM", 2), n9(10, "DI-DD", 2)]),
            group(5, "DATE-OUT", [n9(10, "DI-YY", 4), n9(10, "DI-MM", 2), n9(10, "DI-DD", 2)]),
            flag(5, "DATE-RC", [("DATE-OK", ["0"]), ("DATE-INVALID", ["1"]), ("DATE-RANGE-ERR", ["2"])], n=1)]
    root = Item(1, "CMN-DATE-AREA", children=kids)
    return [root], _collect(root, RecordMeta(), "CMN")


def error_area() -> Tuple[List[Item], RecordMeta]:
    kids = [x(5, "ERR-PROGRAM", 8), x(5, "ERR-PARAGRAPH", 30), x(5, "ERR-CODE", 4),
            flag(5, "ERR-SEVERITY", [("ERR-WARNING", ["W"]), ("ERR-SEVERE", ["S"]), ("ERR-FATAL", ["F"])]),
            x(5, "ERR-TEXT", 80), binary(5, "ERR-SQLCODE", 9), x(5, "ERR-FILE-STATUS", 2), x(5, "ERR-KEY", 20)]
    root = Item(1, "CMN-ERROR-AREA", children=kids)
    return [root], _collect(root, RecordMeta(), "ERR")


def std_header(system: str) -> Tuple[List[Item], RecordMeta]:
    """Same name in three systems with different content: each system's own copy is the right one."""
    kids = [x(5, "HDR-SYSTEM", 8, value=lit(system)), x(5, "HDR-PROGRAM", 8), n9(5, "HDR-RUN-DATE", 8),
            x(5, "HDR-RUN-TIME", 8)]
    if system == "POLICY":
        kids.append(x(5, "HDR-REGION", 3))
    elif system == "CLAIMS":
        kids.append(x(5, "HDR-ADJUSTER", 6))
        kids.append(x(5, "HDR-OFFICE", 4))
    else:
        kids.append(binary(5, "HDR-CYCLE", 4))
    kids.append(x(5, "HDR-FILLER", 20))
    root = Item(1, "STD-HEADER", children=kids)
    return [root], _collect(root, RecordMeta(), "HDR")


def cust_record() -> Tuple[List[Item], RecordMeta]:
    """The shared customer KSDS record: the START verb's file (CUSTFILE) uses it."""
    kids = [group(5, "CF-CUST-KEY", [x(10, "CF-CUST-NO", 10), x(10, "CF-CUST-TYPE", 2)]),
            x(5, "CF-CUST-NAME", 40), x(5, "CF-CUST-ADDR", 60),
            flag(5, "CF-CUST-STATUS", [("CF-CUST-ACTIVE", ["A"]), ("CF-CUST-INACTIVE", ["I"])]),
            n9(5, "CF-SINCE-DT", 8), packed(5, "CF-CREDIT-LIMIT", 9), x(5, "CF-FILLER", 30)]
    root = Item(1, "CUST-RECORD", children=kids)
    return [root], _collect(root, RecordMeta(key="CF-CUST-KEY", status="CF-CUST-STATUS"), "CF")


def pcb_mask() -> Tuple[List[Item], RecordMeta]:
    """`01 :PCB:-PCB.` - the DB PCB mask, copied with REPLACING ==:PCB:== BY ==POL==."""
    kids = [x(5, ":PCB:-DBD-NAME", 8), x(5, ":PCB:-SEG-LEVEL", 2),
            flag(5, ":PCB:-STATUS", [(":PCB:-OK", ["'  '"]), (":PCB:-END-OF-DB", ["GB"]), (":PCB:-NOT-FOUND", ["GE"]),
                                     (":PCB:-DUPLICATE", ["II"])], n=2),
            x(5, ":PCB:-PROC-OPT", 4), binary(5, ":PCB:-RESERVED", 9), x(5, ":PCB:-SEG-NAME", 8),
            binary(5, ":PCB:-KEY-LENGTH", 9), binary(5, ":PCB:-NUM-SENSEG", 9), x(5, ":PCB:-KEY-FB", 40)]
    root = Item(1, ":PCB:-PCB", children=kids)
    return [root], _collect(root, RecordMeta(), "PCB")


def io_pcb_mask() -> Tuple[List[Item], RecordMeta]:
    kids = [x(5, "IO-LTERM-NAME", 8), x(5, "FILLER", 2), x(5, "IO-STATUS", 2), x(5, "IO-DATE", 4), x(5, "IO-TIME", 4),
            binary(5, "IO-MSG-SEQ", 9), x(5, "IO-MOD-NAME", 8), x(5, "IO-USER-ID", 8)]
    root = Item(1, "IO-PCB", children=kids)
    return [root], _collect(root, RecordMeta(), "IO")


def sql_work_area() -> Tuple[List[Item], RecordMeta]:
    kids = [binary(5, "SQL-ROW-COUNT", 9, value="ZERO"), x(5, "SQL-STMT-ID", 20), x(5, "SQL-TABLE-NAME", 30),
            flag(5, "SQL-CURSOR-SW", [("SQL-CURSOR-OPEN", ["O"]), ("SQL-CURSOR-CLOSED", ["C"])], value=lit("C")),
            x(5, "SQL-ERR-TEXT", 80)]
    root = Item(1, "CMN-SQL-WORK", children=kids)
    return [root], _collect(root, RecordMeta(), "SQL")


def abend_area() -> Tuple[List[Item], RecordMeta]:
    kids = [x(5, "ABEND-PROGRAM", 8), x(5, "ABEND-CODE", 4, value=lit("U100")), x(5, "ABEND-TEXT", 60),
            binary(5, "ABEND-RC", 4, value="+16")]
    root = Item(1, "CMN-ABEND-AREA", children=kids)
    return [root], _collect(root, RecordMeta(), "ABEND")


def generic_work(rng: random.Random, pfx: str, variant: int) -> Tuple[List[Item], RecordMeta]:
    kids: List[Item] = []
    words = rng.sample(EXTRA_WORDS, 5 + rng.randrange(6))
    for w in words:
        r = rng.random()
        if r < 0.3:
            kids.append(x(5, f"{pfx}-{w}-CD", rng.choice([1, 2, 4]), value="SPACES"))
        elif r < 0.55:
            kids.append(packed(5, f"{pfx}-{w}-AMT", 9, value="ZERO"))
        elif r < 0.75:
            kids.append(binary(5, f"{pfx}-{w}-CNT", 4, value="ZERO"))
        else:
            kids.append(x(5, f"{pfx}-{w}-TEXT", 30, value="SPACES"))
    kids.append(flag(5, f"{pfx}-STATE", [(f"{pfx}-STATE-NEW", ["N"]), (f"{pfx}-STATE-DONE", ["D"])], value=lit("N")))
    root = Item(1, f"{pfx}-AREA", children=kids)
    return [root], _collect(root, RecordMeta(), pfx)


# --------------------------------------------------------------------------
# DB2 tables and DCLGENs
# --------------------------------------------------------------------------

@dataclass
class Column:
    name: str
    sqltype: str
    nullable: bool = False

    def cobol(self, level: int, pfx: str = "") -> Item:
        nm = self.name.replace("_", "-")
        t = self.sqltype.upper()
        if t.startswith("CHAR"):
            n = int(t[5:-1])
            return Item(level, nm, pic=f"X({n})")
        if t.startswith("VARCHAR"):
            n = int(t[8:-1])
            return group(level, nm, [Item(49, nm + "-LEN", pic="S9(4)", usage="COMP"), Item(49, nm + "-TEXT", pic=f"X({n})")])
        if t.startswith("DECIMAL"):
            p, s = t[8:-1].split(",")
            p, s = int(p), int(s)
            pic = f"S9({p - s})" + (f"V9({s})" if s > 1 else "V9" if s == 1 else "")
            return Item(level, nm, pic=pic, usage="COMP-3")
        if t == "INTEGER":
            return Item(level, nm, pic="S9(9)", usage="COMP")
        if t == "SMALLINT":
            return Item(level, nm, pic="S9(4)", usage="COMP")
        if t == "DATE":
            return Item(level, nm, pic="X(10)")
        if t == "TIMESTAMP":
            return Item(level, nm, pic="X(26)")
        return Item(level, nm, pic="X(10)")


@dataclass
class Table:
    name: str            # POLICY_TBL
    dclgen: str          # copybook member name
    struct: str          # 01 name in the DCLGEN
    columns: List[Column]
    key: str             # key column

    @property
    def qualified(self) -> str:
        return f"{DB2_QUAL}.{self.name}"

    def host(self, col: str) -> str:
        return col.replace("_", "-")


def tables_for(system: str) -> List[Table]:
    key, klen = KEY_FIELD[system]
    kcol = key.replace("-", "_")
    s3 = SYS3[system]
    base = [Column(kcol, f"CHAR({klen})"), Column("STATUS_CD", "CHAR(2)"), Column("EFF_DT", "DATE"),
            Column("EXP_DT", "DATE", True), Column("AGENT_ID", "CHAR(6)"), Column("REGION_CD", "CHAR(3)"),
            Column(AMOUNTS[system][0].replace("-", "_"), "DECIMAL(11,2)"),
            Column(AMOUNTS[system][1].replace("-", "_"), "DECIMAL(11,2)"),
            Column("ITEM_CNT", "SMALLINT"), Column("INSURED_NAME", "VARCHAR(40)"),
            Column("LAST_UPD_TS", "TIMESTAMP"), Column("LAST_UPD_USER", "CHAR(8)")]
    mainname = {"POLICY": "POLICY_TBL", "CLAIMS": "CLAIM_TBL", "BILLING": "ACCOUNT_TBL"}[system]
    out = [Table(mainname, f"{s3}TBL", f"DCL{mainname.replace('_', '-')}", base, kcol)]
    hist = [Column(kcol, f"CHAR({klen})"), Column("SEQ_NO", "INTEGER"), Column("CHG_DT", "DATE"), Column("CHG_USER", "CHAR(8)"),
            Column("CHG_TYPE", "CHAR(1)"), Column("OLD_AMT", "DECIMAL(11,2)"), Column("NEW_AMT", "DECIMAL(11,2)"),
            Column("CHG_TEXT", "VARCHAR(80)")]
    out.append(Table(f"{s3}_HISTORY_TBL", f"{s3}HIS", f"DCL{s3}-HISTORY-TBL", hist, kcol))
    item = [Column(kcol, f"CHAR({klen})"), Column("ITEM_SEQ", "SMALLINT"), Column("ITEM_CD", "CHAR(4)"),
            Column("ITEM_LIMIT", "DECIMAL(13,2)"), Column("ITEM_PREM", "DECIMAL(9,2)"), Column("ITEM_DESC", "VARCHAR(30)")]
    out.append(Table(f"{s3}_ITEM_TBL", f"{s3}ITM", f"DCL{s3}-ITEM-TBL", item, kcol))
    agent = [Column("AGENT_ID", "CHAR(6)"), Column("AGENT_NAME", "VARCHAR(40)"), Column("BRANCH_CD", "CHAR(4)"),
             Column("COMM_PCT", "DECIMAL(5,4)"), Column("STATUS_CD", "CHAR(1)"), Column("SINCE_DT", "DATE")]
    out.append(Table(f"{s3}_AGENT_TBL", f"{s3}AGT", f"DCL{s3}-AGENT-TBL", agent, "AGENT_ID"))
    rate = [Column("RATE_CD", "CHAR(4)"), Column("EFF_DT", "DATE"), Column("RATE_PCT", "DECIMAL(7,4)"),
            Column("MIN_AMT", "DECIMAL(11,2)"), Column("MAX_AMT", "DECIMAL(11,2)"), Column("RATE_DESC", "CHAR(30)")]
    out.append(Table(f"{s3}_RATE_TBL", f"{s3}RAT", f"DCL{s3}-RATE-TBL", rate, "RATE_CD"))
    return out


def dclgen_lines(t: Table, library: str) -> Tuple[List[str], Item]:
    """The DCLGEN member's records (72 columns, columns 1-6 blank) and the 01 tree."""
    L: List[str] = []
    L.append("      ******************************************************************")
    L.append(f"      * DCLGEN TABLE({t.qualified})".ljust(71) + "*")
    L.append(f"      *        LIBRARY({library}({t.dclgen}))".ljust(71) + "*")
    L.append("      *        LANGUAGE(COBOL)".ljust(71) + "*")
    L.append("      *        QUOTE".ljust(71) + "*")
    L.append("      * ... IS THE DCLGEN COMMAND THAT MADE THE FOLLOWING STATEMENTS".ljust(71) + "*")
    L.append("      ******************************************************************")
    L.append(f"           EXEC SQL DECLARE {t.qualified} TABLE")
    for k, c in enumerate(t.columns):
        sep = "( " if k == 0 else "  "
        tail = "," if k < len(t.columns) - 1 else ""
        null = "" if c.nullable else " NOT NULL"
        L.append(f"           {sep}{c.name.ljust(30)} {c.sqltype}{null}{tail}")
    L.append("           ) END-EXEC.")
    L.append("      ******************************************************************")
    L.append(f"      * COBOL DECLARATION FOR TABLE {t.qualified}".ljust(71) + "*")
    L.append("      ******************************************************************")
    root = Item(1, t.struct, children=[c.cobol(10) for c in t.columns])
    return L, root


# --------------------------------------------------------------------------
# message tables and headings
# --------------------------------------------------------------------------

MESSAGES = {
    "POLICY": ["POLICY NUMBER NOT FOUND ON THE MASTER FILE", "POLICY STATUS NOT VALID FOR THIS TRANSACTION",
               "EFFECTIVE DATE IS AFTER THE EXPIRY DATE", "PREMIUM AMOUNT EXCEEDS THE AUTHORISED LIMIT",
               "AGENT IS NOT LICENSED IN THIS PROVINCE", "DUPLICATE POLICY NUMBER ON INPUT",
               "COVERAGE TABLE IS FULL - ITEM DROPPED", "INVALID PROVINCE CODE ON ADDRESS"],
    "CLAIMS": ["CLAIM NUMBER NOT FOUND ON THE CLAIM MASTER", "CLAIM IS CLOSED - NO PAYMENT ALLOWED",
               "LOSS DATE IS BEFORE THE POLICY EFFECTIVE DATE", "RESERVE AMOUNT BELOW PAID AMOUNT",
               "ADJUSTER NOT AUTHORISED FOR THIS SEVERITY", "DUPLICATE PAYMENT REQUEST",
               "CAUSE CODE NOT VALID FOR LINE OF BUSINESS", "LITIGATION FLAG REQUIRES A REASON CODE"],
    "BILLING": ["ACCOUNT NUMBER NOT FOUND ON THE BILLING MASTER", "ACCOUNT IS IN SUSPENSE - PAYMENT HELD",
                "DUE DATE IS BEFORE THE BILL DATE", "PAYMENT EXCEEDS THE OUTSTANDING BALANCE",
                "PAYMENT METHOD NOT ALLOWED FOR THIS PLAN", "DUPLICATE INVOICE NUMBER",
                "INSTALLMENT SCHEDULE IS FULL", "CYCLE CODE NOT VALID FOR DIRECT BILL"],
}


def message_table(system: str, pfx: str, rng: random.Random) -> Tuple[List[Item], List[str]]:
    """`01 xxx-MSG-TABLE.` of FILLER VALUEs and its REDEFINES: the codes E001.. and their texts."""
    msgs = MESSAGES[system]
    kids = []
    codes = []
    for k, m in enumerate(msgs, 1):
        code = f"E{k:03d}"
        codes.append(code)
        kids.append(Item(5, "FILLER", pic="X(50)", value=lit(f"{code} {m}".ljust(50)[:50])))
    tbl = Item(1, f"{pfx}-MSG-TABLE", children=kids)
    red = Item(1, f"{pfx}-MSG-TBL", redefines=f"{pfx}-MSG-TABLE",
               children=[group(5, f"{pfx}-MSG-ENTRY", [x(10, f"{pfx}-MSG-CODE", 4), x(10, "FILLER", 1),
                                                       x(10, f"{pfx}-MSG-TEXT", 45)], occurs=len(msgs))])
    return [tbl, red], codes


def report_headings(system: str, pfx: str, title: str) -> List[Item]:
    h1 = Item(1, f"{pfx}-HEADING-1", children=[
        x(5, "FILLER", 1, value=lit("1")), x(5, f"{pfx}-H1-DATE", 10), x(5, "FILLER", 30, value="SPACES"),
        Item(5, "FILLER", pic="X(60)", value=lit(f"{system} SYSTEM - {title} - RUN CONTROL REPORT".ljust(60)[:60])),
        x(5, "FILLER", 20, value="SPACES"), x(5, "FILLER", 5, value=lit("PAGE ")), Item(5, f"{pfx}-H1-PAGE", pic="ZZZ9")])
    h2 = Item(1, f"{pfx}-HEADING-2", children=[
        x(5, "FILLER", 1, value="SPACE"),
        Item(5, "FILLER", pic="X(80)", value=lit("KEY           NAME                            AMOUNT 1        AMOUNT 2     ST  DATE".ljust(80)[:80])),
        x(5, "FILLER", 51, value="SPACES")])
    return [h1, h2]
