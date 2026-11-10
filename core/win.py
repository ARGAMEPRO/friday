"""Backwards-compatible shim.

The Windows-only integration moved to :mod:`core.system`, which speaks
Windows, macOS and Linux. Keep importing ``core.win`` if you have older code;
new code should use ``core.system``.
"""
from __future__ import annotations

from .system import (  # noqa: F401  (re-exported API)
    CREATE_NO_WINDOW,
    IS_LINUX,
    IS_MAC,
    IS_WINDOWS,
    LABEL,
    PLATFORM,
    close_app,
    foreground_window,
    launch_app,
    media,
    notify,
    open_path,
    open_url,
    recent_vscode,
    system_status,
)

__all__ = [
    "CREATE_NO_WINDOW",
    "IS_LINUX",
    "IS_MAC",
    "IS_WINDOWS",
    "LABEL",
    "PLATFORM",
    "close_app",
    "foreground_window",
    "launch_app",
    "media",
    "notify",
    "open_path",
    "open_url",
    "recent_vscode",
    "system_status",
]
