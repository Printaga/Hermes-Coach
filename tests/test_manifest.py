# tests/test_manifest.py
import yaml

from coach.settings import DEFAULTS


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
