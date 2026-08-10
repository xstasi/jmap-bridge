import pytest

from jmap_bridge.config import ConfigError, load_config

EXAMPLE_CONFIG = "/home/sonne/local/lab/jmap/config/domains.example.yaml"


def test_loads_example_config():
    config = load_config(EXAMPLE_CONFIG)
    assert "example.com" in config.domains
    assert config.domains["example.com"].imap.host == "imap.example.com"
    assert config.domains["example.com"].caldav.url == "https://dav.example.com/caldav/"
    assert config.domains["mail-only.example.org"].caldav is None


def test_defaults_applied():
    config = load_config(EXAMPLE_CONFIG)
    mail_only = config.domains["mail-only.example.org"]
    assert mail_only.imap.port == 993
    assert mail_only.imap.tls == "implicit"
    assert mail_only.smtp.port == 587
    assert mail_only.connection_pool_max_per_user == 4


def test_domain_for_email():
    config = load_config(EXAMPLE_CONFIG)
    assert config.domain_for_email("alice@Example.COM") == "example.com"
    assert config.domain_for_email("not-an-email") is None


def test_get_domain_for_email():
    config = load_config(EXAMPLE_CONFIG)
    assert config.get_domain_for_email("alice@example.com").imap.host == "imap.example.com"
    assert config.get_domain_for_email("alice@unknown.tld") is None


def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/path/domains.yaml")


def test_invalid_yaml_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("domains: [this, is, not, a, mapping")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_required_field_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("domains:\n  example.com:\n    imap:\n      host: imap.example.com\n")
    with pytest.raises(ConfigError):
        load_config(bad)  # missing required smtp block
