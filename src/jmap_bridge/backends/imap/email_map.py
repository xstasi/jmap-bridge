"""RFC 822 <-> JMAP Email (RFC 8621 SS4.1) mapping.

Covers the MVP-critical Email properties: id/blobId, mailboxIds, keywords,
size, receivedAt/sentAt, subject, address headers, messageId/inReplyTo/
references, preview, bodyValues/textBody/htmlBody, attachments,
hasAttachment, bodyStructure. `_iter_leaf_parts` is the single canonical
part-numbering scheme shared by all of those - a blobId minted from any
of them (textBody, an attachment, a bodyStructure leaf) refers to the
same physical MIME part. Deferred: per-header `header:X` properties,
S/MIME.
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import json
from datetime import datetime, timezone
from email.message import EmailMessage

_KEYWORD_TO_FLAG = {
    "$seen": "\\Seen",
    "$flagged": "\\Flagged",
    "$answered": "\\Answered",
    "$draft": "\\Draft",
}
_FLAG_TO_KEYWORD = {v.upper(): k for k, v in _KEYWORD_TO_FLAG.items()}
_HIDDEN_FLAGS = {"\\RECENT", "\\DELETED"}

_ADDRESS_HEADERS = {
    "from": "From",
    "to": "To",
    "cc": "Cc",
    "bcc": "Bcc",
    "replyTo": "Reply-To",
    "sender": "Sender",
}

_PREVIEW_MAX_LEN = 256


def flags_to_keywords(flags: frozenset[str]) -> dict[str, bool]:
    keywords: dict[str, bool] = {}
    for flag in flags:
        if flag.upper() in _HIDDEN_FLAGS:
            continue
        keyword = _FLAG_TO_KEYWORD.get(flag.upper(), flag)
        keywords[keyword] = True
    return keywords


def keywords_to_flags(keywords: dict[str, bool]) -> list[str]:
    return [_KEYWORD_TO_FLAG.get(k, k) for k, v in keywords.items() if v]


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_blob_id(mailbox: str, uidvalidity: int, uid: int, part_index: int) -> str:
    raw = json.dumps(
        [mailbox, uidvalidity, uid, part_index], separators=(",", ":")
    ).encode("utf-8")
    return "B" + _b64url_encode(raw)


def decode_blob_id(blob_id: str) -> tuple[str, int, int, int]:
    if not blob_id.startswith("B"):
        raise ValueError(f"not an Email blob id: {blob_id!r}")
    try:
        mailbox, uidvalidity, uid, part_index = json.loads(_b64url_decode(blob_id[1:]))
        return str(mailbox), int(uidvalidity), int(uid), int(part_index)
    except Exception as exc:
        raise ValueError(f"malformed blob id {blob_id!r}: {exc}") from exc


def _addresses_from_header(msg: EmailMessage, header_name: str) -> list[dict] | None:
    values = msg.get_all(header_name)
    if not values:
        return None
    parsed = email.utils.getaddresses(values)
    return [{"name": name or None, "email": addr} for name, addr in parsed if addr]


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_message_ids(raw_header_value: str | None) -> list[str] | None:
    if not raw_header_value:
        return None
    import re

    ids = re.findall(r"<([^<>]+)>", raw_header_value)
    return [f"<{i}>" for i in ids] or None


def _iter_leaf_parts(msg: EmailMessage) -> list[tuple[str, EmailMessage]]:
    """Depth-first walk assigning every leaf (non-multipart) part a
    stable sequential partId ("1", "2", ...). This is the single
    canonical numbering used everywhere a part needs identifying -
    bodyValues/textBody/htmlBody/attachments partIds, blobId part
    indices, and bodyStructure's leaf partIds are all the same number
    for the same physical part, so a blobId minted from any of them
    resolves to the right bytes via `extract_blob_part`.
    """
    leaves: list[tuple[str, EmailMessage]] = []
    counter = [0]

    def walk(part: EmailMessage) -> None:
        if part.is_multipart():
            for sub in part.get_payload():
                walk(sub)
        else:
            counter[0] += 1
            leaves.append((str(counter[0]), part))

    walk(msg)
    return leaves


def _classify_leaves(
    leaves: list[tuple[str, EmailMessage]],
) -> tuple[str | None, str | None, str | None, str | None, list[tuple[str, EmailMessage]]]:
    """Returns (plain_text, plain_part_id, html_text, html_part_id,
    attachment_entries) - the same classification `build_jmap_email`
    always used (first non-attachment text/plain is the text body, first
    non-attachment text/html is the html body, everything else -
    including a parse failure on what looked like a body part - is an
    attachment), now tracking each part's canonical partId too.
    """
    plain_text: str | None = None
    plain_part_id: str | None = None
    html_text: str | None = None
    html_part_id: str | None = None
    attachments: list[tuple[str, EmailMessage]] = []

    for part_id, part in leaves:
        disposition = (part.get_content_disposition() or "").lower()
        content_type = part.get_content_type()
        if disposition == "attachment":
            attachments.append((part_id, part))
        elif content_type == "text/plain" and plain_text is None:
            try:
                plain_text = part.get_content()
                plain_part_id = part_id
            except Exception:
                attachments.append((part_id, part))
        elif content_type == "text/html" and html_text is None:
            try:
                html_text = part.get_content()
                html_part_id = part_id
            except Exception:
                attachments.append((part_id, part))
        elif content_type not in ("text/plain", "text/html"):
            attachments.append((part_id, part))

    return plain_text, plain_part_id, html_text, html_part_id, attachments


def _part_headers(part: EmailMessage) -> list[dict]:
    return [{"name": name, "value": str(value)} for name, value in part.items()]


def _strip_angle_brackets(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lstrip("<").rstrip(">") or None


def _build_body_structure(
    part: EmailMessage, part_id_by_object: dict[int, str], mailbox: str, uidvalidity: int, uid: int
) -> dict:
    """The full MIME tree for Email.bodyStructure (RFC 8621 SS4.1.4) -
    unlike textBody/htmlBody/attachments (a flattened, purpose-classified
    view), this is required to always be present and mirrors the
    message's real part nesting. A client that requests this property and
    gets nothing back for it is operating on a nil/missing value it
    wasn't expecting - exactly the crash this function exists to prevent.
    """
    content_type = part.get_content_type()
    node: dict = {
        "partId": None,
        "blobId": None,
        "size": 0,
        "headers": _part_headers(part),
        "name": part.get_filename(),
        "type": content_type,
        "charset": part.get_content_charset(),
        "disposition": part.get_content_disposition(),
        "cid": _strip_angle_brackets(part.get("Content-Id")),
        "language": None,
        "location": part.get("Content-Location"),
        "subParts": None,
    }
    content_language = part.get_all("Content-Language")
    if content_language:
        node["language"] = [lang.strip() for value in content_language for lang in value.split(",")]

    if part.is_multipart():
        node["subParts"] = [
            _build_body_structure(sub, part_id_by_object, mailbox, uidvalidity, uid)
            for sub in part.get_payload()
        ]
    else:
        part_id = part_id_by_object[id(part)]
        payload = part.get_payload(decode=True) or b""
        node["partId"] = part_id
        node["blobId"] = encode_blob_id(mailbox, uidvalidity, uid, int(part_id))
        node["size"] = len(payload)
    return node


def _preview_from_text(plain_text: str | None) -> str:
    if not plain_text:
        return ""
    collapsed = " ".join(plain_text.split())
    return collapsed[:_PREVIEW_MAX_LEN]


def extract_blob_part(raw_message: bytes, part_index: int) -> tuple[bytes, str] | None:
    """Resolve one blobId's `part_index` (see encode_blob_id) back to raw
    bytes + content type, for the /download endpoint. `part_index == -1`
    means the whole message (Email.blobId); a non-negative index is a
    leaf part's canonical number from `_iter_leaf_parts` - re-walking is
    required since we keep no separate part storage.
    """
    if part_index == -1:
        return raw_message, "message/rfc822"
    msg: EmailMessage = email.message_from_bytes(raw_message, policy=email.policy.default)
    leaves = _iter_leaf_parts(msg)
    target = str(part_index)
    for part_id, part in leaves:
        if part_id != target:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        return payload, part.get_content_type()
    return None


def _derive_thread_id(
    *,
    references: list[str] | None,
    in_reply_to: list[str] | None,
    message_id: list[str] | None,
    fallback: str,
) -> str:
    """Deterministic threadId with no stored thread table: the root
    Message-Id of the conversation (first References entry, else
    In-Reply-To, else the message's own Message-Id) identifies the thread,
    since RFC 5322 conventions keep References append-only. Messages that
    share a root produce the same threadId purely from their headers.
    """
    root = None
    if references:
        root = references[0]
    elif in_reply_to:
        root = in_reply_to[0]
    elif message_id:
        root = message_id[0]
    basis = root if root is not None else fallback
    return "T" + _b64url_encode(basis.encode("utf-8"))[:40]


def derive_thread_id_from_headers(raw_headers: bytes, fallback: str) -> str:
    """Same threadId derivation as `build_jmap_email`, but from just the
    header block (e.g. an IMAP RFC822.HEADER fetch) rather than a full
    message - used by Thread/get, which needs to scan many messages
    cheaply without pulling full bodies.
    """
    msg = email.message_from_bytes(raw_headers, policy=email.policy.default)
    return _derive_thread_id(
        references=_parse_message_ids(msg.get("References")),
        in_reply_to=_parse_message_ids(msg.get("In-Reply-To")),
        message_id=_parse_message_ids(msg.get("Message-Id")),
        fallback=fallback,
    )


def build_jmap_email(
    *,
    raw_message: bytes,
    email_id: str,
    mailbox_ids: dict[str, bool],
    flags: frozenset[str],
    internaldate: datetime | None,
    mailbox: str,
    uidvalidity: int,
    uid: int,
) -> dict:
    msg: EmailMessage = email.message_from_bytes(raw_message, policy=email.policy.default)

    leaves = _iter_leaf_parts(msg)
    plain_text, plain_part_id, html_text, html_part_id, attachment_entries = _classify_leaves(leaves)

    body_values: dict[str, dict] = {}
    text_body = []
    html_body = []
    if plain_text is not None:
        body_values[plain_part_id] = {
            "value": plain_text, "isEncodingProblem": False, "isTruncated": False
        }
        text_body.append({"partId": plain_part_id, "type": "text/plain"})
    if html_text is not None:
        body_values[html_part_id] = {
            "value": html_text, "isEncodingProblem": False, "isTruncated": False
        }
        html_body.append({"partId": html_part_id, "type": "text/html"})
    # JMAP allows an htmlBody part to double as textBody (and vice versa)
    # when only one representation exists (RFC 8621 SS4.1.4) - the client
    # is responsible for rendering accordingly.
    if not text_body and html_body:
        text_body = list(html_body)
    if not html_body and text_body:
        html_body = list(text_body)

    attachments = []
    for part_id, part in attachment_entries:
        attachments.append(
            {
                "partId": part_id,
                "blobId": encode_blob_id(mailbox, uidvalidity, uid, int(part_id)),
                "size": len(part.get_payload(decode=True) or b""),
                "name": part.get_filename(),
                "type": part.get_content_type(),
                "disposition": part.get_content_disposition() or "attachment",
            }
        )

    part_id_by_object = {id(part): part_id for part_id, part in leaves}
    body_structure = _build_body_structure(msg, part_id_by_object, mailbox, uidvalidity, uid)

    date_header = msg.get("Date")
    sent_at = None
    if date_header:
        parsed_date = email.utils.parsedate_to_datetime(date_header)
        if parsed_date is not None:
            sent_at = _to_utc_iso(parsed_date)

    message_id = _parse_message_ids(msg.get("Message-Id"))
    in_reply_to = _parse_message_ids(msg.get("In-Reply-To"))
    references = _parse_message_ids(msg.get("References"))
    thread_id = _derive_thread_id(
        references=references,
        in_reply_to=in_reply_to,
        message_id=message_id,
        fallback=f"{mailbox}:{uidvalidity}:{uid}",
    )

    result: dict = {
        "id": email_id,
        "blobId": encode_blob_id(mailbox, uidvalidity, uid, -1),
        "threadId": thread_id,
        "mailboxIds": mailbox_ids,
        "keywords": flags_to_keywords(flags),
        "size": len(raw_message),
        "receivedAt": _to_utc_iso(internaldate) if internaldate else None,
        "sentAt": sent_at,
        "subject": msg.get("Subject"),
        "messageId": message_id,
        "inReplyTo": in_reply_to,
        "references": references,
        "preview": _preview_from_text(plain_text),
        "hasAttachment": len(attachments) > 0,
        "bodyValues": body_values,
        "textBody": text_body,
        "htmlBody": html_body,
        "attachments": attachments,
        "bodyStructure": body_structure,
    }
    for jmap_prop, header_name in _ADDRESS_HEADERS.items():
        result[jmap_prop] = _addresses_from_header(msg, header_name)
    return result
