# What a senior mainframe developer would still want from this tool (2026-09-24)

His question: "if you were the senior mainframe developer who has to do analysis, design, development, testing and support of this mainframe system, what other functionality or improvement would you recommend for this tool?"

Method: five readers, one per role (analysis, design, development, testing, production support), each walked a real week on an insurance IMS/DB2/CICS shop against README.md, ROADMAP.md, docs/AUDIT-scenarios-2026-09-21.md, templates/PLAYBOOKS.md, LESSONS.md and the command list in atlas/query.py. A judge then checked every recommendation against the repository, struck what already exists (with the file and line), merged duplicates and ranked the rest by value to a real week divided by cost. All names in the examples are fictional.

Cost classes: **query-only** = a new report over facts already in atlas.db, no re-parse; **re-parse** = touches a fact module, so it waits for the next re-parse batch (ROADMAP "Next re-parse batch"); **new parser** = a new input read by a new module outside the fact modules, no estate re-parse; **new host input** = an export the host must produce first.

## Ranked

### 1. `impact`: the affected-components table with the change class decided by rule, and the check-out list beside it

- Roles: analysis, design, development. Cost: query-only. Input: none.
- The moment: the change request says "widen POLICY-NO 10 to 12" or "add a sub-type to the policy master record", and by Thursday the lead wants one table: per program, job step, dataset, PSB, DBD, table and interface, the class of change (RECOMPILE-ONLY, SOURCE-CHANGE, BIND, PSBGEN+ACBGEN, DBDGEN+RELOAD, JCL-CHANGE, DATA-CONVERSION, EXTERNAL-CONTRACT) with the proof beside each row, and the members to reserve in the SCM.
- Today: PLAYBOOKS A1 runs eight commands by hand and the impact/design prompts ask the model to assign the class under INFERENCE. Every deciding fact is stored (the resolved copy per program, uses_sql/uses_dli/uses_cics, per-program field lengths, dataset record sizes, PCB sensitivity and PROCOPT, interface rows, GDG relatives). `copybook X` already groups expanders by copy with jobs, datasets, readers and callers.
- Build: `atlas/impact.py` (VS Code task "Atlas: impact") with `--field F | --copybook C | --column T.C | --segment S | --dataset D [--system S]` writing `work/impact.md`: the components table with the class decided by rule and the deciding fact cited (unchanged length at the root = RECOMPILE-ONLY; changed length or a MOVE into a shorter twin = SOURCE-CHANGE; uses_sql = BIND; a PCB on the DBD = PSBGEN+ACBGEN; segment bytes changed = DBDGEN+RELOAD with the unload/reload jobs; a DD or IDCAMS RECORDSIZE on the record = JCL-CHANGE; a GDG or VSAM holding the record = DATA-CONVERSION; an interface row = EXTERNAL-CONTRACT with the peer; a static CALL literal = "RELINK if NODYNAM, human must verify" until RM-06); the check-out list (member, kind, library, system, action, plus card members and PROCs); `overlap NAME a-b [--program P]` as the byte-range entry point (REDEFINES, parents, OCCURS elements, FILLERs, sort-card positions, DBD fields, MFS fields at those bytes); sort cards restricted to steps whose SORTIN resolves to a dataset written with that copybook. No schema change.
- Why first: every change request starts here, three roles asked for it independently, and it is pure query over stored facts.

### 2. `restart JOB STEP`: what the failed run left behind and where the rerun really starts

- Roles: support, design, testing. Cost: query-only (RESTART=/RD= as JCL facts wait for the batch). Input: none; a scheduler export sharpens the hold list. ROADMAP RM-04.
- The moment: 02:30, step five of eight abended; the pager asks "restart at step five or from the top, and what do I delete or roll back first so it does not die again with NOT CATLGD 2 or post the file twice".
- Today: `job X` prints the effective steps with DISP, GDG relatives, direction, guards and temporary datasets; the on-call works out the restart point by hand. The JOB card's RESTART= and RD= are not read.
- Build: for the steps before STEP: datasets now catalogued (NEW,CATLG and GDG +1, whose (0) on rerun is the half-written generation), MOD datasets that would be appended twice, temporary datasets a later step needs (the true restart point), non-idempotent updates (REWRITE/DELETE on I-O files, INSERT/UPDATE/DELETE, ISRT/REPL/DLET) whose commits a JCL restart does not undo, whether the failing program checkpoints and how the checkpoint id arrives, which guarded steps now run or skip, successors to hold.

