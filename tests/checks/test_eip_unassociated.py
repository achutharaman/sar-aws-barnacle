"""Tests for the unassociated-EIP check.

The interesting assertions are the ones about cost honesty (an unpriceable
region yields None, not a plausible-looking zero) and about the missing age
signal: unlike ebs-unattached, this check has no min_age_days option because
DescribeAddresses has no allocation timestamp to filter on.
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.eip_unassociated import UnassociatedElasticIps
from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"


def _allocate(ec2) -> tuple[str, str]:
    resp = ec2.allocate_address(Domain="vpc")
    return resp["AllocationId"], resp["PublicIp"]


def test_check_metadata_is_well_formed():
    assert UnassociatedElasticIps.id == "eip-unassociated"
    assert UnassociatedElasticIps.scope is Scope.REGIONAL
    assert UnassociatedElasticIps.required_actions == frozenset({"ec2:DescribeAddresses"})


@mock_aws
def test_reports_unassociated_eip_with_exact_cost(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    allocation_id, public_ip = _allocate(ec2)

    findings = list(UnassociatedElasticIps().run(make_context("eip-unassociated")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == allocation_id
    assert finding.resource_type == "AWS::EC2::EIP"
    assert finding.region == REGION
    # seed eip-idle-hour price is 0.005/hr * 730 hours.
    assert finding.estimated_monthly_cost == Decimal("3.65")
    assert finding.cost_confidence is CostConfidence.EXACT
    assert finding.metadata["public_ip"] == public_ip
    assert finding.metadata["domain"] == "vpc"


@mock_aws
def test_ignores_associated_eips(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    instance = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)
    instance_id = instance["Instances"][0]["InstanceId"]
    allocation_id, _ = _allocate(ec2)
    ec2.associate_address(InstanceId=instance_id, AllocationId=allocation_id)

    findings = list(UnassociatedElasticIps().run(make_context("eip-unassociated")))

    assert findings == []


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    _allocate(ec2)

    ctx = make_context("eip-unassociated", region=unpriced_region)
    findings = list(UnassociatedElasticIps().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert findings[0].severity is Severity.MEDIUM
    assert "no price available" in findings[0].detail


@mock_aws
def test_high_severity_threshold_is_configurable(make_context):
    """Idle-EIP cost is flat, so the default threshold rarely fires -- this
    proves the knob still works when a caller lowers it."""
    ec2 = boto3.client("ec2", region_name=REGION)
    _allocate(ec2)

    config = Config(options={"eip-unassociated": {"high_severity_usd": 1}})
    findings = list(UnassociatedElasticIps().run(make_context("eip-unassociated", config=config)))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_default_threshold_is_medium_severity(make_context):
    """The flat ~$3.65/month rate sits well under the default $20
    threshold, so an unassociated EIP is MEDIUM out of the box."""
    ec2 = boto3.client("ec2", region_name=REGION)
    _allocate(ec2)

    findings = list(UnassociatedElasticIps().run(make_context("eip-unassociated")))

    assert findings[0].severity is Severity.MEDIUM


@mock_aws
def test_reports_every_unassociated_eip(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for _ in range(3):
        _allocate(ec2)

    findings = list(UnassociatedElasticIps().run(make_context("eip-unassociated")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
