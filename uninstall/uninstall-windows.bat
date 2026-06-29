@echo off
REM ========================================================================
REM tess - Windows uninstall script
REM
REM Removes everything setup-windows.bat installed:
REM   - Stops any active session
REM   - Removes the Task Scheduler task
REM   - Removes environment variables
REM   - Removes the wrapper from PATH
REM   - Deletes the data directory (including the venv)
REM ========================================================================

setlocal

echo.
echo === tess uninstall (Windows) ===
echo.

set DATA_DIR=%LOCALAPPDATA%\tess
set CONFIG_DIR=%APPDATA%\tess
set WRAPPER=%LOCALAPPDATA%\Microsoft\WindowsApps\tess.bat

REM ---- 1. Stop any active session ----
if exist "%WRAPPER%" (
    echo Stopping any active session...
    call "%WRAPPER%" stop >nul 2>nul
)

REM ---- 2. Remove Task Scheduler task ----
schtasks /query /tn "tess-stop-on-logoff" >nul 2>nul
if not errorlevel 1 (
    echo Removing Task Scheduler task...
    schtasks /delete /tn "tess-stop-on-logoff" /f >nul 2>nul
)

REM ---- 3. Remove environment variables from HKCU\Environment ----
echo Removing environment variables...
for %%V in (AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_SESSION_NAME AWS_STS_REGIONAL_ENDPOINTS AWS_REGION AWS_DEFAULT_REGION) do (
    reg query "HKCU\Environment" /v %%V >nul 2>nul && reg delete "HKCU\Environment" /v %%V /f >nul 2>nul
)

REM Notify other apps that env vars changed (best effort) via WM_SETTINGCHANGE
powershell -NoProfile -Command "foreach ($v in 'AWS_ROLE_ARN','AWS_WEB_IDENTITY_TOKEN_FILE','AWS_ROLE_SESSION_NAME','AWS_STS_REGIONAL_ENDPOINTS','AWS_REGION','AWS_DEFAULT_REGION') { [Environment]::SetEnvironmentVariable($v, $null, 'User') }" >nul 2>nul

REM ---- 4. Remove wrapper batch file ----
if exist "%WRAPPER%" (
    del /f /q "%WRAPPER%" >nul 2>nul
    echo Removed wrapper: %WRAPPER%
)

REM ---- 5. Delete the data directory (includes venv with all Python deps) ----
if exist "%DATA_DIR%" (
    echo Removing data directory: %DATA_DIR%
    REM Clear read-only attributes (tess.py is installed read-only) so removal succeeds.
    attrib -R "%DATA_DIR%\*" /s /d >nul 2>nul
    rmdir /s /q "%DATA_DIR%"
)

REM ---- 6. Clean the config dir: remove our template. Preserve a
REM user-authored tess-config.json (it's their data). rmdir only if empty. ----
if exist "%CONFIG_DIR%\tess-config.example.json" del /f /q "%CONFIG_DIR%\tess-config.example.json" >nul 2>nul
if exist "%CONFIG_DIR%" (
    rmdir "%CONFIG_DIR%" >nul 2>nul && (
        echo Removed config directory: %CONFIG_DIR%
    ) || (
        echo Kept config directory ^(still contains your tess-config.json^): %CONFIG_DIR%
    )
)

echo.
echo === Uninstall complete ===
echo.
echo Notes:
echo   - Sign out and back in once to fully clear the env vars from any running apps.
echo   - Python itself was not uninstalled (it was already on your system).
echo.

endlocal
exit /b 0
