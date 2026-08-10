import pytest

from jmap_bridge.backends.imap.modseq_state import (
    CannotCalculateChanges,
    MailboxCursor,
    decode_email_id,
    decode_mail_state,
    diff_mailbox_state,
    encode_email_id,
    encode_mail_state,
)


def test_mail_state_round_trip():
    cursors = {
        "INBOX": MailboxCursor(uidvalidity=100, highestmodseq=42),
        "Archive": MailboxCursor(uidvalidity=200, highestmodseq=7),
    }
    token = encode_mail_state(cursors)
    assert decode_mail_state(token) == cursors


def test_diff_detects_created_updated_destroyed():
    old = {
        "INBOX": MailboxCursor(1, 10),
        "Trash": MailboxCursor(1, 5),
    }
    new = {
        "INBOX": MailboxCursor(1, 15),  # updated
        "Archive": MailboxCursor(1, 1),  # created
        # Trash destroyed
    }
    diff = diff_mailbox_state(old, new)
    assert diff.created == ["Archive"]
    assert diff.updated == ["INBOX"]
    assert diff.destroyed == ["Trash"]


def test_diff_no_changes():
    cursors = {"INBOX": MailboxCursor(1, 10)}
    diff = diff_mailbox_state(cursors, dict(cursors))
    assert diff.created == diff.updated == diff.destroyed == []


def test_diff_raises_on_uidvalidity_rotation():
    old = {"INBOX": MailboxCursor(1, 10)}
    new = {"INBOX": MailboxCursor(2, 10)}
    with pytest.raises(CannotCalculateChanges):
        diff_mailbox_state(old, new)


def test_email_id_round_trip():
    email_id = encode_email_id("INBOX/Sub folder", 12345, 999)
    assert decode_email_id(email_id) == ("INBOX/Sub folder", 12345, 999)


def test_email_id_is_jmap_id_safe():
    import re

    email_id = encode_email_id("Some/Möbius Mailbox", 1, 1)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", email_id)


def test_decode_email_id_rejects_garbage():
    with pytest.raises(ValueError):
        decode_email_id("not-an-email-id")


def test_decode_email_id_rejects_wrong_prefix():
    with pytest.raises(ValueError):
        decode_email_id("Xabcdef")
