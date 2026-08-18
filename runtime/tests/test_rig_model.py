import copy
import unittest

from runtime.rig_model import (
    IDENTITY,
    RigModel,
    RigValidationError,
    a_multiply,
    a_rotate,
    local_affine,
)


def sample_rig() -> dict:
    """A small synthetic rig in the shape the v1 20-part pack will use.

    Deliberately synthetic: Phase D owns the real ``assets/pet-<pack>-rig.json``,
    and a fixture that lives in the test file can be broken on purpose without
    touching shipped assets.

    Every binding is neutral at its parameter default (additive bindings
    evaluate to 0, multiplicative ones to 1), which is what makes the
    "defaults yield identity" assertion meaningful.
    """
    return {
        "formatVersion": 3,
        "renderer": "rig",
        "logicalWidth": 260,
        "logicalHeight": 260,
        "overflow": {"left": 26, "top": 22, "right": 14, "bottom": 48},
        "params": {
            "headAngleZ": {"min": -18, "max": 18, "default": 0},
            "headAngleY": {"min": -18, "max": 18, "default": 0},
            "breath": {"min": -1, "max": 1, "default": 0},
            "eyeOpen": {"min": 0, "max": 1, "default": 1},
            "mouthOpen": {"min": 0, "max": 1, "default": 0},
            "tail0": {"min": -20, "max": 20, "default": 0},
            "tail1": {"min": -20, "max": 20, "default": 0},
            "tail2": {"min": -20, "max": 20, "default": 0},
            "tail3": {"min": -20, "max": 20, "default": 0},
        },
        "bones": [
            {"id": "root", "parent": None, "pivot": [130, 250]},
            {"id": "body", "parent": "root", "pivot": [130, 200]},
            {"id": "neck", "parent": "body", "pivot": [130, 150]},
            {"id": "head", "parent": "neck", "pivot": [130, 128]},
            {"id": "tail_b0", "parent": "body", "pivot": [150, 205],
             "chain": "tail", "chainIndex": 0},
            {"id": "tail_b1", "parent": "tail_b0", "pivot": [172, 208],
             "chain": "tail", "chainIndex": 1},
            {"id": "tail_b2", "parent": "tail_b1", "pivot": [194, 211],
             "chain": "tail", "chainIndex": 2},
            {"id": "tail_b3", "parent": "tail_b2", "pivot": [216, 214],
             "chain": "tail", "chainIndex": 3},
        ],
        "chains": {
            "tail": {
                "driver": "tailSwing",
                "amplitudeDeg": 16.0,
                "distribution": [1.0, 0.85, 0.7, 0.55],
                "spring": {
                    "stiffness": 90.0,
                    "dampingRatio": 0.45,
                    "lagPerSegmentMs": 26.0,
                    "maxDeg": 24.0,
                },
                "deform": "strips",
                "bones": ["tail_b0", "tail_b1", "tail_b2", "tail_b3"],
                "segmentParams": ["tail0", "tail1", "tail2", "tail3"],
            }
        },
        "parts": [
            {"id": "tail_0", "file": "parts/tail_0.png", "z": 5, "bone": "tail_b0",
             "rect": [146, 192, 30, 28], "pivot": [150, 205], "hitGroup": "tail"},
            {"id": "tail_1", "file": "parts/tail_1.png", "z": 6, "bone": "tail_b1",
             "rect": [168, 195, 30, 28], "pivot": [172, 208], "hitGroup": "tail"},
            {"id": "tail_2", "file": "parts/tail_2.png", "z": 7, "bone": "tail_b2",
             "rect": [190, 198, 30, 28], "pivot": [194, 211], "hitGroup": "tail"},
            {"id": "tail_3", "file": "parts/tail_3.png", "z": 8, "bone": "tail_b3",
             "rect": [212, 201, 34, 30], "pivot": [216, 214], "hitGroup": "tail",
             "strips": 6, "stripBones": ["tail_b2", "tail_b3"]},
            {"id": "hair_back", "file": "parts/hair_back.png", "z": 10, "bone": "head",
             "rect": [96, 96, 68, 80], "pivot": [130, 128]},
            {"id": "body", "file": "parts/body.png", "z": 20, "bone": "body",
             "rect": [100, 150, 60, 100], "pivot": [130, 200]},
            {"id": "neck", "file": "parts/neck.png", "z": 30, "bone": "neck",
             "rect": [118, 138, 24, 20], "pivot": [130, 150]},
            {"id": "head", "file": "parts/head.png", "z": 40, "bone": "head",
             "rect": [98, 90, 64, 64], "pivot": [130, 128], "hitGroup": "head"},
            {"id": "eye_l", "file": "parts/eye_l.png", "z": 44, "bone": "head",
             "rect": [110, 118, 12, 8], "pivot": [116, 122]},
            {"id": "eye_r", "file": "parts/eye_r.png", "z": 45, "bone": "head",
             "rect": [138, 118, 12, 8], "pivot": [144, 122]},
            {"id": "mouth", "file": "parts/mouth.png", "z": 46, "bone": "head",
             "rect": [124, 132, 12, 6], "pivot": [130, 132]},
            {"id": "hair_front", "file": "parts/hair_front.png", "z": 50, "bone": "head",
             "rect": [96, 86, 68, 40], "pivot": [130, 128]},
        ],
        "bindings": [
            {"param": "headAngleZ", "bone": "head", "channel": "rotate", "gain": 1.0},
            {"param": "headAngleY", "bone": "head", "channel": "rotate", "gain": 0.2},
            {"param": "headAngleY", "bone": "head", "channel": "translateX", "gain": 0.35},
            {"param": "headAngleY", "bone": "body", "channel": "rotate", "gain": -0.05},
            {"param": "breath", "bone": "body", "channel": "scaleY", "gain": 0.03, "bias": 1.0},
            {"param": "breath", "bone": "body", "channel": "scaleY", "gain": 0.02, "bias": 1.0},
            {"param": "eyeOpen", "part": "eye_l", "channel": "scaleY", "gain": 1.0},
            {"param": "eyeOpen", "part": "eye_r", "channel": "scaleY", "gain": 1.0},
            {"param": "mouthOpen", "part": "mouth", "channel": "scaleY",
             "curve": [[0.0, 1.0], [0.5, 1.6], [1.0, 2.4]]},
            {"param": "tail0", "bone": "tail_b0", "channel": "rotate", "gain": 1.0},
            {"param": "tail1", "bone": "tail_b1", "channel": "rotate", "gain": 1.0},
            {"param": "tail2", "bone": "tail_b2", "channel": "rotate", "gain": 1.0},
            {"param": "tail3", "bone": "tail_b3", "channel": "rotate", "gain": 1.0},
        ],
    }


