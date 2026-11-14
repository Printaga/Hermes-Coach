# tests/test_manifest.py
import json

import yaml

from coach.settings import DEFAULTS


def test_the_desktop_manifest_version_matches_plugin_yaml():
    """The host reads plugin.yaml, the page reads the manifest: keep them equal."""
    manifest = yaml.safe_load(open("plugin.yaml"))
    desk = json.load(open("dashboard/manifest.json", encoding="utf-8"))
    assert desk["version"] == manifest["version"]


def test_manifest_is_a_standalone_plugin():
    manifest = yaml.safe_load(open("plugin.yaml"))
    assert manifest["name"] == "coach"
    assert manifest["kind"] == "standalone"
    assert "coach_ask" in manifest["provides_tools"]


def test_no_config_schema():
    # A config_schema makes the host add an "Agent settings" tab that edits the
    # same values as the Coach page. Settings live on the page only.
    manifest = yaml.safe_load(open("plugin.yaml"))
    assert not manifest.get("config_schema")


def test_defaults_are_flat_snake_case():
    assert all("." not in key and key == key.lower() for key in DEFAULTS)


def _load_entry():
    """Load the repo-root entry point as a real package module.

    ``__init__.py`` uses ``from .coach.plugin import ...``, so a plain
    ``import __init__`` fails; loading it as ``pkg.entry`` with the repo
    root as the package path resolves the relative import.
    """
    import importlib.util
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    name = "coach_entry_pkg"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(root)]
        sys.modules[name] = package
    full = f"{name}.entry"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, root / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


class _BareCtx:
    """A host with no tool/command/hook surface at all."""

    llm = None

    def get_config(self, key, default):
        return default


def test_register_without_hook_surface_still_registers_unload():
    entry = _load_entry()
    unloaded = []

    class Ctx(_BareCtx):
        def on_unload(self, fn):
            unloaded.append(fn)

    entry.register(Ctx())  # must not raise
    assert len(unloaded) == 1


def test_register_without_any_surface_never_raises():
    entry = _load_entry()
    entry.register(_BareCtx())  # must not raise


def test_manifest_hooks_equal_the_registration_list():
    """A hook advertised but never registered is a silent dead mode."""
    entry = _load_entry()
    manifest = yaml.safe_load(open("plugin.yaml"))
    assert set(manifest["provides_hooks"]) == {name for name, _ in entry.HOOKS}
    from coach.plugin import CoachPlugin

    for _name, attr in entry.HOOKS:
        assert callable(getattr(CoachPlugin, attr, None)), attr
