@echo off
rem Usage: connect-to-server.bat HOST [HTTP_PORT] [PASSWORD]
rem Tells a running Le Mans Ultimate to join HOST:HTTP_PORT through its local API on port 6397.
setlocal
set "HOST=%~1"
set "PORT=%~2"
set "PASS=%~3"
if "%HOST%"=="" set /p "HOST=Server IP: "
if "%PORT%"=="" set "PORT=64297"

curl -s -o nul --max-time 2 "http://127.0.0.1:6397/rest/multiplayer/steam/status" || (
    echo Le Mans Ultimate is not running. Start the game first.
    pause
    exit /b 1
)

curl -s --get "http://127.0.0.1:6397/rest/multiplayer/join" ^
    --data-urlencode "host=%HOST%" ^
    --data-urlencode "port=%PORT%" ^
    --data-urlencode "password=%PASS%" ^
    --data-urlencode "authentication="
echo.
echo Join request sent to %HOST%:%PORT%. Switch to the game.
pause
