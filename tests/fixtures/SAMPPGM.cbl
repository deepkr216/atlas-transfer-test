000100 IDENTIFICATION DIVISION.                                         SAMP0001
000200 PROGRAM-ID.    SAMPPGM.                                          SAMP0002
000300 ENVIRONMENT DIVISION.                                            SAMP0003
000400 INPUT-OUTPUT SECTION.                                            SAMP0004
000500 FILE-CONTROL.                                                    SAMP0005
000600     SELECT POLICY-MASTER ASSIGN TO POLMAST                       SAMP0006
000700         ORGANIZATION IS INDEXED                                  SAMP0007
000800         ACCESS MODE  IS RANDOM                                   SAMP0008
000900         RECORD KEY   IS PM-POLICY-NO.                            SAMP0009
001000 DATA DIVISION.                                                   SAMP0010
001100 FILE SECTION.                                                    SAMP0011
001200 FD  POLICY-MASTER.                                               SAMP0012
001300 01  POLICY-RECORD.                                               SAMP0013
001400     COPY PMASTREC.                                               SAMP0014
001500 WORKING-STORAGE SECTION.                                         SAMP0015
001600 01  WS-SUB-PROGRAM       PIC X(08) VALUE 'RATECALC'.             SAMP0016
001700 01  WS-POLICY-STATUS     PIC X(02).                              SAMP0017
001800     88  WS-STAT-ACTIVE   VALUE 'AC'.                             SAMP0018
001900     88  WS-STAT-LAPSED   VALUE 'LP'.                             SAMP0019
002000     88  WS-STAT-CANCEL   VALUE 'CN' 'CX'.                        SAMP0020
002100 01  WS-PREMIUM-AMT       PIC S9(09)V99 COMP-3.                   SAMP0021
002200 01  WS-LONG-MESSAGE      PIC X(40) VALUE 'POLICY NOT FOUND ON MA SAMP0022
002300-    'STER FILE'.                                                 SAMP0023
002400 PROCEDURE DIVISION.                                              SAMP0024
002500 0000-MAIN-CONTROL.                                               SAMP0025
002600     PERFORM 1000-INIT THRU 1000-INIT-EXIT.                       SAMP0026
002700     CALL 'VALIDATE' USING POLICY-RECORD WS-POLICY-STATUS.        SAMP0027
002800*    CALL 'OLDRATER' USING WS-PREMIUM-AMT.                        SAMP0028
002900     CALL WS-SUB-PROGRAM USING WS-PREMIUM-AMT.                    SAMP0029
003000     EXEC SQL                                                     SAMP0030
003100         SELECT PREMIUM_AMT, STATUS_CD                            SAMP0031
003200           INTO :WS-PREMIUM-AMT, :WS-POLICY-STATUS                SAMP0032
003300           FROM POLICY_TBL                                        SAMP0033
003400          WHERE POLICY_NO = :PM-POLICY-NO                         SAMP0034
003500     END-EXEC.                                                    SAMP0035
003600     MOVE 'ADJUSTER' TO WS-SUB-PROGRAM.                           SAMP0036
003700     CALL WS-SUB-PROGRAM USING WS-PREMIUM-AMT.                    SAMP0037
003800     GOBACK.                                                      SAMP0038
003900 1000-INIT.                                                       SAMP0039
004000     OPEN INPUT POLICY-MASTER.                                    SAMP0040
004100 1000-INIT-EXIT.                                                  SAMP0041
004200     EXIT.                                                        SAMP0042
