"""The anchor guard.

Phase E reserves a fixed ``overflow`` padding around the rest box and computes
``footAnchor``/``bubbleAnchor`` from the *rest* box only, so deformation never
drifts the standing point or shoves the speech bubble. That contract holds only
while deformation stays inside the declared padding. Catching a violation here,
at build time, is the whole reason the sweep exists -- at runtime it would show
up as a silently clipped tail on someone's desktop.
"""

import unittest

from runtime.rig_model import RigModel
from runtime.tests.test_rig_model import sample_rig


def contains(outer, inner, tolerance: float = 1e-9) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox - tolerance
        and iy >= oy - tolerance
        and ix + iw <= ox + ow + tolerance
        and iy + ih <= oy + oh + tolerance
    )


class SweepBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = RigModel(sample_rig())
        self.model.validate()

    def test_sweep_stays_inside_the_declared_overflow(self) -> None:
        sweep = self.model.sweep_bbox()
        allowed = self.model.overflow_bbox()
        self.assertTrue(
            contains(allowed, sweep),
            msg=f"sweep {sweep} escapes declared overflow box {allowed}",
        )

    def test_a_denser_sweep_also_stays_inside(self) -> None:
        # Non-monotone curves can peak between the endpoints, so the guard must
        # survive a finer sample grid, not just min/default/max.
        allowed = self.model.overflow_bbox()
        for samples in (2, 3, 5, 9, 17):
            self.assertTrue(
                contains(allowed, self.model.sweep_bbox(samples)),
                msg=f"samples={samples} escapes {allowed}",
            )

    def test_sweep_contains_the_rest_box(self) -> None:
        self.assertTrue(contains(self.model.sweep_bbox(), self.model.rest_bbox()))

    def test_sweep_is_strictly_larger_than_rest(self) -> None:
        # A sweep that equals the rest box would mean nothing actually deforms,
        # which would make this whole test pass vacuously.
        rest = self.model.rest_bbox()
        sweep = self.model.sweep_bbox()
        self.assertLess(sweep[0], rest[0])
        self.assertGreater(sweep[2], rest[2])
        self.assertGreater(sweep[3], rest[3])

    def test_every_single_param_extreme_stays_inside(self) -> None:
        allowed = self.model.overflow_bbox()
        defaults = self.model.default_params()
        for name, spec in self.model.params.items():
            for value in (spec.minimum, spec.default, spec.maximum):
                probe = dict(defaults)
                probe[name] = value
                box = self.model.solve_bbox(probe)
                self.assertTrue(
                    contains(allowed, box), msg=f"{name}={value} gives {box}"
                )

    def test_chain_extremes_stay_inside(self) -> None:
        allowed = self.model.overflow_bbox()
        defaults = self.model.default_params()
        for chain in self.model.chains.values():
            for sign in (-1.0, 1.0):
                probe = dict(defaults)
                for param in chain.segment_params:
                    spec = self.model.params[param]
                    probe[param] = spec.clamp(sign * chain.max_deg)
                box = self.model.solve_bbox(probe)
                self.assertTrue(
                    contains(allowed, box),
                    msg=f"chain {chain.name} at {sign:+} maxDeg gives {box}",
                )


class SweepGuardHasTeethTests(unittest.TestCase):
    """A guard that cannot fail is not a guard."""

    def test_an_under_declared_overflow_is_rejected(self) -> None:
        rig = sample_rig()
        rig["overflow"] = {"left": 1, "top": 1, "right": 1, "bottom": 1}
        model = RigModel(rig)
        self.assertFalse(contains(model.overflow_bbox(), model.sweep_bbox()))

    def test_a_wider_swing_needs_a_wider_overflow(self) -> None:
        rig = sample_rig()
        for param in ("tail0", "tail1", "tail2", "tail3"):
            rig["params"][param] = {"min": -75, "max": 75, "default": 0}
        rig["chains"]["tail"]["spring"]["maxDeg"] = 75
        model = RigModel(rig)
        self.assertFalse(contains(model.overflow_bbox(), model.sweep_bbox()))

    def test_a_zero_overflow_rig_with_no_bindings_is_fine(self) -> None:
        rig = sample_rig()
        rig["bindings"] = []
        rig["chains"] = {}
        rig["overflow"] = 0
        model = RigModel(rig)
        self.assertTrue(contains(model.overflow_bbox(), model.sweep_bbox()))


if __name__ == "__main__":
    unittest.main()
