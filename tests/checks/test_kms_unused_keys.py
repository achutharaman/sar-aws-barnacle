"""Tests for the customer-managed-KMS-key inventory check.

The interesting assertions: cost is EXACT $1.00/month always, Disabled and
PendingDeletion keys are excluded, severity is fixed LOW, and the detail
text frames this as an inventory finding, not an idleness claim.

Note on the AWS-managed exclusion: moto's ListKeys never returns the
AWS-managed keys behind its seeded alias/aws/* aliases (their target key
IDs don't appear in list_keys() output, so describe_key is never called on
them through the real API), so the real exclusion path can't be exercised
end-to-end via moto. _should_include (the pure predicate) is unit-tested
directly with hand-built metadata, and the run()-level exclusion is proven
by monkeypatching _describe_key to inject a synthetic AWS-managed entry.
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.kms_unused_keys import UnusedKmsKeys
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"


def test_check_metadata_is_well_formed():
    assert UnusedKmsKeys.id == "kms-unused-keys"
    assert UnusedKmsKeys.scope is Scope.REGIONAL
    assert UnusedKmsKeys.required_actions == frozenset(
        {"kms:ListKeys", "kms:DescribeKey", "kms:ListAliases", "kms:ListResourceTags"}
    )


def test_should_include_excludes_aws_managed_keys():
    assert UnusedKmsKeys._should_include({"KeyManager": "AWS", "KeyState": "Enabled"}) is False


def test_should_include_excludes_disabled_keys():
    assert (
        UnusedKmsKeys._should_include({"KeyManager": "CUSTOMER", "KeyState": "Disabled"}) is False
    )


def test_should_include_excludes_pending_deletion_keys():
    assert (
        UnusedKmsKeys._should_include({"KeyManager": "CUSTOMER", "KeyState": "PendingDeletion"})
        is False
    )


def test_should_include_keeps_enabled_customer_keys():
    assert UnusedKmsKeys._should_include({"KeyManager": "CUSTOMER", "KeyState": "Enabled"}) is True


@mock_aws
def test_reports_enabled_customer_managed_key(make_context):
    kms = boto3.client("kms", region_name=REGION)
    key_id = kms.create_key(Description="app key")["KeyMetadata"]["KeyId"]
    kms.create_alias(AliasName="alias/my-app-key", TargetKeyId=key_id)

    findings = list(UnusedKmsKeys().run(make_context("kms-unused-keys", service="kms")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == key_id
    assert finding.resource_type == "AWS::KMS::Key"
    assert finding.region == REGION
    assert finding.estimated_monthly_cost == Decimal("1.00")
    assert finding.cost_confidence is CostConfidence.EXACT
    assert finding.severity is Severity.LOW
    assert "usage can't be determined" in finding.detail
    assert "unused" not in finding.detail.lower()
    assert finding.metadata["aliases"] == "alias/my-app-key"


@mock_aws
def test_ignores_disabled_key(make_context):
    kms = boto3.client("kms", region_name=REGION)
    key_id = kms.create_key(Description="disabled key")["KeyMetadata"]["KeyId"]
    kms.disable_key(KeyId=key_id)

    findings = list(UnusedKmsKeys().run(make_context("kms-unused-keys", service="kms")))

    assert findings == []


@mock_aws
def test_ignores_pending_deletion_key(make_context):
    kms = boto3.client("kms", region_name=REGION)
    key_id = kms.create_key(Description="pending deletion")["KeyMetadata"]["KeyId"]
    kms.schedule_key_deletion(KeyId=key_id)

    findings = list(UnusedKmsKeys().run(make_context("kms-unused-keys", service="kms")))

    assert findings == []


@mock_aws
def test_ignores_aws_managed_key_via_monkeypatched_describe_key(make_context, monkeypatch):
    kms = boto3.client("kms", region_name=REGION)
    kms.create_key(Description="pretend aws-managed")

    monkeypatch.setattr(
        UnusedKmsKeys,
        "_describe_key",
        lambda self, ctx, kid: {"KeyId": kid, "KeyManager": "AWS", "KeyState": "Enabled"},
    )

    findings = list(UnusedKmsKeys().run(make_context("kms-unused-keys", service="kms")))

    assert findings == []


@mock_aws
def test_reports_key_with_no_alias_and_no_name_tag(make_context):
    kms = boto3.client("kms", region_name=REGION)
    key_id = kms.create_key(Description="no alias")["KeyMetadata"]["KeyId"]

    findings = list(UnusedKmsKeys().run(make_context("kms-unused-keys", service="kms")))

    assert len(findings) == 1
    assert findings[0].resource_id == key_id
    assert "aliases" not in findings[0].metadata
    assert "name" not in findings[0].metadata


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    kms = boto3.client("kms", region_name=unpriced_region)
    kms.create_key(Description="unpriced region key")

    ctx = make_context("kms-unused-keys", service="kms", region=unpriced_region)
    findings = list(UnusedKmsKeys().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


@mock_aws
def test_reports_every_surviving_customer_managed_key(make_context):
    kms = boto3.client("kms", region_name=REGION)
    for i in range(3):
        kms.create_key(Description=f"key {i}")

    findings = list(UnusedKmsKeys().run(make_context("kms-unused-keys", service="kms")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
