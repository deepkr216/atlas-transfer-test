---
mode: 'ask'
description: 'Atlas H1: transition / handover document for a release, from the diffs and the QA evidence'
---
Write the transition document for the support team using ONLY the reports attached. Run the tasks
first: `Atlas: release contents`, then `Atlas: diff` for each changed member (keep each under its own
name in `work/`), `Atlas: walk` for each changed program, `Atlas: layout` and `Atlas: pack (kind:
copybook)` for each changed copybook, `Atlas: crud` for the jobs that run them, and `Atlas: document
sections about a term` on the QA workbook for its test evidence. Attach here what exists:
[release](../../work/release.md), [diff](../../work/diff.md), [walk](../../work/walk.md),
[layout](../../work/layout.md), [pack](../../work/pack.md), [crud](../../work/crud.md),
[doc](../../work/doc.md). More than one of a kind: keep the earlier one under another name in
`work/` and attach it with `#file:`.

Release: ${input:release:What the release is for, in one or two sentences}

Write:

## SUMMARY
What the release does for the business, in three sentences, each cited or marked INFERENCE.

## CHANGED COMPONENTS
The table from `release`: member, kind, production copy, changed copy, lines changed. Add a column
"what changed" from each `diff` report's "What changed" section (paragraphs added, calls added,
fields added or shifted).

## WHAT EACH PROGRAM DOES NOW
One section per changed program, from `walk` and `diff`: the paragraphs with changed lines, what
each does now (cited on the NEW side as `[[SYSTEM-TEST/PGM line "token"]]`), and what it did
before (cited on the OLD side). Never describe a change the diff does not show.

## DATA AND LAYOUT CHANGES
Per changed copybook, from `diff` and `layout`: fields added, fields whose picture or length changed,
the fields that shifted and by how many bytes, and every other program that expands the copybook
(from `pack`) - each of those is affected even when its source did not change.

## JOBS AND OPERATIONS
From `crud`: which jobs run the changed programs, which datasets / tables / segments they create,
read, update or delete, and which of those cells the release alters.

## TEST EVIDENCE
From `doc` on the QA workbooks (`doc --list QA` names them; a workbook's tabs are its sections and
its rows the text - `row 12: TC-GEN-01 | ... | PASS`): the test cases, their results, and the
screenshots (their OCR text is a section 1001+ whose heading names the tab; cite as
`[[WORKBOOK n "token"]]`). Say which changed paragraph each test covers and which changed
paragraph no test covers.

## SUPPORT NOTES
Error messages and codes the changed paragraphs can produce (from `walk` / `diff` lines), restart and
rerun facts from `crud` and `job`, and the fields a support analyst will see move in a file.

## UNRESOLVED IN SCOPE
## NOT SEARCHED
## HUMAN MUST VERIFY
Always: the promote list against the change-management record, the QA sign-off, offsets in files
already written with the OLD layout, and every job the reports did not cover.

Rules: use only the attached reports; what they lack is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` with the token copied verbatim (never a comment line); the old and the new
copy are cited with their own system prefix. Uncitable claims go under `## UNVERIFIED` or are not
written.
