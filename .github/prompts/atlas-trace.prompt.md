---
mode: 'ask'
description: 'Atlas A3: trace an error / status code from where it is set to where it is shown'
---
Trace this code using ONLY [the literal report](../../work/literal.md) (task
"Atlas: literal / error code"). It already lists where the literal is defined (VALUE / 88, with
the message text beside it), set (MOVE), tested (IF / WHEN), displayed, and where the value flows
afterwards - positionally through `CALL ... USING` or a LINK's COMMAREA into the callee's
LINKAGE, back to callers, and through file records to other programs at the same bytes.

Code: ${input:code:The error / status code, e.g. E001}

Write:

## FACTS
The chain in order: defined -> set (which condition, which paragraph) -> carried (call
position / record bytes) -> tested -> displayed / written, one row per hop, each cited. Give the
message text shown to the user where the report has it.

## INFERENCE
What situation produces the code, derived only from the facts; which program is the origin.

## UNRESOLVED IN SCOPE
Routes the report could not follow, copied verbatim: DB2 / IMS / MQ hops, group-level MOVEs,
unresolved CALLs.

## NOT SEARCHED

If the report says the literal is not found: the code may be built (STRING, arithmetic), read
from a table, or spelled differently. Say so - do not guess a program.

Rules: use only the report; what it lacks is UNKNOWN. Every fact carries
`[[MEMBER line "token"]]` with the token copied verbatim; never paraphrase; never cite a comment
line. Uncitable claims go under `## UNVERIFIED` or are not written.
