import email
import email.policy
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from imapclient.response_types import BodyData

from jmap_bridge.backends.imap.client import MailboxStatus
from jmap_bridge.backends.imap.mailbox_map import encode_mailbox_id
from jmap_bridge.backends.imap.modseq_state import encode_email_id
from jmap_bridge.blob_cache import BlobCache
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments
from jmap_bridge.id_redirect import IdRedirectCache
from jmap_bridge.types import email as email_types

MSG1 = b"""\
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Subject: First message
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <msg1@example.com>
Content-Type: text/plain; charset=utf-8

Hello Bob.
"""

MSG2 = b"""\
From: Bob <bob@example.com>
To: Alice <alice@example.com>
Subject: Re: First message
Date: Mon, 1 Jan 2024 13:00:00 +0000
Message-Id: <msg2@example.com>
In-Reply-To: <msg1@example.com>
References: <msg1@example.com>
Content-Type: text/plain; charset=utf-8

Hi Alice.
"""


def _body_text_for_search(parsed) -> str:
    body_part = parsed.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return ""
    try:
        return body_part.get_content()
    except Exception:
        return ""


def _rfc822_header_bytes(raw: bytes) -> bytes:
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = raw.find(sep)
        if idx != -1:
            return raw[: idx + len(sep)]
    return raw


def _synth_bodystructure_tuple(part):
    """A minimal RFC 3501-shaped BODYSTRUCTURE synthesized from a parsed
    part, standing in for what a real IMAP server would send - see
    email_map.py's `_walk_native_bodystructure` for the real grammar this
    mirrors. Only needs to support what this test suite's fixture messages
    actually use (simple single-part text/plain, and multipart via
    Email/set assertions on the written raw bytes rather than via
    Email/get) - not full RFC 3501 fidelity.
    """
    if part.is_multipart():
        children = [_synth_bodystructure_tuple(sub) for sub in part.get_payload()]
        return (children, part.get_content_subtype().encode(), None, None, None, None)
    payload = part.get_payload(decode=True) or b""
    maintype = part.get_content_maintype().encode()
    subtype = part.get_content_subtype().encode()
    charset = part.get_content_charset()
    params = (b"charset", charset.encode()) if charset else None
    encoding = (part.get("Content-Transfer-Encoding") or "7bit").encode()
    size = len(payload)
    if maintype == b"text":
        lines = payload.count(b"\n") + (1 if payload and not payload.endswith(b"\n") else 0)
        return (maintype, subtype, params, None, None, encoding, size, lines, None, None, None, None)
    return (maintype, subtype, params, None, None, encoding, size, None, None, None, None)


def _synth_bodystructure(raw: bytes) -> BodyData:
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    return BodyData.create(_synth_bodystructure_tuple(parsed))


