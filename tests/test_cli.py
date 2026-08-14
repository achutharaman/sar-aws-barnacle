"""CLI-level tests.

These exist because the CLI is where flags become behaviour, and a flag that
silently fails to reach the runner is invisible to unit tests. Everything here
runs against moto, so exit codes are exercised end to end.
"""

from __future__ import annotations

import json

import boto3
import pytest
import typer
from moto import mock_aws
from typer.testing import CliRunner

from sar_aws_barnacle import __version__
from sar_aws_barnacle.cli import app
from sar_aws_barnacle.config import ExitCode

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


def test_scan_shows_progress_bar_when_stderr_is_a_terminal(waste, monkeypatch):
    """CliRunner's captured stderr never reports itself as a real terminal,
    so this is the only way to exercise the progress-bar branch in
    _run_scan_with_progress -- call it directly and force is_terminal."""
    from rich.console import Console

    from sar_aws_barnacle.cli import _run_scan_with_progress
    from sar_aws_barnacle.config import Config
    from sar_aws_barnacle.pricing.static import StaticPriceBook
    from sar_aws_barnacle.registry import select_checks
    from sar_aws_barnacle.session import ClientFactory

    monkeypatch.setattr(Console, "is_terminal", property(lambda self: True))

    result = _run_scan_with_progress(
        select_checks(["ebs-unattached"]),
        [REGION],
        factory=ClientFactory(),
        config=Config(),
        prices=StaticPriceBook(),
        account_id=None,
    )

    assert len(result.findings) == 2


def test_resolve_regions_skips_regions_not_enabled_for_the_account(monkeypatch):
    """Scanning a disabled region can't find anything -- it can only burn a
    timeout+retry and clutter the errors table. -r is cross-checked against
    the account's real enabled regions before the scan ever starts."""
    from sar_aws_barnacle.cli import _resolve_regions
    from sar_aws_barnacle.config import Config
    from sar_aws_barnacle.session import ClientFactory

    monkeypatch.setattr(
        ClientFactory, "available_regions", lambda self, service="ec2": ["ap-south-1", "us-east-1"]
    )

    config = Config(regions=("ap-south-1", "me-south-1"))
    to_scan, skipped = _resolve_regions(config, ClientFactory())

    assert to_scan == ["ap-south-1"]
    assert skipped == ["me-south-1"]


def test_resolve_regions_errors_when_nothing_requested_is_enabled(monkeypatch):
    from sar_aws_barnacle.cli import _resolve_regions
    from sar_aws_barnacle.config import Config
    from sar_aws_barnacle.session import ClientFactory

    monkeypatch.setattr(
        ClientFactory, "available_regions", lambda self, service="ec2": ["us-east-1"]
    )

    config = Config(regions=("me-south-1",))
    with pytest.raises(typer.BadParameter):
        _resolve_regions(config, ClientFactory())


def test_scan_reports_skipped_regions_in_json(waste, monkeypatch):
    from sar_aws_barnacle.session import ClientFactory

    monkeypatch.setattr(
        ClientFactory,
        "available_regions",
        lambda self, service="ec2": [REGION],
    )

    result = runner.invoke(
        app, ["scan", "-r", REGION, "-r", "me-south-1", "--no-live-pricing", "-o", "json"]
    )
    payload = json.loads(result.output)

    assert payload["regions"] == [REGION]
    assert payload["skipped_regions"] == ["me-south-1"]
    assert {"region": "me-south-1", "status": "skipped — not enabled for this account"} in (
        payload["region_status"]
    )


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
    (tmp_path / "sar-aws-barnacle.toml").write_text(
        '[sar-aws-barnacle]\nfail_on = "high"\n\n'
        "[sar-aws-barnacle.checks.ebs-unattached]\nhigh_severity_usd = 5\n"
    )
    monkeypatch.chdir(tmp_path)
    result = _scan()
    assert result.exit_code == ExitCode.FINDINGS


def test_cli_flag_overrides_config_file(waste, tmp_path, monkeypatch):
    (tmp_path / "sar-aws-barnacle.toml").write_text('[sar-aws-barnacle]\nfail_on = "high"\n')
    monkeypatch.chdir(tmp_path)
    result = _scan("--fail-on", "none")
    assert result.exit_code == ExitCode.CLEAN
