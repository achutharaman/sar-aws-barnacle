"""Unattached EBS volumes.

The cleanest waste signal in AWS: a volume in the ``available`` state is
attached to nothing, is doing nothing, and is billed in full every hour.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from aws_barnacle.context import ScanContext
from aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from aws_barnacle.pricing.base import EBS_GB_MONTH
from aws_barnacle.registry import register

# io1/io2 bill for provisioned IOPS on top of storage, and gp3 bills separately
# for IOPS/throughput above the free baseline. We only price storage, so for
# these types our number is a floor, not an estimate.
IOPS_BILLED_TYPES = frozenset({"io1", "io2"})

CENTS = Decimal("0.01")


@register
class UnattachedEbsVolumes:
    id = "ebs-unattached"
    title = "Unattached EBS volumes"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeVolumes"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        high_cost_threshold = Decimal(str(ctx.option("high_severity_usd", 20)))
        min_age_days = int(ctx.option("min_age_days", 0))

        paginator = ctx.client().get_paginator("describe_volumes")
        pages = paginator.paginate(
            Filters=[{"Name": "status", "Values": ["available"]}],
        )

        for page in pages:
            for volume in page.get("Volumes", []):
                finding = self._evaluate(ctx, volume, high_cost_threshold, min_age_days)
                if finding is not None:
                    yield finding

    def _evaluate(
        self,
        ctx: ScanContext,
        volume: dict,
        high_cost_threshold: Decimal,
        min_age_days: int,
    ) -> Finding | None:
        volume_id = volume["VolumeId"]
        size_gib = int(volume.get("Size", 0))
        volume_type = volume.get("VolumeType", "gp2")
        age = ctx.age_days(volume.get("CreateTime"))

        if min_age_days and (age is None or age < min_age_days):
            return None

        unit_price = ctx.price(EBS_GB_MONTH, volume_type)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * size_gib).quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = (
                CostConfidence.LOWER_BOUND
                if volume_type in IOPS_BILLED_TYPES
                else CostConfidence.EXACT
            )

        severity = self._severity(monthly_cost, age, high_cost_threshold)
        tags = {t["Key"]: t["Value"] for t in volume.get("Tags", []) if "Key" in t}

        detail = f"{size_gib} GiB {volume_type} volume in state 'available' (attached to nothing)"
        if age is not None:
            detail += f", created {age} days ago"
        if confidence is CostConfidence.LOWER_BOUND:
            detail += "; storage cost only, provisioned IOPS charges not included"
        elif confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {volume_type} in {ctx.region}"

        metadata = {
            "volume_type": volume_type,
            "size_gib": str(size_gib),
            "availability_zone": volume.get("AvailabilityZone", ""),
            "encrypted": str(volume.get("Encrypted", False)).lower(),
            "snapshot_id": volume.get("SnapshotId", "") or "",
        }
        if age is not None:
            metadata["age_days"] = str(age)
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=volume_id,
            resource_type="AWS::EC2::Volume",
            region=ctx.region,
            severity=severity,
            title="Unattached EBS volume",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )

    @staticmethod
    def _severity(cost: Decimal | None, age: int | None, threshold: Decimal) -> Severity:
        """Severity tracks money first, then how long it has been ignored.

        An unpriced volume is not harmless -- it is an unknown -- so it lands at
        MEDIUM rather than being quietly downgraded.
        """
        if cost is not None and cost >= threshold:
            return Severity.HIGH
        if age is not None and age >= 90:
            return Severity.HIGH
        return Severity.MEDIUM
