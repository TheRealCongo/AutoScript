# AutoScript Plugins

Place each plugin in its own folder here, beside `AutoScript.exe` when using the
standalone app. A plugin folder needs a `plugin.json` manifest and the Python
entry point named in that manifest.

AutoScript reads manifests without running plugin code. Code is imported only
after you enable that plugin under **Settings → Plugins**.

Plugins run locally with the same permissions as AutoScript, so install plugins
only from sources you trust. See [PLUGIN_DEVELOPMENT.md](../PLUGIN_DEVELOPMENT.md)
for the manifest format and lifecycle contract.
