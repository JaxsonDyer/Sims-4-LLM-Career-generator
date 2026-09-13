"""
The career specification - the contract between the language model and the
package builder.

The model never writes XML or binary. It fills in this structure, we validate
it hard, and only then do we generate resources. That boundary is what keeps
a creative-but-careless model from producing a package that crashes a save.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any

# Work days are bit flags in tuning. Order matters and matches TS4's enum.
WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday"]

# Skills the game ships with, used to validate promotion requirements.
# Not exhaustive across every pack, but covers base game plus common packs.
KNOWN_SKILLS = {
    "charisma", "comedy", "cooking", "fishing", "fitness", "gardening",
    "gourmet_cooking", "guitar", "handiness", "logic", "mischief", "mixology",
    "painting", "photography", "piano", "programming", "rocket_science",
    "video_gaming", "violin", "writing", "baking", "wellness", "parenting",
    "dancing", "singing", "veterinarian", "flower_arranging", "pipe_organ",
    "archaeology", "juice_fizzing", "media_production", "robotics",
    "rock_climbing", "knitting", "cross_stitch", "entrepreneur",
}

# Pack skills discovered by scanning the installed game (game_content).
# Extends the allowed skill set process-wide so specs drafted with pack
# skills also validate.
_extra_skills: set[str] = set()


def set_extra_skills(names: set[str] | list[str] | None) -> None:
    """Declare pack skills available in this session (from a game scan)."""
    global _extra_skills
    _extra_skills = {str(n).lower() for n in (names or set())}


def known_skills() -> set[str]:
    """The full allowed skill set: base game plus registered pack skills."""
    return KNOWN_SKILLS | _extra_skills

MAX_LEVELS = 10
MAX_BRANCHES = 3


class SpecError(ValueError):
    """Raised when a career specification is invalid."""


def _slug(text: str) -> str:
    """Make a safe identifier fragment out of free text."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return cleaned or "unnamed"


@dataclass
class CareerLevel:
    """One rung of a career ladder."""

    level: int
    title: str
    pay_per_hour: int
    description: str = ""
    # Promotion gates. Skill names are validated against KNOWN_SKILLS.
    required_skills: dict[str, int] = field(default_factory=dict)
    work_days: list[str] = field(default_factory=lambda: [
        "monday", "tuesday", "wednesday", "thursday", "friday"
    ])
    start_hour: int = 9
    hours_per_day: int = 8
    promotion_message: str = ""
    # Paid time off accrued per work day, 0-1. None = use EA's pacing table.
    pto_per_day: float | None = None

    def validate(self, ctx: str) -> list[str]:
        problems: list[str] = []
        where = f"{ctx} level {self.level}"

        if not 1 <= self.level <= MAX_LEVELS:
            problems.append(f"{where}: level must be 1-{MAX_LEVELS}")
        if not self.title.strip():
            problems.append(f"{where}: title is empty")
        if len(self.title) > 60:
            problems.append(f"{where}: title is over 60 characters")
        if self.pay_per_hour < 1:
            problems.append(f"{where}: pay must be at least 1/hour")
        if self.pay_per_hour > 10000:
            problems.append(
                f"{where}: pay of {self.pay_per_hour}/hour is absurd; "
                f"cap is 10000"
            )
        if not 0 <= self.start_hour <= 23:
            problems.append(f"{where}: start_hour must be 0-23")
        if not 1 <= self.hours_per_day <= 12:
            problems.append(f"{where}: hours_per_day must be 1-12")
        if self.pto_per_day is not None and not 0 <= self.pto_per_day <= 1:
            problems.append(
                f"{where}: pto_per_day must be 0-1 (a fraction of a vacation "
                f"day earned per work day), got {self.pto_per_day}"
            )
        if not self.work_days:
            problems.append(f"{where}: needs at least one work day")
        for day in self.work_days:
            if day.lower() not in WEEKDAYS:
                problems.append(f"{where}: {day!r} is not a weekday")
        for skill, value in self.required_skills.items():
            if skill.lower() not in known_skills():
                problems.append(
                    f"{where}: unknown skill {skill!r}. Known skills include "
                    f"charisma, logic, fitness, painting, writing."
                )
            if not 1 <= int(value) <= 10:
                problems.append(
                    f"{where}: skill {skill!r} level {value} out of range 1-10"
                )
        return problems


