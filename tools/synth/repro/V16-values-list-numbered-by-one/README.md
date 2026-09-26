# V16-values-list-numbered-by-one - a copybook numbered by one in columns 1-6 whose VALUES list of six-digit codes runs over three lines is still filed as a compiler listing

Found by the second acceptance test of the re-parse batch (N3, probe p3), after the V01 fix. XQNAIC1 is V01's NAICS copybook with ISPF sequence numbers in columns 1-6 counting up by one (000001, 000002, ...). Lines 000004-000006 hold the member's own number and then a six-digit code: two numbers, the listing's line number counting up by one - the shape the V01 fix still took for a listing's numbered source lines. Truth: a copybook, expanded into XQPGM16. Tool: `listing`, and XQPGM16 `partial - COPY XQNAIC1 NOT FOUND` with the member on disk.

Fixed in LESSONS 253: a run numbered in columns 1-6 whose line before or after stands there with one number is the member's own sequence area, not a listing's (`classify._OWN_NUMBERED`, `classify.listing_hit`).
