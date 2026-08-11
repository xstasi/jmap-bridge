from datetime import datetime, timezone

from imapclient.response_types import BodyData

from jmap_bridge.backends.imap.email_map import (
    build_jmap_email,
    build_jmap_email_headers_only,
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


def test_decode_thread_id_round_trips_header_based_id():
    """Thread/get relies on this to recover the original header value and
    run a targeted IMAP SEARCH instead of scanning every message in the
    account - a real correctness dependency now, not just cosmetic."""
    from jmap_bridge.backends.imap.email_map import decode_thread_id

    result = build_jmap_email(
        raw_message=SIMPLE_MESSAGE, email_id="E1", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="INBOX", uidvalidity=1, uid=1,
    )
    decoded = decode_thread_id(result["threadId"])
    assert decoded == ("H", "<root@example.com>")  # SIMPLE_MESSAGE's References[0]


def test_decode_thread_id_round_trips_location_based_id():
    """A message with none of References/In-Reply-To/Message-Id falls
    back to a "TL" (location) id naming it directly - must decode back
    to the exact (mailbox, uidvalidity, uid) it was built from."""
    from jmap_bridge.backends.imap.email_map import decode_thread_id

    no_headers_message = b"Subject: no thread headers at all\r\n\r\nbody\r\n"
    result = build_jmap_email(
        raw_message=no_headers_message, email_id="E1", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="Archive", uidvalidity=42, uid=7,
    )
    assert decode_thread_id(result["threadId"]) == ("L", "Archive:42:7")


def test_decode_thread_id_survives_long_message_ids_without_truncation():
    """Regression test: the old encoding truncated to 40 base64 chars, a
    latent hash-collision bug where two unrelated messages whose root ids
    shared the same ~30-byte prefix would silently merge into one
    thread. A long, realistic Message-Id must round-trip exactly."""
    from jmap_bridge.backends.imap.email_map import decode_thread_id

    long_id = "<CAEXTREMELYLONGMESSAGEIDSTRINGTHATWOULDHAVEBEENTRUNCATEDBEFORE.this.part.matters.too@mail.example.com>"
    msg = SIMPLE_MESSAGE.replace(b"<root@example.com>", long_id.encode())
    result = build_jmap_email(
        raw_message=msg, email_id="E1", mailbox_ids={}, flags=frozenset(),
        internaldate=None, mailbox="INBOX", uidvalidity=1, uid=1,
    )
    assert decode_thread_id(result["threadId"]) == ("H", long_id)


def test_decode_thread_id_rejects_malformed_ids():
    from jmap_bridge.backends.imap.email_map import decode_thread_id

    assert decode_thread_id("") is None
    assert decode_thread_id("garbage") is None
    assert decode_thread_id("Tsomething") is None  # old (pre-fix) format: no H/L kind marker
    assert decode_thread_id("TX invalid kind") is None
    assert decode_thread_id("TH!!!not-base64!!!") is None


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


MULTIPART_HEADERS_ONLY = b"""\
From: Alice Example <alice@example.com>
To: Bob Example <bob@example.com>
Subject: With attachment
Date: Mon, 1 Jan 2024 12:00:00 +0000
Message-Id: <msg2@example.com>
Content-Type: multipart/mixed; boundary="BOUNDARY"

"""

# Same shape confirmed live against real Dovecot (see the spike this was
# built from) for a plain+html alternative wrapped in mixed with a base64
# pdf attachment - matches MULTIPART_MESSAGE's actual structure. Built via
# direct BodyData(...) construction (the post-parse shape imapclient hands
# to real code), not BodyData.create() - create() expects the raw,
# not-yet-grouped wire tuple, which is a different, easier-to-get-wrong
# shape not worth reproducing here.
_PLAIN_LEAF = BodyData((b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 15, 1, None, None, None, None))
_HTML_LEAF = BodyData((b"text", b"html", (b"charset", b"utf-8"), None, None, b"7bit", 21, 1, None, None, None, None))
_ALTERNATIVE = BodyData(([_PLAIN_LEAF, _HTML_LEAF], b"alternative", (b"boundary", b"ALT"), None, None, None))
_PDF_LEAF = BodyData(
    (b"application", b"pdf", None, None, None, b"base64", 12, None,
     (b"attachment", (b"filename", b"doc.pdf")), None, None)
)
MULTIPART_BODYSTRUCTURE = BodyData(
    ([_ALTERNATIVE, _PDF_LEAF], b"mixed", (b"boundary", b"BOUNDARY"), None, None, None)
)


def test_build_jmap_email_headers_only_multipart_with_attachment():
    result = build_jmap_email_headers_only(
        header_bytes=MULTIPART_HEADERS_ONLY,
        size=999,
        bodystructure=MULTIPART_BODYSTRUCTURE,
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
    assert attachment["partId"] == "3"  # 3rd leaf: plain(1), html(2), pdf(3) - same numbering as the full fetch
    assert result["textBody"] == [{"partId": "1", "type": "text/plain"}]
    assert result["htmlBody"] == [{"partId": "2", "type": "text/html"}]
    # The whole point: no body content, ever.
    assert result["bodyValues"] == {}
    assert result["preview"] == ""
    assert result["size"] == 999  # from RFC822.SIZE, not len(raw_message) - no raw_message here

    structure = result["bodyStructure"]
    assert structure["type"] == "multipart/mixed"
    assert structure["partId"] is None
    alternative, pdf_part = structure["subParts"]
    assert alternative["type"] == "multipart/alternative"
    assert [p["type"] for p in alternative["subParts"]] == ["text/plain", "text/html"]
    assert pdf_part["partId"] == "3"
    assert pdf_part["blobId"] == attachment["blobId"]


def test_build_jmap_email_headers_only_blob_ids_match_full_fetch():
    """The critical safety property: a blobId minted by the lightweight
    path must resolve to the same physical part as the full-fetch path,
    since a later /download always re-walks the full message the
    ordinary way (extract_blob_part -> _iter_leaf_parts)."""
    full = build_jmap_email(
        raw_message=MULTIPART_MESSAGE,
        email_id="E2", mailbox_ids={}, flags=frozenset(), internaldate=None,
        mailbox="INBOX", uidvalidity=1, uid=43,
    )
    light = build_jmap_email_headers_only(
        header_bytes=MULTIPART_HEADERS_ONLY,
        size=999,
        bodystructure=MULTIPART_BODYSTRUCTURE,
        email_id="E2", mailbox_ids={}, flags=frozenset(), internaldate=None,
        mailbox="INBOX", uidvalidity=1, uid=43,
    )
    assert light["attachments"][0]["blobId"] == full["attachments"][0]["blobId"]
    assert light["textBody"][0]["partId"] == full["textBody"][0]["partId"]
    assert light["htmlBody"][0]["partId"] == full["htmlBody"][0]["partId"]
    assert light["blobId"] == full["blobId"]  # whole-message blobId (part_index -1)


def test_build_jmap_email_headers_only_message_rfc822_attachment():
    """RFC 3501's body-type-msg has two extra fields (envelope, nested
    BODYSTRUCTURE) before "lines" that body-type-text doesn't - confirmed
    live against Dovecot. Must not misindex extension fields, and must
    treat the embedded message as a one-child container (matching
    _iter_leaf_parts's Python-email-module-based walk) rather than
    assigning the message/rfc822 wrapper itself a partId.
    """
    # Built via direct BodyData(...) construction at every level (the
    # message/rfc822 child is itself pre-wrapped as BodyData by a real
    # parse, even though it isn't multipart) - only the doubly-nested
    # field at index 8 (the embedded message's own BODYSTRUCTURE) is left
    # as a bare tuple, matching what was confirmed live against Dovecot
    # (imapclient does not recurse BodyData-wrapping into that field).
    outer_plain_leaf = BodyData(
        (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", 14, 1, None, None, None, None)
    )
    rfc822_leaf = BodyData(
        (
            b"message",
            b"rfc822",
            None,
            None,
            None,
            b"base64",
            242,
            (None,) * 10,  # envelope - opaque here, not parsed
            (b"text", b"plain", (b"charset", b"us-ascii"), None, None, b"7bit", 0, 0, None, None, None, None),
            5,
            None,
            (b"attachment", (b"filename", b"fwd.eml")),
            None,
            None,
        )
    )
    bodystructure = BodyData(
        ([outer_plain_leaf, rfc822_leaf], b"mixed", (b"boundary", b"X"), None, None, None)
    )
    result = build_jmap_email_headers_only(
        header_bytes=b"Subject: fwd\nContent-Type: multipart/mixed; boundary=X\n\n",
        size=500,
        bodystructure=bodystructure,
        email_id="E3", mailbox_ids={}, flags=frozenset(), internaldate=None,
        mailbox="INBOX", uidvalidity=1, uid=7,
    )
    structure = result["bodyStructure"]
    plain, rfc822_part = structure["subParts"]
    assert plain["partId"] == "1"
    assert rfc822_part["partId"] is None  # container, not a leaf itself
    assert rfc822_part["type"] == "message/rfc822"
    assert rfc822_part["name"] == "fwd.eml"
    assert rfc822_part["disposition"] == "attachment"
    inner = rfc822_part["subParts"][0]
    assert inner["partId"] == "2"  # the embedded message's own leaf, not the wrapper
    # Pre-existing behavior (matches _classify_leaves/build_jmap_email
    # exactly, not something this path introduces): message/rfc822 is
    # never a leaf, so it's never in `attachments` - only bodyStructure
    # shows it, as a container. Its inner plain-text leaf also isn't
    # classified as anything here, since the outer message's own plain
    # part already claimed the "first text/plain" slot.
    assert result["attachments"] == []


def test_decoded_size_estimate_base64_approximates_decoded_size():
    from jmap_bridge.backends.imap.email_map import _decoded_size_estimate

    # 28 encoded chars (no line-wrap CRLF in play at this size) should
    # land close to 21 decoded bytes - exact base64 ratio is 4:3.
    assert _decoded_size_estimate(28, "base64") == 21
    assert _decoded_size_estimate(28, "BASE64") == 21  # case-insensitive


def test_decoded_size_estimate_passes_through_untransformed_encodings():
    from jmap_bridge.backends.imap.email_map import _decoded_size_estimate

    assert _decoded_size_estimate(100, "7bit") == 100
    assert _decoded_size_estimate(100, "8bit") == 100
    assert _decoded_size_estimate(100, "binary") == 100
    assert _decoded_size_estimate(100, None) == 100
