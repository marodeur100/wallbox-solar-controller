"""
Wallbox Controller – Android App (Kivy)
Spricht direkt per Modbus TCP mit der Compleo eBOX.
Kein Backend, kein Bridge-Server, kein PC nötig.
"""
import struct
import socket
import threading

from kivy.app import App
from kivy.lang import Builder
from kivy.clock import Clock, mainthread
from kivy.storage.jsonstore import JsonStore
from kivy.uix.screenmanager import ScreenManager, Screen

# ── Modbus TCP (pure Python, keine Abhängigkeiten) ────────────────────────────

class Modbus:
    """
    Minimaler Modbus-TCP-Client.
    FC3 = Read Holding Registers (Limit, Fallback)
    FC4 = Read Input Registers   (Ist-Strom)
    FC16 = Write Multiple Registers (Limit setzen)
    """

    def __init__(self):
        self._sock = None
        self._tid  = 0
        self._unit = 1
        self._lock = threading.Lock()

    def connect(self, host: str, port: int = 502, unit: int = 1, timeout: float = 3.0) -> bool:
        with self._lock:
            self._unit = unit
            try:
                if self._sock:
                    try: self._sock.close()
                    except: pass
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((host, port))
                self._sock = s
                return True
            except Exception:
                self._sock = None
                return False

    def close(self):
        with self._lock:
            if self._sock:
                try: self._sock.close()
                except: pass
                self._sock = None

    # ── public reads ──

    def read_f32_fc3(self, addr: int):
        resp = self._rr(0x03, addr, 2)
        if resp and resp[0] == 0x03 and len(resp) >= 6:
            return round(struct.unpack(">f", resp[2:6])[0], 2)
        return None

    def read_f32_fc4(self, addr: int):
        resp = self._rr(0x04, addr, 2)
        if resp and resp[0] == 0x04 and len(resp) >= 6:
            return round(struct.unpack(">f", resp[2:6])[0], 2)
        return None

    def write_f32_x3(self, addr: int, value: float) -> bool:
        """Schreibt denselben Float-Wert auf 3 aufeinanderfolgende Phasen-Register."""
        with self._lock:
            raw  = struct.pack(">f", float(value))
            data = raw * 3          # 3 Phasen × 4 Bytes = 12 Bytes = 6 Register
            pdu  = struct.pack(">BHHB", 0x10, addr, 6, 12) + data
            resp = self._req(pdu)
            return resp is not None and resp[0] == 0x10

    def read_u16_fc3(self, addr: int):
        resp = self._rr(0x03, addr, 1)
        if resp and resp[0] == 0x03 and len(resp) >= 4:
            return struct.unpack(">H", resp[2:4])[0]
        return None

    def status(self) -> dict:
        return {
            "current_l1": self.read_f32_fc4(1006),
            "current_l2": self.read_f32_fc4(1008),
            "current_l3": self.read_f32_fc4(1010),
            "limit":      self.read_f32_fc3(1012),
            "avail":      self.read_u16_fc3(1028),
        }

    # ── internals ──

    def _rr(self, fc: int, addr: int, count: int):
        with self._lock:
            return self._req(struct.pack(">BHH", fc, addr, count))

    def _req(self, pdu: bytes):
        if not self._sock:
            return None
        self._tid = (self._tid + 1) & 0xFFFF
        mbap = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self._unit)
        try:
            self._sock.sendall(mbap + pdu)
            hdr = self._recv(7)
            if not hdr:
                return None
            n = struct.unpack(">H", hdr[4:6])[0] - 1   # length - unit_byte
            return self._recv(n)
        except Exception:
            return None

    def _recv(self, n: int):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


# ── KV Layout ─────────────────────────────────────────────────────────────────

