"""Cross-platform headless scheduler dispatcher.

Picks the right OS backend at import time and re-exports the canonical
install / uninstall / list_installed / install_pipeline API. The CLI
imports this module instead of a specific backend so the same code path
works on macOS (launchd) and Windows (Task Scheduler).

Linux is intentionally not supported in this release. Use Mode 1
(`boundary run`) or Mode 2 (`boundary fielding-coach`) on Linux.
"""
from __future__ import annotations

import sys

if sys.platform == "darwin":
    from boundary.launchd import (  # noqa: F401
        install,
        install_pipeline,
        list_installed,
        uninstall,
    )
    from boundary.launchd import (
        plist_path_for as _plist_path_for,
    )

    BACKEND = "launchd"

    def task_path_for(name: str):  # alias for backend-agnostic callers
        return _plist_path_for(name)
elif sys.platform.startswith("win"):
    from boundary.win_scheduler import (  # noqa: F401
        install,
        install_pipeline,
        list_installed,
        task_path_for,
        uninstall,
    )

    BACKEND = "schtasks"
else:
    BACKEND = "unsupported"

    def _unsupported(*_a, **_kw):
        raise RuntimeError(
            "Headless scheduling is only supported on macOS (launchd) and "
            "Windows (Task Scheduler). Use `boundary run` or "
            "`boundary fielding-coach` directly on this platform."
        )

    install = _unsupported
    install_pipeline = _unsupported
    uninstall = _unsupported
    task_path_for = _unsupported

    def list_installed():
        return []
