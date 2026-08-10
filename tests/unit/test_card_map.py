import pytest

from jmap_bridge.backends.carddav.card_map import (
    apply_jscontact_patch,
    build_vcard_text,
    decode_card_id,
    encode_card_id,
    vcard_to_jscontact_card,
)

FULL_PROPS = {
    "kind": "individual",
    "name": {
        "@type": "Name",
        "components": [
            {"kind": "given", "value": "Jane"},
            {"kind": "surname", "value": "Doe"},
        ],
        "isOrdered": True,
    },
    "nicknames": {"k1": {"@type": "Nickname", "name": "Janie"}},
    "organizations": {
        "k1": {"@type": "Organization", "name": "Acme Corp", "units": [{"@type": "OrgUnit", "name": "Engineering"}]}
    },
    "titles": {"k1": {"@type": "Title", "name": "Senior Engineer"}},
    "emails": {"k1": {"@type": "EmailAddress", "address": "jane@example.com", "contexts": {"work": True}}},
    "phones": {"k1": {"@type": "Phone", "number": "+1-555-1234", "contexts": {"private": True}}},
    "onlineServices": {"k1": {"@type": "OnlineService", "uri": "https://example.com/jane"}},
    "addresses": {
        "k1": {
            "@type": "Address",
            "name": "123 Main St",
            "locality": "Springfield",
            "region": "IL",
            "postcode": "62704",
            "country": "USA",
            "contexts": {"private": True},
        }
    },
    "anniversaries": {
        "k1": {"@type": "Anniversary", "kind": "birth", "date": {"@type": "Timestamp", "utc": "1990-06-15"}}
    },
    "notes": {"k1": {"@type": "Note", "note": "Met at conference"}},
    "keywords": {"friend": True, "vip": True},
}


def test_card_id_round_trip_and_normalizes_percent_encoding():
    cid = encode_card_id(
        "http://dav.example.com/alice%40example.com/personal/",
        "http://dav.example.com/alice@example.com/personal/abc.vcf",
    )
    addressbook_href, card_href = decode_card_id(cid)
    assert addressbook_href == "/alice@example.com/personal/"
    assert card_href == "/alice@example.com/personal/abc.vcf"


def test_decode_card_id_rejects_garbage():
    with pytest.raises(ValueError):
        decode_card_id("not-a-card-id")


def test_full_round_trip():
    vcard_text, _ = build_vcard_text(FULL_PROPS, uid="test-uid-1")
    parsed = vcard_to_jscontact_card(vcard_text, "Ksomeid", "Dab1")

    assert parsed["uid"] == "test-uid-1"
    assert parsed["kind"] == "individual"
    components = parsed["name"]["components"]
    assert {"kind": "given", "value": "Jane"} in components
    assert {"kind": "surname", "value": "Doe"} in components
    assert list(parsed["nicknames"].values())[0]["name"] == "Janie"
    assert list(parsed["organizations"].values())[0]["name"] == "Acme Corp"
    assert list(parsed["organizations"].values())[0]["units"][0]["name"] == "Engineering"
    assert list(parsed["titles"].values())[0]["name"] == "Senior Engineer"
    assert list(parsed["emails"].values())[0]["address"] == "jane@example.com"
    assert list(parsed["emails"].values())[0]["contexts"] == {"work": True}
    assert list(parsed["phones"].values())[0]["number"] == "+1-555-1234"
    assert list(parsed["onlineServices"].values())[0]["uri"] == "https://example.com/jane"
    assert list(parsed["addresses"].values())[0]["locality"] == "Springfield"
    assert list(parsed["anniversaries"].values())[0]["kind"] == "birth"
    assert list(parsed["notes"].values())[0]["note"] == "Met at conference"
    assert set(parsed["keywords"].keys()) == {"friend", "vip"}
    assert parsed["addressBookIds"] == {"Dab1": True}


def test_partial_patch_preserves_untouched_properties():
    vcard_text, _ = build_vcard_text(FULL_PROPS, uid="test-uid-1")
    patched = apply_jscontact_patch(
        vcard_text, {"titles": {"k1": {"@type": "Title", "name": "Principal Engineer"}}}
    )
    reparsed = vcard_to_jscontact_card(patched, "Ksomeid", "Dab1")

    assert list(reparsed["titles"].values())[0]["name"] == "Principal Engineer"
    assert reparsed["name"] is not None
    assert list(reparsed["emails"].values())[0]["address"] == "jane@example.com"
    assert list(reparsed["organizations"].values())[0]["name"] == "Acme Corp"
    assert list(reparsed["notes"].values())[0]["note"] == "Met at conference"


