"""Server-wide wiring and the uniform response envelope."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cad_harness.adapters import (
    BridgeDrawingReader,
    ComDrawingReader,
    DxfDrawingReader,
    build_adapter,
)
from cad_harness.adapters.dotnet_bridge import DotNetBridgeAdapter
from cad_harness.application.live_session_proof import (
    LIVE_SESSION_PROOF_ENV,
    issue_live_session_proof,
    verify_live_session_proof,
)
from cad_harness.application.manual_gate import (
    ManualStepId,
    require_live_setup_confirmations,
)
from cad_harness.application.services.drawing_audit_service import DrawingAuditService
from cad_harness.application.services.drawing_read_service import DrawingReadService
from cad_harness.application.services.harness_service import HarnessService
from cad_harness.application.services.instrumented_measurement_service import (
    InstrumentedMeasurementService,
)
from cad_harness.application.services.lease_service import LeaseService
from cad_harness.application.services.measurement_service import MeasurementService
from cad_harness.application.services.metrics_service import MetricsService
from cad_harness.application.services.raster_trace_service import RasterTraceService
from cad_harness.application.services.retention_cleanup_service import (
    cleanup_filesystem_retention,
)
from cad_harness.application.services.takeoff_service import TakeoffService
from cad_harness.company_rules.loader import CompanyProfile
from cad_harness.company_rules.material_loader import YamlMaterialTableLoader
from cad_harness.compatibility import load_compatibility_matrix
from cad_harness.comprehension.raster_trace import LocalRasterTracer, RasterTraceLimits
from cad_harness.config import Settings, load_settings, resolve_config_relative_path
from cad_harness.domain.errors import (
    AdapterCapabilityMissingError,
    ErrorCode,
    HarnessError,
    IdempotencyKeyReusedError,
    StaleDocumentRevisionError,
    WriterLeaseConflictError,
)
from cad_harness.domain.models.drawing_model import DrawingModel, ReadScope
from cad_harness.domain.models.envelope import ToolResponse, ToolStatus
from cad_harness.domain.ports.autocad_adapter import (
    AdapterCapability,
    InspectRequest,
)
from cad_harness.domain.ports.drawing_source import (
    DrawingReadRequest,
    DrawingSourcePort,
    DrawingSourceRef,
)
from cad_harness.geometry.tolerance import ToleranceProfile
from cad_harness.metrics.collector import load_pilot_thresholds
from cad_harness.metrics.recorder import OperationMetricsRecorder
from cad_harness.observability.logging import configure_logging, get_logger
from cad_harness.persistence.engine import build_engine, build_session_factory, create_all
from cad_harness.persistence.sql_audit_sink import SqlAuditSink
from cad_harness.persistence.sql_drawing_audit_store import SqlDrawingAuditStore
from cad_harness.persistence.sql_job_store import SqlJobStore
from cad_harness.persistence.sql_lease_store import SqlLeaseStore
from cad_harness.persistence.sql_metrics_store import SqlMetricsStore
from cad_harness.persistence.sql_takeoff_report_store import SqlTakeoffReportStore
from cad_harness.security.local_only import configure_local_only_network_guard
from cad_harness.security.redaction import redact_payload
from cad_harness.security.retention import RetentionPolicy

#: Errors that mean "the world moved", not "you asked wrongly".
_CONFLICT_ERRORS = (
    StaleDocumentRevisionError,
    WriterLeaseConflictError,
    IdempotencyKeyReusedError,
)

#: Adapter or environment failures, as opposed to caller mistakes.
_FAILURE_CODES = frozenset(
    {
        ErrorCode.AUTOCAD_NOT_RUNNING,
        ErrorCode.AUTOCAD_BUSY,
        ErrorCode.COM_CALL_FAILED,
        ErrorCode.IPC_TIMEOUT,
        ErrorCode.TRANSACTION_ABORTED,
        ErrorCode.POST_COMMIT_VALIDATION_FAILED,
        ErrorCode.UNKNOWN_COMMIT_STATE,
        ErrorCode.INTERNAL_ERROR,
    }
)


@dataclass(slots=True)
class ServerContext:
    settings: Settings
    service: HarnessService
    drawing_read_service: DrawingReadService
    drawing_audit_service: DrawingAuditService
    takeoff_service: TakeoffService
    measurement_service: InstrumentedMeasurementService
    operation_metrics: OperationMetricsRecorder
    metrics_service: MetricsService
    company_profile: CompanyProfile
    tolerance_profile: ToleranceProfile
    raster_tracer: LocalRasterTracer
    raster_trace_service: RasterTraceService | None


def build_context(
    config_path: Path | None = None,
    *,
    manual_confirmations: tuple[ManualStepId, ...] | None = None,
) -> ServerContext:
    """Load settings, configure logging, wire the adapter and the service."""
    settings = load_settings(config_path)
    write_requested = os.environ.get("CAD_HARNESS_LIVE_WRITE_VERIFIED") == "1"
    configure_local_only_network_guard(enabled=settings.app.local_only)
    configure_logging(
        level=settings.observability.log_level,
        json_output=settings.observability.log_json,
    )

    def apply_retention() -> Any:
        retention = cleanup_filesystem_retention(
            preview_root=Path(settings.storage.preview_directory),
            preview_policy=RetentionPolicy(
                settings.storage.preview_retention_days,
                settings.storage.preview_max_total_bytes,
            ),
            checkpoint_root=Path(settings.storage.checkpoint_directory),
            checkpoint_policy=RetentionPolicy(
                settings.storage.checkpoint_retention_days,
                settings.storage.checkpoint_max_total_bytes,
            ),
            now=datetime.now(UTC),
        )
        failures = (*retention.preview.failures, *retention.checkpoint.failures)
        get_logger(__name__).info(
            "retention_cleanup_completed",
            preview_selected=len(retention.preview.selected),
            preview_deleted=len(retention.preview.deleted),
            checkpoint_selected=len(retention.checkpoint.selected),
            checkpoint_deleted=len(retention.checkpoint.deleted),
            failure_count=len(failures),
        )
        return retention

    # Validate both roots and enforce existing TTL/quota before any adapter starts.
    apply_retention()
    adapter = build_adapter(
        settings.adapter.type,
        preview_directory=Path(settings.storage.preview_directory),
        autocad_prog_id=settings.adapter.autocad_prog_id,
        pipe_name=settings.bridge.pipe_name_template,
        timeout_seconds=settings.bridge.ipc_timeout_seconds,
        max_request_bytes=settings.bridge.max_request_bytes,
        write_enabled=write_requested,
    )

    # COM needs an explicit attach. Failing here is better than failing on first write.
    if settings.adapter.type == "com":
        from cad_harness.adapters.autocad_com import ComAutoCADAdapter

        assert isinstance(adapter, ComAutoCADAdapter)
        adapter.connect(launch_if_missing=settings.adapter.launch_autocad_if_missing)

    if settings.adapter.type in {"com", "dotnet_bridge"} and write_requested:
        status = adapter.status()
        if not status.available or status.process_id is None or status.active_document_id is None:
            raise AdapterCapabilityMissingError(
                "Live write setup cannot prove the active AutoCAD process and document",
                required_action="Load the verified bridge or attach COM to the intended drawing",
            )
        snapshot = adapter.inspect_document(InspectRequest(document_id=status.active_document_id))
        if AdapterCapability.COMMIT not in adapter.capabilities:
            raise AdapterCapabilityMissingError(
                "The live adapter does not advertise a verified commit capability",
                required_action="Complete the adapter-specific atomic write acceptance gate",
            )
        secret = settings.approval_secret()
        if manual_confirmations is not None:
            confirmations = require_live_setup_confirmations(
                settings.adapter.type, manual_confirmations
            )
            session_proof = issue_live_session_proof(
                adapter_type=settings.adapter.type,
                process_id=status.process_id,
                document_id=snapshot.document_id,
                revision=snapshot.revision,
                setup_steps=confirmations,
                secret=secret,
            )
        else:
            session_proof = os.environ.get(LIVE_SESSION_PROOF_ENV, "")
        verify_live_session_proof(
            session_proof,
            secret,
            adapter_type=settings.adapter.type,
            process_id=status.process_id,
            document_id=snapshot.document_id,
            revision=snapshot.revision,
        )

    get_logger(__name__).info(
        "server_configured",
        adapter_type=settings.adapter.type,
        environment=settings.app.environment,
        profile=settings.standards.company_profile,
    )
    engine = build_engine(Path(settings.storage.sqlite_path))
    # Development remains zero-setup; pilot/production schema changes are applied by
    # Alembic so migration history and backups stay auditable.
    if settings.app.environment == "development":
        create_all(engine)
    session_factory = build_session_factory(engine)
    store = SqlJobStore(session_factory)
    audit = SqlAuditSink(session_factory)
    drawing_audit_store = SqlDrawingAuditStore(session_factory)
    metrics_store = SqlMetricsStore(session_factory, pilot_run_id=settings.pilot.run_id)
    operation_metrics = OperationMetricsRecorder(metrics_store)
    raster_tracer = LocalRasterTracer(
        Path(settings.storage.preview_directory) / "raster",
        limits=RasterTraceLimits(
            max_bytes=settings.raster.max_bytes,
            max_pixels=settings.raster.max_pixels,
            max_dimension_px=settings.raster.max_dimension_px,
        ),
        confidence_threshold=settings.raster.confidence_threshold,
    )
    raster_secret = settings.approval_secret()
    raster_trace_service = (
        RasterTraceService(
            raster_tracer,
            signing_secret=raster_secret,
            acceptance_ttl=timedelta(minutes=settings.raster.acceptance_ttl_minutes),
        )
        if raster_secret
        else None
    )
    compatibility_matrix = load_compatibility_matrix(
        resolve_config_relative_path(settings.compatibility.matrix_path, config_path)
    )
    metrics_service = MetricsService(
        thresholds=load_pilot_thresholds(
            resolve_config_relative_path(settings.pilot.thresholds_path, config_path)
        ),
        store=metrics_store,
        audit_events=audit,
    )
    lease_service = LeaseService(
        SqlLeaseStore(session_factory),
        ttl_seconds=settings.lease.ttl_seconds,
        heartbeat_interval_seconds=settings.lease.heartbeat_interval_seconds,
        minimum_remaining_seconds=settings.lease.minimum_remaining_seconds,
    )
    drawing_source: DrawingSourcePort
    if settings.read.semantic_adapter == "dotnet_bridge" and not isinstance(
        adapter, DotNetBridgeAdapter
    ):
        semantic_adapter = build_adapter(
            "dotnet_bridge",
            pipe_name=settings.bridge.pipe_name_template,
            timeout_seconds=settings.bridge.ipc_timeout_seconds,
            max_request_bytes=settings.bridge.max_request_bytes,
            write_enabled=False,
        )
        assert isinstance(semantic_adapter, DotNetBridgeAdapter)
        drawing_source = BridgeDrawingReader(semantic_adapter)
    elif isinstance(adapter, DotNetBridgeAdapter):
        drawing_source = BridgeDrawingReader(adapter)
    elif settings.adapter.type == "com":
        from cad_harness.adapters.autocad_com import ComAutoCADAdapter

        assert isinstance(adapter, ComAutoCADAdapter)
        drawing_source = ComDrawingReader(adapter)
    else:
        drawing_source = DxfDrawingReader()
    drawing_read_service = DrawingReadService(
        settings, drawing_source, operation_metrics=operation_metrics
    )

    def read_active_drawing_model(document_id: str) -> DrawingModel:
        result = drawing_read_service.read(
            DrawingReadRequest(
                source=DrawingSourceRef(
                    kind="active_document",
                    format="dwg",
                    ref=document_id,
                ),
                scope=ReadScope(kind="model_space"),
                max_entities=settings.read.max_entities,
                max_block_nesting_depth=settings.read.max_block_nesting_depth,
                include_geometry=True,
            )
        )
        if not isinstance(result, DrawingModel):  # explicit scope must never summarize
            raise AdapterCapabilityMissingError(
                "Structured remediation readback returned no drawing geometry",
                required_action="Use a drawing reader that supports model-space geometry",
                details={"missing_capability": "semantic_geometry_read"},
            )
        if result.document_id != document_id:
            raise StaleDocumentRevisionError(
                "Structured remediation readback returned a different active document",
                required_action="Activate the audited drawing and retry remediation",
                details={"requested_document_id": document_id},
            )
        return result

    drawing_audit_service = DrawingAuditService(store=drawing_audit_store, audit=audit)
    takeoff_service = TakeoffService(
        settings,
        YamlMaterialTableLoader(),
        persistence=SqlTakeoffReportStore(session_factory),
        operation_metrics=operation_metrics,
    )
    service = HarnessService(
        settings,
        adapter,
        store=store,
        audit=audit,
        lease_service=lease_service,
        drawing_model_reader=read_active_drawing_model,
        drawing_audit_store=drawing_audit_store,
        operation_metrics=operation_metrics,
        compatibility_matrix=compatibility_matrix,
        retention_cleanup=apply_retention,
        raster_trace_service=raster_trace_service,
    )
    return ServerContext(
        settings=settings,
        service=service,
        drawing_read_service=drawing_read_service,
        drawing_audit_service=drawing_audit_service,
        takeoff_service=takeoff_service,
        measurement_service=InstrumentedMeasurementService(
            MeasurementService(), operation_metrics, settings
        ),
        operation_metrics=operation_metrics,
        metrics_service=metrics_service,
        company_profile=service.profile,
        tolerance_profile=service.tolerance,
        raster_tracer=raster_tracer,
        raster_trace_service=raster_trace_service,
    )


def ok(
    data: dict[str, Any],
    *,
    job_id: str | None = None,
    request_id: str | None = None,
    warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    return ToolResponse.ok(
        data, job_id=job_id, request_id=request_id, warnings=warnings
    ).model_dump(mode="json", exclude_none=True)


def failure(
    error: Exception,
    *,
    job_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Map an exception onto the response envelope.

    Unexpected exceptions become a generic ``INTERNAL_ERROR``: no stack traces or
    absolute paths cross the MCP boundary.
    """
    if isinstance(error, HarnessError):
        if error.code is ErrorCode.MISSING_REQUIRED_INPUTS:
            status = ToolStatus.NEEDS_INPUT
        elif isinstance(error, _CONFLICT_ERRORS):
            status = ToolStatus.CONFLICT
        elif error.code in _FAILURE_CODES:
            status = ToolStatus.FAILED
        else:
            status = ToolStatus.REJECTED
        get_logger(__name__).warning(
            "tool_error", error_code=error.code.value, job_id=job_id, outcome=status.value
        )
        response = ToolResponse.from_error(
            error,
            status=status,
            job_id=job_id,
            request_id=request_id,
        )
        if response.error is not None:
            response = response.model_copy(
                update={
                    "error": response.error.model_copy(
                        update={"details": redact_payload(response.error.details)}
                    )
                }
            )
        return response.model_dump(mode="json", exclude_none=True)

    get_logger(__name__).error(
        "tool_unhandled_error", error_type=type(error).__name__, job_id=job_id
    )
    return ToolResponse(
        status=ToolStatus.FAILED,
        job_id=job_id,
        request_id=request_id,
        error={  # type: ignore[arg-type]
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": "An internal error occurred",
            "retryable": False,
            "required_action": "Check the server log for the correlated request id",
            "details": {"error_type": type(error).__name__},
        },
    ).model_dump(mode="json", exclude_none=True)
