from contextlib import asynccontextmanager

import pytest

from jmap_bridge.backends.imap.client import MailboxStatus
from jmap_bridge.backends.imap.mailbox_map import encode_mailbox_id
from jmap_bridge.errors import CannotCalculateChanges
from jmap_bridge.id_redirect import IdRedirectCache
from jmap_bridge.types import mailbox as mailbox_types


class FakeConn:
    def __init__(self, mailboxes: dict[str, dict], delimiter: str = "/"):
        # mailboxes: name -> {uidvalidity, highestmodseq, exists, unseen, flags}
        self._mailboxes = mailboxes
        self._selected = None
        self.delimiter = delimiter
        self.select_count = 0

    async def list_mailboxes(self):
        return [
            (info.get("flags", frozenset()), self.delimiter, name)
            for name, info in self._mailboxes.items()
        ]

    async def status(self, mailbox):
        info = self._mailboxes[mailbox]
        return MailboxStatus(
            uidvalidity=info["uidvalidity"],
            highestmodseq=info["highestmodseq"],
            uidnext=info["exists"] + 1,
            exists=info["exists"],
            unseen=info["unseen"],
        )

    async def select(self, mailbox, readonly=True):
        # _status_map uses select() (not status()) for the authoritative
        # cursor - see its docstring for why. unseen is deliberately 0
        # here, matching the real ImapConnection.select()'s contract -
        # _status_and_unseen_map gets a real count via search("UNSEEN")
        # on this same just-selected connection instead.
        self.select_count += 1
        self._selected = mailbox
        info = self._mailboxes[mailbox]
        return MailboxStatus(
            uidvalidity=info["uidvalidity"],
            highestmodseq=info["highestmodseq"],
            uidnext=info["exists"] + 1,
            exists=info["exists"],
            unseen=0,
        )

    async def search(self, criteria="ALL"):
        if criteria == "UNSEEN":
            # Dummy uids - _status_and_unseen_map only needs the count.
            return list(range(self._mailboxes[self._selected]["unseen"]))
        raise AssertionError(f"FakeConn.search: unhandled criteria {criteria!r}")

    async def create_folder(self, name):
        self._mailboxes[name] = {
            "uidvalidity": 1, "highestmodseq": 0, "exists": 0, "unseen": 0, "flags": frozenset()
        }

    async def rename_folder(self, old_name, new_name):
        self._mailboxes[new_name] = self._mailboxes.pop(old_name)

    async def delete_folder(self, name):
        del self._mailboxes[name]


class FakeContext:
    account_id = "Aalice"
    id_redirect_key = ("example.com", "alice@example.com")

    def __init__(self, conn: FakeConn):
        self._conn = conn
        self.id_redirects = IdRedirectCache()
        self._request_cache = {}

    def require_account(self, account_id):
        assert account_id == self.account_id

    def imap(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()

    async def imap_parallel_map(self, entries, work, *, max_concurrency=None):
        # Sequential on the one fake conn - concurrency behavior is
        # verified live against Dovecot, not here; these tests only need
        # `work` to be called correctly for every entry.
        return [await work(self._conn, entry) for entry in entries]

    async def cached(self, key, compute):
        if key not in self._request_cache:
            self._request_cache[key] = await compute()
        return self._request_cache[key]

    def invalidate_cache(self):
        self._request_cache.clear()


def _default_mailboxes():
    return {
        "INBOX": {"uidvalidity": 100, "highestmodseq": 10, "exists": 5, "unseen": 2, "flags": frozenset()},
        "Archive": {
            "uidvalidity": 200, "highestmodseq": 3, "exists": 20, "unseen": 0,
            "flags": frozenset({"\\Archive"}),
        },
    }


async def test_mailbox_get_returns_all_with_counts():
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    result = await mailbox_types.mailbox_get(ctx, {})
    assert result["accountId"] == "Aalice"
    by_name = {m["name"]: m for m in result["list"]}
    assert by_name["INBOX"]["role"] == "inbox"
    assert by_name["INBOX"]["totalEmails"] == 5
    assert by_name["INBOX"]["unreadEmails"] == 2
    assert by_name["Archive"]["role"] == "archive"
    assert result["notFound"] == []


async def test_mailbox_get_unread_count_never_uses_status():
    """Regression test: unreadEmails must come from SELECT + SEARCH
    UNSEEN, never STATUS - STATUS on a mailbox not authoritatively
    synced by this connection can be served from a backend cache that
    doesn't see other connections' writes (confirmed live against a real
    server), which is exactly the bug this replaced.
    """
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)

    async def failing_status(mailbox):
        raise AssertionError("mailbox_get must not call status() for unread counts")

    conn.status = failing_status

    result = await mailbox_types.mailbox_get(ctx, {})
    by_name = {m["name"]: m for m in result["list"]}
    assert by_name["INBOX"]["unreadEmails"] == 2


