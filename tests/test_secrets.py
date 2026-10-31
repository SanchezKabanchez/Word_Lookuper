import copy
import json

import pytest
import word_lookup as w
import word_lookup_secrets as secrets


class Vault:
    def __init__(self):
        self.value = None
        self.writes = 0

    def get_password(self, service, account):
        assert (service, account) == (secrets.SERVICE, secrets.ACCOUNT)
        return self.value

    def set_password(self, service, account, key):
        self.value = key
        self.writes += 1

    def delete_password(self, service, account):
        self.value = None


def test_legacy_key_migrates_to_vault_without_json_plaintext(monkeypatch):
    vault = Vault()
    monkeypatch.setattr(secrets, '_backend', lambda: vault)
    legacy = copy.deepcopy(w.DEFAULT_CONFIG)
    legacy['version'] = 4
    legacy['deepl']['key'] = 'unit-test-key:fx'
    legacy['freeze']['pause_seconds'] = 120
    w.atomic_json_write(w.config_path(), legacy)
    loaded = w.load_config()
    assert loaded['deepl']['key'] == 'unit-test-key:fx'
    assert loaded['freeze']['pause_seconds'] == 20
    assert w.save_config(loaded)
    assert vault.value == 'unit-test-key:fx'
    assert 'unit-test-key' not in open(w.config_path()).read()
    assert w.load_config()['deepl']['key'] == vault.value
    w.save_config(loaded)
    assert vault.writes == 1
    loaded['deepl']['key'] = ''
    w.save_config(loaded)
    assert vault.value is None
    assert w.load_config()['deepl']['key'] == ''


def test_unavailable_vault_never_falls_back_to_plaintext(monkeypatch, capsys):
    def broken():
        raise RuntimeError('never-log-this-secret')
    monkeypatch.setattr(secrets, '_backend', broken)
    config = copy.deepcopy(w.DEFAULT_CONFIG)
    config['deepl']['key'] = 'never-log-this-secret'
    assert w.save_config(config)
    assert json.load(open(w.config_path()))['deepl']['key'] == ''
    assert config['deepl']['key'] == 'never-log-this-secret'
    assert 'never-log-this-secret' not in capsys.readouterr().out


def test_vault_overrides_legacy_json(monkeypatch):
    vault = Vault()
    vault.value = 'vault-value'
    monkeypatch.setattr(secrets, '_backend', lambda: vault)
    w.atomic_json_write(w.config_path(), {'deepl': {'key': 'old-json-value'}})
    assert w.load_config()['deepl']['key'] == 'vault-value'


def test_actual_configured_key_is_redacted_from_exception_logs(monkeypatch, capsys):
    w.cfg['deepl']['key'] = 'unique-runtime-deepl-value:fx'
    try:
        raise RuntimeError('Provider echoed unique-runtime-deepl-value:fx in failure')
    except RuntimeError:
        w.log_exc('provider')
    assert 'unique-runtime-deepl-value' not in capsys.readouterr().out
    assert 'unique-runtime-deepl-value' not in open(w.log_path()).read()


@pytest.mark.skipif(not __import__('sys').platform.startswith('win'), reason='Windows Credential Manager integration')
def test_native_windows_credential_roundtrip(monkeypatch):
    import uuid
    # An isolated service/account exercises the real OS vault, never a user key.
    monkeypatch.setattr(secrets, 'SERVICE', 'WordLookup.test.' + uuid.uuid4().hex)
    monkeypatch.setattr(secrets, 'ACCOUNT', 'test-key')
    try:
        assert secrets.store_key('synthetic-test-key:fx')
        assert secrets.load_key() == 'synthetic-test-key:fx'
        assert secrets.store_key('')
        assert secrets.load_key() == ''
    finally:
        secrets.store_key('')
