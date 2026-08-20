import unittest

from runtime.rig_model import (
    CHAIN_STIFFNESS_FALLOFF,
    SUBSTEP_S,
    ChainSolver,
    Spring1D,
)


def tail_chain(**overrides) -> ChainSolver:
    kwargs = dict(
        stiffness=90.0,
        damping_ratio=0.45,
        amplitude_deg=16.0,
        distribution=[1.0, 0.85, 0.7, 0.55],
        lag_per_segment_ms=26.0,
        max_deg=24.0,
    )
    kwargs.update(overrides)
    return ChainSolver(4, **kwargs)


class SpringDeterminismTests(unittest.TestCase):
    """The whole rig is only testable because the spring is frame-rate free."""

    def test_one_hundred_ms_equals_ten_ten_ms_steps(self) -> None:
        coarse = Spring1D(stiffness=90.0, damping_ratio=0.45)
        fine = Spring1D(stiffness=90.0, damping_ratio=0.45)
        coarse.step(1.0, 0.100)
        for _ in range(10):
            fine.step(1.0, 0.010)
        self.assertAlmostEqual(coarse.value, fine.value, delta=1e-9)
        self.assertAlmostEqual(coarse.velocity, fine.velocity, delta=1e-9)

    def test_accumulator_stays_consistent_over_many_odd_steps(self) -> None:
        coarse = Spring1D(stiffness=140.0, damping_ratio=0.7)
        fine = Spring1D(stiffness=140.0, damping_ratio=0.7)
        for _ in range(6):
            coarse.step(0.5, 0.050)
        for _ in range(60):
            fine.step(0.5, 0.005)
        self.assertAlmostEqual(coarse.value, fine.value, delta=1e-9)
        self.assertAlmostEqual(coarse.velocity, fine.velocity, delta=1e-9)

    def test_substep_is_a_quarter_of_a_millisecond_grid(self) -> None:
        self.assertAlmostEqual(SUBSTEP_S, 1.0 / 240.0, delta=1e-12)


class SpringBehaviourTests(unittest.TestCase):
    def test_critical_damping_never_overshoots(self) -> None:
        spring = Spring1D(stiffness=120.0, damping_ratio=1.0)
        peak = 0.0
        for _ in range(200):
            spring.step(1.0, 0.010)
            peak = max(peak, spring.value)
        self.assertLessEqual(peak, 1.0 + 1e-9)
        self.assertAlmostEqual(spring.value, 1.0, delta=1e-3)

    def test_under_damped_overshoots_then_converges(self) -> None:
        spring = Spring1D(stiffness=200.0, damping_ratio=0.25)
        peak = 0.0
        for _ in range(100):
            spring.step(1.0, 0.010)
            peak = max(peak, spring.value)
        self.assertGreater(peak, 1.05)
        for _ in range(200):
            spring.step(1.0, 0.010)
        self.assertAlmostEqual(spring.value, 1.0, delta=1e-2)

    def test_huge_dt_is_clamped_and_does_not_explode(self) -> None:
        spring = Spring1D(stiffness=200.0, damping_ratio=0.25)
        for _ in range(20):
            spring.step(1.0, 2.0)
        self.assertLess(abs(spring.value), 3.0)
        self.assertAlmostEqual(spring.value, 1.0, delta=1e-2)

    def test_kick_then_free_decay_returns_to_zero(self) -> None:
        spring = Spring1D(stiffness=140.0, damping_ratio=0.35)
        spring.kick(9.0)
        self.assertAlmostEqual(spring.velocity, 9.0, delta=1e-12)
        moved = False
        for _ in range(200):
            spring.step(0.0, 0.010)
            moved = moved or abs(spring.value) > 0.1
        self.assertTrue(moved)
        self.assertAlmostEqual(spring.value, 0.0, delta=1e-3)
        self.assertAlmostEqual(spring.velocity, 0.0, delta=1e-2)

    def test_snap_zeroes_velocity_and_pending_substeps(self) -> None:
        spring = Spring1D(stiffness=140.0, damping_ratio=0.35)
        spring.step(1.0, 0.007)
        spring.snap(0.25)
        self.assertEqual(spring.value, 0.25)
        self.assertEqual(spring.velocity, 0.0)
        twin = Spring1D(stiffness=140.0, damping_ratio=0.35, value=0.25)
        for _ in range(5):
            spring.step(1.0, 0.010)
            twin.step(1.0, 0.010)
        self.assertAlmostEqual(spring.value, twin.value, delta=1e-12)


