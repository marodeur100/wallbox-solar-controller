from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

MIN_A = 6.0
MAX_A = 16.0
PHASES = 3
VOLTAGE = 230.0
MIN_POWER_W = PHASES * VOLTAGE * MIN_A  # 4 140 W


@dataclass
class Decision:
    recommended_amps: float
    recommended_power_w: int
    charge_enabled: bool
    reason: str
    surplus_w_raw: Optional[float]
    surplus_w_smoothed: Optional[float]


@dataclass
class ControllerState:
    smoothed_surplus_w: Optional[float] = None
    charging_enabled: bool = False
    last_setpoint_a: float = 0.0
    low_surplus_cycles: int = 0


def decide(
    state: ControllerState,
    raw_surplus: Optional[float],
    *,
    reserve_w: float,
    smoothing_alpha: float,
    start_margin_w: float,
    stop_margin_w: float,
    stop_hold_cycles: int,
    max_step_a: float,
) -> Decision:
    state.smoothed_surplus_w = _smooth(state.smoothed_surplus_w, raw_surplus, smoothing_alpha)
    s = state.smoothed_surplus_w

    if s is None:
        return Decision(0.0, 0, False, "Kein gültiger PV-Überschusswert.", raw_surplus, None)

    usable = max(s - reserve_w, 0.0)
    start_threshold = MIN_POWER_W + start_margin_w
    stop_threshold = MIN_POWER_W - stop_margin_w

    if state.charging_enabled and usable < stop_threshold:
        state.low_surplus_cycles += 1
    else:
        state.low_surplus_cycles = 0

    if not state.charging_enabled:
        if usable < start_threshold:
            return Decision(
                0.0, 0, False,
                f"Unter Startschwelle: {usable:.0f} W < {start_threshold:.0f} W.",
                raw_surplus, s,
            )
        stepped_a = _clamp(
            _clamp(usable / (PHASES * VOLTAGE), MIN_A, MAX_A),
            MIN_A, max(state.last_setpoint_a + max_step_a, MIN_A),
        )
        d = _make(stepped_a, True, "Startschwelle überschritten, Ladefreigabe aktiv.", raw_surplus, s)
        state.charging_enabled = True
        state.last_setpoint_a = d.recommended_amps
        return d

    if usable < stop_threshold and state.low_surplus_cycles >= stop_hold_cycles:
        d = Decision(
            0.0, 0, False,
            f"Seit {state.low_surplus_cycles} Zyklen unter Stoppschwelle "
            f"({usable:.0f} W < {stop_threshold:.0f} W).",
            raw_surplus, s,
        )
        state.charging_enabled = False
        state.last_setpoint_a = 0.0
        return d

    if usable < MIN_POWER_W:
        target_a = MIN_A
        reason = "Kurzer Einbruch – Haltebereich aktiv: bleibe bei 6 A."
    else:
        target_a = _clamp(usable / (PHASES * VOLTAGE), MIN_A, MAX_A)
        reason = "Ladung aktiv, Sollwert nachgeführt."

    stepped_a = (
        min(target_a, state.last_setpoint_a + max_step_a)
        if target_a > state.last_setpoint_a
        else max(target_a, state.last_setpoint_a - max_step_a)
    )
    stepped_a = _clamp(stepped_a, MIN_A, MAX_A)
    d = _make(stepped_a, True, reason, raw_surplus, s)
    state.charging_enabled = True
    state.last_setpoint_a = d.recommended_amps
    return d


def _make(amps: float, enabled: bool, reason: str, raw: Optional[float], smooth: Optional[float]) -> Decision:
    return Decision(
        round(amps, 1),
        int(round(amps * PHASES * VOLTAGE)),
        enabled, reason, raw, smooth,
    )


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _smooth(prev: Optional[float], curr: Optional[float], alpha: float) -> Optional[float]:
    if curr is None:
        return prev
    if prev is None:
        return curr
    return prev * (1.0 - alpha) + curr * alpha
