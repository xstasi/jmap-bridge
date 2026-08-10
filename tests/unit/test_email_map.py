from datetime import datetime, timezone

from jmap_bridge.backends.imap.email_map import (
    build_jmap_email,
    decode_blob_id,
    encode_blob_id,
    extract_blob_part,
    flags_to_keywords,
    keywords_to_flags,
)

SIMPLE_MESSAGE = b"""\
From: Alice Example <alice@example.com>
To: Bob Example <bob@example.com>
Cc: carol@example.com
Subject: Hello there
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <msg1@example.com>
In-Reply-To: <parent@example.com>
References: <root@example.com> <parent@example.com>
Content-Type: text/plain; charset=utf-8

This is the plain text body.
"""

MULTIPART_MESSAGE = b"""\
From: Alice Example <alice@example.com>
To: Bob Example <bob@example.com>
Subject: With attachment
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <msg2@example.com>
Content-Type: multipart/mixed; boundary="BOUNDARY"

--BOUNDARY
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset=utf-8

Plain version.
--ALT
Content-Type: text/html; charset=utf-8

<p>HTML version.</p>
--ALT--
--BOUNDARY
Content-Type: application/pdf
Content-Disposition: attachment; filename="doc.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--BOUNDARY--
"""


def test_flags_to_keywords_maps_system_flags():
    keywords = flags_to_keywords(frozenset({"\\Seen", "\\Flagged", "\\Recent"}))
    assert keywords == {"$seen": True, "$flagged": True}


def test_flags_to_keywords_passes_through_custom_keywords():
    keywords = flags_to_keywords(frozenset({"CustomTag"}))
    assert keywords == {"CustomTag": True}


def test_keywords_to_flags_round_trip():
    keywords = {"$seen": True, "$flagged": True, "CustomTag": True, "$draft": False}
    flags = set(keywords_to_flags(keywords))
    assert flags == {"\\Seen", "\\Flagged", "CustomTag"}


def test_blob_id_round_trip():
    blob_id = encode_blob_id("INBOX", 123, 456, 2)
    assert decode_blob_id(blob_id) == ("INBOX", 123, 456, 2)


def test_build_jmap_email_simple_message():
    result = build_jmap_email(
        raw_message=SIMPLE_MESSAGE,
        email_id="E1",
        mailbox_ids={"Mabc": True},
        flags=frozenset({"\\Seen"}),
        internaldate=datetime(2024, 1, 1, 12, 30, tzinfo=timezone.utc),
        mailbox="INBOX",
        uidvalidity=1,
        uid=42,
    )
    assert result["subject"] == "Hello there"
    assert result["from"] == [{"name": "Alice Example", "email": "alice@example.com"}]
    assert result["to"] == [{"name": "Bob Example", "email": "bob@example.com"}]
    assert result["cc"] == [{"name": None, "email": "carol@example.com"}]
    assert result["messageId"] == ["<msg1@example.com>"]
    assert result["inReplyTo"] == ["<parent@example.com>"]
    assert result["references"] == ["<root@example.com>", "<parent@example.com>"]
    assert result["threadId"].startswith("T")
    assert result["keywords"] == {"$seen": True}
    assert result["receivedAt"] == "2024-01-01T12:30:00Z"
    assert result["sentAt"] == "2024-01-01T12:00:00Z"
    assert result["preview"] == "This is the plain text body."
    assert result["hasAttachment"] is False
    assert result["bodyValues"]["1"]["value"].strip() == "This is the plain text body."
    assert result["textBody"] == [{"partId": "1", "type": "text/plain"}]
    assert result["htmlBody"] == [{"partId": "1", "type": "text/plain"}]
    assert result["size"] == len(SIMPLE_MESSAGE)

    # bodyStructure: single-leaf message, no subParts.
    structure = result["bodyStructure"]
    assert structure["partId"] == "1"
    assert structure["type"] == "text/plain"
    assert structure["subParts"] is None
    assert structure["blobId"]


def test_build_jmap_email_multipart_with_attachment():
    result = build_jmap_email(
        raw_message=MULTIPART_MESSAGE,
        email_id="E2",
        mailbox_ids={"Mabc": True},
        flags=frozenset(),
        internaldate=None,
        mailbox="INBOX",
        uidvalidity=1,
        uid=43,
    )
    assert result["hasAttachment"] is True
    assert len(result["attachments"]) == 1
    attachment = result["attachments"][0]
    assert attachment["name"] == "doc.pdf"
    assert attachment["type"] == "application/pdf"
    assert attachment["partId"] == "3"  # 3rd leaf: plain(1), html(2), pdf(3)
    assert result["bodyValues"]["1"]["value"].strip() == "Plain version."
    assert result["bodyValues"]["2"]["value"].strip() == "<p>HTML version.</p>"
    assert result["textBody"] == [{"partId": "1", "type": "text/plain"}]
    assert result["htmlBody"] == [{"partId": "2", "type": "text/html"}]
    assert result["receivedAt"] is None

    # bodyStructure: multipart/mixed > [multipart/alternative > [plain, html], pdf]
    structure = result["bodyStructure"]
    assert structure["type"] == "multipart/mixed"
    assert structure["partId"] is None
    assert len(structure["subParts"]) == 2
    alternative, pdf_part = structure["subParts"]
    assert alternative["type"] == "multipart/alternative"
    assert [p["type"] for p in alternative["subParts"]] == ["text/plain", "text/html"]
    assert pdf_part["partId"] == "3"
    assert pdf_part["type"] == "application/pdf"
    assert pdf_part["blobId"] == attachment["blobId"]


def test_messages_sharing_a_reference_root_share_a_thread_id():
    reply = SIMPLE_MESSAGE  # References: <root@example.com> <parent@example.com>
    other_reply = SIMPLE_MESSAGE.replace(
        b"Message-Id: <msg1@example.com>", b"Message-Id: <msg-other@example.com>"
    )
    r1 = build_jmap_email(
        raw_message=reply, email_id="E1", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="INBOX", uidvalidity=1, uid=1,
    )
    r2 = build_jmap_email(
        raw_message=other_reply, email_id="E2", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="INBOX", uidvalidity=1, uid=2,
    )
    assert r1["threadId"] == r2["threadId"]


def test_unrelated_messages_get_different_thread_ids():
    r1 = build_jmap_email(
        raw_message=SIMPLE_MESSAGE, email_id="E1", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="INBOX", uidvalidity=1, uid=1,
    )
    r2 = build_jmap_email(
        raw_message=MULTIPART_MESSAGE, email_id="E2", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="INBOX", uidvalidity=1, uid=2,
    )
    assert r1["threadId"] != r2["threadId"]


def test_extract_blob_part_whole_message():
    data, content_type = extract_blob_part(SIMPLE_MESSAGE, -1)
    assert data == SIMPLE_MESSAGE
    assert content_type == "message/rfc822"


def test_extract_blob_part_attachment():
    data, content_type = extract_blob_part(MULTIPART_MESSAGE, 3)  # 3rd leaf: plain, html, pdf
    assert content_type == "application/pdf"
    assert data.startswith(b"%PDF")


def test_extract_blob_part_out_of_range():
    assert extract_blob_part(SIMPLE_MESSAGE, 5) is None
