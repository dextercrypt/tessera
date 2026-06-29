@echo off
REM ========================================================================
REM tess - Windows setup script  (bootstrap only)
REM
REM Run once per laptop. Creates a dedicated venv, installs dependencies,
REM copies the program in, creates the config dir, drops the config template,
REM sets ONLY the constant environment variables, and registers the logoff
REM cleanup task. It does NOT set role/region/identity vars (synced by
REM `tess start`) and never prompts for any values.
REM ========================================================================

setlocal enabledelayedexpansion

echo.
echo === tess setup (Windows) ===
echo.

REM ---- 1. Verify Python 3.10+ is installed ----
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python is not on PATH.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During install, check "Add Python to PATH".
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo Found Python !PY_VER!

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required. Found !PY_VER!.
    exit /b 1
)

REM ---- 2. Locate the program source (..\src relative to this script) ----
set SCRIPT_DIR=%~dp0
set SRC_DIR=%SCRIPT_DIR%..\src\
if not exist "%SRC_DIR%tess.py" (
    echo ERROR: tess.py not found in %SRC_DIR%
    echo Run this script from the setup\ folder of an intact tess bundle.
    exit /b 1
)
if not exist "%SRC_DIR%tess-config.example.json" (
    echo ERROR: tess-config.example.json not found in %SRC_DIR%
    exit /b 1
)
if not exist "%SRC_DIR%requirements.txt" (
    echo ERROR: requirements.txt not found in %SRC_DIR%
    exit /b 1
)

REM Welcome banner (rendered by Python so it is byte-identical on every OS).
python "%SRC_DIR%tess.py" _banner 2>nul
echo.

REM ---- 3. Create data + config directories and venv ----
set DATA_DIR=%LOCALAPPDATA%\tess
set VENV_DIR=%DATA_DIR%\venv
set CONFIG_DIR=%APPDATA%\tess

echo Data directory:   %DATA_DIR%
echo Config directory: %CONFIG_DIR%

if exist "%VENV_DIR%" (
    echo Removing existing venv...
    rmdir /s /q "%VENV_DIR%"
)

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"

echo Creating venv at %VENV_DIR%...
python -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo ERROR: Failed to create venv.
    exit /b 1
)

REM ---- 4. Install dependencies into the venv ----
echo Installing dependencies into venv...
"%VENV_DIR%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV_DIR%\Scripts\python.exe" -m pip install --quiet -r "%SRC_DIR%requirements.txt"
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    exit /b 1
)

REM ---- 5. Copy tess.py in; drop the config template into the config dir ----
REM Clear any read-only attribute from a prior install so the copy can overwrite,
REM then re-apply read-only so the live script can't be edited by accident.
if exist "%DATA_DIR%\tess.py" attrib -R "%DATA_DIR%\tess.py" >nul 2>nul
copy /y "%SRC_DIR%tess.py" "%DATA_DIR%\tess.py" >nul
attrib +R "%DATA_DIR%\tess.py" >nul 2>nul
echo Copied tess.py to %DATA_DIR% (read-only)
copy /y "%SRC_DIR%tess-config.example.json" "%CONFIG_DIR%\tess-config.example.json" >nul
echo Dropped config template: %CONFIG_DIR%\tess-config.example.json

REM ---- 6. Create wrapper batch file on PATH ----
REM Use WindowsApps which is already on every user's PATH
set WRAPPER_DIR=%LOCALAPPDATA%\Microsoft\WindowsApps
if not exist "%WRAPPER_DIR%" mkdir "%WRAPPER_DIR%"
set WRAPPER=%WRAPPER_DIR%\tess.bat

(
    echo @echo off
    echo "%VENV_DIR%\Scripts\python.exe" "%DATA_DIR%\tess.py" %%*
) > "%WRAPPER%"

echo Created wrapper: %WRAPPER%

REM ---- 7. Set ONLY the constant env vars ----
REM Constants never change for the life of the install: the token file path and
REM the regional-STS flag. setx writes the user registry, so all later-launched
REM processes (GUI and terminal) inherit them. The changeable vars (role ARN,
REM region, session name) are synced by `tess start`, so they are NOT set here.
set "TOKEN_FILE=%DATA_DIR%\token"

setx AWS_WEB_IDENTITY_TOKEN_FILE "%TOKEN_FILE%" >nul
setx AWS_STS_REGIONAL_ENDPOINTS "regional" >nul

echo Set constant env vars:
echo   AWS_WEB_IDENTITY_TOKEN_FILE=%TOKEN_FILE%
echo   AWS_STS_REGIONAL_ENDPOINTS=regional

REM ---- 8. Register Task Scheduler task for logoff cleanup ----
echo Registering Task Scheduler task for logout cleanup...

schtasks /delete /tn "tess-stop-on-logoff" /f >nul 2>nul

set TASK_XML=%TEMP%\tess-task.xml
(
    echo ^<?xml version="1.0" encoding="UTF-16"?^>
    echo ^<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
    echo   ^<Triggers^>
    echo     ^<EventTrigger^>
    echo       ^<Enabled^>true^</Enabled^>
    echo       ^<Subscription^>^&lt;QueryList^&gt;^&lt;Query Id="0"^&gt;^&lt;Select Path="Security"^&gt;*[System[EventID=4647]]^&lt;/Select^&gt;^&lt;/Query^&gt;^&lt;/QueryList^&gt;^</Subscription^>
    echo     ^</EventTrigger^>
    echo   ^</Triggers^>
    echo   ^<Actions^>
    echo     ^<Exec^>
    echo       ^<Command^>%WRAPPER%^</Command^>
    echo       ^<Arguments^>stop^</Arguments^>
    echo     ^</Exec^>
    echo   ^</Actions^>
    echo   ^<Settings^>
    echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
    echo     ^<AllowHardTerminate^>true^</AllowHardTerminate^>
    echo     ^<ExecutionTimeLimit^>PT30S^</ExecutionTimeLimit^>
    echo   ^</Settings^>
    echo ^</Task^>
) > "%TASK_XML%"

schtasks /create /tn "tess-stop-on-logoff" /xml "%TASK_XML%" /f >nul 2>&1
if errorlevel 1 (
    echo WARNING: Could not register Task Scheduler task. The 8-hour cap will still apply.
) else (
    echo Registered Task Scheduler task: tess-stop-on-logoff
)
del "%TASK_XML%" >nul 2>nul

REM ---- Done ----
echo.
echo === Setup complete ===
echo.
echo Config: no tess-config.json yet. Create it once:
echo   copy "%CONFIG_DIR%\tess-config.example.json" "%CONFIG_DIR%\tess-config.json"
echo   then edit: tenant_id, client_id, role_arn, region
echo (Or have it delivered to that path by your org, or pass --config / set TESS_CONFIG.)
echo.
echo Next steps:
echo   1. Sign out of Windows and sign back in once
echo      (so environment variables take effect for IntelliJ etc.)
echo   2. After signing back in, run: tess start
echo.

endlocal
exit /b 0
