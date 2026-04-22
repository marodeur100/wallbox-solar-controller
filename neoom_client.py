from __future__ import annotations
from typing import Any, Dict, Optional

import requests


def fetch_site_state(beaam_host: str, api_key: str, timeout: float = 8.0) -> Dict[str, Any]:
    url = f"http://{beaam_host}/api/v1/site/state"
    headers = {"accept": "application/json", "authorization": f"Bearer {api_key}"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def parse_metrics(data: Dict[str, Any]) -> Dict[str, Optional[float]]:
    s = _extract_state_map(data)
    pv_power = _to_float(s.get("POWER_PRODUCTION"))
    load_power = _to_float(s.get("POWER_CONSUMPTION_CALC"))
    power_grid = _to_float(s.get("POWER_GRID"))
    power_storage = _to_float(s.get("POWER_STORAGE"))
    battery_soc = _to_float(s.get("STATE_OF_CHARGE"))
    grid_import_w = power_grid if power_grid is not None and power_grid > 0 else 0.0
    grid_export_w = -power_grid if power_grid is not None and power_grid < 0 else 0.0
    battery_discharge_w = power_storage if power_storage is not None and power_storage > 0 else 0.0
    battery_charge_w = -power_storage if power_storage is not None and power_storage < 0 else 0.0
    pv_priority_surplus_w = None
    if pv_power is not None and load_power is not None:
        pv_priority_surplus_w = pv_power - load_power
    return {
        "pv_power_w": pv_power,
        "load_power_w": load_power,
        "pv_priority_surplus_w": pv_priority_surplus_w,
        "grid_power_signed_w": power_grid,
        "grid_import_w": grid_import_w,
        "grid_export_w": grid_export_w,
        "battery_power_signed_w": power_storage,
        "battery_charge_w": battery_charge_w,
        "battery_discharge_w": battery_discharge_w,
        "battery_soc_pct": battery_soc,
    }


def _extract_state_map(data: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    states = (((data or {}).get("energyFlow") or {}).get("states") or [])
    for entry in states:
        key = entry.get("key")
        if key:
            result[str(key)] = entry.get("value")
    return result


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None
