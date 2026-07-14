@echo off
title Real Book Song Finder (LAN / Tailscale mode)
echo Starting Real Book Song Finder in multi-device mode...
cd /d "%~dp0"

python -c "import flask, pymupdf" 2>nul
if errorlevel 1 (
    echo Installing required packages...
    python -m pip install --quiet flask pymupdf
)

echo.
echo   This PC:        http://127.0.0.1:8765
echo   Other devices:   see the access URL + token printed below
echo   (requires Tailscale installed and signed in on both this PC
echo    and the other device - see README.md)
echo.
echo   Keep this window open, or use the auto-start setup instead
echo   so this always runs in the background.
echo.
python server.py --lan
pause
