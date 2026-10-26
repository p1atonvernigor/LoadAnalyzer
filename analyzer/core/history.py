"""Журнал нагрузки: замеры в памяти плюс файл data/history.jsonl, чтобы они не терялись при перезапуске.

Сколько хранить — настройка history_hours (по умолчанию 12 часов). Всё, что старше, выбрасывается
из памяти сразу, а из файла — при запуске и примерно раз в 10 минут.

Один замер — строка JSON (см. analyzer/services/spikes.py). За 12 часов при замере каждые 10 секунд
это около 4 тысяч строк, несколько мегабайт.
"""

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

TRIM_EVERY = 600            # раз в 10 минут переписываем файл без старых строк
MAX_LINE = 200_000          # защита от битой строки длиной в мегабайты


class History:
    def __init__(self, path, hours=12, clock=time.time):
        self.path = Path(path)
        self.hours = float(hours or 12)
        self.clock = clock
        self.samples = deque()
        self._lock = threading.Lock()
        self._last_trim = 0.0
        self._file_broken = ""

    # ---------- чтение ----------

    def seconds(self):
        return max(60.0, self.hours * 3600.0)

    def all(self):
        with self._lock:
            return list(self.samples)

    def tail(self, minutes):
        """Замеры за последние minutes минут (по времени последнего замера)."""
        rows = self.all()
        if not rows:
            return []
        edge = rows[-1]["t"] - max(1.0, float(minutes)) * 60.0
        return [s for s in rows if s["t"] >= edge]

    def last(self):
        with self._lock:
            return self.samples[-1] if self.samples else None

    def __len__(self):
        return len(self.samples)

    # ---------- запись ----------

    def set_hours(self, hours):
        self.hours = float(hours or 12)
        self._cut()

    def add(self, row):
        with self._lock:
            self.samples.append(row)
        self._cut()
        self._append_file(row)

    def _cut(self):
        with self._lock:
            if not self.samples:
                return
            edge = self.samples[-1]["t"] - self.seconds()
            while self.samples and self.samples[0]["t"] < edge:
                self.samples.popleft()

    def _append_file(self, row):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._file_broken = ""
        except OSError as exc:
            self._file_broken = "журнал не пишется в файл: %s" % exc
            return
        now = self.clock()
        if now - self._last_trim > TRIM_EVERY:
            self._last_trim = now
            self.trim_file()

    def trim_file(self):
        """Переписывает файл, оставляя только свежие замеры."""
        rows = self.all()
        if not rows:
            return
        edge = rows[-1]["t"] - self.seconds()
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for s in rows:
                    if s["t"] >= edge:
                        f.write(json.dumps(s, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            self._file_broken = "журнал не пишется в файл: %s" % exc

    # ---------- загрузка при запуске ----------

    def load(self, now=None):
        """Читает файл и оставляет замеры за последние history_hours. Битые строки пропускает."""
        now = self.clock() if now is None else now
        edge = now - self.seconds()
        rows = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    if len(line) > MAX_LINE:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("t"), (int, float)) and row["t"] >= edge \
                            and row["t"] <= now + 60:
                        rows.append(row)
        except OSError:
            return 0
        rows.sort(key=lambda s: s["t"])
        with self._lock:
            self.samples = deque(rows)
        return len(rows)

    def note(self):
        return self._file_broken
