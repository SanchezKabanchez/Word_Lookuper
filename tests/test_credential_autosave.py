"""Unrelated settings saves must never touch the OS credential backend."""
import copy
import json
from pathlib import Path

import pytest
import word_lookup as w
import word_lookup_secrets as secrets


class RecordingVault:
    def __init__(self, value=None):
        self.value = value
        self.calls = []
        self.fail_read = self.fail_write = self.fail_delete = False

    def get_password(self, service, account):
        self.calls.append("read")
        if self.fail_read:
            raise RuntimeError("backend echoed credential: " + str(self.value))
        return self.value

    def set_password(self, service, account, key):
        self.calls.append("write")
        if self.fail_write:
            raise RuntimeError("backend echoed credential: " + key)
        self.value = key

    def delete_password(self, service, account):
        self.calls.append("delete")
        if self.fail_delete:
            raise RuntimeError("backend echoed credential: " + str(self.value))
        self.value = None


def install_vault(monkeypatch, vault):
    def backend():
        vault.calls.append("backend")
        return vault
    monkeypatch.setattr(secrets, "_backend", backend)


def saved_json():
    return json.loads(Path(w.config_path()).read_text(encoding="utf-8"))


def test_ordinary_checkbox_saves_do_not_read_or_write_loaded_vault(monkeypatch):
    vault = RecordingVault("loaded-secure-test-key")
    install_vault(monkeypatch, vault)
    config = w.load_config()
    vault.calls.clear()
    for enabled in (False, True, False, True):
        config["ocr"]["highlight"] = enabled
        assert w.save_config(config)
    assert vault.calls == []
    assert config["deepl"]["key"] == vault.value
    assert saved_json()["deepl"]["key"] == ""


def test_fresh_empty_key_save_does_not_even_open_vault(monkeypatch):
    def forbidden():
        raise AssertionError("Ordinary empty-key save opened the credential backend")
    monkeypatch.setattr(secrets, "_backend", forbidden)
    assert w.save_config(copy.deepcopy(w.DEFAULT_CONFIG))
    assert saved_json()["deepl"]["key"] == ""


def test_changed_key_is_written_once_then_unrelated_saves_skip_backend(monkeypatch):
    vault = RecordingVault("previous-secure-test-key")
    install_vault(monkeypatch, vault)
    config = w.load_config()
    config["deepl"]["key"] = "replacement-secure-test-key"
    vault.calls.clear()
    assert w.save_config(config)
    assert vault.calls.count("write") == 1
    calls_after_change = list(vault.calls)
    config["popup"]["font_size"] = 12
    assert w.save_config(config)
    assert vault.calls == calls_after_change
    assert vault.value == config["deepl"]["key"]
    assert "replacement-secure-test-key" not in Path(w.config_path()).read_text(encoding="utf-8")


def test_failed_key_save_is_not_retried_until_explicit_retry(monkeypatch, capsys):
    sensitive = "failed-save-test-key:fx"
    vault = RecordingVault()
    vault.fail_write = True
    install_vault(monkeypatch, vault)
    config = w.load_config()
    config["deepl"]["key"] = sensitive
    vault.calls.clear()
    assert w.save_config(config)
    assert vault.calls.count("write") == 1
    failed_calls, failed_status = list(vault.calls), w._credential_status
    assert failed_status
    for enabled in (True, False, True):
        config["ocr"]["highlight"] = enabled
        assert w.save_config(config)
        assert w._credential_status == failed_status
    assert vault.calls == failed_calls
    assert config["deepl"]["key"] == sensitive
    assert saved_json()["deepl"]["key"] == ""
    vault.fail_write = False
    assert w.save_config(config, retry_key=True)
    assert vault.calls.count("write") == 2
    assert vault.value == sensitive
    retried_calls = list(vault.calls)
    assert w.save_config(config)
    assert vault.calls == retried_calls
    assert sensitive not in capsys.readouterr().out
    if Path(w.log_path()).exists():
        assert sensitive not in Path(w.log_path()).read_text(encoding="utf-8")


def test_failed_key_delete_keeps_feedback_and_does_not_retry_on_checkbox_save(monkeypatch, capsys):
    sensitive = "previous-key-delete-test:fx"
    vault = RecordingVault(sensitive)
    install_vault(monkeypatch, vault)
    config = w.load_config()
    config["deepl"]["key"] = ""
    vault.fail_delete = True
    vault.calls.clear()
    assert w.save_config(config)
    assert vault.calls.count("delete") == 1
    failed_calls, failed_status = list(vault.calls), w._credential_status
    assert "видал" in failed_status.lower()
    assert vault.value == sensitive
    config["ocr"]["highlight"] = False
    assert w.save_config(config)
    assert vault.calls == failed_calls
    assert w._credential_status == failed_status
    vault.fail_delete = False
    assert w.save_config(config, retry_key=True)
    assert vault.value is None
    assert vault.calls.count("delete") == 2
    successful_calls = list(vault.calls)
    assert w.save_config(config)
    assert vault.calls == successful_calls
    assert w.load_config()["deepl"]["key"] == ""
    assert sensitive not in capsys.readouterr().out


