import importlib
import ssl
import sys
import types


def _load_config(monkeypatch, *, db_ssl: str, verify: str):
    monkeypatch.setenv("DB_SSL", db_ssl)
    monkeypatch.setenv("DB_SSL_VERIFY", verify)
    monkeypatch.delenv("DB_SSL_CA_FILE", raising=False)

    if "certifi" not in sys.modules:
        certifi_stub = types.SimpleNamespace(where=lambda: "/etc/ssl/certs/ca-certificates.crt")
        sys.modules["certifi"] = certifi_stub

    import config

    return importlib.reload(config)


def test_db_ssl_verify_true_context(monkeypatch):
    cfg = _load_config(monkeypatch, db_ssl="require", verify="true")

    ctx = cfg.get_db_ssl_config()

    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_db_ssl_verify_false_context(monkeypatch):
    cfg = _load_config(monkeypatch, db_ssl="require", verify="false")

    ctx = cfg.get_db_ssl_config()

    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
