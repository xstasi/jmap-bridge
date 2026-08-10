"""vCard (RFC 6350) <-> JMAP ContactCard (JSContact Card, RFC 9553)
mapping - non-group subset. Group support (`kind:"group"` + `members`,
vCard's `KIND:group`/`MEMBER:` convention) is added in a later pass, per
the Phase 3 plan.

ContactCard id = deterministic encoding of `(addressbook_href, card_href)`
(mirrors CalendarEvent id = encoding of `(calendar_href, event_href)`) -
decoding gives exactly what's needed to GET/PUT/DELETE the resource
directly, no lookup table for the common (never-moved) case. A moved
card (`addressBookIds` changed) would reuse id_redirect.py, the same
mechanism as CalendarEvent's calendarIds move (though `addressBookIds`
is capped at 1 here - see session.py's `maxAddressBooksPerCard`).

vCard construction: build via `vobject.vCard()` + `Component.add()`
calls throughout, never raw string templating, with an explicit
`VERSION:4.0` override (`vobject.vCard()` hardcodes 3.0 only if left
unset) - independently confirmed this escapes text values correctly
through the default serialize path even for vCard-4.0-only properties
with no registered behavior (KIND, MEMBER), and confirmed `VERSION:4.0`
is preserved (not rewritten) on a parse-then-reserialize of an existing
4.0 document. One real gotcha found by direct testing, not assumed:
`CATEGORIES`' behavior treats `.value` as an iterable of separate list
items - assigning a plain comma-joined *string* gets iterated character
by character (`"friend,vip"` -> escaped single characters); it needs an
actual `list[str]`. `NICKNAME` is the opposite - it wants a plain string
(RFC 6350 SS6.2.3's own comma-separated text-list convention), a list
crashes serialize(). Round-trip fidelity for *untouched* properties on
update: `vobject`'s parse->serialize does NOT preserve property order
(confirmed), so - same principle as Phase 2's event_map.py - mutate only
the touched properties on the parsed component in place, never rebuild
from scratch.

Structured `name`/`addresses` mapping follows RFC 9555 SS2.5.5/SS2.6.1's
canonical tables directly, not a re-derived mapping:
- N -> Name: Family->surname, Given->given, Additional->given2 (the N
  3rd slot maps *specifically* to given2, not a shared "additional
  names" bucket). `surname2` has no N slot of its own - on write, append
  its value onto Family *after* the surname value; `generation`
  similarly has no slot - append onto Honorific-suffix *after*
  credential. On read, values appended this way aren't distinguishable
  from a plain multi-word Family/suffix - this is a known, accepted
  lossy edge (see the plan) affecting only cards using `surname2`/
  `generation`, which are themselves uncommon (Spanish/Portuguese double
  surnames, generational suffixes).
- ADR -> Address (plain, non-RFC-9554-extended form only - v1 scope
  excludes the extended room/floor/building/block/subdistrict/district/
  direction/landmark component set entirely): extended-address->
  `apartment`, street-address->`name`, plus the direct box/locality/
  region/postcode/country mappings.

Deferred (not modeled at all in this file, v1 scope - see the plan for
the full reasoning): `media` (photos - confirmed real client behavior:
Bulwark webmail sends photos as inline base64 `data:` URIs in
`media.<key>.uri`, no `Blob/upload` flow to reuse; a client trying to set
one against this bridge will find it silently doesn't save, since an
unmodeled property is simply never read or written, matching this
project's established "unknown property is inert" pattern rather than
erroring - a real, known gap, not a hypothetical one), `cryptoKeys`,
`directories`, `links`, `localizations`, `personalInfo`, `speakToAs`,
`calendars`/`schedulingAddresses`, `relatedTo`, RFC 9554's extended ADR
component set, RDATE-style anything.

`uid` on create: confirmed real client behavior (Bulwark webmail
generates its own client-side uid before ContactCard/set create,
expecting the server to honor it exactly - not a hypothetical concern,
this was a real bug caught by cross-checking client behavior after the
fact) - `build_vcard_text`'s `uid` parameter must be threaded through
from `props.get("uid")` by the caller (types/contact_card.py's
`_create_card`), not silently discarded in favor of always minting a
fresh one. The identical bug was found and fixed in Phase 2's
`event_map.py`/`types/calendar_event.py` at the same time.
"""

