"""Tests for Z2M failure parsing, on real lines from the live logs."""

from __future__ import annotations

import pytest

from custom_components.light_man.z2m import (
    KIND_BUSY,
    KIND_DELIVERY_FAILED,
    KIND_OTHER,
    KIND_UNCONFIRMED,
    classify_failure,
    parse_publish_failure,
    set_topic_for,
    z2m_base_topic,
)

BUSY = (
    "Publish 'set' 'brightness' to 'zgb_upstairs_bath' failed: 'Error: Command 16 "
    "manuSpecificPhilips2.multiColor({...}) "
    "failed (~x~> [ZCL GROUP groupId=16] Failed to send with status=BUSY.)'"
)
TIMEOUT = (
    "Publish 'set' 'brightness' to 'Michael Closet Overhead Light' failed: 'Error: ZCL "
    "command 0x001788010d68c587/11 manuSpecificPhilips2.multiColor(...) failed "
    '({"target":19153} timed out after 10000ms)\''
)
MULTICAST = (
    "Publish 'set' 'state' to 'zgb_kitchen' failed: 'Error: Delivery of MULTICAST "
    "failed for 'zgb_kitchen'.'"
)


@pytest.mark.parametrize(
    ("message", "name", "kind"),
    [
        (BUSY, "zgb_upstairs_bath", KIND_BUSY),
        (TIMEOUT, "Michael Closet Overhead Light", KIND_UNCONFIRMED),
        (MULTICAST, "zgb_kitchen", KIND_DELIVERY_FAILED),
    ],
)
def test_parse_real_lines(message: str, name: str, kind: str) -> None:
    failure = parse_publish_failure(message)
    assert failure is not None
    assert failure.name == name
    assert failure.kind == kind


@pytest.mark.parametrize(
    "message",
    [
        "MQTT failed to connect, exiting... ()",
        "Failed to poll onOff from Primary Bath Toilet Light (timed out)",
        "",
    ],
)
def test_unrelated_lines_are_ignored(message: str) -> None:
    assert parse_publish_failure(message) is None


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("Delivery failed for '0x1234'.", KIND_DELIVERY_FAILED),
        (
            "~x~> [ZCL to=0x1:0x2] Failed to send request with status=NO_ROUTE.",
            KIND_DELIVERY_FAILED,
        ),
        ("something odd", KIND_OTHER),
    ],
)
def test_classify(error: str, kind: str) -> None:
    assert classify_failure(error) == kind


def test_topic_helpers() -> None:
    assert set_topic_for("zigbee2mqtt", "zgb_kitchen") == "zigbee2mqtt/zgb_kitchen/set"
    assert z2m_base_topic(["zigbee2mqtt/a/set", "zigbee2mqtt/b/set"]) == (
        "zigbee2mqtt",
        [],
    )
    assert z2m_base_topic(["z2m/a/set", "zigbee2mqtt/b/set"]) == (
        "z2m",
        ["zigbee2mqtt/b/set"],
    )
    assert z2m_base_topic([]) == ("zigbee2mqtt", [])
