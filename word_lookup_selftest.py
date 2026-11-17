"""Explicit packaged-app smoke check; no hotkeys, network, vault or game pause."""
import json
from pathlib import Path
import tkinter as tk
from word_lookup_version import __version__


def run(path: str) -> None:
    from PIL import Image
    from word_lookup_lexicon import lookup
    from word_lookup_ocr_budget import create_rapid_engine
    import numpy as np
    # Instantiate the real bundled backend, then exercise a blank image once.
    engine = create_rapid_engine(1, 640)
    engine(np.asarray(Image.new('RGB', (320, 100), 'white')), use_cls=False)
    import ctranslate2
    import sentencepiece
    assert ctranslate2.get_supported_compute_types("cpu")
    assert sentencepiece.SentencePieceProcessor
    from keyring.backends.Windows import WinVaultKeyring
    assert WinVaultKeyring
    assert lookup('kindness')
    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
        output = {'ok': True, 'version': __version__, 'tk': str(root.tk.call('info', 'patchlevel'))}
    finally:
        root.destroy()
    Path(path).write_text(json.dumps(output), encoding='utf-8')
