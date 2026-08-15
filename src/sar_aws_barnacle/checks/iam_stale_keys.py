"""Old or never-used IAM access keys.

Global scope: IAM has no regional resources, so this runs exactly once per
account regardless of how many regions were scanned (see Scope.GLOBAL).

Security/hygiene, not cost-waste, same as ebs-unencrypted -- IAM itself has
no cost, so there is no dollar figure to report here. Every finding carries
CostConfidence.UNKNOWN: an honest "this check has no cost claim to make,"
not a pricing gap.

No credential-report shortcut: iam:GenerateCredentialReport would be one
call instead of ListAccessKeys + GetAccessKeyLastUsed per user, but its
operation name doesn't start with an allowed read-only prefix
(Describe/Get/List/BatchGet/Lookup/Head), so the botocore guard would block
it and crash the scan rather than degrade. Expanding that allowlist is an
explicit discussion this project's hard rules call for, not a workaround to
take unilaterally -- so this uses only already-permitted List/Get calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from sar_aws_barnacle.registry import register


@register
class StaleIamAccessKeys:
    id = "iam-stale-keys"
    title = "Old or never-used IAM access keys"
    service = "iam"
    scope = Scope.GLOBAL
    required_actions = frozenset(
        {"iam:ListUsers", "iam:ListAccessKeys", "iam:GetAccessKeyLastUsed"}
    )

    def run(self, ctx: ScanContext) -> Iterator[Finding]:
        min_age_days = int(ctx.option("min_age_days", 90))
        high_severity_days = int(ctx.option("high_severity_days", 365))

        users_paginator = ctx.client().get_paginator("list_users")
        for user_page in users_paginator.paginate():
            for user in user_page.get("Users", []):
                username = user["UserName"]
                keys_paginator = ctx.client().get_paginator("list_access_keys")
                for key_page in keys_paginator.paginate(UserName=username):
                    for key_meta in key_page.get("AccessKeyMetadata", []):
                        key_id = key_meta["AccessKeyId"]
                        last_used = ctx.client().get_access_key_last_used(AccessKeyId=key_id)
                        last_used_date = last_used.get("AccessKeyLastUsed", {}).get("LastUsedDate")
                        finding = self._evaluate(
                            ctx,
                            username,
                            key_meta,
                            last_used_date,
                            min_age_days,
                            high_severity_days,
                        )
                        if finding is not None:
                            yield finding

    def _evaluate(
        self,
        ctx: ScanContext,
        username: str,
        key_meta: dict,
        last_used_date: datetime | None,
        min_age_days: int,
        high_severity_days: int,
    ) -> Finding | None:
        key_id = key_meta["AccessKeyId"]
        status = key_meta.get("Status", "Active")
        created = key_meta.get("CreateDate")

        never_used = last_used_date is None
        age = ctx.age_days(created if never_used else last_used_date)

        if age is None or age < min_age_days:
            return None

        severity = Severity.HIGH if age >= high_severity_days else Severity.MEDIUM

        detail = f"Access key for user {username} "
        detail += (
            f"has never been used, created {age} days ago"
            if never_used
            else f"was last used {age} days ago"
        )
        if status != "Active":
            detail += f" (status: {status})"

        metadata = {
            "user_name": username,
            "status": status,
            "never_used": str(never_used).lower(),
            "age_days": str(age),
        }

        return Finding(
            check_id=self.id,
            resource_id=key_id,
            resource_type="AWS::IAM::AccessKey",
            region=ctx.region,
            severity=severity,
            title="Stale IAM access key",
            detail=detail,
            estimated_monthly_cost=None,
            cost_confidence=CostConfidence.UNKNOWN,
            metadata=metadata,
        )
