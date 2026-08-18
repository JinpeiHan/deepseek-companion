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


def fit(image: Image.Image, box: tuple[int, int, int, int], scale: float) -> Image.Image:
    """Place one character into a 512x512 frame using a shared scale factor."""
    character = image.crop(box)
    size = (max(1, round(character.width * scale)), max(1, round(character.height * scale)))
    character = character.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", TARGET, (0, 0, 0, 0))
    left = max(0, (TARGET[0] - size[0]) // 2)
    top = max(0, min(round(TARGET[1] * FOOT_ANCHOR_Y) - size[1], TARGET[1] - size[1]))
    canvas.paste(character, (left, top), character)
    return canvas
