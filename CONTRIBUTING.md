# Contributing

Thanks for looking. New checks are the most useful contribution — the whole
architecture exists to make them cheap to add.

## Setup

```bash
git clone https://github.com/achutharaman/sar-aws-barnacle
cd sar-aws-barnacle
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest
ruff check . && ruff format --check .
```

No test touches a real AWS account. Credentials are stubbed in `conftest.py` and
every AWS interaction is mocked with `moto`. If a test of yours needs real
credentials, something has gone wrong in the design.

## Adding a check

Two files. Nothing in core changes.

**1. `src/aws_barnacle/checks/your_check.py`**

```python
from aws_barnacle.models import CostConfidence, Finding, Scope, Severity
from aws_barnacle.registry import register


@register
class UnassociatedElasticIps:
    id = "eip-unassociated"  # public API — appears in JSON and --check
    title = "Unassociated Elastic IPs"
    service = "ec2"  # boto3 client name
    scope = Scope.REGIONAL  # or Scope.GLOBAL for IAM, Route 53, etc.
    required_actions = frozenset({"ec2:DescribeAddresses"})

    def run(self, ctx):
        for address in ctx.client().describe_addresses()["Addresses"]:
            if address.get("AssociationId"):
                continue
            yield Finding(...)
```

**2. `tests/checks/test_your_check.py`** — use the `make_context` fixture and
`@mock_aws`. See `tests/checks/test_ebs_unattached.py` for the shape.

**3. Regenerate the IAM policy** if you added a new API call:

```bash
python scripts/generate_iam_policy.py
```

CI fails if `docs/iam-policy.json` is stale, so this is not optional.

## What gets a check merged

**A test that proves the detection logic, not just that the code runs.** Assert
on the specific cost, the specific severity, and at least one negative case — a
resource that should *not* be flagged.

**Honest cost confidence.** If you cannot price something, return `None` and
`CostConfidence.UNKNOWN`. Never `Decimal(0)`. If the real cost is higher than
what you can compute (unbilled dimensions), use `LOWER_BOUND` and say so in the
finding's `detail`. A cost report loses all its value the moment a reader catches
it overstating one number.

**Read-only API calls.** The `before-call` guard will reject anything else, and
that is deliberate. If your check genuinely needs a mutating call, open an issue
first — it is a design conversation, not a one-line addition.

**Pagination.** Use `get_paginator` wherever the API offers one. Accounts with
2,000 volumes are common and truncated results silently understate waste.

**No false positives you know about.** A check that cries wolf gets disabled, and
a disabled check finds nothing. If a resource *might* legitimately look idle,
either detect the legitimate case or document the limitation in `detail`.

## Style

`ruff` handles formatting and linting; both run in CI. Line length 100. Type
hints on public functions. Comments should explain *why*, not restate the code —
if a line needs explaining because AWS behaves strangely, that comment is worth
more than the code.

## Reporting bugs

Include the check id, the region, and the redacted `--output json` for the
finding if you can. Never paste account IDs, ARNs with account numbers, or
anything from `~/.aws/`.

## Scope

Before proposing a large feature, read [`docs/DECISIONS.md`](docs/DECISIONS.md) —
several obvious-looking ideas (reservation recommendations, CloudWatch-based
idle detection) were considered and deliberately deferred or cut, with reasons.
If you disagree with one of those calls, the issue tracker is the right place;
the reasoning is written down precisely so it can be argued with.
