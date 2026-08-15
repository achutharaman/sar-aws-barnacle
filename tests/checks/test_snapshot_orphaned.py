"""Tests for the orphaned-snapshot check.

The interesting assertions: cost is UPPER_BOUND, always (the canonical case
from docs/DECISIONS.md §5), a snapshot whose source volume still exists is
never reported (that's what ebs-snapshot-age is for), and an AMI-backed
snapshot is excluded entirely rather than flagged with softer wording.

Note on the AMI-exclusion tests: moto's register_image does not honour a
caller-supplied SnapshotId in BlockDeviceMappings -- it synthesizes its own
(verified by inspecting the actual response), so the real AMI-backed
relationship can't be constructed through the API. _ami_backed_snapshot_ids
is monkeypatched directly to prove run() itself respects the exclusion once
that set is populated, and _extract_backing_snapshot_ids (the pure parsing
logic) is unit-tested separately with hand-built data.
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.snapshot_orphaned import OrphanedSnapshots
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def test_check_metadata_is_well_formed():
    assert OrphanedSnapshots.id == "snapshot-orphaned"
    assert OrphanedSnapshots.scope is Scope.REGIONAL
    assert OrphanedSnapshots.required_actions == frozenset(
        {
            "ec2:DescribeSnapshots",
            "ec2:DescribeVolumes",
            "ec2:DescribeImages",
            "sts:GetCallerIdentity",
        }
    )


def test_extract_backing_snapshot_ids():
    images = [
        {
            "BlockDeviceMappings": [
                {"Ebs": {"SnapshotId": "snap-1"}},
                {"DeviceName": "/dev/sdb"},  # no Ebs key -- e.g. an instance-store mapping
            ]
        },
        {"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-2"}}]},
        {"BlockDeviceMappings": []},
    ]
    assert OrphanedSnapshots._extract_backing_snapshot_ids(images) == {"snap-1", "snap-2"}


@mock_aws
def test_reports_snapshot_with_deleted_source_volume(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=50, VolumeType="gp3")
    volume_id = vol["VolumeId"]
    snapshot_id = ec2.create_snapshot(VolumeId=volume_id)["SnapshotId"]
    ec2.delete_volume(VolumeId=volume_id)

    findings = list(OrphanedSnapshots().run(make_context("snapshot-orphaned")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == snapshot_id
    assert finding.resource_type == "AWS::EC2::Snapshot"
    assert finding.region == REGION
    # ap-south-1 seed price is 0.0500/GiB-month * 50 GiB.
    assert finding.estimated_monthly_cost == Decimal("2.50")
    assert finding.cost_confidence is CostConfidence.UPPER_BOUND
    assert finding.severity is Severity.MEDIUM
    assert "upper bound" in finding.detail
    assert volume_id in finding.metadata["volume_id"]


@mock_aws
def test_ignores_snapshot_with_existing_source_volume(make_context):
    """This is what ebs-snapshot-age is for -- a live source volume means
    the snapshot might be a deliberate backup, not orphaned debris."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=50, VolumeType="gp3")
    ec2.create_snapshot(VolumeId=vol["VolumeId"])

    findings = list(OrphanedSnapshots().run(make_context("snapshot-orphaned")))

    assert findings == []


@mock_aws
def test_ami_backed_snapshot_is_excluded(make_context, monkeypatch):
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=20, VolumeType="gp3")
    snapshot_id = ec2.create_snapshot(VolumeId=vol["VolumeId"])["SnapshotId"]
    ec2.delete_volume(VolumeId=vol["VolumeId"])

    monkeypatch.setattr(
        OrphanedSnapshots, "_ami_backed_snapshot_ids", lambda self, ctx: {snapshot_id}
    )

    findings = list(OrphanedSnapshots().run(make_context("snapshot-orphaned")))

    assert findings == []


@mock_aws
def test_unrelated_ami_does_not_exclude_other_orphans(make_context, monkeypatch):
    """The exclusion is per-snapshot-id, not "any AMI exists in the
    account" -- an orphan that isn't backing anything must still show up."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=20, VolumeType="gp3")
    snapshot_id = ec2.create_snapshot(VolumeId=vol["VolumeId"])["SnapshotId"]
    ec2.delete_volume(VolumeId=vol["VolumeId"])

    monkeypatch.setattr(
        OrphanedSnapshots, "_ami_backed_snapshot_ids", lambda self, ctx: {"snap-unrelated"}
    )

    findings = list(OrphanedSnapshots().run(make_context("snapshot-orphaned")))

    assert len(findings) == 1
    assert findings[0].resource_id == snapshot_id


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    vol = ec2.create_volume(AvailabilityZone="eu-north-1a", Size=500, VolumeType="gp3")
    ec2.create_snapshot(VolumeId=vol["VolumeId"])
    ec2.delete_volume(VolumeId=vol["VolumeId"])

    ctx = make_context("snapshot-orphaned", region=unpriced_region)
    findings = list(OrphanedSnapshots().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


@mock_aws
def test_reports_every_orphaned_snapshot(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for _ in range(3):
        vol = ec2.create_volume(AvailabilityZone=AZ, Size=10, VolumeType="gp3")
        ec2.create_snapshot(VolumeId=vol["VolumeId"])
        ec2.delete_volume(VolumeId=vol["VolumeId"])

    findings = list(OrphanedSnapshots().run(make_context("snapshot-orphaned")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
