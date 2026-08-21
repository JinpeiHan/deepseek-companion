#!/usr/bin/env python3
"""Cut one approved 512x512 master into the ~20 rig-ready layers of a rig pack.

The pipeline is five resumable stages:

``segment``
    Turn the hand-authored shape annotations into a mask per part. The master
    is already matted, so the *only* correct relationship is
    ``part_alpha = master_alpha * part_mask``: the masks must partition the
    master's opaque pixels, every pixel owned by exactly one part. That is
    asserted, not hoped for -- leftovers are handed to their nearest claimant
    rather than quietly dropped, and a pixel claimed twice is a hard failure.

``fill``
    Synthesise the pixels the master never showed. Two different problems hide
    under the word "fill" and the annotations keep them apart: ``grow`` is a
    couple of pixels of bleed past the cut line, sourced from the master itself
    so a seam cannot crack open under rotation; ``occlude`` is a genuinely
    hidden region -- the skull under the bangs, the dress under the apron, the
    tail root behind the body -- where no true pixel exists and the value has to
    come from mirroring the near-symmetric character, from a sampled base
    colour, or from ``cv2.inpaint``.

``chain``
    Derive the tail chain's bone pivots from the tail part rects and assert
    adjacent segments overlap, so bending cannot tear the chain apart.

``emit``
    Tight-crop every layer, write ``assets/pet-<pack>-rig/parts/*.png``, join
    the per-pack geometry to the shared ``scripts/rig_template.py`` ids, derive
    ``overflow`` from an actual parameter sweep, and validate the result against
    ``runtime.rig_pack`` before anything lands.

``qa``
    Render the two sheets a human has to look at. The exploded sheet shows what
    was cut; the *posed* sheet is the one that matters, because fill quality is
    invisible at rest and only a swept head, swung arms and a bent tail reveal
    a hole.

Every write is atomic (``.tmp`` then ``os.replace``), and ``--resume`` skips a
stage whose inputs fingerprint identically to the recorded run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import rig_template as T  # noqa: E402
from rig_overlap import required_grow  # noqa: E402
from runtime.rig_model import (  # noqa: E402
    RigModel,
    a_invert,
    a_multiply,
    a_translate,
)
from runtime.rig_pack import schema_errors  # noqa: E402

try:
    import cv2
except ImportError:  # pragma: no cover - cv2 is in the dsh-art env
    cv2 = None

try:
    from scipy import ndimage
except ImportError:  # pragma: no cover
    ndimage = None

STAGES = ("segment", "fill", "chain", "emit", "qa")

#: Alpha at or below this is background. The masters are matted with a soft
#: edge, so a hard 0 test would leave a halo of near-transparent pixels
#: unowned and trip the partition assertion for no visual reason.
ALPHA_FLOOR = 8


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


def sha256_obj(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


JSON_WIDTH = 116


def pretty_json(value, indent: int = 0) -> str:
    """Indent only what does not fit on a line.

    A rig manifest is mostly coordinate lists, and ``json.dumps(indent=2)``
    puts every number on its own line -- 4000 lines whose diffs are unreadable.
    Keeping short structures inline makes "which rect moved" a one-line diff.
    """
    pad = " " * indent
    flat = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    if len(flat) + indent <= JSON_WIDTH:
        return flat
    if isinstance(value, dict):
        items = [
            f"{pad}  {json.dumps(key, ensure_ascii=False)}: {pretty_json(item, indent + 2)}"
            for key, item in value.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list):
        items = [f"{pad}  {pretty_json(item, indent + 2)}" for item in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return flat


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_bytes(path, (pretty_json(obj) + "\n").encode("utf-8"))


def atomic_save_image(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    image.save(tmp, format="PNG", optimize=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Shape rasterisation
# --------------------------------------------------------------------------- #


def _ellipse_points(cx, cy, rx, ry, rot=0.0, steps=128):
    theta = math.radians(rot)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    points = []
    for index in range(steps):
        angle = 2.0 * math.pi * index / steps
        ex, ey = rx * math.cos(angle), ry * math.sin(angle)
        points.append((cx + ex * cos_t - ey * sin_t, cy + ex * sin_t + ey * cos_t))
    return points


def rasterise(shapes, size: int) -> np.ndarray:
    """Binary mask for a list of shape primitives.

    Deliberately *not* anti-aliased. A soft mask edge would make two adjacent
    parts each own a fraction of the same pixel, and the partition invariant --
    the cheap, exact check this whole pipeline leans on -- would degrade into a
    fuzzy image-similarity metric.
    """
    image = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(image)
    for shape in shapes or ():
        kind = shape[0]
        if kind == "rect":
            _, x, y, w, h = shape
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=255)
        elif kind == "poly":
            draw.polygon([tuple(point) for point in shape[1]], fill=255)
        elif kind == "ellipse":
            _, cx, cy, rx, ry = shape[:5]
            rot = shape[5] if len(shape) > 5 else 0.0
            draw.polygon(_ellipse_points(cx, cy, rx, ry, rot), fill=255)
        else:
            raise ValueError(f"unknown shape primitive {kind!r}")
    return np.asarray(image) > 127


# --------------------------------------------------------------------------- #
# Colour gates
# --------------------------------------------------------------------------- #


def colour_gate(name: str, rgb: np.ndarray) -> np.ndarray:
    """Named material predicates over the master's RGB.

    Shapes alone cannot follow a jagged hairline, but the palette can: the
    bangs are saturated blue and the forehead under them is skin, so the
    *shape* only has to be generous and the gate does the precise cut.
    """
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    if name == "skin":
        return (r > 175) & (r - b > 14) & (g > 130)
    if name == "hair":
        return (b > 70) & (b - r > 26)
    if name == "white":
        return (np.minimum(np.minimum(r, g), b) > 168) & ((np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)) < 62)
    if name == "navy":
        return (b > r) & ((r.astype(np.int32) + g + b) / 3 < 130)
    if name == "dark":
        return (r.astype(np.int32) + g + b) / 3 < 118
    if name == "notskin":
        return ~colour_gate("skin", rgb)
    if name == "nothair":
        return ~colour_gate("hair", rgb)
    raise ValueError(f"unknown colour gate {name!r}")


def apply_gates(mask: np.ndarray, spec: dict, rgb: np.ndarray) -> np.ndarray:
    for gate in spec.get("gate", ()) or ():
        mask = mask & colour_gate(gate, rgb)
    return mask


# --------------------------------------------------------------------------- #
# Stage: segment
# --------------------------------------------------------------------------- #


def _despeckle(masks: dict[str, np.ndarray], min_area: int) -> int:
    """Hand every small island to the part that surrounds it.

    An island is what a shape boundary drawn a few pixels off leaves behind: a
    scrap of skirt inside the hair polygon, say. At rest it is invisible, since
    it sits exactly where it came from. Under a pose it becomes a chip of dress
    flying off the side of the head, which is precisely the failure the posed
    sweep exists to catch -- so catch it here instead.
    """
    assert ndimage is not None, "scipy is required for component cleanup"
    ids = list(masks)
    moved = 0
    for _ in range(4):
        owner = np.zeros(next(iter(masks.values())).shape, dtype=np.int16)
        for index, part_id in enumerate(ids):
            owner[masks[part_id]] = index + 1
        changed = False
        for index, part_id in enumerate(ids):
            labelled, count = ndimage.label(masks[part_id])
            if count <= 1:
                continue
            areas = ndimage.sum(masks[part_id], labelled, range(1, count + 1))
            # Never dissolve a part's main body, however small the part is:
            # a cheek blush is legitimately tiny.
            keep = int(np.argmax(areas)) + 1
            for label_index, area in enumerate(areas, start=1):
                if area >= min_area or label_index == keep:
                    continue
                component = labelled == label_index
                ring = _binary_dilate(component, 2) & ~component & (owner > 0)
                neighbours = np.bincount(owner[ring], minlength=len(ids) + 1)
                neighbours[index + 1] = 0
                neighbours[0] = 0
                if not neighbours.any():
                    continue
                winner = ids[int(neighbours.argmax()) - 1]
                masks[part_id] &= ~component
                masks[winner] |= component
                owner[component] = ids.index(winner) + 1
                moved += int(area)
                changed = True
        if not changed:
            break
    return moved


def segment(master: np.ndarray, annotations: dict) -> dict[str, np.ndarray]:
    """Partition the master's opaque pixels across the parts. Exactly."""
    size = master.shape[0]
    rgb = master[..., :3]
    opaque = master[..., 3] > ALPHA_FLOOR
    regions = annotations["regions"]

    order = sorted(
        (part for part in T.PARTS if not part.get("synthetic")),
        key=lambda part: -part["claim"],
    )

    taken = np.zeros_like(opaque)
    masks: dict[str, np.ndarray] = {part["id"]: np.zeros_like(opaque) for part in T.PARTS}
    for part in order:
        spec = regions[part["id"]]
        claim = rasterise(spec.get("include"), size)
        exclude = spec.get("exclude")
        if exclude:
            claim = claim & ~rasterise(exclude, size)
        claim = apply_gates(claim, spec, rgb)
        claim = claim & opaque & ~taken
        masks[part["id"]] = claim
        taken |= claim

    # Anything the shapes missed goes to its nearest claimant. Dropping a pixel
    # instead would put a hole in the recomposite; letting two parts have it
    # would put a double-drawn seam in a rotated pose.
    leftover = opaque & ~taken
    if leftover.any():
        assert ndimage is not None, "scipy is required to resolve unclaimed pixels"
        _, indices = ndimage.distance_transform_edt(~taken, return_indices=True)
        owner = np.zeros(opaque.shape, dtype=np.int16)
        for index, part in enumerate(T.PARTS):
            owner[masks[part["id"]]] = index + 1
        nearest = owner[indices[0], indices[1]]
        for index, part in enumerate(T.PARTS):
            masks[part["id"]] |= leftover & (nearest == index + 1)

    _despeckle(masks, min_area=int(annotations.get("minComponentPx", 260)))

    union = np.zeros_like(opaque)
    overlap = np.zeros_like(opaque)
    for mask in masks.values():
        overlap |= union & mask
        union |= mask
    assert not overlap.any(), f"masks overlap on {int(overlap.sum())} pixels"
    assert np.array_equal(union, opaque), (
        f"mask union misses {int((opaque & ~union).sum())} opaque pixels and "
        f"claims {int((union & ~opaque).sum())} transparent ones"
    )
    return masks


