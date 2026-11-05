"""LMU Local Server Toolkit - host and join Le Mans Ultimate races without renting a server.

Hosting runs lmu_server.py (our own server, see PROTOCOL.md) in the background. Joining tells a
running game to connect through its local REST API, then enters the session like the game's UI.
"""

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import upnp

HERE = Path(__file__).resolve().parent
APP_ID = "2399420"
GAME_EXE = "Le Mans Ultimate.exe"
GAME_API_PORT = 6397
LAN_BROADCAST_PORT = 5399
LAN_MAGIC = "LMU_LAN_SERVER"
DEFAULT_HTTP_PORT = 64298
DEFAULT_SIM_PORT = 54298
SETTINGS_FILE = HERE / "toolkit_settings.json"
SERVER_LOG = HERE / "lmu_server.log"
DATA_DIR = HERE / "data"
FIREWALL_RULE_PREFIX = "LMU Local Server"
JOIN_TIMEOUT = 120
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- game discovery

def steam_root():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return None


def find_game_dir():
    candidates = []
    root = steam_root()
    if root:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            candidates += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', text)]
        candidates.append(root)
    for lib in candidates:
        game = lib / "steamapps" / "common" / "Le Mans Ultimate"
        if (game / GAME_EXE).exists():
            return str(game)
    return ""


def track_scenes():
    """Tracks the server can host: one BoP file per layout in data/bop (named like the sceneDesc)."""
    bop = DATA_DIR / "bop"
    if not bop.is_dir():
        return []
    return sorted(p.stem.upper() for p in bop.glob("*.json") if not p.stem.startswith("_") and p.stem != "default")


def server_data_missing():
    missing = []
    if not (DATA_DIR / "default.rules.json").exists():
        missing.append(str(DATA_DIR / "default.rules.json"))
    if not track_scenes():
        missing.append(str(DATA_DIR / "bop" / "<Track>.json"))
    return missing


# ---------------------------------------------------------------- networking helpers

def public_ip():
    with urlopen("https://api.ipify.org", timeout=5) as r:
        return r.read().decode().strip()


def firewall_rules(http_port, sim_port):
    return [("TCP", http_port), ("TCP", sim_port), ("UDP", sim_port)]


def open_firewall(http_port, sim_port):
    rules = firewall_rules(http_port, sim_port)
    lines = [f'netsh advfirewall firewall delete rule name="{FIREWALL_RULE_PREFIX}" >nul']
    lines += [f'netsh advfirewall firewall add rule name="{FIREWALL_RULE_PREFIX}" dir=in action=allow '
              f'protocol={proto} localport={port}' for proto, port in rules]
    script = Path(os.environ.get("TEMP", ".")) / "lmu_firewall.cmd"
    script.write_text("@echo off\r\n" + "\r\n".join(lines) + "\r\n", encoding="ascii")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process -FilePath '{script}' -Verb RunAs -Wait -WindowStyle Hidden"],
                   check=True, creationflags=NO_WINDOW)
    return rules


# ---------------------------------------------------------------- the running game

