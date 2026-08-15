"""Tests for the ageing-snapshot check.

The interesting assertions are about cost honesty (UPPER_BOUND, never EXACT
-- snapshot billing is incremental and describe_snapshots can't report it)
and that an unpriceable region yields None, not a plausible-looking zero.

Note on timing: like ec2-stopped, moto stamps StartTime from the real wall
clock, and the shared frozen `now` fixture (2026-08-12) sits in the past
relative to real time -- so a freshly created snapshot's age against that
frozen `now` clamps to 0. Tests that need a snapshot to look genuinely old
compute `now` forward from real time instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.ebs_snapshot_age import AgingEbsSnapshots
from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def _create_snapshot(ec2, size: int = 100) -> tuple[str, str]:
    vol = ec2.create_volume(AvailabilityZone=AZ, Size=size, VolumeType="gp3")
    snap = ec2.create_snapshot(VolumeId=vol["VolumeId"])
    return snap["SnapshotId"], vol["VolumeId"]


def test_check_metadata_is_well_formed():
    assert AgingEbsSnapshots.id == "ebs-snapshot-age"
    assert AgingEbsSnapshots.scope is Scope.REGIONAL
    assert AgingEbsSnapshots.required_actions == frozenset(
        {"ec2:DescribeSnapshots", "sts:GetCallerIdentity"}
    )


@mock_aws
def test_reports_aging_snapshot_with_upper_bound_cost(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    snapshot_id, volume_id = _create_snapshot(ec2, size=100)

    ctx = make_context("ebs-snapshot-age", at=datetime.now(UTC) + timedelta(days=100))
    findings = list(AgingEbsSnapshots().run(ctx))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == snapshot_id
    assert finding.resource_type == "AWS::EC2::Snapshot"
    assert finding.region == REGION
    # ap-south-1 seed price is 0.0500/GiB-month * 100 GiB.
    assert finding.estimated_monthly_cost == Decimal("5.00")
    assert finding.cost_confidence is CostConfidence.UPPER_BOUND
    assert "upper bound" in finding.detail
    assert finding.metadata["volume_id"] == volume_id
    assert finding.metadata["age_days"] == "100"


@mock_aws
def test_min_age_days_filters_recent_snapshots(make_context):
    """Default min_age_days=90; a snapshot created "now" (age effectively 0
    against the frozen `now` fixture) must not be reported."""
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_snapshot(ec2)

    findings = list(AgingEbsSnapshots().run(make_context("ebs-snapshot-age")))

    assert findings == []


@mock_aws
def test_min_age_days_is_configurable(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_snapshot(ec2)

    config = Config(options={"ebs-snapshot-age": {"min_age_days": 0}})
    findings = list(AgingEbsSnapshots().run(make_context("ebs-snapshot-age", config=config)))

    assert len(findings) == 1


@mock_aws
def test_very_old_snapshot_is_high_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_snapshot(ec2)

    ctx = make_context("ebs-snapshot-age", at=datetime.now(UTC) + timedelta(days=400))
    findings = list(AgingEbsSnapshots().run(ctx))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_moderately_old_snapshot_is_medium_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_snapshot(ec2)

    ctx = make_context("ebs-snapshot-age", at=datetime.now(UTC) + timedelta(days=100))
    findings = list(AgingEbsSnapshots().run(ctx))

    assert findings[0].severity is Severity.MEDIUM


@mock_aws
def test_high_severity_days_threshold_is_configurable(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _create_snapshot(ec2)

    config = Config(options={"ebs-snapshot-age": {"high_severity_days": 50}})
    ctx = make_context(
        "ebs-snapshot-age", config=config, at=datetime.now(UTC) + timedelta(days=100)
    )
    findings = list(AgingEbsSnapshots().run(ctx))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    _create_snapshot(ec2, size=500)

    ctx = make_context(
        "ebs-snapshot-age", region=unpriced_region, at=datetime.now(UTC) + timedelta(days=100)
    )
    findings = list(AgingEbsSnapshots().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


@mock_aws
def test_reports_every_aging_snapshot(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for _ in range(3):
        _create_snapshot(ec2, size=10)

    ctx = make_context("ebs-snapshot-age", at=datetime.now(UTC) + timedelta(days=100))
    findings = list(AgingEbsSnapshots().run(ctx))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
