@echo off
rem Opens the LMU Local Server Toolkit window.
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw lmu_server_toolkit.py & exit /b 0)
where python >nul 2>&1 && (python lmu_server_toolkit.py & exit /b 0)
echo Python 3.8+ is required: https://www.python.org/downloads/
pause