class FakeConn:
    def __init__(self, mailboxes=None):
        # name -> {uidvalidity, highestmodseq, next_uid, messages: {uid: {raw, flags, internaldate}}}
        self._mailboxes = mailboxes or {}
        self._selected = None

    def add_mailbox(self, name, uidvalidity=1, highestmodseq=1):
        self._mailboxes.setdefault(
            name, {"uidvalidity": uidvalidity, "highestmodseq": highestmodseq, "next_uid": 1, "messages": {}}
        )

    def add_message(self, mailbox, raw, flags=(), internaldate=None):
        mb = self._mailboxes[mailbox]
        uid = mb["next_uid"]
        mb["next_uid"] += 1
        mb["highestmodseq"] += 1  # CONDSTORE: creation bumps modseq too
        mb["messages"][uid] = {
            "raw": raw,
            "flags": set(flags),
            "internaldate": internaldate or datetime(2024, 1, 1, tzinfo=timezone.utc),
            "modseq": mb["highestmodseq"],
        }
        return uid

    async def list_mailboxes(self):
        return [(frozenset(), "/", name) for name in self._mailboxes]

    async def status(self, mailbox):
        mb = self._mailboxes[mailbox]
        unseen = sum(1 for m in mb["messages"].values() if "\\Seen" not in m["flags"])
        return MailboxStatus(
            uidvalidity=mb["uidvalidity"], highestmodseq=mb["highestmodseq"],
            uidnext=mb["next_uid"], exists=len(mb["messages"]), unseen=unseen,
        )

    async def select(self, mailbox, readonly=True):
        self._selected = mailbox
        return await self.status(mailbox)

    def _matches(self, uid, msg, tokens):
        """Minimal interpreter for the IMAP SEARCH criteria our code
        actually generates, enough to exercise real filtering behavior
        instead of a fake that ignores criteria entirely.
        """
        import email as email_module
        import email.policy

        parsed = email_module.message_from_bytes(msg["raw"], policy=email.policy.default)
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "ALL":
                i += 1
            elif token == "OR":
                left, right = tokens[i + 1], tokens[i + 2]
                if not (self._eval_operand(uid, msg, parsed, left) or self._eval_operand(uid, msg, parsed, right)):
                    return False
                i += 3
            elif token == "NOT":
                if self._eval_operand(uid, msg, parsed, tokens[i + 1]):
                    return False
                i += 2
            else:
                ok, consumed = self._match_one(uid, msg, parsed, tokens[i:])
                if not ok:
                    return False
                i += consumed
        return True

    def _eval_operand(self, uid, msg, parsed, operand):
        """An OR/NOT operand is always a nested list (a grouped
        sub-expression) coming from our real translation code - never a
        bare scalar - so this just recurses into _matches.
        """
        return self._matches(uid, msg, operand)

    def _match_one(self, uid, msg, parsed, tokens):
        key = tokens[0]
        if key in ("SEEN", "FLAGGED", "ANSWERED", "DRAFT", "DELETED"):
            flag = "\\" + key.capitalize()
            return (flag in msg["flags"]), 1
        if key in ("KEYWORD", "UNKEYWORD"):
            flag = tokens[1]
            present = flag in msg["flags"]
            return (present if key == "KEYWORD" else not present), 2
        if key in ("SUBJECT", "TEXT", "BODY", "FROM", "TO", "CC", "BCC"):
            value = tokens[1].lower()
            haystack = {
                "SUBJECT": parsed.get("Subject", ""),
                "TEXT": msg["raw"].decode("utf-8", "replace"),
                "BODY": _body_text_for_search(parsed),
                "FROM": parsed.get("From", ""),
                "TO": parsed.get("To", ""),
                "CC": parsed.get("Cc", ""),
                "BCC": parsed.get("Bcc", ""),
            }[key].lower()
            return (value in haystack), 2
        if key == "HEADER":
            name, value = tokens[1], tokens[2]
            header_value = parsed.get(name, "")
            return (value.lower() in header_value.lower()), 3
        if key in ("BEFORE", "SINCE"):
            from datetime import datetime

            date = datetime.strptime(tokens[1], "%d-%b-%Y").replace(tzinfo=msg["internaldate"].tzinfo)
            if key == "BEFORE":
                return (msg["internaldate"].date() < date.date()), 2
            return (msg["internaldate"].date() >= date.date()), 2
        if key == "UID":
            lo_str, _, hi_str = tokens[1].partition(":")
            lo = int(lo_str)
            hi = float("inf") if hi_str in ("", "*") else int(hi_str)
            return (lo <= uid <= hi), 2
        raise AssertionError(f"FakeConn._match_one: unhandled criteria token {key!r}")

    async def search(self, criteria="ALL"):
        tokens = criteria if isinstance(criteria, list) else [criteria]
        mb = self._mailboxes[self._selected]
        return sorted(uid for uid, msg in mb["messages"].items() if self._matches(uid, msg, tokens))

    async def sort(self, sort_criteria, criteria="ALL"):
        import email as email_module
        import email.policy

        uids = await self.search(criteria)
        mb = self._mailboxes[self._selected]

        reverse = False
        sort_keys = []
        for token in sort_criteria:
            if token == "REVERSE":
                reverse = True
                continue
            sort_keys.append((token, reverse))
            reverse = False

        def key_value(uid, imap_key):
            msg = mb["messages"][uid]
            if imap_key == "ARRIVAL":
                return msg["internaldate"]
            parsed = email_module.message_from_bytes(msg["raw"], policy=email.policy.default)
            if imap_key == "SUBJECT":
                return parsed.get("Subject", "")
            if imap_key == "SIZE":
                return len(msg["raw"])
            if imap_key in ("FROM", "TO", "CC"):
                return parsed.get(imap_key.capitalize(), "")
            raise AssertionError(f"FakeConn.sort: unhandled sort key {imap_key!r}")

        for imap_key, key_reverse in reversed(sort_keys):
            uids.sort(key=lambda u: key_value(u, imap_key), reverse=key_reverse)
        return uids

    async def fetch(self, uids, data_items):
        import email as email_module
        import email.policy
        from types import SimpleNamespace

        mb = self._mailboxes[self._selected]
        result = {}
        for uid in uids:
            if uid not in mb["messages"]:
                continue
            m = mb["messages"][uid]
            row = {
                b"RFC822": m["raw"],
                b"RFC822.HEADER": _rfc822_header_bytes(m["raw"]),
                b"BODYSTRUCTURE": _synth_bodystructure(m["raw"]),
                b"FLAGS": tuple(m["flags"]),
                b"INTERNALDATE": m["internaldate"],
                b"RFC822.SIZE": len(m["raw"]),
            }
            if "ENVELOPE" in data_items:
                parsed = email_module.message_from_bytes(m["raw"], policy=email.policy.default)
                subject = parsed.get("Subject", "")
                row[b"ENVELOPE"] = SimpleNamespace(subject=subject.encode("utf-8"))
            result[uid] = row
        return result

    def _bump_modseq(self, mb, uid):
        mb["highestmodseq"] += 1
        mb["messages"][uid]["modseq"] = mb["highestmodseq"]

    async def set_flags(self, uids, flags):
        mb = self._mailboxes[self._selected]
        for uid in uids:
            mb["messages"][uid]["flags"] = set(flags)
            self._bump_modseq(mb, uid)

    async def add_flags(self, uids, flags):
        mb = self._mailboxes[self._selected]
        for uid in uids:
            mb["messages"][uid]["flags"] |= set(flags)
            self._bump_modseq(mb, uid)

    async def remove_flags(self, uids, flags):
        mb = self._mailboxes[self._selected]
        for uid in uids:
            mb["messages"][uid]["flags"] -= set(flags)
            self._bump_modseq(mb, uid)

    async def fetch_changed_since(self, modseq, data_items=None):
        mb = self._mailboxes[self._selected]
        uids = [uid for uid, msg in mb["messages"].items() if msg["modseq"] > modseq]
        return await self.fetch(uids, data_items or ["FLAGS", "UID"])

    async def copy(self, uids, destination):
        src = self._mailboxes[self._selected]
        dst = self._mailboxes[destination]
        result = None
        for uid in uids:
            new_uid = dst["next_uid"]
            dst["next_uid"] += 1
            dst["highestmodseq"] += 1
            dst["messages"][new_uid] = {**src["messages"][uid], "modseq": dst["highestmodseq"]}
            result = (dst["uidvalidity"], new_uid)
        return result

    async def move(self, uids, destination):
        await self.copy(uids, destination)
        src = self._mailboxes[self._selected]
        for uid in uids:
            del src["messages"][uid]

    async def expunge(self, uids=None):
        mb = self._mailboxes[self._selected]
        target_uids = uids if uids is not None else list(mb["messages"].keys())
        for uid in target_uids:
            if mb["messages"].pop(uid, None) is not None:
                mb["highestmodseq"] += 1

    async def append(self, mailbox, message, flags=()):
        mb = self._mailboxes[mailbox]
        uid = mb["next_uid"]
        mb["next_uid"] += 1
        mb["highestmodseq"] += 1
        mb["messages"][uid] = {
            "raw": message, "flags": set(flags),
            "internaldate": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "modseq": mb["highestmodseq"],
        }
        return uid  # simulates a UIDPLUS-capable server


