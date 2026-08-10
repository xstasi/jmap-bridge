"""EmailSubmission/set (RFC 8621 SS7): sends a previously-imported/created
Email via the domain's configured SMTP server, using the same
credential-passthrough login as IMAP. Submit-only for the MVP - no local
storage means EmailSubmission objects aren't retrievable afterwards (no
EmailSubmission/get/query), and `maxDelayedSend: 0` in the session's
submission capability documents that scheduled/delayed send isn't
supported.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import time
from typing import Any

from jmap_bridge.backends.imap.client import ImapError
from jmap_bridge.backends.imap.modseq_state import decode_email_id
from jmap_bridge.backends.smtp.client import SmtpError, send_message
from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method
from jmap_bridge.errors import InvalidArguments, MethodError, ServerFail
from jmap_bridge.types.email import _apply_email_update
from jmap_bridge.types.identity import default_identity


async def _fetch_raw_message(conn, email_id: str) -> bytes:
    mailbox, uidvalidity, uid = decode_email_id(email_id)
    status = await conn.select(mailbox, readonly=True)
    if status.uidvalidity != uidvalidity:
        raise ValueError(f"stale email id {email_id!r}: mailbox UIDVALIDITY changed")
    fetched = await conn.fetch([uid], ["RFC822"])
    data = fetched.get(uid)
    if data is None or b"RFC822" not in data:
        raise ValueError(f"email id {email_id!r} not found")
    return data[b"RFC822"]


def _recipients_from_message(raw_message: bytes) -> list[str]:
    msg = email.message_from_bytes(raw_message, policy=email.policy.default)
    recipients: list[str] = []
    for header in ("To", "Cc", "Bcc"):
        for value in msg.get_all(header) or []:
            recipients.extend(addr for _name, addr in email.utils.getaddresses([value]) if addr)
    return recipients


@method("EmailSubmission/set")
async def email_submission_set(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    create = args.get("create") or {}
    on_success_update = args.get("onSuccessUpdateEmail") or {}

    created: dict[str, dict] = {}
    not_created: dict[str, dict] = {}

    identity = default_identity(ctx)

    try:
        async with ctx.imap() as conn:
            for creation_id, props in create.items():
                email_id = props.get("emailId")
                if not email_id:
                    not_created[creation_id] = InvalidArguments(
                        "emailId is required", arguments=["emailId"]
                    ).to_response()
                    continue
                try:
                    raw_message = await _fetch_raw_message(conn, email_id)
                except (ValueError, ImapError) as exc:
                    not_created[creation_id] = InvalidArguments(str(exc)).to_response()
                    continue

                envelope = props.get("envelope")
                if envelope:
                    mail_from = envelope["mailFrom"]["email"]
                    rcpt_to = [r["email"] for r in envelope["rcptTo"]]
                else:
                    mail_from = identity["email"]
                    rcpt_to = _recipients_from_message(raw_message)

                if not rcpt_to:
                    not_created[creation_id] = InvalidArguments(
                        "no recipients (no envelope and no To/Cc/Bcc headers)"
                    ).to_response()
                    continue

                try:
                    await send_message(
                        raw_message=raw_message,
                        sender=mail_from,
                        recipients=rcpt_to,
                        smtp_config=ctx.credentials.domain_config.smtp,
                        username=ctx.credentials.email,
                        password=ctx.credentials.password,
                    )
                except SmtpError as exc:
                    not_created[creation_id] = ServerFail(str(exc)).to_response()
                    continue

                created[creation_id] = {
                    "id": f"S{int(time.time() * 1000)}",
                    "emailId": email_id,
                    "identityId": props.get("identityId", identity["id"]),
                    "threadId": None,
                    "envelope": {
                        "mailFrom": {"email": mail_from, "parameters": None},
                        "rcptTo": [{"email": r, "parameters": None} for r in rcpt_to],
                    },
                    "sendAt": None,
                    "undoStatus": "final",
                    "deliveryStatus": None,
                    "dsnBlobIds": [],
                    "mdnBlobIds": [],
                }

                # RFC 8621 SS7.2.4: apply a patch to the Email the
                # submission was built from, "as though it was passed as
                # the corresponding update in an Email/set call within the
                # same API request". This is how aerc files a sent message
                # out of Drafts into Sent and clears $draft after send -
                # go-jmap's client keys the patch by "#<creationId>". The
                # submission already succeeded (mail is sent) by this
                # point, so a patch failure is reported nowhere else in
                # the response; best-effort, not fatal to the submission.
                patch = on_success_update.get(f"#{creation_id}")
                if patch:
                    try:
                        await _apply_email_update(ctx, conn, email_id, patch)
                    except (MethodError, ImapError):
                        pass
    except ImapError as exc:
        raise ServerFail(str(exc)) from exc

    return {
        "accountId": account_id,
        "oldState": None,
        "newState": "",
        "created": created,
        "notCreated": not_created,
        "updated": {},
        "notUpdated": {},
        "destroyed": [],
        "notDestroyed": {},
    }
