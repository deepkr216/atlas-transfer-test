---
mode: 'ask'
description: 'Atlas H1: transition document for a release - Application Operations and Contact Center - from the reports and the release facts'
---
Write the transition document for a release using ONLY the hand-over pack and the release facts
file attached here: [handover pack](../../work/handover.md), [release facts](../../work/release-facts.md).
The pack is written by one task, `Atlas: handover pack` (give the system holding the changed copies,
e.g. GC-DEV, and the documents: the plan, the stories, the QA workbooks). It holds, in order: the
release list, a `diff` per changed member, the `walk` of each changed program's new copy, each
changed copybook's users and layout, `crud` and `program` for the jobs, and the documents' sections.
`work/release-facts.md` is created beside it from `templates/release-facts.md`: fill in what no file
holds - dates, names, contacts, escalation, what agents may say - before running this prompt.
(The same reports can also be gathered one task at a time and attached with `#file:`.)

Release: ${input:release:What the release is for, in one or two sentences}

Write the document in plain working English for people who will run and support this, not for the
developers who built it: short sentences, one fact each, names of jobs, programs, screens, messages
and fields exactly as the reports spell them, no filler and no advice that the reports do not
support. Where a fact is not in any attached file, write UNKNOWN and list it under HUMAN MUST VERIFY
rather than writing around it.

# Part 1 - for everyone

## WHAT THIS RELEASE DOES
Three to six sentences from the release facts, the plan and the stories (`doc` sections, cited as
`[[DOCNAME n "token"]]`): the business change, who it affects, when it goes live.

## CHANGED COMPONENTS
The table from `release`: member, kind, production copy, changed copy, lines changed. Add "what
changed" from each `diff` report's "What changed" section (paragraphs added, calls added, fields added
or shifted). When no previous copy exists in the index, list the members from the release facts and
mark the "what changed" column FROM THE PLAN, cited to the plan's section.

# Part 2 - for Application Operations

## JOBS AND SCHEDULES
From `crud` and `job`: every job that runs a changed program, its PROC and steps, the datasets,
tables and segments it creates, reads, updates or deletes, and which of those the release alters.
Schedule or dependency changes only from the release facts or the plan, cited.

## WHAT CHANGED IN EACH PROGRAM
One short section per changed program, from `walk` and `diff`: the paragraphs with changed lines and
what each does now, cited on the NEW copy (`[[SYSTEM-TEST/PGM line "token"]]`) and, when a previous
copy exists, what it did before, cited on the OLD copy. Never describe a change the diff does not show.

## FILES AND RECORD LAYOUTS
Per changed copybook, from `diff` and `layout`: fields added, fields whose picture or length changed,
fields that shifted and by how many bytes, and every other program that expands the copybook (from
`pack`) - each is affected even when its source did not change. Say which files already written
with the old layout are in play (HUMAN MUST VERIFY if the reports do not say).

## MESSAGES, ABENDS, RESTART
Error messages and codes the changed paragraphs can produce (from `walk` / `diff` lines), the
condition that produces each, and the restart / rerun facts from `crud` and `job` (checkpoints,
GDG generations, what to rerun from where). Escalation contacts from the release facts only.

## MONITORING FOR THE FIRST RUNS
What to look at after go-live, derived only from the facts above: the jobs to watch, the datasets
whose record counts should move, the messages that would indicate the new logic did not take.

# Part 3 - for the Contact Center

## WHAT CUSTOMERS AND AGENTS WILL NOTICE
From the stories, the plan and the screen / message reports: which screens, letters, values and
messages change and how, in the words a caller would use. Cited. If the stories do not say what a
customer will see, write UNKNOWN.

## NEW OR CHANGED VALUES AND CODES
Every code or value the release adds or changes (from `values`, `literal`, `diff`), what it means,
and where an agent will see it.

## WHAT TO TELL A CALLER
For each noticeable change: the situation, what the agent can confirm on screen, what to say, and
when to hand off - built only from the facts above and the release facts. No invented procedures:
where the release facts give no instruction, write "instruction needed" under HUMAN MUST VERIFY.

## KNOWN ISSUES AND OPEN TEST CASES
From the QA workbooks (`doc` sections; a tab's rows read `row 12: TC-GEN-01 | ... | PASS`): every
test case not marked passed, and every changed paragraph no test covers.

# Part 4 - evidence and gaps

## TEST EVIDENCE
The test cases and results from the QA workbooks, cited section by section; screenshots cited as the
OCR section whose heading names the tab (`[[WORKBOOK 1001 "token"]]`).

## UNRESOLVED IN SCOPE
## NOT SEARCHED
## HUMAN MUST VERIFY
Always: the promote list against the change-management record, the QA sign-off, the go-live date and
contacts as given in the release facts, offsets in files already written with the OLD layout, every
job the reports did not cover, and every "instruction needed" line from Part 3.

Rules: use only the attached reports and the release facts; what they lack is UNKNOWN. Every fact
carries `[[MEMBER line "token"]]` or `[[DOCNAME n "token"]]` with the token copied verbatim (never
from a comment line); a fact from the release facts file is cited by its path and line,
`[[work/release-facts.md 7 "token"]]`, which the gate checks like any other; the old and the new
copy of a member are cited with their own system prefix. Uncitable claims go under `## UNVERIFIED`
or are not written.
