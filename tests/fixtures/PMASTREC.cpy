000100*================================================================*SAMP0001
000200* POLICY MASTER RECORD LAYOUT                                   *SAMP0002
000300*================================================================*SAMP0003
000400 05  PM-POLICY-NO            PIC X(12).                          SAMP0004
000500 05  PM-POLICY-STATUS        PIC X(02).                          SAMP0005
000600     88  PM-ACTIVE           VALUE 'AC'.                         SAMP0006
000700     88  PM-LAPSED           VALUE 'LP'.                         SAMP0007
000800     88  PM-CANCELLED        VALUE 'CN' 'CX' 'CR'.               SAMP0008
000900     88  PM-INFORCE          VALUE 'AC' THRU 'AZ'.               SAMP0009
001000 05  PM-EFFECTIVE-DT         PIC 9(08).                          SAMP0010
001100 05  PM-PREMIUM-AMT          PIC S9(09)V99  COMP-3.              SAMP0011
001200 05  PM-COVERAGE-CNT         PIC S9(04)     COMP.                SAMP0012
001300 05  PM-INSURED-NAME.                                            SAMP0013
001400     10  PM-LAST-NAME        PIC X(20).                          SAMP0014
001500     10  PM-FIRST-NAME       PIC X(15).                          SAMP0015
001600     10  PM-MID-INIT         PIC X(01).                          SAMP0016
001700 05  PM-ADDRESS-BLOCK.                                           SAMP0017
001800     10  PM-ADDR-LINE        PIC X(30) OCCURS 3 TIMES.           SAMP0018
001900     10  PM-POSTAL-CD        PIC X(09).                          SAMP0019
002000 05  PM-AGENT-DATA.                                              SAMP0020
002100     10  PM-AGENT-ID         PIC X(06).                          SAMP0021
002200     10  PM-COMMISSION-PCT   PIC S9(03)V9(04) COMP-3.            SAMP0022
002300 05  PM-LEGACY-AREA          REDEFINES PM-AGENT-DATA.            SAMP0023
002400     10  PM-OLD-AGENT-KEY    PIC X(12).                          SAMP0024
002500 05  PM-COV-COUNT            PIC S9(03)     COMP-3.              SAMP0025
002600 05  PM-COVERAGE-TBL         OCCURS 1 TO 20 TIMES                SAMP0026
002700                             DEPENDING ON PM-COV-COUNT.          SAMP0027
002800     10  PM-COV-CODE         PIC X(04).                          SAMP0028
002900     10  PM-COV-LIMIT        PIC S9(11)V99  COMP-3.              SAMP0029
003000 05  PM-FILLER               PIC X(25).                          SAMP0030
