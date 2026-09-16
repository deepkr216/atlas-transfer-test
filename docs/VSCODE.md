# Using Atlas from VS Code

You open the toolkit folder (the `git pull` clone, which also holds
`atlas.db`) as the VS Code workspace. Everything below then works without
installing anything: the files arrive with `git pull`.

## The easy way: `/atlas` and a question in your own words

1. Open the chat (**Ctrl+Alt+I**).
2. Type `/atlas`, press Enter, and type your question the way you would
   ask a colleague - for example: *Gender is M, F or U and we are adding N.
   Which error messages must change, which are related but need no change,
   in which copybook or program is each one, and where is each message ID
   created?*
3. The chat switches to agent mode and runs the toolkit's queries itself
   (it asks before each terminal command - click **Continue**), reads the
   reports, answers with a citation on every fact, and finishes by running
   the gate. The last line of the reply is the gate's verdict:
   `RESULT: citations verified` or `RESULT: REJECT`.

That is all you need. The tasks and the other `/atlas-*` prompts below do
the same work step by step when you want to control which report the model
sees, or when a chat has no agent mode.

```
.vscode/tasks.json               Terminal > Run Task > "Atlas: ..."  - runs the queries, writes work/*.md
.github/copilot-instructions.md  the operating contract; Copilot Chat loads it by itself
.github/prompts/atlas-*.prompt.md  the requests; type "/" in the chat and pick /atlas-answer, /atlas-impact ...
templates/REQUESTS.md            the same requests as text, for a chat tool without prompt files
work/                            every report and answer; ignored by git; never leaves the laptop
```

## The loop

1. **Run the query** - `Terminal > Run Task > Atlas: pack NAME -> work/pack.md`,
   type the name, done. The report is in `work/pack.md` (UTF-8; the task
   uses `--out`, never the shell's `>` - PowerShell 5.1 writes UTF-16 and
   the model then reads garbage).
2. **Ask** - in Copilot Chat type `/atlas-answer`, fill in the question,
   send. The prompt attaches `work/pack.md` for you and carries the rules
   (only the pack, cite every fact, FACTS / INFERENCE / UNRESOLVED / NOT
   SEARCHED). Pick the model you want from the model list - the rules do not
   depend on the vendor.
3. **Save** - copy the answer into `work/answer.md` (in agent mode, add
   "write the answer to work/answer.md" and it does it).
4. **Gate** - `Run Task > Atlas: verify work/answer.md -> work/gate.txt`.
   `RESULT: citations verified` means every quoted token is on the cited
   line of a member that has not changed since the index was built.
5. **If it failed** - `/atlas-fix` in the chat: it reads `work/answer.md`,
   `work/gate.txt` and the pack, and returns the corrected answer. Save,
   verify again. A claim without a line behind it disappears; it is not
   reworded.

That is one model call per question, over a few thousand tokens, and the
answer is checked mechanically before you rely on it.

## Which task and which prompt

| You want | Run the task(s) | Then type |
|---|---|---|
| One question about a program, job, copybook, transaction, field | Atlas: pack | `/atlas-answer` |
| What does this program do (the read you do by hand) | Atlas: walk (and pack for the JCL side) | `/atlas-program` |
| Impact of changing a field | Atlas: field, layout, crud for a job, pack (copybook), interfaces | `/atlas-impact` |
| What does this job do | Atlas: job, pack (choose kind: job) | `/atlas-job` |
| Where does error code E001 come from | Atlas: literal / error code | `/atlas-trace` |
| Where is this field populated | Atlas: field, pack | `/atlas-populate` |
| Abend triage | paste the JES lines into `work/abend.txt`; Atlas: job, pack, conditions, layout | `/atlas-abend` |
| A new code value / codes merged | Atlas: value domain | `/atlas-values` |
| Test conditions and test data | Atlas: conditions, layout, pack | `/atlas-tests` |
| Change design document | the tasks for each field and job touched | `/atlas-design` |
| Transition document for a release (Application Operations + Contact Center) | Atlas: handover pack (one task: give the system holding the changed copies and the plan / stories / QA workbook names), then fill `work/release-facts.md` | `/atlas-handover` (one request) |
| What a recorded session showed and said | Atlas: read recordings (once per new recording), then the build; then Atlas: document sections about a term | `/atlas-answer` (quote it as a recording, not as fact) |
| The gate said FAIL | Atlas: verify | `/atlas-fix` |

`Atlas: coverage` before trusting anything; `Atlas: UI` for fetch and build;
`Atlas: selfcheck` after every `git pull`.

## Ask mode or agent mode

**Ask mode** (default in the prompts): the model sees exactly the files the
prompt attaches. Cheapest, and nothing else in the workspace is read.

**Agent mode**: the model may run the queries itself - the instructions file
tells it to use `--out work/...` and to run the gate before finishing. You
approve each terminal command it proposes. Useful for a multi-report
question (a design document); costlier, because the model also reads what it
decides to read. Keep the instruction in `copilot-instructions.md` §6 in
mind: it must not scan the workspace or read `atlas/`.

## Settings to check once

- `github.copilot.chat.codeGeneration.useInstructionFiles` - on (default):
  Copilot reads `.github/copilot-instructions.md`.
- `chat.promptFiles` - on: the `/atlas-*` prompts appear when you type `/`.
  If your Copilot is too old for prompt files, use `templates/REQUESTS.md`
  and attach the `work/` files with `#file:` or the paperclip.
- Python on PATH as `python` (the tasks call it that way). If only `py`
  works, change `python` to `py -3` in `.vscode/tasks.json` - it is a plain
  text file.

## Other chat tools

The contract is vendor-neutral. Copy `.github/copilot-instructions.md` to the
name the tool reads: `.cursorrules` (Cursor), `.clinerules` (Cline),
`CLAUDE.md` (Claude Code - `templates/CLAUDE.md` is that file already), or
paste it as the system / custom instruction of a web chat. The requests are
in `templates/REQUESTS.md`; the tasks work in any VS Code.

If your workspace is not the toolkit folder (for instance the estate folder
itself), copy `.vscode/`, `.github/` and point `--db` in `tasks.json` at
wherever `atlas.db` lives.

## What the company model may see

The packs contain real source lines from the estate - that is the point,
and the company's approved model on the company laptop is allowed to see
the company's code. The rule that nothing real leaves the laptop applies to
the way home: `work/`, `atlas.db`, `out/`, real member, dataset or job names
never go into a message to the home side. What comes home is the output of
`selfcheck.py`, `atlas-crash.txt`, `atlas.diag --redact`, and tiny fake
fixtures (see `REPORTING.md`).

## When something looks wrong

- The prompt is not in the `/` list: the `chat.promptFiles` setting, or the
  folder you opened is not the one holding `.github/prompts/`.
- "Task not found": open the folder that contains `.vscode/tasks.json`.
- The model answers from general knowledge anyway: it did not get the
  file - check the chat's "References" line shows `work/pack.md`; attach it
  by hand with `#file:work/pack.md` if not.
- The gate says `changed since index`: the source changed after the build;
  rebuild (`Atlas: UI` > Build), run the query again, then verify again.
- An answer that needed more than three `cite` ranges: the query you needed
  is missing from the toolkit - note the question shape (no real names) for
  the home side; it becomes a query.
