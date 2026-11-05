# LMU Local Server Toolkit

Host and join Le Mans Ultimate races on your own PC or LAN instead of renting a RaceControl server.

> **Status: experimental.** Joining by IP through the game's local API uses the same call the game's own UI makes. Hosting relies on rFactor 2 engine features found in the LMU executable. Whether the retail client starts a server with these launch arguments **has not been confirmed yet**. If you try it, please open an issue with what happened and attach your `UserData\Log` files.

Not affiliated with or endorsed by Motorsport Games, Studio 397 or RaceControl. Use at your own risk and respect the game's terms of service. This tool does not modify game files (except the `Multiplayer.JSON` settings file, which it backs up first) and does not touch Easy Anti-Cheat.

## Features

- **Host**: edits your profile's `Multiplayer.JSON` (server name, passwords, max clients, ports) and starts the game **through Steam** (`steam.exe -applaunch 2399420 ...`) with hosting arguments, so Steam, anti-cheat and your own Steam launch options all apply. It also shows whether the server is actually accepting connections.
- **Attaches to an already-running game**: reads the game's launch arguments, state and anti-cheat status from its local API. If the game was started with `+host` (for example from your Steam launch options), the toolkit treats it as the server, whoever launched it. Hosting can only be turned on at startup, so **Restart game as host** closes the game cleanly and relaunches it through Steam.
- **Live Game**: chat window and send box for the running game, so you can use admin commands such as `/admin <password>`.
- **Share / Ports**: generates the connect line for friends, looks up your public IP on request, and adds Windows Firewall rules in one click.
- **Join**: tells a running LMU to connect to `IP:port` through its local REST API (`127.0.0.1:6397`).
- **LAN Browser**: toolkit instances announce running servers to each other on the local network.
- Finds the game automatically through Steam's library folders. Settings are remembered in `toolkit_settings.json`.

## Requirements

- Windows, Le Mans Ultimate (Steam)
- Python 3.8+ (tkinter is included with the standard Windows installer)

## Usage

1. Launch LMU once normally so it creates `UserData\<profile>\Multiplayer.JSON`.
2. Run `start-server.bat` (or `python lmu_server_toolkit.py`).
3. **Host tab**: set the name, passwords and ports, then click **Start server**. If the game is already open, the toolkit asks before closing it and restarting it as a host. Steam may ask you to confirm the launch arguments the first time. The status line turns green when the server is accepting connections on the HTTP port.
4. **Share / Ports tab**: click **Add Windows Firewall rules**, then send friends the text shown.
5. Friends start LMU and use the **Join** tab, `connect-to-server.bat HOST PORT [PASSWORD]`, or the Steam launch option `+connect HOST:PORT`.

## Ports

Players connect to the **HTTP Server Port**. In the engine's own words it is "used for file sharing and for clients to get the initial server config (including the simulation port)". The **Simulation Port** carries the race itself and uses a range from that port up to port + max clients.

| What | Protocol | Default (rFactor 2) |
|------|----------|---------------------|
| HTTP Server Port (join here) | TCP + UDP | 64297 |
| Simulation Port range | TCP + UDP | 54297 to 54297 + max clients |

The toolkit reads the real values from your `Multiplayer.JSON` when it exists.

- **Same network**: firewall rules are enough.
- **Over the internet**: forward the same ports on your router to the host PC's LAN IP. Alternatively, put everyone on a virtual LAN such as [Tailscale](https://tailscale.com) or [ZeroTier](https://www.zerotier.com) and share the host's virtual IP. That way no router changes are needed.

## What we found in the game

LMU is built on the rFactor 2 engine, and the retail executable still contains the engine's multiplayer server code:

- Launch arguments: `+host`, `+port`, `+maxplayers N`, `+password X`, `+connect IP:PORT`, `+profile=NAME`, `+path=`.
- `Multiplayer.JSON` sections **Multiplayer General Options** and **Multiplayer Server Options**, including `Default Game Name`, `Join Password`, `Admin Password`, `Max Clients`, `Max Vehicles`, `HTTP Server Port`, `Simulation Port`, `Use RUDP`, `Pause While Zero Players`, `Race Rejoin`, voting, allowed driving aids, tyre rules, and `Protected Server` (whether the server enforces Easy Anti-Cheat).
- In-game admin chat commands: `/admin <password>` and `/apwd <new_password>`.
- Dedicated-mode code paths (`IsDedicated`, `"build":"dedicated"`). These suggest RaceControl's cloud servers run a dedicated build of the same engine, but no dedicated server download exists on Steam.

The game UI talks to a local REST API on port 6397. The multiplayer endpoints used here:

| Endpoint | Purpose |
|----------|---------|
| `GET /rest/multiplayer/join?host=&port=&password=&authentication=` | Join a server |
| `GET /rest/multiplayer/join/state` | Join progress |
| `GET /rest/multiplayer/steam/status` | Used to check the game is running |
| `GET /rest/options/commandline` | Launch arguments (used to detect `+host`) |
| `GET /rest/sessions/GetGameState` | Current state, e.g. `Setup` at the menu |
| `GET /rest/profile/eacActive` | Whether anti-cheat is active |
| `GET` / `POST /rest/chat/` | Read chat / send a chat line (plain text body) |

The full list of 183 endpoints is published by the game itself at `http://127.0.0.1:6397/swagger-schema.json` while it runs. None of them starts hosting, which is why hosting needs a restart with `+host`.

Launch arguments to experiment with go in the **Launch arguments** field on the Host tab (default `+host`). The game's log confirms `+host` is recognised: it prints `NetComm activated autohost mode`.

**Avoid `key=value` arguments such as `+profile=player`.** When the game is launched through Steam, Steam wraps them in quotes. The game then looks for a profile literally named `player"`, fails with `Could not find player profile and failed to create one! FATAL`, and crashes. The toolkit therefore always uses the game's default profile. Launching the executable directly instead of through Steam doesn't work either: Steam isn't initialised and the game exits with fatal error 11.

## Files

| File | Purpose |
|------|---------|
| `lmu_server_toolkit.py` | The toolkit (GUI) |
| `start-server.bat` | Opens the toolkit |
| `connect-to-server.bat` | Command-line join: `connect-to-server.bat HOST [PORT] [PASSWORD]` |

## License

MIT
