"""Phase F interaction wiring: pointer follow, per-part hits, drag physics.

Almost everything here is Qt-free. That is not an accident of convenience: the
interaction *decisions* were deliberately lifted out of ``CompanionWindow`` into
module-level functions in ``runtime.helper`` precisely so they could be pinned
without a display server, leaving the window methods thin enough that reading
them is enough to see what they do.

The two exceptions are marked:

* the hit-test seam is exercised through the *real* ``RigRenderer.to_source``
  plus ``rig_model.hit_test`` against the same synthetic rig
  ``test_rig_render_qt`` builds, because a hit test that agrees with a
  reimplementation of the transform proves nothing; and
* three source-invariant guards, which assert properties that only exist inside
  the ``run_visual`` closure and so cannot be reached by constructing an object.
"""

from __future__ import annotations

import inspect
import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from runtime import helper
from runtime.helper import (
    ANCHOR_VELOCITY_MAX,
    IMPULSE_SCALE_MAX,
    IMPULSE_SCALE_MIN,
    POINTER_ATTENTION_Y,
    RIG_DRAG_TICK_MS,
    AnchorVelocity,
    drag_tick_interval,
    frame_click_interaction,
    hit_group_for_part,
    impulse_scale,
    interaction_copy_group,
    part_geometry,
    pointer_offset,
    pointer_target,
    rig_interaction_for_part,
)
from runtime.rig_driver import POINTER_DEAD_ZONE, RigDriver
from runtime.rig_model import RigModel, hit_test

from runtime.tests.test_rig_render_qt import HAVE_QT, PARTS, build_rig

if HAVE_QT:
    from PySide6.QtGui import QPainter  # noqa: F401  (import guard only)


#: A pet box somewhere on a virtual desktop, deliberately not at the origin so
#: a missing offset term cannot pass by coincidence.
PET_RECT = (1000.0, 400.0, 260.0, 260.0)


def _attention_point(rect=PET_RECT) -> tuple[float, float]:
    x, y, w, h = rect
    return (x + w / 2.0, y + POINTER_ATTENTION_Y * h)


# --------------------------------------------------------------------------- #
# Pointer normalisation
# --------------------------------------------------------------------------- #


class PointerNormalisationTests(unittest.TestCase):
    def test_cursor_on_the_attention_point_is_exactly_zero(self) -> None:
        """Exactly, not approximately: the dead zone is a magnitude test."""
        self.assertEqual(pointer_offset(_attention_point(), PET_RECT), (0.0, 0.0))

    def test_cursor_at_the_pet_centre_stays_inside_the_dead_zone(self) -> None:
        """The attention point is the head, so the box centre is slightly low.

        It must still be well inside the dead zone, or the pet would sit with a
        permanent downward tilt whenever the cursor rested on it.
        """
        x, y, w, h = PET_RECT
        dx, dy = pointer_offset((x + w / 2.0, y + h / 2.0), PET_RECT)
        self.assertEqual(dx, 0.0)
        self.assertLess(math.hypot(dx, dy), POINTER_DEAD_ZONE)

    def test_far_away_clamps_to_the_unit_square(self) -> None:
        for cursor, expected in (
            ((100000.0, 400.0), (1.0,)),
            ((-100000.0, 400.0), (-1.0,)),
        ):
            with self.subTest(cursor=cursor):
                dx, _ = pointer_offset(cursor, PET_RECT)
                self.assertEqual(dx, expected[0])
        _, dy_down = pointer_offset((1130.0, 100000.0), PET_RECT)
        _, dy_up = pointer_offset((1130.0, -100000.0), PET_RECT)
        self.assertEqual(dy_down, 1.0)
        self.assertEqual(dy_up, -1.0)

    def test_offset_is_monotonic_and_signed_towards_the_cursor(self) -> None:
        ax, ay = _attention_point()
        near = pointer_offset((ax + 40.0, ay + 40.0), PET_RECT)
        far = pointer_offset((ax + 200.0, ay + 200.0), PET_RECT)
        self.assertGreater(near[0], 0.0)
        self.assertGreater(near[1], 0.0)
        self.assertGreater(far[0], near[0])
        self.assertGreater(far[1], near[1])

    def test_degenerate_pet_box_never_divides_by_zero(self) -> None:
        self.assertEqual(pointer_offset((5.0, 5.0), (0.0, 0.0, 0.0, 0.0)), (0.0, 0.0))

    def test_reduced_motion_reports_absent_and_zero(self) -> None:
        self.assertEqual(
            pointer_target(
                (99999.0, 99999.0),
                PET_RECT,
                reduced_motion=True,
                same_screen=True,
            ),
            (0.0, 0.0, False),
        )

    def test_a_cursor_on_another_screen_reports_present_but_zero(self) -> None:
        """Per-monitor DPI: degrade to a zero target, not a skewed one."""
        self.assertEqual(
            pointer_target(
                (99999.0, 99999.0),
                PET_RECT,
                reduced_motion=False,
                same_screen=False,
            ),
            (0.0, 0.0, True),
        )

    def test_same_screen_follows(self) -> None:
        dx, dy, present = pointer_target(
            (2000.0, 400.0), PET_RECT, reduced_motion=False, same_screen=True
        )
        self.assertTrue(present)
        self.assertGreater(dx, POINTER_DEAD_ZONE)
        self.assertLessEqual(dx, 1.0)
        self.assertLess(abs(dy), 1.0)

    def test_a_zero_target_produces_no_head_turn_in_the_driver(self) -> None:
        """End to end through the seam the window actually calls."""
        rig = build_rig()
        driver = RigDriver(rig)
        driver.set_pointer(*pointer_target(
            (99999.0, 99999.0), PET_RECT, reduced_motion=False, same_screen=False
        )[:3], now_ms=0)
        params = driver.advance(16, 16)
        self.assertAlmostEqual(params.get("headAngleZ", 0.0), 0.0, places=9)


