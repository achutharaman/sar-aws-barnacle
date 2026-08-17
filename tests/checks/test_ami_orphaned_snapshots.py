"""Tests for the AMI-orphaned-snapshot check.

The interesting assertions: only the exact "Created by CreateImage(...) for
ami-xxx from vol-xxx" description is matched (moto's own create_image()
does not reproduce this wording, so descriptions are hand-crafted via
create_snapshot directly), a snapshot referencing an AMI that still exists
is never reported, and a snapshot whose source volume is *also* gone is
excluded here -- that's snapshot-orphaned's case, not this check's, per the
dedup rule in the module docstring.
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.ami_orphaned_snapshots import OrphanedAmiSnapshots
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def _description_for(ami_id: str, instance_id: str = "i-0123456789abcdef0") -> str:
    return f"Created by CreateImage({instance_id}) for {ami_id} from vol-0123456789abcdef0"


def test_check_metadata_is_well_formed():
    assert OrphanedAmiSnapshots.id == "ami-orphaned-snapshots"
    assert OrphanedAmiSnapshots.scope is Scope.REGIONAL
    assert OrphanedAmiSnapshots.required_actions == frozenset(
        {
            "ec2:DescribeSnapshots",
            "ec2:DescribeImages",
            "ec2:DescribeVolumes",
            "sts:GetCallerIdentity",
        }
    )


def test_extract_ami_id_matches_real_aws_description():
    description = _description_for("ami-0d47d489e6ff3c290")
    assert OrphanedAmiSnapshots._extract_ami_id(description) == "ami-0d47d489e6ff3c290"


def test_extract_ami_id_ignores_non_matching_descriptions():
    # moto's own wording, RegisterImage-preserved descriptions, and manual
    # descriptions all fall through as false negatives, not false positives.
    assert OrphanedAmiSnapshots._extract_ami_id("Auto-created snapshot for AMI ami-1234") is None
    assert OrphanedAmiSnapshots._extract_ami_id("nightly backup") is None
    assert OrphanedAmiSnapshots._extract_ami_id("") is None


@mock_aws
def test_reports_snapshot_whose_ami_no_longer_exists(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=50, VolumeType="gp3")
    volume_id = vol["VolumeId"]
    snapshot_id = ec2.create_snapshot(
        VolumeId=volume_id, Description=_description_for("ami-0123456789abcdef0")
    )["SnapshotId"]
    # Source volume stays alive -- only the AMI is gone.

    findings = list(OrphanedAmiSnapshots().run(make_context("ami-orphaned-snapshots")))

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
    assert finding.metadata["orphaned_ami_id"] == "ami-0123456789abcdef0"
    assert finding.metadata["volume_id"] == volume_id


@mock_aws
def test_ignores_snapshot_whose_ami_still_exists(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=50, VolumeType="gp3")
    instance = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"][0]
    image_id = ec2.create_image(InstanceId=instance["InstanceId"], Name="still-here")["ImageId"]
    ec2.create_snapshot(VolumeId=vol["VolumeId"], Description=_description_for(image_id))

    findings = list(OrphanedAmiSnapshots().run(make_context("ami-orphaned-snapshots")))

    assert findings == []


@mock_aws
def test_ignores_snapshot_with_non_matching_description(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=50, VolumeType="gp3")
    ec2.create_snapshot(VolumeId=vol["VolumeId"], Description="manual nightly backup")

    findings = list(OrphanedAmiSnapshots().run(make_context("ami-orphaned-snapshots")))

    assert findings == []


@mock_aws
def test_defers_to_snapshot_orphaned_when_source_volume_is_also_gone(make_context):
    """If the volume is gone too, snapshot-orphaned already reports it --
    this check must stay silent to avoid double-reporting the same snapshot."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=50, VolumeType="gp3")
    ec2.create_snapshot(
        VolumeId=vol["VolumeId"], Description=_description_for("ami-0123456789abcdef0")
    )
    ec2.delete_volume(VolumeId=vol["VolumeId"])

    findings = list(OrphanedAmiSnapshots().run(make_context("ami-orphaned-snapshots")))

    assert findings == []


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    vol = ec2.create_volume(AvailabilityZone="eu-north-1a", Size=500, VolumeType="gp3")
    ec2.create_snapshot(
        VolumeId=vol["VolumeId"], Description=_description_for("ami-0123456789abcdef0")
    )

    ctx = make_context("ami-orphaned-snapshots", region=unpriced_region)
    findings = list(OrphanedAmiSnapshots().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


@mock_aws
def test_reports_every_orphaned_ami_snapshot(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for i in range(3):
        vol = ec2.create_volume(AvailabilityZone=AZ, Size=10, VolumeType="gp3")
        ec2.create_snapshot(
            VolumeId=vol["VolumeId"], Description=_description_for(f"ami-000000000000000{i}0")
        )

    findings = list(OrphanedAmiSnapshots().run(make_context("ami-orphaned-snapshots")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