from __future__ import annotations

import base64
import json
import re
import uuid

import vobject

from jmap_bridge.webdav_common.href import canonicalize_collection_href, canonicalize_href_path

_KIND_VALUES = {"individual", "group", "org", "location", "device", "application"}

_NAME_COMPONENT_TO_N_SLOT = {
    "surname": 0,  # Family
    "given": 1,  # Given
    "given2": 2,  # Additional
    "title": 3,  # Honorific-prefix (name-prefix, NOT the separate `titles` ContactCard property)
    "credential": 4,  # Honorific-suffix
}
# surname2/generation have no N slot of their own (RFC 9555 SS2.5.5) -
# surname2 appends onto Family after surname, generation appends onto
# Honorific-suffix after credential. Handled specially in the mapping
# functions below, not via this table.


def canonicalize_addressbook_href(href: str) -> str:
    return canonicalize_collection_href(href)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_card_id(addressbook_href: str, card_href: str) -> str:
    # "K" - see addressbook_map.py's encode_addressbook_id comment: every
    # JMAP id type in this bridge uses a distinct single-letter prefix.
    addressbook_path = canonicalize_collection_href(addressbook_href)
    card_path = canonicalize_href_path(card_href)
    raw = json.dumps([addressbook_path, card_path], separators=(",", ":")).encode("utf-8")
    return "K" + _b64url_encode(raw)


def decode_card_id(card_id: str) -> tuple[str, str]:
    """Inverse of encode_card_id. Raises ValueError on malformed input -
    callers should map that to a `notFound` result, not propagate it as
    a server error.
    """
    if not card_id.startswith("K"):
        raise ValueError(f"not a ContactCard id: {card_id!r}")
    try:
        addressbook_href, card_href = json.loads(_b64url_decode(card_id[1:]))
        return str(addressbook_href), str(card_href)
    except Exception as exc:
        raise ValueError(f"malformed ContactCard id {card_id!r}: {exc}") from exc


def extract_uid(vcard_text: str) -> str:
    """The card's own UID property - needed when moving an existing card
    to a different addressbook (types/contact_card.py), since the new
    href is derived from the uid (see build_vcard_text's docstring) and
    a move re-PUTs the *existing* vCard text as-is, not a freshly built
    one with a caller-supplied uid.
    """
    vcard = vobject.readOne(vcard_text)
    uid_prop = vcard.contents.get("uid")
    return uid_prop[0].value if uid_prop else str(uuid.uuid4())


def _get_all(vcard, name: str) -> list:
    return list(vcard.contents.get(name, []))


def _name_from_n(n_prop) -> dict | None:
    if n_prop is None:
        return None
    n = n_prop.value
    components = []
    if n.family:
        components.append({"kind": "surname", "value": n.family})
    if n.given:
        components.append({"kind": "given", "value": n.given})
    if n.additional:
        components.append({"kind": "given2", "value": n.additional})
    if n.prefix:
        components.append({"kind": "title", "value": n.prefix})
    if n.suffix:
        components.append({"kind": "credential", "value": n.suffix})
    if not components:
        return None
    return {"@type": "Name", "components": components, "isOrdered": True}


def _n_from_name(name: dict):
    """JSContact Name -> vobject.vcard.Name, per RFC 9555 SS2.5.5. surname2
    is appended onto Family after surname; generation is appended onto
    Honorific-suffix after credential - neither has its own N slot.
    """
    family_parts: list[str] = []
    given = ""
    given2 = ""
    prefix_parts: list[str] = []
    suffix_parts: list[str] = []
    for comp in name.get("components") or []:
        kind = comp.get("kind")
        value = comp.get("value", "")
        if kind == "surname":
            family_parts.insert(0, value) if family_parts else family_parts.append(value)
        elif kind == "surname2":
            family_parts.append(value)
        elif kind == "given":
            given = value
        elif kind == "given2":
            given2 = value
        elif kind == "title":
            prefix_parts.append(value)
        elif kind == "credential":
            suffix_parts.insert(0, value) if suffix_parts else suffix_parts.append(value)
        elif kind == "generation":
            suffix_parts.append(value)
    return vobject.vcard.Name(
        family=" ".join(family_parts), given=given, additional=given2,
        prefix=" ".join(prefix_parts), suffix=" ".join(suffix_parts),
    )


