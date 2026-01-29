@echo off
taskkill /F /IM pythonw.exe 2>nul
if %errorlevel%==0 (
    echo Dashboard stopped.
) else (
    echo No dashboard process found.
)
