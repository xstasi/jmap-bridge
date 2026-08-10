"""Domain -> backend-server configuration.

One YAML file maps each served email domain to its backend IMAP server
(required), SMTP server (required for EmailSubmission), and optional
CalDAV/CardDAV servers. A domain's `caldav`/`carddav` presence directly
drives whether the JMAP Session object advertises those capabilities for
accounts in that domain (see session.py).

Malformed config fails startup loudly (`load_config` raises) rather than
degrading silently — every other module assumes a validated config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class ConfigError(Exception):
    """Raised when the domain config file is missing or fails validation."""


class ImapConfig(BaseModel):
    host: str
    port: int = 993
    tls: Literal["implicit", "starttls", "plain"] = "implicit"


class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    tls: Literal["implicit", "starttls", "plain"] = "starttls"


class CaldavConfig(BaseModel):
    url: str


class CarddavConfig(BaseModel):
    url: str


class DomainConfig(BaseModel):
    imap: ImapConfig
    smtp: SmtpConfig
    caldav: CaldavConfig | None = None
    carddav: CarddavConfig | None = None
    connection_pool_max_per_user: int = 4
    idle_eviction_seconds: int = 300


class BridgeConfig(BaseModel):
    domains: dict[str, DomainConfig] = Field(default_factory=dict)

    def domain_for_email(self, email: str) -> str | None:
        """Extract the domain from an email-shaped username (the MVP
        assumption is IMAP username == JMAP email address, so this is also
        the IMAP login). Returns None if `email` isn't email-shaped.
        """
        if "@" not in email:
            return None
        return email.rsplit("@", 1)[1].lower()

    def get_domain(self, domain: str) -> DomainConfig | None:
        return self.domains.get(domain.lower())

    def get_domain_for_email(self, email: str) -> DomainConfig | None:
        domain = self.domain_for_email(email)
        if domain is None:
            return None
        return self.get_domain(domain)


def load_config(path: str | Path) -> BridgeConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    try:
        return BridgeConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}:\n{exc}") from exc
