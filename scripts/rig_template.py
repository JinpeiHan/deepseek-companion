"""The pack-independent half of a rig: ids, skeleton topology, params, clips.

Every rig pack ships the *same* part ids, bone ids, chain ids, param names and
clip names. Only geometry -- pivots, rects, masks, pixels -- differs between
proportions. Keeping the invariant half here, as one Python literal shared by
every pack, is what makes the validator's "standard and chibi expose identical
ids" assertion true by construction instead of by review.

The per-pack half lives in ``art-references/rig/<pack>/annotations.json``:
shapes to cut with, bone pivots, part pivots. Nothing there can introduce or
drop an id.
"""

from __future__ import annotations

#: Parts in paint order (z ascending). ``claim`` is the segmentation priority:
#: higher claims master pixels first, which is *not* the same as z (the tail
#: paints behind the body yet must claim its own pixels before the body's
#: generous silhouette polygon can swallow them).
#:
#: ``synthetic`` parts own no master pixel at all -- the eyelids only exist
#: while the eye is closed, a pose the master never shows -- so their entire
#: content comes from the fill stage.
PARTS: tuple[dict, ...] = (
    {"id": "tail_0", "z": 5, "bone": "tail_b0", "claim": 73, "hitGroup": "tail"},
    {"id": "tail_1", "z": 6, "bone": "tail_b1", "claim": 74, "hitGroup": "tail"},
    {"id": "tail_2", "z": 7, "bone": "tail_b2", "claim": 75, "hitGroup": "tail"},
    {
        "id": "tail_3",
        "z": 8,
        "bone": "tail_b3",
        "claim": 76,
        "hitGroup": "tail",
        "strips": 6,
        "stripBones": ["tail_b2", "tail_b3"],
    },
    {"id": "hair_back", "z": 10, "bone": "head", "claim": 20, "hitGroup": "hair"},
    {"id": "neck", "z": 18, "bone": "neck", "claim": 82, "hitGroup": "body"},
    {"id": "body", "z": 20, "bone": "body", "claim": 60, "hitGroup": "body"},
    {"id": "apron", "z": 24, "bone": "body", "claim": 65, "hitGroup": "body"},
    {"id": "arm_l", "z": 30, "bone": "arm_l", "claim": 70, "hitGroup": "arm"},
    {"id": "arm_r", "z": 31, "bone": "arm_r", "claim": 70, "hitGroup": "arm"},
    {"id": "head", "z": 40, "bone": "head", "claim": 80, "hitGroup": "head"},
    {"id": "cheek_l", "z": 42, "bone": "head", "claim": 86, "hitGroup": "cheek"},
    {"id": "cheek_r", "z": 43, "bone": "head", "claim": 86, "hitGroup": "cheek"},
    {"id": "eye_l", "z": 44, "bone": "head", "claim": 88, "hitGroup": "head"},
    {"id": "eye_r", "z": 45, "bone": "head", "claim": 88, "hitGroup": "head"},
    {"id": "eye_l_lid", "z": 46, "bone": "head", "claim": 0, "synthetic": True,
     "hitGroup": "head"},
    {"id": "eye_r_lid", "z": 47, "bone": "head", "claim": 0, "synthetic": True,
     "hitGroup": "head"},
    {"id": "mouth", "z": 48, "bone": "head", "claim": 90, "hitGroup": "head"},
    {"id": "hair_front", "z": 50, "bone": "head", "claim": 95, "hitGroup": "hair"},
    {"id": "headdress", "z": 55, "bone": "head", "claim": 100, "hitGroup": "head"},
)

PART_IDS: tuple[str, ...] = tuple(part["id"] for part in PARTS)

#: Bone topology. Pivots come from the pack annotations; the parent chain does
#: not, because a proportion change must never re-parent a skeleton.
BONES: tuple[dict, ...] = (
    {"id": "root", "parent": None},
    {"id": "body", "parent": "root"},
    {"id": "neck", "parent": "body"},
    {"id": "head", "parent": "neck"},
    {"id": "arm_l", "parent": "body"},
    {"id": "arm_r", "parent": "body"},
    {"id": "tail_b0", "parent": "body", "chain": "tail", "chainIndex": 0},
    {"id": "tail_b1", "parent": "tail_b0", "chain": "tail", "chainIndex": 1},
    {"id": "tail_b2", "parent": "tail_b1", "chain": "tail", "chainIndex": 2},
    {"id": "tail_b3", "parent": "tail_b2", "chain": "tail", "chainIndex": 3},
)

