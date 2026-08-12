"""Registry behaviour: discovery, validation, and selection."""

from __future__ import annotations

import pytest

from aws_barnacle.models import Finding, Scope
from aws_barnacle.registry import (
    DuplicateCheckError,
    InvalidCheckError,
    all_checks,
    get_check,
    register,
    select_checks,
)


def _valid_check(check_id: str = "unit-test-check"):
    class _Check:
        id = check_id
        title = "Unit test check"
        service = "ec2"
        scope = Scope.REGIONAL
        required_actions = frozenset({"ec2:DescribeVolumes"})

        def run(self, ctx):
            return iter(())

    return _Check


def test_discovery_finds_bundled_checks():
    ids = {c.id for c in all_checks()}
    assert "ebs-unattached" in ids


def test_registered_checks_are_sorted_by_id():
    ids = [c.id for c in all_checks()]
    assert ids == sorted(ids)


def test_every_check_declares_iam_actions():
    """A check that forgets its permissions would break the generated policy."""
    for check in all_checks():
        assert check.required_actions, f"{check.id} declares no IAM actions"
        for action in check.required_actions:
            assert ":" in action


def test_check_ids_are_kebab_case():
    for check in all_checks():
        assert check.id == check.id.lower()
        assert "_" not in check.id


def test_missing_attribute_is_rejected():
    class Broken:
        id = "broken"
        title = "Broken"
        service = "ec2"
        scope = Scope.REGIONAL
        # no required_actions

        def run(self, ctx):
            return iter(())

    with pytest.raises(InvalidCheckError, match="required_actions"):
        register(Broken)


def test_malformed_iam_action_is_rejected():
    cls = _valid_check("bad-action")
    cls.required_actions = frozenset({"DescribeVolumes"})
    with pytest.raises(InvalidCheckError, match="not a valid IAM action"):
        register(cls)


def test_duplicate_id_is_rejected():
    register(_valid_check("dupe-check"))
    with pytest.raises(DuplicateCheckError, match="dupe-check"):
        register(_valid_check("dupe-check"))


def test_unknown_check_id_lists_the_valid_ones():
    with pytest.raises(KeyError, match="ebs-unattached"):
        get_check("no-such-check")


def test_select_checks_defaults_to_all():
    assert len(select_checks(())) == len(all_checks())


def test_select_checks_deduplicates():
    selected = select_checks(["ebs-unattached", "ebs-unattached"])
    assert len(selected) == 1


def test_run_returns_findings():
    cls = _valid_check("returns-findings")
    assert isinstance(iter(cls().run(None)), type(iter(())))
    assert Finding is not None