async def test_mailbox_get_with_ids_and_properties():
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    inbox_id = encode_mailbox_id("INBOX")
    result = await mailbox_types.mailbox_get(
        ctx, {"ids": [inbox_id, "Mbogus"], "properties": ["name", "role"]}
    )
    assert result["list"] == [{"id": inbox_id, "name": "INBOX", "role": "inbox"}]
    assert result["notFound"] == ["Mbogus"]


async def test_mailbox_query_filters_by_role():
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    result = await mailbox_types.mailbox_query(ctx, {"filter": {"role": "archive"}})
    assert result["ids"] == [encode_mailbox_id("Archive")]
    assert result["total"] == 1


async def test_mailbox_changes_detects_updated_and_created():
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    initial = await mailbox_types.mailbox_get(ctx, {})
    since_state = initial["state"]

    mailboxes["INBOX"]["highestmodseq"] = 15  # simulate new mail arriving
    mailboxes["Sent"] = {
        "uidvalidity": 1, "highestmodseq": 0, "exists": 0, "unseen": 0, "flags": frozenset({"\\Sent"})
    }

    changes = await mailbox_types.mailbox_changes(ctx, {"sinceState": since_state})
    assert changes["updated"] == [encode_mailbox_id("INBOX")]
    assert changes["created"] == [encode_mailbox_id("Sent")]
    assert changes["destroyed"] == []


async def test_mailbox_changes_detects_destroyed():
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    initial = await mailbox_types.mailbox_get(ctx, {})
    since_state = initial["state"]

    del mailboxes["Archive"]

    changes = await mailbox_types.mailbox_changes(ctx, {"sinceState": since_state})
    assert changes["destroyed"] == [encode_mailbox_id("Archive")]


async def test_mailbox_changes_uidvalidity_rotation_raises_cannot_calculate():
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    initial = await mailbox_types.mailbox_get(ctx, {})
    since_state = initial["state"]

    mailboxes["INBOX"]["uidvalidity"] = 999  # simulate UIDVALIDITY rotation

    with pytest.raises(CannotCalculateChanges):
        await mailbox_types.mailbox_changes(ctx, {"sinceState": since_state})


async def test_mailbox_changes_rejects_missing_since_state():
    from jmap_bridge.errors import InvalidArguments

    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await mailbox_types.mailbox_changes(ctx, {})


async def test_mailbox_changes_garbage_since_state_is_cannot_calculate():
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    with pytest.raises(CannotCalculateChanges):
        await mailbox_types.mailbox_changes(ctx, {"sinceState": "not-a-real-token"})


async def test_mailbox_set_create_update_destroy():
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)

    result = await mailbox_types.mailbox_set(
        ctx,
        {
            "create": {"c1": {"name": "Projects"}},
            "update": {encode_mailbox_id("Archive"): {"name": "Old Mail"}},
            "destroy": [encode_mailbox_id("INBOX")],
        },
    )
    assert "c1" in result["created"]
    assert "Projects" in mailboxes
    assert "Old Mail" in mailboxes and "Archive" not in mailboxes
    assert "INBOX" not in mailboxes
    assert result["destroyed"] == [encode_mailbox_id("INBOX")]
    assert result["notCreated"] == {}
    assert result["notUpdated"] == {}
    assert result["notDestroyed"] == {}


