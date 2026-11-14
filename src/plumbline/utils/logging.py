"""Application logging, configured at the CLI and API entry points."""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload)


def configure_logging() -> None:
    """Configure one application handler without disturbing host handlers."""
    root = logging.getLogger()
    if any(getattr(handler, "_plumbline_handler", False) for handler in root.handlers):
        return
    level = os.getenv("PLUMBLINE_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level, logging.INFO) if level in
                  {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler._plumbline_handler = True
    if os.getenv("PLUMBLINE_LOG_JSON", "false").lower() in {"1", "true", "yes"}:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with_context(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    """Log a message with additional context.

    Args:
        logger: Logger instance
        level: Log level (e.g., logging.INFO)
        message: Log message
        **context: Additional context as keyword arguments
    """
    log_data = {"message": message, "timestamp": datetime.now(timezone.utc).isoformat(), **context}
    logger.log(level, log_data)


def log_event(logger: logging.Logger, event_type: str, data: Dict[str, Any]) -> None:
    """Log an event with structured data.

    Args:
        logger: Logger instance
        event_type: Type of event
        data: Event data
    """
    event = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    logger.info(event)


def log_error(logger: logging.Logger, error: Exception, context: Dict[str, Any]) -> None:
    """Log an error with context.

    Args:
        logger: Logger instance
        error: Exception that occurred
        context: Error context
    """
    error_data = {
        "error_type": type(error).__name__,
        "error_message": str(error),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "context": context,
    }
    logger.error(error_data)
