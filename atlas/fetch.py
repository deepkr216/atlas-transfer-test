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
    "zowe": {"executable": "zowe", "profile": "", "encoding": "", "max_concurrent": 8,
             "timeout_seconds": 3600, "extra_args": []},
    "local_root": "C:/estate",
    "db": "atlas.db",
    "sources": [],
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path: str) -> Dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        cfg["zowe"].update(user.get("zowe", {}))
        for k in ("local_root", "db"):
            if user.get(k):
                cfg[k] = user[k]
        cfg["sources"] = [dict(new_source(s.get("dataset", ""), s.get("kind", "other")), **s)
                          for s in user.get("sources", [])]
    return cfg


def save_config(cfg: Dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, path)


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
    return src


def local_path(cfg: Dict, src: Dict) -> str:
    return os.path.normpath(os.path.join(cfg["local_root"], src.get("local") or src["dataset"]))


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
    return out


def version_cmd(cfg: Dict) -> List[str]:
    return [_exe(cfg), "--version"]


def list_members_cmd(cfg: Dict, dataset: str) -> List[str]:
    return [_exe(cfg), "zos-files", "list", "all-members", dataset, "--response-format-json"] + _flags(cfg)


def download_cmd(cfg: Dict, src: Dict) -> List[str]:
    dest = local_path(cfg, src)
    if src.get("type", "pds") == "seq":
        return ([_exe(cfg), "zos-files", "download", "data-set", src["dataset"], "--file", dest]
                + _flags(cfg, src))
    return ([_exe(cfg), "zos-files", "download", "all-members", src["dataset"],
             "--directory", dest, "--extension", src.get("ext") or EXT_FOR_KIND.get(src.get("kind", ""), "txt"),
             "--max-concurrent-requests", str(cfg["zowe"].get("max_concurrent") or 8)]
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

class Runner:
    """subprocess wrapper; tests substitute a fake with the same run() shape."""

    def __init__(self, timeout: int = 3600):
        self.timeout = timeout

    def run(self, cmd: List[str]) -> Tuple[int, str, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
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


def check_zowe(cfg: Dict, runner: Optional[Runner] = None) -> Tuple[bool, str]:
    if zowe_exe(cfg) is None:
        return False, (f"'{cfg['zowe'].get('executable') or 'zowe'}' is not on PATH. Install Zowe CLI "
                       f"or set zowe.executable in the config to its full path.")
    runner = runner or Runner(60)
    rc, out, err = runner.run(version_cmd(cfg))
    if rc != 0:
        return False, f"zowe --version failed (rc {rc}): {err.strip()[:200]}"
    msg = f"zowe {out.strip().splitlines()[0] if out.strip() else '?'}"
    if cfg["zowe"].get("profile"):
        msg += f", profile {cfg['zowe']['profile']}"
    return True, msg


def _count_files(path: str) -> int:
    if os.path.isfile(path):
        return 1
    if not os.path.isdir(path):
        return 0
    return sum(len(files) for _d, _s, files in os.walk(path))


def fetch_source(cfg: Dict, src: Dict, runner: Optional[Runner] = None,
                 log: Callable[[str], None] = print) -> FetchResult:
    runner = runner or Runner(int(cfg["zowe"].get("timeout_seconds") or 3600))
    cmd = download_cmd(cfg, src)
    dest = local_path(cfg, src)
    os.makedirs(dest if src.get("type", "pds") != "seq" else os.path.dirname(dest) or ".", exist_ok=True)
    log(f"> {' '.join(cmd)}")
    t0 = time.time()
    if zowe_exe(cfg) is None:
        res = FetchResult(src["dataset"], False, 0, 0.0, "zowe not on PATH", cmd)
    else:
        rc, out, err = runner.run(cmd)
        secs = time.time() - t0
        n = _count_files(dest)
        if rc == 0:
            res = FetchResult(src["dataset"], True, n, secs, f"{n} file(s) in {dest}", cmd)
        else:
            tail = (err or out).strip().splitlines()[-3:]
            res = FetchResult(src["dataset"], False, n, secs, f"rc {rc}: " + " | ".join(tail)[:300], cmd)
    src["last_fetched"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    src["last_result"] = ("ok " if res.ok else "FAILED ") + res.message
    log(("  ok   " if res.ok else "  FAIL ") + f"{src['dataset']}: {res.message}")
    return res


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

def write_manifest(cfg: Dict, path: str) -> Dict:
    """build.py's manifest, derived from the sources so they cannot disagree."""
    man = {"authoritative": [], "system_of": {}}
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        lp = local_path(cfg, src).replace("\\", "/")
        if src.get("authoritative"):
            man["authoritative"].append(lp)
        if src.get("system"):
            man["system_of"][os.path.basename(lp)] = src["system"]
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
    if a.check:
        ok, msg = check_zowe(cfg)
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    if a.plan:
        for src in cfg["sources"]:
            flag = "" if src.get("enabled", True) else "  (disabled)"
            print(" ".join(download_cmd(cfg, src)) + flag)
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
