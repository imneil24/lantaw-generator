import logging


class _RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._secrets:
            if secret in msg:
                msg = msg.replace(secret, "[REDACTED]")
        record.msg = msg
        record.args = ()
        return True


def configure_logging(secrets: list[str]) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    redact_filter = _RedactFilter(secrets)

    # Filters attached to a Logger only run when that logger originates the
    # record, not when it propagates up from a child logger. Attaching to
    # every handler instead guarantees redaction regardless of which logger
    # emitted the record (including handlers added later, e.g. by caplog).
    root.addFilter(redact_filter)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    for existing_handler in root.handlers:
        if not any(isinstance(f, _RedactFilter) for f in existing_handler.filters):
            existing_handler.addFilter(redact_filter)
