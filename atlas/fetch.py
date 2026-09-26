"""
fetch.py - pull libraries from the mainframe into the local estate folder
with Zowe CLI, driven by one config file that the CLI and the UI share.

    python -m atlas.fetch --config sources.json --init        # write a starter config
    python -m atlas.fetch --config sources.json --check       # is zowe on PATH, does the profile work
    python -m atlas.fetch --config sources.json --plan        # print every command, run nothing
    python -m atlas.fetch --config sources.json --all         # download every enabled source
    python -m atlas.fetch --config sources.json --source PROD.CLAIMS.SRC
    python -m atlas.fetch --config sources.json --all --build # ...then rebuild atlas.db incrementally

The config answers "where does the automation look": each source maps a
mainframe dataset to a local folder, a kind (cobol, copybook, jcl, csd, ...),
a system, and whether it is the production (authoritative) copy. The manifest
that build.py needs is generated from it, so the two never disagree.

Zowe is invoked as a subprocess; nothing here talks to the mainframe itself.
Every command is logged exactly as run, so a shop-specific flag (a different
profile type, an encoding, `--preserve-original-letter-case`) is a config
edit, not a code change: put it in `extra_args`.

The subprocess runs the command exactly as the developer's own window does
(LESSONS 179): `zowe zos-files download all-members "DSN" -d folder`, from
the folder the window would run it in (`zowe.working_dir`, empty = the
folder that holds sources.json - Zowe looks for its project zowe.config.json
from the current folder upward), with the Zowe daemon left as the
environment has it (`zowe.daemon`: "window", or "off" for ZOWE_USE_DAEMON=no
when zowe hangs). A command that works in the window works unchanged here
and is asked for nothing the window does not ask for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field as dc_field
from typing import Callable, Dict, List, Optional, Tuple

KINDS = ["cobol", "copybook", "jcl", "proc", "ctlcard", "dbd", "psb", "bms", "mfs", "csd",
         "imsgen", "sql", "listing", "doc", "sched", "other"]
EXT_FOR_KIND = {"cobol": "cbl", "copybook": "cpy", "jcl": "jcl", "proc": "prc", "ctlcard": "ctl",
                "dbd": "dbd", "psb": "psb", "bms": "bms", "mfs": "mfs", "csd": "csd",
                "imsgen": "imsgen", "sql": "sql", "listing": "lst", "doc": "txt", "sched": "csv",
                "other": "txt"}

DEFAULT_CONFIG: Dict = {
    "zowe": {"executable": "zowe", "profile": "", "encoding": "", "timeout_seconds": 3600, "extra_args": [],
             "working_dir": "",       # folder zowe runs from; "" = the folder that holds sources.json
             "daemon": "window"},     # "window" = ZOWE_USE_DAEMON as the environment has it; "off" = no
    "local_root": "C:/estate",
    "extra_roots": [],       # folders indexed as well - the documentation folder, listings, exports
    "db": "atlas.db",
    "sources": [],
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

CONFIG_DIR_KEY = "_config_dir"      # where sources.json lives: in memory only, never written back


def load_config(path: str) -> Dict:
    """sources.json with every missing key at its default, so a file written
    before a key existed (no `working_dir`, no `daemon`) still loads."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        cfg["zowe"].update(user.get("zowe", {}))
        for k in ("local_root", "db"):
            if user.get(k):
                cfg[k] = user[k]
        cfg["extra_roots"] = [p for p in (user.get("extra_roots") or []) if str(p).strip()]
        # copybook names the compiles read from a product library the shop does not keep (beside the IBM ones the
        # build knows): written into the manifest, where build.load_system_includes reads them. Kept only when given
        if isinstance(user.get("system_includes"), list) and user["system_includes"]:
            cfg["system_includes"] = [str(n).strip().upper() for n in user["system_includes"] if str(n).strip()]
        cfg["sources"] = [dict(new_source(s.get("dataset", ""), s.get("kind", "other")), **s)
                          for s in user.get("sources", [])]
    cfg[CONFIG_DIR_KEY] = os.path.dirname(os.path.abspath(path))
    return cfg


def save_config(cfg: Dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in cfg.items() if k != CONFIG_DIR_KEY}, fh, indent=2)
    os.replace(tmp, path)


def zowe_cwd(cfg: Dict) -> str:
    """The folder every zowe subprocess runs from. Zowe looks for a project
    zowe.config.json from the CURRENT folder upward, so run from the wrong
    folder it finds no profile and asks for host / user / password - the
    window the developer types in never has that problem because it sits in
    the right folder. `zowe.working_dir` names that folder; empty means the
    folder that holds sources.json."""
    wd = str((cfg.get("zowe") or {}).get("working_dir") or "").strip()
    if not wd:
        wd = cfg.get(CONFIG_DIR_KEY) or os.getcwd()
    return os.path.abspath(os.path.expanduser(wd))


