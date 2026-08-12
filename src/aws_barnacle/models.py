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

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank


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
    """Everything one ``barnacle scan`` produced."""

    findings: tuple[Finding, ...]
    errors: tuple[CheckError, ...]
    regions: tuple[str, ...]
    checks_run: tuple[str, ...]
    started_at: datetime
    duration_seconds: float
    account_id: str | None = None

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
