# CLAUDE.md — working agreement for this repo

Read docs/DECISIONS.md before proposing anything. It is the living spec;
update it whenever scope or a design decision changes.

## What this is
Read-only AWS waste scanner. Lint metaphor: findings, severities, exit
codes, CI-usable. There is NO delete path and none may be added without
an explicit design discussion.

## How we work
- Before implementing any requested change, state its impact first: which
  modules/tests are affected, whether it breaks anything, whether redesign
  is needed. Wait for confirmation on anything non-trivial.
- If a request conflicts with an earlier decision in docs/DECISIONS.md,
  push back and propose an alternative instead of silently complying.
- Every change must leave the repo committable: tests green, ruff clean.
- Small commits, conventional-commit messages (feat:, fix:, test:, docs:).

## Hard rules (structural, tested — do not weaken)
- Adding a check touches exactly two files: src/aws_barnacle/checks/<id>.py
  and tests/checks/test_<id>.py. If a check requires touching core, stop
  and flag it — that's an architecture failure, not a workaround target.
- Every check declares required_actions; after adding one, run
  `python scripts/generate_iam_policy.py` (CI diffs the committed policy).
- Cost honesty: unknown price → None + CostConfidence.UNKNOWN, never
  Decimal(0). Under-counted dimensions → LOWER_BOUND, stated in detail.
- Only read-only AWS calls (Describe/Get/List/BatchGet/Lookup/Head).
  The botocore guard enforces this; don't add prefixes to the allowlist
  without discussion.
- Use paginators for any list call. Global-scope checks (IAM, etc.) use
  Scope.GLOBAL or they'll report duplicates per region.
- Checks get everything from ScanContext: ctx.client(), ctx.option(),
  ctx.price(), ctx.age_days(ctx.now). Never boto3.client() directly,
  never datetime.now() in a check.

## Commands
- Test: `pytest` (offline; moto — no test may need real AWS creds)
- Lint: `ruff check . && ruff format --check .`
- Coverage: `pytest --cov --cov-report=term-missing` (currently 92%; don't regress)
- Regenerate IAM policy: `python scripts/generate_iam_policy.py`

## Roadmap position
Done: ebs-unattached. Next, in order: eip-unassociated, ec2-stopped,
ebs-snapshot-age, rds-snapshot-age, iam-stale-keys. Deferred/cut items
and reasons: docs/DECISIONS.md §3.