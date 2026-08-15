"""Tests for the ageing-RDS-snapshot check.

Same interesting assertions as ebs-snapshot-age: cost is UPPER_BOUND, never
EXACT, and an unpriceable region yields None, not a plausible-looking zero.
Also: automated snapshots (which moto creates automatically alongside any
DB instance, simulating RDS's real backup behavior) must never appear --
only manual ones.

Same timing note as ebs-snapshot-age/ec2-stopped: moto stamps
SnapshotCreateTime from the real wall clock, which is after the shared
frozen `now` fixture, so tests that need a snapshot to look genuinely old
compute `now` forward from real time instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.rds_snapshot_age import AgingRdsSnapshots
from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"


def _create_db_snapshot(rds, db_id: str = "mydb", size: int = 100) -> str:
    rds.create_db_instance(
        DBInstanceIdentifier=db_id,
        DBInstanceClass="db.t3.micro",
        Engine="postgres",
        AllocatedStorage=size,
        MasterUsername="admin",
        MasterUserPassword="password123",
    )
    snapshot_id = f"{db_id}-snap"
    rds.create_db_snapshot(DBSnapshotIdentifier=snapshot_id, DBInstanceIdentifier=db_id)
    return snapshot_id


def test_check_metadata_is_well_formed():
    assert AgingRdsSnapshots.id == "rds-snapshot-age"
    assert AgingRdsSnapshots.scope is Scope.REGIONAL
    assert AgingRdsSnapshots.required_actions == frozenset({"rds:DescribeDBSnapshots"})


@mock_aws
def test_reports_aging_snapshot_with_upper_bound_cost(make_context):
    rds = boto3.client("rds", region_name=REGION)
    snapshot_id = _create_db_snapshot(rds, size=100)

    ctx = make_context(
        "rds-snapshot-age", service="rds", at=datetime.now(UTC) + timedelta(days=100)
    )
    findings = list(AgingRdsSnapshots().run(ctx))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == snapshot_id
    assert finding.resource_type == "AWS::RDS::DBSnapshot"
    assert finding.region == REGION
    # ap-south-1 seed price is 0.0950/GiB-month * 100 GiB.
    assert finding.estimated_monthly_cost == Decimal("9.50")
    assert finding.cost_confidence is CostConfidence.UPPER_BOUND
    assert "upper bound" in finding.detail
    assert finding.metadata["db_instance_id"] == "mydb"
    assert finding.metadata["engine"] == "postgres"


@mock_aws
def test_automated_snapshots_are_never_reported(make_context):
    """moto creates an automated snapshot alongside every DB instance --
    only the manual one should ever show up."""
    rds = boto3.client("rds", region_name=REGION)
    _create_db_snapshot(rds)

    ctx = make_context(
        "rds-snapshot-age", service="rds", at=datetime.now(UTC) + timedelta(days=100)
    )
    findings = list(AgingRdsSnapshots().run(ctx))

    assert len(findings) == 1
    assert findings[0].resource_id == "mydb-snap"


@mock_aws
def test_min_age_days_filters_recent_snapshots(make_context):
    rds = boto3.client("rds", region_name=REGION)
    _create_db_snapshot(rds)

    findings = list(AgingRdsSnapshots().run(make_context("rds-snapshot-age", service="rds")))

    assert findings == []


@mock_aws
def test_min_age_days_is_configurable(make_context):
    rds = boto3.client("rds", region_name=REGION)
    _create_db_snapshot(rds)

    config = Config(options={"rds-snapshot-age": {"min_age_days": 0}})
    findings = list(
        AgingRdsSnapshots().run(make_context("rds-snapshot-age", service="rds", config=config))
    )

    assert len(findings) == 1


@mock_aws
def test_very_old_snapshot_is_high_severity(make_context):
    rds = boto3.client("rds", region_name=REGION)
    _create_db_snapshot(rds)

    ctx = make_context(
        "rds-snapshot-age", service="rds", at=datetime.now(UTC) + timedelta(days=400)
    )
    findings = list(AgingRdsSnapshots().run(ctx))

    assert findings[0].severity is Severity.HIGH


@mock_aws
def test_moderately_old_snapshot_is_medium_severity(make_context):
    rds = boto3.client("rds", region_name=REGION)
    _create_db_snapshot(rds)

    ctx = make_context(
        "rds-snapshot-age", service="rds", at=datetime.now(UTC) + timedelta(days=100)
    )
    findings = list(AgingRdsSnapshots().run(ctx))

    assert findings[0].severity is Severity.MEDIUM


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    rds = boto3.client("rds", region_name=unpriced_region)
    _create_db_snapshot(rds, size=500)

    ctx = make_context(
        "rds-snapshot-age",
        service="rds",
        region=unpriced_region,
        at=datetime.now(UTC) + timedelta(days=100),
    )
    findings = list(AgingRdsSnapshots().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


@mock_aws
def test_reports_every_aging_snapshot(make_context):
    rds = boto3.client("rds", region_name=REGION)
    for i in range(3):
        _create_db_snapshot(rds, db_id=f"mydb{i}", size=10)

    ctx = make_context(
        "rds-snapshot-age", service="rds", at=datetime.now(UTC) + timedelta(days=100)
    )
    findings = list(AgingRdsSnapshots().run(ctx))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
