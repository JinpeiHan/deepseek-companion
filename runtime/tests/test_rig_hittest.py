import unittest

from runtime.rig_model import (
    IDENTITY,
    AlphaMask,
    PartTransform,
    RigModel,
    a_map,
    a_scale,
    hit_test,
)
from runtime.tests.test_rig_model import by_id, sample_rig


def solid(rect, cells: int = 8) -> AlphaMask:
    return AlphaMask.from_rows(["#" * cells] * cells, rect)


def ring(rect, cells: int = 8) -> AlphaMask:
    """Opaque border with a transparent hole in the middle."""
    rows = []
    for v in range(cells):
        if v in (0, cells - 1):
            rows.append("#" * cells)
        else:
            rows.append("#" + "." * (cells - 2) + "#")
    return AlphaMask.from_rows(rows, rect)


def part(part_id: str, z: float, rect, matrix=IDENTITY, opacity: float = 1.0):
    return PartTransform(
        part_id=part_id, z=z, matrix=matrix, src_rect=tuple(rect), opacity=opacity
    )


class AlphaMaskTests(unittest.TestCase):
    def test_rows_pack_into_bits_row_major(self) -> None:
        mask = AlphaMask.from_rows(["#.", ".#"], (0, 0, 2, 2))
        self.assertEqual(mask.width, 2)
        self.assertEqual(mask.height, 2)
        self.assertTrue(mask.covers(0.5, 0.5))
        self.assertFalse(mask.covers(1.5, 0.5))
        self.assertFalse(mask.covers(0.5, 1.5))
        self.assertTrue(mask.covers(1.5, 1.5))

    def test_points_outside_the_rect_never_cover(self) -> None:
        mask = solid((10, 20, 40, 40))
        self.assertFalse(mask.covers(9.0, 25.0))
        self.assertFalse(mask.covers(51.0, 25.0))
        self.assertFalse(mask.covers(25.0, 19.0))
        self.assertFalse(mask.covers(25.0, 61.0))
        self.assertTrue(mask.covers(25.0, 25.0))

    def test_mask_cells_scale_to_the_rect(self) -> None:
        # 8 cells across a 64px rect: one cell is 8 source pixels wide.
        mask = AlphaMask.from_rows(["#......."] * 8, (0, 0, 64, 64))
        self.assertTrue(mask.covers(7.9, 30.0))
        self.assertFalse(mask.covers(8.1, 30.0))


