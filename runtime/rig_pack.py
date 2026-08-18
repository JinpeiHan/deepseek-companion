"""Loading, schema validation, and AnimationModel bridging for rig packs.

Qt-free by construction: this module only reads JSON and rearranges dicts, so
it can be unit tested without a display server and shipped inside a PyInstaller
bundle with no extra dependencies.

The load-bearing idea is :func:`animation_manifest_from_rig`. A rig pack has no
frame sequences at all, yet everything the pet's state machine already does --
state to clip resolution, working-activity mapping, pulse TTL, one-shot overlay
auto-clear, idle-micro gating -- is exactly what a rig pack needs too. Rather
than teach :mod:`runtime.animation_model` about rigs (and re-test all of it),
this synthesises a manifest whose frames are inert ``@rig/<clip>`` tokens. A rig
pack never indexes ``self.pixmaps``, so the tokens are never dereferenced; they
exist only to give :class:`~runtime.animation_model.AnimationModel` something to
count time with.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # package import when bundled, flat import when helper.py runs as a script
    from .rig_model import RigModel, RigValidationError
except ImportError:  # pragma: no cover - exercised by the frozen helper
    from rig_model import RigModel, RigValidationError

#: Prefix of the synthetic, never-dereferenced frame tokens.
RIG_FRAME_PREFIX = "@rig/"

#: Frame granularity of the synthetic manifest. 40 ms is the reduced-motion
#: ticker period, the coarsest clock the helper ever runs at, so a synthesised
#: clip can never expire between two ticks of any supported tick rate.
SYNTHETIC_FRAME_MS = 40

#: Per-frame cost scales linearly with the part count, and an artist adding
#: "just a few more layers" is the most likely way this pet quietly doubles its
#: paint time. The cap is asserted in tests rather than left to review.
MAX_PARTS = 20

TRACK_BLENDS = frozenset({"add", "override"})
TRACK_INTERPS = frozenset({"linear", "step", "smooth"})
OSCILLATOR_WAVES = frozenset({"sin", "cos", "triangle", "pulse"})

_REQUIRED_STATES = ("IDLE",)


# --------------------------------------------------------------------------- #
# Frame tokens
# --------------------------------------------------------------------------- #


def rig_frame_token(clip_name: str) -> str:
    return f"{RIG_FRAME_PREFIX}{clip_name}"


def clip_name_from_frame(frame: str) -> str | None:
    """Recover the clip behind a synthetic token, or ``None`` for real frames."""
    if isinstance(frame, str) and frame.startswith(RIG_FRAME_PREFIX):
        return frame[len(RIG_FRAME_PREFIX) :]
    return None


# --------------------------------------------------------------------------- #
# Path confinement
# --------------------------------------------------------------------------- #


def _lexical_path_error(relative: Any) -> str | None:
    """Reject a part path without touching the filesystem.

    Doing this lexically matters: validation has to work on a rig dict that was
    never loaded from disk (build-time linting, unit tests), and a symlink race
    cannot influence a decision made purely on the string.
    """
    if not isinstance(relative, str) or not relative:
        return "part file must be a non-empty string"
    if "\\" in relative:
        return "part file must use forward slashes"
    path = Path(relative)
    if path.is_absolute() or path.drive or relative.startswith("/"):
        return "part file must be relative"
    if any(segment == ".." for segment in path.parts):
        return "part file must not escape the pack root"
    return None


def resolve_part_path(asset_root: Path, relative: str) -> Path:
    """Resolve a ``parts[].file`` under *asset_root*, refusing to escape it."""
    error = _lexical_path_error(relative)
    if error is not None:
        raise ValueError(f"{error}: {relative!r}")
    root = Path(asset_root).resolve()
    resolved = (root / relative).resolve()
    if root not in resolved.parents:
        raise ValueError(f"part path escapes pack root: {relative!r}")
    return resolved


def rig_part_paths(rig: Mapping[str, Any], asset_root: Path) -> dict[str, Path]:
    """Map every part id to its confined on-disk path."""
    paths: dict[str, Path] = {}
    for entry in _parts_entries(rig):
        part_id = str(entry.get("id", ""))
        paths[part_id] = resolve_part_path(asset_root, entry.get("file"))
    return paths


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


def _parts_entries(rig: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = rig.get("parts", ())
    if isinstance(raw, Mapping):
        out = []
        for key, value in raw.items():
            entry = dict(value) if isinstance(value, Mapping) else {}
            entry.setdefault("id", key)
            out.append(entry)
        return out
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        return [dict(entry) for entry in raw if isinstance(entry, Mapping)]
    return []


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return []
    return [str(item) for item in value]


def schema_errors(rig: Mapping[str, Any]) -> list[str]:
    """Every rig-pack problem the solver itself does not already report.

    Returns a list rather than raising so an artist mid-edit sees the whole
    batch at once; :func:`validate_rig` is the raising wrapper.
    """
    model = RigModel(rig)
    errors = list(model.validation_errors())
    clips = _as_mapping(rig.get("clips"))
    params = model.params
    parts = model.parts

    if not clips:
        errors.append("rig declares no clips")

    part_entries = _parts_entries(rig)
    if len(part_entries) > MAX_PARTS:
        errors.append(
            f"rig declares {len(part_entries)} parts, more than the {MAX_PARTS} allowed"
        )
    for entry in part_entries:
        part_id = str(entry.get("id", ""))
        error = _lexical_path_error(entry.get("file"))
        if error is not None:
            errors.append(f"part {part_id!r}: {error}")

    # -- clips ------------------------------------------------------------- #
    oscillator_targets: set[str] = set()
    for name, raw_clip in clips.items():
        clip = _as_mapping(raw_clip)
        loop = bool(clip.get("loop", True))
        duration = float(clip.get("durationMs", 0.0) or 0.0)
        if not loop and duration <= 0.0:
            errors.append(f"clip {name!r} is one-shot but has no positive durationMs")
        envelope = _as_mapping(clip.get("envelope"))
        for key in ("attackMs", "holdMs", "decayMs"):
            if key in envelope and float(envelope[key]) < 0.0:
                errors.append(f"clip {name!r} envelope {key} is negative")
        for index, raw_osc in enumerate(clip.get("oscillators") or ()):
            osc = _as_mapping(raw_osc)
            target = str(osc.get("param", ""))
            label = f"clip {name!r} oscillator {index}"
            if not target:
                errors.append(f"{label} names no param")
            else:
                oscillator_targets.add(target)
            wave = str(osc.get("wave", "sin"))
            if wave not in OSCILLATOR_WAVES:
                errors.append(f"{label} uses unknown wave {wave!r}")
            if float(osc.get("periodMs", 0.0) or 0.0) <= 0.0:
                errors.append(f"{label} needs a positive periodMs")
        for index, raw_track in enumerate(clip.get("tracks") or ()):
            track = _as_mapping(raw_track)
            label = f"clip {name!r} track {index}"
            param = str(track.get("param", ""))
            if param not in params:
                errors.append(f"{label} drives undeclared param {param!r}")
            blend = str(track.get("blend", "add"))
            if blend not in TRACK_BLENDS:
                errors.append(f"{label} uses unknown blend {blend!r}")
            interp = str(track.get("interp", "linear"))
            if interp not in TRACK_INTERPS:
                errors.append(f"{label} uses unknown interp {interp!r}")
            keys = track.get("keys") or ()
            pairs = [
                key
                for key in keys
                if isinstance(key, Sequence) and not isinstance(key, (str, bytes))
                and len(key) >= 2
            ]
            if not pairs:
                errors.append(f"{label} has no keys")
            elif [float(key[0]) for key in pairs] != sorted(float(key[0]) for key in pairs):
                errors.append(f"{label} keys are not sorted by time")

    # An oscillator may write a driver-side scalar that is not a declared param
    # (chain drivers work exactly that way), so the reverse check is the useful
    # one: a chain whose driver nothing produces can never move.
    for chain in model.chains.values():
        if not chain.driver:
            errors.append(f"chain {chain.name!r} names no driver")
        elif chain.driver not in params and chain.driver not in oscillator_targets:
            errors.append(
                f"chain {chain.name!r} driver {chain.driver!r} is neither a declared "
                "param nor produced by any clip oscillator"
            )

    # -- clip references ---------------------------------------------------- #
    state_map = _as_mapping(rig.get("stateMap"))
    if not state_map:
        errors.append("rig declares no stateMap")
    for state in _REQUIRED_STATES:
        if state not in state_map:
            errors.append(f"stateMap is missing required state {state!r}")
    for state, clip_name in state_map.items():
        if clip_name not in clips:
            errors.append(f"stateMap[{state!r}] names unknown clip {clip_name!r}")
    for activity, clip_name in _as_mapping(rig.get("workingActivityMap")).items():
        if clip_name not in clips:
            errors.append(
                f"workingActivityMap[{activity!r}] names unknown clip {clip_name!r}"
            )
    for clip_name in _string_list(rig.get("idleMicroClips")):
        if clip_name not in clips:
            errors.append(f"idleMicroClips names unknown clip {clip_name!r}")

    # -- hit groups and interactions ---------------------------------------- #
    hit_groups = _as_mapping(rig.get("hitGroups"))
    for group, members in hit_groups.items():
        listed = _string_list(members)
        if not listed:
            errors.append(f"hitGroup {group!r} has no parts")
        for part_id in listed:
            if part_id not in parts:
                errors.append(f"hitGroup {group!r} names unknown part {part_id!r}")

    for group, raw_entry in _as_mapping(rig.get("interactions")).items():
        entry = _as_mapping(raw_entry)
        if hit_groups and group not in hit_groups:
            errors.append(f"interaction {group!r} is not a declared hitGroup")
        clip_name = entry.get("clip")
        if clip_name is not None and clip_name not in clips:
            errors.append(f"interaction {group!r} names unknown clip {clip_name!r}")
        impulse = _as_mapping(entry.get("impulse"))
        chain = impulse.get("chain")
        if chain is not None and chain not in model.chains:
            errors.append(f"interaction {group!r} impulse names unknown chain {chain!r}")
        param = impulse.get("param")
        if param is not None and param not in params:
            errors.append(f"interaction {group!r} impulse names undeclared param {param!r}")

    return errors


def validate_rig(rig: Mapping[str, Any]) -> RigModel:
    """Raise :class:`RigValidationError` listing every problem in *rig*."""
    errors = schema_errors(rig)
    if errors:
        raise RigValidationError(errors)
    return RigModel(rig)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_rig(descriptor: Any) -> dict[str, Any]:
    """Read and validate the rig behind a ``renderer == "rig"`` pack descriptor.

    Validation happens at load time on purpose: a structurally broken rig must
    fail while the caller can still fall back to another pack, not later inside
    a paint handler where the only outcome is a crashed pet.
    """
    renderer = getattr(descriptor, "renderer", "frames")
    if renderer != "rig":
        raise ValueError(f"pack {getattr(descriptor, 'pack_id', '?')!r} is not a rig pack")
    rig_path = getattr(descriptor, "rig_path", None)
    if rig_path is None:
        raise ValueError("rig pack descriptor carries no rig_path")
    rig = json.loads(Path(rig_path).read_text(encoding="utf-8"))
    if not isinstance(rig, Mapping):
        raise ValueError(f"rig at {rig_path} is not an object")
    rig = dict(rig)
    validate_rig(rig)
    # Confirm every part resolves inside the pack before anything is painted.
    rig_part_paths(rig, getattr(descriptor, "asset_root", Path(rig_path).parent))
    return rig


# --------------------------------------------------------------------------- #
# AnimationModel bridge
# --------------------------------------------------------------------------- #


def clip_playback_ms(clip: Mapping[str, Any]) -> float:
    """Wall-clock length a one-shot clip should occupy in the state machine.

    The decay tail counts: the pose is still visibly returning to rest during
    it, so clearing the overlay at ``durationMs`` would hand the pet back to its
    base clip while the reaction is still on screen.
    """
    envelope = _as_mapping(clip.get("envelope"))
    return float(clip.get("durationMs", 0.0) or 0.0) + float(
        envelope.get("decayMs", 0.0) or 0.0
    )


def synthetic_frame_count(clip: Mapping[str, Any]) -> int:
    """Number of inert frames a one-shot clip needs to time out correctly.

    Never fewer than two: :meth:`AnimationModel.advance` returns early for
    single-frame clips, so a one-frame one-shot overlay would stick forever.
    """
    total = clip_playback_ms(clip)
    return max(2, math.ceil(total / SYNTHETIC_FRAME_MS))


def animation_manifest_from_rig(rig: Mapping[str, Any]) -> dict[str, Any]:
    """Synthesise an :class:`AnimationModel`-compatible manifest for a rig.

    Looping clips collapse to a single token (a loop's timing lives entirely in
    the driver's oscillator phase, so the state machine has nothing to count),
    while one-shot clips get a ladder of identical tokens whose length encodes
    the reaction duration at :data:`SYNTHETIC_FRAME_MS` granularity.
    """
    clips: dict[str, Any] = {}
    for name, raw_clip in _as_mapping(rig.get("clips")).items():
        clip = _as_mapping(raw_clip)
        loop = bool(clip.get("loop", True))
        token = rig_frame_token(name)
        frames = [token] if loop else [token] * synthetic_frame_count(clip)
        entry: dict[str, Any] = {
            "frames": frames,
            "frameMs": SYNTHETIC_FRAME_MS,
            "loop": loop,
        }
        motion = clip.get("motion")
        if motion is not None:
            entry["motion"] = motion
        clips[name] = entry

    manifest: dict[str, Any] = {
        "clips": clips,
        "stateMap": dict(_as_mapping(rig.get("stateMap"))),
    }
    working = _as_mapping(rig.get("workingActivityMap"))
    if working:
        manifest["workingActivityMap"] = working
    idle_micro = _string_list(rig.get("idleMicroClips"))
    if idle_micro:
        manifest["idleMicroClips"] = idle_micro
    return manifest
