import pytest
import importlib
import ssl
import sys
import types


def _load_config(
    monkeypatch,
    *,
    db_ssl: str | None = None,
    verify: str | None = None,
    database_url: str | None = None,
):
    if db_ssl is None:
        monkeypatch.delenv("DB_SSL", raising=False)
    else:
        monkeypatch.setenv("DB_SSL", db_ssl)

    if verify is None:
        monkeypatch.delenv("DB_SSL_VERIFY", raising=False)
    else:
        monkeypatch.setenv("DB_SSL_VERIFY", verify)

    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)

    monkeypatch.delenv("DB_SSL_CA_FILE", raising=False)

    if "certifi" not in sys.modules:
        certifi_stub = types.SimpleNamespace(where=lambda: "/etc/ssl/certs/ca-certificates.crt")
        sys.modules["certifi"] = certifi_stub

    import config

    return importlib.reload(config)


def test_db_ssl_require_defaults_to_encrypted_no_verify(monkeypatch):
    cfg = _load_config(monkeypatch, db_ssl="require", verify=None)

    ctx = cfg.get_db_ssl_config()

    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_db_ssl_verify_full_defaults_to_required_verification(monkeypatch):
    cfg = _load_config(monkeypatch, db_ssl="verify-full", verify=None)

    ctx = cfg.get_db_ssl_config()

    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_db_ssl_require_with_explicit_verify_true_is_ignored(monkeypatch):
    cfg = _load_config(monkeypatch, db_ssl="require", verify="true")

    ctx = cfg.get_db_ssl_config()

    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_database_url_sslmode_derives_db_ssl_when_unset(monkeypatch):
    cfg = _load_config(
        monkeypatch,
        db_ssl=None,
        verify=None,
        database_url="postgresql://u:p@h:5432/db?sslmode=verify-full",
    )

    assert cfg.DB_SSL == "verify-full"
    assert cfg.DB_SSL_VERIFY is True


def test_verify_full_without_ca_bundle_raises_clear_error(monkeypatch):
    monkeypatch.setenv("DB_SSL", "verify-full")
    monkeypatch.delenv("DB_SSL_CA_FILE", raising=False)
    monkeypatch.delenv("DB_SSL_VERIFY", raising=False)
    monkeypatch.setitem(sys.modules, "certifi", None)

    import config

    cfg = importlib.reload(config)
    with pytest.raises(RuntimeError, match="Install certifi or set DB_SSL_CA_FILE"):
        cfg.get_db_ssl_config()
