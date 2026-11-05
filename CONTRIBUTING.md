# Continuing this project

A hand-off guide: where things stand, how to test, and how the protocol was worked out, so you
can pick it up without the original history.

## Where it stands

`lmu_server.py` is a from-scratch server that the retail Le Mans Ultimate client (1.4200)
joins as if it were a RaceControl server. Working, tested against the real game:

- joining, track load, car choice, slot assignment
- the Drive button and driving out of the garage
- session schedule (practice, qualifying, race, then repeat) with a proper race start
- clock sync, car relay between players (tested with a fake replay player, `--ghost`)
- clean disconnects, internet hosting with UPnP, the GUI toolkit

What's missing is tracked as [GitHub issues](https://github.com/WhoaThatCombo/lmu-local-server/issues).
The most useful next step is #1, a test with two real players on two PCs.

[PROTOCOL.md](PROTOCOL.md) documents everything known about the protocol; read it first.

## Code map

| File | What's in it |
|------|--------------|
| `lmu_server.py` | `Server`: HTTP side (`/SessionInfo`, `/Rules`, `/mp_vehicle`, tyres, teams). `SessionSchedule`: session order and clock. `SimServer`: TCP/UDP transport handshake, HotPacket system messages, NetComm events (cars, race state, relay). `main()`: arguments, UPnP, graceful stop. |
| `lmu_protocol_probe.py` | Logging (`log`), plain listeners, and the helper that makes the local game join (`--join`). |
| `upnp.py` | Router port mapping, standard library only. |
| `lmu_server_toolkit.py` | The tkinter GUI: runs the server, picks an owned car, joins, LAN discovery. `--join HOST [PORT] [PASSWORD] [--car ID]` on the command line. |
| `tools/live_test.ps1` | Live test against the game on this PC. |
| `tools/ghidra/` | Headless Ghidra scripts used for the reverse engineering. |

## Testing

You need LMU and the game data files the server reads (`data/`, see issue #8).

1. Start LMU through Steam. Add `+trace=3 +traceFlush` to its launch options to get a detailed
   log at `UserData\Log\trace_*.txt`. The game keeps that file open, so Explorer shows 0 bytes;
   open it with shared read access (the test script does).
2. Wait for the main menu, then either:
   - `powershell -ExecutionPolicy Bypass -File tools\live_test.ps1 -Seconds 120 -Drive -Car <owned car id>`
   - or by hand: `python lmu_server.py --join --run-seconds 300 --sessions P:2,R:3` (add
     `--ghost 3` to get a second car that replays yours 3 s later).
3. Read `lmu_server.log` and the trace. `NETLOG`, `State Change`, `Results` and `tires` lines
   show what the client did with each message.

Handy local REST endpoints of the game (port 6397; the full list is at
`http://127.0.0.1:6397/swagger-schema.json` while it runs):
`/rest/multiplayer/join/state`, `/rest/sessions/GetGameState`,
`/rest/race/getAllowedToStartRacing`, `POST /rest/garage/drive`,
`POST /rest/sessions/getAllAvailableVehicles` (body `null`).

### Things that bite

- **Stop the server gracefully** (Ctrl+C, `--run-seconds` or the toolkit button). A client
  whose server vanishes while it polls HTTP crashes.
- After a stop the ports stay busy for ~10 s. The server refuses to start a second copy.
- A game stuck in `JOIN_JOINING_SERVER`/`JOIN_SHUTTING_DOWN` refuses new joins; restart it.
- Event headers carry the session number; events for a different session are dropped.
  Event times are in 1/4096 s, the sync reply is a double in seconds.
- With a car from DLC you don't own, Drive never enables.
- Game crashes are sent to the developers' crash reporter (BugSplat). Keep tests to what you need.
- Don't script the game's UI with mouse/keyboard automation; everything needed is in the REST API.

## How the protocol was found

The client contains a lot of the server code (it is the same rFactor 2 engine), and its log
strings name the source file and often the message (`NetCommClient.cpp`, `HotPacket.cpp`,
`steward.cpp`, "ProcessRaceStateEvent2()", "Failed to receive tires from dedi"...). The method:

1. Make the server do something, read the trace for the line where the client complains or stops.
2. Find that string in the executable and decompile the functions that use it
   (`tools/ghidra/StrTrace.java`).
3. Follow the callers/callees (`DecompTree.java`), fields (`OffsetWriters.java`,
   `DataRefs.java`), constants (`ScalarUse.java`) and callbacks (`VtDecomp.java`).
4. Implement the smallest answer that satisfies the check, test again.

Useful anchors: the client's event dispatcher has a `switch` on the event type with cases like
`0xe3` "Received slot assignment"; `FUN_140d44cd0` maps every event type to a name; the Drive
check is the handler of `/rest/race/getAllowedToStartRacing`. Addresses are for 1.4200 and
move with every update; search by string instead.

Ghidra runs headless, for example:

```
analyzeHeadless <project dir> <project> -process "<program>" -noanalysis -readOnly \
  -scriptPath tools/ghidra -postScript StrTrace.java out.txt "Failed to receive tires"
```

Analyse your own copy of the game. The Steam executable is wrapped by Steam's DRM, so a static
disassembler can't read it as shipped; however you get a readable copy for analysis, keep it to
yourself.

## Rules for this repo

- Never commit game files: no executables (wrapped or unwrapped), no extracted game data (the
  `data/` folder is git-ignored), no decryption tools or keys for the game's archives.
- Don't publish anything that helps bypass anti-cheat or ownership checks. The server respects
  content ownership: players can only drive cars they own.
- Keep `PROTOCOL.md` up to date with every finding, including the trace line or function
  that proved it.

## Taking over the repo

The owner can add you as a collaborator (GitHub: Settings > Collaborators), or transfer the
repository (Settings > General > Transfer ownership). Forking works too.
