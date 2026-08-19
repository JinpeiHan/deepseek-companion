"""Phase 0 native BigFish helper.

The DSH plugin owns this process and sends newline-delimited JSON over stdin.
Closing stdin is a lifecycle signal: the helper exits instead of becoming an
independent desktop application.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, TextIO

try:
    from .animation_model import AnimationModel, crossfade_duration
    from .asset_pack import PackDescriptor, load_pack_descriptor, load_pack_pixmaps, normalise_pack_id
    from .layout_store import default_layout_path, load_layout, save_layout
    from .persona_copy import interaction_copy, load_persona_copy
    from .rig_driver import RigDriver
    from .rig_model import hit_test
    from .rig_pack import animation_manifest_from_rig, baked_frame_paths, load_rig
except ImportError:
    from animation_model import AnimationModel, crossfade_duration
    from asset_pack import PackDescriptor, load_pack_descriptor, load_pack_pixmaps, normalise_pack_id
    from layout_store import default_layout_path, load_layout, save_layout
    from persona_copy import interaction_copy, load_persona_copy
    from rig_driver import RigDriver
    from rig_model import hit_test
    from rig_pack import animation_manifest_from_rig, baked_frame_paths, load_rig


PROTOCOL_VERSION = 1
STATES = {"IDLE", "THINKING", "WORKING", "WAITING", "SUCCESS", "ERROR", "DISCONNECTED"}
PACK_LOAD_ERRORS = (OSError, ValueError, KeyError, json.JSONDecodeError)


# --------------------------------------------------------------------------- #
# Interaction maths
# --------------------------------------------------------------------------- #
#
# Everything in this section is deliberately Qt-free and stateless-per-call so
# it can be unit tested without a display server. The window methods further
# down are then thin enough that reading them is enough to see what they do.

#: Vertical fraction of the pet box the pointer is measured against. The pet
#: looks at the cursor with its *head*, not its centroid -- aiming at the
#: middle of the box makes a tall character read as staring at its own feet.
POINTER_ATTENTION_Y = 0.38

#: Follow saturates this many pet-boxes away from the attention point. Wide
#: enough that a cursor still on the pet barely moves the head, narrow enough
#: that the pet is fully turned long before the far edge of a 4K screen.
POINTER_SPAN_FACTOR = 2.5

#: Distance falloff for a poke, as a multiplier on the declared impulse.
#: Clamped at both ends: a click on a part's pivot must still register, and no
#: click anywhere may exceed the maximum, however large the part is.
IMPULSE_SCALE_MIN = 0.35
IMPULSE_SCALE_MAX = 1.6

#: Ticker interval a rig pack degrades to while the window is being dragged.
RIG_DRAG_TICK_MS = 33

#: Anchor-velocity low-pass. Mouse move events are irregularly spaced, so a raw
#: ``delta / dt`` spikes on a short interval and reads as a stall on a long
#: one. Smoothing is what makes the lean usable and its derivative -- which the
#: driver differentiates back out to shake the chains -- usable at all.
ANCHOR_VELOCITY_ALPHA = 0.35
#: Intervals below this are noise-dominated; the sample is folded into the next.
ANCHOR_VELOCITY_MIN_DT_MS = 4.0
#: Pixels per second. The driver clamps its own outputs, but an unbounded input
#: still poisons the acceleration term it takes from this.
ANCHOR_VELOCITY_MAX = 4000.0
#: No move event for this long means the drag has stalled, not that the pet is
#: still travelling at the last measured speed.
ANCHOR_VELOCITY_STALE_MS = 250.0

#: The shipped rectangle zone heuristic, as (clip, persona copy group, ttl_ms).
FRAME_CLICK_HEAD = ("head_pat", "headPat", 1800)
FRAME_CLICK_TAIL = ("tail", "tail", 1500)
FRAME_CLICK_POKE = ("poke", "poke", 1500)

#: Fallback bubble lifetime for a rig interaction that declares no ``ttlMs``.
RIG_INTERACTION_TTL_MS = 1600


def _clamp_unit(value: float) -> float:
    return min(1.0, max(-1.0, float(value)))


def _clamp_speed(value: float) -> float:
    return min(ANCHOR_VELOCITY_MAX, max(-ANCHOR_VELOCITY_MAX, float(value)))


def pointer_offset(
    cursor: tuple[float, float],
    pet_rect: tuple[float, float, float, float],
    *,
    attention_y: float = POINTER_ATTENTION_Y,
    span_factor: float = POINTER_SPAN_FACTOR,
) -> tuple[float, float]:
    """Normalise a global cursor position against the pet box.

    Both arguments are in the same virtual-desktop space -- ``QCursor.pos()``
    and ``pet_x``/``pet_y`` both derive from ``QScreen.availableGeometry()``,
    which is what makes polling work with no focus and no pointer grab.

    Returns a pair clamped to [-1, 1], and *exactly* ``(0.0, 0.0)`` when the
    cursor sits on the attention point. That exactness matters: the dead zone
    in :meth:`RigDriver.set_pointer` is a magnitude test, so a normalisation
    that merely approached zero would leave the head permanently a hair off
    centre.
    """
    cursor_x, cursor_y = cursor
    pet_x, pet_y, pet_width, pet_height = pet_rect
    if pet_width <= 0 or pet_height <= 0:
        return (0.0, 0.0)
    attention_point_x = pet_x + pet_width / 2.0
    attention_point_y = pet_y + attention_y * pet_height
    span_x = max(1.0, pet_width * span_factor)
    span_y = max(1.0, pet_height * span_factor)
    return (
        _clamp_unit((cursor_x - attention_point_x) / span_x),
        _clamp_unit((cursor_y - attention_point_y) / span_y),
    )


def pointer_target(
    cursor: tuple[float, float],
    pet_rect: tuple[float, float, float, float],
    *,
    reduced_motion: bool,
    same_screen: bool,
) -> tuple[float, float, bool]:
    """``(dx, dy, present)`` to hand :meth:`RigDriver.set_pointer`.

    The two degenerate cases are separated on purpose:

    * **Reduced motion** reports *absent*. It is a promise, not a damping
      factor -- no poll, no follow, and the driver snaps its pointer springs to
      zero rather than easing them there.
    * **A cursor on another monitor** reports *present but dead centre*. Under
      per-monitor DPI the two positions are not points in one uniform space, so
      their difference is not a distance; the pet looks straight ahead instead
      of following a target skewed by the scale factor mismatch.
    """
    if reduced_motion:
        return (0.0, 0.0, False)
    if not same_screen:
        return (0.0, 0.0, True)
    offset_x, offset_y = pointer_offset(cursor, pet_rect)
    return (offset_x, offset_y, True)


def part_geometry(
    rig: Mapping[str, Any], part_id: str | None
) -> tuple[tuple[float, float, float, float], tuple[float, float]]:
    """``(rect, pivot)`` of one part in rig source space.

    The pivot defaults to the rect centre exactly as
    :class:`~runtime.rig_model.RigModel` does, so the distance falloff below is
    measured against the same point the part actually rotates about.
    """
    empty = (0.0, 0.0, 0.0, 0.0)
    if not part_id:
        return empty, (0.0, 0.0)
    for entry in rig.get("parts", ()) or ():
        if not isinstance(entry, Mapping) or entry.get("id") != part_id:
            continue
        raw_rect = entry.get("rect")
        if isinstance(raw_rect, (list, tuple)) and len(raw_rect) >= 4:
            rect = tuple(float(value) for value in raw_rect[:4])
        else:
            rect = empty
        raw_pivot = entry.get("pivot")
        if isinstance(raw_pivot, (list, tuple)) and len(raw_pivot) >= 2:
            pivot = (float(raw_pivot[0]), float(raw_pivot[1]))
        else:
            pivot = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
        return rect, pivot
    return empty, (0.0, 0.0)


def impulse_scale(
    distance: float,
    part_rect: tuple[float, float, float, float],
    *,
    minimum: float = IMPULSE_SCALE_MIN,
    maximum: float = IMPULSE_SCALE_MAX,
) -> float:
    """Distance falloff for a poke, measured in the part's own source space.

    The reference length is half the part's diagonal, so the result is a
    property of the part's *size* rather than of the pack's pixel resolution --
    a 512px rig and a 1024px rig of the same character whip identically.
    Monotonic in ``distance`` and clamped at both ends, which is what makes
    "poke the tail tip and it lashes, poke its root and it nudges" true without
    letting a stray click on a huge part produce an absurd kick.
    """
    _, _, width, height = part_rect
    reference = math.hypot(float(width), float(height)) / 2.0
    if reference <= 0.0:
        return minimum
    ratio = max(0.0, float(distance)) / reference
    return min(maximum, max(minimum, minimum + (maximum - minimum) * ratio))


def hit_group_for_part(rig: Mapping[str, Any], part_id: str | None) -> str | None:
    """Resolve a hit-tested part id to its declared hit group.

    A part's own ``hitGroup`` wins over the ``hitGroups`` index, because that is
    the field :class:`~runtime.rig_model.Part` carries and the one a rig author
    edits next to the part they are naming.
    """
    if not part_id:
        return None
    for entry in rig.get("parts", ()) or ():
        if isinstance(entry, Mapping) and entry.get("id") == part_id:
            group = entry.get("hitGroup")
            if isinstance(group, str) and group:
                return group
            break
    groups = rig.get("hitGroups")
    if isinstance(groups, Mapping):
        for group, members in groups.items():
            if isinstance(members, (list, tuple)) and part_id in members:
                return str(group)
    return None


def rig_interaction_for_part(
    rig: Mapping[str, Any], part_id: str | None
) -> tuple[str, dict[str, Any]] | None:
    """``(group, entry)`` for the part under the click, or ``None``.

    ``None`` covers three cases that mean the same thing to the caller -- no
    part, a part in no hit group, and a group with no ``interactions`` entry --
    and the caller answers all three by doing nothing, which is what leaves the
    press free to have started a window drag.
    """
    group = hit_group_for_part(rig, part_id)
    if group is None:
        return None
    interactions = rig.get("interactions")
    if not isinstance(interactions, Mapping):
        return None
    entry = interactions.get(group)
    if not isinstance(entry, Mapping):
        return None
    return group, dict(entry)


def interaction_copy_group(
    entry: Mapping[str, Any], group: str, copy: Mapping[str, Any]
) -> str:
    """Persona copy group for a rig interaction, falling back to ``poke``.

    A rig may declare hit groups the shipped persona file has never heard of
    (``cheek``, say). Falling back keeps the whale talking rather than raising
    a KeyError out of a mouse handler.
    """
    available = copy.get("interaction") if isinstance(copy, Mapping) else None
    available = available if isinstance(available, Mapping) else {}
    for candidate in (entry.get("copy"), group):
        if isinstance(candidate, str) and candidate in available:
            return candidate
    return "poke"


def frame_click_interaction(
    x: float, y: float, pet_rect: tuple[float, float, float, float]
) -> tuple[str, str, int]:
    """The shipped rectangle zone heuristic, unchanged in behaviour.

    Frame packs keep this forever: they have no parts to hit test against, and
    chibi's behaviour is pinned by the snapshot hash. Lifted out of the mouse
    handler only so it can be tested without a window.
    """
    pet_x, pet_y, pet_width, pet_height = pet_rect
    relative_x = max(0.0, x - pet_x)
    relative_y = max(0.0, y - pet_y)
    if relative_y < pet_height * 0.45:
        return FRAME_CLICK_HEAD
    if relative_x > pet_width * 0.72:
        return FRAME_CLICK_TAIL
    return FRAME_CLICK_POKE


def drag_tick_interval(renderer_tick_ms: int, is_rig: bool) -> int | None:
    """Ticker interval to use during a drag; ``None`` means "stop the ticker".

    Frame packs stop it, which is the shipped behaviour and, per
    ``crossfade_duration``'s docstring, the reason it exists at all: it
    suppresses Windows layered-window flicker while the window is moving. A
    frame pack loses nothing by stopping, because it has nothing to integrate.

    A rig does. Its springs must keep stepping through the drag or the hair and
    tail would teleport into place on release instead of lagging behind the
    throw and settling out of it -- which is the entire point of dragging a
    rigged pet. The compromise is the degraded interval plus a pet-rect-only
    repaint: the same load the renderer already degrades itself to under
    sustained slow paints, rather than the full 16ms rate.
    """
    if not is_rig:
        return None
    return max(RIG_DRAG_TICK_MS, int(renderer_tick_ms))


class AnchorVelocity:
    """Low-passed pet-anchor velocity in pixels per second.

    Fed from ``_move_to_pet`` -- i.e. from mouse move events, which arrive at
    whatever irregular rate the platform feels like -- and read once per tick.
    Sampling and reporting are separate calls precisely because those two rates
    are unrelated.
    """

    def __init__(self, alpha: float = ANCHOR_VELOCITY_ALPHA) -> None:
        self.alpha = float(alpha)
        self.reset()

    def reset(self) -> None:
        self._x: float | None = None
        self._y: float | None = None
        self._t: float | None = None
        self._vx = 0.0
        self._vy = 0.0

    @property
    def value(self) -> tuple[float, float]:
        return (self._vx, self._vy)

    @property
    def started(self) -> bool:
        return self._t is not None

    def update(self, x: float, y: float, now_ms: float) -> tuple[float, float]:
        """Record an anchor position and fold it into the running estimate."""
        if self._t is None:
            self._x, self._y, self._t = float(x), float(y), float(now_ms)
            return self.value
        dt_ms = float(now_ms) - self._t
        if dt_ms < ANCHOR_VELOCITY_MIN_DT_MS:
            # Too short a baseline to differentiate. Keep the old anchor so the
            # next sample measures across the *whole* gap instead of dividing a
            # one-pixel move by a fraction of a millisecond.
            return self.value
        sample_x = _clamp_speed((float(x) - self._x) * 1000.0 / dt_ms)
        sample_y = _clamp_speed((float(y) - self._y) * 1000.0 / dt_ms)
        self._x, self._y, self._t = float(x), float(y), float(now_ms)
        self._vx += self.alpha * (sample_x - self._vx)
        self._vy += self.alpha * (sample_y - self._vy)
        return self.value

    def sample(self, now_ms: float) -> tuple[float, float]:
        """Velocity to report this tick, zeroed when the drag has stalled."""
        if self._t is None:
            return (0.0, 0.0)
        if float(now_ms) - self._t > ANCHOR_VELOCITY_STALE_MS:
            self._vx = 0.0
            self._vy = 0.0
        return self.value


class PackAssets(NamedTuple):
    """Everything a loaded pack needs, whichever renderer it uses.

    Both renderers travel through the same all-or-nothing loading path, so a
    rig whose schema, part paths or PNGs are broken is rejected while the caller
    can still fall back -- exactly like a frame pack with a missing frame.
    """

    descriptor: PackDescriptor
    #: Frame pixmaps. Always empty for a rig pack, which never indexes it.
    pixmaps: dict[str, Any]
    #: Manifest handed to :class:`AnimationModel`. For a rig this is the
    #: synthesised one from :func:`animation_manifest_from_rig`, not the rig.
    animation_manifest: dict[str, Any]
    #: Parsed rig, or ``None`` for a frame pack.
    rig: dict[str, Any] | None
    #: ``FrameRenderer`` or ``RigRenderer``; both expose ``tick_ms``/``paint``.
    renderer: Any


def resolve_pack(
    root: Path,
    pack_id: Any,
    loader: Callable[[Path, str], Any] = load_pack_descriptor,
) -> Any:
    """Load the requested proportion pack, falling back to chibi exactly once.

    The proportion is a user-facing setting that can name a pack whose art was
    never shipped, so a broken pack has to degrade to the bundled default
    instead of killing the helper the moment somebody flips the setting. A
    broken chibi is still fatal: there is nothing left to fall back to.
    """
    selected = normalise_pack_id(pack_id)
    try:
        return loader(root, selected)
    except PACK_LOAD_ERRORS as error:
        if selected == "chibi":
            raise
        print(
            f"warning: proportion pack '{selected}' unavailable ({error}); falling back to chibi",
            file=sys.stderr,
            flush=True,
        )
    return loader(root, "chibi")


def bundle_root() -> Path:
    """Locate packaged assets both from source and a PyInstaller one-file build."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


