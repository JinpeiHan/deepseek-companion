"""How far each part's seams can travel, and therefore how much they must overlap.

The extraction partitions the master exactly -- every opaque pixel belongs to
one part, none to two. That is the right invariant for a *still* pose and the
wrong one for a moving rig: the moment two parts rotate by different amounts,
an exact partition opens along the line where they were cut. Measured on the
shipped standard rig, ``hair_back`` carries its own ``hairSway`` binding of
+/-6 degrees and its seam with the head sits ~53px from the pivot, so the cut
line separates by 5.6px -- against the 2px of overlap the annotation gave it.
That is the crack you see when the head moves.

The fix is not a better segmentation. A pixel-perfect boundary cracks exactly
as wide as a sloppy one; what closes it is margin. So each part is grown by the
distance its own seams can travel, and the extraction clips that growth to the
master's silhouette, which means the extra pixels only ever go *under* a
neighbouring part. They are invisible in the rest pose and only surface when
the neighbour swings away -- precisely when the hole would otherwise appear.

The travel is derived from the rig's own declared limits rather than tuned by
hand, so widening a parameter range cannot silently reintroduce the seam.
"""

from __future__ import annotations

import math

# Growth is clipped to the silhouette, so an over-estimate costs hidden pixels
# rather than a visible artefact -- but it still costs memory and fill rate.
MAX_GROW_PX = 40
MIN_GROW_PX = 2


def _rotation_limits(params: dict, bindings) -> tuple[dict[str, float], dict[str, float]]:
    """Peak rotation each bone and each part can reach, in degrees."""
    bones: dict[str, float] = {}
    parts: dict[str, float] = {}
    for binding in bindings:
        if binding.get("channel") != "rotate":
            continue
        spec = params[binding["param"]]
        span = max(abs(spec["min"]), abs(spec["max"])) * abs(binding.get("gain", 1.0))
        if "part" in binding:
            parts[binding["part"]] = parts.get(binding["part"], 0.0) + span
        else:
            bones[binding["bone"]] = bones.get(binding["bone"], 0.0) + span
    return bones, parts


def _accumulated(bone: str, parents: dict[str, str | None], bones: dict[str, float]) -> float:
    """A bone turns by its own rotation plus every ancestor's."""
    total = 0.0
    seen = set()
    current: str | None = bone
    while current and current not in seen:
        seen.add(current)
        total += bones.get(current, 0.0)
        current = parents.get(current)
    return total


def required_grow(
    part_table,
    bone_table,
    params: dict,
    bindings,
    neighbours: dict[str, dict[str, float]],
) -> dict[str, int]:
    """Overlap in pixels for each part id.

    ``neighbours`` maps a part to the parts it touches and the mean distance
    from its pivot to that shared boundary, which is what actually separates --
    the far end of a strand of hair is free silhouette and cannot crack.
    """
    parents = {bone["id"]: bone.get("parent") for bone in bone_table}
    bone_rot, part_rot = _rotation_limits(params, bindings)
    by_id = {part["id"]: part for part in part_table}

    def total_rotation(part_id: str) -> float:
        part = by_id[part_id]
        return _accumulated(part["bone"], parents, bone_rot) + part_rot.get(part_id, 0.0)

    grow: dict[str, int] = {}
    for part_id in by_id:
        worst = 0.0
        mine = total_rotation(part_id)
        for other, radius in neighbours.get(part_id, {}).items():
            if other not in by_id:
                continue
            relative = abs(mine - total_rotation(other))
            if relative < 0.5:
                # Same rigid group: they cannot separate, so no margin is owed.
                continue
            worst = max(worst, radius * math.radians(relative))
        grow[part_id] = max(MIN_GROW_PX, min(MAX_GROW_PX, math.ceil(worst)))
    return grow
