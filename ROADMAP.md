# Coverage and roadmap

This file answers one question honestly: **for each thing a large insurance
mainframe estate contains, and each question a developer asks of it, does
the toolkit produce facts, and is anything still missing?**

It comes from a coverage audit run on 2026-09-12: six independent readers
each took one lens (the estate's libraries and artefact types; JCL and
batch; IMS / DB2 / CICS / MQ / interfaces; the questions a developer asks in
analysis, design, development and testing; operations, safety and scale; a
final adversarial consolidation), read the code, built probes against it,
and returned ranked gaps with a fixture for each. 128 gaps were found and
consolidated into 30 "implement now" items and 13 roadmap items. Everything
below the line marked **done** has a regression test in `tests/` and a row
in `LESSONS.md`.

Run the numbers yourself: `python -m atlas.query --db atlas.db coverage`
prints what contributes facts in *your* index, what was silently skipped,
which optional inputs are loaded, and which fetched libraries are
incomplete.

## Coverage matrix (as of 2026-09-12)

`facts` = parsed into rows a query uses; `text` = searchable and citable
only; `no` = not handled.

| Artefact / question | Coverage | Notes |
|---|---|---|
| COBOL batch program: calls, copies, files, paragraphs, sections, GO TO / fall-through, ENTRY points, OPEN lists, WRITE→file, SORT procedures | facts | 96 parser lessons; EXEC spans masked; `PERFORM n TIMES` never an edge |
| COBOL CICS: LINK/XCTL with COMMAREA, START/RETURN TRANSID chains, SEND/RECEIVE MAP, TD/TS queues, containers, web services, FILE(var), host fields | facts | `cics_cmd`; `transaction` shows "reached from code"; `screen` shows SEND/RECEIVE facts |
| COBOL IMS DL/I: function in a VALUE field, parm-count, CHNG message switch, EXEC DLI operands, ENTRY 'DLITCBL', PCB → database/PROCOPT through the PSB, GSAM → JCL DD | facts | `post_pcb_positions` stores the reasoning per call; `dbd` / `segment` queries |
| COBOL DB2: tables, cursors, column lineage, DCLGEN DECLARE TABLE, `SELECT * INTO :group`, FETCH ROWSET, qualified = unqualified, stored-procedure CALL | facts | `table` query (CRUD per program, columns, batch LOAD/UNLOAD) |
| COBOL MQ: queue name, direction, message layout | facts | from MQOD-OBJECTNAME VALUE/MOVE; run-time names reported as such |
| Copybook layouts: offsets, COMP/COMP-3, group USAGE, anonymous FILLER, 77, 88 VALUES, ODO, REDEFINES, PIC CR/DB | facts | `layout` query; SYNC alignment slack still **not** modelled |
| COPY REPLACING: pseudo-text prefix renames, LEADING/TRAILING, level renumbering, field aliases | facts | `field PM-X` finds the program's `LK-X` |
| JCL: jobs, several JOB cards, PROCs (cataloged, instream, system), INCLUDE, overrides (entry-by-entry, DUMMY, PARM.PS=), symbolics (EXEC > PROC default > SET), JOBLIB, IF/ELSE guards, JOB COND, referbacks, `&&TEMP`, GDG (+1)/(0)/G0001V00, DUMMY/NULLFILE, PATH=, INTRDR, run-time symbols, trailing comments, quoted PARM continuation | facts | JCLLIB / department-aware PROC choice; `ambiguous_proc` reported |
| Control cards: instream, `DSN=LIB(MEMBER)`, sequential card datasets, TOOLIN/xxxxCNTL/DFSPARM/SYMNAMES, sort byte positions incl. `c:p,l` and symbols, OUTFIL/JOINKEYS/ICETOOL DD roles, IDCAMS DEFINE attributes, DB2 utility LOAD/UNLOAD/DSNTIAUL, Easytrieve FILE/fields, FTP put/get (credentials redacted), Connect:Direct process members | facts | cards the JCL names but the estate folder lacks are listed by `coverage` |
| IMS DBD (segments, FIELDs, XDFLD, DATASET DD), PSB (positions, LIST=NO, PROCSEQ, CMPAT), stage-1 (also inside JCL SYSIN; SPA, INQUIRY, DATABASE) | facts | |
| MFS (MID/MOD offsets incl. inherited DFLD lengths, ATTR=(YES,n)), BMS (fields, symbolic names) | facts | MFS message ↔ program I/O-area field join: **roadmap RM-03** |
| CICS CSD: TRANSACTION/PROGRAM/FILE, TDQUEUE→dataset, DB2ENTRY/DB2TRAN plans, URIMAP→program, every other block kept | facts | |
| Interfaces: FTP/NDM/USS steps, MQ, IMS switches, TD queues, web entry points, manifest-declared peers | facts | `interfaces` query; peers only from the manifest |
| Documents (Office/PDF, OCR of pictures, recordings: screens by OCR and speech by the Windows recogniser or a caption file) | text (cited by section) | never facts, by design; speech recognition of jargon is weak - caption files are better |
| Scheduler: CSV loader; INTRDR submissions | facts | Zeke / Control-M / CA-7 native exports: **roadmap RM-07** (reported as `scheduler_format`) |
| Estate freshness: host member list vs folder, INCOMPLETE libraries, NOT FETCHED, "index built" header, parser/manifest change forces re-parse, own outputs never re-indexed | facts | incremental (stats-based) refresh: **roadmap RM-09** |
| Compiler listings | classified, not parsed | **roadmap RM-01** (abend offset → statement) |
| DB2 DDL / BIND cards / catalog unloads | classified (`sql`, `unknown`) | **roadmap RM-02**; `table` exists, views/qualifiers not resolved |
| Easytrieve / Assembler / PL/I / REXX / CLIST / SAS source | classified or text | **roadmap RM-05**; Easytrieve steps already yield files and fields from JCL |
| Link-edit cards, load-module aliases, compile-PROC SYSLIB | no | **roadmap RM-06** |
| Nested COBOL programs, COPY inside a copybook | partial | **roadmap RM-08** |
| Multi-hop lineage, batch order, restart, release diff, clones, conventions, hotspots | no (one-hop rows with cites) | **roadmap RM-04 / RM-10** |