def daemon_off(cfg: Optional[Dict]) -> bool:
    """`zowe.daemon`: "window" (default) leaves ZOWE_USE_DAEMON exactly as
    the environment has it - the developer's own window runs that way;
    "off" sets ZOWE_USE_DAEMON=no for the subprocess (only if zowe hangs)."""
    if not cfg:
        return False
    return str((cfg.get("zowe") or {}).get("daemon") or "window").strip().lower() in ("off", "no", "false")


def run_from_line(cfg: Dict) -> str:
    """One line for the plan and the log: the folder the commands run from
    and the daemon setting, so the run can be compared with the window."""
    return (f"run from: {zowe_cwd(cfg)}"
            + ("   [ZOWE_USE_DAEMON=no - 'Zowe daemon off' is set]" if daemon_off(cfg)
               else "   [zowe daemon as your window has it]"))


def new_source(dataset: str, kind: str = "cobol", **kw) -> Dict:
    src = {
        "dataset": dataset.strip().upper(),
        "type": "pds",                     # pds | seq
        "kind": kind if kind in KINDS else "other",
        "system": "",
        "local": dataset.strip().upper(),  # folder (pds) or file (seq) under local_root
        "ext": EXT_FOR_KIND.get(kind, "txt"),
        "authoritative": False,
        "enabled": True,
        "extra_args": [],
        "last_fetched": None,
        "last_result": None,
    }
    src.update({k: v for k, v in kw.items() if k in src})
    src["system"] = (src.get("system") or "").strip().upper()
    # Departments get their own folder: C:/estate/CLAIMS/PROD.CLAIMS.SRC. The
    # library folder name is still the dataset name, so classification and the
    # manifest keep working, and same-named members in two departments never
    # collide on disk.
    if src["system"] and "local" not in kw:
        src["local"] = f"{src['system']}/{src['dataset']}"
    return src


def local_path(cfg: Dict, src: Dict) -> str:
    return os.path.normpath(os.path.join(cfg["local_root"], src.get("local") or src["dataset"]))


# --------------------------------------------------------------------------
# many departments, many libraries
# --------------------------------------------------------------------------

# Compiled output: PSBLIB / DBDLIB / ACBLIB / LOADLIB hold binaries, not
# source. Downloading them as text yields garbage, so they are recognised and
# added DISABLED with a warning - fetch PSBSOURCE / DBDSOURCE / the COBOL
# source library instead.
LOAD_SUFFIXES = {"LOAD", "LOADLIB", "LINKLIB", "LNKLIB", "PSBLIB", "DBDLIB", "ACBLIB", "MAPLIB",
                 "OBJ", "OBJLIB", "STEPLIB", "JOBLIB", "MODLIB"}
_KIND_BY_SUFFIX = [
    (("COPYLIB", "COPY", "CPY", "COPYBOOK", "COPYBOOKS", "INCLUDE", "INCLIB", "DCLGEN", "DCLLIB"), "copybook"),
    (("SRC", "SOURCE", "COBOL", "COB", "SRCLIB", "COBSRC", "PGMSRC", "PGMLIB"), "cobol"),
    (("JCL", "JCLLIB", "JOB", "JOBS"), "jcl"),
    (("PROC", "PROCLIB", "PROCS"), "proc"),
    (("PARM", "PARMLIB", "CNTL", "CONTROL", "CARDS", "CARDLIB", "CTL", "CTLCARD", "SYSIN"), "ctlcard"),
    (("DBDSRC", "DBDSOURCE", "DBDGEN", "DBD"), "dbd"),
    (("PSBSRC", "PSBSOURCE", "PSBGEN", "PSB"), "psb"),
    (("BMS", "BMSSRC", "MAPSRC", "MAPS"), "bms"),
    (("MFS", "MFSSRC"), "mfs"),
    (("CSD", "CSDUP", "CSDEXTR"), "csd"),
    (("STAGE1", "SYSGEN", "IMSGEN", "GEN"), "imsgen"),
    (("LIST", "LISTING", "LISTINGS", "LST", "SYSPRINT"), "listing"),
    (("SCHED", "SCHEDULE", "CA7", "CTM", "TWS", "OPC", "ZEKE", "ZEKELIB", "ZEKEDEF", "EVENTS"), "sched"),
    (("DDL", "SQL", "DCL"), "sql"),
    (("DOC", "DOCS", "SPEC", "SPECS"), "doc"),
]


def infer_kind(dataset: str) -> Tuple[str, bool]:
    """(kind, is_load_library) from the dataset name's qualifiers, last first."""
    toks = [t for t in re.split(r"[.()]", dataset.upper()) if t]
    is_load = any(t in LOAD_SUFFIXES for t in toks)
    for t in reversed(toks):
        for names, kind in _KIND_BY_SUFFIX:
            if t in names:
                return kind, is_load
    return "other", is_load


