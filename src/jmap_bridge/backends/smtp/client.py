"""SMTP submission for EmailSubmission/set (RFC 8621 SS7). Credential
passthrough: the same username/password used for IMAP is used to
authenticate to the backend SMTP server, per the config's assumption that
a domain's mail servers share one login.
"""

from __future__ import annotations

import aiosmtplib

from jmap_bridge.config import SmtpConfig


class SmtpError(Exception):
    """Wraps any aiosmtplib send failure."""


async def send_message(
    *,
    raw_message: bytes,
    sender: str,
    recipients: list[str],
    smtp_config: SmtpConfig,
    username: str,
    password: str,
) -> None:
    use_tls = smtp_config.tls == "implicit"
    start_tls = True if smtp_config.tls == "starttls" else (False if smtp_config.tls == "plain" else None)
    try:
        await aiosmtplib.send(
            raw_message,
            sender=sender,
            recipients=recipients,
            hostname=smtp_config.host,
            port=smtp_config.port,
            username=username,
            password=password,
            use_tls=use_tls,
            start_tls=start_tls,
        )
    except aiosmtplib.SMTPException as exc:
        raise SmtpError(f"SMTP submission to {smtp_config.host}:{smtp_config.port} failed: {exc}") from exc
