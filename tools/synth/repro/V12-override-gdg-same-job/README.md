# V12-override-gdg-same-job - a job step's //PS.DD override naming a (+1) an earlier PROC step writes stays output [gdg_relative]

Reported by the verifier of ROADMAP re-parse item 29. KVGJOB runs KVGDG, whose PS1 writes PROD.KV.EXTRACT(+1); the job overrides PS2's KVIN with `//PS2.KVIN DD DSN=PROD.KV.EXTRACT(+1),DISP=SHR` - the same new generation, read (JES resolves relative generations once per job). The effective step S1.PS2 read it (`input [gdg_same_job]`, the second check, a control); the override row on the job's own step S1 - where the job codes it, and what `dataset` lists - still said `output [gdg_relative]`, a second writer.

Fixed in LESSONS 250: the override row follows the effective DD built from it.
