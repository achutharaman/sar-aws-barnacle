"""Unencrypted EBS volumes.

This is a security/hygiene signal, not a cost one -- encryption status has no
bearing on what a volume costs. Every finding here carries
``CostConfidence.UNKNOWN``: not a gap in pricing data, but an honest
statement that this check has no cost claim to make at all.
"""

from __future__ import annotations

from collections.abc import Iterator

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.registry import register


@register
class UnencryptedEbsVolumes:
    id = "ebs-unencrypted"
    title = "Unencrypted EBS volumes"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeVolumes"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        paginator = ctx.client().get_paginator("describe_volumes")
        pages = paginator.paginate(
            Filters=[{"Name": "encrypted", "Values": ["false"]}],
        )

        for page in pages:
            for volume in page.get("Volumes", []):
                yield self._evaluate(ctx, volume)

    def _evaluate(self, ctx: ScanContext, volume: dict) -> Finding:
        volume_id = volume["VolumeId"]
        size_gib = int(volume.get("Size", 0))
        volume_type = volume.get("VolumeType", "gp2")
        attachments = volume.get("Attachments", [])
        attached = bool(attachments)
        age = ctx.age_days(volume.get("CreateTime"))

        # An attached volume is actively serving a running workload with
        # unencrypted data at rest right now; an unattached one is still a
        # finding, but nothing is reading or writing to it at this moment.
        severity = Severity.HIGH if attached else Severity.MEDIUM

        detail = f"{size_gib} GiB {volume_type} volume is not encrypted"
        detail += ", attached and in use" if attached else ", not attached"
        if age is not None:
            detail += f", created {age} days ago"

        tags = {t["Key"]: t["Value"] for t in volume.get("Tags", []) if "Key" in t}
        metadata = {
            "volume_type": volume_type,
            "size_gib": str(size_gib),
            "availability_zone": volume.get("AvailabilityZone", ""),
            "attached": str(attached).lower(),
        }
        if attached:
            metadata["instance_id"] = attachments[0].get("InstanceId", "")
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
            title="Unencrypted EBS volume",
            detail=detail,
            estimated_monthly_cost=None,
            cost_confidence=CostConfidence.UNKNOWN,
            metadata=metadata,
        )