# --------------------------------------------------------------------------- #
# Hit routing
# --------------------------------------------------------------------------- #


class HitRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = build_rig()

    def test_hit_groups_index_resolves_every_fixture_part(self) -> None:
        for part_id, _, _, _, _ in PARTS:
            with self.subTest(part=part_id):
                self.assertEqual(hit_group_for_part(self.rig, part_id), part_id)

    def test_a_parts_own_hit_group_wins_over_the_index(self) -> None:
        rig = build_rig()
        for entry in rig["parts"]:
            if entry["id"] == "head":
                entry["hitGroup"] = "cheek"
        self.assertEqual(hit_group_for_part(rig, "head"), "cheek")

    def test_unknown_or_missing_part_has_no_group(self) -> None:
        self.assertIsNone(hit_group_for_part(self.rig, "nose"))
        self.assertIsNone(hit_group_for_part(self.rig, None))

    def test_interaction_lookup_returns_the_declared_entry(self) -> None:
        resolved = rig_interaction_for_part(self.rig, "tail")
        self.assertIsNotNone(resolved)
        group, entry = resolved
        self.assertEqual(group, "tail")
        self.assertEqual(entry["clip"], "poke")
        self.assertEqual(entry["impulse"]["chain"], "tail")

    def test_a_group_with_no_interaction_is_a_miss(self) -> None:
        """The fixture declares a ``body`` hit group but no ``body`` interaction.

        A hit on it must read as "nothing happened", which is what leaves the
        click free to have been the start of a window drag.
        """
        self.assertIn("body", self.rig["hitGroups"])
        self.assertNotIn("body", self.rig["interactions"])
        self.assertIsNone(rig_interaction_for_part(self.rig, "body"))

    def test_no_part_is_a_miss(self) -> None:
        self.assertIsNone(rig_interaction_for_part(self.rig, None))

    def test_copy_group_falls_back_to_poke_for_an_unknown_group(self) -> None:
        copy = {"interaction": {"headPat": ["a"], "tail": ["b"], "poke": ["c"]}}
        self.assertEqual(interaction_copy_group({}, "cheek", copy), "poke")
        self.assertEqual(interaction_copy_group({}, "tail", copy), "tail")
        self.assertEqual(
            interaction_copy_group({"copy": "headPat"}, "tail", copy), "headPat"
        )
        # A declared copy group the persona file lacks falls back to the hit
        # group before it falls back to poke.
        self.assertEqual(
            interaction_copy_group({"copy": "nope"}, "tail", copy), "tail"
        )