def _address_from_adr(adr_prop) -> dict:
    a = adr_prop.value
    address: dict = {"@type": "Address"}
    if a.box:
        address["postOfficeBox"] = a.box
    if a.extended:
        address["apartment"] = a.extended
    if a.street:
        address["name"] = a.street
    if a.city:
        address["locality"] = a.city
    if a.region:
        address["region"] = a.region
    if a.code:
        address["postcode"] = a.code
    if a.country:
        address["country"] = a.country
    label_types = [t.upper() for t in (getattr(adr_prop, "type_paramlist", None) or [])]
    if "HOME" in label_types:
        address["contexts"] = {"private": True}
    elif "WORK" in label_types:
        address["contexts"] = {"work": True}
    return address


def _adr_from_address(address: dict):
    return vobject.vcard.Address(
        box=address.get("postOfficeBox", ""),
        extended=address.get("apartment", ""),
        street=address.get("name", ""),
        city=address.get("locality", ""),
        region=address.get("region", ""),
        code=address.get("postcode", ""),
        country=address.get("country", ""),
    )


def _context_from_type_param(prop) -> dict | None:
    types = [t.upper() for t in (getattr(prop, "type_paramlist", None) or [])]
    if "HOME" in types:
        return {"private": True}
    if "WORK" in types:
        return {"work": True}
    return None


def ical_kind_default(kind_prop) -> str | None:
    if kind_prop is None:
        return "individual"
    value = str(kind_prop.value).lower()
    return value if value in _KIND_VALUES else "individual"


_MEMBER_URI_PREFIX = "urn:uuid:"


def _member_uid_from_value(value: str) -> str:
    """MEMBER values are opaque URIs (RFC 9555 SS2.9.3) whose value is
    exactly the target Card's `uid` property, verbatim - never resolved
    to a JMAP id server-side (see the plan: there's no cheap CardDAV
    "get by uid" primitive, and the member could live in a different
    addressbook or not exist at all). Strip a `urn:uuid:` prefix, the
    convention this bridge itself writes (see `_member_value_from_uid`);
    any other URI form is passed through as-is, still opaque.
    """
    if value.startswith(_MEMBER_URI_PREFIX):
        return value[len(_MEMBER_URI_PREFIX) :]
    return value


_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _member_value_from_uid(uid: str) -> str:
    if _URI_SCHEME_RE.match(uid):
        return uid
    return _MEMBER_URI_PREFIX + uid


