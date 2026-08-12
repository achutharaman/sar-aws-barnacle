"""CLI-level tests.

These exist because the CLI is where flags become behaviour, and a flag that
silently fails to reach the runner is invisible to unit tests. Everything here
runs against moto, so exit codes are exercised end to end.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from aws_barnacle import __version__
from aws_barnacle.cli import app
from aws_barnacle.config import ExitCode

REGION = "ap-south-1"
runner = CliRunner()


@pytest.fixture
def waste():
    """An account with one expensive and one cheap unattached volume."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=1000, VolumeType="gp3")
        ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=100, VolumeType="gp3")
        yield ec2


def _scan(*args):
    return runner.invoke(app, ["scan", "-r", REGION, "--no-live-pricing", *args])


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == ExitCode.CLEAN
    assert __version__ in result.output


def test_checks_lists_registered_checks():
    result = runner.invoke(app, ["checks"])
    assert result.exit_code == ExitCode.CLEAN
    assert "ebs-unattached" in result.output


def test_checks_json_output():
    result = runner.invoke(app, ["checks", "--output", "json"])
    payload = json.loads(result.output)
    ids = {c["id"] for c in payload}
    assert "ebs-unattached" in ids
    assert all("required_actions" in c for c in payload)


def test_iam_policy_is_valid_json():
    result = runner.invoke(app, ["iam-policy"])
    policy = json.loads(result.output)
    assert policy["Version"] == "2012-10-17"
    assert "ec2:DescribeVolumes" in policy["Statement"][0]["Action"]


def test_iam_policy_can_be_scoped_to_one_check():
    result = runner.invoke(app, ["iam-policy", "--check", "ebs-unattached"])
    assert json.loads(result.output)["Statement"][0]["Effect"] == "Allow"


@mock_aws
def test_scan_of_clean_account_exits_zero():
    result = _scan()
    assert result.exit_code == ExitCode.CLEAN
    assert "No findings" in result.output


def test_scan_reports_findings(waste):
    result = _scan()
    assert result.exit_code == ExitCode.CLEAN  # --fail-on defaults to none
    assert "$91.20" in result.output
    assert "Estimated monthly waste" in result.output


def test_scan_json_output_is_parseable(waste):
    result = _scan("--output", "json")
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1.0"
    assert payload["summary"]["finding_count"] == 2
    assert payload["summary"]["estimated_monthly_savings"] == "100.32"


def test_fail_on_high_exits_one(waste):
    """The 1000 GiB volume is HIGH, so CI should trip."""
    result = _scan("--fail-on", "high")
    assert result.exit_code == ExitCode.FINDINGS


def test_fail_on_none_never_trips(waste):
    assert _scan("--fail-on", "none").exit_code == ExitCode.CLEAN


def test_min_savings_filters_output(waste):
    result = _scan("--min-savings", "50", "--output", "json")
    payload = json.loads(result.output)
    assert payload["summary"]["finding_count"] == 1
    assert payload["findings"][0]["estimated_monthly_cost"] == "91.20"


def test_check_selector_limits_the_scan(waste):
    result = _scan("--check", "ebs-unattached", "--output", "json")
    assert json.loads(result.output)["checks_run"] == ["ebs-unattached"]


def test_unknown_check_is_a_clear_error(waste):
    result = _scan("--check", "no-such-check")
    assert result.exit_code != ExitCode.CLEAN
    assert "no-such-check" in str(result.exception) or "no-such-check" in result.output


def test_no_live_pricing_uses_the_seed_table(waste):
    result = _scan("--output", "json")
    assert "seed table" in json.loads(result.output)["summary"]["price_source"]


def test_multiple_regions_are_scanned(waste):
    result = runner.invoke(
        app, ["scan", "-r", REGION, "-r", "us-east-1", "--no-live-pricing", "-o", "json"]
    )
    payload = json.loads(result.output)
    assert payload["regions"] == [REGION, "us-east-1"]


def test_all_regions_discovers_from_the_account(waste):
    result = runner.invoke(app, ["scan", "--all-regions", "--no-live-pricing", "-o", "json"])
    payload = json.loads(result.output)
    assert len(payload["regions"]) > 5
    assert REGION in payload["regions"]


def test_config_file_is_honoured(waste, tmp_path, monkeypatch):
    (tmp_path / "barnacle.toml").write_text(
        '[barnacle]\nfail_on = "high"\n\n[barnacle.checks.ebs-unattached]\nhigh_severity_usd = 5\n'
    )
    monkeypatch.chdir(tmp_path)
    result = _scan()
    assert result.exit_code == ExitCode.FINDINGS


def test_cli_flag_overrides_config_file(waste, tmp_path, monkeypatch):
    (tmp_path / "barnacle.toml").write_text('[barnacle]\nfail_on = "high"\n')
    monkeypatch.chdir(tmp_path)
    result = _scan("--fail-on", "none")
    assert result.exit_code == ExitCode.CLEAN
