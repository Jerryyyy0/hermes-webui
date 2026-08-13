from __future__ import annotations

import io
import logging
import sys

import pytest

from integration.project_logging.config import (
    configure_logging,
    get_logger,
    is_debug_level,
    log_gateway_line,
    log_error,
    log_info,
    log_warning,
    resolve_log_level,
)
from integration.project_logging.formatting import sanitize_fields


@pytest.fixture
def stderr_capture(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    configure_logging(level=logging.DEBUG, force=True, stream=stream)
    yield stream
    configure_logging(level=logging.INFO, force=True, stream=stream)


def test_resolve_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_LOG_LEVEL", raising=False)
    assert resolve_log_level() == logging.INFO


def test_resolve_log_level_env(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "warning")
    assert resolve_log_level() == logging.WARNING


def test_configure_logging_is_idempotent(stderr_capture):
    configure_logging(level=logging.INFO, force=True)
    handlers_before = len(logging.getLogger().handlers)
    configure_logging(level=logging.INFO)
    assert len(logging.getLogger().handlers) == handlers_before


def test_module_logger_emits_to_stderr(stderr_capture):
    get_logger("api.example").warning("module-warning")
    output = stderr_capture.getvalue()
    assert "module-warning" in output
    assert "api.example" in output


def test_console_logger_emits_level_prefix(stderr_capture):
    log_warning("GET /health -> 200 1.0ms")
    output = stderr_capture.getvalue()
    assert "WARNING GET /health -> 200 1.0ms" in output


def test_debug_level_gate(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "INFO")
    assert is_debug_level() is False
    monkeypatch.setenv("HERMES_WEBUI_LOG_LEVEL", "DEBUG")
    assert is_debug_level() is True


def test_sanitize_fields_redacts_sensitive_keys():
    safe = sanitize_fields(
        {
            "operation": "lookup",
            "access_token": "secret-token",
            "authorization": "Bearer abc",
            "status": 200,
        }
    )
    assert safe["operation"] == "lookup"
    assert safe["status"] == 200
    assert safe["access_token"] == "<redacted>"
    assert safe["authorization"] == "<redacted>"


def test_log_level_filters_debug(stderr_capture):
    configure_logging(level=logging.WARNING, force=True, stream=stderr_capture)
    log_info("hidden-info")
    log_warning("visible-warning")
    output = stderr_capture.getvalue()
    assert "hidden-info" not in output
    assert "visible-warning" in output


def test_log_error_visible_at_warning_level(stderr_capture):
    configure_logging(level=logging.WARNING, force=True, stream=stderr_capture)
    log_error("visible-error")
    assert "visible-error" in stderr_capture.getvalue()


def test_gateway_lines_bypass_webui_log_level(stderr_capture):
    configure_logging(level=logging.WARNING, force=True, stream=stderr_capture)

    log_gateway_line(logging.DEBUG, "[gateway:default] debug-visible")

    assert "DEBUG [gateway:default] debug-visible" in stderr_capture.getvalue()
