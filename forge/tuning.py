"""
Tuning XML and SimData generation for a career.

The Sims 4 tuning format uses a small element vocabulary:
    <I>  instance (a whole tuning file)   attrs: c=class i=type m=module
                                                 n=name s=instance-id
    <T>  a single value
    <L>  a list
    <U>  a tuple / structured block
    <V>  a variant, with t= selecting the case
    <E>  an enum value

A career is several resources that reference each other by instance id:
    Career          the entry the player picks; points at its start track
    CareerTrack     one ladder: name, icon, levels, branch tracks
    CareerLevel     one rung: title, pay, schedule, performance, promotion gate
    Statistic       the career's work-performance meter
    AspirationCareer  a level's promotion gate (skill objectives), if any

Every field name and structure below comes from the game itself (version
1.127): the tunable definitions in careers/career_tuning.py, and EA's own
career_Adult_Writer tuning decoded from SimulationDeltaBuild0.package.
References to EA content (go-to-work interaction, notifications, tones) are
in ea_refs.py, all base game.

Each resource also gets SimData (see career_simdata.py). The UI reads the
career picker and career panel from SimData, not from the XML.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.dom import minidom

from . import ea_refs
from .career_simdata import (
    aspiration_simdata, career_simdata, level_simdata, statistic_simdata,
    track_simdata,
)
from .ids import InstanceAllocator, ResourceType
from .schema import CareerSpec, CareerBranch, CareerLevel, WEEKDAYS
from .stbl import StringTable

PERFORMANCE_MIN = -100
PERFORMANCE_MAX = 100


@dataclass
class GeneratedResource:
    kind: str  # career | career_track | career_level | statistic | aspiration
    name: str  # resource name within the mod, fed to InstanceAllocator
    xml: bytes
    simdata: bytes


@dataclass
class CareerResources:
    resources: list[GeneratedResource] = field(default_factory=list)
    # Things the player should know, e.g. skill requirements the game can't
    # enforce with base-game content.
    notes: list[str] = field(default_factory=list)


def _pretty(element: ET.Element) -> bytes:
    """Serialise an element to indented UTF-8 XML."""
    rough = ET.tostring(element, encoding="utf-8")
    parsed = minidom.parseString(rough)
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8")
    # minidom leaves blank lines where text nodes were; strip them.
    lines = [ln for ln in pretty.split(b"\n") if ln.strip()]
    return b"\n".join(lines) + b"\n"


def _tuning_root(class_name: str, type_name: str, module: str,
                 name: str, instance: int) -> ET.Element:
    """Create an <I> tuning instance root element."""
    return ET.Element("I", {
        "c": class_name,
        "i": type_name,
        "m": module,
        "n": name,
        "s": str(instance),
    })


def _value(parent: ET.Element, name: str | None, value) -> ET.Element:
    node = ET.SubElement(parent, "T", {"n": name} if name else {})
    node.text = str(value)
    return node


def _enum(parent: ET.Element, name: str, value: str) -> ET.Element:
    node = ET.SubElement(parent, "E", {"n": name})
    node.text = value
    return node


def _string(key: int) -> str:
    """Localized string keys are written as hex, as in EA's tuning."""
    return f"0x{key:08X}"


def _ref_list(parent: ET.Element, name: str, ids: list[int]) -> ET.Element:
    node = ET.SubElement(parent, "L", {"n": name})
    for i in ids:
        _value(node, None, i)
    return node


def _image_key(instance: int) -> str:
    return f"{ResourceType.PNG:08x}:00000000:{instance:016x}"


def _fragment(xml: str) -> ET.Element:
    """Parse one of ea_refs' XML blocks, dropping layout whitespace."""
    element = ET.fromstring(xml.strip())
    for el in element.iter():
        if el.text is not None and not el.text.strip():
            el.text = None
        if el.tail is not None and not el.tail.strip():
            el.tail = None
    return element


def _enabled(parent: ET.Element, name: str) -> ET.Element:
    return ET.SubElement(parent, "V", {"n": name, "t": "enabled"})


def _buff(parent: ET.Element, name: str, buff: tuple[int, int]) -> None:
    buff_id, reason = buff
    block = ET.SubElement(_enabled(parent, name), "U", {"n": "enabled"})
    _value(ET.SubElement(block, "V", {"n": "buff_reason", "t": "enabled"}), "enabled", _string(reason))
    _value(block, "buff_type", buff_id)


