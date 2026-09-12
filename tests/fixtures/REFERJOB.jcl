//REFERJOB JOB (ACCT),'REFERBACK TEST',CLASS=A,MSGCLASS=X
//*--------------------------------------------------------------------
//* Four shapes the original fixtures did not have:
//*   1. an INSTREAM PROC (// PROC ... // PEND) executed by a job step
//*   2. control cards kept in a PARMLIB MEMBER, not instream (DD *)
//*   3. a &&TEMP dataset that exists only between two steps of this job
//*   4. DSN=*.STEP.DD referbacks, inside the PROC and across it
//*--------------------------------------------------------------------
//SRTPROC  PROC HLQ=PROD
//SORT1    EXEC PGM=SORT
//SYSOUT   DD SYSOUT=*
//SORTIN   DD DSN=&HLQ..CLM.EXTRACT(0),DISP=SHR
//SORTOUT  DD DSN=&&SORTED,DISP=(NEW,PASS),UNIT=SYSDA,
//            SPACE=(CYL,(10,5),RLSE)
//SYSIN    DD DSN=&HLQ..PARMLIB(SRTCLM),DISP=SHR
//RPT1     EXEC PGM=IKJEFT01,DYNAMNBR=20
//SYSTSPRT DD SYSOUT=*
//CLMIN    DD DSN=*.SORT1.SORTOUT,DISP=(OLD,DELETE)
//CLMRPT   DD DSN=&HLQ..CLM.REPORT(+1),DISP=(NEW,CATLG,DELETE),
//            UNIT=SYSDA,SPACE=(CYL,(5,1),RLSE)
//SYSTSIN  DD DSN=&HLQ..PARMLIB(RUNCLM),DISP=SHR
//         PEND
//*
//STEP010  EXEC SRTPROC,HLQ=TEST
//*
//STEP020  EXEC PGM=IEBGENER
//SYSPRINT DD SYSOUT=*
//SYSUT1   DD DSN=*.STEP010.RPT1.CLMRPT,DISP=SHR
//SYSUT2   DD DSN=TEST.CLM.REPORT.COPY,DISP=(NEW,CATLG,DELETE),
//            UNIT=SYSDA,SPACE=(CYL,(5,1),RLSE)
//SYSIN    DD DUMMY
//
