import importlib
import ssl

import pytest


@pytest.fixture
def reload_config(monkeypatch):
    def _reload(db_ssl: str, db_ssl_ca_file: str | None = None):
        monkeypatch.setenv("DB_SSL", db_ssl)
        if db_ssl_ca_file is None:
            monkeypatch.delenv("DB_SSL_CA_FILE", raising=False)
        else:
            monkeypatch.setenv("DB_SSL_CA_FILE", db_ssl_ca_file)

        import config

        return importlib.reload(config)

    return _reload


def test_db_ssl_disable_returns_none(reload_config):
    config = reload_config("disable")

    assert config.get_db_ssl_config() is None


@pytest.mark.parametrize("mode", ["require", "prefer", "allow", "true", "1", "yes"])
def test_db_ssl_require_like_modes_disable_verification(reload_config, mode):
    config = reload_config(mode)

    ssl_config = config.get_db_ssl_config()

    assert isinstance(ssl_config, ssl.SSLContext)
    assert ssl_config.verify_mode == ssl.CERT_NONE
    assert ssl_config.check_hostname is False


@pytest.mark.parametrize(
    ("mode", "check_hostname"),
    [("verify-ca", False), ("verify-full", True)],
)
def test_verify_modes_require_certificate_validation(reload_config, mode, check_hostname):
    config = reload_config(mode)

    ssl_config = config.get_db_ssl_config()

    assert isinstance(ssl_config, ssl.SSLContext)
    assert ssl_config.verify_mode == ssl.CERT_REQUIRED
    assert ssl_config.check_hostname is check_hostname


def test_verify_mode_accepts_readable_ca_file(reload_config):
    default_ca_file = ssl.get_default_verify_paths().cafile
    if not default_ca_file:
        pytest.skip("No system CA bundle available in test environment")

    config = reload_config("verify-ca", default_ca_file)

    ssl_config = config.get_db_ssl_config()

    assert isinstance(ssl_config, ssl.SSLContext)
    assert ssl_config.verify_mode == ssl.CERT_REQUIRED


def test_unreadable_ca_file_raises_clear_error(reload_config, tmp_path):
    missing = tmp_path / "missing.pem"
    config = reload_config("verify-ca", str(missing))

    with pytest.raises(RuntimeError, match="DB_SSL_CA_FILE is set but unreadable"):
        config.get_db_ssl_config()
