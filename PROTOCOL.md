# LMU multiplayer protocol (as implemented by `lmu_server.py`)

Reverse engineered from the LMU 1.4200 client (`Le Mans Ultimate.exe`, build
`RF2-LM100`). Function addresses refer to that build and will move on updates;
the strings next to them are the stable way to find the code again.

## How to re-check after a game update

1. Launch the game through Steam with `+trace=3 +traceFlush`.
2. Run `python lmu_server.py --join`.
3. Read `UserData\Log\trace_*.txt` (open with shared read; Windows reports 0 bytes
   while the game holds it open). `NetHTTP.cpp`, `Connection.cpp`, `HotPacket.cpp`
   and `RestMultiplayer` lines say exactly which step failed.

If the game gets stuck in `JOIN_JOINING_SERVER` / `JOIN_SHUTTING_DOWN` it refuses
new joins (HTTP 400); restart it.

## Stage 1 - HTTP (server `httpPort`, the port given to `/rest/multiplayer/join`)

Client user agent: `LMUHTTPServiceCli`. Join states: `JOIN_GETTING_DETAILS ->
JOIN_GOT_DETAILS -> JOIN_CHECKING_PASSWORD -> JOIN_VERIFYING_CONTENT ->
JOIN_JOIN_SERVER -> JOIN_JOINING_SERVER -> JOIN_JOINED_SERVER`.

### `GET /SessionInfo` (parser `FUN_140ad3010`)

Required (nlohmann `at()`, missing key = join failure): `name`, `guid`
(`{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`, braces required), `lanIp`, `wanIp`,
`httpPort`, `simulationPort`, `maxPlayers`, `players` (array of `{name, countryCode}`),
`playerCount`, `driverCount`, `passwordProtected`, `build`.

Optional: `useRUDP`, `connectionTimeout`, `official`, `mod` (`{location, layout}`),
`rules`, `paintBlobIdSet`, `details`.

`details` is optional to the parser but required in practice: without it the client
starts in session 15 ("none") and crashes in `Driver::InitPitStrategy` (jump table
indexed by session). When present every key is read with `at()` (`FUN_140ad4740`):
`game_name` (string), `aids`, `ping`, `packed_flags`, `stream`, `special_pack`,
`damage_mult`, `laps_left`, `time_left`, `finish_type`, `team_conf` (numbers),
`fixed_setups`, `fixed_upgrades`, `tire_warmers`, `is_closed`, `in_development`,
`is_anonymous` (JSON booleans), `session` (1-4 practice, 5-8 qualifying, 9 warmup,
10-13 race), `ai_vehicles` (array; entries have `ai_drivers` [{`steamId`,`nickname`}],
`car_upgrades` [16 numbers], `paintBlobID`, `vehicle`).

`build` = `Core\Version.txt` first line without the dot (`1.4200` -> `14200`);
mismatch fails with "Client - Server version mis-match".

### `GET /Rules` (checker `FUN_1402c5e40`)

* `SessionInfo.rules` must equal
  `sha256("Driver " + <exact /Rules body> + ", class GTE").hexdigest()`
  (format string `"Driver {}, class {}"`, constant `"GTE"`), else "Invalid server rules".
* Body is JSON with three top-level keys:
  * `Components`: the `Signature=` of **every installed version** of every car and
    track (`Installed\Vehicles|Locations\<name>\<version>\*.mft`, Flags has `0x1000`,
    not `0x2`). The count must equal the client's own count, else "Local content
    did not match expected server content" with a "Content missing" list in the trace.
  * `Rules`: the game's rule set (the `Rules` object of `default.rules.json`). Missing
    -> `game.cpp` "Rules file did not contain game rules! FATAL!!!" (crash).
  * `Bop`: the Balance of Performance object for the session track (one JSON per
    layout, e.g. `SpaWEC.json`). Missing -> the client tries to build it from local
    data while in multiplayer and throws `std::bad_optional_access` (crash).
  The server reads `Rules`/`Bop` from `data/` (git-ignored). They come from the
  game's own encrypted archives and are extracted from your install locally; neither
  the files nor the extraction tool are published in this repo.

### `POST /Access`

Body `{ "Password" : "<pw>" }`, only when `passwordProtected`.

## Stage 2 - transport handshake (`simulationPort`, `Networking/Connection.cpp`)

