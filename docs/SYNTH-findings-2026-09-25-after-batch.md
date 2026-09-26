# Synthetic-estate comparison - after the re-parse batch (2026-09-26)

This is the acceptance test of branch reparse-batch-2 at 4d6d976 (66 commits after main a0448c1). The report it
answers is `docs/SYNTH-findings-2026-09-25.md`, which `tools/synth/check.py` wrote on the toolkit at a0448c1.

## For the owner, in plain words

The synthetic estate is a made-up insurance estate whose every fact is known, because the generator wrote it. On
the toolkit before the batch, 434 of its facts came out wrong in the index or the reports. On the batch, 16 do,
and all 16 are the checker's own expectations, not errors of the toolkit: 7 are the D1 programs that the checker
still expects to be `partial` (item 18 makes them `ok` on purpose), 5 are the D2 listing check that the checker
still expects to read "contradicted" after the build that follows the listing (item 19 makes it confirmed on
purpose), and 4 are two layout checks that read the wrong copy of STDHDR or forgot to add 3 bytes to CLMTRANR's
record after the checker changed it. A second, larger estate (seed 20260926, scale 30) gives the same 16 and
nothing new. All fifteen minimal reproductions now say FIXED.

That does not make the batch ready for the re-parse night. The stages' own verifiers found wrong facts in the
fact modules that the synthetic estate never generates, and I reproduced ten of them on this branch as it
stands (the list is below). Each one would be written into the index on the re-parse night and would need
another night to take out. Next: fix the rows below whose status is "confirmed", each with its regression
test and LESSONS row, then run this acceptance again before the night.

## Before and after

Seed 20260925, scale 12 (268 members, 95 programs, 61 jobs). "Before" is the report of 2026-09-25 (toolkit at
a0448c1, the checker as first written). "After" is `tools/synth/check.py` on 4d6d976 (the checker as the batch
left it, with its truth for DFHAID and for D3 / D4 moved to what items 20, 22 and 27 decided).

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

The "after" checker makes more checks than the first one did, so the matched counts are not a one-for-one
comparison; the wrong counts are.

Seed 20260926, scale 30 (376 members, 149 programs, 115 jobs), run to catch what the first seed never generates:

| | before (a0448c1, the first checker) | after (4d6d976) |
|---|---|---|
| findings (facts) | 430 (434), in the same nine modules as seed 20260925 | 15 (16) - the same nine rows as seed 20260925 |
| facts matched | 11,832 | 12,477 |

## The reproductions

`python tools/synth/repro/verify.py`: 15 reproduction folders, 34 checks, every one FIXED, 0 still showing the
symptom. F01 F02 F04 F05 F06 F07 F08 F09 F10 F12 F13 F14 F15 F16 F17.

## Every remaining finding, judged

The nine rows are the same at both seeds.

| id | what the checker says | verdict |
|---|---|---|
| F01, F02 | POLEXT01, POLRPT01, CLMEXT01, CLMRPT01, BILEXT01, BILRPT01 and CMNUTIL should be `partial` (STDHDR chosen among several); the index says `ok` | The checker's expectation. ROADMAP re-parse item 18 records the pick once as the `ambiguous_copybook` row and leaves the program `ok`; D1's own words in the truth ("Complete, with a copybook chosen among several", "NOT parsed only in part") are matched in coverage and in `program` for all seven. Next: the checker's truth for D1 should say `ok`. |
| F03 | recover's sentence "copybook choices checked against the listings: ..." is absent | The checker's expectation. The sentence is there: "1 confirmed, 1 contradicted by a current listing, 0 named by an older listing, 0 name a library the index does not hold, 6 unknown". The checker's pattern expects the older three-count wording, from before main's five listing verdicts. Next: widen the pattern in `phase_recover`. |
| F04 | CLMRPT01 should be left alone by recover, and it is marked pending | The checker's expectation. CLMRPT01's current listing names a held copy with different text, so recover marks it for the next build (item 19), and the next build follows the listing. The second recover run then reads "2 confirmed, 0 contradicted". Next: the checker should expect D2's contradicted program among the marked ones. |
| F05 | `program CLMRPT01` should say the listing CONTRADICTS the copy used | The checker's expectation. The queries run after the build that followed the listing, and the page says "listing says: STDHDR came from PROD.POL.COPYLIB (SYSLIB) - confirms the copy the build used", which is right. Next: expect "confirms" after the final build. |
| F06 | coverage's "of these choices is confirmed by the program's listing" is absent | The checker's expectation. Coverage says "2 of these choices are confirmed by the program's listing, 0 contradicted": the plural of the same sentence, for the same reason as F05. |
| F07, F09 | `layout STDHDR`: STD-HEADER 55 bytes, HDR-FILLER at 35 | The checker's reading. The page prints all three copies of STDHDR, BILLING's first (54 bytes, HDR-FILLER at 34), then CLAIMS's (62), then POLICY's (55, HDR-FILLER at 35). Each is right for its own text; the checker compares the first block with POLICY's truth. Next: the checker should pick the block of the copy its truth describes. |
| F08 | CLMTRANR's CT-TRAN-RECORD: length 126 | The checker's expectation. After the checker adds `05 CT-NEW-CHANNEL-CD PIC X(03)` for the incremental build, the record is 40 + 63 + 3 + 23 = 129 bytes, which is what the index says. `_patch_incremental_truth` adds 3 to `record_length` and to CT-FILLER's offset, but not to the 01 group's own length. Next: add 3 there too. |

