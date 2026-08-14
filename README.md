# sar-aws-barnacle

**A read-only linter for AWS account waste.** Scans for resources you are paying for and not using, prints what they cost you, and never touches a thing.

[![CI](https://github.com/achutharaman/sar-aws-barnacle/actions/workflows/ci.yml/badge.svg)](https://github.com/achutharaman/sar-aws-barnacle/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sar-aws-barnacle.svg)](https://pypi.org/project/sar-aws-barnacle/)
[![Python](https://img.shields.io/pypi/pyversions/sar-aws-barnacle.svg)](https://pypi.org/project/sar-aws-barnacle/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The problem

Cloud waste is boring, invisible, and continuous. An EBS volume detached during a migration two years ago still bills every hour. A developer's stopped test instance keeps its 200 GiB root volume forever. Nobody notices, because nothing is broken — the bill just quietly grows.

AWS Cost Explorer tells you *what you spent*. It does not tell you *which specific resources were pointless*. Existing cleanup tools mostly solve the opposite problem: they delete things, which means nobody is allowed to run them against production.

sar-aws-barnacle sits in the gap. It answers one question — **"what am I paying for that nothing is using?"** — and answers it with resource IDs, dollar figures, and an exit code you can wire into CI. It has no delete path at all.

## Demo

<!-- TODO: replace with an asciinema recording or GIF of `sar-aws-barnacle scan` -->
![sar-aws-barnacle scan demo](docs/demo.gif)

```
$ sar-aws-barnacle scan --region ap-south-1

sar-aws-barnacle scan · 1 region(s) · account 123456789012

Regions
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Region     ┃ Status       ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ ap-south-1 │ 2 finding(s) │
└────────────┴──────────────┘

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

The `sar-aws-barnacle` command is installed on your `PATH`.

## Usage

```bash
# Scan your default region using your default credentials
sar-aws-barnacle scan

# A specific region and profile
sar-aws-barnacle scan --region ap-south-1 --profile prod-readonly

# Several regions at once
sar-aws-barnacle scan -r ap-south-1 -r eu-west-1 -r us-east-1

# Every region enabled on the account
sar-aws-barnacle scan --all-regions

# Machine-readable, for pipelines and dashboards
sar-aws-barnacle scan --output json > findings.json

# Only certain checks
sar-aws-barnacle scan --check ebs-unattached

# Hide small change
sar-aws-barnacle scan --min-savings 10

# No network calls for pricing — bundled table only
sar-aws-barnacle scan --no-live-pricing

# What checks exist, and what permissions do they need?
sar-aws-barnacle checks
sar-aws-barnacle iam-policy
```

Every report opens with a **Regions** table listing every region considered — scanned-and-clean regions included, not just ones with findings, so you can tell "checked, found nothing" apart from "never looked." A region passed to `-r` that isn't actually enabled for the account is skipped before the scan starts rather than attempted and left to fail, and shows up there as `skipped`. `--all-regions` already only ever considers enabled regions, so nothing is skipped there.

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
  run: sar-aws-barnacle scan --all-regions --fail-on high
```

Note that errors outrank findings. A scan that could not complete is an unanswered question, not a clean bill of health.

## IAM policy

sar-aws-barnacle needs read access and nothing else. This policy is **generated from the code** — every check declares the API calls it makes, and CI fails if this file drifts from what the code actually calls. Regenerate the exact policy for your selection of checks with `sar-aws-barnacle iam-policy`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SarAwsBarnacleReadOnlyScan",
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

Add `pricing:GetProducts` if you want live pricing. Without it, sar-aws-barnacle falls back to its bundled price table and says so in the report footer.

If you would rather trust nothing: AWS's own `ReadOnlyAccess` managed policy is a superset of the above.

## Set up read-only access

The policy above is generated, not created — you still need to turn it into something `--profile` can point at. This only needs whatever access your own AWS credentials already have; sar-aws-barnacle itself never touches IAM.

**1. Save the policy and create it**

```bash
sar-aws-barnacle iam-policy > sar-aws-barnacle-policy.json

aws iam create-policy \
  --policy-name sar-aws-barnacle-read-only-scan \
  --policy-document file://sar-aws-barnacle-policy.json
```

**2. Attach it to a user or role**

A dedicated IAM user with programmatic access is the simplest path for a laptop or a CI runner:

```bash
aws iam create-user --user-name sar-aws-barnacle-scanner
aws iam attach-user-policy \
  --user-name sar-aws-barnacle-scanner \
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/sar-aws-barnacle-read-only-scan
```

Prefer a role instead if you're scanning from an EC2 instance, a CI job with OIDC, or assuming into other accounts — attach the same policy to the role and skip the rest of this section entirely.

**3. Create the access key and wire it straight into a profile**

Piping the key from `create-access-key` into `aws configure set` means the secret only ever lives in a shell variable, never a file on disk:

```bash
CREDS=$(aws iam create-access-key --user-name sar-aws-barnacle-scanner --output json)
aws configure set aws_access_key_id "$(jq -r .AccessKey.AccessKeyId <<<"$CREDS")" \
  --profile sar-aws-barnacle-scanner
aws configure set aws_secret_access_key "$(jq -r .AccessKey.SecretAccessKey <<<"$CREDS")" \
  --profile sar-aws-barnacle-scanner
aws configure set region ap-south-1 --profile sar-aws-barnacle-scanner
unset CREDS
```

Requires `jq`. Without it, fall back to `aws configure --profile sar-aws-barnacle-scanner` and paste the `AccessKeyId` / `SecretAccessKey` from the `create-access-key` output by hand.

**4. Scan with it**

```bash
sar-aws-barnacle scan --profile sar-aws-barnacle-scanner --region ap-south-1
```

`--profile` is actually optional here: if a profile named `sar-aws-barnacle-scanner` exists in your AWS config, `sar-aws-barnacle scan` uses it automatically when you don't pass `--profile` at all. This is a fallback, not a requirement — it only ever applies when that exact profile is present, so it changes nothing for default credentials, an instance role, or CI OIDC.

If this was a one-off audit, clean up afterwards — `aws iam delete-access-key` and `aws iam delete-user` (after detaching the policy). Read-only access has nothing to steal, but an unused access key is still a credential not worth leaving around.

## Configuration

Optional `sar-aws-barnacle.toml`, discovered from the working directory upwards. Precedence, highest first: **CLI flags → `SAR_AWS_BARNACLE_*` env vars → config file → defaults**.

### Create one

```bash
# Project-local: only applies when you run the CLI from this directory or below
cp sar-aws-barnacle.toml.example sar-aws-barnacle.toml

# Or a personal default: found no matter which directory you scan from, as
# long as it's somewhere under your home directory (the leading dot is required)
cp sar-aws-barnacle.toml.example ~/.sar-aws-barnacle.toml
```

Then edit the values you want to change — anything you leave out falls back to its default. `sar-aws-barnacle.toml.example` is a committed template only; the CLI never reads that file directly, so copying it is required, not optional decoration.

### Top-level settings

| Setting | TOML key | Env var | CLI flag | Default | What it does |
|---|---|---|---|---|---|
| Regions | `regions` | `SAR_AWS_BARNACLE_REGIONS` (comma-separated) | `--region` / `-r` | none — falls back to your profile's default region | Which regions to scan |
| All regions | `all_regions` | — | `--all-regions` | `false` | Scan every region enabled on the account; overrides `regions` |
| Profile | `profile` | `SAR_AWS_BARNACLE_PROFILE` | `--profile` | `sar-aws-barnacle-scanner` if configured, else default AWS credentials | Named AWS CLI profile to authenticate with |
| Output | `output` | `SAR_AWS_BARNACLE_OUTPUT` | `--output` / `-o` | `table` | `table` or `json` |
| Fail on | `fail_on` | `SAR_AWS_BARNACLE_FAIL_ON` | `--fail-on` | `none` | Minimum severity that exits 1: `none`, `any`, `low`, `medium`, `high` |
| Minimum savings | `min_monthly_savings` | — | `--min-savings` | `0` | Hide findings cheaper than this per month. Unpriced findings are always kept — see [Cost honesty](docs/DECISIONS.md) |
| Live pricing | `live_pricing` | `SAR_AWS_BARNACLE_LIVE_PRICING` | `--live-pricing` / `--no-live-pricing` | `true` | Query the AWS Pricing API (cached 30 days) vs. the bundled seed table only |
| Max workers | `max_workers` | — | — | `8` | Parallel (check × region) units in the thread pool |
| Checks to run | `checks_enabled` (array, top-level) | — | `--check` / `-c` | all registered checks | Restrict the scan to specific check ids |

Two settings have no config-file or env var form at all, CLI flag only: `--refresh-prices` (bust the price cache) and the pricing cache directory itself, which follows the standard `XDG_CACHE_HOME` env var and defaults to `~/.cache/sar-aws-barnacle`.

### Per-check tunables

Each check can expose its own options in a `[sar-aws-barnacle.checks.<check-id>]` table — note this is a *different* key (`checks`, nested) from the top-level `checks_enabled` *array* above; one selects which checks run, the other tunes a check that's already running. Unknown keys are ignored, so a config written for an older version stays valid.

For `ebs-unattached`:

| Key | Default | What it does |
|---|---|---|
| `high_severity_usd` | `20` | Findings at or above this monthly cost are `HIGH` instead of `MEDIUM` |
| `min_age_days` | `0` | Ignore volumes created more recently than this — useful to avoid flagging churn during an active migration |

```toml
[sar-aws-barnacle]
regions = ["ap-south-1", "eu-west-1"]
output = "table"
fail_on = "high"
min_monthly_savings = 5

[sar-aws-barnacle.checks.ebs-unattached]
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

**Adding a check touches only two files.** A check is a class with five class attributes and a `run(ctx)` generator. Drop it in `checks/`, add its test, done — no import list, no registry edit, no CLI change. Third-party packages can ship checks via the `sar_aws_barnacle.checks` entry-point group.

### Adding a check

```python
from sar_aws_barnacle.registry import register
from sar_aws_barnacle.models import Finding, Scope, Severity


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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). New checks are very welcome; the bar is a test that proves the detection logic and an honest cost confidence level.

## License

MIT — see [LICENSE](LICENSE).
