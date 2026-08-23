"""
Structured JSON Logging Configuration

Provides a JSON formatter and helpers for consistent structured logging
across all modules. Every log entry is a single-line JSON object with
standard fields: timestamp, level, module, function, line, tag, endpoint,
user_id, request_id, message, extra.

Usage in modules:
    from log_config import get_logger, log_info, log_error, log_warning

    logger = get_logger(__name__)
    log_info(logger, 'INCOME', 'Entry created', entry_id=123, amount='50.00')
    log_error(logger, 'REDIS', 'Connection failed', error=str(e))
"""

import json
import logging
import sys
import uuid
from datetime import datetime, date
from decimal import Decimal


class _JsonEncoder(json.JSONEncoder):
    """Handles Decimal, date, datetime serialization in log extra fields."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


class JsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.
    Automatically injects Flask request context (request_id, user_id, endpoint)
    when available.
    """

    def format(self, record):
        # Build base log entry
        entry = {
            'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.') +
                         f'{datetime.utcnow().microsecond // 1000:03d}Z',
            'level': record.levelname,
            'logger': record.name,
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'tag': getattr(record, 'tag', None),
            'endpoint': None,
            'user_id': None,
            'request_id': None,
            'message': record.getMessage(),
        }

        # Inject Flask request context if available
        try:
            from flask import g, request as flask_request
            if flask_request:
                entry['endpoint'] = f"{flask_request.method} {flask_request.path}"
                entry['user_id'] = getattr(g, 'log_user_id', None)
                entry['request_id'] = getattr(g, 'request_id', None)
        except (RuntimeError, ImportError):
            # Outside Flask request context or Flask not available
            pass

        # Attach extra fields if provided
        extra = getattr(record, 'extra_data', None)
        if extra:
            entry['extra'] = extra

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            entry['exception'] = self.formatException(record.exc_info)

        return json.dumps(entry, cls=_JsonEncoder, ensure_ascii=False)


def get_logger(name):
    """
    Get a logger configured with JSON formatting.
    Call once per module: logger = get_logger(__name__)
    """
    lgr = logging.getLogger(name)
    # Only add handler if this logger has none (prevents duplicate handlers)
    if not lgr.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler.setLevel(logging.INFO)
        lgr.addHandler(handler)
        lgr.setLevel(logging.INFO)
    return lgr


def _log(logger, level, tag, message, **extra):
    """Internal helper to emit a structured log entry."""
    record_extra = {'tag': tag}
    if extra:
        record_extra['extra_data'] = extra
    logger.log(level, message, extra=record_extra)


def log_info(logger, tag, message, **extra):
    """Log an INFO-level structured entry."""
    _log(logger, logging.INFO, tag, message, **extra)


def log_warning(logger, tag, message, **extra):
    """Log a WARNING-level structured entry."""
    _log(logger, logging.WARNING, tag, message, **extra)


def log_error(logger, tag, message, **extra):
    """Log an ERROR-level structured entry."""
    _log(logger, logging.ERROR, tag, message, **extra)


def log_exception(logger, tag, message, **extra):
    """Log an ERROR-level entry with exception traceback."""
    record_extra = {'tag': tag}
    if extra:
        record_extra['extra_data'] = extra
    logger.error(message, exc_info=True, extra=record_extra)


def generate_request_id():
    """Generate a short unique request ID (8 hex chars)."""
    return uuid.uuid4().hex[:8]