### 3. `after JOB` / `before JOB` / `lineage DSN --downstream`: the transitive batch closure, with the schedule printed on `job X`

- Roles: support, analysis, design, testing. Cost: query-only for the dataset-implied order; the native scheduler loaders (RM-07) in a new `atlas/sched.py`, never build.py. Input: the shop's scheduler export (CA-7 LJOB, Control-M XML, Zeke event list: twenty lines with the names changed) for calendar, frequency and the true predecessors.
- The moment: 03:00, the nightly claims chain is down: which jobs now wait, whose morning file will be late, which department to page, which partner's NDM will not arrive; at design time, every job downstream of the policy extract in tonight's order.
- Today: one hop in three places (`job X` scheduler rows, `dataset DSN` writers and readers, `interfaces --dsn`); the schedule and calendar columns are loaded from the CSV and never printed.
- Build: a breadth-first walk over scheduler edges, dataset writer-to-reader edges (GDG-joined) and INTRDR submissions, every edge labelled with how the order is known (scheduler, INTRDR, dataset-implied) and cited, grouped by system, with fixed leaf reasons ("no indexed reader", "leaves the mainframe via NDM to PEER", "hop limit"); `job X` gains a Schedule line.

### 4. `review OLD NEW`: rule-based review of the changed copy against what the index already knows, with SQL host-variable and release-night preflight rules

- Roles: development, testing, support. Cost: query-only. Input: none (the lower environment is already indexed as its own system folder).
- The moment: Friday afternoon, the test copy is done and the reviewer has forty minutes before the promote: is there anything in this member the index already knows is wrong; and on release night, which LINK target has no CSD entry and which BMP step lacks the DBD's DD.
- Today: `diff` prints fact deltas, shifted fields and the flow from changed statements, and judges nothing.
- Build: a fixed list of rule ids applied to the new copy's facts, filtered to what differs from the old: an ISRT/REPL/DLET on a PROCOPT=G PCB, a dynamic CALL resolved to nothing, a USING count or byte mismatch, a literal compared to a field whose 88 levels never name it, a REPLACING pseudo-text that no longer occurs in the copybook, an unreached paragraph, a cursor opened and never closed, an EVALUATE without WHEN OTHER, a host variable whose PIC disagrees with the DCLGEN column type or a nullable column fetched with no indicator, a condition delta with no test class, an item declared and never referenced; release preflight: CSD, stage-1, DD and PSB presence for every job and transaction that runs a changed member. Each finding with severity, rule id and a cite on the new side; the handover pack includes it per changed member.

### 5. `regression --system`: the test scope closed from the release, and `reach PGM PARA`: the inputs that steer execution into the changed paragraph

- Roles: testing, development. Cost: query-only. Input: none.
- The moment: the release is three programs and two copybooks; QA asks which jobs and transactions must be in the regression run and which branch no test can reach; then the first run's DISPLAYs show the fixed paragraph never executed and you read backwards through the IFs to find which input flag would have got you there.
- Today: `diff --system`, `copybook`, `callers`, `program`, `paragraph`, `conditions` and `flow --up` each give one hop; the closure is chained by hand across six reports and "not covered by this regression" is never written from facts.
- Build: changed members to expanders to callers up to batch and online roots, to jobs, steps and transactions, to outputs and their one-hop readers, one table per hop with cites, then "reached only through" (unresolved CALLs, PROCs without a job, programs with no entry); `reach PGM PARA` walks the PERFORM graph from the entry, lists the tests on each path and, per tested field, `flow --up` cut at the first input (file record, parameter, DB2 column, DL/I area, screen field, JCL PARM via a new flow rule); a field nothing writes prints "branch has no input, candidate unreachable". Later on the same tables: `seed JOB`, a compare specification, and test records encoded from the layout.

