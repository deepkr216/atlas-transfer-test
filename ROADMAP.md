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
20. **classify.py: a procedure copybook is a copybook by its CONTENT, before the folder hint.**
   His `A-100-BEGIN SECTION.  COPY PROCBOOK.` copies a member of paragraph names and statements -
   no level numbers, no DIVISION header - which has no content signature today, so the FOLDER NAME
   types it: PROCLIB / PROCS make it a proc (a JCL PROC), CNTL / CARDLIB a control card, a plain
   folder a document, a dataset-named folder with no hint 'unknown'. Only copybook / cobol / sql /
   unknown reach the resolver, so a procedure copybook fetched into a PROCS or CNTL folder is never
   expanded ('COPY PROCBOOK NOT FOUND' with the member on disk - LESSONS 183). A member with COBOL
   procedure statements (PERFORM / MOVE / IF / EVALUATE / EXEC CICS / GOBACK / paragraph names in
   area A) and no level numbers and no DIVISION is a copybook before the folder hint is consulted.
   Until then `atlas.recover` names each such member with the fix (rename the folder to end in
   COPYLIB, or declare the library's kind in the UI's table) and `coverage` / `program` /
   `copybook` say 'filed as proc' instead of a bare NOT FOUND. The same rename is what puts an
   'unknown' member's own lines in the index: the build expands it into its programs but has no
   parser for that kind (HANDLERS), so `paragraph` shows its lines empty and nothing can cite them
   (LESSONS 184) - until this item, the folder fix is the one thing to do in both cases.
21. **build.py: inventory forcing (`changed_names`) covers every kind the resolver accepts.**
   A new or changed member forces the programs that copy it to be parsed again only when its kind
   is copybook or cobol; the resolver also expands sql and unknown members, so a procedure copybook
   typed 'unknown' by its dataset-named folder arrives, is in the index, and forces nothing - every
   program that copied it stays 'partial - COPY X NOT FOUND' until something else re-parses it
   (his two builds after the fetch changed nothing; LESSONS 183). `changed_names` must take every
   kind in (copybook, cobol, sql, unknown), so a program is re-parsed when ANY member it can copy
   arrives. Until then `atlas.recover`'s 'copybooks that have arrived since the program was parsed'
   step marks those programs pending for the next build - the query-side stand-in (item 1 of
   LESSONS 183's fix), not the fix. Marking alone is not the whole fix either: an 'unknown'
   member's own lines stay outside the index until item 20 files it as a copybook, so every note
   that says 'run recover, then the build' adds the folder rename (LESSONS 184). The stand-in marks
   PROGRAMS only: a copybook member's own COPY rows are never resolved by the build (index_copybook
   parses for copies, never resolves), so they say nothing, and a copybook marked pending is
   re-inserted under a new id, which nulls the links of every program copying it. And it leaves
   alone a COPY the expander SKIPPED (a copybook copying itself; nesting deeper than 12): that
   program row is NULL too, but the member was found and the program parsed after it - the
   program's own 'expand' note tells the two apart (recover.skipped_copies, LESSONS 185).
22. **classify.py: the level-number signature and a COBOL-statement signature are checked BEFORE the
   Assembler, listing and MFS signatures, and `_SIG_ASM` requires the Assembler shape.** His copybook
   in a `.COPYLIB` folder was filed `asm`: `_SIG_ASM` (`^(?:[ \t]*\w+)?[ \t]+(CSECT|DSECT|START|DFHEIENT)\b`)
   fires on any line whose first or second word BEGINS with START - `05 START-DATE PIC X(8).`,
   `PERFORM START-PARA.`, `MOVE START-DATE TO WS-DATE` - and runs before `_SIG_DATA_LEVEL` and before the
   folder hint; `_SIG_LISTING` fires on a comment saying MODULE MAP or CROSS REFERENCE TABLE, `_SIG_MFS`
   on a line whose first word is MSG / FMT / DEV / DFLD / MFLD. The resolver never looks at asm / listing /
   mfs, so every program copying such a member says COPY X NOT FOUND while the file is in the estate, and
   the folder fix of item 20 changes nothing - the content decided (LESSONS 186); data copybooks with a
   START-DATE field are common in insurance code. At the re-parse: (a) the level-number signature and a
   COBOL-statement signature (PERFORM / MOVE / IF / EVALUATE / EXEC / GOBACK / paragraph names in area A,
   no DIVISION header) are checked before the Assembler, listing and MFS signatures; (b) `_SIG_ASM`
   requires the Assembler shape - a label in column 1 followed by CSECT / DSECT, or START with a numeric
   operand or alone - so START-DATE, START-PARA, a comment naming MODULE MAP and a line starting with MSG
   no longer type a copybook as something else; (c) the declared kind in sources.json (the manifest
   kinds) wins over a weak content signature, not only over 'unknown' (`_inventory_one` applies it to
   'unknown' alone today, so 'declare the library's kind' never helped a member a signature had typed).
   Until then `atlas.recover` re-files such a member as a copybook in the index (`kind='copybook'`,
   `parse_status='skipped'`, the reason in `parse_error`; `refile_misfiled()`) and marks its programs:
   the build keeps the stored kind of an unchanged, settled member and the expander reads the copybook's
   text from disk, so the next incremental build makes the programs whole; the member's own field rows
   stay absent, a `--rebuild` files it as before and recover re-files it again (so does any build that
   re-parses every member: the manifest changed - the UI rewrites manifest.json from the sources table on
   every build, so every library he adds or re-kinds in its table changes it - or a parser module changed;
   run recover after such a build, and it re-files them again - LESSONS 188); a real Assembler, listing
   or MFS member with a copybook's name is never re-filed (its shape is checked) and the report says so.
   This item makes the stand-in unnecessary: once the classifier files these as copybooks the re-parse
   gives them their own rows and `refile_misfiled()` finds nothing to do. Two windows the stand-in leaves
   open, closed by this item as well (LESSONS 187): a re-filed member whose text changes on disk before
   the re-parse is filed as before again by the next build, under a new member id, which un-links the
   programs copying it without parsing them again - they read 'ok' with the old text's fields until
   recover has run again and the build after it (the report's 'Re-filed as copybook' section says so);
   and the stand-in's shape guard (`recover._ASM_SHAPE`) first erred towards refusing, so a COBOL `START
   CUSTFILE` alone on its line (the KEY clause on the next line) read as an Assembler START - his procedure
   copybook, LESSONS 189; the guard now takes START as Assembler only with a label in column 1 or a numeric
   or quoted operand, and a strong signature found only in comment lines refuses nothing.
23. **reader.py: a copybook member whose every non-blank line keeps its text within columns 1-7 is read as
   code.** Two of his missing copybooks hold a 7-8 digit number in column 1 and nothing else (a stub, or a
   value meant to be copied): fixed-format reading takes columns 1-6 as the sequence area and column 7 as
   the indicator, `read_cobol_lines` yields no code, `build.code_line_count` gives 0 and the build files the
   member `empty` - the resolver never looks at it and every program copying it says COPY X NOT FOUND
   (LESSONS 192). At the re-parse: a member with no line reaching column 8 is read as free format (its
   text is the code) and counted; the reader says so in the member's note. Until then `atlas.recover`,
   `coverage` and `copybook NAME` say where the text sits ('the file holds N line(s) whose text sits in
   columns 1-7 ...') instead of the bare word `empty`, and the disk check names the file.

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