BONE_IDS: tuple[str, ...] = tuple(bone["id"] for bone in BONES)

CHAINS: dict[str, dict] = {
    "tail": {
        "driver": "tailSwing",
        "amplitudeDeg": 11.0,
        "distribution": [1.0, 0.85, 0.7, 0.55],
        "spring": {
            "stiffness": 84.0,
            "dampingRatio": 0.42,
            "lagPerSegmentMs": 26.0,
            "maxDeg": 10.0,
        },
        "deform": "strips",
        "bones": ["tail_b0", "tail_b1", "tail_b2", "tail_b3"],
        "segmentParams": ["tail0", "tail1", "tail2", "tail3"],
    }
}

#: Parameter space. Ranges are the contract the anchor guard sweeps, so they are
#: deliberately tight: ``headAngleZ`` at +-18 deg is the spec, and widening it
#: later must be a decision that re-runs ``sweep_bbox`` rather than a silent
#: edit to a JSON file nobody diffs.
PARAMS: dict[str, dict] = {
    "headAngleX": {"min": -14, "max": 14, "default": 0},
    "headAngleY": {"min": -18, "max": 18, "default": 0},
    "headAngleZ": {"min": -18, "max": 18, "default": 0},
    "bodyAngleY": {"min": -4, "max": 4, "default": 0},
    "bodyAngleZ": {"min": -4, "max": 4, "default": 0},
    "eyeBallX": {"min": -1, "max": 1, "default": 0},
    "eyeBallY": {"min": -1, "max": 1, "default": 0},
    "rootLeanZ": {"min": -5, "max": 5, "default": 0},
    "rootBobY": {"min": -6, "max": 6, "default": 0},
    "breath": {"min": -1, "max": 1, "default": 0},
    "eyeOpen": {"min": 0, "max": 1, "default": 1},
    "mouthOpen": {"min": 0, "max": 1, "default": 0},
    "cheekSquash": {"min": 0, "max": 1, "default": 0},
    "armSwingL": {"min": -14, "max": 14, "default": 0},
    "armSwingR": {"min": -14, "max": 14, "default": 0},
    "hairSway": {"min": -6, "max": 6, "default": 0},
    "tail0": {"min": -10, "max": 10, "default": 0},
    "tail1": {"min": -10, "max": 10, "default": 0},
    "tail2": {"min": -10, "max": 10, "default": 0},
    "tail3": {"min": -10, "max": 10, "default": 0},
}

