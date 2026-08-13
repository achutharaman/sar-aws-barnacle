"""Offline price book backed by the bundled seed table."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from typing import Any

SEED_PACKAGE = "sar_aws_barnacle.pricing.data"
SEED_FILENAME = "prices.seed.json"


@lru_cache(maxsize=1)
def load_seed() -> dict[str, Any]:
    text = resources.files(SEED_PACKAGE).joinpath(SEED_FILENAME).read_text(encoding="utf-8")
    return json.loads(text)


class StaticPriceBook:
    """Reads prices from an in-memory table. No network, fully deterministic.

    Used as the offline mode (``--no-live-pricing``), as the fallback when the
    Pricing API is unreachable, and as the price source in every unit test.
    """

    def __init__(self, table: Mapping[str, Any] | None = None, *, label: str | None = None) -> None:
        if table is None:
            seed = load_seed()
            self._regions: Mapping[str, Any] = seed.get("regions", {})
            self._label = label or f"bundled seed table ({seed.get('_meta', {}).get('captured')})"
        else:
            self._regions = table.get("regions", table)
            self._label = label or "static table"

    @property
    def source(self) -> str:
        return self._label

    def price(self, region: str, dimension: str, variant: str = "default") -> Decimal | None:
        entry = self._regions.get(region, {}).get(dimension)
        if not isinstance(entry, Mapping):
            return None
        raw = entry.get(variant)
        if raw is None and variant != "default":
            raw = entry.get("default")
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None

    def known_regions(self) -> list[str]:
        return sorted(self._regions)