class FakeContext:
    account_id = "Aalice"
    id_redirect_key = ("example.com", "alice@example.com")

    def __init__(self, conn: FakeConn, blob_cache=None):
        self._conn = conn
        self.blob_cache = blob_cache or BlobCache()
        self.id_redirects = IdRedirectCache()
        self._request_cache = {}

    def require_account(self, account_id):
        assert account_id == self.account_id

    def imap(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()

    async def cached(self, key, compute):
        if key not in self._request_cache:
            self._request_cache[key] = await compute()
        return self._request_cache[key]

    def invalidate_cache(self):
        self._request_cache.clear()


@pytest.fixture
def conn():
    c = FakeConn()
    c.add_mailbox("INBOX", uidvalidity=100, highestmodseq=5)
    return c


@pytest.fixture
def ctx(conn):
    return FakeContext(conn)


async def test_email_get_by_id(conn, ctx):
    uid = conn.add_message("INBOX", MSG1, flags=("\\Seen",))
    email_id = encode_email_id("INBOX", 100, uid)

    result = await email_types.email_get(ctx, {"ids": [email_id]})
    assert result["notFound"] == []
    assert len(result["list"]) == 1
    email_obj = result["list"][0]
    assert email_obj["subject"] == "First message"
    assert email_obj["keywords"] == {"$seen": True}
    assert email_obj["mailboxIds"] == {encode_mailbox_id("INBOX"): True}
    assert result["state"]


async def test_email_get_requires_ids(ctx):
    with pytest.raises(InvalidArguments):
        await email_types.email_get(ctx, {})


async def test_email_get_not_found_for_stale_uidvalidity(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    stale_id = encode_email_id("INBOX", 999, uid)  # wrong uidvalidity
    result = await email_types.email_get(ctx, {"ids": [stale_id]})
    assert result["notFound"] == [stale_id]
    assert result["list"] == []


async def test_email_get_properties_filter(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    result = await email_types.email_get(ctx, {"ids": [email_id], "properties": ["subject"]})
    assert result["list"][0] == {"id": email_id, "subject": "First message"}


async def test_email_query_in_mailbox(conn, ctx):
    conn.add_message("INBOX", MSG1)
    conn.add_message("INBOX", MSG2)
    result = await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}})
    assert result["total"] == 2
    assert len(result["ids"]) == 2
    assert result["queryState"]


async def test_email_query_requires_in_mailbox(ctx):
    with pytest.raises(InvalidArguments):
        await email_types.email_query(ctx, {"filter": {}})


async def test_email_query_filters_by_keyword(conn, ctx):
    conn.add_message("INBOX", MSG1, flags=("\\Seen",))
    conn.add_message("INBOX", MSG2)
    result = await email_types.email_query(
        ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX"), "hasKeyword": "$seen"}}
    )
    assert result["total"] == 1


async def test_email_query_text_search(conn, ctx):
    conn.add_message("INBOX", MSG1)
    conn.add_message("INBOX", MSG2)
    result = await email_types.email_query(
        ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX"), "text": "Re:"}}
    )
    assert result["total"] == 1


