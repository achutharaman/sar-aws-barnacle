# Decisions

The living spec. Updated whenever scope or a design choice changes, so neither of
us has to reconstruct "why is it like this?" from a long conversation.

**Status:** v0.1 scaffolding complete — core engine and one check, green tests.
**Last updated:** 2026-08-13

---

## 1. What this is

A read-only linter for AWS account waste. It answers one question — *what am I
paying for that nothing is using?* — with resource IDs, dollar figures, and a
CI-usable exit code. It has no delete path.

Positioning matters because the adjacent tools solve the opposite problem.
`aws-nuke` and `cloud-nuke` delete things, which means nobody is allowed to point
them at production. Cost Explorer tells you what you spent, not which specific
resources were pointless.

---

## 2. Naming

| Surface | Name | Reason |
|---|---|---|
| PyPI distribution | `sar-aws-barnacle` | `barnacle` is taken on PyPI by a tensor-decomposition library and an older progress-bar package |
| Import package | `sar_aws_barnacle` | Matches the distribution and CLI naming; see the 2026-08-13 revision below for why this moved off `aws_barnacle` |
| CLI command | `sar-aws-barnacle` | One console script, matching the distribution name exactly |
| GitHub repo | `sar-aws-barnacle` | Matches the distribution |

"Janitor" was rejected: it promises deletion, and this tool deliberately does not
delete. The lint metaphor carries the right contract — read-only, severities,
exit codes.

**2026-08-13 revision:** the CLI command was originally the short `barnacle`
(plus an `aws-barnacle` alias for scripts/CI), on the reasoning that a short
name is nicer to type daily. That was superseded: every project in this
author's portfolio uses a `sar-aws-*` prefix for AWS-focused tools, so the CLI
command, config filename (`sar-aws-barnacle.toml`), env var prefix
(`SAR_AWS_BARNACLE_*`), and IAM policy Sid now all match the distribution name
for consistency across the portfolio.

The import package was originally kept at `aws_barnacle` rather than moving to
`sar_aws_barnacle` with the rest, specifically to dodge a PyPI namespace
collision with the unrelated, already-published `barnacle` package. That
constraint turned out not to apply: this project itself has never been
published (`pypi.org/project/sar-aws-barnacle` returns 404, no `v*` tag has
ever been pushed), so nothing depends on `import aws_barnacle` yet and there
was no live collision risk to avoid. With the constraint gone, the import
package was renamed to `sar_aws_barnacle` for the same portfolio-wide
consistency as everything else in this table. This reasoning stops applying
the moment the package is actually published — renaming the import path after
that point would break every existing installation.

---

## 3. v1 scope

Six checks across four services. Every one is a pure `Describe`/`List` call,
mockable offline with moto, and covered by a single read-only IAM policy.

| Check | Status | Notes |
|---|---|---|
| `ebs-unattached` | **Done** | Volumes in `available` state. Exact cost by size × type |
| `eip-unassociated` | Next | Trivial, exact cost |
| `ec2-stopped` | Planned | Stopped longer than N days |
| `ebs-snapshot-age` | Planned | Age only; cost is an upper bound (see §5) |
| `rds-snapshot-age` | Planned | Same shape as above, different API |
| `iam-stale-keys` | Planned | Old and never-used keys. Global scope. Highest signal per line of code |

### Deferred to v0.3

**`ec2-low-cpu`** — idle running instances via CloudWatch. Different API,
different IAM scope, and moto's CloudWatch metric support is thin enough that it
would need hand-stubbed responses. Also the check most prone to false positives:
a low-CPU box may be memory- or IO-bound.

**`sg-unused`** — "unused" is subtle. A security group with no attached instance
may still be referenced by another SG's rules, or by an RDS instance, ELB, or
Lambda ENI. The naive version ("no rules") produces false positives. The correct
version diffs `describe_network_interfaces` and cross-SG references.

### Cut entirely

**RDS reservation candidates.** Not a hygiene check — a financial recommendation.
Doing it honestly needs weeks of usage history plus Cost Explorer's
`GetReservationPurchaseRecommendation`, which bills per call, cannot be mocked by
moto, and pushes the IAM policy well past read-only-describe. It does not fit a
stateless single-pass scanner. If it returns, it returns as a separate command
with its own opt-in permissions, not as a check in this registry.

---

## 4. Architecture

```
cli.py          Typer adapter. Parses flags, renders. No detection logic.
  └── runner.py Fans out (check × region) across a thread pool, isolates failures
        ├── registry.py   Discovers checks; validates metadata at import time
        ├── context.py    The single object a check receives
        ├── checks/       One file per check
        ├── pricing/      PriceBook: live API → disk cache → bundled seed
        └── output/       Renderer: table, json
```

**Adding a check touches two files** — `checks/foo.py` and
`tests/checks/test_foo.py`. No import list, no registry edit, no CLI change.
Third-party distributions can ship checks via the `sar_aws_barnacle.checks`
entry-point group.

**The check contract** is five class attributes plus `run(ctx)`:

```python
id  # "ebs-unattached" — public API; appears in JSON and --check
title  # human-readable
service  # boto3 client name
scope  # REGIONAL | GLOBAL
required_actions  # frozenset of IAM actions
```

`scope` exists because IAM is global with a regional-looking endpoint. Without
it, a six-region scan reports every stale access key six times.

