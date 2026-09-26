# Synthetic-estate comparison - findings 2026-09-25

> **Status of this report (2026-09-25).** Written by `tools/synth/check.py` on the toolkit at main a0448c1. Every one of the 15 minimal reproductions under `tools/synth/repro/` still showed its symptom when `tools/synth/repro/verify.py` was run afterwards (32 checks REPRODUCED, 0 FIXED): they are errors of the toolkit, not of the generator. Three findings have no reproduction yet and may be the generator's own expectation: F25 (a dynamic CALL's variable name in the Calls table), F48 (a PROC's default datasets beside the job's), F51 (`flow --up` to a DB2 column). The fixes ride on the re-parse batch (branch reparse-batch-2); the harness is re-run there as the acceptance test.

> **The COBOL parser's findings, on the re-parse batch (ROADMAP re-parse item 26, LESSONS 211).** verify.py reports FIXED for every check of F01, F02, F04, F14 and F15. The report's rows they stand for: F43-F46 (DL/I calls in one sentence), F47 and the field cites F04, F06, F07, F09, F10, F14 (EXEC SQL statements in one sentence), F11, F12, F15, F19 (END-DATE), F18, F20 (a file name and INTEGER-OF-DATE as fields). F25 is the toolkit's, not the generator's: `EXEC CICS XCTL PROGRAM(WS-NEXT-PGM)` with WS-NEXT-PGM holding one literal was printed as its target alone, and `program`'s Calls table now shows the variable beside the targets it resolves to (`WS-NEXT-PGM -> BILONL02`), as it did for a CALL through a variable. POLUPD05's rows F16, F17, F21-F24 did not come from F15's shape: the 8-digit stub POLSTUBC, expanded as code, ran `01 WS-STUB-AREA-2.` on into the PROCEDURE DIVISION header (checked by building this estate with the toolkit at a0448c1); ROADMAP re-parse item 23 fixed them before item 26. The harness over the whole estate on the batch, before item 26 and after it: 428 findings (433 facts) and 310 (315), 10,886 facts matched and 11,053, none new; the findings left in the cobol module are F05 / F08 / F13 (EXEC SQL INCLUDE BILTBL, the reproduction F05) and F27 (the sort card's LENGTH token in `values`).

