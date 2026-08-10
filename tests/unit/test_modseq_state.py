import pytest

from jmap_bridge.backends.imap.modseq_state import (
    CannotCalculateChanges,
    MailboxCursor,
    decode_email_id,
    decode_mail_state,
    diff_mailbox_state,
    encode_email_id,
    encode_mail_state,
    verify_no_missing_destroys,
)


def test_mail_state_round_trip():
    cursors = {
        "INBOX": MailboxCursor(uidvalidity=100, highestmodseq=42, uidnext=50, exists=49),
        "Archive": MailboxCursor(uidvalidity=200, highestmodseq=7, uidnext=10, exists=9),
    }
    token = encode_mail_state(cursors)
    assert decode_mail_state(token) == cursors


def test_diff_detects_created_updated_destroyed():
    old = {
        "INBOX": MailboxCursor(1, 10, uidnext=5, exists=4),
        "Trash": MailboxCursor(1, 5, uidnext=2, exists=1),
    }
    new = {
        "INBOX": MailboxCursor(1, 15, uidnext=6, exists=5),  # updated
        "Archive": MailboxCursor(1, 1, uidnext=1, exists=0),  # created
        # Trash destroyed
    }
    diff = diff_mailbox_state(old, new)
    assert diff.created == ["Archive"]
    assert diff.updated == ["INBOX"]
    assert diff.destroyed == ["Trash"]


def test_diff_no_changes():
    cursors = {"INBOX": MailboxCursor(1, 10, uidnext=5, exists=4)}
    diff = diff_mailbox_state(cursors, dict(cursors))
    assert diff.created == diff.updated == diff.destroyed == []


def test_diff_raises_on_uidvalidity_rotation():
    old = {"INBOX": MailboxCursor(1, 10, uidnext=5, exists=4)}
    new = {"INBOX": MailboxCursor(2, 10, uidnext=5, exists=4)}
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


def test_verify_no_missing_destroys_passes_when_counts_reconcile():
    old = MailboxCursor(uidvalidity=1, highestmodseq=10, uidnext=5, exists=4)
    new = MailboxCursor(uidvalidity=1, highestmodseq=12, uidnext=7, exists=6)
    verify_no_missing_destroys(old, new, created_count=2)  # 4 + 2 == 6, no raise


def test_verify_no_missing_destroys_raises_when_counts_dont_reconcile():
    old = MailboxCursor(uidvalidity=1, highestmodseq=10, uidnext=5, exists=4)
    new = MailboxCursor(uidvalidity=1, highestmodseq=12, uidnext=7, exists=5)
    # 2 new messages arrived (uidnext 5->7) but exists only went 4->5,
    # not 4->6 - one message must have been destroyed too.
    with pytest.raises(CannotCalculateChanges):
        verify_no_missing_destroys(old, new, created_count=2)


def test_verify_no_missing_destroys_passes_with_zero_created():
    old = MailboxCursor(uidvalidity=1, highestmodseq=10, uidnext=5, exists=4)
    new = MailboxCursor(uidvalidity=1, highestmodseq=10, uidnext=5, exists=4)
    verify_no_missing_destroys(old, new, created_count=0)
