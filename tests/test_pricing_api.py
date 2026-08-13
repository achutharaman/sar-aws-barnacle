"""Tests for the AWS Pricing API response parsers.

The Pricing API returns SKU records as *embedded JSON strings* whose real
structure is terms -> OnDemand -> {opaque offer key} -> priceDimensions ->
{opaque dimension key} -> pricePerUnit -> USD. Nothing about that shape is
guessable, and moto does not mock it, so these tests drive the parsers with
recorded-shape payloads instead.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from sar_aws_barnacle.pricing.api import (
    CACHE_FILENAME,
    PricingApiPriceBook,
    _fetch_ebs_storage,
    _fetch_idle_eip,
    _fetch_snapshot_storage,
    _price_from_terms,
)
from sar_aws_barnacle.pricing.base import EBS_GB_MONTH, EIP_IDLE_HOUR

REGION = "ap-south-1"


def _terms(usd: str) -> dict:
    """The nested shape AWS actually returns, opaque keys and all."""
    return {
        "OnDemand": {
            "7Y8XQ3ZJ2M.JRTCKXETXF": {
                "priceDimensions": {
                    "7Y8XQ3ZJ2M.JRTCKXETXF.6YS6EN2CT7": {"pricePerUnit": {"USD": usd}}
                }
            }
        }
    }


def _product(attributes: dict, usd: str) -> str:
    return json.dumps({"product": {"attributes": attributes}, "terms": _terms(usd)})


class _FakePaginator:
    def __init__(self, pages_by_family):
        self._pages_by_family = pages_by_family

    def paginate(self, **kwargs):
        family = next(f["Value"] for f in kwargs["Filters"] if f["Field"] == "productFamily")
        return iter(self._pages_by_family.get(family, [{"PriceList": []}]))


class _FakePricingClient:
    def __init__(self, pages_by_family):
        self._pages_by_family = pages_by_family

    def get_paginator(self, _name):
        return _FakePaginator(self._pages_by_family)


class _FakeFactory:
    def __init__(self, client):
        self._client = client

    def client(self, service, region=None):
        return self._client


# --- term parsing -----------------------------------------------------------


def test_price_is_extracted_from_nested_terms():
    assert _price_from_terms(_terms("0.0912000000")) == Decimal("0.0912000000")


def test_missing_terms_yield_none():
    assert _price_from_terms({}) is None
    assert _price_from_terms({"OnDemand": {}}) is None


def test_zero_priced_sku_is_skipped():
    """Free tiers and $0 placeholder SKUs must not masquerade as a real price."""
    assert _price_from_terms(_terms("0.0000000000")) is None


def test_non_usd_currency_is_ignored():
    terms = {"OnDemand": {"a": {"priceDimensions": {"b": {"pricePerUnit": {"CNY": "0.5"}}}}}}
    assert _price_from_terms(terms) is None


# --- fetchers ---------------------------------------------------------------


def test_ebs_storage_fetcher_keys_by_volume_type():
    client = _FakePricingClient(
        {
            "Storage": [
                {
                    "PriceList": [
                        _product({"volumeApiName": "gp3"}, "0.0912"),
                        _product({"volumeApiName": "gp2"}, "0.114"),
                    ]
                },
                {"PriceList": [_product({"volumeApiName": "io2"}, "0.1425")]},
            ]
        }
    )

    prices = _fetch_ebs_storage(client, REGION)

    assert prices == {"gp3": "0.0912", "gp2": "0.114", "io2": "0.1425"}


def test_ebs_fetcher_skips_products_without_a_volume_type():
    client = _FakePricingClient(
        {"Storage": [{"PriceList": [_product({"storageMedia": "SSD"}, "1.00")]}]}
    )
    assert _fetch_ebs_storage(client, REGION) == {}


def test_snapshot_fetcher_returns_a_default_price():
    client = _FakePricingClient(
        {"Storage Snapshot": [{"PriceList": [_product({"usagetype": "SnapshotUsage"}, "0.05")]}]}
    )
    assert _fetch_snapshot_storage(client, REGION) == {"default": "0.05"}


def test_idle_eip_fetcher_selects_the_idle_sku():
    """An account's in-use address SKU sits in the same product family."""
    client = _FakePricingClient(
        {
            "IP Address": [
                {
                    "PriceList": [
                        _product({"usagetype": "PublicIPv4:InUseAddress"}, "0.009"),
                        _product({"usagetype": "ElasticIP:IdleAddress"}, "0.005"),
                    ]
                }
            ]
        }
    )

    assert _fetch_idle_eip(client, REGION) == {"default": "0.005"}


