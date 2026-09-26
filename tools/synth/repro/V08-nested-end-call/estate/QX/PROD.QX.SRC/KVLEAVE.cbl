       IDENTIFICATION DIVISION.
       PROGRAM-ID. KVLEAVE.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-DOC          PIC X(80).
       01  WS-IX           PIC 9.
       PROCEDURE DIVISION.
       1000-MAIN.
           XML PARSE WS-DOC PROCESSING PROCEDURE 3000-HANDLER
               ON EXCEPTION GO TO 9000-ERR
           END-XML.
       2000-CALLS.
           CALL 'KVSUB' USING WS-DOC ON EXCEPTION
               CALL 'KVABND' USING WS-DOC END-CALL
               GO TO 9000-ERR
           END-CALL.
       2500-BRANCH.
           GO TO 3100-A, 3200-B DEPENDING ON WS-IX;
           GO 9000-ERR.
       3000-HANDLER.
           EXIT.
       3100-A.
           EXIT.
       3200-B.
           EXIT.
       9000-ERR.
           GOBACK.
