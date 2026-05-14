@echo off
setlocal

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%" || goto error

if "%DJANGO_HOST%"=="" set "DJANGO_HOST=127.0.0.1"
if "%DJANGO_PORT%"=="" set "DJANGO_PORT=8000"
set "SERVER_URL=http://%DJANGO_HOST%:%DJANGO_PORT%/"

if not exist ".venv\Scripts\python.exe" (
    echo Creating local virtual environment...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv || goto error
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo Python 3 was not found. Install Python 3 first, then run this script again.
            goto error
        )
        python -m venv .venv || goto error
    )
)

set "PYTHON=.venv\Scripts\python.exe"

echo Installing/updating Python packages...
"%PYTHON%" -m pip install --upgrade pip || goto error
"%PYTHON%" -m pip install -r requirements.txt || goto error

echo Preparing local data folders...
for %%D in (
    "data\incoming"
    "data\active_final"
    "data\old_versions"
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

"%PYTHON%" manage.py runserver %DJANGO_HOST%:%DJANGO_PORT%
goto end

:error
echo.
echo Startup failed. Review the message above, then run this script again.
pause
exit /b 1

:end
endlocal
