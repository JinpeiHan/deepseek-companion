"""Qt-free parameter driver: animation state plus interaction to rig params.

:class:`~runtime.rig_model.RigModel` solves matrices from a parameter dict and
is deliberately pure. This module owns everything the solver refuses to: the
clock, the springs, the reaction envelopes, the pointer, the drag velocity. One
call to :meth:`RigDriver.advance` per tick turns all of that into the dict the
solver eats.

Three properties are the reason this is a separate object rather than a handful
of statements in ``paintEvent``:

* **Oscillator phase is global.** It is never reset on a clip change, and two
  oscillator sets crossfade over :data:`OSCILLATOR_CROSSFADE_MS`. Without that,
  THINKING -> WORKING restarts the breathing sine mid-inhale and the pet visibly
  hitches at exactly the moment the user is watching it start work.
* **A reaction keeps decaying after the state machine has moved on.**
  :class:`~runtime.animation_model.AnimationModel` supports one overlay, so a
  blink interrupted by a SUCCESS pulse is simply gone from its point of view.
  The envelope here outlives that, which turns a hard cut into a fade.
* **Interaction is velocity, not keyframes.** A poke calls
  :meth:`RigDriver.apply_impulse`, which kicks a spring. That is what makes the
  response continuous (never pops), self-decaying (never needs an end time),
  and stackable (two pokes add velocity instead of restarting an animation).
  Keyframed tracks carry only the *expression*; the *deformation* is the spring.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:  # package import when bundled, flat import when helper.py runs as a script
    from .rig_model import ChainSolver, ParamSpec, RigModel, Spring1D
except ImportError:  # pragma: no cover - exercised by the frozen helper
    from rig_model import ChainSolver, ParamSpec, RigModel, Spring1D

# --------------------------------------------------------------------------- #
# Tuning constants
# --------------------------------------------------------------------------- #

#: Two oscillator sets blend over this long on a clip change.
OSCILLATOR_CROSSFADE_MS = 220.0

#: At most this many reactions decay at once. The pet has one face; beyond four
#: overlapping expression envelopes the result is mush, and the list would grow
#: without bound if a user sat on the mouse button.
MAX_ENVELOPES = 4

#: Pointer offsets below this magnitude are ignored outright. Without a dead
#: zone the pet micro-twitches at every pixel of cursor jitter.
POINTER_DEAD_ZONE = 0.08

#: Pointer motion smaller than this does not count as "the user moved".
POINTER_MOVE_EPSILON = 0.01

#: A pointer parked this long stops being interesting; the pet looks away over
#: :data:`POINTER_RELEASE_MS` instead of staring at an idle cursor forever.
POINTER_IDLE_MS = 6000.0
POINTER_RELEASE_MS = 1500.0

#: Full-deflection pointer follow, in degrees.
HEAD_YAW_DEG = 22.0
HEAD_PITCH_DEG = 14.0
#: Roll opposes yaw. Turning your head right tips it slightly left; this is the
#: classic Live2D counter-roll and its absence is what makes a rig read as a
#: puppet on a stick.
HEAD_ROLL_DEG = -8.0

#: The body leans back against the head so the neck does not shear off.
BODY_COUNTER_GAIN = -0.25

#: Eyes over-shoot the head slightly and, being stiffer, arrive first. Eyes
#: leading the head is what makes the pet look like it decided to look at you.
EYE_GAIN_X = 1.35
EYE_GAIN_Y = 1.20

ACTIVITY_GAINS = {"quiet": 0.6, "normal": 1.0, "lively": 1.15}
DEFAULT_ACTIVITY_LEVEL = "normal"

#: Head follow is critically damped: overshooting a gaze target reads as a
#: flinch, not as liveliness.
HEAD_SPRING = (110.0, 1.0)
EYE_SPRING = (340.0, 1.0)
ROOT_SPRING = (140.0, 0.7)

#: Per-impulse spring characteristics, as ``(stiffness, damping_ratio)``.
#: Angular and squash impulses are underdamped so a poke rebounds; a plain
#: parameter nudge is nearly critical so an expression does not wobble.
IMPULSE_SPRINGS = {
    "angular": (170.0, 0.30),
    "squash": (300.0, 0.35),
    "param": (220.0, 0.80),
}

#: Drag velocity is in logical pixels per second; these convert it to degrees.
ROOT_LEAN_PER_VELOCITY = 0.020
ROOT_BOB_PER_VELOCITY = 0.010
ROOT_LEAN_MAX_DEG = 12.0
ROOT_BOB_MAX = 8.0

#: Acceleration (px/s^2) fed to every chain as a shared target offset. This is
#: the term that makes hair and the whale tail fly when the window is thrown.
ROOT_ACCEL_GAIN = 0.004
ROOT_ACCEL_MAX = 18.0

#: After the mouse is released the estimated velocity bleeds off with this time
#: constant, so chains keep swinging into a coast rather than stopping dead.
ROOT_RELEASE_TAU_S = 0.25

# Parameter names the driver writes by convention. A rig that does not declare
# one simply does not receive it, which is how a pack opts out of, say, eye
# tracking without any code change.
PARAM_HEAD_YAW = "headAngleY"
PARAM_HEAD_PITCH = "headAngleX"
PARAM_HEAD_ROLL = "headAngleZ"
PARAM_BODY_YAW = "bodyAngleY"
PARAM_BODY_ROLL = "bodyAngleZ"
PARAM_EYE_X = "eyeBallX"
PARAM_EYE_Y = "eyeBallY"
PARAM_ROOT_LEAN = "rootLeanZ"
PARAM_ROOT_BOB = "rootBobY"


def smoothstep(t: float) -> float:
    """Hermite ease clamped to [0, 1]. Zero slope at both ends, hence no kink."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# --------------------------------------------------------------------------- #
