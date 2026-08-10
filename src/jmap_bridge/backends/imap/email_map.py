"""RFC 822 <-> JMAP Email (RFC 8621 SS4.1) mapping.

Covers the MVP-critical Email properties: id/blobId, mailboxIds, keywords,
size, receivedAt/sentAt, subject, headers, address headers, messageId/
inReplyTo/references, preview, bodyValues/textBody/htmlBody, attachments,
hasAttachment, bodyStructure. `_iter_leaf_parts` is the single canonical
part-numbering scheme shared by all of those - a blobId minted from any
of them (textBody, an attachment, a bodyStructure leaf) refers to the
same physical MIME part. Deferred: per-header `header:X` property forms,
S/MIME.

Two ways to build the Email object:

- `build_jmap_email` - from a full RFC822 fetch, needed only when actual
  body content is requested (`preview`, or `bodyValues` when a
  fetchTextBodyValues/fetchHTMLBodyValues/fetchAllBodyValues flag is set).
- `build_jmap_email_headers_only` - from just a header block + IMAP's own
  BODYSTRUCTURE fetch item, no body/attachment bytes ever downloaded.
  Confirmed live against aerc's real request shape (ref/aerc/worker/jmap/
  fetch.go): its list view asks for `bodyStructure` but never body
  content, and even opening a message fetches `bodyStructure` first, then
  downloads exactly one part's bytes via a separate `/download` call -
  Email/get is never the path real content flows through. Before this,
  every Email/get call downloaded the entire message (headers, body, all
  attachments) regardless of what was actually requested, then threw it
  away - the bigger the mailbox, the more that cost compounded per page.
  `types/email.py` decides which path to use per call.

  `_walk_native_bodystructure` mirrors `_iter_leaf_parts`'s exact
  depth-first leaf-counting order so a blobId minted via either path
  resolves to the same bytes through `extract_blob_part` (which always
  re-walks a full message the ordinary way) - confirmed live against
  Dovecot, including the message/rfc822 edge case (RFC 3501's
  body-type-msg embeds two extra fields - envelope, nested body structure
  - before "lines" that body-type-text doesn't have; `_iter_leaf_parts`
  treats message/rfc822 as a one-child container via Python's email
  module, and this path matches that exactly rather than misindexing
  extension fields or assigning it a spurious partId of its own).
  Per-part `headers` is left `[]` in this path (not `None`, to keep the
  same shape as the full-fetch node) - neither aerc's bodyProperties nor
  RFC 8621's own EmailBodyPart default includes it, and this bridge
  doesn't support the bodyProperties argument's per-part filtering at all
  yet (pre-existing gap, not introduced by this path).
"""

from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import json
from datetime import datetime, timezone
from email.message import EmailMessage

from imapclient.response_types import BodyData

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


def _headers_and_envelope_fields(msg: EmailMessage, mailbox: str, uidvalidity: int, uid: int) -> dict:
    """Everything derivable from headers alone, shared between
    `build_jmap_email` (full fetch) and `build_jmap_email_headers_only`
    (lightweight fetch) so the two paths can't silently drift apart.
    """
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
    fields = {
        "threadId": thread_id,
        "sentAt": sent_at,
        "subject": msg.get("Subject"),
        "headers": _part_headers(msg),
        "messageId": message_id,
        "inReplyTo": in_reply_to,
        "references": references,
    }
    for jmap_prop, header_name in _ADDRESS_HEADERS.items():
        fields[jmap_prop] = _addresses_from_header(msg, header_name)
    return fields


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

    result: dict = {
        "id": email_id,
        "blobId": encode_blob_id(mailbox, uidvalidity, uid, -1),
        "mailboxIds": mailbox_ids,
        "keywords": flags_to_keywords(flags),
        "size": len(raw_message),
        "receivedAt": _to_utc_iso(internaldate) if internaldate else None,
        "preview": _preview_from_text(plain_text),
        "hasAttachment": len(attachments) > 0,
        "bodyValues": body_values,
        "textBody": text_body,
        "htmlBody": html_body,
        "attachments": attachments,
        "bodyStructure": body_structure,
    }
    result.update(_headers_and_envelope_fields(msg, mailbox, uidvalidity, uid))
    return result


def _bs_get(node: tuple, index: int):
    return node[index] if len(node) > index else None


def _bs_text(raw) -> str | None:
    if raw is None:
        return None
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def _bs_params(raw) -> dict[str, str] | None:
    """BODYSTRUCTURE param field: a flat (k, v, k, v, ...) tuple/list of
    bytes, or None - never nested key/value pairs."""
    if not raw:
        return None
    items = [_bs_text(v) for v in raw]
    return dict(zip(items[0::2], items[1::2]))


def _bs_language(raw) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, str)):
        return [_bs_text(raw)]
    return [_bs_text(v) for v in raw]