def test_legacy_migration_failure_is_attempted_once_and_never_written_to_json(monkeypatch):
    sensitive = "legacy-migration-test-key:fx"
    vault = RecordingVault()
    vault.fail_write = True
    install_vault(monkeypatch, vault)
    w.atomic_json_write(w.config_path(), {"version": 4, "deepl": {"key": sensitive}})
    config = w.load_config()
    assert config["deepl"]["key"] == sensitive
    vault.calls.clear()
    assert w.save_config(config)
    assert vault.calls.count("write") == 1
    failed_calls = list(vault.calls)
    assert w.save_config(config)
    assert vault.calls == failed_calls
    assert sensitive not in Path(w.config_path()).read_text(encoding="utf-8")
    assert config["deepl"]["key"] == sensitive
    vault.fail_write = False
    assert w.save_config(config, retry_key=True)
    assert vault.value == sensitive


def test_secure_vault_override_only_cleans_up_stale_legacy_json(monkeypatch):
    vault = RecordingVault("authoritative-vault-test-key")
    install_vault(monkeypatch, vault)
    w.atomic_json_write(w.config_path(), {"deepl": {"key": "obsolete-legacy-json-key"}})
    config = w.load_config()
    assert config["deepl"]["key"] == vault.value
    vault.calls.clear()
    assert w.save_config(config)
    assert vault.calls == []
    assert saved_json()["deepl"]["key"] == ""
    assert "obsolete-legacy-json-key" not in Path(w.config_path()).read_text(encoding="utf-8")


def test_json_failure_does_not_repeat_an_already_successful_vault_write(monkeypatch):
    vault = RecordingVault()
    install_vault(monkeypatch, vault)
    config = w.load_config()
    config["deepl"]["key"] = "successful-vault-before-disk-failure"
    atomic_write = w.atomic_json_write
    def disk_failure(*args, **kwargs):
        raise OSError("Settings file is temporarily locked")
    monkeypatch.setattr(w, "atomic_json_write", disk_failure)
    assert not w.save_config(config)
    assert vault.value == config["deepl"]["key"]
    assert vault.calls.count("write") == 1
    successful_calls = list(vault.calls)
    monkeypatch.setattr(w, "atomic_json_write", atomic_write)
    assert w.save_config(config)
    assert vault.calls == successful_calls
    assert saved_json()["deepl"]["key"] == ""


def test_new_config_path_with_empty_key_cannot_delete_other_paths_vault_key(monkeypatch, tmp_path):
    vault = RecordingVault("existing-vault-test-key")
    install_vault(monkeypatch, vault)
    w.load_config()
    monkeypatch.setattr(w, "config_path", lambda: str(tmp_path / "other" / "config.json"))
    vault.calls.clear()
    assert w.save_config(copy.deepcopy(w.DEFAULT_CONFIG))
    assert vault.calls == []
    assert vault.value == "existing-vault-test-key"


def test_failed_initial_vault_read_does_not_trigger_automatic_delete(monkeypatch, capsys):
    sensitive = "unreadable-vault-test-key"
    vault = RecordingVault(sensitive)
    vault.fail_read = True
    install_vault(monkeypatch, vault)
    config = w.load_config()
    assert config["deepl"]["key"] == ""
    vault.calls.clear()
    config["ocr"]["highlight"] = False
    assert w.save_config(config)
    assert vault.calls == []
    assert vault.value == sensitive
    assert sensitive not in capsys.readouterr().out


@pytest.mark.parametrize("read_failure", [False, True])
def test_real_checkbox_autosave_has_no_credential_side_effect(app, monkeypatch, read_failure):
    vault = RecordingVault("ui-checkbox-vault-test-key")
    vault.fail_read = read_failure
    install_vault(monkeypatch, vault)
    monkeypatch.setattr(w, "cfg", w.load_config())
    vault.calls.clear()
    for enabled in (False, True, False, True):
        app._setting_vars[("freeze", "hover_highlight")].set(enabled)
        app._flush_config_save()
        assert vault.calls == []
        assert saved_json()["freeze"]["hover_highlight"] is enabled
    assert saved_json()["deepl"]["key"] == ""


def test_failed_delete_feedback_remains_visible_with_empty_key(app, monkeypatch):
    vault = RecordingVault("ui-delete-vault-test-key")
    install_vault(monkeypatch, vault)
    monkeypatch.setattr(w, "cfg", w.load_config())
    vault.fail_delete = True
    app._setting_vars[("deepl", "key")].set("")
    app._flush_config_save()
    assert vault.calls.count("delete") == 1
    assert "видал" in app.deepl_var.get().lower()
    assert app.deepl_var.get() == w._credential_status


def test_explicit_retry_button_cancels_pending_autosave_and_attempts_once(app, monkeypatch):
    vault = RecordingVault()
    install_vault(monkeypatch, vault)
    monkeypatch.setattr(w, "cfg", w.load_config())
    vault.fail_write = True
    app._setting_vars[("deepl", "key")].set("ui-retry-button-test-key")
    app._flush_config_save()
    assert vault.calls.count("write") == 1
    vault.fail_write = False
    app._setting_vars[("freeze", "hover_highlight")].set(False)
    assert app._config_save_timer is not None
    app.retry_deepl_key_storage()
    assert app._config_save_timer is None
    assert vault.calls.count("write") == 2
    assert vault.value == "ui-retry-button-test-key"
    successful_calls = list(vault.calls)
    app._flush_config_save()
    assert vault.calls == successful_calls
    assert saved_json()["freeze"]["hover_highlight"] is False
