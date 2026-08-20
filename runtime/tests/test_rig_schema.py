"""Schema validation for rig packs.

The fixture extends the Phase B solver fixture with everything Phase C adds --
clips, state maps, hit groups, interactions -- so there is exactly one synthetic
rig in the test suite and a change to the part list cannot silently desync the
solver tests from the schema tests. Phase D owns the real
``assets/pet-<pack>-rig.json``; nothing here touches shipped assets.

Every test here breaks the fixture on purpose. The point of the module is that
a rig which is wrong in a way an artist can plausibly get wrong fails at load
time with a readable message, rather than at paint time with a KeyError.
"""

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from runtime.rig_model import RigValidationError
from runtime.rig_pack import (
    MAX_PARTS,
    animation_manifest_from_rig,
    load_rig,
    resolve_part_path,
    schema_errors,
    validate_rig,
)
from runtime.tests.test_rig_model import sample_rig as solver_rig


def sample_rig() -> dict:
    """The Phase B rig plus the clip/interaction layer Phase C drives."""
    rig = solver_rig()
    rig["params"].update(
        {
            "headAngleX": {"min": -14, "max": 14, "default": 0},
            "bodyAngleY": {"min": -8, "max": 8, "default": 0},
            "bodyAngleZ": {"min": -8, "max": 8, "default": 0},
            "eyeBallX": {"min": -1, "max": 1, "default": 0},
            "eyeBallY": {"min": -1, "max": 1, "default": 0},
            "rootLeanZ": {"min": -12, "max": 12, "default": 0},
            "rootBobY": {"min": -8, "max": 8, "default": 0},
        }
    )
    rig["bindings"].extend(
        [
            {"param": "headAngleX", "bone": "head", "channel": "translateY", "gain": 0.4},
            {"param": "bodyAngleY", "bone": "body", "channel": "rotate", "gain": 1.0},
            {"param": "bodyAngleZ", "bone": "body", "channel": "rotate", "gain": 1.0},
            {"param": "eyeBallX", "part": "eye_l", "channel": "translateX", "gain": 2.0},
            {"param": "eyeBallX", "part": "eye_r", "channel": "translateX", "gain": 2.0},
            {"param": "eyeBallY", "part": "eye_l", "channel": "translateY", "gain": 1.5},
            {"param": "eyeBallY", "part": "eye_r", "channel": "translateY", "gain": 1.5},
            {"param": "rootLeanZ", "bone": "root", "channel": "rotate", "gain": 1.0},
            {"param": "rootBobY", "bone": "root", "channel": "translateY", "gain": 1.0},
        ]
    )
    rig["clips"] = {
        "idle": {
            "loop": True,
            "motion": "breathe",
            "oscillators": [
                {"param": "breath", "wave": "sin", "periodMs": 3600, "amplitude": 0.6},
                {"param": "tailSwing", "wave": "sin", "periodMs": 2600, "amplitude": 0.5},
            ],
        },
        "thinking": {
            "loop": True,
            "motion": "think",
            "oscillators": [
                {"param": "breath", "wave": "sin", "periodMs": 2400, "amplitude": 0.4},
                {"param": "tailSwing", "wave": "sin", "periodMs": 3000, "amplitude": 0.3},
            ],
        },
        "working": {
            "loop": True,
            "motion": "work",
            "oscillators": [
                {"param": "breath", "wave": "sin", "periodMs": 1800, "amplitude": 0.8},
                {"param": "tailSwing", "wave": "sin", "periodMs": 2000, "amplitude": 0.7},
                # The pet narrows its eyes in concentration while working. The
                # bias is what makes the anti-pop test in test_rig_driver
                # non-vacuous: at every phase, idle and working disagree about
                # eyeOpen by 0.70-0.90, far more than the 0.15 that test allows
                # between two consecutive ticks.
                {
                    "param": "eyeOpen",
                    "wave": "sin",
                    "periodMs": 1700,
                    "amplitude": 0.10,
                    "bias": -0.80,
                },
            ],
        },
        "waiting": {
            "loop": True,
            "oscillators": [
                {"param": "breath", "wave": "sin", "periodMs": 3000, "amplitude": 0.5}
            ],
        },
        "success": {
            "loop": True,
            "oscillators": [
                {"param": "breath", "wave": "sin", "periodMs": 900, "amplitude": 1.0}
            ],
        },
        "error": {
            "loop": True,
            "oscillators": [
                {"param": "headAngleZ", "wave": "sin", "periodMs": 300, "amplitude": 3.0}
            ],
        },
        "dragging": {"loop": True},
        # A deliberately slow, sleepy blink. A real 100 ms blink closes the eye
        # faster than the 0.15-per-16ms-tick ceiling the anti-pop test asserts,
        # which would make that test measure eyelid speed instead of the thing
        # it is for: discontinuities introduced by switching clips.
        "blink": {
            "loop": False,
            "durationMs": 560,
            "envelope": {"attackMs": 140, "decayMs": 120},
            "tracks": [
                {
                    "param": "eyeOpen",
                    "blend": "override",
                    "interp": "linear",
                    "keys": [[0, 1.0], [260, 0.05], [320, 0.05], [560, 1.0]],
                }
            ],
        },
        "poke": {
            "loop": False,
            "durationMs": 320,
            "envelope": {"attackMs": 60, "decayMs": 140},
            "tracks": [
                {
                    "param": "mouthOpen",
                    "blend": "add",
                    "interp": "smooth",
                    "keys": [[0, 0.0], [100, 0.6], [320, 0.0]],
                }
            ],
        },
        "head_pat": {
            "loop": False,
            "durationMs": 400,
            "envelope": {"attackMs": 80, "decayMs": 160},
            "tracks": [
                {
                    "param": "headAngleZ",
                    "blend": "add",
                    "interp": "smooth",
                    "keys": [[0, 0.0], [200, -6.0], [400, 0.0]],
                },
                {
                    "param": "eyeOpen",
                    "blend": "override",
                    "interp": "linear",
                    "keys": [[0, 1.0], [150, 0.25], [400, 1.0]],
                },
            ],
        },
    }
    rig["stateMap"] = {
        "IDLE": "idle",
        "THINKING": "thinking",
        "WORKING": "working",
        "WAITING": "waiting",
        "SUCCESS": "success",
        "ERROR": "error",
        "DISCONNECTED": "idle",
    }
    rig["workingActivityMap"] = {
        "searching": "working",
        "commanding": "working",
        "editing": "working",
        "testing": "working",
        "using-tool": "working",
    }
    rig["idleMicroClips"] = ["blink"]
    rig["hitGroups"] = {
        "head": ["head", "hair_front", "eye_l", "eye_r"],
        "tail": ["tail_0", "tail_1", "tail_2", "tail_3"],
        "body": ["body", "neck"],
    }
    rig["interactions"] = {
        "head": {
            "clip": "head_pat",
            "copy": "head_pat",
            "impulse": {"param": "headAngleZ", "angularVel": 42.0},
        },
        "tail": {
            "clip": "poke",
            "copy": "tail",
            "impulse": {"chain": "tail", "chainAngularVel": 220.0},
        },
        "body": {
            "clip": "poke",
            "copy": "poke",
            "impulse": {"param": "breath", "squashVel": 6.0},
        },
    }
    return rig


