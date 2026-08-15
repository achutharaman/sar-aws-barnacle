"""Ageing EBS snapshots.

Cost here is an upper bound, not an estimate -- see docs/DECISIONS.md §5.
Snapshots bill on *incremental* consumed storage, but describe_snapshots only
returns VolumeSize, the size of the source volume. Any tool reporting an
exact snapshot cost is guessing; we report age (reliable -- StartTime is a
real structured field, unlike EC2's stop time) and a cost that can only be
too high, never too low, and say so.

Filtered by owner-id explicitly (via one sts:GetCallerIdentity call, already
part of the baseline IAM actions) rather than the OwnerIds=["self"] keyword:
moto does not implement that keyword and returns its entire seeded public-AMI
snapshot catalog instead, which would be untestable. The explicit owner-id
filter is equally correct against real AWS and is what actually gets tested.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import EBS_SNAPSHOT_GB_MONTH
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")


@register
class AgingEbsSnapshots:
    id = "ebs-snapshot-age"
    title = "Ageing EBS snapshots"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeSnapshots", "sts:GetCallerIdentity"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        min_age_days = int(ctx.option("min_age_days", 90))
        high_severity_days = int(ctx.option("high_severity_days", 365))

        account_id = ctx.client("sts").get_caller_identity()["Account"]

        paginator = ctx.client().get_paginator("describe_snapshots")
        pages = paginator.paginate(
            Filters=[{"Name": "owner-id", "Values": [account_id]}],
        )

        for page in pages:
            for snapshot in page.get("Snapshots", []):
                if snapshot.get("State") != "completed":
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
        snapshot_id = snapshot["SnapshotId"]
        size_gib = int(snapshot.get("VolumeSize", 0))
        volume_id = snapshot.get("VolumeId", "")
        age = ctx.age_days(snapshot.get("StartTime"))

        if age is None or age < min_age_days:
            return None

        unit_price = ctx.price(EBS_SNAPSHOT_GB_MONTH)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * size_gib).quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = CostConfidence.UPPER_BOUND

        severity = Severity.HIGH if age >= high_severity_days else Severity.MEDIUM

        detail = (
            f"{size_gib} GiB snapshot of {volume_id or 'a deleted volume'}, created {age} days ago"
        )
        if confidence is CostConfidence.UPPER_BOUND:
            detail += (
                "; cost is an upper bound -- snapshots bill on incremental storage, "
                "which the API does not report"
            )
        elif confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {EBS_SNAPSHOT_GB_MONTH} in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in snapshot.get("Tags", []) if "Key" in t}
        metadata = {
            "volume_id": volume_id,
            "size_gib": str(size_gib),
            "age_days": str(age),
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=snapshot_id,
            resource_type="AWS::EC2::Snapshot",
            region=ctx.region,
            severity=severity,
            title="Ageing EBS snapshot",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
