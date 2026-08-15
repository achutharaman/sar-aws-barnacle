"""Tests for the VPC-Interface-endpoint check.

The interesting assertions: Gateway endpoints (free) are never reported,
cost scales with the number of AZs an endpoint spans (not a flat per-endpoint
rate), and the detail text is honest about not being able to detect actual
idleness -- this is a cost-awareness finding, not a confirmed-unused claim.
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.vpc_endpoints_unused import UnusedVpcEndpoints
from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"


def _create_vpc_with_subnets(ec2, azs=("ap-south-1a",)):
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_ids = [
        ec2.create_subnet(VpcId=vpc_id, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=az)["Subnet"][
            "SubnetId"
        ]
        for i, az in enumerate(azs)
    ]
    return vpc_id, subnet_ids


def _create_interface_endpoint(ec2, vpc_id, subnet_ids, service="ec2"):
    resp = ec2.create_vpc_endpoint(
        VpcId=vpc_id,
        ServiceName=f"com.amazonaws.{REGION}.{service}",
        VpcEndpointType="Interface",
        SubnetIds=subnet_ids,
    )
    return resp["VpcEndpoint"]["VpcEndpointId"]


def test_check_metadata_is_well_formed():
    assert UnusedVpcEndpoints.id == "vpc-endpoints-unused"
    assert UnusedVpcEndpoints.scope is Scope.REGIONAL
    assert UnusedVpcEndpoints.required_actions == frozenset({"ec2:DescribeVpcEndpoints"})


@mock_aws
def test_reports_interface_endpoint_with_lower_bound_cost(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnets = _create_vpc_with_subnets(ec2)
    endpoint_id = _create_interface_endpoint(ec2, vpc_id, subnets)

    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == endpoint_id
    assert finding.resource_type == "AWS::EC2::VPCEndpoint"
    assert finding.region == REGION
    # ap-south-1 seed price is 0.01/hr * 730 hours * 1 AZ.
    assert finding.estimated_monthly_cost == Decimal("7.30")
    assert finding.cost_confidence is CostConfidence.LOWER_BOUND
    assert "lower bound" in finding.detail
    assert "can't be determined" in finding.detail
    assert finding.metadata["az_count"] == "1"


@mock_aws
def test_ignores_gateway_endpoints(make_context):
    """Gateway endpoints are free -- reporting one would claim imaginary savings."""
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, _ = _create_vpc_with_subnets(ec2)
    ec2.create_vpc_endpoint(
        VpcId=vpc_id,
        ServiceName=f"com.amazonaws.{REGION}.s3",
        VpcEndpointType="Gateway",
        RouteTableIds=[],
    )

    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused")))

    assert findings == []


@mock_aws
def test_cost_scales_with_az_count(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnets = _create_vpc_with_subnets(
        ec2, azs=("ap-south-1a", "ap-south-1b", "ap-south-1c")
    )
    _create_interface_endpoint(ec2, vpc_id, subnets)

    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused")))

    assert findings[0].metadata["az_count"] == "3"
    # 0.01 * 730 * 3 AZs.
    assert findings[0].estimated_monthly_cost == Decimal("21.90")


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    vpc_id, subnets = _create_vpc_with_subnets(ec2, azs=("eu-north-1a",))
    _create_interface_endpoint(ec2, vpc_id, subnets)

    ctx = make_context("vpc-endpoints-unused", region=unpriced_region)
    findings = list(UnusedVpcEndpoints().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


@mock_aws
def test_expensive_endpoint_is_high_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnets = _create_vpc_with_subnets(
        ec2, azs=("ap-south-1a", "ap-south-1b", "ap-south-1c")
    )
    _create_interface_endpoint(ec2, vpc_id, subnets)  # 3 AZs = $21.90 > default $20

    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused")))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_default_single_az_endpoint_is_medium_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnets = _create_vpc_with_subnets(ec2)
    _create_interface_endpoint(ec2, vpc_id, subnets)  # 1 AZ = $7.30 < default $20

    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused")))

    assert findings[0].severity is Severity.MEDIUM


@mock_aws
def test_high_severity_threshold_is_configurable(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnets = _create_vpc_with_subnets(ec2)
    _create_interface_endpoint(ec2, vpc_id, subnets)  # $7.30

    config = Config(options={"vpc-endpoints-unused": {"high_severity_usd": 5}})
    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused", config=config)))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_reports_every_interface_endpoint(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc_id, subnets = _create_vpc_with_subnets(ec2)
    for service in ("ec2", "sns", "sqs"):
        _create_interface_endpoint(ec2, vpc_id, subnets, service=service)

    findings = list(UnusedVpcEndpoints().run(make_context("vpc-endpoints-unused")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
