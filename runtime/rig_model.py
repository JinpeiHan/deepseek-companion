"""Qt-free solver for the self-built "Live2D-lite" layered bone deformer.

Nothing in this module imports PySide6, numpy, or touches the filesystem: it is
the math core, so it can be unit tested without a display server and shipped
inside a PyInstaller bundle without extra dependencies.

Three ideas carry the whole design:

* ``Affine`` is a 6-tuple laid out exactly like ``QTransform(a, b, c, d, e, f)``
  so the renderer can hand a solved matrix straight to Qt with no repacking.
* ``RigModel.solve()`` is a *pure* function of the parameter dict. Every piece of
  state that evolves over time -- springs, envelopes, clocks -- lives in the
  driver, which is why the solver can be swept exhaustively in tests.
* ``Spring1D`` integrates on a fixed 1/240 s grid with a carry accumulator, so
  one 100 ms step equals ten 10 ms steps to within 1e-9. Frame-rate independence
  is what makes every downstream animation assertion reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


# --------------------------------------------------------------------------- #
# Affine primitives
# --------------------------------------------------------------------------- #

# (a, b, c, d, e, f) with x' = a*x + c*y + e and y' = b*x + d*y + f.
# This is QTransform's (m11, m12, m21, m22, dx, dy) memory order.
Affine = tuple[float, float, float, float, float, float]

IDENTITY: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

#: Determinants below this are treated as singular by :func:`a_invert`.
DET_EPSILON = 1e-12


def a_multiply(outer: Affine, inner: Affine) -> Affine:
    """Return ``outer o inner`` -- the matrix that applies *inner* first.

    Qt composes the other way round (``a * b`` means "a then b"), so a renderer
    passing our matrices to Qt writes ``QTransform(*matrix) * world``; inside
    this module we stay with conventional right-to-left function composition
    because that is how ``T(pivot) . R . T(-pivot)`` reads.
    """
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def a_translate(tx: float, ty: float) -> Affine:
    return (1.0, 0.0, 0.0, 1.0, float(tx), float(ty))


def a_rotate(degrees: float) -> Affine:
    """Rotation about the origin, matching ``QTransform().rotate(degrees)``."""
    radians = math.radians(degrees)
    cos = math.cos(radians)
    sin = math.sin(radians)
    return (cos, sin, -sin, cos, 0.0, 0.0)


def a_scale(sx: float, sy: float) -> Affine:
    return (float(sx), 0.0, 0.0, float(sy), 0.0, 0.0)


def a_shear(kx: float, ky: float) -> Affine:
    """Shear matching ``QTransform().shear(kx, ky)``: x' = x + kx*y."""
    return (1.0, float(ky), float(kx), 1.0, 0.0, 0.0)