async def test_email_set_update_keywords(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    result = await email_types.email_set(ctx, {"update": {email_id: {"keywords": {"$seen": True}}}})
    assert email_id in result["updated"]
    assert result["notUpdated"] == {}
    assert "\\Seen" in conn._mailboxes["INBOX"]["messages"][uid]["flags"]


async def test_email_set_update_single_keyword_patch(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    await email_types.email_set(ctx, {"update": {email_id: {"keywords/$flagged": True}}})
    assert "\\Flagged" in conn._mailboxes["INBOX"]["messages"][uid]["flags"]


async def test_email_set_add_to_second_mailbox_via_copy(conn, ctx):
    conn.add_mailbox("Archive", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    new_mailbox_ids = {encode_mailbox_id("INBOX"): True, encode_mailbox_id("Archive"): True}

    result = await email_types.email_set(ctx, {"update": {email_id: {"mailboxIds": new_mailbox_ids}}})
    assert email_id in result["updated"]
    assert 1 in conn._mailboxes["INBOX"]["messages"]  # original untouched
    assert len(conn._mailboxes["Archive"]["messages"]) == 1  # copied


async def test_email_set_move_to_different_mailbox_via_full_replacement(conn, ctx):
    conn.add_mailbox("Archive", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    new_mailbox_ids = {encode_mailbox_id("Archive"): True}

    result = await email_types.email_set(ctx, {"update": {email_id: {"mailboxIds": new_mailbox_ids}}})
    assert result["notUpdated"] == {}
    assert email_id in result["updated"]  # id echoed back unchanged, per RFC 8620
    assert uid not in conn._mailboxes["INBOX"]["messages"]  # removed from source
    assert len(conn._mailboxes["Archive"]["messages"]) == 1  # copied to destination


async def test_email_set_move_via_mailbox_id_patch_keys(conn, ctx):
    """This is the exact patch shape aerc (and presumably other real
    clients) use for every move/delete/archive operation:
    `mailboxIds/<id>: true/null`, never a full mailboxIds replacement.
    """
    conn.add_mailbox("Trash", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    patch = {
        f"mailboxIds/{encode_mailbox_id('INBOX')}": None,
        f"mailboxIds/{encode_mailbox_id('Trash')}": True,
    }

    result = await email_types.email_set(ctx, {"update": {email_id: patch}})
    assert result["notUpdated"] == {}
    assert uid not in conn._mailboxes["INBOX"]["messages"]
    assert len(conn._mailboxes["Trash"]["messages"]) == 1


async def test_email_set_move_records_id_redirect_and_get_resolves_it(conn, ctx):
    """After a move, the old id must not just silently 404 forever - an
    in-memory redirect (id_redirect.py) should let it keep resolving for
    this process's uptime, since some real clients (confirmed: Bulwark
    webmail) cache ids across a move and never handle a stale-id miss
    gracefully.
    """
    conn.add_mailbox("Archive", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    old_id = encode_email_id("INBOX", 100, uid)
    patch = {
        f"mailboxIds/{encode_mailbox_id('INBOX')}": None,
        f"mailboxIds/{encode_mailbox_id('Archive')}": True,
    }

    result = await email_types.email_set(ctx, {"update": {old_id: patch}})
    assert result["notUpdated"] == {}

    # Email/get on the now-physically-gone old id must still resolve, and
    # must report itself back under the *old* id, not the new physical one.
    get_result = await email_types.email_get(ctx, {"ids": [old_id]})
    assert get_result["notFound"] == []
    assert len(get_result["list"]) == 1
    assert get_result["list"][0]["id"] == old_id
    assert get_result["list"][0]["mailboxIds"] == {encode_mailbox_id("Archive"): True}


async def test_email_set_update_on_redirected_id_acts_on_new_location(conn, ctx):
    """A client that still has the pre-move id and tries to act on it
    again (e.g. mark it read) should be resolved to the message's current
    physical location, not rejected as stale.
    """
    conn.add_mailbox("Archive", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    old_id = encode_email_id("INBOX", 100, uid)
    await email_types.email_set(
        ctx,
        {
            "update": {
                old_id: {
                    f"mailboxIds/{encode_mailbox_id('INBOX')}": None,
                    f"mailboxIds/{encode_mailbox_id('Archive')}": True,
                }
            }
        },
    )

    result = await email_types.email_set(ctx, {"update": {old_id: {"keywords": {"$seen": True}}}})
    assert result["notUpdated"] == {}
    archived_uid = next(iter(conn._mailboxes["Archive"]["messages"]))
    assert "\\Seen" in conn._mailboxes["Archive"]["messages"][archived_uid]["flags"]


async def test_email_set_destroy_on_redirected_id(conn, ctx):
    conn.add_mailbox("Archive", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    old_id = encode_email_id("INBOX", 100, uid)
    await email_types.email_set(
        ctx,
        {
            "update": {
                old_id: {
                    f"mailboxIds/{encode_mailbox_id('INBOX')}": None,
                    f"mailboxIds/{encode_mailbox_id('Archive')}": True,
                }
            }
        },
    )

    result = await email_types.email_set(ctx, {"destroy": [old_id]})
    assert result["destroyed"] == [old_id]
    assert result["notDestroyed"] == {}
    assert not conn._mailboxes["Archive"]["messages"]


async def test_email_set_add_mailbox_via_single_patch_key(conn, ctx):
    """A single `mailboxIds/<id>: true` patch key (no removal) is a pure
    label-add - aerc's ModifyLabels flow - and must not touch the
    original mailbox.
    """
    conn.add_mailbox("Archive", uidvalidity=1)
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    patch = {f"mailboxIds/{encode_mailbox_id('Archive')}": True}

    result = await email_types.email_set(ctx, {"update": {email_id: patch}})
    assert result["notUpdated"] == {}
    assert uid in conn._mailboxes["INBOX"]["messages"]  # original untouched
    assert len(conn._mailboxes["Archive"]["messages"]) == 1


async def test_email_set_remove_only_mailbox_is_invalid_properties(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    patch = {f"mailboxIds/{encode_mailbox_id('INBOX')}": None}

    result = await email_types.email_set(ctx, {"update": {email_id: patch}})
    assert result["notUpdated"][email_id]["type"] == "invalidProperties"
    assert uid in conn._mailboxes["INBOX"]["messages"]  # untouched, not deleted


async def test_email_set_update_stale_uidvalidity(conn, ctx):
    conn.add_message("INBOX", MSG1)
    stale_id = encode_email_id("INBOX", 999, 1)
    result = await email_types.email_set(ctx, {"update": {stale_id: {"keywords": {"$seen": True}}}})
    assert stale_id in result["notUpdated"]


async def test_email_set_update_invalid_id(ctx):
    result = await email_types.email_set(ctx, {"update": {"not-a-real-id": {}}})
    assert result["notUpdated"]["not-a-real-id"]["type"] == "invalidArguments"


async def test_email_set_destroy(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    email_id = encode_email_id("INBOX", 100, uid)
    result = await email_types.email_set(ctx, {"destroy": [email_id]})
    assert result["destroyed"] == [email_id]
    assert uid not in conn._mailboxes["INBOX"]["messages"]


async def test_email_changes_garbage_since_state_is_cannot_calculate(ctx):
    with pytest.raises(CannotCalculateChanges):
        await email_types.email_changes(ctx, {"sinceState": "anything"})


async def test_email_changes_requires_since_state(ctx):
    with pytest.raises(InvalidArguments):
        await email_types.email_changes(ctx, {})


async def test_email_changes_detects_created(conn, ctx):
    conn.add_message("INBOX", MSG1)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    new_uid = conn.add_message("INBOX", MSG2)

    # A later Email/changes poll is a separate HTTP request in real usage
    # (a fresh RequestContext, hence a fresh per-request mail-state cache
    # - see context.py's _RequestCache) - reusing the same ctx here would
    # incorrectly serve the query's cached pre-mutation sweep.
    ctx = FakeContext(conn)
    result = await email_types.email_changes(ctx, {"sinceState": since_state})
    assert result["created"] == [encode_email_id("INBOX", 100, new_uid)]
    assert result["updated"] == []
    assert result["destroyed"] == []
    assert result["oldState"] == since_state
    assert result["newState"] != since_state


async def test_email_changes_detects_updated(conn, ctx):
    uid = conn.add_message("INBOX", MSG1)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    await email_types.email_set(ctx, {"update": {encode_email_id("INBOX", 100, uid): {"keywords": {"$seen": True}}}})

    ctx = FakeContext(conn)  # separate request, see test_email_changes_detects_created
    result = await email_types.email_changes(ctx, {"sinceState": since_state})
    assert result["created"] == []
    assert result["updated"] == [encode_email_id("INBOX", 100, uid)]
    assert result["destroyed"] == []


async def test_email_changes_mixed_create_and_update(conn, ctx):
    uid1 = conn.add_message("INBOX", MSG1)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    await email_types.email_set(ctx, {"update": {encode_email_id("INBOX", 100, uid1): {"keywords": {"$seen": True}}}})
    uid2 = conn.add_message("INBOX", MSG2)

    ctx = FakeContext(conn)  # separate request, see test_email_changes_detects_created
    result = await email_types.email_changes(ctx, {"sinceState": since_state})
    assert set(result["created"]) == {encode_email_id("INBOX", 100, uid2)}
    assert set(result["updated"]) == {encode_email_id("INBOX", 100, uid1)}
    assert result["destroyed"] == []


async def test_email_changes_falls_back_when_deletion_cannot_be_reconciled(conn, ctx):
    uid1 = conn.add_message("INBOX", MSG1)
    conn.add_message("INBOX", MSG2)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    # Destroy without any compensating create - message count no longer
    # reconciles as "pure creates", so we can't honestly claim
    # destroyed: [] without QRESYNC to identify what was removed.
    await email_types.email_set(ctx, {"destroy": [encode_email_id("INBOX", 100, uid1)]})

    ctx = FakeContext(conn)  # separate request, see test_email_changes_detects_created
    with pytest.raises(CannotCalculateChanges):
        await email_types.email_changes(ctx, {"sinceState": since_state})


async def test_email_changes_no_changes_returns_empty(conn, ctx):
    conn.add_message("INBOX", MSG1)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    result = await email_types.email_changes(ctx, {"sinceState": since_state})
    assert result["created"] == result["updated"] == result["destroyed"] == []
    assert result["newState"] == since_state


async def test_email_changes_uidvalidity_rotation_is_cannot_calculate(conn, ctx):
    conn.add_message("INBOX", MSG1)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    conn._mailboxes["INBOX"]["uidvalidity"] = 999999

    ctx = FakeContext(conn)  # separate request, see test_email_changes_detects_created
    with pytest.raises(CannotCalculateChanges):
        await email_types.email_changes(ctx, {"sinceState": since_state})


async def test_email_changes_mailbox_removed_is_cannot_calculate(conn, ctx):
    conn.add_mailbox("Temp", uidvalidity=1)
    conn.add_message("Temp", MSG1)
    since_state = (await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}}))[
        "queryState"
    ]
    del conn._mailboxes["Temp"]

    ctx = FakeContext(conn)  # separate request, see test_email_changes_detects_created
    with pytest.raises(CannotCalculateChanges):
        await email_types.email_changes(ctx, {"sinceState": since_state})


async def test_email_import_success(conn, ctx):
    blob_id = ctx.blob_cache.put(MSG1, "message/rfc822")
    result = await email_types.email_import(
        ctx, {"emails": {"i1": {"blobId": blob_id, "mailboxIds": {encode_mailbox_id("INBOX"): True}}}}
    )
    assert result["notCreated"] == {}
    assert "i1" in result["created"]
    imported = result["created"]["i1"]
    assert imported["subject"] == "First message"
    assert len(conn._mailboxes["INBOX"]["messages"]) == 1


async def test_email_import_with_keywords(conn, ctx):
    blob_id = ctx.blob_cache.put(MSG1, "message/rfc822")
    result = await email_types.email_import(
        ctx,
        {
            "emails": {
                "i1": {
                    "blobId": blob_id,
                    "mailboxIds": {encode_mailbox_id("INBOX"): True},
                    "keywords": {"$seen": True},
                }
            }
        },
    )
    imported = result["created"]["i1"]
    assert imported["keywords"] == {"$seen": True}


async def test_email_import_to_multiple_mailboxes(conn, ctx):
    conn.add_mailbox("Archive", uidvalidity=1)
    blob_id = ctx.blob_cache.put(MSG1, "message/rfc822")
    new_mailbox_ids = {encode_mailbox_id("INBOX"): True, encode_mailbox_id("Archive"): True}
    await email_types.email_import(ctx, {"emails": {"i1": {"blobId": blob_id, "mailboxIds": new_mailbox_ids}}})
    assert len(conn._mailboxes["INBOX"]["messages"]) == 1
    assert len(conn._mailboxes["Archive"]["messages"]) == 1


async def test_email_import_unknown_blob_id(ctx):
    result = await email_types.email_import(
        ctx, {"emails": {"i1": {"blobId": "Unotreal", "mailboxIds": {encode_mailbox_id("INBOX"): True}}}}
    )
    assert result["notCreated"]["i1"]["type"] == "invalidArguments"


async def test_email_import_requires_blob_id_and_mailbox_ids(ctx):
    result = await email_types.email_import(ctx, {"emails": {"i1": {}}})
    assert result["notCreated"]["i1"]["type"] == "invalidArguments"


async def test_email_import_requires_emails_arg(ctx):
    with pytest.raises(InvalidArguments):
        await email_types.email_import(ctx, {})


async def test_email_query_sort_by_received_at_ascending(conn, ctx):
    conn.add_message("INBOX", MSG2)  # 13:00
    conn.add_message("INBOX", MSG1)  # 12:00 - added second, sorts first
    result = await email_types.email_query(
        ctx,
        {
            "filter": {"inMailbox": encode_mailbox_id("INBOX")},
            "sort": [{"property": "receivedAt", "isAscending": True}],
        },
    )
    assert len(result["ids"]) == 2
    # Both messages carry the same internaldate in this fake (test doesn't
    # vary it), so assert count/order-stability rather than exact order;
    # the real ordering guarantee is covered by the FakeConn.sort() unit
    # behavior exercised via test_email_query_sort_by_subject below.
    assert result["total"] == 2


async def test_email_query_sort_by_subject(conn, ctx):
    conn.add_message("INBOX", MSG2)  # Subject: Re: First message
    conn.add_message("INBOX", MSG1)  # Subject: First message
    result = await email_types.email_query(
        ctx,
        {
            "filter": {"inMailbox": encode_mailbox_id("INBOX")},
            "sort": [{"property": "subject", "isAscending": True}],
        },
    )
    from jmap_bridge.backends.imap.modseq_state import decode_email_id

    subjects_in_order = []
    for email_id in result["ids"]:
        _, _, uid = decode_email_id(email_id)
        raw = conn._mailboxes["INBOX"]["messages"][uid]["raw"]
        subjects_in_order.append(email.message_from_bytes(raw).get("Subject"))
    assert subjects_in_order == ["First message", "Re: First message"]


async def test_email_query_sort_falls_back_when_sort_unsupported(conn, ctx):
    from jmap_bridge.backends.imap.client import ImapError

    conn.add_message("INBOX", MSG2)
    conn.add_message("INBOX", MSG1)

    async def failing_sort(sort_criteria, criteria="ALL"):
        raise ImapError("SORT not supported")

    conn.sort = failing_sort

    result = await email_types.email_query(
        ctx,
        {
            "filter": {"inMailbox": encode_mailbox_id("INBOX")},
            "sort": [{"property": "subject", "isAscending": True}],
        },
    )
    from jmap_bridge.backends.imap.modseq_state import decode_email_id

    subjects_in_order = []
    for email_id in result["ids"]:
        _, _, uid = decode_email_id(email_id)
        raw = conn._mailboxes["INBOX"]["messages"][uid]["raw"]
        subjects_in_order.append(email.message_from_bytes(raw).get("Subject"))
    assert subjects_in_order == ["First message", "Re: First message"]


async def test_email_query_unsupported_filter_property_rejected(conn, ctx):
    from jmap_bridge.errors import UnsupportedFilter

    with pytest.raises(UnsupportedFilter):
        await email_types.email_query(
            ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX"), "hasAttachment": True}}
        )


async def test_email_query_filter_operator_without_findable_in_mailbox_rejected(conn, ctx):
    """inMailbox nested inside an OR (rather than a top-level AND, the
    shape real clients actually use) isn't something IMAP can honor in
    one command - _find_in_mailbox deliberately doesn't chase it, so this
    should read as "no mailbox scope", not silently search everything.
    """
    with pytest.raises(InvalidArguments):
        await email_types.email_query(
            ctx,
            {
                "filter": {
                    "operator": "OR",
                    "conditions": [{"inMailbox": encode_mailbox_id("INBOX")}],
                }
            },
        )


async def test_email_query_unsupported_filter_operator_type_rejected(conn, ctx):
    from jmap_bridge.errors import UnsupportedFilter

    with pytest.raises(UnsupportedFilter):
        await email_types.email_query(
            ctx,
            {
                "filter": {
                    "operator": "AND",
                    "conditions": [
                        {"inMailbox": encode_mailbox_id("INBOX")},
                        {"operator": "XOR", "conditions": [{"subject": "x"}]},
                    ],
                }
            },
        )


async def test_email_query_unsupported_sort_property_rejected(conn, ctx):
    from jmap_bridge.errors import UnsupportedSort

    with pytest.raises(UnsupportedSort):
        await email_types.email_query(
            ctx,
            {
                "filter": {"inMailbox": encode_mailbox_id("INBOX")},
                "sort": [{"property": "hasAttachment", "isAscending": True}],
            },
        )


async def test_email_query_and_operator_combines_conditions(conn, ctx):
    conn.add_message("INBOX", MSG1)  # Subject: First message, from alice
    conn.add_message("INBOX", MSG2)  # Subject: Re: First message, from bob
    result = await email_types.email_query(
        ctx,
        {
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"inMailbox": encode_mailbox_id("INBOX")},
                    {"subject": "First message"},
                    {"from": "alice"},
                ],
            }
        },
    )
    assert result["total"] == 1


async def test_email_query_or_operator_matches_either_condition(conn, ctx):
    conn.add_message("INBOX", MSG1)  # from alice
    conn.add_message("INBOX", MSG2)  # from bob
    result = await email_types.email_query(
        ctx,
        {
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"inMailbox": encode_mailbox_id("INBOX")},
                    {
                        "operator": "OR",
                        "conditions": [{"from": "alice"}, {"from": "bob"}],
                    },
                ],
            }
        },
    )
    assert result["total"] == 2


async def test_email_query_or_operator_excludes_neither_match(conn, ctx):
    conn.add_message("INBOX", MSG1)  # from alice
    conn.add_message("INBOX", MSG2)  # from bob
    result = await email_types.email_query(
        ctx,
        {
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"inMailbox": encode_mailbox_id("INBOX")},
                    {
                        "operator": "OR",
                        "conditions": [{"from": "nobody"}, {"from": "nobody-else"}],
                    },
                ],
            }
        },
    )
    assert result["total"] == 0


