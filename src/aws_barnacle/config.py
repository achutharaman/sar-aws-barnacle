"""Configuration resolution.

Precedence, highest wins: CLI flags > environment (``SAR_AWS_BARNACLE_*``) > config file
> defaults. Per-check settings live in a nested table keyed by check id, so a
new check can add tunables without this module changing.

TOML via stdlib ``tomllib`` (3.11+), which is why there is no YAML dependency.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

CONFIG_FILENAMES = ("sar-aws-barnacle.toml", ".sar-aws-barnacle.toml")
ENV_PREFIX = "SAR_AWS_BARNACLE_"


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


class FailOn(StrEnum):
    """What makes ``sar-aws-barnacle scan`` exit non-zero."""

    NONE = "none"
    """Only infrastructure errors fail. Findings are informational."""

    ANY = "any"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExitCode:
    """Exit codes are an interface. Changing them is a breaking change."""

    CLEAN = 0
    FINDINGS = 1
    ERRORS = 2
    USAGE = 3


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "sar-aws-barnacle"


@dataclass(frozen=True, slots=True)
class Config:
    regions: tuple[str, ...] = ()
    all_regions: bool = False
    profile: str | None = None
    output: OutputFormat = OutputFormat.TABLE
    fail_on: FailOn = FailOn.NONE
    min_monthly_savings: Decimal = Decimal(0)
    live_pricing: bool = True
    refresh_prices: bool = False
    max_workers: int = 8
    cache_dir: Path = field(default_factory=_default_cache_dir)
    checks: tuple[str, ...] = ()
    options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def option(self, check_id: str, key: str, default: Any) -> Any:
        """Per-check tunable with a fallback.

        Checks call this instead of reading config attributes directly, which is
        what keeps ``Config`` from growing a field for every new check.
        """
        value = self.options.get(check_id, {}).get(key, default)
        if isinstance(default, int) and not isinstance(default, bool) and value is not None:
            return int(value)
        if isinstance(default, Decimal) and value is not None:
            return Decimal(str(value))
        return value

    def merged_with(self, **overrides: Any) -> Config:
        """Apply non-None overrides. Used to layer CLI flags on top."""
        clean = {k: v for k, v in overrides.items() if v is not None and v != ()}
        return replace(self, **clean)


def find_config_file(start: Path | None = None) -> Path | None:
    """Look for a config file in the given directory, then its parents."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(path: Path | None = None, *, search: bool = True) -> Config:
    """Build a Config from file plus environment. CLI flags are layered after."""
    resolved = path or (find_config_file() if search else None)
    data: dict[str, Any] = {}
    if resolved is not None:
        with resolved.open("rb") as fh:
            raw = tomllib.load(fh)
        data = raw.get("sar-aws-barnacle", raw)

    options = {
        str(k): dict(v) for k, v in (data.get("checks") or {}).items() if isinstance(v, dict)
    }

    cfg = Config(
        regions=tuple(data.get("regions", ())),
        all_regions=bool(data.get("all_regions", False)),
        profile=data.get("profile"),
        output=OutputFormat(data.get("output", OutputFormat.TABLE.value)),
        fail_on=FailOn(data.get("fail_on", FailOn.NONE.value)),
        min_monthly_savings=Decimal(str(data.get("min_monthly_savings", 0))),
        live_pricing=bool(data.get("live_pricing", True)),
        max_workers=int(data.get("max_workers", 8)),
        checks=tuple(data.get("checks_enabled", ())),
        options=options,
    )
    return _apply_env(cfg)


def _apply_env(cfg: Config) -> Config:
    env = os.environ
    updates: dict[str, Any] = {}
    if v := env.get(f"{ENV_PREFIX}PROFILE"):
        updates["profile"] = v
    if v := env.get(f"{ENV_PREFIX}REGIONS"):
        updates["regions"] = tuple(r.strip() for r in v.split(",") if r.strip())
    if v := env.get(f"{ENV_PREFIX}OUTPUT"):
        updates["output"] = OutputFormat(v)
    if v := env.get(f"{ENV_PREFIX}FAIL_ON"):
        updates["fail_on"] = FailOn(v)
    if v := env.get(f"{ENV_PREFIX}LIVE_PRICING"):
        updates["live_pricing"] = v.lower() not in ("0", "false", "no")
    return replace(cfg, **updates) if updates else cfg


def resolve_exit_code(
    *, finding_count: int, worst_severity_rank: int, has_errors: bool, fail_on: FailOn
) -> int:
    """Map a scan outcome onto an exit code.

    Errors always win: a partial scan that found nothing is not a clean account,
    it is an unanswered question, and CI should treat it that way.
    """
    if has_errors:
        return ExitCode.ERRORS
    if fail_on is FailOn.NONE or finding_count == 0:
        return ExitCode.CLEAN
    if fail_on is FailOn.ANY:
        return ExitCode.FINDINGS

    thresholds = {FailOn.LOW: 1, FailOn.MEDIUM: 2, FailOn.HIGH: 3}
    return ExitCode.FINDINGS if worst_severity_rank >= thresholds[fail_on] else ExitCode.CLEAN
