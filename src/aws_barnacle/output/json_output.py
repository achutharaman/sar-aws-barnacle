"""Machine-readable output.

Two decisions worth defending:

* Money is serialised as a *string*, not a JSON number. ``0.1 + 0.2`` in IEEE
  754 is not ``0.3``, and a cost report that drifts by cents under summation
  invites exactly the kind of doubt this tool exists to remove.
* ``schema_version`` is emitted on every document. People will pipe this into
  jq, dashboards and Lambda functions; changing the shape without a version
  field breaks them silently.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

from aws_barnacle.config import Config
from aws_barnacle.models import SCHEMA_VERSION, Finding, ScanResult


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "check_id": finding.check_id,
        "resource_id": finding.resource_id,
        "resource_type": finding.resource_type,
        "region": finding.region,
        "severity": finding.severity.value,
        "title": finding.title,
        "detail": finding.detail,
        "estimated_monthly_cost": (
            None
            if finding.estimated_monthly_cost is None
            else f"{finding.estimated_monthly_cost:.2f}"
        ),
        "cost_confidence": finding.cost_confidence.value,
        "metadata": dict(finding.metadata),
    }


def result_to_dict(result: ScanResult, *, price_source: str | None = None) -> dict[str, Any]:
    findings = result.sorted_findings()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": result.started_at.isoformat(),
        "duration_seconds": result.duration_seconds,
        "account_id": result.account_id,
        "regions": list(result.regions),
        "checks_run": list(result.checks_run),
        "summary": {
            "finding_count": len(findings),
            "estimated_monthly_savings": f"{result.estimated_monthly_savings:.2f}",
            "currency": "USD",
            "unpriced_findings": result.unpriced_count,
            "error_count": len(result.errors),
            "partial": result.is_partial,
            "price_source": price_source,
        },
        "findings": [finding_to_dict(f) for f in findings],
        "errors": [
            {
                "check_id": e.check_id,
                "region": e.region,
                "error_code": e.error_code,
                "message": e.message,
            }
            for e in result.errors
        ],
    }


class JsonRenderer:
    """Emits one JSON document per scan."""

    name = "json"

    def __init__(self, *, indent: int | None = 2, price_source: str | None = None) -> None:
        self.indent = indent
        self.price_source = price_source

    def render(self, result: ScanResult, config: Config, stream: TextIO) -> None:
        payload = result_to_dict(result, price_source=self.price_source)
        json.dump(payload, stream, indent=self.indent, sort_keys=False)
        stream.write("\n")
