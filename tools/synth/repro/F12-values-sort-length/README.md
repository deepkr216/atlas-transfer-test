# F12-values-sort-length - `values` reports the sort card's LENGTH token as an observed value of the field

The INCLUDE card compares bytes 13-14 (R12-STATUS) with C'AC'. Truth: the value AC, documented by R12-ACTIVE. Tool: also a value `2` - the card's length token - flagged **USED BUT NOT DOCUMENTED BY ANY 88-LEVEL**, the line the value-domain playbook tells the reader to chase.

Fixed on the re-parse batch, query side only (LESSONS 235): `values` reads each condition of the step's deck (`query.sort_card_compares`) and keeps the constant after the relational operator of a condition on the field's bytes - AC here; a position or a length is never a value. The check counts the row `| 2 | (none) |` and the flag, 1 each being the symptom and 0 the truth: looking only for the symptom, it printed OTHER once the symptom was gone.