> **The expander's and the copybook parser's findings, on the re-parse batch (ROADMAP re-parse item 27, LESSONS 214).** verify.py reports FIXED for every check of F05, F06, F07 and F16 (a check whose truth and symptom are the same - the program's own view, right before the fix too - is a control and now prints FIXED; it printed REPRODUCED for ever). The report's rows they stand for: F05, F08, F13, F39, F41, F49 (EXEC SQL / INCLUDE BILTBL / END-EXEC. over three lines, the reproduction F05), F28, F29, F32, F33 (a copybook's own layout without its nested COPY's bytes, F06), F01-F03, F30, F31 (the PCB mask's `:PCB:` names, F07). The harness's truth for the CICS programs said `partial` for `COPY DFHAID NOT FOUND` and expected DFHAID in 'Copybooks not found' with 3 uses, which the reproduction F16 contradicts: it now says `ok` and expects DFHAID in coverage's 'IBM-supplied copybooks, not in the estate' with 0 programs parsed only in part for it and 3 copying it. The harness over the whole estate on the batch, before item 27 and after it: 310 findings (315 facts) and 62 (65), 11,053 facts matched and 11,358, none new. Found while writing item 27: `OCCURS 1 TO 10 DEPENDING ON X` written without TIMES lost its DEPENDING clause (LESSONS 215) - the generator writes TIMES, so the harness did not see it.

> **The JCL parser's findings, on the re-parse batch (ROADMAP re-parse item 29, LESSONS 222-224).** verify.py reports FIXED for every check of F09 and F10. The report's rows they stand for: F34, F35, F37, F38 (the shared sort PROC's card member named by `&CARDS`: the effective step now reads the job's member, its text and its byte positions, and coverage no longer names the PROC's default CMNSRT1) and F36, F40 (a (+1) read in the same job that wrote it: `input [gdg_same_job]`, not a second writer). Two reproductions were corrected on the way: F10's EXEC ran to column 82, past the 71 columns JCL reads, and is continued on a second line (the toolkit before the item still gave the default there); F09's second check said `input [gdg_relative]` where F36 says `gdg_same_job`. In the nightly jobs of this estate the job's override renames the extract step's (+1), so no earlier step writes the (+1) their sort reads: the index says `input [dd_convention]` there (a sort never writes its SORTIN) where the generator names the reason gdg_same_job; the checker compares the direction. The harness over the whole estate on the batch, before item 29 and after it: 62 findings (65 facts) and 27 (30), 11,358 facts matched and 11,393, none new; the JCL finding left is F42 (`crud --job POLNIGHT` without the sorted extract).

> **The query side's findings, on the re-parse batch (LESSONS 235-239; atlas/query.py and atlas/flow.py, no fact module).** verify.py reports FIXED for every check of F12, F13 and F17. The report's rows they stand for: F27 (`values PM-STATUS` took every number of the sort card as a value, and flagged the 2 of `INCLUDE COND=(13,2,CH,EQ,C'AC')` as used but not documented; it now reads each condition of the deck and keeps the constant after the operator of a condition on the field's bytes, LESSONS 235), F50 (`interfaces` listed each FTP put twice, as the dataset's row and as the step's 'external interface via FTP' row; it now gives one row per transfer, with the peer the step names, LESSONS 237) and F42 (`crud --job POLNIGHT` gave POLRPT01 POLEXTRT's `&&SORTED`, the first dataset any job gave its DD; it now gives the datasets of POLNIGHT's own steps - PROD.POL.EXTRACT.SORTED - and, without `--job`, every job's, each with the jobs that give it, LESSONS 238). F12's check looked only for the symptom, so it printed OTHER once the symptom was gone: it now counts the row `| 2 | (none) |` and the flag, with 0 as the truth. The two findings without a reproduction, judged against the generator: **F48 is the toolkit's.** README says `program X` 'falls back to the bare PROC only when no indexed job expands it'. Its 'Runs in' kept that rule and its Files table did not: the table listed the PROC's own step, read with the default HLQ=TEST, beside the jobs' PROD datasets. The Files table, `flow` (both walkers) and `values` now leave out a PROC's own step while an indexed job expands the PROC, and the Files table says how many rows it left out (LESSONS 236). **F51 is the toolkit's, and it was fixed before this stage.** Built with the toolkit at a0448c1, `flow STATUS-CD --program POLDB201 --up` read 'STATUS-CD is NOT DEFINED in POLDB201'. The DCLGEN POLTBL is included by `EXEC SQL` / `INCLUDE POLTBL` / `END-EXEC.` on three lines, which gave no copy_use row (F05), so its items were not the program's. On the batch since ROADMAP re-parse item 27 the page reads `EXEC SQL FETCH PRD.POLICY_TBL STATUS_CD` (the SELECT INTO of the same column is the same stop) and ends 'DB2 column: no static writer'. flow.py needed no change, and a test now pins the answer (LESSONS 239). The harness over the whole estate on the batch, before this stage and after it: 27 findings (30 facts) and 15 (16), 11,393 facts matched and 11,394, none new. The nine rows left (build and recover on the D1 / D2 defects, STDHDR's and CLMTRANR's layout) were in the run before this stage too, and none of them is in the query side.

> **The verifier's round on the query side (LESSONS 240-244).** The one row per transfer of F50 read the step by its member and line. A job running one FTP PROC twice, or two PROCs whose FTP step is on the same line, gave both steps one step's peer, and a step whose card member is not in the estate lost its row beside another step at that line. Each interface row is now tied to its own step (LESSONS 240, 241). The Files table's sentence said 'default symbolics' for every row of a PROC's own step, a literal DSN and a DUMMY among them; it now says 'rows of PROC members' own steps', as `dataset` does, and a DD with no dataset no longer prints as `None` (LESSONS 242). The `field` table of sort cards and the `copybook` counts still read a PROC's own default card while a job expands the PROC; F48's rule now holds in every reader of the sort cards (LESSONS 243). F12's two counts read 0 on an empty or crashed page too, so a third check is a control: the page must hold `| AC | R12-ACTIVE |` with its sort card. Found in passing, a fact of jcl.py for the re-parse night: a condition's operator (`INCLUDE COND=(13,2,NE,C'CN'),FORMAT=CH`), OR / AND and a BUILD list's X were stored as the format (ROADMAP re-parse item 30, LESSONS 244). verify.py: F12 F13 F17 FIXED (5 checks).


Estate: seed 20260925, scale 12: 268 members, 95 programs, 61 jobs, 75 copybooks, 15 DB2 tables, 5 DBDs, 9 PSBs, 17 transactions. Generated by `tools/synth/generate.py`, compared by `tools/synth/check.py` (this report is its output; rerun both to reproduce).

The flow: `build --rebuild`, a copybook changed and an incremental build, the arrived copybook dropped in, `recover --from listings`, an incremental build, then every query command over the samples, and the gate over a pack.

## Summary

- findings: **430** distinct (434 facts affected), in 9 modules; documented limitations met: 3
- matched: **10776** facts / sentences (the table at the end)
- samples: 58 programs, 25 jobs, 34 copybooks, 34 fields, 12 datasets, 6 tables, 5 DBDs, 17 transactions

## Timing

- build_rebuild: 2.55 s wall for 268 members = **105.0 members/s** (parse phase None s; this run {'new': 268, 'changed': 0, 'unchanged': 0, 'pruned': 0})
- build_incremental: 0.46 s wall for 268 members = **580.6 members/s** (parse phase None s; this run {'new': 0, 'changed': 3, 'unchanged': 265, 'pruned': 0})
- build_after-recover: 1.07 s wall for 268 members = **251.5 members/s** (parse phase None s; this run {'new': 2, 'changed': 35, 'unchanged': 233, 'pruned': 0})
- recover --from listings: 0.35 s
- generate: 0.56 s

| command | runs | mean s | max s |
|---|---|---|---|
| ambiguous | 1 | 0.25 | 0.25 |
| callees | 1 | 0.25 | 0.25 |
| callers | 2 | 0.26 | 0.27 |
| cite | 2 | 0.30 | 0.31 |
| column | 1 | 0.28 | 0.28 |
| conditions | 1 | 0.24 | 0.24 |
| copybook | 35 | 0.29 | 0.35 |
| coverage | 2 | 0.33 | 0.35 |
| crud | 2 | 0.25 | 0.25 |
| dataset | 12 | 0.32 | 0.42 |
| dbd | 5 | 0.29 | 0.33 |
| dead | 1 | 0.27 | 0.27 |
| diff | 1 | 0.29 | 0.29 |
| field | 34 | 0.26 | 0.31 |
| flow | 3 | 0.30 | 0.30 |
| interfaces | 1 | 0.26 | 0.26 |
| job | 27 | 0.25 | 0.29 |
| layout | 34 | 0.26 | 0.31 |
| literal | 2 | 0.25 | 0.25 |
| messages | 1 | 0.24 | 0.24 |
| pack | 1 | 0.30 | 0.30 |
| pair | 1 | 0.23 | 0.23 |
| paragraph | 1 | 0.24 | 0.24 |
| program | 61 | 0.27 | 0.34 |
| screen | 1 | 0.28 | 0.28 |
| search | 1 | 0.25 | 0.25 |
| segment | 4 | 0.28 | 0.30 |
| table | 6 | 0.31 | 0.33 |
| transaction | 17 | 0.34 | 0.49 |
| values | 1 | 0.25 | 0.25 |
| verify_citations | 2 | 0.20 | 0.20 |
| walk | 2 | 0.27 | 0.29 |

## Findings, by module, ranked by facts affected

Severity: `wrong` a wrong or missing fact; `cite` a right fact cited to the wrong line; `words` a promised sentence absent; `invented` a fact the estate does not hold; `silent` a gap the tool does not name; `noise` a right fact shown beside a misleading one; `status` a parse status.

### expand (154 facts, 3 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F01 | `field (REPLACING)` | BILIMS01 CMNPCBM, BILIMS01 CMNPCBM, BILIMS01 CMNPCBM, BILIMS01 CMNPCBM (+128 more) | alias :PCB:-DBD-NAME -> BIL-DBD-NAME | no field_alias row | 132 | wrong |  |
| F02 | `field (REPLACING)` | BILIMS01 CMNPCBM, BILIMS02 CMNPCBM, BILONL03 CMNPCBM, CLMIMS01 CMNPCBM (+7 more) | alias :PCB:-PCB -> BIL-PCB | no field_alias row | 11 | wrong |  |
| F03 | `field (REPLACING)` | BILIMS01 CMNPCBM, BILIMS02 CMNPCBM, BILONL03 CMNPCBM, CLMIMS01 CMNPCBM (+7 more) | alias :PCB:-OK -> BIL-OK | no field_alias row | 11 | wrong |  |

### cobol (94 facts, 24 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F04 | `field (references)` | BILDB201 WS-KEY-IN read, BILDB201 WS-KEY-IN read, BILDB201 WS-KEY-IN read, BILDB201 WS-KEY-IN read (+8 more) | a read reference at member line 83 (EXEC-SQL) | read references only at lines [82, 101, 117, 123] | 12 | cite |  |
| F05 | `program (copybooks)` | BILDB201 line 41, BILDB202 line 44, BILONL02 line 23, CLMDB201 line 41 (+5 more) | COPY BILTBL (EXEC SQL INCLUDE) | no copy_use row | 9 | wrong |  |
| F06 | `field (references)` | BILDB201 WS-OLD-AMT write, BILONL02 AGENT-NAME write, BILONL02 BRANCH-CD write, CLMDB201 WS-OLD-AMT write (+5 more) | a write reference at member line 83 (EXEC-SQL) | write references only at lines [82] | 9 | cite |  |
| F07 | `field (references)` | BILDB201 BT-USER-ID read, BILDB201 WS-OLD-AMT read, BILDB201 BT-USER-ID read, CLMDB201 CT-USER-ID read (+5 more) | a read reference at member line 109 (EXEC-SQL) | read references only at lines [101, 123] | 9 | cite |  |
| F08 | `program` | BILDB201, BILDB202, BILONL02, CLMDB201 (+5 more) | copybooks in the Copybooks table: BILTBL | absent from the report | 9 | wrong |  |
| F09 | `field (references)` | BILDB201 WS-SEQ-NO read, BILONL02 WS-AGENT-ID read, CLMDB201 WS-SEQ-NO read, CLMONL02 WS-AGENT-ID read (+2 more) | a read reference at member line 109 (EXEC-SQL) | read references only at lines [101] | 6 | cite |  |
| F10 | `field (references)` | BILDB201 BT-TRAN-AMT read, BILDB201 BT-TRAN-AMT read, CLMDB201 CT-TRAN-AMT read, CLMDB201 CT-TRAN-AMT read (+2 more) | a read reference at member line 109 (EXEC-SQL) | read references only at lines [101, 119, 123] | 6 | cite |  |
| F11 | `field (references)` | BILEDIT END-DATE, BILHIS01 END-DATE, CLMEDIT END-DATE, CLMHIS01 END-DATE (+2 more) | a write reference at member line 71 (MOVE) | no write reference (modes seen: []) | 6 | wrong |  |
| F12 | `field (references)` | BILEDIT BM-DUE-DT, CLMEDIT CM-REPORT-DT, CMNDATE END-DATE, POLEDIT PM-EXPIRY-DT | a read reference at member line 71 (MOVE) | no read reference (modes seen: []) | 4 | wrong |  |
| F13 | `copybook` | BILAGT, BILITM, BILTBL, CLMAGT | programs including it: BILONL02 | absent from the report | 4 | wrong |  |
| F14 | `field (references)` | BILDB201 STATUS-CD write, CLMDB201 STATUS-CD write, POLDB201 STATUS-CD write | a write reference at member line 83 (EXEC-SQL) | write references only at lines [54, 67, 82] | 3 | cite |  |
| F15 | `field (references)` | BILHIS01 WS-PURGE-DATE, CLMHIS01 WS-PURGE-DATE, POLHIS01 WS-PURGE-DATE | a read reference at member line 57 (MOVE) | no read reference (modes seen: ['write']) | 3 | wrong |  |
| F16 | `program (files)` | POLUPD05 TRAN-FILE, POLUPD05 TRAN-FILE | CLOSE at line 33 | ops recorded: [] | 2 | wrong |  |
| F17 | `paragraph` | POLUPD05 0000-MAIN, POLUPD05 1000-PROCESS | a paragraph row | none | 2 | wrong |  |
| F18 | `field (references)` | file names | field references to data items only | a SELECT file name recorded as a field reference (HIST-FILE in BILDB201, and others) | 1 | noise |  |
| F19 | `field (references)` | CMNDATE END-DATE | a test reference at member line 24 (IF) | no test reference (modes seen: []) | 1 | wrong |  |
| F20 | `field (references)` | CMNDATE | references only to declared data names | references to undeclared names: ['INTEGER-OF-DATE'] | 1 | wrong |  |
| F21 | `program (files)` | POLUPD05 TRAN-FILE | OPEN INPUT at line 31 | ops recorded: [] | 1 | wrong |  |
| F22 | `paragraph / walk` | POLUPD05 line 32 | perform 1000-PROCESS | no edge | 1 | wrong |  |
| F23 | `field (references)` | POLUPD05 PW-EOF | a test reference at member line 32 (PERFORM) | no test reference (modes seen: []) | 1 | wrong |  |
| F24 | `field (references)` | POLUPD05 PW-READ-CNT | a write reference at member line 39 (ADD) | no write reference (modes seen: []) | 1 | wrong |  |
| F25 | `program` | BILONL01 | call targets in the Calls table: WS-NEXT-PGM | absent from the report | 1 | wrong |  |
| F26 | `field` | PW-READ-CNT write | programs with a write reference: ['POLEXT01', 'POLGEN01', 'POLGEN02', 'POLGEN03', 'POLGEN04', 'POLGEN05', 'POLGEN06', 'POLGEN07', 'POLGEN08', 'POLGEN09', 'POLGEN10', 'POLGEN11', 'POLGEN12', 'POLHIS01', 'POLIMS01', 'POLOLD01', 'POLRPT01', 'POLUPD01', 'POLUPD03', 'POLUPD04', 'POLUPD05', 'POLUPD06', 'P | missing ['POLUPD05'] | 1 | wrong |  |
| F27 | `values` | PM-STATUS | only values the code holds (the INCLUDE card compares bytes 13-14 with C'AC') | a value `2` from the sort card's LENGTH token, flagged USED BUT NOT DOCUMENTED | 1 | invented |  |

### copybook (72 facts, 6 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F28 | `layout / field` | POLMASTR PM-LINE-CD, POLMASTR PM-AGENT-DATA, POLMASTR PM-AGENT-ID, POLMASTR PM-COMMISSION-PCT (+33 more) | offset 215 length 2 _(the ADDRESS group is a nested COPY: its 121 bytes sit inside the record, so every item after it is 121 bytes further on than the copybook's own text suggests)_ | offset 94 length 2 | 37 | wrong |  |
| F29 | `layout` | BILMASTR BM-DUNNING, BILMASTR BM-AGENT-DATA, BILMASTR BM-AGENT-ID, BILMASTR BM-COMMISSION-PCT (+13 more) | offset 215 len 1 _(the ADDRESS group is a nested COPY: its 121 bytes sit inside the record, so every item after it is 121 bytes further on than the copybook's own text suggests)_ | offset 94 len 1 | 17 | wrong |  |
| F30 | `layout / field` | CMNPCBM :PCB:-PCB, CMNPCBM :PCB:-DBD-NAME, CMNPCBM :PCB:-SEG-LEVEL, CMNPCBM :PCB:-STATUS (+6 more) | a field row at offset 0 len 76 | no row | 10 | wrong |  |
| F31 | `field` | CMNPCBM 88 :PCB:-OK, CMNPCBM 88 :PCB:-END-OF-DB, CMNPCBM 88 :PCB:-NOT-FOUND, CMNPCBM 88 :PCB:-DUPLICATE | an 88-level row | no row | 4 | wrong |  |
| F32 | `layout` | BILMASTR, STDHDR | record length 511 _(the ADDRESS group is a nested COPY: its 121 bytes sit inside the record, so every item after it is 121 bytes further on than the copybook's own text suggests)_ | record length 390 | 2 | wrong |  |
| F33 | `field` | PM-ITEM-TBL in POLMASTR, PM-FILLER in POLMASTR | offset 229 _(the ADDRESS group is a nested COPY: its 121 bytes sit inside the record, so every item after it is 121 bytes further on than the copybook's own text suggests)_ | offset 108 | 2 | wrong |  |

### jcl (39 facts, 9 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F34 | `job / field (sort cards)` | BILWEEK WEEK3.SRT010, BILWEEK WEEK3.SRT010, BILWEEK WEEK3.SRT010, BILWEEK WEEK3.SRT010 (+8 more) | SORT bytes 1-12 | [] | 12 | wrong |  |
| F35 | `job` | BILWEEK SRT010, BILWEEK SRT010, BILWEEK SRT010, BILWEEK SRT010 (+8 more) | sort card byte positions: SORT 1-12 | absent from the report | 12 | wrong |  |
| F36 | `job / dataset` | BILNIGHT NIGHT.PS030 SORTIN (PROD.BIL.EXTRACT), CLMNIGHT NIGHT.PS030 SORTIN (PROD.CLM.EXTRACT), POLNIGHT NIGHT.PS030 SORTIN (PROD.POL.EXTRACT) | direction input [gdg_same_job] _(a (+1) generation created by an earlier step of the same job is read here)_ | output [gdg_relative] | 3 | wrong |  |
| F37 | `job` | BILWEEK WEEK3.SRT010 SYSIN, CLMWEEK WEEK3.SRT010 SYSIN, POLWEEK WEEK3.SRT010 SYSIN | cards from member BILSORT1 | CMNSRT1 | 3 | wrong |  |
| F38 | `job` | BILWEEK SRT010 SYSIN, CLMWEEK SRT010 SYSIN, POLWEEK SRT010 SYSIN | the card member's text loaded: card lines loaded from member | absent from the report | 3 | wrong |  |
| F39 | `copybook` | BILITM | jobs running the including programs: BILDB2LD, BILWEEK | absent from the report | 2 | wrong |  |
| F40 | `dataset` | PROD.POL.EXTRACT POLNIGHT PS030, PROD.CLM.EXTRACT CLMNIGHT PS030 | direction input [gdg_same_job] _(a (+1) generation created by an earlier step of the same job is read here)_ | output | 2 | wrong |  |
| F41 | `copybook` | BILTBL | jobs running the including programs: BILDB2LD | absent from the report | 1 | wrong |  |
| F42 | `crud --job` | POLNIGHT | programs and files of the job: PROD.POL.EXTRACT.SORTED | absent from the report | 1 | wrong |  |

### ims (29 facts, 4 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F43 | `program (DL/I)` | BILIMS01 line 47, BILIMS01 line 64, BILONL03 line 64, BILONL03 line 65 (+9 more) | CHKP on IO-PCB | no dli_call row | 13 | wrong |  |
| F44 | `program (DL/I)` | BILIMS01 line 45, BILONL03 line 55, CLMIMS01 line 45, CLMIMS02 line 38 (+3 more) | GU on BIL-PCB | no dli_call row | 7 | wrong |  |
| F45 | `program (DL/I)` | BILIMS01 line 70, BILIMS02 line 40, CLMIMS01 line 70, CLMIMS02 line 49 (+2 more) | GN on BIL-PCB | no dli_call row | 6 | wrong |  |
| F46 | `program (DL/I)` | BILIMS01 line 74, CLMIMS01 line 74, POLIMS01 line 74 | GNP on BIL-PCB | no dli_call row | 3 | wrong |  |

### db2 (27 facts, 1 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F47 | `table / column / crud` | BILDB201 line 55, BILDB201 line 59, BILDB201 line 83, BILDB201 line 109 (+23 more) | EXEC SQL OPEN on ['PRD.ACCOUNT_TBL'] cited at its own line | a OPEN row cited at member line 54 | 27 | cite |  |

### query (16 facts, 3 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F48 | `program` | BILEXT01, BILRPT01, BILUPD01, CLMEXT01 (+5 more) | datasets via JCL from the jobs that run it (PROD.*) _(README: the bare PROC only when no indexed job expands it)_ | the PROC's default symbolic datasets (TEST.*) listed beside them | 9 | noise |  |
| F49 | `copybook` | BILAGT, BILITM, BILTBL, CLMAGT | 1 programs including it | (0) | 4 | wrong |  |
| F50 | `interfaces` | FTP steps | one row per FTP put (3) | 6 ftp rows (each put listed twice) | 3 | noise |  |

### flow (2 facts, 1 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F51 | `flow --up` | STATUS-CD in POLDB201 | the DB2 column it is read from: POLICY_TBL, STATUS_CD | absent from the report | 2 | wrong |  |

### classify (1 facts, 1 rows)

| id | command | member(s) | truth says | tool says | facts | severity | repro |
|---|---|---|---|---|---|---|---|
| F52 | `program / copybook` | POLSTUBC (an 8-digit stub) | reported as a stub (no code: its text sits in columns 1-8) | resolved as a copybook with no fields, nothing said | 1 | silent |  |

### Names printed that the estate does not hold (candidates for invented facts - triaged below)

| name | times | first seen in |
|---|---|---|
| FIELD-NAME | 32 | layout |
| FIELD | 30 | layout |
| MEMBER | 30 | layout |
| SYNC | 29 | layout |
| WRITTEN | 25 | copybook |
| FOUND | 23 | program |
| APPLCTN | 20 | program |
| GPSB | 17 | transaction |
| INTENDED | 16 | program |
| CYCLE | 10 | program |
| TABLE | 10 | job |
| RESTART | 9 | program |
| RUNMODE | 9 | program |
| WKRPT | 9 | program |
| AGENT | 8 | program |
| BATCH | 6 | program |
| USER | 6 | program |
| DELTA | 6 | program |
| BILWKLY | 6 | program |
| DEFAULT | 6 | job |
| VIEW | 6 | table |
| BIND | 6 | table |
| QUALIFIER | 6 | table |
| PREMIUM | 5 | field |
| ITEM | 5 | table |
| THIS | 4 | program |
| CLAIM | 4 | program |
| SYSLIB | 4 | program |
| EXEC-SQL | 4 | field |
| ACCOUNT | 3 | program |
| TOTAL | 3 | program |
| CLMWKLY | 3 | program |
| POLWKLY | 3 | program |
| RESUME | 3 | job |
| SYSTEM | 3 | job |
| INDDN | 3 | job |
| RPTDT | 3 | job |
| NOOP | 3 | job |
| LAST | 3 | table |
| INSURED | 3 | table |
| MSGTYPE | 3 | transaction |
| SNGLSEG | 3 | transaction |
| POLGRP | 3 | transaction |
| CLMGRP | 3 | transaction |
| BILGRP | 3 | transaction |
| BILQ | 2 | program |
| CLMQ | 2 | program |
| POLQ | 2 | program |
| DEFINED | 2 | field |
| VERSION | 2 | field |
| REINSURER-X | 2 | dataset |
| RESERVE | 2 | table |
| PAID | 2 | table |
| CODE | 2 | values |
| EFFECTIVE | 2 | literal |
| BILL | 2 | literal |
| EXPIRY | 2 | literal |
| ORIGINAL | 2 | walk |
| BILTSQ01 | 1 | program |
| SMITH | 1 | program |

### Documented limitations met (not findings)

| command | member | truth says | tool says |
|---|---|---|---|
| `layout` | CMNDATEA | field rows | none until the next full re-parse (documented, LESSONS 186) |
| `conditions` | POLUPD01 IF NOT DATE-OK | the 88 DATE-OK expanded to its value | absent: the re-filed copybook has no 88 rows until the next full re-parse (LESSONS 186) |
| `literal` | E003 in CLMMSGT | the code inside a 50-byte FILLER literal | not found as a code (documented: `literal` finds codes kept in their own FILLER) |

## What matched

| check | count |
|---|---|
| field reference exact (name, mode, line) | 1221 |
| copybook field present | 851 |
| copybook field line exact | 851 |
| copybook offset/length exact | 814 |
| program: cites inside the member | 508 |
| copybook: cites inside the member | 491 |
| layout offsets exact | 326 |
| member indexed | 268 |
| member kind | 268 |
| member status | 268 |
| field: cites inside the member | 242 |
| copy_use row exact | 211 |
| copy resolution as expected | 211 |
| program: copybooks in the Copybooks table printed | 207 |
| io_op present | 200 |
| DSN resolved exact | 196 |
| paragraph present | 182 |
| literal reference exact | 172 |
| field references by program | 148 |
| perform edge exact | 145 |
| job: resolved dataset names printed | 145 |
| copybook: programs including it printed | 145 |
| copybook: jobs running the including programs printed | 134 |
| field alias exact | 121 |
| db2 columns exact | 114 |
| DD direction exact | 111 |
| job: cites inside the member | 106 |
| sql column lineage exact | 93 |
| incremental: untouched programs kept | 92 |
| effective step present | 91 |
| job: step headings printed | 91 |
| 88 values exact | 88 |
| effective program exact | 79 |
| file_decl exact | 62 |
| dataset: cites inside the member | 62 |
| program: SELECT names in the Files table printed | 60 |
| program row | 59 |
| linkage USING exact | 59 |
| table: declared columns printed | 56 |
| call edge exact (kind, target, line) | 51 |
| program: call cite points at the CALL line | 51 |
| gate: pack cites verified | 45 |
| job/step running the program exact | 44 |
| program: call targets in the Calls table printed | 44 |
| job: effective programs printed | 43 |
| program: jobs in Runs in printed | 40 |
| dataset row present | 40 |
| dataset direction exact | 38 |
| field definition present | 37 |
| field offset exact | 35 |
| recover: marked exactly the programs that need the build | 32 |
| layout 88 printed | 31 |
| cics command exact | 29 |
| record length exact | 26 |
| ims field exact | 25 |
| job row | 25 |
| dbd: fields at their bytes printed | 25 |
| table: cites inside the member | 24 |
| job: effective step cited to its PROC member | 21 |
| callers: direct callers printed | 21 |
| sql statement at its line | 18 |
| inline cards kept | 18 |
| sort card position exact | 18 |
| job: sort card byte positions printed | 18 |
| table CRUD exact | 18 |
| transaction routing exact | 17 |
| pcb exact | 16 |
| screen field exact | 15 |
| dli call exact | 14 |
| dli database resolved exact | 14 |
| transaction: the routed program printed | 14 |
| coverage member counts exact | 14 |
| dbd: PSBs addressing it printed | 10 |
| dbd: programs whose calls resolve to it printed | 10 |
| recover: sentence present | 9 |
| psb present | 9 |
| card member loaded | 9 |
| program: transactions on the Online line printed | 9 |
| job: the card member's text loaded printed | 9 |
| segment: programs on a sensitive PCB printed | 9 |
| transaction: the program is indexed printed | 9 |
| screen: the fields and the program printed | 8 |
| column: writers, readers and the declared type printed | 8 |
| segment exact | 7 |
| program: chosen-copybook wording | 7 |
| dbd: segments printed | 7 |
| coverage: chosen program in its table | 7 |
| recover: recovered text exact | 6 |
| screen constant exact | 6 |
| IF guard present | 6 |
| job: the IF guard printed | 6 |
| coverage call resolution counts exact | 6 |
| values: the documented values and their 88s printed | 6 |
| paragraph: who reaches it and what it does printed | 6 |
| interfaces: FTP, manifest peers and TD queues printed | 6 |
| dead: live program not listed | 6 |
| dbd present | 5 |
| transaction: the program is not in the index printed | 5 |
| callees: direct callees printed | 5 |
| crud --job: programs and files of the job printed | 5 |
| coverage: healed copybook gone from not-found | 4 |
| flow: the MOVE targets and the CALL USING hop printed | 4 |
| recover: report sentence present | 3 |
| entry alias present | 3 |
| dynamic call resolved exact | 3 |
| section present | 3 |
| db2 utility table exact | 3 |
| transaction: no program (REMOTESYSTEM) printed | 3 |
| coverage not-found row exact | 3 |
| coverage: the folder-decided sentence printed | 3 |
| condition literal printed | 3 |
| literal: the programs that MOVE it printed | 3 |
| messages: the message tables holding such texts printed | 3 |
| walk: the procedure copybook's paragraphs, cited to the copybook printed | 3 |
| flow: the file, the reader and its field at the same bytes printed | 3 |
| dead: the retired program printed | 3 |
| job: the shared sort PROC with the job's symbolics (CARDS=POLSORT1) printed | 3 |
| incremental: copier re-parsed | 2 |
| recover: re-filed exactly the two misfiled-by-content copybooks | 2 |
| recover: listing check counts | 2 |
| program: listing-says sentence (D2) | 2 |
| copybook: the re-filed sentence printed | 2 |
| coverage: the chosen-copybook sentences printed | 2 |
| coverage: the listing-check sentence printed | 2 |
| callees: both dynamic targets printed | 2 |
| crud: DB2 tables printed | 2 |
| walk: the paragraph nothing reaches printed | 2 |
| walk: the re-filed copybook's fields in the data section printed | 2 |
| screen: the two constant labels printed | 2 |
| ambiguous: the two duplicate names with different content printed | 2 |
| program: the job that runs a program with no source printed | 2 |
| incremental: changed copybook re-parsed | 1 |
| recover: wrote only the missing copybook | 1 |
| xdfld present | 1 |
| program: D3 wording | 1 |
| program: D6 stub wording | 1 |
| copybook: recovered wording | 1 |
| copybook: the stub verdict printed | 1 |
| copybook: the D3 sentence printed | 1 |
| dataset: crosses departments said | 1 |
| coverage partial split adds up | 1 |
| coverage: the stub in the not-found table printed | 1 |
| coverage: stub verdict | 1 |
| crud DB2 letters exact | 1 |
| crud IMS row present | 1 |
| crud CICS row present | 1 |
| pair: the co-referencing program printed | 1 |
| literal: message-table definition found (split FILLERs) | 1 |
| literal: the MOVE that names the dynamic target printed | 1 |
| diff: the field only the CLAIMS copy has printed | 1 |
| cite exact | 1 |
| program: the two copies printed | 1 |
| job: the three jobs that execute the shared PROC printed | 1 |
| copybook: the listings' libraries printed | 1 |
| gate: wrong cite rejected | 1 |
| gate: a job/PROC name resolved by the token | 1 |
| flow --up: the DB2 column it is read from printed | 0 |

## Samples

- programs (58): BILDB201, BILDB202, BILEDIT, BILEXT01, BILHIS01, BILIMS01, BILIMS02, BILOLD01, BILONL01, BILONL02, BILONL03, BILRATE1, BILRPT01, BILSUB01, BILUPD01, CLMDB201, CLMDB202, CLMEDIT, CLMEXT01, CLMHIS01, CLMIMS01, CLMIMS02, CLMOLD01, CLMONL01, CLMONL02, CLMONL03, CLMRATE1, CLMRPT01, CLMSUB01, CLMUPD01, CMNABEND, CMNCUST1, CMNDATE, CMNERR, CMNSQLER, CMNUTIL, POLDB201, POLDB202, POLEDIT, POLEXT01, POLHIS01, POLIMS01, POLIMS02, POLOLD01, POLONL01, POLONL02, POLONL03, POLRATE1, POLRATE2, POLRPT01, POLSUB01, POLUPD01, POLUPD02, POLUPD03, POLUPD04, POLUPD05, POLUPD06, POLUPD07
- jobs (25): BILDB2LD, BILEXTRT, BILFTP, BILIMSBT, BILNIGHT, BILPURGE, BILRERUN, BILWEEK, CLMDB2LD, CLMEXTRT, CLMFTP, CLMIMSBT, CLMNIGHT, CLMPURGE, CLMRERUN, CLMWEEK, CMNHOUSE, POLDB2LD, POLEXTRT, POLFTP, POLIMSBT, POLNIGHT, POLPURGE, POLRERUN, POLWEEK
- copybooks (34): BILADDRA, BILAGT, BILCOMMA, BILCTLR, BILEXTR, BILGEN1R, BILGEN2R, BILHIS, BILHISTR, BILITM, BILMAPS, BILMASTR, BILMSGT, BILRAT, BILRPTL, BILSEGA, BILSEGB, BILTBL, BILTRANR, BILWORKA, CLMADDRA, CLMAGT, CLMCOMMA, CLMCTLR, CLMEXTR, CLMGEN1R, STDHDR, POLMISSB, POLSTUBB, POLPROCB, CMNDATEA, CMNCUSTP, POLARRVB, POLSTUBC
- fields (34): PM-STATUS, PM-POLICY-NO, PM-INSURED-NAME, PM-ITEM-TBL, PM-FILLER, PM-CITY, CM-CLAIM-NO, BM-ACCOUNT-NO, PT-TRAN-AMT, PT-TRAN-TYPE, CT-NEW-CHANNEL-CD, PX-NAME-TEXT, PH-CHG-TYPE, PC-RUN-DATE, PW-EOF-SW, PW-READ-CNT, PA-RETURN-CODE, ERR-CODE, START-DATE, DATE-RC, CF-CUST-KEY, PS-STATUS, PSC-ITEM-PREM, STATUS-CD, PREMIUM-AMT, HDR-PROGRAM, PR-AMT-1, POLICY-NO, PMSS-AUDIT-KEY, PARR-RESTART-SW, WS-RTN-NAME, SSA-ROOT-KEY, CS-INSURED-NAME, BW-TOTAL-AMT
- datasets (12): PROD.POL.MASTER.KSDS, PROD.POL.EXTRACT, PROD.POL.EXTRACT.SORTED, PROD.CLM.TRANS, PROD.BIL.HISTORY, PROD.POL.UNLOAD, PROD.CMN.CUSTOMER.KSDS, PROD.CLM.MASTER.KSDS, STG.POL.EXTRACT, PROD.BIL.REPORT.DAILY, PROD.POL.GSAM.OUT, PROD.CLM.EXTRACT
- tables (6): PRD.POLICY_TBL, PRD.CLAIM_TBL, PRD.ACCOUNT_TBL, PRD.POL_HISTORY_TBL, PRD.CLM_ITEM_TBL, PRD.BIL_AGENT_TBL

