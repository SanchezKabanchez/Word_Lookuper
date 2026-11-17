"""Explicit OS keychains, with a user-scoped Windows DPAPI fallback."""
import os
from pathlib import Path
import sys
import tempfile
from word_lookup_paths import data_dir

SERVICE = "WordLookup.DeepL"
ACCOUNT = "api-key"
_HEADER = b"WordLookup.DeepL.DPAPI.v1\0"
_PAYLOAD = b"WordLookup.DeepL\0"
_MAX_FILE = 65536
_message = ""
_VAULT_SAVED = "Ключ збережено у Windows Credential Manager."
_LOCAL_SAVED = "Ключ збережено локально й зашифровано Windows (DPAPI)."


def storage_message():
    """Safe UI feedback, containing no backend exception or credential value."""
    return _message


def _dpapi_available():
    return sys.platform.startswith("win")


def _key_path():
    return data_dir() / "deepl-key.dpapi"


def _crypt(data, *, protect):
    """Use the current Windows user's protection, without a prompt or new deps."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(Blob)
    crypt32.CryptProtectData.argtypes = [pointer, wintypes.LPCWSTR, pointer,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pointer]
    crypt32.CryptUnprotectData.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR), pointer,
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pointer]
    crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately no LOCAL_MACHINE flag.
        if protect:
            ok = crypt32.CryptProtectData(ctypes.byref(source), "Word Lookup DeepL API key",
                                          None, None, None, 1, ctypes.byref(result))
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None,
                                            None, 1, ctypes.byref(result))
        if not ok:
            raise OSError("Windows DPAPI operation failed")
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel32.LocalFree(result.data)


def _read_key_at(path):
    with path.open("rb") as stream:
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE or not data.startswith(_HEADER):
        raise ValueError("Invalid encrypted credential file")
    payload = _crypt(data[len(_HEADER):], protect=False)
    if not payload.startswith(_PAYLOAD):
        raise ValueError("Invalid encrypted credential payload")
    return payload[len(_PAYLOAD):].decode("utf-8")


def _write_key_at(path, key, verify):
    payload = _PAYLOAD + key.encode("utf-8")
    encrypted = _crypt(payload, protect=True)
    # Check both cryptographic directions before replacing a previous value.
    if _crypt(encrypted, protect=False) != payload:
        raise OSError("Encrypted credential verification failed")
    data = _HEADER + encrypted
    if len(data) > _MAX_FILE:
        raise ValueError("Encrypted credential is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".wordlookup-key-", delete=False) as stream:
            tmp = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        # Verify the actual on-disk value, not just the buffer passed to Windows.
        if verify() != key:
            raise OSError("Saved credential verification failed")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def _read_local_key():
    return _read_key_at(_key_path())


def _write_local_key(key):
    return _write_key_at(_key_path(), key, _read_local_key)


def _backend():
    # Never use keyring.get_keyring(): a user-installed plaintext alternate
    # backend must not become an automatic credential store.
    if sys.platform.startswith("win"):
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        return Keyring()
    if sys.platform.startswith("linux") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        from keyring.backends.SecretService import Keyring
        return Keyring()
    return None


class ProtectedKeyStore:
    def __init__(self, service, path, *, account="api-key", backend_factory=None, reader=None, writer=None):
        self.service, self.account, self.path = service, account, Path(path)
        self.backend = backend_factory or _backend
        self.local = _dpapi_available()
        self.read_local = reader or (lambda: _read_key_at(self.path))
        self.write_local = writer or (lambda key: _write_key_at(self.path, key, self.read_local))
        label = ("Windows Credential Manager" if sys.platform.startswith("win") else
                 "macOS Keychain" if sys.platform == "darwin" else "Secret Service")
        self.saved_notice = f"Ключ збережено у {label}."
        self.message = ""

    def load(self):
        self.message = ""
        if self.local:
            try:
                key = self.read_local()
            except FileNotFoundError:
                pass
            else:
                # A fallback update can shadow an older, inaccessible vault entry.
                # An empty encrypted value is an explicit clear, not a missing key.
                self.message = _LOCAL_SAVED if key else ""
                return key
            # A damaged/unreadable fallback must not resurrect a stale vault value.
        backend = self.backend()
        key = (backend.get_password(self.service, self.account) or "") if backend else ""
        self.message = self.saved_notice if key else ""
        return key


    def store(self, key):
        """Persist and read back a key; fall back to DPAPI if the vault fails.

        Once the local fallback exists, it remains authoritative across restarts.
        Unavailable OS keychains retain only the in-memory key, never plaintext.
        """
        self.message = ""
        local = self.local
        vault_cleared = False
        try:
            # Continue using an existing fallback instead of reviving an old vault
            # entry. Only one local write attempt is made per explicit save.
            backend = None if key and local and self.path.exists() else self.backend()
            if backend is not None:
                previous = backend.get_password(self.service, self.account)
                if key:
                    if previous != key:
                        backend.set_password(self.service, self.account, key)
                    if backend.get_password(self.service, self.account) != key:
                        raise OSError("Credential Manager verification failed")
                    self.message = self.saved_notice
                    return True
                if previous is not None:
                    backend.delete_password(self.service, self.account)
                if backend.get_password(self.service, self.account) is not None:
                    raise OSError("Credential Manager deletion verification failed")
                vault_cleared = True
                if local:
                    self.path.unlink(missing_ok=True)
                self.message = "Ключ видалено зі сховища."
                return True
        except Exception:
            # OS/backend exceptions can echo the key. Only static feedback escapes.
            pass
        if local:
            try:
                self.write_local(key)
                if key:
                    self.message = _LOCAL_SAVED
                elif vault_cleared:
                    self.message = "Ключ видалено зі сховища."
                else:
                    self.message = ("Ключ очищено з програми. Старий запис Credential Manager "
                                "не вдалося видалити; він більше не завантажуватиметься.")
                return True
            except Exception:
                pass
        return False

def _default_store():
    return ProtectedKeyStore(SERVICE, _key_path(), account=ACCOUNT,
                             reader=_read_local_key, writer=_write_local_key)


def provider_key_store(provider):
    services = {"googlecloud": "WordLookup.GoogleCloud", "libretranslate": "WordLookup.LibreTranslate"}
    if provider not in services:
        raise ValueError("Unknown credential provider")
    return ProtectedKeyStore(services[provider], data_dir() / (provider + "-key.dpapi"))


def load_key():
    global _message
    _message = ""
    store = _default_store()
    key = store.load()
    _message = store.message
    return key


def store_key(key):
    global _message
    store = _default_store()
    success = store.store(key)
    _message = store.message
    return success
