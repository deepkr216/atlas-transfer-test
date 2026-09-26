# V08-nested-end-call - a scope terminator of a nested statement of the same verb ends the outer statement's phrase

Found by the verifier of ROADMAP re-parse item 26, confirmed by the acceptance test. 2000-CALLS: `CALL 'KVSUB' USING WS-DOC ON EXCEPTION CALL 'KVABND' USING WS-DOC END-CALL GO TO 9000-ERR END-CALL.` The first END-CALL is the inner CALL's (it took no phrase), but it closed the outer CALL's ON EXCEPTION, and the GO TO read as one that always runs. Truth: 2000-CALLS falls through to 2500-BRANCH when KVSUB is called. Tool: no fall-through edge.

Fixed in LESSONS 252: a scope terminator right after a statement of its own verb that opened no scope ends that statement only.
