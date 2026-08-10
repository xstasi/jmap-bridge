from jmap_bridge.id_redirect import IdRedirectCache

ACCOUNT = ("example.com", "alice@example.com")
OTHER_ACCOUNT = ("example.com", "bob@example.com")


def test_resolve_unknown_id_returns_it_unchanged():
    cache = IdRedirectCache()
    assert cache.resolve(ACCOUNT, "Esomeid") == "Esomeid"


def test_record_and_resolve_single_hop():
    cache = IdRedirectCache()
    cache.record(ACCOUNT, "Eold", "Enew")
    assert cache.resolve(ACCOUNT, "Eold") == "Enew"


def test_resolve_follows_multi_hop_chain():
    cache = IdRedirectCache()
    cache.record(ACCOUNT, "E1", "E2")
    cache.record(ACCOUNT, "E2", "E3")
    assert cache.resolve(ACCOUNT, "E1") == "E3"


def test_re_recording_an_id_updates_its_chain_target():
    """Recording old_id -> new_id again (e.g. a message moves a second
    time) should update in place, effectively compressing the chain
    rather than growing it unboundedly.
    """
    cache = IdRedirectCache()
    cache.record(ACCOUNT, "E1", "E2")
    cache.record(ACCOUNT, "E1", "E3")  # E1 moved again, directly to E3
    assert cache.resolve(ACCOUNT, "E1") == "E3"


def test_cycle_is_safely_detected():
    cache = IdRedirectCache()
    cache.record(ACCOUNT, "E1", "E2")
    cache.record(ACCOUNT, "E2", "E1")  # pathological, shouldn't happen in practice
    assert cache.resolve(ACCOUNT, "E1") == "E1"


def test_recording_same_id_as_target_is_a_no_op():
    cache = IdRedirectCache()
    cache.record(ACCOUNT, "E1", "E1")
    assert cache.resolve(ACCOUNT, "E1") == "E1"


def test_accounts_are_isolated():
    """A redirect recorded for one account must never resolve for
    another - this is a security boundary (see id_redirect.py's
    docstring), not just an organizational nicety.
    """
    cache = IdRedirectCache()
    cache.record(ACCOUNT, "Eshared", "Enew")
    assert cache.resolve(OTHER_ACCOUNT, "Eshared") == "Eshared"


def test_per_account_entries_are_bounded():
    cache = IdRedirectCache(max_entries_per_account=3)
    for i in range(5):
        cache.record(ACCOUNT, f"Eold{i}", f"Enew{i}")
    # Oldest entries evicted first - the two most recent must survive.
    assert cache.resolve(ACCOUNT, "Eold4") == "Enew4"
    assert cache.resolve(ACCOUNT, "Eold3") == "Enew3"
    assert cache.resolve(ACCOUNT, "Eold0") == "Eold0"  # evicted, so unresolved
