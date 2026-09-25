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
   `lineage --upstream/--downstream`: **delivered** for values as `flow FIELD` /
   `flow FIELD --up` (next re-parse batch, item 14). `flow JOB --before/--after`,
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
worth the night anyway (LESSONS 153). The quoted-COPY fix of 2026-09-21 (LESSONS 173)
shipped alone and cost one such night; these still wait for the next one:

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
10. A file name ending with a space or a dot before its extension gives a
    member name that keeps it (`PLAN `): strip it at inventory (the OCR folder,
    the lookups and the gate tolerate it meanwhile - LESSONS 156).
11. `make_resolver` should rank a member under `RECOVERED-COPYBOOKS` after
    every other candidate of the same name, so a recovered copybook never
    beats the real one once it arrives (coverage warns meanwhile - LESSONS 168).
12. An expression index on `UPPER(TRIM(name))` next to the one on
    `UPPER(name)` (QUERY_INDEXES): the trimmed-name lookups the gate, `doc`
    and `diff` fall back to scan the member table on a miss (LESSONS 158).
13. Parser reach (from his coverage tables): dynamic CALL targets across
    members, `sql_cursor` declared in another member, INTRDR-submitted JCL
    through a card member, the MFS macros in his estate.
14. **Value flow** (RM-04, first part): `pfield` (per-program data items with section, FD, this
   program's offsets, ODO flag, link to the copybook row; unused roots pruned), `data_flow` (one row
   per source->target pair per verb: MOVE / CORR / literal / figurative / arithmetic / STRING /
   UNSTRING / INITIALIZE / SET / SET ADDRESS OF / ACCEPT / READ INTO / WRITE FROM / bare READ-WRITE /
   INSPECT / FUNCTION / RETURNING / EXEC CICS FROM-INTO / DL/I / MQ, with the enclosing IF as
   `guard`), `call_arg` (BY REFERENCE / CONTENT / VALUE / LENGTH OF / ADDRESS OF / COMMAREA / START
   FROM), `param` (PROCEDURE DIVISION USING, every ENTRY, RETURNING, DFHCOMMAREA), `file_record`
   (every 01 under an FD), `field_ref.pfield_id`, `sql_col_ref.pfield_id`, `call_edge.returning`;
   `ADD a b GIVING c` records `c` as a write (LESSONS 174); `expand_map` dropped. Query `flow FIELD`
   reads them; on an index built before this batch it reconstructs MOVE pairs from single-pair lines
   only and marks every hop `(reconstructed)`.
15. **Done with item 14** - **`ADD A B GIVING C` records C as read, not written** (also SUBTRACT /
   MULTIPLY / DIVIDE ... GIVING, and RETURNING on a CALL is lost): `_ARITH` in cobol.py stopped at
   TO/FROM/BY/INTO and never saw GIVING, so `field C` listed the statement under 'read' and an impact
   search for who sets C missed it. Found by the value-flow review (LESSONS 174). Now the GIVING target
   is a write and a CALL's RETURNING item is kept (`call_edge.returning`);
   `test_add_giving_without_to_is_a_write`.
16. **OS/VS `01 data-name COPY text.`** (also `77 ... COPY`, `FD file-name COPY`, `SD ... COPY`; the
   COPY may sit on the next line): the compiler copies the library text and puts data-name in place of
   the library's own 01 / 77 / FD / SD name. expand.py commented out the whole line, so the program's
   name vanished and the library's 01 name took its place - every MOVE, READ INTO, DL/I I/O area
   (his `-SEG` areas) and CALL USING naming it pointed at nothing, in `field` and in `flow`. Now the
   library's first entry is renamed as REPLACING would (cited in the copybook, a `field_alias` row both
   ways, the program's `field` row); a library that starts at 05 hangs under the program's own 01,
   kept as code. `01 X.` with the COPY after the period is unchanged (LESSONS 177).