# --------------------------------------------------------------------------- #
# Impulse scaling
# --------------------------------------------------------------------------- #


class ImpulseScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = build_rig()
        self.rect, self.pivot = part_geometry(self.rig, "tail")

    def test_fixture_pivot_is_read_from_the_rig(self) -> None:
        self.assertEqual(self.rect, (60.0, 300.0, 120.0, 140.0))
        self.assertEqual(self.pivot, (120.0, 300.0))

    def test_pivot_defaults_to_the_rect_centre(self) -> None:
        rect, pivot = part_geometry(self.rig, "head")
        self.assertEqual(pivot, (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0))

    def test_scale_grows_with_distance_from_the_pivot(self) -> None:
        previous = -1.0
        for distance in (0.0, 10.0, 30.0, 60.0, 90.0):
            value = impulse_scale(distance, self.rect)
            self.assertGreater(value, previous)
            previous = value

    def test_scale_is_clamped_at_both_ends(self) -> None:
        self.assertEqual(impulse_scale(0.0, self.rect), IMPULSE_SCALE_MIN)
        self.assertEqual(impulse_scale(-500.0, self.rect), IMPULSE_SCALE_MIN)
        self.assertEqual(impulse_scale(1e9, self.rect), IMPULSE_SCALE_MAX)

    def test_scale_is_resolution_independent(self) -> None:
        """A 2x rig of the same character must whip identically."""
        doubled = tuple(value * 2.0 for value in self.rect)
        self.assertAlmostEqual(
            impulse_scale(40.0, self.rect), impulse_scale(80.0, doubled), places=12
        )

    def test_degenerate_part_returns_the_minimum(self) -> None:
        self.assertEqual(impulse_scale(10.0, (0.0, 0.0, 0.0, 0.0)), IMPULSE_SCALE_MIN)

    def test_a_tip_poke_kicks_the_chain_harder_than_a_root_poke(self) -> None:
        """The property the scaling exists for, measured on the real solver."""
        spec = dict(build_rig()["interactions"]["tail"]["impulse"])

        def peak(distance: float) -> float:
            driver = RigDriver(build_rig())
            kicked = dict(spec)
            kicked["scale"] = impulse_scale(distance, self.rect)
            self.assertTrue(driver.apply_impulse(kicked))
            worst = 0.0
            for step in range(60):
                driver.advance(16, 16 * (step + 1))
                worst = max(worst, max(abs(a) for a in driver.chain_angles("tail")))
            return worst

        self.assertGreater(peak(90.0), peak(0.0) * 1.5)


# --------------------------------------------------------------------------- #
# Drag physics
# --------------------------------------------------------------------------- #


class AnchorVelocityTests(unittest.TestCase):
    #: Deliberately irregular: 16, 3, 40, 7, 25ms. The 3ms and 7ms gaps are
    #: below the differentiation floor and must be folded forward rather than
    #: dividing a small move by a near-zero interval.
    IRREGULAR = ((0, 0.0), (16, 20.0), (19, 24.0), (59, 90.0), (66, 101.0), (91, 130.0))

    def test_first_sample_only_seeds_the_anchor(self) -> None:
        tracker = AnchorVelocity()
        self.assertEqual(tracker.update(100.0, 100.0, 0.0), (0.0, 0.0))
        self.assertTrue(tracker.started)

    def test_an_irregular_drag_produces_a_bounded_velocity(self) -> None:
        tracker = AnchorVelocity()
        seen = []
        for now_ms, x in self.IRREGULAR:
            vx, _ = tracker.update(x, 0.0, now_ms)
            seen.append(vx)
            self.assertLessEqual(abs(vx), ANCHOR_VELOCITY_MAX)
        # The drag really moved, so this is not a trivially-zero pass.
        self.assertGreater(seen[-1], 100.0)
        # And the low-pass is a low-pass: no sample may exceed the fastest raw
        # rate in the trace (~1650 px/s across the 40ms leg).
        self.assertLess(max(seen), 1700.0)

    def test_a_sub_millisecond_gap_does_not_spike(self) -> None:
        tracker = AnchorVelocity()
        tracker.update(0.0, 0.0, 0.0)
        tracker.update(1.0, 0.0, 0.5)
        self.assertEqual(tracker.value, (0.0, 0.0))
        # ...and the deferred sample is measured across the whole gap.
        tracker.update(10.0, 0.0, 10.0)
        self.assertAlmostEqual(tracker.value[0], 0.35 * 1000.0, places=6)

    def test_a_teleport_is_clamped(self) -> None:
        tracker = AnchorVelocity(alpha=1.0)
        tracker.update(0.0, 0.0, 0.0)
        tracker.update(1e9, 0.0, 16.0)
        self.assertEqual(tracker.value[0], ANCHOR_VELOCITY_MAX)

    def test_a_stalled_drag_reports_zero(self) -> None:
        tracker = AnchorVelocity()
        tracker.update(0.0, 0.0, 0.0)
        tracker.update(100.0, 0.0, 20.0)
        self.assertGreater(tracker.sample(30.0)[0], 0.0)
        self.assertEqual(tracker.sample(2000.0), (0.0, 0.0))

    def test_sample_before_any_update_is_zero(self) -> None:
        self.assertEqual(AnchorVelocity().sample(1234.0), (0.0, 0.0))


