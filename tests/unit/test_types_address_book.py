from contextlib import asynccontextmanager

import pytest

from jmap_bridge.backends.carddav.addressbook_map import encode_addressbook_id
from jmap_bridge.backends.carddav.client import AddressBookEntry
from jmap_bridge.errors import CannotCalculateChanges, InvalidArguments
from jmap_bridge.types import address_book as address_book_types


class FakeCarddavConn:
    def __init__(self, addressbooks: dict[str, dict]):
        # href -> {"name": str, "sync_token": str}
        self._addressbooks = addressbooks

    async def list_addressbooks(self):
        return [
            AddressBookEntry(href=href, display_name=info["name"], sync_token=info["sync_token"])
            for href, info in self._addressbooks.items()
        ]

    async def create_addressbook(self, name):
        href = f"/alice/{name.lower().replace(' ', '-')}/"
        self._addressbooks[href] = {"name": name, "sync_token": "tok-new"}
        return href

    async def rename_addressbook(self, href, new_name):
        self._addressbooks[href]["name"] = new_name

    async def delete_addressbook(self, href):
        del self._addressbooks[href]


class FakeContext:
    account_id = "Aalice"

    def __init__(self, conn: FakeCarddavConn):
        self._conn = conn

    def require_account(self, account_id):
        assert account_id == self.account_id

    def carddav(self):
        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


def _default_addressbooks():
    return {
        "/alice/personal/": {"name": "Personal", "sync_token": "tok1"},
        "/alice/work/": {"name": "Work", "sync_token": "tok2"},
    }


async def test_address_book_get_returns_all():
    conn = FakeCarddavConn(_default_addressbooks())
    ctx = FakeContext(conn)
    result = await address_book_types.address_book_get(ctx, {})
    assert result["accountId"] == "Aalice"
    by_name = {a["name"]: a for a in result["list"]}
    assert "Personal" in by_name and "Work" in by_name
    assert result["notFound"] == []


async def test_address_book_get_with_ids_and_properties():
    conn = FakeCarddavConn(_default_addressbooks())
    ctx = FakeContext(conn)
    personal_id = encode_addressbook_id("/alice/personal/")
    result = await address_book_types.address_book_get(
        ctx, {"ids": [personal_id, "Dbogus"], "properties": ["name"]}
    )
    assert result["list"] == [{"id": personal_id, "name": "Personal"}]
    assert result["notFound"] == ["Dbogus"]


async def test_address_book_changes_detects_created_updated_destroyed():
    addressbooks = _default_addressbooks()
    conn = FakeCarddavConn(addressbooks)
    ctx = FakeContext(conn)
    initial = await address_book_types.address_book_get(ctx, {})
    since_state = initial["state"]

    addressbooks["/alice/personal/"]["name"] = "Personal Renamed"  # updated
    addressbooks["/alice/friends/"] = {"name": "Friends", "sync_token": "tok3"}  # created
    del addressbooks["/alice/work/"]  # destroyed

    changes = await address_book_types.address_book_changes(ctx, {"sinceState": since_state})
    assert changes["created"] == [encode_addressbook_id("/alice/friends/")]
    assert changes["updated"] == [encode_addressbook_id("/alice/personal/")]
    assert changes["destroyed"] == [encode_addressbook_id("/alice/work/")]


async def test_address_book_changes_rejects_missing_since_state():
    conn = FakeCarddavConn(_default_addressbooks())
    ctx = FakeContext(conn)
    with pytest.raises(InvalidArguments):
        await address_book_types.address_book_changes(ctx, {})


async def test_address_book_changes_garbage_since_state_is_cannot_calculate():
    conn = FakeCarddavConn(_default_addressbooks())
    ctx = FakeContext(conn)
    with pytest.raises(CannotCalculateChanges):
        await address_book_types.address_book_changes(ctx, {"sinceState": "not-a-real-token"})


async def test_address_book_set_create_update_destroy():
    addressbooks = _default_addressbooks()
    conn = FakeCarddavConn(addressbooks)
    ctx = FakeContext(conn)

    result = await address_book_types.address_book_set(
        ctx,
        {
            "create": {"a1": {"name": "Clients"}},
            "update": {encode_addressbook_id("/alice/personal/"): {"name": "Old Personal"}},
            "destroy": [encode_addressbook_id("/alice/work/")],
        },
    )
    assert "a1" in result["created"]
    assert any(info["name"] == "Clients" for info in addressbooks.values())
    assert addressbooks["/alice/personal/"]["name"] == "Old Personal"
    assert "/alice/work/" not in addressbooks
    assert result["destroyed"] == [encode_addressbook_id("/alice/work/")]
    assert result["notCreated"] == {}
    assert result["notUpdated"] == {}
    assert result["notDestroyed"] == {}


async def test_address_book_set_create_requires_name():
    conn = FakeCarddavConn(_default_addressbooks())
    ctx = FakeContext(conn)
    result = await address_book_types.address_book_set(ctx, {"create": {"a1": {}}})
    assert result["notCreated"]["a1"]["type"] == "invalidArguments"


async def test_address_book_set_update_keeps_same_id_after_rename():
    addressbooks = _default_addressbooks()
    conn = FakeCarddavConn(addressbooks)
    ctx = FakeContext(conn)
    personal_id = encode_addressbook_id("/alice/personal/")

    result = await address_book_types.address_book_set(
        ctx, {"update": {personal_id: {"name": "New Name"}}}
    )
    assert result["updated"] == {personal_id: None}

    get_result = await address_book_types.address_book_get(ctx, {"ids": [personal_id]})
    assert get_result["list"][0]["id"] == personal_id
    assert get_result["list"][0]["name"] == "New Name"


async def test_address_book_set_destroy_unknown_id():
    conn = FakeCarddavConn(_default_addressbooks())
    ctx = FakeContext(conn)
    result = await address_book_types.address_book_set(ctx, {"destroy": ["Dnotreal!!!"]})
    assert "Dnotreal!!!" in result["notDestroyed"]
