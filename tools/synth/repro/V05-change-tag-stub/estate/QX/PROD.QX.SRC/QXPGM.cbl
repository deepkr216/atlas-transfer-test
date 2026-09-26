       IDENTIFICATION DIVISION.
       PROGRAM-ID. QXPGM.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-OUT              PIC X(5).
       01  WS-REC.
           COPY QXTAG.
       PROCEDURE DIVISION.
           MOVE QX-NAME TO WS-OUT.
           GOBACK.