class DragTickIntervalTests(unittest.TestCase):
    def test_frame_packs_stop_the_ticker(self) -> None:
        """The shipped Windows layered-window flicker workaround, untouched."""
        for tick_ms in (20, 40):
            self.assertIsNone(drag_tick_interval(tick_ms, False))

    def test_rig_packs_keep_ticking_at_the_degraded_rate(self) -> None:
        self.assertEqual(drag_tick_interval(16, True), RIG_DRAG_TICK_MS)
        self.assertEqual(drag_tick_interval(33, True), RIG_DRAG_TICK_MS)

    def test_a_slower_renderer_is_never_sped_up_by_a_drag(self) -> None:
        self.assertEqual(drag_tick_interval(50, True), 50)


class DragSettleTests(unittest.TestCase):
    """A synthetic irregular drag, then release, then the chain settles.

    Run against the real ``RigDriver`` in exactly the order ``_advance_rig``
    calls it, so a wiring mistake -- most likely re-reporting the drag velocity
    after release, which would pin the chain at throw speed forever -- fails
    here rather than only on a real desktop.
    """

    TICK_MS = 33

    @staticmethod
    def _rig() -> dict:
        """The render fixture plus the two root params a drag actually writes.

        ``rootLeanZ``/``rootBobY`` are driver-owned parameters; a rig that does
        not declare them simply never leans, which is legal but would make the
        assertions below vacuous.
        """
        rig = build_rig()
        rig["params"]["rootLeanZ"] = {"min": -20.0, "max": 20.0, "default": 0.0}
        rig["params"]["rootBobY"] = {"min": -20.0, "max": 20.0, "default": 0.0}
        return rig

    def _drag_then_release(self) -> tuple[RigDriver, AnchorVelocity]:
        driver = RigDriver(self._rig())
        tracker = AnchorVelocity()
        now_ms = 0
        x = 0.0
        tracker.update(x, 0.0, now_ms)
        # ~500ms of dragging, with irregular per-tick travel.
        for step in range(15):
            now_ms += self.TICK_MS
            x += 24.0 if step % 3 else 9.0
            tracker.update(x, 0.0, now_ms)
            driver.set_root_motion(*tracker.sample(now_ms), True)
            driver.advance(self.TICK_MS, now_ms)
        return driver, tracker

    def test_dragging_actually_disturbs_the_chain(self) -> None:
        driver, _ = self._drag_then_release()
        self.assertGreater(
            max(abs(a) for a in driver.chain_angles("tail")),
            0.5,
            "the drag never moved the tail, so the settle test proves nothing",
        )
        # And the body leans into the throw, negatively: the pet is dragged
        # rightwards, so it leans back against the motion.
        self.assertLess(driver.advance(0, 0).get("rootLeanZ", 0.0), -1.0)

    def test_the_chain_lags_then_settles_after_release(self) -> None:
        driver, tracker = self._drag_then_release()
        now_ms = 15 * self.TICK_MS

        # The release tick, exactly as ``_advance_rig`` does it: hand the final
        # velocity over once with dragging=False, then stop reporting, so the
        # driver's own release decay is what runs the settle.
        driver.set_root_motion(*tracker.value, False)
        tracker.reset()

        peak = 0.0
        at_one_second = None
        for elapsed in range(self.TICK_MS, 2001, self.TICK_MS):
            now_ms += self.TICK_MS
            driver.advance(self.TICK_MS, now_ms)
            worst = max(abs(a) for a in driver.chain_angles("tail"))
            peak = max(peak, worst)
            if at_one_second is None and elapsed >= 1000:
                at_one_second = worst
                lean_at_one_second = abs(driver.advance(0, now_ms).get("rootLeanZ", 0.0))

        # It overshoots on the way down rather than snapping to rest -- that
        # overshoot *is* the "hair keeps going after you stop" read.
        self.assertGreater(peak, 3.0, f"no overshoot at all (peak {peak:.3f} deg)")
        self.assertLess(
            at_one_second, 1.0, f"tail still at {at_one_second:.3f} deg after 1s"
        )
        self.assertLess(lean_at_one_second, 0.5)
        settled = max(abs(a) for a in driver.chain_angles("tail"))
        self.assertLess(settled, 0.1, f"tail still at {settled:.3f} deg after 2s")
        self.assertLess(settled, at_one_second)


