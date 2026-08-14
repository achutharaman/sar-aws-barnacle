"""Command-line interface.

Deliberately thin. Every command here parses flags, builds a Config, calls into
the library, and renders. No detection logic lives in this file, which is what
lets the package be imported and driven from a Lambda or a notebook without
Typer being involved at all.
"""

from __future__ import annotations

import json as _json
import logging
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from sar_aws_barnacle import __version__
from sar_aws_barnacle.config import (
    Config,
    ExitCode,
    FailOn,
    OutputFormat,
    load_config,
    resolve_exit_code,
)
from sar_aws_barnacle.iam import build_policy
from sar_aws_barnacle.models import ScanResult
from sar_aws_barnacle.output.base import get_renderer
from sar_aws_barnacle.pricing.api import PricingApiPriceBook
from sar_aws_barnacle.pricing.base import PriceBook
from sar_aws_barnacle.pricing.static import StaticPriceBook
from sar_aws_barnacle.registry import Check, all_checks, select_checks
from sar_aws_barnacle.runner import plan_units, run_scan
from sar_aws_barnacle.session import ClientFactory

app = typer.Typer(
    name="sar-aws-barnacle",
    help="Read-only AWS cost-waste and hygiene scanner. Reports; never deletes.",
    no_args_is_help=True,
    add_completion=False,
)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _resolve_regions(config: Config, factory: ClientFactory) -> tuple[list[str], list[str]]:
    """Returns (regions to scan, regions skipped as not enabled for this account).

    --all-regions already comes from the account's real enabled-region list,
    so nothing is ever skipped there. Explicit -r is cross-checked against
    that same list first -- scanning a disabled region can't find anything,
    it can only burn a timeout+retry and clutter the errors table.
    """
    if config.all_regions:
        return factory.available_regions(), []
    if config.regions:
        requested = list(config.regions)
        enabled = set(factory.available_regions())
        to_scan = [r for r in requested if r in enabled]
        skipped = [r for r in requested if r not in enabled]
        if not to_scan:
            raise typer.BadParameter(
                f"none of the requested regions are enabled for this account: "
                f"{', '.join(requested)}"
            )
        return to_scan, skipped
    if default := factory.default_region():
        return [default], []
    raise typer.BadParameter(
        "no region specified and no default found. Pass --region, or --all-regions, "
        "or set AWS_REGION / a region in your AWS profile."
    )


def _build_pricebook(config: Config, factory: ClientFactory) -> PriceBook:
    if not config.live_pricing:
        return StaticPriceBook()
    return PricingApiPriceBook(
        factory,
        cache_dir=config.cache_dir,
        refresh=config.refresh_prices,
        fallback=StaticPriceBook(),
    )


def _run_scan_with_progress(
    checks: list[type[Check]],
    regions: list[str],
    *,
    factory: ClientFactory,
    config: Config,
    prices: PriceBook,
    account_id: str | None,
) -> ScanResult:
    """Runs the scan, showing a progress bar on stderr when there's a
    terminal to draw one on.

    stderr, not stdout: ``--output json`` gets piped (``> findings.json``),
    and a progress bar interleaved into that stream would corrupt it. A
    non-interactive stderr (CI logs, redirected to a file) would otherwise
    get one line per refresh, so the bar is skipped there too rather than
    spammed.
    """
    from rich.console import Console
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    console = Console(stderr=True)
    if not console.is_terminal:
        return run_scan(
            checks, regions, factory=factory, config=config, prices=prices, account_id=account_id
        )

    total = len(plan_units(checks, regions))
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning", total=total)
        return run_scan(
            checks,
            regions,
            factory=factory,
            config=config,
            prices=prices,
            account_id=account_id,
            on_progress=lambda done, _total: progress.update(task, completed=done),
        )


