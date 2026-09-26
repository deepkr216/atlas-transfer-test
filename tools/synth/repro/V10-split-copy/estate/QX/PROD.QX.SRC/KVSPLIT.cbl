       IDENTIFICATION DIVISION.
       PROGRAM-ID. KVSPLIT.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-REC.
           COPY
               KVBOOK.
       01  WS-B            PIC X(5).
       01  WS-MSG          PIC X(20) VALUE 'SEE COPY'.
       PROCEDURE DIVISION.
           MOVE KV-KEY TO WS-B.
           GOBACK.
