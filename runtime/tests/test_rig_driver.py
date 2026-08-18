"""Behavioural tests for the Qt-free parameter driver.

These are the tests that describe what "feels alive" means mechanically. Each
one pins a property that is invisible in a still frame and obvious in motion:
no pops, no dead-zone twitch, no runaway envelopes, and a poke that decays on
its own instead of ending on a keyframe.
"""

import math
import unittest
from unittest import mock

from runtime.animation_model import AnimationModel
from runtime.rig_driver import (
    MAX_ENVELOPES,
    Impulse,
    ReactionEnvelope,
    RigDriver,
)
from runtime.rig_pack import SYNTHETIC_FRAME_MS, animation_manifest_from_rig
from runtime.tests.test_rig_schema import sample_rig

#: The rig ticker runs at 16 ms; every timing assertion here uses it so the
#: numbers mean the same thing they will mean on a real desktop.
TICK = 16


def run(driver: RigDriver, duration_ms: int, start_ms: int = 0, tick: int = TICK):
    """Advance *driver* for *duration_ms*, returning (now_ms, param dicts)."""
    now = start_ms
    frames = []
    while now - start_ms < duration_ms:
        now += tick
        frames.append(driver.advance(tick, now))
    return now, frames


def max_step(values) -> float:
    return max((abs(b - a) for a, b in zip(values, values[1:])), default=0.0)


class ManifestBridgeTests(unittest.TestCase):
    """The synthesised manifest has to drive the *real* state machine."""

    def setUp(self) -> None:
        self.rig = sample_rig()
        self.model = AnimationModel(animation_manifest_from_rig(self.rig))

    def test_state_and_activity_routing_still_works(self) -> None:
        self.model.apply_state("THINKING")
        self.assertEqual(self.model.active_clip_name, "thinking")
        self.model.apply_state("WORKING", "searching")
        self.assertEqual(self.model.active_clip_name, "working")
        self.model.apply_state("DISCONNECTED")
        self.assertEqual(self.model.active_clip_name, "idle")

    def test_frames_are_inert_rig_tokens(self) -> None:
        self.model.apply_state("IDLE")
        self.assertEqual(self.model.frame, "@rig/idle")

    def test_one_shot_overlay_clears_after_duration_plus_decay(self) -> None:
        """The overlay must expire when the reaction is visually over.

        ``durationMs + decayMs`` is the honest length: the pose is still
        returning to rest during the decay, so clearing at ``durationMs`` hands
        the pet back to its base clip while the reaction is still on screen.
        """
        self.model.apply_state("IDLE")
        self.assertTrue(self.model.play_overlay("blink"))
        elapsed = 0
        while self.model.overlay_clip_name is not None and elapsed < 5000:
            self.model.advance(SYNTHETIC_FRAME_MS, elapsed)
            elapsed += SYNTHETIC_FRAME_MS
        clip = self.rig["clips"]["blink"]
        expected = clip["durationMs"] + clip["envelope"]["decayMs"]
        self.assertLessEqual(abs(elapsed - expected), SYNTHETIC_FRAME_MS)
        self.assertEqual(self.model.active_clip_name, "idle")

    def test_idle_micro_gating_is_inherited_unchanged(self) -> None:
        self.model.apply_state("WORKING")
        self.assertFalse(self.model.play_idle_micro())
        self.model.apply_state("IDLE")
        self.assertTrue(self.model.play_idle_micro())
        self.assertEqual(self.model.active_clip_name, "blink")

    def test_pulse_ttl_is_inherited_unchanged(self) -> None:
        self.model.apply_state("IDLE")
        self.model.apply_pulse("SUCCESS", 600, now_ms=1000)
        self.assertEqual(self.model.active_clip_name, "success")
        self.model.advance(SYNTHETIC_FRAME_MS, 1700)
        self.assertEqual(self.model.active_clip_name, "idle")


