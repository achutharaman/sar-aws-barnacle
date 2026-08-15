"""Interface VPC endpoints: a cost-awareness signal, not a confirmed-idle one.

Interface endpoints bill an hourly charge per Availability Zone they span
(~$7.30/month per AZ) whether or not anything uses them. Gateway endpoints
(S3, DynamoDB) are free and excluded entirely -- flagging them would report
imaginary savings.

There is no usage signal here, and this check does not pretend otherwise.
DescribeVpcEndpoints has no traffic, connection-count, or last-activity
field (confirmed against the botocore service model) -- PrivateLink usage
is exclusively a CloudWatch metric (AWS/PrivateLinkEndpoints), which is
exactly the kind of dependency the CloudWatch-blocked checks in
docs/CHECK-BACKLOG.md are waiting on. Every Interface endpoint is reported,
worded as "here's what this costs, confirm it's still needed" rather than
"this is unused" -- an honest cost-awareness finding, not a fabricated
idleness detection.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import HOURS_PER_MONTH, VPC_ENDPOINT_HOUR
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")


@register
class UnusedVpcEndpoints:
    id = "vpc-endpoints-unused"
    title = "Interface VPC endpoints"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeVpcEndpoints"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        high_cost_threshold = Decimal(str(ctx.option("high_severity_usd", 20)))

        paginator = ctx.client().get_paginator("describe_vpc_endpoints")
        pages = paginator.paginate(
            Filters=[{"Name": "vpc-endpoint-type", "Values": ["Interface"]}],
        )

        for page in pages:
            for endpoint in page.get("VpcEndpoints", []):
                yield self._evaluate(ctx, endpoint, high_cost_threshold)

    def _evaluate(self, ctx: ScanContext, endpoint: dict, high_cost_threshold: Decimal) -> Finding:
        endpoint_id = endpoint["VpcEndpointId"]
        service_name = endpoint.get("ServiceName", "")
        az_count = max(len(endpoint.get("SubnetIds", [])), 1)

        unit_price = ctx.price(VPC_ENDPOINT_HOUR)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * HOURS_PER_MONTH * az_count).quantize(
                CENTS, rounding=ROUND_HALF_UP
            )
            confidence = CostConfidence.LOWER_BOUND

        severity = (
            Severity.HIGH
            if monthly_cost is not None and monthly_cost >= high_cost_threshold
            else Severity.MEDIUM
        )

        detail = (
            f"Interface endpoint for {service_name} spans {az_count} "
            f"availability zone(s); usage can't be determined from Describe "
            f"calls alone, so confirm it's still needed"
        )
        if confidence is CostConfidence.LOWER_BOUND:
            detail += "; cost is a lower bound -- data processing charges are not included"
        elif confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {VPC_ENDPOINT_HOUR} in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in endpoint.get("Tags", []) if "Key" in t}
        metadata = {
            "service_name": service_name,
            "vpc_id": endpoint.get("VpcId", ""),
            "az_count": str(az_count),
            "state": endpoint.get("State", ""),
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=endpoint_id,
            resource_type="AWS::EC2::VPCEndpoint",
            region=ctx.region,
            severity=severity,
            title="Interface VPC endpoint",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
