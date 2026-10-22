"""Ladenachweis: sammelt Ladesessions aus dem WebConfig der Compleo eBOX und
erzeugt daraus einen PDF-Nachweis (Übersicht, Sessionliste, Screenshots).

Datensammlung nutzt nur ``requests``. Playwright wird ausschliesslich fuer
Screenshots und PDF-Erzeugung benoetigt (pip install playwright).
"""
from __future__ import annotations

import base64
import csv
import hashlib
import html
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DATA_DIR = Path("data") / "ladenachweis"
RAW_DIR = DATA_DIR / "raw"
SESSIONS_CSV = DATA_DIR / "sessions.csv"
SNAPSHOTS_CSV = DATA_DIR / "meter_snapshots.csv"
MANIFEST_CSV = DATA_DIR / "manifest.csv"
REPORT_DIR = DATA_DIR / "reports"

FIELDS = ["START_TIME", "END_TIME", "AUTHENTICATION_TYPE", "CONTRACT_ID",
          "SESSION_END_REASON", "METER_START", "METER_END", "UUID"]
TS_FMT = "%d-%m-%Y %H:%M:%S"

_lock = threading.Lock()
status: Dict[str, Any] = {"last_collect": None, "last_error": None,
                          "sessions": 0, "meter_kwh": None, "new_sessions": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kwh(s: str) -> float:
    return float(s.split()[0])


def _de(x: float, nd: int = 2) -> str:
    return f"{x:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _base(cfg: dict) -> str:
    return f"https://{cfg['ebox']['host']}/"


def _login(cfg: dict) -> requests.Session:
    nc = cfg["nachweis"]
    s = requests.Session()
    s.verify = False
    r = s.post(_base(cfg) + "cgi_c_login",
               data={"username": nc.get("web_user", "admin"), "password": nc["web_password"]},
               timeout=15)
    r.raise_for_status()
    if 'name="password"' in r.text:
        raise RuntimeError("Login am WebConfig der eBOX fehlgeschlagen (Benutzer/Passwort?)")
    return s


def _store_if_new(kind: str, suffix: str, content: bytes, stamp: str) -> Optional[Path]:
    """Rohdatei nur ablegen, wenn sie sich seit der letzten gleichen Art geaendert hat."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    digest = _sha(content)
    last = None
    if MANIFEST_CSV.exists():
        with open(MANIFEST_CSV, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f, delimiter=";"):
                if row["kind"] == kind:
                    last = row
    if last and last["sha256"] == digest:
        return None
    path = RAW_DIR / f"{stamp}_{kind}{suffix}"
    path.write_bytes(content)
    new = not MANIFEST_CSV.exists()
    with open(MANIFEST_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["timestamp", "kind", "file", "sha256"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), kind, path.name, digest])
    return path


def load_sessions() -> List[Dict[str, str]]:
    if not SESSIONS_CSV.exists():
        return []
    with open(SESSIONS_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _save_sessions(rows: Dict[str, Dict[str, str]]) -> None:
    ordered = sorted(rows.values(), key=lambda r: datetime.strptime(r["START_TIME"], TS_FMT))
    with open(SESSIONS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(ordered)


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

def collect(cfg: dict) -> Dict[str, Any]:
    """Meldet sich an der eBOX an, merged das Session-Log in sessions.csv."""
    with _lock:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            base = _base(cfg)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            s = _login(cfg)

            r = s.get(base + "cgi_c_ldp1.session-charge_session", timeout=30)
            r.raise_for_status()
            if not r.content.startswith(b"START_TIME"):
                raise RuntimeError("Unerwartete Antwort beim Session-Download")
            _store_if_new("charge_sessions", ".csv", r.content, stamp)

            rows = {r_["UUID"]: r_ for r_ in load_sessions()}
            before = len(rows)
            text = r.content.decode("utf-8", "replace")
            for row in csv.DictReader(text.splitlines(), delimiter=";"):
                rows[row["UUID"]] = {k: row[k] for k in FIELDS}
            _save_sessions(rows)

            try:
                sd = s.get(base + "cgi_c_ldp1.session-sdrs", timeout=60)
                if sd.ok and sd.content[:2] == b"\x1f\x8b":
                    _store_if_new("sdr_archive", ".tar.gz", sd.content, stamp)
            except Exception:
                pass

            page = s.get(base + "cgi_s_ldp1.session", timeout=15).content.decode("utf-8", "replace")
            plain = html.unescape(re.sub(r"<[^>]+>", " ", page))
            m = re.search(r"(?<!start )Zählerstand:\s*([\d.]+)\s*kWh", plain)
            meter = float(m.group(1)) if m else None
            if meter is not None:
                new = not SNAPSHOTS_CSV.exists()
                with open(SNAPSHOTS_CSV, "a", encoding="utf-8", newline="") as f:
                    w = csv.writer(f, delimiter=";")
                    if new:
                        w.writerow(["timestamp", "meter_kwh"])
                    w.writerow([datetime.now().isoformat(timespec="seconds"), meter])

            status.update({"last_collect": datetime.now().isoformat(timespec="seconds"),
                           "last_error": None, "sessions": len(rows),
                           "new_sessions": len(rows) - before, "meter_kwh": meter})
        except Exception as exc:
            status.update({"last_collect": datetime.now().isoformat(timespec="seconds"),
                           "last_error": str(exc)})
        return dict(status)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _period(period: str) -> Tuple[datetime, datetime, str]:
    if re.fullmatch(r"\d{4}-\d{2}", period):
        y, m = int(period[:4]), int(period[5:])
        start = datetime(y, m, 1)
        end = datetime(y + (m == 12), m % 12 + 1, 1)
        return start, end, f"{m:02d}/{y}"
    if re.fullmatch(r"\d{4}", period):
        y = int(period)
        return datetime(y, 1, 1), datetime(y + 1, 1, 1), str(y)
    raise ValueError("Zeitraum muss 'YYYY-MM' oder 'YYYY' sein")


def _screenshots(cfg: dict) -> List[Tuple[str, str, str]]:
    """(Titel, URL-Pfad, PNG-base64); leer, wenn Playwright/Wallbox fehlen."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    nc = cfg["nachweis"]
    pages = [("Übersicht", "cgi_s_ecu.overview"),
             ("Ladepunkt LDP1 – Session und Zählerstand", "cgi_s_ldp1.session"),
             ("System – Seriennummer und Firmware", "cgi_s_system.system_data")]
    shots: List[Tuple[str, str, str]] = []
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            b = p.chromium.launch(headless=True)
        ctx = b.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(_base(cfg), wait_until="networkidle", timeout=30000)
        page.locator("input[name=username]").fill(nc.get("web_user", "admin"))
        page.locator("input[name=password]").fill(nc["web_password"])
        page.locator("input[type=submit]").click()
        page.wait_for_load_state("networkidle")
        for title, path in pages:
            try:
                page.goto(_base(cfg) + path, wait_until="networkidle", timeout=30000)
                banner = f"{_base(cfg)}{path} – aufgenommen {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
                page.evaluate(
                    """t => { const d = document.createElement('div'); d.textContent = t;
                    d.style = 'background:#ffe9a8;color:#000;font:12px monospace;padding:4px 8px;';
                    document.body.prepend(d); }""", banner)
                png = page.screenshot(full_page=True)
                shots.append((title, path, base64.b64encode(png).decode()))
            except Exception:
                continue
        b.close()
    return shots


def _manifest_last() -> List[Dict[str, str]]:
    if not MANIFEST_CSV.exists():
        return []
    last: Dict[str, Dict[str, str]] = {}
    with open(MANIFEST_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            last[row["kind"]] = row
    return list(last.values())


def build_report(cfg: dict, period: str) -> Path:
    """Erzeugt data/ladenachweis/reports/Ladenachweis_<period>.pdf."""
    from playwright.sync_api import sync_playwright

    nc = cfg["nachweis"]
    start, end, label = _period(period)
    all_rows = load_sessions()
    rows = [r for r in all_rows if start <= datetime.strptime(r["START_TIME"], TS_FMT) < end]
    if not rows:
        raise RuntimeError(f"Keine Sessions im Zeitraum {label} gespeichert")

    total = sum(_kwh(r["METER_END"]) - _kwh(r["METER_START"]) for r in rows)
    price = nc.get("price_eur_per_kwh")
    first, last = rows[0], rows[-1]

    # Kettenpruefung: METER_END einer Session == METER_START der naechsten
    gaps: List[str] = []
    idx = all_rows.index(first)
    chain = all_rows[max(idx - 1, 0): all_rows.index(last) + 1]
    for a, b_ in zip(chain, chain[1:]):
        d = _kwh(b_["METER_START"]) - _kwh(a["METER_END"])
        if abs(d) > 0.001:
            gaps.append(f"Zwischen {a['END_TIME']} und {b_['START_TIME']}: {_de(d, 4)} kWh nicht in Sessions erfasst")

    esc = html.escape
    trs = "".join(
        f"<tr><td>{esc(r['START_TIME'])}</td><td>{esc(r['END_TIME'])}</td>"
        f"<td class=n>{_de(_kwh(r['METER_START']), 4)}</td><td class=n>{_de(_kwh(r['METER_END']), 4)}</td>"
        f"<td class=n>{_de(_kwh(r['METER_END']) - _kwh(r['METER_START']), 2)}</td></tr>"
        for r in rows)
    cost = (f"<tr><td>Strompreis</td><td>{_de(float(price), 4)} €/kWh</td></tr>"
            f"<tr><td>Stromkosten</td><td><b>{_de(total * float(price), 2)} €</b></td></tr>") if price else ""
    gap_html = ("<p class=warn><b>Hinweis Zählerkette:</b><br>" + "<br>".join(map(esc, gaps)) + "</p>") if gaps else \
        "<p>Zählerkette lückenlos: Der Endstand jeder Session entspricht dem Startstand der nächsten.</p>"
    shots = _screenshots(cfg)
    shots_html = "".join(
        f"<div class=pb><h3>{esc(t)}</h3><img src='data:image/png;base64,{b64}'></div>" for t, _, b64 in shots) \
        or "<p>Keine Screenshots verfügbar (Playwright oder Wallbox nicht erreichbar).</p>"
    man = "".join(f"<tr><td>{esc(m['file'])}</td><td class=h>{m['sha256']}</td></tr>" for m in _manifest_last())

    doc = f"""<!doctype html><html lang=de><meta charset=utf-8><style>
    body{{font:11px Arial,sans-serif;color:#000}} h1{{font-size:20px}} h2{{font-size:14px;margin-top:22px}}
    table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #999;padding:3px 6px;text-align:left}}
    .n{{text-align:right}} .h{{font:9px monospace;word-break:break-all}} .warn{{background:#ffe9a8;padding:6px}}
    .kv td:first-child{{width:38%;background:#f0f0f0}} img{{max-width:100%;border:1px solid #999}} .pb{{page-break-before:always}}
    </style>
    <h1>Ladenachweis Elektro-Dienstwagen – {esc(label)}</h1>
    <p>Aus den Daten der privaten Wallbox (Compleo eBOX). Werte unverändert aus dem WebConfig der Wallbox übernommen.</p>
    <h2>Zusammenfassung</h2>
    <table class=kv>
    <tr><td>Name</td><td>{esc(str(nc.get('owner', '')))}</td></tr>
    <tr><td>Fahrzeug / Kennzeichen</td><td>{esc(str(nc.get('vehicle', '')))}</td></tr>
    <tr><td>Wallbox</td><td>{esc(str(nc.get('wallbox', 'Compleo eBOX')))}</td></tr>
    <tr><td>Zeitraum</td><td>{esc(label)} (Zuordnung nach Startzeit der Session)</td></tr>
    <tr><td>Anzahl Ladevorgänge</td><td>{len(rows)}</td></tr>
    <tr><td>Zählerstand Beginn</td><td>{_de(_kwh(first['METER_START']), 4)} kWh ({esc(first['START_TIME'])})</td></tr>
    <tr><td>Zählerstand Ende</td><td>{_de(_kwh(last['METER_END']), 4)} kWh ({esc(last['END_TIME'])})</td></tr>
    <tr><td>Geladene Energie gesamt</td><td><b>{_de(total, 2)} kWh</b></td></tr>
    {cost}
    </table>
    {gap_html}
    <p>{esc(str(nc.get('meter_note', '')))}</p>
    <h2>Ladevorgänge</h2>
    <table><tr><th>Start</th><th>Ende</th><th>Zähler Start [kWh]</th><th>Zähler Ende [kWh]</th><th>Energie [kWh]</th></tr>
    {trs}<tr><td colspan=4><b>Summe</b></td><td class=n><b>{_de(total, 2)}</b></td></tr></table>
    <h2>Integrität der Rohdaten (SHA-256)</h2>
    <table>{man}</table>
    <p>Rohdateien liegen unverändert in data/ladenachweis/raw/ (Session-CSV und Secure-SDR-Archiv).
    Erstellt am {datetime.now().strftime('%d.%m.%Y %H:%M')}.</p>
    <div class=pb></div><h2>Anhang: Screenshots aus dem Webfrontend der Wallbox</h2>{shots_html}
    </html>"""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"Ladenachweis_{period}.pdf"
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.set_content(doc)
        page.pdf(path=str(out), format="A4", print_background=True,
                 margin={"top": "15mm", "bottom": "15mm", "left": "12mm", "right": "12mm"})
        b.close()
    return out


def previous_month() -> str:
    now = datetime.now()
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return f"{y}-{m:02d}"


if __name__ == "__main__":
    import argparse
    import sys
    import yaml

    ap = argparse.ArgumentParser(description="Ladenachweis sammeln / PDF erzeugen")
    ap.add_argument("action", choices=["collect", "report"])
    ap.add_argument("--period", default=None, help="YYYY-MM oder YYYY (Standard: Vormonat)")
    a = ap.parse_args()
    with open("config.yaml", encoding="utf-8") as f:
        _cfg = yaml.safe_load(f)
    if a.action == "collect":
        print(collect(_cfg))
    else:
        print(build_report(_cfg, a.period or previous_month()))
    sys.exit(0)
