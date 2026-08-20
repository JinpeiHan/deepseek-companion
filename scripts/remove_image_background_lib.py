"""Shared matting and placement primitives for the art pipeline.

Extracted from remove-image-background.py so normalize-pack-scale.py can reuse
the exact same matting rather than growing a second implementation. That script
name has hyphens and cannot be imported, hence this module.

The non-obvious constraint lives in cut_alpha: gpt-image-2 rejects
`background=transparent`, so frames arrive on a flat white plate. `u2netp`
cannot tell the white apron, frilled hem and white socks from that plate and
eats them, so `isnet-anime` is used and its mask is hardened above a low
threshold. RGB is taken from the untouched source, which keeps hair gaps
transparent without leaving black fringes.
"""

from __future__ import annotations

import numpy as np
from PIL import Image
from rembg import new_session, remove
from scipy import ndimage

TARGET = (512, 512)
# Matches footAnchor/logical sizing in the pack manifests: the character stands
# on 97% of the frame height and never fills more than 90% of it.
FOOT_ANCHOR_Y = 0.97
MAX_HEIGHT_RATIO = 0.90
MAX_WIDTH_RATIO = 0.94
# Anything the matting model gives more than this much opacity is treated as
# character. Kept low on purpose: white-on-white cloth comes back faint.
SOLID_ALPHA = 30


def new_matting_session(model: str = "isnet-anime"):
    return new_session(model, providers=["CPUExecutionProvider"])


def cut_alpha(source: Image.Image, session) -> Image.Image:
    """Return the source RGB with a hardened alpha channel."""
    mask = np.asarray(remove(source, session=session).convert("RGBA"))[:, :, 3]
    solid = mask > SOLID_ALPHA
    labels, count = ndimage.label(solid)
    if count > 1:
        sizes = ndimage.sum(solid, labels, range(1, count + 1))
        solid = labels == (int(np.argmax(sizes)) + 1)
    if not solid.any():
        raise ValueError("matting produced an empty mask")
    cut = source.copy()
    cut.putalpha(Image.fromarray(np.where(solid, 255, 0).astype(np.uint8)))
    return cut


