"""The single argument every check receives.

Two things here are deliberate. ``now`` is injected rather than read from the
clock inside checks, so "is this snapshot older than 90 days?" is testable
without freezing time globally. And ``client()`` defaults to the check's own
declared service, which keeps checks from reaching for clients they never
declared IAM permissions for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sar_aws_barnacle.config import Config
from sar_aws_barnacle.pricing.base import PriceBook
from sar_aws_barnacle.session import ClientFactory


@dataclass(frozen=True, slots=True)
class ScanContext:
    region: str
    check_id: str
    service: str
    config: Config
    prices: PriceBook
    factory: ClientFactory
    now: datetime

    def client(self, service: str | None = None):
        """Client for this check's service in this region."""
        return self.factory.client(service or self.service, region=self.region)

    def option(self, key: str, default: Any) -> Any:
        """Per-check tunable, e.g. ``ctx.option("age_days", 90)``."""
        return self.config.option(self.check_id, key, default)

    def price(self, dimension: str, variant: str = "default") -> Decimal | None:
        """Unit price for this region, or None if unknown."""
        return self.prices.price(self.region, dimension, variant)

    def age_days(self, timestamp: datetime | None) -> int | None:
        """Whole days between ``timestamp`` and the scan's reference time."""
        if timestamp is None:
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=self.now.tzinfo)
        return max((self.now - timestamp).days, 0)