def _time_of_day(parent: ET.Element, name: str, hour: int,
                 with_minute: bool = True) -> None:
    block = ET.SubElement(parent, "U", {"n": name})
    _value(block, "hour", hour)
    if with_minute:  # EA writes it for wakeup_time but not schedule start_time
        _value(block, "minute", 0)


# ---------------------------------------------------------------------------
# Performance statistic
# ---------------------------------------------------------------------------

def build_performance_stat(alloc: InstanceAllocator) -> GeneratedResource:
    res_name = "performance"
    name = alloc.tuning_name(res_name)
    root = _tuning_root("Statistic", "statistic", "statistics.statistic",
                        name, alloc.instance(res_name))
    initial = ET.SubElement(root, "U", {"n": "initial_tuning"})
    _value(initial, "_initial_value", 0)
    _value(initial, "_use_stat_value_on_init", True)
    _value(root, "max_value_tuning", PERFORMANCE_MAX)
    _value(root, "min_value_tuning", PERFORMANCE_MIN)
    return GeneratedResource("statistic", res_name, _pretty(root),
                             statistic_simdata(name, PERFORMANCE_MIN, PERFORMANCE_MAX))


# ---------------------------------------------------------------------------
# Promotion gate
# ---------------------------------------------------------------------------

def build_aspiration(level: CareerLevel, branch: CareerBranch,
                     alloc: InstanceAllocator,
                     notes: list[str]) -> GeneratedResource | None:
    """The skill objectives a Sim must meet to be promoted out of `level`."""
    objectives: list[int] = []
    for skill, needed in sorted(level.required_skills.items()):
        found = ea_refs.skill_objective(skill, int(needed))
        where = f"{branch.name} level {level.level} ({level.title})"
        if found is None:
            notes.append(f"{where}: {skill} {needed} is not enforced; the base "
                         f"game has no {skill} objective at or below level {needed}.")
            continue
        objective, actual = found
        if actual != int(needed):
            notes.append(f"{where}: {skill} {needed} is enforced as {skill} "
                         f"{actual}, the nearest level the base game has an "
                         f"objective for.")
        objectives.append(objective)
    if not objectives:
        return None

    res_name = f"aspiration_{branch.key}_{level.level}"
    name = alloc.tuning_name(res_name)
    root = _tuning_root("AspirationCareer", "aspiration",
                        "aspirations.aspiration_tuning", name, alloc.instance(res_name))
    _ref_list(root, "objectives", objectives)
    return GeneratedResource("aspiration", res_name, _pretty(root),
                             aspiration_simdata(name, objectives))


# ---------------------------------------------------------------------------
# Career level
# ---------------------------------------------------------------------------

def build_level_tuning(level: CareerLevel, branch: CareerBranch, user_level: int,
                       alloc: InstanceAllocator, strings: StringTable,
                       performance_stat: int,
                       aspiration: int | None) -> GeneratedResource:
    """Build one CareerLevel. `user_level` is its 1-based rung in the career."""
    res_name = f"career_level_{branch.key}_{level.level}"
    name = alloc.tuning_name(res_name)
    instance = alloc.instance(res_name)

    title_key = strings.add(alloc.string_key(f"{res_name}_title"), level.title)
    desc_key = strings.add(alloc.string_key(f"{res_name}_desc"),
                           level.description or level.title)
    pacing_level = min(max(user_level, 1), 10)
    pto = ea_refs.PTO_PER_DAY[pacing_level]

    root = _tuning_root("CareerLevel", "career_level", "careers.career_tuning",
                        name, instance)
    if aspiration is not None:
        _value(root, "aspiration", aspiration)
    _ref_list(root, "end_of_day_loot", ea_refs.END_OF_DAY_LOOT)
    _ref_list(root, "loot_on_join", ea_refs.LOOT_ON_JOIN)

    pay = ET.SubElement(root, "V", {"n": "pay_type", "t": "simoleons_per_hour"})
    _value(pay, "simoleons_per_hour", level.pay_per_hour)

    metrics = ET.SubElement(root, "U", {"n": "performance_metrics"})
    _value(metrics, "base_performance", ea_refs.BASE_PERFORMANCE[pacing_level])
    _value(metrics, "daily_assignment_performance", ea_refs.DAILY_ASSIGNMENT_PERFORMANCE)
    _value(metrics, "missed_work_penalty", ea_refs.MISSED_WORK_PENALTY)
    _value(root, "performance_stat", performance_stat)

    sting = ET.SubElement(_enabled(root, "promotion_audio_sting"), "U", {"n": "enabled"})
    _value(sting, "audio", ea_refs.PROMOTION_AUDIO)
    _value(root, "pto_per_day", pto)
    _value(root, "title", _string(title_key))
    _value(root, "title_description", _string(desc_key))

    tones = ET.SubElement(_enabled(root, "tones"), "U", {"n": "enabled"})
    default = ET.SubElement(ET.SubElement(tones, "L", {"n": "default_action_list"}), "U")
    _value(default, "default_action", ea_refs.TONE_DEFAULT)
    _value(tones, "leave_work_early", ea_refs.TONE_LEAVE_EARLY)
    _ref_list(tones, "optional_actions", ea_refs.TONES)

    _time_of_day(root, "wakeup_time", (level.start_hour - 2) % 24)
    outfit = ET.SubElement(root, "U", {"n": "work_outfit"})
    _value(ET.SubElement(outfit, "V", {"n": "outfit_generator", "t": "enabled"}),
           "enabled", ea_refs.WORK_OUTFIT)

    schedule = ET.SubElement(root, "U", {"n": "work_schedule"})
    entry = ET.SubElement(ET.SubElement(schedule, "L", {"n": "schedule_entries"}), "U")
    days = ET.SubElement(entry, "U", {"n": "days_available"})
    wanted = {d.lower() for d in level.work_days}
    for index, day in enumerate(WEEKDAYS):
        _value(days, f"{index} {day.upper()}", day in wanted)
    _value(entry, "duration", level.hours_per_day)
    _time_of_day(entry, "start_time", level.start_hour, with_minute=False)

    simdata = level_simdata(
        name, title=title_key, title_description=desc_key,
        pay_per_hour=level.pay_per_hour, work_days=level.work_days,
        start_hour=level.start_hour, hours=level.hours_per_day,
        performance_stat=performance_stat, aspiration=aspiration or 0,
        pto_per_day=pto,
    )
    return GeneratedResource("career_level", res_name, _pretty(root), simdata)


