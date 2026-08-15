"""Tests for the long-stopped-EC2 check.

The interesting assertions: cost is the *attached storage*, not a phantom
compute charge (a stopped instance doesn't bill for compute at all); a
running instance is never reported; and severity follows the 1-day
threshold even though the underlying "how long stopped" signal is
best-effort (StateTransitionReason has no documented, stable format).

Note on timing: moto sets StateTransitionReason from the real wall clock
when stop_instances() is called -- there is no way to back-date it the way
create_volume()'s CreateTime can be reasoned about against a frozen `now`.
To exercise the >1-day-stopped branch, `now` is computed forward from real
time instead of using the shared frozen fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
from moto import mock_aws

from sar_aws_barnacle.checks.ec2_stopped import StoppedEc2Instances
from sar_aws_barnacle.models import CostConfidence, Scope, Severity

REGION = "ap-south-1"
AZ = "ap-south-1a"


def _stopped_instance(ec2) -> str:
    r = ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)
    instance_id = r["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[instance_id])
    return instance_id


def test_check_metadata_is_well_formed():
    assert StoppedEc2Instances.id == "ec2-stopped"
    assert StoppedEc2Instances.scope is Scope.REGIONAL
    assert StoppedEc2Instances.required_actions == frozenset(
        {"ec2:DescribeInstances", "ec2:DescribeVolumes"}
    )


@mock_aws
def test_reports_stopped_instance_with_attached_volume_cost(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    instance_id = _stopped_instance(ec2)

    findings = list(StoppedEc2Instances().run(make_context("ec2-stopped")))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == instance_id
    assert finding.resource_type == "AWS::EC2::Instance"
    assert finding.region == REGION
    assert finding.cost_confidence is CostConfidence.EXACT
    assert finding.estimated_monthly_cost is not None
    assert finding.estimated_monthly_cost > 0
    assert finding.metadata["attached_volume_count"] == "1"


@mock_aws
def test_ignores_running_instances(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)

    findings = list(StoppedEc2Instances().run(make_context("ec2-stopped")))

    assert findings == []


@mock_aws
def test_recently_stopped_is_low_severity(make_context):
    """Stopped moments ago (moto's real-time StateTransitionReason, checked
    against a `now` that's also effectively "now") -- within 24h, per the
    explicit LOW/HIGH split."""
    ec2 = boto3.client("ec2", region_name=REGION)
    _stopped_instance(ec2)

    ctx = make_context("ec2-stopped", at=datetime.now(UTC))
    findings = list(StoppedEc2Instances().run(ctx))

    assert findings[0].severity is Severity.LOW
    assert findings[0].metadata["stopped_days"] == "0"


@mock_aws
def test_stopped_over_a_day_is_high_severity(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    _stopped_instance(ec2)

    # moto stamps StateTransitionReason from the real wall clock; evaluating
    # against a `now` two days ahead of it reliably crosses the threshold
    # without needing to fake AWS's own timestamp.
    ctx = make_context("ec2-stopped", at=datetime.now(UTC) + timedelta(days=2))
    findings = list(StoppedEc2Instances().run(ctx))

    assert findings[0].severity is Severity.HIGH
    assert findings[0].metadata["stopped_days"] == "2"


@mock_aws
def test_unpriceable_region_yields_none_not_zero(make_context):
    """A missing price must never be rendered as $0.00 of waste."""
    unpriced_region = "eu-north-1"
    ec2 = boto3.client("ec2", region_name=unpriced_region)
    _stopped_instance(ec2)

    ctx = make_context("ec2-stopped", region=unpriced_region)
    findings = list(StoppedEc2Instances().run(ctx))

    assert findings[0].estimated_monthly_cost is None
    assert findings[0].cost_confidence is CostConfidence.UNKNOWN
    assert "no price available" in findings[0].detail


def test_stopped_since_returns_none_for_an_unparseable_reason():
    """moto always produces a parseable "User initiated (...)" reason, so
    this can't be reached via run() -- a Spot interruption or a failed
    health check doesn't always embed a date at all, and that must degrade
    to "unknown", not raise or silently misparse."""
    assert StoppedEc2Instances._stopped_since({"StateTransitionReason": ""}) is None
    assert (
        StoppedEc2Instances._stopped_since(
            {"StateTransitionReason": "Server.SpotInstanceTermination"}
        )
        is None
    )


@mock_aws
def test_unknown_stop_time_is_medium_severity_not_a_guess(make_context):
    """Same unreachable-via-moto scenario as above, exercised through
    _evaluate directly: an instance whose stop time can't be determined
    must not be silently downgraded to LOW or upgraded to HIGH."""
    ec2 = boto3.client("ec2", region_name=REGION)
    instance_id = _stopped_instance(ec2)
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    instance["StateTransitionReason"] = "Server.SpotInstanceTermination"

    finding = StoppedEc2Instances()._evaluate(make_context("ec2-stopped"), instance, {})

    assert finding.severity is Severity.MEDIUM
    assert "stopped_days" not in finding.metadata
    assert "stop time unknown" in finding.detail


@mock_aws
def test_reports_every_stopped_instance(make_context):
    ec2 = boto3.client("ec2", region_name=REGION)
    for _ in range(3):
        _stopped_instance(ec2)

    findings = list(StoppedEc2Instances().run(make_context("ec2-stopped")))

    assert len(findings) == 3
    assert len({f.resource_id for f in findings}) == 3
