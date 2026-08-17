# Roadmap

What's shipped, what's next, and what was considered and rejected. For the
reasoning behind any individual decision, see [`docs/DECISIONS.md`](DECISIONS.md)
§3; for candidates not yet scheduled, see [`docs/CHECK-BACKLOG.md`](CHECK-BACKLOG.md).

**v0.1 — shipped, all seven planned checks done**
- [x] Core engine, registry, read-only guard, pricing, table + JSON output
- [x] `ebs-unattached`
- [x] `ebs-unencrypted` — security/hygiene, not cost-waste; see [`docs/DECISIONS.md`](DECISIONS.md) §3
- [x] `eip-unassociated` — unassociated Elastic IPs
- [x] `ec2-stopped` — long-stopped instances; cost is attached storage, age is best-effort; see [`docs/DECISIONS.md`](DECISIONS.md) §3
- [x] `ebs-snapshot-age` — ageing snapshots; cost is an upper bound (see [`docs/DECISIONS.md`](DECISIONS.md) §5)
- [x] `rds-snapshot-age` — same shape, manual snapshots only
- [x] `iam-stale-keys` — old and never-used access keys; security/hygiene, first `Scope.GLOBAL` check; see [`docs/DECISIONS.md`](DECISIONS.md) §3

**Pulled forward from [`docs/CHECK-BACKLOG.md`](CHECK-BACKLOG.md) — shipped**
- [x] `vpc-endpoints-unused` — Interface VPC endpoints; cost-awareness, not confirmed-idle; see [`docs/DECISIONS.md`](DECISIONS.md) §3
- [x] `snapshot-orphaned` — EBS snapshots whose source volume no longer exists; cost is an upper bound (see [`docs/DECISIONS.md`](DECISIONS.md) §5)
- [x] `ami-orphaned-snapshots` — snapshots left behind by a deregistered AMI; cost is an upper bound (see [`docs/DECISIONS.md`](DECISIONS.md) §5)
- [x] `nat-gw-idle` — NAT Gateway in a VPC with no running instances; a heuristic, not proof — see [`docs/DECISIONS.md`](DECISIONS.md) §3
- [x] `kms-unused-keys` — customer-managed KMS key inventory; a cost finding, not an idleness claim — see [`docs/DECISIONS.md`](DECISIONS.md) §3

Twelve checks shipped in total (seven original + five pulled forward).

**v0.3 — designed, not started**
- [ ] `ec2-low-cpu` — idle running instances via CloudWatch
- [ ] `sg-unused` — security groups attached to no ENI and referenced by no rule
- [ ] `--fix` behind an explicit opt-in, with a plan/apply split

**Considered and rejected for now**
- *RDS reservation recommendations.* Not a hygiene check — it needs weeks of usage history and Cost Explorer's paid API, which does not fit a stateless single-pass scanner. See [`docs/DECISIONS.md`](DECISIONS.md).
