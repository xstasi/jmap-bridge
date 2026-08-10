"""JMAP Session object (RFC 8620 SS2): `GET /.well-known/jmap` redirects to
`GET /session`, which returns the capabilities/accounts/URLs a client needs
to start making API calls.

Since credential-passthrough auth means one HTTP request always maps to
exactly one backend account (no delegated/shared accounts in this MVP),
the session is built fresh per request from the authenticated
`Credentials` - there is nothing to look up, only to encode.
"""

from __future__ import annotations

import base64
import hashlib

from jmap_bridge.auth import Credentials

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"
SUBMISSION_CAPABILITY = "urn:ietf:params:jmap:submission"
CALENDARS_CAPABILITY = "urn:ietf:params:jmap:calendars"
CONTACTS_CAPABILITY = "urn:ietf:params:jmap:contacts"

MAX_SIZE_UPLOAD = 50_000_000
MAX_SIZE_REQUEST = 10_000_000
MAX_CALLS_IN_REQUEST = 32
MAX_OBJECTS_IN_GET = 500
MAX_OBJECTS_IN_SET = 500


def encode_account_id(email: str) -> str:
    return "A" + base64.urlsafe_b64encode(email.encode("utf-8")).decode("ascii").rstrip("=")


def decode_account_id(account_id: str) -> str:
    if not account_id.startswith("A"):
        raise ValueError(f"not an Account id: {account_id!r}")
    padded = account_id[1:]
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"malformed Account id {account_id!r}: {exc}") from exc


def build_capabilities(credentials: Credentials) -> dict:
    capabilities: dict = {
        CORE_CAPABILITY: {
            "maxSizeUpload": MAX_SIZE_UPLOAD,
            "maxConcurrentUpload": 4,
            "maxSizeRequest": MAX_SIZE_REQUEST,
            "maxConcurrentRequests": 4,
            "maxCallsInRequest": MAX_CALLS_IN_REQUEST,
            "maxObjectsInGet": MAX_OBJECTS_IN_GET,
            "maxObjectsInSet": MAX_OBJECTS_IN_SET,
            "collationAlgorithms": ["i;ascii-casemap", "i;unicode-casemap"],
        },
        MAIL_CAPABILITY: {
            "maxMailboxesPerEmail": 1,  # multi-mailbox Email is modeled via IMAP COPY, see plan
            "maxMailboxDepth": None,
            "maxSizeMailboxName": 490,
            "maxSizeAttachmentsPerEmail": 50_000_000,
            "emailQuerySortOptions": ["receivedAt", "subject", "size"],
            "mayCreateTopLevelMailbox": True,
        },
        SUBMISSION_CAPABILITY: {
            "maxDelayedSend": 0,
            "submissionExtensions": {},
        },
    }
    if credentials.domain_config.caldav is not None:
        capabilities[CALENDARS_CAPABILITY] = {
            "minDateTime": "0001-01-01T00:00:00Z",
            "maxDateTime": "9999-12-31T23:59:59Z",
            "maxExpandedQueryDuration": "P1Y",
            "maxParticipantsPerEvent": None,
            "mayCreateCalendar": True,
        }
    if credentials.domain_config.carddav is not None:
        capabilities[CONTACTS_CAPABILITY] = {
            "mayCreateAddressBook": True,
        }
    return capabilities


def build_session(base_url: str, credentials: Credentials) -> dict:
    account_id = encode_account_id(credentials.email)
    capabilities = build_capabilities(credentials)

    account = {
        "name": credentials.email,
        "isPersonal": True,
        "isReadOnly": False,
        "accountCapabilities": capabilities,
    }
    primary_accounts = {urn: account_id for urn in capabilities}

    state_input = f"{account_id}:{','.join(sorted(capabilities))}"
    state = hashlib.sha256(state_input.encode("utf-8")).hexdigest()[:16]

    base_url = base_url.rstrip("/")
    return {
        "capabilities": capabilities,
        "accounts": {account_id: account},
        "primaryAccounts": primary_accounts,
        "username": credentials.email,
        "apiUrl": f"{base_url}/api",
        "downloadUrl": f"{base_url}/download/{{accountId}}/{{blobId}}/{{name}}?type={{type}}",
        "uploadUrl": f"{base_url}/upload/{{accountId}}",
        "eventSourceUrl": f"{base_url}/events?types={{types}}&closeafter={{closeafter}}&ping={{ping}}",
        "state": state,
    }