class ChainSolverTests(unittest.TestCase):
    def test_is_frame_rate_independent(self) -> None:
        coarse = tail_chain()
        fine = tail_chain()
        coarse.step(1.0, 0.100)
        for _ in range(10):
            fine.step(1.0, 0.010)
        for got, want in zip(coarse.angles, fine.angles):
            self.assertAlmostEqual(got, want, delta=1e-9)

    def test_distal_segments_are_floppier(self) -> None:
        chain = tail_chain()
        stiffnesses = [spring.stiffness for spring in chain.springs]
        self.assertEqual(stiffnesses, sorted(stiffnesses, reverse=True))
        self.assertAlmostEqual(
            stiffnesses[1], 90.0 * (1.0 - CHAIN_STIFFNESS_FALLOFF), delta=1e-12
        )

    def test_the_tip_lags_behind_the_root(self) -> None:
        chain = tail_chain()
        for _ in range(6):  # 60 ms: shorter than the tip's 78 ms delay
            chain.step(1.0, 0.010)
        angles = chain.angles
        self.assertGreater(angles[0], 0.5)
        self.assertGreater(angles[0], angles[1])
        self.assertGreater(angles[1], angles[2])
        self.assertGreater(angles[2], angles[3])
        self.assertAlmostEqual(angles[3], 0.0, delta=1e-9)

    def test_zero_lag_moves_every_segment_together(self) -> None:
        chain = tail_chain(lag_per_segment_ms=0.0)
        for _ in range(6):
            chain.step(1.0, 0.010)
        self.assertTrue(all(angle > 0.0 for angle in chain.angles))

    def test_steady_state_follows_the_distribution(self) -> None:
        chain = tail_chain()
        for _ in range(300):
            chain.step(1.0, 0.010)
        for angle, share in zip(chain.angles, (1.0, 0.85, 0.7, 0.55)):
            self.assertAlmostEqual(angle, 16.0 * share, delta=1e-2)

    def test_output_is_clamped_to_max_deg(self) -> None:
        chain = tail_chain(amplitude_deg=90.0, damping_ratio=0.15)
        for _ in range(400):
            chain.step(1.0, 0.010)
            for angle in chain.angles:
                self.assertLessEqual(abs(angle), 24.0 + 1e-9)

    def test_root_accel_swings_the_chain_with_no_driver(self) -> None:
        chain = tail_chain()
        for _ in range(20):
            chain.step(0.0, 0.010, root_accel=(6.0, 0.0))
        # Acceleration to the right leaves the tail trailing to the left.
        self.assertTrue(all(angle < -0.5 for angle in chain.angles))

    def test_root_accel_reaches_every_segment_including_the_tip(self) -> None:
        chain = tail_chain()
        for _ in range(60):
            chain.step(0.0, 0.010, root_accel=(0.0, 8.0))
        self.assertTrue(all(abs(angle) > 0.5 for angle in chain.angles))

    def test_kick_whips_the_tip_hardest_then_decays(self) -> None:
        chain = tail_chain()
        chain.kick(-40.0)
        self.assertLess(chain.springs[0].velocity, 0.0)
        peak = 0.0
        for _ in range(400):
            chain.step(0.0, 0.010)
            peak = max(peak, max(abs(a) for a in chain.angles))
        self.assertGreater(peak, 1.0)
        for angle in chain.angles:
            self.assertAlmostEqual(angle, 0.0, delta=0.5)

    def test_snap_to_rest_clears_history_and_velocity(self) -> None:
        chain = tail_chain()
        for _ in range(30):
            chain.step(1.0, 0.010)
        chain.snap_to_rest()
        self.assertEqual(chain.angles, (0.0, 0.0, 0.0, 0.0))
        self.assertTrue(all(s.velocity == 0.0 for s in chain.springs))
        fresh = tail_chain()
        for _ in range(5):
            chain.step(1.0, 0.010)
            fresh.step(1.0, 0.010)
        for got, want in zip(chain.angles, fresh.angles):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_a_huge_dt_is_clamped(self) -> None:
        chain = tail_chain()
        for _ in range(50):
            chain.step(1.0, 5.0)
        for angle in chain.angles:
            self.assertLessEqual(abs(angle), 24.0 + 1e-9)

    def test_a_short_distribution_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChainSolver(4, distribution=[1.0, 0.5])

    def test_zero_segments_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ChainSolver(0)


if __name__ == "__main__":
    unittest.main()