async def test_email_query_not_operator_excludes_matching_condition(conn, ctx):
    conn.add_message("INBOX", MSG1)  # from alice
    conn.add_message("INBOX", MSG2)  # from bob
    result = await email_types.email_query(
        ctx,
        {
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"inMailbox": encode_mailbox_id("INBOX")},
                    {"operator": "NOT", "conditions": [{"from": "alice"}]},
                ],
            }
        },
    )
    assert result["total"] == 1


async def test_email_query_three_way_or_folds_correctly(conn, ctx):
    """IMAP's OR is binary - verify the right-fold for >2 operands
    actually matches any of the three, not just the last two."""
    conn.add_message("INBOX", MSG1)  # Subject: First message
    conn.add_message("INBOX", MSG2)  # Subject: Re: First message
    third = conn.add_message("INBOX", MSG1.replace(b"First message", b"Totally different"))
    result = await email_types.email_query(
        ctx,
        {
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"inMailbox": encode_mailbox_id("INBOX")},
                    {
                        "operator": "OR",
                        "conditions": [
                            {"subject": "First message"},
                            {"subject": "Re:"},
                            {"subject": "Totally different"},
                        ],
                    },
                ],
            }
        },
    )
    assert result["total"] == 3


async def test_email_query_body_filter(conn, ctx):
    conn.add_message("INBOX", MSG1)  # body: "Hello Bob."
    conn.add_message("INBOX", MSG2)  # body: "Hi Alice."
    result = await email_types.email_query(
        ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX"), "body": "Hello Bob"}}
    )
    assert result["total"] == 1


