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
