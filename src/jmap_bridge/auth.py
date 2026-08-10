"""HTTP Basic credential extraction, per the credential-passthrough auth
model: the client's Basic-auth username/password are exactly the
credentials we hand to the backend IMAP/CalDAV/CardDAV server (auth.py
never stores them beyond the current request; pool.py is the only place
they live briefly in memory).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from jmap_bridge.config import BridgeConfig, DomainConfig
from jmap_bridge.errors import Unauthorized


@dataclass(frozen=True, slots=True)
class Credentials:
    email: str
    password: str
    domain: str
    domain_config: DomainConfig


def parse_basic_auth_header(header_value: str | None) -> tuple[str, str] | None:
    if not header_value or not header_value.startswith("Basic "):
        return None
    encoded = header_value[len("Basic "):].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def authenticate_request(authorization_header: str | None, config: BridgeConfig) -> Credentials:
    """Extract and validate Basic-auth credentials, resolving the backend
    domain config from the username's email domain. Raises `Unauthorized`
    (mapped to HTTP 401) if the header is missing/malformed or the domain
    isn't configured - both cases get the same generic error, to avoid
    revealing which domains are and aren't served.
    """
    parsed = parse_basic_auth_header(authorization_header)
    if parsed is None:
        raise Unauthorized("missing or malformed Authorization header")
    username, password = parsed

    domain_config = config.get_domain_for_email(username)
    if domain_config is None:
        raise Unauthorized("no backend configured for this account")

    domain = config.domain_for_email(username)
    assert domain is not None  # guaranteed by get_domain_for_email returning non-None above
    return Credentials(email=username, password=password, domain=domain, domain_config=domain_config)
