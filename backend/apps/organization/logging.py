import logging
import re


_RECOVERY_PATH = re.compile(
    r"(/accounts/(?:activate|password-reset)/)[^\s/?#]+/[^\s/?#]+",
    re.IGNORECASE,
)


class ActivationTokenRedactionFilter(logging.Filter):
    """Redact uid/token URL segments from application and development-server logs."""

    def filter(self, record):
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = _RECOVERY_PATH.sub(r"\1[redacted]/[redacted]", rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True
