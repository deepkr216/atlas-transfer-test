# F01-dli-two-calls-one-sentence - several CBLTDLI calls in one sentence: only the first is indexed, at the sentence's first line

R01IMS has a GU on line 16 and a CHKP on line 18, both in the sentence that starts with the MOVE on line 15 (no period until GOBACK). Truth: two dli_call rows at lines 16 and 18. Tool: one row (GU) at line 15 - the CHKP is lost and `program R01IMS` cites the MOVE line.

The same shape in the estate: POLIMS01 (6 calls, 4 indexed).