# --------------------------------------------------------------------------- #
# Frame packs are untouched
# --------------------------------------------------------------------------- #


class FrameClickHeuristicTests(unittest.TestCase):
    """chibi keeps the rectangle zones verbatim; the snapshot hash depends on it."""

    RECT = (25, 18, 195, 260)

    def test_upper_zone_is_a_head_pat(self) -> None:
        clip, group, ttl = frame_click_interaction(100.0, 60.0, self.RECT)
        self.assertEqual((clip, group, ttl), ("head_pat", "headPat", 1800))

    def test_right_zone_is_the_tail(self) -> None:
        clip, group, ttl = frame_click_interaction(
            25 + 195 * 0.9, 18 + 260 * 0.8, self.RECT
        )
        self.assertEqual((clip, group, ttl), ("tail", "tail", 1500))

    def test_everything_else_is_a_poke(self) -> None:
        clip, group, ttl = frame_click_interaction(
            25 + 195 * 0.4, 18 + 260 * 0.8, self.RECT
        )
        self.assertEqual((clip, group, ttl), ("poke", "poke", 1500))

    def test_the_zone_boundaries_are_the_shipped_ones(self) -> None:
        pet_x, pet_y, pet_width, pet_height = self.RECT
        just_above = pet_y + pet_height * 0.45 - 0.01
        just_below = pet_y + pet_height * 0.45 + 0.01
        self.assertEqual(frame_click_interaction(100.0, just_above, self.RECT)[0], "head_pat")
        self.assertNotEqual(frame_click_interaction(100.0, just_below, self.RECT)[0], "head_pat")
        self.assertEqual(
            frame_click_interaction(pet_x + pet_width * 0.72 - 0.01, just_below, self.RECT)[0],
            "poke",
        )
        self.assertEqual(
            frame_click_interaction(pet_x + pet_width * 0.72 + 0.01, just_below, self.RECT)[0],
            "tail",
        )

    def test_a_click_left_of_or_above_the_pet_clamps_like_before(self) -> None:
        self.assertEqual(frame_click_interaction(0.0, 0.0, self.RECT)[0], "head_pat")


# --------------------------------------------------------------------------- #
# Source invariants that live inside the run_visual closure
# --------------------------------------------------------------------------- #


