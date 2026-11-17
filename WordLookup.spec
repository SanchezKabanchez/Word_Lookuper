# Build from locked dependencies with: python scripts/build_exe.py
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata, collect_all

root = Path(SPECPATH)
datas = collect_data_files('wordlookup')
datas += collect_data_files('rapidocr_onnxruntime', include_py_files=False)
for package in ('rapidocr-onnxruntime', 'onnxruntime', 'numpy', 'requests', 'keyring',
                'pillow', 'mss', 'pynput', 'psutil', 'pygame', 'pytesseract', 'genanki'):
    datas += copy_metadata(package)
datas += [(str(root / 'LICENSE'), '.'), (str(root / 'THIRD_PARTY_NOTICES.md'), '.')]
datas += [(str(root / 'build' / 'THIRD_PARTY_LICENSES'), 'THIRD_PARTY_LICENSES')]
local_data, local_binaries, local_hidden = collect_all('ctranslate2')
sp_data, sp_binaries, sp_hidden = collect_all('sentencepiece')
datas += local_data + sp_data + copy_metadata('ctranslate2') + copy_metadata('sentencepiece')
hidden = collect_submodules('rapidocr_onnxruntime') + local_hidden + sp_hidden
hidden += ['keyring.backends.Windows', 'win32ctypes', 'pynput.keyboard._win32',
           'pynput.mouse._win32', 'pygame.mixer', 'numpy', 'cv2', 'onnxruntime']
a = Analysis([str(root / 'word_lookup_launcher.py')], pathex=[str(root)],
             binaries=local_binaries + sp_binaries, datas=datas, hiddenimports=hidden,
             excludes=['argostranslate', 'torch', 'stanza', 'minisbd', 'pytest', 'mypy', 'pip_audit'],
             noarchive=False, module_collection_mode={'pynput': 'py', 'pygame': 'py'})
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WordLookup', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, manifest=str(root / "WordLookup.manifest"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='WordLookup')