def _bs_disposition(raw) -> tuple[str | None, dict[str, str] | None]:
    if not raw:
        return None, None
    return _bs_text(_bs_get(raw, 0)), _bs_params(_bs_get(raw, 1))


def _decoded_size_estimate(raw_size: int, encoding: str | None) -> int:
    """Best-effort decoded size without downloading content. BODYSTRUCTURE
    only reports the on-wire (possibly transfer-encoded) octet count, but
    RFC 8621 SS4.1.4 requires EmailBodyPart.size to be the size *after*
    Content-Transfer-Encoding decoding - confirmed live: `build_jmap_email`
    (full fetch) already reports the decoded size via
    `part.get_payload(decode=True)`, and comparing the two paths for the
    same base64 attachment showed BODYSTRUCTURE's raw count is ~33% too
    high. Exact for 7bit/8bit/binary (no transformation). For base64,
    computed from the fixed 4-char-in/3-byte-out ratio - slightly
    overstates the true value since it doesn't subtract line-wrapping
    CRLF overhead (unknown from BODYSTRUCTURE alone), but is far closer
    than the raw count. Quoted-printable's ratio is content-dependent (no
    reliable formula without the actual bytes) - left as the raw on-wire
    size, which may overstate for content with many escaped bytes.
    """
    if (encoding or "").lower() == "base64":
        return (raw_size * 3) // 4
    return raw_size


def _walk_native_bodystructure(
    node: BodyData, counter: list[int], mailbox: str, uidvalidity: int, uid: int, leaves_out: list[dict]
) -> dict:
    """Builds the same shape `_build_body_structure` does, but from IMAP's
    own BODYSTRUCTURE fetch item instead of a fully-downloaded, re-parsed
    message - see this module's docstring for why, and for the
    message/rfc822 indexing this depends on (confirmed live against
    Dovecot, see the spike this was built from). `leaves_out` accumulates
    (part_id, type, disposition, size, name, cid, blobId) dicts for
    `_classify_native_leaves` to turn into textBody/htmlBody/attachments,
    mirroring `_classify_leaves`.
    """
    if node.is_multipart:
        children = node[0]
        subtype = _bs_text(_bs_get(node, 1))
        params = _bs_params(_bs_get(node, 2))
        disposition, disposition_params = _bs_disposition(_bs_get(node, 3))
        language = _bs_language(_bs_get(node, 4))
        location = _bs_text(_bs_get(node, 5))
        sub_nodes = [
            _walk_native_bodystructure(child, counter, mailbox, uidvalidity, uid, leaves_out)
            for child in children
        ]
        return {
            "partId": None,
            "blobId": None,
            "size": 0,
            "headers": [],
            "name": (disposition_params or {}).get("filename") or (params or {}).get("name"),
            "type": f"multipart/{subtype or 'mixed'}",
            "charset": (params or {}).get("charset"),
            "disposition": disposition,
            "cid": None,
            "language": language,
            "location": location,
            "subParts": sub_nodes,
        }

    type_raw = _bs_text(_bs_get(node, 0))
    subtype_raw = _bs_text(_bs_get(node, 1))
    content_type = f"{(type_raw or 'application').lower()}/{(subtype_raw or 'octet-stream').lower()}"
    params = _bs_params(_bs_get(node, 2))
    encoding = _bs_text(_bs_get(node, 5))
    size = _decoded_size_estimate(_bs_get(node, 6) or 0, encoding)

    if content_type == "message/rfc822" and isinstance(_bs_get(node, 8), tuple):
        # RFC 3501's body-type-msg embeds the forwarded message's own
        # envelope (index 7) and BODYSTRUCTURE (index 8) before "lines"
        # (index 9) - body-type-text only has "lines" (index 7). Extension
        # fields (md5/disposition/language/location) start right after,
        # at 10 here vs 8 for text/*. `_iter_leaf_parts` (the full-parse
        # path) treats message/rfc822 as a one-child container via
        # Python's email module, never a leaf - match that exactly, since
        # a blobId minted here must resolve via the same numbering
        # `extract_blob_part` uses when re-walking the full message.
        disposition, disposition_params = _bs_disposition(_bs_get(node, 11))
        language = _bs_language(_bs_get(node, 12))
        location = _bs_text(_bs_get(node, 13))
        child = _walk_native_bodystructure(
            BodyData.create(_bs_get(node, 8)), counter, mailbox, uidvalidity, uid, leaves_out
        )
        return {
            "partId": None,
            "blobId": None,
            "size": 0,
            "headers": [],
            "name": (disposition_params or {}).get("filename") or (params or {}).get("name"),
            "type": content_type,
            "charset": (params or {}).get("charset"),
            "disposition": disposition,
            "cid": None,
            "language": language,
            "location": location,
            "subParts": [child],
        }

    content_id = _strip_angle_brackets(_bs_text(_bs_get(node, 3)))
    # body-type-text has an extra "lines" field (index 7) that every other
    # leaf type lacks - extension fields start one slot later because of it.
    ext_start = 8 if (type_raw or "").lower() == "text" else 7
    disposition, disposition_params = _bs_disposition(_bs_get(node, ext_start + 1))
    language = _bs_language(_bs_get(node, ext_start + 2))
    location = _bs_text(_bs_get(node, ext_start + 3))

    counter[0] += 1
    part_id = str(counter[0])
    blob_id = encode_blob_id(mailbox, uidvalidity, uid, int(part_id))
    name = (disposition_params or {}).get("filename") or (params or {}).get("name")

    leaves_out.append(
        {
            "part_id": part_id,
            "type": content_type,
            "disposition": (disposition or "").lower(),
            "size": size,
            "name": name,
            "cid": content_id,
            "blob_id": blob_id,
        }
    )

    return {
        "partId": part_id,
        "blobId": blob_id,
        "size": size,
        "headers": [],
        "name": name,
        "type": content_type,
        "charset": (params or {}).get("charset"),
        "disposition": disposition,
        "cid": content_id,
        "language": language,
        "location": location,
        "subParts": None,
    }


