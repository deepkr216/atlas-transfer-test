# Mainframe Atlas

A deterministic fact index over a COBOL / JCL / IMS / DB2 / VSAM estate, built
with **zero model calls**, so that a language model can answer analysis,
design, development and testing questions from **facts it queries** instead of
**text it half-remembers**.

Standard-library Python only (3.9+). No `pip install`. Copy the folder, run it.

## Why this exists

The usual "index the folder and chat with it" approach hallucinates on a large
mainframe estate, and not because the estate is big:

1. **Fine-tuning / embedding does not store facts.** It teaches naming style.
   A model trained on the estate becomes fluent at inventing plausible program
   names, field names and job flows.
2. **Embeddings are near-useless on cryptic identifiers.** `WS-PLCY-STAT-CD-01`
   and `WS-PLCY-STAT-CD-02` are the same point in vector space; thousands of
   near-identical CRUD paragraphs collapse together; the retriever returns the
   *wrong but similar* member and the model reasons fluently about it.
3. **Silent zero-recall.** When retrieval brings back nothing relevant, the
   model fills the gap from memory instead of saying "not found". That *is* the
   hallucination mechanism.
4. **The facts are not in the text.** What a job does lives in control cards,
   PROC symbolics, `IKJEFT01`'s SYSTSIN, `DFSRRC00`'s PARM, GDG relative
   generations, PCB order, COPY REPLACING and the scheduler - none of which a
   chunk of COBOL contains.

So the answer is a parser, not a model. Atlas walks the folder, extracts hard
facts into SQLite with a line citation on every row, records everything it
could **not** resolve, and gives the model a small **fact pack** to reason
over. The model is demoted to a reader that cites; a mechanical gate rejects
any answer whose citations do not check out.

## Quick start

```bash
# 1. index the estate (source + documents; minutes for tens of thousands of members)
python -m atlas.build  C:/estate  --db atlas.db  --manifest manifest.json  --write-expanded out/expanded

# 2. read the coverage report FIRST - it lists what the index cannot know
python -m atlas.query --db atlas.db coverage
python -m atlas.query --db atlas.db ambiguous          # duplicate member names -> fix the manifest

# 3. ask
python -m atlas.query --db atlas.db program  CLMPOST
python -m atlas.query --db atlas.db job      CLMNIGHT
python -m atlas.query --db atlas.db field    PM-POLICY-STATUS
python -m atlas.query --db atlas.db literal  E001               # where is this code set / tested / shown
python -m atlas.query --db atlas.db values   WS-GENDER-CD       # every value the code assumes (incl. undocumented)
python -m atlas.query --db atlas.db pair     WS-REL-CD WS-GENDER-CD   # cross-field rules (son must be male)
python -m atlas.query --db atlas.db messages GENDER             # message texts naming a rule
python -m atlas.query --db atlas.db copybook PMASTREC           # impact
python -m atlas.query --db atlas.db callers  RATECALC --depth 3
python -m atlas.query --db atlas.db dataset  PROD.POLICY.EXTRACT
python -m atlas.query --db atlas.db search   '"WS-PLCY-STAT-CD"'

# 4. hand a model a fact pack, not the repository
python -m atlas.query --db atlas.db pack CLMPOST > pack.md

# 5. gate the model's answer before anyone reads it
python -m atlas.verify_citations answer.md --db atlas.db
```

`selfcheck.py` runs the regression suite and a smoke build; run it after every
change to the toolkit and before copying it to another machine.

## What gets extracted

| Artifact | Facts | The trap it avoids |
|---|---|---|
| COBOL | PROGRAM-ID, paragraphs (Area A), PERFORM/THRU, CALL literal **and** identifier (resolved via MOVE/VALUE, else *unresolved*), COPY / `EXEC SQL INCLUDE` / `++INCLUDE` / `-INC`, SELECT…ASSIGN→DD, OPEN mode, EXEC SQL tables+host vars (INTO = write), CBLTDLI/AIBTDLI, EXEC CICS LINK/XCTL, MQ, every field reference with **read/write/test/display** mode, every **literal** with how it is used | Commented-out CALLs indexed as live; `WS-CALL-FLAG` matched as a CALL; multi-line statements split; `PROGRAM-ID.` on its own line; period inside PIC/literal |
| Copybook | Field tree with **byte offsets/lengths** (COMP-3 packed, COMP 2/4/8, SIGN SEPARATE), REDEFINES held at the same offset, OCCURS DEPENDING ON flagged variable, 88-levels, VALUE literals, fragments (05s without an 01) wrapped correctly | Test data of the wrong length; "change field X" without knowing what moves |
| Expansion | Every program materialised with COPY/INCLUDE inline and REPLACING applied, with a line map back to (member, line) | Fields renamed by REPLACING are invisible to grep; procedure copybooks attributed to the wrong member |
| JCL / PROC | JOB/EXEC/DD with continuations, inline control cards captured, symbolics resolved (SET > EXEC override > PROC default), GDG base split from `(+1)`, `//STEP.DD` overrides, **effective program** unwrapped from IKJEFT01/DFSRRC00/SORT/IDCAMS/IEBGENER/IEFBR14/DSNUTILB/FTP/NDM, sort-card **byte positions** | Steps attributed to TSO or the IMS region driver; producer/consumer linkage lost through GDG; `DISP` mistaken for direction |
| IMS | DBD segments/fields/hierarchy; PSB PCBs in **positional** order with PROCOPT, `CMPAT=YES` / TP PCB → I/O PCB first | Off-by-one PCB → wrong database named |
| Documents | .docx/.xlsx/.pptx/.vsdx text, headings, tables, image manifests; PDF best-effort; legacy .doc/.xls reported | Docs indexed as if they were facts |

