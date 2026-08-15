# Decisions

The living spec. Updated whenever scope or a design choice changes, so neither of
us has to reconstruct "why is it like this?" from a long conversation.

**Status:** all eight shipped checks green; two Tier-1 backlog checks
(`vpc-endpoints-unused`, `snapshot-orphaned`) pulled forward and shipped.
**Last updated:** 2026-08-15

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
constraint turned out not to apply *at the time*: this project itself had not
yet been published (`pypi.org/project/sar-aws-barnacle` returned 404, no
`v*` tag had been pushed), so nothing depended on `import aws_barnacle` and
there was no live collision risk to avoid. With the constraint gone, the
import package was renamed to `sar_aws_barnacle` for the same portfolio-wide
consistency as everything else in this table.

**That window has since closed.** `sar-aws-barnacle` `0.1.0` published to
PyPI on 2026-08-14, with the import package already at `sar_aws_barnacle`.
The import path is now load-bearing for real installations — renaming it
again would break every one of them. Treat it as fixed going forward.

---

## 3. v1 scope

Six checks across four services, plus two Tier-1 checks pulled forward from
[`docs/CHECK-BACKLOG.md`](CHECK-BACKLOG.md) once the original six shipped.
Every one is a pure `Describe`/`List` call, mockable offline with moto, and
covered by a single read-only IAM policy.

| Check | Status | Notes |
|---|---|---|
| `ebs-unattached` | **Done** | Volumes in `available` state. Exact cost by size × type |
| `ebs-unencrypted` | **Done** | Security/hygiene, not cost-waste — see note below |
| `eip-unassociated` | **Done** | Trivial, exact flat cost — no age dimension; see note below |
| `ec2-stopped` | **Done** | Cost is attached-EBS-storage only; age is best-effort — see note below |
| `ebs-snapshot-age` | **Done** | Age only; cost is an upper bound (see §5) |
| `rds-snapshot-age` | **Done** | Same shape as above, different API; manual snapshots only — see note below |
| `iam-stale-keys` | **Done** | Security/hygiene, not cost-waste. Global scope — see note below |
| `vpc-endpoints-unused` | **Done** | Cost-awareness, not confirmed-idle — see note below |
| `snapshot-orphaned` | **Done** | Missing source volume, not age; cost is an upper bound (see §5) — see note below |

All eight checks are now shipped. Next up is v0.3 (deferred): `ec2-low-cpu`
and `sg-unused`, both still blocked on the reasons in the "Deferred to v0.3"
section below, plus `--fix` (design not started). Beyond v0.3, candidate
checks are triaged in [`docs/CHECK-BACKLOG.md`](CHECK-BACKLOG.md) rather
than listed here — that document exists specifically so scope decisions for
new checks get recorded once, not re-derived each time someone asks what's
next.

**2026-08-15 addition: `snapshot-orphaned`.** Same cost-honesty shape as
`ebs-snapshot-age` (`VolumeSize` is the size of the source volume — which,
here, doesn't even exist anymore to compare against — so cost is
`UPPER_BOUND`, never `EXACT`). Reuses the same `owner-id`-filter-via-
`sts:GetCallerIdentity` pattern for the same moto `OwnerIds=["self"]`
limitation. AMI-backed snapshots are excluded entirely rather than flagged
with softer wording — a snapshot backing a registered AMI has an active
purpose even though its source volume is gone, the same way
`ebs-unattached` excludes attached volumes rather than caveat-flagging
them. One moto limitation worth recording: `register_image` does not honour
a caller-supplied `SnapshotId` in `BlockDeviceMappings` (it synthesizes its
own), so the real AMI-backed relationship can't be constructed through the
API in tests — the AMI lookup (`_ami_backed_snapshot_ids`) is monkeypatched
directly in `run()` tests, with the pure parsing logic
(`_extract_backing_snapshot_ids`) unit-tested separately against hand-built
data.

**2026-08-15 addition: `vpc-endpoints-unused`.** Dropped
`ec2:DescribeNetworkInterfaces` from the original backlog spec after
verifying it's unnecessary — `DescribeVpcEndpoints` already returns
`SubnetIds`, which is enough to determine the AZ count the hourly charge
scales with. This is a cost-awareness finding, not a confirmed-idle one:
`DescribeVpcEndpoints` has no traffic, connection-count, or last-activity
field (confirmed against the botocore service model) — actual PrivateLink
usage is exclusively a CloudWatch metric, the same dependency blocking the
CloudWatch checks in `docs/CHECK-BACKLOG.md`. Every Interface endpoint is
reported, worded as "here's what this costs, confirm it's still needed"
rather than "this is unused." Gateway endpoints (S3, DynamoDB) are free and
excluded entirely — flagging one would report imaginary savings. Cost is
`LOWER_BOUND`: the hourly per-AZ charge is priced, but data-processing
charges aren't visible from a Describe call. Needed a new pricing
dimension, `VPC_ENDPOINT_HOUR` — added as data, same as
`RDS_SNAPSHOT_GB_MONTH` before it.

