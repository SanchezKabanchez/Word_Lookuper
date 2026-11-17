import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import word_lookup as w
from word_lookup_config import DEFAULT_CONFIG, load_settings


def test_bom_config_is_loaded_without_quarantine(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 8, "popup": {"opacity": 1}, "languages": {"target": "pl"}}), encoding="utf-8-sig")
    loaded = load_settings(str(path))
    assert loaded.values["popup"]["opacity"] == 1.0
    assert type(loaded.values["popup"]["opacity"]) is float
    assert loaded.values["languages"]["target"] == "pl"
    assert path.exists() and not list(tmp_path.glob("*.corrupt*"))


@pytest.mark.parametrize("raw", [b"broken configuration", b"[]", b"\xff\xfe invalid"])
def test_corrupt_configuration_is_preserved_before_defaults_and_new_save(raw):
    path = Path(w.config_path())
    path.write_bytes(raw)
    config = w.load_config()
    assert config == w.DEFAULT_CONFIG
    assert not path.exists()
    assert path.with_name(path.name + ".corrupt").read_bytes() == raw
    assert w.save_config(config)
    assert json.loads(path.read_text())["version"] == w.DEFAULT_CONFIG["version"]


def test_quarantine_does_not_replace_existing_corrupt_copy(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("second invalid file")
    previous = tmp_path / "config.json.corrupt"
    previous.write_text("first invalid file")
    assert load_settings(str(path)).writable
    assert previous.read_text() == "first invalid file"
    copies = list(tmp_path.glob("config.json.corrupt.*"))
    assert len(copies) == 1 and copies[0].read_text() == "second invalid file"


def test_failed_quarantine_blocks_save_of_defaults(monkeypatch):
    path = Path(w.config_path())
    raw = b"unreadable invalid config"
    path.write_bytes(raw)
    original = Path.rename
    def denied(source, target):
        if source == path:
            raise PermissionError("synthetic permission failure")
        return original(source, target)
    monkeypatch.setattr(Path, "rename", denied)
    config = w.load_config()
    assert not w.save_config(config)
    assert path.read_bytes() == raw


def test_newer_schema_is_never_overwritten():
    path = Path(w.config_path())
    path.write_text('{"version":999,"future_settings":{"data":true}}')
    original = path.read_bytes()
    config = w.load_config()
    assert not w.save_config(config)
    assert path.read_bytes() == original


def test_types_ranges_and_enumerations_are_explicit(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 8, "popup": {"font_size": True, "opacity": 1,
        "width": 99999, "theme": "invalid", "highlight_color": "not-a-colour"},
        "ocr": {"cpu_threads": False, "region_w": -200}, "auto": {"double_click_s": .25}}))
    config = load_settings(str(path)).values
    assert config["popup"]["font_size"] == DEFAULT_CONFIG["popup"]["font_size"]
    assert config["popup"]["opacity"] == 1.0
    assert config["popup"]["width"] == 900
    assert config["popup"]["theme"] == "dark"
    assert config["popup"]["highlight_color"] == "#ffd43b"
    assert config["ocr"]["cpu_threads"] == 2
    assert config["ocr"]["region_w"] == 200
    assert config["auto"]["double_click_s"] == .25


def test_import_does_not_read_configuration_or_keychain(tmp_path):
    folder = tmp_path / "isolated-profile"
    folder.mkdir()
    config = folder / "config.json"
    config.write_text('{"deepl":{"key":"synthetic-legacy-key"}}')
    program = '''import builtins, os
import word_lookup_secrets as secrets
def forbidden():
    raise AssertionError("Import accessed the credential backend")
secrets._backend = forbidden
original = builtins.open
def guarded(path, *args, **kwargs):
    if os.fspath(path) == os.path.join(os.environ["WORD_LOOKUP_DATA_DIR"], "config.json"):
        raise AssertionError("Import read the configuration")
    return original(path, *args, **kwargs)
builtins.open = guarded
import word_lookup
assert word_lookup.cfg["deepl"]["key"] == ""
print("import-safe")
'''
    child = subprocess.run([sys.executable, "-c", program], env=dict(os.environ, WORD_LOOKUP_DATA_DIR=str(folder)),
                           text=True, capture_output=True, timeout=20)
    assert child.returncode == 0, child.stderr
    assert "import-safe" in child.stdout
    assert not (folder / "app.log").exists()
