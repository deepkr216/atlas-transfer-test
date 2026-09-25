# F10-proc-card-member-symbolic - a PROC's SYSIN DD DSN=LIB(&CARDS): the effective step takes the card member from the PROC default, not the job's CARDS= override

R10JOB executes the shared sort PROC with CARDS=R10CARD. Truth: the effective step's SYSIN is PROD.CMN.PARMLIB(R10CARD) - the DSN is resolved that way - and R10CARD's SORT / INCLUDE byte positions belong to the step. Tool: the card member stays the PROC default DEFCARD, so the step has no cards, no byte positions, and coverage lists DEFCARD as a control-card member 'referenced by JCL but NOT indexed'.
