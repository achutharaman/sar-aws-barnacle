"""Invariants on the Finding value type."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sar_aws_barnacle.models import CostConfidence, Finding, Severity


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
