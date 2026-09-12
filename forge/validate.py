"""
Post-build checks on a written .package.

Re-reads the file from disk and checks it the way the game will see it, so
a structural bug is caught at build time instead of as a crash at game load
or a UI that silently refuses to open.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .dbpf import DBPFError, Resource, read_package
from .ids import SIMDATA_GROUP, ResourceType
from .simdata import SimDataError, read_simdata
from .stbl import check_stbl

# Tuning type id -> the `i=` attribute its root element must carry.
TUNING_ROOT_TYPE = {
    ResourceType.CAREER: "career",
    ResourceType.CAREER_TRACK: "career_track",
    ResourceType.CAREER_LEVEL: "career_level",
    ResourceType.STATISTIC: "statistic",
    ResourceType.ASPIRATION: "aspiration",
}
ROOT_TYPE_TO_TUNING = {v: k for k, v in TUNING_ROOT_TYPE.items()}

# Instance ids this tool allocates always have the high bit set (EA's are
# small numbers), so any such id in our tuning must be in our package.
OWN_ID_FLOOR = 1 << 63


def validate_package(path: str | Path,
                     expected: list[Resource] | None = None) -> list[str]:
    """
    Return every problem found in the package at `path`; empty means clean.

    Pass `expected` (the resources that were meant to be written) to also
    confirm the file round-trips byte for byte.
    """
    try:
        resources = read_package(path)
    except DBPFError as exc:
        return [f"package does not parse: {exc}"]

    problems: list[str] = []

    if expected is not None:
        on_disk = {r.key: r.data for r in resources}
        for res in expected:
            if res.key not in on_disk:
                problems.append(f"{res.key_string()} missing from written package")
            elif on_disk[res.key] != res.data:
                problems.append(f"{res.key_string()} does not round-trip")
        if len(resources) != len(expected):
            problems.append(
                f"package holds {len(resources)} resources, expected {len(expected)}"
            )

    instances = {r.instance_id for r in resources}
    tuning: dict[int, tuple[Resource, ET.Element]] = {}
    simdata: dict[int, Resource] = {}

    for res in resources:
        if res.type_id == ResourceType.STBL:
            problems.extend(f"{res.key_string()}: {p}" for p in check_stbl(res.data))
        elif res.type_id == ResourceType.SIMDATA:
            simdata[res.instance_id] = res
        elif res.type_id in TUNING_ROOT_TYPE or res.type_id == ResourceType.TUNING:
            root, found = _check_tuning(res, instances)
            problems.extend(f"{res.key_string()}: {p}" for p in found)
            if root is not None:
                tuning[res.instance_id] = (res, root)

    for instance, (res, root) in tuning.items():
        tuning_type = ROOT_TYPE_TO_TUNING.get(root.get("i"))
        if tuning_type not in SIMDATA_GROUP:
            continue
        sd = simdata.get(instance)
        if sd is None:
            problems.append(f"{res.key_string()}: {root.get('i')} tuning has no SimData; "
                            f"the game UI will not see it")
            continue
        problems.extend(f"{sd.key_string()}: {p}"
                        for p in _check_simdata(sd, SIMDATA_GROUP[tuning_type], root.get("n")))

    for instance, sd in simdata.items():
        if instance not in tuning:
            problems.append(f"{sd.key_string()}: SimData has no matching tuning")
    return problems


def _check_tuning(res: Resource, instances: set[int]) -> tuple[ET.Element | None, list[str]]:
    if res.data.startswith(b"\xef\xbb\xbf"):
        return None, ["tuning XML starts with a byte-order mark; the game expects none"]
    try:
        root = ET.fromstring(res.data)
    except ET.ParseError as exc:
        return None, [f"tuning XML does not parse: {exc}"]

    problems: list[str] = []
    if root.tag != "I":
        problems.append(f"tuning root is <{root.tag}>, expected <I>")
    if root.get("s") != str(res.instance_id):
        problems.append(
            f"tuning s={root.get('s')!r} does not match its instance id "
            f"{res.instance_id}"
        )
    want = TUNING_ROOT_TYPE.get(res.type_id)
    if want is not None and root.get("i") != want:
        problems.append(
            f"tuning i={root.get('i')!r} stored under type "
            f"0x{res.type_id:08X}, which holds {want!r} tuning"
        )
    for el in root.iter():
        text = (el.text or "").strip()
        if text.isdigit() and int(text) >= OWN_ID_FLOOR and int(text) not in instances:
            problems.append(
                f"<{el.tag} n={el.get('n')!r}> references {text}, which is not "
                f"in this package"
            )
    return root, problems


def _check_simdata(res: Resource, group: int, tuning_name: str) -> list[str]:
    problems: list[str] = []
    if res.group_id != group:
        problems.append(f"SimData group is 0x{res.group_id:08X}, expected 0x{group:08X}")
    try:
        model, _layout = read_simdata(res.data)
    except (SimDataError, ValueError, IndexError) as exc:
        return problems + [f"SimData does not parse: {exc}"]
    if not model.tables or model.tables[0].name != tuning_name:
        problems.append(
            f"SimData main table is {model.tables[0].name if model.tables else None!r}, "
            f"expected the tuning name {tuning_name!r}"
        )
    return problems