async def test_mailbox_set_create_resolves_parent_id_created_in_same_call():
    """Regression test for a real bug found live: Bulwark webmail's
    archive-by-year/month feature creates a year Mailbox and a month
    Mailbox in the same Mailbox/set call, with the month's `parentId`
    set to the year's "#creationId". dispatch.py's substitute_created_ids
    can't resolve this (RFC 8620 SS5.3's same-call, same-type reference:
    it only resolves references against calls that have already fully
    returned), so mailbox_set must do it itself, processing creates in
    order and resolving "#..." parentId against what it's created so far
    in this same call."""
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)

    result = await mailbox_types.mailbox_set(
        ctx,
        {
            "create": {
                "year-2026": {"name": "2026"},
                "month-2026-06": {"name": "June", "parentId": "#year-2026"},
            }
        },
    )
    assert result["notCreated"] == {}
    assert "2026" in mailboxes
    assert "2026/June" in mailboxes
    assert result["created"]["month-2026-06"]["id"] == encode_mailbox_id("2026/June")


async def test_mailbox_set_update_rejects_unsupported_role_sort_order_parent_id():
    """Regression test for a real bug found live: Bulwark webmail's
    drag-to-reorder (sortOrder) and "set folder role" (role) UI actions
    both send Mailbox/set updates this bridge had no way to apply -
    IMAP has no client-settable special-use verb and no per-mailbox
    sortOrder metadata. Previously these were silently accepted and
    ignored, so the client's optimistic UI update would look like it
    worked, then quietly revert once it refetched from the server. Must
    fail loudly instead."""
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    mid = encode_mailbox_id("INBOX")

    for props in ({"role": "drafts"}, {"sortOrder": 2}, {"parentId": encode_mailbox_id("Archive")}):
        result = await mailbox_types.mailbox_set(ctx, {"update": {mid: props}})
        assert result["notUpdated"][mid]["type"] == "invalidProperties"

    # A rename combined with an unsupported property is rejected in full,
    # not partially applied.
    result = await mailbox_types.mailbox_set(
        ctx, {"update": {mid: {"name": "Renamed", "sortOrder": 1}}}
    )
    assert result["notUpdated"][mid]["type"] == "invalidProperties"
    assert "INBOX" in mailboxes and "Renamed" not in mailboxes


async def test_mailbox_set_create_requires_name():
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    result = await mailbox_types.mailbox_set(ctx, {"create": {"c1": {}}})
    assert result["notCreated"]["c1"]["type"] == "invalidArguments"


async def test_mailbox_set_destroy_unknown_id():
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)
    result = await mailbox_types.mailbox_set(ctx, {"destroy": ["Mnotreal!!!"]})
    assert "Mnotreal!!!" in result["notDestroyed"]


async def test_mailbox_set_rename_response_keyed_by_original_id():
    """RFC 8620 SS5.3: `updated` must be keyed by the id the client passed
    in, not a newly-derived one - regression test for a bug found
    reviewing Bulwark webmail's JMAP client (it assumes id stability
    across a rename, per spec).
    """
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    archive_id = encode_mailbox_id("Archive")

    result = await mailbox_types.mailbox_set(ctx, {"update": {archive_id: {"name": "Old Mail"}}})

    assert result["updated"] == {archive_id: None}
    assert result["notUpdated"] == {}


async def test_mailbox_set_rename_records_id_redirect_and_get_resolves_it():
    """A renamed mailbox's old id must not just 404 forever - an
    in-memory redirect (id_redirect.py) should let it keep resolving for
    this process's uptime, same reasoning as the Email-move redirect
    (some real clients, confirmed: Bulwark webmail, cache ids across a
    rename and never handle a stale-id miss gracefully).
    """
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    archive_id = encode_mailbox_id("Archive")

    await mailbox_types.mailbox_set(ctx, {"update": {archive_id: {"name": "Old Mail"}}})

    result = await mailbox_types.mailbox_get(ctx, {"ids": [archive_id]})
    assert result["notFound"] == []
    assert len(result["list"]) == 1
    assert result["list"][0]["id"] == archive_id  # reported back under the original id
    assert result["list"][0]["name"] == "Old Mail"