def vcard_to_jscontact_card(vcard_text: str, card_id: str, addressbook_id: str) -> dict:
    vcard = vobject.readOne(vcard_text)

    uid_prop = vcard.contents.get("uid")
    uid = uid_prop[0].value if uid_prop else ""

    kind_prop = vcard.contents.get("kind")
    kind = ical_kind_default(kind_prop[0] if kind_prop else None)

    n_prop = vcard.contents.get("n")
    name = _name_from_n(n_prop[0]) if n_prop else None

    nickname_prop = vcard.contents.get("nickname")
    nicknames = None
    if nickname_prop:
        raw = str(nickname_prop[0].value)
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        nicknames = {f"k{i + 1}": {"@type": "Nickname", "name": p} for i, p in enumerate(parts)}

    org_props = _get_all(vcard, "org")
    organizations = None
    if org_props:
        organizations = {}
        for i, org_prop in enumerate(org_props):
            value = org_prop.value
            org_name = value[0] if isinstance(value, list) and value else str(value)
            units = value[1:] if isinstance(value, list) else []
            entry: dict = {"@type": "Organization", "name": org_name}
            if units:
                entry["units"] = [{"@type": "OrgUnit", "name": u} for u in units if u]
            organizations[f"k{i + 1}"] = entry

    title_props = _get_all(vcard, "title")
    titles = None
    if title_props:
        titles = {f"k{i + 1}": {"@type": "Title", "name": str(p.value)} for i, p in enumerate(title_props)}

    email_props = _get_all(vcard, "email")
    emails = None
    if email_props:
        emails = {}
        for i, p in enumerate(email_props):
            entry: dict = {"@type": "EmailAddress", "address": str(p.value)}
            ctx = _context_from_type_param(p)
            if ctx:
                entry["contexts"] = ctx
            emails[f"k{i + 1}"] = entry

    tel_props = _get_all(vcard, "tel")
    phones = None
    if tel_props:
        phones = {}
        for i, p in enumerate(tel_props):
            entry = {"@type": "Phone", "number": str(p.value)}
            ctx = _context_from_type_param(p)
            if ctx:
                entry["contexts"] = ctx
            phones[f"k{i + 1}"] = entry

    url_props = _get_all(vcard, "url")
    online_services = None
    if url_props:
        online_services = {
            f"k{i + 1}": {"@type": "OnlineService", "uri": str(p.value)} for i, p in enumerate(url_props)
        }

    adr_props = _get_all(vcard, "adr")
    addresses = None
    if adr_props:
        addresses = {f"k{i + 1}": _address_from_adr(p) for i, p in enumerate(adr_props)}

    anniversary_prop = vcard.contents.get("anniversary")
    bday_prop = vcard.contents.get("bday")
    anniversaries = None
    entries = []
    if bday_prop:
        entries.append(("birth", str(bday_prop[0].value)))
    if anniversary_prop:
        entries.append(("wedding", str(anniversary_prop[0].value)))
    if entries:
        anniversaries = {
            f"k{i + 1}": {"@type": "Anniversary", "kind": kind_, "date": {"@type": "Timestamp", "utc": date}}
            for i, (kind_, date) in enumerate(entries)
        }

    note_props = _get_all(vcard, "note")
    notes = None
    if note_props:
        notes = {f"k{i + 1}": {"@type": "Note", "note": str(p.value)} for i, p in enumerate(note_props)}

    categories_prop = vcard.contents.get("categories")
    keywords = None
    if categories_prop:
        value = categories_prop[0].value
        values = value if isinstance(value, list) else [value]
        keywords = {str(v): True for v in values if v}

    member_props = _get_all(vcard, "member")
    members = None
    if member_props:
        members = {_member_uid_from_value(str(p.value)): True for p in member_props}

    return {
        "id": card_id,
        "addressBookIds": {addressbook_id: True},
        "uid": uid,
        "members": members,
        "kind": kind,
        "name": name,
        "nicknames": nicknames,
        "organizations": organizations,
        "titles": titles,
        "emails": emails,
        "phones": phones,
        "onlineServices": online_services,
        "addresses": addresses,
        "anniversaries": anniversaries,
        "notes": notes,
        "keywords": keywords,
    }


def _clear(vcard, name: str) -> None:
    if name in vcard.contents:
        del vcard.contents[name]


def _set_type_param(prop, contexts: dict | None) -> None:
    if not contexts:
        return
    if contexts.get("private"):
        prop.type_param = "HOME"
    elif contexts.get("work"):
        prop.type_param = "WORK"


def _compute_fn(props: dict, existing_fn: str | None) -> str:
    """vCard's FN is mandatory (validation fails without it), but
    JSContact's Card has no direct equivalent - derive it from
    `name.full`, else by joining name components, else preserve
    whatever FN already existed (untouched-property fidelity), else fall
    back to a placeholder so serialize() never fails validation.
    """
    if "name" in props and props["name"]:
        name = props["name"]
        if name.get("full"):
            return name["full"]
        parts = [c.get("value", "") for c in name.get("components") or [] if c.get("kind") != "separator"]
        joined = " ".join(p for p in parts if p)
        if joined:
            return joined
    if existing_fn:
        return existing_fn
    return "Unnamed Contact"


