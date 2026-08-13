"""Renderer protocol and lookup.

Renderers receive a finished ScanResult and a stream. They never talk to AWS and
never decide what is a finding, which is what makes adding a new output format
(csv, sarif, html) a matter of adding one class here.
"""

from __future__ import annotations

from typing import Protocol, TextIO, runtime_checkable

from sar_aws_barnacle.config import Config, OutputFormat
from sar_aws_barnacle.models import ScanResult


@runtime_checkable
class Renderer(Protocol):
    name: str

    def render(self, result: ScanResult, config: Config, stream: TextIO) -> None: ...


def get_renderer(fmt: OutputFormat, *, price_source: str | None = None) -> Renderer:
    from sar_aws_barnacle.output.json_output import JsonRenderer
    from sar_aws_barnacle.output.table import TableRenderer

    if fmt is OutputFormat.JSON:
        return JsonRenderer(price_source=price_source)
    return TableRenderer(price_source=price_source)
