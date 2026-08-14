# Security Policy

## Supported versions

sar-aws-barnacle has no stable release yet — nothing has been published to
PyPI and no `v*` tag exists. Only the latest commit on `main` is supported;
there is no older version to patch.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Use [GitHub's private vulnerability reporting](https://github.com/achutharaman/sar-aws-barnacle/security/advisories/new)
(repo's **Security** tab → "Report a vulnerability"). This keeps the report
private until a fix is ready.

This is a single-maintainer open-source project, so there's no SLA and no
bug bounty, but every report gets read and credited in the eventual
advisory or release notes unless you ask not to be.

## Scope

**In scope:** the `sar-aws-barnacle` CLI and library — anything that could
let it make a mutating AWS call despite the read-only guard, leak
credentials, or otherwise behave differently than documented.

**Out of scope:** findings the tool *reports* about your AWS account (an
unencrypted volume, an unattached EIP, ...) are not vulnerabilities in this
tool — they're what it's designed to surface. Disagreement with a finding's
logic or severity is a regular issue, not a security report.

## What this tool does with your credentials

sar-aws-barnacle never stores, transmits, or logs AWS credentials. It uses
the standard AWS SDK (boto3) credential chain — the same one the AWS CLI
uses — and writes nothing to disk besides an optional, opt-in local price
cache (`~/.cache/sar-aws-barnacle/`, which holds no credentials).

Read-only is enforced structurally, not just documented: a botocore
`before-call` hook inspects every API call before it goes over the wire and
raises if it isn't a `Describe`/`Get`/`List`/`BatchGet`/`Lookup`/`Head`
operation. See [`docs/DECISIONS.md`](docs/DECISIONS.md) §7.
