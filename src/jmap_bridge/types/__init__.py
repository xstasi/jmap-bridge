"""Importing this package registers every JMAP method handler (each
module below calls `@method(...)` at import time - see dispatch.py).
"""

from jmap_bridge.types import (  # noqa: F401
    address_book,
    calendar,
    calendar_event,
    contact_card,
    core,
    email,
    identity,
    mailbox,
    submission,
    thread,
)
