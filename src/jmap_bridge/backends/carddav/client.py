"""Thin async wrapper around `caldav.aio.AsyncDAVClient`'s low-level
WebDAV verb methods (propfind/report/mkcol/proppatch/put/delete) -
`caldav` has no CardDAV-aware object layer at all (confirmed: no
`AddressBook`/`Card` class, no `addressbook_home_set` on `Principal`),
but its low-level HTTP/auth/XML-response-parsing plumbing is generic
WebDAV, not CalDAV-specific - exactly what a maintainer comment in
`davclient.py`'s `mkcol()` docstring anticipates reusing this way.

Two things confirmed live (spike, not just source reading) before this
was written:
1. The convenience methods (`sync_collection()`, `calendar_query()`,
   `calendar_multiget()`) hardcode extracting only CalDAV properties
   (`CalendarData`/`GetEtag`) and would SILENTLY DROP a CardDAV
   `address-data` property rather than erroring - never used here.
   Always call `report()` directly and parse via `.parse_propfind()`,
   which is generic multistatus/propstat/prop parsing with no per-method
   or per-namespace branching.
2. Unlike Phase 2's `DAVObject`-based `Calendar`/`Event` classes (which
   auto-resolve relative hrefs against the client's base URL), these
   low-level verb methods do NOT - every href from a PROPFIND/REPORT
   response must be `urljoin()`-resolved against the base URL before
   being used in a follow-up request, or the underlying HTTP client
   raises `MissingSchema`. `_resolve()` below is that step.

A `CarddavConnection` wraps one authenticated `AsyncDAVClient`, valid for
the lifetime of one JMAP HTTP request/batch (see context.py's
`ctx.carddav()`) - mirrors `backends/caldav/client.py`'s `CaldavConnection`
exactly, including no connection pool (HTTP+Basic-auth, no expensive
handshake like IMAP LOGIN to amortize).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urljoin

from caldav.aio import AsyncDAVClient
from caldav.lib.error import DAVError, NotFoundError

_DAV_NS = "DAV:"
_CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"

_PROPFIND_ADDRESSBOOK_HOME = f"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="{_DAV_NS}" xmlns:C="{_CARDDAV_NS}">
  <D:prop>
    <C:addressbook-home-set/>
  </D:prop>
</D:propfind>"""

_PROPFIND_LIST_MEMBERS = f"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="{_DAV_NS}">
  <D:prop>
    <D:resourcetype/>
    <D:getetag/>
    <D:displayname/>
  </D:prop>
</D:propfind>"""

_PROPFIND_GETETAG = f"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="{_DAV_NS}">
  <D:prop><D:getetag/></D:prop>
</D:propfind>"""


