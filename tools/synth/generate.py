"""
generate.py - write the synthetic estate and its ground truth.

    python tools/synth/generate.py --out FOLDER [--seed N]

Writes FOLDER/estate (the members, estate\\SYSTEM\\LIBRARY\\member),
FOLDER/listings (compiler listings for atlas.recover --from),
FOLDER/manifest.json and FOLDER/truth.json. Deterministic for a seed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from synth_estate import generate  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the synthetic estate and its truth.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--scale", type=int, default=12, help="generic programs (and jobs) per system")
    a = ap.parse_args(argv)
    est = generate(a.out, a.seed, a.scale)
    truth = est.truth()
    with open(os.path.join(est.out, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=1, default=list)
    kinds = {}
    for m in truth["members"]:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    print(f"estate written to {est.root}: {len(truth['members'])} members "
          + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print(f"programs {len(truth['programs'])}  jobs {len(truth['jobs'])}  procs {len(truth['procs'])}  "
          f"copybooks {len(truth['copybooks'])}  datasets {len(truth['datasets'])}  tables {len(truth['db2'])}  "
          f"dbds {len(truth['ims']['dbds'])}  psbs {len(truth['ims']['psbs'])}  transactions {len(truth['transactions'])}")
    print(f"listings: {len(truth['listings'])} in {est.listings_dir}; truth: {os.path.join(est.out, 'truth.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