def test_idle_eip_fetcher_returns_empty_when_absent():
    client = _FakePricingClient(
        {"IP Address": [{"PriceList": [_product({"usagetype": "InUseAddress"}, "0.009")]}]}
    )
    assert _fetch_idle_eip(client, REGION) == {}


# --- price book integration -------------------------------------------------


def _full_client() -> _FakePricingClient:
    return _FakePricingClient(
        {
            "Storage": [{"PriceList": [_product({"volumeApiName": "gp3"}, "0.0999")]}],
            "Storage Snapshot": [{"PriceList": [_product({}, "0.049")]}],
            "IP Address": [
                {"PriceList": [_product({"usagetype": "ElasticIP:IdleAddress"}, "0.006")]}
            ],
        }
    )


def test_live_price_beats_the_seed(tmp_path: Path):
    book = PricingApiPriceBook(_FakeFactory(_full_client()), cache_dir=tmp_path)

    assert book.price(REGION, EBS_GB_MONTH, "gp3") == Decimal("0.0999")
    assert book.price(REGION, EIP_IDLE_HOUR) == Decimal("0.006")
    assert book.source == "AWS Pricing API (cached)"


def test_dimension_the_api_did_not_return_falls_back_to_seed(tmp_path: Path):
    """A partial live response must not blank out prices we already have."""
    client = _FakePricingClient(
        {"Storage": [{"PriceList": [_product({"volumeApiName": "gp3"}, "0.0999")]}]}
    )
    book = PricingApiPriceBook(_FakeFactory(client), cache_dir=tmp_path)

    assert book.price(REGION, EBS_GB_MONTH, "gp3") == Decimal("0.0999")
    assert book.price(REGION, EIP_IDLE_HOUR) == Decimal("0.005")  # seed value


def test_successful_fetch_is_written_to_the_cache(tmp_path: Path):
    book = PricingApiPriceBook(_FakeFactory(_full_client()), cache_dir=tmp_path)
    book.price(REGION, EBS_GB_MONTH, "gp3")

    cached = json.loads((tmp_path / CACHE_FILENAME).read_text())

    assert "_fetched_at" in cached
    assert cached["regions"][REGION][EBS_GB_MONTH]["gp3"] == "0.0999"


def test_region_is_fetched_only_once(tmp_path: Path):
    calls = []

    class _CountingClient(_FakePricingClient):
        def get_paginator(self, name):
            calls.append(name)
            return super().get_paginator(name)

    client = _CountingClient(
        {"Storage": [{"PriceList": [_product({"volumeApiName": "gp3"}, "0.0999")]}]}
    )
    book = PricingApiPriceBook(_FakeFactory(client), cache_dir=tmp_path)

    for _ in range(5):
        book.price(REGION, EBS_GB_MONTH, "gp3")

    assert len(calls) == 3, "each dimension should be fetched exactly once per region"


def test_refresh_ignores_a_fresh_cache(tmp_path: Path):
    (tmp_path / CACHE_FILENAME).write_text(
        json.dumps(
            {
                "_fetched_at": "2026-08-12T00:00:00+00:00",
                "regions": {REGION: {EBS_GB_MONTH: {"gp3": "9.99"}}},
            }
        )
    )
    book = PricingApiPriceBook(_FakeFactory(_full_client()), cache_dir=tmp_path, refresh=True)

    assert book.price(REGION, EBS_GB_MONTH, "gp3") == Decimal("0.0999")


def test_unwritable_cache_directory_does_not_break_the_scan(tmp_path: Path):
    unwritable = tmp_path / "file-not-a-dir"
    unwritable.write_text("blocking the path")
    book = PricingApiPriceBook(_FakeFactory(_full_client()), cache_dir=unwritable / "sub")

    assert book.price(REGION, EBS_GB_MONTH, "gp3") == Decimal("0.0999")
