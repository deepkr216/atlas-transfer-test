# V09-go-to-separators - GO TO ... DEPENDING ON with a comma or a semicolon, and GO without TO, give no edge

Reported by the verifier of ROADMAP re-parse item 26 (minor, older than the batch). 2500-BRANCH: `GO TO 3100-A, 3200-B DEPENDING ON WS-IX;` then `GO 9000-ERR.` COBOL takes a comma or a semicolon for a space, and TO is optional after GO. Truth: two goto_depending edges and a goto edge to 9000-ERR. Tool: none, so `dead` listed 3100-A and 3200-B.

Fixed in LESSONS 252: the separators and the optional TO are read as COBOL writes them.
