@echo off
title VISION LIVE - realtime prices + algorithm
setlocal
set "VL=%USERPROFILE%\vision_live"
set "RAW=https://raw.githubusercontent.com/16VITAWS/market-scanner/main/runner"
if not exist "%VL%" mkdir "%VL%"
cd /d "%VL%"

echo.
echo  VISION LIVE - realtime NSE prices (Shoonya / Angel One free API) and the VISION AI algorithm.
echo  Default mode is PAPER: no real money moves. Nothing here is investment advice.
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  Installing Python (one time, about 2 minutes)...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo  Python installed. Close this window and double-click VISION-LIVE again.
  pause
  exit /b
)

echo  Getting the latest VISION LIVE program...
powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing '%RAW%/vision_live.py' -OutFile 'vision_live.py'; Invoke-WebRequest -UseBasicParsing '%RAW%/requirements-live.txt' -OutFile 'requirements-live.txt'"
if not exist vision_live.py (
  echo  Could not download the program. Check the internet connection and try again.
  pause
  exit /b
)
python -m pip install --quiet --disable-pip-version-check -r requirements-live.txt

if not exist settings.env (
  python -c "import vision_live as v; v.load_settings()"
  echo.
  echo  FIRST TIME: Notepad will open your settings file.
  echo  Paste your 3 Shoonya details (SHOONYA_UID, SHOONYA_CLIENT_ID, SHOONYA_SECRET) after the = signs, save, close Notepad.
echo  Each morning click 'Login to Shoonya' on the live screen and log in on Shoonya's own page.
  echo  They stay only on this computer.
  echo.
  notepad settings.env
)

start "" http://127.0.0.1:8765
python vision_live.py
pause
