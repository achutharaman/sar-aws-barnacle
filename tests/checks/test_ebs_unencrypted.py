"""Tests for the unencrypted-EBS check.

The interesting assertion is not "it finds a volume" -- it's that this check
never invents a cost. Encryption status doesn't affect price, so every
finding must carry CostConfidence.UNKNOWN, not a plausible-looking number.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.ebs_unencrypted import UnencryptedEbsVolumes
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def _create_volume(ec2, *, encrypted: bool, size: int = 100, volume_type: str = "gp3") -> str:
    volume = ec2.create_volume(
        AvailabilityZone=AZ, Size=size, VolumeType=volume_type, Encrypted=encrypted
    )
    return volume["VolumeId"]


def test_check_metadata_is_well_formed():
    assert UnencryptedEbsVolumes.id == "ebs-unencrypted"
    assert UnencryptedEbsVolumes.scope is Scope.REGIONAL
    assert UnencryptedEbsVolumes.required_actions == frozenset({"ec2:DescribeVolumes"})


@mock_aws
def test_reports_unencrypted_volume_with_unknown_cost(make_context):
    """Encryption status doesn't affect price -- reporting a number here
    would be a fabrication, not a finding."""
    ec2 = boto3.client("ec2", region_name=REGION)
    volume_id = _create_volume(ec2, encrypted=False)

    findings = list(UnencryptedEbsVolumes().run(make_context("ebs-unencrypted")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == volume_id
    assert finding.resource_type == "AWS::EC2::Volume"
    assert finding.region == REGION
    assert finding.estimated_monthly_cost is None
    assert finding.cost_confidence is CostConfidence.UNKNOWN


@mock_aws
def test_ignores_encrypted_volumes(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, encrypted=True)

    findings = list(UnencryptedEbsVolumes().run(make_context("ebs-unencrypted")))

    assert findings == []


@mock_aws
def test_attached_unencrypted_volume_is_high_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    instance = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)
    instance_id = instance["Instances"][0]["InstanceId"]
    volume_id = _create_volume(ec2, encrypted=False)
    ec2.attach_volume(Device="/dev/sdh", InstanceId=instance_id, VolumeId=volume_id)

    findings = list(UnencryptedEbsVolumes().run(make_context("ebs-unencrypted")))

    assert findings[0].severity is Severity.HIGH
    assert findings[0].metadata["attached"] == "true"
    assert findings[0].metadata["instance_id"] == instance_id
    assert "attached and in use" in findings[0].detail


@mock_aws
def test_unattached_unencrypted_volume_is_medium_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, encrypted=False)

    findings = list(UnencryptedEbsVolumes().run(make_context("ebs-unencrypted")))

    assert findings[0].severity is Severity.MEDIUM
    assert findings[0].metadata["attached"] == "false"
    assert "instance_id" not in findings[0].metadata
    assert "not attached" in findings[0].detail


@mock_aws
def test_reports_every_unencrypted_volume(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for _ in range(3):
        _create_volume(ec2, encrypted=False, size=10)
    _create_volume(ec2, encrypted=True, size=10)

    findings = list(UnencryptedEbsVolumes().run(make_context("ebs-unencrypted")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3


@mock_aws
def test_reports_age_when_available(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, encrypted=False)

    ctx = make_context("ebs-unencrypted", at=datetime(2026, 8, 12, tzinfo=UTC))
    findings = list(UnencryptedEbsVolumes().run(ctx))

    assert findings[0].metadata["age_days"] is not None
    assert "created" in findings[0].detail
