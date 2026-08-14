"""Scan orchestration.

Fans out over (check, region) pairs and collects findings. The important
behaviour here is what happens when something goes wrong: a check that raises
becomes a recorded ``CheckError`` and the scan continues. A tool that aborts the
whole run because one IAM permission was missing is useless in CI, and losing
five checks' worth of findings to one AccessDenied is a bad trade.

The one exception is ``ReadOnlyViolationError``. That means sar-aws-barnacle
itself tried to mutate a resource, which is a bug in this codebase, and it
propagates.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from sar_aws_barnacle.config import Config
from sar_aws_barnacle.context import ScanContext
from sar_aws_barnacle.models import GLOBAL_REGION, CheckError, Finding, ScanResult, Scope
from sar_aws_barnacle.pricing.base import PriceBook
from sar_aws_barnacle.registry import Check
from sar_aws_barnacle.session import ClientFactory, ReadOnlyViolationError

log = logging.getLogger(__name__)

# Global-scope checks still need an endpoint to talk to; IAM lives in us-east-1.
GLOBAL_ENDPOINT_REGION = "us-east-1"


def plan_units(
    checks: Sequence[type[Check]], regions: Sequence[str]
) -> list[tuple[type[Check], str]]:
    """Expand checks across regions, running global checks exactly once.

    Without the scope split, a six-region scan would report every stale IAM
    access key six times.
    """
    units: list[tuple[type[Check], str]] = []
    for check in checks:
        if check.scope is Scope.GLOBAL:
            units.append((check, GLOBAL_ENDPOINT_REGION))
        else:
            units.extend((check, region) for region in regions)
    return units


def run_scan(
    checks: Sequence[type[Check]],
    regions: Sequence[str],
    *,
    factory: ClientFactory,
    config: Config,
    prices: PriceBook,
    now: datetime | None = None,
    account_id: str | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ScanResult:
    started = datetime.now(UTC)
    reference_time = now or started
    clock_start = time.perf_counter()

    findings: list[Finding] = []
    errors: list[CheckError] = []
    units = plan_units(checks, regions)
    total = len(units)
    workers = max(1, min(config.max_workers, total or 1))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sar-aws-barnacle") as pool:
        futures = {
            pool.submit(_run_unit, check, region, factory, config, prices, reference_time): (
                check,
                region,
            )
            for check, region in units
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            unit_findings, unit_error = future.result()
            findings.extend(unit_findings)
            if unit_error is not None:
                errors.append(unit_error)
            if on_progress is not None:
                on_progress(completed, total)

    findings = _apply_filters(findings, config)

    return ScanResult(
        findings=tuple(findings),
        errors=tuple(sorted(errors, key=lambda e: (e.check_id, e.region))),
        regions=tuple(regions),
        checks_run=tuple(c.id for c in checks),
        started_at=started,
        duration_seconds=round(time.perf_counter() - clock_start, 3),
        account_id=account_id,
        global_checks=frozenset(c.id for c in checks if c.scope is Scope.GLOBAL),
    )


def _run_unit(
    check_cls: type[Check],
    region: str,
    factory: ClientFactory,
    config: Config,
    prices: PriceBook,
    now: datetime,
) -> tuple[list[Finding], CheckError | None]:
    # DEBUG, not INFO: with --all-regions and several checks, this fires
    # dozens of times per second across threads. Its purpose is narrow --
    # --verbose shows exactly which units are still in flight if one is
    # slow, rather than a progress bar count that can't say which.
    log.debug("starting check %s in %s", check_cls.id, region)
    ctx = ScanContext(
        region=region,
        check_id=check_cls.id,
        service=check_cls.service,
        config=config,
        prices=prices,
        factory=factory,
        now=now,
    )
    reported_region = GLOBAL_REGION if check_cls.scope is Scope.GLOBAL else region

    try:
        instance = check_cls()
        findings = [_relabel(f, reported_region) for f in instance.run(ctx)]
    except ReadOnlyViolationError:
        # Never swallowed: this is our bug, not the user's environment.
        raise
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        log.debug("check %s failed in %s: %s", check_cls.id, region, message)
        return [], CheckError(check_cls.id, region, code, message)
    except BotoCoreError as exc:
        return [], CheckError(check_cls.id, region, type(exc).__name__, str(exc))
    except Exception as exc:
        log.exception("unexpected failure in check %s (%s)", check_cls.id, region)
        return [], CheckError(check_cls.id, region, "InternalError", f"{type(exc).__name__}: {exc}")

    return findings, None


def _relabel(finding: Finding, region: str) -> Finding:
    """Global checks report region 'global' regardless of the endpoint used."""
    if finding.region == region:
        return finding
    from dataclasses import replace

    return replace(finding, region=region)


def _apply_filters(findings: Iterable[Finding], config: Config) -> list[Finding]:
    """Apply ``--min-savings``.

    Unpriced findings are kept. Filtering them out would mean a threshold
    silently hides the things we know least about.
    """
    threshold = config.min_monthly_savings
    if threshold <= 0:
        return list(findings)
    return [
        f
        for f in findings
        if f.estimated_monthly_cost is None or f.estimated_monthly_cost >= threshold
    ]
