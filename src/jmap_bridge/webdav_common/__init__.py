"""Shared WebDAV logic used by both the CalDAV and CardDAV backends -
currently just href canonicalization (href.py). RFC 6578 sync-collection
REPORT bodies are hand-rolled independently in each backend's client.py
rather than factored here, since CalDAV's and CardDAV's differ only in
their query-body namespace/element names, which didn't turn out to be
worth abstracting over for the amount of code involved.
"""
