"""Ageing RDS snapshots.

Same shape as ebs-snapshot-age, and the same cost-honesty reasoning applies:
AllocatedStorage is the source database's provisioned size, not what the
snapshot's backup storage actually consumes, so cost is reported as an
upper bound, never exact. See docs/DECISIONS.md §5.

Restricted to manual snapshots. Automated snapshots are AWS-managed with a
retention window capped at 35 days, so one old enough to trip even the
default 90-day threshold cannot exist in practice -- the waste pattern this
check targets is a manual snapshot someone took and forgot about, which
persists indefinitely unless deleted.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import RDS_SNAPSHOT_GB_MONTH
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")


@register
class AgingRdsSnapshots:
    id = "rds-snapshot-age"
    title = "Ageing RDS snapshots"
    service = "rds"
    scope = Scope.REGIONAL
    required_actions = frozenset({"rds:DescribeDBSnapshots"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        min_age_days = int(ctx.option("min_age_days", 90))
        high_severity_days = int(ctx.option("high_severity_days", 365))

        paginator = ctx.client().get_paginator("describe_db_snapshots")
        pages = paginator.paginate(SnapshotType="manual")

        for page in pages:
            for snapshot in page.get("DBSnapshots", []):
                if snapshot.get("Status") != "available":
                    continue
                finding = self._evaluate(ctx, snapshot, min_age_days, high_severity_days)
                if finding is not None:
                    yield finding

    def _evaluate(
        self,
        ctx: ScanContext,
        snapshot: dict,
        min_age_days: int,
        high_severity_days: int,
    ) -> Finding | None:
        snapshot_id = snapshot["DBSnapshotIdentifier"]
        size_gib = int(snapshot.get("AllocatedStorage", 0))
        db_instance_id = snapshot.get("DBInstanceIdentifier", "")
        engine = snapshot.get("Engine", "")
        age = ctx.age_days(snapshot.get("SnapshotCreateTime"))

        if age is None or age < min_age_days:
            return None

        unit_price = ctx.price(RDS_SNAPSHOT_GB_MONTH)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * size_gib).quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = CostConfidence.UPPER_BOUND

        severity = Severity.HIGH if age >= high_severity_days else Severity.MEDIUM

        detail = (
            f"{size_gib} GiB {engine} snapshot of "
            f"{db_instance_id or 'a deleted instance'}, created {age} days ago"
        )
        if confidence is CostConfidence.UPPER_BOUND:
            detail += (
                "; cost is an upper bound -- snapshot backup storage bills "
                "incrementally, which the API does not report"
            )
        elif confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {RDS_SNAPSHOT_GB_MONTH} in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in snapshot.get("TagList", []) if "Key" in t}
        metadata = {
            "db_instance_id": db_instance_id,
            "engine": engine,
            "size_gib": str(size_gib),
            "age_days": str(age),
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=snapshot_id,
            resource_type="AWS::RDS::DBSnapshot",
            region=ctx.region,
            severity=severity,
            title="Ageing RDS snapshot",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