def _mkcol_body(display_name: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<D:mkcol xmlns:D="{_DAV_NS}" xmlns:C="{_CARDDAV_NS}">
  <D:set>
    <D:prop>
      <D:resourcetype><D:collection/><C:addressbook/></D:resourcetype>
      <D:displayname>{display_name}</D:displayname>
    </D:prop>
  </D:set>
</D:mkcol>"""


def _proppatch_displayname_body(display_name: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<D:propertyupdate xmlns:D="{_DAV_NS}">
  <D:set>
    <D:prop>
      <D:displayname>{display_name}</D:displayname>
    </D:prop>
  </D:set>
</D:propertyupdate>"""


def _multiget_body(hrefs: list[str]) -> str:
    href_xml = "".join(f"<D:href>{h}</D:href>" for h in hrefs)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<C:addressbook-multiget xmlns:D="{_DAV_NS}" xmlns:C="{_CARDDAV_NS}">
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
  {href_xml}
</C:addressbook-multiget>"""


def _sync_collection_body(sync_token: str | None) -> str:
    token_xml = f"<D:sync-token>{sync_token}</D:sync-token>" if sync_token else "<D:sync-token/>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<D:sync-collection xmlns:D="{_DAV_NS}">
  {token_xml}
  <D:sync-level>1</D:sync-level>
  <D:prop>
    <D:getetag/>
  </D:prop>
</D:sync-collection>"""


class CarddavError(Exception):
    """Wraps any CardDAV/WebDAV protocol failure."""


@dataclass(frozen=True, slots=True)
class AddressBookEntry:
    href: str
    display_name: str | None
    sync_token: str | None


@dataclass(frozen=True, slots=True)
class CardEntry:
    href: str
    vcard_text: str
    etag: str | None


class CarddavConnection:
    def __init__(self, client: AsyncDAVClient, base_url: str, principal_href: str):
        self._client = client
        self._base_url = base_url
        self.principal_href = principal_href

    @classmethod
    async def connect(cls, url: str, username: str, password: str) -> "CarddavConnection":
        client = AsyncDAVClient(
            url=url,
            username=username,
            password=password,
            auth_type="basic",
            require_tls=url.startswith("https://"),
        )
        try:
            principal = await client.principal()
        except DAVError as exc:
            await client.close()
            raise CarddavError(f"CardDAV discovery at {url!r} failed: {exc}") from exc
        return cls(client, url, str(principal.url))

    async def close(self) -> None:
        await self._client.close()

    def _resolve(self, href: str) -> str:
        return urljoin(self._base_url, href)

    async def _addressbook_home_href(self) -> str:
        try:
            resp = await self._client.propfind(
                url=self.principal_href, body=_PROPFIND_ADDRESSBOOK_HOME, depth=0
            )
            results = resp.parse_propfind()
        except DAVError as exc:
            raise CarddavError(f"addressbook-home-set discovery failed: {exc}") from exc
        for r in results:
            for tag, value in r.properties.items():
                if tag.endswith("}addressbook-home-set") and value:
                    return self._resolve(value)
        return self.principal_href

    async def _sync_token(self, addressbook_href: str) -> str | None:
        try:
            resp = await self._client.propfind(
                url=addressbook_href,
                body='<D:propfind xmlns:D="DAV:"><D:prop><D:sync-token/></D:prop></D:propfind>',
                depth=0,
            )
            results = resp.parse_propfind()
        except DAVError:
            return None
        for r in results:
            for tag, value in r.properties.items():
                if tag.endswith("}sync-token") and value:
                    return str(value)
        return None

    async def list_addressbooks(self) -> list[AddressBookEntry]:
        home_href = await self._addressbook_home_href()
        try:
            resp = await self._client.propfind(url=home_href, body=_PROPFIND_LIST_MEMBERS, depth=1)
            results = resp.parse_propfind()
        except DAVError as exc:
            raise CarddavError(f"listing addressbooks failed: {exc}") from exc
        entries = []
        for r in results:
            resourcetypes = r.properties.get("{DAV:}resourcetype") or []
            if not isinstance(resourcetypes, list):
                resourcetypes = [resourcetypes]
            if not any(str(rt).endswith("}addressbook") for rt in resourcetypes):
                continue
            href = self._resolve(r.href)
            display_name = r.properties.get("{DAV:}displayname")
            sync_token = await self._sync_token(href)
            entries.append(AddressBookEntry(href=href, display_name=display_name, sync_token=sync_token))
        return entries

    async def create_addressbook(self, name: str) -> str:
        home_href = await self._addressbook_home_href()
        new_href = home_href.rstrip("/") + f"/{uuid.uuid4()}/"
        try:
            resp = await self._client.mkcol(url=new_href, body=_mkcol_body(name))
        except DAVError as exc:
            raise CarddavError(f"creating addressbook {name!r} failed: {exc}") from exc
        if resp.status not in (200, 201):
            raise CarddavError(f"creating addressbook {name!r} failed: HTTP {resp.status}")
        return new_href

    async def rename_addressbook(self, href: str, new_name: str) -> None:
        href = self._resolve(href)
        try:
            resp = await self._client.proppatch(url=href, body=_proppatch_displayname_body(new_name))
        except DAVError as exc:
            raise CarddavError(f"renaming addressbook {href!r} failed: {exc}") from exc
        if resp.status not in (200, 207):
            raise CarddavError(f"renaming addressbook {href!r} failed: HTTP {resp.status}")

    async def delete_addressbook(self, href: str) -> None:
        href = self._resolve(href)
        try:
            await self._client.delete(url=href)
        except DAVError as exc:
            raise CarddavError(f"deleting addressbook {href!r} failed: {exc}") from exc

    async def list_cards(self, addressbook_href: str) -> list[CardEntry]:
        """PROPFIND Depth:1 to enumerate member hrefs, then one
        addressbook-multiget REPORT to batch-fetch address-data - not an
        N+1 per-card GET (see the plan: v1's query scope needs the whole
        addressbook materialized for every query anyway, so batching is
        free here, unlike CalendarEvent's per-id fetch tradeoff).
        """
        addressbook_href = self._resolve(addressbook_href)
        try:
            resp = await self._client.propfind(url=addressbook_href, body=_PROPFIND_LIST_MEMBERS, depth=1)
            results = resp.parse_propfind()
        except DAVError as exc:
            raise CarddavError(f"listing cards in {addressbook_href!r} failed: {exc}") from exc
        card_hrefs = []
        for r in results:
            resourcetypes = r.properties.get("{DAV:}resourcetype") or []
            if not isinstance(resourcetypes, list):
                resourcetypes = [resourcetypes]
            if any(str(rt).endswith("}collection") for rt in resourcetypes):
                continue  # the addressbook collection itself, not a member card
            card_hrefs.append(self._resolve(r.href))
        if not card_hrefs:
            return []
        return await self._multiget(addressbook_href, card_hrefs)

    async def _multiget(self, addressbook_href: str, card_hrefs: list[str]) -> list[CardEntry]:
        # `_resolve()` is idempotent on an already-absolute URL (urljoin
        # just returns it unchanged), so it's safe to call unconditionally
        # here even though callers sometimes pass already-resolved hrefs
        # (list_cards) and sometimes pass raw hrefs straight from a
        # decoded JMAP id (get_card).
        addressbook_href = self._resolve(addressbook_href)
        card_hrefs = [self._resolve(h) for h in card_hrefs]
        try:
            resp = await self._client.report(
                url=addressbook_href, body=_multiget_body(card_hrefs), depth=1
            )
            results = resp.parse_propfind()
        except DAVError as exc:
            raise CarddavError(f"batch-fetching cards in {addressbook_href!r} failed: {exc}") from exc
        entries = []
        for r in results:
            vcard_text = None
            etag = None
            for tag, value in r.properties.items():
                if tag.endswith("}address-data"):
                    vcard_text = value
                elif tag.endswith("}getetag"):
                    etag = value
            if vcard_text:
                entries.append(CardEntry(href=self._resolve(r.href), vcard_text=vcard_text, etag=etag))
        return entries

    async def get_cards(self, addressbook_href: str, card_hrefs: list[str]) -> list[CardEntry]:
        """Public batch-fetch entry point for ContactCard/get's multi-id
        case (grouped by addressbook by the caller) - one multiget REPORT
        per addressbook, not one GET per id.
        """
        return await self._multiget(addressbook_href, card_hrefs)

    async def get_card(self, addressbook_href: str, card_href: str) -> CardEntry | None:
        entries = await self._multiget(addressbook_href, [card_href])
        return entries[0] if entries else None

    async def create_card(self, addressbook_href: str, vcard_text: str, uid: str) -> CardEntry:
        addressbook_href = self._resolve(addressbook_href)
        card_href = addressbook_href.rstrip("/") + f"/{uid}.vcf"
        try:
            resp = await self._client.put(
                url=card_href, body=vcard_text, headers={"Content-Type": "text/vcard; charset=utf-8"}
            )
        except DAVError as exc:
            raise CarddavError(f"creating card in {addressbook_href!r} failed: {exc}") from exc
        if resp.status not in (200, 201, 204):
            raise CarddavError(f"creating card in {addressbook_href!r} failed: HTTP {resp.status}")
        etag = resp.headers.get("ETag") if resp.headers else None
        return CardEntry(href=card_href, vcard_text=vcard_text, etag=etag)

    async def update_card(self, addressbook_href: str, card_href: str, vcard_text: str) -> CardEntry:
        card_href = self._resolve(card_href)
        try:
            resp = await self._client.put(
                url=card_href, body=vcard_text, headers={"Content-Type": "text/vcard; charset=utf-8"}
            )
        except DAVError as exc:
            raise CarddavError(f"updating card {card_href!r} failed: {exc}") from exc
        if resp.status not in (200, 201, 204):
            raise CarddavError(f"updating card {card_href!r} failed: HTTP {resp.status}")
        etag = resp.headers.get("ETag") if resp.headers else None
        return CardEntry(href=card_href, vcard_text=vcard_text, etag=etag)

    async def delete_card(self, addressbook_href: str, card_href: str) -> None:
        card_href = self._resolve(card_href)
        try:
            await self._client.delete(url=card_href)
        except NotFoundError:
            pass  # already gone - destroy is idempotent
        except DAVError as exc:
            raise CarddavError(f"deleting card {card_href!r} failed: {exc}") from exc
