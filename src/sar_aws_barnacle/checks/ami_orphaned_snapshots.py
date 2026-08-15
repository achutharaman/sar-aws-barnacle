"""EBS snapshots left behind when the AMI they were created for is deregistered.

AWS auto-generates a fixed, immutable Description on the snapshot(s)
CreateImage takes of an instance-backed AMI:

    Created by CreateImage(i-xxxxxxxxxxxxxxxxx) for ami-xxxxxxxxxxxxxxxxx
    from vol-xxxxxxxxxxxxxxxxx

DeregisterImage does not delete these snapshots, so they accumulate
silently. This check matches that exact, anchored pattern -- no fuzzy or
case-insensitive fallback -- and only reports a snapshot when the
referenced AMI no longer exists. A non-matching description (snapshots
from RegisterImage against a pre-existing snapshot, or from cross-region
CopyImage, which use different wording) is skipped entirely: false
negatives, never false positives. The description is undocumented but
stable -- there is no API to edit a snapshot's description after creation,
so a match found today cannot be invalidated by a later edit the way
ec2-stopped's StateTransitionReason parsing can be.

moto's own create_image() does not reproduce this wording (it generates
"Auto-created snapshot for AMI ami-xxx" instead) and, like
describe_snapshots(OwnerIds=["self"]), leaks moto's seeded public-AMI
catalog. Tests construct the description directly via
create_snapshot(..., Description=...) rather than relying on moto's
create_image() side effects.

Deduplicated against snapshot-orphaned: the only overlap is a snapshot
whose source volume is *also* gone, which snapshot-orphaned already claims
(its AMI-backing exclusion only covers snapshots backing a currently
existing AMI). This check only reports a snapshot when its VolumeId still
exists -- the "AMI gone, volume still there" case snapshot-orphaned never
touches.

Cost is CostConfidence.UPPER_BOUND, always -- same incremental-billing
caveat as snapshot-orphaned (VolumeSize is the source volume's size, not
what the snapshot's incremental storage actually consumes).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import EBS_SNAPSHOT_GB_MONTH
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")
_AMI_SNAPSHOT_PATTERN = re.compile(r"Created by CreateImage\([^)]+\) for (ami-[0-9a-f]+) from")


@register
class OrphanedAmiSnapshots:
    id = "ami-orphaned-snapshots"
    title = "Snapshots orphaned by AMI deregistration"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset(
        {
            "ec2:DescribeSnapshots",
            "ec2:DescribeImages",
            "ec2:DescribeVolumes",
            "sts:GetCallerIdentity",
        }
    )

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        account_id = ctx.client("sts").get_caller_identity()["Account"]

        existing_ami_ids = self._existing_ami_ids(ctx)
        existing_volume_ids = self._existing_volume_ids(ctx)

        paginator = ctx.client().get_paginator("describe_snapshots")
        pages = paginator.paginate(
            Filters=[{"Name": "owner-id", "Values": [account_id]}],
        )

        for page in pages:
            for snapshot in page.get("Snapshots", []):
                if snapshot.get("State") != "completed":
                    continue
                ami_id = self._extract_ami_id(snapshot.get("Description", ""))
                if ami_id is None:
                    continue
                if ami_id in existing_ami_ids:
                    continue
                if snapshot.get("VolumeId") not in existing_volume_ids:
                    continue
                yield self._evaluate(ctx, snapshot, ami_id)

    def _existing_ami_ids(self, ctx: ScanContext) -> set[str]:
        images = ctx.client().describe_images(Owners=["self"])
        return {image["ImageId"] for image in images.get("Images", [])}

    def _existing_volume_ids(self, ctx: ScanContext) -> set[str]:
        paginator = ctx.client().get_paginator("describe_volumes")
        return {
            volume["VolumeId"]
            for page in paginator.paginate()
            for volume in page.get("Volumes", [])
        }

    @staticmethod
    def _extract_ami_id(description: str) -> str | None:
        match = _AMI_SNAPSHOT_PATTERN.search(description)
        return match.group(1) if match else None

    def _evaluate(self, ctx: ScanContext, snapshot: dict, ami_id: str) -> Finding:
        snapshot_id = snapshot["SnapshotId"]
        size_gib = int(snapshot.get("VolumeSize", 0))
        volume_id = snapshot.get("VolumeId", "")

        unit_price = ctx.price(EBS_SNAPSHOT_GB_MONTH)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * size_gib).quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = CostConfidence.UPPER_BOUND

        detail = f"{size_gib} GiB snapshot was created for {ami_id}, which no longer exists"
        if confidence is CostConfidence.UPPER_BOUND:
            detail += (
                "; cost is an upper bound -- snapshots bill on incremental storage, "
                "which the API does not report, and the real cost is lower"
            )
        elif confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {EBS_SNAPSHOT_GB_MONTH} in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in snapshot.get("Tags", []) if "Key" in t}
        metadata = {
            "volume_id": volume_id,
            "size_gib": str(size_gib),
            "orphaned_ami_id": ami_id,
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=snapshot_id,
            resource_type="AWS::EC2::Snapshot",
            region=ctx.region,
            severity=Severity.MEDIUM,
            title="Snapshot orphaned by AMI deregistration",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
