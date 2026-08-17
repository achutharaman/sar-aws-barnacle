"""NAT Gateways in a VPC with no running EC2 instances.

"No running instances in the VPC" is a heuristic, not proof of idleness --
Lambda functions, ECS/Fargate tasks, and RDS instances in the same VPC can
all generate NAT Gateway traffic with zero EC2 instances present. Describe
calls cannot see any of that, so this check flags a common, cheap-to-check
correlate of waste and says exactly that in the finding text, rather than
claiming the gateway is confirmed unused.

Only Available NAT Gateways are considered -- one that's already
Deleting/Deleted/Failed isn't billing.

Cost is CostConfidence.LOWER_BOUND: the hourly charge is priced, but data
processing charges (per-GB, usage-dependent) aren't visible from
DescribeNatGateways.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import HOURS_PER_MONTH, NAT_GATEWAY_HOUR
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")


@register
class IdleNatGateways:
    id = "nat-gw-idle"
    title = "Idle NAT Gateways"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeNatGateways", "ec2:DescribeInstances"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        vpcs_with_running_instances = self._vpcs_with_running_instances(ctx)

        paginator = ctx.client().get_paginator("describe_nat_gateways")
        pages = paginator.paginate(
            Filter=[{"Name": "state", "Values": ["available"]}],
        )

        for page in pages:
            for gateway in page.get("NatGateways", []):
                if gateway.get("VpcId") in vpcs_with_running_instances:
                    continue
                yield self._evaluate(ctx, gateway)

    def _vpcs_with_running_instances(self, ctx: ScanContext) -> set[str]:
        paginator = ctx.client().get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}],
        )
        return {
            instance["VpcId"]
            for page in pages
            for reservation in page.get("Reservations", [])
            for instance in reservation.get("Instances", [])
            if "VpcId" in instance
        }

    def _evaluate(self, ctx: ScanContext, gateway: dict) -> Finding:
        gateway_id = gateway["NatGatewayId"]
        vpc_id = gateway.get("VpcId", "")

        unit_price = ctx.price(NAT_GATEWAY_HOUR)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * HOURS_PER_MONTH).quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = CostConfidence.LOWER_BOUND

        detail = (
            f"NAT Gateway has no running EC2 instances in VPC {vpc_id}; this is a "
            f"heuristic, not proof of idleness -- Lambda functions, ECS/Fargate "
            f"tasks, and RDS instances in the same VPC can generate NAT Gateway "
            f"traffic with zero EC2 instances present. Confirm actual usage (e.g. "
            f"via the CloudWatch BytesOutToDestination metric) before acting"
        )
        if confidence is CostConfidence.LOWER_BOUND:
            detail += "; cost is a lower bound -- data processing charges are not included"
        elif confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {NAT_GATEWAY_HOUR} in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in gateway.get("Tags", []) if "Key" in t}
        metadata = {
            "vpc_id": vpc_id,
            "subnet_id": gateway.get("SubnetId", ""),
            "state": gateway.get("State", ""),
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=gateway_id,
            resource_type="AWS::EC2::NatGateway",
            region=ctx.region,
            severity=Severity.HIGH,
            title="Idle NAT Gateway",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
