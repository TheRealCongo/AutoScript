"""Public API for optional AutoScript plugins.

Plugins are kept outside the AutoScript executable.  A plugin directory contains a
``plugin.json`` manifest and the Python module named by its ``entry_point``.
AutoScript keeps this contract small: plugins receive a lifecycle and a limited
context without access to the app's private GUI or recorder implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

PLUGIN_API_VERSION = 1


@dataclass(frozen=True)
class PluginContext:
    """Safe, intentionally limited services available to a loaded plugin."""

    app_version: str
    data_dir: Path
    transcripts_dir: Path
    log: Callable[[str], None]
    copy_token: Callable[[str], None] | None = None


class ASPlugin(Protocol):
    """The optional lifecycle implemented by a plugin entry-point class."""

    def activate(self, context: PluginContext) -> None:
        """Start the plugin after the user enables it."""

    def deactivate(self) -> None:
        """Stop the plugin before AutoScript disables or exits."""

    def open_settings(self, parent) -> None:
        """Optionally open this plugin's configuration UI inside AutoScript."""