### 6. IMS one level deeper: `psb NAME`, the SSA decoded to its segment, and MFS fields joined to the program's I/O field

- Roles: design, development, analysis, support. Cost: query-only for the common literal-SSA case. Input: none. ROADMAP RM-03 for the SSA and MFS halves; `psb` was never considered.
- The moment: the change adds a PCB to a PSB or turns a PROCOPT=G into A, and PSBGEN night is when you learn which other programs run under that PSB and address PCBs by position; "the dependant segment gains a 2-byte code" needs the programs that ISRT/REPL that segment, not a sibling on the same PCB, and the MFS screens whose input lands in it.
- Today: the PSB is reachable only from the DBD side; `segment SEG --dbd D` aligns DBD fields to every program's I/O area but warns that a call on a multi-segment PCB may touch a sibling; the SSA's segment name is a VALUE literal in the program's own data division and is never decoded; MFS field offsets and the I/O area are both indexed and never joined.
- Build: `psb NAME [--insert-at N | --remove N | --procopt N=A]`: the PCB table in positional order with the I/O-PCB-first rule and its source, programs bound to it (DFSRRC00 PARM, stage-1 APPLCTN, the calls), every call grouped by PCB with function checked against PROCOPT, and with `--insert-at N` the calls whose PCB position shifts; `ssa PGM` / `segment X --ssa`: each SSA decoded (segment, command codes, field, operator, value length) and checked against the DBD (unknown segment, unknown field, length differs, unqualified update, GHU without REPL); `segment` then splits "exact" from "possible", and `flow` crosses ISRT/REPL to GU/GN by DBD, segment and offset; `screen MID --programs` aligns each MFS field to the field at that offset in the program that reads the I/O PCB, the way `segment` does.

### 7. `uow PGM`: commit, checkpoint and restart profile, exits and return codes, and `contention T`: who else updates it and how often each commits

- Roles: design, development, support. Cost: query-only (EXEC CICS SYNCPOINT, ABEND ABCODE and HANDLE ABEND as facts wait for the batch). Input: a manifest list of the shop's abend routines.
- The moment: every BMP design is reviewed on its restart paragraph first (how often CHKP, is XRST coded, what is in the checkpoint area, does the DB2 COMMIT sit on the same counter); at 02:40 the same rows answer "-911 on the claims table in this program: who else was updating it, does it hold its locks for the run" and "U0100 in step three: which paragraph, on what condition; is RC 8 from step two an error or its normal warning".
- Today: CHKP, XRST, ROLB, COMMIT and ROLLBACK are stored as calls and statements and printed among the others; nothing summarises where the unit of work ends.
- Build: commit points in walk order with paragraph, guard, counter field and threshold; the checkpoint area with offsets; writes between consecutive commit points with PROCOPT; cursors open across a commit; the restart branch; a one-line verdict (restartable from checkpoint, rerun from start, no checkpoint logic). `exits PGM`: RETURN-CODE and abend-code settings with their guards, calls to the abend routines, STOP RUN and GOBACK per paragraph; `job X` gains "step two can end RC 0/4/8 (set at lines ...)". `contention T [--dbd | --dsn]`: every updater with the steps and transactions that run it, the utilities, and each program's commit line.

### 8. `abend PGM +HEX` from the compiler listings already fetched, the listing header as facts, and an on-call pack that reads the pasted JES lines

