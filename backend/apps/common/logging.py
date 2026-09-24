from __future__ import annotations

import contextvars
import logging

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("tracelens_request_id", default="-")


def set_request_id(value: str):
    return _request_id.set(value)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True
