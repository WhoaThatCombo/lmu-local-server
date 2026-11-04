"""LMU Local Server Toolkit - host and join Le Mans Ultimate servers on your own machine."""

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
from urllib.parse import urlencode
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


def game_api(path, params=None, timeout=5):
    url = f"http://127.0.0.1:{GAME_API_PORT}{path}"
    if params:
        url += "?" + urlencode(params)
    with urlopen(Request(url, headers={"Content-Type": "application/json"}), timeout=timeout) as r:
        return r.status, r.read().decode(errors="ignore")


def game_running():
    try:
        return game_api("/rest/multiplayer/steam/status", timeout=1)[0] == 200
    except OSError:
        return False


def join_server(host, port, password=""):
    return game_api("/rest/multiplayer/join",
                    {"host": host, "port": port, "password": password, "authentication": ""},
                    timeout=10)


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
                   check=True)
    return rules


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


# ---------------------------------------------------------------- server process

class ServerProcess:
    def __init__(self, game_dir, args):
        self.game_dir = game_dir
        self.args = args
        self.proc = None
        self.started_at = None

    def start(self):
        exe = Path(self.game_dir) / GAME_EXE
        self.proc = subprocess.Popen([str(exe), *self.args], cwd=self.game_dir)
        self.started_at = time.time()
        return self.proc.pid

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


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

BG, PANEL, ACCENT, TEXT, OK, WARN, ERR = "#14161f", "#1d2130", "#4fc3f7", "#e6e6e6", "#81c784", "#ffb74d", "#ef5350"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LMU Local Server Toolkit")
        self.geometry("860x640")
        self.minsize(760, 560)
        self.configure(bg=BG)
        self.settings = load_settings()
        self.server = None
        self.listener = LANListener()
        self.listener.start()
        self.broadcaster = LANBroadcaster(self._broadcast_info)
        self.broadcaster.start()
        self.vars = {}
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
        s.configure("Muted.TLabel", foreground="#9aa0b4")
        s.configure("TLabelframe", background=BG, bordercolor="#2c3247")
        s.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        s.configure("TEntry", foreground=TEXT, insertcolor=ACCENT)
        s.configure("TCombobox", foreground=TEXT)
        s.configure("TCheckbutton", background=BG, foreground=TEXT)
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
        self.status = tk.Label(top, text="", bg=BG, fg="#9aa0b4", font=("Segoe UI", 10))
        self.status.pack(side=tk.RIGHT)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 14))
        nb.add(self._host_tab(nb), text="Host")
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
        self.profile_note = ttk.Label(game, text="", style="Muted.TLabel", wraplength=780, justify=tk.LEFT)
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

        self.server_state = tk.Label(f, text="Server not running", bg=BG, fg="#9aa0b4",
                                     font=("Segoe UI", 10), anchor="w", justify=tk.LEFT)
        self.server_state.pack(fill=tk.X, pady=(10, 0))
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
        ttk.Label(fw, style="Muted.TLabel", wraplength=780, justify=tk.LEFT, text=(
            "Same network: open the Windows Firewall rules below and you are done.\n"
            "Over the internet: also forward the same ports on your router to this PC's LAN IP, "
            "or use a virtual LAN such as Tailscale or ZeroTier and share that IP instead (no router changes)."
        )).pack(fill=tk.X, pady=(6, 6))
        ttk.Button(fw, text="Add Windows Firewall rules (asks for admin)",
                   command=self._open_firewall).pack(anchor="w")
        self._public_ip = None
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
        for c, w in zip(cols, (300, 150, 90, 90, 90)):
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

    def _toggle_server(self):
        if self.server and self.server.running():
            self.server.stop()
            self.server = None
            self.start_btn.configure(text="Start server")
            return
        s = self._apply_settings(quiet=True)
        if not s:
            return
        args = s["extra_args"].split()
        args += ["+profile=" + s["profile"], "+maxplayers", str(s["max_clients"])]
        if s["join_password"]:
            args += ["+password", s["join_password"]]
        try:
            self.server = ServerProcess(s["game_dir"], args)
            pid = self.server.start()
        except OSError as e:
            messagebox.showerror("Could not start", str(e))
            return
        self.start_btn.configure(text="Stop server")
        shown = ["***" if i and args[i - 1] == "+password" else a for i, a in enumerate(args)]
        self.server_state.configure(text=f"Started PID {pid}: {GAME_EXE} {' '.join(shown)}", fg=WARN)

    def _server_status(self):
        if not self.server:
            return "Server not running", "#9aa0b4"
        if not self.server.running():
            code = self.server.proc.returncode
            self.server = None
            self.start_btn.configure(text="Start server")
            return f"Game process exited (code {code}). Check the log folder for details.", ERR
        up = int(time.time() - self.server.started_at)
        port = self.settings["http_port"]
        if tcp_listening(port):
            return f"Running {up}s - accepting connections on TCP {port}. Friends can join now.", OK
        return (f"Running {up}s - not listening on TCP {port} yet. Wait for the track to load, "
                "then start a session in-game if needed."), WARN

    def _broadcast_info(self):
        if not (self.server and self.server.running() and tcp_listening(self.settings["http_port"])):
            return None
        return {"magic": LAN_MAGIC, "name": self.settings["server_name"], "port": self.settings["http_port"],
                "maxPlayers": self.settings["max_clients"], "password": bool(self.settings["join_password"])}

    def _open_logs(self):
        logs = Path(self._var("game_dir").get()) / "UserData" / "Log"
        if logs.exists():
            os.startfile(logs)
        else:
            messagebox.showinfo("No logs yet", f"{logs} does not exist yet.")

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
        if game_running():
            self.status.configure(text="LMU running", fg=OK)
        else:
            self.status.configure(text="LMU not running", fg="#9aa0b4")
        text, color = self._server_status()
        self.server_state.configure(text=text, fg=color)

        selected = {self.tree.item(i, "values")[1:3] for i in self.tree.selection()}
        self.tree.delete(*self.tree.get_children())
        for srv in self.listener.live():
            values = (srv.get("name", "?"), srv["ip"], srv.get("port", "?"),
                      srv.get("maxPlayers", "?"), "yes" if srv.get("password") else "no")
            item = self.tree.insert("", tk.END, values=values)
            if (str(values[1]), str(values[2])) in selected:
                self.tree.selection_add(item)
        self.after(3000, self._tick)

    def _on_close(self):
        if self.server and self.server.running():
            if not messagebox.askyesno("Server running", "Stop the server and quit?"):
                return
            self.server.stop()
        try:
            self._collect()
        except ValueError:
            pass
        self.listener.stop_event.set()
        self.broadcaster.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
