import base64
import logging

import pytest
from starlette.testclient import TestClient

from jmap_bridge.app import configure_logging, create_app
from jmap_bridge.config import load_config
from jmap_bridge.session import encode_account_id

ALICE_ACCOUNT_ID = encode_account_id("alice@example.com")

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


def _basic_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    config = load_config(EXAMPLE_CONFIG)
    app = create_app(config, "https://bridge.example")
    with TestClient(app) as c:
        yield c


def test_well_known_redirects_to_session(client):
    response = client.get("/.well-known/jmap", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/session"


def test_session_requires_auth(client):
    response = client.get("/session")
    assert response.status_code == 401


def test_session_success(client):
    response = client.get("/session", headers=_basic_header("alice@example.com", "pw"))
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alice@example.com"
    assert "urn:ietf:params:jmap:core" in body["capabilities"]
    assert body["apiUrl"] == "https://bridge.example/api"


def test_session_unknown_domain_rejected(client):
    response = client.get("/session", headers=_basic_header("alice@unknown.tld", "pw"))
    assert response.status_code == 401


def test_api_requires_auth(client):
    response = client.post("/api", json={"methodCalls": []})
    assert response.status_code == 401


def test_api_core_echo(client):
    response = client.post(
        "/api",
        headers=_basic_header("alice@example.com", "pw"),
        json={"methodCalls": [["Core/echo", {"hello": "world"}, "t0"]]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["methodResponses"] == [["Core/echo", {"hello": "world"}, "t0"]]


def test_api_session_state_matches_session_endpoint(client):
    headers = _basic_header("alice@example.com", "pw")
    session_state = client.get("/session", headers=headers).json()["state"]
    api_state = client.post(
        "/api", headers=headers, json={"methodCalls": [["Core/echo", {}, "t0"]]}
    ).json()["sessionState"]
    # RFC 8620 SS3.4: a client compares these and refetches /session when
    # they differ - a mismatch here means every /api call looks like a
    # session change (regression test for a bug found reviewing aerc).
    assert api_state == session_state


def test_api_unknown_method(client):
    response = client.post(
        "/api",
        headers=_basic_header("alice@example.com", "pw"),
        json={"methodCalls": [["Bogus/method", {}, "t0"]]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["methodResponses"][0][0] == "error"
    assert body["methodResponses"][0][1]["type"] == "unknownMethod"


def test_api_rejects_non_json_body(client):
    response = client.post(
        "/api", headers=_basic_header("alice@example.com", "pw"), content=b"not json"
    )
    assert response.status_code == 400
    assert response.json()["type"] == "urn:ietf:params:jmap:error:notJSON"


def test_api_rejects_missing_method_calls(client):
    response = client.post(
        "/api", headers=_basic_header("alice@example.com", "pw"), json={"nope": True}
    )
    assert response.status_code == 400
    assert response.json()["type"] == "urn:ietf:params:jmap:error:notRequest"


def test_upload_requires_auth(client):
    response = client.post("/upload/Aalice", content=b"data")
    assert response.status_code == 401


def test_upload_download_round_trip(client):
    headers = _basic_header("alice@example.com", "pw")
    upload_response = client.post(
        f"/upload/{ALICE_ACCOUNT_ID}",
        headers={**headers, "content-type": "text/plain"},
        content=b"hello world",
    )
    assert upload_response.status_code == 200
    body = upload_response.json()
    blob_id = body["blobId"]
    assert body["size"] == 11

    download_response = client.get(
        f"/download/{body['accountId']}/{blob_id}/file.txt", headers=headers
    )
    assert download_response.status_code == 200
    assert download_response.content == b"hello world"


def test_download_unknown_blob_is_not_found(client):
    headers = _basic_header("alice@example.com", "pw")
    response = client.get(
        f"/download/{ALICE_ACCOUNT_ID}/Ubogus/file.txt", headers=headers
    )
    assert response.status_code == 404


def test_upload_download_rejects_mismatched_account(client):
    headers = _basic_header("alice@example.com", "pw")
    upload_response = client.post(
        f"/upload/{ALICE_ACCOUNT_ID}", headers=headers, content=b"data"
    )
    blob_id = upload_response.json()["blobId"]
    other_headers = _basic_header("bob@example.com", "pw")
    response = client.get(
        f"/download/{ALICE_ACCOUNT_ID}/{blob_id}/file.txt", headers=other_headers
    )
    assert response.status_code == 404


def test_cors_preflight_reflects_requesting_origin(client):
    """Browser-based JMAP clients (e.g. Bulwark webmail) served from a
    different origin than the bridge need this - confirmed live that
    Starlette's CORSMiddleware reflects the specific origin rather than a
    literal "*" whenever allow_credentials=True (browsers reject "*"
    combined with credentials)."""
    response = client.options(
        "/api",
        headers={
            "Origin": "https://webmail.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://webmail.example"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_cors_real_response_reflects_requesting_origin(client):
    response = client.get(
        "/session",
        headers={**_basic_header("alice@example.com", "pw"), "Origin": "https://webmail.example"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://webmail.example"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_headers_absent_without_origin_header():
    """A same-origin/native (non-browser) request never sends Origin -
    CORSMiddleware should be a complete no-op for it, not add headers
    unconditionally."""
    config = load_config(EXAMPLE_CONFIG)
    app = create_app(config, "https://bridge.example")
    with TestClient(app) as c:
        response = c.get("/session", headers=_basic_header("alice@example.com", "pw"))
    assert "access-control-allow-origin" not in response.headers


@pytest.fixture
def restore_logging_state():
    """`configure_logging` mutates global logging state (root + named
    loggers) - save and restore it so this doesn't leak into other tests
    sharing the same process."""
    root = logging.getLogger()
    saved_root_level = root.level
    saved_handlers = list(root.handlers)
    saved_levels = {name: logging.getLogger(name).level for name in ("jmap_bridge", "__main__")}
    yield
    root.setLevel(saved_root_level)
    root.handlers[:] = saved_handlers
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_suppresses_bare_root_third_party_spam(restore_logging_state):
    """Regression test for the fix found live: the caldav library logs
    its own error diagnostics (a full traceback) at INFO through the
    bare, unnamespaced root logger - this showed up regardless of
    JMAP_BRIDGE_LOG_LEVEL (even at the INFO default, not just DEBUG), so
    root must stay at WARNING while our own loggers get the configured
    level explicitly."""
    configure_logging("INFO")
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert not logging.getLogger().isEnabledFor(logging.INFO)


def test_configure_logging_applies_configured_level_to_own_loggers(restore_logging_state):
    configure_logging("DEBUG")
    assert logging.getLogger("__main__").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("jmap_bridge.pool").isEnabledFor(logging.DEBUG)


def test_configure_logging_own_loggers_respect_info_default(restore_logging_state):
    configure_logging("INFO")
    assert not logging.getLogger("__main__").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("__main__").isEnabledFor(logging.INFO)


def test_configure_logging_third_party_errors_still_surface(restore_logging_state):
    """Suppressing INFO-level root spam must not silence a genuine
    warning/error from some other third-party library using the bare
    root logger too."""
    configure_logging("INFO")
    assert logging.getLogger().isEnabledFor(logging.WARNING)
    assert logging.getLogger().isEnabledFor(logging.ERROR)
