"""Fallback persistence and precedence; native DPAPI runs in Windows CI."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import word_lookup as w
import word_lookup_secrets as secrets


class Vault:
    def __init__(self, value="obsolete-vault-key", failure=None):
        self.value = value
        self.failure = failure
        self.calls = []

    def get_password(self, *_args):
        self.calls.append("read")
        if self.failure == "read":
            raise OSError("synthetic vault read failure")
        return self.value

    def set_password(self, _service, _account, key):
        self.calls.append("write")
        if self.failure == "write":
            raise OSError("synthetic vault write failure: " + key)
        if self.failure != "silent_write":
            self.value = key

    def delete_password(self, *_args):
        self.calls.append("delete")
        if self.failure == "delete":
            raise OSError("synthetic vault delete failure")
        if self.failure != "silent_delete":
            self.value = None


@pytest.fixture
def dpapi(monkeypatch):
    # Model the OS's opaque encryption service, independently of module/session
    # state. These tokens test persistence flow, not cryptographic strength.
    sealed, calls = {}, []

    def crypt(data, *, protect):
        calls.append(protect)
        if protect:
            token = b"synthetic-dpapi:" + os.urandom(48)
            sealed[token] = bytes(data)
            return token
        if data not in sealed:
            raise OSError("synthetic DPAPI integrity or user mismatch")
        return sealed[data]

    monkeypatch.setattr(secrets, "_dpapi_available", lambda: True)
    monkeypatch.setattr(secrets, "_crypt", crypt)
    return calls


def broken_backend():
    raise OSError("synthetic unavailable Credential Manager")


@pytest.mark.parametrize("failure", ["factory", "read", "write", "silent_write"])
def test_vault_failure_saves_key_and_restores_after_new_config_load(monkeypatch, dpapi, capsys, failure):
    vault = Vault(failure=failure)
    monkeypatch.setattr(secrets, "_backend", broken_backend if failure == "factory" else lambda: vault)
    config = w.load_config()
    key = "synthetic-persistent-deepl-key:fx"
    config["deepl"]["key"] = key
    assert w.save_config(config)
    assert "DPAPI" in w._credential_status
    assert key.encode() not in secrets._key_path().read_bytes()
    assert json.loads(Path(w.config_path()).read_text())["deepl"]["key"] == ""

    # Forget session metadata. A fresh settings load must recover from disk.
    w._credential_state.clear()
    secrets._message = ""
    assert w.load_config()["deepl"]["key"] == key
    assert "DPAPI" in w._credential_status
    calls = list(dpapi), list(vault.calls)
    for enabled in (True, False, True):
        config["freeze"]["hover_highlight"] = enabled
        assert w.save_config(config)
    assert (dpapi, vault.calls) == calls
    assert key not in capsys.readouterr().out
    if Path(w.log_path()).exists():
        assert key not in Path(w.log_path()).read_text(encoding="utf-8")


def test_local_update_stays_authoritative_when_vault_recovers(monkeypatch, dpapi):
    vault = Vault(failure="write")
    monkeypatch.setattr(secrets, "_backend", lambda: vault)
    assert secrets.store_key("first-local-key")
    vault.failure = None
    vault.calls.clear()
    assert secrets.store_key("replacement-local-key")
    assert secrets.load_key() == "replacement-local-key"
    assert vault.value == "obsolete-vault-key"
    assert vault.calls == []


@pytest.mark.parametrize("failure", ["read", "delete", "silent_delete"])
def test_clear_masks_stale_vault_key_across_restart(monkeypatch, dpapi, failure):
    vault = Vault(failure="write")
    monkeypatch.setattr(secrets, "_backend", lambda: vault)
    assert secrets.store_key("new-local-key")
    vault.failure = failure
    assert secrets.store_key("")
    assert "очищено" in secrets.storage_message()
    vault.failure = None
    vault.calls.clear()
    assert secrets.load_key() == ""
    assert vault.calls == []
    assert vault.value == "obsolete-vault-key"


def test_successful_clear_removes_vault_and_local_key(monkeypatch, dpapi):
    vault = Vault(failure="write")
    monkeypatch.setattr(secrets, "_backend", lambda: vault)
    assert secrets.store_key("new-local-key")
    vault.failure = None
    assert secrets.store_key("")
    assert "видалено" in secrets.storage_message()
    assert not secrets._key_path().exists()
    assert vault.value is None
    assert secrets.load_key() == ""


@pytest.mark.parametrize("corruption", [b"plain damaged file", secrets._HEADER + b"invalid ciphertext"])
def test_corrupt_local_file_never_restores_obsolete_vault_key(monkeypatch, dpapi, corruption):
    vault = Vault()
    monkeypatch.setattr(secrets, "_backend", lambda: vault)
    secrets._key_path().write_bytes(corruption)
    config = w.load_config()
    assert config["deepl"]["key"] == ""
    assert "Не вдалося прочитати" in w._credential_status
    assert w.save_config(config)
    assert secrets._key_path().read_bytes() == corruption
    assert vault.calls == []
    config["deepl"]["key"] = "corrected-local-key"
    assert w.save_config(config)
    assert secrets.load_key() == "corrected-local-key"


def test_failed_dpapi_encryption_keeps_previous_ciphertext_and_reports_failure(monkeypatch, dpapi, capsys):
    monkeypatch.setattr(secrets, "_backend", broken_backend)
    assert secrets.store_key("previous-local-key")
    previous = secrets._key_path().read_bytes()
    config = w.load_config()
    key = "failed-local-save-key:fx"
    config["deepl"]["key"] = key
    attempts = []

    def denied(*_args, **_kwargs):
        attempts.append(True)
        raise OSError("DPAPI echoed " + key)

    monkeypatch.setattr(secrets, "_crypt", denied)
    assert w.save_config(config)
    assert "лише в цьому сеансі" in w._credential_status
    assert secrets._key_path().read_bytes() == previous
    assert attempts == [True]
    assert key not in capsys.readouterr().out
    assert key not in Path(w.config_path()).read_text()
    assert not list(secrets._key_path().parent.glob(".wordlookup-key-*"))


def test_failed_atomic_replace_leaves_previous_key_readable(monkeypatch, dpapi):
    monkeypatch.setattr(secrets, "_backend", broken_backend)
    assert secrets.store_key("previous-local-key")

    def denied(*_args):
        raise OSError("synthetic sharing violation")

    monkeypatch.setattr(secrets.os, "replace", denied)
    assert not secrets.store_key("unsaved-local-key")
    assert not secrets.storage_message()
    assert secrets.load_key() == "previous-local-key"
    assert not list(secrets._key_path().parent.glob(".wordlookup-key-*"))


def test_failed_file_readback_cannot_report_saved(monkeypatch, dpapi):
    monkeypatch.setattr(secrets, "_backend", broken_backend)
    monkeypatch.setattr(secrets, "_read_local_key", lambda: "unexpected-value")
    assert not secrets.store_key("requested-value")
    assert not secrets.storage_message()


def test_non_windows_has_no_file_fallback(monkeypatch):
    monkeypatch.setattr(secrets, "_backend", lambda: None)
    assert not secrets.store_key("session-only-key")
    assert not secrets._key_path().exists()


def test_ui_restores_key_and_shows_verified_storage_status(monkeypatch, dpapi, request):
    monkeypatch.setattr(secrets, "_backend", broken_backend)
    assert secrets.store_key("ui-restored-local-key:fx")
    monkeypatch.setattr(w, "cfg", w.load_config())
    app = request.getfixturevalue("app")
    assert app.deepl_entry.get() == "ui-restored-local-key:fx"
    assert "DPAPI" in app.deepl_var.get()
    app._setting_vars[("deepl", "key")].set("ui-edited-local-key:fx")
    app._flush_config_save()
    assert "DPAPI" in app.deepl_var.get()
    assert secrets.load_key() == "ui-edited-local-key:fx"
    calls = list(dpapi)
    app._setting_vars[("freeze", "hover_highlight")].set(False)
    app._flush_config_save()
    assert dpapi == calls


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Native Windows DPAPI integration")
def test_native_dpapi_restores_in_separate_python_process(monkeypatch):
    monkeypatch.setattr(secrets, "_dpapi_available", lambda: True)
    monkeypatch.setattr(secrets, "_backend", broken_backend)
    key = "synthetic-native-dpapi-key:fx"
    assert secrets.store_key(key)
    assert key.encode() not in secrets._key_path().read_bytes()
    child = subprocess.run([
        sys.executable, "-c",
        "import sys; from pathlib import Path; import word_lookup_secrets as s; "
        "s._key_path = lambda: Path(sys.argv[1]); print(s.load_key())",
        str(secrets._key_path()),
    ], check=True, capture_output=True, text=True, timeout=20)
    assert child.stdout.strip() == key
    # Native tamper detection and an encrypted clear are independent of keyring.
    ciphertext = secrets._key_path().read_bytes()
    secrets._key_path().write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
    with pytest.raises(OSError):
        secrets.load_key()
    assert secrets.store_key("")
    assert secrets.load_key() == ""
