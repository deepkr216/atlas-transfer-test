---
mode: 'agent'
description: 'Atlas: ask anything about the estate in your own words - the queries are run for you'
---
The user asks a question about the mainframe estate indexed in `atlas.db` in this workspace.
Answer it by running the Atlas queries yourself - never from general knowledge, and never by
reading the toolkit's own source (`atlas/`, `tests/`): it holds no facts about the estate.

Question: ${input:question:Ask in your own words - a program, a job, a field, an error message, a value that changes...}

How to work:

1. Decide which reports answer the question and run them in the terminal, each written to
   `work/` with `--out` (never the shell's `>`):
   `python -m atlas.query --db atlas.db --out work/<name>.md <query> <args>`
   Queries: `walk PGM` (a program in reading order, with source) · `pack NAME` · `program NAME` ·
   `job NAME` · `field NAME` · `layout COPYBOOK` · `literal CODE` (where an error / message code
   is defined, set, tested, shown, and where its value flows) · `values FIELD` (every value the
   code assumes for a field, documented or not) · `pair F1 F2` (rules binding two fields) ·
   `messages PATTERN` (message texts, with the field each is moved to) · `search "TOKEN"` (any
   identifier or text - use it when the question names a concept but not a field) ·
   `conditions PGM` · `paragraph PGM NAME` · `crud --job JOB` · `callers PGM` / `callees PGM` ·
   `dataset DSN` · `table T` · `column T.C` · `dbd D` · `segment S` · `transaction CODE` ·
   `screen MAP` · `interfaces` · `docs TERM` · `cite MEMBER a-b` · `coverage`.
   When the question is about a concept (gender, relationship, a status) rather than a named
   field: start with `search "GENDER"` and `messages GENDER` to learn the field names, the
   copybooks and the message texts; then `values` on each field found, `literal` on each
   message code found. Run as many reports as the question needs; read them all.
2. What the reports do not contain is UNKNOWN - say so. Never fill a gap from memory. Use at
   most three `cite` ranges; if more is needed, say which query is missing.
3. Answer with these headings, in this order: `## FACTS` (one fact per line, each ending with
   `[[MEMBER line "token"]]` - the token copied verbatim from the report, never paraphrased,
   never from a comment line) · `## INFERENCE` (your reasoning, labelled) ·
   `## UNRESOLVED IN SCOPE` (copied from the reports) · `## NOT SEARCHED` ·
   `## UNVERIFIED` (anything you could not cite).
4. Write the answer to `work/answer.md`, then run
   `python -m atlas.verify_citations work/answer.md --db atlas.db --out work/gate.txt`.
   If it prints `RESULT: REJECT`, repair the failed citations from the reports (or move the
   claim to `## UNVERIFIED`) and run it again. End your reply with the gate's `RESULT:` line.

If `atlas.db` does not exist: say so and stop. The user builds it with the task
"Atlas: UI (fetch, build, coverage)" for the real libraries, or - to practise on the sample
programs that ship with the toolkit - with
`python -m atlas.build tests\fixtures --db atlas.db --rebuild --quiet`.
