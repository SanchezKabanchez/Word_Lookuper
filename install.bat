@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 exit /b 1

if not exist ".venv\Scripts\python.exe" goto select_python
".venv\Scripts\python.exe" -c "import sys, tkinter; assert (3, 11) <= sys.version_info[:2] <= (3, 13)" >nul 2>&1
if not errorlevel 1 goto install
set "WL_OLD_ENV=.venv-previous-%RANDOM%-%RANDOM%"
ren ".venv" "%WL_OLD_ENV%"
if errorlevel 1 goto fail
echo Previous Python environment retained in %WL_OLD_ENV%. User data are stored separately.

:select_python
set "WL_PYTHON="
for %%V in (3.13 3.12 3.11) do (
    if not defined WL_PYTHON (
        py -%%V -c "import sys, tkinter; assert (3, 11) <= sys.version_info[:2] <= (3, 13)" >nul 2>&1
        if not errorlevel 1 set "WL_PYTHON=py -%%V"
    )
)
if defined WL_PYTHON goto create
python -c "import sys, tkinter; assert (3, 11) <= sys.version_info[:2] <= (3, 13)" >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.11-3.13 from https://www.python.org/downloads/windows/
    echo Include pip, Tcl/Tk and the Python launcher, then run install.bat again.
    goto fail
)
set "WL_PYTHON=python"

:create
%WL_PYTHON% -m venv .venv
if errorlevel 1 goto fail

:install
".venv\Scripts\python.exe" -c "import sys, tkinter; assert (3, 11) <= sys.version_info[:2] <= (3, 13), 'Use Python 3.11-3.13; recreate .venv if necessary'"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
rem The universal constraints include the Windows Credential Manager backend.
rem RapidOCR bundles its models, so a separate Tesseract installation is optional.
".venv\Scripts\python.exe" -m pip install -r requirements-ocr.txt -c requirements.lock
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import tkinter, keyring, rapidocr_onnxruntime; from keyring.backends.Windows import WinVaultKeyring; print('Word Lookup is ready. Run start.bat.')"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" scripts\compile_translations.py
if errorlevel 1 goto fail
popd
exit /b 0

:fail
echo Installation failed. Check the error above, then run install.bat again.
if not defined CI pause
popd
exit /b 1
