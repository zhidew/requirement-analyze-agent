@echo off
chcp 65001 >nul
echo ============================================
echo   Requirement Analyze Agent - Start All Services
echo ============================================
echo.
echo This script will start both frontend and backend services
echo.
echo [Backend] http://localhost:8000
echo [Frontend] http://localhost:5173
echo.
echo Press any key to start services...
pause >nul

echo.
echo [Starting] Launching services...

REM Start backend (in new window)
start "Requirement Analyze Agent - Backend" cmd /c "start-backend.bat"

REM Wait for backend to start
timeout /t 3 /nobreak >nul

REM Start frontend (in new window)
start "Requirement Analyze Agent - Frontend" cmd /c "start-frontend.bat"

echo.
echo ============================================
echo   Services Started
echo ============================================
echo.
echo Backend:  http://localhost:8000
echo Frontend: http://localhost:5173
echo API Docs: http://localhost:8000/docs
echo.
echo Tip: Close the corresponding command window to stop the service
echo ============================================
echo.
pause