def bulk_add(cfg: Dict, system: str, text: str, authoritative: bool = False) -> Tuple[List[Dict], List[str]]:
    """One dataset per line -> sources for one department, kinds inferred.
    Returns (added sources, warnings). Load libraries are added disabled."""
    existing = {s["dataset"] for s in cfg["sources"]}
    added: List[Dict] = []
    warnings: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("*", "#", "//")):
            continue
        ds = s.split()[0].upper()
        if ds in existing:
            warnings.append(f"{ds}: already listed")
            continue
        kind, is_load = infer_kind(ds)
        src = new_source(ds, kind, system=system, authoritative=authoritative)
        if is_load:
            src["enabled"] = False
            warnings.append(f"{ds}: looks like a LOAD library (compiled) - added disabled; fetch the SOURCE library instead")
        elif kind == "other":
            warnings.append(f"{ds}: kind not recognised from the name - set it in Edit")
        cfg["sources"].append(src)
        added.append(src)
        existing.add(ds)
    return added, warnings


def systems_in(cfg: Dict) -> List[str]:
    return sorted({(s.get("system") or "").upper() for s in cfg["sources"] if s.get("system")})


def filter_sources(cfg: Dict, system: Optional[str] = None) -> List[int]:
    """Indices into cfg['sources'] for one department (None / '' / 'All' = every source)."""
    if not system or system.upper() == "ALL":
        return list(range(len(cfg["sources"])))
    return [i for i, s in enumerate(cfg["sources"]) if (s.get("system") or "").upper() == system.upper()]


# --------------------------------------------------------------------------
# zowe commands (pure functions - testable without zowe)
# --------------------------------------------------------------------------

def zowe_exe(cfg: Dict) -> Optional[str]:
    return shutil.which(cfg["zowe"].get("executable") or "zowe")


def _exe(cfg: Dict) -> str:
    return zowe_exe(cfg) or (cfg["zowe"].get("executable") or "zowe")


def _flags(cfg: Dict, src: Optional[Dict] = None) -> List[str]:
    z = cfg["zowe"]
    out: List[str] = []
    if z.get("profile"):
        out += ["--zosmf-profile", z["profile"]]
    if z.get("encoding"):
        out += ["--encoding", str(z["encoding"])]
    out += list(z.get("extra_args") or [])
    if src:
        out += list(src.get("extra_args") or [])
    # the session password goes on the command line too, not only in the
    # environment: an option is read by every Zowe version and mode, and
    # redact_cmd hides it from every log
    if _SESSION.get("ZOWE_OPT_USER"):
        out += ["--user", _SESSION["ZOWE_OPT_USER"]]
    if _SESSION.get("ZOWE_OPT_PASSWORD"):
        out += ["--password", _SESSION["ZOWE_OPT_PASSWORD"]]
    return out


def version_cmd(cfg: Dict) -> List[str]:
    return [_exe(cfg), "--version"]


def config_locations_cmd(cfg: Dict) -> List[str]:
    """`zowe config list --locations --root`: the configuration files zowe
    finds from the working folder, root property names only - no profile
    values, so nothing secret can reach a log."""
    return [_exe(cfg), "config", "list", "--locations", "--root"]


# a path may hold spaces (C:\Users\John Smith\.zowe\...): only quotes, brackets, a colon and a line end bound it
_CONFIG_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[^\"'<>|:\r\n]*?zowe\.config(?:\.user)?\.json", re.IGNORECASE)


def parse_config_locations(stdout: str) -> List[str]:
    """The zowe.config.json / zowe.config.user.json paths named in the
    output of `zowe config list --locations` (with or without --root) -
    paths only, never a value, in the order zowe listed them."""
    seen: List[str] = []
    for m in _CONFIG_PATH_RE.finditer(stdout or ""):
        p = m.group(0).strip().lstrip("-").strip()                     # a list bullet before the path is not the path
        if p not in seen:
            seen.append(p)
    return seen


def list_members_cmd(cfg: Dict, dataset: str) -> List[str]:
    return [_exe(cfg), "zos-files", "list", "all-members", dataset, "--response-format-json"] + _flags(cfg)


def download_cmd(cfg: Dict, src: Dict) -> List[str]:
    dest = local_path(cfg, src)
    if src.get("type", "pds") == "seq":
        return ([_exe(cfg), "zos-files", "download", "data-set", src["dataset"], "--file", dest]
                + _flags(cfg, src))
    # Exactly the command a developer types by hand, word for word:
    #     zowe zos-files download all-members "DSN" -d <folder>
    # Nothing else by default: a Zowe CLI that does not know a flag rejects
    # the whole command and downloads nothing (LESSONS 127); no profile flag
    # unless one is configured, no --user / --password unless a session
    # password was given (LESSONS 179). A shop that wants --extension or
    # --max-concurrent-requests puts them in extra_args.
    return ([_exe(cfg), "zos-files", "download", "all-members", src["dataset"], "-d", dest]
            + _flags(cfg, src))