def _apply_jscontact_props(vcard, props: dict) -> None:
    if "kind" in props:
        _clear(vcard, "kind")
        if props["kind"]:
            vcard.add("kind").value = props["kind"]

    if "name" in props:
        _clear(vcard, "n")
        name = props["name"]
        if name:
            vcard.add("n").value = _n_from_name(name)

    existing_fn_prop = vcard.contents.get("fn")
    existing_fn = existing_fn_prop[0].value if existing_fn_prop else None
    if "name" in props or existing_fn is None:
        _clear(vcard, "fn")
        vcard.add("fn").value = _compute_fn(props, existing_fn)

    if "nicknames" in props:
        _clear(vcard, "nickname")
        nicknames = props["nicknames"] or {}
        names = [v.get("name") for v in nicknames.values() if v.get("name")]
        if names:
            vcard.add("nickname").value = ", ".join(names)

    if "organizations" in props:
        _clear(vcard, "org")
        for entry in (props["organizations"] or {}).values():
            org_prop = vcard.add("org")
            units = [u.get("name", "") for u in entry.get("units") or []]
            org_prop.value = [entry.get("name", "")] + units

    if "titles" in props:
        _clear(vcard, "title")
        for entry in (props["titles"] or {}).values():
            if entry.get("name"):
                vcard.add("title").value = entry["name"]

    if "emails" in props:
        _clear(vcard, "email")
        for entry in (props["emails"] or {}).values():
            if not entry.get("address"):
                continue
            p = vcard.add("email")
            p.value = entry["address"]
            _set_type_param(p, entry.get("contexts"))

    if "phones" in props:
        _clear(vcard, "tel")
        for entry in (props["phones"] or {}).values():
            if not entry.get("number"):
                continue
            p = vcard.add("tel")
            p.value = entry["number"]
            _set_type_param(p, entry.get("contexts"))

    if "onlineServices" in props:
        _clear(vcard, "url")
        for entry in (props["onlineServices"] or {}).values():
            if entry.get("uri"):
                vcard.add("url").value = entry["uri"]

    if "addresses" in props:
        _clear(vcard, "adr")
        for entry in (props["addresses"] or {}).values():
            p = vcard.add("adr")
            p.value = _adr_from_address(entry)
            _set_type_param(p, entry.get("contexts"))

    if "anniversaries" in props:
        _clear(vcard, "bday")
        _clear(vcard, "anniversary")
        for entry in (props["anniversaries"] or {}).values():
            date = (entry.get("date") or {}).get("utc")
            if not date:
                continue
            if entry.get("kind") == "birth":
                vcard.add("bday").value = date
            else:
                vcard.add("anniversary").value = date

    if "notes" in props:
        _clear(vcard, "note")
        for entry in (props["notes"] or {}).values():
            if entry.get("note"):
                vcard.add("note").value = entry["note"]

    if "keywords" in props:
        _clear(vcard, "categories")
        keywords = [k for k, v in (props["keywords"] or {}).items() if v]
        if keywords:
            vcard.add("categories").value = keywords

    if "members" in props:
        _clear(vcard, "member")
        for member_uid, keep in (props["members"] or {}).items():
            if keep:
                vcard.add("member").value = _member_value_from_uid(member_uid)


def build_vcard_text(props: dict, uid: str | None = None) -> tuple[str, str]:
    """Build a full vCard from JMAP ContactCard creation properties.
    Returns `(vcard_text, uid)` - `uid`, if not given, is a freshly
    minted uuid4; the caller needs it back (unlike Phase 2's CalDAV
    `build_vevent_ical`, whose caller doesn't) because this bridge's
    hand-rolled CardDAV client derives the new card's href directly from
    the uid (backends/carddav/client.py's create_card takes `uid`
    explicitly - no library-level auto-derivation the way `caldav`'s
    `add_event()` has for CalDAV).
    """
    vcard = vobject.vCard()
    vcard.add("version").value = "4.0"
    card_uid = uid or str(uuid.uuid4())
    vcard.add("uid").value = card_uid
    _apply_jscontact_props(vcard, props)
    return vcard.serialize(), card_uid


def apply_jscontact_patch(vcard_text: str, patch: dict) -> str:
    """Update path: mutate the parsed vobject Component in place,
    touching only properties the patch actually changed - preserves
    everything else (PHOTO, custom X- properties, ...). See module
    docstring: `vobject`'s parse->serialize does not preserve property
    order, so this can't rely on byte-identical output for untouched
    lines, only on not touching their *content*.
    """
    vcard = vobject.readOne(vcard_text)
    _apply_jscontact_props(vcard, patch)
    return vcard.serialize()
