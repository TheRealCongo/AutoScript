# AutoScript plugin development

Plugins are optional folders placed under `plugins/`, next to `AutoScript.exe`.
They are not bundled into the AutoScript executable and can be released
independently.

## Manifest

`plugins/example/plugin.json`:

```json
{
  "id": "example.extension",
  "name": "Example Extension",
  "version": "0.1.0",
  "api_version": 1,
  "entry_point": "plugin.py:Plugin",
  "description": "Short user-facing description."
}
```

The ID must be unique and use lowercase letters, digits, `.`, `_`, or `-`.
AutoScript reads the manifest before it imports plugin code. A plugin that needs
a different API version remains visible but cannot be enabled.

## Lifecycle

`plugins/example/plugin.py`:

```python
from plugin_api import PluginContext


class Plugin:
    def activate(self, context: PluginContext) -> None:
        context.data_dir.mkdir(parents=True, exist_ok=True)
        context.log("Example Extension enabled")

    def deactivate(self) -> None:
        pass
```

`activate()` runs after the user enables the plugin under **Settings → Plugins**.
`deactivate()` runs when it is disabled or AutoScript exits. Plugin code runs
locally with the same permissions as AutoScript, so publishers should document
what their plugin does and users should install only trusted releases.

A plugin with third-party Python dependencies may ship them in its own `lib/`
folder. AutoScript adds that folder to the plugin import path only when the
plugin is enabled.