def parse_member_list(stdout: str) -> List[str]:
    """Member names from `zowe zos-files list all-members --rfj` (or plain) output."""
    try:
        obj = json.loads(stdout)
        found: List[str] = []

        def walk(o):
            if isinstance(o, dict):
                if "member" in o and isinstance(o["member"], str):
                    found.append(o["member"].strip().upper())
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(obj)
        if found:
            return found
    except ValueError:
        pass
    return [m.upper() for m in re.findall(r"^\s*([A-Z0-9@#$]{1,8})\s*$", stdout, re.M)]


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

_SECRET_FLAGS = {"--password", "--pass", "--pw", "--token-value", "--tv", "--cert-key-file", "--api-key"}


def redact_list(cmd: List[str]) -> List[str]:
    """The command with every secret value replaced - what may be kept in a
    result object, a status column or a crash file."""
    return redact_cmd(cmd).split(" ")


def redact_cmd(cmd: List[str]) -> str:
    """The command line as it may be logged: a password or token given in
    extra_args never reaches the live log, a crash file or a pasted pack."""
    out: List[str] = []
    hide = False
    for tok in cmd:
        if hide:
            out.append("<redacted>")
            hide = False
            continue
        low = tok.lower()
        if low in _SECRET_FLAGS:
            out.append(tok)
            hide = True
        elif "=" in low and low.split("=", 1)[0] in _SECRET_FLAGS:
            out.append(low.split("=", 1)[0] + "=<redacted>")
        else:
            out.append(tok)
    return " ".join(out)


# --------------------------------------------------------------------------
# credentials for THIS RUN ONLY - OPTIONAL. A zowe command that works in a
# window without a prompt needs nothing here (LESSONS 179: run from the same
# folder, the same way). For a shop whose profile stores no password, `zowe`
# asks for it on the terminal - and a background run cannot answer, so Fetch
# all creates the folders and downloads nothing (LESSONS 128). Zowe also
# reads ZOWE_OPT_USER / ZOWE_OPT_PASSWORD from the environment: the UI's
# Sign in and the CLI's --ask-password put them there for the subprocess and
# nowhere else - not in sources.json, not in the log, not in a crash file.
# --------------------------------------------------------------------------
_SESSION: Dict[str, str] = {}


def set_session_credentials(user: Optional[str], password: Optional[str]) -> None:
    _SESSION.clear()
    if user:
        _SESSION["ZOWE_OPT_USER"] = user
    if password:
        _SESSION["ZOWE_OPT_PASSWORD"] = password


def has_session_credentials() -> bool:
    return "ZOWE_OPT_PASSWORD" in _SESSION


def session_env(cfg: Optional[Dict] = None) -> Dict[str, str]:
    """The zowe subprocess environment: this process's environment as it is
    - ZOWE_USE_DAEMON included, exactly as the developer's window has it -
    plus the session credentials. Only `zowe.daemon: "off"` sets
    ZOWE_USE_DAEMON=no: Zowe CLI v2/v3 normally hands the command to a
    background daemon process, and from a process with no console that
    hand-off has been seen to wait forever; the plain CLI is slower to start
    but answers. Off is the exception, never the default, because the plain
    CLI must then find the team configuration itself (LESSONS 179)."""
    env = dict(os.environ)
    if daemon_off(cfg):
        env["ZOWE_USE_DAEMON"] = "no"
    env.update(_SESSION)
    return env


HANG_SECONDS = 120     # a host command that prints nothing for this long is waiting for something it cannot ask


def _folder_hint(cfg: Optional[Dict]) -> str:
    """The first thing to try when zowe asked for host / user / password: it
    did not find the configuration the window uses, and that is a matter of
    the folder it ran from and the daemon setting, not of typing a password."""
    where = f" (this run was from {zowe_cwd(cfg)})" if cfg else ""
    return ("run the toolkit from the folder where your zowe command works, or set the working folder "
            "('Run zowe from folder' in the UI / sources.json -> zowe.working_dir)" + where
            + ", and leave the daemon as your window has it ('Zowe daemon off' unchecked / zowe.daemon \"window\")")


