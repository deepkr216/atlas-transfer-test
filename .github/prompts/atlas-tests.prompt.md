---
mode: 'ask'
description: 'Atlas T1: test conditions and test data for a program, from its conditions and byte layout'
---
Build the test-condition inventory and the test records using ONLY
[conditions](../../work/conditions.md) (task "Atlas: conditions"), [layout](../../work/layout.md)
(task "Atlas: layout" on the input record copybook) and [the pack](../../work/pack.md) (task
"Atlas: pack" on the program).

Under test: ${input:scope:The change or requirement being tested (expected results come from this, not from the program)}

Write:

## CONDITIONS
From `conditions`, one row per test in the program: field, operator, literal or 88 name with its
values, line (cited). Then the classes:
- every 88 value is a positive class; every THRU range gives low / high / mid;
- one value outside all 88s under a field is the mandatory negative;
- every IF gets true and false (implicit false when there is no ELSE);
- every EVALUATE gets each WHEN plus OTHER;
- every sort INCLUDE / OMIT gets a record on each side.

## TEST RECORDS
Built to the byte layout from `layout`: offset, length, PIC and usage per field (COMP-3 packed,
correct lengths, RDW if VB). Give each record as a table of field -> value with the byte
position, one record per condition class. The parser's numbers, never yours.

## EXPECTED RESULTS
From the requirement in the scope above. Where the requirement is silent, write
`EXPECTED: UNKNOWN - specification needed`; do not read the expected result off the program.

## UNRESOLVED IN SCOPE
## NOT SEARCHED
## HUMAN MUST VERIFY
Offsets and lengths, COMP-3 sign nibbles, any record that crosses to a non-mainframe system.

Rules: use only the three reports; what they lack is UNKNOWN. Every condition carries
`[[MEMBER line "token"]]` with the token copied verbatim; never cite a comment line; a
condition you cannot cite goes under `## UNVERIFIED` or is not written.
