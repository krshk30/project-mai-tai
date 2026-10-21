from __future__ import annotations

import logging
import re
import sys


_WEBULL_SDK_LOGGER_NAME = "webull.core.client"
_REDACTED = "[REDACTED]"
_SENSITIVE_WEBULL_FIELD = (
    r"(?:x-app-key|x-signature|authorization|proxy-authorization|x-api-key|api-key|"
    r"api_key|x-access-token|access-token|access_token|refresh-token|refresh_token|"
    r"cookie|set-cookie|x-app-secret|app-secret|app_secret|account(?:_|-)?id)"
)
_QUOTED_SENSITIVE_WEBULL_VALUE = re.compile(
    rf"(?P<prefix>(?P<key_quote>['\"]?){_SENSITIVE_WEBULL_FIELD}"
    rf"(?P=key_quote)\s*[:=]\s*)(?P<value_quote>['\"])[^'\"]*(?P=value_quote)",
    re.IGNORECASE,
)
_ESCAPED_QUOTED_SENSITIVE_WEBULL_VALUE = re.compile(
    rf'(?P<prefix>\\"{_SENSITIVE_WEBULL_FIELD}\\"\s*[:=]\s*\\")'
    r'(?P<value>[^"\\\r\n]*)(?P<suffix>\\")',
    re.IGNORECASE,
)
_BARE_SENSITIVE_WEBULL_VALUE = re.compile(
    rf"(?P<prefix>(?P<key_quote>['\"]?){_SENSITIVE_WEBULL_FIELD}"
    rf"(?P=key_quote)\s*[:=]\s*+)(?P<value>(?!['\"])[^,\n}}\]]+)",
    re.IGNORECASE,
)


def _redact_webull_sdk_request(message: str) -> str:
    redacted = _ESCAPED_QUOTED_SENSITIVE_WEBULL_VALUE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}{match.group('suffix')}",
        message,
    )
    redacted = _QUOTED_SENSITIVE_WEBULL_VALUE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('value_quote')}"
            f"{_REDACTED}{match.group('value_quote')}"
        ),
        redacted,
    )
    return _BARE_SENSITIVE_WEBULL_VALUE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}",
        redacted,
    )


class _WebullSdkRequestRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != _WEBULL_SDK_LOGGER_NAME:
            return True
        message = record.getMessage()
        redacted = _redact_webull_sdk_request(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_WEBULL_SDK_REQUEST_REDACTION_FILTER = _WebullSdkRequestRedactionFilter()


def _install_webull_sdk_request_redaction() -> None:
    sdk_logger = logging.getLogger(_WEBULL_SDK_LOGGER_NAME)
    if _WEBULL_SDK_REQUEST_REDACTION_FILTER not in sdk_logger.filters:
        sdk_logger.addFilter(_WEBULL_SDK_REQUEST_REDACTION_FILTER)


def configure_logging(service_name: str, level: str = "INFO") -> logging.Logger:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    if service_name == "oms-risk":
        _install_webull_sdk_request_redaction()
    logger = logging.getLogger(service_name)
    logger.debug("logging configured")
    return logger
