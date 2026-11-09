# Runtime dependencies

The plugin has no package install. `coach/` and `__init__.py` import the Python
standard library only; the single third-party import is `fastapi`, used by
`dashboard/plugin_api.py`, which the host supplies at runtime. `scripts/test.sh`
additionally pins pytest, pyyaml, fastapi and httpx through `uv run --with`, but
those are the **test toolchain**, not the runtime set.

Because nothing in the tree declares the runtime set, a dependency scanner has
no target here: trivy reports zero language-specific files and osv-scanner has
nothing to resolve. That is an unassessed surface, not a clean verdict. To close
it:

1. Export the host environment with the interpreter the host runs:
   `python -m pip freeze > requirements.host.lock`
   (or `uv pip freeze`, if the host is uv-managed).
2. Scan the export:
   `osv-scanner --lockfile requirements.host.lock`
   or `trivy fs --scanners vuln requirements.host.lock`.
3. Re-export after a host upgrade and re-scan.

Do not add a project manifest just to give the scanner a target: the plugin
never installs its dependencies, and an invented inventory would hide which
versions actually run.
