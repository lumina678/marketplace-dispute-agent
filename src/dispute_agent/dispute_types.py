from __future__ import annotations

from enum import StrEnum


class DisputeType(StrEnum):
    """Stable top-level dispute taxonomy used by claims, skills and routing."""

    DESCRIPTION_MISMATCH = "DESCRIPTION_MISMATCH"
    MISSING_PARTS = "MISSING_PARTS"
    EMPTY_PACKAGE = "EMPTY_PACKAGE"
    SHIPPING_DAMAGE = "SHIPPING_DAMAGE"
    COUNTERFEIT = "COUNTERFEIT"
    OTHER = "OTHER"


class RoutingSource(StrEnum):
    """How a claim received its current dispute type and skill binding."""

    USER_DECLARED = "USER_DECLARED"
    PLATFORM_REASON_CODE = "PLATFORM_REASON_CODE"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    MODEL_SUGGESTION = "MODEL_SUGGESTION"
    REVIEWER_OVERRIDE = "REVIEWER_OVERRIDE"
    LEGACY_MIGRATION = "LEGACY_MIGRATION"


class RoutingStatus(StrEnum):
    """Routing lifecycle. The Router introduced in step 19 owns transitions."""

    UNROUTED = "UNROUTED"
    ROUTED = "ROUTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    OVERRIDDEN = "OVERRIDDEN"


SUPPORTED_DISPUTE_TYPES = frozenset(item.value for item in DisputeType)
SUPPORTED_ROUTING_SOURCES = frozenset(item.value for item in RoutingSource)
SUPPORTED_ROUTING_STATUSES = frozenset(item.value for item in RoutingStatus)