- Roles: support, development, analysis. Cost: new parser in recover.py's family (not a fact module, no estate re-parse). Input: the listings already in the estate, compiled with LIST or OFFSET plus MAP; one real listing head with the names changed, to build against. ROADMAP RM-01.
- The moment: 02:10, "ABEND=S0C7 offset +1A2C in the posting program": which statement, which COMP-3 field, which bytes of which input record, answered tonight by "cannot localise"; and with a tiny monthly quota, one file to read and one to hand the morning shift.
- Today: recover reads the source echo of every listing and deliberately stops before the Data Division Map and the cross-reference, so the OFFSET table and the map are never read; the compile date is read and dropped; nothing reads the compiler level or the options in effect.
- Build: tables `listing_run` (program, listing member, compiled at, compiler, options), `listing_stmt` (source line, hexloc, verb) from the OFFSET or LIST table and `listing_data` (item, base, displacement, length) from the map, filled on the same read as the copybook-source table. `abend PGM +HEX`: the statement at the greatest hexloc at or below the offset, its paragraph, the operands on that line with PIC, USAGE and offset (COMP-3 first), and through the file facts "bytes 45-49 of the record read from DD X = dataset Y, written by job Z step N"; a staleness check lines the listing's source up against the indexed member and refuses another compile's listing. `program X` prints "compiled on DATE (compiler LEVEL, DYNAM, TRUNC(BIN))"; `copybook X --stale` lists programs whose newest listing predates the copybook's member date (ISPF statistics via the member list the fetch already runs). `python -m atlas.oncall JOB STEP --abend S0C7 [--offset +HEX] [--paste work/abend.txt]`: a message-id reader over the pasted JES lines (IEF450I, IEF472I, CEE3204S, IGZ0035S, DSNT408I, DFS0555I, IEC030I) resolved against the index, then the failing step, restart, downstream, unit of work, the abend statement, the messages and the documents naming the job, in one `work/oncall.md` with its token size, plus a shift hand-over template.

### 9. `contract PGM` / `callers X --args --bytes`: CALL, ENTRY, LINK/XCTL COMMAREA and container arguments compared in bytes, both sides

- Roles: development, design, analysis. Cost: query-only. Input: none. Audit rank 10.
- The moment: adding a fourth USING argument, or extending the COMMAREA between the inquiry front-end and the validation program: which callers overrun (S0C4), which pass a 200-byte area the callee tests with EIBCALEN = 250, which BY CONTENT positions lose the return code.
- Today: `callers X --args` flags a count mismatch only and exempts LINK/XCTL; the per-argument rows (call_arg, param, per-program lengths) the value-flow batch delivered are the prerequisite, and the query never followed.
- Build: one section per entry (USING, each ENTRY, DFHCOMMAREA, each container GET) with the LINKAGE layout, then every call site with position, mode, the argument's length and a verdict per position (same bytes, caller shorter by n, caller longer, BY CONTENT where the callee writes back, literal, OMITTED, not in index); the EIBCALEN literals beside the callers' lengths; `commarea X` aligns the caller's 01 and the callee's DFHCOMMAREA by offset. Gate rule: an answer calling a flagged position compatible fails.

### 10. `live PGM FIELD --at LINE|PARA` and `flow --order`: which write is still live at a statement

- Roles: analysis, development. Cost: query-only (in atlas/flow.py). Input: none. Audit rank 2, the ORDER IN TIME family.
- The moment: the change is a MOVE in one paragraph and the reviewer asks whether that value still reaches the CALL in another, or whether a default paragraph overwrites it first.
- Today: `flow` is flow-insensitive by design and says so; `walk` gives reading order; the PERFORM graph and every write with its guard are stored.
- Build: a reaching-definitions pass per program over the paragraph graph (PERFORM returns, GO TO does not, THRU ranges as `walk` models them) for the field and its byte overlays; the writes that can reach the line, the writes dead there, and the paths; loops and GO TO DEPENDING stay conservative and say so; IF truth is never evaluated, stated in the footer.

### 11. `dataset X --layout`: the record contract in one place now; DCB, SPACE, REGION, RESTART= and the three DISP parts as JCL facts at the next re-parse

