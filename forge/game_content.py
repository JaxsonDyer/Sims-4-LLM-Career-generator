"""
Installed-game content discovery: which packs you own and what they add.

Skill-based promotion gates point at EA's own objective tuning
(objective_Skill_<Skill>_LevelNN). The base-game objectives are hardcoded in
ea_refs.py, but pack skills (robotics, entrepreneur, ...) live in the pack's
own packages, and EA assigns instance ids by hand rather than by hash, so
the only way to reference them is to read them out of an installed game.

Retail packages strip the XML tuning and ship SimData only, but SimData
keeps the tuning's name in its header, so the scan is: walk every package,
pull index entries whose type/group match EA's objective SimData, and look
for "objective_Skill_" names in the payload. That takes ~17s across a full
install, so the result is cached in the config folder and only rescanned on
demand.
"""

from __future__ import annotations

import json
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from .dbpf import iter_index, read_resource_at

# EA ships objective tuning as SimData under this type/group pair (read from
# both base-game and pack packages; identical everywhere).
SIMDATA_TYPE = 0x545AC67A
OBJECTIVE_GROUP = 0x0069453E

OBJECTIVE_RE = re.compile(rb"objective_Skill_([A-Za-z]+)_Level(\d{2})")

# Folder code -> display name, read from the game's dlc.ini at scan time.
# This table is only a fallback for cached scans made before names were
# captured; the values below are the real shipped pack names.
PACK_NAMES = {
    "EP01": "Get to Work",
    "EP02": "Get Together",
    "EP03": "City Living",
    "EP04": "Cats & Dogs",
    "EP05": "Seasons",
    "EP06": "Get Famous",
    "EP07": "Island Living",
    "EP08": "Discover University",
    "EP09": "Eco Lifestyle",
    "EP10": "Snowy Escape",
    "EP11": "Cottage Living",
    "EP12": "High School Years",
    "EP13": "Growing Together",
    "EP14": "Horse Ranch",
    "EP15": "For Rent",
    "EP16": "Lovestruck",
    "EP17": "Life & Death",
    "EP18": "Businesses & Hobbies",
    "EP19": "Enchanted by Nature",
    "GP01": "Outdoor Retreat",
    "GP02": "Spa Day",
    "GP03": "Dine Out",
    "GP04": "Vampires",
    "GP05": "Parenthood",
    "GP06": "Jungle Adventure",
    "GP07": "StrangerVille",
    "GP08": "Realm of Magic",
    "GP09": "Journey to Batuu",
    "GP10": "Dream Home Decorator",
    "GP11": "My Wedding Stories",
    "GP12": "Werewolves",
}

_BRAND_PREFIXES = ("the simst 4 ", "the sims™ 4 ", "the sims 4 ",
                   "the simst 4")


def _clean_pack_name(raw: str) -> str:
    """Strip the 'The Sims 4' brand prefix EA puts on every dlc.ini name."""
    lowered = raw.strip().lower()
    for prefix in _BRAND_PREFIXES:
        if lowered.startswith(prefix):
            return raw.strip()[len(prefix):].strip()
    return raw.strip()


def pack_names_from_ini(game_dir: Path) -> dict[str, str]:
    """Read pack display names out of the game's dlc.ini, if present."""
    ini = Path(game_dir) / "dlc.ini"
    if not ini.is_file():
        return {}
    names: dict[str, str] = {}
    code = ""
    try:
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("[") and line.endswith("]"):
                code = line[1:-1].strip()
            elif code and line.startswith("Name_en_US"):
                _, _, value = line.partition("=")
                if value:
                    names[code] = _clean_pack_name(value)
    except OSError:
        return {}
    return names


def pack_display_name(code: str) -> str:
    return PACK_NAMES.get(code, code)