# --------------------------------------------------------------------------- #
# Stage: fill
# --------------------------------------------------------------------------- #


def _binary_dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask
    assert ndimage is not None, "scipy is required for dilation"
    structure = ndimage.generate_binary_structure(2, 2)
    return ndimage.binary_dilation(mask, structure=structure, iterations=int(radius))


def boundary_radii(
    masks: "dict[str, np.ndarray]", annotations: dict
) -> "dict[str, dict[str, float]]":
    """For each pair of touching parts, how far their shared seam sits from the pivot.

    Distance is measured from the part's own pivot to the seam, because that is
    what sets how far the seam travels when the part turns. Using the farthest
    pixel instead would over-grow every layer: the tip of a strand of hair is
    free silhouette and has no neighbour to crack away from.
    """
    assert ndimage is not None, "scipy is required to measure part boundaries"
    pivots = annotations.get("partPivots", {})
    bone_pivots = annotations["bonePivots"]
    by_id = {part["id"]: part for part in T.PARTS}
    out: dict[str, dict[str, float]] = {}
    dilated = {pid: _binary_dilate(mask, 2) for pid, mask in masks.items()}
    for part_id, mask in masks.items():
        if not mask.any():
            continue
        pivot = pivots.get(part_id) or bone_pivots[by_id[part_id]["bone"]]
        radii: dict[str, float] = {}
        for other, other_mask in masks.items():
            if other == part_id or not other_mask.any():
                continue
            seam = dilated[part_id] & other_mask
            if seam.sum() < 12:
                continue
            ys, xs = np.where(seam)
            radii[other] = float(np.hypot(xs - pivot[0], ys - pivot[1]).mean())
        out[part_id] = radii
    return out


