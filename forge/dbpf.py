"""
DBPF 2.1 reader/writer - the .package container format used by The Sims 4.

Layout:
    [96-byte header]
    [resource data, back to back]
    [index table]

The header stores the index offset and size; the index stores one entry per
resource with its TGI key, where to find it, and how it is compressed.

Index entries can omit fields that are constant across the whole index, using
a flags word at the start of the index. We always write flags=0 (no constant
fields), which is what EA's own Strings_ENG_US and SimulationFullBuild
packages use. The reader handles constant fields because s4pi-written
packages use them.

The reader follows s4pi's Package.cs / Compression.cs (Sims4Tools): the
index position falls back to the legacy 0x28 field, compressed payloads are
identified by their first bytes (0x78 = zlib, xx FB = RefPack) rather than by
trusting the marker, and deleted entries are skipped.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"DBPF"
HEADER_SIZE = 96

# Compression type markers stored in the index.
COMP_NONE = 0x0000
COMP_ZLIB = 0x5A42
COMP_REFPACK = 0xFFFF  # EA's internal compression; we never write it, but game packages use it.

# Below this size, compression costs more overhead than it saves.
MIN_COMPRESS_BYTES = 220


@dataclass
class Resource:
    """One resource inside a package."""

    type_id: int
    group_id: int
    instance_id: int  # full 64-bit
    data: bytes
    compress: bool = True
    # Populated on read; ignored on write.
    _was_compressed: bool = field(default=False, repr=False)

    @property
    def instance_high(self) -> int:
        return (self.instance_id >> 32) & 0xFFFFFFFF

    @property
    def instance_low(self) -> int:
        return self.instance_id & 0xFFFFFFFF

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.type_id, self.group_id, self.instance_id)

    def key_string(self) -> str:
        """S4Studio-style display key, useful in logs and manifests."""
        return f"{self.type_id:08X}!{self.group_id:08X}!{self.instance_id:016X}"


class DBPFError(Exception):
    """Raised when a package cannot be parsed."""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _pack_header(index_count: int, index_offset: int, index_size: int) -> bytes:
    """Build the 96-byte header for a DBPF 2.1 package."""
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<4s", header, 0x00, MAGIC)
    struct.pack_into("<I", header, 0x04, 2)  # major version
    struct.pack_into("<I", header, 0x08, 1)  # minor version -> 2.1 = TS4
    # 0x0C-0x17 unused
    # 0x18 dateCreated / 0x1C dateModified: TS4 ignores these, leave zero so
    # that builds are byte-reproducible.
    struct.pack_into("<I", header, 0x20, 0)  # index major version (TS4 uses 0)
    struct.pack_into("<I", header, 0x24, index_count)
    struct.pack_into("<I", header, 0x28, 0)  # legacy low index offset
    struct.pack_into("<I", header, 0x2C, index_size)
    struct.pack_into("<I", header, 0x30, 0)  # trash entry count
    struct.pack_into("<I", header, 0x34, 0)  # trash offset
    struct.pack_into("<I", header, 0x38, 0)  # trash size
    struct.pack_into("<I", header, 0x3C, 3)  # index minor version
    struct.pack_into("<I", header, 0x40, index_offset)
    struct.pack_into("<I", header, 0x44, 0)  # unused
    # 0x48-0x4F reserved, already zero
    struct.pack_into("<I", header, 0x50, 0x0000FFFF)  # EA's packages set this; s4pi leaves 0
    # 0x54-0x5F reserved, already zero
    return bytes(header)


def write_package(resources: list[Resource], path: str | Path) -> Path:
    """
    Write resources to a .package file.

    Returns the path written. Raises DBPFError on duplicate TGI keys, since a
    package with two resources under one key loads unpredictably.
    """
    path = Path(path)
    if not resources:
        raise DBPFError("refusing to write a package with no resources")

    seen: dict[tuple[int, int, int], Resource] = {}
    for res in resources:
        if res.key in seen:
            raise DBPFError(
                f"duplicate resource key {res.key_string()} - two resources "
                f"cannot share a TGI triplet"
            )
        seen[res.key] = res

    blobs: list[tuple[Resource, bytes, int, int]] = []
    # (resource, bytes_to_write, uncompressed_size, compression_marker)
    for res in resources:
        raw = res.data
        if res.compress and len(raw) >= MIN_COMPRESS_BYTES:
            packed = zlib.compress(raw, 9)
            if len(packed) < len(raw):
                blobs.append((res, packed, len(raw), COMP_ZLIB))
                continue
        blobs.append((res, raw, len(raw), COMP_NONE))

    body = bytearray()
    offsets: list[int] = []
    for _res, payload, _mem, _comp in blobs:
        offsets.append(HEADER_SIZE + len(body))
        body.extend(payload)

    # Index: 4-byte flags word (0 = nothing constant) then 32 bytes per entry.
    index = bytearray()
    index.extend(struct.pack("<I", 0))
    for (res, payload, mem_size, comp), offset in zip(blobs, offsets):
        # The high bit of the size field flags "extended compression info
        # present", which is how TS4 knows to read the trailing fields.
        size_field = len(payload) | 0x80000000
        index.extend(
            struct.pack(
                "<IIIIIIIHH",
                res.type_id,
                res.group_id,
                res.instance_high,
                res.instance_low,
                offset,
                size_field,
                mem_size,
                comp,
                1,  # committed
            )
        )

    index_offset = HEADER_SIZE + len(body)
    header = _pack_header(len(blobs), index_offset, len(index))

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(body)
        handle.write(index)
    return path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_package(path: str | Path) -> list[Resource]:
    """
    Read every resource out of a .package file.

    Used for clone mode, where an existing career package is the structural
    template. Handles constant-field index flags, which game packages use.
    """
    path = Path(path)
    blob = path.read_bytes()
    if len(blob) < HEADER_SIZE or blob[:4] != MAGIC:
        raise DBPFError(f"{path.name} is not a DBPF package (bad magic bytes)")

    major, minor = struct.unpack_from("<II", blob, 0x04)
    if major != 2:
        raise DBPFError(
            f"{path.name} is DBPF {major}.{minor}; The Sims 4 uses 2.x. "
            f"This looks like a Sims 2 or Sims 3 package."
        )

    index_count = struct.unpack_from("<I", blob, 0x24)[0]
    index_size = struct.unpack_from("<I", blob, 0x2C)[0]
    # s4pi: prefer the 0x40 position, fall back to the legacy 0x28 one.
    index_offset = (struct.unpack_from("<I", blob, 0x40)[0]
                    or struct.unpack_from("<I", blob, 0x28)[0])

    if index_offset + index_size > len(blob):
        raise DBPFError(f"{path.name} index runs past end of file; truncated?")

    cursor = index_offset
    flags = struct.unpack_from("<I", blob, cursor)[0]
    cursor += 4

    const_type = const_group = const_inst_high = None
    if flags & 0x1:
        const_type = struct.unpack_from("<I", blob, cursor)[0]
        cursor += 4
    if flags & 0x2:
        const_group = struct.unpack_from("<I", blob, cursor)[0]
        cursor += 4
    if flags & 0x4:
        const_inst_high = struct.unpack_from("<I", blob, cursor)[0]
        cursor += 4

    resources: list[Resource] = []
    deleted = 0
    for _ in range(index_count):
        if const_type is None:
            type_id = struct.unpack_from("<I", blob, cursor)[0]
            cursor += 4
        else:
            type_id = const_type
        if const_group is None:
            group_id = struct.unpack_from("<I", blob, cursor)[0]
            cursor += 4
        else:
            group_id = const_group
        if const_inst_high is None:
            inst_high = struct.unpack_from("<I", blob, cursor)[0]
            cursor += 4
        else:
            inst_high = const_inst_high

        inst_low, offset, size_field, mem_size = struct.unpack_from(
            "<IIII", blob, cursor
        )
        cursor += 16
        comp, _committed = struct.unpack_from("<HH", blob, cursor)
        cursor += 4

        size = size_field & 0x7FFFFFFF
        if offset == 0xFFFFFFFF or (size == 1 and mem_size == 0xFFFFFFFF):
            deleted += 1  # s4pi treats these as deleted placeholders
            continue
        if offset + size > len(blob):
            raise DBPFError(
                f"resource {type_id:08X}!{group_id:08X} in {path.name} "
                f"runs past end of file"
            )
        payload = blob[offset:offset + size]
        label = f"{type_id:08X}!{group_id:08X}!{(inst_high << 32) | inst_low:016X}"

        if size == mem_size:
            data = payload
        else:
            data = _decompress(payload, mem_size, label, path.name)

        resources.append(
            Resource(
                type_id=type_id,
                group_id=group_id,
                instance_id=(inst_high << 32) | inst_low,
                data=data,
                compress=(comp != COMP_NONE),
                _was_compressed=(comp != COMP_NONE),
            )
        )

    if len(resources) + deleted != index_count:
        raise DBPFError(
            f"expected {index_count} resources in {path.name}, "
            f"parsed {len(resources) + deleted}"
        )
    return resources


def _decompress(payload: bytes, mem_size: int, label: str, where: str) -> bytes:
    """Decompress one resource, detecting the codec from its header bytes like s4pi."""
    if len(payload) < 2:
        raise DBPFError(f"resource {label} in {where} is truncated")
    try:
        if payload[0] == 0x78:
            data = zlib.decompress(payload)
        elif payload[1] == 0xFB:
            data = refpack_decompress(payload)
        else:
            raise DBPFError(
                f"resource {label} in {where} uses an unrecognised "
                f"compression format (header {payload[:2].hex()})"
            )
    except (zlib.error, IndexError) as exc:
        raise DBPFError(f"failed to decompress resource {label} in {where}: {exc}") from exc
    if len(data) != mem_size:
        raise DBPFError(
            f"resource {label} in {where} decompressed to {len(data)} bytes, "
            f"index says {mem_size}"
        )
    return data


def refpack_decompress(src: bytes) -> bytes:
    """
    Decode EA's RefPack (QFS) compression, which game packages mark 0xFFFF.

    Port of Compression.OldDecompress from s4pi. Header is a flags byte, 0xFB,
    then the uncompressed size as big-endian 3 bytes (4 if flags == 0x80).
    Each control byte then says how many literal bytes to copy from the input
    and how many to copy back from earlier output.
    """
    size_bytes = 4 if src[0] == 0x80 else 3
    pos = 2
    size = int.from_bytes(src[pos:pos + size_bytes], "big")
    pos += size_bytes

    out = bytearray()
    while len(out) < size:
        b0 = src[pos]
        pos += 1
        copy = offset = 0
        if b0 <= 0x7F:
            b1 = src[pos]
            pos += 1
            plain = b0 & 0x03
            copy = ((b0 & 0x1C) >> 2) + 3
            offset = ((b0 & 0x60) << 3) + b1 + 1
        elif b0 <= 0xBF:
            b1, b2 = src[pos], src[pos + 1]
            pos += 2
            plain = (b1 >> 6) & 0x03
            copy = (b0 & 0x3F) + 4
            offset = ((b1 & 0x3F) << 8) + b2 + 1
        elif b0 <= 0xDF:
            b1, b2, b3 = src[pos], src[pos + 1], src[pos + 2]
            pos += 3
            plain = b0 & 0x03
            copy = ((b0 & 0x0C) << 6) + b3 + 5
            offset = ((b0 & 0x10) << 12) + (b1 << 8) + b2 + 1
        elif b0 <= 0xFB:
            plain = ((b0 & 0x1F) << 2) + 4
        else:
            plain = b0 & 0x03

        out += src[pos:pos + plain]
        pos += plain
        # Back-references may overlap the bytes they produce, so copy one at
        # a time rather than slicing.
        start = len(out) - offset
        for i in range(copy):
            out.append(out[start + i])
    return bytes(out)


def describe_package(path: str | Path) -> str:
    """Human-readable inventory of a package, for the GUI's inspect button."""
    resources = read_package(path)
    from collections import Counter

    counts = Counter(r.type_id for r in resources)
    lines = [f"{Path(path).name}: {len(resources)} resources"]
    for type_id, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  type 0x{type_id:08X}  x{count}")
    return "\n".join(lines)
