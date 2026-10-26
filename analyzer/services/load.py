"""Загрузка ПК: процессор, видеокарта, оперативная память. Только стандартная библиотека.

- Процессор: функция Windows GetSystemTimes (на других системах /proc/stat). Процент считается
  между двумя замерами, поэтому первый замер после включения пустой.
- Видеокарта NVIDIA: nvidia-smi (загрузка и видеопамять), см. hub/services/sensors.py.
- Память: hub/services/sensors.read_ram.

Функции возвращают данные или None и не бросают исключений.
"""

import ctypes
import os
from pathlib import Path

from . import sensors


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def _ft(value):
    return (value.high << 32) | value.low


def read_cpu_times():
    """(простой, всего) в условных единицах с момента включения ПК, или None."""
    try:
        if os.name == "nt":
            idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                         ctypes.byref(user)):
                return None
            # время ядра уже включает простой
            return _ft(idle), _ft(kernel) + _ft(user)
        return parse_proc_stat(Path("/proc/stat").read_text())
    except Exception:
        return None


def parse_proc_stat(text):
    for line in (text or "").splitlines():
        if line.startswith("cpu "):
            nums = [int(x) for x in line.split()[1:]]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            return idle, sum(nums)
    return None


class CpuLoad:
    """Загрузка процессора в процентах между соседними вызовами read()."""

    def __init__(self, reader=read_cpu_times):
        self.reader = reader
        self._last = None

    def read(self):
        now = self.reader()
        last, self._last = self._last, now
        if now is None or last is None:
            return None
        idle = now[0] - last[0]
        total = now[1] - last[1]
        if total <= 0:
            return None
        return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)


def read_gpu():
    """{"load": %, "mem_used": МБ, "mem_total": МБ, "name": …} или None (нет NVIDIA)."""
    try:
        gpu = sensors.read_gpu_nvidia()
    except Exception:
        return None
    if not gpu or gpu.get("load") is None:
        return None
    return {"load": gpu["load"], "mem_used": gpu.get("mem_used"), "mem_total": gpu.get("mem_total"),
            "name": gpu.get("name", "")}
