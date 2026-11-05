# LMU Retail Binary — Server Hosting Analysis

**Binary:** Le Mans Ultimate v1.4200 (Build 25661166)  
**Engine:** rFactor 2 (rFactorSource)  
**Build path:** `D:\bamboo-agent-home\xml-data\build-dir\RF2-LM100-JOB1\code\rFactorSource\Source\`  

## Executive Summary

**The retail LMU executable cannot host a multiplayer server.** The binary shares
its codebase with the dedicated server (same rFactorSource), so server-side
networking code (bind, listen, accept, WSASocketW) is compiled in and present.
However, the code path that activates Server mode is completely absent from the
retail build. No amount of command-line flags, REST API calls, or configuration
will make the retail exe act as a server — the initialization simply doesn't exist.

---

## Architecture: The NetComm Mode System

The game's multiplayer state is controlled by two variables:

### Mode Field (`DAT_141dff960 + 0x21f80`)
| Value | State | Description |
|-------|-------|-------------|
| 0 | Single Player | Default / disconnected |
| 1 | **Server** | Hosting — **never written by retail code** |
| 2 | Client | Connected to a server |
| 3 | Connecting | Transitional (converts to 2 when established) |
| 4 | Disconnecting | Transitional (converts to 0 when complete) |

### Mode Name (`DAT_141a5a58c`)
Mirrors the mode field as logged strings: 1="Server", 2="Client", 3="Server and client".

### IsDedicated Flag (`DAT_143b4cb26`)
Boolean flag. Retail build hardcodes it to **0** (false) in `main.cpp`. Controls
whether telemetry reports `"build":"client"` or `"build":"dedicated"`.

---

## Evidence: No Path to Server Mode

### 1. Exhaustive Mode Field Write Catalog

A complete scan found **493 instructions** across **196 functions** that reference
the mode field (offset 0x21f80). Every single write was categorized:

| Written Value | Count | Description |
|---------------|-------|-------------|
| 0 (Single)    | ~2    | Disconnect/cleanup paths |
| 2 (Client)    | ~180+ | Mode 3→2 conversions (connection established) |
| 3 (Connecting)| 1     | JOIN function (`FUN_140b6c930` in NetCommClient.cpp) |
| 4 (Disconn.)  | 2     | Shutdown paths |
| **1 (Server)**| **0** | **Never written** |
| Register vals | ~12   | All resolve to 0, 2, 3, or 4 in decompiled context |

**No code path in the retail binary writes value 1 (Server) to the mode field.**

### 2. SetMode Function Never Called with "Server"

`FUN_140cda230` is the generic SetMode function:
```c
void FUN_140cda230(undefined8 param_1, int param_2) {
    // Log: "[State Change] Changing mode from %s to %s"
    // param_2: 1="Server", 2="Client", 3="Server and client"
    DAT_141a5a58c = param_2;
}
```

This function CAN set mode to 1 (Server). But across all callers in the binary,
it is only ever called with:
- `FUN_140cda230(x, 2)` — Set Client mode
- `FUN_140cda230(x, 3)` — Set "Server and client" (transitional)

**Never called with param_2 = 1.**

### 3. IsDedicated Hardcoded to False

In `main.cpp`, the IsDedicated flag is explicitly cleared:
```c
DAT_143b4cb26 = 0;  // Always "client" build
```

The telemetry function (`FUN_1403d1ca0`) checks this flag:
```c
if (DAT_143b4cb26 == '\0') {
    // "build":"client"     ← retail build always takes this path
} else {
    // "build":"dedicated"  ← never reached
}
```

### 4. Game Loop Assertion Against Dedicated Mode

The game loop tick function (`FUN_140cd0bd0` in game.cpp, line 0xa4e) stores an
assertion expression:
```
"false == gNetComm.IsDedicated()"
```

This explicitly asserts the retail game loop should NOT run in dedicated/server mode.

### 5. Game::Init Handles Mode==1 But Can't Reach It

`FUN_140cd36b0` (Game::Init in game.cpp) contains code that checks for mode==1:
```c
else if (uVar11 == 1) goto LAB_140cd3893;
```

This code IS present, confirming Server mode is a valid runtime state in the
engine. But since nothing sets mode to 1 before Game::Init runs, this branch
is dead code in the retail build.

---

## What IS Present (Shared Codebase Artifacts)

The retail binary contains all of these from the shared rFactor 2 codebase:

### Server Networking Infrastructure
- `bind()` — 7 xrefs, real callers in NetComm functions
- `listen()` — 6 xrefs
- `accept()` — 6 xrefs
- `AcceptEx()` — 3 xrefs
- `WSASocketW()` — 6 xrefs
- `FUN_14021fa90` — Complete server socket setup (WSASocket + bind + listen + accept)

### Server Mode Runtime Code
- `"Auto Advancing Server Session (%d) at et %.3f after %d seconds past session end"` — in `FUN_140df0f10` (O_AppGz.cpp), which checks `mode==1`
- IsServer() function (`FUN_1402a37a0`) — returns true when mode==1
- Mode==1 checks scattered through 196 functions
- Server-side session management, driver management, race control

### Command-Line Flags (Parsed but Ineffective)
- `+host` — Sets autohost flag at offset 0x14c. Logs `"[NETLOG] NetComm activated autohost mode"`. But nothing reads this flag to activate server mode.
- `+multiplayer` — Sets `DAT_141d7a51c = 1`. Checked during init but doesn't change mode.
- `+gamename ""` — Parsed by `FUN_140b68570` alongside +host
- `+port` — Parsed but never used for listening
- `+maxplayers` — Parsed but never applied

### REST API
- 183 endpoints on port 6397
- `GET /rest/multiplayer/join?host=&port=&password=&authentication=` — Client join only
- No endpoint exists to start hosting

---

## Why It's This Way

Studio 397 / Motorsport Games sell server hosting through RaceControl. The
dedicated server is a separate binary (same codebase, different build
configuration) that:

1. Sets `DAT_143b4cb26 = 1` (IsDedicated = true)
2. Has startup code that sets mode = 1 (Server)
3. Initializes listening sockets via the NetComm server functions
4. Runs without the graphics/UI game loop

The retail build deliberately omits the Server mode initialization path.
The +host and +multiplayer flags are vestigial from the rFactor 2 engine
where client-hosted servers were supported.

---

## Feasibility of Workarounds

### Memory Patching at Runtime
**Theoretically possible, practically fragile.**  
Would need to set mode field to 1, IsDedicated to 1, and mode name to 1 — but
the server initialization code (listening socket setup, server-side game state)
wouldn't run because the initialization ORDER matters: the mode must be 1 BEFORE
Game::Init, and the server networking must be set up in the right sequence.
The game loop assertion (`false == gNetComm.IsDedicated()`) would also fire.

### rFactor 2 Dedicated Server with LMU Content
**Ruled out.** The free rF2 dedicated server (Steam app 400300, last built
2023) has no `Rules`/`Components` join step, uses an older netcode without
LMU's `Connection.cpp`/`HotPacket.cpp` transport, and LMU's MAS header differs
from rF2's.

### Our own server (chosen)
`lmu_server.py` implements what the client expects directly. It currently gets
a real LMU client through the HTTP join and the transport handshake to
"Sent JoinRequest to server". See PROTOCOL.md.

### Binary Patching
**Extremely complex.** Would need to reconstruct the entire server initialization
path that the dedicated binary has, patch it into the retail exe's startup
sequence before Game::Init, and disable the IsDedicated assertion. Not viable
without the dedicated server source or binary for reference.

---

## Key Function Reference

| Address | Name | Role |
|---------|------|------|
| `140cdb630` | main() | Entry point, parses +host/+multiplayer |
| `140b68570` | ParseHostArgs | Handles +host, +gamename, +maxplayers, +port |
| `140cd36b0` | Game::Init | Mode branching (Single vs Multi), calls Client Init |
| `140b64180` | ClientModeInit | Always sets mode to Client (2) |
| `140cd0bd0` | GameLoopTick | Main game loop, asserts !IsDedicated |
| `140b6c930` | NetCommJoin | JOIN function, sets mode=3 (→2) |
| `140b623f0` | NetCommTick | Network state machine (handles 3→2, cleanup→0) |
| `1402a37a0` | IsServer | Returns mode==1 |
| `140cda230` | SetMode | Generic mode setter (never called with 1) |
| `140df0f10` | ServerSessionAdv | Auto-advance server session (mode==1 handler) |
| `14021fa90` | ServerSocketSetup | WSASocket + bind + listen + accept |
| `140c41a90` | WriteServerUnlock | RC4-encrypts & writes ServerUnlock.bin |
| `1403d1ca0` | BuildTelemetry | Reports "build":"client" or "dedicated" |