# ---------------------------------------------------------------------------
# Career track
# ---------------------------------------------------------------------------

def build_track_tuning(branch: CareerBranch, spec: CareerSpec,
                       alloc: InstanceAllocator, strings: StringTable,
                       level_ids: list[int],
                       branch_ids: list[int]) -> GeneratedResource:
    """Build one CareerTrack. The base track carries the career's name."""
    res_name = f"career_track_{branch.key}"
    name = alloc.tuning_name(res_name)
    is_base = branch.branches_at is None

    shown_name = spec.name if is_base else branch.name
    description = (spec.description if is_base else branch.description) or spec.description or shown_name
    name_key = strings.add(alloc.string_key(f"{res_name}_name"), shown_name)
    # Must be a different key from career_name, even with the same text;
    # the game logs an error otherwise.
    neutral_key = strings.add(alloc.string_key(f"{res_name}_name_neutral"), shown_name)
    desc_key = strings.add(alloc.string_key(f"{res_name}_desc"), description)
    icon, icon_high_res = ea_refs.pick_icon(spec.name, spec.icon_hint, branch.name)

    root = _tuning_root("TunableCareerTrack", "career_track", "careers.career_tuning",
                        name, alloc.instance(res_name))
    _value(root, "active_assignment_amount", 0)
    if branch_ids:
        _ref_list(root, "branches", branch_ids)
    _value(root, "busy_time_situation_picker_tooltip", _string(ea_refs.BUSY_TOOLTIP))
    _value(root, "career_description", _string(desc_key))
    _ref_list(root, "career_levels", level_ids)
    _value(root, "career_name", _string(name_key))
    _value(root, "career_name_gender_neutral", _string(neutral_key))
    goodbye = ET.SubElement(root, "V", {"n": "goodbye_notification", "t": "reference"})
    _value(goodbye, "reference", ea_refs.GOODBYE_NOTIFICATION)
    _value(root, "icon", _image_key(icon))
    _value(root, "icon_high_res", _image_key(icon_high_res))
    _value(root, "image", _image_key(ea_refs.TRACK_IMAGE))
    knowledge = ET.SubElement(_enabled(root, "knowledge_notification"),
                              "V", {"n": "enabled", "t": "reference"})
    _value(knowledge, "reference", ea_refs.KNOWLEDGE_NOTIFICATION)
    _value(root, "show_now_hiring_string", True)

    simdata = track_simdata(
        name, career_name=name_key, career_name_neutral=neutral_key,
        description=desc_key, busy_tooltip=ea_refs.BUSY_TOOLTIP,
        levels=level_ids, branches=branch_ids, icon=icon,
        icon_high_res=icon_high_res, image=ea_refs.TRACK_IMAGE,
        dds_type=ResourceType.DDS,
    )
    return GeneratedResource("career_track", res_name, _pretty(root), simdata)


