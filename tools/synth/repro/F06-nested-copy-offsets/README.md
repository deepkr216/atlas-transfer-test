# F06-nested-copy-offsets - a copybook that COPYs another: the copybook's own layout ignores the nested bytes (the program's view is right)

R06OUTER copies R06INNER (25 bytes) inside its ADDRESS group, so R06-AFTER starts at byte 35. Truth: offset 35 in `layout R06OUTER` and `field R06-AFTER`. Tool: offset 10 (the nested bytes are not counted) while the program's own view (`layout R06-RECORD --program R06PGM`) says 35. Test data and sort-card overlaps built from `layout` are wrong for every field after a nested COPY.
