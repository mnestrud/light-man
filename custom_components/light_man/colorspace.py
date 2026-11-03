"""CIE 1931 xy chromaticity for colour temperatures and sRGB colours.

Hue bulbs fade between colours along their own path, which bows well away from
a straight line for cross-hue changes (measured 2026-11-03: 0.098 off the chord in
a 60 s CT → purple fade, whatever form the endpoints were sent in). When the
engine blends across colour modes it therefore interpolates itself — in OKLab, so
equal steps look equally large and the hue does not drift through odd tints — and
sends one short xy segment per tick, so the bulb's own curvature stays negligible.

Only chromaticity is blended (luminance is fixed at Y = 1); brightness is blended
separately by the engine.

Pure functions; no Home Assistant imports.
"""

from __future__ import annotations

KELVIN_MIN = 1667.0
KELVIN_MAX = 25000.0


def kelvin_to_xy(kelvin: float) -> tuple[float, float]:
    """Planckian-locus chromaticity (Kang et al. 2002 cubic approximation)."""
    t = min(max(kelvin, KELVIN_MIN), KELVIN_MAX)
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return (round(x, 4), round(y, 4))


def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_xy(rgb: tuple[int, int, int]) -> tuple[float, float]:
    """Convert sRGB (D65) to xy; black maps to the D65 white point."""
    r, g, b = (_linear(c) for c in rgb)
    big_x = 0.4124 * r + 0.3576 * g + 0.1805 * b
    big_y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    big_z = 0.0193 * r + 0.1192 * g + 0.9505 * b
    total = big_x + big_y + big_z
    if total <= 0:
        return (0.3127, 0.3290)
    return (round(big_x / total, 4), round(big_y / total, 4))


def lerp_xy(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    """Straight-line interpolation in xy."""
    return (round(a[0] + (b[0] - a[0]) * t, 4), round(a[1] + (b[1] - a[1]) * t, 4))


# OKLab (Björn Ottosson, 2020), from CIE XYZ (D65).
_XYZ_TO_LMS = (
    (0.8189330101, 0.3618667424, -0.1288597137),
    (0.0329845436, 0.9293118715, 0.0361456387),
    (0.0482003018, 0.2643662691, 0.6338517070),
)
_LMS_TO_LAB = (
    (0.2104542553, 0.7936177850, -0.0040720468),
    (1.9779984951, -2.4285922050, 0.4505937099),
    (0.0259040371, 0.7827717662, -0.8086757660),
)

type _Vec = tuple[float, float, float]
type _Mat = tuple[_Vec, _Vec, _Vec]


def _mul(m: _Mat, v: _Vec) -> _Vec:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _inverse(m: _Mat) -> _Mat:
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )


_LMS_TO_XYZ = _inverse(_XYZ_TO_LMS)
_LAB_TO_LMS = _inverse(_LMS_TO_LAB)


def _cbrt(v: float) -> float:
    root = float(abs(v) ** (1 / 3))
    return root if v >= 0 else -root


def xy_to_oklab(xy: tuple[float, float]) -> _Vec:
    """Chromaticity (at luminance Y = 1) to OKLab."""
    x, y = xy
    y = max(y, 1e-6)
    xyz = (x / y, 1.0, (1 - x - y) / y)
    long_, medium, short = _mul(_XYZ_TO_LMS, xyz)
    return _mul(_LMS_TO_LAB, (_cbrt(long_), _cbrt(medium), _cbrt(short)))


def oklab_to_xy(lab: _Vec) -> tuple[float, float]:
    """OKLab back to chromaticity."""
    l_, m_, s_ = _mul(_LAB_TO_LMS, lab)
    big_x, big_y, big_z = _mul(_LMS_TO_XYZ, (l_**3, m_**3, s_**3))
    total = big_x + big_y + big_z
    if total <= 0:
        return (0.3127, 0.3290)
    return (round(big_x / total, 4), round(big_y / total, 4))


def lerp_oklab(
    a: tuple[float, float], b: tuple[float, float], t: float
) -> tuple[float, float]:
    """Interpolate two chromaticities perceptually (straight line in OKLab)."""
    la, lb = xy_to_oklab(a), xy_to_oklab(b)
    mix = (
        la[0] + (lb[0] - la[0]) * t,
        la[1] + (lb[1] - la[1]) * t,
        la[2] + (lb[2] - la[2]) * t,
    )
    return oklab_to_xy(mix)
