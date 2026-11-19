@echo off
REM Media Grabber - start on Windows. Double-click this file.
cd /d "%~dp0"

REM Folder that contains ffmpeg.exe (change if yours is elsewhere)
set "FFMPEG_LOCATION=C:\downloader\ffmpeg-8.0.1-essentials_build\bin"

where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^). & pause & exit /b 1)

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up, this takes a few minutes...
  python -m venv .venv || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
  ".venv\Scripts\python.exe" -m playwright install chromium || (pause & exit /b 1)
)

REM keep yt-dlp current - YouTube changes often
".venv\Scripts\python.exe" -m pip install -q --upgrade yt-dlp

echo.
echo Media Grabber is running at http://localhost:8000  (close this window to stop)
start "" http://localhost:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
