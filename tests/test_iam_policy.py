"""The committed IAM policy must match what the code actually calls.

This is the test that keeps the README honest. Add a check with a new API call
and forget to regenerate the policy, and CI fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_barnacle.iam import BASELINE_ACTIONS, build_policy, collect_actions
from aws_barnacle.registry import all_checks

POLICY_PATH = Path(__file__).resolve().parents[1] / "docs" / "iam-policy.json"


def test_committed_policy_matches_registry():
    committed = json.loads(POLICY_PATH.read_text())
    assert committed == build_policy(), (
        "docs/iam-policy.json is stale. Regenerate with:\n  python scripts/generate_iam_policy.py"
    )


def test_policy_includes_every_check_action():
    actions = set(collect_actions())
    for check in all_checks():
        assert check.required_actions <= actions


def test_policy_includes_runner_baseline():
    assert set(collect_actions()) >= BASELINE_ACTIONS


def test_policy_contains_no_write_actions():
    """A read-only tool must never ask for a permission it cannot use."""
    forbidden = ("Create", "Delete", "Put", "Update", "Modify", "Terminate", "Attach", "Detach")
    for action in collect_actions():
        verb = action.split(":", 1)[1]
        assert not verb.startswith(forbidden), f"{action} is not read-only"


def test_policy_can_be_scoped_to_selected_checks():
    single = build_policy([all_checks()[0]])
    assert single["Statement"][0]["Effect"] == "Allow"
    assert len(single["Statement"]) == 1
