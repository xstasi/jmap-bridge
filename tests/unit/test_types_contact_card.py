from contextlib import asynccontextmanager

import pytest

from jmap_bridge.backends.carddav.addressbook_map import encode_addressbook_id
from jmap_bridge.backends.carddav.card_map import build_vcard_text, encode_card_id
from jmap_bridge.backends.carddav.client import AddressBookEntry, CardEntry
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments, UnsupportedFilter
from jmap_bridge.id_redirect import IdRedirectCache
from jmap_bridge.types import contact_card as cc_types

PERSONAL = "/alice/personal/"
WORK = "/alice/work/"


class FakeCarddavConn:
    def __init__(self, addressbooks: dict[str, dict], cards: dict[str, dict[str, str]]):
        # addressbooks: href -> {"name": str, "sync_token": str}
        # cards: addressbook_href -> {card_href: vcard_text}
        self._addressbooks = addressbooks
        self._cards = cards

    async def list_addressbooks(self):
        return [
            AddressBookEntry(href=href, display_name=info["name"], sync_token=info["sync_token"])
            for href, info in self._addressbooks.items()
        ]

    async def list_cards(self, addressbook_href):
        return [
            CardEntry(href=href, vcard_text=text, etag=None)
            for href, text in self._cards.get(addressbook_href, {}).items()
        ]

    async def get_card(self, addressbook_href, card_href):
        text = self._cards.get(addressbook_href, {}).get(card_href)
        if text is None:
            return None
        return CardEntry(href=card_href, vcard_text=text, etag="etag1")

    async def get_cards(self, addressbook_href, card_hrefs):
        entries = []
        for href in card_hrefs:
            text = self._cards.get(addressbook_href, {}).get(href)
            if text is not None:
                entries.append(CardEntry(href=href, vcard_text=text, etag="etag1"))
        return entries

    async def create_card(self, addressbook_href, vcard_text, uid):
        href = f"{addressbook_href}{uid}.vcf"
        self._cards.setdefault(addressbook_href, {})[href] = vcard_text
        return CardEntry(href=href, vcard_text=vcard_text, etag="etag1")

    async def update_card(self, addressbook_href, card_href, vcard_text):
        self._cards[addressbook_href][card_href] = vcard_text
        return CardEntry(href=card_href, vcard_text=vcard_text, etag="etag2")

    async def delete_card(self, addressbook_href, card_href):
        self._cards.get(addressbook_href, {}).pop(card_href, None)


class FakeContext:
    account_id = "Aalice"
    id_redirect_key = ("example.com", "alice@example.com")

    def __init__(self, conn: FakeCarddavConn):
        self._conn = conn
        self.id_redirects = IdRedirectCache()

    def require_account(self, account_id):
        assert account_id == self.account_id

    def carddav(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


def _default_addressbooks():
    return {
        PERSONAL: {"name": "Personal", "sync_token": "tok1"},
        WORK: {"name": "Work", "sync_token": "tok2"},
    }


def _make_vcard(uid, name="Jane Doe", email="jane@example.com"):
    props = {
        "name": {"@type": "Name", "components": [{"kind": "given", "value": name.split()[0]}, {"kind": "surname", "value": name.split()[-1]}]},
        "emails": {"k1": {"@type": "EmailAddress", "address": email}},
    }
    text, _ = build_vcard_text(props, uid=uid)
    return text


async def test_contact_card_get():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    cid = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    result = await cc_types.contact_card_get(ctx, {"ids": [cid]})
    assert result["notFound"] == []
    assert result["list"][0]["id"] == cid
    assert result["list"][0]["addressBookIds"] == {encode_addressbook_id(PERSONAL): True}
    assert list(result["list"][0]["emails"].values())[0]["address"] == "jane@example.com"


async def test_contact_card_get_unknown_id():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    result = await cc_types.contact_card_get(ctx, {"ids": ["Kbogus"]})
    assert result["notFound"] == ["Kbogus"]


async def test_contact_card_get_requires_ids():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await cc_types.contact_card_get(ctx, {})


async def test_contact_card_query_without_in_addressbook_merges_all_addressbooks():
    """Regression test for a real bug found live: Bulwark webmail's
    default "load all contacts" call (getContacts() with no
    addressBookId selected) omits the inAddressBook filter entirely,
    expecting a merged result across every address book in the account -
    unlike Email/query's required inMailbox, this bridge doesn't demand
    a scope here (see contact_card.py's module docstring for why)."""
    cards = {
        PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1"), f"{PERSONAL}c2.vcf": _make_vcard("c2")},
        WORK: {f"{WORK}c3.vcf": _make_vcard("c3")},
    }
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)

    result = await cc_types.contact_card_query(ctx, {"filter": {}})
    assert result["total"] == 3

    result_no_filter_arg = await cc_types.contact_card_query(ctx, {})
    assert result_no_filter_arg["total"] == 3


async def test_contact_card_query_invalid_in_addressbook_id_rejected():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await cc_types.contact_card_query(ctx, {"filter": {"inAddressBook": "not-a-real-id"}})


async def test_contact_card_query_lists_cards_in_addressbook():
    cards = {
        PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1"), f"{PERSONAL}c2.vcf": _make_vcard("c2")},
        WORK: {f"{WORK}c3.vcf": _make_vcard("c3")},
    }
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)

    result = await cc_types.contact_card_query(
        ctx, {"filter": {"inAddressBook": encode_addressbook_id(PERSONAL)}}
    )
    assert result["total"] == 2


