"""AWS session handling and the read-only guarantee.

"Read-only by default" is a structural property here, not a README promise. A
botocore ``before-call`` hook inspects every operation about to go over the wire
and raises if it is not a read. A check that tried to delete something would
fail loudly in CI rather than quietly succeed against someone's production
account.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import boto3
from botocore.config import Config as BotoConfig

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

USER_AGENT_SUFFIX = "aws-barnacle"


class ReadOnlyViolationError(RuntimeError):
    """A check attempted a mutating AWS API call.

    This is always a bug in barnacle itself, never a user error, so the runner
    deliberately does *not* swallow it into a CheckError.
    """


def _guard_handler(model=None, **_kwargs) -> None:
    operation = getattr(model, "name", None)
    if operation is None:
        return
    if not operation.startswith(READ_ONLY_PREFIXES):
        raise ReadOnlyViolationError(
            f"blocked non-read-only AWS call: {operation!r}. barnacle never mutates "
            f"resources; allowed operation prefixes are {', '.join(READ_ONLY_PREFIXES)}"
        )


def install_read_only_guard(session: boto3.Session) -> None:
    """Register the guard on every client created from this session."""
    session.events.register("before-call", _guard_handler, unique_id="barnacle-read-only-guard")


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
    retries: int = 5
    _local: threading.local = field(default_factory=threading.local, repr=False)

    def _session(self) -> boto3.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = boto3.Session(profile_name=self.profile)
            if self.read_only:
                install_read_only_guard(session)
            self._local.session = session
            self._local.clients = {}
        return session

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
            resp = self.client("ec2", region_name="us-east-1").describe_regions()
            return sorted(r["RegionName"] for r in resp["Regions"])
        except Exception:  # noqa: BLE001 - fall back to the SDK's partition list
            return sorted(self._session().get_available_regions(service))

    def default_region(self) -> str | None:
        return self._session().region_name