`required_actions` is not documentation. `sar-aws-barnacle iam-policy` unions it across
the registry to *generate* the policy, and CI asserts `docs/iam-policy.json`
still matches. The README's policy therefore cannot drift from what the code
calls, and a check that forgets to declare a permission fails the build.

---

## 5. Cost honesty

The rule: **an unknown price reports `None`, never `$0.00`**, and is excluded
from the savings total rather than silently deflating it.

`CostConfidence` has four values, and each one exists because of a specific AWS
reality:

| Value | When | Example |
|---|---|---|
| `EXACT` | Price and quantity both known | 100 GiB gp3 volume |
| `LOWER_BOUND` | Real cost is higher than reported | io1/io2 — provisioned IOPS billed separately |
| `UPPER_BOUND` | Real cost is lower than reported | EBS snapshots (below) |
| `UNKNOWN` | No price available | Region absent from the price table |

**EBS snapshot cost is not knowable from the API.** Snapshots bill on
*incremental* consumed storage; `describe_snapshots` returns `VolumeSize`, the
size of the source volume. Any tool reporting an exact snapshot cost is guessing.
We report count + age with an explicitly-labelled upper bound.

`Finding.__post_init__` enforces the pairing: a cost without a confidence level
is rejected, and a confidence level without a cost is rejected.

---

## 6. Pricing source

**Decided:** AWS Pricing API at runtime, backed by a 30-day on-disk cache, with
the bundled seed table as fallback.

The original ask was runtime-only. Pushback and why it was accepted: the Pricing
API is served from only a few regions, so scanning `ap-southeast-2` still opens a
cross-region client purely to ask about prices; it adds `pricing:GetProducts` to
the advertised least-privilege policy; and moto has no Pricing support, so tests
need a static table *regardless* — which means shipping one is free.

The cache-and-fallback design keeps the intent (accurate live prices) while
keeping tests hermetic and the tool usable under a pure-describe policy.

```
--live-pricing      default: fetch, cache 30d at ~/.cache/sar-aws-barnacle/
--no-live-pricing   bundled seed only — offline, deterministic, CI-safe
--refresh-prices    bust the cache
```

Every failure path — no network, no permission, unparseable response — degrades
to the seed and names the fallback in the report footer.

> **Open item.** The seed table is hand-entered and marked approximate in its own
> `_meta.note`. Before publishing: run one live scan, diff the cache against the
> seed, correct anything materially off.

---

## 7. Read-only enforcement

A botocore `before-call` hook inspects every operation before it goes over the
wire and raises `ReadOnlyViolationError` on anything outside
`Describe`/`Get`/`List`/`BatchGet`/`Lookup`/`Head`.

The runner deliberately does **not** swallow this into a `CheckError` the way it
swallows `AccessDenied`. A blocked mutation means sar-aws-barnacle itself has a bug, so
it must fail loudly rather than degrade quietly.

`ClientFactory(read_only=False)` is the seam a future `--fix` mode will use. It
is already tested so the eventual write path does not require redesign.

---

## 8. Failure behaviour

One check failing in one region records a `CheckError` and the scan continues.
A missing `rds:DescribeDBSnapshots` costs you one check, not the whole report.

Exit codes — a stable interface; changing them is a breaking change:

| Code | Meaning |
|---:|---|
| 0 | Completed; nothing tripped `--fail-on` |
| 1 | Findings met or exceeded `--fail-on` |
| 2 | A check failed — results are **partial** |
| 3 | Usage error |

Errors outrank findings. A scan that could not complete is an unanswered
question, not a clean bill of health.

---

## 9. Smaller decisions

- **src layout** — forces tests to run against the installed package, catching
  packaging bugs that otherwise only surface after `pipx install`.
- **TOML config via stdlib `tomllib`** — no YAML dependency.
- **Money serialised as JSON strings** — floats let a total drift by cents under
  summation, which invites exactly the doubt this tool exists to remove.
- **`schema_version` on every JSON document** — people will pipe this into jq and
  dashboards; changing the shape without a version breaks them silently.
- **Thread-local boto3 sessions** — `Session` is not safe to share across threads
  for client creation, and the runner fans out.
- **`output/json_output.py`, not `json.py`** — module name would shadow stdlib
  and trip ruff's A005.
- **`StrEnum` over `str, Enum`** — we require 3.11+.
- **Responsive findings table** — the Detail column budget is computed from the
  measured width of the fixed columns. Rich's own allocator either collapses
  Detail to one character or truncates the identifier columns instead.
- **`min-savings` keeps unpriced findings** — filtering them would make the
  threshold a blindfold over the things we know least about.

---

## 10. Open questions

- [ ] Verify seed prices against a live scan before first PyPI publish
- [ ] Add `src/sar_aws_barnacle/py.typed` (PEP 561 marker) — `pyproject.toml`
      already claims the `Typing :: Typed` classifier, but without this file
      type checkers won't treat the installed package as typed
- [ ] Demo GIF for the README (asciinema → gif)
- [ ] Does `--fix` become a separate `sar-aws-barnacle fix` command with plan/apply, or a
      flag on `scan`? Leaning separate command
- [ ] Coverage gate in CI — what threshold, and does it apply to `cli.py`?
- [ ] `--output sarif` for GitHub code-scanning integration? Speculative
