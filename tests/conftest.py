"""Shared fixtures.

Two things every test here relies on: credentials are always fake (so a
misconfigured test can never reach a real account), and time is always injected
(so "older than 90 days" assertions do not rot).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sar_aws_barnacle.config import Config
from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.pricing.base import PriceBook
from sar_aws_barnacle.pricing.static import StaticPriceBook
from sar_aws_barnacle.session import ClientFactory

FROZEN_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
TEST_REGION = "ap-south-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee no test can touch a real account, even by accident."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": TEST_REGION,
    }.items():
        monkeypatch.setenv(key, value)
    for key in (
        "AWS_PROFILE",
        "SAR_AWS_BARNACLE_PROFILE",
        "SAR_AWS_BARNACLE_REGIONS",
        "SAR_AWS_BARNACLE_OUTPUT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def now() -> datetime:
    return FROZEN_NOW


@pytest.fixture
def prices() -> StaticPriceBook:
    return StaticPriceBook()


@pytest.fixture
def factory() -> ClientFactory:
    return ClientFactory()


@pytest.fixture
def make_context(factory: ClientFactory, prices: StaticPriceBook):
    def _make(
        check_id: str,
        service: str = "ec2",
        *,
        region: str = TEST_REGION,
        config: Config | None = None,
        price_book: PriceBook | None = None,
        at: datetime | None = None,
    ) -> ScanContext:
        return ScanContext(
            region=region,
            check_id=check_id,
            service=service,
            config=config or Config(),
            prices=price_book or prices,
            factory=factory,
            now=at or FROZEN_NOW,
        )

    return _make
