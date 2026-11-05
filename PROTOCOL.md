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
| `08` | S->C | sync response | `08` + same id u32 + server time u64 (UDP); clock units still unverified |
| `09` | S->C | session description | `09` + u8 + **exactly 0x69 bytes** (see below) |
| `0a` | C->S | NetComm event | event (see stage 4) |
| `0c` | S->C | NetComm event | event |
| `0d` | S->C | NetComm event from node | node id u32 + event |
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

Event = `[time u32][header u32][body]`; header bits: 0-7 slot (`ff` none),
8-16 body size, 17-25 type (top 3 bits = category), 26-29 session, 30-31 flags.

* `0xc7` SelfAnnounce (C->S): SteamID u64, name char[32], auth char[65], nationality.
* `0xe7` Welcome (S->C): no body; client sets its player info and re-sends `02` with auth.
* `0xe3` slot assignment, `0xc4`/`0xe1`/`0xe5`... vehicles, sessions (not implemented yet).

* `0xf8` (C->S, no body) sent once in-session; `0xe9` (C->S, 1-byte body) every 250 ms.

## Entering the session

After `JOIN_JOINED_SERVER` the game's UI (not the netcode) moves on with two local
REST calls, which `--join` replicates:

1. `POST /rest/garage/SetCurrentVehicle` body `{"id": "<car id from /rest/race/car>"}`
2. `POST /navigation/action/NAV_RACE_MULTIPLAYER` body `null`

## Status

A real client joins, loads the track, enters `DYN` ("Entering DYN"), processes the
Welcome, syncs its clock and the session goes green (`GPHASE_GREEN`). Not done yet:
server time base for sync, car requests / slot assignment, a second client and
position relay, session progression, results.
