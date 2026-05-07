@echo off
chcp 65001 >nul
echo ============================================
echo   Requirement Analyze Agent - Frontend Service
echo ============================================
echo.

cd /d "%~dp0\admin-ui"

REM Check if Node.js is installed
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [Error] Node.js not detected. Please install Node.js 18+
    pause
    exit /b 1
)

REM Check if node_modules exists
if not exist "node_modules" (
    echo [Installing] First run, installing dependencies...
    echo.
    npm install
    if %errorlevel% neq 0 (
        echo [Error] Dependency installation failed
        pause
        exit /b 1
    )
    echo.
)

echo.
echo [Starting] Launching frontend service...
echo [URL]     http://localhost:5173
echo.
echo Press Ctrl+C to stop the service
echo ============================================
echo.

npm run dev
