#!/usr/bin/env python3
"""
Prove forge.simdata against EA's own SimData.

For every SimData resource in a game package:
  1. read it, then write it back at EA's original table positions and
     require the bytes to match exactly (proves every encoding rule);
  2. write it again with our own 16-byte table placement, read that back,
     and require the same tables, rows and pointers (proves re-layout).

    python tools/check_simdata.py
    python tools/check_simdata.py "D:\\Games\\The Sims 4\\Data\\Simulation\\SimulationDeltaBuild0.package"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.dbpf import read_package  # noqa: E402
from forge.ids import ResourceType  # noqa: E402
from forge.simdata import read_simdata, write_simdata  # noqa: E402

DEFAULT = Path(r"C:\Program Files\EA Games\The Sims 4\Data\Simulation\SimulationDeltaBuild0.package")


def same_content(a, b) -> bool:
    if len(a.tables) != len(b.tables):
        return False
    index_a = {id(t): i for i, t in enumerate(a.tables)}
    index_b = {id(t): i for i, t in enumerate(b.tables)}
    for ta, tb in zip(a.tables, b.tables):
        if (ta.name, ta.data_type, ta.row_size, ta.rows) != (tb.name, tb.data_type, tb.row_size, tb.rows):
            return False
        for pa, pb in zip(ta.pointers, tb.pointers):
            flat_a = {k: None if r is None else (index_a[id(r.table)], r.row) for k, r in pa.items()}
            flat_b = {k: None if r is None else (index_b[id(r.table)], r.row) for k, r in pb.items()}
            if flat_a != flat_b:
                return False
    return True


def main() -> int:
    package = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    resources = [r for r in read_package(package)
                 if r.type_id == ResourceType.SIMDATA and r.data]
    exact = relaid = 0
    failures: list[str] = []
    for res in resources:
        try:
            model, layout = read_simdata(res.data)
            if write_simdata(model, layout) == res.data:
                exact += 1
            else:
                mine = write_simdata(model, layout)
                first = next((i for i, (x, y) in enumerate(zip(mine, res.data)) if x != y),
                             min(len(mine), len(res.data)))
                failures.append(f"{res.key_string()}: exact rewrite differs at 0x{first:X} "
                                f"(sizes {len(mine)} vs {len(res.data)})")
            again, _ = read_simdata(write_simdata(model))
            if same_content(model, again):
                relaid += 1
            else:
                failures.append(f"{res.key_string()}: content changed after re-layout")
        except Exception as exc:  # report and keep going
            failures.append(f"{res.key_string()}: {type(exc).__name__}: {exc}")

    total = len(resources)
    print(f"{package.name}: {total} SimData resources")
    print(f"  byte-exact at EA layout: {exact}/{total}")
    print(f"  intact after re-layout:  {relaid}/{total}")
    for line in failures[:15]:
        print("  FAIL", line)
    if len(failures) > 15:
        print(f"  ... {len(failures) - 15} more")
    return 0 if exact == relaid == total else 1


if __name__ == "__main__":
    sys.exit(main())
