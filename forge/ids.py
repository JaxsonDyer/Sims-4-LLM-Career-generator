"""
Resource identity for The Sims 4.

Every resource in a .package is addressed by a TGI triplet:
    Type    (u32)  - what kind of resource it is
    Group   (u32)  - usually 0 for mods
    Instance(u64)  - the unique id

The Sims 4 derives tuning instance ids from an FNV hash of the tuning's
name, and string-table keys from an FNV-32 hash of the string identifier.
Getting these wrong means the game loads the resource but nothing can
reference it, so the implementations below are the exact variants TS4 uses.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# FNV-1 (note: FNV-1, not FNV-1a, for the 32-bit high-bit variant TS4 uses)
# ---------------------------------------------------------------------------

FNV32_PRIME = 0x01000193
FNV32_OFFSET = 0x811C9DC5
FNV64_PRIME = 0x00000100000001B3
FNV64_OFFSET = 0xCBF29CE484222325

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF


def fnv32(text: str) -> int:
    """Standard FNV-1 32-bit. TS4 lowercases before hashing."""
    h = FNV32_OFFSET
    for byte in text.lower().encode("utf-8"):
        h = (h * FNV32_PRIME) & MASK32
        h ^= byte
    return h


def fnv64(text: str) -> int:
    """Standard FNV-1 64-bit, lowercased."""
    h = FNV64_OFFSET
    for byte in text.lower().encode("utf-8"):
        h = (h * FNV64_PRIME) & MASK64
        h ^= byte
    return h


def fnv32_high(text: str) -> int:
    """
    FNV-1 32-bit with the high bit forced on.

    String table keys use this. The high bit is set so that a hash can never
    collide with the small hand-assigned ids EA uses internally.
    """
    return fnv32(text) | 0x80000000


def fnv64_high(text: str) -> int:
    """FNV-1 64-bit with the high bit forced on. Used for tuning instance ids."""
    return fnv64(text) | 0x8000000000000000


# ---------------------------------------------------------------------------
# Resource type ids
# ---------------------------------------------------------------------------

class ResourceType:
    """
    TGI type ids for the resources a career mod needs.

    Cross-checked against s4pi's type tables in Sims4Tools
    (s4pi Wrappers/TextResource/TextResources.txt and
    s4pi Extras/Extensions/Extensions.txt).
    """

    # Generic binary tuning container. Most gameplay tuning (careers, tracks,
    # levels, buffs, interactions) lives under this type, distinguished by the
    # root element's `i=` attribute inside the XML rather than by type id.
    TUNING = 0x03B33DDF

    # SimData - the binary sidecar that accompanies certain tuning resources.
    SIMDATA = 0x545AC67A

    # String table. One per language.
    STBL = 0x220557DA

    # Images. Tuning names icons as PNG keys; SimData names the same
    # instance under the DDS type, which is what the UI actually loads.
    PNG = 0x2F7D0004
    DDS = 0x00B2D882

    # Tuning subtypes that TS4 gives their own type id rather than folding
    # into the generic TUNING type.
    CAREER = 0x73996BEB
    CAREER_TRACK = 0x48C75CE3
    CAREER_LEVEL = 0x2C70ADF8
    ASPIRATION = 0x28B64675
    BUFF = 0x6017E896
    INTERACTION = 0xE882D22F
    STATISTIC = 0x339BC5BD
    SNIPPET = 0x7DF2169C


# A SimData resource shares its tuning's instance id; its group id says which
# tuning type it belongs to. Read from EA's SimulationDeltaBuild0.package.
SIMDATA_GROUP = {
    ResourceType.CAREER: 0x00996B98,
    ResourceType.CAREER_TRACK: 0x00C75CAB,
    ResourceType.CAREER_LEVEL: 0x0070ADD4,
    ResourceType.STATISTIC: 0x009BC58E,
    ResourceType.ASPIRATION: 0x00B6465D,
}


# Language codes for string tables. TS4 expects the language code in the
# top byte of the STBL instance id.
LANG_ENGLISH = 0x00


def stbl_instance(mod_key: str, language: int = LANG_ENGLISH) -> int:
    """
    Build a string-table instance id.

    Layout is: high byte = language code, remaining 56 bits = a hash unique
    to this mod. Two mods with different names therefore never collide.
    """
    base = fnv64(f"stbl::{mod_key}") & 0x00FFFFFFFFFFFFFF
    return (language << 56) | base


class InstanceAllocator:
    """
    Hands out deterministic, collision-resistant instance ids for a mod.

    Deterministic matters: rebuilding the same career must produce the same
    ids, or a player who updates the mod gets orphaned references in saves
    where sims already hold the career.

    Ids are derived from the mod key plus a per-resource name, so two
    different mods built by this tool will not collide with each other, and
    neither will collide with base-game content because of the high bit.
    """

    def __init__(self, mod_key: str):
        if not mod_key or not mod_key.strip():
            raise ValueError("mod_key must be a non-empty string")
        self.mod_key = mod_key.strip()
        self._issued: dict[str, int] = {}

    def tuning_name(self, resource_name: str) -> str:
        """The fully-qualified tuning name the game will see."""
        return f"{self.mod_key}:{resource_name}"

    def instance(self, resource_name: str) -> int:
        """Return the 64-bit instance id for a named resource in this mod."""
        name = self.tuning_name(resource_name)
        if name not in self._issued:
            self._issued[name] = fnv64_high(name)
        return self._issued[name]

    def string_key(self, resource_name: str) -> int:
        """Return the 32-bit string-table key for a named string."""
        return fnv32_high(f"{self.mod_key}:{resource_name}")

    @property
    def issued(self) -> dict[str, int]:
        """Every id handed out so far, for the build manifest."""
        return dict(self._issued)


def check_collisions(allocator: InstanceAllocator) -> list[str]:
    """
    Verify no two resource names in this mod hashed to the same instance id.

    A 64-bit FNV collision is vanishingly unlikely, but a silent collision
    would produce a mod that half-works in confusing ways, so it is worth
    the cheap check.
    """
    seen: dict[int, str] = {}
    problems: list[str] = []
    for name, inst in allocator.issued.items():
        if inst in seen:
            problems.append(
                f"instance id collision: {name!r} and {seen[inst]!r} "
                f"both hash to 0x{inst:016X}"
            )
        seen[inst] = name
    return problems