def password_hint(rc: int, out: str, err: str, cfg: Optional[Dict] = None) -> str:
    """What to tell the user when zowe failed - or hung - because it asked
    for something the window never asks for: the host name, the user, the
    password. rc 124 (timed out) with no output is the hang: zowe put up a
    prompt and, run in the background, waits forever. The FIRST suggestion
    is always the folder and the daemon - a window command that works
    without a prompt proves the configuration exists and holds what zowe
    needs; the toolkit only has to run from the same place the same way.
    The session password comes last, for shops whose profile stores none."""
    text = f"{out}\n{err}"
    folder = _folder_hint(cfg)
    if rc != 0 and re.search(r"host\s*name|hostname|Enter the host", text, re.IGNORECASE):
        return ("zowe asked for the HOST NAME: it did not find the configuration your window uses. First, "
                + folder + ". Only if the window needs them too, put the connection into sources.json -> "
                "zowe.extra_args, e.g. [\"--host\", \"HOST\", \"--port\", \"PORT\", \"--reject-unauthorized\", "
                "\"false\"] (`zowe config list --locations` shows them) - never the password")
    if rc == 124 and not (out or "").strip():
        if has_session_credentials():
            return ("zowe printed nothing and did not come back although a session password was given: it is "
                    "waiting for something else it cannot ask for - usually the HOST NAME, because it did not "
                    "find the configuration your window uses. First, " + folder + ". Only then --host / --port "
                    "(and --reject-unauthorized false if your certificate needs it) in sources.json -> "
                    "zowe.extra_args; `zowe config list --locations` shows the values")
        return ("zowe printed nothing and did not come back: it is waiting for something it cannot ask for in "
                "the background - the host name when it did not find your configuration, or the mainframe "
                "password when the profile stores none. First, " + folder + ". If your window asks for the "
                "password too, store it once with `zowe config secure` (Windows Credential Manager), or give "
                "it for this run last of all: Sign in (UI) or --ask-password (CLI)")
    if rc != 0 and re.search(r"password", text, re.IGNORECASE):
        return ("zowe wanted a password and a background run cannot type one. First, " + folder
                + " - a window command that works without a prompt needs no password here either. If your "
                "window asks for the password too, store it once in the profile (`zowe config secure`, kept in "
                "Windows Credential Manager), or give it for this session last of all: Sign in (UI) / "
                "--ask-password (CLI)")
    return ""


class Runner:
    """subprocess wrapper; tests substitute a fake with the same run() shape.
    Every zowe subprocess runs from `zowe_cwd(cfg)` with `session_env(cfg)`
    - the folder and the daemon setting of the developer's own window."""

    def __init__(self, timeout: int = 3600, cfg: Optional[Dict] = None):
        self.timeout = timeout
        self.cfg = cfg
        self.cwd: Optional[str] = zowe_cwd(cfg) if cfg else None

    def run(self, cmd: List[str]) -> Tuple[int, str, str]:
        if self.cwd and not os.path.isdir(self.cwd):
            return 2, "", f"the zowe working folder does not exist: {self.cwd}"
        try:
            # Zowe prints UTF-8; the Windows console codepage would otherwise
            # abort the whole fetch on the first unmappable byte. stdin is
            # closed so an interactive prompt fails at once instead of
            # waiting for the timeout; the session credentials ride in env.
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                               env=session_env(self.cfg), cwd=self.cwd)
            return p.returncode, p.stdout or "", p.stderr or ""
        except FileNotFoundError:
            return 127, "", f"not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {self.timeout}s"


@dataclass
class FetchResult:
    dataset: str
    ok: bool
    files: int
    seconds: float
    message: str
    command: List[str] = dc_field(default_factory=list)


