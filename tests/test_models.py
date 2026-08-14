"""Invariants on the Finding value type, and ScanResult.region_check_status()."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sar_aws_barnacle.models import CheckError, CostConfidence, Finding, ScanResult, Severity


def _finding(**overrides):
    base = {
        "check_id": "c",
        "resource_id": "r",
        "resource_type": "AWS::Test::Thing",
        "region": "ap-south-1",
        "severity": Severity.MEDIUM,
        "title": "t",
        "detail": "d",
    }
    base.update(overrides)
    return Finding(**base)


def test_cost_without_confidence_is_rejected():
    """Silently defaulting to UNKNOWN would let a real number look untrusted."""
    with pytest.raises(ValueError, match="without a confidence"):
        _finding(estimated_monthly_cost=Decimal("5"))


def test_confidence_without_cost_is_rejected():
    with pytest.raises(ValueError, match=r"use CostConfidence\.UNKNOWN"):
        _finding(cost_confidence=CostConfidence.EXACT)


def test_unpriced_finding_is_valid():
    finding = _finding()
    assert finding.estimated_monthly_cost is None
    assert finding.cost_confidence is CostConfidence.UNKNOWN


def test_findings_are_immutable():
    finding = _finding()
    with pytest.raises(AttributeError):
        finding.severity = Severity.HIGH


def test_finding_metadata_cannot_be_mutated():
    """frozen=True only blocks rebinding finding.metadata, not mutating the
    dict it points to -- this asserts the read-only copy actually closes
    that gap."""
    finding = _finding(metadata={"key": "value"})
    with pytest.raises(TypeError):
        finding.metadata["injected"] = "yes"


def test_finding_metadata_is_a_copy_not_the_original():
    """The caller's dict must not be mutable through the finding after the
    fact, or the read-only wrapper is just theater around a live reference."""
    original = {"key": "value"}
    finding = _finding(metadata=original)
    original["injected"] = "yes"
    assert "injected" not in finding.metadata


def test_severity_ordering():
    assert Severity.HIGH.rank > Severity.MEDIUM.rank > Severity.LOW.rank > Severity.INFO.rank


@pytest.mark.parametrize(
    "op",
    [
        lambda a, b: a < b,
        lambda a, b: a <= b,
        lambda a, b: a > b,
        lambda a, b: a >= b,
    ],
)
def test_severity_comparison_operators_are_disabled(op):
    """StrEnum inherits str's alphabetical comparison ("high" < "low"), which
    disagrees with .rank. Rather than leave that trap in place, comparison
    operators are disabled outright -- .rank is the one ordering mechanism."""
    with pytest.raises(TypeError):
        op(Severity.LOW, Severity.HIGH)


def test_sort_puts_high_severity_and_big_money_first():
    cheap_high = _finding(
        resource_id="a",
        severity=Severity.HIGH,
        estimated_monthly_cost=Decimal("1"),
        cost_confidence=CostConfidence.EXACT,
    )
    rich_medium = _finding(
        resource_id="b",
        severity=Severity.MEDIUM,
        estimated_monthly_cost=Decimal("900"),
        cost_confidence=CostConfidence.EXACT,
    )
    ordered = sorted([rich_medium, cheap_high], key=lambda f: f.sort_key)
    assert [f.resource_id for f in ordered] == ["a", "b"]


def _result(**overrides) -> ScanResult:
    base = {
        "findings": (),
        "errors": (),
        "regions": ("ap-south-1",),
        "checks_run": ("ebs-unattached",),
        "started_at": datetime(2026, 8, 12, tzinfo=UTC),
        "duration_seconds": 0.5,
    }
    base.update(overrides)
    return ScanResult(**base)


def test_region_check_status_scanned_with_no_findings_is_clean():
    result = _result(regions=("ap-south-1", "us-east-1"))
    assert result.region_check_status() == [
        ("ap-south-1", "ebs-unattached", "clean"),
        ("us-east-1", "ebs-unattached", "clean"),
    ]


def test_region_check_status_breaks_down_per_check():
    """Two checks, one region: each check gets its own row, not one
    combined count for the region."""
    result = _result(
        findings=(_finding(check_id="ebs-unattached", region="ap-south-1"),),
        regions=("ap-south-1",),
        checks_run=("ebs-unattached", "ebs-unencrypted"),
    )
    assert ("ap-south-1", "ebs-unattached", "1 finding(s)") in result.region_check_status()
    assert ("ap-south-1", "ebs-unencrypted", "clean") in result.region_check_status()


def test_region_check_status_counts_findings_per_region():
    result = _result(
        findings=(
            _finding(check_id="ebs-unattached", region="ap-south-1"),
            _finding(check_id="ebs-unattached", region="ap-south-1"),
        ),
        regions=("ap-south-1", "us-east-1"),
    )
    assert ("ap-south-1", "ebs-unattached", "2 finding(s)") in result.region_check_status()
    assert ("us-east-1", "ebs-unattached", "clean") in result.region_check_status()


def test_region_check_status_flags_a_failed_check():
    result = _result(
        errors=(CheckError("ebs-unattached", "ap-south-1", "AccessDenied", "x"),),
        regions=("ap-south-1",),
    )
    assert result.region_check_status() == [("ap-south-1", "ebs-unattached", "check failed")]


def test_region_check_status_lists_skipped_regions_last():
    result = _result(regions=("ap-south-1",), skipped_regions=("me-south-1",))
    assert result.region_check_status() == [
        ("ap-south-1", "ebs-unattached", "clean"),
        ("me-south-1", None, "skipped — not enabled for this account"),
    ]


def test_region_check_status_reports_a_global_check_exactly_once():
    """A global check runs exactly once regardless of how many real regions
    were scanned -- it must show up once under "global", not once per real
    region (which would misreport it as having run in each of them) and not
    disappear because "global" was never one of the requested regions."""
    result = _result(
        findings=(_finding(check_id="iam-stale-keys", region="global"),),
        regions=("ap-south-1", "us-east-1"),
        checks_run=("ebs-unattached", "iam-stale-keys"),
        global_checks=frozenset({"iam-stale-keys"}),
    )
    lines = result.region_check_status()
    assert lines.count(("global", "iam-stale-keys", "1 finding(s)")) == 1
    assert not any(
        check_id == "iam-stale-keys" and region != "global" for region, check_id, _ in lines
    )


def test_region_check_status_reports_a_global_check_with_zero_findings():
    """The same guarantee in the other direction: a global check that found
    nothing must still show up once, as clean -- not zero times."""
    result = _result(
        regions=("ap-south-1",),
        checks_run=("iam-stale-keys",),
        global_checks=frozenset({"iam-stale-keys"}),
    )
    assert result.region_check_status() == [("global", "iam-stale-keys", "clean")]
