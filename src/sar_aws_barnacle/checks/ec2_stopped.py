"""Long-stopped EC2 instances.

A stopped instance doesn't bill for compute -- AWS only charges for its
attached EBS storage while it sits idle. This check reports that storage
cost, not a phantom compute charge; an instance with no billable storage
correctly reports $0, not UNKNOWN.

"How long stopped" has no reliable, structured source. describe_instances
has no StoppedTime field; LaunchTime is when the instance was *launched*,
not last stopped, so it misreports an old instance stopped yesterday as
having been idle for years. The only place a stop date appears is
StateTransitionReason, e.g. "User initiated (2026-08-15 07:19:54 UTC)" --
a field AWS documents as descriptive text, not a stable API contract, and
whose format varies by *why* the instance stopped (a Spot interruption or a
failed health check don't always embed a parseable date). Parsed
best-effort: every stopped instance is still reported either way, with
MEDIUM severity when the age genuinely can't be determined rather than a
confident-looking guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import EBS_GB_MONTH
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")

# "User initiated (2026-08-15 07:19:54 UTC)" -- best-effort only; see the
# module docstring for why this can't be trusted as a stable contract.
_STATE_TRANSITION_TIMESTAMP = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


@register
class StoppedEc2Instances:
    id = "ec2-stopped"
    title = "Long-stopped EC2 instances"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeInstances", "ec2:DescribeVolumes"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        paginator = ctx.client().get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}],
        )
        instances = [
            instance
            for page in pages
            for reservation in page.get("Reservations", [])
            for instance in reservation.get("Instances", [])
        ]
        if not instances:
            return

        volumes = self._attached_volumes(ctx, instances)
        for instance in instances:
            yield self._evaluate(ctx, instance, volumes)

    def _attached_volumes(self, ctx: ScanContext, instances: list[dict]) -> dict[str, dict]:
        """volume_id -> volume, for every EBS volume attached to any of
        these stopped instances. One batched DescribeVolumes call rather
        than one per instance."""
        volume_ids = [
            mapping["Ebs"]["VolumeId"]
            for instance in instances
            for mapping in instance.get("BlockDeviceMappings", [])
            if "Ebs" in mapping
        ]
        if not volume_ids:
            return {}

        volumes: dict[str, dict] = {}
        paginator = ctx.client().get_paginator("describe_volumes")
        for page in paginator.paginate(VolumeIds=volume_ids):
            for volume in page.get("Volumes", []):
                volumes[volume["VolumeId"]] = volume
        return volumes

    def _evaluate(self, ctx: ScanContext, instance: dict, volumes: dict[str, dict]) -> Finding:
        instance_id = instance["InstanceId"]
        instance_type = instance.get("InstanceType", "")
        attached_volume_ids = [
            m["Ebs"]["VolumeId"] for m in instance.get("BlockDeviceMappings", []) if "Ebs" in m
        ]

        stopped_since = self._stopped_since(instance)
        age_days = ctx.age_days(stopped_since) if stopped_since is not None else None

        total_cost, confidence = self._storage_cost(ctx, attached_volume_ids, volumes)

        if age_days is None:
            severity = Severity.MEDIUM
        elif age_days >= 1:
            severity = Severity.HIGH
        else:
            severity = Severity.LOW

        detail = f"{instance_type} instance stopped"
        detail += f" {age_days} day(s) ago" if age_days is not None else ", stop time unknown"
        if attached_volume_ids:
            detail += f"; {len(attached_volume_ids)} attached EBS volume(s) still billing"
        if confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for one or more volume types in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", []) if "Key" in t}
        metadata = {
            "instance_type": instance_type,
            "attached_volume_count": str(len(attached_volume_ids)),
        }
        if age_days is not None:
            metadata["stopped_days"] = str(age_days)
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=instance_id,
            resource_type="AWS::EC2::Instance",
            region=ctx.region,
            severity=severity,
            title="Long-stopped EC2 instance",
            detail=detail,
            estimated_monthly_cost=total_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )

    @staticmethod
    def _storage_cost(
        ctx: ScanContext, volume_ids: list[str], volumes: dict[str, dict]
    ) -> tuple[Decimal | None, CostConfidence]:
        total = Decimal(0)
        for volume_id in volume_ids:
            volume = volumes.get(volume_id, {})
            size_gib = int(volume.get("Size", 0))
            volume_type = volume.get("VolumeType", "gp2")
            unit_price = ctx.price(EBS_GB_MONTH, volume_type)
            if unit_price is None:
                return None, CostConfidence.UNKNOWN
            total += unit_price * size_gib
        # No unpriced volume: even zero attached volumes is an honest EXACT
        # $0, not an unknown -- there is nothing left to price.
        return total.quantize(CENTS, rounding=ROUND_HALF_UP), CostConfidence.EXACT

    @staticmethod
    def _stopped_since(instance: dict) -> datetime | None:
        reason = instance.get("StateTransitionReason", "")
        match = _STATE_TRANSITION_TIMESTAMP.search(reason)
        if match is None:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
