"""
atlas_hello.py - transfer and environment test for Mainframe Atlas.

Run on the company laptop:   python atlas_hello.py
Contains no company data, makes no network calls, installs nothing.
"""
import hashlib
import os
import platform
import shutil
import sqlite3
import sys

checks = []


def check(name, ok, detail=""):
    checks.append((name, ok, detail))
    print(f"[{'OK ' if ok else 'NO '}] {name}" + (f"  - {detail}" if detail else ""))


print("=== ATLAS HELLO - transfer & environment test ===")

# 1. the file itself: print its hash so it can be compared with the sender's value
with open(os.path.abspath(__file__), "rb") as fh:
    digest = hashlib.sha256(fh.read()).hexdigest()
print(f"this file SHA-256: {digest}")
print("      (compare with the value in the message that sent this file)")

# 2. Python
v = sys.version_info
check("Python 3.9 or newer", v >= (3, 9), f"{platform.python_version()} on {platform.system()} {platform.release()}")

# 3. SQLite with full-text search (the index needs FTS5)
try:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    c.execute("INSERT INTO t VALUES('WS-PLCY-STAT-CD')")
    hit = c.execute("SELECT COUNT(*) FROM t WHERE t MATCH '\"WS-PLCY-STAT-CD\"'").fetchone()[0]
    check("SQLite FTS5 full-text search", hit == 1, f"sqlite {sqlite3.sqlite_version}")
except Exception as e:  # noqa: BLE001
    check("SQLite FTS5 full-text search", False, f"{type(e).__name__}: {e}")

# 4. Tkinter (only needed for the desktop UI; the command line works without it)
try:
    import tkinter  # noqa: F401
    check("Tkinter (desktop UI)", True, f"tk {tkinter.TkVersion}")
except Exception as e:  # noqa: BLE001
    check("Tkinter (desktop UI)", False, f"{type(e).__name__} - command line still works")

# 5. Zowe CLI (only needed to fetch from the mainframe)
z = shutil.which("zowe")
check("Zowe CLI on PATH", z is not None, z or "not found - fetching would need zowe.executable set to its full path")

# 6. can we write next to this file (the index db and downloaded members go somewhere like this)
try:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atlas_hello.tmp")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("hello\n")
    with open(p, "r", encoding="utf-8") as fh:
        back = fh.read()
    os.remove(p)
    check("Write/read/delete a file here", back == "hello\n", os.path.dirname(p))
except Exception as e:  # noqa: BLE001
    check("Write/read/delete a file here", False, f"{type(e).__name__}: {e}")

# 7. a tiny fixed-format COBOL read, the way the toolkit does it
line = ("000100" + " " + "    CALL 'VALIDATE' USING POLICY-RECORD.").ljust(72) + "SAMP0001"
indicator, code, ident = line[6], line[7:72].rstrip(), line[72:80]
check("Column rules (7 / 8-72 / 73-80)", indicator == " " and code.strip().startswith("CALL") and ident == "SAMP0001")

core = [ok for name, ok, _ in checks if name.startswith(("Python", "SQLite", "Write", "Column"))]
print()
if all(core):
    print("ATLAS HELLO: ALL GOOD - the toolkit will run on this machine.")
    sys.exit(0)
print("ATLAS HELLO: something core is missing - send this output back.")
sys.exit(1)
