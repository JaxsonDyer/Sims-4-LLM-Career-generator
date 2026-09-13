"""
Custom career icon art, from prompt to packed DDS.

EA's own icons were extracted from the installed game and measured:
  - `icon` (the Find a Job picker emblem) is 50x50 uncompressed RGBA8 DDS
    with a transparent background;
  - `icon_high_res` (the career panel image) is 500x250, fully opaque;
  - both live under the DDS type id 0x00B2D882, never PNG, despite tuning
    naming them with a PNG key. The game's own packages carry no PNG career
    icons at all, so we write DDS only, in the same RGBA8 layout.

The pipeline per branch:
  1. build an emblem prompt from the career/branch text;
  2. generate one square emblem via the Image API, using 2-3 EA's own icons
     as style references when the game is installed;
  3. key out the white background, trim, and resize to EA's exact formats;
  4. cache the generated art on disk so a rebuild never re-bills.

Failure policy: every failure - no Pillow, no key, a failed generation -
falls back to EA's keyword-picked icon for that branch with a warning. A
career with broken icon art still ships; it just looks less custom.
"""

from __future__ import annotations

import hashlib
import io
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .dbpf import iter_index, read_resource_at
from .ea_refs import ICONS, ICON_KEYWORDS, pick_icon
from .ids import InstanceAllocator, ResourceType
from .schema import CareerSpec, CareerBranch

# EA's exact icon geometry, verified from game bytes.
PICKER_SIZE = 50
PANEL_W, PANEL_H = 500, 250

# Panel card: a quiet navy gradient, the tool's own UI palette, so the
# emblem reads as a deliberate career card rather than a stretched thumbnail.
PANEL_TOP = (26, 58, 82)
PANEL_BOTTOM = (13, 30, 47)

STYLE_PROMPT = (
    "chunky flat cartoon game icon emblem of {subject}. Bold simple shapes, "
    "smooth cel shading, bright saturated colours, subtle dark outline, "
    "single centred object, no text, no letters, no border, no watermark, "
    "no drop shadow, on a plain pure white background."
)

# Reference icons to steer style: thematically closest EA icons when the
# career text matches keywords, else a varied default trio.
DEFAULT_REFS = ["tech", "culinary", "astronaut"]

# Where the installed game usually lives, checked in order.
GAME_DIRS = [
    Path(r"C:\Program Files\EA Games\The Sims 4"),
    Path(r"C:\Program Files\EA\The Sims 4"),
    Path(r"C:\Program Files (x86)\Origin Games\The Sims 4"),
    Path(r"C:\Program Files\Steam\steamapps\common\The Sims 4"),
]


class IconArtError(Exception):
    pass


@dataclass
class BranchIconArt:
    """The custom art chosen for one branch's track."""

    branch_key: str
    prompt: str
    picker_instance: int
    panel_instance: int
    cached: bool
    preview_png: bytes = b""  # 50x50 PNG, for the web interface


@dataclass
class IconSet:
    """Everything the builder needs to write icons into the package."""

    mode: str  # "custom" | "ea"
    image_model: str = ""
    branches: dict[str, BranchIconArt] = field(default_factory=dict)
    # instance -> DDS payload, to merge into the package resources.
    resources: dict[int, bytes] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# EA reference art, extracted from the installed game
# ---------------------------------------------------------------------------

def find_game_dir(explicit: str | None = None) -> Path | None:
    """The installed game folder, for pulling reference icons."""
    if explicit and Path(explicit).is_dir():
        return Path(explicit)
    for candidate in GAME_DIRS:
        if (candidate / "Data" / "Client").is_dir():
            return candidate
    return None


def extract_reference_icons(client_dir: Path, haystack: str, count: int = 3
                            ) -> list[bytes]:
    """
    Up to `count` EA career icons as PNG bytes, preferring ones whose
    keywords match the career text so the style reference is on-theme.
    """
    wanted: list[str] = []
    for key, words in ICON_KEYWORDS:
        if any(word in haystack for word in words) and key in ICONS:
            wanted.append(key)
    for key in DEFAULT_REFS:
        if key not in wanted:
            wanted.append(key)
    wanted = wanted[:count]

    instances = {ICONS[key][0]: key for key in wanted}
    found: dict[int, bytes] = {}
    for package in sorted(client_dir.glob("*.package")):
        if len(found) == len(instances):
            break
        try:
            entries = list(iter_index(package))
        except Exception:
            continue
        for entry in entries:
            if entry.type_id == ResourceType.DDS and entry.instance_id in instances:
                try:
                    data = read_resource_at(package, entry)
                except Exception:
                    continue
                png = _dds_to_png(data)
                if png:
                    found.setdefault(entry.instance_id, png)
        if len(found) == len(instances):
            break

    return [found[inst] for inst in instances if inst in found]


# ---------------------------------------------------------------------------
# Prompt -> emblem -> DDS
# ---------------------------------------------------------------------------

