# atlas-transfer-test

A transfer and environment check: one small Python script that verifies a
file arrived intact and that the machine has what a stdlib-only Python
toolkit needs (Python 3.9+, SQLite FTS5, Tkinter, write access). It contains
no data of any kind, makes no network calls and installs nothing.

Steps (on the target machine):

1. Click `atlas_hello.py` -> **Raw** -> save it into a folder.
2. In PowerShell, in that folder:

       Get-FileHash .\atlas_hello.py -Algorithm SHA256
       # expected: EC308A601A4FB13C625AE88F025E5A24E5CE6132917BBFF7530C353DEAADA93D
       python atlas_hello.py
       # expected last line: ATLAS HELLO: ALL GOOD

3. Optional: `atlas_hello.zip` is the same file zipped, to check that zip
   downloads survive the network path (expected SHA-256
   2E3DC009835A8520F9229759F5B081F60BB126EBD9ED351BCC0C18A012A4FE3F).
4. Optional: `git clone` this repository from PowerShell to check that git
   over HTTPS works through the proxy.