async def test_mailbox_set_update_on_redirected_id_acts_on_new_location():
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    archive_id = encode_mailbox_id("Archive")

    await mailbox_types.mailbox_set(ctx, {"update": {archive_id: {"name": "Old Mail"}}})
    result = await mailbox_types.mailbox_set(ctx, {"update": {archive_id: {"name": "Older Mail"}}})

    assert result["notUpdated"] == {}
    assert "Older Mail" in mailboxes
    assert "Old Mail" not in mailboxes


async def test_mailbox_set_destroy_on_redirected_id():
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes)
    ctx = FakeContext(conn)
    archive_id = encode_mailbox_id("Archive")

    await mailbox_types.mailbox_set(ctx, {"update": {archive_id: {"name": "Old Mail"}}})
    result = await mailbox_types.mailbox_set(ctx, {"destroy": [archive_id]})

    assert result["destroyed"] == [archive_id]
    assert result["notDestroyed"] == {}
    assert "Old Mail" not in mailboxes


async def test_mailbox_set_uses_server_delimiter_not_hardcoded_slash():
    """Regression test: a server using "." (not "/") as its IMAP
    hierarchy delimiter must not get a malformed "/"-joined path on
    create-with-parentId or rename.
    """
    mailboxes = _default_mailboxes()
    conn = FakeConn(mailboxes, delimiter=".")
    ctx = FakeContext(conn)
    inbox_id = encode_mailbox_id("INBOX")

    result = await mailbox_types.mailbox_set(
        ctx, {"create": {"c1": {"name": "Sub", "parentId": inbox_id}}}
    )
    assert "INBOX.Sub" in mailboxes
    assert result["created"]["c1"]["id"] == encode_mailbox_id("INBOX.Sub")

    archive_id = encode_mailbox_id("Archive")
    result = await mailbox_types.mailbox_set(ctx, {"update": {archive_id: {"name": "Old"}}})
    assert "Old" in mailboxes and "Archive" not in mailboxes
    assert result["updated"] == {archive_id: None}


async def test_mailbox_sweep_is_cached_across_calls_sharing_one_context():
    """Regression test for the redundant-sweep bug found live: an account
    with 16 mailboxes triggered ~85 SELECTs (5 full sweeps) for one HTTP
    request, because every JMAP method needing mail state redid its own
    full per-mailbox SELECT sweep. Mailbox/query and Email/query-style
    state both go through _cached_mail_sweep now, memoized per
    RequestContext (== one real HTTP request) - calling two such methods
    with the same ctx must only sweep once.
    """
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)

    await mailbox_types.mailbox_query(ctx, {})
    first_count = conn.select_count
    assert first_count == len(_default_mailboxes())  # one sweep

    since_state = (await mailbox_types.mailbox_query(ctx, {}))["queryState"]
    assert conn.select_count == first_count  # second query reused the cache

    await mailbox_types.mailbox_changes(ctx, {"sinceState": since_state})
    assert conn.select_count == first_count  # changes reused it too


async def test_mailbox_sweep_cache_is_invalidated_by_set():
    """A mutation must force a fresh sweep for a later call in the same
    request (see dispatch.py's invalidate-after-/set), otherwise a
    Mailbox/set immediately followed by Mailbox/query in the same batch
    would see stale pre-mutation state.
    """
    conn = FakeConn(_default_mailboxes())
    ctx = FakeContext(conn)

    await mailbox_types.mailbox_query(ctx, {})
    first_count = conn.select_count

    ctx.invalidate_cache()
    await mailbox_types.mailbox_query(ctx, {})
    assert conn.select_count == first_count * 2  # cache was cleared, swept again
