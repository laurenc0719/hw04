@echo off
REM ---------------------------------------------------------------------------
REM  HW4 - one-click runner (Windows): runs every script in order
REM  Creates a private virtual environment in hw04\.venv (first run only),
REM  installs requirements.txt into it, then runs:
REM      scripts\01_profile.py    (Part A audit)
REM      scripts\02_clean.py      (Part C cleaning)
REM      scripts\03_validate.py   (Part D validation + scorecard)
REM  Stops at the first script that fails.
REM  Works no matter which Python VS Code or the terminal has selected.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install

echo Creating virtual environment in .venv ...
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ERROR: could not create a virtual environment. Is Python 3 installed?
    echo Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

:install
echo Installing requirements ...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed - check the internet connection and try again.
    pause
    exit /b 1
)

for %%S in (01_profile.py 02_clean.py 03_validate.py) do (
    if not exist "scripts\%%S" (
        echo.
        echo ERROR: scripts\%%S not found. Stopping.
        pause
        exit /b 1
    )
    echo.
    echo ===== Running scripts\%%S =====
    ".venv\Scripts\python.exe" "scripts\%%S"
    if errorlevel 1 (
        echo.
        echo scripts\%%S failed - see the message above. Stopping.
        pause
        exit /b 1
    )
)

echo.
echo Done. Reports are in reports\, clean data in data\clean\, audit trail in data\audit\.
pause
exit /b 0
