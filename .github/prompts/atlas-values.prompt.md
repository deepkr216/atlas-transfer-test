---
mode: 'ask'
description: 'Atlas A7: value-domain change - a new code value, or codes merged'
---
Inventory every place a value of this field is assumed, using ONLY the reports written by the
task "Atlas: value domain": [values](../../work/values.md), [pair](../../work/pair.md),
[messages](../../work/messages.md), [field](../../work/field.md).

Change: ${input:change:e.g. GENDER gets value N; relationship 3 and 4 both become 4; 5 is a special case}

Write, per system (department) that has a copy of the field:

## FACTS
1. **Value table** from `values`: every value the code knows - documented by an 88, set, tested
   (including tests through 88 names), used in SQL predicates / SET / INSERT, compared by sort
   INCLUDE / OMIT cards. Mark the two lines that matter most: **USED BUT NOT DOCUMENTED** and
   **documented but never used**.
2. **Rule list** from `pair`: every statement where the field and its partner (or their 88s)
   are used together - each row is a validation or derivation the change touches.
3. **Message list** from `messages`: every message naming the old rule, with the field it is
   moved to.
4. **Reader list** from `field`: copies per system with offset and length, programs, jobs,
   datasets written and who reads them, callers.

## INFERENCE
For each rule: keep / rewrite / delete, and why. For each message: the new text. For each
reader outside the mainframe: a contract note. Whether any byte moves (usually not - the
file contracts survive, the readers that test old values do not).

## UNRESOLVED IN SCOPE
## NOT SEARCHED
## HUMAN MUST VERIFY
Values present in data but absent from code (compare with the tables' distinct values), screen
picklists and validation done outside COBOL, DB2 CHECK constraints and lookup tables,
consumers not declared in the manifest, programs the index could not expand.

## TEST CONDITIONS
Straight from the value table: every old value, every new value, every pair combination the
rules mention, and one value outside all of them.

Rules: use only the four reports; what they lack is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` (token copied verbatim, never a comment line). Uncitable claims go
under `## UNVERIFIED` or are not written.