async def test_contact_card_query_text_filter():
    cards = {
        PERSONAL: {
            f"{PERSONAL}c1.vcf": _make_vcard("c1", name="Jane Doe", email="jane@example.com"),
            f"{PERSONAL}c2.vcf": _make_vcard("c2", name="Bob Smith", email="bob@example.com"),
        }
    }
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)

    result = await cc_types.contact_card_query(
        ctx, {"filter": {"inAddressBook": encode_addressbook_id(PERSONAL), "text": "bob"}}
    )
    assert result["total"] == 1


async def test_contact_card_query_unsupported_filter_rejected():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    with pytest.raises(UnsupportedFilter):
        await cc_types.contact_card_query(
            ctx, {"filter": {"inAddressBook": encode_addressbook_id(PERSONAL), "kind": "individual"}}
        )


async def test_contact_card_changes_always_cannot_calculate():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    with pytest.raises(CannotCalculateChanges):
        await cc_types.contact_card_changes(ctx, {"sinceState": "whatever"})


async def test_contact_card_changes_requires_since_state():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await cc_types.contact_card_changes(ctx, {})


async def test_contact_card_set_create():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)

    result = await cc_types.contact_card_set(
        ctx,
        {
            "create": {
                "c1": {
                    "addressBookIds": {encode_addressbook_id(PERSONAL): True},
                    "name": {"@type": "Name", "components": [{"kind": "given", "value": "New"}]},
                    "emails": {"k1": {"@type": "EmailAddress", "address": "new@example.com"}},
                }
            }
        },
    )
    assert result["notCreated"] == {}
    assert "c1" in result["created"]
    assert list(result["created"]["c1"]["emails"].values())[0]["address"] == "new@example.com"
    assert len(conn._cards[PERSONAL]) == 1


async def test_contact_card_set_create_respects_client_supplied_uid():
    """Regression test: confirmed real client behavior (Bulwark webmail)
    generates its own uid client-side before ContactCard/set create and
    expects the server to honor it - the server must not silently swap
    in a different one the client's own local cache never learns about.
    """
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)

    result = await cc_types.contact_card_set(
        ctx,
        {
            "create": {
                "c1": {
                    "addressBookIds": {encode_addressbook_id(PERSONAL): True},
                    "uid": "client-chosen-uid-123",
                    "emails": {"k1": {"@type": "EmailAddress", "address": "new@example.com"}},
                }
            }
        },
    )
    assert result["notCreated"] == {}
    assert result["created"]["c1"]["uid"] == "client-chosen-uid-123"


async def test_contact_card_set_create_requires_exactly_one_addressbook():
    conn = FakeCarddavConn(_default_addressbooks(), {})
    ctx = FakeContext(conn)
    result = await cc_types.contact_card_set(
        ctx, {"create": {"c1": {"addressBookIds": {}, "emails": {}}}}
    )
    assert result["notCreated"]["c1"]["type"] == "invalidProperties"


async def test_contact_card_set_update_property_patch():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    cid = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    result = await cc_types.contact_card_set(
        ctx, {"update": {cid: {"notes": {"k1": {"@type": "Note", "note": "Updated note"}}}}}
    )
    assert result["notUpdated"] == {}
    assert "Updated note" in conn._cards[PERSONAL][f"{PERSONAL}c1.vcf"]


async def test_contact_card_set_move_via_full_replacement():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    cid = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    result = await cc_types.contact_card_set(
        ctx, {"update": {cid: {"addressBookIds": {encode_addressbook_id(WORK): True}}}}
    )
    assert result["notUpdated"] == {}
    assert f"{PERSONAL}c1.vcf" not in conn._cards.get(PERSONAL, {})
    assert len(conn._cards[WORK]) == 1


async def test_contact_card_set_move_via_patch_key():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    cid = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    result = await cc_types.contact_card_set(
        ctx,
        {
            "update": {
                cid: {
                    f"addressBookIds/{encode_addressbook_id(PERSONAL)}": None,
                    f"addressBookIds/{encode_addressbook_id(WORK)}": True,
                }
            }
        },
    )
    assert result["notUpdated"] == {}
    assert len(conn._cards[WORK]) == 1


async def test_contact_card_move_records_id_redirect_and_get_resolves_it():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    old_id = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    result = await cc_types.contact_card_set(
        ctx, {"update": {old_id: {"addressBookIds": {encode_addressbook_id(WORK): True}}}}
    )
    assert result["notUpdated"] == {}

    get_result = await cc_types.contact_card_get(ctx, {"ids": [old_id]})
    assert get_result["notFound"] == []
    assert get_result["list"][0]["id"] == old_id
    assert get_result["list"][0]["addressBookIds"] == {encode_addressbook_id(WORK): True}


async def test_contact_card_set_destroy():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    cid = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    result = await cc_types.contact_card_set(ctx, {"destroy": [cid]})
    assert result["destroyed"] == [cid]
    assert f"{PERSONAL}c1.vcf" not in conn._cards[PERSONAL]


async def test_contact_card_set_destroy_on_redirected_id():
    cards = {PERSONAL: {f"{PERSONAL}c1.vcf": _make_vcard("c1")}}
    conn = FakeCarddavConn(_default_addressbooks(), cards)
    ctx = FakeContext(conn)
    old_id = encode_card_id(PERSONAL, f"{PERSONAL}c1.vcf")

    await cc_types.contact_card_set(
        ctx, {"update": {old_id: {"addressBookIds": {encode_addressbook_id(WORK): True}}}}
    )
    result = await cc_types.contact_card_set(ctx, {"destroy": [old_id]})
    assert result["destroyed"] == [old_id]
    assert result["notDestroyed"] == {}
    assert not conn._cards.get(WORK, {})