#: Every binding is neutral at its parameter default -- additive channels
#: evaluate to 0, multiplicative ones to 1 -- with exactly one deliberate
#: exception: ``eyeOpen`` collapses the eyelids to zero height when the eye is
#: open, which is how a lid that only exists closed stays invisible at rest.
#: The validator knows about that exception by name (``qa.restHiddenParts``).
BINDINGS: tuple[dict, ...] = (
    {"param": "headAngleZ", "bone": "head", "channel": "rotate", "gain": 1.0},
    {"param": "headAngleY", "bone": "head", "channel": "rotate", "gain": 0.16},
    {"param": "headAngleY", "bone": "head", "channel": "translateX", "gain": 0.22},
    {"param": "headAngleY", "bone": "body", "channel": "rotate", "gain": -0.05},
    {"param": "headAngleX", "bone": "head", "channel": "translateY", "gain": 0.30},
    {"param": "bodyAngleY", "bone": "body", "channel": "rotate", "gain": 0.5},
    {"param": "bodyAngleZ", "bone": "body", "channel": "rotate", "gain": 1.0},
    {"param": "rootLeanZ", "bone": "root", "channel": "rotate", "gain": 1.0},
    {"param": "rootBobY", "bone": "root", "channel": "translateY", "gain": 1.0},
    {"param": "breath", "bone": "body", "channel": "scaleY", "gain": 0.012, "bias": 1.0},
    {"param": "breath", "bone": "head", "channel": "translateY", "gain": 0.8},
    {"param": "hairSway", "part": "hair_back", "channel": "rotate", "gain": 1.0},
    {"param": "armSwingL", "bone": "arm_l", "channel": "rotate", "gain": 1.0},
    {"param": "armSwingR", "bone": "arm_r", "channel": "rotate", "gain": 1.0},
    {"param": "eyeBallX", "part": "eye_l", "channel": "translateX", "gain": 2.2},
    {"param": "eyeBallX", "part": "eye_r", "channel": "translateX", "gain": 2.2},
    {"param": "eyeBallY", "part": "eye_l", "channel": "translateY", "gain": 1.6},
    {"param": "eyeBallY", "part": "eye_r", "channel": "translateY", "gain": 1.6},
    {"param": "eyeOpen", "part": "eye_l_lid", "channel": "scaleY",
     "curve": [[0.0, 1.0], [1.0, 0.0]]},
    {"param": "eyeOpen", "part": "eye_r_lid", "channel": "scaleY",
     "curve": [[0.0, 1.0], [1.0, 0.0]]},
    {"param": "mouthOpen", "part": "mouth", "channel": "scaleY",
     "curve": [[0.0, 1.0], [0.5, 1.6], [1.0, 2.3]]},
    {"param": "cheekSquash", "part": "cheek_l", "channel": "scaleX",
     "gain": 0.25, "bias": 1.0},
    {"param": "cheekSquash", "part": "cheek_l", "channel": "scaleY",
     "gain": -0.20, "bias": 1.0},
    {"param": "cheekSquash", "part": "cheek_r", "channel": "scaleX",
     "gain": 0.25, "bias": 1.0},
    {"param": "cheekSquash", "part": "cheek_r", "channel": "scaleY",
     "gain": -0.20, "bias": 1.0},
    {"param": "tail0", "bone": "tail_b0", "channel": "rotate", "gain": 1.0},
    {"param": "tail1", "bone": "tail_b1", "channel": "rotate", "gain": 1.0},
    {"param": "tail2", "bone": "tail_b2", "channel": "rotate", "gain": 1.0},
    {"param": "tail3", "bone": "tail_b3", "channel": "rotate", "gain": 1.0},
)


def _breathe(period: float, amp: float) -> dict:
    return {"param": "breath", "wave": "sin", "periodMs": period, "amplitude": amp}


def _swing(period: float, amp: float) -> dict:
    return {"param": "tailSwing", "wave": "sin", "periodMs": period, "amplitude": amp}


