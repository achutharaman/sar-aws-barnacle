"""Tests for the unattached-EBS check.

The interesting assertions are not "it finds a volume" -- they are the ones
about *cost honesty*: that an io2 volume is flagged as a lower bound, and that
an unpriceable region yields ``None`` rather than a plausible-looking zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from sar_aws_barnacle.checks.ebs_unattached import UnattachedEbsVolumes
from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def _create_volume(ec2, size: int = 100, volume_type: str = "gp3", **kwargs) -> str:
    volume = ec2.create_volume(AvailabilityZone=AZ, Size=size, VolumeType=volume_type, **kwargs)
    return volume["VolumeId"]


def test_check_metadata_is_well_formed():
    assert UnattachedEbsVolumes.id == "ebs-unattached"
    assert UnattachedEbsVolumes.scope is Scope.REGIONAL
    assert UnattachedEbsVolumes.required_actions == frozenset({"ec2:DescribeVolumes"})


@mock_aws
def test_reports_unattached_volume_with_exact_cost(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    volume_id = _create_volume(ec2, size=100, volume_type="gp3")

    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == volume_id
    assert finding.resource_type == "AWS::EC2::Volume"
    assert finding.region == REGION
    # ap-south-1 gp3 seed price is 0.0912/GiB-month.
    assert finding.estimated_monthly_cost == Decimal("9.12")
    assert finding.cost_confidence is CostConfidence.EXACT
    assert finding.metadata["volume_type"] == "gp3"
    assert finding.metadata["size_gib"] == "100"


@mock_aws
def test_ignores_attached_volumes(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    instance = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)
    instance_id = instance["Instances"][0]["InstanceId"]
    volume_id = _create_volume(ec2)
    ec2.attach_volume(Device="/dev/sdh", InstanceId=instance_id, VolumeId=volume_id)

    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached")))

    assert findings == []


@mock_aws
def test_io2_cost_is_reported_as_a_lower_bound(make_context):
    """io1/io2 bill provisioned IOPS separately, so storage-only is a floor."""
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, size=50, volume_type="io2", Iops=3000)

    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached")))

    assert findings[0].cost_confidence is CostConfidence.LOWER_BOUND
    assert findings[0].estimated_monthly_cost == Decimal("7.13")  # 50 * 0.1425
    assert "provisioned IOPS" in findings[0].detail


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    ec2.create_volume(AvailabilityZone="eu-north-1a", Size=500, VolumeType="gp3")

    ctx = make_context("ebs-unattached", region=unpriced_region)
    findings = list(UnattachedEbsVolumes().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert findings[0].severity is Severity.MEDIUM
    assert "no price available" in findings[0].detail


@mock_aws
def test_expensive_volume_is_high_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, size=1000, volume_type="gp3")  # ~$91.20/month

    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached")))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_high_severity_threshold_is_configurable(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, size=100, volume_type="gp3")  # $9.12/month

    config = Config(options={"ebs-unattached": {"high_severity_usd": 5}})
    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached", config=config)))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_min_age_days_filters_recent_volumes(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, size=100)

    config = Config(options={"ebs-unattached": {"min_age_days": 30}})
    ctx = make_context("ebs-unattached", config=config, at=datetime(2026, 8, 12, tzinfo=UTC))

    assert list(UnattachedEbsVolumes().run(ctx)) == []


@mock_aws
def test_reports_every_unattached_volume(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for _ in range(3):
        _create_volume(ec2, size=10)

    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3


@pytest.mark.parametrize(
    ("volume_type", "size", "expected"),
    [
        ("gp2", 100, Decimal("11.40")),
        ("st1", 500, Decimal("25.65")),
        ("sc1", 500, Decimal("8.70")),
    ],
)
@mock_aws
def test_cost_matches_seed_table_per_volume_type(make_context, volume_type, size, expected):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_volume(ec2, size=size, volume_type=volume_type)

    findings = list(UnattachedEbsVolumes().run(make_context("ebs-unattached")))

    assert findings[0].estimated_monthly_cost == expected
