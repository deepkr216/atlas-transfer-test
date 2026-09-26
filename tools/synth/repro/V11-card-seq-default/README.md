# V11-card-seq-default - a job running a PROC with its default sequential card dataset gets no card_seq_assumed row

Found by the verifier of ROADMAP re-parse item 29, confirmed by the acceptance test. KVSORT reads its cards from the sequential dataset `PROD.KV.CARDS.&CARDS`; the toolkit takes the card member whose name is the last qualifier and says so with a card_seq_assumed row. KVCJOB runs KVSORT twice: S1 with CARDS=KVJOBC gets the row; S2, with the PROC's default KVDEFC, got none - the row was written only when the job's symbols named another member than the PROC's default.

Fixed in LESSONS 250: every job step whose cards were taken so carries the row.
