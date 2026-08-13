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
from sar_aws_barnacle.output.base import get_renderer
from sar_aws_barnacle.pricing.api import PricingApiPriceBook
from sar_aws_barnacle.pricing.base import PriceBook
from sar_aws_barnacle.pricing.static import StaticPriceBook
from sar_aws_barnacle.registry import all_checks, select_checks
from sar_aws_barnacle.runner import run_scan
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


def _resolve_regions(config: Config, factory: ClientFactory) -> list[str]:
    if config.all_regions:
        return factory.available_regions()
    if config.regions:
        return list(config.regions)
    if default := factory.default_region():
        return [default]
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
    regions = _resolve_regions(config, factory)
    checks = select_checks(config.checks)
    prices = _build_pricebook(config, factory)

    result = run_scan(
        checks,
        regions,
        factory=factory,
        config=config,
        prices=prices,
        account_id=factory.account_id(),
    )

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
