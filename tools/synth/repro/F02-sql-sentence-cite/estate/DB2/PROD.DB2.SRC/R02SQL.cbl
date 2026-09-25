       IDENTIFICATION DIVISION.
       PROGRAM-ID. R02SQL.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-STATUS        PIC X(02).
       01  WS-KEY           PIC X(12).
       01  WS-AMT           PIC S9(09)V99 COMP-3.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'AC' TO WS-STATUS
           EXEC SQL
               SELECT PREMIUM_AMT INTO :WS-AMT
                 FROM PRD.POLICY_TBL WHERE POLICY_NO = :WS-KEY
           END-EXEC
           EXEC SQL
               UPDATE PRD.POLICY_TBL SET STATUS_CD = :WS-STATUS
                WHERE POLICY_NO = :WS-KEY
           END-EXEC
           GOBACK.