class HitTestTransparencyTests(unittest.TestCase):
    def test_a_transparent_pixel_inside_the_bbox_misses(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [part("donut", 10, rect)]
        masks = {"donut": ring(rect)}
        # Dead centre of the hole: inside the bounding box, no ink.
        self.assertIsNone(hit_test(transforms, masks, 40.0, 40.0, radius=0.0))
        # The opaque border still hits.
        self.assertEqual(hit_test(transforms, masks, 5.0, 40.0, radius=0.0), "donut")

    def test_a_point_outside_every_rect_misses(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [part("body", 10, rect)]
        self.assertIsNone(
            hit_test(transforms, {"body": solid(rect)}, 500.0, 500.0, radius=3.0)
        )

    def test_parts_without_a_mask_fall_back_to_rect_containment(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [part("body", 10, rect)]
        self.assertEqual(hit_test(transforms, {}, 40.0, 40.0), "body")
        self.assertIsNone(hit_test(transforms, {}, 400.0, 40.0))

    def test_fully_transparent_parts_are_skipped(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [
            part("ghost", 50, rect, opacity=0.0),
            part("body", 10, rect),
        ]
        masks = {"ghost": solid(rect), "body": solid(rect)}
        self.assertEqual(hit_test(transforms, masks, 40.0, 40.0), "body")

    def test_collapsed_parts_are_skipped(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [
            part("closed_eye", 50, rect, matrix=a_scale(1.0, 0.0)),
            part("body", 10, rect),
        ]
        masks = {"closed_eye": solid(rect), "body": solid(rect)}
        self.assertEqual(hit_test(transforms, masks, 40.0, 40.0), "body")


class HitTestOrderingTests(unittest.TestCase):
    def test_topmost_z_wins_on_overlap(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [
            part("body", 20, rect),
            part("hair_front", 50, rect),
            part("head", 40, rect),
        ]
        masks = {name: solid(rect) for name in ("body", "hair_front", "head")}
        self.assertEqual(hit_test(transforms, masks, 40.0, 40.0), "hair_front")

    def test_a_hole_in_the_top_layer_lets_the_layer_below_win(self) -> None:
        rect = (0, 0, 80, 80)
        transforms = [part("body", 20, rect), part("hair_front", 50, rect)]
        masks = {"body": solid(rect), "hair_front": ring(rect)}
        self.assertEqual(
            hit_test(transforms, masks, 40.0, 40.0, radius=0.0), "body"
        )

    def test_input_order_does_not_matter(self) -> None:
        rect = (0, 0, 80, 80)
        masks = {"a": solid(rect), "b": solid(rect)}
        forward = [part("a", 1, rect), part("b", 2, rect)]
        self.assertEqual(hit_test(forward, masks, 10.0, 10.0), "b")
        self.assertEqual(hit_test(list(reversed(forward)), masks, 10.0, 10.0), "b")


class HitTestFollowsDeformationTests(unittest.TestCase):
    """A swung-out tail must still be pokeable where it is, not where it was."""

    def setUp(self) -> None:
        self.model = RigModel(sample_rig())
        self.head_rect = self.model.parts["head"].rect

    def head_transform(self, params):
        return by_id(self.model.solve(params))["head"]

    def test_the_hit_follows_the_part_after_a_rotation(self) -> None:
        mask = solid(self.head_rect)
        probe = (104.0, 96.0)  # near the top-left of the head rect

        rest = self.head_transform({})
        self.assertEqual(
            hit_test([rest], {"head": mask}, probe[0], probe[1], radius=0.0), "head"
        )

        turned = self.head_transform({"headAngleZ": 25.0})
        moved = a_map(turned.matrix, *probe)
        self.assertGreater(
            abs(moved[0] - probe[0]) + abs(moved[1] - probe[1]),
            6.0,
            msg="the 25 degree rotation must actually move the probe point",
        )
        self.assertEqual(
            hit_test([turned], {"head": mask}, moved[0], moved[1], radius=0.0), "head"
        )
        self.assertIsNone(
            hit_test([turned], {"head": mask}, probe[0], probe[1], radius=0.0)
        )

    def test_a_swung_tail_is_pokeable_at_its_new_position(self) -> None:
        params = {"tail0": 20.0, "tail1": 20.0, "tail2": 20.0, "tail3": 20.0}
        transforms = self.model.solve(params)
        tip = by_id(transforms)["tail_3"]
        masks = {"tail_3": solid(tip.src_rect)}
        centre = (
            tip.src_rect[0] + tip.src_rect[2] / 2.0,
            tip.src_rect[1] + tip.src_rect[3] / 2.0,
        )
        moved = a_map(tip.matrix, *centre)
        self.assertGreater(abs(moved[1] - centre[1]), 5.0)
        self.assertEqual(
            hit_test([tip], masks, moved[0], moved[1], radius=0.0), "tail_3"
        )


class HitTestHaloTests(unittest.TestCase):
    def setUp(self) -> None:
        # A 32x32 source rect at 1 source pixel per mask cell, inked only in
        # columns 16 and 17 -- a two-pixel ribbon, like an eyelid edge.
        rows = ["." * 16 + "##" + "." * 14 for _ in range(32)]
        self.rect = (0, 0, 32, 32)
        self.mask = AlphaMask.from_rows(rows, self.rect)
        self.transforms = [part("ribbon", 10, self.rect)]

    def test_zero_radius_requires_a_direct_hit(self) -> None:
        masks = {"ribbon": self.mask}
        self.assertEqual(
            hit_test(self.transforms, masks, 16.5, 16.0, radius=0.0), "ribbon"
        )
        self.assertIsNone(hit_test(self.transforms, masks, 10.5, 16.0, radius=0.0))

    def test_a_generous_radius_catches_a_near_miss(self) -> None:
        masks = {"ribbon": self.mask}
        self.assertEqual(
            hit_test(self.transforms, masks, 10.5, 16.0, radius=6.0), "ribbon"
        )

    def test_the_radius_does_not_reach_arbitrarily_far(self) -> None:
        masks = {"ribbon": self.mask}
        self.assertIsNone(hit_test(self.transforms, masks, 2.0, 16.0, radius=3.0))

    def test_the_halo_is_measured_in_output_pixels(self) -> None:
        # The part is scaled 4x, so 6 output pixels is only 1.5 source pixels
        # and no longer bridges the same gap in source space.
        masks = {"ribbon": self.mask}
        scaled = [part("ribbon", 10, self.rect, matrix=a_scale(4.0, 4.0))]
        self.assertEqual(hit_test(scaled, masks, 66.0, 64.0, radius=6.0), "ribbon")
        self.assertIsNone(hit_test(scaled, masks, 42.0, 64.0, radius=6.0))


if __name__ == "__main__":
    unittest.main()
