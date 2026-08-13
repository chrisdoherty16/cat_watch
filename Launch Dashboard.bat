@echo off
setlocal
title Capital Markets Dashboard

REM ===========================================================
REM  DOUBLE-CLICK THIS FILE TO LAUNCH.
REM  It finds uv automatically, even if it's not on the
REM  Windows PATH (the reason the old .bat failed).
REM  A browser tab opens at http://localhost:8501.
REM  To stop the dashboard: close this black window.
REM ===========================================================

REM Always run from THIS file's own folder.
cd /d "%~dp0"

REM --- Locate uv.exe ----------------------------------------
set "UV="

REM 1) Is uv already on PATH (cmd can see it)?
where uv >nul 2>nul
if %errorlevel%==0 set "UV=uv"

REM 2) Standard uv install location for your user.
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"

REM 3) Alternative install location (some installers use this).
if not defined UV if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" set "UV=%LOCALAPPDATA%\Programs\uv\uv.exe"

REM 4) Cargo-style install location.
if not defined UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"

if not defined UV (
    echo.
    echo   Could not find "uv" on this computer.
    echo   Open Git Bash and run this once to see where it lives:
    echo        which uv
    echo   then tell me the path and I'll point this launcher at it.
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting the Capital Markets Dashboard...
echo   Using uv at: %UV%
echo   A browser tab will open at http://localhost:8501 shortly.
echo   Keep this window open while using it. Close it to stop.
echo.

"%UV%" run streamlit run app.py

REM Keep the window open so any error stays readable.
echo.
echo   Dashboard stopped. Press any key to close this window.
pause >nul
endlocal
