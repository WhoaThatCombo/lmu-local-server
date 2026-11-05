"""Standalone LMU server (work in progress).

Implements the HTTP join handshake the LMU client expects from a server
(SessionInfo -> Rules -> Access) and logs the simulation-port traffic that
follows, so the sim protocol can be implemented next.

Usage:
  python lmu_server.py [--game-dir PATH] [--http 64298] [--sim 54298] [--join]
"""
import argparse
import hashlib
import json
import socket
import struct
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lmu_protocol_probe as probe

RULES_SALT_FMT = "Driver {}, class GTE"
MFT_INSTALLED = 0x1000
MFT_EXCLUDED = 0x2
COMPONENT_TYPES = {"Locations": 1, "Vehicles": 2}


def read_mft(path):
    fields = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields.setdefault(k.strip(), v.strip())
    return fields


def version_key(name):
    return [int(p) if p.isdigit() else p for p in name.replace("-", ".").split(".")]


def scan_components(game_dir, all_versions=False):
    """Signatures of installed cars and tracks, as the client's Components check expects."""
    comps = []
    for folder in COMPONENT_TYPES:
        root = Path(game_dir) / "Installed" / folder
        if not root.is_dir():
            continue
        for comp in sorted(p for p in root.iterdir() if p.is_dir()):
            versions = sorted((v for v in comp.iterdir() if v.is_dir()), key=lambda v: version_key(v.name))
            for ver in (versions if all_versions else versions[-1:]):
                mft = next(ver.glob("*.mft"), None)
                if not mft:
                    continue
                f = read_mft(mft)
                flags = int(f.get("Flags", "0") or 0)
                if not flags & MFT_INSTALLED or flags & MFT_EXCLUDED:
                    continue
                comps.append({"name": f.get("Name", comp.name), "version": f.get("Version", ver.name),
                              "type": int(f.get("Type", "0") or 0), "signature": f.get("Signature", "")})
    return comps


def find_bop_file(bop_dir, scene):
    """Per-track Balance of Performance JSON (BoP.mas names, e.g. SPAWEC -> SpaWEC.json), else default.json."""
    files = {p.stem.lower(): p for p in Path(bop_dir).glob("*.json")}
    return files.get(scene.lower()) or files["default"]


def game_build(game_dir):
    """Version.txt "1.4200" -> 14200, the number the client compares against."""
    first = (Path(game_dir) / "Core" / "Version.txt").read_text().split()[0]
    return int(first.replace(".", ""))


class Server:
    def __init__(self, a):
        self.a = a
        self.components = scan_components(a.game_dir, a.all_versions)
        rules = json.loads(Path(a.rules_file).read_text(encoding="utf-8"))["Rules"]
        self.bop_file = find_bop_file(a.bop_dir, a.scene)
        bop = json.loads(self.bop_file.read_text(encoding="utf-8"))
        self.rules_body = json.dumps({"Rules": rules, "Bop": bop,
                                      "Components": [c["signature"] for c in self.components]})
        self.rules_hash = hashlib.sha256(RULES_SALT_FMT.format(self.rules_body).encode()).hexdigest()
        self.guid = "{%s}" % str(uuid.uuid5(uuid.NAMESPACE_DNS, f"lmu-local-{a.name}")).upper()
        self.build = a.build if a.build is not None else game_build(a.game_dir)

    def session_info(self):
        return {
            "name": self.a.name,
            "guid": self.guid,
            "lanIp": self.a.lan_ip,
            "wanIp": self.a.wan_ip,
            "httpPort": self.a.http,
            "simulationPort": self.a.sim,
            "useRUDP": False,
            "maxPlayers": self.a.max_players,
            "players": [],
            "playerCount": 0,
            "driverCount": 0,
            "passwordProtected": bool(self.a.password),
            "official": False,
            "build": self.build,
            "mod": {"location": self.a.location, "layout": self.a.layout},
            "rules": self.rules_hash,
            # Optional for the parser, but without it the client starts in session 15 ("none")
            # and crashes in Driver::InitPitStrategy (FUN_140ad4740 reads every key with at()).
            "details": {
                "game_name": self.a.name,
                "aids": 0,
                "ping": 0,
                "packed_flags": 0,
                "stream": 0,
                "special_pack": 0,
                "damage_mult": 100,
                "laps_left": 0,
                "time_left": 3600,
                "fixed_setups": False,
                "fixed_upgrades": False,
                "tire_warmers": True,
                "finish_type": 0,
                "ai_vehicles": [],
                "is_closed": False,
                "in_development": False,
                "is_anonymous": False,
                "session": SESSION_PRACTICE1,
                "team_conf": 0,
            },
        }


