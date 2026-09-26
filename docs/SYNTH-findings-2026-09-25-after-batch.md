# Synthetic-estate comparison - after the re-parse batch (2026-09-26, second acceptance run)

This is the second acceptance test of branch reparse-batch-2, run on 5e63965 (71 commits after a0448c1). The first
run tested 4d6d976 and is in this file's history (47ebf2e, with its closing section in cca6c73). Between the two,
6a572c7 and c921d35 fixed the thirteen fact-module errors the first run left open. The report this one answers is
`docs/SYNTH-findings-2026-09-25.md`, which `tools/synth/check.py` wrote on the toolkit at a0448c1.

## For the owner, in plain words

The synthetic estate is a made-up insurance estate whose every fact is known, because the generator wrote it. On
the toolkit before the batch, 434 of its facts came out wrong. On the batch as it stands, 16 do, at both sizes of
the estate, and all 16 are the checker's own expectations, not errors of the toolkit (the table below says why for
each). All 30 minimal reproductions say FIXED: the fifteen from the synthetic estate and the fifteen shapes the
stages' verifiers found. The tests pass on Python 3.9 and 3.12, and selfcheck passes.

An index built by main still opens: this branch's `query` and `recover` read it without a crash, and the stand-ins
built this week still speak there. After this branch's build over that index (what the re-parse night does), the
index is the same as one built fresh by this branch, table for table, and recover finds nothing to re-file and
nothing arrived.

The batch is not ready for the re-parse night yet, for two reasons that the synthetic estate never shows. I tried
shapes around the fixes of 6a572c7 and found two new wrong facts, both in fact modules and both worse than main:

1. A program line that ends in a floating comment whose last word is COPY (`MOVE 1 TO WS-A.  *> KEEP A COPY`) makes
   the build read the first word of the next line as a copybook: `COPY GOBACK NOT FOUND`, and the program is
   `partial`. Main and 47ebf2e left the program `ok`. The cause is the new rule that reads a COPY over two lines,
   together with an older gap: in fixed format the reader keeps a `*>` comment as code.
2. A data copybook whose sequence numbers go up by 1 (000001, 000002, ...) and whose 88-level VALUES list of
   six-digit codes runs over three lines is still filed as a compiler listing, and its programs say `COPY ... NOT
   FOUND`. 6a572c7 fixed the usual numbering by 100; this is the case it left. Main filed it as a copybook.

Next: fix both in the fact modules, each with its regression test, LESSONS row and reproduction, then run this
acceptance again. Neither is likely in your estate (floating comments are rare in older code, and sequence numbers
usually go up by 100), but each would be written into the index on the re-parse night and would need another night
to take out.

## Before and after

Seed 20260925, scale 12 (268 members, 95 programs, 61 jobs). "Before" is the report of 2026-09-25 (toolkit at
a0448c1, the checker as first written). "After" is `tools/synth/check.py` on 5e63965, with the checker as the batch
left it (its truth for DFHAID and for D3 / D4 moved to what items 20, 22 and 27 decided).

| module | before: facts (rows) | after: facts (rows) | what is left |
|---|---|---|---|
| expand | 154 (3) | 0 | |
| cobol | 94 (24) | 0 | |
| copybook | 72 (6) | 4 (3) | F07 F08 F09 - the checker's |
| jcl | 39 (9) | 0 | |
| ims | 29 (4) | 0 | |
| db2 | 27 (1) | 0 | |
| query | 16 (3) | 0 | |
| flow | 2 (1) | 0 | |
| classify | 1 (1) | 0 | |
| build | 0 | 7 (2) | F01 F02 - the checker's (item 18) |
| recover | 0 | 5 (4) | F03-F06 - the checker's (item 19) |
| **total** | **434 facts, 430 findings, 52 rows** | **16 facts, 15 findings, 9 rows** | none is the toolkit's |
| facts matched | 10,776 | 11,394 | |

The "after" checker makes more checks than the first one, so the matched counts are not a one-for-one comparison.
The wrong counts are.

Seed 20260926, scale 30 (376 members, 149 programs, 115 jobs), to catch what the first seed never generates:

| | before (a0448c1, the first checker, measured in the first acceptance run) | after (5e63965) |
|---|---|---|
| findings (facts) | 430 (434), in the same nine modules as seed 20260925 | 15 (16) - the same nine rows as seed 20260925 |
| facts matched | 11,832 | 12,477 |

Both runs are the same as on 4d6d976, so the fixes of 6a572c7 and c921d35 changed nothing the estate generates. One
thing did change, as intended: in the first run, the harness's step-by-step index said "1 choice decided by the
listing" where a full re-parse said 2, because POLRPT01 was not parsed again when its listing rows arrived. Now the
harness's own index says "2 choices decided by the program's compiler listing", as a full re-parse does
(`build.picks_moved`, LESSONS 249).