#: Clip names match the v2 frame manifests one for one, so a pack can be swapped
#: between renderers without touching ``stateMap`` consumers.
CLIPS: dict[str, dict] = {
    "idle": {
        "loop": True,
        "motion": "breathe",
        "oscillators": [_breathe(3600, 0.6), _swing(2600, 0.5)],
    },
    "thinking": {
        "loop": True,
        "motion": "think",
        "oscillators": [
            _breathe(2800, 0.5),
            _swing(3000, 0.35),
            {"param": "headAngleZ", "wave": "sin", "periodMs": 5200, "amplitude": 3.5},
        ],
    },
    # Sweeping, not generic "arms move". Both arms travel TOGETHER because they
    # share a broom handle -- the previous version swung them in opposition,
    # which reads as marching. The body counter-rotates against the stroke and
    # the tail trails it, so the whole figure participates instead of only the
    # shoulders. One stroke is 1400ms; every partner of that period is either
    # equal to it or double it, so the cycle never drifts out of phase.
    "working": {
        "loop": True,
        "motion": "work",
        "oscillators": [
            _breathe(2800, 0.5),
            {"param": "armSwingL", "wave": "sin", "periodMs": 1400, "amplitude": 9.0},
            {"param": "armSwingR", "wave": "sin", "periodMs": 1400, "amplitude": 7.5},
            {"param": "bodyAngleZ", "wave": "sin", "periodMs": 1400, "amplitude": -2.6},
            {"param": "rootLeanZ", "wave": "sin", "periodMs": 1400, "amplitude": -1.8},
            {"param": "headAngleZ", "wave": "sin", "periodMs": 1400, "amplitude": -2.2},
            # Eyes track the broom head, which sits low and to the side.
            {"param": "eyeBallX", "wave": "sin", "periodMs": 1400, "amplitude": 0.45},
            {"param": "eyeBallY", "wave": "sin", "periodMs": 2800, "amplitude": 0.22, "bias": 0.30},
            {"param": "headAngleX", "wave": "sin", "periodMs": 2800, "amplitude": 1.2, "bias": 2.4},
            # The tail lags the stroke by half a cycle rather than leading it.
            {"param": "tailSwing", "wave": "sin", "periodMs": 1400, "amplitude": 0.8, "phase": 0.5},
            {"param": "rootBobY", "wave": "sin", "periodMs": 700, "amplitude": 1.1},
        ],
    },
    # Eating a token: a repeating nibble. The mouth and the cheek squash share
    # the 900ms bite period so the squash lands on the closing mouth rather than
    # drifting against it, and the arms stay raised (bias) instead of swinging
    # through rest, because the hands are holding something up to the face.
    "eat_token": {
        "loop": True,
        "motion": "work",
        "oscillators": [
            _breathe(2700, 0.4),
            {"param": "mouthOpen", "wave": "sin", "periodMs": 900, "amplitude": 0.5, "bias": 0.45},
            {"param": "cheekSquash", "wave": "sin", "periodMs": 900, "amplitude": 0.34, "bias": 0.36},
            {"param": "armSwingL", "wave": "sin", "periodMs": 1800, "amplitude": 3.0, "bias": 15.0},
            {"param": "armSwingR", "wave": "sin", "periodMs": 1800, "amplitude": -3.0, "bias": -15.0},
            # Head dips toward the hands and bobs with each bite.
            {"param": "headAngleX", "wave": "sin", "periodMs": 900, "amplitude": 1.8, "bias": 4.0},
            {"param": "headAngleZ", "wave": "sin", "periodMs": 3600, "amplitude": 2.4},
            # Eyes squeeze shut a little on every bite: happy, not sleepy.
            {"param": "eyeOpen", "wave": "sin", "periodMs": 900, "amplitude": -0.22, "bias": 0.74},
            {"param": "eyeBallY", "wave": "sin", "periodMs": 1800, "amplitude": 0.15, "bias": 0.28},
            _swing(1800, 0.9),
            {"param": "rootBobY", "wave": "sin", "periodMs": 900, "amplitude": 1.4},
        ],
    },
    "working_search": {
        "loop": True,
        "motion": "work",
        "oscillators": [
            _breathe(2100, 0.6),
            _swing(2400, 0.5),
            {"param": "headAngleY", "wave": "sin", "periodMs": 2600, "amplitude": 7.0},
        ],
    },
    "working_command": {
        "loop": True,
        "motion": "work",
        "oscillators": [
            _breathe(1700, 0.9),
            _swing(1800, 0.8),
            {"param": "armSwingR", "wave": "triangle", "periodMs": 900, "amplitude": 9.0},
        ],
    },
    "waiting": {
        "loop": True,
        "oscillators": [_breathe(3000, 0.5), _swing(3400, 0.3)],
    },
    "success": {
        "loop": True,
        "oscillators": [
            _breathe(900, 1.0),
            _swing(900, 1.0),
            {"param": "rootBobY", "wave": "sin", "periodMs": 900, "amplitude": 3.0},
        ],
    },
    "error": {
        "loop": True,
        "oscillators": [
            _breathe(2600, 0.4),
            {"param": "headAngleZ", "wave": "sin", "periodMs": 320, "amplitude": 2.5},
        ],
    },
    "error_dizzy": {
        "loop": True,
        "oscillators": [
            _breathe(2200, 0.4),
            {"param": "headAngleZ", "wave": "sin", "periodMs": 1400, "amplitude": 9.0},
            {"param": "headAngleY", "wave": "cos", "periodMs": 1400, "amplitude": 7.0},
        ],
    },
    "dragging": {
        "loop": True,
        "oscillators": [_breathe(2400, 0.5), _swing(1500, 0.9)],
    },
    "blink": {
        "loop": False,
        "durationMs": 240,
        "envelope": {"attackMs": 60, "holdMs": 40, "decayMs": 120},
        "tracks": [
            {
                "param": "eyeOpen",
                "blend": "override",
                "interp": "linear",
                "keys": [[0, 1.0], [90, 0.0], [140, 0.0], [240, 1.0]],
            }
        ],
    },
    "glance": {
        "loop": False,
        "durationMs": 900,
        "envelope": {"attackMs": 160, "holdMs": 260, "decayMs": 320},
        "tracks": [
            {
                "param": "eyeBallX",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [260, 0.7], [620, 0.7], [900, 0.0]],
            },
            {
                "param": "headAngleY",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [300, 4.0], [620, 4.0], [900, 0.0]],
            },
        ],
    },
    "head_pat": {
        "loop": False,
        "durationMs": 520,
        "envelope": {"attackMs": 90, "holdMs": 120, "decayMs": 200},
        "tracks": [
            {
                "param": "headAngleZ",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [180, -5.0], [340, 3.0], [520, 0.0]],
            },
            {
                "param": "eyeOpen",
                "blend": "override",
                "interp": "linear",
                "keys": [[0, 1.0], [160, 0.2], [380, 0.2], [520, 1.0]],
            },
            {
                "param": "mouthOpen",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [200, 0.45], [520, 0.0]],
            },
        ],
    },
    "poke": {
        "loop": False,
        "durationMs": 360,
        "envelope": {"attackMs": 60, "holdMs": 60, "decayMs": 180},
        "tracks": [
            {
                "param": "cheekSquash",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [110, 0.85], [360, 0.0]],
            },
            {
                "param": "mouthOpen",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [110, 0.6], [360, 0.0]],
            },
            {
                "param": "headAngleX",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [110, -3.0], [360, 0.0]],
            },
        ],
    },
    "tail": {
        "loop": False,
        "durationMs": 420,
        "envelope": {"attackMs": 50, "holdMs": 60, "decayMs": 260},
        "tracks": [
            {
                "param": "tail3",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [120, 10.0], [260, -6.0], [420, 0.0]],
            },
            {
                "param": "tail2",
                "blend": "add",
                "interp": "smooth",
                "keys": [[0, 0.0], [140, 6.0], [280, -3.0], [420, 0.0]],
            },
            {
                "param": "eyeOpen",
                "blend": "override",
                "interp": "linear",
                "keys": [[0, 1.0], [120, 0.35], [300, 0.35], [420, 1.0]],
            },
        ],
    },
}

