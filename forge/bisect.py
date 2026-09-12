"""
Crash bisection.

When a package crashes the game at load, the crash log rarely says why: the
failure is inside EA's native resource loader, before Python starts, so there
is no traceback to read. Guessing at the cause burns a game restart per guess.

This module inverts the problem. It emits a numbered series of packages, each
adding one more resource type than the last. You drop them in one at a time
and note the first one that crashes. That number identifies the guilty
resource type directly, without needing to decode anything.

    01  string table only            - proves the container format is sound
    02  + career levels              - the most numerous tuning type
    03  + career tracks              - adds cross-references between tuning
    04  + career                     - the full mod

If 01 crashes, the DBPF container itself is malformed and nothing else
matters. If 01 loads and 02 crashes, career level tuning is the problem. And
so on. Whichever step first fails is the thing to fix.

Each package also gets a variant with tuning under the generic tuning type id
rather than the career-specific ones. The career-specific ids have since been
confirmed against s4pi's type tables, so this variant is kept only as a
control.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .builder import TYPE_FOR_KIND, package_resources
from .dbpf import Resource, write_package
from .ids import InstanceAllocator, ResourceType, stbl_instance
from .schema import CareerSpec
from .stbl import StringTable
from .tuning import build_all_tuning
from .validate import validate_package

# Order matters: each stage adds the kinds listed, on top of everything
# before it. Every tuning resource travels with its SimData.
STAGES: list[tuple[str, str, list[str]]] = [
    ("01_strings_only", "String table only. No tuning at all.", []),
    ("02_plus_levels", "Adds career levels, their promotion gates and the "
                       "performance statistic.",
     ["statistic", "aspiration", "career_level"]),
    ("03_plus_tracks", "Adds career track tuning.", ["career_track"]),
    ("04_full", "Adds the top-level career. This is the full mod.", ["career"]),
]


@dataclass
class BisectResult:
    output_dir: Path
    packages: list[Path]
    instructions: Path


def build_bisect(spec: CareerSpec, output_root: str | Path,
                 generic_type: bool = False,
                 on_progress=None) -> BisectResult:
    """
    Build the test series.

    generic_type=True puts every tuning resource under the generic tuning
    type id (0x03B33DDF) instead of the career-specific ids. Run the series
    both ways: if the generic set loads where the specific set crashed, the
    career type ids are wrong and that is the whole bug.
    """
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    problems = spec.validate()
    if problems:
        raise ValueError("spec is not valid:\n" + "\n".join(problems))

    suffix = "generic_type" if generic_type else "career_types"
    output_dir = Path(output_root) / f"{spec.mod_key}_bisect_{suffix}"
    output_dir.mkdir(parents=True, exist_ok=True)

    alloc = InstanceAllocator(spec.mod_key)
    strings = StringTable()
    tuning = build_all_tuning(spec, alloc, strings)

    stbl_resource = Resource(
        type_id=ResourceType.STBL,
        group_id=0,
        instance_id=stbl_instance(spec.mod_key),
        data=strings.to_bytes(),
    )

    written: list[Path] = []
    included_kinds: list[str] = []

    for index, (name, description, adds) in enumerate(STAGES):
        included_kinds.extend(adds)

        resources = [stbl_resource]
        for item in tuning.resources:
            if item.kind in included_kinds:
                resources.extend(package_resources(
                    item, alloc, ResourceType.TUNING if generic_type else None))

        path = output_dir / f"{spec.mod_key}_{name}.package"
        write_package(resources, path)
        problems = validate_package(path, expected=resources)
        if problems:
            path.unlink()
            raise ValueError(f"{path.name} failed verification:\n" + "\n".join(problems))
        written.append(path)
        report(f"{path.name}  ({len(resources)} resources) - {description}")

    instructions = output_dir / "HOW_TO_USE.txt"
    instructions.write_text(
        _render_instructions(spec, written, generic_type), encoding="utf-8"
    )

    manifest = {
        "mod_key": spec.mod_key,
        "type_mode": suffix,
        "type_ids": {
            kind: f"0x{(ResourceType.TUNING if generic_type else tid):08X}"
            for kind, tid in TYPE_FOR_KIND.items()
        },
        "stages": [
            {"file": p.name, "description": d}
            for p, (_n, d, _a) in zip(written, STAGES)
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    return BisectResult(
        output_dir=output_dir, packages=written, instructions=instructions
    )


def _render_instructions(spec: CareerSpec, packages: list[Path],
                         generic_type: bool) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 68)
    add(f"  Crash bisection for {spec.name}")
    add(f"  tuning type ids: "
        f"{'generic (0x03B33DDF)' if generic_type else 'career-specific'}")
    add("=" * 68)
    add("")
    add("Goal: find the first package that crashes the game. That tells us")
    add("which resource type the game cannot load.")
    add("")
    add("Before you start:")
    add("  - Take every other Career Forge file out of the Mods folder.")
    add("  - Leave your other mods out too, so nothing else muddies it.")
    add("")
    add("For EACH package below, in order:")
    add("  1. Put exactly ONE of these .package files in the Mods folder.")
    add("  2. Delete localthumbcache.package.")
    add("  3. Start the game. Note: does it reach the main menu?")
    add("  4. Quit. Remove that package before testing the next.")
    add("")
    add("-" * 68)
    for index, path in enumerate(packages):
        add(f"  [ ] {path.name}")
        add(f"        {STAGES[index][1]}")
    add("-" * 68)
    add("")
    add("What the result means:")
    add("")
    add("  01 crashes")
    add("      The DBPF container or the string table is malformed. Nothing")
    add("      about career tuning matters yet. Report this - builds now")
    add("      verify both against EA's own format before writing, so it")
    add("      would mean the verification itself is missing something.")
    add("")
    add("  01 loads, 02 crashes")
    add("      Career level tuning is rejected. Either the resource type id")
    add("      is wrong, or the XML structure is invalid for this patch.")
    add("")
    add("  02 loads, 03 crashes")
    add("      Career track tuning is the problem. Tracks are the first")
    add("      resource that references other tuning by instance id, so a")
    add("      broken reference is the likely cause.")
    add("")
    add("  03 loads, 04 crashes")
    add("      The top-level career resource is the problem.")
    add("")
    add("  Everything loads")
    add("      No crash from tuning. Check whether the career appears under")
    add("      Find a Job. If it does not, the tuning loads but does not")
    add("      register, which is a field-name problem rather than a")
    add("      structural one.")
    add("")
    add("Run the other series too (the folder ending in the opposite of")
    add(f"'{'generic_type' if generic_type else 'career_types'}'). If one")
    add("series loads where the other crashed, the resource type ids are")
    add("the bug and that is a one-line fix.")
    add("")
    add("Report back: the number of the first package that crashed, for")
    add("both series. That is all I need.")
    add("")

    return "\n".join(lines) + "\n"