def bleed_edges(image: Image.Image, iterations: int = 8) -> Image.Image:
    """Push character colour outward under the transparent pixels.

    Resizing an RGBA image interpolates RGB across the alpha edge, so whatever
    colour sits *outside* the silhouette gets pulled into the semi-transparent
    rim. MiniMax-H3 renders the pet on a black plate, which turned the white
    apron's edge into a dark fringe: measured mean RGB 30/32/38 on the rim.

    Replacing the transparent RGB with the nearest opaque colour before any
    resize makes that interpolation a no-op. Alpha is untouched, so the
    silhouette does not change -- only the colour hiding under it.
    """
    rgba = np.asarray(image.convert("RGBA")).copy()
    alpha = rgba[:, :, 3]
    known = alpha > 0
    if not known.any() or known.all():
        return image
    rgb = rgba[:, :, :3].astype(np.float32)
    filled = known.copy()
    for _ in range(iterations):
        if filled.all():
            break
        # One dilation step: every unfilled pixel takes the mean of its filled
        # neighbours, so colour spreads outward one ring at a time.
        neighbour_sum = np.zeros_like(rgb)
        neighbour_count = np.zeros(filled.shape, dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted_rgb = np.roll(rgb, (dy, dx), axis=(0, 1))
            shifted_filled = np.roll(filled, (dy, dx), axis=(0, 1))
            neighbour_sum += shifted_rgb * shifted_filled[:, :, None]
            neighbour_count += shifted_filled
        target = (~filled) & (neighbour_count > 0)
        if not target.any():
            break
        rgb[target] = neighbour_sum[target] / neighbour_count[target, None]
        filled |= target
    rgba[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def fit(
    image: Image.Image,
    box: tuple[int, int, int, int],
    scale: float,
    lift_px: float = 0.0,
) -> Image.Image:
    """Place one character into a 512x512 frame using a shared scale factor.

    ``lift_px`` raises the character above the foot anchor, in output pixels.
    Bottom-anchoring every frame is right for a frame pack -- the pet stands on
    a point, and any bob is added procedurally at paint time -- but it destroys
    vertical motion that belongs to the clip itself, which a baked clip has no
    other way to express. A `dragging` clip mattes to a dead-flat foot line at
    497 in all 16 frames without this.
    """
    character = bleed_edges(image.crop(box))
    size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
    character = character.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", TARGET, (0, 0, 0, 0))
    left = max(0, (TARGET[0] - size[0]) // 2)
    top = round(TARGET[1] * FOOT_ANCHOR_Y) - size[1] - round(lift_px)
    top = max(0, min(top, TARGET[1] - size[1]))
    # Straight copy, NOT paste(..., mask=character). Using the image as its own
    # mask composites it over the transparent black canvas, so every
    # semi-transparent pixel gets its RGB multiplied by alpha and its alpha
    # squared: a 50% white rim pixel came out (128,128,128,64) instead of
    # (255,255,255,128). That is what darkened the character's edge.
    canvas.paste(character, (left, top))
    return canvas


# The plate colour is not constant across a generated clip. MiniMax renders the
# pet on a flat plate, but the first few and last few frames of the video come
# back on a *different* plate -- typically pure black where the body of the clip
# is white, sometimes a transitional grey. Measured across 83 clips: 46 of them
# contained such frames, 260 frames in total.
#
# That matters because every frame is matted independently against whatever is
# behind it. An anti-aliased silhouette edge is a blend of character and plate,
# and cut_alpha's hard threshold keeps those blended pixels as opaque character
# -- so a frame shot on black gets a dark rim and a frame shot on white gets a
# light rim, and the same clip visibly flickers between the two. Decimating the
# video uniformly across its whole length guaranteed those frames were picked.
#
# No amount of post-hoc matting recovers the true edge colour, because the plate
# is baked into the pixels. Dropping the frames does, and it is free: the video
# is 124 frames long and only 16 are kept, so there is ample material inside the
# plate-consistent run.
PLATE_TOLERANCE = 40.0
# A frame can sit inside PLATE_TOLERANCE and still be visibly mid-fade: measured
# at the run boundaries, the first kept frame was 22-34 grey levels off the
# plate the rest of the clip settles on. One such frame is enough to make the
# clip's edge flicker, and it accounted for 8 of the 15 clips that still had
# inconsistent rims after the run was introduced. So the run is found with the
# loose tolerance and then trimmed against its own settled median.
PLATE_SETTLED = 8.0


def plate_colour_of(image: Image.Image) -> float:
    """Mean luminance of the 4px frame border, i.e. the plate behind the pet."""
    array = np.asarray(image.convert("RGB")).astype(np.float32)
    border = np.concatenate(
        [
            array[:4].reshape(-1, 3),
            array[-4:].reshape(-1, 3),
            array[:, :4].reshape(-1, 3),
            array[:, -4:].reshape(-1, 3),
        ]
    )
    return float(border.mean())


def plate_consistent_run(plates: "list[float]") -> tuple[int, int]:
    """Longest run of frames sharing the clip's dominant plate colour.

    Returns a half-open ``(start, stop)``. The dominant plate is the median, so
    a clip that is mostly black tolerates a white intro rather than the other
    way round. A *run* rather than a mask because the kept frames have to stay
    contiguous in time -- stitching around a dropped middle frame would put a
    jump cut in the animation.
    """
    if not plates:
        raise ValueError("no frames to inspect")
    dominant = 255.0 if float(np.median(plates)) > 128.0 else 0.0
    best = (0, 0)
    start = None
    for index, plate in enumerate([*plates, None]):
        matches = plate is not None and abs(plate - dominant) < PLATE_TOLERANCE
        if matches and start is None:
            start = index
        elif not matches and start is not None:
            if index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    if best[1] - best[0] < 2:
        # Nothing consistent: keep everything rather than emit an empty clip.
        return (0, len(plates))

    start, stop = best
    settled = float(np.median(plates[start:stop]))
    while stop - start > 2 and abs(plates[start] - settled) > PLATE_SETTLED:
        start += 1
    while stop - start > 2 and abs(plates[stop - 1] - settled) > PLATE_SETTLED:
        stop -= 1
    return (start, stop)
