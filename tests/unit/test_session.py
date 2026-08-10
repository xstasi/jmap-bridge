from jmap_bridge.auth import Credentials
from jmap_bridge.config import load_config
from jmap_bridge.session import (
    CALENDARS_CAPABILITY,
    CONTACTS_CAPABILITY,
    build_session,
    decode_account_id,
    encode_account_id,
)

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


def _creds(email, domain_name):
    config = load_config(EXAMPLE_CONFIG)
    return Credentials(
        email=email, password="pw", domain=domain_name, domain_config=config.domains[domain_name]
    )


def test_account_id_round_trip():
    aid = encode_account_id("alice@example.com")
    assert decode_account_id(aid) == "alice@example.com"


def test_session_advertises_calendars_and_contacts_when_configured():
    session = build_session("https://bridge.example", _creds("alice@example.com", "example.com"))
    assert CALENDARS_CAPABILITY in session["capabilities"]
    assert CONTACTS_CAPABILITY in session["capabilities"]


def test_session_omits_calendars_and_contacts_when_not_configured():
    session = build_session(
        "https://bridge.example", _creds("bob@mail-only.example.org", "mail-only.example.org")
    )
    assert CALENDARS_CAPABILITY not in session["capabilities"]
    assert CONTACTS_CAPABILITY not in session["capabilities"]


def test_session_urls_and_account_wiring():
    creds = _creds("alice@example.com", "example.com")
    session = build_session("https://bridge.example/", creds)
    account_id = encode_account_id("alice@example.com")
    assert session["apiUrl"] == "https://bridge.example/api"
    assert session["uploadUrl"] == "https://bridge.example/upload/{accountId}"
    assert account_id in session["accounts"]
    assert session["accounts"][account_id]["name"] == "alice@example.com"
    for urn in session["capabilities"]:
        assert session["primaryAccounts"][urn] == account_id
    assert session["username"] == "alice@example.com"


def test_session_state_is_stable_for_same_input():
    creds = _creds("alice@example.com", "example.com")
    s1 = build_session("https://bridge.example", creds)
    s2 = build_session("https://bridge.example", creds)
    assert s1["state"] == s2["state"]