## Implemented from the audit (all with tests)

| # | Item | Status |
|---|---|---|
| IN-01 | EXEC spans leaking into COBOL verb extractors; EXEC SQL CALL | done |
| IN-02 | EXEC CICS facts persisted (maps, TRANSIDs, queues, COMMAREA, host fields) | done |
| IN-03 | OPEN/CLOSE lists, WRITE→file, SORT procedures, SD files | done |
| IN-04 | EJECT/SKIP/TITLE, ID-division comment-entries | done |
| IN-05 | CALL parsing: USING lists, every CALL in a sentence, LENGTH OF, subscripts, OF, 88-driven targets | done |
| IN-06 | DL/I function in a variable, parm-count, message switches, ENTRY 'DLITCBL' | done |
| IN-07 | PCB positions resolved; `dbd` / `segment` queries; EXEC DLI operands; DBD detail; LIST=NO | done |
| IN-08 | SECTIONs, GO TO, PERFORM n TIMES, fall-through | done |
| IN-09 | Copybook arithmetic corners | done |
| IN-10 | COPY REPLACING renames and corners | done |
| IN-11 | DCLGEN DECLARE TABLE, SELECT * INTO :group, no false `procedure_copybook` | done |
| IN-12 | Qualified/unqualified tables, FETCH cursor, SQL `--` comment ending in a period | done |
| IN-13 | Classification: CNTL, listings, unknown-not-doc, empty members, card decks in JCL libraries | done |
| IN-14 | JCL facts persisted and queried (guards, multi-JOB, extra RUNs, JCLLIB) | done |
| IN-15 | `dataset`: online CICS updaters, override double-counting, PROC-default phantoms, IEFBR14 | done |
| IN-16 | Citation gate: kind resolution, department qualifiers, weak tokens, comment lines, sha check, `MEMBER:line` | done |
| IN-17 | Report completeness: cites everywhere, `field` grouping / `--all` / 88 names / aliases, PROC "executed by", literal message text | done |
| IN-18 | Packs with budget, token header, no paths, COND evidence; `pack job` / `copybook` / `transaction` / `field`; `layout` | done |
| IN-19 | MQ queue names, `interfaces` report, Connect:Direct process members, manifest peers | done (MQSC definitions: roadmap) |
| IN-20 | Fetch reconciliation (INCOMPLETE, NOT FETCHED, stale members), "index built" header | done |
| IN-21 | Incremental correctness: parser/manifest fingerprint, PROC change → jobs, nested copybooks, own outputs skipped | done |
| IN-22 | Symbol precedence EXEC > PROC default > SET; job-level PARM/PGM substitution | done (confirm once on your system - see `jcl.PROC_DEFAULT_BEATS_SET`) |
| IN-23 | System PROCs synthesised, INTRDR, DD PATH= | done |
| IN-24 | Sequential card datasets, DB2 utility cards, SYMNAMES/ICETOOL/OUTFIL/JOINKEYS, IDCAMS attributes | done (DCB=/LRECL on the DD: not stored) |
| IN-25 | CSD extra types, stage-1 in JCL, SPA/INQUIRY/DATABASE | done (PROGRAM REMOTESYSTEM: not stored) |
| IN-26 | MFS lengths and extended attributes | done |
| IN-27 | ENTRY aliases and stored-procedure calls in call resolution | done |
| IN-28 | `crud`, `conditions`, `paragraph`, `callers --args`, `dead` beyond programs | done |
| IN-29 | `coverage` names what contributes facts and what was skipped | done |
| IN-30 | Manifest overwrite protection, credential redaction, subprocess decoding, diag redaction | done (db lock / UI cancel / FTS scan speed: roadmap RM-13) |