class OscillatorPhaseTests(unittest.TestCase):
    def test_phase_survives_a_round_trip_through_another_clip(self) -> None:
        """Breathing must not restart when the state changes.

        A reference driver stays on ``idle`` while the subject detours through
        ``waiting`` and back. Once the crossfade has finished the two must agree
        exactly, which they can only do if the phase clock is global.
        """
        reference = RigDriver(sample_rig())
        subject = RigDriver(sample_rig())
        for driver in (reference, subject):
            driver.sync_model("idle", None, None, 0)
        now, _ = run(reference, 2000)
        run(subject, 800)
        subject.sync_model("waiting", None, None, 800)
        run(subject, 400, start_ms=800)
        subject.sync_model("idle", None, None, 1200)
        run(subject, 800, start_ms=1200)
        self.assertAlmostEqual(
            subject.advance(TICK, now + TICK)["breath"],
            reference.advance(TICK, now + TICK)["breath"],
            places=9,
        )

    @staticmethod
    def _breath_across_a_clip_change() -> list[float]:
        driver = RigDriver(sample_rig())
        driver.sync_model("idle", None, None, 0)
        now, frames = run(driver, 1200)
        driver.sync_model("working", None, None, now)
        _, after = run(driver, 1200, start_ms=now)
        return [frame["breath"] for frame in frames + after]

    def test_clip_change_does_not_step_the_output(self) -> None:
        """idle and working breathe at different rates and depths.

        Swapping one sine for another of a different amplitude is a step
        discontinuity unless the two are blended, and breathing is the most
        watched thing on the pet.
        """
        self.assertLess(max_step(self._breath_across_a_clip_change()), 0.2)

    def test_the_crossfade_is_what_smooths_the_clip_change(self) -> None:
        with mock.patch("runtime.rig_driver.OSCILLATOR_CROSSFADE_MS", 0.0):
            values = self._breath_across_a_clip_change()
        self.assertGreater(max_step(values), 0.5)


class AntiPopTests(unittest.TestCase):
    """The whole point of putting envelopes in the driver rather than the model."""

    @staticmethod
    def _blink_with_base_switch(driver: RigDriver) -> list[float]:
        driver.sync_model("idle", None, None, 0)
        now, _ = run(driver, 600)
        start = now
        driver.sync_model("idle", None, "blink", now)
        values: list[float] = []
        switched = False
        while now - start < 1200:
            now += TICK
            if not switched and now - start >= 580:
                # 580 ms into a 560 ms blink: the envelope is in its 120 ms
                # decay, so the base clip's eyeOpen oscillator is coming back
                # into view -- exactly the moment a naive implementation snaps
                # from a wide-eyed idle to a narrow-eyed working pose.
                driver.sync_model("working", None, "blink", now)
                switched = True
            values.append(driver.advance(TICK, now)["eyeOpen"])
        return values

    def test_base_clip_switch_mid_blink_never_jumps(self) -> None:
        values = self._blink_with_base_switch(RigDriver(sample_rig()))
        self.assertLessEqual(max_step(values), 0.15)

    def test_the_crossfade_is_what_prevents_the_jump(self) -> None:
        """Guard against this test quietly becoming vacuous.

        With the crossfade disabled the same sequence must fail the same
        assertion; if it does not, the fixture no longer exercises anything.
        """
        with mock.patch("runtime.rig_driver.OSCILLATOR_CROSSFADE_MS", 0.0):
            values = self._blink_with_base_switch(RigDriver(sample_rig()))
        self.assertGreater(max_step(values), 0.15)

    def test_envelope_outlives_the_cleared_overlay(self) -> None:
        """The state machine forgetting a blink must not un-blink the pet.

        ``AnimationModel`` holds one overlay slot, so a reaction interrupted by
        anything else is simply gone from its point of view. The driver keeps
        decaying it, which turns a hard cut into a fade.
        """
        driver = RigDriver(sample_rig())
        driver.sync_model("idle", None, None, 0)
        now, _ = run(driver, 600)
        driver.sync_model("idle", None, "blink", now)
        _, closing = run(driver, 320, start_ms=now)
        now += 320
        self.assertLess(closing[-1]["eyeOpen"], 0.3)

        # The overlay disappears from the model while the eye is still shut.
        driver.sync_model("idle", None, None, now)
        self.assertEqual(len(driver.envelopes), 1)
        _, after = run(driver, 600, start_ms=now)
        values = [frame["eyeOpen"] for frame in closing + after]
        self.assertLessEqual(max_step(values), 0.15)
        self.assertGreater(after[-1]["eyeOpen"], 0.6)
        self.assertEqual(driver.envelopes, ())


