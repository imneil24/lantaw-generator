import logging
from app.logging_conf import configure_logging


def test_redacts_secret_in_log_message(caplog):
    configure_logging(secrets=["super-secret-value"])
    logger = logging.getLogger("test.redact")
    with caplog.at_level(logging.INFO):
        logger.info("token=super-secret-value used")
    assert "super-secret-value" not in caplog.text
    assert "[REDACTED]" in caplog.text
