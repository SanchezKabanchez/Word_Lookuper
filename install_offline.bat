@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 exit /b 1
call install.bat
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements-offline.lock
if errorlevel 1 goto fail
echo Offline engine installed. Import an .argosmodel from Settings.
popd
exit /b 0
:fail
echo Offline installation failed. See the error above.
if not defined CI pause
popd
exit /b 1