- Roles: analysis, design, support, testing. Cost: the `--layout` half query-only; the DD facts re-parse. Input: none. Audit ranks 19 and 25.
- The moment: widening a field changes the record length, and the one place left unchanged (copybook, FD, DD DCB, IDCAMS RECORDSIZE, sort card, GSAM DBD) is the S0C4 or "record length mismatch" at 02:00 on go-live night; and an SB37 on SORTOUT is answered by opening the JCL to see whether SPACE is the PROC default or this job's override.
- Today: `dataset DSN` prints the IDCAMS record size and never names the copybook; the FD record and the per-program fields are stored and not printed there; the DD's LRECL, RECFM, BLKSIZE and SPACE and the FD's RECORD CONTAINS are not facts.
- Build: `dataset X --layout` walks writer and reader programs to their FD by DD name and prints the 01 with its copybook and length as each program sees it, next to the IDCAMS size, the highest sort-card byte on steps reading it, the GSAM segment bytes and the map lengths, with a DISAGREE flag where two numbers differ. Batch: DD LRECL/RECFM/BLKSIZE/DSORG/SPACE/UNIT and step REGION/TIME with the same override-or-PROC-default provenance the DSN has, RECORD CONTAINS, RESTART=/RD=, DISP in three columns.

### 12. DB2 bind, plan and package facts: a query-only first cut over the BIND decks already in the indexed SYSTSIN text, the SELECT * flag, then the catalog unload

- Roles: support, design, analysis, development. Cost: query-only first cut; new host input for the catalog. Input: none for the first cut (BIND decks already in the fetched JCL and CNTL libraries); a DB2 catalog unload (SYSPACKAGE, SYSPLAN, SYSPACKLIST, SYSPACKDEP, SYSVIEWDEP, SYSINDEXES, SYSCOLUMNS; header row, names changed) for the truth of what is bound today. ROADMAP RM-02.
- The moment: "SQLCODE -805 DBRM OR PACKAGE NAME NOT FOUND IN PLAN" the night after a promote: which collection, which bind job to rerun; and "add a column to the policy table": which programs SELECT * and break on column count, which packages and plans to rebind, which views hide readers.
- Today: `table X` gives CRUD, DCLGEN columns, cursors, utilities and the CICS DB2ENTRY plan and admits views, aliases and BIND QUALIFIER are not resolved; the SYSTSIN text of every IKJEFT01 step is stored and only RUN PROGRAM is read from it; no "uses SELECT *" flag is printed.
- Build: (1) now: BIND PLAN/PACKAGE, MEMBER, PKLIST, COLLID, QUALIFIER, OWNER and ISOLATION read from the stored SYSTSIN text and card members at query time, so `program X` prints "bound: plan P, collection C, ISOLATION CS, by job J step S"; `table X --change` lists SELECT * and FETCH INTO users with their INTO structure, DCLGEN includers, FOR UPDATE cursors and dynamic statements; the impact table turns BIND into "REBIND package P in collection C". (2) then, in a new module: CREATE TABLE/VIEW/INDEX/ALIAS (views resolved to base tables, CHECK constraints into `values`) and a `--db2cat` loader for the unloads; `table` gains packages, indexes, views and referential constraints and DCLGEN-versus-catalog skew; with the precompiler timestamp from item 8 the consistency-token check closes -805.

### 13. `tag ID`: every line carrying a change tag, across the estate (added by the author, not by the readers)

- Roles: development, testing, support, analysis. Cost: query-only as a scan of the source members for one tag; a `line_tag` fact at the next re-parse for speed. Input: none.
- The moment: the shop marks every changed line with a change id in columns 1-6 or 73-80 (his listings show them beside the sequence numbers). "Show me everything project X touched", "what did the fix six months ago change, we may have to back it out", "review scope for this ticket" are asked in every release and every incident review.
- Today: the parser ignores columns 1-6 and 73-80 by design; `search` finds the tag only when it also appears in the text; nothing lists the lines, members and paragraphs a tag touches.
- Build: `tag ID [--system S] [--kind cobol|copybook|jcl]` scans the members' fixed columns for the id, prints member, line, paragraph (from the stored paragraph ranges) and the statement, grouped by member with cites; `diff` and the handover pack name the tags found on changed lines. At the next re-parse a `line_tag(member_id, line, tag)` table makes it instant and lets `program X` print "change tags present: ...".

## Already there, possibly unused

