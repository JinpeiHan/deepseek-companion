from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PACK_IDS = frozenset({"chibi", "standard", "slender"})
SUPPORTED_FORMAT_VERSIONS = frozenset({1, 2, 3})
RENDERERS = frozenset({"frames", "rig"})


@dataclass(frozen=True)
class PackDescriptor:
    pack_id: str
    manifest: dict[str, Any]
    asset_root: Path
    logical_width: int
    logical_height: int
    foot_anchor: tuple[float, float]
    bubble_anchor: tuple[float, float]
    renderer: str = "frames"
    rig_path: Path | None = None

    @property
    def logical_scale(self) -> float:
        """Ratio between authored frame pixels and the logical pet box.

        v2 packs author 512px frames for a 260px logical character, so the
        renderer has to divide it back out; v1 authors at logical size and this
        is exactly 1.0, which keeps chibi's arithmetic bit-for-bit unchanged.
        """
        return self.logical_width / float(self.manifest["maxFrameWidth"])


def normalise_pack_id(value: Any) -> str:
    return value if isinstance(value, str) and value in PACK_IDS else "chibi"


def _confined(assets_root: Path, relative: str) -> Path:
    resolved = (assets_root / relative).resolve()
    if resolved != assets_root and assets_root not in resolved.parents:
        raise ValueError(f"asset path escapes assets root: {relative}")
    return resolved


def load_pack_descriptor(bundle_root: Path, pack_id: Any) -> PackDescriptor:
    assets_root = (bundle_root / "assets").resolve()
    registry = json.loads((assets_root / "pet-packs.json").read_text(encoding="utf-8"))
    selected = normalise_pack_id(pack_id)
    entry = registry["packs"].get(selected) or registry["packs"][registry["defaultPack"]]
    manifest_path = _confined(assets_root, entry["manifest"])
    asset_root = _confined(assets_root, entry["root"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    format_version = int(manifest.get("formatVersion", 1))
    if format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError(f"unsupported manifest formatVersion {format_version} for {selected}")
    renderer = manifest.get("renderer", "frames")
    if renderer not in RENDERERS:
        raise ValueError(f"unsupported renderer {renderer!r} for {selected}")
    # formatVersion 3 exists only to carry a rig; a v3 manifest that still asks
    # for the frame renderer has no frame sequences to play.
    if format_version == 3 and renderer != "rig":
        raise ValueError(f"formatVersion 3 requires renderer 'rig' for {selected}")
    logical_width = int(manifest.get("logicalWidth", manifest["maxFrameWidth"]))
    logical_height = int(manifest.get("logicalHeight", manifest["maxFrameHeight"]))
    foot = tuple(manifest.get("footAnchor", [0.5, 1.0]))
    bubble = tuple(manifest.get("bubbleAnchor", [0.5, 0.0]))
    if logical_height != 260 or logical_width <= 0:
        raise ValueError(f"invalid logical dimensions for {selected}")
    if len(foot) != 2 or len(bubble) != 2:
        raise ValueError(f"invalid anchors for {selected}")
    # A rig manifest is its own rig definition; part paths inside it resolve
    # against the pack root, so the file itself is what a rig loader needs.
    rig_path = manifest_path if renderer == "rig" else None
    return PackDescriptor(
        selected,
        manifest,
        asset_root,
        logical_width,
        logical_height,
        foot,
        bubble,
        renderer,
        rig_path,
    )


def load_pack_pixmaps(
    descriptor: PackDescriptor,
    pixmap_type: Callable[[str], Any],
    strict: bool = True,
) -> Any:
    """Load every frame of a pack up front.

    ``strict=False`` reports the unreadable frames instead of raising so the
    caller can reject the pack as a whole; a half-loaded pack would crash later
    on a KeyError deep inside paintEvent rather than at load time.
    """
    pixmaps: dict[str, Any] = {}
    missing: list[str] = []
    for clip in descriptor.manifest["clips"].values():
        for frame in clip["frames"]:
            if frame in pixmaps:
                continue
            frame_path = (descriptor.asset_root / frame).resolve()
            if descriptor.asset_root.resolve() not in frame_path.parents:
                raise ValueError(f"frame escapes pack root: {frame}")
            pixmap = pixmap_type(str(frame_path))
            if pixmap.isNull():
                if strict:
                    raise ValueError(f"unable to load frame: {descriptor.pack_id}/{frame}")
                missing.append(frame)
                continue
            pixmaps[frame] = pixmap
    return pixmaps if strict else (pixmaps, missing)