# Clip pieces
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Oscillator:
    """A looping wave written into one parameter or driver-side scalar.

    ``param`` need not be a declared rig parameter: a chain's ``driver`` is a
    driver-side scalar, produced here and consumed by a :class:`ChainSolver`
    without ever appearing in the solver's parameter dict.
    """

    param: str
    period_ms: float
    amplitude: float = 1.0
    phase: float = 0.0
    bias: float = 0.0
    wave: str = "sin"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Oscillator":
        return cls(
            param=str(raw.get("param", "")),
            period_ms=float(raw.get("periodMs", 1000.0) or 1000.0),
            amplitude=float(raw.get("amplitude", 1.0)),
            phase=float(raw.get("phase", 0.0)),
            bias=float(raw.get("bias", 0.0)),
            wave=str(raw.get("wave", "sin")),
        )

    def value(self, phase_ms: float, amplitude_scale: float = 1.0) -> float:
        if self.period_ms <= 0.0:
            return self.bias
        turns = phase_ms / self.period_ms + self.phase
        radians = 2.0 * math.pi * turns
        if self.wave == "cos":
            shape = math.cos(radians)
        elif self.wave == "triangle":
            frac = turns - math.floor(turns)
            shape = 4.0 * abs(frac - 0.5) - 1.0
        elif self.wave == "pulse":
            shape = 1.0 if (turns - math.floor(turns)) < 0.5 else -1.0
        else:
            shape = math.sin(radians)
        return self.bias + self.amplitude * amplitude_scale * shape


@dataclass(frozen=True)
class Track:
    """A keyframed parameter curve belonging to a one-shot reaction clip."""

    param: str
    keys: tuple[tuple[float, float], ...]
    blend: str = "add"
    interp: str = "linear"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Track":
        keys = tuple(
            (float(key[0]), float(key[1]))
            for key in (raw.get("keys") or ())
            if isinstance(key, Sequence)
            and not isinstance(key, (str, bytes))
            and len(key) >= 2
        )
        return cls(
            param=str(raw.get("param", "")),
            keys=keys,
            blend=str(raw.get("blend", "add")),
            interp=str(raw.get("interp", "linear")),
        )

    def sample(self, t_ms: float) -> float:
        """Value at *t_ms*, held flat outside the keyed range."""
        if not self.keys:
            return 0.0
        if t_ms <= self.keys[0][0]:
            return self.keys[0][1]
        if t_ms >= self.keys[-1][0]:
            return self.keys[-1][1]
        for index in range(1, len(self.keys)):
            t1, v1 = self.keys[index]
            if t_ms <= t1:
                t0, v0 = self.keys[index - 1]
                if self.interp == "step":
                    return v0
                span = t1 - t0
                if span <= 0.0:
                    return v1
                u = (t_ms - t0) / span
                if self.interp == "smooth":
                    u = smoothstep(u)
                return v0 + (v1 - v0) * u
        return self.keys[-1][1]


