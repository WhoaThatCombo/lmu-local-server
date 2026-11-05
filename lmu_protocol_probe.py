"""Listen like an LMU server and log everything a joining client sends.

Usage:
  python lmu_protocol_probe.py [--sim 54298] [--http 64298] [--join] [--log probe.log]

--join asks the running game (REST port 6397) to join 127.0.0.1:<sim>.
Ports default to +1 of the game's own defaults so they don't collide with the
local client's sockets.
"""
import argparse
import datetime
import json
import socket
import sys
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOCK = threading.Lock()
LOG = None


def log(kind, peer, msg, data=b""):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    lines = [f"{ts} [{kind}] {peer} {msg}"]
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"    {off:04x}  {hexs:<47}  {asc}")
    text = "\n".join(lines)
    with LOCK:
        print(text, flush=True)
        if LOG:
            LOG.write(text + "\n")
            LOG.flush()


def tcp_session(kind, conn, peer, idle):
    conn.settimeout(idle)
    total = 0
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                log(kind, peer, f"closed by client after {total} bytes")
                return
            total += len(data)
            log(kind, peer, f"recv {len(data)} bytes", data)
    except socket.timeout:
        log(kind, peer, f"idle {idle}s, client sent {total} bytes total; closing")
    except OSError as e:
        log(kind, peer, f"error {e}")
    finally:
        conn.close()


def tcp_listener(kind, port, idle):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        log(kind, f":{port}", f"bind failed: {e}")
        return
    s.listen(16)
    log(kind, f":{port}", "listening (tcp)")
    while True:
        conn, addr = s.accept()
        peer = f"{addr[0]}:{addr[1]}->:{port}"
        log(kind, peer, "connect")
        threading.Thread(target=tcp_session, args=(kind, conn, peer, idle), daemon=True).start()


def udp_listener(kind, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        log(kind, f":{port}", f"bind failed: {e}")
        return
    log(kind, f":{port}", "listening (udp)")
    while True:
        data, addr = s.recvfrom(65536)
        log(kind, f"{addr[0]}:{addr[1]}->:{port}", f"datagram {len(data)} bytes", data)


def cancel_join():
    for method in ("GET", "POST"):
        try:
            req = Request("http://127.0.0.1:6397/rest/multiplayer/cancelJoinRequest", method=method, data=b"" if method == "POST" else None)
            with urlopen(req, timeout=5) as r:
                log("join", "rest", f"cancelJoinRequest ({method}) -> {r.status}")
                return
        except Exception as e:
            log("join", "rest", f"cancelJoinRequest ({method}) failed: {e}")


def game_post(path, body, ctype="application/json"):
    req = Request(f"http://127.0.0.1:6397{path}", data=body.encode(), method="POST",
                  headers={"Content-Type": ctype})
    try:
        with urlopen(req, timeout=15) as r:
            log("join", "rest", f"POST {path} -> {r.status} {r.read()[:200]!r}")
    except Exception as e:
        log("join", "rest", f"POST {path} failed: {e}")


def enter_race(car_id):
    """What the UI does after JOIN_JOINED_SERVER: pick the car, then navigate into the multiplayer race."""
    if car_id:
        game_post("/rest/garage/SetCurrentVehicle", json.dumps({"id": car_id}), "text/plain")
    game_post("/navigation/action/NAV_RACE_MULTIPLAYER", "null")


def request_join(sim_port, password, car_id=None):
    cancel_join()
    time.sleep(1)
    q = urlencode({"host": "127.0.0.1", "port": sim_port, "password": password, "authentication": ""})
    url = f"http://127.0.0.1:6397/rest/multiplayer/join?{q}"
    try:
        with urlopen(url, timeout=10) as r:
            log("join", "rest", f"GET {url} -> {r.status} {r.read()[:300]!r}")
    except Exception as e:
        log("join", "rest", f"GET {url} failed: {e}")
        return
    entered = False
    last = None
    for _ in range(60):
        time.sleep(1)
        try:
            with urlopen("http://127.0.0.1:6397/rest/multiplayer/join/state", timeout=3) as r:
                state = r.read()[:300]
        except Exception as e:
            state = f"failed: {e}".encode()
        if state != last:
            log("join", "rest", f"join/state {state!r}")
            last = state
        if b"JOIN_JOINED_SERVER" in state and not entered:
            entered = True
            enter_race(car_id)


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", type=int, default=54298)
    ap.add_argument("--http", type=int, default=64298)
    ap.add_argument("--extra", type=int, nargs="*", default=[], help="more tcp+udp ports to watch")
    ap.add_argument("--idle", type=float, default=60.0)
    ap.add_argument("--join", action="store_true")
    ap.add_argument("--password", default="")
    ap.add_argument("--log", default="probe.log")
    a = ap.parse_args()
    LOG = open(a.log, "a", encoding="utf-8")
    log("probe", "-", f"start sim={a.sim} http={a.http} extra={a.extra}")

    for port, kind in [(a.sim, "sim"), (a.http, "http"), *[(p, "extra") for p in a.extra]]:
        threading.Thread(target=tcp_listener, args=(kind, port, a.idle), daemon=True).start()
        threading.Thread(target=udp_listener, args=(kind, port), daemon=True).start()
    time.sleep(0.5)

    if a.join:
        threading.Thread(target=request_join, args=(a.sim, a.password), daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("probe", "-", "stop")


if __name__ == "__main__":
    sys.exit(main())
