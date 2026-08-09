"""Structured logging setup.

Two constraints shape this module:

1. On STDIO transport, stdout belongs to the MCP protocol. All logs go to stderr.
2. Full prompts, raw paths and entire geometry are never logged (section 21.1).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from cad_harness.security.redaction import redact_payload

#: Fields every log line should carry when available (architecture section 21.1).
STANDARD_FIELDS = (
    "request_id",
    "job_id",
    "document_id_pseudonym",
    "plan_hash_prefix",
    "adapter_type",
    "duration_ms",
    "outcome",
    "error_code",
)


def _redact_event(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> dict[str, Any]:
    """Apply the same bounded redaction policy used by audit and metrics."""
    redacted = redact_payload(event_dict)
    assert isinstance(redacted, dict)
    return redacted


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog to write to stderr."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )

    # ezdxf logs every table it creates at INFO, which drowns the harness's own events.
    logging.getLogger("ezdxf").setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_event,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "cad_harness") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_job(
    *,
    job_id: str | None = None,
    request_id: str | None = None,
    document_id: str | None = None,
    plan_hash: str | None = None,
    adapter_type: str | None = None,
) -> None:
    """Bind correlation fields for the current task.

    ``document_id`` is pseudonymized and only a hash prefix of the plan is kept, so a
    log file cannot be used to reconstruct a customer's drawing identity.
    """
    from cad_harness.domain.canonical import hash_prefix, sha256_of

    context: dict[str, Any] = {}
    if job_id:
        context["job_id"] = job_id
    if request_id:
        context["request_id"] = request_id
    if document_id:
        context["document_id_pseudonym"] = hash_prefix(sha256_of(document_id))
    if plan_hash:
        context["plan_hash_prefix"] = hash_prefix(plan_hash)
    if adapter_type:
        context["adapter_type"] = adapter_type

    structlog.contextvars.bind_contextvars(**context)
