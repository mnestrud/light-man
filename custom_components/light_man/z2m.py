"""Zigbee2MQTT bridge messages: publish-failure parsing and topic helpers.

Z2M reports every failed publish on ``<base>/bridge/logging`` (non-retained,
``{"level", "message", "namespace"}``) as::

    Publish '<type>' '<attr>' to '<friendly name>' failed: '<error>'

The error text comes from zigbee-herdsman's ember adapter (v11.2.0) and tells the
failure apart:

- ``busy``: ``status=BUSY`` on a group send — the coordinator's broadcast table
  was full; herdsman never retries it.
- ``unconfirmed``: ``timed out after <n>ms`` — the command was APS-acked but its
  ZCL default response never arrived (verified 2026-11-03: the bulb applied it).
- ``delivery_failed``: ``Delivery failed`` / ``Delivery of MULTICAST failed`` /
  ``Failed to send request with status=`` — the frame did not get through.
- ``other``: anything else.

Pure and hass-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KIND_BUSY = "busy"
KIND_UNCONFIRMED = "unconfirmed"
KIND_DELIVERY_FAILED = "delivery_failed"
KIND_OTHER = "other"
RESEND_KINDS = frozenset({KIND_BUSY, KIND_DELIVERY_FAILED})

_FAILURE = re.compile(
    r"^Publish '[^']*' '(?P<attr>[^']*)' to '(?P<name>[^']+)' failed: (?P<error>.*)$"
)


@dataclass(frozen=True)
class PublishFailure:
    """One failed publish reported by Z2M."""

    name: str
    kind: str
    error: str
    attr: str = ""  # the key Z2M was setting (``brightness``, ``led_effect`` ...)


def classify_failure(error: str) -> str:
    """Map herdsman's error text to a failure kind."""
    if "status=BUSY" in error:
        return KIND_BUSY
    if "timed out after" in error:
        return KIND_UNCONFIRMED
    if (
        "Delivery failed" in error
        or "Delivery of MULTICAST failed" in error
        or "Failed to send request with status=" in error
    ):
        return KIND_DELIVERY_FAILED
    return KIND_OTHER


def parse_publish_failure(message: str) -> PublishFailure | None:
    """Parse one ``bridge/logging`` message; None if it is not a publish failure."""
    match = _FAILURE.match(message.strip())
    if match is None:
        return None
    error = match.group("error")
    return PublishFailure(
        match.group("name"), classify_failure(error), error[:300], match.group("attr")
    )


def set_topic_for(base: str, name: str) -> str:
    """Return the ``/set`` topic for a Z2M friendly name."""
    return f"{base}/{name}/set"


def z2m_base_topic(topics: list[str]) -> tuple[str, list[str]]:
    """Derive the Z2M base topic from set topics; also return disagreeing ones."""
    if not topics:
        return "zigbee2mqtt", []
    base = topics[0].split("/", 1)[0]
    return base, [topic for topic in topics if topic.split("/", 1)[0] != base]