@dataclass(frozen=True)
class RigClip:
    name: str
    loop: bool
    motion: str | None
    duration_ms: float
    attack_ms: float
    hold_ms: float
    decay_ms: float
    oscillators: tuple[Oscillator, ...]
    tracks: tuple[Track, ...]

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, Any]) -> "RigClip":
        envelope = _as_mapping(raw.get("envelope"))
        duration = float(raw.get("durationMs", 0.0) or 0.0)
        attack = float(envelope.get("attackMs", 0.0) or 0.0)
        # A clip that declares no explicit hold holds for whatever is left of
        # its duration, so ``attack + hold + decay == durationMs + decayMs`` and
        # the driver's envelope and the synthesised manifest agree on length.
        hold = float(envelope.get("holdMs", max(0.0, duration - attack)))
        decay = float(envelope.get("decayMs", 0.0) or 0.0)
        return cls(
            name=name,
            loop=bool(raw.get("loop", True)),
            motion=raw.get("motion"),
            duration_ms=duration,
            attack_ms=max(0.0, attack),
            hold_ms=max(0.0, hold),
            decay_ms=max(0.0, decay),
            oscillators=tuple(
                Oscillator.from_mapping(_as_mapping(entry))
                for entry in (raw.get("oscillators") or ())
            ),
            tracks=tuple(
                Track.from_mapping(_as_mapping(entry))
                for entry in (raw.get("tracks") or ())
            ),
        )


def parse_clips(rig: Mapping[str, Any]) -> dict[str, RigClip]:
    return {
        str(name): RigClip.from_mapping(str(name), _as_mapping(raw))
        for name, raw in _as_mapping(rig.get("clips")).items()
    }


# --------------------------------------------------------------------------- #
# Reaction envelopes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReactionEnvelope:
    """Attack / hold / decay weight for one playing reaction clip.

    The envelope is owned by the driver, not the state machine, and is never
    cancelled from outside. That is the anti-pop guarantee: whatever the state
    machine decides to show next, an in-flight blink still fades out over its
    own ``decay_ms`` instead of being cut mid-frame.
    """

    clip: str
    started_ms: int
    attack_ms: float
    hold_ms: float
    decay_ms: float

    @classmethod
    def from_clip(cls, clip: RigClip, started_ms: int) -> "ReactionEnvelope":
        return cls(
            clip=clip.name,
            started_ms=int(started_ms),
            attack_ms=clip.attack_ms,
            hold_ms=clip.hold_ms,
            decay_ms=clip.decay_ms,
        )

    @property
    def total_ms(self) -> float:
        return self.attack_ms + self.hold_ms + self.decay_ms

    def weight(self, now_ms: int) -> float:
        """Blend weight in [0, 1]: 0 -> 1 -> 1 -> 0 with smoothstep ends."""
        t = float(now_ms) - self.started_ms
        if t <= 0.0:
            return 0.0
        if t < self.attack_ms:
            return smoothstep(t / self.attack_ms)
        sustain_end = self.attack_ms + self.hold_ms
        if t <= sustain_end:
            return 1.0
        if self.decay_ms <= 0.0:
            return 0.0
        return smoothstep(1.0 - (t - sustain_end) / self.decay_ms)

    def finished(self, now_ms: int) -> bool:
        return float(now_ms) - self.started_ms >= self.total_ms