def emblem_prompt(spec: CareerSpec, branch: CareerBranch) -> str:
    """The image prompt for one branch's emblem."""
    hint = (spec.icon_hint or "").strip()
    parts = [spec.name]
    if branch.branches_at is not None:
        parts.append(branch.name)
    if hint:
        parts.append(hint)
    elif branch.description:
        parts.append(branch.description)
    elif spec.description:
        parts.append(spec.description)
    subject = " ".join(parts)
    return STYLE_PROMPT.format(subject=subject)


def _dds_to_png(data: bytes) -> bytes | None:
    """Decode EA's uncompressed RGBA8 DDS (verified format) into PNG bytes."""
    try:
        from PIL import Image
    except ImportError:
        return None
    if data[:4] != b"DDS ":
        return None
    try:
        height, width = struct.unpack_from("<II", data, 12)
        image = Image.frombytes(
            "RGBA", (width, height), data[128:128 + width * height * 4]
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def _key_out_white(image) -> "object":
    """
    Turn a near-white background into transparency, with a soft edge.

    The prompt asks for pure white; models usually comply. Anything with all
    channels >= 240 becomes fully transparent, 210-240 fades out, so flat
    cartoon art keeps its outline without haloing.
    """
    from PIL import Image

    image = image.convert("RGBA")
    pixels = image.load()
    width, height = image.size
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            lowest = min(r, g, b)
            if lowest >= 240:
                pixels[x, y] = (r, g, b, 0)
            elif lowest >= 210:
                fade = int((240 - lowest) / 30 * 255)
                pixels[x, y] = (r, g, b, min(a, fade))
    return image


def _trim_transparent(image):
    alpha = image.getchannel("A")
    box = alpha.getbbox()
    if box:
        image = image.crop(box)
    return image


def _square_canvas(art, size: int):
    """Trimmed art centred on a transparent square, EA's icon geometry."""
    from PIL import Image

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scaled = art.copy()
    scaled.thumbnail((size, size))
    canvas.alpha_composite(scaled, ((size - scaled.width) // 2,
                                    (size - scaled.height) // 2))
    return canvas


def _gradient_card(emblem, width: int = PANEL_W, height: int = PANEL_H):
    """The career-panel card: emblem centred on the tool's navy gradient."""
    from PIL import Image, ImageDraw

    card = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(card)
    for y in range(height):
        t = y / max(1, height - 1)
        colour = tuple(
            int(top + (bottom - top) * t)
            for top, bottom in zip(PANEL_TOP, PANEL_BOTTOM)
        )
        draw.line([(0, y), (width, y)], fill=colour + (255,))

    # Emblem takes most of the card's height, centred.
    target = int(height * 0.84)
    art = emblem.copy()
    art.thumbnail((target, target))
    x = (width - art.width) // 2
    y = (height - art.height) // 2
    card.alpha_composite(art, (x, y))
    return card


def _dds_rgba8(image) -> bytes:
    """
    Uncompressed RGBA8 DDS in the exact shape EA's own career icons use
    (128-byte header, no mipmaps), with a canonical header where EA's is
    sloppy: EA leaves the bit count 0 and the masks nonsense, and the game
    loader ignores them for uncompressed data, but correct masks keep every
    other DDS reader honest.
    """
    width, height = image.size
    raw = image.convert("RGBA").tobytes()

    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)             # header size, as EA writes it
    struct.pack_into("<I", header, 8, 0x100F)          # caps flags
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 20, width * 4)      # pitch
    struct.pack_into("<I", header, 76, 32)             # pixel format size
    struct.pack_into("<I", header, 80, 0x41)           # ALPHA | RGB, fourcc 0
    struct.pack_into("<I", header, 84, 32)             # 32 bits per pixel
    struct.pack_into("<I", header, 88, 0x000000FF)     # R mask (RGBA order)
    struct.pack_into("<I", header, 92, 0x0000FF00)
    struct.pack_into("<I", header, 96, 0x00FF0000)
    struct.pack_into("<I", header, 100, 0xFF000000)
    struct.pack_into("<I", header, 108, 0x1000)        # DDSCAPS_TEXTURE
    return bytes(header) + raw


# ---------------------------------------------------------------------------
# The pipeline the builder calls
# ---------------------------------------------------------------------------

def icon_instances(alloc: InstanceAllocator, branch_key: str) -> tuple[int, int]:
    """Deterministic instance ids for a branch's picker and panel icons."""
    return (
        alloc.instance(f"icon_picker_{branch_key}"),
        alloc.instance(f"icon_panel_{branch_key}"),
    )


def cache_paths(output_dir: Path, mod_key: str, branch_key: str,
                prompt: str, image_model: str) -> tuple[Path, str]:
    """
    Cache file for a branch's art, plus the hash that keys it.

    `output_dir` is the mod's own build folder (the one containing the
    package), so icons/ sits alongside it as the build report says.
    """
    digest = hashlib.sha256(
        f"{image_model}\x00{prompt}".encode("utf-8")
    ).hexdigest()[:8]
    folder = Path(output_dir) / "icons"
    return folder / f"{branch_key}.{digest}.png", digest


def prepare_icon_art(spec: CareerSpec, alloc: InstanceAllocator,
                     output_dir: str | Path, *, api_key: str = "",
                     image_model: str = "", game_dir: str | None = None,
                     generate=None, on_progress=None) -> IconSet:
    """
    Icon art for every branch, with graceful fallback.

    `generate(prompt, references) -> bytes` is injected so the web layer can
    pass its own progress-reporting wrapper; tests pass a fake.
    """
    where = spec.mod_key

    def report(message: str) -> None:
        if on_progress:
            on_progress(message)

    if spec.icon_mode != "custom":
        return _ea_fallback(spec, alloc, "custom icons are off")

    if not api_key:
        return _ea_fallback(spec, alloc, "no OpenRouter key, so no custom art")

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return _ea_fallback(
            spec, alloc, "Pillow is not installed (pip install pillow)"
        )

    if generate is None:
        if not image_model:
            return _ea_fallback(spec, alloc, "no image model configured")
        from .imagegen import ImageClient, ImageGenError

        client = ImageClient(api_key)

        def generate(prompt, references):
            return client.generate(image_model, prompt,
                                   input_references=references or None)

    icon_set = IconSet(mode="custom", image_model=image_model)

    references: list[bytes] = []
    game = find_game_dir(game_dir)
    if game is not None:
        report("Extracting EA icon style references from the game...")
        try:
            references = extract_reference_icons(
                game / "Data" / "Client",
                " ".join([spec.name, spec.icon_hint, spec.description]),
            )
        except Exception as exc:
            icon_set.warnings.append(
                f"could not read EA reference icons ({exc}); continuing "
                f"without them"
            )
    if references:
        report(f"Using {len(references)} EA icons as style references.")

    for branch in spec.branches:
        picker, panel = icon_instances(alloc, branch.key)
        prompt = emblem_prompt(spec, branch)
        cache_file, digest = cache_paths(
            Path(output_dir), spec.mod_key, branch.key, prompt, image_model
        )

        source = None
        if cache_file.exists():
            source = cache_file.read_bytes()
            report(f"{branch.name}: reusing cached icon art "
                   f"({cache_file.name}).")
            cached = True
        else:
            report(f"{branch.name}: generating icon art "
                   f"({image_model}, billed per image)...")
            try:
                source = generate(prompt, references)
                cached = False
            except Exception as exc:
                icon_set.warnings.append(
                    f"{branch.name}: custom icon failed ({exc}); using EA's "
                    f"keyword-picked icon instead."
                )
                _stamp_ea_fallback(icon_set, spec, alloc, branch)
                continue
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(source)

        try:
            art = _key_out_white(Image.open(io.BytesIO(source)))
            art = _trim_transparent(art)
            if art.width < 8 or art.height < 8:
                raise ValueError("generated image was blank")
            picker_image = _square_canvas(art, PICKER_SIZE)
            panel_image = _gradient_card(_square_canvas(art, PICKER_SIZE * 4))
        except Exception as exc:
            icon_set.warnings.append(
                f"{branch.name}: generated art was unusable ({exc}); using "
                f"EA's keyword-picked icon instead."
            )
            _stamp_ea_fallback(icon_set, spec, alloc, branch)
            continue

        picker_png = _png_bytes(picker_image)
        icon_set.branches[branch.key] = BranchIconArt(
            branch_key=branch.key, prompt=prompt, picker_instance=picker,
            panel_instance=panel, cached=cached, preview_png=picker_png,
        )
        icon_set.resources[picker] = _dds_rgba8(picker_image)
        icon_set.resources[panel] = _dds_rgba8(panel_image)

    return icon_set


def _png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _ea_fallback(spec: CareerSpec, alloc: InstanceAllocator,
                 reason: str) -> IconSet:
    """EA keyword icons for every branch, plus one clear warning."""
    icon_set = IconSet(mode="ea")
    icon_set.warnings.append(
        f"Custom icon art is not available ({reason}); the career uses EA's "
        f"own keyword-picked icons."
    )
    for branch in spec.branches:
        _stamp_ea_fallback(icon_set, spec, alloc, branch)
    return icon_set


def _stamp_ea_fallback(icon_set: IconSet, spec: CareerSpec,
                       alloc: InstanceAllocator, branch: CareerBranch) -> None:
    picker, panel = icon_instances(alloc, branch.key)
    icon_low, icon_high = pick_icon(spec.name, spec.icon_hint, branch.name)
    # Re-point at EA's ids so no image resources are written for this branch.
    icon_set.branches[branch.key] = BranchIconArt(
        branch_key=branch.key, prompt="",
        picker_instance=icon_low, panel_instance=icon_high, cached=False,
    )
    icon_set.resources.pop(picker, None)
    icon_set.resources.pop(panel, None)