def a_map(matrix: Affine, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def a_invert(matrix: Affine) -> Affine | None:
    """Return the inverse, or ``None`` when the matrix is (near) singular.

    Returning ``None`` rather than raising keeps hit testing branch-free: a
    part collapsed to zero scale simply cannot be poked.
    """
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    if abs(det) < DET_EPSILON:
        return None
    inv_a = d / det
    inv_b = -b / det
    inv_c = -c / det
    inv_d = a / det
    return (
        inv_a,
        inv_b,
        inv_c,
        inv_d,
        -(inv_a * e + inv_c * f),
        -(inv_b * e + inv_d * f),
    )


def a_bbox(matrix: Affine, rect: Sequence[float]) -> tuple[float, float, float, float]:
    """Axis-aligned hull of ``rect`` (x, y, w, h) after *matrix*."""
    x, y, w, h = (float(v) for v in rect)
    corners = (
        a_map(matrix, x, y),
        a_map(matrix, x + w, y),
        a_map(matrix, x, y + h),
        a_map(matrix, x + w, y + h),
    )
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return (left, top, right - left, bottom - top)


def _union_bbox(
    first: tuple[float, float, float, float] | None,
    second: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if first is None:
        return second
    if second is None:
        return first
    left = min(first[0], second[0])
    top = min(first[1], second[1])
    right = max(first[0] + first[2], second[0] + second[2])
    bottom = max(first[1] + first[3], second[1] + second[3])
    return (left, top, right - left, bottom - top)


# --------------------------------------------------------------------------- #
# Solved output
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PartTransform:
    """One drawable layer, fully resolved for a single frame.

    ``matrix`` maps *source pixel* coordinates (the coordinates ``src_rect`` is
    expressed in) to logical pet-space pixels. ``strip_matrices`` is non-empty
    only for parts that opted into ``deform: "strips"``; the renderer then draws
    ``strips`` horizontal slices of ``src_rect``, each with its own matrix, to
    fake a curved bend out of affine pieces.
    """

    part_id: str
    z: float
    matrix: Affine
    src_rect: tuple[float, float, float, float]
    opacity: float
    strips: int = 0
    strip_matrices: tuple[Affine, ...] = ()


# --------------------------------------------------------------------------- #
# Springs
# --------------------------------------------------------------------------- #

#: Fixed integration grid. Every spring in the rig advances on this clock.
SUBSTEP_S = 1.0 / 240.0

#: Longest wall-clock slice a single ``step()`` will integrate. A stalled event
#: loop must not be able to launch the pet across the screen.
MAX_STEP_S = 0.10

#: Guard against float dust dropping a substep at exact multiples of the grid.
_SUBSTEP_EPSILON = 1e-9


class Spring1D:
    """Damped spring integrated at a fixed rate.

    ``stiffness`` is the squared natural frequency ``(rad/s)^2``, so the damping
    coefficient is ``2 * damping_ratio * sqrt(stiffness)`` and ``damping_ratio``
    means the textbook thing: 1.0 is critical, below overshoots, above crawls.

    The integrator is semi-implicit Euler on :data:`SUBSTEP_S` slices with the
    leftover time carried in an accumulator measured *in substeps*. Carrying the
    remainder (rather than rounding each call) is what makes a 100 ms step
    identical to ten 10 ms steps: both consume exactly 24 substeps.
    """

    __slots__ = ("stiffness", "damping_ratio", "value", "velocity", "_carry")

    def __init__(
        self,
        stiffness: float = 120.0,
        damping_ratio: float = 1.0,
        value: float = 0.0,
        velocity: float = 0.0,
    ) -> None:
        self.stiffness = max(0.0, float(stiffness))
        self.damping_ratio = max(0.0, float(damping_ratio))
        self.value = float(value)
        self.velocity = float(velocity)
        self._carry = 0.0

    # -- integration -------------------------------------------------------- #

    def substep(self, target: float) -> None:
        """Advance exactly one :data:`SUBSTEP_S` slice toward *target*."""
        damping = 2.0 * self.damping_ratio * math.sqrt(self.stiffness)
        accel = -self.stiffness * (self.value - target) - damping * self.velocity
        self.velocity += accel * SUBSTEP_S
        self.value += self.velocity * SUBSTEP_S

    def pending_substeps(self, dt_s: float) -> int:
        """Consume *dt_s* into the carry accumulator and return whole substeps."""
        dt = float(dt_s)
        if not dt > 0.0:  # also rejects NaN
            return 0
        if dt > MAX_STEP_S:
            dt = MAX_STEP_S
        self._carry += dt / SUBSTEP_S
        count = int(math.floor(self._carry + _SUBSTEP_EPSILON))
        if count <= 0:
            return 0
        self._carry -= count
        return count

    def step(self, target: float, dt_s: float) -> float:
        """Integrate *dt_s* seconds toward *target* and return the new value."""
        for _ in range(self.pending_substeps(dt_s)):
            self.substep(target)
        return self.value

    # -- impulses ----------------------------------------------------------- #

    def kick(self, delta_velocity: float) -> None:
        """Add an impulse. Poking the pet is velocity, never a keyframe."""
        self.velocity += float(delta_velocity)

    def snap(self, value: float) -> None:
        """Teleport to *value*, killing velocity and any pending substep carry."""
        self.value = float(value)
        self.velocity = 0.0
        self._carry = 0.0


# --------------------------------------------------------------------------- #
# Chains
# --------------------------------------------------------------------------- #

#: Each segment down the chain loses this fraction of the root stiffness, so the
#: tip is floppier than the root and the chain reads as a curve, not a hinge.
CHAIN_STIFFNESS_FALLOFF = 0.18

#: Never let the falloff drive stiffness to zero (or negative) on long chains.
CHAIN_MIN_STIFFNESS_FACTOR = 0.10


class ChainSolver:
    """N-segment bone chain (whale tail, hair) driven by one scalar.

    Segment ``i`` chases ``driver * distribution[i] * amplitude_deg``, runs its
    own spring at ``stiffness * (1 - 0.18*i)``, and reads the driver through a
    ring buffer delayed by ``lag_per_segment_ms * i`` (quantised to substeps).
    The delay is the whole point: without it every segment turns at once and the
    tail reads as a rigid plank instead of a whip.

    ``root_accel`` is added to *every* segment's target. Feeding the derivative
    of the window-drag velocity in there is what makes the hair fly when the
    user throws the pet across the desktop; the sign is negated because the
    tail lags *behind* the acceleration, like a passenger in a braking car.
    """

    def __init__(
        self,
        segments: int,
        *,
        stiffness: float = 90.0,
        damping_ratio: float = 0.45,
        amplitude_deg: float = 14.0,
        distribution: Sequence[float] | None = None,
        lag_per_segment_ms: float = 26.0,
        max_deg: float = 24.0,
        root_gain_x: float = 1.0,
        root_gain_y: float = 0.35,
    ) -> None:
        if segments <= 0:
            raise ValueError("a chain needs at least one segment")
        self.segments = int(segments)
        self.amplitude_deg = float(amplitude_deg)
        self.max_deg = abs(float(max_deg))
        self.lag_per_segment_ms = max(0.0, float(lag_per_segment_ms))
        self.root_gain_x = float(root_gain_x)
        self.root_gain_y = float(root_gain_y)
        if distribution is None:
            distribution = [max(0.0, 1.0 - 0.15 * i) for i in range(self.segments)]
        if len(distribution) < self.segments:
            raise ValueError("distribution is shorter than the segment count")
        self.distribution = tuple(float(v) for v in distribution[: self.segments])

        base = max(0.0, float(stiffness))
        self.springs = [
            Spring1D(
                stiffness=base
                * max(CHAIN_MIN_STIFFNESS_FACTOR, 1.0 - CHAIN_STIFFNESS_FALLOFF * i),
                damping_ratio=damping_ratio,
            )
            for i in range(self.segments)
        ]

        # Delay in whole substeps, quantised once so playback is deterministic.
        self.delays = tuple(
            int(round(self.lag_per_segment_ms * i / 1000.0 / SUBSTEP_S))
            for i in range(self.segments)
        )
        self._history: list[float] = [0.0] * (max(self.delays) + 1)
        self._head = 0
        self._carry = 0.0

    # -- state -------------------------------------------------------------- #

    @property
    def angles(self) -> tuple[float, ...]:
        """Current per-segment angles in degrees, clamped to +/- ``max_deg``."""
        return tuple(spring.value for spring in self.springs)

    # -- integration -------------------------------------------------------- #

    def step(
        self,
        driver_value: float,
        dt_s: float,
        root_accel: tuple[float, float] = (0.0, 0.0),
    ) -> tuple[float, ...]:
        driver = float(driver_value)
        root_term = -(
            float(root_accel[0]) * self.root_gain_x
            + float(root_accel[1]) * self.root_gain_y
        )
        size = len(self._history)
        for _ in range(self._pending_substeps(dt_s)):
            self._history[self._head] = driver
            for i, spring in enumerate(self.springs):
                delayed = self._history[(self._head - self.delays[i]) % size]
                target = delayed * self.distribution[i] * self.amplitude_deg + root_term
                spring.substep(target)
                if spring.value > self.max_deg:
                    spring.value = self.max_deg
                    spring.velocity = min(spring.velocity, 0.0)
                elif spring.value < -self.max_deg:
                    spring.value = -self.max_deg
                    spring.velocity = max(spring.velocity, 0.0)
            self._head = (self._head + 1) % size
        return self.angles

    def _pending_substeps(self, dt_s: float) -> int:
        dt = float(dt_s)
        if not dt > 0.0:
            return 0
        if dt > MAX_STEP_S:
            dt = MAX_STEP_S
        self._carry += dt / SUBSTEP_S
        count = int(math.floor(self._carry + _SUBSTEP_EPSILON))
        if count <= 0:
            return 0
        self._carry -= count
        return count

    # -- impulses ----------------------------------------------------------- #

    def kick(self, angular_velocity: float) -> None:
        """Whip the chain. Distal segments get more, so a poke reads as a lash."""
        for i, spring in enumerate(self.springs):
            spring.kick(float(angular_velocity) * self.distribution[i])

    def snap_to_rest(self) -> None:
        """Instantly settle. Reduced-motion mode calls this instead of stepping."""
        for spring in self.springs:
            spring.snap(0.0)
        self._history = [0.0] * len(self._history)
        self._head = 0
        self._carry = 0.0


# --------------------------------------------------------------------------- #
# Rig
# --------------------------------------------------------------------------- #

ADDITIVE_CHANNELS = frozenset({"rotate", "translateX", "translateY", "shearX", "shearY"})
MULTIPLICATIVE_CHANNELS = frozenset({"scaleX", "scaleY", "opacity"})
CHANNELS = ADDITIVE_CHANNELS | MULTIPLICATIVE_CHANNELS

_CHANNEL_ALIASES = {
    "rotateZ": "rotate",
    "rotation": "rotate",
    "tx": "translateX",
    "ty": "translateY",
    "sx": "scaleX",
    "sy": "scaleY",
    "alpha": "opacity",
}


def canonical_channel(name: str) -> str:
    return _CHANNEL_ALIASES.get(name, name)


def _neutral(channel: str) -> float:
    return 1.0 if channel in MULTIPLICATIVE_CHANNELS else 0.0


class RigValidationError(ValueError):
    """Raised by :meth:`RigModel.validate` with every problem found at once."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ParamSpec:
    name: str
    minimum: float
    maximum: float
    default: float

    def clamp(self, value: float) -> float:
        return min(self.maximum, max(self.minimum, float(value)))


@dataclass(frozen=True)
class Bone:
    id: str
    parent: str | None
    pivot: tuple[float, float]
    chain: str | None = None
    chain_index: int = 0


@dataclass(frozen=True)
class Part:
    id: str
    z: float
    bone: str
    rect: tuple[float, float, float, float]
    pivot: tuple[float, float]
    file: str | None = None
    hit_group: str | None = None
    strips: int = 0
    strip_bones: tuple[str, ...] = ()


@dataclass(frozen=True)
class Binding:
    param: str
    channel: str
    bone: str | None = None
    part: str | None = None
    gain: float = 1.0
    bias: float = 0.0
    curve: tuple[tuple[float, float], ...] = ()

    def evaluate(self, param_value: float) -> float:
        """Map a parameter to a channel contribution.

        A ``curve`` LUT wins when present: sorted keys, piecewise linear,
        clamped at both ends. Otherwise the plain affine ``gain/bias`` form.
        """
        if self.curve:
            return _sample_curve(self.curve, param_value)
        return param_value * self.gain + self.bias


@dataclass(frozen=True)
class ChainSpec:
    """Declarative half of a chain. The live springs belong to the driver."""

    name: str
    driver: str
    amplitude_deg: float
    distribution: tuple[float, ...]
    stiffness: float
    damping_ratio: float
    lag_per_segment_ms: float
    max_deg: float
    deform: str | None
    bones: tuple[str, ...]
    segment_params: tuple[str, ...]

    def solver(self) -> ChainSolver:
        """Build a live :class:`ChainSolver` matching this declaration."""
        return ChainSolver(
            segments=max(1, len(self.bones) or len(self.distribution)),
            stiffness=self.stiffness,
            damping_ratio=self.damping_ratio,
            amplitude_deg=self.amplitude_deg,
            distribution=self.distribution or None,
            lag_per_segment_ms=self.lag_per_segment_ms,
            max_deg=self.max_deg,
        )


def _sample_curve(curve: Sequence[Sequence[float]], x: float) -> float:
    first_in, first_out = curve[0]
    if x <= first_in:
        return float(first_out)
    last_in, last_out = curve[-1]
    if x >= last_in:
        return float(last_out)
    for index in range(1, len(curve)):
        x1, y1 = curve[index]
        if x <= x1:
            x0, y0 = curve[index - 1]
            span = x1 - x0
            if span <= 0:
                return float(y1)
            return float(y0) + (float(y1) - float(y0)) * ((x - x0) / span)
    return float(last_out)


def _as_pair(
    value: Any, fallback: tuple[float, float] = (0.0, 0.0)
) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return (float(value.get("x", fallback[0])), float(value.get("y", fallback[1])))
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) >= 2
    ):
        return (float(value[0]), float(value[1]))
    return fallback


def _as_rect(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, Mapping):
        return (
            float(value.get("x", 0.0)),
            float(value.get("y", 0.0)),
            float(value.get("w", value.get("width", 0.0))),
            float(value.get("h", value.get("height", 0.0))),
        )
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) >= 4
    ):
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    return (0.0, 0.0, 0.0, 0.0)


def local_affine(
    pivot: tuple[float, float],
    *,
    tx: float = 0.0,
    ty: float = 0.0,
    rotate: float = 0.0,
    shear_x: float = 0.0,
    shear_y: float = 0.0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> Affine:
    """``L = T(pivot) . T(tx,ty) . R(rot) . Shear(kx,ky) . S(sx,sy) . T(-pivot)``.

    Scale is innermost so a squashed part squashes about its own pivot before
    the bone rotates it; shear sits between so it stays axis-aligned in the
    part's own frame, which is what artists expect when they tune a lean.
    """
    px, py = pivot
    matrix = a_translate(px + tx, py + ty)
    if rotate:
        matrix = a_multiply(matrix, a_rotate(rotate))
    if shear_x or shear_y:
        matrix = a_multiply(matrix, a_shear(shear_x, shear_y))
    if scale_x != 1.0 or scale_y != 1.0:
        matrix = a_multiply(matrix, a_scale(scale_x, scale_y))
    return a_multiply(matrix, a_translate(-px, -py))


def _lerp_affine(first: Affine, second: Affine, t: float) -> Affine:
    return (
        first[0] + (second[0] - first[0]) * t,
        first[1] + (second[1] - first[1]) * t,
        first[2] + (second[2] - first[2]) * t,
        first[3] + (second[3] - first[3]) * t,
        first[4] + (second[4] - first[4]) * t,
        first[5] + (second[5] - first[5]) * t,
    )


class RigModel:
    """Parsed rig with a pure :meth:`solve`.

    Construction is deliberately forgiving -- a rig with a bone cycle still
    builds, so :meth:`validate` can report *every* problem at once instead of
    blowing up on the first one while an artist is mid-edit.
    """

    def __init__(self, rig: Mapping[str, Any]) -> None:
        self.raw = dict(rig)
        self.format_version = int(rig.get("formatVersion", 3))
        self.logical_width = float(rig.get("logicalWidth", 0.0))
        self.logical_height = float(rig.get("logicalHeight", 0.0))
        self.overflow = self._parse_overflow(rig.get("overflow"))
        self.params = self._parse_params(rig.get("params", {}))
        self.bones = self._parse_bones(rig.get("bones", ()))
        self.parts = self._parse_parts(rig.get("parts", ()))
        self.bindings = self._parse_bindings(rig.get("bindings", ()))
        self.chains = self._parse_chains(rig.get("chains", {}))
        self._duplicate_bone_ids = self._find_duplicates(rig.get("bones", ()))
        self._duplicate_part_ids = self._find_duplicates(rig.get("parts", ()))

    # -- parsing ------------------------------------------------------------ #

    @staticmethod
    def _find_duplicates(entries: Any) -> tuple[str, ...]:
        if isinstance(entries, Mapping) or not isinstance(entries, Iterable):
            return ()
        seen: set[str] = set()
        dupes: list[str] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            value = str(entry.get("id", ""))
            if value in seen and value not in dupes:
                dupes.append(value)
            seen.add(value)
        return tuple(dupes)

    @staticmethod
    def _parse_overflow(value: Any) -> tuple[float, float, float, float]:
        """Return (left, top, right, bottom) padding the window must reserve."""
        if isinstance(value, Mapping):
            return (
                float(value.get("left", 0.0)),
                float(value.get("top", 0.0)),
                float(value.get("right", 0.0)),
                float(value.get("bottom", 0.0)),
            )
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) >= 4
        ):
            return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            pad = float(value)
            return (pad, pad, pad, pad)
        return (0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def _parse_params(raw: Any) -> dict[str, ParamSpec]:
        params: dict[str, ParamSpec] = {}
        if not isinstance(raw, Mapping):
            return params
        for name, spec in raw.items():
            spec = spec if isinstance(spec, Mapping) else {}
            minimum = float(spec.get("min", 0.0))
            maximum = float(spec.get("max", 1.0))
            if maximum < minimum:
                minimum, maximum = maximum, minimum
            default = float(spec.get("default", 0.0))
            params[name] = ParamSpec(
                name=str(name),
                minimum=minimum,
                maximum=maximum,
                default=min(maximum, max(minimum, default)),
            )
        return params

    @staticmethod
    def _iter_entries(raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, Mapping):
            out: list[dict[str, Any]] = []
            for key, value in raw.items():
                entry = dict(value) if isinstance(value, Mapping) else {}
                entry.setdefault("id", key)
                out.append(entry)
            return out
        if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
            return [dict(entry) for entry in raw if isinstance(entry, Mapping)]
        return []

    def _parse_bones(self, raw: Any) -> dict[str, Bone]:
        bones: dict[str, Bone] = {}
        for entry in self._iter_entries(raw):
            bone_id = str(entry.get("id", ""))
            parent = entry.get("parent")
            bones[bone_id] = Bone(
                id=bone_id,
                parent=None if parent in (None, "") else str(parent),
                pivot=_as_pair(entry.get("pivot")),
                chain=entry.get("chain"),
                chain_index=int(entry.get("chainIndex", 0) or 0),
            )
        return bones

    def _parse_parts(self, raw: Any) -> dict[str, Part]:
        parts: dict[str, Part] = {}
        for entry in self._iter_entries(raw):
            part_id = str(entry.get("id", ""))
            rect = _as_rect(entry.get("rect"))
            default_pivot = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
            parts[part_id] = Part(
                id=part_id,
                z=float(entry.get("z", 0.0)),
                bone=str(entry.get("bone", "")),
                rect=rect,
                pivot=_as_pair(entry.get("pivot"), default_pivot),
                file=entry.get("file"),
                hit_group=entry.get("hitGroup"),
                strips=int(entry.get("strips", 0) or 0),
                strip_bones=tuple(str(b) for b in (entry.get("stripBones") or ())),
            )
        return parts

    @staticmethod
    def _parse_bindings(raw: Any) -> tuple[Binding, ...]:
        if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
            return ()
        bindings: list[Binding] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            curve = entry.get("curve") or ()
            bindings.append(
                Binding(
                    param=str(entry.get("param", "")),
                    channel=canonical_channel(str(entry.get("channel", ""))),
                    bone=entry.get("bone"),
                    part=entry.get("part"),
                    gain=float(entry.get("gain", 1.0)),
                    bias=float(entry.get("bias", 0.0)),
                    curve=tuple(
                        (float(point[0]), float(point[1]))
                        for point in curve
                        if isinstance(point, Sequence) and len(point) >= 2
                    ),
                )
            )
        return tuple(bindings)

    @staticmethod
    def _parse_chains(raw: Any) -> dict[str, ChainSpec]:
        chains: dict[str, ChainSpec] = {}
        if not isinstance(raw, Mapping):
            return chains
        for name, entry in raw.items():
            entry = entry if isinstance(entry, Mapping) else {}
            spring = entry.get("spring")
            spring = spring if isinstance(spring, Mapping) else {}
            chains[str(name)] = ChainSpec(
                name=str(name),
                driver=str(entry.get("driver", "")),
                amplitude_deg=float(entry.get("amplitudeDeg", 0.0)),
                distribution=tuple(
                    float(v) for v in (entry.get("distribution") or ())
                ),
                stiffness=float(spring.get("stiffness", 90.0)),
                damping_ratio=float(spring.get("dampingRatio", 0.45)),
                lag_per_segment_ms=float(spring.get("lagPerSegmentMs", 0.0)),
                max_deg=float(spring.get("maxDeg", 0.0)),
                deform=entry.get("deform"),
                bones=tuple(str(b) for b in (entry.get("bones") or ())),
                segment_params=tuple(
                    str(p) for p in (entry.get("segmentParams") or ())
                ),
            )
        return chains

    # -- validation --------------------------------------------------------- #

    def validation_errors(self) -> list[str]:
        """Every structural problem in the rig, as human-readable strings."""
        errors: list[str] = []

        for dup in self._duplicate_bone_ids:
            errors.append(f"duplicate bone id: {dup!r}")
        for dup in self._duplicate_part_ids:
            errors.append(f"duplicate part id: {dup!r}")

        if not self.bones:
            errors.append("rig declares no bones")
        else:
            roots = [bone for bone in self.bones.values() if bone.parent is None]
            if len(roots) != 1:
                errors.append(f"expected exactly one root bone, found {len(roots)}")
        for bone in self.bones.values():
            if bone.parent is not None and bone.parent not in self.bones:
                errors.append(f"bone {bone.id!r} has unknown parent {bone.parent!r}")
        for bone_id in self._cyclic_bones():
            errors.append(f"bone {bone_id!r} is part of a parent cycle")

        for part in self.parts.values():
            if part.bone not in self.bones:
                errors.append(f"part {part.id!r} references unknown bone {part.bone!r}")
            if part.rect[2] <= 0 or part.rect[3] <= 0:
                errors.append(f"part {part.id!r} has an empty rect")
            for bone_id in part.strip_bones:
                if bone_id not in self.bones:
                    errors.append(
                        f"part {part.id!r} strip bone {bone_id!r} is not declared"
                    )

        for binding in self.bindings:
            if binding.param not in self.params:
                errors.append(f"binding references undeclared param {binding.param!r}")
            if binding.channel not in CHANNELS:
                errors.append(f"binding uses unknown channel {binding.channel!r}")
            if bool(binding.bone) == bool(binding.part):
                errors.append(
                    f"binding on param {binding.param!r} must name exactly one of bone/part"
                )
            if binding.bone and binding.bone not in self.bones:
                errors.append(f"binding references unknown bone {binding.bone!r}")
            if binding.part and binding.part not in self.parts:
                errors.append(f"binding references unknown part {binding.part!r}")
            if binding.curve:
                keys = [point[0] for point in binding.curve]
                if keys != sorted(keys):
                    errors.append(
                        f"curve on param {binding.param!r} is not sorted by input"
                    )

        for chain in self.chains.values():
            for bone_id in chain.bones:
                if bone_id not in self.bones:
                    errors.append(
                        f"chain {chain.name!r} references unknown bone {bone_id!r}"
                    )
            for param in chain.segment_params:
                if param not in self.params:
                    errors.append(
                        f"chain {chain.name!r} drives undeclared param {param!r}"
                    )
            if chain.distribution and len(chain.distribution) < len(chain.bones):
                errors.append(
                    f"chain {chain.name!r} distribution is shorter than its bone list"
                )
            if chain.max_deg <= 0:
                errors.append(f"chain {chain.name!r} has a non-positive maxDeg")

        return errors

    def validate(self) -> None:
        """Raise :class:`RigValidationError` listing every structural problem."""
        errors = self.validation_errors()
        if errors:
            raise RigValidationError(errors)

    def _cyclic_bones(self) -> list[str]:
        cyclic: list[str] = []
        for bone_id in self.bones:
            seen: set[str] = set()
            cursor: str | None = bone_id
            while cursor is not None and cursor in self.bones:
                if cursor in seen:
                    cyclic.append(bone_id)
                    break
                seen.add(cursor)
                cursor = self.bones[cursor].parent
        return cyclic

    def _bone_order(self) -> list[str]:
        """Parents before children. Cycles are dropped -- validate() reports them."""
        order: list[str] = []
        placed: set[str] = set()
        remaining = list(self.bones)
        while remaining:
            progressed = False
            still: list[str] = []
            for bone_id in remaining:
                parent = self.bones[bone_id].parent
                if parent is None or parent not in self.bones or parent in placed:
                    order.append(bone_id)
                    placed.add(bone_id)
                    progressed = True
                else:
                    still.append(bone_id)
            if not progressed:
                break
            remaining = still
        return order

    # -- solving ------------------------------------------------------------ #

    def default_params(self) -> dict[str, float]:
        return {name: spec.default for name, spec in self.params.items()}

    def resolve_params(self, params: Mapping[str, float] | None) -> dict[str, float]:
        """Fill in defaults and clamp every value to its declared range."""
        values = self.default_params()
        if params:
            for name, value in params.items():
                spec = self.params.get(name)
                if spec is None:
                    continue
                try:
                    values[name] = spec.clamp(float(value))
                except (TypeError, ValueError):
                    continue
        return values

    def _accumulate(
        self,
        params: Mapping[str, float],
        extra: Mapping[tuple[str, str, str], float] | None,
    ) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
        """Fold every binding into per-target channel dictionaries.

        Multiple bindings onto the same (target, channel) **sum** for additive
        channels (``rotate``, ``translateX/Y``, ``shearX/Y``) and **multiply**
        for ``scaleX``, ``scaleY`` and ``opacity``. This is the single rule
        artists get wrong most often: a breath binding of 1.03 and a squash
        binding of 0.90 on the same ``scaleY`` give 0.927, not 1.93.
        """
        bone_ch: dict[str, dict[str, float]] = {}
        part_ch: dict[str, dict[str, float]] = {}

        def fold(
            table: dict[str, dict[str, float]], key: str, channel: str, value: float
        ) -> None:
            slot = table.setdefault(key, {})
            current = slot.get(channel, _neutral(channel))
            if channel in MULTIPLICATIVE_CHANNELS:
                slot[channel] = current * value
            else:
                slot[channel] = current + value

        for binding in self.bindings:
            if binding.param not in params or binding.channel not in CHANNELS:
                continue
            value = binding.evaluate(params[binding.param])
            if binding.bone and binding.bone in self.bones:
                fold(bone_ch, binding.bone, binding.channel, value)
            elif binding.part and binding.part in self.parts:
                fold(part_ch, binding.part, binding.channel, value)

        if extra:
            for (kind, key, raw_channel), value in extra.items():
                channel = canonical_channel(raw_channel)
                if channel not in CHANNELS:
                    continue
                if kind == "bone" and key in self.bones:
                    fold(bone_ch, key, channel, value)
                elif kind == "part" and key in self.parts:
                    fold(part_ch, key, channel, value)

        return bone_ch, part_ch

    @staticmethod
    def _matrix_from(
        channels: Mapping[str, float], pivot: tuple[float, float]
    ) -> Affine:
        return local_affine(
            pivot,
            tx=channels.get("translateX", 0.0),
            ty=channels.get("translateY", 0.0),
            rotate=channels.get("rotate", 0.0),
            shear_x=channels.get("shearX", 0.0),
            shear_y=channels.get("shearY", 0.0),
            scale_x=channels.get("scaleX", 1.0),
            scale_y=channels.get("scaleY", 1.0),
        )

    def solve(self, params: Mapping[str, float] | None = None) -> list[PartTransform]:
        """Resolve every part for one frame, z-ascending (paint order).

        Pure: the same params always give the same transforms. Springs, decay
        envelopes and clocks live in the driver precisely so this stays true.
        """
        return self._solve(params, None)

    def _solve(
        self,
        params: Mapping[str, float] | None,
        extra: Mapping[tuple[str, str, str], float] | None,
    ) -> list[PartTransform]:
        values = self.resolve_params(params)
        bone_ch, part_ch = self._accumulate(values, extra)

        world: dict[str, Affine] = {}
        bone_opacity: dict[str, float] = {}
        for bone_id in self._bone_order():
            bone = self.bones[bone_id]
            channels = bone_ch.get(bone_id, {})
            local = self._matrix_from(channels, bone.pivot)
            parent = bone.parent
            if parent is not None and parent in world:
                world[bone_id] = a_multiply(world[parent], local)
                bone_opacity[bone_id] = bone_opacity.get(parent, 1.0) * channels.get(
                    "opacity", 1.0
                )
            else:
                world[bone_id] = local
                bone_opacity[bone_id] = channels.get("opacity", 1.0)

        transforms: list[PartTransform] = []
        for part in self.parts.values():
            base = world.get(part.bone, IDENTITY)
            channels = part_ch.get(part.id, {})
            local = self._matrix_from(channels, part.pivot)
            strip_matrices = self._strip_matrices(part, world, local)
            transforms.append(
                PartTransform(
                    part_id=part.id,
                    z=part.z,
                    matrix=a_multiply(base, local),
                    src_rect=part.rect,
                    opacity=max(
                        0.0,
                        min(
                            1.0,
                            bone_opacity.get(part.bone, 1.0)
                            * channels.get("opacity", 1.0),
                        ),
                    ),
                    strips=len(strip_matrices),
                    strip_matrices=strip_matrices,
                )
            )
        transforms.sort(key=lambda t: (t.z, t.part_id))
        return transforms

    def _strip_matrices(
        self, part: Part, world: Mapping[str, Affine], local: Affine
    ) -> tuple[Affine, ...]:
        """Blend the chain's bone matrices along the part's own height.

        QPainter has no textured-triangle primitive, so a bend is faked by
        slicing the part into horizontal strips and giving each one an affine
        interpolated along the chain's arc length. Component-wise lerp is only
        faithful for small inter-segment angles -- which is exactly the regime a
        6-12 segment tail lives in, and why the format caps ``maxDeg``.
        """
        if part.strips <= 1 or len(part.strip_bones) < 2:
            return ()
        chain = [world[b] for b in part.strip_bones if b in world]
        if len(chain) < 2:
            return ()
        span = len(chain) - 1
        out: list[Affine] = []
        for index in range(part.strips):
            position = (index + 0.5) / part.strips * span
            low = min(int(math.floor(position)), span - 1)
            blended = _lerp_affine(chain[low], chain[low + 1], position - low)
            out.append(a_multiply(blended, local))
        return tuple(out)

    # -- bounds ------------------------------------------------------------- #

    def rest_bbox(self) -> tuple[float, float, float, float]:
        """Union of the undeformed part rects: the artboard the pack was cut on."""
        box: tuple[float, float, float, float] | None = None
        for part in self.parts.values():
            box = _union_bbox(box, part.rect)
        return box or (0.0, 0.0, 0.0, 0.0)

    def solve_bbox(
        self, params: Mapping[str, float] | None = None
    ) -> tuple[float, float, float, float]:
        return self._bbox_of(self._solve(params, None))

    @staticmethod
    def _bbox_of(
        transforms: Iterable[PartTransform],
    ) -> tuple[float, float, float, float]:
        box: tuple[float, float, float, float] | None = None
        for transform in transforms:
            box = _union_bbox(box, a_bbox(transform.matrix, transform.src_rect))
            for strip in transform.strip_matrices:
                box = _union_bbox(box, a_bbox(strip, transform.src_rect))
        return box or (0.0, 0.0, 0.0, 0.0)

    def overflow_bbox(self) -> tuple[float, float, float, float]:
        """Rest box grown by the declared overflow: the window's safe area."""
        x, y, w, h = self.rest_bbox()
        left, top, right, bottom = self.overflow
        return (x - left, y - top, w + left + right, h + top + bottom)

    def sweep_bbox(self, samples: int = 5) -> tuple[float, float, float, float]:
        """Worst-case deformed bounds over the declared parameter space.

        Sweeps each param independently (``samples`` points across its range,
        which catches non-monotone curves), then the all-min and all-max
        corners, then every chain pinned to +/- its ``maxDeg``. This is the
        build-time anchor guard: if the union escapes :meth:`overflow_bbox` the
        window would clip the pet at runtime, and failing a test beats shipping
        a clipped tail.
        """
        samples = max(2, int(samples))
        defaults = self.default_params()
        box = _union_bbox(None, self.solve_bbox(defaults))

        for name, spec in self.params.items():
            for index in range(samples):
                t = index / (samples - 1)
                probe = dict(defaults)
                probe[name] = spec.minimum + (spec.maximum - spec.minimum) * t
                box = _union_bbox(box, self.solve_bbox(probe))

        for corner in ("minimum", "maximum"):
            probe = {name: getattr(spec, corner) for name, spec in self.params.items()}
            box = _union_bbox(box, self.solve_bbox(probe))

        for chain in self.chains.values():
            if not chain.bones:
                continue
            for sign in (-1.0, 1.0):
                extra = {
                    ("bone", bone_id, "rotate"): sign * chain.max_deg
                    for bone_id in chain.bones
                }
                box = _union_bbox(box, self._bbox_of(self._solve(defaults, extra)))
        return box or (0.0, 0.0, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# Alpha hit testing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AlphaMask:
    """Coarse packed-bit coverage map for one part, in source-pixel space.

    ``bits`` is row-major, LSB-first within each byte, with no row padding, so
    index ``v * width + u`` addresses cell (u, v). Masks are downsampled to at
    most 128x128 when the pack loads: a poke only needs to know "is there ink
    here", and a 2 KB bitmap keeps a full 20-part sweep well under a
    microsecond. Masks are *injected* into :func:`hit_test` rather than decoded
    here so tests can build them from literal bit patterns without Qt.
    """

    width: int
    height: int
    bits: bytes
    rect: tuple[float, float, float, float]

    @classmethod
    def from_rows(
        cls, rows: Sequence[str], rect: Sequence[float], ink: str = "#"
    ) -> "AlphaMask":
        """Build from ``('..##..', '.####.')`` style rows -- test-friendly."""
        height = len(rows)
        width = max((len(row) for row in rows), default=0)
        raw = bytearray((width * height + 7) // 8)
        for v, row in enumerate(rows):
            for u, char in enumerate(row):
                if char == ink:
                    index = v * width + u
                    raw[index >> 3] |= 1 << (index & 7)
        return cls(width=width, height=height, bits=bytes(raw), rect=_as_rect(rect))

    def covers(self, sx: float, sy: float) -> bool:
        """True when source-space point (sx, sy) lands on an opaque mask cell."""
        x, y, w, h = self.rect
        if w <= 0 or h <= 0 or self.width <= 0 or self.height <= 0:
            return False
        # floor, not int(): truncation toward zero would fold the strip just
        # left of the rect back onto column 0 and report a phantom hit.
        u = math.floor((sx - x) * self.width / w)
        v = math.floor((sy - y) * self.height / h)
        if u < 0 or v < 0 or u >= self.width or v >= self.height:
            return False
        index = v * self.width + u
        byte = index >> 3
        if byte >= len(self.bits):
            return False
        return bool(self.bits[byte] >> (index & 7) & 1)


#: Compass offsets used to grow the probe point into a forgiving halo.
_HALO = (
    (1.0, 0.0),
    (-1.0, 0.0),
    (0.0, 1.0),
    (0.0, -1.0),
    (0.70710678, 0.70710678),
    (0.70710678, -0.70710678),
    (-0.70710678, 0.70710678),
    (-0.70710678, -0.70710678),
)


def hit_test(
    transforms: Iterable[PartTransform],
    masks: Mapping[str, AlphaMask] | None,
    x: float,
    y: float,
    radius: float = 3.0,
) -> str | None:
    """Return the topmost part covering logical point (x, y), else ``None``.

    Walks z-descending and inverse-maps the probe through each part's *current*
    deformed matrix, so a tail swung far out of its rest rect is still pokeable
    and a part collapsed to zero scale is not. ``radius`` is measured in logical
    output pixels and grows the probe into a halo, so a user does not have to
    hit a three-pixel-wide eyelid ribbon dead centre.

    Parts with no mask fall back to plain ``src_rect`` containment, which keeps
    a partially-loaded pack pokeable instead of inert.
    """
    masks = masks or {}
    probes = [(x, y)]
    if radius > 0:
        probes.extend((x + dx * radius, y + dy * radius) for dx, dy in _HALO)

    for transform in sorted(transforms, key=lambda t: (t.z, t.part_id), reverse=True):
        if transform.opacity <= 0.0:
            continue
        inverse = a_invert(transform.matrix)
        if inverse is None:
            continue
        mask = masks.get(transform.part_id)
        rx, ry, rw, rh = transform.src_rect
        for px, py in probes:
            sx, sy = a_map(inverse, px, py)
            if mask is not None:
                if mask.covers(sx, sy):
                    return transform.part_id
            elif rx <= sx <= rx + rw and ry <= sy <= ry + rh:
                return transform.part_id
    return None
