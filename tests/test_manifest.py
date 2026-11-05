# tests/test_manifest.py
import yaml
from coach.settings import DEFAULTS

def test_manifest_mirrors_defaults():
    manifest = yaml.safe_load(open("plugin.yaml"))
    schema = manifest["config_schema"]
    assert manifest["name"] == "coach"
    for key, default in DEFAULTS.items():
        assert key in schema, key
        assert schema[key].get("default") == default, key

def test_every_schema_key_has_a_description():
    schema = yaml.safe_load(open("plugin.yaml"))["config_schema"]
    assert all(len(str(spec.get("description", "")).strip()) > 20 for spec in schema.values())
