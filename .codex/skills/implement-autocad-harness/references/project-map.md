# Project map

Use this routing table after identifying the affected requirement.

| Concern | Authoritative or primary locations |
|---|---|
| Scope and acceptance | `.kiro/specs/cad-ai-production-roadmap/requirements.md` |
| Architecture and properties | `.kiro/specs/cad-ai-production-roadmap/design.md`, `docs/AUTOCAD_MECHANICAL_HARNESS_ARCHITECTURE.en.md` |
| Dependency order and progress | `.kiro/specs/cad-ai-production-roadmap/tasks.md` |
| Repository rules | `.kiro/steering/project-conventions.md` |
| Wire contracts | `src/cad_harness/domain/models/`, `contracts/`, `scripts/generate_schemas.py` |
| Write orchestration | `src/cad_harness/application/services/harness_service.py`, `plan_compiler.py` |
| Read orchestration | `src/cad_harness/application/services/drawing_read_service.py`, `takeoff_service.py`, `drawing_audit_service.py`, `measurement_service.py` |
| Pure geometry | `src/cad_harness/geometry/` |
| Feature semantics | `src/cad_harness/feature_catalog/`, `src/cad_harness/annotation/` |
| Read comprehension | `src/cad_harness/comprehension/` |
| Validation | `src/cad_harness/validation/` |
| AutoCAD integration | `src/cad_harness/adapters/`, `dotnet/AutoCADBridge/` |
| MCP surface | `apps/mcp_server/` |
| Approval UI | `apps/engineer_desktop/` |
| Persistence/audit | `src/cad_harness/persistence/`, `src/cad_harness/observability/` |
| Test evidence | `tests/unit`, `tests/property`, `tests/contract`, `tests/golden_drawings`, `tests/fault_injection`, `tests/integration`, `tests/performance` |
| Raster intake | `src/cad_harness/comprehension/raster_trace.py`, `application/services/raster_trace_service.py`, `apps/mcp_server/tools/raster_tools.py` |
| Operations/release | `docs/operations.md`, `dotnet/AutoCADBridge/Package-BridgeBundle.ps1`, `.github/workflows/` |

Authority order when documents disagree:

1. Explicit current user requirement.
2. Architecture document.
3. Roadmap requirements and design.
4. Task checklist.
5. README and implementation notes.

Record a deliberate scope or architecture change in an ADR before relying on it in code.