def by_id(transforms):
    return {transform.part_id: transform for transform in transforms}


class RigDefaultsTests(unittest.TestCase):
    def test_sample_rig_validates(self) -> None:
        RigModel(sample_rig()).validate()

    def test_defaults_yield_identity_and_source_rects(self) -> None:
        model = RigModel(sample_rig())
        transforms = model.solve()
        self.assertEqual(len(transforms), len(model.parts))
        for transform in transforms:
            for got, want in zip(transform.matrix, IDENTITY):
                self.assertAlmostEqual(
                    got, want, delta=1e-12, msg=f"{transform.part_id} matrix"
                )
            self.assertEqual(transform.src_rect, model.parts[transform.part_id].rect)
            self.assertAlmostEqual(transform.opacity, 1.0, delta=1e-12)

    def test_solve_is_pure(self) -> None:
        model = RigModel(sample_rig())
        params = {"headAngleZ": 9.0, "breath": 0.4, "tail1": -7.0}
        self.assertEqual(model.solve(params), model.solve(dict(params)))

    def test_out_of_range_params_are_clamped(self) -> None:
        model = RigModel(sample_rig())
        clamped = model.solve({"headAngleZ": 900.0})
        pinned = model.solve({"headAngleZ": 18.0})
        self.assertEqual(clamped, pinned)

    def test_unknown_params_are_ignored(self) -> None:
        model = RigModel(sample_rig())
        self.assertEqual(model.solve({"nonsense": 5.0}), model.solve())

    def test_output_is_z_ascending(self) -> None:
        transforms = RigModel(sample_rig()).solve({"headAngleZ": 6.0})
        zs = [transform.z for transform in transforms]
        self.assertEqual(zs, sorted(zs))
        self.assertEqual(transforms[0].part_id, "tail_0")
        self.assertEqual(transforms[-1].part_id, "hair_front")


