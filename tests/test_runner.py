"""Runner behaviour: error isolation, scope handling, and filtering.

The core promise being tested here is that a scan degrades instead of dying.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from aws_barnacle.config import Config
from aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from aws_barnacle.pricing.base import NullPriceBook
from aws_barnacle.runner import plan_units, run_scan
from aws_barnacle.session import ClientFactory, ReadOnlyViolationError

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def _finding(check_id: str, region: str, cost: str | None = "10.00") -> Finding:
    return Finding(
        check_id=check_id,
        resource_id=f"res-{region}",
        resource_type="AWS::Test::Thing",
        region=region,
        severity=Severity.MEDIUM,
        title="Test finding",
        detail="detail",
        estimated_monthly_cost=None if cost is None else Decimal(cost),
        cost_confidence=CostConfidence.UNKNOWN if cost is None else CostConfidence.EXACT,
    )


def _make_check(check_id, *, scope=Scope.REGIONAL, behaviour=None, severity=Severity.MEDIUM):
    class _Check:
        id = check_id
        title = check_id
        service = "ec2"
        required_actions = frozenset({"ec2:DescribeVolumes"})

        def run(self, ctx):
            if behaviour is not None:
                return behaviour(ctx)
            return iter([_finding(check_id, ctx.region)])

    _Check.scope = scope
    return _Check


def _run(checks, regions=("ap-south-1",), config=None):
    return run_scan(
        checks,
        list(regions),
        factory=ClientFactory(),
        config=config or Config(max_workers=2),
        prices=NullPriceBook(),
        now=NOW,
    )


def test_regional_check_runs_once_per_region():
    result = _run([_make_check("regional")], regions=("ap-south-1", "us-east-1"))
    assert len(result.findings) == 2
    assert {f.region for f in result.findings} == {"ap-south-1", "us-east-1"}


def test_global_check_runs_once_regardless_of_region_count():
    """Otherwise a six-region scan reports every IAM key six times."""
    check = _make_check("global-check", scope=Scope.GLOBAL)
    result = _run([check], regions=("ap-south-1", "us-east-1", "eu-west-1"))
    assert len(result.findings) == 1
    assert result.findings[0].region == "global"


def test_plan_units_expands_correctly():
    regional = _make_check("r")
    glob = _make_check("g", scope=Scope.GLOBAL)
    units = plan_units([regional, glob], ["a", "b", "c"])
    assert sum(1 for c, _ in units if c is regional) == 3
    assert sum(1 for c, _ in units if c is glob) == 1


def test_access_denied_is_recorded_not_raised():
    def deny(ctx):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "DescribeVolumes"
        )

    result = _run([_make_check("denied", behaviour=deny)])

    assert result.findings == ()
    assert len(result.errors) == 1
    assert result.errors[0].error_code == "AccessDenied"
    assert result.is_partial is True


def test_one_failing_check_does_not_lose_the_others():
    def boom(ctx):
        raise RuntimeError("kaboom")

    result = _run([_make_check("healthy"), _make_check("broken", behaviour=boom)])

    assert len(result.findings) == 1
    assert result.findings[0].check_id == "healthy"
    assert result.errors[0].error_code == "InternalError"


def test_failure_is_isolated_per_region():
    def fail_in_one(ctx):
        if ctx.region == "us-east-1":
            raise RuntimeError("regional failure")
        return iter([_finding("partial", ctx.region)])

    result = _run(
        [_make_check("partial", behaviour=fail_in_one)], regions=("ap-south-1", "us-east-1")
    )

    assert len(result.findings) == 1
    assert len(result.errors) == 1
    assert result.errors[0].region == "us-east-1"


def test_read_only_violation_propagates():
    """A mutating call is our bug, so it must fail loudly rather than degrade."""

    def violate(ctx):
        raise ReadOnlyViolationError("blocked CreateTags")

    with pytest.raises(ReadOnlyViolationError):
        _run([_make_check("violator", behaviour=violate)])


def test_savings_total_excludes_unpriced_findings():
    def mixed(ctx):
        return iter([_finding("mixed", ctx.region, "10.00"), _finding("mixed", ctx.region, None)])

    result = _run([_make_check("mixed", behaviour=mixed)])

    assert result.estimated_monthly_savings == Decimal("10.00")
    assert result.unpriced_count == 1


def test_min_savings_filter_keeps_unpriced_findings():
    """Hiding what we cannot price would make the threshold a blindfold."""

    def mixed(ctx):
        return iter(
            [
                _finding("mixed", ctx.region, "1.00"),
                _finding("mixed", ctx.region, "50.00"),
                _finding("mixed", ctx.region, None),
            ]
        )

    config = Config(max_workers=2, min_monthly_savings=Decimal("10"))
    result = _run([_make_check("mixed", behaviour=mixed)], config=config)

    costs = {f.estimated_monthly_cost for f in result.findings}
    assert costs == {Decimal("50.00"), None}


def test_result_records_metadata():
    result = _run([_make_check("meta")], regions=("ap-south-1", "us-east-1"))
    assert result.regions == ("ap-south-1", "us-east-1")
    assert result.checks_run == ("meta",)
    assert result.duration_seconds >= 0
    assert result.is_partial is False
