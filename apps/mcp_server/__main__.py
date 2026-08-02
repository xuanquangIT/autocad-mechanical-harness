"""Entry point: ``uv run cad-harness-mcp`` or ``python -m apps.mcp_server``."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    # STDIO carries the protocol, so force UTF-8 on Windows before anything writes.
    if sys.platform == "win32" and os.environ.get("PYTHONIOENCODING") is None:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")

    from apps.mcp_server.server import run_stdio

    config = os.environ.get("CAD_HARNESS_CONFIG")
    run_stdio(Path(config) if config else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
