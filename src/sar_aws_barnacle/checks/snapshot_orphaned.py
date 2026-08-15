"""EBS snapshots whose source volume no longer exists.

Distinct from ebs-snapshot-age: an old snapshot of a live volume may be a
deliberate backup, but a snapshot of a deleted volume is usually forgotten
debris -- orphaned in a way age alone doesn't capture.

Cost is CostConfidence.UPPER_BOUND, always -- the canonical case from
docs/DECISIONS.md §5. Snapshots bill on *incremental* consumed storage, but
describe_snapshots only returns VolumeSize, the size of the source volume
(which, here, doesn't even exist anymore to compare against). Any tool
reporting an exact cost for this is guessing.

AMI-backed snapshots are excluded entirely, not flagged with softer
wording. A snapshot backing a registered AMI has an active purpose even
though its original source volume is gone -- it isn't "orphaned" in any
useful sense, the same way ebs-unattached excludes attached volumes rather
than flagging them with a caveat. Deleting one breaks the AMI.

Filtered by owner-id explicitly (via one sts:GetCallerIdentity call,
already a baseline action) rather than DescribeSnapshots's
OwnerIds=["self"] keyword -- same moto limitation as ebs-snapshot-age: moto
does not implement that keyword and returns its entire seeded public-AMI
snapshot catalog instead. describe_images(Owners=["self"]) does not have
this problem and is used as-is.
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
class OrphanedSnapshots:
    id = "snapshot-orphaned"
    title = "Orphaned EBS snapshots"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset(
        {
            "ec2:DescribeSnapshots",
            "ec2:DescribeVolumes",
            "ec2:DescribeImages",
            "sts:GetCallerIdentity",
        }
    )

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        account_id = ctx.client("sts").get_caller_identity()["Account"]

        existing_volume_ids = self._existing_volume_ids(ctx)
        ami_backed_snapshot_ids = self._ami_backed_snapshot_ids(ctx)

        paginator = ctx.client().get_paginator("describe_snapshots")
        pages = paginator.paginate(
            Filters=[{"Name": "owner-id", "Values": [account_id]}],
        )

        for page in pages:
            for snapshot in page.get("Snapshots", []):
                if snapshot.get("State") != "completed":
                    continue
                if snapshot.get("VolumeId") in existing_volume_ids:
                    continue
                if snapshot["SnapshotId"] in ami_backed_snapshot_ids:
                    continue
                yield self._evaluate(ctx, snapshot)

    def _existing_volume_ids(self, ctx: ScanContext) -> set[str]:
        paginator = ctx.client().get_paginator("describe_volumes")
        return {
            volume["VolumeId"]
            for page in paginator.paginate()
            for volume in page.get("Volumes", [])
        }

    def _ami_backed_snapshot_ids(self, ctx: ScanContext) -> set[str]:
        images = ctx.client().describe_images(Owners=["self"])
        return self._extract_backing_snapshot_ids(images.get("Images", []))

    @staticmethod
    def _extract_backing_snapshot_ids(images: list[dict]) -> set[str]:
        return {
            mapping["Ebs"]["SnapshotId"]
            for image in images
            for mapping in image.get("BlockDeviceMappings", [])
            if "Ebs" in mapping and "SnapshotId" in mapping["Ebs"]
        }

    def _evaluate(self, ctx: ScanContext, snapshot: dict) -> Finding:
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

        detail = f"{size_gib} GiB snapshot's source volume ({volume_id}) no longer exists"
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
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=snapshot_id,
            resource_type="AWS::EC2::Snapshot",
            region=ctx.region,
            severity=Severity.MEDIUM,
            title="Orphaned EBS snapshot",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
