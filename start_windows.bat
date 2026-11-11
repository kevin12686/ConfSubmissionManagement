@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || goto error

if "%DJANGO_HOST%"=="" set "DJANGO_HOST=127.0.0.1"
if "%DJANGO_PORT%"=="" set "DJANGO_PORT=8000"
set "SERVER_URL=http://%DJANGO_HOST%:%DJANGO_PORT%/"

if exist ".venv" goto validate_venv
where py >nul 2>nul
if errorlevel 1 goto bootstrap_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || goto python_version_error
echo Creating local virtual environment...
py -3 -m venv .venv || goto venv_error
goto validate_venv

:bootstrap_python
where python >nul 2>nul
if errorlevel 1 goto python_not_found
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" || goto python_version_error
echo Creating local virtual environment...
python -m venv .venv || goto venv_error

:validate_venv
set "PYTHON=%ROOT_DIR%.venv\Scripts\python.exe"
if not exist "%PYTHON%" goto venv_error
"%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) and sys.prefix != sys.base_prefix else 1)" || goto venv_error
"%PYTHON%" -m pip --version >nul 2>nul || goto venv_error

echo Installing/updating Python packages...
"%PYTHON%" -m pip install --upgrade pip || goto error
"%PYTHON%" -m pip install -r requirements.txt || goto error
"%PYTHON%" -m pip check || goto error

if not exist "scripts\check_comparison_runtime.py" goto checker_missing
echo Checking comparison runtime...
"%PYTHON%" scripts\check_comparison_runtime.py || goto error

echo Running Django system checks...
"%PYTHON%" manage.py check || goto error

echo Preparing local data folders...
for %%D in (
    "data\reports"
    "data\extraction_results"
    "data\plagiarism_reports"
    "data\media"
) do (
    if not exist "%%~D" (
        mkdir "%%~D" || goto error
    )
)

echo Applying database migrations...
"%PYTHON%" manage.py migrate || goto error

echo.
echo Conference Final Manager is starting.
echo Open: %SERVER_URL%
echo Press Ctrl+C in this window to stop the server.
echo.

if not "%OPEN_BROWSER%"=="0" (
    start "" cmd /c "timeout /t 2 /nobreak >nul && start "" "%SERVER_URL%""
)

"%PYTHON%" manage.py runserver "%DJANGO_HOST%:%DJANGO_PORT%" || goto error
goto end

:python_not_found
echo Python 3.12 or newer was not found. Install it with the Python launcher or add python.exe to PATH.
goto error

:python_version_error
echo Python 3.12 or newer is required. Install a supported version, then run this script again.
goto error

:venv_error
echo The .venv environment is incomplete, broken, or does not use Python 3.12 or newer.
echo Check Python venv support. Rename only .venv and rerun this script; do not remove conference data.
goto error

:checker_missing
echo Startup checker is missing. Update or restore the complete project checkout.
goto error

:error
echo.
echo Startup failed. Review the message above, then run this script again.
pause
exit /b 1

:end
endlocal
