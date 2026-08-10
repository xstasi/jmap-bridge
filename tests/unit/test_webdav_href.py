from jmap_bridge.webdav_common.href import canonicalize_collection_href, canonicalize_href_path


def test_canonicalize_href_path_strips_scheme_and_host():
    assert canonicalize_href_path("https://dav.example.com/alice/work/") == "/alice/work/"


def test_canonicalize_href_path_unquotes_percent_encoding():
    assert canonicalize_href_path("https://dav.example.com/alice%40example.com/") == "/alice@example.com/"


def test_canonicalize_href_path_does_not_force_trailing_slash():
    assert canonicalize_href_path("https://dav.example.com/alice/card.vcf") == "/alice/card.vcf"


def test_canonicalize_collection_href_adds_trailing_slash():
    assert canonicalize_collection_href("https://dav.example.com/alice/work") == "/alice/work/"


def test_canonicalize_collection_href_stable_across_absolute_and_relative_forms():
    absolute = canonicalize_collection_href("http://dav.example.com/alice%40example.com/work/")
    relative = canonicalize_collection_href("/alice@example.com/work/")
    assert absolute == relative
