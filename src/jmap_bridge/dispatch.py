"""JMAP method dispatch (RFC 8620 SS3.3-SS3.7): the `POST /api` request
envelope, `"Type/verb"` -> handler routing, result-reference (`#/...`,
SS3.7) resolution, and creation-id reference (`"#creationId"`, SS3.6.1)
substitution.

Handlers are plain async functions registered via the `@method(...)`
decorator: `async def handler(ctx, args: dict) -> dict`, returning the
method's *result* properties only (not the `[name, args, tag]` envelope,
which this module builds). Raise a `jmap_bridge.errors.MethodError`
subclass to produce a method-level error response.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Protocol

from jmap_bridge.errors import (
    InvalidArguments,
    InvalidResultReference,
    MethodError,
    ServerFail,
    UnknownMethod,
)

logger = logging.getLogger(__name__)

MethodHandler = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, MethodHandler] = {}


class MethodContext(Protocol):
    """Duck-typed per-request context passed to every handler. Concrete
    implementation (holding config/pool/credentials/account_id) lives in
    app.py once auth.py and pool.py exist — kept as a Protocol here so
    dispatch.py has no dependency on those modules.
    """

    account_id: str


def method(name: str) -> Callable[[MethodHandler], MethodHandler]:
    """Register `handler` as the implementation of JMAP method `name`
    (e.g. "Mailbox/get"). Raises at import time on accidental double
    registration, since that's always a bug.
    """

    def decorator(handler: MethodHandler) -> MethodHandler:
        if name in _REGISTRY and _REGISTRY[name] is not handler:
            raise RuntimeError(f"duplicate method registration for {name!r}")
        _REGISTRY[name] = handler
        return handler

    return decorator


def registered_methods() -> list[str]:
    return sorted(_REGISTRY)


def _resolve_path(value: Any, segments: list[str]) -> Any:
    """Walk a back-reference path (RFC 8620 SS3.7) into a prior method's
    result. `*` means "for each element of this array, resolve the rest of
    the path against it, then flatten one level" (e.g. "/list/*/id").
    """
    if not segments:
        return value
    seg, rest = segments[0], segments[1:]
    if seg == "*":
        if not isinstance(value, list):
            raise InvalidResultReference(f"'*' applied to non-array value: {value!r}")
        out: list[Any] = []
        for item in value:
            resolved = _resolve_path(item, rest)
            if isinstance(resolved, list):
                out.extend(resolved)
            else:
                out.append(resolved)
        return out
    if isinstance(value, dict):
        if seg not in value:
            raise InvalidResultReference(f"path segment {seg!r} not found")
        return _resolve_path(value[seg], rest)
    raise InvalidResultReference(f"cannot resolve segment {seg!r} into {type(value).__name__}")


def resolve_backref(results_by_tag: dict[str, dict[str, Any]], result_of: str, path: str) -> Any:
    if result_of not in results_by_tag:
        raise InvalidResultReference(f"no prior result tagged {result_of!r}")
    entry = results_by_tag[result_of]
    if entry["name"] == "error":
        raise InvalidResultReference(f"referenced call {result_of!r} returned an error")
    segments = [s for s in path.split("/") if s != ""]
    return _resolve_path(entry["args"], segments)


def resolve_args(args: dict[str, Any], results_by_tag: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Resolve `#property` back-references (RFC 8620 SS3.7) in a method
    call's arguments against prior results in the same request.
    """
    resolved: dict[str, Any] = {}
    for key, value in args.items():
        if key.startswith("#"):
            out_key = key[1:]
            if not isinstance(value, dict) or "resultOf" not in value or "path" not in value:
                raise InvalidResultReference(f"malformed result reference for {key!r}")
            resolved[out_key] = resolve_backref(results_by_tag, value["resultOf"], value["path"])
        else:
            resolved[key] = value
    return resolved


_CREATION_REF_RE = re.compile(r"^#(.+)$")

# RFC 8620 SS3.6.1 / SS5.3: a "#creationId" reference is only valid where a
# property is itself typed as an Id (or Id-keyed map, or Id[] list) - "Some
# records may hold references to other records (foreign keys)... the client
# refers to the new record using its creation id prefixed with a '#'".
# Substitution must NOT run on every string in the whole args tree: found
# live that Bulwark webmail's Calendar/set create sends a `color` property
# like "#3b82f6" (a CSS hex color, not a reference), which a tree-wide scan
# misreads as a reference to an unknown creation id "3b82f6" and rejects
# the entire create with invalidArguments. These are every Id-typed
# property actually used by a create/update PatchObject across this
# bridge's supported types.
_ID_VALUE_PROPERTIES = frozenset({"emailId", "identityId"})
_ID_KEYED_MAP_PROPERTIES = frozenset(
    {"mailboxIds", "calendarIds", "addressBookIds", "onSuccessUpdateEmail"}
)
_ID_LIST_PROPERTIES = frozenset({"onSuccessDestroyEmail"})


def _substitute_id_string(value: str, created_ids: dict[str, str]) -> str:
    match = _CREATION_REF_RE.match(value)
    if not match:
        return value
    creation_id = match.group(1)
    if creation_id not in created_ids:
        raise InvalidArguments(f"reference to unknown creation id {creation_id!r}")
    return created_ids[creation_id]


def substitute_created_ids(value: Any, created_ids: dict[str, str]) -> Any:
    """Resolve creation-id references (RFC 8620 SS3.6.1) - see
    `_ID_VALUE_PROPERTIES` et al above for exactly which properties this
    applies to. Recurses through the whole argument tree looking for
    those specific property names (they can appear at any depth, e.g.
    nested under `create`/`update`), but only ever substitutes a string
    when it's reached via one of those names - every other string,
    however "#"-shaped, passes through untouched. Runs after
    `resolve_args`, on its output.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, sub_value in value.items():
            if key in _ID_VALUE_PROPERTIES and isinstance(sub_value, str):
                result[key] = _substitute_id_string(sub_value, created_ids)
            elif key in _ID_KEYED_MAP_PROPERTIES and isinstance(sub_value, dict):
                result[key] = {
                    (_substitute_id_string(sub_key, created_ids) if isinstance(sub_key, str) else sub_key): sub_sub_value
                    for sub_key, sub_sub_value in sub_value.items()
                }
            elif key in _ID_LIST_PROPERTIES and isinstance(sub_value, list):
                result[key] = [
                    _substitute_id_string(item, created_ids) if isinstance(item, str) else item
                    for item in sub_value
                ]
            else:
                result[key] = substitute_created_ids(sub_value, created_ids)
        return result
    if isinstance(value, list):
        return [substitute_created_ids(item, created_ids) for item in value]
    return value


_SET_RESPONSE_NULLABLE_KEYS = ("created", "updated", "destroyed", "notCreated", "notUpdated", "notDestroyed")


def _normalize_set_response(result: dict[str, Any]) -> None:
    """RFC 8620 SS5.3: these six `Foo/set` response arguments are typed
    `...|null` and MUST be `null` specifically when there is nothing to
    report - not an empty object/array. Every handler builds them as
    always-present dicts/lists for its own internal convenience; found
    live that this breaks real clients (confirmed: Bulwark webmail's
    createDraft()/createCalendar() do `if (result.notCreated) throw ...`,
    and `{}` is truthy in JS even when empty - so every *successful*
    create was being reported to the user as a failure). Normalized once
    here, for every /set response, rather than in each handler.
    """
    for key in _SET_RESPONSE_NULLABLE_KEYS:
        if key in result and not result[key]:
            result[key] = None


def _record_created_ids(result: dict[str, Any], created_ids: dict[str, str]) -> None:
    """After any `Foo/set` call, remember creationId -> server-assigned id
    for every object it created, so later calls in the same request can
    reference them via `substitute_created_ids`.
    """
    created = result.get("created")
    if not isinstance(created, dict):
        return
    for creation_id, obj in created.items():
        if isinstance(obj, dict) and isinstance(obj.get("id"), str):
            created_ids[creation_id] = obj["id"]


async def dispatch_request(
    ctx: Any, method_calls: list[list[Any]], created_ids: dict[str, str] | None = None
) -> tuple[list[list[Any]], dict[str, str]]:
    """Execute a `methodCalls` array against the handler registry.
    Returns `(methodResponses, createdIds)` ready to embed in the JMAP
    response object (RFC 8620 SS3.4/SS3.5).
    """
    results_by_tag: dict[str, dict[str, Any]] = {}
    method_responses: list[list[Any]] = []
    created_ids = dict(created_ids or {})

    for call in method_calls:
        try:
            name, args, tag = call
        except (ValueError, TypeError):
            method_responses.append(
                ["error", InvalidArguments("malformed method call").to_response(), "unknown"]
            )
            continue

        handler = _REGISTRY.get(name)
        if handler is None:
            error_body = UnknownMethod().to_response()
            method_responses.append(["error", error_body, tag])
            results_by_tag[tag] = {"name": "error", "args": error_body}
            continue

        try:
            resolved_args = resolve_args(args, results_by_tag)
            resolved_args = substitute_created_ids(resolved_args, created_ids)
            result = await handler(ctx, resolved_args)
        except MethodError as exc:
            error_body = exc.to_response()
            method_responses.append(["error", error_body, tag])
            results_by_tag[tag] = {"name": "error", "args": error_body}
            continue
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all boundary
            logger.exception("unhandled error in method %s (tag=%s)", name, tag)
            error_body = ServerFail(str(exc)).to_response()
            method_responses.append(["error", error_body, tag])
            results_by_tag[tag] = {"name": "error", "args": error_body}
            continue

        _record_created_ids(result, created_ids)
        if name.endswith("/set"):
            _normalize_set_response(result)
            # A later call in the same batch must see state reflecting
            # this mutation, not a cached pre-mutation snapshot (see
            # RequestContext._cache) - getattr'd since minimal fake
            # contexts in unit tests don't need to implement this.
            invalidate = getattr(ctx, "invalidate_cache", None)
            if invalidate is not None:
                invalidate()
        method_responses.append([name, result, tag])
        results_by_tag[tag] = {"name": name, "args": result}

    return method_responses, created_ids
