"""Qt renderer for the layered bone rig.

This is the only Qt module the rig adds. Everything that decides *where* a part
goes lives in :mod:`runtime.rig_model` (pure solver) and
:mod:`runtime.rig_driver` (pure parameter driver); this file only turns the
solver's affine output into ``drawPixmap`` calls, so the interesting behaviour
stays testable without a display server.

Two invariants are load-bearing and deliberately not negotiable here:

* **Anchors are computed from the rest logical box, never from the deformed
  bounding box.** The caller passes an anchor point already derived from
  ``footAnchor`` and the logical pet size, and this renderer maps the rig's
  rest-space foot point onto it. A tail swung 30 degrees out therefore cannot
  move the speech bubble, drift the standing point, or perturb the saved
  ``petX``/``petY`` -- the deformation happens entirely *inside* a frame whose
  origin never moves.
* **The painter transform is set absolutely, not composed.** ``paintEvent``
  draws the bubble first and uses ``save()``/``translate()``/``restore()`` while
  doing it; relying on the painter being at identity afterwards would make this
  renderer silently depend on the bubble code path. ``paint`` brackets itself in
  ``save()``/``restore()`` and uses ``setTransform(..., combine=False)``.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform

try:  # package import when bundled, flat import when helper.py runs as a script
    from .rig_model import AlphaMask, PartTransform
    from .rig_pack import rig_part_paths
except ImportError:  # pragma: no cover - exercised by the frozen helper
    from rig_model import AlphaMask, PartTransform
    from rig_pack import rig_part_paths


#: Longest edge of a packed hit-test mask. A poke only needs "is there ink
#: here", so 128x128 (2 KB packed) is plenty and keeps a 20-part sweep well
#: inside a microsecond.
MASK_MAX_EDGE = 128

#: Alpha at or above this counts as ink. Matches the extractor's own threshold
#: so a part that validates as non-empty is also pokeable.
MASK_ALPHA_THRESHOLD = 24

#: Paint budget. 60 fps leaves 16.6 ms for everything, so a paint averaging
#: more than this leaves no room for the driver, the event loop or the
#: compositor and the honest response is to ask for fewer frames.
SLOW_PAINT_MS = 11.0

#: Sliding window over which the average is taken.
SLOW_PAINT_WINDOW = 30

#: Rig tick periods. Faster than the frame renderer's 20/40 because a rig is
#: continuous motion rather than a frame sequence: at 20 ms a swinging tail
#: visibly steps.
TICK_MS = 16
TICK_MS_REDUCED = 33
TICK_MS_DEGRADED = 33

#: Source pixels of overlap between adjacent strips. Adjacent strips get
#: slightly different affines, so butting them edge to edge opens a hairline
#: gap the moment the chain bends; one row of shared source pixels closes it.
STRIP_OVERLAP_PX = 1.0


class RigRendererError(ValueError):
    """A rig could not be turned into drawable pixmaps.

    Deliberately a ``ValueError``: the helper's pack-fallback path already
    treats that as "this pack is unusable, keep the old one", which is exactly
    the right response to an undecodable part PNG.
    """


class RigRenderer:
    """Draws one solved rig pose per tick.

    Mirrors :class:`runtime.frame_renderer.FrameRenderer`'s shape (``paint`` and
    ``tick_ms``) so ``paintEvent`` can dispatch between the two without knowing
    which it holds.
    """

    def __init__(self, descriptor: Any, rig: Mapping[str, Any]) -> None:
        self.descriptor = descriptor
        self.rig = dict(rig)
        manifest = getattr(descriptor, "manifest", {}) or {}
        # The rig is authored on the pack's frame artboard, so the same
        # maxFrame* / logicalScale relationship the frame renderer uses applies
        # unchanged -- which is what lets a rig pack and a frame pack of the
        # same proportion occupy exactly the same on-screen box.
        self.source_width = float(manifest.get("maxFrameWidth", 0.0) or 0.0)
        self.source_height = float(manifest.get("maxFrameHeight", 0.0) or 0.0)
        self.logical_scale = float(getattr(descriptor, "logical_scale", 1.0))
        foot = tuple(getattr(descriptor, "foot_anchor", (0.5, 1.0)))
        self.foot_fraction = (float(foot[0]), float(foot[1]))
        #: Rest-space point that must land on the caller's anchor. Derived from
        #: the *rest* artboard only; nothing solved ever feeds back into it.
        self.foot_source = (
            self.foot_fraction[0] * self.source_width,
            self.foot_fraction[1] * self.source_height,
        )

        #: Declared deformation headroom, in source pixels. Constant for the
        #: pack's lifetime and read on every tick, so it is parsed once.
        self.overflow = _overflow(self.rig)
        self._rects = _part_rects(self.rig)

        self._pixmaps: dict[str, QPixmap] = {}
        self._masks: dict[str, AlphaMask] = {}
        self._scaled: dict[str, QPixmap] = {}
        self._scaled_bucket: float | None = None

        self._paint_samples: list[float] = []
        self._degraded = False
        self._degradation_reported = False
        self.last_paint_ms = 0.0

    # -- asset loading ------------------------------------------------------ #

    def part_pixmaps(self, pixmap_type: Any = QPixmap) -> dict[str, QPixmap]:
        """Decode every part PNG, or fail before anything is on screen.

        All-or-nothing on purpose, exactly like ``load_pack_pixmaps``: a rig
        missing one layer would otherwise surface as a hole in the character
        long after the pack was selected.
        """
        if self._pixmaps:
            return self._pixmaps
        asset_root = getattr(self.descriptor, "asset_root", None)
        paths = rig_part_paths(self.rig, asset_root)
        loaded: dict[str, QPixmap] = {}
        missing: list[str] = []
        for part_id, path in paths.items():
            pixmap = pixmap_type(str(path))
            if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
                missing.append(part_id)
                continue
            loaded[part_id] = pixmap
        if missing:
            raise RigRendererError(
                f"{len(missing)} rig part(s) unreadable, first '{missing[0]}'"
            )
        self._pixmaps = loaded
        self._scaled = {}
        self._scaled_bucket = None
        return self._pixmaps

    def alpha_masks(self) -> dict[str, AlphaMask]:
        """Downsample each part's alpha into the packed bitmap ``hit_test`` wants.

        The mask lives in *source* space (its ``rect`` is the part's rect), so
        ``hit_test`` can inverse-map a click through the part's current deformed
        matrix and land in mask coordinates directly.
        """
        if self._masks:
            return self._masks
        pixmaps = self.part_pixmaps()
        rects = self._rects
        masks: dict[str, AlphaMask] = {}
        for part_id, pixmap in pixmaps.items():
            rect = rects.get(part_id)
            if rect is None:
                continue
            masks[part_id] = _mask_from_pixmap(pixmap, rect)
        self._masks = masks
        return self._masks

    # -- geometry ----------------------------------------------------------- #

    def world_scale(self, scale: float) -> float:
        """Source pixels to widget pixels for a given character scale."""
        return float(scale) * self.logical_scale

    def world_transform(
        self, *, anchor_x: float, anchor_y: float, scale: float
    ) -> QTransform:
        """translate(anchor) then scale then translate(-foot_source).

        Built once per paint and reused by every part; ``QTransform``
        composition reads left-to-right as "a then b", so this spells out
        "shift the rest foot point to the origin, scale, then move to where the
        window wants the pet to stand".
        """
        world_scale = self.world_scale(scale)
        transform = QTransform()
        transform.translate(anchor_x, anchor_y)
        transform.scale(world_scale, world_scale)
        transform.translate(-self.foot_source[0], -self.foot_source[1])
        return transform

    def to_source(
        self, x: float, y: float, *, anchor_x: float, anchor_y: float, scale: float
    ) -> tuple[float, float]:
        """Map a widget point into rig source space (for :func:`hit_test`)."""
        world_scale = self.world_scale(scale) or 1.0
        return (
            (x - anchor_x) / world_scale + self.foot_source[0],
            (y - anchor_y) / world_scale + self.foot_source[1],
        )

    def overflow_px(self, scale: float) -> tuple[float, float, float, float]:
        """Declared overflow in widget pixels: the padding the window must keep."""
        world_scale = self.world_scale(scale)
        return tuple(value * world_scale for value in self.overflow)

    def pet_rect(self, scale: float) -> tuple[float, float, float, float]:
        """Safe area to repaint, in widget pixels *relative to the anchor*.

        This is the rest artboard grown by the rig's declared ``overflow``, not
        the deformed bbox: the window reserves the overflow padding
        unconditionally, so the repaint rect is a constant per scale and cannot
        jitter with the pose.
        """
        world_scale = self.world_scale(scale)
        left, top, right, bottom = self.overflow
        x = (-self.foot_source[0] - left) * world_scale
        y = (-self.foot_source[1] - top) * world_scale
        width = (self.source_width + left + right) * world_scale
        height = (self.source_height + top + bottom) * world_scale
        return (x, y, width, height)

    # -- ticking ------------------------------------------------------------ #

    def tick_ms(self, reduced_motion: bool) -> int:
        if reduced_motion:
            return TICK_MS_REDUCED
        return TICK_MS_DEGRADED if self._degraded else TICK_MS

    @property
    def degraded(self) -> bool:
        return self._degraded

    def take_degradation_notice(self) -> bool:
        """True exactly once, on the tick that degradation was decided."""
        if self._degraded and not self._degradation_reported:
            self._degradation_reported = True
            return True
        return False

    def record_paint_ms(self, elapsed_ms: float) -> None:
        """Feed the sliding window that decides whether to drop to 33 ms.

        One-way on purpose: flapping between 16 and 33 ms because the average
        hovers around the threshold would look far worse than simply staying at
        the slower rate.
        """
        if self._degraded:
            return
        self._paint_samples.append(float(elapsed_ms))
        if len(self._paint_samples) < SLOW_PAINT_WINDOW:
            return
        window = self._paint_samples[-SLOW_PAINT_WINDOW:]
        self._paint_samples = window
        if sum(window) / len(window) > SLOW_PAINT_MS:
            self._degraded = True

    # -- painting ----------------------------------------------------------- #

    def paint(
        self,
        painter: QPainter,
        transforms: Iterable[PartTransform],
        *,
        anchor_x: float,
        anchor_y: float,
        scale: float,
    ) -> None:
        """Draw one solved pose. *transforms* must be z-ascending."""
        started = time.perf_counter()
        pixmaps = self.part_pixmaps()
        world = self.world_transform(anchor_x=anchor_x, anchor_y=anchor_y, scale=scale)
        self._ensure_scaled(scale)

        painter.save()
        try:
            # Antialiasing off: it applies to path edges, not pixmaps, and the
            # only edges here are the strip seams where it would *add* a
            # semi-transparent hairline. Smooth pixmap transform on: parts are
            # rotated and scaled every frame.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            for transform in transforms:
                pixmap = pixmaps.get(transform.part_id)
                if pixmap is None or transform.opacity <= 0.0:
                    continue
                drawable = self._scaled.get(transform.part_id, pixmap)
                painter.setOpacity(transform.opacity)
                if transform.strips > 1 and transform.strip_matrices:
                    self._paint_strips(painter, transform, drawable, world)
                else:
                    painter.setTransform(QTransform(*transform.matrix) * world, False)
                    painter.drawPixmap(
                        QRectF(*transform.src_rect),
                        drawable,
                        QRectF(0.0, 0.0, drawable.width(), drawable.height()),
                    )
        finally:
            painter.setOpacity(1.0)
            painter.restore()

        self.last_paint_ms = (time.perf_counter() - started) * 1000.0
        self.record_paint_ms(self.last_paint_ms)

    def paint_baked(
        self,
        painter: QPainter,
        pixmap: QPixmap,
        *,
        anchor_x: float,
        anchor_y: float,
        scale: float,
    ) -> None:
        """Draw one pre-rendered frame through the rig's own world transform.

        A baked clip is authored on the same 512 canvas as the rig master and
        shares its foot anchor, so reusing world_transform is what keeps the
        character from jumping when a baked clip starts or ends. Doing the
        arithmetic separately here would drift the moment either side changed.
        """
        world = self.world_transform(anchor_x=anchor_x, anchor_y=anchor_y, scale=scale)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setOpacity(1.0)
            painter.setTransform(world, False)
            painter.drawPixmap(
                QRectF(0.0, 0.0, float(self.source_width), float(self.source_height)),
                pixmap,
                QRectF(0.0, 0.0, float(pixmap.width()), float(pixmap.height())),
            )
        finally:
            painter.restore()

    def _paint_strips(
        self,
        painter: QPainter,
        transform: PartTransform,
        pixmap: QPixmap,
        world: QTransform,
    ) -> None:
        rx, ry, rw, rh = transform.src_rect
        count = len(transform.strip_matrices)
        if count <= 0 or rh <= 0.0:
            return
        pw = float(pixmap.width())
        ph = float(pixmap.height())
        overlap = STRIP_OVERLAP_PX / rh
        for index, matrix in enumerate(transform.strip_matrices):
            low = index / count
            high = (index + 1) / count
            if index + 1 < count:
                high = min(1.0, high + overlap)
            span = high - low
            if span <= 0.0:
                continue
            painter.setTransform(QTransform(*matrix) * world, False)
            painter.drawPixmap(
                QRectF(rx, ry + low * rh, rw, span * rh),
                pixmap,
                QRectF(0.0, low * ph, pw, span * ph),
            )

    # -- pre-scale cache ---------------------------------------------------- #

    def _ensure_scaled(self, scale: float) -> None:
        """Keep one pre-scaled pixmap per part for the current scale bucket.

        Bucketing to 1% steps means the cache is rebuilt on a settings change,
        not on every frame. Rotation is deliberately *not* pre-baked: the angles
        are continuous, so a useful bucket size would need well over a thousand
        pixmaps per part and would still resample at draw time.
        """
        bucket = round(self.world_scale(scale) * 100.0) / 100.0
        if bucket == self._scaled_bucket and self._scaled:
            return
        self._scaled_bucket = bucket
        self._scaled = {}
        if bucket <= 0.0:
            return
        rects = self._rects
        for part_id, pixmap in self._pixmaps.items():
            rect = rects.get(part_id)
            if rect is None:
                continue
            width = max(1, int(round(rect[2] * bucket)))
            height = max(1, int(round(rect[3] * bucket)))
            if width >= pixmap.width() and height >= pixmap.height():
                # Never upscale into the cache: it would burn memory to bake in
                # a resample the GPU does for free, and lose detail at 140%.
                continue
            self._scaled[part_id] = pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        out = []
        for key, value in raw.items():
            entry = dict(value) if isinstance(value, Mapping) else {}
            entry.setdefault("id", key)
            out.append(entry)
        return out
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [dict(entry) for entry in raw if isinstance(entry, Mapping)]
    return []


def _part_rects(rig: Mapping[str, Any]) -> dict[str, tuple[float, float, float, float]]:
    rects: dict[str, tuple[float, float, float, float]] = {}
    for entry in _entries(rig.get("parts")):
        rect = entry.get("rect") or (0.0, 0.0, 0.0, 0.0)
        values = [float(v) for v in rect] + [0.0, 0.0, 0.0, 0.0]
        rects[str(entry.get("id", ""))] = (
            values[0],
            values[1],
            values[2],
            values[3],
        )
    return rects


def _overflow(rig: Mapping[str, Any]) -> tuple[float, float, float, float]:
    value = rig.get("overflow")
    if isinstance(value, Mapping):
        return (
            float(value.get("left", 0.0)),
            float(value.get("top", 0.0)),
            float(value.get("right", 0.0)),
            float(value.get("bottom", 0.0)),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 4:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        pad = float(value)
        return (pad, pad, pad, pad)
    return (0.0, 0.0, 0.0, 0.0)


def _mask_from_pixmap(
    pixmap: QPixmap, rect: tuple[float, float, float, float]
) -> AlphaMask:
    """Pack a part's alpha into the row-major, LSB-first bitmap ``AlphaMask`` reads."""
    width = max(1, pixmap.width())
    height = max(1, pixmap.height())
    factor = max(1.0, width / MASK_MAX_EDGE, height / MASK_MAX_EDGE)
    mask_width = max(1, min(MASK_MAX_EDGE, int(round(width / factor))))
    mask_height = max(1, min(MASK_MAX_EDGE, int(round(height / factor))))

    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    if (mask_width, mask_height) != (width, height):
        image = image.scaled(
            mask_width,
            mask_height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_ARGB32)
    mask_width = image.width()
    mask_height = image.height()

    bits = bytearray((mask_width * mask_height + 7) // 8)
    for v in range(mask_height):
        for u in range(mask_width):
            if image.pixelColor(u, v).alpha() >= MASK_ALPHA_THRESHOLD:
                index = v * mask_width + u
                bits[index >> 3] |= 1 << (index & 7)
    return AlphaMask(
        width=mask_width,
        height=mask_height,
        bits=bytes(bits),
        rect=(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])),
    )
