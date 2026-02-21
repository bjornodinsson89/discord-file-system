from utils.redaction import redact
from utils.structured_log import log_event


class _CaptureLogger:
    def __init__(self):
        self.last_extra = None

    def log(self, _level, _event, *, extra=None, exc_info=None):
        self.last_extra = extra
        self.last_exc = exc_info


def test_redact_text_masks_common_secret_patterns():
    text = (
        "DISCORD_TOKEN=abc123 "
        "DATABASE_URL=postgresql://user:pass@host/db "
        "token=mfa.abcdefghijklmnopqrstuvwxyz1234567890"
    )
    redacted = redact(text)
    assert "abc123" not in redacted
    assert "postgresql://" not in redacted
    assert "[REDACTED]" in redacted


def test_structured_log_redacts_payload_fields():
    logger = _CaptureLogger()

    log_event(
        logger,
        20,
        "unit_test",
        user_input="DATABASE_URL=postgres://user:pw@localhost/db",
        fernet_key="something-secret",
    )

    structured = logger.last_extra["structured"]
    assert structured["fernet_key"] == "[REDACTED]"
    assert "postgres://" not in structured["user_input"]
