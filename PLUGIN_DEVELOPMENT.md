# AutoScript plugin development

AutoScript plugins are optional folders placed under `plugins/`, next to `AutoScript.exe`.
They are not bundled into AutoScript and can be released independently.

## Manifest

`plugins/example/plugin.json`:

```json
{
  "id": "example.capture-source",
  "name": "Example Capture Source",
  "version": "0.1.0",
  "api_version": 1,
  "entry_point": "plugin.py:Plugin",
  "description": "Short user-facing description."
}
```

The ID must be unique and use lowercase letters, digits, `.`, `_`, or `-`.
AutoScript reads this file before it imports any plugin code. A plugin that needs a
different API version remains visible but cannot be enabled.

## Lifecycle

`plugins/example/plugin.py`:

```python
from plugin_api import PluginContext


class Plugin:
    def activate(self, context: PluginContext) -> None:
        context.data_dir.mkdir(parents=True, exist_ok=True)
        context.log("Example Capture Source enabled")

    def deactivate(self) -> None:
        pass
```

`activate()` runs only after the user checks the plugin in **Settings →
Plugins**. `deactivate()` runs when it is unchecked or AutoScript exits. Plugin code
runs locally with the same permissions as AutoScript; publishers should document
what their plugin does and users should install only trusted releases.

If a plugin has third-party Python dependencies, ship them in its own `lib/`
subfolder. AutoScript adds that folder to the plugin's import path only when the
plugin is enabled; the base AutoScript executable stays dependency-free.

The initial API intentionally establishes discovery, compatibility checks,
persistent enablement, and lifecycle handling. The next API addition will be
a labeled audio-source contract used by the Discord plugin, so plugin authors
should avoid relying on AutoScript internals.