def build_closed_lid(master, cx, cy, rx, ry, pad=3):
    """Draw a closed anime eye for the lid layer: skin over the socket, the
    character's own lash line carried across it.

    The lid used to be stamped -- the top five rows of the open eye, gated to
    dark pixels and shifted down 13px, over an ellipse flooded from a skin
    sample. What came out was a cream blob the size of the whole eye with a
    smear along its bottom, so every blink and every poke reaction painted a
    pale patch over the character's face. That is the "it covers her eyes"
    report.

    Lifting the lash contour instead of stamping rows keeps the line the artist
    drew: its weight, its colour and its curve. The skin is sampled only from
    pixels that are actually skin, because the socket is ringed by hair and
    averaging across it dragged blue bands into the fill.
    """
    x0,x1 = cx-rx-pad, cx+rx+pad
    y0,y1 = cy-ry-pad, cy+ry+pad
    tile = master[y0:y1, x0:x1].astype(np.float32)
    h,w = tile.shape[:2]
    yy,xx = np.mgrid[0:h,0:w]
    ecx,ecy = cx-x0, cy-y0
    inside = ((xx-ecx)/rx)**2 + ((yy-ecy)/ry)**2 <= 1.0
    lum = tile[:,:,:3].mean(axis=2)

    # Skin is sampled from skin, not from whole rows: the socket is ringed by
    # hair, and averaging across it dragged blue bands into the fill.
    rgb = tile[:,:,:3]
    warm = (rgb[:,:,0] >= rgb[:,:,2] - 4) & (lum > 150) & (tile[:,:,3] > 200)
    ring = (~inside) & warm
    skin = np.median(rgb[ring], axis=0) if ring.sum() >= 12 else np.array([236.,226.,220.])

    out = tile.copy()
    out[inside, :3] = skin

    # The lash contour is the topmost dark pixel of each column of the open eye.
    # Lifting that curve wholesale and dropping it to the middle of the socket
    # keeps the artist's line weight and its shape; drawing an arc instead gave
    # a straight stub that read as a scratch.
    dark = (lum < 110) & inside
    target = ecy + 1
    for col in range(w):
        rows = np.where(dark[:, col])[0]
        if rows.size == 0:
            continue
        top = rows.min()
        thickness = 2 if rows.size > 3 else 1
        for k in range(thickness):
            src = min(top + k, h - 1)
            dst = target + k
            if 0 <= dst < h and inside[dst, col]:
                out[dst, col, :3] = tile[src, col, :3]

    out[:,:,3] = np.where(inside | (tile[:,:,3] > 0), 255, 0)
    return out.round().astype(np.uint8), (x0,y0)



