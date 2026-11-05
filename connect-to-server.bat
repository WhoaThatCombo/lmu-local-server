@echo off
rem Usage: connect-to-server.bat HOST [HTTP_PORT] [PASSWORD]
rem Tells a running Le Mans Ultimate to join HOST:HTTP_PORT through its local API on port 6397,
rem then enters the session the way the game's own menu does.
setlocal
cd /d "%~dp0"
set "HOST=%~1"
set "PORT=%~2"
set "PASS=%~3"
if "%HOST%"=="" set /p "HOST=Server IP: "
if "%PORT%"=="" set "PORT=64298"
python lmu_server_toolkit.py --join "%HOST%" "%PORT%" "%PASS%"
pause
