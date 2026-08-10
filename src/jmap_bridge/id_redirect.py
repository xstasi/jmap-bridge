"""In-memory id-redirect cache: when an Email move or Mailbox rename
changes the physical location our id encoding is derived from (see
modseq_state.py / mailbox_map.py), the *old* id would otherwise 404
forever - breaking the guarantee (RFC 8620 SS5.3: an object's id never
changes across an update) that some clients actually depend on and don't
handle a stale id gracefully for (confirmed: Bulwark webmail's cached
thread/search-result ids never get re-resolved after a move).

This is the same class of "deliberate, narrow exception to no local
storage" as blob_cache.py: process memory only, never disk, bounded size,
scoped per (domain, username) so one account's ids can never resolve into
another account's data. A bridge restart clears it, so an id survives a
move only for the process's uptime - weaker than a real JMAP-native
server's permanent guarantee, but a large improvement over immediate,
permanent loss, and a low-risk way to buy time to see whether a fuller
fix (e.g. RFC 8474 OBJECTID/MAILBOXID, where a backend supports it) is
actually worth building.
"""

from __future__ import annotations

DEFAULT_MAX_ENTRIES_PER_ACCOUNT = 5000

AccountKey = tuple[str, str]  # (domain, username)


class IdRedirectCache:
    def __init__(self, max_entries_per_account: int = DEFAULT_MAX_ENTRIES_PER_ACCOUNT) -> None:
        self._max_entries = max_entries_per_account
        self._by_account: dict[AccountKey, dict[str, str]] = {}

    def record(self, account_key: AccountKey, old_id: str, new_id: str) -> None:
        if old_id == new_id:
            return
        table = self._by_account.setdefault(account_key, {})
        table.pop(old_id, None)  # re-insert at the end (simple recency-based eviction order)
        table[old_id] = new_id
        while len(table) > self._max_entries:
            table.pop(next(iter(table)))

    def resolve(self, account_key: AccountKey, requested_id: str) -> str:
        """Follow the redirect chain (with cycle protection) to find the
        id currently backing `requested_id`. Returns `requested_id`
        unchanged if there's no recorded redirect for it - the common
        case, and the only one for an id that's never moved/renamed.
        """
        table = self._by_account.get(account_key)
        if not table:
            return requested_id
        seen = {requested_id}
        current = requested_id
        while current in table:
            current = table[current]
            if current in seen:
                return requested_id  # cycle guard - shouldn't happen, fail safe
            seen.add(current)
        return current
