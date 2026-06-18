#!/usr/bin/env python3
"""Parse a RISC-V Linux kernel tree into the bundled xevdb kernel JSON.

Unlike the RISC-V ISA data (curated), this is parsed from a real kernel source
checkout — point ``--kernel-tree`` at any ``linux/`` tree. The parsing logic
lives in ``xevdb.kernel`` so it is reused by ``ingest-kernel --kernel-tree`` and
unit-tested. Output is committed under ``src/xevdb/data/kernel/`` so ingest
needs no kernel tree at run time.

Run:  python scripts/gen_kernel_data.py --kernel-tree /path/to/linux
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xevdb import kernel  # noqa: E402

OUT = ROOT / "src" / "xevdb" / "data" / "kernel"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kernel-tree", required=True, help="path to a linux/ checkout")
    args = ap.parse_args()

    data = kernel.parse_tree(args.kernel_tree)
    OUT.mkdir(parents=True, exist_ok=True)
    for cat in kernel.CATEGORIES:
        rows = [asdict(r) for r in getattr(data, cat)]
        (OUT / f"{cat}.json").write_text(json.dumps(
            {"kernel_version": data.kernel_version, "count": len(rows), cat: rows},
            indent=2) + "\n", encoding="utf-8")
        print(f"wrote data/kernel/{cat}.json  ({len(rows)} rows)")
    print(f"kernel_version = {data.kernel_version}")


if __name__ == "__main__":
    main()