## The reproductions

`python tools/synth/repro/verify.py`: 30 reproduction folders, 58 checks, every one FIXED, none REPRODUCED or OTHER.

- From the synthetic estate: F01 F02 F04 F05 F06 F07 F08 F09 F10 F12 F13 F14 F15 F16 F17.
- From the stages' verifiers: V01 (VALUES list read as a listing), V02 (MFS continued literal), V03 (prose heading),
  V04 (PL/I GO TO), V05 (change tag read as a stub), V06 (CA-PROGRAM-ID field), V07 (XML PARSE), V08 (nested
  END-CALL), V09 (GO TO separators), V10 (COPY over two lines), V11 (default card dataset), V12 (override row's
  generation), V13 (listing arrives after), V14 (listing moved while a twin exists), V15 (listing gone while a twin
  exists).

## Every remaining finding, judged

The nine rows are the same at both seeds. I checked each against this run's index and pages, not only against the
first run's verdicts.

| id | what the checker says | verdict |
|---|---|---|
| F01, F02 | POLEXT01, POLRPT01, CLMEXT01, CLMRPT01, BILEXT01, BILRPT01 and CMNUTIL should be `partial` (STDHDR chosen among several); the index says `ok` | The checker's expectation. ROADMAP re-parse item 18 records the pick once as the `ambiguous_copybook` row and leaves the program `ok`. `program POLEXT01` and `program CMNUTIL` say "parse: complete (copybook chosen among several - see notes)", which is D1's own promise. Next: the checker's truth for D1 should say `ok`. |
| F03 | recover's sentence "copybook choices checked against the listings: ..." is absent | The checker's expectation. The sentence is there: "1 confirmed, 1 contradicted by a current listing, 0 named by an older listing, 0 name a library the index does not hold, 6 unknown". The checker's pattern expects the older three-count wording from before main's five listing verdicts. Next: widen the pattern in `phase_recover`. |
| F04 | CLMRPT01 should be left alone by recover, and it is marked pending | The checker's expectation. CLMRPT01's current listing names a held copy with different text, so recover marks it for the next build (item 19), and the next build follows the listing. The second recover run reads "2 confirmed, 0 contradicted". Next: the checker should expect D2's contradicted program among the marked ones. |
| F05 | `program CLMRPT01` should say the listing CONTRADICTS the copy used | The checker's expectation. The queries run after the build that followed the listing, and the page says "listing says: STDHDR came from PROD.POL.COPYLIB (SYSLIB) - confirms the copy the build used", which is right. Next: expect "confirms" after the final build. |
| F06 | coverage's "of these choices is confirmed by the program's listing" is absent | The checker's expectation. Coverage says "2 of these choices are confirmed by the program's listing, 0 contradicted by a current listing": the plural of the same sentence, for the same reason as F05. |
| F07, F09 | `layout STDHDR`: STD-HEADER 55 bytes, HDR-FILLER at 35 | The checker's reading. The page prints all three copies of STDHDR: 54 bytes with HDR-FILLER at 34 first, then 62 with HDR-FILLER at 42, then 55 with HDR-FILLER at 35. Each is right for its own text. The checker compares the first block with POLICY's truth. Next: the checker should pick the block of the copy its truth describes. |
| F08 | CLMTRANR's CT-TRAN-RECORD: length 126 | The checker's expectation. After the checker adds `05 CT-NEW-CHANNEL-CD PIC X(03)` for the incremental build, the record is 1 + 10 + 8 + 6 + 8 + 6 + 1 + 63 + 3 + 23 = 129 bytes, which is what the index says. `_patch_incremental_truth` adds 3 to `record_length` and to CT-FILLER's offset, but not to the 01 group's own length. Next: add 3 there too. |