# --------------------------------------------------------------------------- #
# Impulses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Impulse:
    """A velocity injection. Never a pose, never a duration.

    Fields mirror the ``interactions[<group>].impulse`` block of a rig manifest:
    ``chainAngularVel`` whips a whole chain, ``angularVel`` rebounds a rotation
    parameter, ``squashVel`` wobbles a squash parameter, and ``paramVel`` nudges
    anything else. ``scale`` is the caller's distance falloff -- poking the tail
    tip should lash harder than poking its root.
    """

    chain: str | None = None
    param: str | None = None
    chain_angular_vel: float = 0.0
    angular_vel: float = 0.0
    squash_vel: float = 0.0
    param_vel: float = 0.0
    scale: float = 1.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Impulse":
        return cls(
            chain=raw.get("chain"),
            param=raw.get("param"),
            chain_angular_vel=float(raw.get("chainAngularVel", 0.0) or 0.0),
            angular_vel=float(raw.get("angularVel", 0.0) or 0.0),
            squash_vel=float(raw.get("squashVel", 0.0) or 0.0),
            param_vel=float(raw.get("paramVel", 0.0) or 0.0),
            scale=float(raw.get("scale", 1.0)),
        )


# --------------------------------------------------------------------------- #
# Oscillator crossfade
# --------------------------------------------------------------------------- #


@dataclass
class _OscillatorFader:
    """Holds the current oscillator set and fades in from the previous one.

    The phase clock is *not* stored here -- it is global to the driver -- so a
    crossfade changes only which waves are summed, never where in the wave the
    pet is. Breathing therefore continues through a state change instead of
    restarting at the top of the sine.
    """

    key: str | None = None
    current: tuple[Oscillator, ...] = ()
    previous: tuple[Oscillator, ...] = ()
    elapsed_ms: float = OSCILLATOR_CROSSFADE_MS

    def set(self, key: str | None, oscillators: tuple[Oscillator, ...]) -> None:
        if key == self.key:
            return
        self.previous = self.current
        self.current = oscillators
        self.key = key
        self.elapsed_ms = 0.0

    def tick(self, dt_ms: float) -> None:
        if self.elapsed_ms < OSCILLATOR_CROSSFADE_MS:
            self.elapsed_ms = min(OSCILLATOR_CROSSFADE_MS, self.elapsed_ms + dt_ms)

    @property
    def blend(self) -> float:
        if OSCILLATOR_CROSSFADE_MS <= 0.0:
            return 1.0
        return smoothstep(self.elapsed_ms / OSCILLATOR_CROSSFADE_MS)

    def contributions(
        self, phase_ms: float, amplitude_scale: float
    ) -> dict[str, float]:
        blend = self.blend
        out: dict[str, float] = {}
        if blend < 1.0:
            for osc in self.previous:
                out[osc.param] = out.get(osc.param, 0.0) + osc.value(
                    phase_ms, amplitude_scale
                ) * (1.0 - blend)
        for osc in self.current:
            out[osc.param] = out.get(osc.param, 0.0) + osc.value(
                phase_ms, amplitude_scale
            ) * blend
        return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


