       IDENTIFICATION DIVISION.
       PROGRAM-ID. XQPGM16.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-OUT              PIC X(6).
       01  WS-REC.
           COPY XQNAIC1.
       PROCEDURE DIVISION.
           IF XQ-BUILDING
               MOVE XQ-NAICS1-CD TO WS-OUT
           END-IF.
           GOBACK.