@dataclass
class CareerBranch:
    """
    A track within a career.

    Every career has at least one branch (the base track). Careers that
    branch at a given level have additional branches that take over from
    that point.
    """

    name: str
    description: str = ""
    # Level at which this branch splits off the base track. None = base track.
    branches_at: int | None = None
    levels: list[CareerLevel] = field(default_factory=list)

    @property
    def key(self) -> str:
        return _slug(self.name).lower()

    def validate(self, ctx: str) -> list[str]:
        problems: list[str] = []
        where = f"{ctx} branch {self.name!r}"

        if not self.name.strip():
            problems.append(f"{ctx}: a branch has an empty name")
        if not self.levels:
            problems.append(f"{where}: has no levels")
            return problems
        if len(self.levels) > MAX_LEVELS:
            problems.append(
                f"{where}: {len(self.levels)} levels exceeds the cap of {MAX_LEVELS}"
            )

        seen = set()
        for lvl in self.levels:
            problems.extend(lvl.validate(where))
            if lvl.level in seen:
                problems.append(f"{where}: duplicate level number {lvl.level}")
            seen.add(lvl.level)

        ordered = sorted(self.levels, key=lambda l: l.level)
        expected = list(range(ordered[0].level, ordered[0].level + len(ordered)))
        if [l.level for l in ordered] != expected:
            problems.append(
                f"{where}: level numbers must be consecutive, got "
                f"{[l.level for l in ordered]}"
            )

        # Pay should climb. A career where level 6 pays less than level 5 is
        # almost always a model slip rather than a deliberate choice.
        for prev, nxt in zip(ordered, ordered[1:]):
            if nxt.pay_per_hour < prev.pay_per_hour:
                problems.append(
                    f"{where}: pay drops from {prev.pay_per_hour} at level "
                    f"{prev.level} to {nxt.pay_per_hour} at level {nxt.level}"
                )
        return problems