class RigHierarchyTests(unittest.TestCase):
    def test_child_bone_inherits_parent_rotation(self) -> None:
        model = RigModel(sample_rig())
        head = by_id(model.solve({"headAngleY": 10.0}))["head"]
        # headAngleY drives body rotate -0.5, head rotate +2.0 and head tx +3.5.
        body_local = local_affine((130.0, 200.0), rotate=-0.5)
        neck_local = local_affine((130.0, 150.0))
        head_local = local_affine((130.0, 128.0), tx=3.5, rotate=2.0)
        expected = a_multiply(a_multiply(body_local, neck_local), head_local)
        for got, want in zip(head.matrix, expected):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_parent_rotation_moves_the_child_part(self) -> None:
        model = RigModel(sample_rig())
        rest = by_id(model.solve())["head"]
        leaned = by_id(model.solve({"headAngleY": 18.0}))["head"]
        self.assertEqual(rest.matrix, IDENTITY)
        self.assertNotEqual(leaned.matrix, IDENTITY)

    def test_deeper_chain_accumulates_down_the_tree(self) -> None:
        model = RigModel(sample_rig())
        tip = by_id(model.solve({"tail0": 10.0, "tail1": 10.0, "tail2": 10.0}))["tail_2"]
        expected = a_multiply(
            a_multiply(
                local_affine((150.0, 205.0), rotate=10.0),
                local_affine((172.0, 208.0), rotate=10.0),
            ),
            local_affine((194.0, 211.0), rotate=10.0),
        )
        for got, want in zip(tip.matrix, expected):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_local_affine_pivot_is_a_fixed_point(self) -> None:
        matrix = local_affine((130.0, 128.0), rotate=25.0, scale_x=1.5, scale_y=0.6)
        self.assertAlmostEqual(matrix[4] + 130.0 * matrix[0] + 128.0 * matrix[2], 130.0, delta=1e-9)


class RigChannelCombinationTests(unittest.TestCase):
    def test_rotate_bindings_sum(self) -> None:
        model = RigModel(sample_rig())
        head = by_id(model.solve({"headAngleZ": 10.0, "headAngleY": 10.0}))["head"]
        # head bone: 10.0 * 1.0 + 10.0 * 0.2 = 12.0, summed not overwritten;
        # the body bone contributes a further -0.05 * 10.0 down the tree.
        expected = a_rotate(12.0 - 0.5)
        self.assertAlmostEqual(head.matrix[0], expected[0], delta=1e-12)
        self.assertAlmostEqual(head.matrix[1], expected[1], delta=1e-12)

    def test_scale_bindings_multiply(self) -> None:
        model = RigModel(sample_rig())
        body = by_id(model.solve({"breath": 1.0}))["body"]
        self.assertAlmostEqual(body.matrix[3], 1.03 * 1.02, delta=1e-12)
        self.assertAlmostEqual(body.matrix[0], 1.0, delta=1e-12)

    def test_scale_bindings_are_not_summed(self) -> None:
        body = by_id(RigModel(sample_rig()).solve({"breath": 1.0}))["body"]
        self.assertLess(body.matrix[3], 1.5)

    def test_curve_binding_beats_gain_and_bias(self) -> None:
        model = RigModel(sample_rig())
        self.assertAlmostEqual(
            by_id(model.solve({"mouthOpen": 0.0}))["mouth"].matrix[3], 1.0, delta=1e-12
        )
        self.assertAlmostEqual(
            by_id(model.solve({"mouthOpen": 0.25}))["mouth"].matrix[3], 1.3, delta=1e-12
        )
        self.assertAlmostEqual(
            by_id(model.solve({"mouthOpen": 1.0}))["mouth"].matrix[3], 2.4, delta=1e-12
        )

    def test_curve_ends_are_clamped(self) -> None:
        rig = sample_rig()
        rig["params"]["mouthOpen"] = {"min": -5, "max": 5, "default": 0}
        model = RigModel(rig)
        self.assertAlmostEqual(
            by_id(model.solve({"mouthOpen": -5.0}))["mouth"].matrix[3], 1.0, delta=1e-12
        )
        self.assertAlmostEqual(
            by_id(model.solve({"mouthOpen": 5.0}))["mouth"].matrix[3], 2.4, delta=1e-12
        )

    def test_part_bindings_apply_after_the_bone(self) -> None:
        model = RigModel(sample_rig())
        eye = by_id(model.solve({"eyeOpen": 0.25, "headAngleZ": 12.0}))["eye_l"]
        expected = a_multiply(
            local_affine((130.0, 128.0), rotate=12.0),
            local_affine((116.0, 122.0), scale_y=0.25),
        )
        for got, want in zip(eye.matrix, expected):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_opacity_defaults_to_one_and_multiplies(self) -> None:
        rig = sample_rig()
        rig["params"]["fade"] = {"min": 0, "max": 1, "default": 1}
        rig["bindings"].append(
            {"param": "fade", "part": "hair_front", "channel": "opacity", "gain": 1.0}
        )
        rig["bindings"].append(
            {"param": "fade", "part": "hair_front", "channel": "opacity", "gain": 0.5}
        )
        model = RigModel(rig)
        model.validate()
        self.assertAlmostEqual(
            by_id(model.solve())["hair_front"].opacity, 0.5, delta=1e-12
        )
        self.assertAlmostEqual(
            by_id(model.solve({"fade": 0.0}))["hair_front"].opacity, 0.0, delta=1e-12
        )


