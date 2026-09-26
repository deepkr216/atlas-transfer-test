# V10-split-copy - a COPY with the copybook's name on the next line is silently not expanded

Found by the verifier of ROADMAP re-parse item 27, confirmed by the acceptance test. KVSPLIT writes `COPY` on one line and `KVBOOK.` on the next - a COBOL statement may break between any two words. The expander looked for the name on the keyword's own line, so the two lines stayed code: `parse: ok`, no copybook listed, no KV-KEY.

Fixed in LESSONS 251: a COPY keyword closing its line takes its text-name from the next code line (`expand.copy_over_lines`).
