#!/usr/bin/env python3
"""
Minimaler HTTP → Modbus-Bridge für die Wallbox-App.

Starten:  python bridge.py
Optionen: python bridge.py --ebox-host 192.168.0.244 --port 8001

Endpunkte:
  GET  /status          → Wallbox-Zustand (JSON)
  POST /set  {"amps": 10.5}  → Ladestrom setzen
  GET  /health          → {"ok": true}

Liest Host/Port aus config.yaml wenn vorhanden, sonst aus Argumenten.
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from ebox_client import EBoxModbusClient

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if yaml and Path("config.yaml").exists():
        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg["ebox"]["host"], int(cfg["ebox"]["port"]), int(cfg["ebox"]["unit_id"])
    return "192.168.0.244", 502, 1


def parse_args(host, port):
    p = argparse.ArgumentParser(description="Wallbox HTTP-Modbus-Bridge")
    p.add_argument("--ebox-host", default=host)
    p.add_argument("--ebox-port", type=int, default=502)
    p.add_argument("--ebox-unit", type=int, default=1)
    p.add_argument("--port",      type=int, default=8001, help="HTTP-Port (default: 8001)")
    return p.parse_args()


# ── Modbus client (lazy connect) ──────────────────────────────────────────────

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


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    ebox_host: str = "192.168.0.244"
    ebox_port: int = 502
    ebox_unit: int = 1

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
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

    def _handle_status(self):
        try:
            status = get_client(self.ebox_host, self.ebox_port, self.ebox_unit).read_status()
            self._json(200, status)
        except Exception as e:
            reset_client()
            try:
                status = get_client(self.ebox_host, self.ebox_port, self.ebox_unit).read_status()
                self._json(200, status)
            except Exception as e2:
                self._json(500, {"error": str(e2)})

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
        except Exception as e:
            reset_client()
            self._json(500, {"error": str(e)})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg_host, cfg_port, cfg_unit = load_config()
    args = parse_args(cfg_host, cfg_port)

    Handler.ebox_host = args.ebox_host
    Handler.ebox_port = args.ebox_port
    Handler.ebox_unit = args.ebox_unit

    print(f"Wallbox Bridge gestartet")
    print(f"  eBOX:    {args.ebox_host}:{args.ebox_port} (Unit {args.ebox_unit})")
    print(f"  App-URL: http://<diese-ip>:{args.port}/set")
    print(f"  Stoppen: Strg+C\n")

    try:
        HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nBridge beendet.")