class ValidRigTests(unittest.TestCase):
    def test_fixture_validates(self) -> None:
        self.assertEqual(schema_errors(sample_rig()), [])
        validate_rig(sample_rig())

    def test_part_count_stays_within_the_budget(self) -> None:
        """Per-frame cost scales linearly with the part count.

        v1 is capped at 20 deliberately; the 44-part art breakdown is a v2
        proposal. Without this assertion the cap is a sentence in a design doc,
        which is not a thing that stops a pull request.
        """
        self.assertLessEqual(len(sample_rig()["parts"]), MAX_PARTS)

    def test_too_many_parts_is_rejected(self) -> None:
        rig = sample_rig()
        template = dict(rig["parts"][-1])
        while len(rig["parts"]) <= MAX_PARTS:
            extra = dict(template)
            extra["id"] = f"filler_{len(rig['parts'])}"
            extra["file"] = f"parts/filler_{len(rig['parts'])}.png"
            rig["parts"].append(extra)
        self.assertTrue(
            any("more than the 20 allowed" in error for error in schema_errors(rig))
        )


class StructuralTests(unittest.TestCase):
    def test_bone_cycle_is_reported(self) -> None:
        rig = sample_rig()
        bones = {bone["id"]: bone for bone in rig["bones"]}
        bones["body"]["parent"] = "head"
        errors = schema_errors(rig)
        self.assertTrue(any("parent cycle" in error for error in errors), errors)

    def test_unknown_parent_is_reported(self) -> None:
        rig = sample_rig()
        rig["bones"][1]["parent"] = "spine"
        self.assertTrue(
            any("unknown parent 'spine'" in error for error in schema_errors(rig))
        )

    def test_duplicate_bone_id_is_reported(self) -> None:
        rig = sample_rig()
        rig["bones"].append({"id": "head", "parent": "neck", "pivot": [130, 128]})
        self.assertTrue(
            any("duplicate bone id: 'head'" in error for error in schema_errors(rig))
        )

    def test_duplicate_part_id_is_reported(self) -> None:
        rig = sample_rig()
        rig["parts"].append(dict(rig["parts"][0]))
        self.assertTrue(
            any("duplicate part id: 'tail_0'" in error for error in schema_errors(rig))
        )

    def test_binding_to_undeclared_param_is_reported(self) -> None:
        rig = sample_rig()
        rig["bindings"].append(
            {"param": "browRaise", "bone": "head", "channel": "rotate"}
        )
        self.assertTrue(
            any(
                "undeclared param 'browRaise'" in error for error in schema_errors(rig)
            )
        )

    def test_validate_rig_raises_with_every_error(self) -> None:
        rig = sample_rig()
        rig["bindings"].append({"param": "nope", "bone": "head", "channel": "rotate"})
        rig["stateMap"]["IDLE"] = "missing"
        with self.assertRaises(RigValidationError) as caught:
            validate_rig(rig)
        self.assertEqual(len(caught.exception.errors), 2)


