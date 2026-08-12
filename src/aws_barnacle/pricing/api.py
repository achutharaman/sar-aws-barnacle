"""Live pricing from the AWS Pricing API, cached to disk, seeded as fallback.

Design notes worth knowing:

* The Pricing API is only served from a few regions (us-east-1 and eu-central-1
  reliably, ap-south-1 more recently). Scanning ``ap-southeast-2`` therefore
  still opens a client to a *different* region purely to ask about prices. We
  pin to us-east-1 and treat failure as non-fatal.
* Prices change rarely, so a 30-day on-disk cache turns a multi-second
  ``GetProducts`` pagination into a file read on every scan after the first.
* Every failure path -- no network, no ``pricing:GetProducts`` permission, an
  unparseable response -- degrades to the bundled seed table and records that
  fact so the report footer can say which source produced the numbers.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from aws_barnacle.pricing.base import EBS_GB_MONTH, EBS_SNAPSHOT_GB_MONTH, EIP_IDLE_HOUR
from aws_barnacle.pricing.static import StaticPriceBook

log = logging.getLogger(__name__)

PRICING_ENDPOINT_REGION = "us-east-1"
CACHE_FILENAME = "prices.cache.json"
DEFAULT_TTL_DAYS = 30


def _price_from_terms(product_terms: dict[str, Any]) -> Decimal | None:
    """Dig the USD on-demand unit price out of a Pricing API term blob.

    The shape is terms -> OnDemand -> {offer} -> priceDimensions -> {dim} ->
    pricePerUnit -> USD, with opaque generated keys at both levels.
    """
    for offer in (product_terms.get("OnDemand") or {}).values():
        for dimension in (offer.get("priceDimensions") or {}).values():
            usd = (dimension.get("pricePerUnit") or {}).get("USD")
            if usd is None:
                continue
            value = Decimal(str(usd))
            if value > 0:
                return value
    return None


def _fetch_ebs_storage(client, region: str) -> dict[str, str]:
    prices: dict[str, str] = {}
    paginator = client.get_paginator("get_products")
    pages = paginator.paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ],
    )
    for page in pages:
        for raw in page.get("PriceList", []):
            product = json.loads(raw) if isinstance(raw, str) else raw
            attrs = product.get("product", {}).get("attributes", {})
            volume_type = attrs.get("volumeApiName")
            if not volume_type:
                continue
            price = _price_from_terms(product.get("terms", {}))
            if price is not None:
                prices[volume_type] = str(price)
    return prices


def _fetch_snapshot_storage(client, region: str) -> dict[str, str]:
    paginator = client.get_paginator("get_products")
    pages = paginator.paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ],
    )
    for page in pages:
        for raw in page.get("PriceList", []):
            product = json.loads(raw) if isinstance(raw, str) else raw
            price = _price_from_terms(product.get("terms", {}))
            if price is not None:
                return {"default": str(price)}
    return {}


def _fetch_idle_eip(client, region: str) -> dict[str, str]:
    paginator = client.get_paginator("get_products")
    pages = paginator.paginate(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "IP Address"},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ],
    )
    for page in pages:
        for raw in page.get("PriceList", []):
            product = json.loads(raw) if isinstance(raw, str) else raw
            attrs = product.get("product", {}).get("attributes", {})
            usage = (attrs.get("usagetype") or "") + (attrs.get("group") or "")
            if "IdleAddress" not in usage:
                continue
            price = _price_from_terms(product.get("terms", {}))
            if price is not None:
                return {"default": str(price)}
    return {}


# Adding a newly-priced dimension means adding one fetcher here. Adding a *check*
# that reuses an existing dimension means changing nothing at all.
FETCHERS = {
    EBS_GB_MONTH: _fetch_ebs_storage,
    EBS_SNAPSHOT_GB_MONTH: _fetch_snapshot_storage,
    EIP_IDLE_HOUR: _fetch_idle_eip,
}


class PricingApiPriceBook:
    """Live prices with cache and fallback. Safe to share across threads."""

    def __init__(
        self,
        client_factory,
        *,
        cache_dir: Path,
        ttl_days: int = DEFAULT_TTL_DAYS,
        refresh: bool = False,
        fallback: StaticPriceBook | None = None,
    ) -> None:
        self._factory = client_factory
        self._cache_path = Path(cache_dir) / CACHE_FILENAME
        self._ttl = timedelta(days=ttl_days)
        self._refresh = refresh
        self._fallback = fallback or StaticPriceBook()
        self._lock = threading.Lock()
        self._regions: dict[str, dict[str, dict[str, str]]] = {}
        self._fell_back = False
        self._loaded_cache = False

    @property
    def source(self) -> str:
        if self._fell_back:
            return f"AWS Pricing API unavailable - using {self._fallback.source}"
        return "AWS Pricing API (cached)"

    def price(self, region: str, dimension: str, variant: str = "default") -> Decimal | None:
        table = self._region_table(region)
        entry = table.get(dimension, {})
        raw = entry.get(variant) or entry.get("default")
        if raw is not None:
            return Decimal(str(raw))
        return self._fallback.price(region, dimension, variant)

    def _region_table(self, region: str) -> dict[str, dict[str, str]]:
        with self._lock:
            if region in self._regions:
                return self._regions[region]

            self._load_cache()
            cached = self._regions.get(region)
            if cached is not None and not self._refresh:
                return cached

            fetched = self._fetch_region(region)
            self._regions[region] = fetched
            if fetched:
                self._write_cache()
            else:
                self._fell_back = True
            return fetched

    def _fetch_region(self, region: str) -> dict[str, dict[str, str]]:
        try:
            client = self._factory.client("pricing", region=PRICING_ENDPOINT_REGION)
        except Exception as exc:  # noqa: BLE001 - pricing is never worth failing a scan
            log.debug("pricing client unavailable: %s", exc)
            return {}

        table: dict[str, dict[str, str]] = {}
        for dimension, fetcher in FETCHERS.items():
            try:
                values = fetcher(client, region)
            except Exception as exc:  # noqa: BLE001
                log.debug("pricing fetch failed for %s/%s: %s", region, dimension, exc)
                continue
            if values:
                table[dimension] = values
        return table

    def _load_cache(self) -> None:
        if self._loaded_cache or self._refresh:
            self._loaded_cache = True
            return
        self._loaded_cache = True
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["_fetched_at"])
            if datetime.now(UTC) - fetched_at > self._ttl:
                return
            self._regions.update(payload.get("regions", {}))
        except (OSError, KeyError, ValueError) as exc:
            log.debug("no usable price cache at %s: %s", self._cache_path, exc)

    def _write_cache(self) -> None:
        payload = {"_fetched_at": datetime.now(UTC).isoformat(), "regions": self._regions}
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError as exc:
            log.debug("could not write price cache: %s", exc)