17. **A statement before COPY on the same line** - his `A-100-BEGIN SECTION.  COPY PROCBOOK.`
   (also a paragraph name, `MOVE A TO B.  COPY X.`, `01 X.  COPY Y.`): the compiler keeps the text
   before COPY and replaces only the COPY statement. expand.py commented out the whole line, so the
   section vanished from the expanded text - `PERFORM A-100-BEGIN` pointed at nothing, the copybook's
   paragraphs fell into the section before it, and the build's paragraph and perform_edge rows followed.
   Now the prefix stays live on its own line number with the COPY part blanked (no line is invented,
   so the citation line map is unchanged); only the statement's continuation lines are the comment
   echo, and the statement is gathered from the COPY keyword, so the prefix's period no longer cuts
   off a REPLACING continued on the next line. The OS/VS `01 X COPY Y.` rename (item 16) is decided
   first and is unchanged; a COPY that starts its line is unchanged (LESSONS 178).
18. **Delivered in the batch - an ambiguous-copybook pick is not a partial parse.** `make_resolver`
   records the choice once, as the 'ambiguous_copybook' row ("N copies of X with different content;
   used PATH (how)"), and hands the expander no note, so no 'expand' note repeats it and the
   program's parse_status is `ok`; a copybook NOT FOUND, a COPY skipped (recursive, nested too deep)
   and an unrecognised map still make a member `partial`. Before, the resolver returned the note,
   expand.py repeated it as a COPY warning, and `index_cobol` stored every expander warning as
   `("expand", w)` and set `partial` when any existed - so a program that expanded completely with
   a chosen copy counted as "parsed only in part", the same as one whose copybook is missing (his
   701 "partial" COBOL programs with ~60 copybooks missing; LESSONS 181). The query side keeps both
   shapes apart: on an index built before the batch `partial_kind` / `is_truly_partial` read the
   resolver's wording in the 'expand' notes, on one built by it the program is `ok` with the row
   alone (`chose_a_copybook`); `coverage` lists such members under "Complete, with a copybook chosen
   among several" on both and says which word the index uses, and `program` / `pack` print "parse:
   complete (copybook chosen among several - see notes)" on both, so a program's header does not
   change with the re-parse. tests/test_chosen_copybook.py (the build of this batch, and the earlier
   shape written by hand).
19. **Delivered in the batch - the resolver reads `listing_copy_source`: a current listing that names
   a held copy with a different text decides.** His compiler listings end with a table naming, per
   copybook, the DD name and the LIBRARY DATASET the compiler read it from. `atlas.recover` reads
   that table from every program's listing into `listing_copy_source(program, copybook, ddname,
   dataset, listing, seen, current, matched)` - each row dated: `current` is 1 when the listing's
   own program lines are the member as indexed, 0 for an older compile's (`matched` in percent),
   NULL when not dated - and checks each 'ambiguous_copybook' choice against it in five verdicts
   (confirmed, promoted included / contradicted / named by an older listing / a library the index
   does not hold / unknown - `work\recover.md` names each with why; LESSONS 182, 193). Now
   `make_resolver` reads that table once for the whole build (`load_listing_sources`: a dict keyed
   (program, copybook), so 121k members cost one lookup per COPY). The chain picks as before
   (COPY..OF > same system in declared order > authoritative > same folder > first); then the
   listing changes the pick only where the compiler's record and the chain's guess differ in
   CONTENT (`current_datasets`, `listing_pick`):
   - the row must speak for the program. The rows are keyed by the listing's file stem, so every
     listing of one program NAME shares the key; one rule, one function for the build, recover's
     check and `program`'s 'listing says' lines (`build.rows_that_count`), decided per (program,
     copybook) - the build's key; recover and `program` group a program's rows by copybook first,
     so all three read the same rows. When another system holds a program of that name, only the
     listings in the program's own system (the member's top-level folder) count: GC and GC-TEST
     each keep their own listing's word, a listing filed elsewhere for a name both hold decides for
     neither (recover dates a listing against one program of the name only), and recover says
     UNKNOWN with what the program's own listing lacks and the step that changes it (not read: put
     it under its system's folder, build, run recover again; no copybook-source table, or a
     current listing whose table has no row for the copybook: the chain's copy stands; an older
     compile's: its current listing; not yet dated: run recover again). While no other system
     holds one, a CURRENT listing in the program's own system decides every copybook it names,
     and a listing filed elsewhere never overrides it (its own listing naming a staging library
     the index does not hold keeps the chain's copy, whatever a SHARED listing names); for a
     copybook no current listing of its own names (none there, an older compile's, one not yet
     dated, a table without the row), every listing of the name speaks wherever it is filed
     (SHARED or a LISTINGS folder, a `--from` folder), recover dates each against that one
     program, and the current one decides - an older compile's listing in the program's own folder
     never hides a current one filed elsewhere. A listing read with no table is stored as one row
     with no copybook and its path, so its system is known;
   - the listing must be current (`current` = 1): an older compile's listing or one not yet dated
     never decides, and a table written before recover dated listings decides nothing;
   - the index must hold a copy in the dataset it names (the `library` table from the fetcher's
     `.atlas-library.json`, else the folder named after the dataset - `folder_dataset`, cached per
     folder) whose text DIFFERS from the chain's pick: that copy is expanded and the how says 'the
     program's compiler listing names DATASET'. Where the copy it names has the same text - his
     listings name the staging library the compile ran against, the copybook promoted unchanged -
     the chain's pick stays (production, not the staging copy) and the how says 'confirmed by the
     program's listing (same text as DATASET)'. Where it names the chain's own dataset, that copy
     with 'the program's compiler listing names DATASET'. A different text beats a same text; a
     library the index does not hold changes nothing.

   A nested COPY is looked up by the program's name, as the listing lists it. recover's verdicts
   take the same order (a current listing's findings decide over older or undated ones; then a
   different text, a same text, a library not held; a dataset is 'same' when any held copy in it has
   the text of the copy used), so a program the resolver parsed is never called contradicted, and
   `recover` marks for the next build exactly the programs whose choice a CURRENT listing
   contradicts (on an index this toolkit built; after a toolkit change the next build re-parses
   every member anyway) - never one contradicted by a listing not yet dated, which the build would
   not follow: every count says those apart ('N contradicted by a current listing, M contradicted
   by a listing not yet dated') and the console says to run recover again with `--from` so it
   dates them. The table absent
   (recover never ran on that index) or empty changes nothing, and the build never creates it. For
   the re-parse night, his sequence: pull, `python -m atlas.recover --db atlas.db` (with `--from
   FOLDER` for listings outside the estate), which stores and dates the rows, then the build (a
   `--rebuild` deletes the index and the table with it - the toolkit change re-parses every member
   without it). tests/test_listing_resolver.py, tests/test_listing_systems.py,
   tests/test_copy_sources.py TheReparseNightOnTheStagingEstate (LESSONS 193, 194, 195, 196, 197).
20. **Delivered in the batch - a procedure copybook is a copybook by its CONTENT, before the folder hint.**
   His `A-100-BEGIN SECTION.  COPY PROCBOOK.` copies a member of paragraph names and statements - no
   level numbers, no DIVISION header - which had no content signature, so the FOLDER NAME typed it:
   PROCLIB / PROCS made it a proc (a JCL PROC), CNTL / CARDLIB a control card, a plain folder a document,
   a dataset-named folder with no hint 'unknown'. Only copybook / cobol / sql / unknown reach the
   resolver, so a procedure copybook fetched into a PROCS or CNTL folder was never expanded ('COPY
   PROCBOOK NOT FOUND' with the member on disk - LESSONS 183). Now classify.py types a member with COBOL
   procedure statements and no DIVISION header as a copybook before the Assembler and MFS shapes and
   before the folder hint (`_SIG_COBOL_PROC`): MOVE ... TO, the arithmetic verbs with their preposition,
   SET ... TO, PERFORM, GOBACK, GO TO, EVALUATE, STOP RUN, EXIT., CONTINUE, COMPUTE, INITIALIZE, ACCEPT ...
   FROM, STRING / UNSTRING ... DELIMITED, INSPECT, OPEN INPUT and the COBOL forms of READ / WRITE, a scope
   terminator, a paragraph or section name in area A (columns 8-11) ended by a period - in upper case, where
   a COBOL line's code begins (never column 1, a `//` line or a `*` comment). The statements an Assembler or
   MFS member writes too - IF, DISPLAY, CALL, COPY, EXEC CICS / SQL / DLI, CLOSE with a file name (the
   Assembler CLOSE macro takes one DCB name bare), START with a file name (the COBOL verb alone on its line,
   its KEY IS on the next) - count only where neither the Assembler nor the MFS shape fired and the library
   says nothing (`_SIG_COBOL_SHARED`), and the IDCAMS `IF LASTCC`, ICETOOL `DISPLAY FROM(` / `COPY FROM(`,
   IEBCOPY `COPY OUTDD=` and TSO `CALL 'LIB(PGM)'` forms never count. The library says how much the
   statements must say (`classify.library_says`, LESSONS 199, 200), so control cards, documents and MFS
   source keep their kind: an Easytrieve program (JOB INPUT, END-PROC, FILE with its field definitions) or a
   Connect:Direct process (label PROCESS SNODE=, COPY FROM (DSN=, RUN TASK, EIF) is never typed by a COBOL
   signature (`classify.not_cobol`) - its job reads it through SYSIN, which `build._card_text` does only for
   a control-card member, never a copybook; in a library of documents (a folder named DOCS / SPECS / DESIGN,
   a library declared doc, a document's extension other than .txt - .md, .html, .csv ... - or a .txt outside
   a dataset-named folder with a line of prose: a letter in column 1, a Markdown heading, a numbered line
   `2.`, or an English word no COBOL statement carries - THE, YOU, PLEASE ... - outside a literal on a line
   that is not a comment) the statements and the loose level number count for nothing - a run book says
   PERFORM THE FOLLOWING STEPS, a design note quotes a paragraph - while a data description entry still
   types a copybook, as before the batch; in a library of control cards (a folder named CNTL / PARMLIB /
   CARDLIB ..., a library declared ctlcard or sched) two statement lines are needed, one of a form no card
   language has, and the loose level number counts for nothing; in a library of Assembler or macro source
   (a folder named MFS / BMS / DBD / PSB ..., a library declared mfs, bms, dbd, psb or imsgen, an extension
   .asm / .mfs ...) the statements an Assembler or MFS member writes too and the loose level number count
   for nothing - an MFS member of COPY lines only takes its library's kind. So a procedure copybook -
   paragraph names and statements - is a copybook in any library but one of documents, and in a library of
   control cards when it has two statement lines (a single MOVE or START line keeps the folder's kind). A member with no signature at all (a literal copied into a VALUE clause)
   still takes its folder's kind or the declared one, and `atlas.recover`, `coverage`, `program` and
   `copybook` name it with the fix - rename the folder to end in COPYLIB, or declare the library's kind
   in the UI's table: both are true now, since item 22 lets a declaration win over the folder name. The
   same rename (or declaration) is what puts an 'unknown' member's own lines in the index: the build
   expands it into its programs but has no parser for that kind (LESSONS 184).
   tests/test_refiled_copybooks.py TheBuildFilesThemRight, ClassifierShapes; tests/test_arrived_copybooks.py
   ArrivedAfterTheParse, FiledAsAnotherKind, NestedCopybookArrivesLater; tests/test_library_says.py
   TheClassifierReadsTheLibrary, JobsReadTheirCards, TheSecondRoundInABuild (LESSONS 198, 199, 200).
21. **Delivered in the batch - an incremental build parses again every program whose COPY may now resolve
   differently, and every job whose PROC, INCLUDE or card member may now read differently: a member of any kind
   the resolver expands that arrives, changes, goes, is re-typed or is recorded again; a member of any kind a job
   reads that arrives, changes, goes or is re-typed.** The build forced the copiers of a new or changed member only
   when it was filed copybook or cobol (`changed_names`), while the resolver expands copybook, cobol, sql and
   unknown: a copybook typed 'unknown' by its dataset-named folder (since item 20 one with no signature - a literal
   copied into a VALUE clause, a procedure copybook written in lower case) or a DDL member filed 'sql' arrived, was
   in the index, and forced nothing - every program that copied it stayed 'partial - COPY X NOT FOUND' until
   something else re-parsed it (his two builds after the fetch changed nothing; LESSONS 183). A member that went from
   disk, whose new text was filed as another kind, or that was recorded again under a new id with its bytes
   unchanged (a copybook marked pending, as LESSONS 184's recover once did; a build stopped before it was parsed; a
   parser exception) forced nothing either: `_forget_member` set the programs' copy_use rows to NULL and they kept
   'ok' with the fields of the earlier read (LESSONS 188's un-linked 'ok'). The jobs had the same gap under their own
   rule: only a new or changed member filed proc, jcl or ctlcard forced them, so a PROC that went from disk left its
   jobs with the PROC's steps and datasets and no 'missing_proc' row, and a card member arriving 'unknown' or 'sql'
   (which the card lookup reads) left them without its cards (LESSONS 202). Now `build.RESOLVER_KINDS` is one tuple
   for the resolver and the forcing (recover.RESOLVER_KINDS is pinned equal to it); `moved_names` takes every member
   of those kinds that is new, whose bytes changed, that is recorded again or that went, under its kind in the last
   build and in this one, so a member re-typed into or out of them counts; and `copiers_to_parse` finds every member
   copying one of the names, then the copiers of what is recorded again - each name asked once, 500 to a query, so
   the cost grows with the names and not with the chain. `build.JOB_READ_KINDS` is the union of the kinds the PROC,
   INCLUDE and card lookups read (PROC_KINDS, INCLUDE_KINDS, CARD_KINDS: proc, jcl, ctlcard, unknown, sql), and a
   name `moved_names(..., JOB_READ_KINDS, again=False)` finds parses every job and PROC again - every one, because a
   card looked up by its sequential dataset's last qualifier leaves no name behind when nothing is found; a member
   recorded again with the same bytes does not count there, since a job keeps the names of what it read, never a
   member id. For what a COPY and a job expand, an incremental build gives the facts a --rebuild of the same estate
   gives, checked row for row (tests/test_inventory_forcing.py `facts`): a copybook removed from disk leaves its
   programs 'partial - COPY X NOT FOUND', or resolved to the other copy of the name, never 'ok' with a NULL row; a
   PROC removed leaves its job with 'missing_proc'. The one list left out is the dataset names (item 24, with a DD
   direction an OPEN verb set, which outlives its OPEN the same way). The price: a copybook the parser fails on
   with an exception (not a time limit, which settles) is recorded again on every build until the parser is fixed,
   and its programs are parsed again with it - the problem list names the member each time; and a member of an
   'unknown' or 'sql' kind arriving parses every job again (JCL is cheap next to COBOL). The stand-ins stay for an
   index built before the batch, and tell its two states apart by the program's own note
   (`recover.not_found_copies`): a program that says `COPY X NOT FOUND` was parsed before X arrived - 'copybooks
   that have arrived since the program was parsed', with the kind clause ('that build parsed them again only for a
   member filed copybook or cobol') only for an unknown or sql member; a program that says nothing had expanded a
   copy of X that left the index after the parse - 'parsed with a copy that has left the index since', its own
   report section, `program`'s '**no longer linked**' cell and un-linked note (LESSONS 188), `copybook`'s 'was
   parsed with a copy of this copybook that has left the index since'. `atlas.recover` marks both PENDING; on an
   index this toolkit built both find nothing, and their words say the state came from the build that made the
   index. Marking alone was never the whole fix for an 'unknown' member: its own lines stay outside the index until
   the folder is renamed to end in COPYLIB or the library's kind is declared (item 20, LESSONS 184) - and since the
   build now makes such a program whole at once, `program` (the 'resolved to' cell), `paragraph` (a note under the
   Source lines, which carry no cite) and `copybook` say so wherever the member is expanded; the stand-in marks
   PROGRAMS only - a copybook member's own COPY rows are never resolved by the build - and leaves alone a COPY the
   expander SKIPPED (LESSONS 185). tests/test_inventory_forcing.py (TheRule, CopiersToParse, ArrivingUnderEveryKind,
   RemovedFromDisk, ReTyped, RecordedAgain, NothingElseIsParsedAgain, JobsFollowWhatTheyRead),
   tests/test_arrived_copybooks.py ArrivedAfterTheParse, NestedLiteralArrivesLater, FiledAsAnotherKind (LESSONS
   201, 202).
22. **Delivered in the batch - the level-number and COBOL-statement signatures are checked BEFORE the
   Assembler, listing and MFS signatures, `_SIG_ASM` requires the Assembler shape, and a declared kind wins
   over a shape.** His copybook in a `.COPYLIB` folder was filed `asm`: `_SIG_ASM`
   (`^(?:[ \t]*\w+)?[ \t]+(CSECT|DSECT|START|DFHEIENT)\b`) fired on any line whose first or second word BEGAN
   with START - `05 START-DATE PIC X(8).`, `PERFORM START-PARA.`, `MOVE START-DATE TO WS-DATE` - and ran
   before `_SIG_DATA_LEVEL` and before the folder hint; `_SIG_LISTING` fired on a comment saying MODULE MAP
   or CROSS REFERENCE TABLE, `_SIG_MFS` on a line whose first word is MSG / FMT / DEV / DFLD / MFLD. The
   resolver never looks at asm / listing / mfs, so every program copying such a member said COPY X NOT FOUND
   while the file was in the estate, and no folder change helped - the content decided (LESSONS 186). Now,
   in classify.py: (a) a data description entry (a level number where a COBOL line's code begins, then a
   data-name ended by a period, the end of the line or a clause - `_SIG_DATA_LEVEL`; the data-name may carry
   a tag, `WS-:XR:-ID`, and a copybook written from column 1 is one by a level number there, a data-name and
   a PIC / VALUE / REDEFINES / OCCURS / USAGE clause - LESSONS 200) and the COBOL-statement signature of item
   20 are checked before the Assembler and MFS shapes; the looser level-number signature
   (`_SIG_LEVEL_NUMBER`), which an Assembler register equate `R12 EQU 12` trips, stays after them as before.
   A compiler listing is still looked for before every COBOL signature - it echoes the program's level
   numbers and PROGRAM-ID - but by its shape alone: the compiler's banner on a line that is not a comment,
   a map heading at the start of a line, or three numbered source lines (`listing_hit`). (b) The Assembler,
   listing and MFS signatures are the shapes the stand-in proved on his estate - `recover._ASM_SHAPE`,
   `_MFS_SHAPE`, `_LISTING_SHAPE` / `_LISTING_HEAD` moved into classify.py and recover imports them (LESSONS
   187-189): CSECT / DSECT as the operation after a label of any length or none, START with a label in
   column 1 or a numeric or quoted operand, DFHEIENT, DS / DC with a type, `USING *`, `EQU *`, `BR 14`; a
   labelled MFS statement, TYPE= / POS= / LTH= operands, MSGEND / FMTEND / TABLEEND, an operator control
   table's `IF DATA=` / `IF LENGTH=` - and both shapes are checked before the statements such a member writes
   too, so an MFS member that COPYs its device header, or an Assembler member with `CLOSE INFILE`, keeps its
   kind (LESSONS 200). So `05 START-DATE`, `PERFORM START-PARA`, `START
   CUSTFILE` alone on its line, a comment naming MODULE MAP and a line whose first word is MSG no longer
   type a copybook as something else; nor does a signature on a comment line - a remark naming DFHMDF,
   PROGRAM-ID, CREATE TABLE or the compiler (`_code_hit`). CREATE TABLE counts where a statement begins, so
   an Assembler remark `CREATE TABLE OF RATES` or a COBOL literal is no DDL; the compiler's banner counts at
   the start of a line (`1PP 5655-`, `LineID PL SL`), so a program's `DISPLAY 'BUILT WITH IBM ENTERPRISE
   COBOL'` or a copybook's VALUE literal is no listing (atlas.recover, reading a text known to be a listing,
   keeps the looser banner, `classify._LISTING_BANNER`); a REXX header six blanks in - the slash in column 7,
   where a COBOL comment has it - counts after a COBOL copybook's own lines, so the exec is rexx and a
   copybook with such a banner comment a copybook (LESSONS 199).
   (c) The kind declared for a library in sources.json (the manifest kinds; `build.load_declared_kinds`,
   `_inventory_one` passes it to `classify.classify`) wins over a shape (asm / listing / mfs), the folder
   name and the extension, not only over 'unknown' (`classify.declared_wins`) - 'declare the library's kind'
   is true advice now for a member any of those typed; a strong signature (JOB card, PROC, DBD, PSB, BMS
   macro, CSD, stage-1, IDENTIFICATION DIVISION / PROGRAM-ID) and a COBOL copybook's own lines still win
   over it, and 'cobol' - the kind the UI's table gives a new row and every ...SRC name - replaces
   'unknown' only, since a member a shape or a folder typed never carries a PROGRAM-ID (a mixed source
   library's Assembler members, a listing library left at the default, would be filed as programs). The
   build records the kinds each run was declared (`build_run.declared_kinds`), and a member the declared
   kind filed as a copybook over the shape of an Assembler, listing or MFS member carries a `declared_kind`
   row that `program` (beside the copy), `copybook` and `coverage` print - fetch.infer_kind declares every
   COPYLIB copybook, so a real Assembler member there is expanded into its programs, and 'ok' must not be
   silent about it; a member the build re-parses because a member it copies changed keeps its stored
   classification, and its `declared_kind` row with it (LESSONS 200). atlas.recover and `coverage` say 'by
   its declared kind' only for a library declared so
   (`recover.declared_kinds_used`: the run's record, or for an older index the manifest.json beside it whose
   sha the build recorded) - a card member the build itself filed ctlcard in a JCL folder reads 'by its
   folder' (LESSONS 199).
   The stand-in stays for an index built before the batch: `atlas.recover` re-files a misfiled member
   whose bytes are the ones indexed and which the classifier of this toolkit reads as a copybook
   (`how_classified` says how the older classifier filed it - its weak signature and the line, the folder,
   the extension or the declared kind: `earlier`), marks its programs, and the next build expands it; the
   first build after the toolkit changed re-parses every member and files it a copybook itself, with its
   own rows, and on an index this toolkit built `refile_misfiled()` finds nothing to do. A real Assembler,
   listing or MFS member with a copybook's name is refused as what it is - the shape is checked before
   the folder now (a real member in a folder with no COPY hint was told to rename the folder to end in
   COPYLIB and run again, and the second run refused it on the shape) - with 'declare its library copybook
   in the UI's table and run the build' if it IS the copybook; on an older index whose library is declared
   copybook already (its manifest.json beside it) the member is re-filed, since the build of this toolkit
   files it one, and where that cannot be known the refusal says a library declared copybook already needs
   only the build. The two windows the stand-in left open are
   closed: a re-filed member whose text changes on disk is classified again and reads as a copybook, and a
   build that re-parses every member (--rebuild, the manifest changed, a parser module changed) files it
   a copybook instead of undoing the re-file (LESSONS 187, 188). tests/test_refiled_copybooks.py
   (TheBuildFilesThemRight, ClassifierShapes, GenuineAssemblerIsNotRefiled, RealAssemblerShapesAreNotRefiled,
   DeclaredKinds, AnIndexBuiltBeforeTheBatch, TheFirstBuildAfterTheBatch, SecondRunBeforeTheBuild,
   DryRunWithAFolderTypedCopyToo, OkWithAnUnlinkedCopyRow, OneProgramCopyingTwoRefiled, WrittenCopybookIsFiledRight,
   TheVerdict), tests/test_library_says.py (TheClassifierReadsTheLibrary, TheSecondRoundInABuild,
   TheBuildsOwnRuleIsNoDeclaration, ADeclaredCopybookOverAShape, TheNoteOutlivesAForcedReparse, DeclaredKindsUsed),
   LESSONS 186-189, 198, 199, 200.
23. **reader.py: a copybook member whose every non-blank line keeps its text within columns 1-7 is read as
   code.** Two of his missing copybooks hold a 7-8 digit number in column 1 and nothing else (a stub, or a
   value meant to be copied): fixed-format reading takes columns 1-6 as the sequence area and column 7 as
   the indicator, `read_cobol_lines` yields no code, `build.code_line_count` gives 0 and the build files the
   member `empty` - the resolver never looks at it and every program copying it says COPY X NOT FOUND
   (LESSONS 192). At the re-parse: a member with no line reaching column 8 is read as free format (its
   text is the code) and counted; the reader says so in the member's note. Until then `atlas.recover`,
   `coverage` and `copybook NAME` say where the text sits ('the file holds N line(s) whose text sits in
   columns 1-7 ...') instead of the bare word `empty`, and the disk check names the file.
24. **Two facts an incremental build keeps after their source is gone: the list of dataset names keeps a name no DD
   names any more, and a DD keeps the direction an OPEN verb gave it after the program stops opening the file.**
   The build adds a row to `dataset` for every
   DSN a DD, an IDCAMS DEFINE or a CICS / IMS definition names (`INSERT OR IGNORE`), and no incremental build
   removes one: after a job stops naming a dataset - a PROC or INCLUDE gone, a DD deleted, a DEFINE dropped - the
   name, and a DEFINE's attributes, stay until a `--rebuild`. The list is read only by `dataset NAME` for an IDCAMS
   DEFINE's attributes, so a plain row says nothing; a DEFINE row still prints 'IDCAMS DEFINE X: ...' after the job
   that defined it went. Found while checking item 21 row for row against a --rebuild
   (tests/test_inventory_forcing.py JobsFollowWhatTheyRead leaves the table out and says why). At the re-parse:
   record the member that wrote each row (or each DEFINE's attributes) so `_forget_member` takes it with the rest,
   or delete after the parse the rows nothing names; then compare the table too. The second: `post_open_modes`
   sets `dd.mode` from each program's OPEN verb (mode_source 'open_verb') on every build and never sets it back,
   so after GCOPN1's `OPEN OUTPUT OUT-FILE` is removed, the kept job's DD OUTF still reads 'output' from
   'open_verb', while a --rebuild gives 'unknown' from 'undetermined' (checked on a scratch estate while writing
   item 21's tests). At the re-parse: keep the JCL's own direction in a column of its own and let the pass start
   from it on every build.
25. **A COPY NOT FOUND inside a VALUE clause loses the PROCEDURE DIVISION.** `05 WS-STATES PIC X(10) VALUE`
   followed by `COPY LITBK.` with LITBK missing: the expander turns the COPY line into a comment, period
   included, so the data entry runs on into `PROCEDURE DIVISION.` - the member is 'partial' with no paragraphs,
   WS-STATES carries the value 'PROCEDURE DIVISION', and the note says only 'fields/code from it are missing'.
   The same shape as the synthetic reproduction F15 (an EXEC SQL in WORKING-STORAGE without its period,
   tools/synth/repro). Since item 21 an incremental build reaches this state as well when such a copybook goes
   from disk (a --rebuild gives the same; before, the program kept 'ok' and its old paragraphs). At the re-parse,
   with F15: end an open data entry at a division header in area A (cobol_statements), and say in the note when
   the procedure division was lost.

### flow: known limits (open after review)

Left open after the review of `flow --up` (the LINK / XCTL COMMAREA origin, the per-path cost of
`passes_on` and the fallback's BY CONTENT pass-on are fixed, each with its test in
tests/test_flow.py FlowKnownLimits). None needs a re-parse (atlas/flow.py only).

- **A concatenated DD with a long DSN cites only the DSN.** On a continuation line (`//  DD
  DSN=...`) longer than the 40-character token, the cite keeps the tail - the DSN - and loses the
  `// DD` statement words; the DSN is on that line, so the cite still PASSES the gate.
- **At the hop limit a pass-on is counted without checking the chain.** A callee that only hands the
  parameter on is printed on the way back when the program it hands it to lies past `--hops`, even
  if nobody further on sets it; the line ends `[end: hop limit N]`, so it is labelled, not silent.
- **JUSTIFIED RIGHT receivers are not modelled.** Every alphanumeric MOVE is taken as
  left-justified, so a shorter value moved into a `JUSTIFIED RIGHT` item is placed in the wrong
  bytes of it (the hop and its cite are right, the byte range is not).
- **A circle of programs that pass the parameter among themselves is slow when nobody sets it.** A
  `no` cut short by the cycle guard is not kept (the same question asked from elsewhere may have its
  way through the item being asked about), so with `--hops` larger than the circle the time grows
  with the number of paths round it: 12 programs each CALLing 4 of the others, `--hops 14`, takes
  about 23 seconds.
- **The fallback's `HUMAN MUST VERIFY` one-way note stays on a position it has proven BY
  REFERENCE.** Before the re-parse, a CALL whose text holds BY CONTENT anywhere carries the note on
  every hop through it, even on a position its own text shows is BY REFERENCE (noise, not a wrong
  answer).

## What the tool was not built for - scenario audit (2026-09-21)

docs/AUDIT-scenarios-2026-09-21.md lists the scenarios a month of real analysis asks (change impact, abends,
data fixes, audits) and marks each full / partial / none against the tool as it is, ranked by value for
change-impact work. Three families are open: following a VALUE across renames (item 14 above closes most of
it), ORDER IN TIME (which write is live at the CALL, which job ran before this one), and OVERLAP judgement
(REDEFINES, group moves, truncation). Pick from that table; do not add to it from guesswork.

docs/RECOMMENDATIONS-2026-09-24.md answers "what else would a senior mainframe developer want": thirteen
ranked items (impact table with the change class decided by rule, restart, batch closure, rule-based review,
regression scope, PSB/SSA/MFS, unit of work, abend offset from the listings, argument contracts in bytes,
reaching definitions, dataset layout contract, DB2 bind facts, change tags), each with the moment it serves,
what exists today, the cost class and the host input it needs; the already-built commands he may not use;
and what stays out of reach. Items there enter this roadmap only when scheduled.

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
