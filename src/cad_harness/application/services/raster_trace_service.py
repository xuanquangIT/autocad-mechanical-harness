"""Application boundary for calibrated raster tracing and engineer acceptance.

Raster pixels are untrusted input.  This service keeps them local to the tracer and
issues a short-lived signature over the exact reviewed result before any draft
operations can leave the raster workflow.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from string import hexdigits
from typing import Any

from cad_harness.comprehension.raster_trace import (
    LocalRasterTracer,
    accept_trace,
    accepted_operations,
)
from cad_harness.domain.canonical import canonical_json
from cad_harness.domain.errors import (
    ApprovalExpiredError,
    ApprovalRequiredError,
    ApprovalScopeMismatchError,
    InvalidFeatureParametersError,
    MissingRequiredInputsError,
    UnsupportedInputFormatError,
)
from cad_harness.domain.models.operation_plan import Operation
from cad_harness.domain.models.raster import (
    RasterCalibration,
    RasterTraceAcceptance,
    RasterTraceReport,
)

_TOKEN_VERSION = "raster-v1"
_TOKEN_FIELDS = frozenset(
    {
        "trace_id",
        "trace_digest",
        "source_sha256",
        "accepted_candidate_ids",
        "accepted_by",
        "layer",
        "expires_at",
    }
)
_BASE64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_DEFAULT_ACCEPTANCE_TTL = timedelta(minutes=15)
_MAX_ACCEPTANCE_TTL = timedelta(minutes=15)


class RasterTraceService:
    """Trace images locally and gate draft operations on signed engineer review."""

    def __init__(
        self,
        tracer: LocalRasterTracer,
        *,
        signing_secret: str,
        acceptance_ttl: timedelta = _DEFAULT_ACCEPTANCE_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not signing_secret.strip():
            raise ApprovalRequiredError(
                "No raster acceptance signing secret is configured",
                required_action="Configure a non-empty local raster acceptance secret",
            )
        if acceptance_ttl <= timedelta(0) or acceptance_ttl > _MAX_ACCEPTANCE_TTL:
            raise ApprovalRequiredError(
                "Raster acceptance lifetime must be between zero and fifteen minutes",
                required_action=(
                    "Configure a positive raster acceptance lifetime of at most 15 minutes"
                ),
            )
        self._tracer = tracer
        self._secret = signing_secret.encode("utf-8")
        self._acceptance_ttl = acceptance_ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    def trace(
        self,
        payload: bytes,
        display_name: str,
        calibration: RasterCalibration | None,
    ) -> RasterTraceReport:
        """Trace bytes without retaining or exposing their contents."""
        if Path(display_name).name != display_name or not display_name.strip():
            raise InvalidFeatureParametersError(
                "Raster display name must be a non-empty basename",
                required_action="Submit a display name without directory components",
            )
        try:
            return self._tracer.trace(
                payload,
                display_name=display_name,
                calibration=calibration,
            )
        except (TypeError, ValueError) as exc:
            raise UnsupportedInputFormatError(
                "Raster payload could not be decoded within the configured safety limits",
                required_action="Submit a valid bounded PNG, JPEG, or TIFF image",
            ) from exc

    def accept(
        self,
        report: RasterTraceReport,
        accepted_candidate_ids: tuple[str, ...],
        accepted_by: str,
        *,
        layer: str,
    ) -> tuple[RasterTraceAcceptance, str]:
        """Record and sign the engineer's exact candidate selection."""
        if report.calibration is None:
            raise MissingRequiredInputsError(
                "Raster calibration is required before engineer acceptance",
                required_action="Provide two calibration pixels and their real distance",
                details={"missing": ["calibration"]},
            )
        if not accepted_by.strip():
            raise ApprovalRequiredError(
                "An identified engineer must accept raster candidates",
                required_action="Supply the accepting engineer identity",
            )
        if not accepted_candidate_ids:
            raise ApprovalRequiredError(
                "At least one raster candidate must be explicitly accepted",
                required_action="Review the overlay and select one or more proposed candidates",
            )
        _require_layer(layer)
        try:
            acceptance = accept_trace(
                report,
                accepted_candidate_ids=tuple(accepted_candidate_ids),
                accepted_by=accepted_by,
            )
        except ValueError as exc:
            raise ApprovalScopeMismatchError(
                "Raster acceptance is outside the reviewed trace scope",
                required_action="Review the current trace and accept only its proposed candidates",
            ) from exc

        expires_at = self._now() + self._acceptance_ttl
        claims = _claims(acceptance, expires_at, layer)
        payload = _encode_claims(claims)
        digest = hmac.new(self._secret, payload.encode("ascii"), sha256).hexdigest()
        return acceptance, f"{_TOKEN_VERSION}.{payload}.{digest}"

    def draft_operations(
        self,
        report: RasterTraceReport,
        acceptance: RasterTraceAcceptance,
        token: str,
        *,
        layer: str,
    ) -> tuple[Operation, ...]:
        """Return draft-only operations after signature, scope and expiry verification."""
        claims = self._verified_claims(token)
        expires_at = _parse_expiry(claims["expires_at"])
        if self._now() >= expires_at:
            raise ApprovalExpiredError(
                "Raster acceptance has expired",
                required_action="Review the current raster trace and approve it again",
                details={"expires_at": expires_at.isoformat()},
            )
        expected_claims = _claims(acceptance, expires_at, layer)
        if not hmac.compare_digest(
            canonical_json(claims).encode("utf-8"),
            canonical_json(expected_claims).encode("utf-8"),
        ):
            raise ApprovalScopeMismatchError(
                "Raster acceptance token does not match the supplied acceptance",
                required_action="Use the acceptance and token issued for this exact trace",
            )
        if (
            acceptance.trace_id != report.trace_id
            or acceptance.trace_digest != report.trace_digest
            or acceptance.source_sha256 != report.source.source_sha256
        ):
            raise ApprovalScopeMismatchError(
                "Raster acceptance does not cover the supplied trace",
                required_action="Review and approve the current raster trace",
            )
        _require_layer(layer)
        try:
            return accepted_operations(report, acceptance, layer=layer)
        except ValueError as exc:
            raise ApprovalScopeMismatchError(
                "Raster trace changed after engineer acceptance",
                required_action="Regenerate the trace overlay and request a fresh acceptance",
            ) from exc

    def _verified_claims(self, token: str) -> dict[str, Any]:
        try:
            version, payload, received_digest = token.split(".", 2)
            if (
                version != _TOKEN_VERSION
                or len(received_digest) != 64
                or any(character not in hexdigits for character in received_digest)
            ):
                raise ValueError("invalid token shape")
            expected_digest = hmac.new(
                self._secret,
                payload.encode("ascii"),
                sha256,
            ).hexdigest()
        except (AttributeError, UnicodeEncodeError, ValueError):
            payload = ""
            received_digest = ""
            expected_digest = ""
        if not hmac.compare_digest(received_digest, expected_digest):
            raise ApprovalScopeMismatchError(
                "Raster acceptance token signature is invalid",
                required_action="Request a fresh acceptance for the current raster trace",
            )
        try:
            return _decode_claims(payload)
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ApprovalScopeMismatchError(
                "Raster acceptance token claims are invalid",
                required_action="Request a fresh acceptance for the current raster trace",
            ) from exc

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApprovalRequiredError(
                "Raster acceptance clock must return a timezone-aware timestamp",
                required_action="Configure the service clock with an explicit timezone",
            )
        return value.astimezone(UTC)


