"""Tests for CIE xy conversions used by cross-mode blending."""

from __future__ import annotations

import pytest

from custom_components.light_man.colorspace import (
    kelvin_to_xy,
    lerp_oklab,
    lerp_xy,
    oklab_to_xy,
    rgb_to_xy,
    xy_to_oklab,
)


@pytest.mark.parametrize(
    ("kelvin", "expected"),
    [
        (2700, (0.4599, 0.4106)),  # warm white on the Planckian locus
        (2000, (0.5267, 0.4133)),
        (6500, (0.3135, 0.3237)),
    ],
)
def test_kelvin_to_xy_on_the_locus(
    kelvin: float, expected: tuple[float, float]
) -> None:
    x, y = kelvin_to_xy(kelvin)
    assert x == pytest.approx(expected[0], abs=0.003)
    assert y == pytest.approx(expected[1], abs=0.003)


def test_kelvin_to_xy_clamps_range() -> None:
    assert kelvin_to_xy(500) == kelvin_to_xy(1667)
    assert kelvin_to_xy(99999) == kelvin_to_xy(25000)


def test_rgb_to_xy_primaries_and_white() -> None:
    assert rgb_to_xy((255, 0, 0)) == pytest.approx((0.64, 0.33), abs=0.001)
    assert rgb_to_xy((255, 255, 255)) == pytest.approx((0.3127, 0.329), abs=0.001)
    assert rgb_to_xy((0, 0, 0)) == (0.3127, 0.329)
    assert rgb_to_xy((5, 5, 5)) == pytest.approx((0.3127, 0.329), abs=0.001)


def test_lerp_xy() -> None:
    assert lerp_xy((0.2, 0.4), (0.4, 0.2), 0.5) == (0.3, 0.3)


def test_oklab_round_trip_and_endpoints() -> None:
    for xy in ((0.4593, 0.4107), (0.248, 0.1901), (0.3127, 0.329), (0.64, 0.33)):
        assert oklab_to_xy(xy_to_oklab(xy)) == pytest.approx(xy, abs=2e-4)
    a, b = kelvin_to_xy(2700), rgb_to_xy((155, 123, 232))
    assert lerp_oklab(a, b, 0.0) == pytest.approx(a, abs=2e-4)
    assert lerp_oklab(a, b, 1.0) == pytest.approx(b, abs=2e-4)


def test_oklab_path_is_perceptually_even_not_the_xy_chord() -> None:
    a, b = kelvin_to_xy(2700), rgb_to_xy((155, 123, 232))
    lab = [xy_to_oklab(lerp_oklab(a, b, t / 4)) for t in range(5)]
    steps = [
        sum((p - q) ** 2 for p, q in zip(lab[i], lab[i + 1], strict=True)) ** 0.5
        for i in range(4)
    ]
    assert max(steps) - min(steps) < 0.01  # equal perceptual steps
    assert lerp_oklab(a, b, 0.5) != lerp_xy(a, b, 0.5)


def test_oklab_degenerate_inputs() -> None:
    assert xy_to_oklab((0.3, 0.0))  # y clamped, no division by zero
    assert oklab_to_xy((0.0, 0.0, 0.0)) == (0.3127, 0.329)
