import pytest

from jmap_bridge import dispatch
from jmap_bridge.errors import InvalidArguments


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    # Each test gets an isolated registry so tests can't interfere with
    # each other (or with real types/*.py registrations imported elsewhere).
    monkeypatch.setattr(dispatch, "_REGISTRY", {})


class Ctx:
    account_id = "u1"


async def test_dispatch_unknown_method():
    responses, _ = await dispatch.dispatch_request(Ctx(), [["Foo/bar", {}, "t0"]])
    assert responses == [["error", {"type": "unknownMethod"}, "t0"]]


async def test_dispatch_basic_call():
    @dispatch.method("Core/echo")
    async def echo(ctx, args):
        return dict(args)

    responses, _ = await dispatch.dispatch_request(Ctx(), [["Core/echo", {"hello": "world"}, "t0"]])
    assert responses == [["Core/echo", {"hello": "world"}, "t0"]]


async def test_dispatch_method_error():
    @dispatch.method("Foo/fail")
    async def fail(ctx, args):
        raise InvalidArguments("bad", arguments=["x"])

    responses, _ = await dispatch.dispatch_request(Ctx(), [["Foo/fail", {}, "t0"]])
    assert responses == [
        ["error", {"type": "invalidArguments", "arguments": ["x"], "description": "bad"}, "t0"]
    ]


async def test_dispatch_unexpected_exception_becomes_server_fail():
    @dispatch.method("Foo/boom")
    async def boom(ctx, args):
        raise RuntimeError("kaboom")

    responses, _ = await dispatch.dispatch_request(Ctx(), [["Foo/boom", {}, "t0"]])
    assert responses[0][0] == "error"
    assert responses[0][1]["type"] == "serverFail"
    assert responses[0][2] == "t0"


async def test_back_reference_resolution():
    @dispatch.method("A/get")
    async def a_get(ctx, args):
        return {"list": [{"id": "1"}, {"id": "2"}]}

    @dispatch.method("B/get")
    async def b_get(ctx, args):
        return {"received": args["ids"]}

    calls = [
        ["A/get", {}, "t0"],
        ["B/get", {"#ids": {"resultOf": "t0", "path": "/list/*/id"}}, "t1"],
    ]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[1] == ["B/get", {"received": ["1", "2"]}, "t1"]


async def test_back_reference_to_missing_tag_is_method_error():
    @dispatch.method("B/get")
    async def b_get(ctx, args):
        return {}

    calls = [["B/get", {"#ids": {"resultOf": "nope", "path": "/list/*/id"}}, "t1"]]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[0][0] == "error"
    assert responses[0][1]["type"] == "invalidResultReference"
    assert responses[0][2] == "t1"


async def test_back_reference_to_errored_call_is_method_error():
    @dispatch.method("B/get")
    async def b_get(ctx, args):
        return {}

    calls = [
        ["Foo/bar", {}, "t0"],  # unknown method -> error
        ["B/get", {"#ids": {"resultOf": "t0", "path": "/list"}}, "t1"],
    ]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[1][0] == "error"
    assert responses[1][1]["type"] == "invalidResultReference"


async def test_malformed_call_shape():
    responses, _ = await dispatch.dispatch_request(Ctx(), [["only-one-item"]])
    assert responses[0][0] == "error"
    assert responses[0][1]["type"] == "invalidArguments"


async def test_creation_id_reference_substituted_as_value():
    @dispatch.method("Email/set")
    async def email_set(ctx, args):
        return {"created": {"aerc": {"id": "Ereal123"}}}

    @dispatch.method("EmailSubmission/set")
    async def submission_set(ctx, args):
        return {"received_email_id": args["create"]["s1"]["emailId"]}

    calls = [
        ["Email/set", {"create": {"aerc": {"subject": "hi"}}}, "t0"],
        ["EmailSubmission/set", {"create": {"s1": {"emailId": "#aerc"}}}, "t1"],
    ]
    responses, created_ids = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[1][1]["received_email_id"] == "Ereal123"
    assert created_ids == {"aerc": "Ereal123"}


async def test_creation_id_reference_substituted_as_dict_key():
    @dispatch.method("Mailbox/set")
    async def mailbox_set(ctx, args):
        return {"created": {"newbox": {"id": "Mreal456"}}}

    @dispatch.method("Email/set")
    async def email_set(ctx, args):
        return {"mailbox_ids_seen": args["create"]["e1"]["mailboxIds"]}

    calls = [
        ["Mailbox/set", {"create": {"newbox": {"name": "Projects"}}}, "t0"],
        [
            "Email/set",
            {"create": {"e1": {"mailboxIds": {"#newbox": True}}}},
            "t1",
        ],
    ]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[1][1]["mailbox_ids_seen"] == {"Mreal456": True}


async def test_creation_id_reference_to_unknown_id_is_method_error():
    @dispatch.method("EmailSubmission/set")
    async def submission_set(ctx, args):
        return {}

    calls = [["EmailSubmission/set", {"create": {"s1": {"emailId": "#nonexistent"}}}, "t0"]]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[0][0] == "error"
    assert responses[0][1]["type"] == "invalidArguments"


async def test_seeded_created_ids_available_from_the_start():
    @dispatch.method("EmailSubmission/set")
    async def submission_set(ctx, args):
        return {"received": args["create"]["s1"]["emailId"]}

    calls = [["EmailSubmission/set", {"create": {"s1": {"emailId": "#fromEarlierRequest"}}}, "t0"]]
    responses, _ = await dispatch.dispatch_request(
        Ctx(), calls, created_ids={"fromEarlierRequest": "Eold789"}
    )
    assert responses[0][1]["received"] == "Eold789"


async def test_plain_strings_not_matching_creation_ref_pattern_untouched():
    @dispatch.method("Core/echo")
    async def echo(ctx, args):
        return dict(args)

    calls = [["Core/echo", {"subject": "no hashtag here"}, "t0"]]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[0][1]["subject"] == "no hashtag here"
