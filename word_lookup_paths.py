"""One platform-specific data directory for settings, keys and recovery."""
import os
from pathlib import Path
import sys


def data_dir() -> Path:
    if os.environ.get("WORD_LOOKUP_DATA_DIR"):
        return Path(os.environ["WORD_LOOKUP_DATA_DIR"]).expanduser().resolve()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "WordLookup"


def config_path() -> str:
    return str(data_dir() / "config.json")
