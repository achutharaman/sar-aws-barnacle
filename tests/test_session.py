"""ClientFactory's default-profile fallback, and its client timeouts.

The interesting property of the profile fallback isn't "it picks the scanner
profile" -- it's that it never invents a profile that doesn't exist. boto3
raises ProfileNotFound immediately for an unconfigured name, so forcing
DEFAULT_SCANNER_PROFILE unconditionally would break anyone who didn't create
it (default credentials, an instance role, CI OIDC).

The timeout and retry tests exist because botocore's own 60s/60s timeout
defaults, even at a handful of retries, let one unreachable region take
minutes to fail -- indistinguishable from a hang on a progress bar with no
way to show it's still retrying.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from moto import mock_aws

from sar_aws_barnacle.session import DEFAULT_SCANNER_PROFILE, ClientFactory


def _write_credentials(path: Path, *profiles: str) -> None:
    path.write_text(
        "".join(
            f"[{name}]\naws_access_key_id = testing\naws_secret_access_key = testing\n"
            for name in profiles
        )
    )


@pytest.fixture
def isolated_aws_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point boto3's config resolution at throwaway files.

    Without this, these tests would depend on whatever happens to be in the
    machine's real ~/.aws/ -- exactly the kind of hidden-state flakiness the
    project's fake-credentials fixture exists to avoid.
    """
    creds = tmp_path / "credentials"
    config = tmp_path / "config"
    config.write_text("")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    return creds


def test_falls_back_to_scanner_profile_when_configured(isolated_aws_config: Path):
    _write_credentials(isolated_aws_config, DEFAULT_SCANNER_PROFILE)

    session = ClientFactory()._session()

    assert session.profile_name == DEFAULT_SCANNER_PROFILE


def test_does_not_invent_a_profile_that_does_not_exist(isolated_aws_config: Path):
    _write_credentials(isolated_aws_config, "some-other-profile")

    session = ClientFactory()._session()

    assert session.profile_name == "default"


def test_explicit_profile_wins_over_the_default(isolated_aws_config: Path):
    _write_credentials(isolated_aws_config, DEFAULT_SCANNER_PROFILE, "explicit")

    session = ClientFactory(profile="explicit")._session()

    assert session.profile_name == "explicit"


def test_no_config_files_at_all_falls_back_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No ~/.aws/ at all (e.g. a fresh CI runner) must not raise."""
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-such-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-such-config"))

    session = ClientFactory()._session()

    assert session.profile_name == "default"


def test_client_has_bounded_timeouts_by_default():
    """Never botocore's 60s/60s defaults -- see docs/DECISIONS.md §9."""
    client = ClientFactory().client("ec2", region="ap-south-1")

    assert client.meta.config.connect_timeout == 10
    assert client.meta.config.read_timeout == 30


def test_client_timeouts_are_configurable():
    client = ClientFactory(connect_timeout=3, read_timeout=7).client("ec2", region="ap-south-1")

    assert client.meta.config.connect_timeout == 3
    assert client.meta.config.read_timeout == 7


def test_default_retries_means_one_retry_two_total_attempts():
    """botocore's Config(retries={"max_attempts": N}) means N *retries*, not
    N total attempts -- it silently adds 1 for the initial request. Asserting
    on the real total_max_attempts it produces, not just our own retries
    field, so a botocore behaviour change here doesn't go unnoticed."""
    client = ClientFactory().client("ec2", region="ap-south-1")

    assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 2}


def test_retries_are_configurable():
    client = ClientFactory(retries=4).client("ec2", region="ap-south-1")

    assert client.meta.config.retries["total_max_attempts"] == 5


def test_available_regions_does_not_fall_back_when_the_live_call_succeeds(monkeypatch):
    """Regression test for a parameter-name bug: available_regions() called
    self.client("ec2", region_name="us-east-1"), but client()'s parameter is
    named `region`, not `region_name`. That TypeError'd on every single
    call, silently caught by the broad except, so --all-regions always fell
    back to botocore's full static partition list (every region it has ever
    heard of, opt-in or not) instead of the account's real enabled regions.

    The fallback (get_available_regions) is made to blow up here, so if the
    live ec2:DescribeRegions call is ever broken again the same way, this
    fails loudly instead of quietly returning a plausible-looking list.
    """
    with mock_aws():
        factory = ClientFactory()
        monkeypatch.setattr(
            factory._session(),
            "get_available_regions",
            lambda *a, **kw: pytest.fail("fell back to the static region list"),
        )

        regions = factory.available_regions()

        assert len(regions) > 0


def test_available_regions_warns_and_falls_back_on_failure(monkeypatch, caplog):
    factory = ClientFactory()
    monkeypatch.setattr(
        factory,
        "client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated failure")),
    )

    with caplog.at_level(logging.WARNING):
        regions = factory.available_regions()

    assert len(regions) > 0  # still returns the static fallback, not empty
    assert any("DescribeRegions failed" in r.message for r in caplog.records)