STATE_MAP: dict[str, str] = {
    "IDLE": "idle",
    "THINKING": "thinking",
    "WORKING": "working",
    "WAITING": "waiting",
    "SUCCESS": "success",
    "ERROR": "error",
    "DISCONNECTED": "idle",
}

WORKING_ACTIVITY_MAP: dict[str, str] = {
    "searching": "working_search",
    "commanding": "working_command",
    "editing": "working",
    "testing": "working_command",
    "using-tool": "working",
}

IDLE_MICRO_CLIPS: tuple[str, ...] = ("blink", "glance")

HIT_GROUPS: dict[str, tuple[str, ...]] = {
    "head": ("head", "eye_l", "eye_r", "eye_l_lid", "eye_r_lid", "mouth", "headdress"),
    "hair": ("hair_front", "hair_back"),
    "cheek": ("cheek_l", "cheek_r"),
    "arm": ("arm_l", "arm_r"),
    "body": ("body", "apron", "neck"),
    "tail": ("tail_0", "tail_1", "tail_2", "tail_3"),
}

INTERACTIONS: dict[str, dict] = {
    "head": {
        "clip": "head_pat",
        "copy": "head_pat",
        "impulse": {"param": "headAngleZ", "angularVel": 36.0},
    },
    "hair": {
        "clip": "head_pat",
        "copy": "head_pat",
        "impulse": {"param": "hairSway", "angularVel": 44.0},
    },
    "cheek": {
        "clip": "poke",
        "copy": "poke",
        "impulse": {"param": "cheekSquash", "squashVel": 3.0},
    },
    "arm": {
        "clip": "poke",
        "copy": "poke",
        "impulse": {"param": "armSwingL", "angularVel": 30.0},
    },
    "body": {
        "clip": "poke",
        "copy": "poke",
        "impulse": {"param": "bodyAngleZ", "angularVel": 22.0},
    },
    "tail": {
        "clip": "tail",
        "copy": "tail",
        "impulse": {"chain": "tail", "chainAngularVel": 180.0},
    },
}

#: Parts the renderer legitimately draws nothing for at default parameters.
#: The rest-pose recomposite check in ``validate-rig.mjs`` must skip exactly
#: these, and naming them in the manifest keeps that agreement checkable.
REST_HIDDEN_PARTS: tuple[str, ...] = ("eye_l_lid", "eye_r_lid")

LOGICAL_WIDTH = 260
LOGICAL_HEIGHT = 260
FOOT_ANCHOR = (0.5, 0.97)
BUBBLE_ANCHOR = (0.5, 0.04)
CANVAS = 512
