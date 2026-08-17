# Check backlog

Triaged candidates for future checks, so scope decisions get recorded once
instead of re-derived every time someone asks "what should we build next?"
Placement in a tier is not a schedule commitment.

See [`docs/DECISIONS.md`](DECISIONS.md) §3 for the twelve checks already
shipped, §4 for the check contract every candidate below would need to
satisfy, and §5 for the cost-confidence rules the "Cost confidence" column
maps onto. This document only covers what's not built yet.

---

## Tier 1 — high dollar value, pure describe calls, exactly priceable

No new plumbing needed. Each of these fits the existing two-file check
contract the same way the shipped checks do — a paginated Describe/List
call, a priced dimension already knowable from the response, and no
cross-service lookup beyond what a couple of already-baseline actions
provide.

| Check id | What it finds | AWS API | Cost confidence | Notes |
|---|---|---|---|---|
| `logs-no-retention` | CloudWatch log groups set to Never Expire | `logs:DescribeLogGroups` | EXACT | Returns `storedBytes` directly. Often the largest unnoticed line item in an account. |
| `elb-no-targets` | ALB/NLB with zero registered targets | `elasticloadbalancing:DescribeLoadBalancers`, `DescribeTargetGroups`, `DescribeTargetHealth` | LOWER_BOUND | LCU usage isn't visible from Describe calls, so this floors on base hourly cost only. |
| `ebs-gp2-upgradable` | gp2 volumes cheaper as gp3 | `ec2:DescribeVolumes` | EXACT | Same API `ebs-unattached` already calls. Applies to attached volumes too, so this is often the single biggest dollar figure in an account — most gp2 volumes predate gp3 and nobody revisits them. |
| `rds-stopped` | Stopped RDS instances | `rds:DescribeDBInstances` | EXACT (storage) | Storage still bills while stopped, and AWS auto-restarts the instance after 7 days regardless — "stop it to save money" doesn't work for RDS the way it does for EC2. |

`vpc-endpoints-unused` and `snapshot-orphaned` were also Tier 1 candidates
and have since shipped — see `docs/DECISIONS.md` §3.

---

## Tier 2 — real money, more implementation work

Genuine cost-waste signals, but each needs a cross-reference beyond one
resource's own Describe response, a less-common permission, or enough
nuance in "what counts as unused" that it isn't a drop-in the way Tier 1
is.

| Check id | What it finds | AWS API | Cost confidence | Notes |
|---|---|---|---|---|
| `s3-incomplete-multipart` | Incomplete multipart uploads | `s3:ListMultipartUploads` (per bucket) | EXACT | Bills storage forever and is invisible in the console UI. |
| `s3-no-lifecycle` | Buckets with no transition or expiration rules | `s3:ListBuckets`, `GetBucketLifecycleConfiguration` | N/A — hygiene | A configuration gap, not a priced resource. Same shape as `ebs-unencrypted`: real signal, no dollar figure to honestly attach. |
| `ecr-no-lifecycle` | ECR repositories with no lifecycle policy | `ecr:DescribeRepositories`, `GetLifecyclePolicy` | N/A — hygiene | Same shape as `s3-no-lifecycle`; repos grow unbounded without one. |
| `sagemaker-notebook-running` | Notebook instances left running | `sagemaker:ListNotebookInstances` | EXACT (instance-hour) | Expensive per hour, easy to forget outside an active training session. |
| `elasticache-idle` | Clusters with no apparent client usage | `elasticache:DescribeCacheClusters` + a usage signal (TBD) | LOWER_BOUND | Same usage-signal gap as `kms-unused-keys`. |
| `redshift-idle` | Clusters with no apparent usage | `redshift:DescribeClusters` + a usage signal (TBD) | LOWER_BOUND | Same shape as `elasticache-idle`. |
| `backup-stale-recovery-points` | AWS Backup vaults holding very old recovery points | `backup:ListRecoveryPointsByBackupVault` | UPPER_BOUND | Same age/cost-honesty shape as `ebs-snapshot-age` / `rds-snapshot-age`, different service. |
| `elb-classic` | Classic Load Balancers still running | `elasticloadbalancing:DescribeLoadBalancers` (classic API) | EXACT (base) | Deprecated account-wide, but existing ones keep billing indefinitely until migrated off. |

