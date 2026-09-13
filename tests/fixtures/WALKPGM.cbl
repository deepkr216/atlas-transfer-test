       IDENTIFICATION DIVISION.
       PROGRAM-ID.    WALKPGM.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT CLAIM-FILE ASSIGN TO CLMIN
               ORGANIZATION IS SEQUENTIAL.
       DATA DIVISION.
       FILE SECTION.
       FD  CLAIM-FILE.
       01  CLAIM-RECORD.
           COPY WALKREC.
       WORKING-STORAGE SECTION.
       01  WS-FLAGS.
           05  WS-EOF-FLAG          PIC X(01) VALUE 'N'.
               88  WS-EOF           VALUE 'Y'.
           05  WS-UNUSED-FLAG       PIC X(01) VALUE 'N'.
       01  WS-COUNTERS.
           05  WS-READ-COUNT        PIC S9(07) COMP-3 VALUE 0.
           05  WS-NEVER-COUNT       PIC S9(07) COMP-3 VALUE 0.
       01  WS-RATE-PARM.
           05  WS-RATE-AMT          PIC S9(09)V99 COMP-3.
       LINKAGE SECTION.
       01  LS-ENTRY-PARM            PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INIT.
           PERFORM 2000-PROCESS THRU 2000-EXIT
               UNTIL WS-EOF.
           PERFORM 3000-WRAP.
           STOP RUN.
       1000-INIT.
           OPEN INPUT CLAIM-FILE.
           MOVE 0 TO WS-READ-COUNT.
       1500-UNUSED.
           ADD 1 TO WS-NEVER-COUNT.
       2000-PROCESS.
           READ CLAIM-FILE
               AT END SET WS-EOF TO TRUE.
           IF WS-EOF
               GO TO 2000-EXIT.
           PERFORM 2100-RATE.
       2050-ADJ.
           COMPUTE WS-RATE-AMT = WS-RATE-AMT * WR-CLAIM-AMT.
       2000-EXIT.
           EXIT.
       2100-RATE.
           CALL 'RATECALC' USING WS-RATE-PARM WR-POLICY-NO.
           ADD 1 TO WS-READ-COUNT.
       3000-WRAP.
           PERFORM 8000-COMMON.
           PERFORM 9000-ERRORS.
           CLOSE CLAIM-FILE.
           COPY WALKPROC.
       7000-ENTRY.
           ENTRY 'WALKENT' USING LS-ENTRY-PARM.
           DISPLAY 'ENTERED ' LS-ENTRY-PARM.
           GOBACK.
       9000-ERRORS SECTION.
       9010-SHOW.
           DISPLAY 'READ ' WS-READ-COUNT.
       9020-DONE.
           DISPLAY 'DONE'.
