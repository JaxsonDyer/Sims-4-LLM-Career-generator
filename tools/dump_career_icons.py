#!/usr/bin/env python3
"""
Find and dump EA's own career icons from the installed game.

Answers, from the game's own bytes rather than from folklore:
  - which resource type career icons live under (PNG 0x2F7D0004, DDS
    0x00B2D882, or both) for the instance ids in forge.ea_refs.ICONS;
  - the exact pixel dimensions and DDS format of the low/high-res icons;
  - writes PNG copies to tools/ea_icon_refs/ for use as style references
    when generating custom icon art (and as resize targets).

    python tools/dump_career_icons.py
    python tools/dump_career_icons.py "D:\\Games\\The Sims 4"
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.dbpf import iter_index, read_resource_at  # noqa: E402
from forge.ea_refs import ICONS, TRACK_IMAGE  # noqa: E402
from forge.ids import ResourceType  # noqa: E402

DEFAULT_GAME = Path(r"C:\Program Files\EA Games\The Sims 4")
OUT_DIR = Path(__file__).resolve().parent / "ea_icon_refs"


def describe(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return f"PNG {w}x{h}, {len(data)} bytes"
    if data[:4] == b"DDS ":
        height, width = struct.unpack_from("<II", data, 12)
        fourcc = data[84:88]
        return f"DDS {width}x{height}, fourcc {fourcc!r}, {len(data)} bytes"
    return f"unknown format, {len(data)} bytes, head {data[:16].hex()}"


def to_png(data: bytes) -> bytes | None:
    """Decode a PNG or uncompressed-DDS payload into PNG bytes, via Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    if data[:4] != b"DDS ":
        return None
    try:
        height, width = struct.unpack_from("<II", data, 12)
        image = Image.frombytes("RGBA", (width, height), data[128:128 + width * height * 4])
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def main() -> int:
    game = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GAME
    client = game / "Data" / "Client"
    if not client.is_dir():
        print(f"error: no client folder at {client}", file=sys.stderr)
        return 2

    wanted: dict[int, str] = {}
    for name, (icon, hires) in ICONS.items():
        wanted[icon] = f"{name} icon"
        wanted[hires] = f"{name} hires"
    wanted[TRACK_IMAGE] = "track_bkg"

    found: dict[int, tuple[Path, object]] = {}
    packages = sorted(client.glob("*.package"))
    print(f"Scanning {len(packages)} client packages for {len(wanted)} keys "
          f"under PNG {PNG_HEX} and DDS {DDS_HEX}...")

    for package in packages:
        for entry in iter_index(package):
            if entry.type_id in (ResourceType.PNG, ResourceType.DDS) \
                    and entry.instance_id in wanted:
                found.setdefault((entry.type_id, entry.instance_id), (package, entry))

    print(f"\nFound {len(found)} of {len(wanted) * 2} keys.\n")
    hits_by_type: dict[int, int] = {}
    for (type_id, instance), (package, entry) in sorted(found.items()):
        label = wanted[instance]
        data = read_resource_at(package, entry)
        hits_by_type[type_id] = hits_by_type.get(type_id, 0) + 1
        print(f"  {label:<28} {type_id:08X}  {describe(data)}")

    print("\nSummary by type:")
    for type_id, count in sorted(hits_by_type.items()):
        print(f"  0x{type_id:08X}: {count} hits")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    for (type_id, instance), (package, entry) in sorted(found.items()):
        png = to_png(read_resource_at(package, entry))
        if png:
            (OUT_DIR / f"{wanted[instance].replace(' ', '_')}.png").write_bytes(png)
            saved += 1
        else:
            (OUT_DIR / f"{wanted[instance].replace(' ', '_')}.bin").write_bytes(
                read_resource_at(package, entry))
    print(f"\nWrote {saved} reference images to {OUT_DIR}")
    return 0


PNG_HEX = f"0x{ResourceType.PNG:08X}"
DDS_HEX = f"0x{ResourceType.DDS:08X}"


if __name__ == "__main__":
    sys.exit(main())