class PartPathTests(unittest.TestCase):
    def test_parent_traversal_is_rejected(self) -> None:
        rig = sample_rig()
        rig["parts"][0]["file"] = "../../../etc/passwd"
        self.assertTrue(
            any("must not escape the pack root" in error for error in schema_errors(rig))
        )

    def test_absolute_path_is_rejected(self) -> None:
        rig = sample_rig()
        rig["parts"][0]["file"] = "/etc/passwd"
        self.assertTrue(
            any("must be relative" in error for error in schema_errors(rig))
        )

    def test_backslash_path_is_rejected(self) -> None:
        rig = sample_rig()
        rig["parts"][0]["file"] = "parts\\..\\..\\secret.png"
        self.assertTrue(
            any("forward slashes" in error for error in schema_errors(rig))
        )

    def test_missing_file_is_rejected(self) -> None:
        rig = sample_rig()
        rig["parts"][0].pop("file")
        self.assertTrue(
            any("non-empty string" in error for error in schema_errors(rig))
        )

    def test_resolve_part_path_confines_to_the_pack_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pack"
            (root / "parts").mkdir(parents=True)
            resolved = resolve_part_path(root, "parts/head.png")
            self.assertEqual(resolved, (root / "parts" / "head.png").resolve())
            with self.assertRaises(ValueError):
                resolve_part_path(root, "../outside.png")


@dataclass
class FakeDescriptor:
    """The slice of ``PackDescriptor`` that :func:`load_rig` actually reads."""

    pack_id: str
    asset_root: Path
    rig_path: Path
    renderer: str = "rig"


