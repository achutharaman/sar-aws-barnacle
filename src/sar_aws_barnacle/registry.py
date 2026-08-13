"""The check registry: the one seam that keeps adding a check from touching core.

A check is any class carrying the class-level metadata below and a ``run``
method. Registration is a decorator, discovery is automatic, and third parties
can ship checks from their own distributions via the ``sar_aws_barnacle.checks``
entry-point group without this repo knowing they exist.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from sar_aws_barnacle.models import Finding, Scope

if TYPE_CHECKING:
    from sar_aws_barnacle.context import ScanContext

CHECK_ENTRY_POINT_GROUP = "sar_aws_barnacle.checks"


@runtime_checkable
class Check(Protocol):
    """The contract every check implements.

    ``run`` takes only a :class:`~sar_aws_barnacle.context.ScanContext`. Checks never
    build boto3 clients, never read config files, and never know a renderer
    exists -- which is exactly why they are trivial to test.
    """

    id: ClassVar[str]
    """Stable machine-readable identifier, e.g. ``ebs-unattached``. Appears in
    JSON output and in ``--check`` selectors, so treat it as public API."""

    title: ClassVar[str]
    """Human-readable one-liner for table headings."""

    service: ClassVar[str]
    """boto3 service name used to build the client, e.g. ``ec2``."""

    scope: ClassVar[Scope]
    """REGIONAL (run once per selected region) or GLOBAL (run once per account)."""

    required_actions: ClassVar[frozenset[str]]
    """IAM actions this check calls, e.g. ``{"ec2:DescribeVolumes"}``.

    This is not documentation -- ``sar-aws-barnacle iam-policy`` unions these across the
    registry to *generate* the least-privilege policy, and CI asserts the
    committed ``docs/iam-policy.json`` still matches. The README's IAM policy
    therefore cannot drift from what the code actually calls."""

    def run(self, ctx: ScanContext) -> Iterable[Finding]:
        """Yield findings. Implementations should be generators."""
        ...


class DuplicateCheckError(ValueError):
    """Two checks claimed the same id."""


class InvalidCheckError(TypeError):
    """A check is missing required metadata."""


_REGISTRY: dict[str, type[Check]] = {}
_discovered = False

_REQUIRED_ATTRS = ("id", "title", "service", "scope", "required_actions")


def register(cls: type[Check]) -> type[Check]:
    """Class decorator that adds a check to the registry.

    Validation happens at import time, so a malformed check fails the test suite
    immediately rather than at 3am during a scan.
    """
    missing = [a for a in _REQUIRED_ATTRS if not getattr(cls, a, None)]
    if missing:
        raise InvalidCheckError(f"{cls.__name__} is missing required attribute(s): {missing}")
    if not callable(getattr(cls, "run", None)):
        raise InvalidCheckError(f"{cls.__name__} has no run() method")
    if not isinstance(cls.scope, Scope):
        raise InvalidCheckError(f"{cls.__name__}.scope must be a Scope, got {cls.scope!r}")
    if not isinstance(cls.required_actions, frozenset):
        raise InvalidCheckError(f"{cls.__name__}.required_actions must be a frozenset")
    for action in cls.required_actions:
        if ":" not in action:
            raise InvalidCheckError(
                f"{cls.__name__}.required_actions entry {action!r} is not a valid IAM action"
            )

    existing = _REGISTRY.get(cls.id)
    if existing is not None and existing is not cls:
        raise DuplicateCheckError(
            f"check id {cls.id!r} is claimed by both {existing.__name__} and {cls.__name__}"
        )
    _REGISTRY[cls.id] = cls
    return cls


def discover(*, include_plugins: bool = True, force: bool = False) -> None:
    """Import every module under ``sar_aws_barnacle.checks``, then load plugins.

    Idempotent. Dropping a new file into ``checks/`` is enough to register it --
    no import list to update, which is the whole point.
    """
    global _discovered
    if _discovered and not force:
        return

    from sar_aws_barnacle import checks as checks_pkg

    for mod in pkgutil.iter_modules(checks_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{checks_pkg.__name__}.{mod.name}")

    if include_plugins:
        _load_plugin_checks()

    _discovered = True


def _load_plugin_checks() -> None:
    """Load checks published by other distributions.

    A broken third-party plugin must not take down the tool, so failures here
    are downgraded to a warning rather than an exception.
    """
    from importlib.metadata import entry_points

    for ep in entry_points(group=CHECK_ENTRY_POINT_GROUP):
        try:
            loaded = ep.load()
        except Exception as exc:  # noqa: BLE001 - third-party code, never trusted
            import warnings

            warnings.warn(
                f"failed to load check plugin {ep.name!r}: {exc}", RuntimeWarning, stacklevel=2
            )
            continue
        if isinstance(loaded, type):
            register(loaded)


def all_checks() -> list[type[Check]]:
    """Every registered check, in stable id order."""
    discover()
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def get_check(check_id: str) -> type[Check]:
    discover()
    try:
        return _REGISTRY[check_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(f"unknown check {check_id!r}; registered checks: {known}") from None


def select_checks(ids: Iterable[str] | None = None) -> list[type[Check]]:
    """Resolve a ``--check`` selection, or return everything if none given."""
    if not ids:
        return all_checks()
    return [get_check(i) for i in dict.fromkeys(ids)]


def iter_registered() -> Iterator[tuple[str, type[Check]]]:
    discover()
    yield from sorted(_REGISTRY.items())


def _reset_for_tests() -> None:
    """Clear registry state. Tests only."""
    global _discovered
    _REGISTRY.clear()
    _discovered = False
