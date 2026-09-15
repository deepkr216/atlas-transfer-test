r"""
atlas.supervise - the build under a watchdog that lives OUTSIDE the process.

    python -m atlas.supervise estate --db atlas.db --also "C:\docs"     (the build's own arguments)

A parser stuck inside a regular expression holds the whole Python process:
no status line, no time limit, no stack dump - the second overnight hang
looked exactly like that (LESSONS 145). Nothing inside the process can act
on it, so this runs the build as a child, echoes its lines, and when the
child has printed nothing for SILENCE_SECONDS (the status clock prints
every 10 s, so silence means frozen) it kills the child, records the
member in hand (from atlas-current.txt beside the db) in atlas-skip.txt,
and starts the build again: parsed members are kept, the frozen member is
recorded as failed with the reason, and the build goes on. A build that
stops by itself on a member it could not interrupt (rc 3) is restarted
the same way. Ctrl+C stops both.
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from typing import List, Optional

SILENCE_SECONDS = 180
MAX_RESTARTS = 50
CURRENT_FILE = "atlas-current.txt"
SKIP_FILE = "atlas-skip.txt"


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def db_of(build_args: List[str]) -> str:
    for i, a in enumerate(build_args):
        if a == "--db" and i + 1 < len(build_args):
            return build_args[i + 1]
        if a.startswith("--db="):
            return a[5:]
    return "atlas.db"


def _read_current(path: str) -> tuple:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        return (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")
    except OSError:
        return "", ""


def _record(skip: str, member: str, label: str, why: str) -> None:
    with open(skip, "a", encoding="utf-8") as fh:
        fh.write(f"{member}\t# {label}: {why} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")


def run(build_args: List[str], silence: float = SILENCE_SECONDS, max_restarts: int = MAX_RESTARTS,
        say=print, python: Optional[str] = None) -> int:
    python = python or sys.executable
    folder = os.path.dirname(os.path.abspath(db_of(build_args))) or "."
    skip = os.path.join(folder, SKIP_FILE)
    current = os.path.join(folder, CURRENT_FILE)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the folder holding atlas/
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    restarts = 0
    last_phase_freeze: Optional[str] = None
    while True:
        cmd = [python, "-u", "-m", "atlas.build", *build_args, "--skip-list", skip]
        say(f"{_stamp()} supervisor: build started" + (f" (restart {restarts})" if restarts else "")
            + f" - a build silent for {int(silence)} s is frozen: it is killed, the member in hand is recorded in "
              f"{SKIP_FILE}, the build restarts and keeps what was parsed")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", env=env, bufsize=1)
        except OSError as e:
            say(f"supervisor: cannot start the build: {e}")
            return 2
        q: "queue.Queue[Optional[str]]" = queue.Queue()

        def reader() -> None:
            try:
                for line in proc.stdout:                                       # type: ignore[union-attr]
                    q.put(line)
            finally:
                try:
                    proc.stdout.close()                                        # type: ignore[union-attr]
                except OSError:
                    pass
                q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        last = time.time()
        frozen = False
        try:
            while True:
                try:
                    line = q.get(timeout=1.0)
                except queue.Empty:
                    if time.time() - last > silence:
                        frozen = True
                        break
                    continue
                if line is None:
                    break
                last = time.time()
                say(line.rstrip("\r\n"))
        except KeyboardInterrupt:
            say(f"\n{_stamp()} supervisor: Ctrl+C - stopping the build (run the same command again to continue)")
            try:
                proc.terminate()
                proc.wait(timeout=30)
            except Exception:                                                  # noqa: BLE001
                proc.kill()
            return 130
        if frozen:
            member, label = _read_current(current)
            try:
                proc.kill()
                proc.wait(timeout=30)
            except Exception:                                                  # noqa: BLE001
                pass
            if member == "PHASE":
                # a step of the build, not a member: nothing to put on the skip list
                if last_phase_freeze == label:
                    say(f"{_stamp()} supervisor: FROZEN twice in the same step ({label}) - stopping. Send "
                        f"atlas-stuck.txt if it exists, and the last 20 lines above.")
                    return 3
                last_phase_freeze = label
                say(f"{_stamp()} supervisor: FROZEN - nothing printed for {int(silence)} s during the step "
                    f"'{label}'. Build killed; restarting once - parsed members are kept.")
                restarts += 1
                continue
            last_phase_freeze = None
            if member:
                _record(skip, member, label, f"froze the parser (no output for {int(silence)} s)")
            say(f"{_stamp()} supervisor: FROZEN - nothing printed for {int(silence)} s while parsing "
                f"{label or '?'}. Build killed; {member or '(member unknown)'} recorded in {skip}; restarting - "
                f"parsed members are kept. Report that member's kind, size and shape (never its content) so the "
                f"parser can be fixed.")
            restarts += 1
            if restarts > max_restarts:
                say(f"supervisor: {max_restarts} restarts - stopping; the skip list names every member that froze")
                return 3
            continue
        rc = proc.wait()
        if rc == 3:
            member, label = _read_current(current)
            if member and member != "PHASE":
                _record(skip, member, label, "could not be interrupted (build stopped itself)")
            restarts += 1
            if restarts > max_restarts:
                return 3
            say(f"{_stamp()} supervisor: the build stopped on {label or member or '?'} - recorded in {skip}; restarting")
            continue
        if rc == 0:
            say(f"{_stamp()} supervisor: build finished" + (f" after {restarts} restart(s)" if restarts else ""))
        return rc


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    silence = SILENCE_SECONDS
    max_restarts = MAX_RESTARTS
    rest: List[str] = []
    i = 0
    while i < len(argv):                       # our own two options; everything else is the build's
        if argv[i] == "--silence" and i + 1 < len(argv):
            silence = float(argv[i + 1])
            i += 2
        elif argv[i] == "--max-restarts" and i + 1 < len(argv):
            max_restarts = int(argv[i + 1])
            i += 2
        elif argv[i] in ("-h", "--help"):
            print(__doc__)
            print("  --silence SECONDS      no output for this long = frozen (default 180)")
            print("  --max-restarts N       give up after this many restarts (default 50)")
            print("  every other argument goes to atlas.build unchanged")
            return 0
        else:
            rest.append(argv[i])
            i += 1
    if not rest:
        print("usage: python -m atlas.supervise <the build's arguments>   e.g.  estate --db atlas.db --also C:\\docs")
        return 2
    return run(rest, silence, max_restarts)


if __name__ == "__main__":
    sys.exit(main())
