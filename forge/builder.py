"""
Build orchestration: CareerSpec in, installable mod files out.

Produces, in an output folder named after the mod:
    <mod_key>.package     tuning + string table
    <mod_key>.ts4script   diagnostic script mod
    <mod_key>.spec.json   the spec that produced this build, for re-editing
    BUILD_REPORT.txt      what was made, what to check, how to install
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ea_refs
from .dbpf import Resource, write_package
from .ids import (
    SIMDATA_GROUP, InstanceAllocator, ResourceType, check_collisions,
    stbl_instance,
)
from .schema import CareerSpec
from .script_mod import write_ts4script
from .stbl import StringTable
from .tuning import GeneratedResource, build_all_tuning
from .validate import validate_package

# Map the tuning kind produced by build_all_tuning to its TGI type id.
TYPE_FOR_KIND = {
    "career": ResourceType.CAREER,
    "career_track": ResourceType.CAREER_TRACK,
    "career_level": ResourceType.CAREER_LEVEL,
    "statistic": ResourceType.STATISTIC,
    "aspiration": ResourceType.ASPIRATION,
}


def package_resources(generated: GeneratedResource, alloc: InstanceAllocator,
                      type_id: int | None = None) -> list[Resource]:
    """
    The tuning resource and its SimData twin, which shares the instance id.

    `type_id` overrides the tuning type (bisect uses this); the SimData
    group always follows the real type.
    """
    real_type = TYPE_FOR_KIND.get(generated.kind)
    if real_type is None:
        raise BuildError(f"no TGI type id known for tuning kind {generated.kind!r}")
    instance = alloc.instance(generated.name)
    return [
        Resource(type_id=type_id or real_type, group_id=0,
                 instance_id=instance, data=generated.xml),
        Resource(type_id=ResourceType.SIMDATA, group_id=SIMDATA_GROUP[real_type],
                 instance_id=instance, data=generated.simdata),
    ]


@dataclass
class BuildResult:
    output_dir: Path
    package_path: Path
    script_path: Path
    spec_path: Path
    report_path: Path
    resource_count: int
    string_count: int
    warnings: list[str] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)


class BuildError(Exception):
    """Raised when a career cannot be built."""


def _build_id(spec: CareerSpec) -> str:
    """Short stable-ish id so a player can tell two builds apart."""
    digest = hashlib.sha256(spec.to_json().encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{stamp}-{digest}"


def build_career(spec: CareerSpec, output_root: str | Path,
                 on_progress=None) -> BuildResult:
    """
    Build a complete career mod.

    Validates first and refuses to write anything if the spec is bad, so a
    failed build never leaves half a mod in the output folder.
    """
    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    problems = spec.validate()
    if problems:
        raise BuildError(
            "career spec is not valid:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    warnings: list[str] = []
    output_dir = Path(output_root) / spec.mod_key
    output_dir.mkdir(parents=True, exist_ok=True)

    alloc = InstanceAllocator(spec.mod_key)
    strings = StringTable()

    report("Generating tuning XML and SimData...")
    generated = build_all_tuning(spec, alloc, strings)
    warnings.extend(generated.notes)

    collisions = check_collisions(alloc)
    if collisions:
        raise BuildError("instance id collisions:\n" + "\n".join(collisions))

    resources: list[Resource] = []
    for item in generated.resources:
        resources.extend(package_resources(item, alloc))

    report(f"Building string table ({len(strings)} strings)...")
    resources.append(Resource(
        type_id=ResourceType.STBL,
        group_id=0,
        instance_id=stbl_instance(spec.mod_key),
        data=strings.to_bytes(),
    ))

    report("Writing .package...")
    package_path = write_package(resources, output_dir / f"{spec.mod_key}.package")

    report("Verifying .package...")
    package_problems = validate_package(package_path, expected=resources)
    if package_problems:
        package_path.unlink()
        raise BuildError(
            "written package failed verification and was deleted:\n"
            + "\n".join(f"  - {p}" for p in package_problems)
        )

    build_id = _build_id(spec)
    report("Writing .ts4script...")
    script_path = write_ts4script(
        spec, alloc.instance("career"), build_id,
        output_dir / f"{spec.mod_key}.ts4script",
    )

    spec_path = output_dir / f"{spec.mod_key}.spec.json"
    spec_path.write_text(spec.to_json(), encoding="utf-8")

    manifest = {
        "career": spec.name,
        "mod_key": spec.mod_key,
        "build_id": build_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branches": [
            {"name": b.name, "branches_at": b.branches_at,
             "levels": len(b.levels)}
            for b in spec.branches
        ],
        "resources": [
            {"key": r.key_string(), "type": f"0x{r.type_id:08X}",
             "bytes": len(r.data)}
            for r in resources
        ],
        "tuning_names": {
            name: f"0x{inst:016X}" for name, inst in alloc.issued.items()
        },
    }
    (output_dir / f"{spec.mod_key}.manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    total_levels = sum(len(b.levels) for b in spec.branches)
    if total_levels < 5:
        warnings.append(
            f"Career has only {total_levels} levels. Base game careers have "
            f"10; short careers feel abrupt."
        )
    if len(spec.branches) == 1:
        warnings.append(
            "Career has no branches. That is valid, but most careers split "
            "into two specialisations near the top."
        )

    report("Writing build report...")
    report_path = output_dir / "BUILD_REPORT.txt"
    report_path.write_text(
        _render_report(spec, manifest, resources, strings, warnings),
        encoding="utf-8",
    )

    report(f"Done. {len(resources)} resources written.")
    return BuildResult(
        output_dir=output_dir,
        package_path=package_path,
        script_path=script_path,
        spec_path=spec_path,
        report_path=report_path,
        resource_count=len(resources),
        string_count=len(strings),
        warnings=warnings,
        manifest=manifest,
    )


def _render_report(spec: CareerSpec, manifest: dict,
                   resources: list[Resource], strings: StringTable,
                   warnings: list[str]) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 70)
    add(f"  {spec.name}")
    add(f"  built by Sims 4 Career Forge - {manifest['generated_utc']}")
    add("=" * 70)
    add("")
    add(spec.description)
    add("")

    add("-" * 70)
    add("CAREER STRUCTURE")
    add("-" * 70)
    for branch in spec.branches:
        origin = ("base track" if branch.branches_at is None
                  else f"branches from level {branch.branches_at}")
        add("")
        add(f"{branch.name}  ({origin})")
        for level in sorted(branch.levels, key=lambda l: l.level):
            skills = ", ".join(
                f"{k} {v}" for k, v in sorted(level.required_skills.items())
            ) or "none"
            days = len(level.work_days)
            add(f"  {level.level:>2}. {level.title:<34} "
                f"${level.pay_per_hour:>5}/hr  "
                f"{days}d  skills: {skills}")
    add("")

    add("-" * 70)
    add("FILES")
    add("-" * 70)
    add(f"{spec.mod_key}.package        {len(resources)} resources, "
        f"{len(strings)} strings")
    add(f"{spec.mod_key}.ts4script      diagnostic console command")
    add(f"{spec.mod_key}.spec.json      edit and rebuild to tweak")
    add(f"{spec.mod_key}.manifest.json  every resource id, for conflict checks")
    add("")

    add("-" * 70)
    add("INSTALL")
    add("-" * 70)
    add("1. Copy the .package AND the .ts4script into your Mods folder:")
    add("     Windows  Documents\\Electronic Arts\\The Sims 4\\Mods")
    add("     macOS    ~/Documents/Electronic Arts/The Sims 4/Mods")
    add("   They can sit in a subfolder, but no deeper than one level down.")
    add("")
    add("2. In game: Game Options > Other > enable BOTH")
    add("     'Enable Custom Content and Mods'")
    add("     'Script Mods Allowed'")
    add("   Then restart the game fully. Script mods only load at startup.")
    add("")
    add("3. Delete localthumbcache.package from the The Sims 4 folder. Stale")
    add("   cache is the single most common reason a new mod does not appear.")
    add("")

    add("-" * 70)
    add("VERIFYING IT WORKED")
    add("-" * 70)
    add("Open the console with Ctrl+Shift+C and run:")
    add(f"     forge.{spec.mod_key}")
    add("")
    add("  Command responds     -> script loaded. If the career is still")
    add("                          missing, the .package tuning did not")
    add("                          resolve; see TROUBLESHOOTING below.")
    add("  Unknown command      -> the .ts4script is not loading. Check that")
    add("                          script mods are enabled and the file is")
    add("                          not nested too deep.")
    add("")
    add("Then have an adult sim use a phone or computer > Find a Job.")
    add(f"The career appears as '{spec.name}'.")
    add("")

    add("-" * 70)
    add("TROUBLESHOOTING")
    add("-" * 70)
    add("The tuning and SimData follow EA's own careers as of game version")
    add(f"{ea_refs.GAME_VERSION}, and only reference base-game content. A")
    add("later patch can still rename fields. If the career misbehaves,")
    add("check the game's exception log, which names the tuning file and")
    add("field at fault:")
    add("  Documents/Electronic Arts/The Sims 4/lastException*.txt")
    add("")

    if warnings:
        add("-" * 70)
        add("NOTES")
        add("-" * 70)
        for warning in warnings:
            add(f"  - {warning}")
        add("")

    return "\n".join(lines) + "\n"
