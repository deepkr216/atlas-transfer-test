# tools/synth - a synthetic estate with its ground truth, and the comparison

Owner-side tooling, not part of the toolkit. It answers one question without
a live system: **does the index say what the source says?** A generator
writes an estate whose every fact it knows because it wrote it; a checker
builds the index over it and compares every answer with that truth.

Standard-library Python only. Nothing here reads `atlas/`: the generator's
byte rules, JCL expansion and listing shapes are its own reading of the
references, so a wrong rule in the toolkit shows up as a disagreement
instead of being copied into the truth.

## Files

| file | what |
|---|---|
| `synth_layout.py` | data items, byte layout (COMP / COMP-3 / SIGN SEPARATE / OCCURS / ODO / REDEFINES), rendering as fixed-format entries, COPY REPLACING applied to item trees |
| `synth_domain.py` | the insurance vocabulary: master / transaction / extract / history / control records, work and communication areas, DCLGENs, PCB masks, message tables |
| `synth_cobol.py` | a program written statement by statement, each fact (CALL, COPY, file, SQL, DL/I, CICS, paragraph, PERFORM, reference, literal) recorded at the line it was written |
| `synth_jcl.py` | jobs and PROCs with the generator's own expansion: symbolics, overrides, launchers, GDGs, directions |
| `synth_listing.py` | Enterprise COBOL listings (numbered source, C on copied lines, page breaks, the copybook-source table) for `atlas.recover --from` |
| `synth_programs.py` | the program templates per system: batch master update, extract, report, DB2, DL/I batch, BMP, MPP, CICS, subroutines, the special forms |
| `synth_estate.py` | the estate: three systems (POLICY, CLAIMS, BILLING) and SHARED, the deliberate defects, `truth.json` |
| `generate.py` | `python tools/synth/generate.py --out FOLDER [--seed N] [--scale N]` |
| `check.py` | `python tools/synth/check.py --out FOLDER` - the whole flow, then the findings report |
| `repro/` | one folder per finding: the smallest member(s) that show it, with the command and the two answers |

## The deliberate defects (the shapes LESSONS 181-192 were written for)

| id | shape | what the toolkit must say |
|---|---|---|
| D1 | `STDHDR` in three COPYLIBs with different content | the copying programs are "complete, with a copybook chosen among several", never "parsed only in part" |
| D2 | a listing whose copybook-source table contradicts the build's choice for one program and confirms it for another | `copybook choices checked against the listings: 1 confirmed, 1 contradicted`; `program` says `listing says: ... CONTRADICTS` |
| D3 | `POLPROCB`, a procedure copybook in a PROCS folder | filed `proc` by the folder; every note says so and names the folder fix |
| D4 | `CMNDATEA` (`05 START-DATE`) and `CMNCUSTP` (the COBOL `START` verb) in COPYLIB folders | filed `asm` by a line of their text; recover re-files them, the next build makes the programs whole |
| D5 | `POLMISSB`, copied by two programs (one with a REPLACING over two lines, the period inside the pseudo-text), present only in their listings | recover writes it, exactly |
| D6 | `POLSTUBB`, a stub of 7-digit numbers in columns 1-7 (and `POLSTUBC`, the 8-digit twin) | filed `empty`; the disk check says its text sits in columns 1-7 |
| + | `POLARRVB` dropped into the COPYLIB after the first build | recover says it arrived after the last build; the next build resolves it |

## The flow the checker runs

1. `generate` - the estate, the listings, `manifest.json`, `truth.json`.
2. `build --rebuild`, then one copybook changed (`CLMTRANR`) and an incremental build: the programs that copy it are parsed again, nothing else is.
3. the arrived copybook dropped in; `recover --from listings`; an incremental build; recover again (nothing left to do).
4. every query command over the samples (40 programs, 20 jobs, 34 copybooks, 34 fields, 12 datasets, 6 tables, every DBD, every transaction, coverage), the gate over a pack and over a wrong answer.
5. the comparison at two levels - the fact tables (every truth fact a row, every row a truth fact, offsets and lines exact) and the reports (every fact printed, every name real, every cite inside its member, the promised sentences present) - and the report.

The scratch folder is cleared only when it holds a `truth.json` this tool wrote and is not inside the repository.