class LoadRigTests(unittest.TestCase):
    def _pack(self, tmp: str, rig: dict) -> FakeDescriptor:
        root = Path(tmp) / "pet-standard-rig"
        root.mkdir(parents=True, exist_ok=True)
        rig_path = root / "rig.json"
        rig_path.write_text(json.dumps(rig), encoding="utf-8")
        return FakeDescriptor("standard", root, rig_path)

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            descriptor = self._pack(tmp, sample_rig())
            loaded = load_rig(descriptor)
            self.assertEqual(schema_errors(loaded), [])
            self.assertEqual(set(loaded["clips"]), set(sample_rig()["clips"]))

    def test_a_broken_rig_fails_at_load_not_at_paint(self) -> None:
        """Failing here is what lets the caller fall back to another pack.

        The same rig reaching ``paintEvent`` produces a KeyError inside a Qt
        event handler, where the only available outcome is a dead pet.
        """
        rig = sample_rig()
        rig["stateMap"]["IDLE"] = "nope"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RigValidationError):
                load_rig(self._pack(tmp, rig))

    def test_frame_renderer_packs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            descriptor = self._pack(tmp, sample_rig())
            with self.assertRaises(ValueError):
                load_rig(FakeDescriptor("chibi", descriptor.asset_root, descriptor.rig_path, "frames"))


class ClipReferenceTests(unittest.TestCase):
    def test_state_map_must_name_real_clips(self) -> None:
        rig = sample_rig()
        rig["stateMap"]["ERROR"] = "meltdown"
        self.assertTrue(
            any(
                "stateMap['ERROR'] names unknown clip 'meltdown'" in error
                for error in schema_errors(rig)
            )
        )

    def test_state_map_must_declare_idle(self) -> None:
        rig = sample_rig()
        rig["stateMap"].pop("IDLE")
        self.assertTrue(
            any("missing required state 'IDLE'" in error for error in schema_errors(rig))
        )

    def test_working_activity_map_must_name_real_clips(self) -> None:
        rig = sample_rig()
        rig["workingActivityMap"]["testing"] = "unit_testing"
        self.assertTrue(
            any("unknown clip 'unit_testing'" in error for error in schema_errors(rig))
        )

    def test_idle_micro_clips_must_name_real_clips(self) -> None:
        rig = sample_rig()
        rig["idleMicroClips"] = ["blink", "yawn"]
        self.assertTrue(
            any(
                "idleMicroClips names unknown clip 'yawn'" in error
                for error in schema_errors(rig)
            )
        )

    def test_interaction_clip_must_be_real(self) -> None:
        rig = sample_rig()
        rig["interactions"]["tail"]["clip"] = "tail_whack"
        self.assertTrue(
            any(
                "interaction 'tail' names unknown clip 'tail_whack'" in error
                for error in schema_errors(rig)
            )
        )

    def test_interaction_must_target_a_declared_hit_group(self) -> None:
        rig = sample_rig()
        rig["interactions"]["elbow"] = {"clip": "poke"}
        self.assertTrue(
            any(
                "interaction 'elbow' is not a declared hitGroup" in error
                for error in schema_errors(rig)
            )
        )

    def test_interaction_impulse_targets_are_checked(self) -> None:
        rig = sample_rig()
        rig["interactions"]["tail"]["impulse"] = {
            "chain": "fin",
            "chainAngularVel": 10.0,
        }
        rig["interactions"]["head"]["impulse"] = {"param": "browRaise", "angularVel": 1.0}
        errors = schema_errors(rig)
        self.assertTrue(any("unknown chain 'fin'" in error for error in errors), errors)
        self.assertTrue(
            any("undeclared param 'browRaise'" in error for error in errors), errors
        )


class HitGroupTests(unittest.TestCase):
    def test_members_must_be_real_parts(self) -> None:
        rig = sample_rig()
        rig["hitGroups"]["head"].append("forehead")
        self.assertTrue(
            any(
                "hitGroup 'head' names unknown part 'forehead'" in error
                for error in schema_errors(rig)
            )
        )

    def test_empty_group_is_rejected(self) -> None:
        rig = sample_rig()
        rig["hitGroups"]["body"] = []
        self.assertTrue(
            any("hitGroup 'body' has no parts" in error for error in schema_errors(rig))
        )

    def test_every_declared_group_is_reachable_from_a_part(self) -> None:
        """Parts name a ``hitGroup``; the group list must agree with them."""
        rig = sample_rig()
        declared = set(rig["hitGroups"])
        used = {part["hitGroup"] for part in rig["parts"] if part.get("hitGroup")}
        self.assertTrue(used <= declared, used - declared)


