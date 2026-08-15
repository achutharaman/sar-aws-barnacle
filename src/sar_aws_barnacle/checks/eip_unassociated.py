"""Unassociated Elastic IPs.

AWS bills an idle hourly rate for an Elastic IP the moment it stops being
associated with a running resource. There is nothing subtle here -- an
unassociated EIP is doing nothing and costing money every hour it sits idle.

No age filter, unlike ebs-unattached: ``DescribeAddresses`` has no allocation
timestamp field at all (confirmed against the botocore service model, not
just moto -- this is a real API limitation, not a gap to work around). There
is no ``CreateTime`` to compute "how long has this been idle" from.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import EIP_IDLE_HOUR, HOURS_PER_MONTH
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")


@register
class UnassociatedElasticIps:
    id = "eip-unassociated"
    title = "Unassociated Elastic IPs"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeAddresses"})

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        # No paginator: DescribeAddresses doesn't support one (an account's
        # EIP count is small by design -- AWS caps it well below a page
        # size), and there's no server-side filter for "has no
        # AssociationId", so every address is fetched and checked here.
        addresses = ctx.client().describe_addresses().get("Addresses", [])

        for address in addresses:
            if address.get("AssociationId"):
                continue
            yield self._evaluate(ctx, address)

    def _evaluate(self, ctx: ScanContext, address: dict) -> Finding:
        high_cost_threshold = Decimal(str(ctx.option("high_severity_usd", 20)))

        allocation_id = address.get("AllocationId", "")
        public_ip = address["PublicIp"]
        domain = address.get("Domain", "vpc")

        unit_price = ctx.price(EIP_IDLE_HOUR)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = (unit_price * HOURS_PER_MONTH).quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = CostConfidence.EXACT

        severity = (
            Severity.HIGH
            if monthly_cost is not None and monthly_cost >= high_cost_threshold
            else Severity.MEDIUM
        )

        detail = f"Elastic IP {public_ip} is not associated with any resource"
        if confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {EIP_IDLE_HOUR} in {ctx.region}"

        tags = {t["Key"]: t["Value"] for t in address.get("Tags", []) if "Key" in t}
        metadata = {
            "allocation_id": allocation_id,
            "public_ip": public_ip,
            "domain": domain,
        }
        if name := tags.get("Name"):
            metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=allocation_id or public_ip,
            resource_type="AWS::EC2::EIP",
            region=ctx.region,
            severity=severity,
            title="Unassociated Elastic IP",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=metadata,
        )
