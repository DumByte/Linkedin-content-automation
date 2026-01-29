@echo off
cd /d "%~dp0"
start /B pythonw dashboard/app.py
echo Dashboard started in background on http://127.0.0.1:5001
echo To stop it, run: taskkill /F /IM pythonw.exe
