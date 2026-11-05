<#
Live test against the real game on this PC.

Expects LMU running at the main menu (start it through Steam with launch options
"+trace=3 +traceFlush" to get the trace). Runs the server for -Seconds with the local game
joined, optionally presses Drive, then prints the server log summary and the new lines of the
game's trace. It never starts or closes the game on its own: pass -LaunchGame to let it (re)start
LMU through Steam when the game is not at the main menu.

  powershell -ExecutionPolicy Bypass -File tools\live_test.ps1 -Seconds 120 -Drive
  powershell -ExecutionPolicy Bypass -File tools\live_test.ps1 -Seconds 200 -Extra "--ghost 3 --sessions P:1,Q:1,R:3"

-Car takes a car id from POST /rest/sessions/getAllAvailableVehicles (must be owned), for
example 23_25_UNIT907820FF. Without it the game uses the car last picked in its menus, and
Drive stays disabled if that car is from DLC you don't own.
Crashes of the game are sent to the developers' crash reporter (BugSplat) as usual.
#>
param([int]$Seconds = 90, [string]$Car = "", [switch]$Drive, [switch]$LaunchGame, [string]$Extra = "",
      [string]$GameDir = "")

$repo = Split-Path -Parent $PSScriptRoot
$api = "http://127.0.0.1:6397"
if (-not $GameDir) { $GameDir = (python -c "import sys; sys.path.insert(0, r'$repo'); import lmu_server_toolkit as t; print(t.find_game_dir())").Trim() }
$logDir = Join-Path $GameDir "UserData\Log"
$serverLog = Join-Path $env:TEMP "lmu_live_test_server.log"

function Get-Rest($path) { try { (Invoke-WebRequest -UseBasicParsing "$api$path" -TimeoutSec 3).Content } catch { $null } }
function JoinState { $s = Get-Rest "/rest/multiplayer/join/state"; if ($s) { $s.Trim('"') } else { "?" } }

$state = JoinState
if ($state -notmatch 'JOIN_IDLE|JOIN_FAILURE') {
  if (-not $LaunchGame) {
    "LMU is not at the main menu (join state: $state). Start it through Steam and wait for the main menu,"
    "or pass -LaunchGame to let this script (re)start it. Nothing was started."
    exit 1
  }
  "starting the game through Steam (join state: $state)"
  if (Get-Process "Le Mans Ultimate" -ErrorAction SilentlyContinue) {
    taskkill /F /IM "Le Mans Ultimate.exe" | Out-Null
    while (Get-Process "Le Mans Ultimate" -ErrorAction SilentlyContinue) { Start-Sleep 1 }
    Start-Sleep 8
  }
  $steam = (Get-ItemProperty "HKCU:\Software\Valve\Steam").SteamPath
  Start-Process (Join-Path $steam "steam.exe") -ArgumentList "-applaunch", "2399420", "+trace=3", "+traceFlush"
  $deadline = (Get-Date).AddMinutes(4)
  while (-not (Get-Rest "/rest/multiplayer/steam/status") -and (Get-Date) -lt $deadline) { Start-Sleep 5 }
  Start-Sleep 15
}

# The game keeps its trace open; read it with shared access (Explorer shows 0 bytes meanwhile).
$trace = Get-ChildItem $logDir -Filter "trace_*.txt" | Sort-Object CreationTime -Descending | Select-Object -First 1
function Read-Trace { if (-not $trace) { return @() }; $fs = [IO.File]::Open($trace.FullName, 'Open', 'Read', 'ReadWrite'); $sr = New-Object IO.StreamReader($fs); $t = $sr.ReadToEnd(); $sr.Close(); $t -split "`n" }
$before = (Read-Trace).Count

# A previous server keeps its ports for ~10 s while it shuts down.
while (Get-NetTCPConnection -LocalPort 64298 -State Listen -ErrorAction SilentlyContinue) { Start-Sleep 2 }
Remove-Item $serverLog -ErrorAction SilentlyContinue
$args2 = @("-u", "lmu_server.py", "--game-dir", $GameDir, "--run-seconds", [string]($Seconds + 5), "--log", $serverLog) + ($Extra -split ' ' | Where-Object { $_ })
$server = Start-Process python -ArgumentList $args2 -WorkingDirectory $repo -PassThru -WindowStyle Hidden
Start-Sleep 4

$joinArgs = @("lmu_server_toolkit.py", "--join", "127.0.0.1", "64298")
if ($Car) { $joinArgs += @("--car", $Car) }
Push-Location $repo; python @joinArgs; Pop-Location

if ($Drive) {
  $allowed = $false
  for ($i = 0; $i -lt 20 -and -not $allowed; $i++) { Start-Sleep 2; $allowed = (Get-Rest "/rest/race/getAllowedToStartRacing") -eq "true" }
  "Drive allowed: $allowed"
  if ($allowed) { try { Invoke-WebRequest -UseBasicParsing -Method Post -Body "null" -ContentType "application/json" "$api/rest/garage/drive" -TimeoutSec 10 | Out-Null; "pressed Drive" } catch { "Drive failed: $_" } }
}

$end = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $end -and -not $server.HasExited) {
  $g = Get-Rest "/rest/sessions/GetGameState"
  if ($g -match '"MultiStintState":"([^"]*)".*"gamePhase":"([^"]*)"') { "{0:HH:mm:ss} {1} {2}" -f (Get-Date), $Matches[1], $Matches[2] }
  Start-Sleep 10
}
$server.WaitForExit(40000) | Out-Null

"===== server log (no hex dumps, no polling)"
Get-Content $serverLog | Where-Object { $_ -match '^\d' -and $_ -notmatch 'teams/list|sync request|-> 200' }
"===== game trace (new lines, filtered)"
Read-Trace | Select-Object -Skip $before | Where-Object { $_ -match 'NETLOG|State Change|Results|FATAL|bugsplat|tires' -and $_ -notmatch 'ziplocs' } | Select-Object -First 150
