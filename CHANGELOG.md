# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Core scanning engine: check registry, region fan-out, per-check error isolation.
- Read-only enforcement via a botocore `before-call` guard.
- `ebs-unattached` check with cost estimation and confidence levels.
- Table and JSON renderers; JSON carries `schema_version` 1.0.
- Generated least-privilege IAM policy (`sar-aws-barnacle iam-policy`), verified in CI.
- AWS Pricing API integration with a 30-day disk cache and bundled seed fallback.

[Unreleased]: https://github.com/achutharaman/sar-aws-barnacle/commits/main