def game_request(path, params=None, body=None, timeout=5):
    url = f"http://127.0.0.1:{GAME_API_PORT}{path}"
    if params:
        url += "?" + urlencode(params)
    data = body.encode() if body is not None else None
    req = Request(url, data=data, method="POST" if data is not None else "GET",
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode(errors="ignore")


def game_get(path, timeout=1):
    try:
        _code, body = game_request(path, timeout=timeout)
    except HTTPError as e:
        body = e.read().decode(errors="ignore")
    except OSError:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def game_running():
    return game_get("/rest/multiplayer/steam/status") is not None


def owned_cars():
    """[(car id, label)] of the cars this player owns, from the list the game's multiplayer car
    picker uses. Cars from DLC the player does not own cannot drive (the Drive button stays off)."""
    _code, body = game_request("/rest/sessions/getAllAvailableVehicles", body="null", timeout=20)
    cars = []
    for c in json.loads(body):
        if not c.get("isOwned"):
            continue
        path = [p.strip() for p in c.get("fullPathTree", "").split(",")]
        series = path[0] if path else ""
        model = path[-1] if len(path) > 1 else ""
        cars.append((c["id"], f"{series} | {model} | {c.get('desc', c['id'])}"))
    return sorted(cars, key=lambda x: x[1].lower())


def selected_car():
    """The car the game will use in a session: (id, description, owned) or None."""
    setup = game_get("/rest/garage/UIScreen/SessionSetup", timeout=5)
    car = setup.get("selectedCar") if isinstance(setup, dict) else None
    if not isinstance(car, dict):
        return None
    return car.get("id"), car.get("desc", ""), bool(car.get("isOwned"))


def join_and_enter(host, port, password, report, car_id=None):
    """Join host:port like the game's server browser does, then enter the session like its UI does:
    pick the car (as the multiplayer car screen does with SetCurrentVehicle) and NAV_RACE_MULTIPLAYER.

    report(text, final) is called from this worker thread with progress."""
    state = game_get("/rest/multiplayer/join/state", timeout=3)
    if state not in (None, "JOIN_IDLE", "JOIN_FAILURE"):
        report(f"The game is still connected to or leaving a server ({state}). Leave it in the game "
               "(or wait a few seconds), then connect again.", True)
        return
    try:
        game_request("/rest/multiplayer/join",
                     {"host": host, "port": port, "password": password, "authentication": ""}, timeout=10)
    except HTTPError as e:
        report(f"The game refused the join (HTTP {e.code}). If it is stuck joining, restart it.", True)
        return
    except OSError as e:
        report(f"Could not reach the game: {e}", True)
        return
    last, progressed = None, False
    started = time.time()
    while time.time() - started < JOIN_TIMEOUT:
        time.sleep(0.5)
        state = game_get("/rest/multiplayer/join/state", timeout=3)
        if state != last:
            last = state
            report(f"Joining {host}:{port}: {state}", False)
        if state not in (None, "JOIN_IDLE", "JOIN_FAILURE"):
            progressed = True
        if state == "JOIN_JOINED_SERVER":
            note = ""
            if car_id:
                try:
                    game_request("/rest/garage/SetCurrentVehicle", body=json.dumps({"id": car_id}), timeout=15)
                except OSError as e:
                    note = f" Could not select the car ({e})."
            car = selected_car()
            if car and not car[2]:
                note += (f" WARNING: the selected car ({car[1]}) is from DLC you do not own, so Drive will "
                         "stay disabled. Leave, pick one of your cars in the Join tab, and connect again.")
            try:
                game_request("/navigation/action/NAV_RACE_MULTIPLAYER", body="null", timeout=15)
            except OSError as e:
                report(f"Joined, but entering the session failed ({e}). Use the game's menu.{note}", True)
                return
            driving = f" in {car[1]}" if car and car[2] else ""
            report(f"Joined {host}:{port}{driving}. The game is loading the track; press Drive in the "
                   f"garage.{note}", True)
            return
        if state in ("JOIN_FAILURE", "JOIN_IDLE") and (progressed or time.time() - started > 8):
            report(f"Join failed ({state}). Check the IP, port, password and that the server is running. "
                   "Players need the same game version as the host.", True)
            return
    report("Join timed out.", True)


def launch_via_steam(args=()):
    """Start the game through Steam so Steam, anti-cheat and the user's launch options apply."""
    root = steam_root()
    steam_exe = root / "steam.exe" if root else None
    if steam_exe and steam_exe.exists():
        subprocess.Popen([str(steam_exe), "-applaunch", APP_ID, *args], creationflags=NO_WINDOW)
    else:
        os.startfile(f"steam://run/{APP_ID}//{quote(' '.join(args))}/")


class GamePoller(threading.Thread):
    """Polls the running game's local API in the background so the UI never blocks."""

    def __init__(self):
        super().__init__(daemon=True)
        self.snapshot = {"running": False}
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            snap = {"running": False}
            if game_running():
                snap = {
                    "running": True,
                    "state": game_get("/rest/sessions/GetGameState") or {},
                    "join_state": game_get("/rest/multiplayer/join/state"),
                }
            self.snapshot = snap
            self.stop_event.wait(2)


# ---------------------------------------------------------------- the server process

class ServerProcess:
    """lmu_server.py running in the background; its output lines are collected in a queue."""

    def __init__(self):
        self.proc = None
        self.lines = queue.Queue()
        self.join_address = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, args):
        self.join_address = None
        cmd = [sys.executable, "-u", str(HERE / "lmu_server.py"), *args, "--stdin-control", "--log", str(SERVER_LOG)]
        self.proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                     creationflags=NO_WINDOW)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            m = re.search(r"ready: players join (\S+)", line)
            if m:
                self.join_address = m.group(1)
            self.lines.put(line)
        self.lines.put(f"--- server exited (code {proc.wait()})")

    def stop(self):
        """Graceful stop: players are sent back to the menu, router ports are closed (takes ~10 s)."""
        if self.running():
            try:
                self.proc.stdin.write("stop\n")
                self.proc.stdin.close()
            except OSError:
                self.proc.terminate()