async def test_email_query_strips_trailing_wildcard_from_text_filters(conn, ctx):
    """Bulwark webmail appends a trailing '*' to each word of a text
    search term (prefix-match FTS syntax) - IMAP SEARCH has no wildcard
    syntax, so a literal '*' must be stripped before it reaches SEARCH or
    it becomes part of the literal string being matched and fails to
    match anything.
    """
    conn.add_message("INBOX", MSG1)  # body: "Hello Bob."
    conn.add_message("INBOX", MSG2)  # body: "Hi Alice."
    result = await email_types.email_query(
        ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX"), "body": "Hello* Bob*"}}
    )
    assert result["total"] == 1


async def test_email_query_header_filter_with_value(conn, ctx):
    conn.add_message("INBOX", MSG1)
    conn.add_message("INBOX", MSG2)
    result = await email_types.email_query(
        ctx,
        {
            "filter": {
                "inMailbox": encode_mailbox_id("INBOX"),
                "header": ["Message-Id", "msg1@example.com"],
            }
        },
    )
    assert result["total"] == 1


async def test_email_query_sort_by_from(conn, ctx):
    conn.add_message("INBOX", MSG2)  # From: Bob
    conn.add_message("INBOX", MSG1)  # From: Alice
    result = await email_types.email_query(
        ctx,
        {
            "filter": {"inMailbox": encode_mailbox_id("INBOX")},
            "sort": [{"property": "from", "isAscending": True}],
        },
    )
    assert result["total"] == 2


