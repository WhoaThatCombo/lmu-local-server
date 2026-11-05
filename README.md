# LMU Local Server

Host and join Le Mans Ultimate races on your own PC, over your LAN or the internet, instead of renting a RaceControl server.

The retail game cannot host (see [REVERSE_ENGINEERING_FINDINGS.md](REVERSE_ENGINEERING_FINDINGS.md)), so this project includes its own server, `lmu_server.py`. It speaks the protocol the retail client expects, which was reverse engineered from the client and is documented in [PROTOCOL.md](PROTOCOL.md). Players use the normal, unmodified game. Joining uses the same local API call the game's own server browser makes.

> **Status: experimental.** A real client joins, loads the track, gets a car, drives, and moves through practice, qualifying and race. Cars are relayed between players, which was tested with a replayed "ghost" player. Not tested yet: two real players on two PCs, and the race start (green flag) and finish. See [Status in PROTOCOL.md](PROTOCOL.md#status).

Not affiliated with or endorsed by Motorsport Games, Studio 397 or RaceControl. Use at your own risk and respect the game's terms of service. Nothing here modifies game files or touches Easy Anti-Cheat.

## Requirements

- Windows, Le Mans Ultimate (Steam), and the same game version for everyone
- Python 3.8+ (tkinter comes with the standard Windows installer)
- **Host only:** the game's rule set and Balance of Performance files in `data/` (`data/default.rules.json`, `data/bop/<Track>.json`). They come from the game's own archives in your install. They are not distributed here, and neither is a tool to extract them.

## Hosting

1. Run `start-server.bat` (or `python lmu_server_toolkit.py`).
2. **Host tab**: set the server name, an optional password, the track, and the sessions (`P:30,Q:15,R:30` means 30 min practice, 15 min qualifying, 30 min race; the schedule repeats after the last one). Leave **Open the ports on my router automatically (UPnP)** ticked for internet play.
3. Click **Start server**. The status line shows the address players join, for example `203.0.113.5:64298`.
4. To drive yourself: start LMU (**Start LMU**), pick your car in the game's menus, wait at the main menu, then click **Join my server**.
5. **Share / Ports tab**: copy the text and send it to your friends.

**Stop server** sends everyone back to the menu and closes the router ports again.

Command line, without the toolkit:

```
python lmu_server.py --name "My race" --scene MONZAWEC --sessions P:20,Q:10,R:30 --upnp
```

`python lmu_server.py --help` lists every option (ports, password, max players, `--join` to make the local game join, and `--ghost SECONDS` to test the car relay alone).

## Joining

Start LMU, pick your car in the menus, stay at the main menu, then do one of these:

- the toolkit's **Join** tab (or **LAN Browser** for servers on your network)
- `connect-to-server.bat HOST [PORT] [PASSWORD]`

Both tell the game to join through its local API on port 6397, then enter the session the way the game's menu does. A freshly started game asks for its car automatically. Otherwise press **Drive** in the garage.

## Ports

Only the host needs open ports.

| What | Protocol | Default |
|------|----------|---------|
| HTTP port (players join here) | TCP | 64298 |
| Simulation port (the race) | TCP + UDP | 54298 |

- **UPnP** (the default in the toolkit, `--upnp` on the command line): the server asks the router to forward these three mappings while it runs and removes them when it stops. Discovery listens on port 1900 because Windows Firewall drops UPnP replies sent to other ports. The router found is cached in `upnp_gateway.txt`. Run `python upnp.py` to check your router. If the router's own internet address is not public (double NAT or carrier-grade NAT), the server says so; port mapping can't help there.
- **Without UPnP**: forward the ports above on your router to the host PC's LAN IP, or put everyone on a virtual LAN such as [Tailscale](https://tailscale.com) or [ZeroTier](https://www.zerotier.com) and share the host's virtual IP.
- **Windows Firewall**: Windows normally asks to allow Python the first time the server listens. **Add Windows Firewall rules** on the Share / Ports tab adds port rules instead.

## Files

| File | Purpose |
|------|---------|
| `lmu_server.py` | The server |
| `upnp.py` | Router port mapping (standard library only) |
| `lmu_server_toolkit.py` | GUI to host and join; also `--join HOST [PORT] [PASSWORD]` |
| `lmu_protocol_probe.py` | Logging, listeners and the local-game join helper used by the server |
| `start-server.bat` | Opens the toolkit |
| `connect-to-server.bat` | Command-line join |
| `PROTOCOL.md` | The client/server protocol |
| `REVERSE_ENGINEERING_FINDINGS.md` | Why the retail game cannot host |

## After a game update

Function addresses in PROTOCOL.md will move, but the protocol rarely changes much. Launch the game through Steam with `+trace=3 +traceFlush`, run `python lmu_server.py --join`, and read `UserData\Log\trace_*.txt`. The `NetHTTP`, `Connection`, `HotPacket` and `NetCommClient` lines show which step fails. The `build` value is read from `Core\Version.txt`, so version bumps alone need no change.

## License

MIT
