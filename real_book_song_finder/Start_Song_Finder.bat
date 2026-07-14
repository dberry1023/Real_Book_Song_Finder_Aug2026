@echo off
title Real Book Song Finder
echo Starting Real Book Song Finder...
cd /d "%~dp0"

rem Make sure dependencies are present (quick no-op if already installed)
python -c "import flask, pymupdf" 2>nul
if errorlevel 1 (
    echo Installing required packages...
    python -m pip install --quiet flask pymupdf
)

echo.
echo   Your browser will open at http://127.0.0.1:8765
echo   Keep this window open while using the app.
echo   Press Ctrl+C (or close this window) to stop.
echo.
python server.py
pause
