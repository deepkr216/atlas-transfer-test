# F07-tag-copybook-no-rows - a copybook written for COPY REPLACING with a :TAG: in its names has no field rows, no 88s and no aliases

The PCB mask every IMS program copies with `REPLACING ==:PCB:== BY ==xxx==`. The expansion works (R07PGM's own POL-STATUS is at its offset), but the copybook member has no field rows and no 88s (`layout R07PCB` says NOT FOUND, `field :PCB:-STATUS` / `values` know nothing), and no field_alias row ties POL-STATUS back to the copybook, so `field` cannot cross programs and the member reads `parse: ok`.
