           EXEC SQL DECLARE PRD.R05_TBL TABLE
           ( R05_KEY                        CHAR(12) NOT NULL,
             STATUS_CD                      CHAR(2) NOT NULL
           ) END-EXEC.
       01  DCLR05-TBL.
           10  R05-KEY                PIC X(12).
           10  STATUS-CD              PIC X(02).