async def test_email_query_does_not_fetch_full_bodies(conn, ctx):
    """The bug that motivated this rewrite: Email/query must not pull
    full RFC822 bodies just to return a list of ids - assert the FakeConn
    is never asked for RFC822 during a query.
    """
    conn.add_message("INBOX", MSG1)
    conn.add_message("INBOX", MSG2)
    original_fetch = conn.fetch
    requested_items = []

    async def spying_fetch(uids, data_items):
        requested_items.append(list(data_items))
        return await original_fetch(uids, data_items)

    conn.fetch = spying_fetch

    await email_types.email_query(ctx, {"filter": {"inMailbox": encode_mailbox_id("INBOX")}})
    for items in requested_items:
        assert "RFC822" not in items


async def test_email_set_create_basic_text_message(conn, ctx):
    result = await email_types.email_set(
        ctx,
        {
            "create": {
                "aerc": {
                    "mailboxIds": {encode_mailbox_id("INBOX"): True},
                    "from": [{"name": "Alice", "email": "alice@example.com"}],
                    "to": [{"name": "Bob", "email": "bob@example.com"}],
                    "subject": "Hello from aerc",
                    "bodyValues": {"body": {"value": "Hi Bob, this is a test."}},
                    "textBody": [{"partId": "body", "type": "text/plain"}],
                }
            }
        },
    )
    assert result["notCreated"] == {}
    assert "aerc" in result["created"]
    created = result["created"]["aerc"]
    assert created["subject"] == "Hello from aerc"
    assert created["from"] == [{"name": "Alice", "email": "alice@example.com"}]
    assert len(conn._mailboxes["INBOX"]["messages"]) == 1
    raw = next(iter(conn._mailboxes["INBOX"]["messages"].values()))["raw"]
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert parsed["Subject"] == "Hello from aerc"
    assert parsed["Message-Id"]  # we always mint one
    assert "Hi Bob" in parsed.get_content()


