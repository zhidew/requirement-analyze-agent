@echo off
chcp 65001 >nul
echo ============================================
echo   Requirement Analyze Agent - Backend Service
echo ============================================
echo.

cd /d "%~dp0"

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [Error] Python not detected. Please install Python 3.11+
    pause
    exit /b 1
)

REM Check if in virtual environment
if not defined VIRTUAL_ENV (
    echo [Info] Virtual environment not detected. Recommended to create one.
    echo.
    set /p create_venv="Create virtual environment? (y/n): "
    if /i "%create_venv%"=="y" (
        echo [Creating] Setting up virtual environment...
        python -m venv venv
        call venv\Scripts\activate.bat
        echo [Installing] Installing dependencies...
        pip install -r api_server\requirements.txt
    )
)

REM Check .env file
if not exist .env (
    echo [Warning] .env config file not found
    echo [Info] Please copy .env.example to .env and configure required environment variables
    pause
)

echo.
echo [Starting] Launching backend service...
echo [URL]    http://localhost:8000
echo [Docs]   http://localhost:8000/docs
echo.
echo Press Ctrl+C to stop the service
echo ============================================
echo.

cd api_server
python main.py
