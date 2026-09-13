---
mode: 'ask'
description: 'Atlas A4: where a field is populated / end-to-end trace of a value'
---
Find where this field gets its value, using ONLY [the field report](../../work/field.md) (task
"Atlas: field") and [the pack](../../work/pack.md) (task "Atlas: pack" on the main writing
program). Writers are ranked first in the report: MOVE / COMPUTE targets, `SELECT ... INTO`,
`FETCH ... INTO`, `READ ... INTO`, `CALL ... USING` by reference, group-level writes, and DL/I
calls whose I/O area holds the field (database and PROCOPT already resolved through the PSB).

Field: ${input:field:The field (copybook name or the program's REPLACING name)}

Write:

## FACTS
1. Every write site: program, paragraph, statement, source of the value (another field, a
   column, a file record, a call argument), each cited.
2. Every read site that carries the value onward (WRITE FROM, CALL USING, INSERT/UPDATE).
3. The datasets / tables / segments between them, with producer and consumer.

## INFERENCE
The end-to-end path of the value, hop by hop; where it originates; where it stops being traceable.

## UNRESOLVED IN SCOPE
Copied verbatim (group-level MOVEs that may touch it, unresolved CALLs, DB2/IMS hops).

## NOT SEARCHED

Rules: use only the two reports; what they lack is UNKNOWN. Every hop is a fact with a citation
`[[MEMBER line "token"]]` (token copied verbatim, never a comment line) or it is a gap. Uncitable
claims go under `## UNVERIFIED` or are not written.