Message ids are little-endian `0x5397NNff`.

| Dir | Channel | Message | Bytes |
|-----|---------|---------|-------|
| C->S | TCP | TcpRequest | `ff 00 97 53` |
| S->C | TCP | TcpResponse | `ff 01 97 53` + accept u8 (non-zero) + server UDP port u16 + client id u64 (15 bytes) |
| C->S | UDP | UdpClientVerification | `[client id u64][len u16 = 4] ff 02 97 53` |
| S->C | UDP | UdpServerVerification | same framing, `ff 03 97 53` |
| C->S | TCP | TcpConfirmation | `ff 04 97 53` |

Trace on success: "Finished sending TcpConfirmation and established connection."
then `HotPacket.cpp` "Connected with server." / "Sent JoinRequest to server."

## Stage 3 - HotPacket system messages (`Networking/HotPacket.cpp`)

TCP frames are `[len u16][payload]`; UDP frames are `[client id u64][len u16][payload]`.
The first payload byte is the message type (client dispatcher `FUN_140a72470`):

| Type | Dir | Message | Layout |
|------|-----|---------|--------|
| `00` | C->S | JoinRequest | `00` + spectator u8 + player GUID (16) |
| `01` | S->C | JoinResponse | `01` + accepted u8 + node id u32 + u8 (passed to OnJoin) |
| `02` | C->S | ClientInfoUpdateRequest | `02` + auth (16) + node info |
| `03` | S->C | node info | `03` + node id u32 + info (server echoes 02 to every node) |
| `04` | S->C | node left | `04` + node id u32 + unexpected u8 |
| `05`/`06` | both | ping / pong | 8-byte timestamp; answer `05` with `06` + same bytes (UDP) |
| `07` | C->S | sync request | `07` + id u32 (UDP) |
| `08` | S->C | sync response | `08` + same id u32 + server session time as a **double in seconds** (UDP) |
| `09` | S->C | session description | `09` + change counter u8 + **exactly 0x69 bytes** (see below); resent on every session change |
| `0a` | C->S | NetComm event | event (see stage 4) |
| `0c` | S->C | NetComm event | event |
| `0d` | S->C | NetComm event from node | node id u32 + event (how the server relays one player's events to the others) |
| `0e` | both | keepalive | |

Server order on JoinRequest (from `Server::handleJoinRequest`): `09`, then `01`,
then `03` for each other node.

### Session description (0x69 bytes, defaults from `FUN_1402c1450`)

`0x00` name char[28], `0x1c` scene char[32] (track `sceneDesc` from
`/rest/sessions/getTracksAll`, e.g. `SPAWEC`), `0x44` u32 `0x04400000`,
`0x48` u32 `0x60640000` with max players in bits 7-15, `0x4c` i16 `0x0e`,
`0x52` u16 `0x0100`, `0x65` u32 current session (default `0x0f` = none); the rest are packed race settings
(applied and range-checked in `FUN_140b7f770`). If the size is not exactly 0x69 the
client silently waits in `JOIN_JOINING_SERVER` forever.

## Stage 4 - NetComm events (`Source/NetCommClient.cpp`)

Event = `[time u32][header u32][body]`. Time is in **1/4096 s** of session time. Header bits:
0-7 slot (`ff` none), 8-16 body size, 17-25 type (top 3 bits = category), 26-29 session,
30-31 flags. The client drops events whose session does not match its current session.
Category 3 (`0xc0`-`0xff`) is control traffic the server answers; categories 0-2 are game
state the server relays to the other players (`0d` + sender node id + the raw event) and
caches per type, so late joiners get the latest copy.

### Joining a car

| Type | Dir | Name | Body |
|------|-----|------|------|
| `0xc7` | C->S | SelfAnnounce | SteamID u64, name char[32], auth char[65], nationality u8 at 0x69 |
| `0xe7` | S->C | Welcome | none; client sets its player info and re-sends `02` with auth |
| `0xc2` | S->C | Ready | slot u8 (`ff` = none yet); the client may now ask for a car |
| `0xf8` | C->S | SessionReady ("Player Ready") | none; sent after every (re)load of a session |
| `0xf9` | S->C | SessionAck | none; clears the client's "re-apply session description" flag (+0x140). Without it the client restarts the session in a loop after every `09` |
| `0xe0` | C->S | VehicleRequest | vehicle file char[32] @0, paint blob @0x20, number @0x42, team @0x46, upgrades[16] @0x60 |
| `0xe3` | S->C | SlotAssignment | slot u8 |
| `0xe4` | S->C | DriverInfo (header slot) | driver index u8, flags u8, nationality u8, name (size-3 bytes) |
| `0xe1` | S->C | SlotVehicle (header slot) | parking u8, vehicle[32], paint blob[64], upgrades[16], number[5], team |
| `0xe5` | S->C | ActiveDriver (header slot) | node id u32, driver index u8 |
| `0xf3` | S->C | VehicleResponse | approved u8 |
| `0xf4` | C->S | TakeControl | u8 |
| `0xf5` | S->C | TakeControlResponse | approved u8; then the client logs "Finished taking control of vehicle in slot N" |
| `0xe2` | S->C | RemoveVehicle | slot u8 (sent to everyone when a player leaves, with `04`) |

A freshly started game sends `0xe0` by itself right after `0xf8` (the car last picked in its
menus). A game that already joined once in this run waits for the player to press Drive.

### While driving

The client streams `0x08` (cat 0, 53 bytes, position/velocity, ~50 Hz) plus `0x33`, `0x59`,
`0x8f`, `0x90`, `0xa2` and `0xe9` (cat 3, every 250 ms). Relaying the category 0-2 events with
`0d` is enough for the other clients to draw the car ("Making vehicle in slot N visible
again"). `--ghost N` adds a fake player that replays your own events N seconds later in
another slot, which tests this with one PC.

### Sessions

The server owns the session clock (`08` replies) and the session number (`09` offset 0x65,
event headers). To change session it sends a new `09` with the counter incremented to every
node, then `0xf9`. The client logs `Changing session from Practice1 to Qualify` /
`SessionName from 1 to 5`, reloads, sends `0xf8`, and gets `0xf9` back. Session numbers:
1-4 practice, 5-8 qualifying, 9 warmup, 10-13 race. The client keeps its own results file
per session (`[Results] Post`); the server does not collect results yet.

## Server-side HTTP used during a session

| Request | Answer |
|---------|--------|
| `GET /mp_vehicle/{session}/{slot}` | 200 `{"vehicle": {}, "compressed_setup": null, "start_from_garage": true, "lock_setup_in_garage": false}` for a fresh car. A 404 makes the client log "Becoming spectator". |
| `PUT /mp_vehicle*/...` | client uploads its car state/setup; store it and return it on later `GET`s |
| `GET /teams/list/{n}` | polled every 5 s; `{"coherenceId": 0, "teams": {}, "drivers": {}}` (`coherenceId` must be a number) |

## Entering the session

After `JOIN_JOINED_SERVER` the game's UI (not the netcode) moves on with a local REST call,
which `--join` and the toolkit replicate:

    POST /navigation/action/NAV_RACE_MULTIPLAYER   body: null

`/rest/garage/SetCurrentVehicle` is ignored for multiplayer; the game uses the car last
picked in its menus.

## Disconnecting

Close the players' TCP connections first and keep the HTTP port answering for a few seconds:
a client that loses the HTTP server while polling `/teams/list` crashes. The server does this
on Ctrl+C, `--run-seconds` and the toolkit's Stop button.

## Content

`Components` lists every installed car and track version. Unowned DLC is installed too, so all
installs of the same game build have the same list; a mismatch only happens across builds,
and the `build` check already catches that.

## Status

Working with the retail client: join, track load (any track that has a BoP file), car
request and slot assignment, taking control, clock sync, relaying cars between players
(tested with `--ghost`), session progression P -> Q -> R (then back to the first session),
clean disconnect, internet hosting through UPnP.

Not done or not verified: a second real player (needs a second PC), the race start
procedure (the race reaches `GPHASE_FORMATION`; green flag and finish not checked),
server-side results, chat, and driver names (other players show the car's default driver
name, because `0xe4` does not set the displayed name).

Known crash: a client that has no car (never pressed Drive) crashed once when a race session
began, in a test with 30-second sessions that changed while it was still loading. With a car
and 1-minute sessions, P -> R -> P -> R ran cleanly. Use realistic session lengths.