def configure_stdio() -> None:
    """Make the JSONL pipe UTF-8 regardless of the Windows console code page."""
    for stream, errors in ((sys.stdin, "strict"), (sys.stdout, "backslashreplace"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def parse_message(line: str) -> dict[str, Any]:
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    if message.get("protocolVersion") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    kind = message.get("kind")
    if kind in {"state", "pulse"} and message.get("state") not in STATES:
        raise ValueError("unsupported companion state")
    return message


def ask_hit(
    rects: "list[tuple[int, int, int, int, str]]", x: float, y: float
) -> str | None:
    """Which approval choice, if any, the point falls on.

    Module level and pure so it can be tested without a window: CompanionWindow
    lives inside run_visual's closure. The rects come from the paint pass rather
    than being recomputed, so the click tests exactly what was drawn.
    """
    for left, top, width, height, value in rects:
        if left <= x <= left + width and top <= y <= top + height:
            return value
    return None


def ask_from_message(message: "dict[str, Any]") -> "dict[str, Any] | None":
    """Validate an ask message, or None if it cannot be answered.

    A question with no answerable option would take over the bubble and leave
    no way out of it, so it is dropped rather than shown.
    """
    question = str(message.get("question", "")).strip()
    identifier = str(message.get("id", "")).strip()
    # DSH options carry a label and an optional description, and the answer
    # echoes back labels -- there is no separate value to send.
    options = [
        {"label": str(o["label"]).strip(), "description": str(o.get("description", "")).strip()}
        for o in (message.get("options") or [])
        if isinstance(o, dict) and str(o.get("label", "")).strip()
    ]
    if not question or not identifier or not options:
        return None
    return {
        "id": identifier,
        "question": question,
        "detail": str(message.get("detail", "")),
        "options": options[:4],
    }


def window_setup(platform: str) -> "dict[str, Any]":
    """Window flags and attributes for one platform.

    Pure and module level so the platform choice is testable from a machine
    that is not that platform -- the whole point is that these branches cannot
    all be exercised where the code is written.

    ``Qt.Tool`` is what keeps the pet off the taskbar and above ordinary
    windows, and on Windows and Linux that is all it does. On macOS a tool
    window is an ``NSPanel``, and an NSPanel hides itself whenever its
    application is deactivated -- so the pet would vanish the moment the user
    clicked another app, which is exactly when a desktop companion should still
    be there. ``WA_MacAlwaysShowToolWindow`` is Qt's opt-out from that
    behaviour and exists for this case.
    """
    flags = ["FramelessWindowHint", "WindowStaysOnTopHint", "Tool"]
    attributes = ["WA_TranslucentBackground"]
    if platform == "darwin":
        attributes.append("WA_MacAlwaysShowToolWindow")
    return {"flags": flags, "attributes": attributes}


def emit_reply(kind: str, **payload: Any) -> None:
    print(
        json.dumps(
            {"protocolVersion": PROTOCOL_VERSION, "kind": kind, "timestamp": int(time.time() * 1000), **payload},
            ensure_ascii=False,
        ),
        flush=True,
    )


class EventRecorder:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._stream: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8")

    def record(self, message: dict[str, Any]) -> None:
        if self._stream is None:
            return
        self._stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()


def run_headless(recorder: EventRecorder) -> int:
    try:
        emit_reply("ready")
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = parse_message(line)
            except (ValueError, json.JSONDecodeError) as error:
                print(json.dumps({"kind": "error", "message": str(error)}), flush=True)
                continue
            recorder.record(message)
            if message.get("kind") == "ping":
                emit_reply("pong")
                continue
            if message.get("kind") == "shutdown":
                break
    finally:
        recorder.close()
    return 0


def run_visual(recorder: EventRecorder, snapshot_path: Path | None = None) -> int:
    try:
        from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, Signal
        from PySide6.QtGui import (
            QColor,
            QCursor,
            QDesktopServices,
            QFont,
            QFontMetrics,
            QMouseEvent,
            QPainter,
            QPen,
            QPixmap,
        )
        from PySide6.QtWidgets import QApplication, QMenu, QWidget

        try:
            from .frame_renderer import FrameRenderer
            from .rig_renderer import RigRenderer
        except ImportError:
            from frame_renderer import FrameRenderer
            from rig_renderer import RigRenderer
    except ImportError:
        print(
            "PySide6 is required for visual mode. Run with --headless for protocol tests.",
            file=sys.stderr,
        )
        recorder.close()
        return 2

    class Inbox(QObject):
        message = Signal(dict)
        closed = Signal()

    def load_pack_assets(root: Path, pack_id: str) -> PackAssets:
        """Build a pack only if every one of its assets decodes.

        Half a pack is worse than no pack: a missing frame surfaces as a
        KeyError inside paintEvent long after the setting was changed. The rig
        branch holds the same line -- schema validation, part-path confinement
        and PNG decoding all happen here, so a rig pack can only reach
        paintEvent in a state where it is guaranteed to draw.
        """
        descriptor = load_pack_descriptor(root, pack_id)
        if descriptor.renderer == "rig":
            rig = load_rig(descriptor)
            renderer = RigRenderer(descriptor, rig)
            renderer.part_pixmaps(QPixmap)
            renderer.alpha_masks()
            # Baked clips are decoded here for the same reason the parts are:
            # a frame that fails to load must fail the whole pack now, not
            # inside paintEvent once the setting has already been changed.
            baked: dict[str, Any] = {}
            for name, path in baked_frame_paths(rig, descriptor.asset_root).items():
                pixmap = QPixmap(str(path))
                if pixmap.isNull():
                    raise ValueError(f"baked clip frame unreadable: {name}")
                baked[name] = pixmap
            return PackAssets(
                descriptor, baked, animation_manifest_from_rig(rig), rig, renderer
            )
        pixmaps, missing = load_pack_pixmaps(descriptor, QPixmap, strict=False)
        if missing:
            raise ValueError(f"{len(missing)} frame(s) unreadable, first '{missing[0]}'")
        return PackAssets(descriptor, pixmaps, descriptor.manifest, None, FrameRenderer())

    try:
        persona_copy = load_persona_copy(bundle_root() / "assets" / "persona-copy.zh-CN.json")
    except (OSError, ValueError) as error:
        print(f"Unable to load whale persona copy: {error}", file=sys.stderr)
        recorder.close()
        return 2

    class CompanionWindow(QWidget):
        LABELS = {
            "IDLE": "休息中",
            "THINKING": "思考中",
            "WORKING": "干活中",
            "WAITING": "等你呢",
            "SUCCESS": "完成啦",
            "ERROR": "出问题了",
            "DISCONNECTED": "已断开",
        }

        def __init__(self, assets: PackAssets) -> None:
            super().__init__()
            self.descriptor = assets.descriptor
            self.manifest = assets.animation_manifest
            self.pixmaps = assets.pixmaps
            self.rig = assets.rig
            self.renderer = assets.renderer
            self.rig_driver: RigDriver | None = None
            #: Last solved pose, z-ascending. ``None`` for a frame pack.
            self.rig_transforms: list[Any] | None = None
            self.layout_path = default_layout_path()
            self.layout = load_layout(self.layout_path)
            configured_scale = os.environ.get("DSH_DAFEIYU_SCALE")
            try:
                self.scale = min(1.4, max(0.7, float(configured_scale))) if configured_scale else self.layout["scale"]
            except ValueError:
                self.scale = self.layout["scale"]
            configured_bubble_scale = os.environ.get("DSH_DAFEIYU_BUBBLE_SCALE")
            try:
                self.bubble_scale = (
                    min(1.2, max(0.8, float(configured_bubble_scale)))
                    if configured_bubble_scale
                    else self.layout["bubbleScale"]
                )
            except ValueError:
                self.bubble_scale = self.layout["bubbleScale"]
            configured_reduced_motion = os.environ.get("DSH_DAFEIYU_REDUCED_MOTION")
            self.reduced_motion = (
                configured_reduced_motion == "1"
                if configured_reduced_motion is not None
                else self.layout["reducedMotion"]
            )
            self.activity_level = os.environ.get("DSH_DAFEIYU_ACTIVITY_LEVEL", "normal")
            configured_bubble_mode = os.environ.get("DSH_DAFEIYU_BUBBLE_MODE")
            self.bubble_mode = (
                configured_bubble_mode
                if configured_bubble_mode in {"always", "hidden", "custom"}
                else self.layout.get("bubbleMode", "always")
            )
            configured_bubble_states = os.environ.get("DSH_DAFEIYU_BUBBLE_STATES")
            if configured_bubble_states is not None:
                self.bubble_states = [part.strip() for part in configured_bubble_states.split(",") if part.strip()]
            else:
                self.bubble_states = list(self.layout.get("bubbleStates", ["SUCCESS", "ERROR", "WAITING"]))
            self.model = AnimationModel(self.manifest)
            # Set before the driver exists: ``_build_rig_driver`` solves one
            # pose immediately, and that solve reads the pointer and the drag
            # state through ``_advance_rig``.
            self.pet_x = 0
            self.pet_y = 0
            self.dragging = False
            self.anchor_velocity = AnchorVelocity()
            self._build_rig_driver()

            self.display_state = "IDLE"
            self.status_state = "IDLE"
            self.status_message = "我在这儿等新任务哦"
            self.status_detail = "DSH · 等待下一次任务"
            self.status_deadline_ms: int | None = self._now_ms() + 4200
            self.overlay_state: str | None = None
            self.overlay_message = ""
            self.overlay_detail = ""
            self.overlay_deadline_ms: int | None = None
            self.task = ""
            self.tasks: list[dict[str, Any]] = []
            self.webui_url = os.environ.get("DSH_DAFEIYU_WEBUI_URL", "http://127.0.0.1:3080/")
            self.shake_timer: QTimer | None = None
            self.shake_origin: QPoint | None = None
            self.shake_count = 0
            self.drag_origin: QPoint | None = None
            self.interrupted_overlay: tuple[str, int, int] | None = None
            # A pending approval, drawn as choices in the bubble so it can be
            # answered here instead of in the web UI.
            self.ask: dict[str, Any] | None = None
            self.ask_hit_rects: list[tuple[int, int, int, int, str]] = []
            self.ask_hover: int | None = None
            self.pet_origin: QPoint | None = None
            self.last_tick_ms = self._now_ms()
            self.fade_from_pixmap: QPixmap | None = None
            self.fade_started = 0.0
            self.fade_duration = 0.15
            self.animation_timer = QTimer(self)
            self.animation_timer.timeout.connect(self._tick)
            self.animation_timer.start(self.renderer.tick_ms(self.reduced_motion))
            self.micro_timer = QTimer(self)
            self.micro_timer.setSingleShot(True)
            self.micro_timer.timeout.connect(self._play_idle_micro)
            if not self.reduced_motion:
                self._schedule_micro()
            self.snapshot_saved = False
            self.interaction_seed = 0
            self.card_key: tuple[Any, ...] | None = None
            self.setWindowTitle("DSH 小鲸鱼")
            setup = window_setup(sys.platform)
            flags = Qt.WindowType(0)
            for flag in setup["flags"]:
                flags |= getattr(Qt.WindowType, flag)
            self.setWindowFlags(flags)
            for attribute in setup["attributes"]:
                self.setAttribute(getattr(Qt.WidgetAttribute, attribute), True)
            # Needed for hover feedback on the approval choices. Unrelated to the
            # cursor-follow, which polls QCursor.pos() because it has to work
            # when the pointer is nowhere near this window.
            self.setMouseTracking(True)
            self._apply_window_size()
            QTimer.singleShot(0, self._restore_visible_position)

        def apply_message(self, message: dict[str, Any]) -> None:
            recorder.record(message)
            kind = message.get("kind")
            if kind == "shutdown":
                QApplication.quit()
                return
            if kind == "ask":
                pending = ask_from_message(message)
                if pending is not None:
                    self.ask = pending
                    self.ask_hover = None
                    self._apply_window_size()
                    self.update()
                return
            if kind == "emote":
                # A pack that lacks this clip simply does not react. Packs
                # deliberately carry different vocabularies, and a missing
                # flourish is a better outcome than an error.
                clip = str(message.get("clip", "")).strip()
                if clip and clip in self.model.clips:
                    self._play_model_overlay(clip)
                return
            if kind == "ask-clear":
                if self.ask is not None:
                    self.ask = None
                    self.ask_hit_rects = []
                    self._apply_window_size()
                    self.update()
                return
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            if kind == "task":
                self.task = str(message.get("task", ""))
                self._show_status(
                    str(message.get("message", self.task)),
                    str(message.get("detail", "")),
                    self.model.base_state,
                    None if self.model.base_state in {"THINKING", "WORKING", "WAITING", "ERROR"} else 6000,
                )
            elif kind == "tasks":
                raw_tasks = message.get("tasks")
                self.tasks = raw_tasks if isinstance(raw_tasks, list) else []
                self._sync_bubble_size()
            elif kind == "config":
                self._apply_config(message)
            elif kind in {"state", "pulse"}:
                state = str(message.get("state", "IDLE"))
                self.display_state = state
                if kind == "pulse":
                    ttl_ms = max(250, int(message.get("ttlMs", 1800)))
                    resume_state = str(message.get("resumeState", self.model.base_state))
                    self.model.apply_pulse(
                        state,
                        ttl_ms,
                        self._now_ms(),
                        resume_state,
                        message.get("resumeActivity"),
                    )
                    self._show_status(
                        str(message.get("resumeMessage", self.LABELS.get(resume_state, resume_state))),
                        str(message.get("resumeDetail", "")),
                        resume_state,
                        None if resume_state in {"THINKING", "WORKING", "WAITING", "ERROR"} else ttl_ms + 2200,
                    )
                    self._show_overlay(
                        str(message.get("message", self.LABELS.get(state, state))),
                        str(message.get("detail", "")),
                        state,
                        ttl_ms,
                    )
                    if state in {"SUCCESS", "ERROR"}:
                        self._notify_alert(state)
                else:
                    activity = None if self.reduced_motion else message.get("activity")
                    self.model.apply_state(state, activity)
                    self._clear_overlay()
                    persistent = state in {"THINKING", "WORKING", "WAITING", "ERROR"}
                    self._show_status(
                        str(message.get("message", self.LABELS.get(state, state))),
                        str(message.get("detail", "")),
                        state,
                        None if persistent else 4200,
                    )
            self._sync_frame_transition(previous_frame, previous_clip)
            self._sync_bubble_size()
            self.update()
            if snapshot_path is not None and not self.snapshot_saved:
                QTimer.singleShot(180, self._save_snapshot)

        # -- rig plumbing --------------------------------------------------- #

        def _build_rig_driver(self) -> None:
            """Attach a driver to a rig pack, or clear it for a frame pack."""
            if self.rig is None:
                self.rig_driver = None
                self.rig_transforms = None
                return
            self.rig_driver = RigDriver(
                self.rig,
                reduced_motion=self.reduced_motion,
                activity_level=self.activity_level,
            )
            self._advance_rig(0, self._now_ms())

        def _advance_rig(self, elapsed_ms: int, now_ms: int) -> None:
            """Step the driver and re-solve the pose for this tick.

            Solving here rather than inside ``paintEvent`` keeps the paint
            handler free of state: a repaint triggered by the window manager
            redraws the pose the last tick produced instead of silently
            advancing the animation at the compositor's rate.
            """
            driver = self.rig_driver
            if driver is None:
                return
            driver.sync_model(
                self.model.base_clip_name,
                self.model.pulse_clip_name,
                self.model.overlay_clip_name,
                now_ms,
            )
            pointer_x, pointer_y, present = self._pointer_offset()
            driver.set_pointer(pointer_x, pointer_y, present, now_ms)
            if self.dragging:
                vx, vy = self.anchor_velocity.sample(now_ms)
                driver.set_root_motion(vx, vy, True)
            elif driver.dragging:
                # The release tick, and only the release tick: hand the final
                # velocity over once and then stop reporting, because the
                # driver's own release decay lives in the value we would
                # otherwise overwrite. Keep pushing it every tick and the
                # chains stay pinned at throw speed forever instead of
                # overshooting once and settling.
                driver.set_root_motion(*self.anchor_velocity.value, False)
                self.anchor_velocity.reset()
            params = driver.advance(elapsed_ms, now_ms)
            self.rig_transforms = driver.model.solve(params)

        def _pet_padding(self) -> tuple[int, int, int, int]:
            """Window padding around the logical pet box, in device pixels.

            A frame pack gets exactly the historical 25/18/25/8, so its window
            geometry is unchanged to the pixel. A rig pack widens the padding to
            cover its declared ``overflow`` -- unconditionally, at every scale,
            whatever the current pose is, which is what makes "a swinging tail
            cannot be clipped" a property of the window rather than a hope about
            the animation.
            """
            left, top, right, bottom = 25, 18, 25, 8
            if self.rig is None:
                return left, top, right, bottom
            over_left, over_top, over_right, over_bottom = self.renderer.overflow_px(
                self.scale
            )
            return (
                max(left, math.ceil(over_left)),
                max(top, math.ceil(over_top)),
                max(right, math.ceil(over_right)),
                max(bottom, math.ceil(over_bottom)),
            )

        def _bubble_height(self) -> int:
            """Vertical space the bubble occupies, mirroring ``paintEvent``.

            ``paintEvent`` derives this inline while drawing; recomputing it
            here lets the repaint rect use the same clamped pet top the paint
            will, instead of a rect that is right only when no card is up.
            """
            visible = self._bubble_visible()
            if not visible:
                return 12
            if len(self.tasks) >= 2 or self._current_card():
                _, card_y, _, card_height = self._bubble_rect()
                return card_y + card_height + 19
            return 12

        def _rig_anchor(self, bubble_height: int = 0) -> tuple[float, float]:
            """Widget-space point the rig's rest foot anchor maps onto.

            Derived from ``_pet_rect`` and ``footAnchor`` only -- both of which
            are rest-space quantities -- so no amount of deformation can move
            it. This is also the origin Phase F needs to convert a click into
            rig source coordinates (see ``RigRenderer.to_source``).
            """
            pet_x, pet_y, pet_width, pet_height = self._pet_rect()
            top = max(pet_y, bubble_height)
            foot_x, foot_y = self.renderer.foot_fraction
            return (pet_x + foot_x * pet_width, top + foot_y * pet_height)

        def _pointer_offset(self) -> tuple[float, float, bool]:
            """Poll the global cursor and normalise it against the pet box.

            Polled once per tick rather than tracked through events, because
            ``setMouseTracking``/``enterEvent`` only fire while the pointer is
            *over this widget* and the pet has to follow a cursor anywhere on
            the desktop. ``QCursor.pos()`` is a cheap platform call returning
            virtual-desktop coordinates in the same space as ``pet_x``/
            ``pet_y``, and it needs no focus, no ``WA_Hover``, no pointer grab
            and no global hook.
            """
            if self.reduced_motion:
                return pointer_target(
                    (0.0, 0.0), (0, 0, 0, 0), reduced_motion=True, same_screen=True
                )
            global_pos = QCursor.pos()
            pet_width, pet_height = self._pet_size()
            pet_screen = QApplication.screenAt(
                QPoint(self.pet_x + pet_width // 2, self.pet_y + pet_height // 2)
            )
            cursor_screen = QApplication.screenAt(global_pos)
            same_screen = (
                pet_screen is None
                or cursor_screen is None
                or cursor_screen is pet_screen
            )
            return pointer_target(
                (global_pos.x(), global_pos.y()),
                (self.pet_x, self.pet_y, pet_width, pet_height),
                reduced_motion=False,
                same_screen=same_screen,
            )

        def _record_anchor_velocity(self) -> None:
            """Feed the current anchor into the low-pass, mid-drag only.

            Called from ``_move_to_pet`` because that is where the anchor
            actually changes, and only while dragging because a programmatic
            move (screen change, restore) is a teleport, not a throw.
            """
            if self.rig is None or not self.dragging:
                return
            self.anchor_velocity.update(self.pet_x, self.pet_y, self._now_ms())

        def _switch_pack(self, pack_id: str) -> bool:
            """Swap the proportion pack in place.

            CONFIG is delivered without restarting the helper, so this runs
            while the window is visible. Everything is built before anything is
            published, which is why a pack that fails to load leaves the running
            pack fully intact instead of half-swapped.
            """
            try:
                assets = load_pack_assets(bundle_root(), pack_id)
            except PACK_LOAD_ERRORS as error:
                print(
                    f"warning: proportion pack '{pack_id}' unavailable ({error}); "
                    f"keeping '{self.descriptor.pack_id}'",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            model = AnimationModel(assets.animation_manifest)
            model.apply_state(self.model.base_state, self.model.base_activity)
            self.descriptor = assets.descriptor
            self.manifest = assets.animation_manifest
            self.pixmaps = assets.pixmaps
            self.rig = assets.rig
            self.renderer = assets.renderer
            self.model = model
            # The outgoing pixmap belongs to the old pack; fading into the new
            # one would blend two different characters.
            self.fade_from_pixmap = None
            self.last_tick_ms = self._now_ms()
            # Switching in either direction changes the tick rate and the rig
            # state, so both are rebuilt from the incoming pack rather than
            # patched -- a frames->rig swap must not inherit a stale driver and
            # a rig->frames swap must not keep solving a rig nobody draws.
            self._build_rig_driver()
            self.animation_timer.setInterval(self.renderer.tick_ms(self.reduced_motion))
            self._apply_window_size()
            self._move_to_pet(self.pet_x, self.pet_y)
            self.update()
            return True

        def _apply_config(self, message: dict[str, Any]) -> None:
            """Apply a live CONFIG message without restarting the window."""
            proportion = message.get("characterProportion")
            if isinstance(proportion, str):
                pack_id = normalise_pack_id(proportion)
                if pack_id != self.descriptor.pack_id:
                    self._switch_pack(pack_id)
            scale = message.get("scale")
            if isinstance(scale, (int, float)) and not isinstance(scale, bool):
                self.scale = min(1.4, max(0.7, float(scale)))
            bubble_scale = message.get("bubbleScale")
            if isinstance(bubble_scale, (int, float)) and not isinstance(bubble_scale, bool):
                self.bubble_scale = min(1.2, max(0.8, float(bubble_scale)))
            reduced_motion = message.get("reducedMotion")
            if isinstance(reduced_motion, bool) and reduced_motion != self.reduced_motion:
                self.reduced_motion = reduced_motion
                if self.rig_driver is not None:
                    self.rig_driver.set_reduced_motion(reduced_motion)
                self.animation_timer.setInterval(self.renderer.tick_ms(self.reduced_motion))
                if self.reduced_motion:
                    self.micro_timer.stop()
                else:
                    self._schedule_micro()
            activity_level = message.get("activityLevel")
            if activity_level in {"quiet", "normal", "lively"}:
                self.activity_level = activity_level
                if self.rig_driver is not None:
                    self.rig_driver.set_activity_level(activity_level)
                if not self.reduced_motion:
                    self._schedule_micro()
            bubble_mode = message.get("bubbleMode")
            if bubble_mode in {"always", "hidden", "custom"}:
                self.bubble_mode = bubble_mode
            bubble_states = message.get("bubbleStates")
            if isinstance(bubble_states, list):
                self.bubble_states = [str(state) for state in bubble_states if isinstance(state, str)]
            self._sync_bubble_size()
            self._save_layout()

        def _tick(self) -> None:
            now_ms = self._now_ms()
            elapsed_ms = max(0, now_ms - self.last_tick_ms)
            self.last_tick_ms = now_ms
            had_pulse = self.model.pulse_state is not None
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            model_elapsed = 0 if self.reduced_motion and self.model.active_clip.loop else elapsed_ms
            self.model.advance(model_elapsed, now_ms)
            self._sync_frame_transition(previous_frame, previous_clip)
            self._advance_rig(elapsed_ms, now_ms)
            if had_pulse and self.model.pulse_state is None:
                self.display_state = self.model.base_state
            if self.overlay_deadline_ms is not None and now_ms >= self.overlay_deadline_ms:
                self._clear_overlay()
            # Only the pet animates between ticks, so the bubble is repainted
            # solely when its content actually changes -- including the tick on
            # which a status or overlay deadline expires it away.
            card_key = (self._bubble_visible(), self._current_card())
            if card_key != self.card_key:
                self.card_key = card_key
                # A rig keeps ticking through a drag (see ``drag_tick_interval``)
                # but must not start issuing full-window repaints while the
                # window is moving -- that is exactly the layered-window flicker
                # the frame-pack timer stop was introduced to avoid.
                # ``_finish_drag`` repaints the whole window once on release.
                if not (self.dragging and self.rig is not None):
                    self.update()
                    return
            self.update(*self._pet_repaint_rect())

        def _play_idle_micro(self) -> None:
            if self.reduced_motion:
                return
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            self.model.play_idle_micro(random.randrange(max(1, len(self.model.idle_micro_clips))))
            self._sync_frame_transition(previous_frame, previous_clip)
            self.update()
            self._schedule_micro()

        def _sync_frame_transition(
            self,
            previous_frame: str,
            previous_clip: str,
            *,
            allow_fade: bool = True,
        ) -> None:
            current_frame = self.model.frame
            if current_frame == previous_frame:
                return
            duration = crossfade_duration(previous_clip, self.model.active_clip_name) if allow_fade else None
            if duration is None:
                self.fade_from_pixmap = None
                return
            self.fade_from_pixmap = self.pixmaps.get(previous_frame)
            self.fade_started = time.monotonic()
            self.fade_duration = duration

        def _play_model_overlay(
            self,
            clip_name: str,
            *,
            allow_fade: bool = True,
            repaint: bool = True,
        ) -> bool:
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            if not self.model.play_overlay(clip_name):
                return False
            self._sync_frame_transition(previous_frame, previous_clip, allow_fade=allow_fade)
            if repaint:
                self.update()
            return True

        def _begin_drag(self) -> None:
            if self.dragging:
                return
            self.dragging = True
            interval = drag_tick_interval(
                self.renderer.tick_ms(self.reduced_motion), self.rig is not None
            )
            if interval is None:
                self.animation_timer.stop()
            else:
                self.animation_timer.setInterval(interval)
                self.anchor_velocity.reset()
                self.anchor_velocity.update(self.pet_x, self.pet_y, self._now_ms())
            self.micro_timer.stop()
            # Remember what the drag is interrupting so the drop can put it
            # back. clear_overlay() returns to the *underlay*, so without this a
            # one-shot that was mid-play -- a head pat, a poke, one of the
            # emoji performances -- is silently discarded when the pet is picked
            # up. A looping clip needs nothing: it is the underlay already.
            self.interrupted_overlay = None
            overlay = self.model.overlay_clip_name
            if overlay is not None and overlay != "dragging":
                clip = self.model.clips.get(overlay)
                if clip is not None and not clip.loop:
                    self.interrupted_overlay = (
                        overlay,
                        self.model.frame_index,
                        self.model.frame_elapsed_ms,
                    )
            self._play_model_overlay("dragging", allow_fade=False, repaint=False)

        def _resume_interrupted_overlay(self) -> bool:
            """Put back the one-shot the drag interrupted, at the frame it reached.

            Resuming rather than restarting matters for the longer performances:
            restarting a 16-frame clip after a two-second drag replays the whole
            thing, which reads as the pet forgetting what it was doing and
            starting over.
            """
            pending = getattr(self, "interrupted_overlay", None)
            self.interrupted_overlay = None
            if pending is None:
                return False
            name, index, elapsed = pending
            if not self.model.play_overlay(name):
                return False
            frames = len(self.model.active_clip.frames)
            self.model.frame_index = max(0, min(index, frames - 1))
            self.model.frame_elapsed_ms = max(0, elapsed)
            return True

        def _finish_drag(self) -> None:
            if not self.dragging:
                return
            now_ms = self._now_ms()
            previous_frame = self.model.frame
            previous_clip = self.model.active_clip_name
            # Expire an underlying pulse before revealing it after a long drag.
            self.model.advance(0, now_ms)
            self.model.clear_overlay()
            resumed = self._resume_interrupted_overlay()
            self._sync_frame_transition(
                previous_frame, previous_clip, allow_fade=not resumed
            )
            self.dragging = False
            self.last_tick_ms = now_ms
            if self.rig is None:
                self.animation_timer.start(self.renderer.tick_ms(self.reduced_motion))
            else:
                # Never stopped, so restore the rate rather than restarting --
                # restarting would drop the settle's first frame. The next tick
                # sees ``dragging`` false while the driver still thinks it is
                # dragging, which is what hands the throw velocity over.
                self.animation_timer.setInterval(
                    self.renderer.tick_ms(self.reduced_motion)
                )
                # The pet-rect-only repaints during the drag may have left a
                # stale card, so repaint the whole window once on release.
                self.update()
            if not self.reduced_motion:
                self._schedule_micro()

        def _schedule_micro(self) -> None:
            if self.reduced_motion:
                self.micro_timer.stop()
                return
            intervals = {
                "quiet": (12000, 24000),
                "normal": (6500, 12500),
                "lively": (3500, 8000),
            }
            lower, upper = intervals.get(self.activity_level, intervals["normal"])
            self.micro_timer.start(random.randint(lower, upper))

        def _bubble_visible(self) -> bool:
            # A pending approval overrides the bubble mode: hiding the bubble
            # would hide the only way to answer it.
            if self.ask is not None:
                return True
            if self.bubble_mode == "hidden":
                return False
            if self.bubble_mode == "always":
                return True
            if len(self.tasks) >= 2:
                return any(task.get("state") in self.bubble_states for task in self.tasks)
            state = self.overlay_state or self.status_state or self.model.base_state or "IDLE"
            return state in self.bubble_states

        def _sync_bubble_size(self) -> None:
            old_size = (self.width(), self.height())
            self._apply_window_size()
            if (self.width(), self.height()) != old_size:
                self._move_to_pet(self.pet_x, self.pet_y)

        def _apply_window_size(self) -> None:
            pet_width, pet_height = self._pet_size()
            left, top, right, bottom = self._pet_padding()
            if self._bubble_visible():
                bubble_width = round(420 * self.bubble_scale)
                bubble_height = self._card_height()
                # The bubble already reserves far more headroom than any rig
                # overflow asks for, so only the bottom padding has to grow.
                self.setFixedSize(
                    max(pet_width + left + right, bubble_width + 28),
                    pet_height + bubble_height + 34 + (bottom - 8),
                )
            else:
                self.setFixedSize(pet_width + left + right, pet_height + top + bottom)

        def _screen_geometry_at(self, x: int, y: int):
            screen = QApplication.screenAt(QPoint(x, y)) or QApplication.primaryScreen()
            if screen is None:
                return None
            return screen.availableGeometry()

        def _pet_size(self) -> tuple[int, int]:
            return (
                round(self.descriptor.logical_width * self.scale),
                round(self.descriptor.logical_height * self.scale),
            )

        def _move_to_pet(self, pet_x: int, pet_y: int) -> None:
            """Move the window so the pet stands at (pet_x, pet_y).

            The pet position is the source of truth; the window is just the
            container that keeps the status bubble on screen.  While the window
            fits on screen the pet stays centered under it.  When the window
            would have to leave the screen, it is clamped and the pet shifts
            inside the window instead, so the pet can stand at any screen
            position while the bubble stays fully visible.
            """
            pet_width, pet_height = self._pet_size()
            geometry = self._screen_geometry_at(pet_x, pet_y)
            if geometry is None:
                self.pet_x = pet_x
                self.pet_y = pet_y
                self.move(
                    pet_x - (self.width() - pet_width) // 2,
                    pet_y - (self.height() - pet_height - self._pet_bottom_slack()),
                )
                self._record_anchor_velocity()
                self.update()
                return

            min_x = geometry.left()
            max_x = max(min_x, geometry.right() - self.width() + 1)
            min_y = geometry.top()
            max_y = max(min_y, geometry.bottom() - self.height() + 1)

            center_offset_x = (self.width() - pet_width) // 2
            window_x = min(max(pet_x - center_offset_x, min_x), max_x)
            offset_x = min(max(pet_x - window_x, 0), self.width() - pet_width)
            self.pet_x = window_x + offset_x

            top_offset_y = self.height() - pet_height - self._pet_bottom_slack()
            window_y = min(max(pet_y - top_offset_y, min_y), max_y)
            self.pet_y = window_y + top_offset_y

            self.move(window_x, window_y)
            self._record_anchor_velocity()
            self.update()

        def _pet_offset_x(self, pet_width: int) -> int:
            return min(max(self.pet_x - self.x(), 0), self.width() - pet_width)

        def _pet_bottom_slack(self) -> int:
            """Gap between the logical pet box and the window's bottom edge.

            8 for frame packs -- the shipped value -- and at least the rig's
            declared bottom overflow otherwise, so a tail that swings below the
            feet is inside the window rather than clipped by it.
            """
            return self._pet_padding()[3]

        def _pet_rect(self) -> tuple[int, int, int, int]:
            pet_width, pet_height = self._pet_size()
            bottom = self._pet_bottom_slack()
            return self._pet_offset_x(pet_width), self.height() - pet_height - bottom, pet_width, pet_height

        def _pet_repaint_rect(self) -> tuple[int, int, int, int]:
            """Pet rect plus the slack the procedural motion can escape into.

            The renderer bobs, sways and rotates the frame around its centre, so
            repainting only ``_pet_rect`` would leave a smear at the extremes.
            The margin covers the largest offset (8px), squash (2%) and rotation
            (2.5 degrees) any clip asks for, scaled with the character.
            """
            pet_x, pet_y, pet_width, pet_height = self._pet_rect()
            if self.rig is not None:
                # A rig's deformation is bounded by its declared overflow, which
                # the window already reserves, so the safe area is a constant
                # per scale rather than the frame renderer's motion-table slack.
                anchor_x, anchor_y = self._rig_anchor(self._bubble_height())
                rx, ry, rw, rh = self.renderer.pet_rect(self.scale)
                left = max(0, int(anchor_x + rx) - 1)
                top = max(0, int(anchor_y + ry) - 1)
                right = min(self.width(), int(anchor_x + rx + rw) + 2)
                bottom = min(self.height(), int(anchor_y + ry + rh) + 2)
                return left, top, right - left, bottom - top
            margin = round(24 * self.scale) + 8
            left = max(0, pet_x - margin)
            top = max(0, pet_y - margin)
            right = min(self.width(), pet_x + pet_width + margin)
            bottom = min(self.height(), pet_y + pet_height + margin)
            return left, top, right - left, bottom - top

        def _bubble_rect(self) -> tuple[int, int, int, int]:
            card_width = round(420 * self.bubble_scale)
            card_height = self._card_height()
            pet_width, _ = self._pet_size()
            pet_center_x = self._pet_offset_x(pet_width) + pet_width // 2
            margin = 14
            card_x = pet_center_x - card_width // 2
            min_x = margin
            max_x = self.width() - card_width - margin
            if max_x < min_x:
                max_x = min_x
            card_x = min(max(card_x, min_x), max_x)
            return card_x, 7, card_width, card_height

        def _restore_visible_position(self) -> None:
            pet_width, pet_height = self._pet_size()
            top_offset = self.height() - pet_height - self._pet_bottom_slack()
            center_offset = (self.width() - pet_width) // 2
            saved_pet_x = self.layout.get("petX")
            saved_pet_y = self.layout.get("petY")
            if isinstance(saved_pet_x, int) and isinstance(saved_pet_y, int):
                pet_x, pet_y = saved_pet_x, saved_pet_y
            else:
                saved_x = self.layout.get("x")
                saved_y = self.layout.get("y")
                if isinstance(saved_x, int) and isinstance(saved_y, int):
                    # Legacy layouts stored the window position.  Recreate the
                    # pet position that the old centered layout would have had.
                    pet_x = saved_x + center_offset
                    pet_y = saved_y + top_offset
                else:
                    geometry = self._screen_geometry_at(self.x() + self.width() // 2, self.y() + self.height() // 2)
                    if geometry is None:
                        return
                    pet_x = geometry.right() - pet_width - 24
                    pet_y = geometry.bottom() - pet_height - 24
            self._move_to_pet(pet_x, pet_y)

        def _save_layout(self) -> None:
            self.layout = {
                "version": 1,
                "x": self.x(),
                "y": self.y(),
                "petX": self.pet_x,
                "petY": self.pet_y,
                "scale": self.scale,
                "bubbleScale": self.bubble_scale,
                "reducedMotion": self.reduced_motion,
                "bubbleMode": self.bubble_mode,
                "bubbleStates": self.bubble_states,
            }
            try:
                save_layout(self.layout_path, self.layout)
            except OSError as error:
                print(f"Unable to save BigFish layout: {error}", file=sys.stderr)

        def _save_snapshot(self) -> None:
            if snapshot_path is None or self.snapshot_saved:
                return
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self.snapshot_saved = self.grab().save(str(snapshot_path), "PNG")

        def _show_status(self, message: str, detail: str, state: str, ttl_ms: int | None) -> None:
            self.status_message = message
            self.status_detail = detail
            self.status_state = state
            self.status_deadline_ms = None if ttl_ms is None else self._now_ms() + ttl_ms

        def _show_overlay(self, message: str, detail: str, state: str, ttl_ms: int) -> None:
            self.overlay_message = message
            self.overlay_detail = detail or self.status_detail
            self.overlay_state = state
            self.overlay_deadline_ms = self._now_ms() + ttl_ms

        def _clear_overlay(self) -> None:
            self.overlay_message = ""
            self.overlay_detail = ""
            self.overlay_state = None
            self.overlay_deadline_ms = None

        @staticmethod
        def _now_ms() -> int:
            return int(time.monotonic() * 1000)

        def _current_card(self) -> tuple[str, str, str] | None:
            now_ms = self._now_ms()
            if self.overlay_message and (
                self.overlay_deadline_ms is None or now_ms < self.overlay_deadline_ms
            ):
                return self.overlay_message, self.overlay_detail, self.overlay_state or self.status_state
            if self.status_message and (
                self.status_deadline_ms is None or now_ms < self.status_deadline_ms
            ):
                return self.status_message, self.status_detail, self.status_state
            return None

        @staticmethod
        def _status_colors(state: str) -> tuple[QColor, QColor]:
            return {
                "SUCCESS": (QColor("#D9F7E4"), QColor("#12B85A")),
                "ERROR": (QColor("#FDE3E3"), QColor("#E5484D")),
                "WAITING": (QColor("#FFF0CE"), QColor("#D88A00")),
                "THINKING": (QColor("#E2ECFF"), QColor("#4C78E8")),
                "WORKING": (QColor("#DDEBFF"), QColor("#3478F6")),
                "DISCONNECTED": (QColor("#ECEEF1"), QColor("#7B818A")),
            }.get(state, (QColor("#ECEEF1"), QColor("#747A84")))

        def _draw_status_icon(self, painter: QPainter, state: str, center_x: int, center_y: int) -> None:
            background, foreground = self._status_colors(state)
            radius = 23
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)
            pen = QPen(foreground, 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if state == "SUCCESS":
                painter.drawLine(center_x - 10, center_y, center_x - 3, center_y + 8)
                painter.drawLine(center_x - 3, center_y + 8, center_x + 12, center_y - 10)
            elif state == "ERROR":
                painter.drawLine(center_x - 8, center_y - 8, center_x + 8, center_y + 8)
                painter.drawLine(center_x + 8, center_y - 8, center_x - 8, center_y + 8)
            elif state == "WAITING":
                painter.drawLine(center_x, center_y - 10, center_x, center_y + 3)
                painter.setBrush(foreground)
                painter.drawEllipse(center_x - 2, center_y + 9, 4, 4)
            elif state in {"THINKING", "WORKING"}:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                for offset in (-9, 0, 9):
                    painter.drawEllipse(center_x + offset - 3, center_y - 3, 6, 6)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                painter.drawEllipse(center_x - 5, center_y - 5, 10, 10)

        def _card_height(self) -> int:
            if self.ask is not None:
                rows = len(self.ask["options"])
                return round((60 + rows * 34) * self.bubble_scale)
            if len(self.tasks) >= 2:
                rows = min(len(self.tasks), 3)
                return round((58 + rows * 26) * self.bubble_scale)
            return round(84 * self.bubble_scale)

        def _draw_ask_card(
            self,
            painter: QPainter,
            card_x: int,
            card_y: int,
            card_width: int,
            card_height: int,
            s: float,
        ) -> None:
            """Draw the pending question and its choices, and record hit rects.

            The rects are stored rather than recomputed on click: the click has
            to test exactly the rectangles that were drawn, and recomputing them
            from the same inputs is one refactor away from disagreeing.
            """
            assert self.ask is not None
            self.ask_hit_rects = []
            pad = round(20 * s)
            title_font = QFont(self.font())
            title_font.setPointSizeF(max(8.0, 10.5 * s))
            title_font.setBold(True)
            painter.setFont(title_font)
            painter.setPen(QColor("#111827"))
            text_width = card_width - pad * 2
            painter.drawText(
                card_x + pad,
                card_y + round(12 * s),
                text_width,
                round(24 * s),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(title_font).elidedText(
                    self.ask["question"], Qt.TextElideMode.ElideRight, text_width
                ),
            )

            option_font = QFont(self.font())
            option_font.setPointSizeF(max(7.5, 9.5 * s))
            painter.setFont(option_font)
            metrics = QFontMetrics(option_font)
            top = card_y + round(40 * s)
            height = round(28 * s)
            gap = round(6 * s)
            for index, option in enumerate(self.ask["options"]):
                y = top + index * (height + gap)
                rect = (card_x + pad, y, text_width, height)
                hovered = self.ask_hover == index
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#E8F0FE") if hovered else QColor("#F3F4F6"))
                painter.drawRoundedRect(*rect, round(8 * s), round(8 * s))
                painter.setPen(QColor("#1F5C96") if hovered else QColor("#374151"))
                painter.drawText(
                    rect[0] + round(12 * s),
                    y,
                    text_width - round(24 * s),
                    height,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    metrics.elidedText(
                        option["label"], Qt.TextElideMode.ElideRight, text_width - round(24 * s)
                    ),
                )
                self.ask_hit_rects.append((*rect, option["label"]))

        def _draw_card_background(
            self,
            painter: QPainter,
            card_x: int,
            card_y: int,
            card_width: int,
            card_height: int,
            corner_radius: int,
            s: float,
        ) -> None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(17, 24, 39, 13))
            painter.drawRoundedRect(
                card_x + 1, card_y + round(13 * s), card_width - 2, card_height,
                corner_radius, corner_radius,
            )
            painter.setBrush(QColor(17, 24, 39, 18))
            painter.drawRoundedRect(
                card_x, card_y + round(7 * s), card_width, card_height,
                corner_radius, corner_radius,
            )
            painter.setPen(QPen(QColor(218, 221, 226, 205), 1))
            painter.setBrush(QColor(252, 252, 253, 248))
            painter.drawRoundedRect(
                card_x, card_y, card_width, card_height,
                corner_radius, corner_radius,
            )

        def _draw_multi_task_card(
            self,
            painter: QPainter,
            card_x: int,
            card_y: int,
            card_width: int,
            card_height: int,
            s: float,
        ) -> None:
            title_font = QFont("Microsoft YaHei UI")
            title_font.setPointSizeF(max(8.0, 11.0 * s))
            title_font.setWeight(QFont.Weight.DemiBold)
            detail_font = QFont("Microsoft YaHei UI")
            detail_font.setPointSizeF(max(7.0, 9.0 * s))
            text_x = card_x + round(16 * s)
            text_width = max(40, card_width - round(32 * s))
            painter.setFont(title_font)
            painter.setPen(QColor("#25282D"))
            title = f"{len(self.tasks)} 个任务进行中"
            painter.drawText(
                text_x,
                card_y + round(10 * s),
                text_width,
                max(12, round(22 * s)),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, text_width),
            )
            painter.setFont(detail_font)
            for index, task in enumerate(self.tasks[:3]):
                row_y = card_y + round((36 + index * 24) * s)
                state = str(task.get("state", "IDLE"))
                state_label = self.LABELS.get(state, state)
                label = task.get("project") or task.get("task") or task.get("message") or state_label
                line = f"{state_label} · {label}"
                _, foreground = self._status_colors(state)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(foreground)
                painter.drawEllipse(text_x, row_y + round(4 * s), round(8 * s), round(8 * s))
                painter.setPen(QColor("#747981"))
                painter.drawText(
                    text_x + round(14 * s),
                    row_y,
                    text_width - round(14 * s),
                    max(12, round(20 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    QFontMetrics(detail_font).elidedText(line, Qt.TextElideMode.ElideRight, text_width - round(14 * s)),
                )
            if len(self.tasks) > 3:
                more = f"还有 {len(self.tasks) - 3} 个任务…"
                painter.setPen(QColor("#9AA0A6"))
                painter.drawText(
                    text_x + round(14 * s),
                    card_y + round((36 + 3 * 24) * s),
                    text_width,
                    max(12, round(20 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    more,
                )

        def _notify_alert(self, state: str) -> None:
            try:
                QApplication.beep()
            except Exception:
                pass
            self._shake_window()

        def _shake_window(self) -> None:
            if self.shake_timer is None:
                self.shake_timer = QTimer(self)
                self.shake_timer.timeout.connect(self._shake_tick)
            self.shake_origin = self.pos()
            self.shake_count = 0
            self.shake_timer.start(30)

        def _shake_tick(self) -> None:
            offsets = [(6, 0), (-6, 0), (4, 0), (-4, 0), (2, 0), (-2, 0), (0, 0)]
            if self.shake_origin is None:
                self.shake_timer.stop()
                return
            if self.shake_count < len(offsets):
                dx, dy = offsets[self.shake_count]
                self.move(self.shake_origin.x() + dx, self.shake_origin.y() + dy)
                self.shake_count += 1
            else:
                self.shake_timer.stop()
                self.move(self.shake_origin)

        def paintEvent(self, _event: Any) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 平滑缩放：放大/缩小时插值，避免锯齿和模糊
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            card = self._current_card() if self._bubble_visible() else None
            bubble_height = 12
            card_x, card_y, card_width, card_height = self._bubble_rect()
            s = self.bubble_scale
            corner_radius = round(30 * s)

            if self.ask is not None:
                bubble_height = card_y + card_height + 19
                self._draw_card_background(painter, card_x, card_y, card_width, card_height, corner_radius, s)
                self._draw_ask_card(painter, card_x, card_y, card_width, card_height, s)
            elif len(self.tasks) >= 2 and self._bubble_visible():
                bubble_height = card_y + card_height + 19
                self._draw_card_background(painter, card_x, card_y, card_width, card_height, corner_radius, s)
                self._draw_multi_task_card(painter, card_x, card_y, card_width, card_height, s)
            elif card:
                title, detail, card_state = card
                bubble_height = card_y + card_height + 19
                self._draw_card_background(painter, card_x, card_y, card_width, card_height, corner_radius, s)
                icon_center_x = card_x + card_width - round(39 * s)
                icon_center_y = card_y + card_height // 2
                painter.save()
                painter.translate(icon_center_x, icon_center_y)
                painter.scale(s, s)
                painter.translate(-icon_center_x, -icon_center_y)
                self._draw_status_icon(painter, card_state, icon_center_x, icon_center_y)
                painter.restore()

                text_x = card_x + round(24 * s)
                text_width = max(40, card_width - round(102 * s))
                title_font = QFont("Microsoft YaHei UI")
                title_font.setPointSizeF(max(8.0, 11.0 * s))
                title_font.setWeight(QFont.Weight.DemiBold)
                detail_font = QFont("Microsoft YaHei UI")
                detail_font.setPointSizeF(max(7.0, 9.0 * s))
                painter.setFont(title_font)
                painter.setPen(QColor("#25282D"))
                title_text = QFontMetrics(title_font).elidedText(
                    title,
                    Qt.TextElideMode.ElideRight,
                    text_width,
                )
                painter.drawText(
                    text_x,
                    card_y + round(15 * s),
                    text_width,
                    max(12, round(27 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    title_text,
                )
                painter.setFont(detail_font)
                painter.setPen(QColor("#747981"))
                detail_text = QFontMetrics(detail_font).elidedText(
                    detail,
                    Qt.TextElideMode.ElideRight,
                    text_width,
                )
                painter.drawText(
                    text_x,
                    card_y + round(43 * s),
                    text_width,
                    max(12, round(24 * s)),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    detail_text,
                )

            if self.rig is not None:
                anchor_x, anchor_y = self._rig_anchor(bubble_height)
                baked = self.pixmaps.get(self.model.frame)
                if baked is not None:
                    self.renderer.paint_baked(
                        painter,
                        baked,
                        anchor_x=anchor_x,
                        anchor_y=anchor_y,
                        scale=self.scale,
                    )
                else:
                    self.renderer.paint(
                        painter,
                        self.rig_transforms or (),
                        anchor_x=anchor_x,
                        anchor_y=anchor_y,
                        scale=self.scale,
                    )
                if self.renderer.take_degradation_notice():
                    print(
                        "warning: rig paint averaged over "
                        f"{self.renderer.last_paint_ms:.1f}ms; dropping to "
                        f"{self.renderer.tick_ms(self.reduced_motion)}ms ticks",
                        file=sys.stderr,
                        flush=True,
                    )
                    self.animation_timer.setInterval(
                        self.renderer.tick_ms(self.reduced_motion)
                    )
            else:
                self.renderer.paint(painter, self, bubble_height)

        def _answer_at(self, x: float, y: float) -> bool:
            """Answer the pending question if the point is on one of its choices.

            Checked before the drag gesture starts: an option sits inside the
            bubble, and without this the click would begin a window drag and the
            answer would never be sent.
            """
            if self.ask is None:
                return False
            value = ask_hit(self.ask_hit_rects, x, y)
            if value is None:
                return False
            emit_reply("answer", id=self.ask["id"], value=value)
            self.ask = None
            self.ask_hit_rects = []
            self.ask_hover = None
            self._apply_window_size()
            self.update()
            return True

        def mousePressEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton and self._answer_at(
                event.position().x(), event.position().y()
            ):
                return
            if event.button() == Qt.MouseButton.LeftButton:
                self.drag_origin = event.globalPosition().toPoint()
                self.pet_origin = QPoint(self.pet_x, self.pet_y)
                self.dragging = False

        def mouseMoveEvent(self, event: QMouseEvent) -> None:
            if self.ask is not None and self.drag_origin is None:
                x, y = event.position().x(), event.position().y()
                hover = None
                for index, (left, top, width, height, _) in enumerate(self.ask_hit_rects):
                    if left <= x <= left + width and top <= y <= top + height:
                        hover = index
                        break
                if hover != self.ask_hover:
                    self.ask_hover = hover
                    self.update()
            if self.drag_origin is not None and self.pet_origin is not None:
                if not self.dragging and (event.globalPosition().toPoint() - self.drag_origin).manhattanLength() > 5:
                    self._begin_drag()
                delta = event.globalPosition().toPoint() - self.drag_origin
                self._move_to_pet(self.pet_origin.x() + delta.x(), self.pet_origin.y() + delta.y())

        def mouseReleaseEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                if self.dragging:
                    self._finish_drag()
                    self._move_to_pet(self.pet_x, self.pet_y)
                    self._save_layout()
                else:
                    self._play_click_interaction(event.position().x(), event.position().y())
            self.drag_origin = None
            self.pet_origin = None
            self.dragging = False

        def _interaction_copy(self, group: str) -> str:
            message = interaction_copy(persona_copy, group, self.interaction_seed)
            self.interaction_seed += 1
            return message

        def _rig_hit(self, x: float, y: float) -> tuple[str | None, tuple[float, float]]:
            """Topmost part under a widget point, plus the point in rig source space.

            Uses ``_rig_anchor`` -- the same origin ``paintEvent`` just painted
            with -- and ``rig_transforms``, the pose the last tick solved. Hit
            testing therefore sees exactly the pixels that are on screen: a
            tail swung far out of its rest rect is pokeable where it *looks*,
            not where it started.
            """
            anchor_x, anchor_y = self._rig_anchor(self._bubble_height())
            source = self.renderer.to_source(
                x, y, anchor_x=anchor_x, anchor_y=anchor_y, scale=self.scale
            )
            part_id = hit_test(
                self.rig_transforms or (),
                self.renderer.alpha_masks(),
                source[0],
                source[1],
            )
            return part_id, source

        def _play_rig_click(self, x: float, y: float) -> bool:
            """Route a click through per-part hit testing. True if it landed.

            A miss returns False and does nothing at all. That is deliberate:
            the press has already armed the window drag, and clicking the
            transparent margin to move the pet is an affordance this window has
            always had. Click-through would need ``WS_EX_TRANSPARENT``, which
            would take the drag with it.
            """
            part_id, source = self._rig_hit(x, y)
            resolved = rig_interaction_for_part(self.rig, part_id)
            if resolved is None:
                return False
            group, entry = resolved

            impulse = entry.get("impulse")
            if isinstance(impulse, Mapping) and self.rig_driver is not None:
                rect, pivot = part_geometry(self.rig, part_id)
                spec = dict(impulse)
                # Distance falloff multiplies whatever the rig declared rather
                # than replacing it, so a rig author still controls the ceiling.
                spec["scale"] = float(spec.get("scale", 1.0)) * impulse_scale(
                    math.dist(source, pivot), rect
                )
                self.rig_driver.apply_impulse(spec)

            clip = entry.get("clip")
            if isinstance(clip, str) and clip:
                self._play_model_overlay(clip)
            ttl = entry.get("ttlMs")
            self._show_overlay(
                self._interaction_copy(
                    interaction_copy_group(entry, group, persona_copy)
                ),
                self.status_detail,
                self.status_state,
                int(ttl) if isinstance(ttl, (int, float)) else RIG_INTERACTION_TTL_MS,
            )
            return True

        def _play_click_interaction(self, x: float, y: float) -> None:
            if self.rig is not None:
                self._play_rig_click(x, y)
                return
            clip, copy_group, ttl = frame_click_interaction(x, y, self._pet_rect())
            self._play_model_overlay(clip)
            self._show_overlay(
                self._interaction_copy(copy_group), self.status_detail, self.status_state, ttl
            )

        def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._play_model_overlay("head_pat")
                self._show_overlay(self._interaction_copy("doubleClick"), self.status_detail, self.status_state, 1800)

        def contextMenuEvent(self, event: Any) -> None:
            menu = QMenu(self)
            size_menu = menu.addMenu("大小")
            size_actions = {}
            for label, scale in (("小", 0.8), ("标准", 1.0), ("大", 1.25)):
                action = size_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(abs(self.scale - scale) < 0.05)
                size_actions[action] = scale
            bubble_size_menu = menu.addMenu("气泡大小")
            bubble_size_actions = {}
            for label, bubble_scale in (("小", 0.8), ("标准", 1.0), ("大", 1.2)):
                action = bubble_size_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(abs(self.bubble_scale - bubble_scale) < 0.05)
                bubble_size_actions[action] = bubble_scale
            reduced_action = menu.addAction("减少动态")
            reduced_action.setCheckable(True)
            reduced_action.setChecked(self.reduced_motion)
            open_webui_action = menu.addAction("打开 WebUI")
            menu.addSeparator()
            hide_action = menu.addAction("本次隐藏")
            exit_action = menu.addAction("本次关闭")
            selected = menu.exec(event.globalPos())
            if selected in size_actions:
                self.scale = size_actions[selected]
                self._apply_window_size()
                self._move_to_pet(self.pet_x, self.pet_y)
                self._save_layout()
            elif selected in bubble_size_actions:
                self.bubble_scale = bubble_size_actions[selected]
                self._apply_window_size()
                self._move_to_pet(self.pet_x, self.pet_y)
                self._save_layout()
            elif selected == reduced_action:
                self.reduced_motion = reduced_action.isChecked()
                if self.rig_driver is not None:
                    self.rig_driver.set_reduced_motion(self.reduced_motion)
                self.animation_timer.setInterval(self.renderer.tick_ms(self.reduced_motion))
                if self.reduced_motion:
                    self.micro_timer.stop()
                else:
                    self._schedule_micro()
                self._save_layout()
                self.update()
            elif selected == open_webui_action:
                QDesktopServices.openUrl(QUrl(self.webui_url))
            elif selected == hide_action:
                self.hide()
            elif selected == exit_action:
                self._save_layout()
                emit_reply("closed", reason="user")
                QApplication.quit()

    application = QApplication(sys.argv[:1])
    application.setQuitOnLastWindowClosed(False)
    try:
        pack_assets = resolve_pack(
            bundle_root(),
            os.environ.get("DSH_DAFEIYU_PROPORTION"),
            loader=load_pack_assets,
        )
    except PACK_LOAD_ERRORS as error:
        print(f"Unable to load BigFish asset manifest: {error}", file=sys.stderr)
        recorder.close()
        return 2
    inbox = Inbox()
    window = CompanionWindow(pack_assets)
    inbox.message.connect(window.apply_message)
    inbox.closed.connect(application.quit)

    def read_stdin() -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = parse_message(line)
                if message.get("kind") == "ping":
                    emit_reply("pong")
                inbox.message.emit(message)
            except (ValueError, json.JSONDecodeError) as error:
                print(json.dumps({"kind": "error", "message": str(error)}), flush=True)
        inbox.closed.emit()

    reader = threading.Thread(target=read_stdin, name="dsh-bigfish-stdin", daemon=True)
    reader.start()
    window.show()
    emit_reply("ready")
    code = application.exec()
    recorder.close()
    return code


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="DSH BigFish native helper")
    parser.add_argument("--headless", action="store_true", help="validate the protocol without opening a window")
    parser.add_argument("--event-log", type=Path, help="append received protocol messages to a JSONL file")
    parser.add_argument("--snapshot", type=Path, help="save one diagnostic visual frame after the first message")
    args = parser.parse_args()
    recorder = EventRecorder(args.event_log)
    return run_headless(recorder) if args.headless else run_visual(recorder, args.snapshot)


if __name__ == "__main__":
    raise SystemExit(main())
