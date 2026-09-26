# F01-dli-two-calls-one-sentence - several CBLTDLI calls in one sentence: only the first is indexed, at the sentence's first line

R01IMS has a GU on line 16 and a CHKP on line 18, both in the sentence that starts with the MOVE on line 15 (no period until GOBACK). Truth: two dli_call rows at lines 16 and 18. Tool: one row (GU) at line 15 - the CHKP is lost and `program R01IMS` cites the MOVE line.

The same shape in the estate: POLIMS01 (6 calls, 4 indexed).

Fixed by ROADMAP re-parse item 26 (LESSONS 211): every CBLTDLI / AIBTDLI / PLITDLI call of a sentence is a dli_call row at its own line with its own USING list, so R01IMS has the GU at line 16 and the CHKP at line 18; an ordinary CALL in the same sentence is still a call edge, and several MQ calls of a sentence keep their own lines.
