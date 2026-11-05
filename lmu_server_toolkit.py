"""LMU Local Server Toolkit - host and join Le Mans Ultimate races on your own machine."""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

APP_ID = "2399420"
GAME_EXE = "Le Mans Ultimate.exe"
GAME_API_PORT = 6397
LAN_BROADCAST_PORT = 5399
LAN_MAGIC = "LMU_LAN_SERVER"
# rFactor 2 defaults; the values in the profile's Multiplayer.JSON win when present.
DEFAULT_SIM_PORT = 54297
DEFAULT_HTTP_PORT = 64297
SETTINGS_FILE = Path(__file__).with_name("toolkit_settings.json")
FIREWALL_RULE_PREFIX = "LMU Local Server"
LAUNCH_TIMEOUT = 180
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


def find_profiles(game_dir):
    user_data = Path(game_dir) / "UserData"
    if not user_data.exists():
        return []
    return sorted(p.parent.name for p in user_data.glob("*/Multiplayer.JSON"))


def multiplayer_json_path(game_dir, profile):
    return Path(game_dir) / "UserData" / profile / "Multiplayer.JSON"


# ---------------------------------------------------------------- Multiplayer.JSON

def read_mp_values(path, keys):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    found = {}
    for section in data.values():
        if isinstance(section, dict):
            for k in keys:
                if k in section:
                    found[k] = section[k]
    return found


def patch_mp_json(path, values):
    """Set existing keys only, keeping the game's own structure. Returns keys not found."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = set(values)
    for section in data.values():
        if isinstance(section, dict):
            for k, v in values.items():
                if k in section:
                    section[k] = v
                    missing.discard(k)
    backup = path.with_suffix(".JSON.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return sorted(missing)


# ---------------------------------------------------------------- networking helpers

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def public_ip():
    with urlopen("https://api.ipify.org", timeout=5) as r:
        return r.read().decode().strip()


def tcp_listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def firewall_script(http_port, sim_port, max_clients):
    sim_range = f"{sim_port}-{sim_port + max_clients}"
    rules = [("TCP", http_port), ("UDP", http_port), ("TCP", sim_range), ("UDP", sim_range)]
    lines = [f'netsh advfirewall firewall delete rule name="{FIREWALL_RULE_PREFIX}" >nul']
    lines += [f'netsh advfirewall firewall add rule name="{FIREWALL_RULE_PREFIX}" dir=in action=allow '
              f'protocol={proto} localport={ports}' for proto, ports in rules]
    return lines, rules


def open_firewall(http_port, sim_port, max_clients):
    lines, rules = firewall_script(http_port, sim_port, max_clients)
    script = Path(os.environ.get("TEMP", ".")) / "lmu_firewall.cmd"
    script.write_text("@echo off\r\n" + "\r\n".join(lines) + "\r\n", encoding="ascii")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process -FilePath '{script}' -Verb RunAs -Wait -WindowStyle Hidden"],
                   check=True, creationflags=NO_WINDOW)
    return rules


# ---------------------------------------------------------------- the running game

def game_api(path, params=None, timeout=5):
    url = f"http://127.0.0.1:{GAME_API_PORT}{path}"
    if params:
        url += "?" + urlencode(params)
    with urlopen(Request(url, headers={"Content-Type": "application/json"}), timeout=timeout) as r:
        return r.status, r.read().decode(errors="ignore")


def game_get(path, timeout=1):
    try:
        _code, body = game_api(path, timeout=timeout)
    except HTTPError as e:
        # e.g. GetGameState answers 503 with a JSON explanation when no session is loaded
        body = e.read().decode(errors="ignore")
    except OSError:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def game_running():
    return game_get("/rest/multiplayer/steam/status") is not None


def join_server(host, port, password=""):
    return game_api("/rest/multiplayer/join",
                    {"host": host, "port": port, "password": password, "authentication": ""},
                    timeout=10)


def send_chat(message):
    req = Request(f"http://127.0.0.1:{GAME_API_PORT}/rest/chat/", data=message.encode("utf-8"),
                  headers={"Content-Type": "text/plain"}, method="POST")
    with urlopen(req, timeout=3) as r:
        return r.status


def game_pids():
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    return [int(row.split('","')[1]) for row in out.splitlines() if row.startswith(f'"{GAME_EXE}"')]


def close_game(timeout=30):
    """Ask the game to close like clicking the window's X; returns True once it has exited."""
    for pid in game_pids():
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not game_pids():
            return True
        time.sleep(1)
    return False