# ---------------------------------------------------------------------------
# Career
# ---------------------------------------------------------------------------

def build_career_tuning(spec: CareerSpec, alloc: InstanceAllocator,
                        strings: StringTable, start_track: int) -> GeneratedResource:
    """Build the top-level Career."""
    res_name = "career"
    name = alloc.tuning_name(res_name)
    company_key = strings.add(alloc.string_key("company_name"),
                              spec.company_name or spec.name)

    root = _tuning_root("Career", "career", "careers.career_tuning",
                        name, alloc.instance(res_name))
    _value(root, "career_affordance", ea_refs.CAREER_AFFORDANCE)
    root.append(_fragment(ea_refs.AVAILABILITY_TESTS))
    _enum(root, "career_category", "Work")
    location = ET.SubElement(root, "V", {"n": "career_location", "t": "company"})
    names = ET.SubElement(ET.SubElement(location, "U", {"n": "company"}), "L", {"n": "company_names"})
    _value(names, None, _string(company_key))
    root.append(_fragment(ea_refs.CAREER_MESSAGES))
    _value(root, "career_rabbit_hole", ea_refs.CAREER_RABBIT_HOLE)
    _value(root, "days_to_level_loss", 2)
    _buff(root, "demotion_buff", ea_refs.DEMOTION_BUFF)
    _buff(root, "fired_buff", ea_refs.FIRED_BUFF)
    _value(root, "go_home_to_work_affordance", ea_refs.GO_HOME_TO_WORK_AFFORDANCE)
    _value(root, "initial_delay", 600)
    _value(root, "initial_pto", 3)
    _buff(root, "promotion_buff", ea_refs.PROMOTION_BUFF)
    root.append(_fragment(ea_refs.QUITTABLE_DATA))
    _value(root, "start_track", start_track)

    return GeneratedResource("career", res_name, _pretty(root),
                             career_simdata(name, start_track))


# ---------------------------------------------------------------------------
# Whole-career assembly
# ---------------------------------------------------------------------------

def build_all_tuning(spec: CareerSpec, alloc: InstanceAllocator,
                     strings: StringTable) -> CareerResources:
    """Build every resource for a career, tuning and SimData together."""
    out = CareerResources()
    stat = build_performance_stat(alloc)
    out.resources.append(stat)
    performance_stat = alloc.instance(stat.name)

    base = spec.base_branch
    base_levels = len(base.levels) if base else 0
    children = [b for b in spec.branches if b.branches_at is not None]

    for branch in spec.branches:
        first_user_level = 1 if branch.branches_at is None else base_levels + 1
        level_ids: list[int] = []
        for index, level in enumerate(sorted(branch.levels, key=lambda l: l.level)):
            aspiration = build_aspiration(level, branch, alloc, out.notes)
            if aspiration is not None:
                out.resources.append(aspiration)
            generated = build_level_tuning(
                level, branch, first_user_level + index, alloc, strings,
                performance_stat,
                alloc.instance(aspiration.name) if aspiration else None,
            )
            out.resources.append(generated)
            level_ids.append(alloc.instance(generated.name))

        branch_ids = ([alloc.instance(f"career_track_{c.key}") for c in children]
                      if branch is base else [])
        out.resources.append(build_track_tuning(
            branch, spec, alloc, strings, level_ids, branch_ids))

    start_track = alloc.instance(f"career_track_{base.key}")
    out.resources.append(build_career_tuning(spec, alloc, strings, start_track))
    return out


def verify_against_game(reference_xml: bytes, generated_xml: bytes) -> list[str]:
    """
    Compare generated tuning's field names against a reference extracted from
    the game, and report fields that do not appear in the reference.
    """
    def field_names(blob: bytes) -> set[str]:
        try:
            root = ET.fromstring(blob)
        except ET.ParseError as exc:
            raise ValueError(f"could not parse XML: {exc}") from exc
        return {
            el.attrib["n"]
            for el in root.iter()
            if "n" in el.attrib
        }

    reference = field_names(reference_xml)
    generated = field_names(generated_xml)

    unknown = sorted(generated - reference)
    problems = []
    for name in unknown:
        problems.append(
            f"field {name!r} is not present in the reference tuning; "
            f"the game may ignore it"
        )
    missing = sorted(reference - generated)
    if missing:
        problems.append(
            f"reference has {len(missing)} field(s) this tool does not emit: "
            f"{', '.join(missing[:10])}"
            + (" ..." if len(missing) > 10 else "")
        )
    return problems
