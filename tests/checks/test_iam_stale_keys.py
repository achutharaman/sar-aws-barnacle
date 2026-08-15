"""Tests for the stale-IAM-access-key check.

The interesting assertions: this is a security/hygiene check with no cost
claim at all (CostConfidence.UNKNOWN on every finding, not a pricing gap),
and it correctly distinguishes "never used" (age from CreateDate) from
"used a while ago" (age from LastUsedDate).

Note on _evaluate's signature: moto does not track real API call history,
so it can never populate AccessKeyLastUsed's LastUsedDate -- there is no way
to make a key look "used" through the real API flow. _evaluate takes
last_used_date as a parameter (fetched by run(), not looked up internally)
specifically so the "used N days ago" branch can be tested with a
hand-built value instead of being permanently unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.iam_stale_keys import StaleIamAccessKeys
from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "us-east-1"


def _create_key(iam, username: str = "alice") -> tuple[str, dict]:
    iam.create_user(UserName=username)
    key = iam.create_access_key(UserName=username)["AccessKey"]
    return key["AccessKeyId"], {
        "AccessKeyId": key["AccessKeyId"],
        "Status": key["Status"],
        "CreateDate": key["CreateDate"],
        "UserName": username,
    }


def test_check_metadata_is_well_formed():
    assert StaleIamAccessKeys.id == "iam-stale-keys"
    assert StaleIamAccessKeys.scope is Scope.GLOBAL
    assert StaleIamAccessKeys.required_actions == frozenset(
        {"iam:ListUsers", "iam:ListAccessKeys", "iam:GetAccessKeyLastUsed"}
    )


@mock_aws
def test_reports_never_used_key_with_unknown_cost(make_context):
    """Security/hygiene, not cost-waste -- IAM has no price, so this must
    never invent a dollar figure."""
    iam = boto3.client("iam", region_name=REGION)
    key_id, _ = _create_key(iam)

    ctx = make_context("iam-stale-keys", service="iam", at=datetime.now(UTC) + timedelta(days=100))
    findings = list(StaleIamAccessKeys().run(ctx))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == key_id
    assert finding.resource_type == "AWS::IAM::AccessKey"
    assert finding.estimated_monthly_cost is None
    assert finding.cost_confidence is CostConfidence.UNKNOWN
    assert finding.metadata["never_used"] == "true"
    assert finding.metadata["user_name"] == "alice"
    assert "never been used" in finding.detail


@mock_aws
def test_min_age_days_filters_recent_keys(make_context):
    iam = boto3.client("iam", region_name=REGION)
    _create_key(iam)

    findings = list(StaleIamAccessKeys().run(make_context("iam-stale-keys", service="iam")))

    assert findings == []


@mock_aws
def test_min_age_days_is_configurable(make_context):
    iam = boto3.client("iam", region_name=REGION)
    _create_key(iam)

    config = Config(options={"iam-stale-keys": {"min_age_days": 0}})
    findings = list(
        StaleIamAccessKeys().run(make_context("iam-stale-keys", service="iam", config=config))
    )

    assert len(findings) == 1


@mock_aws
def test_very_old_key_is_high_severity(make_context):
    iam = boto3.client("iam", region_name=REGION)
    _create_key(iam)

    ctx = make_context("iam-stale-keys", service="iam", at=datetime.now(UTC) + timedelta(days=400))
    findings = list(StaleIamAccessKeys().run(ctx))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_moderately_old_key_is_medium_severity(make_context):
    iam = boto3.client("iam", region_name=REGION)
    _create_key(iam)

    ctx = make_context("iam-stale-keys", service="iam", at=datetime.now(UTC) + timedelta(days=100))
    findings = list(StaleIamAccessKeys().run(ctx))

    assert findings[0].severity is Severity.MEDIUM


@mock_aws
def test_used_key_is_evaluated_from_last_used_not_created(make_context):
    """A key created long ago but used recently must not be flagged --
    moto can't simulate real usage tracking, so this exercises _evaluate
    directly with a hand-built LastUsedDate."""
    iam = boto3.client("iam", region_name=REGION)
    _, key_meta = _create_key(iam)
    key_meta["CreateDate"] = datetime.now(UTC) - timedelta(days=400)

    ctx = make_context("iam-stale-keys", service="iam")
    recently_used = ctx.now - timedelta(days=1)

    finding = StaleIamAccessKeys()._evaluate(
        ctx, "alice", key_meta, recently_used, min_age_days=90, high_severity_days=365
    )

    assert finding is None


@mock_aws
def test_used_key_reports_age_since_last_use(make_context):
    iam = boto3.client("iam", region_name=REGION)
    _, key_meta = _create_key(iam)

    ctx = make_context("iam-stale-keys", service="iam")
    last_used = ctx.now - timedelta(days=200)

    finding = StaleIamAccessKeys()._evaluate(
        ctx, "alice", key_meta, last_used, min_age_days=90, high_severity_days=365
    )

    assert finding is not None
    assert finding.metadata["never_used"] == "false"
    assert finding.metadata["age_days"] == "200"
    assert "was last used 200 days ago" in finding.detail


@mock_aws
def test_inactive_key_status_is_noted_in_detail(make_context):
    iam = boto3.client("iam", region_name=REGION)
    key_id, key_meta = _create_key(iam)
    iam.update_access_key(UserName="alice", AccessKeyId=key_id, Status="Inactive")
    key_meta["Status"] = "Inactive"

    ctx = make_context("iam-stale-keys", service="iam", at=datetime.now(UTC) + timedelta(days=100))
    finding = StaleIamAccessKeys()._evaluate(
        ctx, "alice", key_meta, None, min_age_days=90, high_severity_days=365
    )

    assert finding is not None
    assert "Inactive" in finding.detail
    assert finding.metadata["status"] == "Inactive"


@mock_aws
def test_reports_every_stale_key_across_users(make_context):
    iam = boto3.client("iam", region_name=REGION)
    for username in ("alice", "bob", "carol"):
        _create_key(iam, username)

    ctx = make_context("iam-stale-keys", service="iam", at=datetime.now(UTC) + timedelta(days=100))
    findings = list(StaleIamAccessKeys().run(ctx))

    assert len(findings) == 3
    assert {f.metadata["user_name"] for f in findings} == {"alice", "bob", "carol"}
