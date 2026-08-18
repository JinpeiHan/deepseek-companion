import math
import unittest

from runtime.rig_model import (
    IDENTITY,
    a_bbox,
    a_invert,
    a_map,
    a_multiply,
    a_rotate,
    a_scale,
    a_shear,
    a_translate,
)


def reference_multiply(outer, inner):
    """Plain 3x3 matmul, written independently of the packed implementation."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    left = [[a1, c1, e1], [b1, d1, f1], [0.0, 0.0, 1.0]]
    right = [[a2, c2, e2], [b2, d2, f2], [0.0, 0.0, 1.0]]
    out = [
        [sum(left[r][k] * right[k][col] for k in range(3)) for col in range(3)]
        for r in range(3)
    ]
    return (out[0][0], out[1][0], out[0][1], out[1][1], out[0][2], out[1][2])


class AffineMultiplyTests(unittest.TestCase):
    def test_matches_a_reference_matmul(self) -> None:
        left = a_multiply(a_translate(12.0, -4.0), a_rotate(31.0))
        right = a_multiply(a_scale(1.4, 0.8), a_shear(0.2, -0.1))
        expected = reference_multiply(left, right)
        for got, want in zip(a_multiply(left, right), expected):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_is_associative(self) -> None:
        first = a_translate(9.0, 3.0)
        second = a_rotate(-22.5)
        third = a_scale(0.75, 1.3)
        left = a_multiply(a_multiply(first, second), third)
        right = a_multiply(first, a_multiply(second, third))
        for got, want in zip(left, right):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_identity_is_neutral(self) -> None:
        matrix = a_multiply(a_rotate(17.0), a_translate(5.0, -2.0))
        for got, want in zip(a_multiply(IDENTITY, matrix), matrix):
            self.assertAlmostEqual(got, want, delta=1e-12)
        for got, want in zip(a_multiply(matrix, IDENTITY), matrix):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_inner_matrix_is_applied_first(self) -> None:
        # Translate then rotate: the translation itself gets rotated.
        composed = a_multiply(a_rotate(90.0), a_translate(10.0, 0.0))
        x, y = a_map(composed, 0.0, 0.0)
        self.assertAlmostEqual(x, 0.0, delta=1e-9)
        self.assertAlmostEqual(y, 10.0, delta=1e-9)


class AffinePrimitiveTests(unittest.TestCase):
    def test_rotation_matches_qtransform_packing(self) -> None:
        matrix = a_rotate(30.0)
        cos = math.cos(math.radians(30.0))
        sin = math.sin(math.radians(30.0))
        self.assertAlmostEqual(matrix[0], cos, delta=1e-12)  # m11
        self.assertAlmostEqual(matrix[1], sin, delta=1e-12)  # m12
        self.assertAlmostEqual(matrix[2], -sin, delta=1e-12)  # m21
        self.assertAlmostEqual(matrix[3], cos, delta=1e-12)  # m22

    def test_shear_is_x_plus_kx_times_y(self) -> None:
        x, y = a_map(a_shear(0.5, 0.0), 0.0, 10.0)
        self.assertAlmostEqual(x, 5.0, delta=1e-12)
        self.assertAlmostEqual(y, 10.0, delta=1e-12)


class AffineInverseTests(unittest.TestCase):
    def test_rotation_round_trips_a_point(self) -> None:
        matrix = a_rotate(37.0)
        inverse = a_invert(matrix)
        self.assertIsNotNone(inverse)
        x, y = a_map(matrix, 13.5, -8.25)
        bx, by = a_map(inverse, x, y)
        self.assertAlmostEqual(bx, 13.5, delta=1e-9)
        self.assertAlmostEqual(by, -8.25, delta=1e-9)

    def test_composite_round_trips_a_point(self) -> None:
        matrix = a_multiply(
            a_multiply(a_translate(64.0, 20.0), a_rotate(-12.0)), a_scale(1.8, 0.4)
        )
        inverse = a_invert(matrix)
        self.assertIsNotNone(inverse)
        x, y = a_map(matrix, -3.0, 7.0)
        bx, by = a_map(inverse, x, y)
        self.assertAlmostEqual(bx, -3.0, delta=1e-9)
        self.assertAlmostEqual(by, 7.0, delta=1e-9)

    def test_degenerate_scale_returns_none(self) -> None:
        self.assertIsNone(a_invert(a_scale(1.0, 0.0)))
        self.assertIsNone(a_invert(a_scale(0.0, 0.0)))
        self.assertIsNone(a_invert((1.0, 2.0, 2.0, 4.0, 0.0, 0.0)))


class AffineBBoxTests(unittest.TestCase):
    def test_matches_the_four_corner_hull(self) -> None:
        matrix = a_multiply(a_translate(30.0, 10.0), a_rotate(25.0))
        rect = (4.0, 6.0, 40.0, 18.0)
        corners = [
            a_map(matrix, rect[0], rect[1]),
            a_map(matrix, rect[0] + rect[2], rect[1]),
            a_map(matrix, rect[0], rect[1] + rect[3]),
            a_map(matrix, rect[0] + rect[2], rect[1] + rect[3]),
        ]
        left = min(p[0] for p in corners)
        top = min(p[1] for p in corners)
        right = max(p[0] for p in corners)
        bottom = max(p[1] for p in corners)
        got = a_bbox(matrix, rect)
        for value, want in zip(got, (left, top, right - left, bottom - top)):
            self.assertAlmostEqual(value, want, delta=1e-12)

    def test_identity_returns_the_rect_itself(self) -> None:
        rect = (2.0, 3.0, 11.0, 7.0)
        for got, want in zip(a_bbox(IDENTITY, rect), rect):
            self.assertAlmostEqual(got, want, delta=1e-12)

    def test_rotation_grows_the_hull(self) -> None:
        rect = (0.0, 0.0, 10.0, 10.0)
        _, _, w, h = a_bbox(a_rotate(45.0), rect)
        self.assertAlmostEqual(w, 10.0 * math.sqrt(2.0), delta=1e-9)
        self.assertAlmostEqual(h, 10.0 * math.sqrt(2.0), delta=1e-9)


if __name__ == "__main__":
    unittest.main()