class ClosureInvariantTests(unittest.TestCase):
    """Guards for behaviour that cannot be reached by constructing an object.

    ``CompanionWindow`` is defined inside ``run_visual`` so it can close over
    the loaded persona copy and the Qt imports. These three properties are the
    ones a future edit is most likely to break silently, so they are pinned by
    reading the source rather than left unguarded.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = inspect.getsource(helper.run_visual)

    @staticmethod
    def _method(source: str, name: str) -> str:
        """The text of one nested method, from ``def`` to the next dedent."""
        marker = f"def {name}(self"
        start = source.rindex("\n", 0, source.index(marker)) + 1
        lines = source[start:].splitlines()
        indent = len(lines[0]) - len(lines[0].lstrip())
        body = [lines[0]]
        for line in lines[1:]:
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                break
            body.append(line)
        return "\n".join(body)

    def test_a_press_arms_the_drag_without_consulting_the_hit_test(self) -> None:
        """"Click nothing and you still drag the window" -- the affordance the
        plan forbids trading away for click-through."""
        press = self._method(self.source, "mousePressEvent")
        self.assertIn("self.drag_origin = event.globalPosition().toPoint()", press)
        for forbidden in ("hit_test", "_rig_hit", "_play_rig_click", "rig_interaction"):
            self.assertNotIn(forbidden, press, f"{forbidden} must not gate the drag")

    def test_the_drag_only_stops_the_ticker_when_the_helper_says_to(self) -> None:
        begin = self._method(self.source, "_begin_drag")
        self.assertIn("drag_tick_interval(", begin)
        self.assertIn("if interval is None:", begin)
        self.assertIn("self.animation_timer.stop()", begin)
        # A rig sets an interval instead, and never stops.
        self.assertIn("self.animation_timer.setInterval(interval)", begin)

    def test_frame_packs_keep_the_zone_heuristic_and_rigs_do_not(self) -> None:
        click = self._method(self.source, "_play_click_interaction")
        self.assertIn("if self.rig is not None:", click)
        self.assertIn("self._play_rig_click(x, y)", click)
        self.assertIn("frame_click_interaction(x, y, self._pet_rect())", click)

    def test_the_driver_inputs_are_set_before_advance(self) -> None:
        advance = self._method(self.source, "_advance_rig")
        set_pointer = advance.index("driver.set_pointer(")
        set_root = advance.index("driver.set_root_motion(")
        do_advance = advance.index("driver.advance(")
        self.assertLess(set_pointer, do_advance)
        self.assertLess(set_root, do_advance)


# --------------------------------------------------------------------------- #
# The real hit-test seam (needs Qt for the alpha masks)
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class RigHitSeamTests(unittest.TestCase):
    """``to_source`` + ``hit_test`` + ``rig_interaction_for_part``, end to end.

    The anchor and pet rect below are computed with the same formulas
    ``_pet_rect``/``_rig_anchor`` use, so this test walks the identical path a
    click takes -- widget point, source point, part, hit group, interaction.
    """

    SCALE = 1.0

    @classmethod
    def setUpClass(cls) -> None:
        import shutil
        import tempfile
        from pathlib import Path

        from PySide6.QtWidgets import QApplication

        from runtime.rig_pack import load_rig
        from runtime.rig_renderer import RigRenderer
        from runtime.tests.test_rig_render_qt import LOGICAL, write_pack

        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = Path(tempfile.mkdtemp(prefix="rig-interaction-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)

        cls.rig = build_rig()
        descriptor = write_pack(cls.tmp, cls.rig)
        cls.loaded = load_rig(descriptor)
        cls.renderer = RigRenderer(descriptor, cls.loaded)
        cls.renderer.part_pixmaps()
        cls.masks = cls.renderer.alpha_masks()
        cls.transforms = RigModel(cls.loaded).solve({})

        # Mirror the window geometry: the logical pet box sits at the bottom of
        # a window that reserves the declared overflow all round.
        pet_size = round(LOGICAL * cls.SCALE)
        over = cls.renderer.overflow_px(cls.SCALE)
        pad = [max(base, math.ceil(v)) for base, v in zip((25, 18, 25, 8), over)]
        cls.width = pet_size + pad[0] + pad[2]
        cls.height = pet_size + pad[1] + pad[3]
        cls.pet_rect = (pad[0], cls.height - pet_size - pad[3], pet_size, pet_size)
        foot_x, foot_y = cls.renderer.foot_fraction
        cls.anchor = (
            cls.pet_rect[0] + foot_x * pet_size,
            cls.pet_rect[1] + foot_y * pet_size,
        )

    # -- helpers -------------------------------------------------------- #

    def widget_point(self, source_x: float, source_y: float) -> tuple[float, float]:
        """Inverse of ``to_source``, so the tests can name a *source* target."""
        world_scale = self.renderer.world_scale(self.SCALE)
        foot = self.renderer.foot_source
        return (
            self.anchor[0] + (source_x - foot[0]) * world_scale,
            self.anchor[1] + (source_y - foot[1]) * world_scale,
        )

    def resolve(self, widget_x: float, widget_y: float):
        source = self.renderer.to_source(
            widget_x,
            widget_y,
            anchor_x=self.anchor[0],
            anchor_y=self.anchor[1],
            scale=self.SCALE,
        )
        part_id = hit_test(self.transforms, self.masks, source[0], source[1])
        return part_id, source

    # -- tests ---------------------------------------------------------- #

    def test_to_source_round_trips_the_widget_mapping(self) -> None:
        for source in ((256.0, 256.0), (60.0, 440.0), (330.0, 90.0)):
            with self.subTest(source=source):
                back = self.resolve(*self.widget_point(*source))[1]
                self.assertAlmostEqual(back[0], source[0], places=6)
                self.assertAlmostEqual(back[1], source[1], places=6)

    def test_a_click_on_each_part_centroid_resolves_to_its_hit_group(self) -> None:
        for part_id, _, _, rect, _ in PARTS:
            with self.subTest(part=part_id):
                centroid = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
                hit, _ = self.resolve(*self.widget_point(*centroid))
                self.assertEqual(hit, part_id)
                self.assertEqual(hit_group_for_part(self.rig, hit), part_id)

    def test_clicking_the_tail_reaches_its_declared_impulse(self) -> None:
        rect = next(p[3] for p in PARTS if p[0] == "tail")
        centroid = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
        hit, source = self.resolve(*self.widget_point(*centroid))
        resolved = rig_interaction_for_part(self.rig, hit)
        self.assertIsNotNone(resolved)
        group, entry = resolved
        self.assertEqual(group, "tail")

        part_rect, pivot = part_geometry(self.rig, hit)
        spec = dict(entry["impulse"])
        spec["scale"] = float(spec.get("scale", 1.0)) * impulse_scale(
            math.dist(source, pivot), part_rect
        )
        driver = RigDriver(build_rig())
        self.assertTrue(driver.apply_impulse(spec))
        driver.advance(16, 16)
        self.assertGreater(max(abs(a) for a in driver.chain_angles("tail")), 0.0)

    def test_a_transparent_pixel_inside_the_pet_rect_hits_nothing(self) -> None:
        """The fixture's parts do not tile the artboard, so this gap is real.

        The click must resolve to no part and no interaction -- which is what
        leaves it as the window drag it also always was.
        """
        gap = (430.0, 120.0)  # right of the head, above the body: bare artboard
        for _, _, _, rect, _ in PARTS:
            self.assertFalse(
                rect[0] <= gap[0] <= rect[0] + rect[2]
                and rect[1] <= gap[1] <= rect[1] + rect[3],
                "the chosen gap point is inside a part rect after all",
            )
        widget_x, widget_y = self.widget_point(*gap)
        pet_x, pet_y, pet_w, pet_h = self.pet_rect
        self.assertTrue(pet_x <= widget_x <= pet_x + pet_w)
        self.assertTrue(pet_y <= widget_y <= pet_y + pet_h)

        hit, _ = self.resolve(widget_x, widget_y)
        self.assertIsNone(hit)
        self.assertIsNone(rig_interaction_for_part(self.rig, hit))

    def test_a_deformed_part_is_hit_where_it_is_drawn(self) -> None:
        """Hit testing uses the solved pose, so a swung tail is pokeable there."""
        from runtime.rig_model import a_map

        transforms = RigModel(self.loaded).solve({"headAngleZ": 25.0})
        rect = next(p[3] for p in PARTS if p[0] == "head")
        centroid = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
        head = next(t for t in transforms if t.part_id == "head")
        moved = a_map(head.matrix, *centroid)
        self.assertGreater(math.dist(moved, centroid), 5.0)

        widget = self.widget_point(*moved)
        source = self.renderer.to_source(
            *widget, anchor_x=self.anchor[0], anchor_y=self.anchor[1], scale=self.SCALE
        )
        self.assertEqual(hit_test(transforms, self.masks, *source), "head")

        # The converse: a point that *was* on the head at rest has left it.
        # (The fixture head is a solid rect, so its own moved centroid stays
        # inside it -- probing a corner is what makes the pose observable.)
        corner = (rect[0] + 2.0, rect[1] + 2.0)
        self.assertEqual(hit_test(self.transforms, self.masks, *corner), "head")
        self.assertIsNone(hit_test(transforms, self.masks, *corner))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
