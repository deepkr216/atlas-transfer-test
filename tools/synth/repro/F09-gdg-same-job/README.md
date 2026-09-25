# F09-gdg-same-job - a (+1) generation created by an earlier step and named (+1) again in a later step of the same job is recorded as a second writer

JES resolves relative generation numbers once per job: STEP010 creates PROD.R09.EXTRACT(+1) and STEP020's SORTIN (+1) reads that same new generation. Truth: STEP020 reads it (SORTIN, a read-side DD). Tool: `output [gdg_relative]` - the sort step is a second writer of the GDG and `dataset` lists it among the producers.