`ami-orphaned-snapshots`, `nat-gw-idle`, and `kms-unused-keys` were also
Tier 2 candidates and have since shipped — see `docs/DECISIONS.md` §3.
`kms-unused-keys` shipped as a key-inventory cost finding (flat EXACT
$1/key/month) rather than the usage-signal-gated version originally
sketched here — see the note in `docs/DECISIONS.md` §3 for why.

---

## Tier 3 — blocked on CloudWatch metrics plumbing

All five need a time-windowed CloudWatch statistic for a resource against a
threshold — the same shape, every time. That's one shared helper, not five
one-off implementations.

**Build these as a batch once that helper exists, not one at a time.**
`ec2-low-cpu` was already flagged as deferred in
[`docs/DECISIONS.md`](DECISIONS.md) §3, for this same reason plus moto's
thin CloudWatch mock support and high false-positive risk; this tier
formalizes that blocker across all five candidates that share it.

| Check id | What it finds | AWS API | Cost confidence | Notes |
|---|---|---|---|---|
| `ec2-low-cpu` | Idle running instances | `cloudwatch:GetMetricStatistics` (CPUUtilization) + `ec2:DescribeInstances` | EXACT (instance-hour) | Most false-positive-prone check in this whole document — a low-CPU box may be memory- or IO-bound, not idle. |
| `lambda-never-invoked` | Functions with zero invocations in the lookback window | `cloudwatch:GetMetricStatistics` (Invocations) + `lambda:ListFunctions` | N/A — hygiene | Lambda bills per invocation, so zero invocations means zero cost, not hidden waste. Signal is dead code, not money. |
| `dynamodb-overprovisioned` | Provisioned-capacity tables far above actual consumption | `cloudwatch:GetMetricStatistics` (Consumed{Read,Write}CapacityUnits) + `dynamodb:ListTables`, `DescribeTable` | LOWER_BOUND | Only meaningful for provisioned-capacity tables — needs a billing-mode check first, on-demand tables don't apply. |
| `nat-gw-no-traffic` | NAT Gateway with negligible bytes processed | `cloudwatch:GetMetricStatistics` (BytesOutToDestination) + `ec2:DescribeNatGateways` | LOWER_BOUND | Distinct from the shipped `nat-gw-idle`: catches a gateway with instances present but not actually using it, which `nat-gw-idle`'s no-running-instances heuristic can't see. |
| `rds-no-connections` | RDS instances with no client connections | `cloudwatch:GetMetricStatistics` (DatabaseConnections) + `rds:DescribeDBInstances` | EXACT (instance-hour) | Catches a running-but-unused instance that Tier 1's `rds-stopped` can't — that one only sees instances already stopped. |

---

## Deferred / cut (carried over from docs/DECISIONS.md §3)

Recorded here too so every scope decision lives in one place, not split
across two documents depending on when it was made.

**`sg-unused`** — deferred, not tiered above. "Unused" is subtle: a
security group with no attached instance may still be referenced by
another SG's rules, or by an RDS instance, ELB, or Lambda ENI. The naive
version ("no rules") produces false positives; the correct version diffs
`describe_network_interfaces` and cross-SG references. Doesn't fit Tier 1
(not a single-API-call check) or Tier 3 (not a CloudWatch problem) — its
own category of implementation risk.

**RDS reservation candidates — cut entirely.** Not a hygiene check, a
financial recommendation. Doing it honestly needs weeks of usage history
plus Cost Explorer's `GetReservationPurchaseRecommendation`, which bills
per call, cannot be mocked by moto, and pushes the IAM policy well past
read-only-describe. Does not fit a stateless single-pass scanner. If it
returns, it returns as a separate command with its own opt-in permissions,
not as a check in this registry.

---

## See also

- [`docs/DECISIONS.md`](DECISIONS.md) §3 — the twelve shipped checks and the full reasoning behind each.
- [`docs/DECISIONS.md`](DECISIONS.md) §4 — the check contract (five class attributes, `run(ctx)`, two files touched, automatic `region_check_status()` reporting).
- [`docs/DECISIONS.md`](DECISIONS.md) §5 — cost-honesty rules: what `EXACT` / `LOWER_BOUND` / `UPPER_BOUND` / `UNKNOWN` each commit to.
