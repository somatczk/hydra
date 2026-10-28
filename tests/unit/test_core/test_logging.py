"""setup_logging renders every record, stdlib and structlog alike, as complete JSON lines."""

from __future__ import annotations

import json
import logging
import logging.config
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import structlog
import uvicorn
from uvicorn.config import LOGGING_CONFIG

from hydra.core.logging import get_logger, setup_logging

_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved_uvicorn = {
        name: (list(lg.handlers), lg.propagate, lg.level)
        for name, lg in ((n, logging.getLogger(n)) for n in _UVICORN)
    }
    yield
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (handlers, propagate, level) in saved_uvicorn.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)
    structlog.reset_defaults()


def _divide(a: int, b: int) -> float:
    return a / b


def _lookup(data: dict[str, int], key: str) -> int:
    return data[key]


def _json_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _assert_envelope(record: dict[str, Any], level: str, logger: str) -> None:
    assert record["level"] == level
    assert record["logger"] == logger
    assert datetime.fromisoformat(record["timestamp"]).tzinfo is not None


def test_stdlib_exception_has_traceback_level_and_timestamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO", "json")
    try:
        _divide(1, 0)
    except ZeroDivisionError:
        logging.getLogger("hydra.test.stdlib").exception("division failed")

    [record] = _json_lines(capsys.readouterr().out)
    _assert_envelope(record, "error", "hydra.test.stdlib")
    assert record["event"] == "division failed"
    assert "exc_info" not in record
    assert record["exception"].startswith("Traceback (most recent call last):")
    assert "ZeroDivisionError: division by zero" in record["exception"]
    assert "in _divide" in record["exception"]


def test_structlog_exception_has_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", "json")
    log = get_logger("hydra.test.structlog", session_id="s1")
    try:
        _lookup({}, "missing")
    except KeyError:
        log.exception("lookup failed", symbol="BTCUSDT")

    [record] = _json_lines(capsys.readouterr().out)
    _assert_envelope(record, "error", "hydra.test.structlog")
    assert (record["event"], record["session_id"], record["symbol"]) == (
        "lookup failed",
        "s1",
        "BTCUSDT",
    )
    assert "exc_info" not in record
    assert "KeyError: 'missing'" in record["exception"]


def test_stdlib_record_with_args_gets_level_logger_and_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO", "json")
    logging.getLogger("hydra.test.args").warning("feed %s lagging %ds", "BTCUSDT", 42)
    logging.getLogger("hydra.test.args").debug("below the level")

    [record] = _json_lines(capsys.readouterr().out)
    _assert_envelope(record, "warning", "hydra.test.args")
    assert record["event"] == "feed BTCUSDT lagging 42s"


def test_uvicorn_loggers_are_routed_to_root_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    logging.config.dictConfig(LOGGING_CONFIG)
    setup_logging("INFO", "json")

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/livez", "1.1", 200
    )
    logging.getLogger("uvicorn.error").info("Application startup complete.")

    captured = capsys.readouterr()
    access, startup = _json_lines(captured.out)
    _assert_envelope(access, "info", "uvicorn.access")
    assert access["event"] == '127.0.0.1:5000 - "GET /livez HTTP/1.1" 200'
    _assert_envelope(startup, "info", "uvicorn.error")
    assert captured.err == ""
    for name in _UVICORN:
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True


async def _asgi_app(scope: Any, receive: Any, send: Any) -> None:
    return None


def test_uvicorn_startup_lines_stay_json_only_with_log_config_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging("INFO", "json")
    uvicorn.Config(_asgi_app, log_config=None)
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)

    captured = capsys.readouterr()
    [started] = _json_lines(captured.out)
    _assert_envelope(started, "info", "uvicorn.error")
    assert started["event"] == "Started server process [7]"
    assert captured.err == ""

    uvicorn.Config(_asgi_app)
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "INFO:     Started server process [7]\n")


def test_colored_format_still_renders_the_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", "colored")
    try:
        _divide(1, 0)
    except ZeroDivisionError:
        logging.getLogger("hydra.test.colored").exception("division failed")

    out = capsys.readouterr().out
    assert "division failed" in out
    assert "ZeroDivisionError" in out