class RigDriver:
    """Turns animation state and interaction into rig parameters, per tick.

    Qt-free and filesystem-free: construct it from a rig mapping (or a prebuilt
    :class:`RigModel`), feed it the state machine's three clip names, and call
    :meth:`advance` once per frame.
    """

    def __init__(
        self,
        rig: Mapping[str, Any] | RigModel,
        *,
        reduced_motion: bool = False,
        activity_level: str = DEFAULT_ACTIVITY_LEVEL,
    ) -> None:
        self.model = rig if isinstance(rig, RigModel) else RigModel(rig)
        self.raw: Mapping[str, Any] = self.model.raw
        self.clips: dict[str, RigClip] = parse_clips(self.raw)
        self.reduced_motion = bool(reduced_motion)
        self.activity_level = (
            activity_level if activity_level in ACTIVITY_GAINS else DEFAULT_ACTIVITY_LEVEL
        )

        # Clocks. ``_phase_ms`` is monotonic for the whole process lifetime and
        # is deliberately never reset.
        self._phase_ms = 0.0
        self._now_ms = 0

        # Clip state as last reported by the state machine.
        self._base_clip: str | None = None
        self._pulse_clip: str | None = None
        self._overlay_clip: str | None = None
        self._base_osc = _OscillatorFader()
        self._pulse_osc = _OscillatorFader()

        self._envelopes: list[ReactionEnvelope] = []

        # Pointer.
        self._pointer_dx = 0.0
        self._pointer_dy = 0.0
        self._pointer_present = False
        self._pointer_moved_ms = 0
        self._pointer_factor = (0.0, 0.0)

        # Root / drag motion.
        self._root_velocity = (0.0, 0.0)
        self._previous_root_velocity = (0.0, 0.0)
        self._root_accel = (0.0, 0.0)
        self.dragging = False

        self._pointer_springs: dict[str, Spring1D] = {
            PARAM_HEAD_YAW: Spring1D(*HEAD_SPRING),
            PARAM_HEAD_PITCH: Spring1D(*HEAD_SPRING),
            PARAM_HEAD_ROLL: Spring1D(*HEAD_SPRING),
            PARAM_EYE_X: Spring1D(*EYE_SPRING),
            PARAM_EYE_Y: Spring1D(*EYE_SPRING),
        }
        self._pointer_targets: dict[str, float] = {
            name: 0.0 for name in self._pointer_springs
        }
        self._root_springs: dict[str, Spring1D] = {
            PARAM_ROOT_LEAN: Spring1D(*ROOT_SPRING),
            PARAM_ROOT_BOB: Spring1D(*ROOT_SPRING),
        }
        self._root_targets: dict[str, float] = {name: 0.0 for name in self._root_springs}
        #: Impulse springs are created on demand, keyed by (param, kind), and
        #: always relax to zero -- they are a deviation from whatever the rest
        #: of the pipeline produced, not a value in their own right.
        self._impulse_springs: dict[tuple[str, str], Spring1D] = {}

        self._chain_solvers: dict[str, ChainSolver] = {
            name: spec.solver() for name, spec in self.model.chains.items()
        }

        self.last_params: dict[str, float] = self.model.default_params()
        self.last_scalars: dict[str, float] = {}

    # -- configuration ------------------------------------------------------ #

    def set_reduced_motion(self, value: bool) -> None:
        """Toggle reduced motion, settling everything already in flight.

        Snapping on the way *in* matters: leaving a swinging tail to coast would
        contradict the setting for the next second, which is exactly the kind of
        residual motion a user enabling this is trying to get rid of.
        """
        value = bool(value)
        if value == self.reduced_motion:
            return
        self.reduced_motion = value
        if value:
            self._settle()

    def set_activity_level(self, value: str) -> None:
        if value in ACTIVITY_GAINS:
            self.activity_level = value

    @property
    def phase_ms(self) -> float:
        """Monotonic oscillator clock, in milliseconds since construction."""
        return self._phase_ms

    @property
    def activity_gain(self) -> float:
        return ACTIVITY_GAINS[self.activity_level]

    @property
    def envelopes(self) -> tuple[ReactionEnvelope, ...]:
        return tuple(self._envelopes)

    def chain_angles(self, name: str) -> tuple[float, ...]:
        solver = self._chain_solvers.get(name)
        return solver.angles if solver is not None else ()

    def _settle(self) -> None:
        for spring in self._pointer_springs.values():
            spring.snap(0.0)
        for spring in self._root_springs.values():
            spring.snap(0.0)
        for spring in self._impulse_springs.values():
            spring.snap(0.0)
        for solver in self._chain_solvers.values():
            solver.snap_to_rest()

    # -- state machine bridge ----------------------------------------------- #

    def sync_model(
        self,
        base_clip: str | None,
        pulse_clip: str | None,
        overlay_clip: str | None,
        now_ms: int,
    ) -> None:
        """Mirror the three clip layers of :class:`AnimationModel`.

        Only the overlay layer starts reaction envelopes. The base and pulse
        layers contribute oscillators, which crossfade rather than switch.
        """
        self._now_ms = int(now_ms)
        self._base_clip = base_clip
        self._pulse_clip = pulse_clip
        self._base_osc.set(base_clip, self._oscillators_of(base_clip))
        self._pulse_osc.set(pulse_clip, self._oscillators_of(pulse_clip))

        if overlay_clip != self._overlay_clip:
            # Note the asymmetry: a *new* overlay starts an envelope, but the
            # overlay going away does nothing. The envelope decides when it is
            # done, which is what keeps an interrupted reaction from popping.
            if overlay_clip is not None:
                self.trigger_reaction(overlay_clip, now_ms)
            self._overlay_clip = overlay_clip

    def _oscillators_of(self, clip_name: str | None) -> tuple[Oscillator, ...]:
        clip = self.clips.get(clip_name) if clip_name else None
        return clip.oscillators if clip is not None else ()

    def trigger_reaction(self, clip_name: str, now_ms: int) -> bool:
        """Start a reaction envelope for *clip_name*.

        Reactions play under reduced motion too: they are semantic feedback
        ("you patted me"), not decoration, and the existing frame packs already
        play ``head_pat``/``poke`` with reduced motion on.
        """
        clip = self.clips.get(clip_name)
        if clip is None or clip.loop:
            return False
        self._prune_envelopes(now_ms)
        self._envelopes.append(ReactionEnvelope.from_clip(clip, now_ms))
        return True

    def _prune_envelopes(self, now_ms: int) -> None:
        self._envelopes = [
            envelope for envelope in self._envelopes if not envelope.finished(now_ms)
        ]
        # Oldest first: the newest reaction is the one the user just caused and
        # is the one they are looking for feedback from.
        while len(self._envelopes) >= MAX_ENVELOPES:
            self._envelopes.pop(0)

    # -- interaction inputs -------------------------------------------------- #

    def set_pointer(self, dx: float, dy: float, present: bool, now_ms: int) -> None:
        """Report the pointer as an offset from the pet centre, roughly in [-1, 1].

        Values outside that range are fine and expected -- the follow ramp
        saturates, and the resulting angle is clamped to the declared parameter
        range, so a cursor on the far monitor simply means "fully turned".
        """
        dx = float(dx)
        dy = float(dy)
        moved = (
            abs(dx - self._pointer_dx) > POINTER_MOVE_EPSILON
            or abs(dy - self._pointer_dy) > POINTER_MOVE_EPSILON
        )
        if present and (moved or not self._pointer_present):
            self._pointer_moved_ms = int(now_ms)
        self._pointer_dx = dx
        self._pointer_dy = dy
        self._pointer_present = bool(present)

    def set_root_motion(self, vx: float, vy: float, dragging: bool) -> None:
        """Report the pet anchor's velocity in logical pixels per second."""
        self._root_velocity = (float(vx), float(vy))
        self.dragging = bool(dragging)

    def apply_impulse(self, spec: Impulse | Mapping[str, Any]) -> bool:
        """Inject velocity. Ignored entirely under reduced motion."""
        if self.reduced_motion:
            return False
        impulse = spec if isinstance(spec, Impulse) else Impulse.from_mapping(spec)
        scale = impulse.scale
        applied = False
        if impulse.chain and impulse.chain_angular_vel:
            solver = self._chain_solvers.get(impulse.chain)
            if solver is not None:
                solver.kick(impulse.chain_angular_vel * scale)
                applied = True
        if impulse.param:
            for kind, velocity in (
                ("angular", impulse.angular_vel),
                ("squash", impulse.squash_vel),
                ("param", impulse.param_vel),
            ):
                if velocity:
                    self._impulse_spring(impulse.param, kind).kick(velocity * scale)
                    applied = True
        return applied

    def _impulse_spring(self, param: str, kind: str) -> Spring1D:
        key = (param, kind)
        spring = self._impulse_springs.get(key)
        if spring is None:
            stiffness, ratio = IMPULSE_SPRINGS[kind]
            spring = Spring1D(stiffness=stiffness, damping_ratio=ratio)
            self._impulse_springs[key] = spring
        return spring

    # -- per-tick evaluation ------------------------------------------------- #

    def advance(self, elapsed_ms: int, now_ms: int) -> dict[str, float]:
        """Evaluate one tick and return the full declared parameter dict.

        The stage order below *is* the design. Defaults come first so every
        later stage is a deviation from rest; oscillators before envelopes so a
        reaction overrides idle breathing rather than the other way round;
        pointer and root motion after expression because they are pose, not
        expression; springs last because they must integrate whatever the
        earlier stages asked for; clamping last of all so nothing can hand the
        solver an out-of-range parameter.
        """
        dt_ms = max(0.0, float(elapsed_ms))
        dt_s = dt_ms / 1000.0
        self._now_ms = int(now_ms)
        self._phase_ms += dt_ms
        self._base_osc.tick(dt_ms)
        self._pulse_osc.tick(dt_ms)

        # 1. defaults
        bus: dict[str, float] = self.model.default_params()
        scalars: dict[str, float] = {}

        # 2/3. base then pulse oscillators, both crossfaded, both silenced by
        # reduced motion through the amplitude scale rather than by skipping the
        # stage -- so the phase keeps running and re-enabling motion is smooth.
        amplitude = 0.0 if self.reduced_motion else 1.0
        for fader in (self._base_osc, self._pulse_osc):
            for name, value in fader.contributions(self._phase_ms, amplitude).items():
                if name in self.model.params:
                    bus[name] = bus.get(name, 0.0) + value
                else:
                    scalars[name] = scalars.get(name, 0.0) + value

        # 4. reaction envelopes
        self._apply_envelopes(bus, now_ms)

        # 5/6. targets for the springs integrated in stage 7
        self._update_pointer_targets(now_ms)
        self._update_root_targets(dt_s)

        # 7. spring and chain integration
        self._integrate(bus, scalars, dt_s)

        # 8. clamp
        params = {
            name: spec.clamp(bus.get(name, spec.default))
            for name, spec in self.model.params.items()
        }
        self.last_params = params
        self.last_scalars = scalars
        return params

    def _apply_envelopes(self, bus: dict[str, float], now_ms: int) -> None:
        self._envelopes = [
            envelope for envelope in self._envelopes if not envelope.finished(now_ms)
        ]
        for envelope in self._envelopes:
            clip = self.clips.get(envelope.clip)
            if clip is None:
                continue
            weight = envelope.weight(now_ms)
            if weight <= 0.0:
                continue
            t_ms = float(now_ms) - envelope.started_ms
            for track in clip.tracks:
                spec: ParamSpec | None = self.model.params.get(track.param)
                if spec is None:
                    continue
                sampled = track.sample(t_ms)
                if track.blend == "override":
                    current = bus.get(track.param, spec.default)
                    bus[track.param] = current * (1.0 - weight) + sampled * weight
                else:
                    bus[track.param] = bus.get(track.param, spec.default) + sampled * weight

    def _update_pointer_targets(self, now_ms: int) -> None:
        fx = 0.0
        fy = 0.0
        if self._pointer_present and not self.reduced_motion:
            magnitude = math.hypot(self._pointer_dx, self._pointer_dy)
            if magnitude >= POINTER_DEAD_ZONE:
                ramp = smoothstep(
                    (magnitude - POINTER_DEAD_ZONE) / (1.0 - POINTER_DEAD_ZONE)
                )
                idle_ms = float(now_ms) - self._pointer_moved_ms
                attention = 1.0 - smoothstep(
                    (idle_ms - POINTER_IDLE_MS) / POINTER_RELEASE_MS
                )
                gain = ramp * attention * self.activity_gain
                fx = self._pointer_dx / magnitude * gain
                fy = self._pointer_dy / magnitude * gain
        self._pointer_factor = (fx, fy)
        self._pointer_targets[PARAM_HEAD_YAW] = HEAD_YAW_DEG * fx
        self._pointer_targets[PARAM_HEAD_PITCH] = HEAD_PITCH_DEG * fy
        self._pointer_targets[PARAM_HEAD_ROLL] = HEAD_ROLL_DEG * fx
        self._pointer_targets[PARAM_EYE_X] = EYE_GAIN_X * fx
        self._pointer_targets[PARAM_EYE_Y] = EYE_GAIN_Y * fy

    def _update_root_targets(self, dt_s: float) -> None:
        vx, vy = self._root_velocity
        if not self.dragging and dt_s > 0.0:
            decay = math.exp(-dt_s / ROOT_RELEASE_TAU_S)
            vx *= decay
            vy *= decay
            self._root_velocity = (vx, vy)
        if self.reduced_motion:
            self._root_accel = (0.0, 0.0)
            self._root_targets[PARAM_ROOT_LEAN] = 0.0
            self._root_targets[PARAM_ROOT_BOB] = 0.0
            self._previous_root_velocity = self._root_velocity
            return
        pvx, pvy = self._previous_root_velocity
        if dt_s > 0.0:
            ax = (vx - pvx) / dt_s
            ay = (vy - pvy) / dt_s
        else:
            ax = ay = 0.0
        self._previous_root_velocity = (vx, vy)
        self._root_accel = (
            _clamp(ax * ROOT_ACCEL_GAIN, -ROOT_ACCEL_MAX, ROOT_ACCEL_MAX),
            _clamp(ay * ROOT_ACCEL_GAIN, -ROOT_ACCEL_MAX, ROOT_ACCEL_MAX),
        )
        self._root_targets[PARAM_ROOT_LEAN] = _clamp(
            -vx * ROOT_LEAN_PER_VELOCITY, -ROOT_LEAN_MAX_DEG, ROOT_LEAN_MAX_DEG
        )
        self._root_targets[PARAM_ROOT_BOB] = _clamp(
            vy * ROOT_BOB_PER_VELOCITY, -ROOT_BOB_MAX, ROOT_BOB_MAX
        )

    def _integrate(
        self, bus: dict[str, float], scalars: dict[str, float], dt_s: float
    ) -> None:
        reduced = self.reduced_motion

        for name, spring in self._pointer_springs.items():
            target = self._pointer_targets[name]
            if reduced:
                spring.snap(target)
            else:
                spring.step(target, dt_s)
        for name, spring in self._root_springs.items():
            target = self._root_targets[name]
            if reduced:
                spring.snap(target)
            else:
                spring.step(target, dt_s)
        for (param, _kind), spring in self._impulse_springs.items():
            if reduced:
                spring.snap(0.0)
            else:
                spring.step(0.0, dt_s)

        head_yaw = self._pointer_springs[PARAM_HEAD_YAW].value
        head_roll = self._pointer_springs[PARAM_HEAD_ROLL].value
        contributions: dict[str, float] = {
            name: spring.value for name, spring in self._pointer_springs.items()
        }
        # The body counter-rotation is derived from the head spring rather than
        # given its own spring, so the two can never drift out of phase and
        # shear the neck seam apart mid-turn.
        contributions[PARAM_BODY_YAW] = head_yaw * BODY_COUNTER_GAIN
        contributions[PARAM_BODY_ROLL] = head_roll * BODY_COUNTER_GAIN
        for name, spring in self._root_springs.items():
            contributions[name] = contributions.get(name, 0.0) + spring.value
        for (param, _kind), spring in self._impulse_springs.items():
            contributions[param] = contributions.get(param, 0.0) + spring.value

        for name, value in contributions.items():
            spec = self.model.params.get(name)
            if spec is None or value == 0.0:
                continue
            bus[name] = bus.get(name, spec.default) + value

        for name, spec in self.model.chains.items():
            solver = self._chain_solvers[name]
            if reduced:
                solver.snap_to_rest()
                angles = solver.angles
            else:
                driver = scalars.get(spec.driver, bus.get(spec.driver, 0.0))
                angles = solver.step(driver, dt_s, self._root_accel)
            for index, param in enumerate(spec.segment_params):
                if index >= len(angles):
                    break
                param_spec = self.model.params.get(param)
                if param_spec is None:
                    continue
                bus[param] = bus.get(param, param_spec.default) + angles[index]