@dataclass
class CareerSpec:
    """A complete career, ready to build."""

    name: str
    description: str
    branches: list[CareerBranch] = field(default_factory=list)
    author: str = "CareerForge"
    # Short machine key. Derived from name if not given.
    mod_key: str = ""
    icon_hint: str = ""
    # Employer shown in join/quit notifications. Falls back to the name.
    company_name: str = ""
    # "ea" = keyword-picked EA icons; "custom" = AI-generated art styled
    # after EA's own icons. The player chooses this; the model never does.
    icon_mode: str = "ea"

    def __post_init__(self) -> None:
        if not self.mod_key:
            self.mod_key = _slug(self.name).lower()

    @property
    def base_branch(self) -> CareerBranch | None:
        for branch in self.branches:
            if branch.branches_at is None:
                return branch
        return self.branches[0] if self.branches else None

    def validate(self) -> list[str]:
        """Return every problem found. Empty list means the spec is buildable."""
        problems: list[str] = []
        ctx = f"career {self.name!r}"

        if not self.name.strip():
            problems.append("career has no name")
        if len(self.name) > 50:
            problems.append(f"{ctx}: name is over 50 characters")
        if self.icon_mode not in ("ea", "custom"):
            problems.append(
                f"{ctx}: icon_mode must be 'ea' or 'custom', "
                f"got {self.icon_mode!r}"
            )
        if not re.fullmatch(r"[a-z0-9_]+", self.mod_key or ""):
            problems.append(
                f"{ctx}: mod_key {self.mod_key!r} must be lowercase letters, "
                f"digits and underscores only"
            )
        if not self.branches:
            problems.append(f"{ctx}: has no branches; need at least one track")
            return problems
        if len(self.branches) > MAX_BRANCHES:
            problems.append(
                f"{ctx}: {len(self.branches)} branches exceeds the cap of "
                f"{MAX_BRANCHES}"
            )

        base_count = sum(1 for b in self.branches if b.branches_at is None)
        if base_count == 0:
            problems.append(
                f"{ctx}: no base track. Exactly one branch must have "
                f"branches_at = null."
            )
        elif base_count > 1:
            problems.append(
                f"{ctx}: {base_count} branches claim to be the base track; "
                f"only one may have branches_at = null"
            )

        keys = [b.key for b in self.branches]
        if len(set(keys)) != len(keys):
            problems.append(f"{ctx}: two branches resolve to the same key: {keys}")

        base = self.base_branch
        for branch in self.branches:
            problems.extend(branch.validate(ctx))
            if branch.branches_at is not None and base and base.levels:
                # In the game a career only branches after the last level of
                # its base track, and branch levels carry on from there.
                top = max(l.level for l in base.levels)
                if branch.branches_at != top:
                    problems.append(
                        f"{ctx} branch {branch.name!r}: branches_at is "
                        f"{branch.branches_at}, but branches must split off "
                        f"after the base track's last level ({top})"
                    )
                elif branch.levels and min(l.level for l in branch.levels) != top + 1:
                    problems.append(
                        f"{ctx} branch {branch.name!r}: levels must continue "
                        f"from {top + 1}, got {min(l.level for l in branch.levels)}"
                    )
        return problems

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CareerSpec":
        """
        Build a spec from raw parsed JSON.

        Tolerant of the shapes a model tends to produce: missing optional
        keys, a flat `levels` list instead of branches, string numbers.
        """
        if not isinstance(data, dict):
            raise SpecError(f"expected a JSON object, got {type(data).__name__}")

        def as_int(value: Any, default: int) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_float_opt(value) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def build_level(raw: dict[str, Any], fallback_index: int) -> CareerLevel:
            if not isinstance(raw, dict):
                raise SpecError(f"a career level was {type(raw).__name__}, not an object")
            skills_raw = raw.get("required_skills") or {}
            if isinstance(skills_raw, list):
                # Model sometimes emits [{"skill": "logic", "level": 3}]
                skills = {
                    str(item.get("skill", "")).lower(): as_int(item.get("level"), 1)
                    for item in skills_raw
                    if isinstance(item, dict) and item.get("skill")
                }
            elif isinstance(skills_raw, dict):
                skills = {str(k).lower(): as_int(v, 1) for k, v in skills_raw.items()}
            else:
                skills = {}

            days = raw.get("work_days")
            if isinstance(days, str):
                days = [d.strip().lower() for d in days.split(",") if d.strip()]
            elif isinstance(days, list):
                days = [str(d).strip().lower() for d in days]
            else:
                days = ["monday", "tuesday", "wednesday", "thursday", "friday"]

            return CareerLevel(
                level=as_int(raw.get("level"), fallback_index),
                title=str(raw.get("title", "")).strip(),
                pay_per_hour=as_int(raw.get("pay_per_hour"), 20),
                description=str(raw.get("description", "")).strip(),
                required_skills=skills,
                work_days=days,
                start_hour=as_int(raw.get("start_hour"), 9),
                hours_per_day=as_int(raw.get("hours_per_day"), 8),
                promotion_message=str(raw.get("promotion_message", "")).strip(),
                pto_per_day=as_float_opt(raw.get("pto_per_day")),
            )

        raw_branches = data.get("branches")
        branches: list[CareerBranch] = []

        if not raw_branches and data.get("levels"):
            # Flat career with no explicit branching.
            branches.append(
                CareerBranch(
                    name=str(data.get("name", "Career")),
                    description=str(data.get("description", "")),
                    branches_at=None,
                    levels=[
                        build_level(l, i + 1)
                        for i, l in enumerate(data.get("levels") or [])
                    ],
                )
            )
        else:
            for raw in raw_branches or []:
                if not isinstance(raw, dict):
                    raise SpecError("a branch entry was not a JSON object")
                at = raw.get("branches_at")
                branches.append(
                    CareerBranch(
                        name=str(raw.get("name", "")).strip(),
                        description=str(raw.get("description", "")).strip(),
                        branches_at=None if at in (None, "", "null") else as_int(at, 1),
                        levels=[
                            build_level(l, i + 1)
                            for i, l in enumerate(raw.get("levels") or [])
                        ],
                    )
                )

        return cls(
            name=str(data.get("name", "")).strip(),
            description=str(data.get("description", "")).strip(),
            branches=branches,
            author=str(data.get("author") or "CareerForge").strip(),
            mod_key=str(data.get("mod_key", "")).strip().lower(),
            icon_hint=str(data.get("icon_hint", "")).strip(),
            company_name=str(data.get("company_name") or "").strip(),
            icon_mode=str(data.get("icon_mode") or "ea").strip().lower(),
        )

    @classmethod
    def from_json(cls, text: str) -> "CareerSpec":
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise SpecError(f"model did not return valid JSON: {exc}") from exc


# The schema description handed to the model. Kept close to the dataclasses
# above so the two cannot drift apart unnoticed.
JSON_SCHEMA_HINT = """{
  "name": "string, the career's display name, under 50 chars",
  "description": "string, 1-2 sentences shown when choosing the career",
  "mod_key": "lowercase_snake_case identifier, letters/digits/underscore only",
  "icon_hint": "short phrase describing a fitting icon",
  "company_name": "string, the employer's name, e.g. 'Brindleton Bay Labs'",
  "branches": [
    {
      "name": "string, branch name",
      "description": "string",
      "branches_at": null,
      "levels": [
        {
          "level": 1,
          "title": "string, job title at this level",
          "pay_per_hour": 15,
          "description": "string, 1 sentence",
          "required_skills": {"logic": 2},
          "work_days": ["monday","tuesday","wednesday","thursday","friday"],
          "start_hour": 9,
          "hours_per_day": 8,
          "pto_per_day": 0.25,
          "promotion_message": "string shown on promotion to this level"
        }
      ]
    }
  ]
}"""