class EnvelopeTests(unittest.TestCase):
    def test_weight_rises_holds_and_falls(self) -> None:
        envelope = ReactionEnvelope("blink", 0, attack_ms=40, hold_ms=100, decay_ms=60)
        self.assertEqual(envelope.weight(0), 0.0)
        self.assertAlmostEqual(envelope.weight(20), 0.5, places=9)
        self.assertEqual(envelope.weight(40), 1.0)
        self.assertEqual(envelope.weight(140), 1.0)
        self.assertAlmostEqual(envelope.weight(170), 0.5, places=9)
        self.assertEqual(envelope.weight(200), 0.0)
        self.assertTrue(envelope.finished(200))
        self.assertFalse(envelope.finished(199))

    def test_zero_attack_starts_at_full_weight(self) -> None:
        envelope = ReactionEnvelope("poke", 0, attack_ms=0, hold_ms=50, decay_ms=50)
        self.assertEqual(envelope.weight(1), 1.0)

    def test_envelopes_prune_at_the_cap(self) -> None:
        driver = RigDriver(sample_rig())
        for _ in range(6):
            driver.trigger_reaction("poke", 0)
        self.assertEqual(len(driver.envelopes), MAX_ENVELOPES)

    def test_finished_envelopes_are_dropped_before_the_cap_bites(self) -> None:
        driver = RigDriver(sample_rig())
        driver.trigger_reaction("poke", 0)
        driver.trigger_reaction("poke", 0)
        # poke runs 320 ms + 140 ms decay; both are long gone by 2000 ms.
        driver.trigger_reaction("poke", 2000)
        self.assertEqual(len(driver.envelopes), 1)

    def test_looping_clips_cannot_be_reactions(self) -> None:
        driver = RigDriver(sample_rig())
        self.assertFalse(driver.trigger_reaction("idle", 0))
        self.assertFalse(driver.trigger_reaction("nonexistent", 0))

    def test_override_wins_over_the_base_oscillator(self) -> None:
        driver = RigDriver(sample_rig())
        driver.sync_model("working", None, None, 0)
        now, _ = run(driver, 400)
        # working drives eyeOpen down via an oscillator; the blink track
        # overrides that contribution entirely at full envelope weight.
        base = driver.advance(TICK, now + TICK)["eyeOpen"]
        driver.trigger_reaction("blink", now + TICK)
        _, frames = run(driver, 320, start_ms=now + TICK)
        self.assertLess(frames[-1]["eyeOpen"], base)
        self.assertGreaterEqual(frames[-1]["eyeOpen"], 0.0)