KV = """
#:import dp kivy.metrics.dp

<MainScreen>:
    name: 'main'
    canvas.before:
        Color:
            rgba: 0.051, 0.067, 0.090, 1
        Rectangle:
            pos: self.pos
            size: self.size

    BoxLayout:
        orientation: 'vertical'

        # ── Header ──
        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(16), dp(8)
            spacing: dp(8)
            canvas.before:
                Color:
                    rgba: 0.086, 0.106, 0.133, 1
                Rectangle:
                    pos: self.pos
                    size: self.size
            Label:
                text: '⚡  Wallbox'
                font_size: '18sp'
                bold: True
                color: 0.902, 0.929, 0.953, 1
                halign: 'left'
                text_size: self.size
                valign: 'center'
            Label:
                id: conn_label
                text: 'Verbinde…'
                font_size: '12sp'
                color: 0.545, 0.580, 0.620, 1
                size_hint_x: None
                width: dp(100)
                halign: 'right'
                text_size: self.size
                valign: 'center'
            Button:
                text: '⚙'
                font_size: '22sp'
                size_hint_x: None
                width: dp(44)
                background_normal: ''
                background_color: 0, 0, 0, 0
                color: 0.545, 0.580, 0.620, 1
                on_press: app.open_settings()

        # ── Content ──
        ScrollView:
            BoxLayout:
                orientation: 'vertical'
                padding: dp(14)
                spacing: dp(12)
                size_hint_y: None
                height: self.minimum_height

                # Status-Karte
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(104)
                    padding: dp(14), dp(10)
                    spacing: dp(2)
                    canvas.before:
                        Color:
                            rgba: 0.086, 0.106, 0.133, 1
                        RoundedRectangle:
                            pos: self.pos
                            size: self.size
                            radius: [12]
                    Label:
                        text: 'IST-STROM  /  LADELEISTUNG'
                        font_size: '10sp'
                        color: 0.545, 0.580, 0.620, 1
                        halign: 'left'
                        text_size: self.size
                        size_hint_y: None
                        height: dp(16)
                    BoxLayout:
                        size_hint_y: None
                        height: dp(42)
                        Label:
                            id: lbl_current
                            text: '—  A'
                            font_size: '30sp'
                            bold: True
                            color: 0.024, 0.714, 0.831, 1
                            halign: 'left'
                            text_size: self.size
                            valign: 'center'
                        Label:
                            id: lbl_power
                            text: '—  kW'
                            font_size: '30sp'
                            bold: True
                            color: 0.941, 0.753, 0.125, 1
                            halign: 'right'
                            text_size: self.size
                            valign: 'center'
                    Label:
                        id: lbl_limit
                        text: 'Limit: —'
                        font_size: '12sp'
                        color: 0.545, 0.580, 0.620, 1
                        halign: 'left'
                        text_size: self.size
                        size_hint_y: None
                        height: dp(18)

                # Schnellwahl
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(156)
                    padding: dp(14), dp(10)
                    spacing: dp(8)
                    canvas.before:
                        Color:
                            rgba: 0.086, 0.106, 0.133, 1
                        RoundedRectangle:
                            pos: self.pos
                            size: self.size
                            radius: [12]
                    Label:
                        text: 'SCHNELLWAHL'
                        font_size: '10sp'
                        color: 0.545, 0.580, 0.620, 1
                        halign: 'left'
                        text_size: self.size
                        size_hint_y: None
                        height: dp(16)
                    GridLayout:
                        cols: 3
                        spacing: dp(6)
                        row_force_default: True
                        row_default_height: dp(52)
                        Button:
                            id: btn_0
                            text: 'Stopp\n0 A'
                            font_size: '14sp'
                            bold: True
                            background_normal: ''
                            background_color: 0.18, 0.09, 0.09, 1
                            color: 0.97, 0.53, 0.53, 1
                            on_press: app.select(0)
                        Button:
                            id: btn_6
                            text: '6 A'
                            font_size: '16sp'
                            bold: True
                            background_normal: ''
                            background_color: 0.129, 0.149, 0.176, 1
                            color: 0.902, 0.929, 0.953, 1
                            on_press: app.select(6)
                        Button:
                            id: btn_8
                            text: '8 A'
                            font_size: '16sp'
                            bold: True
                            background_normal: ''
                            background_color: 0.129, 0.149, 0.176, 1
                            color: 0.902, 0.929, 0.953, 1
                            on_press: app.select(8)
                        Button:
                            id: btn_10
                            text: '10 A'
                            font_size: '16sp'
                            bold: True
                            background_normal: ''
                            background_color: 0.129, 0.149, 0.176, 1
                            color: 0.902, 0.929, 0.953, 1
                            on_press: app.select(10)
                        Button:
                            id: btn_13
                            text: '13 A'
                            font_size: '16sp'
                            bold: True
                            background_normal: ''
                            background_color: 0.129, 0.149, 0.176, 1
                            color: 0.902, 0.929, 0.953, 1
                            on_press: app.select(13)
                        Button:
                            id: btn_16
                            text: '16 A'
                            font_size: '16sp'
                            bold: True
                            background_normal: ''
                            background_color: 0.129, 0.149, 0.176, 1
                            color: 0.902, 0.929, 0.953, 1
                            on_press: app.select(16)

                # Slider
                BoxLayout:
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(100)
                    padding: dp(14), dp(10)
                    spacing: dp(8)
                    canvas.before:
                        Color:
                            rgba: 0.086, 0.106, 0.133, 1
                        RoundedRectangle:
                            pos: self.pos
                            size: self.size
                            radius: [12]
                    BoxLayout:
                        size_hint_y: None
                        height: dp(24)
                        Label:
                            text: '6 A'
                            font_size: '12sp'
                            color: 0.545, 0.580, 0.620, 1
                            size_hint_x: None
                            width: dp(30)
                        Label:
                            id: lbl_slider
                            text: '6.0 A'
                            font_size: '20sp'
                            bold: True
                            color: 0.024, 0.714, 0.831, 1
                        Label:
                            text: '16 A'
                            font_size: '12sp'
                            color: 0.545, 0.580, 0.620, 1
                            halign: 'right'
                            text_size: self.size
                            size_hint_x: None
                            width: dp(36)
                    Slider:
                        id: slider
                        min: 6
                        max: 16
                        step: 0.5
                        value: 6
                        cursor_size: dp(30), dp(30)
                        on_value: app.on_slider(self.value)

                # Anwenden-Button
                Button:
                    text: '⚡   Anwenden'
                    font_size: '17sp'
                    bold: True
                    size_hint_y: None
                    height: dp(62)
                    background_normal: ''
                    background_color: 0.137, 0.525, 0.212, 1
                    color: 1, 1, 1, 1
                    on_press: app.apply()

                # Hinweis wenn Backend läuft
                Label:
                    id: lbl_hint
                    text: ''
                    font_size: '11sp'
                    color: 0.941, 0.753, 0.125, 1
                    size_hint_y: None
                    height: dp(32)
                    text_size: self.width, None
                    halign: 'center'

<SettingsScreen>:
    name: 'settings'
    canvas.before:
        Color:
            rgba: 0.051, 0.067, 0.090, 1
        Rectangle:
            pos: self.pos
            size: self.size

    BoxLayout:
        orientation: 'vertical'
        padding: dp(16)
        spacing: dp(16)

        # Header
        BoxLayout:
            size_hint_y: None
            height: dp(48)
            spacing: dp(8)
            Button:
                text: '←'
                font_size: '22sp'
                size_hint_x: None
                width: dp(44)
                background_normal: ''
                background_color: 0, 0, 0, 0
                color: 0.902, 0.929, 0.953, 1
                on_press: app.close_settings()
            Label:
                text: 'Einstellungen'
                font_size: '18sp'
                bold: True
                color: 0.902, 0.929, 0.953, 1
                halign: 'left'
                text_size: self.size
                valign: 'center'

        Label:
            text: 'eBOX IP-Adresse'
            font_size: '13sp'
            color: 0.545, 0.580, 0.620, 1
            size_hint_y: None
            height: dp(20)
            halign: 'left'
            text_size: self.size

        TextInput:
            id: inp_host
            hint_text: '192.168.0.244'
            font_size: '17sp'
            foreground_color: 0.902, 0.929, 0.953, 1
            background_color: 0.086, 0.106, 0.133, 1
            cursor_color: 0.024, 0.714, 0.831, 1
            multiline: False
            size_hint_y: None
            height: dp(52)
            padding: dp(14), dp(14)

        Label:
            text: 'Modbus Unit ID  (meist 1)'
            font_size: '13sp'
            color: 0.545, 0.580, 0.620, 1
            size_hint_y: None
            height: dp(20)
            halign: 'left'
            text_size: self.size

        TextInput:
            id: inp_unit
            hint_text: '1'
            font_size: '17sp'
            foreground_color: 0.902, 0.929, 0.953, 1
            background_color: 0.086, 0.106, 0.133, 1
            cursor_color: 0.024, 0.714, 0.831, 1
            multiline: False
            input_filter: 'int'
            size_hint_y: None
            height: dp(52)
            padding: dp(14), dp(14)

        Button:
            text: 'Speichern & Verbinden'
            font_size: '16sp'
            bold: True
            size_hint_y: None
            height: dp(58)
            background_normal: ''
            background_color: 0.122, 0.325, 0.867, 1
            color: 1, 1, 1, 1
            on_press: app.save_settings()

        Widget:
"""


