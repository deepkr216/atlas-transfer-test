# F07-tag-copybook-no-rows - a copybook written for COPY REPLACING with a :TAG: in its names has no field rows, no 88s and no aliases

The PCB mask every IMS program copies with `REPLACING ==:PCB:== BY ==xxx==`. The expansion works (R07PGM's own POL-STATUS is at its offset), but the copybook member has no field rows and no 88s (`layout R07PCB` says NOT FOUND, `field :PCB:-STATUS` / `values` know nothing), and no field_alias row ties POL-STATUS back to the copybook, so `field` cannot cross programs and the member reads `parse: ok`.

Fixed by ROADMAP re-parse item 27 (LESSONS 214): a name with a `:TAG:` of pseudo-text is a data name, so R07PCB has its four field rows, its two 88s and a layout under the tagged names (`layout R07PCB` shows `:PCB:-STATUS`), and R07PGM has a field_alias row for each renamed item - POL-STATUS back to `:PCB:-STATUS` among them - so `field :PCB:-STATUS` says it is also known as POL-STATUS. The program's own view (check 3) was right before and is a control.
