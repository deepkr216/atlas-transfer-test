# F13-interfaces-ftp-twice - `interfaces` lists every FTP put twice

One FTP step, one put. Truth: one ftp interface row. Tool: two rows for the same put (one named by the dataset, one by the 'external interface via FTP' sentence), so `interfaces --system` doubles every FTP count.

Fixed on the re-parse batch, query side only (LESSONS 237): one row per transfer - the dataset's, with the peer the step names (`| ftp | out | PEERHOST | STG.R13.EXTRACT | R13FTP FTP010 |`). The step's own row is kept only for a transfer no dataset row holds (a USS path) or a step whose cards name none.