@app.command()
def scan(
    region: Annotated[
        list[str] | None,
        typer.Option("--region", "-r", help="Region to scan. Repeatable."),
    ] = None,
    all_regions: Annotated[
        bool, typer.Option("--all-regions", help="Scan every region enabled on the account.")
    ] = False,
    profile: Annotated[str | None, typer.Option("--profile", help="AWS profile name.")] = None,
    check: Annotated[
        list[str] | None,
        typer.Option("--check", "-c", help="Run only these check ids. Repeatable."),
    ] = None,
    output: Annotated[
        OutputFormat | None,
        typer.Option("--output", "-o", help="Output format. [default: table]"),
    ] = None,
    fail_on: Annotated[
        FailOn | None,
        typer.Option(
            "--fail-on",
            help="Minimum severity that makes the command exit 1. [default: none]",
        ),
    ] = None,
    min_savings: Annotated[
        float, typer.Option("--min-savings", help="Hide findings cheaper than this per month.")
    ] = 0.0,
    live_pricing: Annotated[
        bool | None,
        typer.Option(
            "--live-pricing/--no-live-pricing",
            help="Query the AWS Pricing API (cached), or use the bundled table only.",
        ),
    ] = None,
    refresh_prices: Annotated[
        bool, typer.Option("--refresh-prices", help="Ignore the price cache and refetch.")
    ] = False,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="Path to sar-aws-barnacle.toml.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Scan an AWS account for cost waste and hygiene issues."""
    _configure_logging(verbose)

    config = load_config(config_file).merged_with(
        regions=tuple(region or ()),
        all_regions=all_regions or None,
        profile=profile,
        output=output,
        fail_on=fail_on,
        min_monthly_savings=Decimal(str(min_savings)) if min_savings else None,
        live_pricing=live_pricing,
        refresh_prices=refresh_prices or None,
        checks=tuple(check or ()),
    )

    factory = ClientFactory(profile=config.profile)
    regions, skipped_regions = _resolve_regions(config, factory)
    checks = select_checks(config.checks)
    prices = _build_pricebook(config, factory)

    result = _run_scan_with_progress(
        checks,
        regions,
        factory=factory,
        config=config,
        prices=prices,
        account_id=factory.account_id(),
    )
    if skipped_regions:
        result = replace(result, skipped_regions=tuple(skipped_regions))

    renderer = get_renderer(config.output, price_source=prices.source)
    renderer.render(result, config, sys.stdout)

    worst = max((f.severity.rank for f in result.findings), default=0)
    raise typer.Exit(
        resolve_exit_code(
            finding_count=len(result.findings),
            worst_severity_rank=worst,
            has_errors=result.is_partial,
            fail_on=config.fail_on,
        )
    )


@app.command(name="checks")
def list_checks(
    output: Annotated[OutputFormat, typer.Option("--output", "-o")] = OutputFormat.TABLE,
) -> None:
    """List every registered check, including plugins."""
    registered = all_checks()
    if output is OutputFormat.JSON:
        payload = [
            {
                "id": c.id,
                "title": c.title,
                "service": c.service,
                "scope": c.scope.value,
                "required_actions": sorted(c.required_actions),
            }
            for c in registered
        ]
        typer.echo(_json.dumps(payload, indent=2))
        raise typer.Exit(ExitCode.CLEAN)

    from rich.console import Console
    from rich.table import Table

    table = Table(header_style="bold")
    table.add_column("ID", no_wrap=True)
    table.add_column("Title")
    table.add_column("Service", no_wrap=True)
    table.add_column("Scope", no_wrap=True)
    table.add_column("IAM actions")
    for c in registered:
        table.add_row(
            c.id, c.title, c.service, c.scope.value, "\n".join(sorted(c.required_actions))
        )
    Console().print(table)


@app.command(name="iam-policy")
def iam_policy(
    check: Annotated[
        list[str] | None,
        typer.Option("--check", "-c", help="Restrict the policy to these checks."),
    ] = None,
) -> None:
    """Print the least-privilege IAM policy required to run these checks.

    Generated from the registry, so it is always exactly what the code calls.
    """
    typer.echo(_json.dumps(build_policy(select_checks(check or ())), indent=2))


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover
    app()
