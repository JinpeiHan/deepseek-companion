"""Frame-sequence renderer for the companion window.

Lifted out of ``CompanionWindow.paintEvent`` unchanged so a rig renderer can be
dispatched next to it later without the two drawing paths drifting apart: every
number here is the shipped chibi look and must stay byte-identical.
"""

from __future__ import annotations

import math
import time
from typing import Any

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter, QPixmap


class FrameRenderer:
    """Draws one manifest frame per tick with a procedural sin-table wobble."""

    @staticmethod
    def tick_ms(reduced_motion: bool) -> int:
        return 40 if reduced_motion else 20

    def paint(self, painter: QPainter, window: Any, bubble_height: int) -> None:
        pixmap = window.pixmaps[window.model.frame]
        phase = time.monotonic()
        motion = window.model.active_clip.motion
        if window.reduced_motion:
            motion = None
        scale_extra = 1.0
        angle = 0.0
        offset_x = 0
        offset_y = 0
        clip_name = window.model.active_clip_name
        if motion == "breathe":
            # 独立版同款：缩放呼吸 + 轻摇摆（无位移）
            scale_extra = 1.0 + 0.02 * math.sin(phase * 2.5)
            angle = math.sin(phase * 2.5) * 1.5
        elif motion == "think":
            offset_y = math.sin(phase * 2.8) * 3
            angle = math.sin(phase * 1.3) * 0.8
        elif motion == "work":
            offset_x = math.sin(phase * 5.4) * 3
            angle = math.sin(phase * 3.1) * 1.0
        elif motion == "wait":
            offset_y = math.sin(phase * 1.8) * 1
            angle = math.sin(phase * 1.2) * 0.8
        elif motion == "bounce":
            offset_y = -abs(math.sin(phase * 5.2)) * 8
            scale_extra = 1.0 + 0.02 * math.sin(phase * 5.2)
        elif motion in {"shake", "dizzy"}:
            offset_x = math.sin(phase * 11.0) * 4
            angle = math.sin(phase * 11.0) * 1.5
        elif motion == "float":
            offset_y = math.sin(phase * 3.0) * 4
            angle = math.sin(phase * 1.6) * 1.0
        # Give walking clips a light bob and quick sway without changing frame timing.
        if clip_name in ("working_search", "working_command"):
            offset_y = -abs(math.sin(phase * 4.5)) * 5
            angle = math.sin(phase * 9.0) * 2.5

        # Scale procedural offsets with the character while retaining subpixel motion.
        offset_x = offset_x * window.scale
        offset_y = offset_y * window.scale

        fade_alpha = 1.0
        if window.fade_from_pixmap is not None and not window.fade_from_pixmap.isNull():
            fade_elapsed = time.monotonic() - window.fade_started
            if fade_elapsed < window.fade_duration:
                fade_alpha = min(1.0, (fade_elapsed / window.fade_duration) ** 0.7)
            else:
                window.fade_from_pixmap = None

        # v2 packs author frames larger than the logical pet box; v1 packs use
        # 1.0 here, so chibi's geometry is unchanged.
        frame_scale = window.scale * window.descriptor.logical_scale

        def draw_pet(pix: QPixmap, alpha: float) -> None:
            base_width = pix.width() * frame_scale
            base_height = pix.height() * frame_scale
            pw = base_width * scale_extra
            ph = base_height * scale_extra
            x = window._pet_offset_x(base_width) + (base_width - pw) / 2 + offset_x
            y = window.height() - ph - 8 + offset_y
            if bubble_height > y:
                y = bubble_height
            cx = x + pw / 2
            cy = y + ph / 2
            painter.save()
            painter.setOpacity(alpha)
            painter.translate(cx, cy)
            painter.rotate(angle)
            painter.translate(-cx, -cy)
            painter.drawPixmap(QRectF(x, y, pw, ph), pix, QRectF(0, 0, pix.width(), pix.height()))
            painter.restore()

        if fade_alpha < 1.0 and window.fade_from_pixmap is not None:
            # Keep the old frame opaque underneath so the pet never flashes transparent.
            draw_pet(window.fade_from_pixmap, 1.0)
        draw_pet(pixmap, fade_alpha)