def fill_part(
    part_id: str,
    mask: np.ndarray,
    master: np.ndarray,
    spec: dict,
    axis_x: float,
    mirror_source: np.ndarray | None,
    masks: dict[str, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Return (rgba, synthesised-pixel mask, per-part fill report).

    Source priority is the whole point, and it runs cheapest-and-truest first:
    real master pixels for the ``grow`` bleed, then the mirrored half of a
    near-symmetric character, then a flat colour sampled from the material
    itself blended by distance, and only then ``cv2.inpaint`` for what is left.
    """
    size = master.shape[0]
    rgb = master[..., :3].astype(np.float32)
    alpha = master[..., 3]
    opaque = alpha > ALPHA_FLOOR

    grow = int(spec.get("grow", 2))
    grown = _binary_dilate(mask, grow) & opaque if mask.any() else np.zeros_like(mask)

    # Margin is only allowed where a part drawn *above* this one covers it in
    # the rest pose. That makes the growth invisible by construction: the rest
    # recomposite cannot change, because every added pixel is painted over by
    # something later in z order. It surfaces exactly when that neighbour moves
    # away, which is the hole it exists to fill.
    #
    # Growing without this restriction is what pushed the rest recomposite from
    # 0.47 to 7.40 mean |dRGB|: body and apron, widened to 40px and 28px, were
    # reaching past their covering layers and repainting the silhouette.
    if masks is not None:
        covered = np.zeros_like(mask)
        own_z = next((entry["z"] for entry in T.PARTS if entry["id"] == part_id), None)
        if own_z is not None:
            for entry in T.PARTS:
                if entry["z"] > own_z and entry["id"] in masks:
                    covered |= masks[entry["id"]]
        grown = mask | (grown & covered)

    # The occluded region is best expressed as "whoever is drawn on top of me":
    # ``occludeParts`` unions those parts' own masks, so the fill is exactly as
    # large as the hole and not one pixel larger. A free-hand ``occlude`` shape
    # that overshoots would paint this layer over a *neighbour* that sits lower
    # in z -- which is how the first pass erased the character's side hair.
    occlude = np.zeros_like(mask)
    if spec.get("occlude"):
        occlude |= rasterise(spec["occlude"], size)
    for other in spec.get("occludeParts", ()) or ():
        assert masks is not None, "occludeParts needs the full mask set"
        occlude |= _binary_dilate(masks[other], int(spec.get("occludeGrow", 3)))
    if spec.get("occludeClip"):
        occlude &= rasterise(spec["occludeClip"], size)
    # A hidden region is by definition *inside* the character. Letting the
    # dilation of an occludeParts mask spill past the silhouette would widen the
    # rest-pose composite beyond the master -- the validator's ``spill`` metric
    # exists to catch exactly that, so do not create it in the first place.
    occlude &= _binary_dilate(opaque, int(spec.get("occludeOutsidePx", 0)))
    for gate in spec.get("occludeGate", ()) or ():
        occlude = occlude & colour_gate(gate, master[..., :3])

    target = mask | grown | occlude
    if not target.any():
        raise AssertionError(f"part {part_id!r} has an empty target region")

    out = np.zeros((size, size, 3), dtype=np.float32)
    valid = mask.copy()
    out[valid] = rgb[valid]

    report = {"claimed": int(mask.sum())}

    # 1. grow -- master pixels, so the seam is literally the original art.
    bleed = grown & ~valid
    out[bleed] = rgb[bleed]
    valid |= bleed
    report["grow"] = int(bleed.sum())

    # 2. mirror across the character's vertical axis.
    todo = target & ~valid
    mirrored = 0
    if todo.any() and spec.get("mirror", True):
        source = mirror_source if mirror_source is not None else valid
        xs = np.arange(size)
        flip = np.clip(np.rint(2.0 * axis_x - xs).astype(int), 0, size - 1)
        source_flipped = source[:, flip]
        rgb_flipped = rgb[:, flip, :]
        usable = todo & source_flipped
        out[usable] = rgb_flipped[usable]
        valid |= usable
        mirrored = int(usable.sum())
    report["mirror"] = mirrored

    # 3/4. inpaint the continuation, then -- for a region whose true content is
    # a flat material (skin skull, navy dress, navy tail root) -- pull the deep
    # interior toward a colour sampled from that material. TELEA alone smears
    # whatever happens to border the hole across it; a flat colour alone steps
    # at the boundary. Blending them by distance from real art gives a
    # continuous edge and a clean interior.
    todo = target & ~valid
    inpainted = base_used = 0
    if todo.any():
        assert cv2 is not None, "cv2 is required for inpainting"
        assert ndimage is not None, "scipy is required for distance blending"
        distance = ndimage.distance_transform_edt(~valid)
        bgr = np.clip(out[..., ::-1], 0, 255).astype(np.uint8)
        painted = cv2.inpaint(bgr, todo.astype(np.uint8) * 255, 12, cv2.INPAINT_TELEA)
        painted = painted[..., ::-1].astype(np.float32)
        if spec.get("base"):
            bx, by = spec["base"]
            base_rgb = rgb[int(by), int(bx)].copy()
            ramp = np.clip(distance / float(spec.get("baseRamp", 14)), 0.0, 1.0)
            weight = ramp[todo][:, None]
            out[todo] = base_rgb[None, :] * weight + painted[todo] * (1.0 - weight)
            base_used = int(todo.sum())
        else:
            out[todo] = painted[todo]
        valid |= todo
        inpainted = int(todo.sum())
    report["inpaint"] = inpainted
    report["base"] = base_used

    # 5. stamps -- copy a piece of the master to somewhere it never appears.
    # The eyelids are the case that forces this: a closed eye needs the lash
    # line the master only draws in the open position, moved down to where the
    # lid closes. Copying real art beats drawing a synthetic arc.
    stamped = 0
    stamped_mask = np.zeros_like(mask)
    for stamp in spec.get("stamp", ()) or ():
        source = np.zeros_like(mask)
        if stamp.get("from"):
            source |= rasterise(stamp["from"], size)
        if stamp.get("fromPart"):
            assert masks is not None, "stamp fromPart needs the full mask set"
            source |= masks[stamp["fromPart"]]
        for gate in stamp.get("gate", ()) or ():
            source = source & colour_gate(gate, master[..., :3])
        source = source & opaque
        # ``topRows`` keeps only the first N set pixels of each column, which is
        # exactly "the upper lash line of this eye" without needing a shape that
        # traces it -- and it follows the real arc, so the closed eye keeps the
        # character's own lash weight instead of a drawn-on curve.
        rows = int(stamp.get("topRows", 0) or 0)
        if rows:
            source = source & (np.cumsum(source, axis=0) <= rows)
        dx, dy = int(stamp["offset"][0]), int(stamp["offset"][1])
        moved = np.roll(np.roll(source, dy, axis=0), dx, axis=1) & target
        # rolled RGB lines up with the rolled mask: source_rgb[y, x] == rgb[y-dy, x-dx]
        source_rgb = np.roll(np.roll(rgb, dy, axis=0), dx, axis=1)
        out[moved] = source_rgb[moved]
        valid |= moved
        stamped_mask |= moved
        stamped += int(moved.sum())
    report["stamp"] = stamped

    # 6. closed eye -- the lid is a drawing, not a stitched-together fill.
    # base+stamp gets the pieces but not the result: the flood fills the whole
    # socket with skin and the stamp lands the lash at the bottom of it, which
    # is why the lid read as a pale blob with a smear under it.
    drawn_mask = np.zeros_like(mask)
    eye = spec.get("closedEye")
    if eye:
        cx, cy, rx, ry = (int(v) for v in eye)
        lid, (ox, oy) = build_closed_lid(master, cx, cy, rx, ry)
        h, w = lid.shape[:2]
        region = np.zeros_like(mask)
        region[oy:oy + h, ox:ox + w] = lid[:, :, 3] > 0
        region &= target
        patch = np.zeros_like(out)
        patch[oy:oy + h, ox:ox + w] = lid[:, :, :3]
        out[region] = patch[region]
        valid |= region
        drawn_mask |= region
        report["closedEye"] = int(region.sum())

    synthesised = target & ~mask
    # Smooth only the synthesised interior: TELEA leaves radial streaks that a
    # posed sweep turns into visible spokes. The one-pixel erosion keeps the
    # boundary with real art untouched.
    if synthesised.any():
        blurred = np.asarray(
            Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).filter(
                ImageFilter.GaussianBlur(1.6)
            ),
            dtype=np.float32,
        )
        # The closed lid is drawn art like a stamp is, so it is excluded from
        # the smoothing for the same reason: the blur exists to kill inpaint
        # streaks, and running it over the lash line softened it to a smudge.
        deep = (~_binary_dilate(~synthesised, 1)
                & ~_binary_dilate(stamped_mask, 1)
                & ~_binary_dilate(drawn_mask, 1))
        out[deep] = blurred[deep]

    out_alpha = np.zeros((size, size), dtype=np.uint8)
    out_alpha[mask] = alpha[mask]
    out_alpha[synthesised] = 255

    rgba = np.dstack([np.clip(out, 0, 255).astype(np.uint8), out_alpha])
    report["synthesised"] = int(synthesised.sum())
    report["total"] = int(target.sum())
    return rgba, synthesised, report


# --------------------------------------------------------------------------- #
# Stage: chain
# --------------------------------------------------------------------------- #


def chain_geometry(bboxes: dict[str, tuple[int, int, int, int]], annotations: dict) -> dict:
    """Assert the tail chain's segments overlap enough to bend without tearing."""
    bones = T.CHAINS["tail"]["bones"]
    parts = ["tail_0", "tail_1", "tail_2", "tail_3"]
    report = {"segments": []}
    for index in range(len(parts) - 1):
        a = bboxes[parts[index]]
        b = bboxes[parts[index + 1]]
        overlap_x = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
        overlap_y = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
        overlap = min(overlap_x, overlap_y)
        report["segments"].append(
            {"from": parts[index], "to": parts[index + 1], "overlapPx": int(overlap)}
        )
        assert overlap >= 2, (
            f"chain segments {parts[index]}/{parts[index + 1]} overlap only "
            f"{overlap}px; a bend would tear them apart"
        )
    report["bones"] = list(bones)
    return report


# --------------------------------------------------------------------------- #
# Stage: emit
# --------------------------------------------------------------------------- #


def tight_bbox(alpha: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(alpha > 0)
    assert xs.size, "layer is empty"
    return int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)



def _merge_baked_clips(clips: dict, manifest_path: Path) -> dict:
    """Carry any already-baked clip through a re-emit.

    ``emit`` rebuilds the manifest from the template, and the template has no
    idea which clips were later replaced by generated frames. Without this a
    re-emit silently drops every ``frames`` list, so a manifest that took hours
    of generation to fill comes back pointing at nothing while the PNGs sit
    untouched on disk.
    """
    if not manifest_path.exists():
        return clips
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("clips", {})
    except (OSError, ValueError):
        return clips
    for name, previous in existing.items():
        frames = previous.get("frames")
        if not frames or name not in clips:
            continue
        clips[name] = {
            "loop": bool(previous.get("loop", True)),
            "frames": list(frames),
            "frameMs": int(previous.get("frameMs", 110)),
        }
        if previous.get("motion"):
            clips[name]["motion"] = previous["motion"]
    return clips


def build_manifest(pack: str, annotations: dict, rects: dict, master_sha: str) -> dict:
    manifest_path = REPO_ROOT / "assets" / f"pet-{pack}-rig.json"
    pivots = annotations["bonePivots"]
    part_pivots = annotations.get("partPivots", {})

    bones = []
    for bone in T.BONES:
        entry = {"id": bone["id"], "parent": bone["parent"], "pivot": list(pivots[bone["id"]])}
        if bone.get("chain"):
            entry["chain"] = bone["chain"]
            entry["chainIndex"] = bone["chainIndex"]
        bones.append(entry)

    # Paint order is a property of the *drawing*, not of the rig: on the
    # standard proportion the tail hangs clear of the hair, on the 2-head-tall
    # chibi it lies in front of it. Ids stay shared; z is allowed to differ.
    part_z = annotations.get("partZ", {})
    parts = []
    for part in T.PARTS:
        rect = rects[part["id"]]
        default_pivot = [rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0]
        entry = {
            "id": part["id"],
            "file": f"parts/{part['id']}.png",
            "z": part_z.get(part["id"], part["z"]),
            "bone": part["bone"],
            "rect": [int(v) for v in rect],
            "pivot": [float(v) for v in part_pivots.get(part["id"], default_pivot)],
        }
        if part.get("hitGroup"):
            entry["hitGroup"] = part["hitGroup"]
        if part.get("strips"):
            entry["strips"] = part["strips"]
            entry["stripBones"] = list(part["stripBones"])
        parts.append(entry)

    manifest = {
        "formatVersion": 3,
        "renderer": "rig",
        "characterId": annotations["characterId"],
        "sourceWidth": T.CANVAS,
        "sourceHeight": T.CANVAS,
        "maxFrameWidth": T.CANVAS,
        "maxFrameHeight": T.CANVAS,
        "logicalWidth": T.LOGICAL_WIDTH,
        "logicalHeight": T.LOGICAL_HEIGHT,
        "footAnchor": list(T.FOOT_ANCHOR),
        "bubbleAnchor": list(T.BUBBLE_ANCHOR),
        "overflow": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        "params": T.PARAMS,
        "bones": bones,
        "chains": {name: json.loads(json.dumps(spec)) for name, spec in T.CHAINS.items()},
        "parts": parts,
        "bindings": [json.loads(json.dumps(b)) for b in T.BINDINGS],
        "clips": _merge_baked_clips(json.loads(json.dumps(T.CLIPS)), manifest_path),
        "stateMap": dict(T.STATE_MAP),
        "workingActivityMap": dict(T.WORKING_ACTIVITY_MAP),
        "idleMicroClips": list(T.IDLE_MICRO_CLIPS),
        "hitGroups": {name: list(members) for name, members in T.HIT_GROUPS.items()},
        "interactions": json.loads(json.dumps(T.INTERACTIONS)),
        "qa": {
            "master": annotations["master"],
            "masterSha256": master_sha,
            "restHiddenParts": list(T.REST_HIDDEN_PARTS),
            "axisX": annotations["axisX"],
        },
    }

    # Overflow is measured, not guessed: sweep the declared parameter space and
    # reserve exactly the padding the worst pose needs (plus 4px of slack).
    model = RigModel(manifest)
    rest = model.rest_bbox()
    sweep = model.sweep_bbox(9)
    pad = 4.0
    manifest["overflow"] = {
        "left": math.ceil(max(0.0, rest[0] - sweep[0]) + pad),
        "top": math.ceil(max(0.0, rest[1] - sweep[1]) + pad),
        "right": math.ceil(max(0.0, (sweep[0] + sweep[2]) - (rest[0] + rest[2])) + pad),
        "bottom": math.ceil(max(0.0, (sweep[1] + sweep[3]) - (rest[1] + rest[3])) + pad),
    }
    return manifest


# --------------------------------------------------------------------------- #
# Stage: qa -- compositing with our own affine math
# --------------------------------------------------------------------------- #


QA_PAD = 90


def _pil_affine(matrix):
    """PIL wants the dest->source map; our matrices are source->dest."""
    inverse = a_invert(matrix)
    if inverse is None:
        return None
    a, b, c, d, e, f = inverse
    return (a, c, e, b, d, f)


def compose(model: RigModel, layers: dict[str, Image.Image], params: dict) -> Image.Image:
    """Composite the parts for one pose. The only artefact that shows bad fill."""
    size = T.CANVAS + 2 * QA_PAD
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shift = a_translate(QA_PAD, QA_PAD)
    for transform in model.solve(params):
        layer = layers[transform.part_id]
        if transform.opacity <= 0.001:
            continue
        pieces = []
        if transform.strip_matrices:
            count = len(transform.strip_matrices)
            rect = transform.src_rect
            for index, strip in enumerate(transform.strip_matrices):
                top = rect[1] + rect[3] * index / count
                bottom = rect[1] + rect[3] * (index + 1) / count
                band = Image.new("RGBA", (T.CANVAS, T.CANVAS), (0, 0, 0, 0))
                band.paste(
                    layer.crop((0, int(math.floor(top)), T.CANVAS, int(math.ceil(bottom)))),
                    (0, int(math.floor(top))),
                )
                pieces.append((band, strip))
        else:
            pieces.append((layer, transform.matrix))
        for band, matrix in pieces:
            coeffs = _pil_affine(a_multiply(shift, matrix))
            if coeffs is None:
                continue
            # The source stays in master pixels and only the *output* is padded,
            # so the affine we invert is exactly the solver's matrix followed by
            # the pad translation -- no hidden offset to get wrong.
            warped = band.transform(
                (size, size), Image.AFFINE, coeffs, resample=Image.BICUBIC
            )
            if transform.opacity < 0.999:
                alpha = warped.getchannel("A").point(lambda v: int(v * transform.opacity))
                warped.putalpha(alpha)
            canvas.alpha_composite(warped)
    return canvas


def _on_white(image: Image.Image) -> Image.Image:
    plate = Image.new("RGBA", image.size, (255, 255, 255, 255))
    plate.alpha_composite(image)
    return plate.convert("RGB")


def qa_sheets(pack: str, model: RigModel, layers: dict, synthesised: dict, out_dir: Path) -> list[Path]:
    written = []

    # -- exploded ----------------------------------------------------------- #
    cell = 176
    columns = 5
    rows = math.ceil(len(T.PARTS) / columns)
    sheet = Image.new("RGB", (columns * cell, rows * cell + 26), (250, 250, 252))
    draw = ImageDraw.Draw(sheet)
    for index, part in enumerate(T.PARTS):
        col, row = index % columns, index // columns
        thumb = _on_white(layers[part["id"]]).resize((cell - 8, cell - 8), Image.LANCZOS)
        sheet.paste(thumb, (col * cell + 4, row * cell + 22))
        share = synthesised.get(part["id"], {})
        label = (
            f"{part['id']}  z{part['z']}  "
            f"synth {share.get('synthesised', 0)}/{share.get('total', 0)}"
        )
        draw.text((col * cell + 6, row * cell + 8), label, fill=(20, 20, 30))
    path = out_dir / "exploded.png"
    atomic_save_image(path, sheet)
    written.append(path)

    # -- overlay of the cut ------------------------------------------------- #
    palette = [
        (230, 60, 60), (60, 160, 230), (90, 200, 110), (240, 170, 40),
        (170, 100, 220), (60, 210, 200), (240, 110, 180), (140, 140, 60),
        (100, 120, 240), (200, 80, 120), (70, 190, 150), (220, 130, 70),
        (120, 200, 240), (180, 60, 200), (90, 90, 200), (200, 200, 70),
        (60, 130, 90), (230, 150, 150), (110, 60, 130), (150, 170, 200),
    ]
    overlay = Image.new("RGB", (T.CANVAS, T.CANVAS), (255, 255, 255))
    for index, part in enumerate(T.PARTS):
        colour = palette[index % len(palette)]
        tint = Image.new("RGBA", (T.CANVAS, T.CANVAS), colour + (255,))
        tint.putalpha(layers[part["id"]].getchannel("A").point(lambda v: min(200, v)))
        overlay.paste(tint, (0, 0), tint)
    path = out_dir / "cut-overlay.png"
    atomic_save_image(path, overlay)
    written.append(path)

    # -- posed sweep -------------------------------------------------------- #
    poses = [
        ("rest", {}),
        ("head -18", {"headAngleZ": -18, "headAngleY": -18, "eyeBallX": -1}),
        ("head +18", {"headAngleZ": 18, "headAngleY": 18, "eyeBallX": 1}),
        ("look down", {"headAngleX": 14, "eyeBallY": 1, "headAngleY": -9}),
        ("blink", {"eyeOpen": 0.0}),
        ("talk", {"mouthOpen": 1.0, "headAngleX": -6}),
        ("poked", {"cheekSquash": 1.0, "mouthOpen": 0.6}),
        ("arms out", {"armSwingL": 14, "armSwingR": -14, "bodyAngleZ": 4}),
        ("arms in", {"armSwingL": -14, "armSwingR": 14}),
        ("tail up", {"tail0": 12, "tail1": 14, "tail2": 15, "tail3": 15}),
        ("tail down", {"tail0": -12, "tail1": -14, "tail2": -15, "tail3": -15}),
        ("drag", {"rootLeanZ": 8, "rootBobY": -6, "hairSway": 8, "tail0": -10, "tail1": -12}),
    ]
    thumb = 300
    columns = 4
    rows = math.ceil(len(poses) / columns)
    sweep = Image.new("RGB", (columns * thumb, rows * (thumb + 20)), (248, 248, 250))
    draw = ImageDraw.Draw(sweep)
    for index, (name, params) in enumerate(poses):
        col, row = index % columns, index // columns
        frame = _on_white(compose(model, layers, params)).resize((thumb, thumb), Image.LANCZOS)
        sweep.paste(frame, (col * thumb, row * (thumb + 20) + 18))
        draw.text((col * thumb + 6, row * (thumb + 20) + 4), name, fill=(20, 20, 30))
    path = out_dir / "pose-sweep.png"
    atomic_save_image(path, sweep)
    written.append(path)

    # -- rest recomposite, at 1:1, for the eye ------------------------------ #
    path = out_dir / "rest-composite.png"
    atomic_save_image(path, _on_white(compose(model, layers, {})))
    written.append(path)
    return written


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def stage_fingerprint(stage: str, annotations: dict, master_sha: str, flags: dict) -> str:
    return sha256_obj(
        {
            "stage": stage,
            "master": master_sha,
            "annotations": annotations,
            "template": sha256_file(REPO_ROOT / "scripts" / "rig_template.py"),
            "flags": flags,
        }
    )


def run(args) -> int:
    pack = args.pack
    work = REPO_ROOT / "art-references" / "rig" / pack
    annotations_path = Path(args.annotations) if args.annotations else work / "annotations.json"
    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))

    master_path = REPO_ROOT / (args.master or annotations["master"])
    master_sha = sha256_file(master_path)
    if annotations.get("masterSha256") and annotations["masterSha256"] != master_sha:
        raise SystemExit(
            f"annotations were authored against master {annotations['masterSha256'][:12]} "
            f"but {master_path} is {master_sha[:12]}"
        )

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    for stage in stages:
        assert stage in STAGES, f"unknown stage {stage!r}"

    state_path = work / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    flags = {"seed": args.seed}

    pack_root = REPO_ROOT / "assets" / f"pet-{pack}-rig"
    parts_dir = pack_root / "parts"
    manifest_path = REPO_ROOT / "assets" / f"pet-{pack}-rig.json"
    masks_dir = work / "masks"
    filled_dir = work / "filled"
    qa_dir = work / "qa"

    master = np.asarray(Image.open(master_path).convert("RGBA"))
    assert master.shape == (T.CANVAS, T.CANVAS, 4), f"master must be {T.CANVAS}x{T.CANVAS} RGBA"

    if args.dry_run:
        print(f"[dry-run] pack={pack} master={master_path} stages={stages}")
        print(f"[dry-run] would write {manifest_path} and {len(T.PARTS)} layers under {parts_dir}")
        return 0

    def should_skip(stage: str) -> bool:
        if args.force or not args.resume:
            return False
        return state.get(stage) == stage_fingerprint(stage, annotations, master_sha, flags)

    def mark(stage: str) -> None:
        state[stage] = stage_fingerprint(stage, annotations, master_sha, flags)
        atomic_write_json(state_path, state)

    masks: dict[str, np.ndarray] | None = None
    layers: dict[str, Image.Image] = {}
    reports: dict[str, dict] = {}

    # -- segment ------------------------------------------------------------ #
    if "segment" in stages:
        if should_skip("segment") and masks_dir.exists():
            print("[segment] up to date, skipping")
        else:
            masks = segment(master, annotations)
            if masks_dir.exists():
                shutil.rmtree(masks_dir)
            for part_id, mask in masks.items():
                atomic_save_image(
                    masks_dir / f"{part_id}.png",
                    Image.fromarray((mask * 255).astype(np.uint8), mode="L"),
                )
            covered = sum(int(mask.sum()) for mask in masks.values())
            print(f"[segment] {len(masks)} masks, {covered} opaque pixels partitioned exactly")
            mark("segment")
    if masks is None:
        masks = {}
        for part in T.PARTS:
            path = masks_dir / f"{part['id']}.png"
            masks[part["id"]] = np.asarray(Image.open(path).convert("L")) > 127

    # -- fill --------------------------------------------------------------- #
    if "fill" in stages:
        axis_x = float(annotations["axisX"])
        mirror_partners = annotations.get("mirrorPartners", {})
        # How much each part must overlap its neighbours, derived from the rig's
        # own declared rotation limits rather than guessed. See rig_overlap: an
        # exact partition cracks the instant two parts turn by different amounts,
        # and the growth below is clipped to the silhouette, so the margin only
        # ever hides under the neighbour it is protecting against.
        neighbours = boundary_radii(masks, annotations)
        computed_grow = required_grow(T.PARTS, T.BONES, T.PARAMS, T.BINDINGS, neighbours)
        widened = {k: v for k, v in computed_grow.items()
                   if v > int(annotations["regions"][k].get("grow", 2))}
        if widened:
            print(f"[fill] seam margin widened for {len(widened)} part(s): "
                  + ", ".join(f"{k}->{v}px" for k, v in sorted(widened.items())))
        for part in T.PARTS:
            part_id = part["id"]
            spec = dict(annotations["regions"][part_id])
            # The annotation may ask for more, never less: a hand-tuned value
            # that already exceeds the computed need is left alone.
            spec["grow"] = max(int(spec.get("grow", 2)), computed_grow[part_id])
            partner = mirror_partners.get(part_id)
            source = masks[partner] if partner else None
            rgba, synth, report = fill_part(
                part_id, masks[part_id], master, spec, axis_x, source, masks
            )
            image = Image.fromarray(rgba, mode="RGBA")
            atomic_save_image(filled_dir / f"{part_id}.png", image)
            layers[part_id] = image
            reports[part_id] = report
        atomic_write_json(work / "fill-report.json", reports)
        total_synth = sum(r["synthesised"] for r in reports.values())
        print(f"[fill] {total_synth} synthesised pixels across {len(reports)} parts")
        mark("fill")
    needs_layers = bool({"chain", "emit", "qa"} & set(stages))
    if not layers and needs_layers:
        for part in T.PARTS:
            layers[part["id"]] = Image.open(filled_dir / f"{part['id']}.png").convert("RGBA")
        if (work / "fill-report.json").exists():
            reports = json.loads((work / "fill-report.json").read_text(encoding="utf-8"))
    if not needs_layers:
        return 0

    rects = {
        part["id"]: tight_bbox(np.asarray(layers[part["id"]])[..., 3]) for part in T.PARTS
    }

    # -- chain -------------------------------------------------------------- #
    if "chain" in stages:
        report = chain_geometry(rects, annotations)
        atomic_write_json(work / "chain-report.json", report)
        print(
            "[chain] tail overlaps: "
            + ", ".join(f"{s['from']}->{s['to']} {s['overlapPx']}px" for s in report["segments"])
        )
        mark("chain")

    # -- emit --------------------------------------------------------------- #
    if "emit" in stages:
        manifest = build_manifest(pack, annotations, rects, master_sha)
        errors = schema_errors(manifest)
        assert not errors, "rig manifest fails runtime validation:\n  " + "\n  ".join(errors)
        if parts_dir.exists():
            shutil.rmtree(parts_dir)
        for part in T.PARTS:
            part_id = part["id"]
            x, y, w, h = rects[part_id]
            atomic_save_image(parts_dir / f"{part_id}.png", layers[part_id].crop((x, y, x + w, y + h)))
        atomic_write_json(manifest_path, manifest)
        print(f"[emit] wrote {manifest_path.relative_to(REPO_ROOT)} and {len(T.PARTS)} layers")
        print(f"[emit] overflow = {manifest['overflow']}")
        mark("emit")

    # -- qa ----------------------------------------------------------------- #
    if "qa" in stages:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        model = RigModel(manifest)
        model.validate()
        written = qa_sheets(pack, model, layers, reports, qa_dir)
        for path in written:
            print(f"[qa] {path.relative_to(REPO_ROOT)}")
        mark("qa")

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pack", required=True, help="pack id, e.g. standard or chibi")
    parser.add_argument("--master", help="override the master path from the annotations")
    parser.add_argument("--annotations", help="override the annotations path")
    parser.add_argument("--stages", default=",".join(STAGES))
    parser.add_argument("--resume", action="store_true", help="skip stages whose inputs are unchanged")
    parser.add_argument("--force", action="store_true", help="re-run every requested stage")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
