from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PACK_IDS = frozenset({"chibi", "standard", "slender"})


@dataclass(frozen=True)
class PackDescriptor:
    pack_id: str
    manifest: dict[str, Any]
    asset_root: Path
    logical_width: int
    logical_height: int
    foot_anchor: tuple[float, float]
    bubble_anchor: tuple[float, float]


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
    logical_width = int(manifest.get("logicalWidth", manifest["maxFrameWidth"]))
    logical_height = int(manifest.get("logicalHeight", manifest["maxFrameHeight"]))
    foot = tuple(manifest.get("footAnchor", [0.5, 1.0]))
    bubble = tuple(manifest.get("bubbleAnchor", [0.5, 0.0]))
    if logical_height != 260 or logical_width <= 0:
        raise ValueError(f"invalid logical dimensions for {selected}")
    if len(foot) != 2 or len(bubble) != 2:
        raise ValueError(f"invalid anchors for {selected}")
    return PackDescriptor(selected, manifest, asset_root, logical_width, logical_height, foot, bubble)


def load_pack_pixmaps(descriptor: PackDescriptor, pixmap_type: Callable[[str], Any]) -> dict[str, Any]:
    pixmaps: dict[str, Any] = {}
    for clip in descriptor.manifest["clips"].values():
        for frame in clip["frames"]:
            frame_path = (descriptor.asset_root / frame).resolve()
            if descriptor.asset_root.resolve() not in frame_path.parents:
                raise ValueError(f"frame escapes pack root: {frame}")
            pixmap = pixmap_type(str(frame_path))
            if pixmap.isNull():
                raise ValueError(f"unable to load frame: {descriptor.pack_id}/{frame}")
            pixmaps[frame] = pixmap
    return pixmaps
