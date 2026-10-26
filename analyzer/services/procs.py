"""Сколько ресурсов берёт каждая программа.

- Процессор и оперативная память — библиотека psutil (одна на все программы, группируем по имени:
  у браузера десятки процессов, а нужна одна строка «chrome»).
- Видеокарта и видеопамять по программам — счётчики Windows «GPU Engine» и «GPU Process Memory»
  (их же показывает Диспетчер задач). nvidia-smi по программам на Windows обычно пуст, поэтому не он.
  Счётчики читаем через pdh.dll по английским именам — так работает и на русской Windows.

Функции не бросают исключений: нет данных — None, причина — в поле .reason.
"""

import ctypes
import os
import re

IDLE_NAMES = {"system idle process", "idle"}


def short_name(name):
    """«chrome.exe» → «chrome»."""
    name = str(name or "").strip()
    return name[:-4] if name.lower().endswith(".exe") else name


class AppReader:
    """Процессор (% от всего процессора, как в Диспетчере задач) и память (МБ) по программам."""

    def __init__(self):
        self.reason = ""
        self._psutil = None
        try:
            import psutil  # noqa: PLC0415 — необязательная библиотека, без неё модуль просто без данных
            self._psutil = psutil
            self._cores = psutil.cpu_count() or 1
        except Exception:
            self.reason = "не установлена библиотека psutil (запустите run.bat — он её поставит)"

    def read(self):
        """({имя: {"cpu", "ram", "n"}}, {pid: имя}) или None."""
        ps = self._psutil
        if ps is None:
            return None
        apps, pids = {}, {}
        try:
            for p in ps.process_iter(["name", "memory_info"]):
                try:
                    if p.pid == 0:
                        continue
                    name = short_name(p.info.get("name") or "")
                    if not name or name.lower() in IDLE_NAMES:
                        continue
                    cpu = p.cpu_percent(None) / self._cores   # первый вызов для нового процесса — 0
                    mem = p.info.get("memory_info")
                    ram = (mem.rss / 1048576.0) if mem else 0.0
                except Exception:
                    continue
                pids[p.pid] = name
                a = apps.setdefault(name, {"cpu": 0.0, "ram": 0.0, "n": 0})
                a["cpu"] += cpu
                a["ram"] += ram
                a["n"] += 1
        except Exception as exc:
            self.reason = "не удалось прочитать список программ: %s" % exc
            return None
        self.reason = ""
        return apps, pids


# ---------- видеокарта по программам: счётчики Windows ----------

_PID = re.compile(r"pid_(\d+)_")
_ENG = re.compile(r"engtype_([A-Za-z0-9]+)")


def parse_gpu_instances(engines, memory):
    """engines: [(имя экземпляра, % загрузки)], memory: [(имя, байт)] → {pid: {"gpu": %, "vram": МБ}}.
    Загрузка программы — самый занятый тип движка (3D, Copy, Video…), как в Диспетчере задач."""
    per = {}
    for name, value in engines or []:
        m, e = _PID.search(name or ""), _ENG.search(name or "")
        if not m:
            continue
        kinds = per.setdefault(int(m.group(1)), {})
        kind = e.group(1) if e else "?"
        kinds[kind] = kinds.get(kind, 0.0) + max(0.0, float(value or 0))
    out = {pid: {"gpu": min(100.0, max(kinds.values()) if kinds else 0.0), "vram": 0.0} for pid, kinds in per.items()}
    for name, value in memory or []:
        m = _PID.search(name or "")
        if not m:
            continue
        row = out.setdefault(int(m.group(1)), {"gpu": 0.0, "vram": 0.0})
        row["vram"] += max(0.0, float(value or 0)) / 1048576.0
    return out


PDH_MORE_DATA = 0x800007D2
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000


class _Value(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_longlong),
                ("ptr", ctypes.c_void_p)]


class _FmtValue(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("u", _Value)]


class _Item(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FmtValue)]


class GpuCounters:
    """Читает загрузку видеокарты и видеопамять по процессам. Запрос держим открытым:
    загрузка считается между двумя чтениями, поэтому первое чтение пустое."""

    ENGINE = "\\GPU Engine(*)\\Utilization Percentage"
    MEMORY = "\\GPU Process Memory(*)\\Dedicated Usage"

    def __init__(self):
        self.reason = ""
        self._pdh = None
        self._query = None
        self._counters = None
        self._broken = os.name != "nt"
        if self._broken:
            self.reason = "счётчики видеокарты по программам есть только в Windows"

    def _open(self):
        pdh = ctypes.WinDLL("pdh")
        vp, dw = ctypes.c_void_p, ctypes.c_ulong
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(vp)]
        pdh.PdhAddEnglishCounterW.argtypes = [vp, ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(vp)]
        pdh.PdhCollectQueryData.argtypes = [vp]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [vp, dw, ctypes.POINTER(dw), ctypes.POINTER(dw), vp]
        pdh.PdhCloseQuery.argtypes = [vp]
        for fn in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
                   pdh.PdhGetFormattedCounterArrayW, pdh.PdhCloseQuery):
            fn.restype = ctypes.c_ulong
        query = vp()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            raise OSError("Windows не дал открыть счётчики")
        counters = []
        for path in (self.ENGINE, self.MEMORY):
            handle = vp()
            status = pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(handle))
            if status != 0:
                pdh.PdhCloseQuery(query)
                raise OSError("в этой Windows нет счётчика %s (код %#x)" % (path, status))
            counters.append(handle)
        pdh.PdhCollectQueryData(query)
        self._pdh, self._query, self._counters = pdh, query, counters

    def _array(self, counter, fmt):
        pdh = self._pdh
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        status = pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if status != PDH_MORE_DATA or not size.value:
            return []                                  # нет экземпляров или ещё нет второго замера
        buf = ctypes.create_string_buffer(size.value)
        status = pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if status != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        return [(items[i].szName, items[i].FmtValue.u.doubleValue) for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)]

    def read(self):
        """{pid: {"gpu": %, "vram": МБ}} или None."""
        if self._broken:
            return None
        try:
            if self._query is None:
                self._open()
                return {}
            self._pdh.PdhCollectQueryData(self._query)
            engines = self._array(self._counters[0], PDH_FMT_DOUBLE | PDH_FMT_NOCAP100)
            memory = self._array(self._counters[1], PDH_FMT_DOUBLE)
        except Exception as exc:
            self._broken = True
            self.reason = "видеокарту по программам узнать не удалось: %s" % exc
            return None
        self.reason = ""
        return parse_gpu_instances(engines, memory)

    def close(self):
        if self._query is not None and self._pdh is not None:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except Exception:
                pass
        self._query = None
