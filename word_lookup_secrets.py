"""DeepL credentials: Windows Credential Manager only, never a JSON fallback."""
import sys

SERVICE = "WordLookup.DeepL"
ACCOUNT = "api-key"


def _backend():
    if not sys.platform.startswith("win"):
        return None
    # Select the OS vault explicitly. A configured keyrings.alt plaintext backend
    # must never become an accidental fallback for API credentials.
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def load_key():
    backend = _backend()
    return (backend.get_password(SERVICE, ACCOUNT) or "") if backend else ""


def store_key(key):
    """Return whether the platform can persist this key securely.

    Non-Windows development keeps it in memory for this session only.
    Exceptions are handled by the caller without logging credential values.
    """
    backend = _backend()
    if backend is None:
        return False
    previous = backend.get_password(SERVICE, ACCOUNT)
    if key:
        if previous != key:
            backend.set_password(SERVICE, ACCOUNT, key)
    elif previous is not None:
        backend.delete_password(SERVICE, ACCOUNT)
    return True
