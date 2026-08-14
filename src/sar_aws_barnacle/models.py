"""Core data types shared by checks, the runner, and the renderers.

Everything here is frozen. Findings are values, not mutable state: once a check
emits one, nothing downstream may edit it. That is what lets the runner fan out
across threads without any locking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

SCHEMA_VERSION = "1.0"
GLOBAL_REGION = "global"


class Severity(StrEnum):
    """How much a finding should worry you."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    # StrEnum inherits str's __lt__/__le__/__gt__/__ge__, which compare
    # members alphabetically ("high" < "low") rather than by severity. Rather
    # than override just __lt__ (which would make the four operators disagree
    # with each other -- a worse trap than today's alphabetical-everywhere
    # behaviour), comparison is disabled outright: .rank is the one ordering
    # mechanism, and misuse fails loudly instead of returning a plausible but
    # wrong bool.
    def __lt__(self, other: object) -> bool:
        return NotImplemented

    __le__ = __gt__ = __ge__ = __lt__


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
}


class CostConfidence(StrEnum):
    """How much to trust ``Finding.estimated_monthly_cost``.

    This exists because AWS does not always let you know the true cost of a
    resource. EBS snapshots bill on *incremental* consumed storage, but the API
    only reports the size of the source volume -- so the honest answer is an
    upper bound, not a number. Reporting a fabricated exact figure is the
    fastest way to lose a reader's trust.
    """

    EXACT = "exact"
    """Price and quantity are both known. e.g. a 100 GiB gp3 volume."""

    LOWER_BOUND = "lower_bound"
    """Real cost is at least this. e.g. io2 storage, excluding provisioned IOPS."""

    UPPER_BOUND = "upper_bound"
    """Real cost is at most this. e.g. a snapshot priced at full source-volume size."""

    UNKNOWN = "unknown"
    """No price available. The finding still stands; the number does not."""


class Scope(StrEnum):
    """Whether a check runs per-region or exactly once per account.

    Without this, a six-region scan reports every stale IAM access key six
    times, because IAM is a global service with a regional-looking endpoint.
    """

    REGIONAL = "regional"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing worth a human's attention, in one account."""

    check_id: str
    resource_id: str
    resource_type: str
    region: str
    severity: Severity
    title: str
    detail: str
    estimated_monthly_cost: Decimal | None = None
    cost_confidence: CostConfidence = CostConfidence.UNKNOWN
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.estimated_monthly_cost is None:
            if self.cost_confidence is not CostConfidence.UNKNOWN:
                raise ValueError(
                    f"{self.check_id}/{self.resource_id}: cost is None but confidence is "
                    f"{self.cost_confidence.value!r}; use CostConfidence.UNKNOWN"
                )
        elif self.cost_confidence is CostConfidence.UNKNOWN:
            raise ValueError(
                f"{self.check_id}/{self.resource_id}: a cost was supplied without a "
                f"confidence level; say how much to trust it"
            )

        # frozen=True only blocks rebinding self.metadata, not mutating the dict
        # it points to. A read-only copy closes that gap so the runner's
        # lock-free fan-out (see the module docstring) can trust findings are
        # actually immutable once yielded.
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def sort_key(self) -> tuple[int, Decimal, str]:
        """Highest severity first, then biggest savings, then stable by id."""
        cost = self.estimated_monthly_cost or Decimal(0)
        return (-self.severity.rank, -cost, self.resource_id)


@dataclass(frozen=True, slots=True)
class CheckError:
    """A check that could not complete, in one region.

    Recorded rather than raised. A missing ``rds:DescribeDBSnapshots`` permission
    should degrade one check, not abort a scan that was about to report on five
    others.
    """

    check_id: str
    region: str
    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Everything one ``sar-aws-barnacle scan`` produced."""

    findings: tuple[Finding, ...]
    errors: tuple[CheckError, ...]
    regions: tuple[str, ...]
    checks_run: tuple[str, ...]
    started_at: datetime
    duration_seconds: float
    account_id: str | None = None
    # Regions the caller asked for (explicit -r, or a stale --all-regions
    # candidate) that were excluded before the scan started because they
    # are not enabled for this account. Skipped, not scanned -- no
    # CheckError, no wasted timeout+retry.
    skipped_regions: tuple[str, ...] = ()
    # Which of checks_run are Scope.GLOBAL. region_check_status() needs this
    # to know a global check reports exactly once against the synthetic
    # "global" region rather than once per real region -- a heuristic based
    # on where its findings landed can't tell "global, zero findings" apart
    # from "regional, never checked here".
    global_checks: frozenset[str] = field(default_factory=frozenset)

    @property
    def estimated_monthly_savings(self) -> Decimal:
        """Sum of costs we could actually price. Unknowns are excluded, not zeroed."""
        return sum(
            (f.estimated_monthly_cost for f in self.findings if f.estimated_monthly_cost),
            start=Decimal(0),
        )

    @property
    def unpriced_count(self) -> int:
        return sum(1 for f in self.findings if f.estimated_monthly_cost is None)

    @property
    def is_partial(self) -> bool:
        """True if any check failed, meaning the report may understate reality."""
        return bool(self.errors)

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.sort_key)

    def region_check_status(self) -> list[tuple[str, str | None, str]]:
        """(region, check_id, status) for every region x check combination
        considered -- scanned-and-clean included, not just the ones that
        happened to have findings -- so a report always accounts for
        everything it looked at, per check, not just an aggregate count.

        Derived entirely from checks_run, regions, findings, and errors: a
        new check needs zero changes here, or in either renderer, to show
        up in this breakdown -- the same "adding a check touches two files"
        guarantee this project makes for checks themselves extends to this
        report. See docs/DECISIONS.md §4.
        """
        finding_counts: dict[tuple[str, str], int] = {}
        for f in self.findings:
            key = (f.region, f.check_id)
            finding_counts[key] = finding_counts.get(key, 0) + 1
        errored: set[tuple[str, str]] = {(e.region, e.check_id) for e in self.errors}

        def status_for(region: str, check_id: str) -> str:
            count = finding_counts.get((region, check_id), 0)
            if (region, check_id) in errored:
                return f"{count} finding(s), check failed" if count else "check failed"
            return f"{count} finding(s)" if count else "clean"

        regional_checks = [c for c in self.checks_run if c not in self.global_checks]
        global_checks_run = [c for c in self.checks_run if c in self.global_checks]

        lines = []
        for region in self.regions:
            for check_id in regional_checks:
                lines.append((region, check_id, status_for(region, check_id)))
        # Reported once, not once per real region -- a global check runs
        # exactly once regardless of how many regions were scanned.
        for check_id in global_checks_run:
            lines.append((GLOBAL_REGION, check_id, status_for(GLOBAL_REGION, check_id)))
        for region in self.skipped_regions:
            lines.append((region, None, "skipped — not enabled for this account"))
        return lines
