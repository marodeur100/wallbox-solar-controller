from __future__ import annotations
import inspect
import struct
import threading
from typing import Dict, Optional

try:
    from pymodbus.client import ModbusTcpClient
except Exception:
    try:
        from pymodbus.client.sync import ModbusTcpClient
    except Exception as e:
        raise SystemExit("pymodbus ist nicht installiert. Installiere es mit: pip install pymodbus") from e


class EBoxModbusClient:
    def __init__(self, host: str, port: int, unit_id: int = 1, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.client = ModbusTcpClient(host=host, port=port, timeout=timeout)
        self._lock = threading.Lock()

    def connect(self) -> bool:
        return bool(self.client.connect())

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def _dispatch_read(self, address: int, count: int):
        method = self.client.read_holding_registers
        try:
            params = inspect.signature(method).parameters
        except Exception:
            params = {}

        attempts = []
        if "address" in params and "count" in params:
            base = {"address": address, "count": count}
            for name in ("slave", "unit", "device_id"):
                if name in params:
                    attempts.append(((), {**base, name: self.unit_id}))
            attempts.append(((), base))
        else:
            for name in ("slave", "unit", "device_id"):
                attempts.append(((address, count), {name: self.unit_id}))
            attempts.append(((address, count, self.unit_id), {}))
            attempts.append(((address, count), {}))

        last_err = None
        for args, kwargs in attempts:
            try:
                return method(*args, **kwargs)
            except TypeError as exc:
                last_err = exc
        raise RuntimeError(f"Inkompatible pymodbus-Version für read_holding_registers: {last_err}")

    def _dispatch_write(self, address: int, values: list):
        method = self.client.write_registers
        try:
            params = inspect.signature(method).parameters
        except Exception:
            params = {}

        attempts = []
        if "address" in params and ("values" in params or "value" in params):
            val_name = "values" if "values" in params else "value"
            base = {"address": address, val_name: values}
            for name in ("slave", "unit", "device_id"):
                if name in params:
                    attempts.append(((), {**base, name: self.unit_id}))
            attempts.append(((), base))
        else:
            for name in ("slave", "unit", "device_id"):
                attempts.append(((address, values), {name: self.unit_id}))
            attempts.append(((address, values, self.unit_id), {}))
            attempts.append(((address, values), {}))

        last_err = None
        for args, kwargs in attempts:
            try:
                return method(*args, **kwargs)
            except TypeError as exc:
                last_err = exc
        raise RuntimeError(f"Inkompatible pymodbus-Version für write_registers: {last_err}")

    @staticmethod
    def _float_to_regs(value: float) -> list:
        raw = struct.pack(">f", float(value))
        return [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]

    @staticmethod
    def _regs_to_float(registers: list) -> float:
        if len(registers) != 2:
            raise ValueError("FLOAT32 erwartet 2 Register")
        raw = registers[0].to_bytes(2, "big") + registers[1].to_bytes(2, "big")
        return round(struct.unpack(">f", raw)[0], 3)

    def read_u16(self, address: int) -> int:
        with self._lock:
            response = self._dispatch_read(address, 1)
        if hasattr(response, "isError") and response.isError():
            raise RuntimeError(f"Modbus-Fehler bei Register {address}: {response}")
        regs = getattr(response, "registers", None)
        if not regs:
            raise RuntimeError(f"Keine Daten bei Register {address}")
        return int(regs[0])

    def read_float32(self, address: int) -> float:
        with self._lock:
            response = self._dispatch_read(address, 2)
        if hasattr(response, "isError") and response.isError():
            raise RuntimeError(f"Modbus-Fehler bei Register {address}: {response}")
        regs = getattr(response, "registers", None)
        if not regs:
            raise RuntimeError(f"Keine Daten bei Register {address}")
        return self._regs_to_float(regs)

    def write_three_phase_limit(self, amps: float) -> None:
        regs = self._float_to_regs(amps) * 3
        with self._lock:
            response = self._dispatch_write(1012, regs)
        if hasattr(response, "isError") and response.isError():
            raise RuntimeError(f"Schreibfehler 1012–1017: {response}")

    def write_three_phase_fallback(self, amps: float) -> None:
        regs = self._float_to_regs(amps) * 3
        with self._lock:
            response = self._dispatch_write(1018, regs)
        if hasattr(response, "isError") and response.isError():
            raise RuntimeError(f"Schreibfehler 1018–1023: {response}")

    def read_status(self) -> Dict[str, Optional[float]]:
        data: Dict[str, Optional[float]] = {}
        for name, address, dtype in [
            ("limit_l1",    1012, "f32"),
            ("limit_l2",    1014, "f32"),
            ("limit_l3",    1016, "f32"),
            ("fallback_l1", 1018, "f32"),
            ("fallback_l2", 1020, "f32"),
            ("fallback_l3", 1022, "f32"),
            ("phase_l1",    1025, "u16"),
            ("phase_l2",    1026, "u16"),
            ("phase_l3",    1027, "u16"),
            ("availability", 1028, "u16"),
            ("current_l1",  1006, "f32"),
            ("current_l2",  1008, "f32"),
            ("current_l3",  1010, "f32"),
        ]:
            try:
                data[name] = self.read_float32(address) if dtype == "f32" else self.read_u16(address)
            except Exception:
                data[name] = None
        return data