# ---------------------------------------------------------------- LAN discovery

class LANBroadcaster(threading.Thread):
    def __init__(self, info_fn):
        super().__init__(daemon=True)
        self.info_fn = info_fn
        self.stop_event = threading.Event()

    def run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while not self.stop_event.is_set():
                info = self.info_fn()
                if info:
                    try:
                        s.sendto(json.dumps(info).encode(), ("<broadcast>", LAN_BROADCAST_PORT))
                    except OSError:
                        pass
                self.stop_event.wait(3)


class LANListener(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.servers = {}
        self.stop_event = threading.Event()

    def run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", LAN_BROADCAST_PORT))
        except OSError:
            return
        s.settimeout(1)
        while not self.stop_event.is_set():
            try:
                data, addr = s.recvfrom(4096)
                info = json.loads(data.decode())
            except (OSError, ValueError):
                continue
            if info.get("magic") == LAN_MAGIC:
                info["ip"] = addr[0]
                info["seen"] = time.time()
                self.servers[f"{addr[0]}:{info.get('port')}"] = info
        s.close()

    def live(self):
        now = time.time()
        return [v for v in self.servers.values() if now - v["seen"] < 12]


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "game_dir": "",
    "server_name": "LMU Local Server",
    "join_password": "",
    "max_clients": 20,
    "http_port": DEFAULT_HTTP_PORT,
    "sim_port": DEFAULT_SIM_PORT,
    "scene": "SPAWEC",
    "sessions": "P:30,Q:15,R:30",
    "upnp": "1",
    "connect_host": "",
    "connect_port": DEFAULT_HTTP_PORT,
    "car_id": "",
    "car_label": "",
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    # rFactor 2 ports from the old +host approach
    if str(settings["http_port"]) == "64297" and str(settings["sim_port"]) == "54297":
        settings["http_port"], settings["sim_port"] = DEFAULT_HTTP_PORT, DEFAULT_SIM_PORT
    if str(settings["connect_port"]) == "64297":
        settings["connect_port"] = DEFAULT_HTTP_PORT
    if not settings["game_dir"] or not (Path(settings["game_dir"]) / GAME_EXE).exists():
        settings["game_dir"] = find_game_dir()
    return settings


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- GUI

BG, PANEL, ACCENT, TEXT, OK, WARN, ERR, MUTED = (
    "#14161f", "#1d2130", "#4fc3f7", "#e6e6e6", "#81c784", "#ffb74d", "#ef5350", "#9aa0b4")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LMU Local Server Toolkit")
        self.geometry("900x700")
        self.minsize(800, 600)
        self.configure(bg=BG)
        self.settings = load_settings()
        self.server = ServerProcess()
        self.stopping_since = None
        self._public_ip = None
        self.vars = {}
        self.join_msgs = queue.Queue()
        self.poller = GamePoller()
        self.poller.start()
        self.listener = LANListener()
        self.listener.start()
        self.broadcaster = LANBroadcaster(self._broadcast_info)
        self.broadcaster.start()
        self._style()
        self._build()
        self.after(500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("TCheckbutton", background=BG, foreground=TEXT)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("Title.TLabel", foreground=ACCENT, font=("Segoe UI", 16, "bold"))
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("TLabelframe", background=BG, bordercolor="#2c3247")
        s.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        s.configure("TEntry", foreground=TEXT, insertcolor=ACCENT)
        s.configure("TCombobox", foreground=TEXT)
        s.configure("TButton", background=PANEL, foreground=TEXT, padding=(10, 4))
        s.map("TButton", background=[("active", "#2a3046")])
        s.configure("Accent.TButton", background="#0f4c75", font=("Segoe UI", 10, "bold"))
        s.map("Accent.TButton", background=[("active", "#1565a0")])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL, foreground=TEXT, padding=(14, 6))
        s.map("TNotebook.Tab", background=[("selected", "#0f4c75")], foreground=[("selected", ACCENT)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=24)
        s.configure("Treeview.Heading", background="#0f4c75", foreground=ACCENT)

    def _var(self, key):
        if key not in self.vars:
            self.vars[key] = tk.StringVar(value=str(self.settings.get(key, "")))
        return self.vars[key]

    def _row(self, parent, label, key, show=None, width=None, browse=None, hint=None):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text=label, width=18).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self._var(key), show=show or "", width=width or 0)
        e.pack(side=tk.LEFT, fill=tk.X if not width else tk.NONE, expand=not width)
        if browse:
            ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT, padx=(6, 0))
        if hint:
            ttk.Label(row, text=hint, style="Muted.TLabel").pack(side=tk.LEFT, padx=(8, 0))
        return e

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=14, pady=(12, 4))
        ttk.Label(top, text="LMU Local Server Toolkit", style="Title.TLabel").pack(side=tk.LEFT)
        self.status = tk.Label(top, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.status.pack(side=tk.RIGHT)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 14))
        nb.add(self._host_tab(nb), text="Host")
        nb.add(self._share_tab(nb), text="Share / Ports")
        nb.add(self._join_tab(nb), text="Join")
        nb.add(self._lan_tab(nb), text="LAN Browser")

    def _host_tab(self, nb):
        f = ttk.Frame(nb, padding=12)

        srv = ttk.LabelFrame(f, text="Server", padding=10)
        srv.pack(fill=tk.X)
        self._row(srv, "Server name", "server_name")
        self._row(srv, "Join password", "join_password", show="*", hint="empty = no password")
        self._row(srv, "Max players", "max_clients", width=8)
        self._row(srv, "HTTP port (join)", "http_port", width=8)
        self._row(srv, "Simulation port", "sim_port", width=8)
        trow = ttk.Frame(srv)
        trow.pack(fill=tk.X, pady=3)
        ttk.Label(trow, text="Track", width=18).pack(side=tk.LEFT)
        ttk.Combobox(trow, textvariable=self._var("scene"), values=track_scenes(), width=30).pack(side=tk.LEFT)
        self._row(srv, "Sessions", "sessions",
                  hint="minutes: P practice, Q qualifying, W warmup, R race; repeats after the last")
        self.upnp_var = tk.BooleanVar(value=str(self.settings.get("upnp")) == "1")
        ttk.Checkbutton(srv, text="Open the ports on my router automatically (UPnP) - needed for internet play",
                        variable=self.upnp_var).pack(anchor="w", pady=(6, 0))
        self._row(srv, "Game folder", "game_dir", browse=self._browse_game)

        btns = ttk.Frame(f)
        btns.pack(fill=tk.X, pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start server", style="Accent.TButton", command=self._toggle_server)
        self.start_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="Join my server", command=self._join_own).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Start LMU", command=self._start_game).pack(side=tk.LEFT)
        ttk.Button(btns, text="Open server log", command=self._open_log).pack(side=tk.LEFT, padx=6)

        self.server_state = tk.Label(f, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10),
                                     anchor="w", justify=tk.LEFT, wraplength=820)
        self.server_state.pack(fill=tk.X, pady=(10, 4))

        out = ttk.LabelFrame(f, text="Server output", padding=6)
        out.pack(fill=tk.BOTH, expand=True)
        self.out_text = tk.Text(out, height=10, bg=PANEL, fg=TEXT, font=("Consolas", 9),
                                relief=tk.FLAT, state=tk.DISABLED, wrap=tk.NONE)
        self.out_text.pack(fill=tk.BOTH, expand=True)
        return f

    def _share_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        box = ttk.LabelFrame(f, text="What to give your friends", padding=10)
        box.pack(fill=tk.X)
        self.share_text = tk.Text(box, height=7, bg=PANEL, fg=TEXT, insertbackground=ACCENT,
                                  font=("Consolas", 10), relief=tk.FLAT, wrap=tk.WORD)
        self.share_text.pack(fill=tk.X)
        b = ttk.Frame(box)
        b.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(b, text="Copy", command=self._copy_share).pack(side=tk.LEFT)
        ttk.Button(b, text="Look up public IP", command=self._lookup_public_ip).pack(side=tk.LEFT, padx=6)

        fw = ttk.LabelFrame(f, text="Ports", padding=10)
        fw.pack(fill=tk.X, pady=10)
        self.ports_label = ttk.Label(fw, text="", justify=tk.LEFT, font=("Consolas", 10))
        self.ports_label.pack(fill=tk.X)
        ttk.Label(fw, style="Muted.TLabel", wraplength=820, justify=tk.LEFT, text=(
            "Only the host needs open ports. With the UPnP box ticked the server asks your router to forward "
            "them while it runs and closes them again when it stops; the server output says whether that "
            "worked. Without UPnP, forward these ports to this PC's LAN IP by hand, or put everyone on a "
            "virtual LAN such as Tailscale or ZeroTier and share that IP instead.\n"
            "Windows Firewall: Windows usually asks the first time the server starts; if nobody can connect, "
            "add the rules with the button below."
        )).pack(fill=tk.X, pady=(6, 6))
        ttk.Button(fw, text="Add Windows Firewall rules (asks for admin)",
                   command=self._open_firewall).pack(anchor="w")
        self._refresh_share()
        return f

    def _join_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        box = ttk.LabelFrame(f, text="Direct connect (LMU must be running, at the main menu)", padding=10)
        box.pack(fill=tk.X)
        self._row(box, "Server IP", "connect_host")
        self._row(box, "HTTP port", "connect_port", width=8)
        self.join_pass = tk.StringVar()
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Password", width=18).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.join_pass, show="*").pack(side=tk.LEFT, fill=tk.X, expand=True)
        crow = ttk.Frame(box)
        crow.pack(fill=tk.X, pady=3)
        ttk.Label(crow, text="Car", width=18).pack(side=tk.LEFT)
        self.car_box = ttk.Combobox(crow, textvariable=self._var("car_label"), state="readonly")
        self.car_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.car_box.bind("<<ComboboxSelected>>", lambda _e: self._car_selected())
        ttk.Button(crow, text="Load my cars", command=self._load_cars).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(box, text="Connect", style="Accent.TButton", command=self._join).pack(anchor="w", pady=(8, 0))
        self.join_state = ttk.Label(box, text="", style="Muted.TLabel", wraplength=820, justify=tk.LEFT)
        self.join_state.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(f, style="Muted.TLabel", wraplength=820, justify=tk.LEFT, text=(
            "Only cars you own are listed: with a DLC car you don't own, the game keeps Drive disabled. "
            "The car is also used by Join my server on the Host tab. "
            "Everyone needs the same game version as the host.")).pack(fill=tk.X, pady=(10, 0))
        self.cars = []
        return f

    def _load_cars(self, quiet=False):
        if not self.poller.snapshot.get("running") and not game_running():
            if not quiet:
                self.join_state.configure(text="Start LMU first; the car list comes from the game.")
            return
        self.join_state.configure(text="Loading your cars from the game...")

        def work():
            try:
                cars = owned_cars()
            except (OSError, ValueError, KeyError) as e:
                self.join_msgs.put(f"Could not load the car list: {e}")
                return
            self.after(0, lambda: self._set_cars(cars))

        threading.Thread(target=work, daemon=True).start()

    def _set_cars(self, cars):
        self.cars = cars
        self.car_box["values"] = [label for _id, label in cars]
        ids = [car_id for car_id, _label in cars]
        saved = self.settings.get("car_id")
        if saved in ids:
            self._var("car_label").set(cars[ids.index(saved)][1])
        elif cars and self._var("car_label").get() not in self.car_box["values"]:
            self._var("car_label").set("")
        self.join_state.configure(text=f"{len(cars)} cars you own. Pick one, then connect.")

    def _car_selected(self):
        label = self._var("car_label").get()
        for car_id, car_label in self.cars:
            if car_label == label:
                self.settings["car_id"] = car_id
                self.settings["car_label"] = label
                save_settings(self.settings)
                return

    def _lan_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        bar = ttk.Frame(f)
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Servers announced by this toolkit on your network",
                  style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(bar, text="Join selected", style="Accent.TButton", command=self._join_selected).pack(side=tk.RIGHT)
        cols = ("name", "ip", "port", "max players", "password")
        self.tree = ttk.Treeview(f, columns=cols, show="headings")
        for c, w in zip(cols, (300, 150, 90, 100, 90)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w)
        self.tree.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.tree.bind("<Double-1>", lambda _e: self._join_selected())
        return f

    # ---- settings

    def _int(self, key, lo, hi):
        try:
            v = int(self._var(key).get())
        except ValueError:
            raise ValueError(f"{key.replace('_', ' ')} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{key.replace('_', ' ')} must be between {lo} and {hi}")
        return v

    def _collect(self):
        s = {k: v.get().strip() for k, v in self.vars.items()}
        s["max_clients"] = self._int("max_clients", 1, 104)
        s["http_port"] = self._int("http_port", 1025, 65535)
        s["sim_port"] = self._int("sim_port", 1025, 65535)
        s["connect_port"] = self._int("connect_port", 1, 65535)
        s["upnp"] = "1" if self.upnp_var.get() else "0"
        if s["http_port"] == s["sim_port"]:
            raise ValueError("HTTP port and simulation port must differ")
        if not re.fullmatch(r"\s*[PQWRpqwr]:\d+(\.\d+)?(\s*,\s*[PQWRpqwr]:\d+(\.\d+)?)*\s*", s["sessions"]):
            raise ValueError("Sessions look like P:30,Q:15,R:30 (letter:minutes)")
        if not (Path(s["game_dir"]) / GAME_EXE).exists():
            raise ValueError(f"{GAME_EXE} not found in the game folder")
        self.settings.update(s)
        save_settings(self.settings)
        return self.settings

    def _browse_game(self):
        d = filedialog.askdirectory(title="Select the Le Mans Ultimate install folder")
        if d:
            self._var("game_dir").set(d)

    # ---- hosting

    def _server_args(self, s):
        args = ["--game-dir", s["game_dir"], "--name", s["server_name"], "--http", str(s["http_port"]),
                "--sim", str(s["sim_port"]), "--max-players", str(s["max_clients"]),
                "--scene", s["scene"].upper(), "--sessions", s["sessions"].replace(" ", "")]
        if s["join_password"]:
            args += ["--password", s["join_password"]]
        if s["upnp"] == "1":
            args.append("--upnp")
        return args

    def _toggle_server(self):
        if self.stopping_since:
            return
        if self.server.running():
            self.server.stop()
            self.stopping_since = time.time()
            return
        try:
            s = self._collect()
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return
        missing = server_data_missing()
        if missing:
            messagebox.showerror("Game data missing", (
                "The server needs the game's rules and Balance of Performance files, extracted from your own "
                "install (they are not shipped with this tool):\n\n" + "\n".join(missing)))
            return
        if s["scene"].upper() not in track_scenes():
            messagebox.showerror("Track", f"No BoP file for track '{s['scene']}'. Pick one from the list.")
            return
        self._clear_output()
        try:
            self.server.start(self._server_args(s))
        except OSError as e:
            messagebox.showerror("Could not start the server", str(e))

    def _join_own(self):
        if not self.server.running():
            messagebox.showinfo("Join my server", "Start the server first.")
            return
        self._join("127.0.0.1", self.settings["http_port"], self.settings.get("join_password", ""))

    def _start_game(self):
        if self.poller.snapshot.get("running"):
            messagebox.showinfo("Start LMU", "Le Mans Ultimate is already running.")
            return
        try:
            launch_via_steam()
        except OSError as e:
            messagebox.showerror("Could not start through Steam", str(e))

    def _server_status(self):
        if self.stopping_since:
            return "Stopping: sending players back to the menu and closing router ports...", WARN
        if not self.server.running():
            return "Server not running.", MUTED
        addr = self.server.join_address
        if not addr:
            return "Starting...", WARN
        return f"Server running. Players join {addr}  (this PC: Join my server).", OK

    def _clear_output(self):
        self.out_text.configure(state=tk.NORMAL)
        self.out_text.delete("1.0", tk.END)
        self.out_text.configure(state=tk.DISABLED)

    def _drain_output(self):
        lines = []
        while True:
            try:
                lines.append(self.server.lines.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.out_text.configure(state=tk.NORMAL)
        for line in lines:
            if line.startswith("    "):  # hex dumps
                continue
            self.out_text.insert(tk.END, line + "\n")
        excess = int(self.out_text.index("end-1c").split(".")[0]) - 2000
        if excess > 0:
            self.out_text.delete("1.0", f"{excess}.0")
        self.out_text.configure(state=tk.DISABLED)
        self.out_text.see(tk.END)
        self._refresh_share()

    def _broadcast_info(self):
        if not self.server.running() or self.stopping_since:
            return None
        return {"magic": LAN_MAGIC, "name": self.settings["server_name"], "port": self.settings["http_port"],
                "maxPlayers": self.settings["max_clients"], "password": bool(self.settings["join_password"])}

    def _open_log(self):
        if SERVER_LOG.exists():
            os.startfile(SERVER_LOG)
        else:
            messagebox.showinfo("No log yet", f"{SERVER_LOG} does not exist yet.")

    # ---- sharing

    def _refresh_share(self):
        try:
            http = int(self._var("http_port").get())
            sim = int(self._var("sim_port").get())
        except ValueError:
            return
        lan = upnp.default_lan_ip()
        public = None
        if self.server.join_address and not self.server.join_address.startswith(lan + ":"):
            public = self.server.join_address.rsplit(":", 1)[0]
        public = public or self._public_ip
        lines = [f"Server:   {self._var('server_name').get()}",
                 f"Same network:  {lan}  port {http}"]
        if public:
            lines.append(f"Internet:      {public}  port {http}")
        else:
            lines.append("Internet:      start the server with UPnP, or click 'Look up public IP'")
        lines.append("Password:      " + ("(ask the host)" if self._var("join_password").get() else "none"))
        lines.append("Join with this toolkit's Join tab, or connect-to-server.bat IP PORT [PASSWORD].")
        self.share_text.delete("1.0", tk.END)
        self.share_text.insert(tk.END, "\n".join(lines))
        self.ports_label.configure(text=(
            f"TCP {http}        HTTP port: players connect here first\n"
            f"TCP + UDP {sim}  simulation port: the race itself"))

    def _copy_share(self):
        self.clipboard_clear()
        self.clipboard_append(self.share_text.get("1.0", tk.END).strip())

    def _lookup_public_ip(self):
        try:
            self._public_ip = public_ip()
        except OSError as e:
            messagebox.showerror("Lookup failed", str(e))
            return
        self._refresh_share()

    def _open_firewall(self):
        try:
            s = self._collect()
            rules = open_firewall(s["http_port"], s["sim_port"])
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return
        except subprocess.CalledProcessError:
            messagebox.showerror("Firewall", "The admin prompt was cancelled or failed.")
            return
        messagebox.showinfo("Firewall", "Added inbound rules:\n" + "\n".join(f"{p} {r}" for p, r in rules))

    # ---- joining

    def _join(self, host=None, port=None, password=None):
        host = host or self._var("connect_host").get().strip()
        port = port or self._var("connect_port").get().strip()
        password = self.join_pass.get() if password is None else password
        if not host or not port:
            self.join_state.configure(text="Enter the server IP and port.")
            return
        if not self.poller.snapshot.get("running") and not game_running():
            msg = "LMU is not running. Start the game, wait for the main menu, then connect."
            self.join_state.configure(text=msg)
            self.server_state.configure(text=msg, fg=WARN)
            return
        try:
            self._collect()
        except ValueError:
            pass
        car_id = self.settings.get("car_id") or None
        self.join_state.configure(text=f"Joining {host}:{port}...")
        threading.Thread(target=join_and_enter, args=(host, port, password,
                                                      lambda text, final: self.join_msgs.put(text), car_id),
                         daemon=True).start()

    def _join_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        _name, ip, port, *_ = self.tree.item(sel[0], "values")
        self._var("connect_host").set(ip)
        self._var("connect_port").set(port)
        self._join(ip, port)

    # ---- loop

    def _tick(self):
        snap = self.poller.snapshot
        if snap.get("running") and not self.cars and not getattr(self, "_cars_requested", False):
            self._cars_requested = True
            self._load_cars(quiet=True)
        if snap.get("running"):
            st = snap.get("state") if isinstance(snap.get("state"), dict) else {}
            self.status.configure(text=f"LMU running ({st.get('gameState', '?')}, {snap.get('join_state')})", fg=OK)
        else:
            self.status.configure(text="LMU not running", fg=MUTED)

        self._drain_output()
        if self.stopping_since and (not self.server.running() or time.time() - self.stopping_since > 30):
            if self.server.running():
                self.server.proc.kill()
            self.stopping_since = None
        text, color = self._server_status()
        self.server_state.configure(text=text, fg=color)
        self.start_btn.configure(text="Stop server" if self.server.running() else "Start server")

        while True:
            try:
                self.join_state.configure(text=self.join_msgs.get_nowait())
            except queue.Empty:
                break

        selected = {self.tree.item(i, "values")[1:3] for i in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        for srv in self.listener.live():
            values = (srv.get("name", "?"), srv["ip"], srv.get("port", "?"),
                      srv.get("maxPlayers", "?"), "yes" if srv.get("password") else "no")
            item = self.tree.insert("", tk.END, values=values)
            if (str(values[1]), str(values[2])) in selected:
                self.tree.selection_add(item)
        self.after(1000, self._tick)

    def _on_close(self):
        try:
            self._collect()
        except ValueError:
            pass
        if self.server.running():
            if not messagebox.askyesno("Quit", "Stop the server and quit?"):
                return
            self.server.stop()
            self.server_state.configure(text="Stopping the server, the window closes when it is done...", fg=WARN)
            self._close_when_stopped(time.time())
            return
        self._shutdown()

    def _close_when_stopped(self, since):
        if self.server.running() and time.time() - since < 30:
            self._drain_output()
            self.after(500, lambda: self._close_when_stopped(since))
            return
        if self.server.running():
            self.server.proc.kill()
        self._shutdown()

    def _shutdown(self):
        for t in (self.poller, self.listener, self.broadcaster):
            t.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--join":
        # command-line join, used by connect-to-server.bat:
        #   --join HOST [PORT] [PASSWORD] [--car CAR_ID]
        # (the car defaults to the one picked in the toolkit's Join tab)
        args = sys.argv[2:]
        car = None
        if "--car" in args:
            i = args.index("--car")
            car = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
        host = args[0]
        port = args[1] if len(args) > 1 else DEFAULT_HTTP_PORT
        password = args[2] if len(args) > 2 else ""
        car = car or load_settings().get("car_id") or None
        if not game_running():
            sys.exit("Le Mans Ultimate is not running. Start the game, wait for the main menu, then try again.")
        join_and_enter(host, port, password, lambda text, final: print(text, flush=True), car)
    else:
        App().mainloop()
