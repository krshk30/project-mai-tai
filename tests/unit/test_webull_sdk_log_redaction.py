from __future__ import annotations

import io
import logging

from project_mai_tai.log import configure_logging


# Shape copied from one 2026-09-19 OMS SDK record; every identifying value is synthetic.
_REAL_SERVER_EXCEPTION_SHAPE = """ServerException occurred. Request:{
    '_action_name': '/trade/order/detail',
    '_method': 'GET',
    '_header_params': {
        'x-app-key': 'APP_KEY_VALUE',
        'x-signature': 'SIGNATURE_VALUE',
        'Authorization': 'Bearer ACCESS_TOKEN_VALUE',
        'x-api-key': 'SECONDARY_KEY_VALUE'
    },
    '_path_params': {'account_id': 'ACCOUNT_VALUE'},
    '_body': {'client_order_id': 'oms-v2-open-keep-me'}
}
Response:{'msg': 'Too many requests'}
ServerException:HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests, RequestID: keep-request-id
"""
_SENSITIVE_VALUES = (
    "APP_KEY_VALUE",
    "SIGNATURE_VALUE",
    "ACCESS_TOKEN_VALUE",
    "SECONDARY_KEY_VALUE",
    "ACCOUNT_VALUE",
)
_REAL_POST_SERVER_EXCEPTION_SHAPE = r"""ServerException occurred. Request:{
    "_action_name": "/trade/order/cancel",
    "_method": "POST",
    "_header_params": {
        "x-app-key": "POST_APP_KEY_VALUE",
        "x-signature": "POST_SIGNATURE_VALUE"
    },
    "_body_params": {
        "account_id": "POST_ACCOUNT_VALUE",
        "client_order_id": "oms-v2-cancel-keep-me"
    },
    "_content": "{\"account_id\":\"POST_ACCOUNT_VALUE\",\"client_order_id\":\"oms-v2-cancel-keep-me\"}"
}
Response:{"msg": "Too many requests"}
ServerException:HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests, RequestID: keep-post-request-id
"""


def _capture(logger: logging.Logger, message: str) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    logger.disabled = False
    logger.error("%s", message)
    handler.flush()
    return stream.getvalue()


def test_oms_logging_redacts_real_multiline_webull_sdk_record_without_blinding_it(
    monkeypatch,
) -> None:
    sdk_logger = logging.getLogger("webull.core.client")
    control_logger = logging.getLogger("test.webull.core.client.unfiltered")
    sdk_state = (
        list(sdk_logger.handlers),
        list(sdk_logger.filters),
        sdk_logger.level,
        sdk_logger.propagate,
        sdk_logger.disabled,
    )
    control_state = (
        list(control_logger.handlers),
        list(control_logger.filters),
        control_logger.level,
        control_logger.propagate,
        control_logger.disabled,
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: None)

    try:
        control_logger.filters = []
        control = _capture(control_logger, _REAL_SERVER_EXCEPTION_SHAPE)
        assert all(value in control for value in _SENSITIVE_VALUES)

        sdk_logger.filters = []
        configure_logging("oms-risk")
        output = _capture(sdk_logger, _REAL_SERVER_EXCEPTION_SHAPE)

        assert all(value not in output for value in _SENSITIVE_VALUES)
        assert output.count("[REDACTED]") == len(_SENSITIVE_VALUES)
        assert "'x-app-key': '[REDACTED]'" in output
        assert "'_action_name': '/trade/order/detail'" in output
        assert "'_method': 'GET'" in output
        assert "'client_order_id': 'oms-v2-open-keep-me'" in output
        assert "HTTP Status: 429, Code: TOO_MANY_REQUESTS" in output
        assert "RequestID: keep-request-id" in output
        assert "\nResponse:" in output
    finally:
        (
            sdk_logger.handlers,
            sdk_logger.filters,
            sdk_logger.level,
            sdk_logger.propagate,
            sdk_logger.disabled,
        ) = sdk_state
        (
            control_logger.handlers,
            control_logger.filters,
            control_logger.level,
            control_logger.propagate,
            control_logger.disabled,
        ) = control_state


def test_oms_logging_redacts_an_unquoted_credential_value(monkeypatch) -> None:
    sdk_logger = logging.getLogger("webull.core.client")
    state = (
        list(sdk_logger.handlers),
        list(sdk_logger.filters),
        sdk_logger.level,
        sdk_logger.propagate,
        sdk_logger.disabled,
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: None)

    try:
        sdk_logger.filters = []
        configure_logging("oms-risk")
        output = _capture(
            sdk_logger,
            "Authorization: Bearer UNQUOTED_TOKEN, _action_name=/trade/order/detail",
        )

        assert "UNQUOTED_TOKEN" not in output
        assert "Authorization: [REDACTED]" in output
        assert "_action_name=/trade/order/detail" in output
    finally:
        (
            sdk_logger.handlers,
            sdk_logger.filters,
            sdk_logger.level,
            sdk_logger.propagate,
            sdk_logger.disabled,
        ) = state


def test_oms_logging_redacts_account_id_from_escaped_post_content(monkeypatch) -> None:
    sdk_logger = logging.getLogger("webull.core.client")
    control_logger = logging.getLogger("test.webull.core.client.post.unfiltered")
    sdk_state = (
        list(sdk_logger.handlers),
        list(sdk_logger.filters),
        sdk_logger.level,
        sdk_logger.propagate,
        sdk_logger.disabled,
    )
    control_state = (
        list(control_logger.handlers),
        list(control_logger.filters),
        control_logger.level,
        control_logger.propagate,
        control_logger.disabled,
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **_kwargs: None)

    try:
        control_logger.filters = []
        control = _capture(control_logger, _REAL_POST_SERVER_EXCEPTION_SHAPE)
        assert control.count("POST_ACCOUNT_VALUE") == 2

        sdk_logger.filters = []
        configure_logging("oms-risk")
        output = _capture(sdk_logger, _REAL_POST_SERVER_EXCEPTION_SHAPE)

        assert "POST_ACCOUNT_VALUE" not in output
        assert output.count("[REDACTED]") == 4
        assert r"\"account_id\":\"[REDACTED]\"" in output
        assert r"\"client_order_id\":\"oms-v2-cancel-keep-me\"" in output
        assert '"client_order_id": "oms-v2-cancel-keep-me"' in output
        assert '"_action_name": "/trade/order/cancel"' in output
        assert "HTTP Status: 429, Code: TOO_MANY_REQUESTS" in output
        assert "RequestID: keep-post-request-id" in output
    finally:
        (
            sdk_logger.handlers,
            sdk_logger.filters,
            sdk_logger.level,
            sdk_logger.propagate,
            sdk_logger.disabled,
        ) = sdk_state
        (
            control_logger.handlers,
            control_logger.filters,
            control_logger.level,
            control_logger.propagate,
            control_logger.disabled,
        ) = control_state
