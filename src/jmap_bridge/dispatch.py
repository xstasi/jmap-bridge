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


def substitute_created_ids(value: Any, created_ids: dict[str, str]) -> Any:
    """Resolve creation-id references (RFC 8620 SS3.6.1): anywhere a
    not-yet-assigned object's id is needed - e.g. EmailSubmission/set's
    `emailId` pointing at an Email created earlier in the same request -
    the client passes the literal string `"#<creationId>"` instead of a
    real id. Unlike `resolve_args`'s SS3.7 back-references (top-level
    argument values only, resolved before the handler runs once), this
    can appear anywhere in the argument tree - including as a dict key,
    e.g. `mailboxIds: {"#creationId": true}` - so this recurses. Runs
    after `resolve_args`, on its output.
    """
    if isinstance(value, str):
        match = _CREATION_REF_RE.match(value)
        if not match:
            return value
        creation_id = match.group(1)
        if creation_id not in created_ids:
            raise InvalidArguments(f"reference to unknown creation id {creation_id!r}")
        return created_ids[creation_id]
    if isinstance(value, dict):
        result = {}
        for key, sub_value in value.items():
            match = _CREATION_REF_RE.match(key)
            new_key = created_ids[match.group(1)] if match and match.group(1) in created_ids else key
            result[new_key] = substitute_created_ids(sub_value, created_ids)
        return result
    if isinstance(value, list):
        return [substitute_created_ids(item, created_ids) for item in value]
    return value


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
