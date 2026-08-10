from jmap_bridge.backends.imap.mailbox_map import (
    ImapMailboxEntry,
    decode_mailbox_id,
    encode_mailbox_id,
    imap_list_to_jmap_mailboxes,
)


def test_mailbox_id_round_trip():
    mid = encode_mailbox_id("INBOX/Sub Folder")
    assert decode_mailbox_id(mid) == "INBOX/Sub Folder"


def test_inbox_gets_inbox_role():
    entries = [ImapMailboxEntry(flags=frozenset(), delimiter="/", name="INBOX")]
    mailboxes = imap_list_to_jmap_mailboxes(entries)
    assert mailboxes[0]["role"] == "inbox"
    assert mailboxes[0]["name"] == "INBOX"
    assert mailboxes[0]["parentId"] is None


def test_special_use_role_mapping():
    entries = [
        ImapMailboxEntry(flags=frozenset({"\\Archive"}), delimiter="/", name="Archive"),
        ImapMailboxEntry(flags=frozenset({"\\Trash"}), delimiter="/", name="Trash"),
    ]
    mailboxes = {m["name"]: m for m in imap_list_to_jmap_mailboxes(entries)}
    assert mailboxes["Archive"]["role"] == "archive"
    assert mailboxes["Trash"]["role"] == "trash"


def test_nested_mailbox_gets_parent_id():
    entries = [
        ImapMailboxEntry(flags=frozenset(), delimiter="/", name="INBOX"),
        ImapMailboxEntry(flags=frozenset(), delimiter="/", name="INBOX/Work"),
    ]
    mailboxes = {m["name"]: m for m in imap_list_to_jmap_mailboxes(entries)}
    assert mailboxes["Work"]["parentId"] == encode_mailbox_id("INBOX")


def test_orphan_parent_is_none_when_parent_not_listed():
    entries = [ImapMailboxEntry(flags=frozenset(), delimiter="/", name="INBOX/Work")]
    mailboxes = imap_list_to_jmap_mailboxes(entries)
    assert mailboxes[0]["parentId"] is None


def test_noselect_mailbox_skipped():
    entries = [
        ImapMailboxEntry(flags=frozenset({"\\Noselect"}), delimiter="/", name="[Gmail]"),
        ImapMailboxEntry(flags=frozenset(), delimiter="/", name="[Gmail]/All Mail"),
    ]
    mailboxes = imap_list_to_jmap_mailboxes(entries)
    names = [m["name"] for m in mailboxes]
    assert "[Gmail]" not in names
    assert "All Mail" in names


def test_unrelated_role_none_defaults_sort_order():
    entries = [ImapMailboxEntry(flags=frozenset(), delimiter="/", name="Personal")]
    mailboxes = imap_list_to_jmap_mailboxes(entries)
    assert mailboxes[0]["role"] is None
    assert mailboxes[0]["sortOrder"] == 1000
