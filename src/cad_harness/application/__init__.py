"""Application layer: orchestration, preconditions, state transitions.

The public service names are loaded lazily. Infrastructure modules such as the
spawn-safe pure worker can therefore import a narrow application submodule without
eagerly constructing the complete service graph in every child process.
"""

from typing import Any

__all__ = ["HarnessService", "PlanCompilerService"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from cad_harness.application.services import HarnessService, PlanCompilerService

        return {
            "HarnessService": HarnessService,
            "PlanCompilerService": PlanCompilerService,
        }[name]
    raise AttributeError(name)