# ── Screens ───────────────────────────────────────────────────────────────────

class MainScreen(Screen):
    pass

class SettingsScreen(Screen):
    pass


# ── App ───────────────────────────────────────────────────────────────────────

PRESETS = [0, 6, 8, 10, 13, 16]
PRESET_IDS = {0: 'btn_0', 6: 'btn_6', 8: 'btn_8', 10: 'btn_10', 13: 'btn_13', 16: 'btn_16'}

class WallboxApp(App):

    def build(self):
        self.store   = JsonStore('wallbox_cfg.json')
        self.modbus  = Modbus()
        self.sel_amps = 6.0
        self._poll   = None
        sm = ScreenManager()
        Builder.load_string(KV)
        sm.add_widget(MainScreen())
        sm.add_widget(SettingsScreen())
        return sm

    def on_start(self):
        self._load_cfg()
        self._connect_bg()

    # ── settings persistence ──

    def _load_cfg(self):
        self._host = self.store.get('host')['v'] if self.store.exists('host') else '192.168.0.244'
        self._unit = int(self.store.get('unit')['v']) if self.store.exists('unit') else 1
        s = self.root.get_screen('settings')
        s.ids.inp_host.text = self._host
        s.ids.inp_unit.text = str(self._unit)

    def open_settings(self):
        self.root.current = 'settings'

    def close_settings(self):
        self.root.current = 'main'

    def save_settings(self):
        s = self.root.get_screen('settings')
        host = s.ids.inp_host.text.strip() or '192.168.0.244'
        unit = int(s.ids.inp_unit.text.strip() or '1')
        self.store.put('host', v=host)
        self.store.put('unit', v=unit)
        self._host = host
        self._unit = unit
        self._reconnect()
        self.root.current = 'main'

    # ── connection ──

    def _reconnect(self):
        if self._poll:
            self._poll.cancel()
            self._poll = None
        self.modbus.close()
        self._connect_bg()

    def _connect_bg(self):
        self._set_chip('Verbinde…', (0.545, 0.580, 0.620, 1))
        threading.Thread(target=self._connect_thread, daemon=True).start()

    def _connect_thread(self):
        ok = self.modbus.connect(self._host, unit=self._unit)
        self._on_connect(ok)

    @mainthread
    def _on_connect(self, ok):
        if ok:
            self._set_chip('● Verbunden', (0.302, 0.796, 0.384, 1))
            self._poll_once(0)
            self._poll = Clock.schedule_interval(self._poll_once, 4)
        else:
            self._set_chip('✗ Offline', (0.973, 0.318, 0.200, 1))
            Clock.schedule_once(lambda dt: self._connect_bg(), 6)

    # ── polling ──

    def _poll_once(self, dt):
        threading.Thread(target=self._poll_thread, daemon=True).start()

    def _poll_thread(self):
        s = self.modbus.status()
        self._update_ui(s)

    @mainthread
    def _update_ui(self, s):
        ids = self.root.get_screen('main').ids
        l1, l2, l3 = s.get('current_l1'), s.get('current_l2'), s.get('current_l3')
        limit = s.get('limit')

        # Connection lost?
        if l1 is None and l2 is None and l3 is None and limit is None:
            self._set_chip('✗ Verbindung verloren', (0.973, 0.318, 0.200, 1))
            if self._poll:
                self._poll.cancel()
                self._poll = None
            self.modbus.close()
            Clock.schedule_once(lambda dt: self._connect_bg(), 5)
            return

        vals = [v for v in [l1, l2, l3] if v is not None]
        avg  = sum(vals) / len(vals) if vals else 0.0
        kw   = avg * 3 * 230 / 1000

        ids.lbl_current.text = f'{avg:.1f} A'
        ids.lbl_power.text   = f'{kw:.2f} kW'
        ids.lbl_limit.text   = f'Limit: {limit:.1f} A' if limit is not None else 'Limit: —'

        self._set_chip('● Verbunden', (0.302, 0.796, 0.384, 1))
        self._highlight(limit)

    def _highlight(self, limit):
        ids = self.root.get_screen('main').ids
        for a, bid in PRESET_IDS.items():
            btn = ids.get(bid)
            if btn is None:
                continue
            active = limit is not None and abs(limit - a) < 0.4
            if a == 0:
                btn.background_color = (0.24, 0.06, 0.06, 1) if active else (0.18, 0.09, 0.09, 1)
            else:
                btn.background_color = (0.024, 0.20, 0.24, 1) if active else (0.129, 0.149, 0.176, 1)

    def _set_chip(self, text, color):
        try:
            self.root.get_screen('main').ids.conn_label.text  = text
            self.root.get_screen('main').ids.conn_label.color = color
        except Exception:
            pass

    # ── user actions ──

    def select(self, amps: float):
        self.sel_amps = float(amps)
        ids = self.root.get_screen('main').ids
        if amps >= 6:
            ids.slider.value = amps
        ids.lbl_slider.text = f'{float(amps):.1f} A'

    def on_slider(self, value):
        self.sel_amps = round(value * 2) / 2
        try:
            self.root.get_screen('main').ids.lbl_slider.text = f'{self.sel_amps:.1f} A'
        except Exception:
            pass

    def apply(self):
        amps = self.sel_amps
        ids  = self.root.get_screen('main').ids

        # Warn if backend might override (check port 8000 silently)
        def _write():
            ok = self.modbus.write_f32_x3(1012, amps)
            self._after_apply(ok, amps)

        ids.lbl_hint.text = f'Setze {amps:.1f} A…'
        threading.Thread(target=_write, daemon=True).start()

    @mainthread
    def _after_apply(self, ok: bool, amps: float):
        ids = self.root.get_screen('main').ids
        if ok:
            ids.lbl_hint.text = (
                f'✓ {amps:.1f} A gesetzt'
                if amps > 0
                else '✓ Ladung gestoppt'
            )
        else:
            ids.lbl_hint.text = '✗ Fehler beim Schreiben'
        Clock.schedule_once(lambda dt: self._clear_hint(), 3)

    def _clear_hint(self):
        try:
            self.root.get_screen('main').ids.lbl_hint.text = ''
        except Exception:
            pass


if __name__ == '__main__':
    WallboxApp().run()
