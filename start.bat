@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
    call install.bat
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import sys, tkinter; assert (3, 11) <= sys.version_info[:2] <= (3, 13)" >nul 2>&1
if errorlevel 1 (
    call install.bat
    if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" word_lookup_launcher.py
if errorlevel 1 goto fail
popd
exit /b 0

:fail
echo Word Lookup could not start. Run install.bat to repair dependencies.
if not defined CI pause
popd
exit /b 1
