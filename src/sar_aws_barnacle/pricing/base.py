"""The pricing interface.

Deliberately generic. An earlier sketch had one method per resource type
(``ebs_gb_month``, ``eip_idle_hour``, ...), which meant every new check forced a
change to this protocol and both implementations -- exactly the coupling we set
out to avoid. A ``(region, dimension, variant)`` lookup adds new prices as data,
not as code.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

# Dimension names are part of the seed-file format, so they are stable strings.
EBS_GB_MONTH = "ebs-gb-month"
EBS_SNAPSHOT_GB_MONTH = "ebs-snapshot-gb-month"
EIP_IDLE_HOUR = "eip-idle-hour"
RDS_SNAPSHOT_GB_MONTH = "rds-snapshot-gb-month"

HOURS_PER_MONTH = Decimal(730)


@runtime_checkable
class PriceBook(Protocol):
    """Resolves a unit price, or admits it cannot."""

    @property
    def source(self) -> str:
        """Short human label for the report footer, e.g. ``bundled seed table``."""
        ...

    def price(self, region: str, dimension: str, variant: str = "default") -> Decimal | None:
        """Unit price in USD, or None when unknown.

        None is a first-class answer. Returning ``Decimal(0)`` for an unknown
        price would silently understate savings, which is worse than saying so.
        """
        ...


class NullPriceBook:
    """Prices nothing. Used by ``--no-pricing`` and by tests that assert on
    unpriced-finding handling."""

    @property
    def source(self) -> str:
        return "pricing disabled"

    def price(self, region: str, dimension: str, variant: str = "default") -> Decimal | None:
        return None