def _classify_native_leaves(leaves: list[dict]) -> tuple[str | None, str | None, list[dict]]:
    """Same first-plain/first-html/rest-are-attachments classification as
    `_classify_leaves`, but from BODYSTRUCTURE-derived metadata - no body
    content is available in this path, so this returns partIds only,
    never text.
    """
    plain_part_id: str | None = None
    html_part_id: str | None = None
    attachments: list[dict] = []
    for leaf in leaves:
        if leaf["disposition"] == "attachment":
            attachments.append(leaf)
        elif leaf["type"] == "text/plain" and plain_part_id is None:
            plain_part_id = leaf["part_id"]
        elif leaf["type"] == "text/html" and html_part_id is None:
            html_part_id = leaf["part_id"]
        elif leaf["type"] not in ("text/plain", "text/html"):
            attachments.append(leaf)
    return plain_part_id, html_part_id, attachments


def build_jmap_email_headers_only(
    *,
    header_bytes: bytes,
    size: int,
    bodystructure: BodyData,
    email_id: str,
    mailbox_ids: dict[str, bool],
    flags: frozenset[str],
    internaldate: datetime | None,
    mailbox: str,
    uidvalidity: int,
    uid: int,
) -> dict:
    """Same Email object shape as `build_jmap_email`, but built from just
    a header block + IMAP's native BODYSTRUCTURE - no message body or
    attachment bytes are ever downloaded. See this module's docstring.

    `preview` and `bodyValues` need real body content, which this path
    never fetches: `preview` is always `""` and `bodyValues` is always
    `{}` - correct per RFC 8621 SS4.4 whenever fetchTextBodyValues/
    fetchHTMLBodyValues/fetchAllBodyValues are all false, since a caller
    that sets any of those takes the full-fetch path instead (see
    types/email.py's `_needs_full_body`).
    """
    msg = email.message_from_bytes(header_bytes, policy=email.policy.default)

    counter = [0]
    leaves: list[dict] = []
    body_structure = _walk_native_bodystructure(bodystructure, counter, mailbox, uidvalidity, uid, leaves)

    plain_part_id, html_part_id, attachment_leaves = _classify_native_leaves(leaves)
    text_body = [{"partId": plain_part_id, "type": "text/plain"}] if plain_part_id else []
    html_body = [{"partId": html_part_id, "type": "text/html"}] if html_part_id else []
    if not text_body and html_body:
        text_body = list(html_body)
    if not html_body and text_body:
        html_body = list(text_body)

    attachments = [
        {
            "partId": leaf["part_id"],
            "blobId": leaf["blob_id"],
            "size": leaf["size"],
            "name": leaf["name"],
            "type": leaf["type"],
            "disposition": leaf["disposition"] or "attachment",
        }
        for leaf in attachment_leaves
    ]

    result: dict = {
        "id": email_id,
        "blobId": encode_blob_id(mailbox, uidvalidity, uid, -1),
        "mailboxIds": mailbox_ids,
        "keywords": flags_to_keywords(flags),
        "size": size,
        "receivedAt": _to_utc_iso(internaldate) if internaldate else None,
        "preview": "",
        "hasAttachment": len(attachments) > 0,
        "bodyValues": {},
        "textBody": text_body,
        "htmlBody": html_body,
        "attachments": attachments,
        "bodyStructure": body_structure,
    }
    result.update(_headers_and_envelope_fields(msg, mailbox, uidvalidity, uid))
    return result