def launch_via_steam(args):
    """Start the game through Steam so Steam, anti-cheat and the user's launch options apply."""
    root = steam_root()
    steam_exe = root / "steam.exe" if root else None
    if steam_exe and steam_exe.exists():
        subprocess.Popen([str(steam_exe), "-applaunch", APP_ID, *args], creationflags=NO_WINDOW)
    else:
        os.startfile(f"steam://run/{APP_ID}//{quote(' '.join(args))}/")


class GamePoller(threading.Thread):
    """Polls the running game's local API in the background so the UI never blocks."""

    def __init__(self, http_port_fn):
        super().__init__(daemon=True)
        self.http_port_fn = http_port_fn
        self.snapshot = {"running": False}
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            snap = {"running": False}
            cmd = game_get("/rest/options/commandline")
            if cmd is not None:
                line = cmd.get("commandLine", "") if isinstance(cmd, dict) else str(cmd)
                snap = {
                    "running": True,
                    "command_line": line,
                    "hosting_args": "+host" in line.lower(),
                    "eac": game_get("/rest/profile/eacActive"),
                    "state": game_get("/rest/sessions/GetGameState") or {},
                    "join_state": game_get("/rest/multiplayer/join/state"),
                    "chat": game_get("/rest/chat/") or [],
                }
            port = self.http_port_fn()
            snap["listening"] = bool(port) and tcp_listening(port)
            self.snapshot = snap
            self.stop_event.wait(2)


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
    "profile": "player",
    "server_name": "LMU Local Server",
    "join_password": "",
    "admin_password": "",
    "max_clients": 16,
    "sim_port": DEFAULT_SIM_PORT,
    "http_port": DEFAULT_HTTP_PORT,
    "extra_args": "+host",
    "connect_host": "",
    "connect_port": DEFAULT_HTTP_PORT,
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
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
        self.geometry("880x660")
        self.minsize(780, 580)
        self.configure(bg=BG)
        self.settings = load_settings()
        self.launch_requested_at = None
        self.busy = False
        self._public_ip = None
        self._chat_seen = None
        self.vars = {}
        self.poller = GamePoller(lambda: self.settings.get("http_port"))
        self.poller.start()
        self.listener = LANListener()
        self.listener.start()
        self.broadcaster = LANBroadcaster(self._broadcast_info)
        self.broadcaster.start()
        self._style()
        self._build()
        self._load_from_profile(quiet=True)
        self.after(500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, font=("Segoe UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT)
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

    def _row(self, parent, label, key, show=None, width=None, browse=None):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text=label, width=18).pack(side=tk.LEFT)
        e = ttk.Entry(row, textvariable=self._var(key), show=show or "", width=width or 0)
        e.pack(side=tk.LEFT, fill=tk.X if not width else tk.NONE, expand=not width)
        if browse:
            ttk.Button(row, text="Browse", command=browse).pack(side=tk.LEFT, padx=(6, 0))
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
        nb.add(self._live_tab(nb), text="Live Game")
        nb.add(self._share_tab(nb), text="Share / Ports")
        nb.add(self._join_tab(nb), text="Join")
        nb.add(self._lan_tab(nb), text="LAN Browser")

    def _host_tab(self, nb):
        f = ttk.Frame(nb, padding=12)

        game = ttk.LabelFrame(f, text="Game", padding=10)
        game.pack(fill=tk.X)
        self._row(game, "Install folder", "game_dir", browse=self._browse_game)
        prow = ttk.Frame(game)
        prow.pack(fill=tk.X, pady=3)
        ttk.Label(prow, text="Profile", width=18).pack(side=tk.LEFT)
        self.profile_box = ttk.Combobox(prow, textvariable=self._var("profile"), width=20)
        self.profile_box.pack(side=tk.LEFT)
        ttk.Button(prow, text="Reload from profile", command=self._load_from_profile).pack(side=tk.LEFT, padx=6)
        self.profile_note = ttk.Label(game, text="", style="Muted.TLabel", wraplength=800, justify=tk.LEFT)
        self.profile_note.pack(fill=tk.X, pady=(4, 0))

        srv = ttk.LabelFrame(f, text="Server settings (written to the profile's Multiplayer.JSON)", padding=10)
        srv.pack(fill=tk.X, pady=10)
        self._row(srv, "Server name", "server_name")
        self._row(srv, "Join password", "join_password", show="*")
        self._row(srv, "Admin password", "admin_password", show="*")
        self._row(srv, "Max clients", "max_clients", width=8)
        self._row(srv, "HTTP port (join)", "http_port", width=8)
        self._row(srv, "Simulation port", "sim_port", width=8)
        self._row(srv, "Launch arguments", "extra_args")

        btns = ttk.Frame(f)
        btns.pack(fill=tk.X)
        self.start_btn = ttk.Button(btns, text="Start server", style="Accent.TButton", command=self._toggle_server)
        self.start_btn.pack(side=tk.LEFT)
        ttk.Button(btns, text="Save settings only", command=self._apply_settings).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Open log folder", command=self._open_logs).pack(side=tk.LEFT)

        self.server_state = tk.Label(f, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10),
                                     anchor="w", justify=tk.LEFT, wraplength=800)
        self.server_state.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(f, style="Muted.TLabel", wraplength=800, justify=tk.LEFT, text=(
            "The game is started through Steam, so your Steam launch options also apply. Steam may ask you to "
            "confirm the extra arguments the first time. If you put +host in your Steam launch options instead, "
            "the toolkit detects it and treats the running game as the server.")).pack(fill=tk.X, pady=(6, 0))
        return f

    def _live_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        info = ttk.LabelFrame(f, text="Running game", padding=10)
        info.pack(fill=tk.X)
        self.live_info = ttk.Label(info, text="", justify=tk.LEFT, font=("Consolas", 10))
        self.live_info.pack(fill=tk.X)
        ttk.Button(info, text="Restart game as host", command=self._restart_as_host).pack(anchor="w", pady=(8, 0))

        chat = ttk.LabelFrame(f, text="Chat and admin commands (in a multiplayer session)", padding=10)
        chat.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.chat_log = tk.Text(chat, height=10, bg=PANEL, fg=TEXT, font=("Consolas", 10),
                                relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
        self.chat_log.pack(fill=tk.BOTH, expand=True)
        row = ttk.Frame(chat)
        row.pack(fill=tk.X, pady=(8, 0))
        self.chat_entry = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.chat_entry)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _e: self._send_chat())
        ttk.Button(row, text="Send", command=self._send_chat).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(chat, style="Muted.TLabel", text=(
            "Admin: /admin <password> to log in, /admin to log out, /apwd <new> to change the admin password."
        )).pack(anchor="w", pady=(6, 0))
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
        ttk.Label(fw, style="Muted.TLabel", wraplength=800, justify=tk.LEFT, text=(
            "Same network: open the Windows Firewall rules below and you are done.\n"
            "Over the internet: also forward the same ports on your router to this PC's LAN IP, "
            "or use a virtual LAN such as Tailscale or ZeroTier and share that IP instead (no router changes)."
        )).pack(fill=tk.X, pady=(6, 6))
        ttk.Button(fw, text="Add Windows Firewall rules (asks for admin)",
                   command=self._open_firewall).pack(anchor="w")
        self._refresh_share()
        return f

    def _join_tab(self, nb):
        f = ttk.Frame(nb, padding=12)
        box = ttk.LabelFrame(f, text="Direct connect (LMU must be running)", padding=10)
        box.pack(fill=tk.X)
        self._row(box, "Server IP", "connect_host")
        self._row(box, "HTTP port", "connect_port", width=8)
        self.join_pass = tk.StringVar()
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text="Password", width=18).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.join_pass, show="*").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(box, text="Connect", style="Accent.TButton", command=self._join).pack(anchor="w", pady=(8, 0))
        self.join_state = ttk.Label(box, text="", style="Muted.TLabel")
        self.join_state.pack(fill=tk.X, pady=(6, 0))

        alt = ttk.LabelFrame(f, text="Alternative: Steam launch options", padding=10)
        alt.pack(fill=tk.X, pady=10)
        ttk.Label(alt, justify=tk.LEFT, text=(
            "Steam > right-click Le Mans Ultimate > Properties > Launch Options:\n"
            "    +connect HOST_IP:HTTP_PORT\n"
            "    +connect HOST_IP:HTTP_PORT +password YOUR_PASSWORD"
        ), font=("Consolas", 10)).pack(anchor="w")
        return f

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
        s["max_clients"] = self._int("max_clients", 1, 120)
        s["http_port"] = self._int("http_port", 1025, 65535)
        s["sim_port"] = self._int("sim_port", 1025, 65535 - s["max_clients"])
        s["connect_port"] = self._int("connect_port", 1, 65535)
        if not (Path(s["game_dir"]) / GAME_EXE).exists():
            raise ValueError(f"{GAME_EXE} not found in the install folder")
        self.settings.update(s)
        save_settings(self.settings)
        return self.settings

    def _mp_values(self, s):
        values = {
            "Default Game Name": s["server_name"],
            "Join Password": s["join_password"],
            "Max Clients": s["max_clients"],
            "HTTP Server Port": s["http_port"],
            "Simulation Port": s["sim_port"],
        }
        if s["admin_password"]:
            values["Admin Password"] = s["admin_password"]
        return values

    def _apply_settings(self, quiet=False):
        try:
            s = self._collect()
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return None
        path = multiplayer_json_path(s["game_dir"], s["profile"])
        if not path.exists():
            self.profile_note.configure(text=(
                f"No Multiplayer.JSON yet at {path}. Launch LMU once normally so it creates your profile, "
                "then come back. Settings are saved in the toolkit until then."))
            if not quiet:
                messagebox.showwarning("Profile not created yet",
                                       "Launch Le Mans Ultimate once so it creates its profile files, then try again.")
            return s
        try:
            missing = patch_mp_json(path, self._mp_values(s))
        except (OSError, ValueError) as e:
            messagebox.showerror("Could not update Multiplayer.JSON", str(e))
            return None
        note = f"Updated {path} (original backed up as Multiplayer.JSON.bak)."
        if missing:
            note += f" Not present in this game version: {', '.join(missing)}."
        self.profile_note.configure(text=note)
        self._refresh_share()
        return s

    def _load_from_profile(self, quiet=False):
        game_dir = self._var("game_dir").get()
        profiles = find_profiles(game_dir) if game_dir else []
        self.profile_box["values"] = profiles
        if profiles and self._var("profile").get() not in profiles:
            self._var("profile").set(profiles[0])
        path = multiplayer_json_path(game_dir, self._var("profile").get()) if game_dir else None
        if not path or not path.exists():
            self.profile_note.configure(text=(
                "No Multiplayer.JSON found yet. Launch LMU once normally so it creates "
                "UserData\\<profile>\\Multiplayer.JSON; until then the rFactor 2 default ports are shown."))
            return
        try:
            v = read_mp_values(path, ["Default Game Name", "Max Clients", "HTTP Server Port", "Simulation Port"])
        except (OSError, ValueError) as e:
            if not quiet:
                messagebox.showerror("Could not read Multiplayer.JSON", str(e))
            return
        for json_key, key in (("Default Game Name", "server_name"), ("Max Clients", "max_clients"),
                              ("HTTP Server Port", "http_port"), ("Simulation Port", "sim_port")):
            if v.get(json_key) not in (None, ""):
                self._var(key).set(str(v[json_key]))
        self.profile_note.configure(text=f"Loaded settings from {path}")
        self._refresh_share()

    def _browse_game(self):
        d = filedialog.askdirectory(title="Select the Le Mans Ultimate install folder")
        if d:
            self._var("game_dir").set(d)
            self._load_from_profile()

    # ---- hosting

    def _hosting(self):
        snap = self.poller.snapshot
        return snap.get("running") and snap.get("hosting_args")

    def _toggle_server(self):
        if self.busy:
            return
        snap = self.poller.snapshot
        if self._hosting():
            if messagebox.askyesno("Stop server", "Close Le Mans Ultimate to stop the server?"):
                self._close_then(None)
        elif snap.get("running"):
            self._restart_as_host()
        else:
            self._launch_host()

    def _restart_as_host(self):
        if self.busy:
            return
        if not self.poller.snapshot.get("running"):
            self._launch_host()
            return
        if messagebox.askyesno("Restart as host", "Hosting can only be turned on when the game starts.\n\n"
                                                  "Close Le Mans Ultimate and start it again through Steam as a host?"):
            self._close_then(self._launch_host)

    def _close_then(self, next_step):
        self.busy = True
        self.server_state.configure(text="Closing Le Mans Ultimate...", fg=WARN)

        def work():
            closed = close_game()
            self.after(0, lambda: self._after_close(closed, next_step))

        threading.Thread(target=work, daemon=True).start()

    def _after_close(self, closed, next_step):
        self.busy = False
        if not closed:
            messagebox.showwarning("Game still running", "The game did not close. Close it yourself, then try again.")
            return
        if next_step:
            next_step()

    def _launch_host(self):
        s = self._apply_settings(quiet=True)
        if not s:
            return
        # No +profile=...: Steam quotes "key=value" args, the game then looks for a profile named `player"` and crashes.
        args = s["extra_args"].split() + ["+maxplayers", str(s["max_clients"])]
        if any("=" in a for a in args):
            if not messagebox.askyesno("Launch arguments", "Arguments containing '=' get quoted by Steam and can "
                                                           "break the launch (this crashed the game before). Continue?"):
                return
        if s["join_password"]:
            args += ["+password", s["join_password"]]
        try:
            launch_via_steam(args)
        except OSError as e:
            messagebox.showerror("Could not start through Steam", str(e))
            return
        self.launch_requested_at = time.time()
        shown = ["***" if i and args[i - 1] == "+password" else a for i, a in enumerate(args)]
        self.server_state.configure(text=f"Asked Steam to start the game with: {' '.join(shown)}", fg=WARN)

    def _server_status(self):
        snap = self.poller.snapshot
        port = self.settings["http_port"]
        if self.busy:
            return "Closing Le Mans Ultimate...", WARN
        if snap.get("running"):
            self.launch_requested_at = None
            if not snap.get("hosting_args"):
                return ("Game is running but was not started with +host, so it is not hosting. "
                        "Click Start server to restart it as a host."), MUTED
            if snap.get("listening"):
                return f"Hosting - accepting connections on TCP {port}. Friends can join now.", OK
            return (f"Game started with +host but nothing is listening on TCP {port} yet. Wait for the track "
                    "to load, or start a session in-game. If it never turns green, check the log folder."), WARN
        if self.launch_requested_at:
            waited = int(time.time() - self.launch_requested_at)
            if waited < LAUNCH_TIMEOUT:
                return f"Waiting for the game to start through Steam ({waited}s)...", WARN
            self.launch_requested_at = None
            return ("The game did not start. Check Steam for a prompt to allow the launch arguments, "
                    "then try again."), ERR
        return "Game not running.", MUTED

    def _broadcast_info(self):
        snap = self.poller.snapshot
        if not (snap.get("running") and snap.get("hosting_args") and snap.get("listening")):
            return None
        return {"magic": LAN_MAGIC, "name": self.settings["server_name"], "port": self.settings["http_port"],
                "maxPlayers": self.settings["max_clients"], "password": bool(self.settings["join_password"])}

    def _open_logs(self):
        logs = Path(self._var("game_dir").get()) / "UserData" / "Log"
        if logs.exists():
            os.startfile(logs)
        else:
            messagebox.showinfo("No logs yet", f"{logs} does not exist yet.")

    # ---- live game

    def _send_chat(self):
        msg = self.chat_entry.get().strip()
        if not msg:
            return
        try:
            send_chat(msg)
            self.chat_entry.set("")
        except OSError as e:
            messagebox.showerror("Chat", f"Could not send: {e}")

    def _refresh_live(self, snap):
        if snap.get("running"):
            st = snap.get("state") if isinstance(snap.get("state"), dict) else {}
            state = st.get("gameState", "?") + (f" ({st['reason']})" if st.get("reason") else "")
            lines = [f"Launched as:   {snap.get('command_line') or '?'}",
                     f"Hosting:       {'yes (+host)' if snap.get('hosting_args') else 'no'}",
                     f"Game state:    {state}",
                     f"Join state:    {snap.get('join_state')}",
                     f"Anti-cheat:    {'active' if snap.get('eac') else 'not active'}"]
        else:
            lines = ["Le Mans Ultimate is not running."]
        self.live_info.configure(text="\n".join(lines))

        chat = snap.get("chat") or []
        if chat != self._chat_seen:
            self._chat_seen = chat
            self.chat_log.configure(state=tk.NORMAL)
            self.chat_log.delete("1.0", tk.END)
            for c in chat:
                self.chat_log.insert(tk.END, (c.get("message", "") if isinstance(c, dict) else str(c)) + "\n")
            self.chat_log.configure(state=tk.DISABLED)
            self.chat_log.see(tk.END)

    # ---- sharing

    def _refresh_share(self):
        try:
            http = int(self._var("http_port").get())
            sim = int(self._var("sim_port").get())
            mc = int(self._var("max_clients").get())
        except ValueError:
            return
        ip = lan_ip()
        lines = [f"Server: {self._var('server_name').get()}",
                 f"Same network:  +connect {ip}:{http}"]
        if self._public_ip:
            lines.append(f"Internet:      +connect {self._public_ip}:{http}")
        else:
            lines.append("Internet:      click 'Look up public IP' (needs port forwarding)")
        lines.append("Password:      " + ("(ask the host)" if self._var("join_password").get() else "none"))
        lines.append("Or use the Join tab of this toolkit with the same IP and port.")
        self.share_text.delete("1.0", tk.END)
        self.share_text.insert(tk.END, "\n".join(lines))
        self.ports_label.configure(text=(
            f"TCP + UDP {http}            (HTTP port: players connect here)\n"
            f"TCP + UDP {sim}-{sim + mc}    (simulation port range: port to port + max clients)"))

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
            rules = open_firewall(s["http_port"], s["sim_port"], s["max_clients"])
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return
        except subprocess.CalledProcessError:
            messagebox.showerror("Firewall", "The admin prompt was cancelled or failed.")
            return
        messagebox.showinfo("Firewall", "Added inbound rules:\n" + "\n".join(f"{p} {r}" for p, r in rules))

    # ---- joining

    def _join(self, host=None, port=None):
        host = host or self._var("connect_host").get().strip()
        port = port or self._var("connect_port").get().strip()
        if not host or not port:
            self.join_state.configure(text="Enter the server IP and port.")
            return
        if not game_running():
            self.join_state.configure(text="LMU is not running. Start the game, then connect.")
            return
        try:
            self._collect()
        except ValueError:
            pass
        try:
            code, _ = join_server(host, port, self.join_pass.get())
            self.join_state.configure(text=f"Join request sent to {host}:{port} (HTTP {code}). Switch to the game.")
        except OSError as e:
            self.join_state.configure(text=f"Join failed: {e}")

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
        if snap.get("running"):
            self.status.configure(text="LMU running" + (" as host" if snap.get("hosting_args") else ""), fg=OK)
        else:
            self.status.configure(text="LMU not running", fg=MUTED)
        text, color = self._server_status()
        self.server_state.configure(text=text, fg=color)
        self.start_btn.configure(text="Stop server" if self._hosting() else "Start server")
        self._refresh_live(snap)

        selected = {self.tree.item(i, "values")[1:3] for i in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        for srv in self.listener.live():
            values = (srv.get("name", "?"), srv["ip"], srv.get("port", "?"),
                      srv.get("maxPlayers", "?"), "yes" if srv.get("password") else "no")
            item = self.tree.insert("", tk.END, values=values)
            if (str(values[1]), str(values[2])) in selected:
                self.tree.selection_add(item)
        self.after(2000, self._tick)

    def _on_close(self):
        try:
            self._collect()
        except ValueError:
            pass
        for t in (self.poller, self.listener, self.broadcaster):
            t.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
