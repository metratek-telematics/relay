#!/usr/bin/env python3
"""One-time import: build DATA_DIR/deploy.json from the three read-only deployment scans.

    python3 tools/seed_deploy_map.py [--scans DIR] [--force] [--list]

The scans (knowledge/deploy/{edge,z2,ci}-deploy.json) are the ground truth for what runs where.
Everything this writes is disabled and non-automatic: enabling a target is the owner's decision,
taken one target at a time in Settings → Deploy. Nothing is deployed by running this.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator import deploy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scans", default=str(deploy.SCANS_DIR), help="folder holding the three *-deploy.json scans")
    ap.add_argument("--force", action="store_true", help="rebuild an existing map (owner switches are kept)")
    ap.add_argument("--list", action="store_true", help="print the map instead of seeding")
    args = ap.parse_args()
    if args.list:
        for t in deploy.list_targets():
            flags = ",".join([k for k in ("enabled", "auto_on_merge", "critical", "never_pull") if t.get(k)]) or "-"
            print(f"{t['repo']:<26} {t['host']:<7} {t['method']:<17} {t.get('service') or t.get('workflow') or '':<28} {flags}")
        return 0
    try:
        result = deploy.seed_from_scans(args.scans, force=args.force)
    except deploy.DeployError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    print(f"Seeded {result['targets']} deploy targets into {result['file']} (all disabled).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
