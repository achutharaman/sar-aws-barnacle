"""Tests for the idle-NAT-Gateway check.

The interesting assertions: a NAT Gateway in a VPC with a running instance
is never reported (this check is a heuristic, not proof), cost is
LOWER_BOUND (data processing charges aren't visible), severity is fixed
HIGH regardless of cost, and the detail text is explicit about the
heuristic's blind spots (Lambda/ECS/RDS can use a NAT Gateway with zero
EC2 instances present).
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.nat_gw_idle import IdleNatGateways
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def _create_vpc_with_nat_gateway(ec2):
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.0.0/24", AvailabilityZone=AZ)[
        "Subnet"
    ]["SubnetId"]
    alloc_id = ec2.allocate_address(Domain="vpc")["AllocationId"]
    gateway_id = ec2.create_nat_gateway(SubnetId=subnet_id, AllocationId=alloc_id)["NatGateway"][
        "NatGatewayId"
    ]
    return vpc_id, subnet_id, gateway_id


def test_check_metadata_is_well_formed():
    assert IdleNatGateways.id == "nat-gw-idle"
    assert IdleNatGateways.scope is Scope.REGIONAL
    assert IdleNatGateways.required_actions == frozenset(
        {"ec2:DescribeNatGateways", "ec2:DescribeInstances"}
    )


@mock_aws
def test_reports_nat_gateway_with_no_running_instances(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnet_id, gateway_id = _create_vpc_with_nat_gateway(ec2)

    findings = list(IdleNatGateways().run(make_context("nat-gw-idle")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == gateway_id
    assert finding.resource_type == "AWS::EC2::NatGateway"
    assert finding.region == REGION
    # ap-south-1 seed price is 0.045/hr * 730 hours.
    assert finding.estimated_monthly_cost == Decimal("32.85")
    assert finding.cost_confidence is CostConfidence.LOWER_BOUND
    assert finding.severity is Severity.HIGH
    assert "heuristic, not proof" in finding.detail
    assert "Lambda" in finding.detail
    assert "lower bound" in finding.detail
    assert finding.metadata["vpc_id"] == vpc_id
    assert finding.metadata["subnet_id"] == subnet_id


@mock_aws
def test_ignores_nat_gateway_with_running_instance_in_same_vpc(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _, subnet_id, _ = _create_vpc_with_nat_gateway(ec2)
    ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1, SubnetId=subnet_id)

    findings = list(IdleNatGateways().run(make_context("nat-gw-idle")))

    assert findings == []


@mock_aws
def test_reports_nat_gateway_when_instances_are_stopped_not_running(make_context):
    """A stopped instance doesn't generate traffic -- the heuristic should
    still fire even though an instance technically exists in the VPC."""
    ec2 = boto3.client("ec2", region_name=REGION)
    _, subnet_id, gateway_id = _create_vpc_with_nat_gateway(ec2)
    instance_id = ec2.run_instances(
        ImageId="ami-12345678", MinCount=1, MaxCount=1, SubnetId=subnet_id
    )["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[instance_id])

    findings = list(IdleNatGateways().run(make_context("nat-gw-idle")))

    assert len(findings) == 1
    assert findings[0].resource_id == gateway_id


@mock_aws
def test_ignores_gateway_not_in_available_state(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _, _, gateway_id = _create_vpc_with_nat_gateway(ec2)
    ec2.delete_nat_gateway(NatGatewayId=gateway_id)

    findings = list(IdleNatGateways().run(make_context("nat-gw-idle")))

    assert findings == []


@mock_aws
def test_running_instance_in_different_vpc_does_not_suppress_finding(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _, _, gateway_id = _create_vpc_with_nat_gateway(ec2)

    other_vpc_id = ec2.create_vpc(CidrBlock="10.1.0.0/16")["Vpc"]["VpcId"]
    other_subnet_id = ec2.create_subnet(
        VpcId=other_vpc_id, CidrBlock="10.1.0.0/24", AvailabilityZone=AZ
    )["Subnet"]["SubnetId"]
    ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1, SubnetId=other_subnet_id)

    findings = list(IdleNatGateways().run(make_context("nat-gw-idle")))

    assert len(findings) == 1
    assert findings[0].resource_id == gateway_id


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_id = ec2.create_subnet(
        VpcId=vpc_id, CidrBlock="10.0.0.0/24", AvailabilityZone="eu-north-1a"
    )["Subnet"]["SubnetId"]
    alloc_id = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.create_nat_gateway(SubnetId=subnet_id, AllocationId=alloc_id)

    ctx = make_context("nat-gw-idle", region=unpriced_region)
    findings = list(IdleNatGateways().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail
