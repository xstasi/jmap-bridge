"""Importing this package registers every JMAP method handler (each
module below calls `@method(...)` at import time - see dispatch.py).
"""

from jmap_bridge.types import (  # noqa: F401
    core,
    email,
    identity,
    mailbox,
    submission,
    thread,
)