**2026-08-15 addition: `iam-stale-keys`.** The first `Scope.GLOBAL` check,
and security/hygiene rather than cost-waste, same as `ebs-unencrypted` — IAM
itself has no price, so every finding carries `CostConfidence.UNKNOWN` by
design, not as a pricing gap. Deliberately does *not* use
`iam:GenerateCredentialReport` (the standard, efficient, one-call way to
audit stale credentials): that operation name doesn't start with an allowed
read-only prefix (`Describe`/`Get`/`List`/`BatchGet`/`Lookup`/`Head`), so
the botocore guard would block it and crash the scan (`ReadOnlyViolationError`
propagates rather than degrading — see §7) rather than expanding the
allowlist without the discussion the hard rules call for. Uses
`ListUsers` + `ListAccessKeys` + `GetAccessKeyLastUsed` instead — more API
calls for a large account, but each one already fits the existing
permission model with zero core changes. "Never used" is measured from
`CreateDate`, "used a while ago" from `LastUsedDate` — a key created
recently and never used yet is not suspicious; a key untouched for a year
is.

**2026-08-15 addition: `rds-snapshot-age`.** Same cost-honesty shape as
`ebs-snapshot-age` (`AllocatedStorage` is the source database's
provisioned size, not what the snapshot's backup storage actually
consumes, so cost is `UPPER_BOUND`, never `EXACT`). Restricted to manual
snapshots only (`SnapshotType=manual`): automated snapshots are AWS-managed
with a retention window capped at 35 days, so one old enough to trip even
the default 90-day threshold cannot exist in practice. Needed a new pricing
dimension, `RDS_SNAPSHOT_GB_MONTH` — added as data (a new dimension
constant plus seed-table entries), not as a change to the pricing
interface itself, matching how `pricing/base.py` is designed to extend.

**2026-08-15 addition: `ebs-snapshot-age`.** Matches the cost design §5
already specified (`UPPER_BOUND`, never `EXACT`) with one implementation
wrinkle worth recording: filters by `owner-id` explicitly (via one
`sts:GetCallerIdentity` call, already a baseline action) rather than
`DescribeSnapshots`'s `OwnerIds=["self"]` keyword. moto does not implement
that keyword — it returns its entire seeded public-AMI snapshot catalog
instead (~1000+ snapshots from unrelated owner accounts), which would have
made this untestable. The explicit `owner-id` filter is equally correct
against real AWS and is what actually gets exercised in tests.

**2026-08-15 addition: `ec2-stopped`.** Two departures from the original
"stopped longer than N days" framing, both because the underlying API
doesn't support it cleanly:

- **Cost is the attached EBS storage, not compute.** AWS does not bill for
  a stopped instance's compute time at all — the only thing still costing
  money is whatever EBS volumes remain attached to it. `DescribeInstances`
  gives volume IDs via `BlockDeviceMappings` but not size/type, so the
  check batches those IDs into one `DescribeVolumes` call (not one per
  instance) and prices them the same way `ebs-unattached` does. An
  instance with no billable storage correctly reports `$0` at
  `EXACT` confidence — that's not the `CostConfidence.UNKNOWN` case; there
  is nothing left unpriced.
- **Age is best-effort, not authoritative.** `DescribeInstances` has no
  `StoppedTime` field, and `LaunchTime` is when the instance was
  *launched* — using it would misreport a two-year-old instance stopped
  yesterday as having sat idle for years. The only place a stop date
  appears is `StateTransitionReason` (e.g. `"User initiated (2026-08-15
  07:19:54 UTC)"`), a field AWS documents as descriptive text, not a
  versioned API contract, and whose format varies by *why* the instance
  stopped — a Spot interruption or failed health check doesn't always
  embed a parseable date. Per explicit instruction: every stopped instance
  is still reported regardless of whether the date parses. Severity is a
  straight 24-hour split — `HIGH` if stopped more than a day ago, `LOW` if
  within the last day, `MEDIUM` when the timestamp can't be parsed at all
  (an unknown age is a real unknown, not quietly downgraded to `LOW`).