Every parse failure is recorded on the member; every unresolved reference is a
row in `unresolved`; every report ends with the unresolved items in its scope.

## Direction of I/O

`DISP` is serialisation, not direction. `DISP=OLD` is routinely an output
dataset. Direction is taken from, in order: the program's own `OPEN`
verb (joined through `ASSIGN`), the GDG relative generation, utility DD-name
conventions (SORTIN/SORTOUT, SYSUT1/SYSUT2), and only then `DISP=NEW/MOD` as a
weak hint. Every DD row records which signal decided it (`mode_source`).

## The four-names problem

PDS member name, `PROGRAM-ID`, CSECT and load-module/alias routinely differ.
Atlas keys members by path, stores `PROGRAM-ID` separately, and never merges
by name. Members with the same name and different content are listed by
`ambiguous`; declare the production libraries in `manifest.json` or the index
cannot know which copy is real.

## Working with a small token budget

The model must never read the repository. It reads **packs**:

1. `query pack PGM` (or `program`/`job`/`field`/`literal`) - a few hundred
   lines of facts with `MEMBER:line` citations and the exact evidence lines.
2. The model answers using **only** the pack, citing `[[MEMBER line "token"]]`.
   Anything it cannot cite it must mark `UNVERIFIED` or `INSUFFICIENT EVIDENCE`.
3. `verify_citations.py` rejects the answer if any citation fails.
4. `query cite MEMBER a-b` fetches more lines only when the pack was not enough.

With this loop a typical analysis question costs one model call over ~2-5k
tokens of input. Bulk work (indexing, impact sets, dossiers, test-condition
inventories) costs zero.

`templates/CLAUDE.md` is the operating contract to put at the root of the
estate folder for a coding agent; `templates/PLAYBOOKS.md` has the per-task
procedures (impact analysis, job dossier, error-code trace, design doc,
code-generation bundle, test conditions, abend triage).

## What Atlas is not

- **Not a compiler.** Extraction is grammar-aware regex over properly
  tokenised source. It is deliberately conservative and reports what it
  skipped, but a compiler listing (LIST/MAP/XREF) is the authoritative expanded
  source and cross-reference - index listings when you have them.
- **Not a scheduler.** Batch flow lives in CA-7/Control-M/TWS. Export it to CSV
  and load it with `--sched`; until then "dead job" and "what runs before"
  are unanswerable, and the reports say so.
- **Not online-aware yet.** `transaction_def` exists for CICS CSD / IMS SYSGEN
  exports; a loader is the next step. Until then transaction→program is unknown.
- **Not modelling** SYNC alignment slack, GO TO in dead-paragraph analysis
  (never-PERFORMed paragraphs are *candidates* only), dynamic SQL targets, or
  targets read from control tables (these appear as `unresolved`).

## Repository layout

```
atlas/
  reader.py            fixed-format reader: cols 7/72, ragged margins, continuations, statement splitting
  classify.py          content-sniffed member typing (extensions are hints only)
  copybook.py          field tree + byte offsets + 88s
  cobol.py             program facts: calls, copies, files, SQL, DL/I, field & literal refs
  expand.py            COPY/INCLUDE expansion with REPLACING and a line map
  jcl.py               jobs/steps/DDs, symbolics, effective program, direction, sort cards
  ims.py               DBD / PSB (HLASM macro format), PCB positions
  docs.py              docx/xlsx/pptx/vsdx/pdf/html/text extraction (stdlib only)
  schema.sql           the fact store
  build.py             folder -> atlas.db
  query.py             questions -> markdown with citations
  verify_citations.py  the gate
templates/             CLAUDE.md operating contract, PLAYBOOKS.md
tests/                 regression suite; every bug found becomes a fixture + assertion (see LESSONS.md)
selfcheck.py           run before trusting or copying the toolkit
manifest.example.json  how to declare production libraries
```

## Getting it onto a locked-down machine

The repository is plain files. Copy it (git bundle, zip, USB - whatever policy
allows), run `python selfcheck.py`, then `python -m atlas.build`. The database
never leaves the machine it is built on. Nothing here makes a network call.