@dataclass
class GameContent:
    """What an installed game contributes beyond the hardcoded base game."""

    # Pack code (folder name) -> skill -> {level: objective instance id}
    packs: dict[str, dict[str, dict[int, int]]] = field(default_factory=dict)
    # Pack code -> display name, read from dlc.ini at scan time.
    pack_names: dict[str, str] = field(default_factory=dict)
    game_version: str = ""
    scanned_utc: str = ""

    def name_for(self, code: str) -> str:
        """Display name for a pack code: scanned name, then the built-in
        table, then the raw code."""
        return (self.pack_names.get(code)
                or PACK_NAMES.get(code, code))

    def skills_for(self, pack_code: str) -> list[str]:
        return sorted(self.packs.get(pack_code, {}))

    def all_skills(self) -> dict[str, dict[int, int]]:
        """Every skill across every pack; last pack wins on collision."""
        merged: dict[str, dict[int, int]] = {}
        for skills in self.packs.values():
            for skill, levels in skills.items():
                merged.setdefault(skill, {}).update(levels)
        return merged

    def to_json(self) -> str:
        packs = {
            code: {skill: {str(l): i for l, i in levels.items()}
                   for skill, levels in skills.items()}
            for code, skills in self.packs.items()
        }
        return json.dumps({
            "packs": packs,
            "pack_names": self.pack_names,
            "game_version": self.game_version,
            "scanned_utc": self.scanned_utc,
        }, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "GameContent":
        data = json.loads(text)
        packs = {
            code: {skill: {int(l): i for l, i in levels.items()}
                   for skill, levels in skills.items()}
            for code, skills in (data.get("packs") or {}).items()
        }
        return cls(packs=packs,
                   pack_names=dict(data.get("pack_names") or {}),
                   game_version=data.get("game_version", ""),
                   scanned_utc=data.get("scanned_utc", ""))


def _game_version(game_dir: Path) -> str:
    """Read GameVersion.txt if present, so the cache can be staleness-checked."""
    candidate = game_dir / "Game" / "Bin" / "GameVersion.txt"
    if candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
    return ""


def scan_game(game_dir: str | Path, on_progress=None) -> GameContent:
    """
    Walk every pack folder's packages and pull out skill objectives.

    Only the index is read for almost every resource; payloads are pulled
    just for entries that already look like objectives by type/group, which
    keeps a full-install scan tolerable.
    """
    game = Path(game_dir)
    packs: dict[str, dict[str, dict[int, int]]] = {}
    names = pack_names_from_ini(game)

    folders = ([d for d in game.iterdir() if d.is_dir()
                and re.fullmatch(r"(EP|GP|SP)\d\d", d.name)]
               if game.is_dir() else [])
    for folder in sorted(folders):
        def report(msg: str) -> None:
            if on_progress:
                on_progress(msg)

        hits: dict[str, dict[int, int]] = {}
        packages = sorted(folder.rglob("*.package"))
        for pkg in packages:
            try:
                entries = iter_index(pkg)
            except Exception:
                continue
            for entry in entries:
                if (entry.type_id != SIMDATA_TYPE
                        or entry.group_id != OBJECTIVE_GROUP
                        or entry.deleted):
                    continue
                try:
                    data = read_resource_at(pkg, entry)
                except Exception:
                    continue
                for skill_b, level_b in OBJECTIVE_RE.findall(data):
                    skill = skill_b.decode("ascii").lower()
                    level = int(level_b)
                    hits.setdefault(skill, {})[level] = entry.instance_id
        if hits:
            report(f"{names.get(folder.name, folder.name)}: "
                   + ", ".join(sorted(hits)))
            packs[folder.name] = hits

    from datetime import datetime, timezone
    return GameContent(
        packs=packs,
        pack_names={code: names[code] for code in packs if code in names},
        game_version=_game_version(game),
        scanned_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Cache + merged view
# ---------------------------------------------------------------------------

def cache_path() -> Path:
    from .config import config_dir
    return config_dir() / "game_content.json"


def load_cached() -> GameContent | None:
    path = cache_path()
    if not path.is_file():
        return None
    try:
        return GameContent.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(content: GameContent) -> None:
    path = cache_path()
    path.write_text(content.to_json(), encoding="utf-8")


def rescan(game_dir: str | Path, on_progress=None) -> GameContent:
    content = scan_game(game_dir, on_progress=on_progress)
    save_cache(content)
    return content


def effective_skills(content: GameContent | None,
                     enabled_packs: list[str] | None) -> dict[str, dict[int, int]]:
    """
    The objective table the builder should use: base game plus every enabled
    pack. `enabled_packs=None` means "no game content available", which is
    base game only.
    """
    from . import ea_refs

    merged: dict[str, dict[int, int]] = {
        skill: dict(levels) for skill, levels in ea_refs.SKILL_OBJECTIVES.items()
    }
    if content is None:
        return merged
    for code in (enabled_packs or []):
        for skill, levels in content.packs.get(code, {}).items():
            merged.setdefault(skill, {}).update(levels)
    return merged


def skill_pack(content: GameContent | None) -> dict[str, str]:
    """skill -> pack code, for UI labels and shareability warnings."""
    if content is None:
        return {}
    return {skill: code
            for code, skills in content.packs.items()
            for skill in skills}