class RigStripTests(unittest.TestCase):
    def test_strips_are_empty_until_the_chain_bends(self) -> None:
        tail = by_id(RigModel(sample_rig()).solve())["tail_3"]
        self.assertEqual(tail.strips, 6)
        self.assertEqual(len(tail.strip_matrices), 6)
        for matrix in tail.strip_matrices:
            for got, want in zip(matrix, IDENTITY):
                self.assertAlmostEqual(got, want, delta=1e-12)

    def test_strips_interpolate_between_the_chain_bones(self) -> None:
        tail = by_id(
            RigModel(sample_rig()).solve({"tail2": 12.0, "tail3": 12.0})
        )["tail_3"]
        first = tail.strip_matrices[0]
        last = tail.strip_matrices[-1]
        self.assertNotEqual(first, last)
        # Root-most strip stays closer to the parent bone than the tip strip.
        self.assertLess(abs(first[1]), abs(last[1]))

    def test_parts_without_strip_bones_have_no_strip_matrices(self) -> None:
        head = by_id(RigModel(sample_rig()).solve({"headAngleZ": 12.0}))["head"]
        self.assertEqual(head.strips, 0)
        self.assertEqual(head.strip_matrices, ())


class RigBoundsBasicsTests(unittest.TestCase):
    def test_rest_bbox_is_the_union_of_part_rects(self) -> None:
        model = RigModel(sample_rig())
        x, y, w, h = model.rest_bbox()
        self.assertAlmostEqual(x, 96.0, delta=1e-12)
        self.assertAlmostEqual(y, 86.0, delta=1e-12)
        self.assertAlmostEqual(x + w, 246.0, delta=1e-12)
        self.assertAlmostEqual(y + h, 250.0, delta=1e-12)

    def test_default_solve_bbox_equals_rest_bbox(self) -> None:
        model = RigModel(sample_rig())
        for got, want in zip(model.solve_bbox(), model.rest_bbox()):
            self.assertAlmostEqual(got, want, delta=1e-9)


