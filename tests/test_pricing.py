"""Pricing: seed lookups, unknown handling, cache, and fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sar_aws_barnacle.pricing.api import CACHE_FILENAME, PricingApiPriceBook
from sar_aws_barnacle.pricing.base import EBS_GB_MONTH, EIP_IDLE_HOUR, NullPriceBook
from sar_aws_barnacle.pricing.static import StaticPriceBook


def test_seed_lookup():
    book = StaticPriceBook()
    assert book.price("ap-south-1", EBS_GB_MONTH, "gp3") == Decimal("0.0912")
    assert book.price("us-east-1", EIP_IDLE_HOUR) == Decimal("0.005")


def test_unknown_region_returns_none_not_zero():
    assert StaticPriceBook().price("eu-north-1", EBS_GB_MONTH, "gp3") is None


def test_unknown_dimension_returns_none():
    assert StaticPriceBook().price("us-east-1", "made-up-dimension") is None


def test_unknown_variant_falls_back_to_default():
    table = {"regions": {"x": {"d": {"default": "1.5"}}}}
    assert StaticPriceBook(table).price("x", "d", "weird-variant") == Decimal("1.5")


def test_seed_covers_every_volume_type_the_check_can_see():
    book = StaticPriceBook()
    for volume_type in ("gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"):
        assert book.price("ap-south-1", EBS_GB_MONTH, volume_type) is not None


def test_null_price_book_prices_nothing():
    assert NullPriceBook().price("us-east-1", EBS_GB_MONTH, "gp3") is None


class _FailingFactory:
    def client(self, service, region=None):
        raise RuntimeError("no network")


def test_api_falls_back_to_seed_when_unreachable(tmp_path: Path):
    """No pricing permission must degrade the numbers, not the scan."""
    book = PricingApiPriceBook(_FailingFactory(), cache_dir=tmp_path)

    assert book.price("ap-south-1", EBS_GB_MONTH, "gp3") == Decimal("0.0912")
    assert "unavailable" in book.source


def test_fresh_cache_is_used_without_calling_aws(tmp_path: Path):
    cache = {
        "_fetched_at": datetime.now(UTC).isoformat(),
        "regions": {"ap-south-1": {EBS_GB_MONTH: {"gp3": "0.1234"}}},
    }
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(cache))

    book = PricingApiPriceBook(_FailingFactory(), cache_dir=tmp_path)

    assert book.price("ap-south-1", EBS_GB_MONTH, "gp3") == Decimal("0.1234")


def test_stale_cache_is_ignored(tmp_path: Path):
    cache = {
        "_fetched_at": (datetime.now(UTC) - timedelta(days=400)).isoformat(),
        "regions": {"ap-south-1": {EBS_GB_MONTH: {"gp3": "9.99"}}},
    }
    (tmp_path / CACHE_FILENAME).write_text(json.dumps(cache))

    book = PricingApiPriceBook(_FailingFactory(), cache_dir=tmp_path)

    assert book.price("ap-south-1", EBS_GB_MONTH, "gp3") == Decimal("0.0912")


def test_corrupt_cache_does_not_crash(tmp_path: Path):
    (tmp_path / CACHE_FILENAME).write_text("{not json")
    book = PricingApiPriceBook(_FailingFactory(), cache_dir=tmp_path)
    assert book.price("ap-south-1", EBS_GB_MONTH, "gp3") == Decimal("0.0912")
