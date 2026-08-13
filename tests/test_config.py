"""Config precedence and exit-code policy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from aws_barnacle.config import (
    Config,
    ExitCode,
    FailOn,
    OutputFormat,
    load_config,
    resolve_exit_code,
)

CONFIG_TOML = """
[sar-aws-barnacle]
output = "json"
fail_on = "high"
regions = ["ap-south-1", "us-east-1"]

[sar-aws-barnacle.checks.ebs-unattached]
high_severity_usd = 50
min_age_days = 7
"""


def test_loads_file(tmp_path: Path):
    path = tmp_path / "sar-aws-barnacle.toml"
    path.write_text(CONFIG_TOML)

    config = load_config(path)

    assert config.output is OutputFormat.JSON
    assert config.fail_on is FailOn.HIGH
    assert config.regions == ("ap-south-1", "us-east-1")


def test_per_check_options_are_typed(tmp_path: Path):
    path = tmp_path / "sar-aws-barnacle.toml"
    path.write_text(CONFIG_TOML)
    config = load_config(path)

    assert config.option("ebs-unattached", "high_severity_usd", 20) == 50
    assert config.option("ebs-unattached", "missing_key", 99) == 99
    assert config.option("other-check", "high_severity_usd", 20) == 20


def test_env_overrides_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "sar-aws-barnacle.toml"
    path.write_text(CONFIG_TOML)
    monkeypatch.setenv("SAR_AWS_BARNACLE_OUTPUT", "table")
    monkeypatch.setenv("SAR_AWS_BARNACLE_REGIONS", "eu-west-1")

    config = load_config(path)

    assert config.output is OutputFormat.TABLE
    assert config.regions == ("eu-west-1",)


def test_cli_overrides_win():
    config = Config(output=OutputFormat.TABLE).merged_with(output=OutputFormat.JSON, profile=None)
    assert config.output is OutputFormat.JSON
    assert config.profile is None


def test_missing_file_yields_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(None)
    assert config.output is OutputFormat.TABLE
    assert config.min_monthly_savings == Decimal(0)


def test_errors_beat_findings_in_exit_code():
    """A partial scan is an unanswered question, not a clean bill of health."""
    code = resolve_exit_code(
        finding_count=0, worst_severity_rank=0, has_errors=True, fail_on=FailOn.NONE
    )
    assert code == ExitCode.ERRORS


def test_fail_on_none_is_always_clean():
    code = resolve_exit_code(
        finding_count=12, worst_severity_rank=3, has_errors=False, fail_on=FailOn.NONE
    )
    assert code == ExitCode.CLEAN


def test_fail_on_any_trips_on_one_finding():
    code = resolve_exit_code(
        finding_count=1, worst_severity_rank=1, has_errors=False, fail_on=FailOn.ANY
    )
    assert code == ExitCode.FINDINGS


def test_fail_on_high_ignores_medium():
    assert (
        resolve_exit_code(
            finding_count=3, worst_severity_rank=2, has_errors=False, fail_on=FailOn.HIGH
        )
        == ExitCode.CLEAN
    )
    assert (
        resolve_exit_code(
            finding_count=3, worst_severity_rank=3, has_errors=False, fail_on=FailOn.HIGH
        )
        == ExitCode.FINDINGS
    )