def _claims(acceptance: RasterTraceAcceptance, expires_at: datetime, layer: str) -> dict[str, Any]:
    return {
        "trace_id": acceptance.trace_id,
        "trace_digest": acceptance.trace_digest,
        "source_sha256": acceptance.source_sha256,
        "accepted_candidate_ids": list(acceptance.accepted_candidate_ids),
        "accepted_by": acceptance.accepted_by,
        "layer": layer,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
    }


def _require_layer(layer: str) -> None:
    if not layer.strip() or len(layer) > 256 or any(character in layer for character in "\r\n"):
        raise InvalidFeatureParametersError(
            "Raster draft layer must be a bounded single-line name",
            required_action="Supply a non-empty layer name of at most 256 characters",
        )


def _encode_claims(claims: dict[str, Any]) -> str:
    encoded = canonical_json(claims).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _decode_claims(payload: str) -> dict[str, Any]:
    if not payload or any(character not in _BASE64URL_CHARS for character in payload):
        raise ValueError("invalid claim encoding")
    padding = "=" * (-len(payload) % 4)
    value = json.loads(base64.b64decode(payload + padding, altchars=b"-_", validate=True))
    if not isinstance(value, dict) or set(value) != _TOKEN_FIELDS:
        raise ValueError("invalid claim fields")
    string_fields = (
        "trace_id",
        "trace_digest",
        "source_sha256",
        "accepted_by",
        "layer",
        "expires_at",
    )
    if any(not isinstance(value[field], str) or not value[field] for field in string_fields):
        raise ValueError("invalid string claim")
    candidate_ids = value["accepted_candidate_ids"]
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or not all(isinstance(item, str) and item for item in candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        raise ValueError("invalid candidate claims")
    _parse_expiry(value["expires_at"])
    return value


def _parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid expiry claim")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expiry claim must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = ["RasterTraceService"]
