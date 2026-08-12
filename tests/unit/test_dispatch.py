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


async def test_hash_prefixed_value_outside_id_property_is_left_untouched():
    """Regression test for a real bug found live: Bulwark webmail's
    Calendar/set create sends `color: "#3b82f6"` (a CSS hex color) - a
    tree-wide creation-id scan misread this as a reference to an unknown
    creation id "3b82f6" and rejected the entire create. Only properties
    that are actually Id-typed (see `_ID_VALUE_PROPERTIES` etc.) may
    ever be substituted; everything else passes through as a literal
    string no matter what it starts with."""

    @dispatch.method("Calendar/set")
    async def calendar_set(ctx, args):
        return {"color_seen": args["create"]["new-calendar"]["color"]}

    calls = [["Calendar/set", {"create": {"new-calendar": {"name": "Work", "color": "#3b82f6"}}}, "t0"]]
    responses, _ = await dispatch.dispatch_request(Ctx(), calls)
    assert responses[0][0] == "Calendar/set"
    assert responses[0][1]["color_seen"] == "#3b82f6"


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


class CtxWithCache(Ctx):
    def __init__(self):
        self.invalidate_calls = 0

    def invalidate_cache(self):
        self.invalidate_calls += 1


async def test_set_call_invalidates_context_cache():
    @dispatch.method("Mailbox/set")
    async def mailbox_set(ctx, args):
        return {}

    ctx = CtxWithCache()
    await dispatch.dispatch_request(ctx, [["Mailbox/set", {}, "t0"]])
    assert ctx.invalidate_calls == 1


async def test_non_set_call_does_not_invalidate_context_cache():
    @dispatch.method("Core/echo")
    async def echo(ctx, args):
        return dict(args)

    ctx = CtxWithCache()
    await dispatch.dispatch_request(ctx, [["Core/echo", {}, "t0"]])
    assert ctx.invalidate_calls == 0


async def test_context_without_invalidate_cache_is_tolerated():
    """Minimal fake contexts (like bare `Ctx`) that don't implement
    invalidate_cache at all must not break dispatch for /set calls."""

    @dispatch.method("Mailbox/set")
    async def mailbox_set(ctx, args):
        return {}

    responses, _ = await dispatch.dispatch_request(Ctx(), [["Mailbox/set", {}, "t0"]])
    assert responses[0][0] == "Mailbox/set"


async def test_set_response_empty_created_etc_normalized_to_null():
    """RFC 8620 SS5.3: created/notCreated/updated/notUpdated/destroyed/
    notDestroyed are `null` when there's nothing to report, not an empty
    object/array. Regression test for a real bug found live: Bulwark
    webmail's createDraft() does `if (result.notCreated) throw ...` -
    since `{}` is truthy in JS, an always-present empty notCreated made
    every successful Email/set create look like a failure client-side
    (confirmed via the IMAP APPEND succeeding twice in the server log,
    once per user retry, while Bulwark reported "Failed to save draft"
    both times)."""

    @dispatch.method("Email/set")
    async def email_set(ctx, args):
        return {
            "created": {"draft1": {"id": "M1"}},
            "notCreated": {},
            "updated": {},
            "notUpdated": {"e2": {"type": "notFound"}},
            "destroyed": [],
            "notDestroyed": {},
        }

    responses, _ = await dispatch.dispatch_request(Ctx(), [["Email/set", {}, "t0"]])
    result = responses[0][1]
    assert result["created"] == {"draft1": {"id": "M1"}}
    assert result["notCreated"] is None
    assert result["updated"] is None
    assert result["notUpdated"] == {"e2": {"type": "notFound"}}
    assert result["destroyed"] is None
    assert result["notDestroyed"] is None


async def test_set_response_normalization_only_applies_to_set_calls():
    @dispatch.method("Email/get")
    async def email_get(ctx, args):
        return {"created": {}}

    responses, _ = await dispatch.dispatch_request(Ctx(), [["Email/get", {}, "t0"]])
    assert responses[0][1]["created"] == {}
