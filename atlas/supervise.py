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
import pathlib
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

SILENCE_SECONDS = 180
STARTUP_SECONDS = 120          # before its first line a build may be slow to start (antivirus scanning Python)
MAX_RESTARTS = 50
CURRENT_FILE = "atlas-current.txt"
SKIP_FILE = "atlas-skip.txt"
PROBLEMS_FILE = "atlas-problems.txt"
# The build's own options and how many values each takes. argparse accepts any
# unique prefix (--rebuil), so the watchdog spells them out before it looks.
BUILD_OPTIONS: Dict[str, int] = {"--db": 1, "--manifest": 1, "--sched": 1, "--also": 1, "--rebuild": 0,
                                 "--write-expanded": 1, "--limit": 1, "--skip-list": 1, "--no-current-file": 0,
                                 "--member-limit": 1, "--quiet": 0}


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


def canonical_args(build_args: List[str]):
    """`--rebuil` and `--als=x` are what argparse reads as --rebuild and
    --also=x: spelled out here, so every check below sees the real option."""
    out: List[str] = []
    notes: List[str] = []
    for a in build_args:
        name, eq, val = a.partition("=")
        if name.startswith("--") and name not in BUILD_OPTIONS:
            full = [o for o in BUILD_OPTIONS if o.startswith(name)]
            if len(full) == 1:
                notes.append(f"{name} read as {full[0]}")
                a = full[0] + (eq + val if eq else "")
        out.append(a)
    return out, notes


def build_settings(build_args: List[str]) -> argparse.Namespace:
    """root, --also folders and --rebuild as the build itself reads them."""
    ap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    ap.add_argument("root", nargs="?")
    for opt, n in BUILD_OPTIONS.items():
        if opt in ("--also", "--sched"):
            ap.add_argument(opt, action="append", default=[])
        elif n:
            ap.add_argument(opt)
        else:
            ap.add_argument(opt, action="store_true")
    ns, _rest = ap.parse_known_args(build_args)
    return ns


def uncovered_members(db: str, roots: List[str]) -> Optional[dict]:
    """The indexed members that lie under none of `roots` - exactly what a
    build given these folders would REMOVE from the index (it keeps only what
    it finds under them, and calls the rest 'gone from disk'). The build
    matches paths as plain strings, so this does too; a folder typed with
    different capitals from last time is reported apart."""
    prefixes = [os.path.normpath(r) + os.sep for r in roots if r]
    try:
        conn = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT path FROM member").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None                                                        # not readable now: the build will say
    other: List[str] = []
    case_only: List[tuple] = []
    for (path,) in rows:
        if any(path.startswith(p) for p in prefixes):
            continue
        hit = next((p for p in prefixes if os.path.normcase(path).startswith(os.path.normcase(p))), None)
        if hit:
            case_only.append((path[:len(hit)].rstrip(os.sep), hit.rstrip(os.sep)))
        else:
            other.append(path)
    return {"total": len(rows), "count": len(other) + len(case_only), "other": other, "case_only": case_only}


def _folders_of(paths: List[str], limit: int = 3) -> List[tuple]:
    """(folder, count, first names) for the folders that hold `paths`: the
    common folder when there is one, else the top folders by count."""
    if not paths:
        return []
    try:
        common = os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError:
        common = ""
    depth = len(common.rstrip(os.sep).split(os.sep)) if common else 0
    if depth < 2:
        depth = 3
    groups: Dict[str, List[str]] = {}
    for p in paths:
        key = os.sep.join(p.split(os.sep)[:depth])
        groups.setdefault(key, []).append(os.path.splitext(os.path.basename(p))[0].upper())
    top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:limit]
    return [(k, len(v), v[:2]) for k, v in top]


def prune_message(check: dict, more_folders_hint: str) -> str:
    lines = [f"{_stamp()} supervisor: NOT STARTED - {check['count']:,} of the {check['total']:,} indexed members are under "
             f"folders this command does not name:"]
    for folder, n, names in _folders_of(check["other"]):
        lines.append(f"    {folder}  ({n:,} members, e.g. {', '.join(names)})")
    if check["case_only"]:
        stored, typed = check["case_only"][0]
        lines.append(f"    {len(check['case_only']):,} under a folder typed with different capitals from last time: "
                     f"{stored} then, {typed} now - the build tells them apart and would parse every one of them again; "
                     f"type it exactly as before")
    lines.append("  A build keeps only what is under the folders it is given: the rest is REMOVED from the index "
                 "(documents with the text read from their pictures) and read again from scratch the next time the "
                 "folder is named.")
    lines.append(f"  Add the folder to the command ({more_folders_hint}); if it moved, or you mean to drop it, "
                 "add --allow-prune and run again.")
    return "\n".join(lines)


