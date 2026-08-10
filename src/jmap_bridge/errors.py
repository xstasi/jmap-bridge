"""JMAP error types (RFC 8620 SS3.6).

Two distinct error shapes exist in JMAP:

- **Request-level** errors (SS3.6.1): the whole HTTP request is malformed
  (not JSON, missing methodCalls, unknown capability urn, over a size
  limit). Returned as an RFC 7807 `application/problem+json` HTTP response,
  never inside `methodResponses`. `RequestError` subclasses model these.

- **Method-level** errors (SS3.6.2): an individual method call within an
  otherwise-valid request fails. Returned as a normal method response
  `["error", {"type": ..., ...}, tag]` alongside successful calls.
  `MethodError` subclasses model these; `dispatch.py` catches them.
"""

from __future__ import annotations

from typing import Any


class RequestError(Exception):
    """A malformed JMAP HTTP request (RFC 8620 SS3.6.1)."""

    error_type: str = "urn:ietf:params:jmap:error:serverFail"
    status: int = 500
    title: str = "Server Fail"

    def __init__(self, detail: str | None = None):
        self.detail = detail
        super().__init__(detail or self.title)

    def to_problem(self) -> dict[str, Any]:
        problem: dict[str, Any] = {"type": self.error_type, "status": self.status, "title": self.title}
        if self.detail:
            problem["detail"] = self.detail
        return problem


class NotJSON(RequestError):
    error_type = "urn:ietf:params:jmap:error:notJSON"
    status = 400
    title = "Not JSON"


class NotRequest(RequestError):
    error_type = "urn:ietf:params:jmap:error:notRequest"
    status = 400
    title = "Not a Request object"


class UnknownCapability(RequestError):
    error_type = "urn:ietf:params:jmap:error:unknownCapability"
    status = 400
    title = "Unknown Capability"


class RequestLimit(RequestError):
    error_type = "urn:ietf:params:jmap:error:limit"
    status = 400
    title = "Request Limit Exceeded"


class Unauthorized(RequestError):
    error_type = "urn:ietf:params:jmap:error:unauthorized"
    status = 401
    title = "Unauthorized"


class MethodError(Exception):
    """Base class for method-level errors (RFC 8620 SS3.6.2). Raise these
    from any `types/*.py` handler; `dispatch.py` catches them and turns
    them into the `["error", {...}, tag]` response item.
    """

    error_type: str = "serverFail"

    def __init__(self, message: str | None = None, **extra: Any):
        self.message = message
        self.extra = extra
        super().__init__(message or self.error_type)

    def to_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.error_type, **self.extra}
        if self.message:
            body["description"] = self.message
        return body


class UnknownMethod(MethodError):
    error_type = "unknownMethod"


class InvalidArguments(MethodError):
    error_type = "invalidArguments"

    def __init__(self, message: str | None = None, arguments: list[str] | None = None):
        super().__init__(message, **({"arguments": arguments} if arguments else {}))


class InvalidResultReference(MethodError):
    error_type = "invalidResultReference"


class Forbidden(MethodError):
    error_type = "forbidden"


class AccountNotFound(MethodError):
    error_type = "accountNotFound"


class AccountNotSupportedByMethod(MethodError):
    error_type = "accountNotSupportedByMethod"


class AccountReadOnly(MethodError):
    error_type = "accountReadOnly"


class RequestTooLarge(MethodError):
    error_type = "requestTooLarge"


class CannotCalculateChanges(MethodError):
    error_type = "cannotCalculateChanges"


class StateMismatch(MethodError):
    error_type = "stateMismatch"


class InvalidPatch(MethodError):
    error_type = "invalidPatch"


class InvalidProperties(MethodError):
    """RFC 8620 SS5.3 SetError - a create/update's properties are invalid,
    e.g. an Email left with an empty mailboxIds (every Email must belong
    to at least one mailbox). `properties` names which ones.
    """

    error_type = "invalidProperties"

    def __init__(self, message: str | None = None, properties: list[str] | None = None):
        super().__init__(message, **({"properties": properties} if properties else {}))


class WillDestroy(MethodError):
    error_type = "willDestroy"


class OverQuota(MethodError):
    error_type = "overQuota"


class TooManyChanges(MethodError):
    error_type = "tooManyChanges"


class UnsupportedFilter(MethodError):
    error_type = "unsupportedFilter"


class UnsupportedSort(MethodError):
    error_type = "unsupportedSort"


class AnchorNotFound(MethodError):
    error_type = "anchorNotFound"


class ServerFail(MethodError):
    error_type = "serverFail"


class ServerUnavailable(MethodError):
    error_type = "serverUnavailable"