MSG_TCP_REQUEST = 0x539700FF
MSG_TCP_RESPONSE = 0x539701FF
MSG_UDP_CLIENT_VERIFY = 0x539702FF
MSG_UDP_SERVER_VERIFY = 0x539703FF
MSG_TCP_CONFIRM = 0x539704FF
MAGIC = lambda v: struct.pack("<I", v)

# HotPacket system message types (first payload byte), from the client dispatcher FUN_140a72470.
SYS_JOIN_REQUEST = 0x00
SYS_JOIN_RESPONSE = 0x01
SYS_NODE_INFO = 0x03
SYS_NODE_LEFT = 0x04
SYS_PING = 0x05
SYS_PONG = 0x06
SYS_SYNC_REQUEST = 0x07
SYS_SYNC_RESPONSE = 0x08
SYS_SESSION_UPDATE = 0x09
SYS_APP = 0x0C
SYS_APP_FROM_NODE = 0x0D
SYS_KEEPALIVE = 0x0E
SYS_CLIENT_INFO_REQUEST = 0x02
SYS_APP_TO_SERVER = 0x0A

# NetComm events ride in SYS_APP_TO_SERVER / SYS_APP frames as [u32 time][u32 header][body].
# header: bits 0-7 slot (0xff none), 8-16 body size, 17-25 type, 26-29 session, 30-31 flags.
EV_SELF_ANNOUNCE = 0xC7
EV_WELCOME = 0xE7
EVENT_NAMES = {EV_SELF_ANNOUNCE: "SelfAnnounce", EV_WELCOME: "Welcome", 0xE3: "SlotAssignment"}


SESSION_DESC_SIZE = 0x69


SESSION_PRACTICE1 = 1  # 1-4 practice, 5-8 qualifying, 9 warmup, 10-13 race; 15 = none (crashes the client)


def build_session_desc(name, scene, max_players, session=SESSION_PRACTICE1):
    """The 105-byte session description sent in SYS_SESSION_UPDATE (defaults from FUN_1402c1450)."""
    d = bytearray(SESSION_DESC_SIZE)
    d[0x00:0x1B] = name.encode("ascii", "replace")[:27].ljust(27, b"\0")
    d[0x1C:0x3B] = scene.encode("ascii", "replace")[:31].ljust(31, b"\0")
    struct.pack_into("<I", d, 0x44, 0x04400000)
    struct.pack_into("<I", d, 0x48, 0x60640000 | ((max_players & 0x1FF) << 7))
    struct.pack_into("<h", d, 0x4C, 0x0E)
    struct.pack_into("<H", d, 0x52, 0x0100)
    struct.pack_into("<I", d, 0x65, session)
    return bytes(d)


def parse_event(data):
    time_, header = struct.unpack_from("<II", data)
    size = (header >> 8) & 0x1FF
    return {"time": time_, "slot": header & 0xFF, "size": size, "type": (header >> 17) & 0x1FF,
            "session": (header >> 26) & 0xF, "flags": header >> 30, "body": data[8:8 + size]}


def build_event(ev_type, body=b"", slot=0xFF, session=0, flags=3, time_=0):
    header = slot | (len(body) << 8) | (ev_type << 17) | (session << 26) | (flags << 30)
    return struct.pack("<II", time_, header) + body


