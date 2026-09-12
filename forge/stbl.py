"""
STBL (string table) resources - every piece of player-visible text.

Career names, level titles, descriptions and promotion messages are not
stored in tuning. Tuning stores a 32-bit key, and the game looks that key up
in a string table for the active language. Text in the tuning file itself is
only a comment for modders; changing it does nothing in game.

STBL version 5 layout:
    'STBL'            4 bytes
    version           u16  = 5
    compressed        u8   = 0
    entry count       u64
    reserved          2 bytes
    total string len  u32   sum over entries of (UTF-8 byte length + 1)
    entries...        u32 key, u8 flags, u16 length, UTF-8 bytes

The "+ 1" per entry is load-bearing. The game allocates one buffer of this
size and copies every string into it with a null terminator, so a value
that leaves out the terminators is a heap overflow in native code: the game
crashes at load with no Python traceback. Verified against EA's own
Strings_ENG_US tables, where the field equals sum(utf8_len + 1) exactly.
s4pi (Sims4Tools) counts UTF-16 characters + 1 instead, which agrees for
ASCII but undercounts non-ASCII text, so we follow EA rather than s4pi.
"""

from __future__ import annotations

import struct

STBL_MAGIC = b"STBL"
STBL_VERSION = 5


class STBLError(Exception):
    """Raised when a string table cannot be built or parsed."""


def string_data_length(encoded_strings) -> int:
    """The header's total-length field: each string's UTF-8 bytes plus its null."""
    return sum(len(raw) + 1 for raw in encoded_strings)


class StringTable:
    """
    Accumulates key -> text pairs and serialises them to an STBL resource.

    Keys come from InstanceAllocator.string_key(), so callers refer to
    strings by readable names and never handle raw hashes.
    """

    def __init__(self) -> None:
        self._entries: dict[int, str] = {}

    def add(self, key: int, text: str) -> int:
        """
        Register a string under a key. Returns the key for chaining.

        Re-adding the same key with the same text is fine (templates often
        reference a string more than once). Re-adding with different text is
        an error, because one of the two would silently win.
        """
        if not isinstance(key, int) or key < 0 or key > 0xFFFFFFFF:
            raise STBLError(f"string key must be a u32, got {key!r}")
        if key in self._entries and self._entries[key] != text:
            raise STBLError(
                f"string key 0x{key:08X} already holds "
                f"{self._entries[key]!r}, cannot also hold {text!r}"
            )
        self._entries[key] = text
        return key

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: int) -> bool:
        return key in self._entries

    @property
    def entries(self) -> dict[int, str]:
        return dict(self._entries)

    def to_bytes(self) -> bytes:
        """Serialise to an STBL v5 resource."""
        if not self._entries:
            raise STBLError("refusing to build an empty string table")

        encoded: list[tuple[int, bytes]] = []
        for key in sorted(self._entries):
            raw = self._entries[key].encode("utf-8")
            if len(raw) > 0xFFFF:
                raise STBLError(
                    f"string for key 0x{key:08X} is {len(raw)} bytes; "
                    f"the format caps a single string at 65535"
                )
            encoded.append((key, raw))

        out = bytearray()
        out.extend(STBL_MAGIC)
        out.extend(struct.pack("<H", STBL_VERSION))
        out.append(0)  # not compressed
        out.extend(struct.pack("<Q", len(encoded)))
        out.extend(b"\x00\x00")  # reserved
        out.extend(struct.pack("<I", string_data_length(raw for _k, raw in encoded)))

        for key, raw in encoded:
            out.extend(struct.pack("<I", key))
            out.append(0)  # flags
            out.extend(struct.pack("<H", len(raw)))
            out.extend(raw)

        return bytes(out)


def _read_stbl(data: bytes) -> tuple[int, list[tuple[int, bytes]], int]:
    """Returns (declared string length, [(key, raw utf-8)], trailing bytes)."""
    if len(data) < 21 or data[:4] != STBL_MAGIC:
        raise STBLError("not an STBL resource (bad magic bytes)")

    version = struct.unpack_from("<H", data, 4)[0]
    if version != STBL_VERSION:
        raise STBLError(f"unsupported STBL version {version}; expected 5")

    count = struct.unpack_from("<Q", data, 7)[0]
    declared = struct.unpack_from("<I", data, 17)[0]
    cursor = 21  # 4 magic + 2 version + 1 compressed + 8 count + 2 reserved + 4 len

    entries: list[tuple[int, bytes]] = []
    for _ in range(count):
        if cursor + 7 > len(data):
            raise STBLError("STBL truncated mid-entry")
        key = struct.unpack_from("<I", data, cursor)[0]
        length = struct.unpack_from("<H", data, cursor + 5)[0]
        cursor += 7
        if cursor + length > len(data):
            raise STBLError("STBL truncated mid-string")
        entries.append((key, data[cursor:cursor + length]))
        cursor += length
    return declared, entries, len(data) - cursor


def parse_stbl(data: bytes) -> dict[int, str]:
    """Read an STBL resource back into a key -> text mapping."""
    _declared, entries, _trailing = _read_stbl(data)
    return {key: raw.decode("utf-8", errors="replace") for key, raw in entries}


def check_stbl(data: bytes) -> list[str]:
    """
    Structural problems that would make the game's native loader misbehave.

    Empty list means the table is shaped the way EA's own tables are.
    """
    try:
        declared, entries, trailing = _read_stbl(data)
    except STBLError as exc:
        return [str(exc)]

    problems: list[str] = []
    expected = string_data_length(raw for _key, raw in entries)
    if declared != expected:
        problems.append(
            f"STBL string-length field is {declared}, must be {expected} "
            f"(sum of UTF-8 lengths + 1 per string); the game would "
            f"overflow its string buffer"
        )
    if trailing:
        problems.append(f"STBL has {trailing} unexpected trailing bytes")
    keys = [key for key, _raw in entries]
    if len(set(keys)) != len(keys):
        problems.append("STBL contains duplicate string keys")
    return problems
