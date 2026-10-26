"""Питание видеокарты NVIDIA: мощность, напряжение, частота, вентилятор, температура.

- Мощность, предел мощности, частота, вентилятор, температура — nvidia-smi --query-gpu.
- Напряжение ядра — nvidia-smi -q -d VOLTAGE (есть не на всех драйверах и картах). Если там пусто —
  LibreHardwareMonitor (тот же веб-сервер, что для температуры процессора). Нет нигде — значит, None.

Ничего не бросает: нет данных — None в соответствующем поле.
"""

import json
import re
import subprocess
import urllib.request

from . import sensors

FULL = "power.draw,enforced.power.limit,clocks.gr,clocks.max.gr,fan.speed,temperature.gpu"
SHORT = "power.draw,power.limit,clocks.gr,clocks.max.gr,fan.speed,temperature.gpu"
KEYS = ("watts", "power_limit", "clock", "clock_max", "fan", "gpu_temp")
_MV = re.compile(r"Graphics\s*:\s*([\d.]+)\s*mV", re.I)
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
VOLTAGE_RETRY = 60          # если напряжение не нашлось, пробуем снова раз в 60 замеров


def _num(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None                       # «[N/A]», «[Not Supported]» и т. п.


def parse_query(output):
    """Первая строка csv без заголовка → {watts, power_limit, clock, clock_max, fan, gpu_temp}."""
    for line in (output or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= len(KEYS):
            return {k: _num(v) for k, v in zip(KEYS, parts)}
    return None


def parse_voltage(output):
    """«Graphics : 806.250 mV» из nvidia-smi -q -d VOLTAGE → 806.25 или None."""
    m = _MV.search(output or "")
    return float(m.group(1)) if m else None


def parse_lhm_voltage(data):
    """Напряжение ядра видеокарты (мВ) из дерева LibreHardwareMonitor или None."""
    found = []

    def walk(node, gpu):
        if not isinstance(node, dict):
            return
        if node.get("Children") and not gpu:
            gpu = sensors._kind(node) == "gpu"
        if gpu and not node.get("Children"):
            value = str(node.get("Value") or "").strip()
            name = str(node.get("Text") or "").lower()
            if value.endswith("V") and not value.lower().endswith("mv"):
                m = _NUM.search(value)
                if m:
                    volts = float(m.group(0).replace(",", "."))
                    if 0.3 < volts < 1.5:
                        found.append((0 if "core" in name else 1, volts * 1000.0))
        for child in node.get("Children") or []:
            walk(child, gpu)

    walk(data, False)
    return min(found)[1] if found else None


class PowerReader:
    def __init__(self, smi=None, run=subprocess.run, lhm_url=None, opener=None):
        self.smi = smi
        self.run = run
        self.lhm_url = lhm_url
        self.opener = opener
        self.query = FULL
        self.voltage_source = None        # "nvidia-smi", "LibreHardwareMonitor" или None
        self._skip_voltage = 0
        self.reason = ""                  # почему нет мощности (тогда и всего остального)
        self.voltage_note = ""            # почему нет напряжения (это не ошибка: у многих карт его не видно)

    def _call(self, args):
        smi = self.smi or sensors.find_nvidia_smi()
        if not smi:
            return None
        try:
            result = self.run([smi] + args, capture_output=True, text=True, timeout=10,
                              creationflags=sensors.NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    def _voltage(self):
        if self._skip_voltage > 0:
            self._skip_voltage -= 1
            return None
        mv = parse_voltage(self._call(["-q", "-d", "VOLTAGE"]))
        if mv is not None:
            self.voltage_source = "nvidia-smi"
            return mv
        if self.lhm_url:
            try:
                open_url = self.opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
                with open_url(self.lhm_url, timeout=3) as resp:
                    mv = parse_lhm_voltage(json.loads(resp.read().decode("utf-8", "replace")))
            except Exception:
                mv = None
            if mv is not None:
                self.voltage_source = "LibreHardwareMonitor"
                return mv
        self.voltage_source = None
        self._skip_voltage = VOLTAGE_RETRY
        return None

    def read(self):
        """{watts, power_limit, clock, clock_max, fan, gpu_temp, volts (мВ)} или None (нет nvidia-smi)."""
        out = self._call(["--query-gpu=" + self.query, "--format=csv,noheader,nounits"])
        if out is None and self.query == FULL:          # старый драйвер не знает enforced.power.limit
            self.query = SHORT
            out = self._call(["--query-gpu=" + self.query, "--format=csv,noheader,nounits"])
        row = parse_query(out)
        if row is None:
            self.reason = "nvidia-smi не отдаёт мощность видеокарты"
            return None
        row["volts"] = self._voltage()
        self.reason = ""
        if row["volts"] is None and self.voltage_source is None:
            self.voltage_note = ("напряжение не видно: nvidia-smi его не отдаёт, а LibreHardwareMonitor "
                                 "(Remote Web Server, порт 8085) не запущен")
        else:
            self.voltage_note = ""
        return row