**2026-08-15 addition: `eip-unassociated`.** Simpler than planned, in one
respect that's worth recording: `DescribeAddresses` has no allocation
timestamp field at all (confirmed against the botocore service model, not
just moto), so there's no `min_age_days` option here the way
`ebs-unattached` has one — there is nothing to compute age from. Severity is
cost-threshold only (`HIGH` if `≥ high_severity_usd`, else `MEDIUM`); since
the idle-hour rate is flat (~$3.65/month at seed pricing, uniform across
regions in the seed table), the default `$20` threshold rarely fires in
practice and severity is effectively always `MEDIUM` unless a caller lowers
it — an honest reflection of there being no dimension differentiating one
idle EIP from another. Also has no pagination for the same reason
`describe_regions` and `get_caller_identity` don't: `DescribeAddresses`
doesn't support a paginator (an account's EIP count is capped well below a
page size by design), so this is not a hard-rule violation.

**2026-08-14 addition: `ebs-unencrypted`.** Flagged before implementing,
built anyway: this is a security/hygiene finding, not a cost-waste one —
encryption status has zero bearing on what a volume costs, so it doesn't
really answer §1's "what am I paying for that nothing is using?" It was
added on explicit request rather than blocked on the mismatch, because it
fits the existing architecture without touching core: same
`ec2:DescribeVolumes` permission already granted, and
`CostConfidence.UNKNOWN` is already the correct, honest answer for "this
check has no cost claim to make" — not a workaround stretched to fit. Every
finding it produces carries `estimated_monthly_cost=None`, so it never
contributes to "Estimated monthly waste." One exception isn't a pattern; if
more non-cost checks show up, this tool's identity as a pure cost-waste
scanner is worth an explicit revisit rather than drifting into a general
hygiene/CSPM tool by accretion.

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

**The "two files" guarantee extends to reporting, not just detection.**
`ScanResult.region_check_status()` is what both renderers use to show a
per-region, per-check breakdown (every scanned region × every check that
ran, "clean" included, not just findings) — and it's derived entirely from
`checks_run`, `regions`, `findings`, and `errors`. A new check needs zero
changes here or in either renderer to appear in that breakdown. If a check
ever *does* need one, that's the same architecture-failure signal as
needing to touch core for the check itself — stop and flag it, don't
hand-wire per-check rendering. The one piece of metadata this needed beyond
what checks already declare: `ScanResult.global_checks` (which of
`checks_run` are `Scope.GLOBAL`), populated by `run_scan()` from the same
`Check.scope` every check already sets — a global check reports exactly
once against the synthetic `"global"` region, not once per real region,
and a heuristic based on where its findings happened to land can't tell
"global, zero findings" apart from "regional, never checked here."

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
- **`--profile` falls back to `sar-aws-barnacle-scanner` if it exists** — the
  README's setup walkthrough tells readers to create a profile with exactly
  that name, so a bare `sar-aws-barnacle scan` should honour it without
  requiring `--profile` on every invocation. Deliberately conditional:
  `ClientFactory` checks `boto3.Session().available_profiles` first and only
  passes the name through if it's actually configured, because
  `boto3.Session(profile_name=...)` raises `ProfileNotFound` immediately for
  a name that isn't. An unconditional default would have broken every
  deployment that doesn't use that exact profile name — default credentials,
  an instance role, CI OIDC — which is most of them.
- **Explicit `connect_timeout=10` / `read_timeout=30`, and `retries=2` (1
  retry, down from this project's own initial default of 5) on every
  client** — found via a
  real `--all-regions` run that appeared to hang at "33/34" on the progress
  bar: `me-south-1` was unreachable from that network, and botocore's own
  60s/60s timeout defaults meant even a small retry count took minutes to
  finally fail, indistinguishable from a genuine hang. A scan is interactive
  and synchronous, so one bad region's resilience against a transient blip
  costs every other region's wait time too — one retry still absorbs a
  blip; further attempts at a hard-down endpoint were never going to
  succeed. `_run_unit` also logs at DEBUG when a unit starts, so `--verbose`
  shows exactly which (check, region) pairs are still in flight if this
  happens again.
- **Disabled regions are skipped before scanning, not scanned and left to
  fail** — the same investigation surfaced a second, unrelated bug:
  `available_regions()` called `self.client("ec2", region_name="us-east-1")`,
  but `client()`'s parameter is named `region`, not `region_name`. That
  `TypeError`'d on every single call, silently caught by a broad `except`,
  so the "real enabled regions" path had never once succeeded --
  `--all-regions` always fell back to botocore's full static partition list
  (every region it has ever heard of, opt-in or not) instead of the
  account's actual ~17 enabled regions, doubling scan time and producing
  `CheckError`s for regions that were never reachable to begin with. Fixed,
  and `-r` is now also cross-checked against the account's real enabled
  regions before the scan starts (not just `--all-regions`, which already
  gets the enabled list directly) — a disabled region can't have findings,
  it can only burn a timeout+retry. `ScanResult.region_status()` reports
  every region considered, scanned-and-clean included, not just ones with
  findings, so a reader can tell "checked, found nothing" apart from "never
  looked" or "skipped, not enabled."

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
