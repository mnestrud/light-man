"""Tests for the Hue 0xFC03 frame codec, against frames captured from the live house."""

from __future__ import annotations

import pytest

from custom_components.light_man.hue_frame import HueFrame, decode, decode_hex, encode

# Frames logged by Z2M on the live system (2026-11-02/03).
TICK = bytes([22, 0, 76, 199, 1, 10, 0])  # brightness 76, mirek 455, 1.0 s
ZONE_CLEAR = bytes([17, 0, 0, 15, 0])  # off, 1.5 s
OCCUPANCY_STAGE = bytes([27, 0, 1, 76, 88, 56, 27, 75, 15, 0])  # on, br, xy, 1.5 s
STATE_REPORT = "0700004c8001"  # philips_raw: off, brightness 76, mirek 384


def test_decode_tick() -> None:
    frame = decode(TICK)
    assert frame == HueFrame(brightness=76, mirek=455, fade_ds=10)
    assert frame.fade_s == 1.0


def test_decode_zone_clear() -> None:
    assert decode(ZONE_CLEAR) == HueFrame(on=False, fade_ds=15)


def test_decode_occupancy_stage_xy() -> None:
    frame = decode(OCCUPANCY_STAGE)
    assert frame.on is True
    assert frame.brightness == 76
    assert frame.xy == (round(14424 / 0xFFFF, 4), round(19227 / 0xFFFF, 4))
    assert frame.fade_s == 1.5


def test_decode_state_report_hex() -> None:
    frame = decode_hex(STATE_REPORT)
    assert frame == HueFrame(on=False, brightness=76, mirek=384)
    assert frame.fade_s is None


@pytest.mark.parametrize("raw", [TICK, ZONE_CLEAR, bytes.fromhex(STATE_REPORT)])
def test_round_trip(raw: bytes) -> None:
    assert encode(decode(raw)) == raw


def test_round_trip_xy_within_quantisation() -> None:
    frame = decode(OCCUPANCY_STAGE)
    assert decode(encode(frame)) == frame


def test_decode_effects_and_gradient_wire_order() -> None:
    # flags: effect_type | gradient_params | effect_speed | gradient_colors
    flags = (0x0020 | 0x0040 | 0x0080 | 0x0100).to_bytes(2, "little")
    # wire order: effect_type, gradient_colors (size-prefixed), effect_speed, params
    raw = flags + bytes([0x01, 2, 0xAA, 0xBB, 128, 8, 0])
    frame = decode(raw)
    assert frame.effect_type == 1
    assert frame.gradient_colors == bytes([0xAA, 0xBB])
    assert frame.effect_speed == 128
    assert frame.gradient_params == (8, 0)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x00", "truncated"),
        (bytes([0x02, 0x00]), "truncated"),
        (bytes([0x00, 0x02]), "unknown flag"),
        (bytes([0x02, 0x00, 5, 9]), "trailing"),
    ],
)
def test_decode_rejects_bad_frames(raw: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        decode(raw)


@pytest.mark.parametrize(
    "frame",
    [
        HueFrame(brightness=0),
        HueFrame(brightness=255),
        HueFrame(xy=(1.2, 0.3)),
        HueFrame(effect_type=1),
        HueFrame(gradient_params=(8, 0)),
    ],
)
def test_encode_rejects_unsupported_or_out_of_range(frame: HueFrame) -> None:
    with pytest.raises(ValueError):
        encode(frame)


def test_encode_on_true() -> None:
    assert encode(HueFrame(on=True, fade_ds=4)) == bytes([0x11, 0x00, 1, 4, 0])
