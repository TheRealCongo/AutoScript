# CDCT Plugins

Drop each plugin into its own folder here, beside `CDCT.exe` when using the
standalone app. A plugin folder must contain a `plugin.json` manifest and the
Python entry-point named in that manifest.

CDCT reads manifests without running plugin code. Code is imported only after
you explicitly enable that plugin in **Settings → Plugins**.

Plugins run locally with the same permissions as CDCT, so only install plugins
from sources you trust. The first extension point is the lifecycle API; the
Discord plugin will be the first capture-source implementation.

See [PLUGIN_DEVELOPMENT.md](../PLUGIN_DEVELOPMENT.md) for the manifest format
and API contract.