class ClipShapeTests(unittest.TestCase):
    def test_one_shot_needs_a_duration(self) -> None:
        rig = sample_rig()
        rig["clips"]["blink"].pop("durationMs")
        self.assertTrue(
            any("no positive durationMs" in error for error in schema_errors(rig))
        )

    def test_track_param_must_be_declared(self) -> None:
        rig = sample_rig()
        rig["clips"]["blink"]["tracks"][0]["param"] = "eyelidOpen"
        self.assertTrue(
            any("undeclared param 'eyelidOpen'" in error for error in schema_errors(rig))
        )

    def test_unknown_blend_and_interp_are_rejected(self) -> None:
        rig = sample_rig()
        rig["clips"]["poke"]["tracks"][0]["blend"] = "multiply"
        rig["clips"]["poke"]["tracks"][0]["interp"] = "bezier"
        errors = schema_errors(rig)
        self.assertTrue(any("unknown blend 'multiply'" in error for error in errors))
        self.assertTrue(any("unknown interp 'bezier'" in error for error in errors))

    def test_unsorted_track_keys_are_rejected(self) -> None:
        rig = sample_rig()
        rig["clips"]["poke"]["tracks"][0]["keys"] = [[0, 0.0], [320, 0.0], [100, 0.6]]
        self.assertTrue(
            any("keys are not sorted by time" in error for error in schema_errors(rig))
        )

    def test_oscillator_needs_a_positive_period(self) -> None:
        rig = sample_rig()
        rig["clips"]["idle"]["oscillators"][0]["periodMs"] = 0
        self.assertTrue(
            any("positive periodMs" in error for error in schema_errors(rig))
        )

    def test_chain_driver_must_be_produced_by_something(self) -> None:
        """A chain whose driver nobody writes is dead weight, and silently so.

        This is the failure mode a rename introduces: the tail simply stops
        swinging, with no error anywhere, and it looks like a physics bug.
        """
        rig = sample_rig()
        for clip in rig["clips"].values():
            for osc in clip.get("oscillators", ()):
                if osc["param"] == "tailSwing":
                    osc["param"] = "tailSway"
        self.assertTrue(
            any(
                "driver 'tailSwing' is neither a declared param" in error
                for error in schema_errors(rig)
            )
        )


class ManifestSynthesisTests(unittest.TestCase):
    def test_manifest_carries_the_routing_tables_through(self) -> None:
        rig = sample_rig()
        manifest = animation_manifest_from_rig(rig)
        self.assertEqual(manifest["stateMap"], rig["stateMap"])
        self.assertEqual(manifest["workingActivityMap"], rig["workingActivityMap"])
        self.assertEqual(list(manifest["idleMicroClips"]), rig["idleMicroClips"])
        self.assertEqual(set(manifest["clips"]), set(rig["clips"]))

    def test_looping_clips_collapse_to_one_inert_token(self) -> None:
        manifest = animation_manifest_from_rig(sample_rig())
        self.assertEqual(manifest["clips"]["idle"]["frames"], ["@rig/idle"])
        self.assertTrue(manifest["clips"]["idle"]["loop"])

    def test_one_shot_frame_count_covers_duration_plus_decay(self) -> None:
        manifest = animation_manifest_from_rig(sample_rig())
        blink = manifest["clips"]["blink"]
        self.assertFalse(blink["loop"])
        self.assertEqual(blink["frameMs"], 40)
        # 560 ms duration + 120 ms decay = 680 ms -> 17 frames of 40 ms.
        self.assertEqual(len(blink["frames"]), 17)
        self.assertEqual(set(blink["frames"]), {"@rig/blink"})

    def test_short_one_shot_still_gets_two_frames(self) -> None:
        """A one-frame clip would never expire.

        ``AnimationModel.advance`` returns early for single-frame clips, so a
        one-shot overlay of one frame pins the overlay layer forever.
        """
        rig = sample_rig()
        rig["clips"]["blink"]["durationMs"] = 10
        rig["clips"]["blink"]["envelope"] = {"attackMs": 2, "decayMs": 2}
        manifest = animation_manifest_from_rig(rig)
        self.assertEqual(len(manifest["clips"]["blink"]["frames"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
