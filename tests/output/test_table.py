"""Table output: the human-facing honesty checks."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal

from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CheckError, CostConfidence, Finding, ScanResult, Severity
from sar_aws_barnacle.output.table import TableRenderer


def _finding(**overrides) -> Finding:
    base = {
        "check_id": "ebs-unattached",
        "resource_id": "vol-1",
        "resource_type": "AWS::EC2::Volume",
        "region": "ap-south-1",
        "severity": Severity.HIGH,
        "title": "Unattached EBS volume",
        "detail": "100 GiB gp3 volume",
        "estimated_monthly_cost": Decimal("9.12"),
        "cost_confidence": CostConfidence.EXACT,
    }
    base.update(overrides)
    return Finding(**base)


def _render(findings=(), errors=(), **overrides) -> str:
    base = {
        "findings": tuple(findings),
        "errors": tuple(errors),
        "regions": ("ap-south-1",),
        "checks_run": ("ebs-unattached",),
        "started_at": datetime(2026, 8, 12, tzinfo=UTC),
        "duration_seconds": 0.5,
        "account_id": "123456789012",
    }
    base.update(overrides)
    result = ScanResult(**base)
    stream = io.StringIO()
    TableRenderer(price_source="bundled seed table", width=200).render(result, Config(), stream)
    return stream.getvalue()


def test_empty_scan_says_so():
    assert "No findings" in _render()


def test_finding_appears_with_cost():
    output = _render([_finding()])
    assert "vol-1" in output
    assert "$9.12" in output
    assert "HIGH" in output


def test_lower_bound_cost_is_marked():
    """'$7.13' and 'at least $7.13' are different claims."""
    finding = _finding(
        estimated_monthly_cost=Decimal("7.13"),
        cost_confidence=CostConfidence.LOWER_BOUND,
    )
    output = _render([finding])
    assert "≥$7.13" in output


def test_unpriced_finding_shows_unknown_not_zero():
    finding = _finding(
        estimated_monthly_cost=None,
        cost_confidence=CostConfidence.UNKNOWN,
    )
    output = _render([finding])
    assert "unknown" in output
    assert "$0.00" not in output.split("Estimated")[0]


def test_annualised_total_is_shown():
    output = _render([_finding()])
    assert "109.44" in output  # 9.12 * 12


def test_partial_scan_is_flagged_loudly():
    output = _render([_finding()], [CheckError("ebs-unattached", "us-east-1", "AccessDenied", "x")])
    assert "partial" in output.lower()
    assert "AccessDenied" in output


def test_price_source_is_disclosed():
    assert "bundled seed table" in _render([_finding()])


def test_clean_region_is_shown_as_clean_not_omitted():
    """A scanned region with zero findings must still appear -- otherwise a
    reader can't tell "checked, found nothing" apart from "never looked"."""
    output = _render(regions=("ap-south-1", "eu-north-1"))
    assert "ap-south-1" in output
    assert "eu-north-1" in output
    assert "clean" in output


def test_skipped_region_is_shown_as_skipped():
    output = _render(regions=("ap-south-1",), skipped_regions=("me-south-1",))
    assert "me-south-1" in output
    assert "Skipped" in output


def test_regions_table_is_a_region_by_check_grid():
    """One row per region, one column per check -- not a long list of
    (region, check) pairs -- so multi-check coverage reads as a single grid."""
    findings = [_finding(check_id="ebs-unattached", region="ap-south-1")]
    output = _render(
        findings,
        regions=("ap-south-1", "us-east-1"),
        checks_run=("ebs-unattached", "ebs-unencrypted"),
    )
    regions_section = output.split("Regions", 1)[1].split("\n\n", 1)[0]
    assert "ebs-unattached" in regions_section
    assert "ebs-unencrypted" in regions_section
    # us-east-1 appears exactly once (one row), with both checks' statuses
    # alongside it, not once per check.
    assert output.count("us-east-1") == 1
