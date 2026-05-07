@echo off
chcp 65001 >nul
echo ============================================
echo   Requirement Analyze Agent - Stop All Services
echo ============================================
echo.
echo Stopping all services...
echo.

REM Stop Python process (uvicorn)
tasklist /FI "WINDOWTITLE eq Requirement Analyze Agent - Backend*" 2>nul | find "cmd.exe" >nul
if %errorlevel% equ 0 (
    taskkill /FI "WINDOWTITLE eq Requirement Analyze Agent - Backend*" /F >nul 2>&1
    echo [OK] Backend service stopped
) else (
    echo [i] Backend service not running
)

REM Stop Node.js process (vite)
tasklist /FI "WINDOWTITLE eq Requirement Analyze Agent - Frontend*" 2>nul | find "cmd.exe" >nul
if %errorlevel% equ 0 (
    taskkill /FI "WINDOWTITLE eq Requirement Analyze Agent - Frontend*" /F >nul 2>&1
    echo [OK] Frontend service stopped
) else (
    echo [i] Frontend service not running
)

echo.
echo ============================================
echo   All Services Stopped
echo ============================================
echo.
pause
