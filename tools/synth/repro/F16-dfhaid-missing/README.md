# F16-dfhaid-missing - COPY DFHAID (a CICS-supplied copybook no shop keeps in its own COPYLIB) makes every CICS program partial

DFHAID, DFHBMSCA and DFHEIBLK come from the CICS SDFHCOB library, as SQLCA comes from DB2; SQLCA is excluded by name, these are not. Every CICS program is 'parsed only in part' and coverage sends the owner to fetch a library the estate never holds.
