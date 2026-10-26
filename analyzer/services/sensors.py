"""Датчики ПК: температура видеокарты и процессора, занятость оперативной памяти. Только стандартная библиотека.

- Видеокарта NVIDIA: программа nvidia-smi (ставится вместе с драйвером).
- Процессор: Windows сама температуру процессора не отдаёт. Её берём у бесплатной программы
  LibreHardwareMonitor (включить Options → Remote Web Server, адрес http://localhost:8085).
  Если nvidia-smi не нашлась, температуру видеокарты тоже берём оттуда.
- Память: функция Windows GlobalMemoryStatusEx (на других системах — /proc/meminfo, для проверки).

Каждая функция возвращает данные или None и никогда не бросает исключений.
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------- видеокарта: nvidia-smi ----------

def find_nvidia_smi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidates = [Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI"
                  / "nvidia-smi.exe"]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def parse_nvidia_smi(output):
    """Первая строка ответа nvidia-smi (csv без заголовка): name, temperature, utilization, mem used, mem total."""
    for line in (output or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            temp = float(parts[1])
        except ValueError:
            continue

        def num(i):
            try:
                return float(parts[i])
            except (IndexError, ValueError):
                return None

        return {"name": parts[0], "temp": temp, "load": num(2), "mem_used": num(3), "mem_total": num(4)}
    return None


def read_gpu_nvidia(smi=None, run=subprocess.run):
    smi = smi or find_nvidia_smi()
    if not smi:
        return None
    try:
        result = run([smi, "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                      "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                     creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_nvidia_smi(result.stdout)


# ---------- LibreHardwareMonitor (веб-сервер программы) ----------

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
CPU_PRIORITY = ("cpu package", "core (tctl/tdie)", "tctl/tdie", "core (tctl)", "tdie", "tctl", "core max",
                "core average", "cpu")
GPU_PRIORITY = ("gpu core", "gpu")


def _value(text):
    """«54,0 °C» → 54.0. Только температуры (°C)."""
    text = str(text or "")
    if "°C" not in text and "° C" not in text:
        return None
    match = _NUM.search(text)
    return float(match.group(0).replace(",", ".")) if match else None


def _kind(node):
    """cpu / gpu / None по значку и названию узла-устройства."""
    image = str(node.get("ImageURL") or "").lower()
    name = str(node.get("Text") or "").lower()
    if "cpu" in image or any(w in name for w in ("intel core", "ryzen", "amd ryzen", "xeon", "threadripper")):
        return "cpu"
    if any(w in image for w in ("nvidia", "ati", "amd", "gpu")) or any(w in name for w in ("geforce", "radeon",
                                                                                              "rtx", "gtx")):
        return "gpu"
    return None


def parse_lhm(data):
    """Дерево data.json → {"cpu": °C или None, "gpu": °C или None, "cpu_name": …, "gpu_name": …}."""
    found = {"cpu": [], "gpu": []}
    names = {}

    def walk(node, kind):
        if not isinstance(node, dict):
            return
        own = _kind(node) if node.get("Children") else None
        if own and kind is None:
            kind = own
            names.setdefault(own, str(node.get("Text") or ""))
        if kind and not node.get("Children"):
            value = _value(node.get("Value"))
            if value is not None and 0 < value < 150:
                found[kind].append((str(node.get("Text") or "").strip().lower(), value))
        for child in node.get("Children") or []:
            walk(child, kind)

    walk(data, None)

    def pick(items, priority):
        for wanted in priority:
            for name, value in items:
                if name == wanted:
                    return value
        cores = [v for n, v in items if n.startswith("core") or n.startswith("cpu core")]
        if cores:
            return max(cores)
        return max((v for _, v in items), default=None)

    return {"cpu": pick(found["cpu"], CPU_PRIORITY), "gpu": pick(found["gpu"], GPU_PRIORITY),
            "cpu_name": names.get("cpu", ""), "gpu_name": names.get("gpu", "")}


def read_lhm(url, timeout=3, opener=None):
    if not url:
        return None
    try:
        open_url = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        with open_url(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    try:
        return parse_lhm(data)
    except Exception:
        return None


# ---------- оперативная память ----------

class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def parse_meminfo(text):
    values = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        match = _NUM.search(rest)
        if match:
            values[key.strip()] = float(match.group(0)) * 1024
    total, avail = values.get("MemTotal"), values.get("MemAvailable")
    if not total or avail is None:
        return None
    used = total - avail
    return {"percent": round(used * 100.0 / total, 1), "used": used, "total": total}


def read_ram():
    """{"percent": занято %, "used": байт, "total": байт} или None."""
    try:
        if os.name == "nt":
            st = _MemStatus()
            st.dwLength = ctypes.sizeof(_MemStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            used = st.ullTotalPhys - st.ullAvailPhys
            return {"percent": round(used * 100.0 / st.ullTotalPhys, 1), "used": float(used),
                    "total": float(st.ullTotalPhys)}
        return parse_meminfo(Path("/proc/meminfo").read_text())
    except Exception:
        return None


# ---------- всё вместе ----------

def read_all(lhm_url):
    """Один замер: {"cpu", "gpu", "gpu_load", "ram", "ram_used", "ram_total", "cpu_name", "gpu_name", "cpu_source"}."""
    out = {"cpu": None, "gpu": None, "gpu_load": None, "ram": None, "ram_used": None, "ram_total": None,
           "cpu_name": "", "gpu_name": "", "lhm": False}
    gpu = read_gpu_nvidia()
    if gpu:
        out.update(gpu=gpu["temp"], gpu_load=gpu["load"], gpu_name=gpu["name"],
                   vram_used=gpu["mem_used"], vram_total=gpu["mem_total"])
    lhm = read_lhm(lhm_url)
    if lhm:
        out["lhm"] = True
        out["cpu"], out["cpu_name"] = lhm["cpu"], lhm["cpu_name"]
        if out["gpu"] is None:
            out["gpu"], out["gpu_name"] = lhm["gpu"], lhm["gpu_name"]
    ram = read_ram()
    if ram:
        out.update(ram=ram["percent"], ram_used=ram["used"], ram_total=ram["total"])
    return out