async def test_email_set_create_header_property_as_text(conn, ctx):
    """RFC 8621 SS4.1.5 dynamic header:Name property on create - Bulwark
    webmail sets `header:Disposition-Notification-To:asText` to request
    an MDN.
    """
    result = await email_types.email_set(
        ctx,
        {
            "create": {
                "c1": {
                    "mailboxIds": {encode_mailbox_id("INBOX"): True},
                    "subject": "MDN please",
                    "bodyValues": {"body": {"value": "hi"}},
                    "textBody": [{"partId": "body", "type": "text/plain"}],
                    "header:Disposition-Notification-To:asText": "alice@example.com",
                }
            }
        },
    )
    assert result["notCreated"] == {}
    raw = next(iter(conn._mailboxes["INBOX"]["messages"].values()))["raw"]
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert parsed["Disposition-Notification-To"] == "alice@example.com"


async def test_email_set_create_requires_mailbox_ids(conn, ctx):
    result = await email_types.email_set(
        ctx, {"create": {"c1": {"subject": "no mailbox"}}}
    )
    assert result["notCreated"]["c1"]["type"] == "invalidArguments"
    assert result["created"] == {}


async def test_email_set_create_text_and_html(conn, ctx):
    result = await email_types.email_set(
        ctx,
        {
            "create": {
                "c1": {
                    "mailboxIds": {encode_mailbox_id("INBOX"): True},
                    "subject": "Multipart",
                    "bodyValues": {
                        "t": {"value": "plain version"},
                        "h": {"value": "<p>html version</p>"},
                    },
                    "textBody": [{"partId": "t", "type": "text/plain"}],
                    "htmlBody": [{"partId": "h", "type": "text/html"}],
                }
            }
        },
    )
    assert "c1" in result["created"]
    raw = next(iter(conn._mailboxes["INBOX"]["messages"].values()))["raw"]
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert parsed.is_multipart()
    body = parsed.get_body(preferencelist=("plain",))
    assert "plain version" in body.get_content()
    html_body = parsed.get_body(preferencelist=("html",))
    assert "html version" in html_body.get_content()


async def test_email_set_create_with_attachment_from_blob_cache(conn, ctx):
    blob_id = ctx.blob_cache.put(b"file bytes here", "text/plain")
    result = await email_types.email_set(
        ctx,
        {
            "create": {
                "c1": {
                    "mailboxIds": {encode_mailbox_id("INBOX"): True},
                    "subject": "With attachment",
                    "bodyValues": {"t": {"value": "see attached"}},
                    "textBody": [{"partId": "t", "type": "text/plain"}],
                    "attachments": [{"blobId": blob_id, "name": "notes.txt", "type": "text/plain"}],
                }
            }
        },
    )
    assert "c1" in result["created"]
    assert result["created"]["c1"]["hasAttachment"] is True
    raw = next(iter(conn._mailboxes["INBOX"]["messages"].values()))["raw"]
    assert b"notes.txt" in raw
    assert b"file bytes here" in raw or b"ZmlsZSBieXRlcyBoZXJl" in raw  # plain or base64-encoded


async def test_email_set_create_unknown_attachment_blob_id(conn, ctx):
    result = await email_types.email_set(
        ctx,
        {
            "create": {
                "c1": {
                    "mailboxIds": {encode_mailbox_id("INBOX"): True},
                    "attachments": [{"blobId": "Ubogus"}],
                }
            }
        },
    )
    assert result["notCreated"]["c1"]["type"] == "invalidArguments"
    assert conn._mailboxes["INBOX"]["messages"] == {}


async def test_email_set_create_invalid_mailbox_ref_does_not_leak_message(conn, ctx):
    """A creationId reference to a nonexistent Mailbox should fail cleanly,
    not append a message with garbage mailbox data.
    """
    result = await email_types.email_set(
        ctx,
        {
            "create": {
                "c1": {
                    "mailboxIds": {"#nonexistent": True},
                    "subject": "orphan",
                }
            }
        },
    )
    assert result["notCreated"]["c1"]["type"] == "invalidArguments"
