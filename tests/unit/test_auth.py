import base64

import pytest

from jmap_bridge.auth import authenticate_request, parse_basic_auth_header
from jmap_bridge.config import load_config
from jmap_bridge.errors import Unauthorized

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


def _basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def test_parse_basic_auth_header():
    assert parse_basic_auth_header(_basic_header("alice@example.com", "secret")) == (
        "alice@example.com",
        "secret",
    )


def test_parse_basic_auth_header_missing():
    assert parse_basic_auth_header(None) is None


def test_parse_basic_auth_header_wrong_scheme():
    assert parse_basic_auth_header("Bearer sometoken") is None


def test_parse_basic_auth_header_malformed_base64():
    assert parse_basic_auth_header("Basic not-valid-base64!!!") is None


def test_parse_basic_auth_header_no_colon():
    token = base64.b64encode(b"nocolonhere").decode()
    assert parse_basic_auth_header(f"Basic {token}") is None


def test_authenticate_request_success():
    config = load_config(EXAMPLE_CONFIG)
    creds = authenticate_request(_basic_header("alice@example.com", "secret"), config)
    assert creds.email == "alice@example.com"
    assert creds.password == "secret"
    assert creds.domain == "example.com"
    assert creds.domain_config.imap.host == "imap.example.com"


def test_authenticate_request_missing_header():
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(Unauthorized):
        authenticate_request(None, config)


def test_authenticate_request_unknown_domain():
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(Unauthorized):
        authenticate_request(_basic_header("alice@unknown.tld", "secret"), config)


def test_authenticate_request_non_email_username():
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(Unauthorized):
        authenticate_request(_basic_header("not-an-email", "secret"), config)
