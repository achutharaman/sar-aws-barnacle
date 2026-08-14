"""AWS session handling and the read-only guarantee.

"Read-only by default" is a structural property here, not a README promise. A
botocore ``before-call`` hook inspects every operation about to go over the wire
and raises if it is not a read. A check that tried to delete something would
fail loudly in CI rather than quietly succeed against someone's production
account.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import boto3
from botocore.config import Config as BotoConfig

log = logging.getLogger(__name__)

# Operation-name prefixes that cannot mutate state. Deliberately conservative:
# a check needing something outside this set is a design conversation, not a
# one-line addition.
READ_ONLY_PREFIXES: tuple[str, ...] = (
    "Describe",
    "Get",
    "List",
    "BatchGet",
    "Lookup",
    "Head",
)

USER_AGENT_SUFFIX = "sar-aws-barnacle"

# The user/profile name the README's "Set up read-only access" walkthrough
# tells readers to create. Used as a fallback only -- never forced -- so
# running the CLI with no --profile at all "just works" for anyone who
# followed that walkthrough verbatim, without changing behaviour for anyone
# who didn't (default credentials, an instance role, CI OIDC, ...).
DEFAULT_SCANNER_PROFILE = "sar-aws-barnacle-scanner"


class ReadOnlyViolationError(RuntimeError):
    """A check attempted a mutating AWS API call.

    This is always a bug in sar-aws-barnacle itself, never a user error, so the
    runner deliberately does *not* swallow it into a CheckError.
    """


def _guard_handler(model=None, **_kwargs) -> None:
    operation = getattr(model, "name", None)
    if operation is None:
        return
    if not operation.startswith(READ_ONLY_PREFIXES):
        raise ReadOnlyViolationError(
            f"blocked non-read-only AWS call: {operation!r}. sar-aws-barnacle never mutates "
            f"resources; allowed operation prefixes are {', '.join(READ_ONLY_PREFIXES)}"
        )


def install_read_only_guard(session: boto3.Session) -> None:
    """Register the guard on every client created from this session."""
    session.events.register(
        "before-call", _guard_handler, unique_id="sar-aws-barnacle-read-only-guard"
    )


@dataclass
class ClientFactory:
    """Builds cached boto3 clients, one set per thread.

    boto3 ``Session`` objects are not safe to share across threads for client
    creation, and the runner fans out over (check, region) pairs. Keeping a
    session in thread-local storage is the cheap, correct fix -- far simpler
    than serialising client creation behind a lock.
    """

    profile: str | None = None
    read_only: bool = True
    # NB: botocore's Config(retries={"max_attempts": N}) means N *retries*,
    # not N total attempts -- it adds 1 internally to get the real
    # total_max_attempts (see botocore/args.py, _compute_retry_max_attempts).
    # So retries=1 here is 1 retry, 2 total attempts: a single unreachable or
    # throttled region in --all-regions should fail fast and move on. A scan
    # is interactive and synchronous, so resilience against a transient blip
    # costs every other region's wait time too. One retry still absorbs a
    # genuine blip; more attempts at a dead endpoint were never going to
    # succeed.
    retries: int = 1
    # botocore's own defaults are 60s connect + 60s read, which -- even at
    # 2 total attempts -- could still take minutes on a hard-down endpoint.
    # This was found via a real --all-regions run that looked hung at
    # "33/34"; see docs/DECISIONS.md §9.
    connect_timeout: int = 10
    read_timeout: int = 30
    _local: threading.local = field(default_factory=threading.local, repr=False)

    def _session(self) -> boto3.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = boto3.Session(profile_name=self.profile or self._default_profile())
            if self.read_only:
                install_read_only_guard(session)
            self._local.session = session
            self._local.clients = {}
        return session

    @staticmethod
    def _default_profile() -> str | None:
        """The scanner profile, but only if it's actually configured.

        boto3.Session(profile_name=...) raises ProfileNotFound immediately if
        the name doesn't exist, so this must confirm presence first -- passing
        DEFAULT_SCANNER_PROFILE unconditionally would break every user who
        didn't create it (default credentials, an instance role, CI OIDC).
        """
        if DEFAULT_SCANNER_PROFILE in boto3.Session().available_profiles:
            return DEFAULT_SCANNER_PROFILE
        return None

    def client(self, service: str, region: str | None = None):
        session = self._session()
        cache: dict[tuple[str, str | None], object] = self._local.clients
        key = (service, region)
        if key not in cache:
            cache[key] = session.client(
                service,
                region_name=region,
                config=BotoConfig(
                    retries={"max_attempts": self.retries, "mode": "standard"},
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                    user_agent_extra=USER_AGENT_SUFFIX,
                ),
            )
        return cache[key]

    def account_id(self) -> str | None:
        """Best-effort account id for the report header. Never fatal."""
        try:
            return self.client("sts").get_caller_identity()["Account"]
        except Exception:  # noqa: BLE001 - a missing sts:GetCallerIdentity is not fatal
            return None

    def available_regions(self, service: str = "ec2") -> list[str]:
        """Regions actually enabled for this account, not the static SDK list."""
        try:
            resp = self.client("ec2", region="us-east-1").describe_regions()
            return sorted(r["RegionName"] for r in resp["Regions"])
        except Exception as exc:  # noqa: BLE001 - fall back to the SDK's partition list
            # This fallback is worse than it looks: the static list is every
            # region botocore has ever heard of, including opt-in regions
            # (me-south-1, af-south-1, ...) this account has almost
            # certainly never enabled. A silent fallback here means
            # --all-regions quietly scans ~2x the real region count and
            # burns a timeout+retry on each one that doesn't apply. Loud by
            # default (not --verbose-gated), because it changes scan scope.
            log.warning(
                "ec2:DescribeRegions failed (%s: %s); falling back to the full SDK region "
                "list for %r, which includes regions this account may not have enabled",
                type(exc).__name__,
                exc,
                service,
            )
            return sorted(self._session().get_available_regions(service))

    def default_region(self) -> str | None:
        return self._session().region_name
