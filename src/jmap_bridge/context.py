"""Per-request context passed to every JMAP method handler: the
authenticated credentials, the loaded config, the shared connection pool,
and the derived JMAP accountId. Satisfies `dispatch.MethodContext`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jmap_bridge.auth import Credentials
from jmap_bridge.blob_cache import BlobCache
from jmap_bridge.config import BridgeConfig
from jmap_bridge.errors import AccountNotFound
from jmap_bridge.id_redirect import IdRedirectCache
from jmap_bridge.pool import ImapConnectionPool
from jmap_bridge.session import encode_account_id


@dataclass(frozen=True, slots=True)
class RequestContext:
    credentials: Credentials
    config: BridgeConfig
    pool: ImapConnectionPool
    blob_cache: BlobCache = field(default_factory=BlobCache)
    id_redirects: IdRedirectCache = field(default_factory=IdRedirectCache)

    @property
    def account_id(self) -> str:
        return encode_account_id(self.credentials.email)

    @property
    def id_redirect_key(self) -> tuple[str, str]:
        return (self.credentials.domain, self.credentials.email)

    def imap(self):
        """Async context manager yielding a pooled, authenticated
        `ImapConnection` for this request's credentials. Usage:
        `async with ctx.imap() as conn: ...`
        """
        imap_config = self.credentials.domain_config.imap
        domain_config = self.credentials.domain_config
        return self.pool.checkout(
            domain=self.credentials.domain,
            username=self.credentials.email,
            password=self.credentials.password,
            host=imap_config.host,
            port=imap_config.port,
            tls=imap_config.tls,
            max_per_user=domain_config.connection_pool_max_per_user,
            idle_eviction_seconds=domain_config.idle_eviction_seconds,
        )

    def require_account(self, account_id: str) -> None:
        """Raise AccountNotFound unless `account_id` is this request's own
        account - we have no shared/delegated accounts in the MVP, so any
        other id is simply unknown to us.
        """
        if account_id != self.account_id:
            raise AccountNotFound(f"unknown account {account_id!r}")