Names printed that the estate does not hold (the report's candidate list): I read them. They are column headings
and words of the reports (FIELD-NAME, MEMBER, WRITTEN, FOUND), IMS system-definition keywords (APPLCTN, GPSB,
SNGLSEG), REPLACING tokens (CLMU in `==:PCB:== BY ==CLMU==`), literals (SMITH), TS queue names (BILTSQ01), the
manifest's peer (REINSURER-X) and the `EXEC-SQL` verb label. None of them is a fact that the estate does not hold.
One is a known wrong cite: BILWKLY, an instream PROC's name, is printed as the member of a cite (`BILWKLY:7`).
ROADMAP's "JCL cites: known limits" lists this. It is query-side only.

## An index built by main, opened by this branch

I built the synthetic estate with main's toolkit (`git archive main atlas`), once as built and once after main's
own recover and build. Then I opened both with this branch's `query` (coverage, program, copybook, layout,
ambiguous, job, interfaces, crud, dead, values: 23 commands on each) and `recover`. Nothing crashed. The stand-ins
speak on the index built by main. Coverage splits the 13 `partial` members into 5 parsed only in part and 8
complete with a copybook chosen among several. recover says that POLARRVB and the recovered POLMISSB arrived after
the last build, that POLPROCB (filed `proc` by main) is a copybook to re-file, and that POLSTUBB is held in a form
the reader cannot see.

This branch's build over that index re-parsed all 270 members, because the toolkit changed. After it, recover finds
nothing to re-file and nothing arrived, coverage prints no "built before" sentence, and the listing check reads
"2 confirmed, 0 contradicted".

One difference remains between that index and one built step by step. POLRPT01's listing confirms the copy the
chain picked. After a full re-parse the pick's note says "the program's compiler listing names PROD.POL.COPYLIB".
After the harness's incremental build it still says "same system", because the program was not parsed again when
its listing rows arrived, so coverage reads "1 choice decided by the listing" in one index and "2" in the other.
The copy used is the same in both. This is the milder form of the arrival-order problem below.

## Open in the fact modules - found by the stages' verifiers, never generated by the synthetic estate

"Confirmed" means I reproduced it on 4d6d976 for this acceptance test. "Reported" means the stage's verifier
reproduced it, and the code it names has not changed since.