## Roadmap (not yet built, in priority order)

1. **RM-01 Compiler listings** (P1, large). Parse the Enterprise COBOL
   LIST/OFFSET table and the Data Division Map; `abend PGM +HEX` → statement
   line, verb, the COMP-3 operand. Until then abend triage is manual and the
   listing's offsets cannot cross-check the parser's. *Needs one real listing
   (first 200 lines, names changed) to build against.*
2. **RM-02 DB2 DDL / BIND / catalog** (P1, large). `index_sql` for CREATE
   TABLE/VIEW/ALIAS/PROCEDURE/TRIGGER (views resolved to base tables, CHECK
   constraints into `values`); BIND PLAN/PACKAGE cards → plan, package,
   QUALIFIER per program; `--db2cat` loader for SYSPACKDEP / SYSVIEWDEP /
   SYSCOLUMNS unloads. `table` already exists and will gain these sections.
   *Needs: one BIND deck and one SYSPACKDEP unload header.*
3. **RM-03 IMS segment from the SSA; MFS message ↔ program field** (P1,
   medium). Derive the segment from the SSA's literal; join a MID's MFLD
   offsets to the I/O-area 01 of the program that GUs the I/O PCB.
4. **RM-04 Lineage, batch order, restart, release diff** (P1, large).
   `lineage --upstream/--downstream`, `flow JOB --before/--after`,
   `schedule`, `restart JOB STEP`. Release diff: **delivered** as
   `diff OLD NEW` / `diff --system SYS-TEST` (one folder per environment
   under `estate\`, the folder names the system).
5. **RM-05 Easytrieve / Assembler / PL/I / REXX / CLIST / SAS handlers**
   (P1, large). Program rows, call edges, DSECT layouts, ALLOC/SUBMIT edges.
6. **RM-06 Link-edit cards, aliases, compile-PROC SYSLIB order, SCM stage**
   (P1, medium).
7. **RM-07 Scheduler native formats** (P1, medium): Control-M XML/JSON,
   CA-7 LJOB, **Zeke events** - *send 20 lines of the Zeke export with the
   names changed and it gets a loader.*
8. **RM-08 Nested COBOL programs, COPY inside a copybook** (P1, medium).
9. **RM-09 Incremental Zowe refresh (ISPF stats), portable index, second
   developer** (P1, medium).
10. **RM-10 similar / clones / conventions / hotspots / SYSOUT reports /
    numeric-literal equivalence** (P2, medium).
11. **RM-11 Rare parser corners**: PIC N/G, REPLACE statement, free-format
    source, minor read/write modes (P2, small).
12. **RM-12 Business document formats** (.xlsm/.eml/.tif/.json) and OCR on
    locked laptops (`ocr --probe`, `--import`) (P2, small).
13. **RM-13 Rare ops corners**: same PROGRAM-ID in two members, one DSN on
    two LPARs, Windows reserved member names, db lock / UI cancel (P2, small).

## Next re-parse batch (build.py / classify.py - one overnight re-parse for all of them)

Each of these touches a FACT_MODULES file, so each alone would cost a full
re-parse of the estate; they ship together, when a parser improvement is
worth the night anyway (LESSONS 153):

1. `load_skip_list` / `load_inventory_skips` cut a line at the first `#`, but
   `#` is a legal national character in a member name (the watchdog now stops
   on the second freeze instead of looping).
2. `fatal_db_error` treats a `MemoryError` inside one member's parser as "the
   index cannot be written": the build stops with disk-space advice instead of
   marking that member failed.
3. A Ctrl+C that lands between two members (status-file write, the 10-second
   commit) escapes the loop's handler: traceback instead of the clean stop.
4. The rc-3, rc-4 and crash messages say "run the same command again" - with
   `--rebuild` in that command it deletes everything (the watchdog's own line
   says WITHOUT --rebuild; README says so).
5. `--db=PATH` spelling: the crash report lands in the current folder, not
   beside the index.
6. `atlas-problems.txt` opened with "w" on every run (the watchdog keeps the
   earlier segments' lines meanwhile); the problem list under `--quiet`;
   library names and raw exception text in the lines the user is asked to send.
7. `MemberTimeout` carries `run_with_limit` as its toolkit line, not the
   parser's own.
8. classify: `.mp4 .m4v .mov .wmv .avi .vtt .srt` inside the build's folders
   become `unknown` / `doc partial` rows and a caption file is classified by
   its words; a quiet skip with a plain reason.
9. `build_run` records only the estate root, not the `--also` folders (the
   watchdog reads the index instead to refuse a command that drops folders).
10. Parser reach (from his coverage tables): dynamic CALL targets across
    members, `sql_cursor` declared in another member, INTRDR-submitted JCL
    through a card member, the MFS macros in his estate.

## What stays out of reach, by design

- Anything decided at **run time**: dynamic SQL, dispatch tables held in DB2
  rows or control cards the toolkit never sees, CALL targets arriving in
  LINKAGE, scheduler calendars. The index reports these as unresolved rows
  and known-unknowns; only a DB2 catalog unload or a loadable scheduler
  export closes them.
- **Which load module really ran**: binder ALIASes, static INCLUDEs,
  Endevor/ChangeMan stage - until RM-06 and a listing (RM-01).
- **Non-COBOL code** contributes no facts until RM-05; every impact chain
  through an Assembler date routine or a REXX driver ends at "NOT in index".
- **One level short of the business object** in IMS/CICS until RM-03: a
  DL/I call is tied to its database and PROCOPT, not yet to the segment its
  SSA names; an MFS field is not yet joined to the program's field.
- **Multi-hop and time-based questions** (RM-04/10): the developer gets
  one-hop rows with cites and chains them; a pack describes the estate as
  of the last complete fetch, which is now stated on every pack.

## How to keep this file true

- A wrong fact found in use becomes a test in `tests/` and a row in
  `LESSONS.md` before the fix (CLAUDE.md §7); this file changes only when a
  roadmap item is delivered or a new class of artefact appears.
- To re-run the audit: the six-lens workflow prompt is in the commit
  history of 2026-09-12 (`mainframe-atlas-coverage-audit`); it needs no
  estate data, only the toolkit and its fixtures.