def check_zowe(cfg: Dict, runner: Optional[Runner] = None,
               log: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """Four stages, each reported AS IT HAPPENS through `log` (zowe is a
    Node program: 10-20 s of silence before its first answer is normal):
    is zowe found (and which file), does it run, which configuration files
    it finds from the working folder (paths only - no values, no secrets),
    and - with the first enabled dataset - does the host answer a member
    list. The stage that fails is the problem; the message says what to do
    about it. Returns (ok, the same lines joined)."""
    lines: List[str] = []

    def say(line: str) -> None:
        lines.append(line)
        if log:
            log(line)

    folder = zowe_cwd(cfg)
    say("   " + run_from_line(cfg))
    exe = zowe_exe(cfg)
    if exe is None:
        say(f"1. '{cfg['zowe'].get('executable') or 'zowe'}' is not on PATH for this process. "
            "If it works in your own window, that window has a PATH this one lacks: start the UI from "
            "that same window, or set zowe.executable in sources.json to the full path (`where zowe` shows it).")
        return False, "\n".join(lines)
    say(f"1. zowe found: {exe}")
    if not os.path.isdir(folder):
        say(f"2. the working folder does not exist: {folder} - set 'Run zowe from folder' to the folder where "
            "your zowe command works (or leave it empty for the folder of sources.json)")
        return False, "\n".join(lines)
    say("   running `zowe --version` (Node starts slowly - up to 20 s of silence is normal) ...")
    runner = runner or Runner(180, cfg)
    rc, out, err = runner.run(version_cmd(cfg))
    if rc != 0:
        say(f"2. zowe --version failed (rc {rc}): {(err or out).strip()[:300]}"
            + ("\n   -> it did not answer in time: a zowe daemon or a Node hang; run `zowe --version` by hand "
               "in this same window and see how long it takes" if rc == 124 else ""))
        return False, "\n".join(lines)
    say(f"2. zowe --version: {out.strip().splitlines()[0] if out.strip() else '?'}"
        + (f", profile {cfg['zowe']['profile']}" if cfg["zowe"].get("profile") else "")
        + (", session password set" if has_session_credentials() else
           ", no session password (not needed when your zowe command works without a prompt)"))
    # Which configuration zowe sees from that folder. Only the file paths are
    # logged: the values (host, user) stay out of the log, the password is in
    # the secure store and never printed by this command anyway.
    say(f"   > {' '.join(config_locations_cmd(cfg))}")
    rc, out, err = runner.run(config_locations_cmd(cfg))
    paths = parse_config_locations(out) if rc == 0 else []
    if rc != 0:
        say(f"3. `zowe config list --locations` failed (rc {rc}): {(err or out).strip().splitlines()[0][:200] if (err or out).strip() else '(no output)'}"
            "\n   -> a Zowe v1 without team configuration answers this way; the check goes on with the profile as is")
    elif paths:
        say(f"3. zowe configuration found from {folder}:")
        for p in paths:
            say(f"     {p}")
    else:
        say(f"3. no zowe configuration found from {folder} - run the check from the folder where your command "
            "works, or set 'Run zowe from folder' (sources.json -> zowe.working_dir)")
    first = next((x for x in cfg.get("sources", []) if x.get("enabled", True) and x.get("type", "pds") != "seq"), None)
    if first is None:
        say("4. no enabled PDS in the table yet - add one, then Check again to test the host connection")
        return True, "\n".join(lines)
    say(f"   asking the host for the member list of {first['dataset']} (this is the first command that logs on; "
        f"if nothing comes back within {HANG_SECONDS} s, zowe is waiting for something it cannot ask for here) ...")
    say(f"   > {redact_cmd(list_members_cmd(cfg, first['dataset']))}"
        + ("   [ZOWE_USE_DAEMON=no]" if daemon_off(cfg) else ""))
    lister = runner if runner is not None and not isinstance(runner, Runner) else Runner(HANG_SECONDS, cfg)
    rc, out, err = lister.run(list_members_cmd(cfg, first["dataset"]))
    if rc != 0:
        tail = " | ".join((err or out).strip().splitlines()[-3:])[:400] or "(no output at all)"
        hint = password_hint(rc, out, err, cfg)
        say(f"4. the host did NOT answer `zowe zos-files list all-members {first['dataset']}` (rc {rc}): {tail}"
            + (f"\n   -> {hint}" if hint else
               "\n   -> the same command with your profile: run it by hand in your window and compare - "
               "a different profile, a certificate flag or a typo in the dataset name shows here"))
        return False, "\n".join(lines)
    members = parse_member_list(out)
    say(f"4. host answered: {first['dataset']} has {len(members)} member(s) - connection, profile and "
        "password are fine; Fetch will work")
    return True, "\n".join(lines)


def _count_files(path: str) -> int:
    if os.path.isfile(path):
        return 1
    if not os.path.isdir(path):
        return 0
    return sum(len(files) for _d, _s, files in os.walk(path))


def fetch_source(cfg: Dict, src: Dict, runner: Optional[Runner] = None,
                 log: Callable[[str], None] = print) -> FetchResult:
    runner = runner or Runner(int(cfg["zowe"].get("timeout_seconds") or 3600), cfg)
    cmd = download_cmd(cfg, src)
    safe = redact_list(cmd)                       # never the password, wherever the result ends up
    dest = local_path(cfg, src)
    is_pds = src.get("type", "pds") != "seq"
    os.makedirs(dest if is_pds else os.path.dirname(dest) or ".", exist_ok=True)
    log(f"  {run_from_line(cfg)}")
    log(f"> {redact_cmd(cmd)}")
    t0 = time.time()
    if zowe_exe(cfg) is None:
        res = FetchResult(src["dataset"], False, 0, 0.0, "zowe not on PATH", safe)
    else:
        rc, out, err = runner.run(cmd)
        secs = time.time() - t0
        n = _count_files(dest)
        if rc == 0:
            res = FetchResult(src["dataset"], True, n, secs, f"{n} file(s) in {dest}", safe)
        else:
            tail = (err or out).strip().splitlines()[-3:]
            hint = password_hint(rc, out, err, cfg)
            res = FetchResult(src["dataset"], False, n, secs,
                              f"rc {rc}: " + " | ".join(tail)[:300] + (f" - {hint}" if hint else ""), safe)
        if is_pds:
            # Reconcile with what the host says the library holds. A download
            # that stopped at member 2,900 of 4,100, or a member deleted on
            # the host months ago, must not be indexed as if complete.
            rec = reconcile_library(cfg, src, dest, rc, runner, log)
            if rec and not rec["complete"]:
                res = FetchResult(src["dataset"], False, n, secs,
                                  f"INCOMPLETE {rec['present']}/{rec['expected']} members"
                                  + (f" ({len(rec['missing'])} missing)" if rec["missing"] else "")
                                  + (f"; rc {rc}" if rc else ""), safe)
    src["last_fetched"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    src["last_result"] = ("ok " if res.ok else "FAILED ") + res.message
    log(("  ok   " if res.ok else "  FAIL ") + f"{src['dataset']}: {res.message}")
    return res


LIBRARY_MARKER = ".atlas-library.json"


def reconcile_library(cfg: Dict, src: Dict, dest: str, rc: int, runner: Runner,
                      log: Callable[[str], None] = print) -> Optional[Dict]:
    """List the members on the host and compare with the folder.

    Writes `<dest>/.atlas-library.json` (dataset, fetched_at, rc, expected,
    present, missing, stale) for the build to load into `library`; local
    files the host no longer lists are moved to `<dest>/.stale/` so a
    retired program does not stay alive in the index. Returns the record,
    or None when the member list could not be obtained (then nothing is
    moved and the folder is trusted as it is)."""
    lister = runner if not isinstance(runner, Runner) else Runner(max(HANG_SECONDS, 300), cfg)
    rc_l, out, err = lister.run(list_members_cmd(cfg, src["dataset"]))
    if rc_l != 0:
        log(f"  (member list unavailable, rc {rc_l}: folder taken as is)")
        return None
    expected = sorted(set(parse_member_list(out)))
    if not expected:
        return None
    present_files = {}
    for fn in os.listdir(dest):
        p = os.path.join(dest, fn)
        if os.path.isfile(p) and not fn.startswith("."):
            present_files[os.path.splitext(fn)[0].upper()] = fn
    present = sorted(set(present_files))
    missing = [m for m in expected if m not in present_files]
    stale = [m for m in present if m not in set(expected)]
    if stale:
        stale_dir = os.path.join(dest, ".stale")
        os.makedirs(stale_dir, exist_ok=True)
        for m in stale:
            try:
                os.replace(os.path.join(dest, present_files[m]), os.path.join(stale_dir, present_files[m]))
            except OSError:
                pass
        log(f"  {len(stale)} local member(s) no longer on the host moved to {stale_dir}")
    rec = {"dataset": src["dataset"], "folder": dest.replace("\\", "/"), "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "rc": rc, "expected": len(expected), "present": len(expected) - len(missing),
           "complete": rc == 0 and not missing, "missing": missing[:2000], "stale": stale[:2000]}
    with open(os.path.join(dest, LIBRARY_MARKER), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    if missing:
        log(f"  {len(missing)} listed member(s) NOT downloaded: {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")
    return rec


def fetch_all(cfg: Dict, runner: Optional[Runner] = None, log: Callable[[str], None] = print,
              only: Optional[List[str]] = None) -> List[FetchResult]:
    results = []
    want = {o.upper() for o in only} if only else None
    for src in cfg["sources"]:
        if not src.get("enabled", True) and not want:
            continue
        if want and src["dataset"].upper() not in want:
            continue
        results.append(fetch_source(cfg, src, runner, log))
    return results


# --------------------------------------------------------------------------
# hand-off to the index
# --------------------------------------------------------------------------

MANIFEST_MARK = "atlas.fetch"


def write_manifest(cfg: Dict, path: str) -> Dict:
    """build.py's manifest, derived from the sources so they cannot disagree.

    A manifest.json written BY HAND (no `_generated_by` marker) is never
    overwritten: the generated one goes to `manifest.generated.json` next to
    it and the caller is told - the hand-kept declarations stay in force.
    """
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and existing.get("_generated_by") != MANIFEST_MARK:
            alt = os.path.join(os.path.dirname(path), "manifest.generated.json")
            print(f"manifest {path} was written by hand - kept; the generated manifest is {alt}")
            path = alt
    man: Dict = {"_generated_by": MANIFEST_MARK, "authoritative": [], "system_of": {}, "systems": {},
                 "copylib_order": {}, "kinds": {}}
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        lp = local_path(cfg, src).replace("\\", "/")
        # the kind declared in the table decides when neither the content nor
        # the folder name can (control cards in a library called ...UTL)
        man["kinds"][os.path.basename(lp).upper()] = src.get("kind", "other")
        if src.get("authoritative"):
            man["authoritative"].append(lp)
        sysname = (src.get("system") or "").upper()
        if sysname:
            man["system_of"][os.path.basename(lp)] = sysname
            man["systems"].setdefault(sysname, []).append(lp)
            # The order copybook libraries are listed for a department is the
            # SYSLIB concatenation order its programs compile against: first
            # match wins when the same copybook name exists in several.
            if src.get("kind") == "copybook":
                man["copylib_order"].setdefault(sysname, []).append(lp)
    if cfg.get("system_includes"):
        # sources.json's copybook names supplied by a product library (ROADMAP re-parse item 27): a COPY of one no
        # member carries is not in the estate - never NOT FOUND, never a partial program
        man["system_includes"] = sorted({str(n).strip().upper() for n in cfg["system_includes"] if str(n).strip()})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)
    return man


def build_cmd(cfg: Dict, manifest_path: str, rebuild: bool = False) -> List[str]:
    cmd = [sys.executable, "-m", "atlas.build", cfg["local_root"], "--db", cfg.get("db") or "atlas.db",
           "--manifest", manifest_path]
    # Scheduler exports (kind "sched") are loaded into the index, not just downloaded.
    for src in cfg["sources"]:
        if src.get("kind") == "sched" and src.get("enabled", True):
            cmd += ["--sched", local_path(cfg, src)]
    # The documentation folder (and any other local-only folder) is indexed too.
    for extra in cfg.get("extra_roots") or []:
        cmd += ["--also", os.path.normpath(str(extra))]
    if rebuild:
        cmd.append("--rebuild")
    return cmd


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch mainframe libraries with Zowe CLI into the local estate.")
    ap.add_argument("--config", default="sources.json")
    ap.add_argument("--init", action="store_true", help="write a starter config and exit")
    ap.add_argument("--check", action="store_true", help="verify zowe is usable")
    ap.add_argument("--plan", action="store_true", help="print the commands, run nothing")
    ap.add_argument("--all", action="store_true", help="fetch every enabled source")
    ap.add_argument("--source", action="append", help="fetch this dataset (repeatable)")
    ap.add_argument("--build", action="store_true", help="rebuild atlas.db after fetching")
    ap.add_argument("--rebuild", action="store_true", help="with --build: start the db from empty")
    ap.add_argument("--ask-password", action="store_true",
                    help="optional - not needed when your zowe command works without a prompt: ask for the "
                         "mainframe password once, for this run only, never stored anywhere")
    ap.add_argument("--user", help="mainframe user id for this run (with --ask-password)")
    a = ap.parse_args(argv)

    if a.init:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["sources"] = [new_source("PROD.CLAIMS.SRC", "cobol", system="CLAIMS", authoritative=True),
                          new_source("PROD.CLAIMS.COPYLIB", "copybook", system="CLAIMS", authoritative=True),
                          new_source("PROD.CLAIMS.JCLLIB", "jcl", system="CLAIMS", authoritative=True),
                          new_source("PROD.CICS.CSD.EXTRACT", "csd", system="CLAIMS", type="seq",
                                     local="routing/cics-csd.txt")]
        save_config(cfg, a.config)
        print(f"wrote {a.config} - edit the datasets, then --check and --plan")
        return 0

    cfg = load_config(a.config)
    if a.ask_password:
        import getpass
        user = a.user or input("Mainframe user id (Enter to use the profile's): ").strip() or None
        set_session_credentials(user, getpass.getpass("Mainframe password (this run only, never stored): "))
    if a.check:
        print(f"checking zowe with {os.path.abspath(a.config)}", flush=True)
        ok, _msg = check_zowe(cfg, log=lambda line: print(line, flush=True))
        print("RESULT: OK - Fetch will work" if ok else "RESULT: FAIL - fix the stage above, then --check again", flush=True)
        return 0 if ok else 1
    if a.plan:
        # the exact command lines and the folder they run from, so the plan
        # can be compared word for word with the command typed in a window
        print(run_from_line(cfg))
        for src in cfg["sources"]:
            flag = "" if src.get("enabled", True) else "  (disabled)"
            print(redact_cmd(download_cmd(cfg, src)) + flag)
        man = os.path.join(os.path.dirname(os.path.abspath(a.config)), "manifest.json")
        print(" ".join(build_cmd(cfg, man, a.rebuild)))
        return 0
    if a.all or a.source:
        results = fetch_all(cfg, only=a.source)
        save_config(cfg, a.config)
        bad = [r for r in results if not r.ok]
        print(f"{len(results) - len(bad)} ok, {len(bad)} failed")
        if bad and not a.build:
            return 1
    if a.build:
        man = os.path.join(os.path.dirname(os.path.abspath(a.config)), "manifest.json")
        write_manifest(cfg, man)
        cmd = build_cmd(cfg, man, a.rebuild)
        print("> " + " ".join(cmd))
        return subprocess.call(cmd)
    if not (a.all or a.source):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