class PointerFollowTests(unittest.TestCase):
    def test_dead_zone_yields_exactly_zero(self) -> None:
        driver = RigDriver(sample_rig())
        driver.sync_model("idle", None, None, 0)
        driver.set_pointer(0.05, 0.05, True, 0)
        _, frames = run(driver, 1000)
        for name in ("headAngleY", "headAngleX", "eyeBallX", "eyeBallY"):
            self.assertEqual(frames[-1][name], 0.0, name)

    def test_just_outside_the_dead_zone_moves_a_little(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_pointer(0.2, 0.0, True, 0)
        _, frames = run(driver, 1500)
        self.assertGreater(frames[-1]["headAngleY"], 0.0)
        self.assertLess(frames[-1]["headAngleY"], 5.0)

    def test_distant_pointer_clamps_to_the_param_range(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_pointer(10.0, 0.0, True, 0)
        _, frames = run(driver, 2000)
        spec = driver.model.params["headAngleY"]
        self.assertEqual(frames[-1]["headAngleY"], spec.maximum)
        self.assertEqual(frames[-1]["eyeBallX"], driver.model.params["eyeBallX"].maximum)

    def test_roll_opposes_yaw_and_the_body_counters_the_head(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_pointer(0.6, 0.0, True, 0)
        _, frames = run(driver, 2000)
        final = frames[-1]
        self.assertGreater(final["headAngleY"], 0.0)
        self.assertLess(final["headAngleZ"], 0.0)
        self.assertLess(final["bodyAngleY"], 0.0)

    def test_eyes_arrive_before_the_head(self) -> None:
        """Eyes lead, head follows. Reversed, the pet looks slow-witted."""
        driver = RigDriver(sample_rig())
        driver.set_pointer(1.0, 0.0, True, 0)
        _, frames = run(driver, 200)
        eye_progress = frames[-1]["eyeBallX"] / 1.35
        head_progress = frames[-1]["headAngleY"] / 22.0
        self.assertGreater(eye_progress, head_progress)

    def test_head_follow_does_not_overshoot(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_pointer(0.5, 0.0, True, 0)
        _, frames = run(driver, 3000)
        target = 22.0 * 0.5
        self.assertLessEqual(max(frame["headAngleY"] for frame in frames), target + 1e-9)

    def test_activity_level_scales_the_gain(self) -> None:
        results = {}
        for level in ("quiet", "normal", "lively"):
            driver = RigDriver(sample_rig(), activity_level=level)
            driver.set_pointer(0.5, 0.0, True, 0)
            _, frames = run(driver, 3000)
            results[level] = frames[-1]["headAngleY"]
        self.assertLess(results["quiet"], results["normal"])
        self.assertLess(results["normal"], results["lively"])
        self.assertAlmostEqual(results["quiet"] / results["normal"], 0.6, places=3)

    def test_a_parked_pointer_stops_being_interesting(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_pointer(0.8, 0.0, True, 0)
        now, frames = run(driver, 3000)
        self.assertGreater(frames[-1]["headAngleY"], 5.0)
        # Same pointer, never moved: after 6 s of stillness the gaze releases
        # over 1.5 s. Re-reporting the identical position must not count as
        # movement, or the pet would stare forever.
        released = frames[-1]
        while now < 9500:
            now += TICK
            driver.set_pointer(0.8, 0.0, True, now)
            released = driver.advance(TICK, now)
        self.assertLess(released["headAngleY"], 0.5)

    def test_moving_the_pointer_re_engages(self) -> None:
        driver = RigDriver(sample_rig())
        now = 0
        while now < 9500:
            now += TICK
            driver.set_pointer(0.8, 0.0, True, now)
            driver.advance(TICK, now)
        driver.set_pointer(0.85, 0.05, True, now)
        _, frames = run(driver, 2000, start_ms=now)
        self.assertGreater(frames[-1]["headAngleY"], 5.0)

    def test_absent_pointer_returns_to_rest(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_pointer(0.8, 0.0, True, 0)
        now, _ = run(driver, 2000)
        driver.set_pointer(0.8, 0.0, False, now)
        _, frames = run(driver, 2000, start_ms=now)
        self.assertAlmostEqual(frames[-1]["headAngleY"], 0.0, places=3)


class ImpulseTests(unittest.TestCase):
    def test_chain_impulse_moves_next_tick_and_settles_within_1500ms(self) -> None:
        """A poke is velocity, so it shows up immediately and ends on its own.

        No sync_model call here: with no clip driving the tail oscillator the
        chain would sit dead still, so every degree measured is the impulse.
        """
        driver = RigDriver(sample_rig())
        driver.apply_impulse({"chain": "tail", "chainAngularVel": 220.0})
        first = driver.advance(TICK, TICK)
        self.assertGreater(abs(first["tail0"]), 0.0)
        self.assertGreater(abs(first["tail3"]), 0.0)
        _, frames = run(driver, 1500, start_ms=TICK)
        for name in ("tail0", "tail1", "tail2", "tail3"):
            self.assertLess(abs(frames[-1][name]), 0.5, name)

    def test_the_tip_whips_harder_than_the_root(self) -> None:
        driver = RigDriver(sample_rig())
        driver.apply_impulse(Impulse(chain="tail", chain_angular_vel=120.0))
        _, frames = run(driver, 200)
        peak = {
            name: max(abs(frame[name]) for frame in frames)
            for name in ("tail0", "tail3")
        }
        # distribution is 1.0 at the root and 0.55 at the tip, but the tip's
        # spring is the softest, so it travels furthest per unit of kick.
        self.assertGreater(peak["tail3"] / 0.55, peak["tail0"] / 1.0)

    def test_two_pokes_stack(self) -> None:
        single = RigDriver(sample_rig())
        single.apply_impulse({"chain": "tail", "chainAngularVel": 40.0})
        _, one = run(single, 300)

        double = RigDriver(sample_rig())
        double.apply_impulse({"chain": "tail", "chainAngularVel": 40.0})
        double.apply_impulse({"chain": "tail", "chainAngularVel": 40.0})
        _, two = run(double, 300)

        self.assertGreater(
            max(abs(frame["tail0"]) for frame in two),
            max(abs(frame["tail0"]) for frame in one) * 1.5,
        )

    def test_param_impulses_decay_back_to_the_default(self) -> None:
        driver = RigDriver(sample_rig())
        driver.apply_impulse({"param": "headAngleZ", "angularVel": 42.0})
        first = driver.advance(TICK, TICK)
        self.assertNotEqual(first["headAngleZ"], 0.0)
        _, frames = run(driver, 2000, start_ms=TICK)
        self.assertAlmostEqual(frames[-1]["headAngleZ"], 0.0, places=3)

    def test_impulse_scale_carries_the_distance_falloff(self) -> None:
        near = RigDriver(sample_rig())
        near.apply_impulse({"chain": "tail", "chainAngularVel": 100.0, "scale": 1.0})
        far = RigDriver(sample_rig())
        far.apply_impulse({"chain": "tail", "chainAngularVel": 100.0, "scale": 0.25})
        _, strong = run(near, 200)
        _, weak = run(far, 200)
        self.assertGreater(
            max(abs(frame["tail3"]) for frame in strong),
            max(abs(frame["tail3"]) for frame in weak) * 2.0,
        )

    def test_unknown_targets_are_ignored(self) -> None:
        driver = RigDriver(sample_rig())
        self.assertFalse(driver.apply_impulse({"chain": "fin", "chainAngularVel": 10.0}))
        self.assertFalse(driver.apply_impulse({}))


class RootMotionTests(unittest.TestCase):
    def test_dragging_leans_the_root_against_the_motion(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_root_motion(400.0, 0.0, dragging=True)
        _, frames = run(driver, 600)
        self.assertLess(frames[-1]["rootLeanZ"], -1.0)

    def test_chains_swing_from_drag_acceleration(self) -> None:
        """Throwing the window is what makes the hair and tail fly."""
        still = RigDriver(sample_rig())
        _, quiet = run(still, 400)
        driver = RigDriver(sample_rig())
        driver.set_root_motion(600.0, 0.0, dragging=True)
        _, frames = run(driver, 400)
        self.assertEqual(max(abs(frame["tail3"]) for frame in quiet), 0.0)
        self.assertGreater(max(abs(frame["tail3"]) for frame in frames), 0.5)

    def test_release_coasts_instead_of_stopping_dead(self) -> None:
        driver = RigDriver(sample_rig())
        driver.set_root_motion(500.0, 0.0, dragging=True)
        now, _ = run(driver, 400)
        driver.set_root_motion(500.0, 0.0, dragging=False)
        _, frames = run(driver, 1500, start_ms=now)
        self.assertGreater(abs(frames[2]["rootLeanZ"]), 0.5)
        self.assertLess(abs(frames[-1]["rootLeanZ"]), 0.1)


class ReducedMotionTests(unittest.TestCase):
    def test_oscillators_are_silenced(self) -> None:
        driver = RigDriver(sample_rig(), reduced_motion=True)
        driver.sync_model("idle", None, None, 0)
        _, frames = run(driver, 2000)
        for frame in frames:
            self.assertEqual(frame["breath"], 0.0)
            self.assertEqual(frame["eyeOpen"], 1.0)
            self.assertEqual(frame["tail0"], 0.0)

    def test_pointer_follow_is_disabled(self) -> None:
        driver = RigDriver(sample_rig(), reduced_motion=True)
        driver.set_pointer(1.0, 1.0, True, 0)
        _, frames = run(driver, 1000)
        for name in ("headAngleY", "headAngleX", "headAngleZ", "eyeBallX"):
            self.assertEqual(frames[-1][name], 0.0, name)

    def test_impulses_are_ignored(self) -> None:
        driver = RigDriver(sample_rig(), reduced_motion=True)
        self.assertFalse(
            driver.apply_impulse({"chain": "tail", "chainAngularVel": 400.0})
        )
        _, frames = run(driver, 500)
        self.assertEqual(frames[-1]["tail3"], 0.0)

    def test_reaction_tracks_still_play(self) -> None:
        """Reactions are semantic feedback, not decoration.

        The frame packs already play ``head_pat``/``poke`` under reduced
        motion; suppressing them here would make the pet stop acknowledging
        clicks for exactly the users least able to tolerate ambiguity.
        """
        driver = RigDriver(sample_rig(), reduced_motion=True)
        driver.sync_model("idle", None, "poke", 0)
        _, frames = run(driver, 200)
        self.assertGreater(max(frame["mouthOpen"] for frame in frames), 0.2)

        blinker = RigDriver(sample_rig(), reduced_motion=True)
        blinker.sync_model("idle", None, "blink", 0)
        _, blink_frames = run(blinker, 320)
        self.assertLess(min(frame["eyeOpen"] for frame in blink_frames), 0.3)

    def test_enabling_it_settles_motion_already_in_flight(self) -> None:
        driver = RigDriver(sample_rig())
        driver.sync_model("idle", None, None, 0)
        driver.apply_impulse({"chain": "tail", "chainAngularVel": 300.0})
        driver.set_pointer(1.0, 0.0, True, 0)
        now, _ = run(driver, 300)
        driver.set_reduced_motion(True)
        frame = driver.advance(TICK, now + TICK)
        self.assertEqual(frame["tail3"], 0.0)
        self.assertEqual(frame["headAngleY"], 0.0)
        self.assertEqual(frame["breath"], 0.0)


class OutputContractTests(unittest.TestCase):
    def test_every_declared_param_is_returned_and_in_range(self) -> None:
        driver = RigDriver(sample_rig())
        driver.sync_model("working", None, "head_pat", 0)
        driver.set_pointer(1.0, -1.0, True, 0)
        driver.set_root_motion(700.0, -300.0, dragging=True)
        driver.apply_impulse({"chain": "tail", "chainAngularVel": 400.0})
        _, frames = run(driver, 1200)
        for frame in frames:
            self.assertEqual(set(frame), set(driver.model.params))
            for name, value in frame.items():
                spec = driver.model.params[name]
                self.assertFalse(math.isnan(value), name)
                self.assertGreaterEqual(value, spec.minimum, name)
                self.assertLessEqual(value, spec.maximum, name)

    def test_output_feeds_the_solver(self) -> None:
        driver = RigDriver(sample_rig())
        driver.sync_model("idle", None, None, 0)
        _, frames = run(driver, 500)
        transforms = driver.model.solve(frames[-1])
        self.assertEqual(len(transforms), len(driver.model.parts))

    def test_zero_elapsed_tick_is_a_no_op(self) -> None:
        driver = RigDriver(sample_rig())
        driver.sync_model("idle", None, None, 0)
        now, _ = run(driver, 500)
        before = driver.advance(TICK, now + TICK)
        after = driver.advance(0, now + TICK)
        self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
