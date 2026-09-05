"""Public API for optional AutoScript plugins.

Plugins are kept outside the AutoScript executable.  A plugin directory contains a
``plugin.json`` manifest and the Python module named by its ``entry_point``.
AutoScript deliberately keeps this contract small: plugins have a lifecycle today;
future capture plugins will use the audio types below without needing access to
AutoScript's private GUI or recorder implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

PLUGIN_API_VERSION = 1


@dataclass(frozen=True)
class AudioFrame:
    """One future per-speaker PCM audio frame supplied by a capture plugin."""

    speaker_id: str
    speaker_name: str
    pcm: bytes
    sample_rate: int
    channels: int
    timestamp: float


@dataclass(frozen=True)
class PluginContext:
    """Safe, intentionally limited services available to a loaded plugin."""

    app_version: str
    data_dir: Path
    transcripts_dir: Path
    log: Callable[[str], None]


class ASPlugin(Protocol):
    """The optional lifecycle implemented by a plugin entry-point class."""

    def activate(self, context: PluginContext) -> None:
        """Start the plugin after the user enables it."""

    def deactivate(self) -> None:
        """Stop the plugin before AutoScript disables or exits."""

    def open_settings(self, parent) -> None:
        """Optionally open this plugin's configuration UI inside AutoScript."""
