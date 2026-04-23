from __future__ import annotations
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import neoom_client
from controller import ControllerState, decide
from ebox_client import EBoxModbusClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("config.yaml")
if not CONFIG_PATH.exists():
    sys.exit("config.yaml nicht gefunden. Bitte config.example.yaml kopieren und anpassen.")

with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

NEOOM_HOST: str = cfg["neoom"]["beaam_host"]
NEOOM_KEY: str = cfg["neoom"]["api_key"]
EBOX_HOST: str = cfg["ebox"]["host"]
EBOX_PORT: int = int(cfg["ebox"]["port"])
EBOX_UNIT: int = int(cfg["ebox"]["unit_id"])
EBOX_FALLBACK: float = float(cfg["ebox"]["fallback_amps"])
CTRL = cfg["controller"]
SERVER = cfg["server"]
POLL_INTERVAL: float = float(SERVER["poll_interval_s"])

MAX_A = 16.0

# ---------------------------------------------------------------------------
# App state (single writer: the polling loop; readers: API handlers)
# ---------------------------------------------------------------------------

app_state: Dict[str, Any] = {
    "mode": "manual",     # "auto" | "manual" – startet immer manuell
    "neoom": None,
    "ebox": None,
    "decision": None,
    "last_update": None,
    "neoom_error": None,
    "ebox_error": None,
}

ctrl_state = ControllerState()
_ebox_client: Optional[EBoxModbusClient] = None
_subscribers: List[asyncio.Queue] = []


# ---------------------------------------------------------------------------
# eBOX helpers (sync – run in thread executor)
# ---------------------------------------------------------------------------

def _get_ebox() -> EBoxModbusClient:
    global _ebox_client
    if _ebox_client is None:
        _ebox_client = EBoxModbusClient(EBOX_HOST, EBOX_PORT, EBOX_UNIT)
        _ebox_client.connect()
    return _ebox_client


def _reconnect_ebox() -> EBoxModbusClient:
    global _ebox_client
    if _ebox_client is not None:
        try:
            _ebox_client.close()
        except Exception:
            pass
        _ebox_client = None
    _ebox_client = EBoxModbusClient(EBOX_HOST, EBOX_PORT, EBOX_UNIT)
    _ebox_client.connect()
    return _ebox_client


def _init_fallback_sync() -> None:
    try:
        _get_ebox().write_three_phase_fallback(EBOX_FALLBACK)
    except Exception:
        pass


def _write_amps_sync(amps: float) -> None:
    try:
        _get_ebox().write_three_phase_limit(amps)
    except Exception:
        _reconnect_ebox().write_three_phase_limit(amps)


def _read_ebox_sync() -> Optional[dict]:
    try:
        return _get_ebox().read_status()
    except Exception:
        try:
            return _reconnect_ebox().read_status()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Polling loop (runs every POLL_INTERVAL seconds)
# ---------------------------------------------------------------------------

def _poll_sync() -> None:
    neoom_metrics = None
    neoom_error = None
    ebox_error = None
    decision_dict = None

    try:
        data = neoom_client.fetch_site_state(NEOOM_HOST, NEOOM_KEY)
        neoom_metrics = neoom_client.parse_metrics(data)
    except Exception as exc:
        neoom_error = str(exc)

    if app_state["mode"] == "auto" and neoom_metrics is not None:
        raw = neoom_metrics.get("pv_priority_surplus_w")
        dec = decide(
            ctrl_state, raw,
            reserve_w=float(CTRL["reserve_w"]),
            smoothing_alpha=float(CTRL["smoothing_alpha"]),
            start_margin_w=float(CTRL["start_margin_w"]),
            stop_margin_w=float(CTRL["stop_margin_w"]),
            stop_hold_cycles=int(CTRL["stop_hold_cycles"]),
            max_step_a=float(CTRL["max_step_a"]),
        )
        decision_dict = {
            "recommended_amps": dec.recommended_amps,
            "recommended_power_w": dec.recommended_power_w,
            "charge_enabled": dec.charge_enabled,
            "reason": dec.reason,
            "surplus_w_raw": dec.surplus_w_raw,
            "surplus_w_smoothed": dec.surplus_w_smoothed,
        }
        target = dec.recommended_amps if dec.charge_enabled else 0.0
        try:
            _write_amps_sync(target)
        except Exception as exc:
            ebox_error = str(exc)

    ebox_status = _read_ebox_sync()
    if ebox_status is None and ebox_error is None:
        ebox_error = "eBOX nicht erreichbar"

    app_state.update({
        "neoom": neoom_metrics,
        "ebox": ebox_status,
        "decision": decision_dict,
        "last_update": time.strftime("%H:%M:%S"),
        "neoom_error": neoom_error,
        "ebox_error": ebox_error,
    })


async def _poll_loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, _poll_sync)
        except Exception:
            pass
        await _broadcast()
        await asyncio.sleep(POLL_INTERVAL)


async def _broadcast() -> None:
    payload = json.dumps(app_state)
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _init_fallback_sync)
    asyncio.create_task(_poll_loop())
    yield


app = FastAPI(title="Wallbox Solar Controller", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse((Path("static") / "index.html").read_text(encoding="utf-8"))


@app.get("/api/state")
async def get_state():
    return JSONResponse(app_state)


@app.get("/api/stream")
async def stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=5)
    _subscribers.append(queue)

    async def generator():
        try:
            yield f"data: {json.dumps(app_state)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=45.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ModeRequest(BaseModel):
    mode: str


@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    if req.mode not in ("auto", "manual"):
        raise HTTPException(400, "mode muss 'auto' oder 'manual' sein")
    if req.mode == "auto":
        global ctrl_state
        ctrl_state = ControllerState()
    app_state["mode"] = req.mode
    return {"mode": req.mode}


class ManualRequest(BaseModel):
    amps: float


@app.post("/api/manual")
async def set_manual(req: ManualRequest):
    if not (0.0 <= req.amps <= MAX_A):
        raise HTTPException(400, f"Ampere muss zwischen 0 und {MAX_A} liegen")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _write_amps_sync, req.amps)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    ebox_status = await loop.run_in_executor(None, _read_ebox_sync)
    if ebox_status:
        app_state["ebox"] = ebox_status
    await _broadcast()
    return {"amps": req.amps}


if __name__ == "__main__":
    uvicorn.run(app, host=SERVER["host"], port=int(SERVER["port"]))
