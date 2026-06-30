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

REM ---- 2. Locate the program source: local ..\src, else fetch the pinned release ----
REM Bump TESS_REF when cutting a new release.
set SCRIPT_DIR=%~dp0
set TESS_REF=v1.0.0
set RAW_BASE=https://raw.githubusercontent.com/dextercrypt/tessera/%TESS_REF%
set STAGE=%TEMP%\tessera-setup
set SRC_DIR=%SCRIPT_DIR%..\src\

if exist "%SRC_DIR%tess.py" (
    echo Using local source: %SRC_DIR%
) else (
    echo Downloading and verifying tess %TESS_REF%...
    if exist "%STAGE%" rmdir /s /q "%STAGE%"
    mkdir "%STAGE%\src"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop';" ^
      "$base='%RAW_BASE%'; $stage='%STAGE%';" ^
      "foreach($f in 'src/tess.py','src/requirements.txt','src/tess-config.example.json','SHA256SUMS'){ $out=Join-Path $stage ($f -replace '/','\'); Invoke-WebRequest -UseBasicParsing \"$base/$f\" -OutFile $out }" ^
      "foreach($l in Get-Content (Join-Path $stage 'SHA256SUMS')){ if($l -match '^([0-9a-fA-F]{64})\s+\*?(.+)$'){ $want=$Matches[1].ToLower(); $rel=$Matches[2].Trim(); $p=Join-Path $stage ($rel -replace '/','\'); $got=(Get-FileHash -Algorithm SHA256 $p).Hash.ToLower(); if($want -ne $got){ throw \"checksum mismatch for $rel\" } } }" ^
      "Write-Host 'Verified tess %TESS_REF%.'"
    if errorlevel 1 (
        echo ERROR: download or checksum verification failed - aborting install.
        exit /b 1
    )
    set SRC_DIR=%STAGE%\src\
)

REM Welcome banner (rendered by Python so it is byte-identical on every OS).
python "%SRC_DIR%tess.py" _banner 2>nul
echo.

REM ---- 3. Create data + config directories and venv ----
set DATA_DIR=%LOCALAPPDATA%\tess
set VENV_DIR=%DATA_DIR%\venv
set CONFIG_DIR=%APPDATA%\tess

REM Detect whether this is a fresh install or an update of an existing one.
if exist "%DATA_DIR%\tess.py" (set MODE=update) else (set MODE=install)

echo Data directory:   %DATA_DIR%
echo Config directory: %CONFIG_DIR%
if "%MODE%"=="update" (
    echo Existing installation found ^> updating tess in place.
) else (
    echo No existing installation ^> fresh install.
)

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
if "%MODE%"=="update" (
    echo === Update complete ===
    echo.
    echo tess was already installed — the program was updated in place.
    echo If a session is currently running, reload the new code with:
    echo   tess stop ^&^& tess start
) else (
    echo === Setup complete ===
)
echo.
if exist "%CONFIG_DIR%\tess-config.json" (
    echo Config: tess-config.json already present at %CONFIG_DIR% ^(left untouched^).
) else (
    echo Config: no tess-config.json yet. Create it once:
    echo   copy "%CONFIG_DIR%\tess-config.example.json" "%CONFIG_DIR%\tess-config.json"
    echo   then edit: tenant_id, client_id, role_arn, region
    echo ^(Or have it delivered to that path by your org, or pass --config / set TESS_CONFIG.^)
)
echo.
echo Next steps:
echo   1. Sign out of Windows and sign back in once
echo      (so environment variables take effect for IntelliJ etc.)
echo   2. After signing back in, run: tess start
echo.

REM Clean up the fetch staging dir (only exists if we downloaded a release).
if exist "%STAGE%" rmdir /s /q "%STAGE%" >nul 2>nul

endlocal
exit /b 0
