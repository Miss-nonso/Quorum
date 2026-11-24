from __future__ import annotations

import json
import logging
import sys

import pytest

from conductor.core.logging import JsonFormatter, bind_log_context, current_log_context


def _record(
    msg: str = "hello", args: tuple[object, ...] = (), **extra: object
) -> logging.LogRecord:
    record = logging.makeLogRecord({"name": "test", "levelname": "INFO", "msg": msg, "args": args})
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_one_parseable_line() -> None:
    line = JsonFormatter().format(_record("task %s done", ("t1",)))
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["msg"] == "task t1 done"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["ts"].endswith("+00:00")


def test_bound_context_and_extras_appear_in_output() -> None:
    with bind_log_context(request_id="req-1", run_id="run-9"):
        payload = json.loads(JsonFormatter().format(_record(status=200)))
    assert payload["request_id"] == "req-1"
    assert payload["run_id"] == "run-9"
    assert payload["status"] == 200


def test_context_is_restored_after_block_even_on_error() -> None:
    with bind_log_context(worker_id="w1"):
        seen: dict[str, str] = {}

        def fail_inside_nested_context() -> None:
            with bind_log_context(task_id="t1"):
                seen.update(current_log_context())
                raise RuntimeError

        with pytest.raises(RuntimeError):
            fail_inside_nested_context()
        assert seen == {"worker_id": "w1", "task_id": "t1"}
        assert current_log_context() == {"worker_id": "w1"}
    assert current_log_context() == {}


def test_exceptions_are_serialised() -> None:
    record = _record()
    try:
        raise ValueError("boom")
    except ValueError:
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]
