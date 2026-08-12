"""JSON output shape, versioning, and money formatting."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from decimal import Decimal

from aws_barnacle.config import Config, OutputFormat
from aws_barnacle.models import (
    SCHEMA_VERSION,
    CheckError,
    CostConfidence,
    Finding,
    ScanResult,
    Severity,
)
from aws_barnacle.output.base import get_renderer
from aws_barnacle.output.json_output import JsonRenderer, result_to_dict


def _result(**overrides) -> ScanResult:
    findings = (
        Finding(
            check_id="ebs-unattached",
            resource_id="vol-1",
            resource_type="AWS::EC2::Volume",
            region="ap-south-1",
            severity=Severity.HIGH,
            title="Unattached EBS volume",
            detail="100 GiB gp3",
            estimated_monthly_cost=Decimal("9.12"),
            cost_confidence=CostConfidence.EXACT,
            metadata={"volume_type": "gp3"},
        ),
        Finding(
            check_id="ebs-unattached",
            resource_id="vol-2",
            resource_type="AWS::EC2::Volume",
            region="eu-north-1",
            severity=Severity.MEDIUM,
            title="Unattached EBS volume",
            detail="unpriced",
        ),
    )
    base = {
        "findings": findings,
        "errors": (),
        "regions": ("ap-south-1",),
        "checks_run": ("ebs-unattached",),
        "started_at": datetime(2026, 8, 12, tzinfo=UTC),
        "duration_seconds": 1.5,
        "account_id": "123456789012",
    }
    base.update(overrides)
    return ScanResult(**base)


def _render(result, **kwargs) -> dict:
    stream = io.StringIO()
    JsonRenderer(**kwargs).render(result, Config(), stream)
    return json.loads(stream.getvalue())


def test_output_is_valid_json_with_schema_version():
    payload = _render(_result())
    assert payload["schema_version"] == SCHEMA_VERSION


def test_money_is_a_string_not_a_float():
    """Floats would let a total drift by cents under summation."""
    payload = _render(_result())
    assert payload["findings"][0]["estimated_monthly_cost"] == "9.12"
    assert isinstance(payload["summary"]["estimated_monthly_savings"], str)


def test_unpriced_finding_serialises_as_null():
    payload = _render(_result())
    unpriced = next(f for f in payload["findings"] if f["resource_id"] == "vol-2")
    assert unpriced["estimated_monthly_cost"] is None
    assert unpriced["cost_confidence"] == "unknown"


def test_summary_counts():
    payload = _render(_result())
    assert payload["summary"]["finding_count"] == 2
    assert payload["summary"]["unpriced_findings"] == 1
    assert payload["summary"]["estimated_monthly_savings"] == "9.12"
    assert payload["summary"]["partial"] is False


def test_errors_are_reported_and_mark_the_scan_partial():
    result = _result(errors=(CheckError("ebs-unattached", "us-east-1", "AccessDenied", "nope"),))
    payload = _render(result)
    assert payload["summary"]["partial"] is True
    assert payload["errors"][0]["error_code"] == "AccessDenied"


def test_findings_are_sorted_by_severity():
    payload = _render(_result())
    assert payload["findings"][0]["severity"] == "high"


def test_price_source_is_recorded():
    payload = _render(_result(), price_source="bundled seed table")
    assert payload["summary"]["price_source"] == "bundled seed table"


def test_result_to_dict_is_json_serialisable():
    json.dumps(result_to_dict(_result()))


def test_renderer_factory_returns_json_renderer():
    assert get_renderer(OutputFormat.JSON).name == "json"
    assert get_renderer(OutputFormat.TABLE).name == "table"