def recorded_members(skip: str) -> set:
    """The members already on the skip list (first field of each line)."""
    out = set()
    try:
        with open(skip, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                item = ln.split("\t", 1)[0].strip()
                if item and not item.startswith("#"):
                    out.add(item)
    except OSError:
        pass
    return out


def restart_args(build_args: List[str]) -> List[str]:
    """The arguments for a restart: never --rebuild. A restart exists to KEEP
    what was parsed; --rebuild again would delete the index and throw away
    every hour already spent (LESSONS 152)."""
    return [a for a in build_args if a != "--rebuild"]


def again_hint(build_args: List[str]) -> str:
    """What to type to continue after a stop."""
    if "--rebuild" in build_args:
        return "run the same command again WITHOUT --rebuild to continue (with it, everything parsed so far is deleted)"
    return "run the same command again to continue"


def _record(skip: str, member: str, label: str, why: str) -> None:
    with open(skip, "a", encoding="utf-8") as fh:
        fh.write(f"{member}\t# {label}: {why} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")


def run(build_args: List[str], silence: float = SILENCE_SECONDS, max_restarts: int = MAX_RESTARTS,
        say=print, python: Optional[str] = None) -> int:
    python = python or sys.executable
    build_args, notes = canonical_args(list(build_args))
    for n in notes:
        say(f"{_stamp()} supervisor: {n}")
    if "--quiet" in build_args:
        build_args = [a for a in build_args if a != "--quiet"]
        say(f"{_stamp()} supervisor: --quiet dropped - the watchdog tells a working build from a frozen one by its "
            "status lines")
    allow_prune = "--allow-prune" in build_args
    build_args = [a for a in build_args if a != "--allow-prune"]
    db = db_of(build_args)
    folder = os.path.dirname(os.path.abspath(db)) or "."
    skip = os.path.join(folder, SKIP_FILE)
    current = os.path.join(folder, CURRENT_FILE)
    problems = os.path.join(folder, PROBLEMS_FILE)
    settings = build_settings(build_args)
    if os.path.isfile(db) and not settings.rebuild and not allow_prune:
        # a build run without --also once removes every document from the index (LESSONS 153)
        check = uncovered_members(db, [settings.root] + list(settings.also))
        if check and check["count"]:
            say(prune_message(check, '--also "folder"' if settings.root else "the estate folder first"))
            return 2
    kept: List[str] = []                                                   # problem lines from before a restart

    def keep_problems() -> None:
        try:
            with open(problems, encoding="utf-8", errors="replace") as fh:
                kept.extend(ln.rstrip("\n") for ln in fh if ln.count("\t") >= 3)
        except OSError:
            pass

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the folder holding atlas/
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    restarts = 0
    last_phase_freeze: Optional[str] = None
    while True:
        args_now = restart_args(build_args) if restarts else build_args
        cmd = [python, "-u", "-m", "atlas.build", *args_now, "--skip-list", skip]
        say(f"{_stamp()} supervisor: build started"
            + (f" (restart {restarts}" + (", without --rebuild" if "--rebuild" in build_args else "") + ")"
               if restarts else "")
            + f" - a build silent for {int(silence)} s is frozen: it is killed, the member in hand is recorded in "
              f"{SKIP_FILE}, the build restarts and keeps what was parsed")
        try:
            os.remove(current)                 # the build rewrites it: yesterday's member must never be skip-listed
        except OSError:
            pass
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
        started = False
        frozen = False
        try:
            while True:
                try:
                    line = q.get(timeout=1.0)
                except queue.Empty:
                    # silence counts from the first line: starting Python and loading the
                    # toolkit is not a frozen parser, however slow the laptop
                    if time.time() - last > (silence if started else silence + STARTUP_SECONDS):
                        frozen = True
                        break
                    continue
                if line is None:
                    break
                started = True
                last = time.time()
                say(line.rstrip("\r\n"))
        except KeyboardInterrupt:
            say(f"\n{_stamp()} supervisor: Ctrl+C - waiting for the build to stop cleanly ({again_hint(build_args)})")
            try:
                proc.wait(timeout=45)          # the build got the same Ctrl+C: it commits and says where it stopped
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=30)
                except Exception:                                              # noqa: BLE001
                    proc.kill()
            deadline = time.time() + 2
            while time.time() < deadline:      # show what it said on the way out
                try:
                    line = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                say(line.rstrip("\r\n"))
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
                keep_problems()
                continue
            last_phase_freeze = None
            if member and member in recorded_members(skip):
                say(f"{_stamp()} supervisor: FROZEN again on {label or member} - it is already on the skip list, so "
                    f"the list is not taking (a '#' in the path? the build cuts the line there). Not restarting: "
                    f"move that one member out of the folder, {again_hint(build_args)}, and report its kind, "
                    f"size and shape (never its content) so the parser can be fixed.")
                return 3
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
            keep_problems()
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
            keep_problems()
            continue
        if kept:
            # every build run starts atlas-problems.txt afresh and lists only what it saw itself;
            # a member given up on before a restart is settled, never parsed again, never listed again
            try:
                with open(problems, "a", encoding="utf-8") as fh:
                    fh.write("".join(ln + "\n" for ln in kept))
            except OSError:
                pass
            say(f"{_stamp()} supervisor: {len(kept)} problem line(s) from before the restart(s) - kept in {problems}:")
            for ln in kept[:10]:
                parts = ln.split("\t")
                say(f"    {parts[1]}: {os.path.basename(parts[2])}  {parts[3][:120]}" if len(parts) >= 4 else "    " + ln[:160])
            if len(kept) > 10:
                say(f"    ... and {len(kept) - 10} more")
        if rc == 0:
            say(f"{_stamp()} supervisor: build finished" + (f" after {restarts} restart(s)" if restarts else ""))
        elif rc == 130:
            say(f"{_stamp()} supervisor: the build was stopped by Ctrl+C - {again_hint(build_args)}")
        elif rc == 4:
            say(f"{_stamp()} supervisor: the build STOPPED - the index cannot be written (see the STOPPED message "
                f"above). Not restarting: fix that first, then {again_hint(build_args)}.")
        else:
            crash = os.path.join(folder, "atlas-crash.txt")
            say(f"{_stamp()} supervisor: the build STOPPED BY AN ERROR (exit code {rc}) - the message is above"
                + (f" and in {crash}" if os.path.exists(crash) else "")
                + ". Not restarting: send that report (toolkit lines only, nothing from the estate).")
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
            print("  --allow-prune          go ahead when the command names fewer folders than the index holds")
            print("  every other argument goes to atlas.build unchanged (a restart never carries --rebuild)")
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
