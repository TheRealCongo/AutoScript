"""Discovery, enablement, and safe lifecycle handling for external plugins."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from plugin_api import PLUGIN_API_VERSION, PluginContext

PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


@dataclass
class PluginRecord:
    plugin_id: str
    name: str
    version: str
    description: str
    folder: Path
    entry_point: str
    enabled: bool = False
    status: str = "Disabled"
    detail: str = ""
    instance: object | None = None


class PluginManager:
    """Loads only manifest metadata until a user explicitly enables a plugin."""

    def __init__(
        self,
        plugins_dir: Path,
        state_file: Path,
        app_version: str,
        transcripts_dir: Path | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.plugins_dir = plugins_dir
        self.state_file = state_file
        self.app_version = app_version
        self.transcripts_dir = transcripts_dir or (state_file.parent / "transcripts")
        self.log = log or (lambda _message: None)
        self.plugins: dict[str, PluginRecord] = {}
        self._enabled_ids = self._load_state()

    def _load_state(self) -> set[str]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {str(plugin_id) for plugin_id in data.get("enabled", [])}
        except (OSError, ValueError, TypeError):
            return set()

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"enabled": sorted(self._enabled_ids)}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.log(f"Could not save plugin settings: {exc}")

    def discover(self) -> list[PluginRecord]:
        """Read manifests without running third-party code."""
        previous_plugins = self.plugins
        discovered: dict[str, PluginRecord] = {}
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        for folder in sorted(self.plugins_dir.iterdir(), key=lambda path: path.name.casefold()):
            manifest_path = folder / "plugin.json"
            if not folder.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                record = self._record_from_manifest(folder, manifest)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                plugin_id = f"invalid.{folder.name.casefold()}"
                discovered[plugin_id] = PluginRecord(
                    plugin_id=plugin_id,
                    name=folder.name,
                    version="—",
                    description="",
                    folder=folder,
                    entry_point="",
                    status="Invalid manifest",
                    detail=str(exc),
                )
                continue
            if record.plugin_id in discovered:
                record.status = "Duplicate ID"
                record.detail = f"Another plugin already uses '{record.plugin_id}'."
            previous = previous_plugins.get(record.plugin_id)
            if previous and previous.enabled and record.status == "Ready":
                # Keep an already-running instance alive while the settings
                # list refreshes. Updated plugin code takes effect next start.
                record.enabled = True
                record.status = previous.status
                record.detail = previous.detail
                record.instance = previous.instance
            discovered[record.plugin_id] = record
        for plugin_id, record in previous_plugins.items():
            if plugin_id not in discovered:
                self._deactivate(record)
        self.plugins = discovered
        return list(self.plugins.values())

    def _record_from_manifest(self, folder: Path, manifest: dict) -> PluginRecord:
        plugin_id = str(manifest["id"])
        if not PLUGIN_ID_RE.fullmatch(plugin_id):
            raise ValueError("'id' must use lowercase letters, digits, dots, dashes, or underscores.")
        entry_point = str(manifest["entry_point"])
        if ":" not in entry_point:
            raise ValueError("'entry_point' must look like 'plugin.py:Plugin'.")
        record = PluginRecord(
            plugin_id=plugin_id,
            name=str(manifest["name"]),
            version=str(manifest["version"]),
            description=str(manifest.get("description", "")),
            folder=folder,
            entry_point=entry_point,
        )
        requested_api = manifest.get("api_version")
        if requested_api != PLUGIN_API_VERSION:
            record.status = "Incompatible"
            record.detail = f"Needs plugin API {requested_api}; AutoScript provides {PLUGIN_API_VERSION}."
        elif plugin_id in self._enabled_ids:
            record.enabled = True
            record.status = "Ready"
        return record

    def activate_enabled(self) -> None:
        for record in self.plugins.values():
            if record.enabled and record.status == "Ready":
                self._activate(record)

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginRecord:
        record = self.plugins[plugin_id]
        if enabled:
            if record.status in {"Incompatible", "Invalid manifest", "Duplicate ID"}:
                return record
            record.enabled = True
            self._enabled_ids.add(plugin_id)
            self._activate(record)
        else:
            self._deactivate(record)
            record.enabled = False
            record.status = "Disabled"
            record.detail = ""
            self._enabled_ids.discard(plugin_id)
        self._save_state()
        return record

    def _activate(self, record: PluginRecord) -> None:
        try:
            module_file, class_name = record.entry_point.split(":", 1)
            source = (record.folder / module_file).resolve()
            if record.folder.resolve() not in source.parents or not source.is_file():
                raise ValueError("Entry point must name a Python file inside this plugin folder.")
            # A downloadable plugin may bundle its Python dependencies in a
            # private ``lib`` directory, keeping optional dependencies out of
            # AutoScript's base executable and requirements file.
            # Some optional Windows-only plugin dependencies (notably
            # pywin32) ship extension modules in a sibling runtime folder.
            # Make that private runtime available without affecting AutoScript's
            # base dependency set.
            for import_root in (
                record.folder / "lib" / "pywin32_system32",
                record.folder / "lib" / "pythonwin",
                record.folder / "lib" / "win32" / "lib",
                record.folder / "lib" / "win32",
                record.folder / "lib",
                record.folder,
            ):
                if import_root.is_dir() and str(import_root) not in sys.path:
                    sys.path.insert(0, str(import_root))
            module_name = f"as_plugin_{record.plugin_id.replace('-', '_').replace('.', '_')}"
            spec = importlib.util.spec_from_file_location(module_name, source)
            if not spec or not spec.loader:
                raise ImportError("Could not load the entry point.")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            instance = getattr(module, class_name)()
            plugin_dir = self.state_file.parent / "plugins" / record.plugin_id
            context = PluginContext(
                self.app_version, plugin_dir, self.transcripts_dir, self.log
            )
            activate = getattr(instance, "activate", None)
            if callable(activate):
                activate(context)
            record.instance = instance
            record.status = "Enabled"
            record.detail = ""
        except Exception as exc:
            record.instance = None
            record.status = "Error"
            record.detail = str(exc)
            self.log(f"Plugin '{record.name}' could not start: {exc}")

    def open_settings(self, plugin_id: str, parent) -> PluginRecord:
        """Ask an enabled plugin to show its own configuration UI, if any."""
        record = self.plugins[plugin_id]
        if record.instance is None:
            record.status = "Configure after enabling"
            record.detail = "Enable this plugin before opening its settings."
            return record
        try:
            open_settings = getattr(record.instance, "open_settings", None)
            if not callable(open_settings):
                record.status = "Enabled"
                record.detail = "This plugin has no additional settings."
            else:
                open_settings(parent)
        except Exception as exc:
            record.status = "Error"
            record.detail = str(exc)
            self.log(f"Plugin '{record.name}' settings could not open: {exc}")
        return record

    def _deactivate(self, record: PluginRecord) -> None:
        if record.instance is not None:
            try:
                deactivate = getattr(record.instance, "deactivate", None)
                if callable(deactivate):
                    deactivate()
            except Exception as exc:
                self.log(f"Plugin '{record.name}' could not stop cleanly: {exc}")
        record.instance = None

    def shutdown(self) -> None:
        for record in self.plugins.values():
            self._deactivate(record)
