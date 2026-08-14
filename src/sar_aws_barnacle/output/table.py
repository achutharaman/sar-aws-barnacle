"""Human-readable output.

Costs carry a confidence marker rather than being printed as bare numbers,
because "$41.04" and "at most $41.04" are different claims and a cost report
loses its value the moment a reader catches it overstating one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TextIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from sar_aws_barnacle.config import Config
from sar_aws_barnacle.models import CostConfidence, ScanResult, Severity

SEVERITY_STYLE = {
    Severity.HIGH: "bold red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}

CONFIDENCE_MARK = {
    CostConfidence.EXACT: "",
    CostConfidence.LOWER_BOUND: "≥",
    CostConfidence.UPPER_BOUND: "≤",
    CostConfidence.UNKNOWN: "",
}


def format_cost(amount: Decimal | None, confidence: CostConfidence) -> Text:
    if amount is None:
        return Text("unknown", style="dim italic")
    mark = CONFIDENCE_MARK[confidence]
    style = "green" if confidence is CostConfidence.EXACT else "green dim"
    return Text(f"{mark}${amount:,.2f}", style=style)


class TableRenderer:
    """Default renderer: one row per finding, plus a summary."""

    name = "table"

    def __init__(self, *, price_source: str | None = None, width: int | None = None) -> None:
        self.price_source = price_source
        self.width = width

    def render(self, result: ScanResult, config: Config, stream: TextIO) -> None:
        console = Console(file=stream, width=self.width, highlight=False)
        findings = result.sorted_findings()

        header = f"sar-aws-barnacle scan · {len(result.regions)} region(s)"
        if result.account_id:
            header += f" · account {result.account_id}"
        console.print(Text(header, style="bold"))
        console.print()

        console.print(self._regions_table(result))
        console.print()

        if not findings:
            console.print(Text("No findings. Nothing obviously wasted.", style="bold green"))
        else:
            console.print(self._findings_table(findings, console.width))
            if console.width < self.WIDE_LAYOUT_COLUMNS:
                console.print(
                    Text(
                        "Narrow terminal: check id and full detail omitted. "
                        "Use --output json or widen the window.",
                        style="dim",
                    )
                )

        console.print()
        self._summary(console, result)

        if result.errors:
            console.print()
            console.print(self._errors_table(result))

    # Below this width there is not enough room for both the check id and a
    # useful Detail column. Rich would otherwise satisfy every no-wrap column
    # first and squeeze Detail down to a single character per line, which is
    # how a report ends up unreadable in exactly the 80-column terminal most
    # people run it in.
    WIDE_LAYOUT_COLUMNS = 110

    MIN_DETAIL_WIDTH = 20

    def _detail_budget(self, findings, width: int, *, show_check: bool) -> int:
        """Width left for Detail once every fixed column has what it needs.

        Rich's own allocator swings between two failure modes here: satisfy the
        no-wrap columns first and Detail collapses to one character per line, or
        let Detail bid for its full content length and the identifier columns
        get truncated instead. Measuring the fixed columns and handing Detail
        the remainder avoids both.
        """

        def col(header: str, values) -> int:
            return max(len(header), max((len(v) for v in values), default=0))

        fixed = (
            col("Severity", (f.severity.value.upper() for f in findings))
            + col("Region", (f.region for f in findings))
            + col("Resource", (f.resource_id for f in findings))
            + col(
                "Monthly",
                (format_cost(f.estimated_monthly_cost, f.cost_confidence).plain for f in findings),
            )
        )
        n_columns = 5
        if show_check:
            fixed += col("Check", (f.check_id for f in findings))
            n_columns += 1

        # Each column costs one padding space either side plus a border glyph.
        overhead = 3 * n_columns + 1
        return max(width - fixed - overhead, self.MIN_DETAIL_WIDTH)

    def _findings_table(self, findings, width: int) -> Table:
        show_check = width >= self.WIDE_LAYOUT_COLUMNS
        detail_width = self._detail_budget(findings, width, show_check=show_check)

        table = Table(show_lines=False, header_style="bold", expand=False, pad_edge=False)
        table.add_column("Severity", no_wrap=True)
        if show_check:
            table.add_column("Check", no_wrap=True)
        table.add_column("Region", no_wrap=True)
        table.add_column("Resource", no_wrap=True)
        table.add_column("Monthly", justify="right", no_wrap=True)
        # Truncate rather than fold: one line per finding keeps the report
        # scannable, and the full text is always in --output json.
        table.add_column("Detail", overflow="ellipsis", no_wrap=True, max_width=detail_width)

        for f in findings:
            row = [Text(f.severity.value.upper(), style=SEVERITY_STYLE[f.severity])]
            if show_check:
                row.append(f.check_id)
            row.extend(
                [
                    f.region,
                    f.resource_id,
                    format_cost(f.estimated_monthly_cost, f.cost_confidence),
                    f.detail,
                ]
            )
            table.add_row(*row)
        return table

    def _summary(self, console: Console, result: ScanResult) -> None:
        savings = result.estimated_monthly_savings
        console.print(
            Text.assemble(
                ("Estimated monthly waste: ", "bold"),
                (f"${savings:,.2f}", "bold green"),
                (f"  (${savings * 12:,.2f}/year)", "green dim"),
            )
        )
        console.print(
            Text(
                f"{len(result.findings)} finding(s) across {len(result.checks_run)} check(s) "
                f"in {result.duration_seconds:.2f}s",
                style="dim",
            )
        )
        if result.unpriced_count:
            console.print(
                Text(
                    f"{result.unpriced_count} finding(s) could not be priced and are "
                    f"excluded from the total.",
                    style="yellow",
                )
            )
        if self.price_source:
            console.print(Text(f"Prices: {self.price_source}", style="dim"))
        if result.is_partial:
            console.print(
                Text(
                    "Scan was partial - some checks failed, so real waste may be higher.",
                    style="bold yellow",
                )
            )

    def _regions_table(self, result: ScanResult) -> Table:
        """Every region considered gets a row -- scanned-and-clean regions
        included, not just the ones with findings -- so a reader can tell
        "checked, found nothing" apart from "never looked"."""
        table = Table(title="Regions", header_style="bold", title_justify="left")
        table.add_column("Region", no_wrap=True)
        table.add_column("Status")
        for region, status in result.region_status():
            if status.startswith("skipped"):
                style = "dim"
            elif "failed" in status:
                style = "yellow"
            elif status == "clean":
                style = "green"
            else:
                style = None
            table.add_row(region, Text(status, style=style) if style else Text(status))
        return table

    def _errors_table(self, result: ScanResult) -> Table:
        table = Table(title="Errors", header_style="bold yellow", title_justify="left")
        table.add_column("Check", no_wrap=True)
        table.add_column("Region", no_wrap=True)
        table.add_column("Code", no_wrap=True)
        table.add_column("Message", overflow="fold")
        for e in result.errors:
            table.add_row(e.check_id, e.region, e.error_code, e.message)
        return table
