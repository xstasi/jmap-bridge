"""Regression tests for the fix found live: a CalDAV/CardDAV server
returning a non-2xx response with a non-XML body (e.g. an nginx 401
error page in front of the real DAV app) makes the `caldav` library's
own internals raise a raw `lxml.etree.XMLSyntaxError` while trying to
parse the error body as WebDAV XML - not a `DAVError` subclass, so it
wasn't caught by any of this bridge's `except DAVError` handlers and
crashed uncaught instead of surfacing as a clean, catchable failure.
"""

import pytest
from caldav.aio import AsyncDAVClient
from lxml import etree

from jmap_bridge.backends.caldav.client import CaldavConnection, CaldavError
from jmap_bridge.backends.carddav.client import CarddavConnection, CarddavError


async def _raise_xml_syntax_error(self):
    etree.XML(b"<html><body><hr></body></html>")  # raises XMLSyntaxError


async def test_caldav_connect_wraps_non_daverror_exception(monkeypatch):
    monkeypatch.setattr(AsyncDAVClient, "principal", _raise_xml_syntax_error)

    with pytest.raises(CaldavError):
        await CaldavConnection.connect("https://dav.example.com/", "alice", "pw")


async def test_carddav_connect_wraps_non_daverror_exception(monkeypatch):
    monkeypatch.setattr(AsyncDAVClient, "principal", _raise_xml_syntax_error)

    with pytest.raises(CarddavError):
        await CarddavConnection.connect("https://dav.example.com/", "alice", "pw")
