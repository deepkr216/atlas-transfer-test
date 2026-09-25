       IDENTIFICATION DIVISION.
       PROGRAM-ID. R05INC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           EXEC SQL
               INCLUDE SQLCA
           END-EXEC.
           EXEC SQL
               INCLUDE R05TBL
           END-EXEC.
       PROCEDURE DIVISION.
       0000-MAIN.
           MOVE 'AC' TO STATUS-CD
           EXEC SQL
               SELECT R05_KEY INTO :R05-KEY FROM PRD.R05_TBL
                WHERE STATUS_CD = :STATUS-CD
           END-EXEC
           GOBACK.
