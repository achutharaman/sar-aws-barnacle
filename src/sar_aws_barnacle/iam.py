"""Generate the least-privilege IAM policy from the registry.

The policy is derived from ``Check.required_actions``, never hand-written. CI
asserts the committed ``docs/iam-policy.json`` still matches what the code
calls, so the policy in the README cannot drift away from reality -- and a new
check that forgets to declare its permissions fails the build.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sar_aws_barnacle.registry import Check, all_checks

POLICY_VERSION = "2012-10-17"
SID = "SarAwsBarnacleReadOnlyScan"

# Needed by the runner itself rather than by any single check: account id for
# the report header, and enabled-region discovery for --all-regions.
BASELINE_ACTIONS = frozenset({"sts:GetCallerIdentity", "ec2:DescribeRegions"})


def collect_actions(checks: Sequence[type[Check]] | None = None) -> list[str]:
    selected = list(checks) if checks is not None else all_checks()
    actions = set(BASELINE_ACTIONS)
    for check in selected:
        actions |= set(check.required_actions)
    return sorted(actions)


def build_policy(checks: Sequence[type[Check]] | None = None) -> dict[str, Any]:
    return {
        "Version": POLICY_VERSION,
        "Statement": [
            {
                "Sid": SID,
                "Effect": "Allow",
                "Action": collect_actions(checks),
                "Resource": "*",
            }
        ],
    }
