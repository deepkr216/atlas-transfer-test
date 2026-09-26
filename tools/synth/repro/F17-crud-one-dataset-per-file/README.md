# F17-crud-one-dataset-per-file - `crud --job` shows one dataset per file: a program two jobs run with different datasets is shown with the other job's

R17RPT reads RPTIN; R17JOBA gives it PROD.R17.DAILY and R17JOBB PROD.R17.WEEKLY. Truth: `crud --job R17JOBB` shows PROD.R17.WEEKLY. Tool: the first dataset found for the DD, whichever job - the design-document matrix for job B names job A's file. In the estate: `crud --job POLNIGHT` shows POLRPT01 reading `&&SORTED` (POLEXTRT's temporary) instead of PROD.POL.EXTRACT.SORTED.

Fixed on the re-parse batch, query side only (LESSONS 238): with `--job`, a file's datasets are the ones that job's own steps give its DD - PROD.R17.WEEKLY for R17JOBB, PROD.POL.EXTRACT.SORTED for POLNIGHT. Without `--job`, `crud R17RPT` lists both, each with its job: `PROD.R17.DAILY (job R17JOBA)`, `PROD.R17.WEEKLY (job R17JOBB)`.
