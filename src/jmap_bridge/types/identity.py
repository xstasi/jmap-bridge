"""Identity/get (RFC 8621 SS6.1): a single identity derived from the
account's own credentials/config - not backend-stored, so there's nothing
to sync. `Identity/set` is intentionally not implemented for the MVP: with
one fixed identity per account and no storage, there is nothing meaningful
to change.
"""

from __future__ import annotations

from typing import Any

from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method


def default_identity(ctx: RequestContext) -> dict:
    email = ctx.credentials.email
    return {
        "id": "default",
        "name": email,
        "email": email,
        "replyTo": None,
        "bcc": None,
        "textSignature": "",
        "htmlSignature": "",
        "mayDelete": False,
    }


@method("Identity/get")
async def identity_get(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    account_id = args.get("accountId", ctx.account_id)
    ctx.require_account(account_id)
    ids = args.get("ids")

    identity = default_identity(ctx)
    all_identities = [identity]
    by_id = {i["id"]: i for i in all_identities}

    if ids is None:
        found = all_identities
        not_found: list[str] = []
    else:
        found = [by_id[i] for i in ids if i in by_id]
        not_found = [i for i in ids if i not in by_id]

    return {"accountId": account_id, "state": "static", "list": found, "notFound": not_found}