def test_surname2_appended_onto_family():
    """RFC 9555 SS2.5.5: surname2 has no N slot of its own - it's
    appended onto Family after surname (e.g. Spanish double surnames).
    Lossy on read-back by design (documented) - not split apart again.
    """
    props = {
        "name": {
            "@type": "Name",
            "components": [
                {"kind": "given", "value": "Maria"},
                {"kind": "surname", "value": "Garcia"},
                {"kind": "surname2", "value": "Lopez"},
            ],
        }
    }
    vcard_text, _ = build_vcard_text(props, uid="test-uid-2")
    assert "N:Garcia Lopez;Maria;;;" in vcard_text
    parsed = vcard_to_jscontact_card(vcard_text, "Kid2", "Dab1")
    assert {"kind": "surname", "value": "Garcia Lopez"} in parsed["name"]["components"]


def test_minimal_card_gets_valid_fn_fallback():
    """vCard's FN is mandatory; JSContact has no direct equivalent -
    build_vcard_text must never produce an invalid vCard even with no
    name info at all.
    """
    props = {"emails": {"k1": {"@type": "EmailAddress", "address": "noname@example.com"}}}
    vcard_text, _ = build_vcard_text(props, uid="test-uid-3")
    assert "FN:" in vcard_text
    parsed = vcard_to_jscontact_card(vcard_text, "Kid3", "Dab1")
    assert parsed["uid"] == "test-uid-3"
    assert list(parsed["emails"].values())[0]["address"] == "noname@example.com"


def test_fn_derived_from_name_full_when_given():
    props = {"name": {"@type": "Name", "components": [], "full": "Dr. Jane Doe, PhD"}}
    vcard_text, _ = build_vcard_text(props, uid="test-uid-4")
    assert "FN:Dr. Jane Doe\\, PhD" in vcard_text


def test_categories_multiple_values_do_not_get_character_split():
    """Regression test: confirmed live that CATEGORIES' vobject behavior
    treats `.value` as an iterable of separate list items - assigning a
    plain comma-joined string gets iterated character by character
    instead of treated as one string.
    """
    vcard_text, _ = build_vcard_text({"keywords": {"friend": True, "vip": True}}, uid="test-uid-5")
    parsed = vcard_to_jscontact_card(vcard_text, "Kid5", "Dab1")
    assert set(parsed["keywords"].keys()) == {"friend", "vip"}


def test_group_card_round_trip():
    """RFC 9610 SS3 / RFC 9555 SS2.9.3: a group is a ContactCard with
    kind:"group" + members (keyed by the referenced Card's uid, not its
    JMAP id - members is opaque, never resolved server-side).
    """
    props = {
        "kind": "group",
        "name": {"@type": "Name", "components": [], "full": "Book Club"},
        "members": {"member-uid-1": True, "member-uid-2": True},
    }
    vcard_text, _ = build_vcard_text(props, uid="group-uid-1")
    assert "KIND:group" in vcard_text
    assert "MEMBER:urn:uuid:member-uid-1" in vcard_text
    assert "MEMBER:urn:uuid:member-uid-2" in vcard_text

    parsed = vcard_to_jscontact_card(vcard_text, "Kgroupid", "Dab1")
    assert parsed["kind"] == "group"
    assert parsed["members"] == {"member-uid-1": True, "member-uid-2": True}


def test_member_value_with_existing_uri_scheme_passed_through():
    props = {"kind": "group", "members": {"mailto:someone@example.com": True}}
    vcard_text, _ = build_vcard_text(props, uid="group-uid-2")
    assert "MEMBER:mailto:someone@example.com" in vcard_text
    parsed = vcard_to_jscontact_card(vcard_text, "Kgroupid2", "Dab1")
    assert parsed["members"] == {"mailto:someone@example.com": True}


def test_member_patch_updates_group_membership():
    props = {"kind": "group", "members": {"uid-a": True, "uid-b": True}}
    vcard_text, _ = build_vcard_text(props, uid="group-uid-3")
    patched = apply_jscontact_patch(vcard_text, {"members": {"uid-a": True, "uid-c": True}})
    parsed = vcard_to_jscontact_card(patched, "Kgroupid3", "Dab1")
    assert parsed["members"] == {"uid-a": True, "uid-c": True}
    assert parsed["kind"] == "group"  # untouched by the members-only patch


def test_clearing_a_property_via_empty_map_removes_it():
    vcard_text, _ = build_vcard_text(FULL_PROPS, uid="test-uid-6")
    patched = apply_jscontact_patch(vcard_text, {"emails": {}})
    parsed = vcard_to_jscontact_card(patched, "Kid6", "Dab1")
    assert parsed["emails"] is None
    # unrelated properties untouched
    assert list(parsed["organizations"].values())[0]["name"] == "Acme Corp"
