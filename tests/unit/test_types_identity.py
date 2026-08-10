from jmap_bridge.auth import Credentials
from jmap_bridge.config import load_config
from jmap_bridge.context import RequestContext
from jmap_bridge.pool import ImapConnectionPool
from jmap_bridge.types import identity as identity_types

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


def _ctx():
    config = load_config(EXAMPLE_CONFIG)
    creds = Credentials(
        email="alice@example.com", password="pw", domain="example.com",
        domain_config=config.domains["example.com"],
    )
    return RequestContext(credentials=creds, config=config, pool=ImapConnectionPool())


async def test_identity_get_returns_default_identity():
    result = await identity_types.identity_get(_ctx(), {})
    assert len(result["list"]) == 1
    assert result["list"][0]["email"] == "alice@example.com"
    assert result["notFound"] == []


async def test_identity_get_by_id():
    result = await identity_types.identity_get(_ctx(), {"ids": ["default", "bogus"]})
    assert [i["id"] for i in result["list"]] == ["default"]
    assert result["notFound"] == ["bogus"]
