# barnacle

**A read-only linter for AWS account waste.** Scans for resources you are paying for and not using, prints what they cost you, and never touches a thing.

[![CI](https://github.com/achutharaman/sar-aws-barnacle/actions/workflows/ci.yml/badge.svg)](https://github.com/achutharaman/sar-aws-barnacle/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sar-aws-barnacle.svg)](https://pypi.org/project/sar-aws-barnacle/)
[![Python](https://img.shields.io/pypi/pyversions/sar-aws-barnacle.svg)](https://pypi.org/project/sar-aws-barnacle/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The problem

Cloud waste is boring, invisible, and continuous. An EBS volume detached during a migration two years ago still bills every hour. A developer's stopped test instance keeps its 200 GiB root volume forever. Nobody notices, because nothing is broken — the bill just quietly grows.

AWS Cost Explorer tells you *what you spent*. It does not tell you *which specific resources were pointless*. Existing cleanup tools mostly solve the opposite problem: they delete things, which means nobody is allowed to run them against production.

barnacle sits in the gap. It answers one question — **"what am I paying for that nothing is using?"** — and answers it with resource IDs, dollar figures, and an exit code you can wire into CI. It has no delete path at all.

## Demo

<!-- TODO: replace with an asciinema recording or GIF of `barnacle scan` -->
![barnacle scan demo](docs/demo.gif)

```
$ barnacle scan --region ap-south-1

barnacle scan · 1 region(s) · account 123456789012

┏━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Severity ┃ Check         ┃ Region     ┃ Resource              ┃ Monthly ┃ Detail                           ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ HIGH     │ ebs-unattached│ ap-south-1 │ vol-0a1b2c3d4e5f67890 │  $91.20 │ 1000 GiB gp3 volume in state     │
│          │               │            │                       │         │ 'available', created 412 days ago│
│ MEDIUM   │ ebs-unattached│ ap-south-1 │ vol-0987654321fedcba0 │   $9.12 │ 100 GiB gp3 volume in state      │
│          │               │            │                       │         │ 'available', created 63 days ago │
└──────────┴───────────────┴────────────┴───────────────────────┴─────────┴──────────────────────────────────┘

Estimated monthly waste: $100.32  ($1,203.84/year)
2 finding(s) across 1 check(s) in 0.84s
Prices: AWS Pricing API (cached)
```

## Install

```bash
pipx install sar-aws-barnacle    # recommended: isolated, on your PATH
# or
pip install sar-aws-barnacle
```

Both `barnacle` and `aws-barnacle` are installed as commands.

## Usage

```bash
# Scan your default region using your default credentials
barnacle scan

# A specific region and profile
barnacle scan --region ap-south-1 --profile prod-readonly

# Several regions at once
barnacle scan -r ap-south-1 -r eu-west-1 -r us-east-1

# Every region enabled on the account
barnacle scan --all-regions

# Machine-readable, for pipelines and dashboards
barnacle scan --output json > findings.json

# Only certain checks
barnacle scan --check ebs-unattached

# Hide small change
barnacle scan --min-savings 10

# No network calls for pricing — bundled table only
barnacle scan --no-live-pricing

# What checks exist, and what permissions do they need?
barnacle checks
barnacle iam-policy
```

### Exit codes

Exit codes are a stable interface; changing them is a breaking change.

| Code | Meaning |
|-----:|---------|
| `0` | Scan completed; nothing tripped the `--fail-on` threshold |
| `1` | Findings met or exceeded `--fail-on` |
| `2` | One or more checks failed — results are **partial** |
| `3` | Usage error |

By default (`--fail-on none`) findings are informational and only real errors fail the command. In CI, pick a threshold:

```yaml
- name: Check for AWS waste
  run: barnacle scan --all-regions --fail-on high
```

Note that errors outrank findings. A scan that could not complete is an unanswered question, not a clean bill of health.

## IAM policy

barnacle needs read access and nothing else. This policy is **generated from the code** — every check declares the API calls it makes, and CI fails if this file drifts from what the code actually calls. Regenerate the exact policy for your selection of checks with `barnacle iam-policy`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BarnacleReadOnlyScan",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeRegions",
        "ec2:DescribeVolumes",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

Add `pricing:GetProducts` if you want live pricing. Without it, barnacle falls back to its bundled price table and says so in the report footer.

If you would rather trust nothing: AWS's own `ReadOnlyAccess` managed policy is a superset of the above.

## Configuration

Optional `barnacle.toml`, discovered from the working directory upwards. Precedence, highest first: **CLI flags → `BARNACLE_*` env vars → config file → defaults**.

```toml
[barnacle]
regions = ["ap-south-1", "eu-west-1"]
output = "table"
fail_on = "high"
min_monthly_savings = 5

[barnacle.checks.ebs-unattached]
high_severity_usd = 50
min_age_days = 7
```

## Architecture

```
cli.py          Typer adapter. Parses flags, renders output. No logic.
  └── runner.py Fans out (check × region) across a thread pool, isolates failures
        ├── registry.py   Discovers checks; validates their metadata at import
        ├── context.py    The single object a check receives
        ├── checks/       One file per check. Adding one touches nothing else.
        ├── pricing/      PriceBook protocol: live API → disk cache → bundled seed
        └── output/       Renderer protocol: table, json
```

Four decisions worth calling out:

**Read-only is enforced, not promised.** A botocore `before-call` hook inspects every operation before it goes over the wire and raises on anything that is not a `Describe`/`Get`/`List`. A check that tried to delete something would fail the test suite, not somebody's production account.

**Costs carry a confidence level.** `EXACT`, `LOWER_BOUND`, `UPPER_BOUND`, or `UNKNOWN`. This is not decoration. EBS snapshots bill on *incremental* consumed storage, but the API only reports the source volume's size — so any tool reporting an exact snapshot cost is guessing. An unpriceable resource reports `None`, never `$0.00`, and is excluded from the savings total rather than silently deflating it.

**Failures degrade the scan, they do not end it.** A missing `rds:DescribeDBSnapshots` permission costs you one check, not the whole report. Errors are collected, rendered, and reflected in exit code 2.

**Adding a check touches only two files.** A check is a class with five class attributes and a `run(ctx)` generator. Drop it in `checks/`, add its test, done — no import list, no registry edit, no CLI change. Third-party packages can ship checks via the `aws_barnacle.checks` entry-point group.

### Adding a check

```python
from aws_barnacle.registry import register
from aws_barnacle.models import Finding, Scope, Severity


@register
class UnassociatedElasticIps:
    id = "eip-unassociated"
    title = "Unassociated Elastic IPs"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeAddresses"})

    def run(self, ctx):
        for address in ctx.client().describe_addresses()["Addresses"]:
            if address.get("AssociationId"):
                continue
            yield Finding(...)
```

Then run `python scripts/generate_iam_policy.py` so the published policy covers the new permission.

## Roadmap

**v0.1 — shipped**
- [x] Core engine, registry, read-only guard, pricing, table + JSON output
- [x] `ebs-unattached`

**v0.2 — in progress**
- [ ] `eip-unassociated` — unassociated Elastic IPs
- [ ] `ec2-stopped` — instances stopped longer than N days
- [ ] `ebs-snapshot-age` / `rds-snapshot-age` — ageing snapshots
- [ ] `iam-stale-keys` — old and never-used access keys

**v0.3 — designed, not started**
- [ ] `ec2-low-cpu` — idle running instances via CloudWatch
- [ ] `sg-unused` — security groups attached to no ENI and referenced by no rule
- [ ] `--fix` behind an explicit opt-in, with a plan/apply split

**Considered and rejected for now**
- *RDS reservation recommendations.* Not a hygiene check — it needs weeks of usage history and Cost Explorer's paid API, which does not fit a stateless single-pass scanner. See [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Development

```bash
git clone https://github.com/achutharaman/sar-aws-barnacle
cd sar-aws-barnacle
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                  # all tests, fully offline (moto)
ruff check . && ruff format --check .
```

No test in this repo touches a real AWS account. Credentials are stubbed in `conftest.py` and every AWS interaction is mocked with `moto`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). New checks are very welcome; the bar is a test that proves the detection logic and an honest cost confidence level.

## License

MIT — see [LICENSE](LICENSE).