| stage | what goes wrong | module | status |
|---|---|---|---|
| 19 | A pick made by following a listing filed elsewhere stays in the index after the program's own current listing arrives. The index then depends on the order the listings arrived, and recover says "the copy it used stands". | build.py / recover.py | confirmed (runner `arrive`: step 3 keeps POLICY's copy, the forced re-parse gives CLAIMS's) |
| 21 | A compiler listing that leaves the program's system (moved to SHARED\LISTINGS, or deleted) while a twin of the program exists forces no re-parse. The program keeps the other system's copy, and recover says "unknown". | build.py / recover.py | confirmed (moved and deleted: DIFFERENT from a full parse) |
| 20/22 | A data copybook whose 88 VALUES list of six-digit codes runs over three lines is filed as a compiler listing, and its copiers go `partial`. | classify.py | confirmed |
| 20/22 | An MFS or Assembler continuation line whose literal starts with MOVE ... TO (or GO TO, PERFORM, SET, ADD) files the member as a copybook, and `screen` says NOT FOUND. | classify.py | confirmed |
| 20/22 | A prose heading such as `02 Premium values` files a document as a copybook. | classify.py | confirmed (minor) |
| 23 | A copybook whose lines carry a `*` change tag in columns 1-6, with one line of digits alone, is filed `stub` and never expanded. | reader.py | confirmed (`stub_count` returns 1) |
| 11/12 | A copybook with a field named `CA-PROGRAM-ID` is taken for a program and dropped from the choice. The program silently expands another system's copy, with no `ambiguous_copybook` row. | build.py / cobol.py / classify.py | confirmed (GC's program expands SHARED's copy; CA-PROGRAM-ID is lost) |
| 26 | `XML PARSE ... ON EXCEPTION GO TO` at the start of a sentence, and a scope terminator inside a nested statement of the same verb, are read as always leaving, so the next paragraph is "reached by nothing". | cobol.py | confirmed (`_leaves` returns True for both) |
| 27 | `COPY` with the copybook's name on the next line is silently not expanded: `parse: ok`, no copybook listed. | expand.py | confirmed |
| 29 | A job that runs a PROC with its default sequential card dataset gets no `card_seq_assumed` row, and the same PROC run with an override does. | jcl.py | confirmed |
| 29 | A job step's `//PS.DD` override row naming a (+1) that an earlier PROC step writes still says `output [gdg_relative]`. | jcl.py | reported |
| 26 | GO TO ... DEPENDING ON with a comma or semicolon after the index, and `GO X` without TO, give no edge. | cobol.py | reported (minor, older than the batch) |
| 20/22 | A PL/I member with `GO TO` in a folder named like a dataset is filed as a copybook. | classify.py | reported (minor; my probe with another PL/I text stayed `unknown`) |

The stages' query-side and recover-side findings (for example: `flow` saying a PROC's own default-card FTP
transfer leaves the mainframe, a dataset matched by substring in `flow`, wording of the STUB verdict for an older
listing, the fetch list for a confirmed recovered pick) are open too. None of them costs a re-parse night.

## How this was run

- `git status` clean. The diff a0448c1..HEAD holds no scratch file: the only files outside `atlas/`, `tests/`,
  `tools/` and `docs/` are README.md, ROADMAP.md, LESSONS.md and manifest.example.json.
- The gate on 4d6d976: `python -m unittest discover -s tests` 1,286 tests OK. `py -3.12 -m unittest discover -s tests`:
  1,286 tests OK. The first run failed one test, `test_convert`'s check that no `atlas-stage-` folder is left in
  the system temp folder, because I ran the two suites at the same time and they share that folder. Run alone, it
  passes. `python selfcheck.py`: PASSED.
- `tools/synth/check.py --out <scratch> --report <scratch>` at seed 20260925 scale 12 and at seed 20260926 scale
  30. The report was written to a scratch file. With no `--report`, check.py writes into this repository's
  `docs/` folder, under the day's date.

## After this report

Every row of "Open in the fact modules" above is fixed in 6a572c7, each with its test in
`tests/test_acceptance_fixes.py`, its LESSONS row (248-252) and a minimal reproduction under `tools/synth/repro/`:
V01-V06 the classifier, the reader and the program test; V07-V09 `cobol._leaves` and the GO TO edges; V10 the COPY
over two lines; V11-V12 the two JCL rows; V13-V15 the listing that arrives, moves or goes (verify.py runs those with
the new `steps` in expect.json). On the toolkit this report tested (4d6d976's code) each of the fifteen shows its
symptom (23 checks REPRODUCED, V12's control FIXED); after the fix all 30 folders say FIXED. The synthetic estate at seed 20260925, scale 12 gives the same 15 findings (all the checker's own) and 11,394
facts matched. What is left: the checker's own rows F01-F09 (its "Next:" in the table of judged findings) and the
query-side and recover-side findings the stages listed, none of which costs a re-parse night. Next: run this
acceptance again before the night.