Names printed that the estate does not hold (the report's candidate list, 60 names at seed 20260926): I read them.
They are column headings and words of the reports (FIELD-NAME, MEMBER, WRITTEN, FOUND, INTENDED from "what was
INTENDED"), PARM text (RUNMODE=DELTA, CYCLE=52, RESTART=NO), IMS system-definition keywords (APPLCTN, GPSB, SNGLSEG),
temporary dataset names (`&&WKRPT`), queue names (POLQ, BILTSQ01), literals (AGENT NOT FOUND FOR THIS RECORD), the
manifest's peer (REINSURER-X) and the `EXEC-SQL` verb label. None of them is a fact the estate does not hold. Two
are the known wrong cite for an instream PROC (`BILWKLY:7`, `CLMWKLY:4`, the PROC's name printed as the member),
which ROADMAP's "JCL cites: known limits" lists. It is query-side only.

## An index built by main, opened by this branch

I built the seed 20260925 estate with main's toolkit (`git archive main atlas`, main at 68c51f9): once as built
(main1), and once after main's own recover and build (main2). The arrived copybook was dropped in after the first
build, as the harness does. Then I opened copies of both with this branch.

- `query`: 32 commands on each (coverage, coverage --all, ambiguous, interfaces, dead, values, program for nine
  programs, copybook for nine copybooks including SQLCA and the stub, layout, job, crud --job). No crash, no
  non-zero exit.
- The stand-ins speak on both. On main1, coverage splits the 45 `partial` members into 40 parsed only in part and 5
  complete with a copybook chosen among several, and says that POLARRVB and the recovered POLMISSB are on disk but
  arrived after the last build. On main2 the split is 5 and 8. Both name POLPROCB (and on main1 CMNDATEA and
  CMNCUSTP) as filed by the older classifier and to be re-filed by recover.
- `recover`: no crash on either. On main1 it re-files the three misfiled copybooks and marks 33 programs; on main2
  it re-files POLPROCB and marks 1. Both say the one contradicted choice will be followed by the next build, which
  re-parses every member because the toolkit changed.
- This branch's build over main2 re-parsed all 270 members. After it, recover finds nothing to re-file and nothing
  arrived, the listing check reads "2 confirmed, 0 contradicted", and coverage has no "built before" sentence. What
  recover still lists is true of the estate: the two stubs (POLSTUBB, POLSTUBC), the recovered POLMISSB, and one
  library the listings name that is not fetched.
- That index against one built fresh by this branch (build, recover, build) over the same estate: member kinds and
  statuses, every copy_use row with the member it resolved to, and every unresolved row are identical, and so are the
  row counts of field, pfield, field_ref, literal_ref, dd, step, paragraph, perform_edge, call_edge and cond88.

## Found in this run - fact modules

I probed the shapes around the fixes of 6a572c7 and c921d35 on small fictional estates and compared the results
with main (68c51f9) and with 47ebf2e. `cobol._leaves` gave the expected answer on 44 sentences (nested terminators,
phrases, inline PERFORM, XML / JSON), where 47ebf2e got 5 of them wrong. The PROGRAM-ID forms (name on the next line,
lower case, `IS INITIAL`, `ID DIVISION`, a CA-PROGRAM-ID field) came out right, and a quoted or unspaced PROGRAM-ID
falls back to the member name as it did before. `picks_moved` parses nothing again on an unchanged estate, including
with a nested COPY ... OF of a name chosen among several. `stub_count` reads change tags and real stubs as intended.
Two shapes came out wrong.

| what goes wrong | module | against main | status |
|---|---|---|---|
| A line ending in a floating comment whose last word is COPY (`MOVE 1 TO WS-A.  *> KEEP A COPY`, then `GOBACK.`) is read as a COPY of the next line's first word: `COPY GOBACK NOT FOUND`, the program `partial`. | expand.py (`copy_over_lines`) with reader.py | new: main and 47ebf2e give `ok` | confirmed (scratch probe p1b, QAPRM4). Next: read `_COPY_AT_END` and `find_copy_start` on the code before an unquoted `*>`; a regression test for both the two-line and the one-line shape; a LESSONS row. |
| The older shape of the same gap: `MOVE 1 TO WS-A.  *> SEE COPY QAKBK` expands QAKBK into the program, so the comment adds fields. | reader.py / expand.py | same on main | confirmed (QAPRM7). The same fix closes it. |
| A copybook numbered by 1 in columns 1-6 (000001, 000002, ...) whose 88-level VALUES list of six-digit codes runs over three lines is filed `listing`; its program says `COPY QCNAICS NOT FOUND`. | classify.py (`listing_hit`) | new since stage 20-22: main files it a copybook | confirmed (probe p3, QCNAICS). Next: check the numbered-lines shape after the copybook's own lines (a listing's numbered lines never match `_SIG_DATA_LEVEL`, because the code sits behind two numbers), as the stage verifier proposed; a regression test; a LESSONS row. |

## Still open outside the fact modules

These are in query.py, recover.py and flow.py. None of them changes the index, so none needs a re-parse night.
"Confirmed" means I read the code on 5e63965 and it is unchanged from what the finding describes; recover.py has not
changed since 08e96fd and flow.py since b100a33.

| stage | what goes wrong | file | severity | status |
|---|---|---|---|---|
| S-query | `flow` says a PROC's own default-card FTP transfer is where the dataset leaves the mainframe, while the only job that runs the PROC sends another dataset; `dataset` and `interfaces` say nothing of it | flow.py `interfaces_on` | bug | confirmed |
| S-query | `flow` matches a dataset against FTP notes by substring, so PROD.QW.IN "leaves the mainframe" because PROD.QW.INNER does | flow.py `interfaces_on` | bug | confirmed (`LIKE '%DSN%'`) |
| 11/12 | A NOT HELD verdict says to fetch a library the index holds as a marker-less dataset folder, and points at a fetch-list.txt that does not name it | recover.py `check_choices` | wrong words | confirmed (`holders` still built from the library table only) |
| 11/12 | The fetch list and coverage say to fetch for a recovered pick that the listing confirms or contradicts | recover.py, query.py | wrong words | reported |
| 19 | A recover run without the earlier `--from` folder drops that folder's listing rows. The next build now follows the rows left (so the index equals a fresh build over them), but the current listing is forgotten until recover is run with `--from` again. Nothing in ROADMAP or README says so. | recover.py `store_copy_sources` | minor | confirmed (probe fdrop: POLICY's copy, then CLAIMS's after the next build) |
| S-expand | A COBOL `COPY SQLCA` with no member is in no fetch list: recover says every copybook is in the index, and coverage's "Copybooks not found" is empty | recover.py, query.py | minor | confirmed (`_SYSTEM_INCLUDES` filter at recover.py:1839) |
| S-expand | A program's view of a record holding an IBM-supplied COPY (the MQ `COPY CMQMDV` shape) gives lengths and offsets without its bytes and says nothing | query.py | wrong words | reported |
| 23 | The STUB verdict of an older listing says "the program was compiled against the text the listing prints" | recover.py, query.py | wrong words | reported |
| 23 | `flow` names the outer copybook with a stray ')' for a stub copied inside another copybook | flow.py `_MISSING_COPY` | minor | confirmed (regex unchanged) |
| 23 | A stub copied only by a copybook no program copies: recover says "the programs copying it", `copybook` says no program copies it | recover.py | minor | reported |
| 21 | `copybook` of a card member filed ctlcard advises renaming its folder to end in COPYLIB although no program copies it | query.py | minor | reported |
| S-cobol | `field`'s flow line pairs the FD record with a program that does not hold it | query.py | minor | confirmed (`flow_name` and `files[0]` still taken apart) |
| S-cobol | coverage's "... N more" line counts a skipped (recursive) COPY as a missing copybook | query.py | minor | confirmed (`by_book` unchanged) |
| S-jcl | `interfaces` lists a job's USS override twice; a PROC run only as the outer PROC of a nest reads as "no indexed job runs it"; an expanded step's unresolved row is cited with the PROC's line | query.py | wrong words / minor | reported (the cite is in ROADMAP's known limits) |
| S-query | The hidden-rows sentence names jobs that the table does not list; a null PROC default prints `&HOST (PROC default )`; "(referback not followed)" for a referback to a DD with no dataset; a PROC named by its label rather than its member | query.py | wrong words / minor | reported |
| 11/12 | The precedence chain as README, the Field Manual, coverage and recover write it leaves out the estate's recovered copy | docs, query.py, recover.py | minor | reported |

## Fact-module limits the stages left on purpose

These are written in ROADMAP or README as the stage's choice. I do not count them against the batch, but the owner
may want to decide some of them before the night.

- A GO TO in both branches of IF ... ELSE (or in every WHEN of an EVALUATE) is still read as one that may not run,
  so a fall-through edge that never runs is recorded (cobol.py; ROADMAP 26 names it for a later night).
- Where a program is the only member of a copybook's name, the caller expands the whole program and the copybook is
  never reported missing (build.py; older behaviour, README states the exception).
- A procedure copybook whose only statement is one START line still takes `ctlcard` in a CNTL library, and prose in a
  `.txt` with no hint is still told from COBOL by its words (classify.py; LESSONS 199 and the stage's notes).
- On an index built before item 21, a COBOL `COPY SQLCA` whose copybook left the index reads as the precompiler's;
  the re-parse night clears it.

## How this was run

- `git status` clean on 5e63965. `git diff --stat a0448c1..HEAD`: 181 files. No scratch file is committed: outside
  `atlas/`, `tests/`, `tools/` and `docs/` the diff holds only README.md, ROADMAP.md, LESSONS.md and
  manifest.example.json; no .db, log or findings file outside `docs/`.
- The gate on 5e63965, run one after the other (they share a temp folder): `python -m unittest discover -s tests`,
  1,314 tests OK, exit 0; `py -3.12 -m unittest discover -s tests`, 1,314 tests OK, exit 0; `python selfcheck.py`,
  SELFCHECK PASSED, exit 0.
- `python tools/synth/repro/verify.py --out <scratch>`: exit 0, 58 checks FIXED.
- `tools/synth/check.py --out <scratch> --report <scratch>` at seed 20260925 scale 12 and at seed 20260926 scale 30.
  Both exited 0. With no `--report`, check.py writes into this repository's `docs/` folder under the day's date, so
  the report was sent to a scratch file.
- The main-built index, the probes and the `--from` scenario were run from scratch scripts outside the repository
  (scratchpad `batch3\acceptance2`).
