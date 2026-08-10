import pytest

from jmap_bridge.state import InvalidStateToken, decode_state, encode_state


def test_round_trip():
    token = encode_state("mystate", {"a": 1, "b": [1, 2, 3]})
    assert decode_state(token, "mystate") == {"a": 1, "b": [1, 2, 3]}


def test_deterministic_encoding():
    t1 = encode_state("k", {"b": 1, "a": 2})
    t2 = encode_state("k", {"a": 2, "b": 1})
    assert t1 == t2


def test_no_padding_characters():
    token = encode_state("k", {"x": "y"})
    assert "=" not in token


def test_wrong_kind_rejected():
    token = encode_state("kind-a", {"x": 1})
    with pytest.raises(InvalidStateToken):
        decode_state(token, "kind-b")


def test_garbage_rejected():
    with pytest.raises(InvalidStateToken):
        decode_state("not-a-valid-token!!!", "k")


def test_non_object_payload_rejected():
    import base64
    import json

    raw = json.dumps({"v": 1, "kind": "k", "payload": "not-a-dict"}).encode()
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    with pytest.raises(InvalidStateToken):
        decode_state(token, "k")
