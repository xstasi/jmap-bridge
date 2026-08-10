"""Core/echo (RFC 8620 SS3.10) - trivial method that returns its
arguments unchanged. Useful for client compatibility testing / connectivity
checks.
"""

from __future__ import annotations

from typing import Any

from jmap_bridge.context import RequestContext
from jmap_bridge.dispatch import method


@method("Core/echo")
async def core_echo(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    return dict(args)