class SimServer:
    """Simulation-port transport handshake (Networking/Connection.cpp), then raw logging."""

    def __init__(self, port, idle, session_desc):
        self.port = port
        self.idle = idle
        self.session_desc = session_desc
        self.next_id = 0x1000
        self.next_node = 1
        self.start_ns = time.monotonic_ns()
        self.lock = threading.Lock()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("0.0.0.0", port))

    def start(self):
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._tcp_loop, daemon=True).start()
        probe.log("sim", f":{self.port}", "listening (tcp+udp)")

    def _udp_send(self, addr, client_id, payload):
        self.udp.sendto(struct.pack("<QH", client_id, len(payload)) + payload, addr)

    def _udp_loop(self):
        pings_logged = 0
        while True:
            data, addr = self.udp.recvfrom(65536)
            peer = f"{addr[0]}:{addr[1]}"
            if len(data) < 10:
                probe.log("udp", peer, f"short datagram {len(data)} bytes", data)
                continue
            client_id, length = struct.unpack_from("<QH", data)
            payload = data[10:10 + length]
            if payload == MAGIC(MSG_UDP_CLIENT_VERIFY):
                self._udp_send(addr, client_id, MAGIC(MSG_UDP_SERVER_VERIFY))
                probe.log("udp", peer, f"UdpClientVerification from {client_id:#x}; replied")
            elif payload[:1] == bytes([SYS_SYNC_REQUEST]) and len(payload) >= 5:
                (sync_id,) = struct.unpack_from("<I", payload, 1)
                server_time = time.monotonic_ns() - self.start_ns
                self._udp_send(addr, client_id, struct.pack("<BIQ", SYS_SYNC_RESPONSE, sync_id, server_time))
                if sync_id <= 2:
                    probe.log("udp", peer, f"sync request {sync_id}; replied server time {server_time / 1e9:.3f}s")
            elif payload[:1] == bytes([SYS_PING]) and len(payload) >= 9:
                self._udp_send(addr, client_id, bytes([SYS_PONG]) + payload[1:9])
                if pings_logged < 2:
                    pings_logged += 1
                    probe.log("udp", peer, "ping; replied pong")
            else:
                probe.log("udp", peer, f"client {client_id:#x} type {payload[:1].hex()} {len(payload)} bytes", payload)

    def _tcp_loop(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.listen(16)
        while True:
            conn, addr = s.accept()
            threading.Thread(target=self._tcp_session, args=(conn, addr), daemon=True).start()

    def _tcp_session(self, conn, addr):
        peer = f"{addr[0]}:{addr[1]}"
        conn.settimeout(self.idle)
        buf = b""
        stage = "request"
        state = {}
        with self.lock:
            client_id = self.next_id
            self.next_id += 1
        probe.log("tcp", peer, f"connect (client id {client_id:#x})")
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    probe.log("tcp", peer, f"closed by client at stage {stage}")
                    return
                probe.log("tcp", peer, f"recv {len(data)} bytes at stage {stage}", data)
                buf += data
                if stage == "request" and len(buf) >= 4:
                    if struct.unpack_from("<I", buf)[0] != MSG_TCP_REQUEST:
                        probe.log("tcp", peer, "unexpected first message")
                    buf = buf[4:]
                    resp = MAGIC(MSG_TCP_RESPONSE) + struct.pack("<BHQ", 1, self.port, client_id)
                    conn.sendall(resp)
                    probe.log("tcp", peer, "-> TcpResponse", resp)
                    stage = "confirm"
                if stage == "confirm" and len(buf) >= 4:
                    if struct.unpack_from("<I", buf)[0] == MSG_TCP_CONFIRM:
                        probe.log("tcp", peer, "TcpConfirmation received; transport established")
                        buf = buf[4:]
                        stage = "game"
                if stage == "game":
                    while len(buf) >= 2:
                        (length,) = struct.unpack_from("<H", buf)
                        if len(buf) < 2 + length:
                            break
                        self._on_frame(conn, peer, buf[2:2 + length], state)
                        buf = buf[2 + length:]
        except socket.timeout:
            probe.log("tcp", peer, f"idle {self.idle}s at stage {stage}; closing")
        except ConnectionError as e:
            probe.log("tcp", peer, f"connection lost at stage {stage}: {e}")
        except OSError as e:
            probe.log("tcp", peer, f"error {e} at stage {stage}")
        finally:
            conn.close()

    def _send_frame(self, conn, peer, payload, what):
        conn.sendall(struct.pack("<H", len(payload)) + payload)
        probe.log("tcp", peer, f"-> {what}", payload[:256])

    def _on_frame(self, conn, peer, payload, state):
        kind = payload[0] if payload else None
        if kind == SYS_KEEPALIVE:
            conn.sendall(struct.pack("<HB", 1, SYS_KEEPALIVE))
            return
        if kind == SYS_JOIN_REQUEST and len(payload) >= 18:
            role = "spectator" if payload[1] else "driver"
            guid = uuid.UUID(bytes_le=bytes(payload[2:18]))
            with self.lock:
                node_id = self.next_node
                self.next_node += 1
            probe.log("tcp", peer, f"JoinRequest as {role}, player guid {{{str(guid).upper()}}}; assigning node {node_id}")
            self._send_frame(conn, peer, bytes([SYS_SESSION_UPDATE, 0]) + self.session_desc, "session description")
            self._send_frame(conn, peer, struct.pack("<BBIB", SYS_JOIN_RESPONSE, 1, node_id, 0), "JoinResponse accepted")
            state["node_id"] = node_id
            return
        if kind == SYS_CLIENT_INFO_REQUEST and len(payload) >= 17:
            info = payload[17:]
            probe.log("tcp", peer, f"ClientInfoUpdateRequest auth={payload[1:17].hex()} info {len(info)} bytes", info)
            self._send_frame(conn, peer, struct.pack("<BI", SYS_NODE_INFO, state.get("node_id", 1)) + info, "node info")
            return
        if kind == SYS_APP_TO_SERVER and len(payload) >= 9:
            ev = parse_event(payload[1:])
            name = EVENT_NAMES.get(ev["type"], f"event {ev['type']:#05x}")
            probe.log("tcp", peer, f"{name} slot={ev['slot']} session={ev['session']} flags={ev['flags']} "
                      f"time={ev['time']} body {len(ev['body'])} bytes", ev["body"][:192])
            if ev["type"] == EV_SELF_ANNOUNCE:
                body = ev["body"]
                steam_id = struct.unpack_from("<Q", body)[0] if len(body) >= 8 else 0
                name_ = body[8:40].split(b"\0")[0].decode(errors="replace")
                probe.log("tcp", peer, f"player '{name_}' steamid {steam_id}")
                welcome = build_event(EV_WELCOME, session=ev["session"])
                self._send_frame(conn, peer, bytes([SYS_APP]) + welcome, "Welcome event")
            return
        probe.log("tcp", peer, f"frame type {kind:#04x} {len(payload)} bytes" if kind is not None else "empty frame", payload)


def make_handler(server):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            probe.log("http", self.client_address[0], f"-> {code} {len(data)} bytes", data[:512])

        def _route(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            probe.log("http", f"{self.client_address[0]}:{self.client_address[1]}",
                      f"{method} {self.path} ua={self.headers.get('User-Agent')}", body)
            path = self.path.split("?")[0].rstrip("/").lower()
            if path == "/sessioninfo":
                return self._send(200, json.dumps(server.session_info()))
            if path == "/rules":
                return self._send(200, server.rules_body)
            if path == "/access":
                try:
                    pw = json.loads(body or b"{}").get("Password", "")
                except ValueError:
                    pw = ""
                ok = pw == server.a.password
                return self._send(200 if ok else 403, json.dumps({"access": ok}))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", default=r"L:\SteamLibrary\steamapps\common\Le Mans Ultimate")
    ap.add_argument("--name", default="LMU Local Server")
    ap.add_argument("--http", type=int, default=64298)
    ap.add_argument("--sim", type=int, default=54298)
    ap.add_argument("--lan-ip", default="127.0.0.1")
    ap.add_argument("--wan-ip", default="127.0.0.1")
    ap.add_argument("--max-players", type=int, default=20)
    ap.add_argument("--password", default="")
    ap.add_argument("--location", default="Spa_2023")
    ap.add_argument("--layout", default="")
    ap.add_argument("--scene", default="SPAWEC", help="track sceneDesc, see /rest/sessions/getTracksAll")
    ap.add_argument("--rules-file", default=str(Path(__file__).with_name("data") / "default.rules.json"),
                    help="game rules JSON with a top-level 'Rules' object (from your own install, not in the repo)")
    ap.add_argument("--bop-dir", default=str(Path(__file__).with_name("data") / "bop"),
                    help="folder of per-track BoP JSON files (from your own install, not in the repo)")
    ap.add_argument("--build", type=int, default=None)
    ap.add_argument("--latest-only", dest="all_versions", action="store_false",
                    help="list only the newest version of each car/track (clients expect every installed version)")
    ap.add_argument("--idle", type=float, default=120.0)
    ap.add_argument("--join", action="store_true", help="ask the local game to join this server")
    ap.add_argument("--car", default="2794929f7db8eebe8c1852fc8b2045ef42f4f299",
                    help="car id (from /rest/race/car) the --join test selects before entering the race")
    ap.add_argument("--log", default="lmu_server.log")
    a = ap.parse_args()

    probe.LOG = open(a.log, "a", encoding="utf-8")
    srv = Server(a)
    probe.log("server", "-", f"start http={a.http} sim={a.sim} build={srv.build} scene={a.scene} "
              f"bop={srv.bop_file.name} components={len(srv.components)} rules={srv.rules_hash}")

    httpd = ThreadingHTTPServer(("0.0.0.0", a.http), make_handler(srv))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    SimServer(a.sim, a.idle, build_session_desc(a.name, a.scene, a.max_players)).start()
    time.sleep(0.5)

    if a.join:
        threading.Thread(target=probe.request_join, args=(a.http, a.password, a.car), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        probe.log("server", "-", "stop")
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
