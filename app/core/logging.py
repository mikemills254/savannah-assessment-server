"""Structured logging.

Every line goes to stdout, as one JSON object per line in production (the
container's output is what Coolify and log collectors read) or as readable
text locally. Each line logged while handling a request carries its
request_id, and its user_id once the caller is known, so all of one
request's lines can be found together. The id is returned to the client in
X-Request-ID, so a user's bug report can be traced to its logs.

Never log passwords, tokens or patient details: log ids, not names or emails.
"""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.access")

# The current request's fields. A dict rather than one variable per field:
# sync dependencies run in the threadpool on a copy of the context, where
# setting a variable wouldn't reach the middleware, but changing the shared
# dict does.
_context: ContextVar[dict[str, str] | None] = ContextVar("log_context", default=None)


def bind(**fields: str) -> None:
    """Add fields to every line logged for the rest of the current request."""
    context = _context.get()
    if context is not None:
        context.update(fields)


# Every record gets the request's fields as `record.context`. One attribute,
# so they can't clash with what a call passes in `extra`.
_base_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    record = _base_factory(*args, **kwargs)
    context = _context.get()
    record.context = dict(context) if context else {}
    return record


logging.setLogRecordFactory(_record_factory)

# Attributes every record has; anything else came in through `extra`.
# color_message is uvicorn's colored copy of the message.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "context",
    "color_message",
}


def _fields(record: logging.LogRecord) -> dict[str, object]:
    extra = {k: v for k, v in vars(record).items() if k not in _STANDARD}
    return {**getattr(record, "context", {}), **extra}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        when = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        line = f"{when} {record.levelname:<7} {record.name}: {record.getMessage()}"
        fields = " ".join(f"{k}={v}" for k, v in _fields(record).items())
        if fields:
            line = f"{line}  {fields}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging() -> None:
    """Send every logger's output, uvicorn's included, through one stdout
    handler. Safe to call again: it replaces its own handler and leaves
    others (pytest's, for one) alone."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, "is_app_handler", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.is_app_handler = True
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else ConsoleFormatter()
    )
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # uvicorn sets up its own handlers before importing the app.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # The middleware below writes the access log, with more in it.
    logging.getLogger("uvicorn.access").disabled = True
    # At INFO, SQLAlchemy logs every statement and watchfiles every reload.
    for name in ("sqlalchemy", "watchfiles"):
        logging.getLogger(name).setLevel(logging.WARNING)


_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestContextMiddleware:
    """Gives each request an id, logs one access line when it finishes, and
    turns an unhandled exception into a logged 500 that carries the id.

    The id is the caller's X-Request-ID if it looks like one (so an id from
    the web client or a proxy carries through), otherwise a new one."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = _context.set({"request_id": request_id})
        status = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            logger.exception("Unhandled error")
            if response_started:
                raise
            response = JSONResponse(
                {"detail": "Internal server error", "request_id": request_id},
                status_code=500,
            )
            await response(scope, receive, send_with_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            client = scope.get("client")
            # The path only: query strings can hold search terms such as a
            # patient's name.
            access_logger.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "%s %s %s %.1fms",
                scope["method"],
                scope["path"],
                status,
                duration_ms,
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": duration_ms,
                    "client_ip": client[0] if client else None,
                },
            )
            _context.reset(token)
