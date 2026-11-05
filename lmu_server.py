"""Standalone LMU server (work in progress).

Implements the HTTP join handshake the LMU client expects from a server
(SessionInfo -> Rules -> Access) and logs the simulation-port traffic that
follows, so the sim protocol can be implemented next.

Usage:
  python lmu_server.py [--game-dir PATH] [--http 64298] [--sim 54298] [--join]
"""
import argparse
import hashlib
import heapq
import itertools
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
import upnp

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
    def __init__(self, a, schedule):
        self.a = a
        self.schedule = schedule
        self.sim = None
        self.components = scan_components(a.game_dir, a.all_versions)
        rules = json.loads(Path(a.rules_file).read_text(encoding="utf-8"))["Rules"]
        self.bop_file = find_bop_file(a.bop_dir, a.scene)
        bop = json.loads(self.bop_file.read_text(encoding="utf-8"))
        self.rules_body = json.dumps({"Rules": rules, "Bop": bop,
                                      "Components": [c["signature"] for c in self.components]})
        self.rules_hash = hashlib.sha256(RULES_SALT_FMT.format(self.rules_body).encode()).hexdigest()
        self.guid = "{%s}" % str(uuid.uuid5(uuid.NAMESPACE_DNS, f"lmu-local-{a.name}")).upper()
        self.http_store = {}
        self.tires = {}      # (slot, wheel, index) -> tyre record uploaded with SaveTire
        self.equipped = {}   # (slot, wheel) -> index chosen with SetEquippedTire
        self.build = a.build if a.build is not None else game_build(a.game_dir)

    def tire_request(self, method, path, body):
        """The tyre inventory the client keeps on the server (limited tyre rules).

        POST /SaveTire/{slot}/{wheel}/{index}: one tyre record (0x1548 bytes).
        POST /SetEquippedTire/{slot}/{wheel}/{index}: which tyre is on that wheel.
        GET /GetTires/{slot}/{id}: the four equipped tyres, wheel 0-3, exactly 4 x 0x1548 bytes.
        Pressing Drive calls GetTires; any other answer sends the player back to the garage monitor
        ("failed all attempts to recieve tires from server"), or to the main menu in a race.
        Returns (status, body) or None when the path is not a tyre request."""
        parts = path.strip("/").split("/")
        name = parts[0]
        if name not in ("savetire", "setequippedtire", "gettires"):
            return None
        try:
            nums = [int(p) for p in parts[1:4] if p.lstrip("-").isdigit()]
        except ValueError:
            nums = []
        if name == "savetire" and method == "POST" and len(nums) == 3:
            self.tires[tuple(nums)] = body
            return 200, "{}"
        if name == "setequippedtire" and method == "POST" and len(nums) == 3:
            slot, wheel, index = nums
            self.equipped[(slot, wheel)] = index
            return 200, "{}"
        if name == "gettires" and method == "GET" and nums:
            slot = nums[0]
            out = b""
            for wheel in range(4):
                index = self.equipped.get((slot, wheel), 0)
                record = self.tires.get((slot, wheel, index))
                if record is None:
                    record = next((v for (s, w, _i), v in sorted(self.tires.items()) if s == slot and w == wheel),
                                  None)
                if record is None:
                    return 404, json.dumps({"error": f"no tyres saved for slot {slot} wheel {wheel}"})
                out += record[:TIRE_RECORD_SIZE].ljust(TIRE_RECORD_SIZE, b"\0")
            return 200, out
        return 404, json.dumps({"error": "bad tyre request"})

    def session_info(self):
        players = [{"name": n.name, "countryCode": ""} for n in (self.sim.real_nodes() if self.sim else []) if n.name]
        return {
            "name": self.a.name,
            "guid": self.guid,
            "lanIp": self.a.lan_ip,
            "wanIp": self.a.wan_ip,
            "httpPort": self.a.http,
            "simulationPort": self.a.sim,
            "useRUDP": False,
            "maxPlayers": self.a.max_players,
            "players": players,
            "playerCount": len(players),
            "driverCount": len(players),
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
                "time_left": int(self.schedule.remaining()),
                "fixed_setups": False,
                "fixed_upgrades": False,
                "tire_warmers": True,
                "finish_type": 0,
                "ai_vehicles": [],
                "is_closed": False,
                "in_development": False,
                "is_anonymous": False,
                "session": self.schedule.session,
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
EV_READY = 0xC2               # S->C body[0] = slot or 0xff; client marks itself ready and requests a vehicle
EV_SELF_ANNOUNCE = 0xC7       # C->S steamid u64, name[32], auth[65], nationality
EV_VEHICLE_REQUEST = 0xE0     # C->S vehicle[32] paintblob[34] number[4] team[26] upgrades[16]
EV_SLOT_VEHICLE = 0xE1        # S->C header slot; parking, vehicle[32], paintblob[64], upgrades[16], number[5], team
EV_REMOVE_VEHICLE = 0xE2      # S->C body[0] = slot
EV_SLOT_DATA = 0xA2           # C->S->C per-slot car data blob, body[0] = slot
EV_SLOT_ASSIGNMENT = 0xE3     # S->C body[0] = slot
EV_DRIVER_INFO = 0xE4         # S->C header slot; driver index, flags, nationality, name
EV_ACTIVE_DRIVER = 0xE5       # S->C header slot; node id u32, driver index u8
EV_WELCOME = 0xE7             # S->C no body
EV_VEHICLE_RESPONSE = 0xF3    # S->C body[0] = approved
EV_TAKE_CONTROL = 0xF4        # C->S
EV_TAKE_CONTROL_RESPONSE = 0xF5  # S->C body[0] = approved
EV_RACE_STATE2 = 0x174        # S->C "Other::RaceState2": phase, session times, per-slot finish status
EV_SESSION_READY = 0xF8       # C->S no body, sent on entering a session
EV_SESSION_ACK = 0xF9         # S->C clears the client's "re-apply session description" flag (+0x140)
EVENT_NAMES = {
    EV_READY: "Ready", EV_SELF_ANNOUNCE: "SelfAnnounce", EV_VEHICLE_REQUEST: "VehicleRequest",
    EV_SLOT_VEHICLE: "SlotVehicle", EV_SLOT_ASSIGNMENT: "SlotAssignment", EV_DRIVER_INFO: "DriverInfo",
    EV_ACTIVE_DRIVER: "ActiveDriver", EV_WELCOME: "Welcome", EV_VEHICLE_RESPONSE: "VehicleResponse",
    EV_TAKE_CONTROL: "TakeControl", EV_TAKE_CONTROL_RESPONSE: "TakeControlResponse",
    EV_SESSION_READY: "SessionReady", EV_SESSION_ACK: "SessionAck", EV_RACE_STATE2: "RaceState2",
}
MAX_SLOTS = 0x68
GHOST_NODE_ID = 200  # must stay below 0x268 (client per-node arrays)
EVENT_TICKS_PER_SECOND = 4096  # event time field unit (constant 0.000244140625 in the client)


def cstr(data):
    return data.split(b"\0")[0].decode("utf-8", "replace")


def fixed(text, size):
    return text.encode("utf-8", "replace")[:size - 1].ljust(size, b"\0")


class Node:
    """One connected player (HotPacket node)."""

    def __init__(self, conn, peer, node_id):
        self.conn = conn
        self.peer = peer
        self.node_id = node_id
        self.session = SESSION_PRACTICE1
        self.slot = None
        self.name = ""
        self.steam_id = 0
        self.nationality = 0
        self.vehicle = None
        self.client_id = 0
        self.udp_addr = None
        self.info = b""
        self.info_sent = False
        self.in_control = False
        self.cached = {}
        self.fake = False
        self.ghost_of = None
        self.send_lock = threading.Lock()
        self.seen_types = {}


SESSION_DESC_SIZE = 0x69


SESSION_PRACTICE1 = 1  # 1-4 practice, 5-8 qualifying, 9 warmup, 10-13 race; 15 = none (crashes the client)
SESSION_CODES = {"P": (1, 4, "Practice"), "Q": (5, 4, "Qualifying"), "W": (9, 1, "Warmup"), "R": (10, 4, "Race")}


class SessionSchedule:
    """Session order and lengths, e.g. "P:10,Q:10,R:20" (minutes). The session clock restarts at 0 each session."""

    def __init__(self, spec):
        self.items = []
        counters = {}
        for part in spec.split(","):
            code, _, minutes = part.strip().partition(":")
            code = code.strip().upper()[:1]
            if code not in SESSION_CODES:
                raise ValueError(f"unknown session '{part}', use P, Q, W or R")
            base, count, label = SESSION_CODES[code]
            n = counters.get(code, 0)
            counters[code] = n + 1
            number = base + min(n, count - 1)
            self.items.append((number, float(minutes or 10) * 60.0, f"{label}{n + 1 if count > 1 else ''}"))
        self.index = 0
        self.started = time.monotonic()
        self.counter = 0
        self.lock = threading.Lock()

    @property
    def session(self):
        return self.items[self.index][0]

    @property
    def label(self):
        return self.items[self.index][2]

    def elapsed(self):
        return time.monotonic() - self.started

    def remaining(self):
        return max(0.0, self.items[self.index][1] - self.elapsed())

    def advance_if_due(self):
        with self.lock:
            if self.remaining() > 0:
                return False
            self.index = (self.index + 1) % len(self.items)
            self.started = time.monotonic()
            self.counter = (self.counter + 1) & 0xFF
            return True


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


# Game phases (rFactor 2 numbering) and the race start used by the server.
PHASE_BEFORE, PHASE_FORMATION, PHASE_COUNTDOWN, PHASE_GREEN, PHASE_OVER = 0, 3, 4, 5, 8
PHASE_NAMES = {0: "before", 3: "formation", 4: "countdown", 5: "green", 8: "over"}
RACE_GRID_SECONDS = 30.0       # race: everyone waits on the grid this long (time to load in)
RACE_COUNTDOWN_SECONDS = 5.0   # then the start lights, then green
FLT_MAX = 3.4028234663852886e38
TIRE_RECORD_SIZE = 0x1548      # one tyre in SaveTire / GetTires (GetTires = 4 of them)
RACE_STATE_INTERVAL = 5.0


def build_race_state(phase, start, end, slots):
    """Body of RaceState2 (0x174), parsed by the client in ProcessRaceStateEvent2() (steward.cpp).

    u8 phase | yellow-flag state << 4, then six floats: session start time (twice), session end
    time, and three more times the game resets to FLT_MAX ("none"). Then 3 bytes per car:
    u16 = slot << 8 | finish status << 6 (0 racing, 1 finished, 2 DNF, 3 DQ) | two small fields
    (left 0), and an i8 qualifying position. A car whose finish status is not 0 cannot drive:
    the UI's Drive button (/rest/race/getAllowedToStartRacing) stays disabled."""
    body = struct.pack("<B6f", phase & 0x0F, start, start, end, FLT_MAX, FLT_MAX, FLT_MAX)
    for position, slot in enumerate(slots):
        body += struct.pack("<Hb", slot << 8, position)
    return body


def exclusive_socket(kind):
    """A socket that refuses to share its port. On Windows SO_REUSEADDR (Python's HTTP server default)
    lets a second server bind the same port, and joins then land on whichever one Windows picks."""
    s = socket.socket(socket.AF_INET, kind)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    elif kind == socket.SOCK_STREAM:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return s


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def build_event(ev_type, body=b"", slot=0xFF, session=0, flags=3, time_=0):
    header = slot | (len(body) << 8) | (ev_type << 17) | (session << 26) | (flags << 30)
    return struct.pack("<II", time_, header) + body


class SimServer:
    """Simulation port: transport handshake, HotPacket system messages, NetComm events and the relay."""

    def __init__(self, port, idle, desc_args, schedule, ghost_delay=0.0):
        self.port = port
        self.idle = idle
        self.desc_args = desc_args
        self.schedule = schedule
        self.ghost_delay = ghost_delay
        self.next_id = 0x1000
        self.next_node = 1
        self.start_ns = time.monotonic_ns()
        self.lock = threading.Lock()
        self.nodes = {}
        self.by_client_id = {}
        self.slots = {}
        self.ghost = None
        self.delayed = []
        self.delayed_cv = threading.Condition()
        self._seq = itertools.count()
        self.udp = exclusive_socket(socket.SOCK_DGRAM)
        self.udp.bind(("0.0.0.0", port))
        self.tcp = exclusive_socket(socket.SOCK_STREAM)
        self.tcp.bind(("0.0.0.0", port))
        self.tcp.listen(16)

    def start(self):
        threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._tcp_loop, daemon=True).start()
        threading.Thread(target=self._delay_loop, daemon=True).start()
        threading.Thread(target=self._schedule_loop, daemon=True).start()
        threading.Thread(target=self._race_state_loop, daemon=True).start()
        extra = f", ghost delay {self.ghost_delay:g}s" if self.ghost_delay else ""
        probe.log("sim", f":{self.port}", f"listening (tcp+udp), session {self.schedule.label}" + extra)

    def session_seconds(self):
        return self.schedule.elapsed()

    def session_update(self):
        desc = build_session_desc(*self.desc_args, session=self.schedule.session)
        return bytes([SYS_SESSION_UPDATE, self.schedule.counter]) + desc

    def _schedule_loop(self):
        while True:
            time.sleep(1)
            if self.schedule.advance_if_due():
                probe.log("sim", "session", f"advancing to {self.schedule.label} (session {self.schedule.session})")
                update = self.session_update()
                for node in self.real_nodes():
                    self.send_raw(node, update)
                time.sleep(2)
                for node in self.real_nodes():
                    self.send_event(node, EV_SESSION_ACK)

    def race_phase(self):
        """(phase, start, end) for the current session. Practice/qualifying are green from the start;
        a race holds everyone on the grid, runs the start lights, then goes green."""
        length = self.schedule.items[self.schedule.index][1]
        if self.schedule.session < 10:
            return PHASE_GREEN, 0.0, length
        elapsed = self.schedule.elapsed()
        green_at = RACE_GRID_SECONDS + RACE_COUNTDOWN_SECONDS
        if elapsed < RACE_GRID_SECONDS:
            phase = PHASE_BEFORE
        elif elapsed < green_at:
            phase = PHASE_COUNTDOWN
        else:
            phase = PHASE_GREEN
        return phase, green_at, max(length, green_at + 60.0)

    def send_race_state(self, nodes=None):
        phase, start, end = self.race_phase()
        with self.lock:
            slots = sorted(self.slots)
        body = build_race_state(phase, start, end, slots)
        for node in nodes if nodes is not None else self.real_nodes():
            self.send_event(node, EV_RACE_STATE2, body, quiet=True)
        return phase

    def _race_state_loop(self):
        """Resend the race state every RACE_STATE_INTERVAL seconds, and at once when the phase changes.
        (Each update makes clients re-run AssignPitAndGarage, so not more often than needed.)"""
        last, sent_at = None, 0.0
        while True:
            time.sleep(0.5)
            state = (self.race_phase()[0], self.schedule.session)
            if state == last and time.monotonic() - sent_at < RACE_STATE_INTERVAL:
                continue
            self.send_race_state()
            sent_at = time.monotonic()
            if state != last:
                last = state
                probe.log("sim", "session", f"{self.schedule.label}: phase {PHASE_NAMES.get(state[0], state[0])}")

    def disconnect_all(self):
        """Drop every player. Their games return to the menu; keep HTTP up a little longer afterwards,
        because a game that polls a vanished server (e.g. /teams/list) crashes."""
        for node in self.real_nodes():
            try:
                node.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ------------------------------------------------------------------ sending

    def _udp_send(self, addr, client_id, payload):
        self.udp.sendto(struct.pack("<QH", client_id, len(payload)) + payload, addr)

    def send_raw(self, node, payload, udp=False):
        if node.fake or node.conn is None:
            return
        if udp:
            if node.udp_addr:
                self._udp_send(node.udp_addr, node.client_id, payload)
            return
        try:
            with node.send_lock:
                node.conn.sendall(struct.pack("<H", len(payload)) + payload)
        except OSError:
            pass

    def send_event(self, node, ev_type, body=b"", slot=0xFF, quiet=False):
        self.send_raw(node, bytes([SYS_APP]) + build_event(ev_type, body, slot=slot, session=node.session))
        if not quiet and not node.fake:
            probe.log("tcp", node.peer, f"-> {EVENT_NAMES.get(ev_type, hex(ev_type))} slot={slot} body {len(body)} bytes",
                      body[:160])

    def real_nodes(self):
        with self.lock:
            return [n for n in self.nodes.values() if not n.fake]

    # ------------------------------------------------------------------ UDP

    def _udp_loop(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(65536)
            except OSError:
                continue
            peer = f"{addr[0]}:{addr[1]}"
            if len(data) < 10:
                continue
            client_id, length = struct.unpack_from("<QH", data)
            payload = data[10:10 + length]
            node = self.by_client_id.get(client_id)
            if node is not None:
                node.udp_addr = addr
            if payload == MAGIC(MSG_UDP_CLIENT_VERIFY):
                self._udp_send(addr, client_id, MAGIC(MSG_UDP_SERVER_VERIFY))
                probe.log("udp", peer, f"UdpClientVerification from {client_id:#x}; replied")
            elif payload[:1] == bytes([SYS_SYNC_REQUEST]) and len(payload) >= 5:
                (sync_id,) = struct.unpack_from("<I", payload, 1)
                now = self.session_seconds()
                # Server time is a double: the shared session clock in seconds (client stores it at +0x188).
                self._udp_send(addr, client_id, struct.pack("<BId", SYS_SYNC_RESPONSE, sync_id, now))
                if sync_id <= 2:
                    probe.log("udp", peer, f"sync request {sync_id}; replied session time {now:.3f}s")
            elif payload[:1] == bytes([SYS_PING]) and len(payload) >= 9:
                self._udp_send(addr, client_id, bytes([SYS_PONG]) + payload[1:9])
            elif payload[:1] == bytes([SYS_APP_TO_SERVER]) and len(payload) >= 9 and node is not None:
                self._on_event(node, parse_event(payload[1:]), bytes(payload[1:]), udp=True)
            else:
                probe.log("udp", peer, f"client {client_id:#x} type {payload[:1].hex()} {len(payload)} bytes", payload)

    # ------------------------------------------------------------------ TCP

    def _tcp_loop(self):
        s = self.tcp
        while True:
            conn, addr = s.accept()
            threading.Thread(target=self._tcp_session, args=(conn, addr), daemon=True).start()

    def _tcp_session(self, conn, addr):
        peer = f"{addr[0]}:{addr[1]}"
        conn.settimeout(self.idle)
        buf = b""
        stage = "request"
        with self.lock:
            client_id = self.next_id
            self.next_id += 1
        state = {"client_id": client_id}
        probe.log("tcp", peer, f"connect (client id {client_id:#x})")
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    probe.log("tcp", peer, f"closed by client at stage {stage}")
                    return
                buf += data
                if stage == "request" and len(buf) >= 4:
                    if struct.unpack_from("<I", buf)[0] != MSG_TCP_REQUEST:
                        probe.log("tcp", peer, "unexpected first message", buf[:32])
                    buf = buf[4:]
                    conn.sendall(MAGIC(MSG_TCP_RESPONSE) + struct.pack("<BHQ", 1, self.port, client_id))
                    stage = "confirm"
                if stage == "confirm" and len(buf) >= 4:
                    if struct.unpack_from("<I", buf)[0] == MSG_TCP_CONFIRM:
                        probe.log("tcp", peer, "transport established")
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
            node = state.get("node")
            if node:
                self._remove_node(node)

    def _on_frame(self, conn, peer, payload, state):
        kind = payload[0] if payload else None
        node = state.get("node")
        if kind == SYS_KEEPALIVE:
            if node:
                self.send_raw(node, bytes([SYS_KEEPALIVE]))
            else:
                conn.sendall(struct.pack("<HB", 1, SYS_KEEPALIVE))
            return
        if kind == SYS_JOIN_REQUEST and len(payload) >= 18:
            role = "spectator" if payload[1] else "driver"
            guid = uuid.UUID(bytes_le=bytes(payload[2:18]))
            with self.lock:
                node_id = self.next_node
                self.next_node += 1
                node = Node(conn, peer, node_id)
                node.client_id = state["client_id"]
                self.nodes[node_id] = node
                self.by_client_id[node.client_id] = node
            state["node"] = node
            probe.log("tcp", peer, f"JoinRequest as {role}, player guid {{{str(guid).upper()}}}; node {node_id}")
            self.send_raw(node, self.session_update())
            self.send_raw(node, struct.pack("<BBIB", SYS_JOIN_RESPONSE, 1, node_id, 0))
            return
        if kind == SYS_CLIENT_INFO_REQUEST and len(payload) >= 17 and node:
            node.info = bytes(payload[17:])
            probe.log("tcp", peer, f"ClientInfoUpdate {len(node.info)} bytes", node.info)
            first = not node.info_sent
            node.info_sent = True
            for other in self.real_nodes():
                self.send_raw(other, struct.pack("<BI", SYS_NODE_INFO, node.node_id) + node.info)
            if first:
                # The newcomer learns about everyone already here.
                with self.lock:
                    others = [n for n in self.nodes.values() if n is not node and n.info_sent]
                for other in others:
                    self.send_raw(node, struct.pack("<BI", SYS_NODE_INFO, other.node_id) + other.info)
            return
        if kind == SYS_APP_TO_SERVER and len(payload) >= 9 and node:
            self._on_event(node, parse_event(payload[1:]), bytes(payload[1:]), udp=False)
            return
        probe.log("tcp", peer, f"frame type {kind:#04x} {len(payload)} bytes" if kind is not None else "empty frame",
                  payload[:64])

    def _remove_node(self, node):
        with self.lock:
            self.nodes.pop(node.node_id, None)
            self.by_client_id.pop(node.client_id, None)
            if node.slot is not None and self.slots.get(node.slot) == node.node_id:
                self.slots.pop(node.slot, None)
        probe.log("tcp", node.peer, f"node {node.node_id} ('{node.name}') left")
        for other in self.real_nodes():
            if node.slot is not None:
                self.send_event(other, EV_REMOVE_VEHICLE, bytes([node.slot]), quiet=True)
            self.send_raw(other, struct.pack("<BIB", SYS_NODE_LEFT, node.node_id, 0))
        ghost = self.ghost
        if ghost is not None and ghost.ghost_of is node:
            self.ghost = None
            self._remove_node(ghost)

    # ------------------------------------------------------------------ NetComm events

    def _on_event(self, node, ev, raw, udp):
        t = ev["type"]
        node.session = ev["session"]
        count = node.seen_types[t] = node.seen_types.get(t, 0) + 1
        if count <= 2:
            name = EVENT_NAMES.get(t, f"event {t:#05x}")
            more = " (further ones not logged)" if count == 2 else ""
            probe.log("udp" if udp else "tcp", node.peer,
                      f"{name} cat={t >> 6} slot={ev['slot']} session={ev['session']} time={ev['time']} "
                      f"body {len(ev['body'])} bytes{more}", ev["body"][:96])
        body = ev["body"]
        if t >> 6 != 3:
            self._relay(node, ev, raw, udp)
            return
        if t == EV_SELF_ANNOUNCE:
            node.steam_id = struct.unpack_from("<Q", body)[0] if len(body) >= 8 else 0
            node.name = cstr(body[8:40])
            node.nationality = body[0x69] if len(body) > 0x69 else 0
            probe.log("tcp", node.peer, f"player '{node.name}' steamid {node.steam_id} nationality {node.nationality}")
            self.send_event(node, EV_WELCOME)
            self.send_event(node, EV_READY, bytes([0xFF]))
            with self.lock:
                others = [n for n in self.nodes.values() if n is not node and n.slot is not None]
            for other in others:
                self._announce_slot(node, other)
        elif t == EV_SESSION_READY:
            self.send_event(node, EV_SESSION_ACK)
            self.send_race_state([node])
            if node.slot is None:
                # Loading the session resets the client's "server is ready" flag (+0x145, set by Ready);
                # without a fresh Ready a game that already joined once never asks for a car and the
                # UI's Drive button stays disabled (getAllowedToStartRacing = false).
                self.send_event(node, EV_READY, bytes([0xFF]))
        elif t == EV_VEHICLE_REQUEST:
            self._assign_vehicle(node, body)
        elif t == EV_TAKE_CONTROL:
            node.in_control = True
            self.send_event(node, EV_TAKE_CONTROL_RESPONSE, b"\x01")
            for other in self.real_nodes():
                self._send_active_driver(other, node)

    def _relay(self, src, ev, raw, udp):
        """Forward a player's gameplay event (positions, car data) to everyone else as 'event from node'."""
        if ev["type"] >> 6 in (1, 2):
            src.cached[ev["type"]] = raw
        for other in self.real_nodes():
            if other is not src:
                self.send_raw(other, bytes([SYS_APP_FROM_NODE]) + struct.pack("<I", src.node_id) + raw, udp=udp)
        ghost = self.ghost
        if ghost is not None and ghost.ghost_of is src and ghost.slot is not None and src.slot is not None:
            rewritten = self._as_ghost(ev, raw, src.slot, ghost.slot)
            if rewritten:
                due = time.monotonic() + self.ghost_delay
                with self.delayed_cv:
                    heapq.heappush(self.delayed, (due, next(self._seq), src, ghost, rewritten, udp))
                    self.delayed_cv.notify()

    def _as_ghost(self, ev, raw, real_slot, ghost_slot):
        header = struct.unpack_from("<I", raw, 4)[0]
        if header & 0xFF == real_slot:
            header = (header & ~0xFF) | ghost_slot
        elif header & 0xFF != 0xFF:
            return None
        out = bytearray(raw)
        shifted = ev["time"] + int(self.ghost_delay * EVENT_TICKS_PER_SECOND)
        struct.pack_into("<II", out, 0, shifted & 0xFFFFFFFF, header)
        if ev["type"] == EV_SLOT_DATA and len(out) > 8 and out[8] == real_slot:
            out[8] = ghost_slot
        return bytes(out)

    def _delay_loop(self):
        while True:
            with self.delayed_cv:
                while not self.delayed or self.delayed[0][0] > time.monotonic():
                    timeout = None if not self.delayed else max(0.0, self.delayed[0][0] - time.monotonic())
                    self.delayed_cv.wait(timeout)
                _, _, target, ghost, raw, udp = heapq.heappop(self.delayed)
            if self.ghost is ghost:
                self.send_raw(target, bytes([SYS_APP_FROM_NODE]) + struct.pack("<I", ghost.node_id) + raw, udp=udp)

    # ------------------------------------------------------------------ slots and vehicles

    def _slot_vehicle_body(self, owner):
        v = owner.vehicle
        return (bytes([owner.slot]) + fixed(v["vehicle"], 32) + fixed(v["paintblob"], 64) + v["upgrades"]
                + fixed(v["number"], 5) + v["team"].encode()[:64])

    def _announce_slot(self, target, owner):
        """Tell target about owner's car: driver name, vehicle, and (if driving) who controls it."""
        if owner.slot is None or owner.vehicle is None:
            return
        quiet = target is not owner
        self.send_event(target, EV_DRIVER_INFO, bytes([0, 0, owner.nationality]) + owner.name.encode()[:31],
                        slot=owner.slot, quiet=quiet)
        self.send_event(target, EV_SLOT_VEHICLE, self._slot_vehicle_body(owner), slot=owner.slot, quiet=quiet)
        if owner.in_control:
            self._send_active_driver(target, owner)
        if target is not owner:
            for raw in owner.cached.values():
                self.send_raw(target, bytes([SYS_APP_FROM_NODE]) + struct.pack("<I", owner.node_id) + raw)

    def _send_active_driver(self, target, owner):
        self.send_event(target, EV_ACTIVE_DRIVER, struct.pack("<IB", owner.node_id, 0), slot=owner.slot,
                        quiet=target is not owner)

    def _assign_vehicle(self, node, body):
        node.vehicle = {
            "vehicle": cstr(body[0x00:0x20]),
            "paintblob": cstr(body[0x20:0x42]),
            "number": cstr(body[0x42:0x46]),
            "team": cstr(body[0x46:0x60]),
            "upgrades": bytes(body[0x60:0x70]).ljust(16, b"\0"),
        }
        with self.lock:
            if node.slot is None:
                node.slot = next(s for s in range(MAX_SLOTS) if s not in self.slots)
                self.slots[node.slot] = node.node_id
        v = node.vehicle
        probe.log("tcp", node.peer, f"vehicle request: {v['vehicle']} #{v['number']} team '{v['team']}' -> slot {node.slot}")
        self.send_event(node, EV_SLOT_ASSIGNMENT, bytes([node.slot]))
        for other in self.real_nodes():
            self._announce_slot(other, node)
        self.send_event(node, EV_VEHICLE_RESPONSE, b"\x01")
        self.send_race_state()
        threading.Thread(target=self._confirm_slot, args=(node,), daemon=True).start()
        if self.ghost_delay and self.ghost is None:
            self._spawn_ghost(node)

    def _confirm_slot(self, node):
        """Re-send SlotAssignment until the client's own info (02) carries its slot at byte 0x24.

        The client only records the slot if it handles SlotAssignment after the car has loaded; a
        single assignment sent straight back with the VehicleResponse is often too early. Without the
        slot in its info the game keeps Drive disabled (getAllowedToStartRacing checks that byte)."""
        for delay in (1.0, 2.0, 3.0, 5.0, 8.0):
            time.sleep(delay)
            if node.slot is None or node.node_id not in self.nodes:
                return
            if len(node.info) > 0x24 and node.info[0x24] == node.slot:
                return
            probe.log("tcp", node.peer, f"client has not confirmed slot {node.slot} yet; re-sending SlotAssignment")
            self.send_event(node, EV_SLOT_ASSIGNMENT, bytes([node.slot]), quiet=True)

    def _spawn_ghost(self, real):
        with self.lock:
            ghost = Node(None, "ghost", GHOST_NODE_ID)
            ghost.fake = True
            ghost.ghost_of = real
            ghost.name = f"Ghost {self.ghost_delay:g}s"
            ghost.nationality = real.nationality
            ghost.vehicle = dict(real.vehicle, number="0")
            ghost.info = real.info
            ghost.info_sent = True
            ghost.in_control = True
            ghost.slot = next(s for s in range(MAX_SLOTS) if s not in self.slots)
            self.slots[ghost.slot] = ghost.node_id
            self.nodes[ghost.node_id] = ghost
            self.ghost = ghost
        probe.log("sim", "ghost", f"replaying '{real.name}' {self.ghost_delay:g}s behind in slot {ghost.slot}")
        self.send_raw(real, struct.pack("<BI", SYS_NODE_INFO, ghost.node_id) + (ghost.info or b""))
        self._announce_slot(real, ghost)


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
            probe.log("http", self.client_address[0], f"-> {code} {len(data)} bytes", data[:256])

        def _route(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            probe.log("http", f"{self.client_address[0]}:{self.client_address[1]}",
                      f"{method} {self.path} ({len(body)} bytes) ua={self.headers.get('User-Agent')}", body[:256])
            raw_path = self.path.split("?")[0].rstrip("/")
            path = raw_path.lower()
            store_key = next((p for p in ("/mp_vehicle_setup/", "/mp_vehicle_pm/", "/mp_vehicle/")
                              if path.startswith(p)), None)
            if store_key and method == "PUT":
                server.http_store[path] = body
                return self._send(200, "{}")
            if store_key and method == "GET":
                if path in server.http_store:
                    return self._send(200, server.http_store[path])
                if store_key == "/mp_vehicle/":
                    # Fresh car: every memento key is optional on read (FUN_1402a5000 returns null),
                    # compressed_setup null zeroes the setup, start_from_garage puts the car in its box.
                    return self._send(200, json.dumps({"vehicle": {}, "compressed_setup": None,
                                                       "start_from_garage": True, "lock_setup_in_garage": False}))
                return self._send(404, json.dumps({"error": "no stored state"}))
            tire = server.tire_request(method, path, body)
            if tire is not None:
                code, data = tire
                return self._send(code, data, "application/octet-stream" if code == 200 and data else
                                  "application/json")
            if path.startswith("/teams/list"):
                return self._send(200, json.dumps({"coherenceId": 0, "teams": {}, "drivers": {}}))
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

        def do_PUT(self):
            self._route("PUT")

    return Handler


def open_router_ports(a):
    """Map the HTTP port (TCP) and simulation port (TCP+UDP) to this PC. Returns (gateway, mapped)."""
    try:
        gw = upnp.discover(lan_ip=a.lan_ip)
    except upnp.UPnPError as e:
        probe.log("upnp", "-", f"{e}. Forward TCP {a.http} and TCP+UDP {a.sim} to {a.lan_ip} by hand.")
        return None, []
    mapped = []
    for port, proto in ((a.http, "TCP"), (a.sim, "TCP"), (a.sim, "UDP")):
        try:
            gw.add_port_mapping(port, proto, f"LMU server {proto} {port}")
            mapped.append((port, proto))
            probe.log("upnp", "-", f"router forwards {proto} {port} -> {gw.local_ip}:{port}")
        except upnp.UPnPError as e:
            probe.log("upnp", "-", f"could not map {proto} {port}: {e}")
    try:
        ext = gw.external_ip()
    except upnp.UPnPError as e:
        probe.log("upnp", "-", f"could not read the public IP: {e}")
        ext = ""
    if ext and not upnp.is_public(ext):
        probe.log("upnp", "-", f"WARNING: the router's internet address {ext} is not public (double NAT or "
                               "carrier-grade NAT); players outside your network cannot reach you this way. "
                               "Use a virtual LAN (Tailscale, ZeroTier) instead.")
    elif ext and a.wan_ip == "auto":
        a.wan_ip = ext
    return gw, mapped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", default=r"L:\SteamLibrary\steamapps\common\Le Mans Ultimate")
    ap.add_argument("--name", default="LMU Local Server")
    ap.add_argument("--http", type=int, default=64298)
    ap.add_argument("--sim", type=int, default=54298)
    ap.add_argument("--lan-ip", default="auto", help="this PC's LAN address (auto = default-route adapter)")
    ap.add_argument("--wan-ip", default="auto", help="public address (auto = from the router with --upnp)")
    ap.add_argument("--upnp", action="store_true",
                    help="open the ports on the router with UPnP while the server runs (internet hosting)")
    ap.add_argument("--max-players", type=int, default=20)
    ap.add_argument("--password", default="")
    ap.add_argument("--location", default="Spa_2023")
    ap.add_argument("--layout", default="")
    ap.add_argument("--scene", default="SPAWEC", help="track sceneDesc, see /rest/sessions/getTracksAll")
    ap.add_argument("--sessions", default="P:30,Q:15,R:30",
                    help="session order and minutes, P=practice Q=qualifying W=warmup R=race, e.g. P:10,Q:10,R:20")
    ap.add_argument("--rules-file", default=str(Path(__file__).with_name("data") / "default.rules.json"),
                    help="game rules JSON with a top-level 'Rules' object (from your own install, not in the repo)")
    ap.add_argument("--bop-dir", default=str(Path(__file__).with_name("data") / "bop"),
                    help="folder of per-track BoP JSON files (from your own install, not in the repo)")
    ap.add_argument("--build", type=int, default=None)
    ap.add_argument("--latest-only", dest="all_versions", action="store_false",
                    help="list only the newest version of each car/track (clients expect every installed version)")
    ap.add_argument("--idle", type=float, default=120.0)
    ap.add_argument("--join", action="store_true", help="ask the local game to join this server")
    ap.add_argument("--ghost", type=float, default=0.0, metavar="SECONDS",
                    help="test the relay alone: add a fake player that replays your car this many seconds behind")
    ap.add_argument("--car", default=None,
                    help="car id (from /rest/race/car) for --join; default is the car last picked in the game")
    ap.add_argument("--run-seconds", type=float, default=0.0,
                    help="stop gracefully after this many seconds (0 = run until Ctrl+C)")
    ap.add_argument("--stdin-control", action="store_true",
                    help="stop gracefully when stdin says 'stop' or closes (used by the toolkit)")
    ap.add_argument("--log", default="lmu_server.log")
    a = ap.parse_args()

    probe.LOG = open(a.log, "a", encoding="utf-8")
    if a.lan_ip == "auto":
        a.lan_ip = upnp.default_lan_ip()
    mapped, gateway = [], None
    if a.upnp:
        gateway, mapped = open_router_ports(a)
    if a.wan_ip == "auto":
        a.wan_ip = a.lan_ip
    schedule = SessionSchedule(a.sessions)
    srv = Server(a, schedule)
    probe.log("server", "-", f"start http={a.http} sim={a.sim} build={srv.build} scene={a.scene} "
              f"bop={srv.bop_file.name} components={len(srv.components)} rules={srv.rules_hash}")

    try:
        httpd = ExclusiveHTTPServer(("0.0.0.0", a.http), make_handler(srv))
        sim = SimServer(a.sim, a.idle, (a.name, a.scene, a.max_players), schedule, ghost_delay=a.ghost)
    except OSError as e:
        probe.log("server", "-", f"cannot open the ports (HTTP {a.http}, simulation {a.sim}): {e}. "
                                 "Is another server still running?")
        return 1
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.sim = sim
    sim.start()
    time.sleep(0.5)

    if a.join:
        threading.Thread(target=probe.request_join, args=(a.http, a.password, a.car), daemon=True).start()
    stop = threading.Event()
    if a.stdin_control:
        def watch_stdin():
            for line in sys.stdin:
                if line.strip().lower() in ("stop", "quit", "exit"):
                    break
            stop.set()
        threading.Thread(target=watch_stdin, daemon=True).start()
    probe.log("server", "-", f"ready: players join {a.wan_ip}:{a.http} (LAN {a.lan_ip}:{a.http})")
    deadline = time.monotonic() + a.run_seconds if a.run_seconds else None
    try:
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    probe.log("server", "-", "stopping: disconnecting players, HTTP stays up 10s")
    sim.disconnect_all()
    time.sleep(10)
    httpd.shutdown()
    for port, proto in mapped:
        try:
            gateway.delete_port_mapping(port, proto)
        except upnp.UPnPError as e:
            probe.log("upnp", "-", f"could not remove {proto} {port}: {e}")
    if mapped:
        probe.log("upnp", "-", "router ports closed again")
    probe.log("server", "-", "stopped")


if __name__ == "__main__":
    sys.exit(main())