class RigValidationTests(unittest.TestCase):
    def assert_rejects(self, rig: dict, needle: str) -> None:
        with self.assertRaises(RigValidationError) as caught:
            RigModel(rig).validate()
        self.assertTrue(
            any(needle in error for error in caught.exception.errors),
            msg=f"{needle!r} not in {caught.exception.errors}",
        )

    def test_rejects_a_bone_parent_cycle(self) -> None:
        rig = sample_rig()
        rig["bones"][1]["parent"] = "head"  # body -> head -> neck -> body
        self.assert_rejects(rig, "parent cycle")

    def test_rejects_an_unknown_parent(self) -> None:
        rig = sample_rig()
        rig["bones"][2]["parent"] = "spine"
        self.assert_rejects(rig, "unknown parent")

    def test_rejects_multiple_roots(self) -> None:
        rig = sample_rig()
        rig["bones"][1]["parent"] = None
        self.assert_rejects(rig, "exactly one root bone")

    def test_rejects_duplicate_bone_ids(self) -> None:
        rig = sample_rig()
        rig["bones"].append({"id": "neck", "parent": "body", "pivot": [130, 150]})
        self.assert_rejects(rig, "duplicate bone id")

    def test_rejects_duplicate_part_ids(self) -> None:
        rig = sample_rig()
        rig["parts"].append(copy.deepcopy(rig["parts"][0]))
        self.assert_rejects(rig, "duplicate part id")

    def test_rejects_a_binding_to_an_undeclared_param(self) -> None:
        rig = sample_rig()
        rig["bindings"].append(
            {"param": "wiggle", "bone": "head", "channel": "rotate", "gain": 1.0}
        )
        self.assert_rejects(rig, "undeclared param")

    def test_rejects_a_binding_to_an_unknown_bone(self) -> None:
        rig = sample_rig()
        rig["bindings"].append(
            {"param": "breath", "bone": "wing", "channel": "rotate", "gain": 1.0}
        )
        self.assert_rejects(rig, "unknown bone")

    def test_rejects_a_binding_naming_both_bone_and_part(self) -> None:
        rig = sample_rig()
        rig["bindings"].append(
            {"param": "breath", "bone": "head", "part": "head", "channel": "rotate"}
        )
        self.assert_rejects(rig, "exactly one of bone/part")

    def test_rejects_an_unknown_channel(self) -> None:
        rig = sample_rig()
        rig["bindings"].append(
            {"param": "breath", "bone": "head", "channel": "wobble", "gain": 1.0}
        )
        self.assert_rejects(rig, "unknown channel")

    def test_rejects_a_part_on_an_unknown_bone(self) -> None:
        rig = sample_rig()
        rig["parts"][0]["bone"] = "fin"
        self.assert_rejects(rig, "unknown bone")

    def test_rejects_an_unsorted_curve(self) -> None:
        rig = sample_rig()
        rig["bindings"].append(
            {
                "param": "breath",
                "part": "body",
                "channel": "scaleX",
                "curve": [[1.0, 1.0], [0.0, 2.0]],
            }
        )
        self.assert_rejects(rig, "not sorted by input")

    def test_rejects_a_chain_on_an_unknown_bone(self) -> None:
        rig = sample_rig()
        rig["chains"]["tail"]["bones"] = ["tail_b0", "fluke"]
        self.assert_rejects(rig, "unknown bone")

    def test_reports_every_problem_at_once(self) -> None:
        rig = sample_rig()
        rig["bones"][2]["parent"] = "spine"
        rig["parts"][0]["bone"] = "fin"
        with self.assertRaises(RigValidationError) as caught:
            RigModel(rig).validate()
        self.assertGreaterEqual(len(caught.exception.errors), 2)

    def test_a_cyclic_rig_still_constructs(self) -> None:
        # validate() must be able to report a cycle, so __init__ cannot choke.
        rig = sample_rig()
        rig["bones"][1]["parent"] = "head"
        model = RigModel(rig)
        self.assertIsInstance(model.solve(), list)


class ChainSpecTests(unittest.TestCase):
    def test_chain_spec_builds_a_matching_solver(self) -> None:
        model = RigModel(sample_rig())
        solver = model.chains["tail"].solver()
        self.assertEqual(solver.segments, 4)
        self.assertAlmostEqual(solver.max_deg, 24.0, delta=1e-12)
        self.assertEqual(solver.distribution, (1.0, 0.85, 0.7, 0.55))
        self.assertEqual(model.chains["tail"].segment_params,
                         ("tail0", "tail1", "tail2", "tail3"))

    def test_chain_declares_its_deform_mode(self) -> None:
        model = RigModel(sample_rig())
        self.assertEqual(model.chains["tail"].deform, "strips")
        self.assertEqual(model.chains["tail"].driver, "tailSwing")


class OverflowParsingTests(unittest.TestCase):
    def test_overflow_accepts_a_scalar(self) -> None:
        rig = sample_rig()
        rig["overflow"] = 12
        self.assertEqual(RigModel(rig).overflow, (12.0, 12.0, 12.0, 12.0))

    def test_overflow_bbox_grows_the_rest_box(self) -> None:
        model = RigModel(sample_rig())
        rest = model.rest_bbox()
        grown = model.overflow_bbox()
        self.assertAlmostEqual(grown[0], rest[0] - 26.0, delta=1e-12)
        self.assertAlmostEqual(grown[1], rest[1] - 22.0, delta=1e-12)
        self.assertAlmostEqual(
            grown[0] + grown[2], rest[0] + rest[2] + 14.0, delta=1e-12
        )
        self.assertAlmostEqual(
            grown[1] + grown[3], rest[1] + rest[3] + 48.0, delta=1e-12
        )


if __name__ == "__main__":
    unittest.main()
