# sar-aws-barnacle

**A read-only linter for AWS account waste.** Scans for resources you are paying for and not using, prints what they cost you, and never touches a thing.

[![CI](https://github.com/achutharaman/sar-aws-barnacle/actions/workflows/ci.yml/badge.svg)](https://github.com/achutharaman/sar-aws-barnacle/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sar-aws-barnacle.svg)](https://pypi.org/project/sar-aws-barnacle/)
[![Python](https://img.shields.io/pypi/pyversions/sar-aws-barnacle.svg)](https://pypi.org/project/sar-aws-barnacle/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/achutharaman/sar-aws-barnacle/blob/main/LICENSE)

---

## The problem

Cloud waste is boring, invisible, and continuous. An EBS volume detached during a migration two years ago still bills every hour. A developer's stopped test instance keeps its 200 GiB root volume forever. Nobody notices, because nothing is broken — the bill just quietly grows.

AWS Cost Explorer tells you *what you spent*. It does not tell you *which specific resources were pointless*. Existing cleanup tools mostly solve the opposite problem: they delete things, which means nobody is allowed to run them against production.

sar-aws-barnacle sits in the gap. It answers one question — **"what am I paying for that nothing is using?"** — and answers it with resource IDs, dollar figures, and an exit code you can wire into CI. It has no delete path at all.

## Demo

```
$ sar-aws-barnacle scan --region ap-south-1

sar-aws-barnacle scan · 1 region(s) · account 123456789012

Regions
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ Region     ┃ ami-orphaned-snapshots ┃ ebs-snapshot-age ┃ ebs-unattached ┃ ebs-unencrypted ┃ ec2-stopped ┃ eip-unassociated ┃ kms-unused-keys ┃ nat-gw-idle ┃ rds-snapshot-age ┃ snapshot-orphaned ┃ vpc-endpoints-unused ┃ iam-stale-keys ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ ap-south-1 │ clean                  │ clean            │ 2 finding(s)   │ 2 finding(s)    │ clean       │ 1 finding(s)     │ clean           │ clean       │ clean            │ 1 finding(s)      │ clean                │ —              │
│ global     │ —                      │ —                │ —              │ —               │ —           │ —                │ —               │ —           │ —                │ —                 │ —                    │ clean          │
└────────────┴────────────────────────┴──────────────────┴────────────────┴─────────────────┴─────────────┴──────────────────┴─────────────────┴─────────────┴──────────────────┴───────────────────┴──────────────────────┴────────────────┘

┏━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃Severity ┃ Check             ┃ Region     ┃ Resource                   ┃ Monthly ┃ Detail                                                                                                                                                             ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│HIGH     │ ebs-unattached    │ ap-south-1 │ vol-2b99def25e63a706f      │  $91.20 │ 1000 GiB gp3 volume in state 'available' (attached to nothing), created 412 days ago                                                                               │
│MEDIUM   │ ebs-unattached    │ ap-south-1 │ vol-c8ba49fa1a469fe0d      │   $9.12 │ 100 GiB gp3 volume in state 'available' (attached to nothing), created 63 days ago                                                                                 │
│MEDIUM   │ eip-unassociated  │ ap-south-1 │ eipalloc-3d7da59782c245151 │   $3.65 │ Elastic IP 127.129.76.70 is not associated with any resource                                                                                                       │
│MEDIUM   │ snapshot-orphaned │ ap-south-1 │ snap-4469326a4e9951727     │  ≤$2.50 │ 50 GiB snapshot's source volume (vol-c91238d5cb46eb87e) no longer exists; cost is an upper bound -- snapshots bill on incremental storage, which the API does not …│
│MEDIUM   │ ebs-unencrypted   │ ap-south-1 │ vol-2b99def25e63a706f      │ unknown │ 1000 GiB gp3 volume is not encrypted, not attached, created 0 days ago                                                                                             │
│MEDIUM   │ ebs-unencrypted   │ ap-south-1 │ vol-c8ba49fa1a469fe0d      │ unknown │ 100 GiB gp3 volume is not encrypted, not attached, created 0 days ago                                                                                              │
└─────────┴───────────────────┴────────────┴────────────────────────────┴─────────┴────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

Estimated monthly waste: $106.47  ($1,277.64/year)
6 finding(s) across 12 check(s) in 1.14s
2 finding(s) could not be priced and are excluded from the total.
Prices: bundled seed table (2026-08-01)
```

This is real, unfiltered output — a bare `sar-aws-barnacle scan` runs all twelve registered checks, one column per check in the **Regions** grid. Eight report `clean` here; two volumes trip both `ebs-unattached` (cost) and `ebs-unencrypted` (hygiene, `unknown` cost by design — see [Cost honesty](https://github.com/achutharaman/sar-aws-barnacle/blob/main/docs/DECISIONS.md) §5) independently. `iam-stale-keys` is the one `Scope.GLOBAL` check, so it reports once under a synthetic `global` row instead of once per region — the `—` cells mark combinations that don't apply (a regional check has no `global` row and vice versa), not unknown status. `≤$2.50` marks an upper-bound cost.

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

Every report opens with a **Regions** table: one row per region, one column per check, so the whole scan's coverage reads as a single grid — scanned-and-clean included, not just cells with findings, so you can tell "checked, found nothing" apart from "never looked," per check. This is fully automatic: a new check appears as a new column the moment it's registered, with no changes anywhere else. A region passed to `-r` that isn't actually enabled for the account is skipped before the scan starts rather than attempted and left to fail — it's reported separately as `Skipped (not enabled for this account): ...` rather than as blank cells, since it was never scanned at all. `--all-regions` already only ever considers enabled regions, so nothing is skipped there.

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

sar-aws-barnacle needs read access and nothing else. This policy is **generated from the code** — every check declares the API calls it makes, and CI fails if this file drifts from what the code actually calls. The block below is the full policy for every check currently shipped; run `sar-aws-barnacle iam-policy` to get the exact (possibly smaller) policy for whichever checks you actually enable.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SarAwsBarnacleReadOnlyScan",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeAddresses",
        "ec2:DescribeImages",
        "ec2:DescribeInstances",
        "ec2:DescribeNatGateways",
        "ec2:DescribeRegions",
        "ec2:DescribeSnapshots",
        "ec2:DescribeVolumes",
        "ec2:DescribeVpcEndpoints",
        "iam:GetAccessKeyLastUsed",
        "iam:ListAccessKeys",
        "iam:ListUsers",
        "kms:DescribeKey",
        "kms:ListAliases",
        "kms:ListKeys",
        "kms:ListResourceTags",
        "rds:DescribeDBSnapshots",
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
| Minimum savings | `min_monthly_savings` | — | `--min-savings` | `0` | Hide findings cheaper than this per month. Unpriced findings are always kept — see [Cost honesty](https://github.com/achutharaman/sar-aws-barnacle/blob/main/docs/DECISIONS.md) |
| Live pricing | `live_pricing` | `SAR_AWS_BARNACLE_LIVE_PRICING` | `--live-pricing` / `--no-live-pricing` | `true` | Query the AWS Pricing API (cached 30 days) vs. the bundled seed table only |
| Max workers | `max_workers` | — | — | `8` | Parallel (check × region) units in the thread pool |
| Checks to run | `checks_enabled` (array, top-level) | — | `--check` / `-c` | all registered checks | Restrict the scan to specific check ids |

Two settings have no config-file or env var form at all, CLI flag only: `--refresh-prices` (bust the price cache) and the pricing cache directory itself, which follows the standard `XDG_CACHE_HOME` env var and defaults to `~/.cache/sar-aws-barnacle`.

### Per-check tunables

Each check can expose its own options in a `[sar-aws-barnacle.checks.<check-id>]` table — note this is a *different* key (`checks`, nested) from the top-level `checks_enabled` *array* above; one selects which checks run, the other tunes a check that's already running. Unknown keys are ignored, so a config written for an older version stays valid.

| Check | Key | Default | What it does |
|---|---|---|---|
| `ebs-unattached` | `high_severity_usd` | `20` | Findings at or above this monthly cost are `HIGH` instead of `MEDIUM` |
| `ebs-unattached` | `min_age_days` | `0` | Ignore volumes created more recently than this — useful to avoid flagging churn during an active migration |
| `eip-unassociated` | `high_severity_usd` | `20` | Findings at or above this monthly cost are `HIGH`. Idle-EIP pricing is flat (~$3.65/month), so this rarely fires unless lowered — severity is effectively always `MEDIUM` out of the box |
| `ebs-snapshot-age`, `rds-snapshot-age`, `iam-stale-keys` | `min_age_days` | `90` | Ignore anything younger than this |
| `ebs-snapshot-age`, `rds-snapshot-age`, `iam-stale-keys` | `high_severity_days` | `365` | Findings at or beyond this age are `HIGH` instead of `MEDIUM` |
| `vpc-endpoints-unused` | `high_severity_usd` | `20` | Findings at or above this monthly cost are `HIGH` instead of `MEDIUM`. Cost scales with the number of AZs an endpoint spans, so a multi-AZ endpoint crosses this more easily than a single-AZ one |

`ec2-stopped`, `ebs-unencrypted`, `snapshot-orphaned`, `ami-orphaned-snapshots`, `nat-gw-idle`, and `kms-unused-keys` have no tunables — `ec2-stopped`'s `LOW`/`HIGH` split is a fixed 24-hour threshold (see [`docs/DECISIONS.md`](https://github.com/achutharaman/sar-aws-barnacle/blob/main/docs/DECISIONS.md) §3), `ebs-unencrypted`'s severity depends only on whether the volume is attached, `snapshot-orphaned` and `ami-orphaned-snapshots` are always `MEDIUM`, `nat-gw-idle` is always `HIGH`, and `kms-unused-keys` is always `LOW`.

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
class IdleEc2Instances:
    id = "ec2-low-cpu"
    title = "Idle running EC2 instances"
    service = "ec2"
    scope = Scope.REGIONAL
    required_actions = frozenset({"ec2:DescribeInstances", "cloudwatch:GetMetricStatistics"})

    def run(self, ctx):
        for reservation in ctx.client().describe_instances()["Reservations"]:
            for instance in reservation["Instances"]:
                # ctx.client("cloudwatch") for a service other than this
                # check's own -- ctx.client() always accepts an override.
                yield Finding(...)
```

This one's illustrative only — the real `ec2-low-cpu` needs more than a CPU threshold to avoid false-positiving on memory- or IO-bound instances; see [`docs/DECISIONS.md`](https://github.com/achutharaman/sar-aws-barnacle/blob/main/docs/DECISIONS.md) §3 for why it's deferred rather than built yet.

Then run `python scripts/generate_iam_policy.py` so the published policy covers the new permission.

## Contributing

See [CONTRIBUTING.md](https://github.com/achutharaman/sar-aws-barnacle/blob/main/CONTRIBUTING.md). New checks are very welcome; the bar is a test that proves the detection logic and an honest cost confidence level. See [`docs/ROADMAP.md`](https://github.com/achutharaman/sar-aws-barnacle/blob/main/docs/ROADMAP.md) for what's shipped and what's next.

Found a security issue? See [SECURITY.md](https://github.com/achutharaman/sar-aws-barnacle/blob/main/SECURITY.md) — please don't open a public issue for it.

## License

MIT — see [LICENSE](https://github.com/achutharaman/sar-aws-barnacle/blob/main/LICENSE).
