# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Five new checks, completing the full v1 roadmap: `ebs-unencrypted`,
  `eip-unassociated`, `ec2-stopped`, `ebs-snapshot-age`, `rds-snapshot-age`,
  and `iam-stale-keys` (the first `Scope.GLOBAL` check). Several needed
  real design deviations from the original roadmap wording, forced by AWS
  API limitations rather than choice — see `docs/DECISIONS.md` §3 for the
  reasoning behind each.
- Two more checks pulled forward from `docs/CHECK-BACKLOG.md`:
  `vpc-endpoints-unused` (Interface VPC endpoints, a cost-awareness finding
  rather than a confirmed-idle one — see `docs/DECISIONS.md` §3) and
  `snapshot-orphaned` (EBS snapshots whose source volume no longer exists,
  with AMI-backed snapshots excluded entirely).
- Live progress bar during `scan` (stderr only; skipped automatically for
  piped or non-interactive output).
- Per-region, per-check status breakdown in both the table and JSON
  reports (`ScanResult.region_check_status()`) — every region × check
  combination is reported, "clean" included, not just ones with findings.
  Derived entirely from existing scan data; a new check needs no renderer
  changes to appear in it.
- Regions not enabled for the account are now skipped before scanning
  (rather than attempted and left to fail) and reported separately.
- `--profile` falls back to a `sar-aws-barnacle-scanner` profile if one is
  configured, so a bare `sar-aws-barnacle scan` works without repeating
  `--profile` every time — falls through to default credentials if that
  profile doesn't exist.
- `SECURITY.md` — vulnerability reporting policy.

### Changed
- CLI command, config filename, env var prefix, and IAM policy Sid renamed
  to the `sar-aws-barnacle` / `SAR_AWS_BARNACLE_*` convention used across
  this author's other projects (previously the shorter `barnacle`).
- Import package renamed `aws_barnacle` → `sar_aws_barnacle` to match. This
  is the last time it moves now that the package is published — see
  `docs/DECISIONS.md` §2.
- Client `connect_timeout` / `read_timeout` are now explicit (10s / 30s)
  and retries reduced to one, so an unreachable or throttled region fails
  in seconds instead of potentially minutes.
- README trimmed: local dev/release/CI process moved out of the public
  README into an internal reference document; a step-by-step IAM setup
  walkthrough added in its place.

### Fixed
- `Severity` comparison operators (`<`, `<=`, `>`, `>=`) silently
  disagreed with each other (`StrEnum`'s inherited alphabetical comparison
  vs. the overridden rank-based `__lt__`). Disabled outright — `.rank` is
  the one supported way to compare severities, and misuse now raises
  `TypeError` instead of returning a plausible-looking wrong answer.
- `Finding.metadata` was mutable at runtime despite `Finding` being a
  frozen dataclass. Now wrapped in `MappingProxyType` over a defensive
  copy.
- `--all-regions` was silently falling back to botocore's full static
  region list (every region it has ever heard of, including ones the
  account was never opted into) instead of the account's real enabled
  regions, due to a parameter-name bug in the internal region-lookup call.

## [0.1.0] - 2026-08-14

### Added
- Core scanning engine: check registry, region fan-out, per-check error isolation.
- Read-only enforcement via a botocore `before-call` guard.
- `ebs-unattached` check with cost estimation and confidence levels.
- Table and JSON renderers; JSON carries `schema_version` 1.0.
- Generated least-privilege IAM policy (`sar-aws-barnacle iam-policy`), verified in CI.
- AWS Pricing API integration with a 30-day disk cache and bundled seed fallback.

[Unreleased]: https://github.com/achutharaman/sar-aws-barnacle/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/achutharaman/sar-aws-barnacle/releases/tag/v0.1.0
