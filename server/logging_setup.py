"""Logging of the app: one setup, one correlation id per request, no noise.

- INFO: one summary line per request that changes something (POST/PUT/PATCH/DELETE), is slow or fails.
  A successful read is DEBUG. Nothing else per call.
- WARNING: something degraded that is worth reading. A normal fallback or retry is DEBUG.
- ERROR: always with the stack trace.
- No document text, prompt or token in a log.
- ``LOG_LEVEL`` (env, default INFO) sets the level of the app; chatty libraries stay at WARNING.
"""

import contextvars
import logging
import os
import time
from logging import Formatter

request_id: contextvars.ContextVar[str] = contextvars.ContextVar('request_id', default='-')

SLOW_REQUEST_S = 1.0

QUIET_PATHS = ('/api/health', '/assets/', '/images/', '/logos/', '/videos/', '/content/', '/favicon')

_NOISY_LOGGERS = ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'httpx', 'httpcore', 'urllib3', 'databricks', 'asyncio', 'PIL')


class _RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` (from the ``request_id`` context variable) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Sets the request id on the record and keeps it.

        :param record: log record being emitted
        :return: always True
        """
        record.request_id = request_id.get()
        return True


def setup_logging() -> None:
    """Configures the root logger once: UTC timestamps, request id, level from ``LOG_LEVEL``.

    :return: None
    """
    level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(Formatter(
        '%(asctime)s UTC - %(name)s - %(levelname)s - [%(request_id)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    Formatter.converter = time.gmtime
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def is_quiet_path(path: str) -> bool:
    """Tells whether a successful request on this path is too frequent to deserve a log line.

    :param path: URL path of the request
    :return: True for health probes and static assets
    """
    return path.startswith(QUIET_PATHS)
