#!/usr/bin/env python3
"""
Wallbox Bridge – eigenständiger HTTP-Server (kein Backend nötig).

Starten:  python bridge.py
Optionen: python bridge.py --ebox-host 192.168.0.244 --port 8001

Dient gleichzeitig als:
  • Modbus-Bridge  GET /status  POST /set {"amps": 10.5}
  • App-Server     GET /app  (und alle statischen Dateien)

Auf dem Handy einfach http://<laptop-ip>:8001/app aufrufen und
"Zum Startbildschirm hinzufügen" – fertig, kein Mini-PC nötig.

Liest eBOX-Verbindungsdaten aus config.yaml wenn vorhanden.
"""
import argparse
import json
import mimetypes
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from ebox_client import EBoxModbusClient

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    if yaml and Path("config.yaml").exists():
        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        e = cfg["ebox"]
        return e["host"], int(e["port"]), int(e["unit_id"])
    return "192.168.0.244", 502, 1


def parse_args(default_host):
    p = argparse.ArgumentParser(description="Wallbox HTTP-Bridge + App-Server")
    p.add_argument("--ebox-host", default=default_host)
    p.add_argument("--ebox-port", type=int, default=502)
    p.add_argument("--ebox-unit", type=int, default=1)
    p.add_argument("--port", type=int, default=8001, help="HTTP-Port (default: 8001)")
    return p.parse_args()


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "localhost"


# ── Modbus client ──────────────────────────────────────────────────────────────

_client: EBoxModbusClient | None = None


def get_client(host, port, unit) -> EBoxModbusClient:
    global _client
    if _client is None:
        _client = EBoxModbusClient(host, port, unit)
        _client.connect()
    return _client


def reset_client():
    global _client
    if _client:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


# ── Static files ───────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

# Map URL path → static file (only files the app needs)
STATIC_ROUTES = {
    "/app":           ("app.html",      "text/html; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/favicon.ico":   ("favicon.ico",   "image/x-icon"),
    "/favicon.svg":   ("favicon.svg",   "image/svg+xml"),
    "/favicon.png":   ("favicon.png",   "image/png"),
    "/icon-192.png":  ("icon-192.png",  "image/png"),
    "/icon-512.png":  ("icon-512.png",  "image/png"),
}


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    ebox_host: str = "192.168.0.244"
    ebox_port: int = 502
    ebox_unit: int = 1

    # ── routing ──

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # redirect bare / to /app
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/app")
            self.end_headers()
            return

        if self.path in STATIC_ROUTES:
            fname, mime = STATIC_ROUTES[self.path]
            self._serve_file(STATIC_DIR / fname, mime)
        elif self.path == "/status":
            self._handle_status()
        elif self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/set":
            self._handle_set()
        else:
            self.send_response(404)
            self.end_headers()

    # ── Modbus handlers ──

    def _handle_status(self):
        try:
            data = get_client(self.ebox_host, self.ebox_port, self.ebox_unit).read_status()
            self._json(200, data)
        except Exception:
            reset_client()
            try:
                data = get_client(self.ebox_host, self.ebox_port, self.ebox_unit).read_status()
                self._json(200, data)
            except Exception as e:
                self._json(500, {"error": str(e)})

    def _handle_set(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            amps = float(body["amps"])
            if not (0.0 <= amps <= 16.0):
                self._json(400, {"error": "amps muss zwischen 0 und 16 liegen"})
                return
            get_client(self.ebox_host, self.ebox_port, self.ebox_unit).write_three_phase_limit(amps)
            self._json(200, {"amps": amps})
        except Exception:
            reset_client()
            self._json(500, {"error": "Modbus-Fehler beim Schreiben"})

    # ── helpers ──

    def _serve_file(self, path: Path, mime: str):
        if not path.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = path.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()}  {fmt % args}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg_host, _, cfg_unit = load_config()
    args = parse_args(cfg_host)

    Handler.ebox_host = args.ebox_host
    Handler.ebox_port = args.ebox_port
    Handler.ebox_unit = args.ebox_unit

    ip = local_ip()
    print("Wallbox Bridge gestartet")
    print(f"  eBOX :  {args.ebox_host}:{args.ebox_port}  (Unit {args.ebox_unit})")
    print(f"  App  :  http://{ip}:{args.port}/app   ← auf dem Handy aufrufen")
    print(f"  Stoppen: Strg+C\n")

    try:
        HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nBridge beendet.")
