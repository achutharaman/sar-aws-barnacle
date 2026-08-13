"""The read-only guarantee must be enforced, not merely documented."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from sar_aws_barnacle.session import (
    READ_ONLY_PREFIXES,
    ClientFactory,
    ReadOnlyViolationError,
    install_read_only_guard,
)

REGION = "ap-south-1"


@mock_aws
def test_read_calls_are_allowed():
    client = ClientFactory().client("ec2", region=REGION)
    assert client.describe_volumes()["Volumes"] == []


@mock_aws
def test_mutating_call_is_blocked():
    client = ClientFactory().client("ec2", region=REGION)
    with pytest.raises(ReadOnlyViolationError, match="CreateTags"):
        client.create_tags(Resources=["vol-123"], Tags=[{"Key": "k", "Value": "v"}])


@mock_aws
def test_delete_is_blocked():
    client = ClientFactory().client("ec2", region=REGION)
    with pytest.raises(ReadOnlyViolationError):
        client.delete_volume(VolumeId="vol-123")


@mock_aws
def test_guard_can_be_disabled_for_a_future_fix_mode():
    """--fix will need this seam; the default stays read-only."""
    client = ClientFactory(read_only=False).client("ec2", region=REGION)
    volume = client.create_volume(AvailabilityZone=f"{REGION}a", Size=1)
    assert volume["VolumeId"].startswith("vol-")


@mock_aws
def test_guard_applies_to_every_service_from_the_session():
    factory = ClientFactory()
    iam = factory.client("iam", region="us-east-1")
    with pytest.raises(ReadOnlyViolationError):
        iam.create_user(UserName="someone")


def test_allowed_prefixes_exclude_obvious_mutations():
    for verb in ("Create", "Delete", "Put", "Update", "Modify", "Terminate", "Attach"):
        assert not verb.startswith(READ_ONLY_PREFIXES)


@mock_aws
def test_install_is_idempotent():
    session = boto3.Session(region_name=REGION)
    install_read_only_guard(session)
    install_read_only_guard(session)
    with pytest.raises(ReadOnlyViolationError):
        session.client("ec2").create_tags(Resources=["vol-1"], Tags=[])