- Which library the compiler really read each copybook from, confirmed or contradicted per program: `python -m atlas.recover`, then `program X` ("listing says"), `work\recover.md`.
- S0C4 from a CALL whose USING count differs from the callee's LINKAGE: `callers PGM --args`.
- IMS AI/AJ/AM before they happen: `program PGM` (the DL/I table shows each call's PSB, DBD and PROCOPT); `dbd NAME`.
- Where a value goes or comes from across MOVEs, group moves, CALL USING, LINK COMMAREA, file bytes, DB2 columns, TS/TD/MQ, every stop labelled: `flow FIELD --program PGM [--up] [--hops N]`.
- A message seen in the JES log traced to the program and line that prints it: `messages 'TEXT'`; an error code's set, test and display sites: `literal E001`.
- Every value the code assumes for a field, including used-but-undocumented and documented-but-unused, cross-field rules, and the condition inventory with a negative per tested field: `values FIELD`, `pair A B`, `conditions PGM`.
- A step's full control cards and a job's effective steps with symbolics, overrides, guards, GDG and temporary datasets: `pack JOB --kind job`, `job JOB`.
- Every batch and online updater of a VSAM master and what crosses departments or leaves the mainframe: `dataset DSN`, `interfaces --dsn DSN`.
- Each DBD field at its bytes in every program's I/O area: `segment SEG --dbd DBD`.
- The record as one program sees it after REPLACING: `layout COPYBOOK --program PGM`.
- What a release changed, gathered with its token size: `diff --system SYS-TEST`, `python -m atlas.handover`.
- The program in reading order and who reaches a paragraph: `walk PGM`, `paragraph PGM NAME`.
- The CRUD matrix per job, copybook or system: `crud --job | --copybook | --system`.
- Transaction to program to map to file to plan: `transaction CODE`, `screen MAP`.
- What the index cannot know, by kind: `coverage`.
- QA workbooks, specs, screenshots and recordings by section and row: `docs TERM`, `doc NAME --grep TERM`, `images NAME`.
- The one-request prompts and the VS Code tasks: `/atlas-answer`, `/atlas-impact`, `/atlas-abend`, `/atlas-handover` and the rest.

## What stays out of reach, honestly

- Anything decided at run time: dynamic SQL, dispatch tables in DB2 rows or control cards, CALL targets arriving in LINKAGE, scheduler calendars. Only a catalog unload or a scheduler export closes it.
- Which load module really ran: binder aliases, static INCLUDEs, SCM stage. Out of reach until RM-06 plus the listing work in item 8.
- Non-COBOL code contributes no facts until RM-05: every chain through an Assembler routine, an Easytrieve report or a REXX driver ends at "NOT in index", and dataset readers are silently short by those programs.
- Flow is may-flow: every hop is a copy that can happen; IF truth is never evaluated. Item 10 adds reachability along the PERFORM graph, never which branch tonight's data takes.
- The tool sees source, never data: what is in the file, the row or the segment tonight is unknowable here, so every abend hypothesis ends with a check on the host (a dump, a SELECT, a browse).
- What actually ran (return codes, elapsed history, whether a paragraph executed in the test, which generation was really read) is not in the index; only a JES, SMF or scheduler-history export loaded as its own table could add it.
- Who changed this line last and why, and which test case covers it: SCM history and a test inventory are not indexed and not planned; item 13 gives the tag, not the author.
- Documents are prose and never facts: a spec-versus-code disagreement is quoted on both sides and left to the human.
- An abend offset map is only as good as the listing's provenance: the tool can prove the listing's source equals the indexed member, never that this listing built the load module that abended.
- Names read from recordings are leads, not facts.
- Peers outside the mainframe are known only from the manifest.
- The gate proves citations, not reasoning: a wrong change class with correct cites passes today; items 1 and 4 move the deciding rules into the tool.

## The three inputs from the host that unlock the most

1. Twenty lines of the scheduler export with the names changed (item 3, and sharper items 2 and 5).
2. One listing head compiled with LIST or OFFSET and MAP, names changed (item 8). The listings themselves are already in the estate.
3. A DB2 catalog unload of the package and plan tables, header row and a few rows with names changed (item 12). The BIND decks may already be in the fetched JCL.
