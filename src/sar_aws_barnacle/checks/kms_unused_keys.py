"""Customer-managed KMS keys, inventoried by their flat monthly cost.

"Unused" cannot be determined from Describe calls -- KMS key activity is a
CloudTrail/usage-metric question, not a Describe-response field. This is
framed as a key-inventory cost finding, not an idleness claim: every
surviving customer-managed key costs $1/month whether or not it's ever
used, and accounts routinely accumulate hundreds. The value is in the
aggregate count, which is why severity is fixed LOW per-finding rather than
threshold-based.

Two exclusions:

- KeyManager == "AWS" -- AWS-managed keys (aws/s3, aws/rds, ...) are free
  and not deletable, so listing them would be pure noise. This exclusion is
  the whole check; verified against AWS's KMS pricing page, which only
  prices customer managed keys.
- KeyState in {Disabled, PendingDeletion} -- PendingDeletion keys are
  confirmed free (AWS KMS pricing: "no charge for customer managed KMS
  keys that ... are scheduled for deletion"). Disabled keys are NOT free --
  disabling isn't deletion, and AWS's per-key monthly charge draws no
  enabled/disabled distinction (confirmed via AWS re:Post reports of being
  billed for disabled keys). Excluding Disabled here is a deliberate scope
  choice, not a cost-honesty gap: a disabled key is one someone already
  consciously acted on, not hidden waste the way an untouched enabled key
  is.

moto's ListKeys does not return the AWS-managed keys behind its seeded
alias/aws/* aliases at all (their target key IDs never appear in
list_keys(), so describe_key is never called on them through the real
API), which makes the AWS-managed exclusion untestable end-to-end via
moto. _should_include is a pure, directly-testable predicate for exactly
this reason, and the run()-level exclusion is verified by monkeypatching
_describe_key to inject a synthetic AWS-managed entry.

Cost is CostConfidence.EXACT, always, when priced -- $1/month is a flat,
well-documented rate with no usage-dependent component.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.pricing.base import KMS_KEY_MONTH
from sar_aws_barnacle.registry import register

CENTS = Decimal("0.01")
_EXCLUDED_STATES = frozenset({"Disabled", "PendingDeletion"})


@register
class UnusedKmsKeys:
    id = "kms-unused-keys"
    title = "Customer-managed KMS keys"
    service = "kms"
    scope = Scope.REGIONAL
    required_actions = frozenset(
        {"kms:ListKeys", "kms:DescribeKey", "kms:ListAliases", "kms:ListResourceTags"}
    )

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        alias_map = self._alias_map(ctx)

        paginator = ctx.client().get_paginator("list_keys")
        for page in paginator.paginate():
            for key in page.get("Keys", []):
                metadata = self._describe_key(ctx, key["KeyId"])
                if not self._should_include(metadata):
                    continue
                yield self._evaluate(ctx, metadata, alias_map.get(metadata["KeyId"], []))

    def _alias_map(self, ctx: ScanContext) -> dict[str, list[str]]:
        paginator = ctx.client().get_paginator("list_aliases")
        mapping: dict[str, list[str]] = {}
        for page in paginator.paginate():
            for alias in page.get("Aliases", []):
                target_key_id = alias.get("TargetKeyId")
                if target_key_id:
                    mapping.setdefault(target_key_id, []).append(alias["AliasName"])
        return mapping

    def _describe_key(self, ctx: ScanContext, key_id: str) -> dict:
        return ctx.client().describe_key(KeyId=key_id)["KeyMetadata"]

    @staticmethod
    def _should_include(metadata: dict) -> bool:
        if metadata.get("KeyManager") == "AWS":
            return False
        return metadata.get("KeyState") not in _EXCLUDED_STATES

    def _evaluate(self, ctx: ScanContext, metadata: dict, aliases: list[str]) -> Finding:
        key_id = metadata["KeyId"]

        unit_price = ctx.price(KMS_KEY_MONTH)
        if unit_price is None:
            monthly_cost: Decimal | None = None
            confidence = CostConfidence.UNKNOWN
        else:
            monthly_cost = unit_price.quantize(CENTS, rounding=ROUND_HALF_UP)
            confidence = CostConfidence.EXACT

        detail = (
            "Customer-managed KMS key; usage can't be determined from Describe "
            "calls -- this key costs $1.00/month regardless of activity, confirm "
            "it's still needed"
        )
        if confidence is CostConfidence.UNKNOWN:
            detail += f"; no price available for {KMS_KEY_MONTH} in {ctx.region}"

        tags = {
            t["TagKey"]: t["TagValue"]
            for t in ctx.client().list_resource_tags(KeyId=key_id).get("Tags", [])
        }
        result_metadata = {"key_state": metadata.get("KeyState", "")}
        if aliases:
            result_metadata["aliases"] = ", ".join(aliases)
        if name := tags.get("Name"):
            result_metadata["name"] = name

        return Finding(
            check_id=self.id,
            resource_id=key_id,
            resource_type="AWS::KMS::Key",
            region=ctx.region,
            severity=Severity.LOW,
            title="Customer-managed KMS key",
            detail=detail,
            estimated_monthly_cost=monthly_cost,
            cost_confidence=confidence,
            metadata=result_metadata,
        )
