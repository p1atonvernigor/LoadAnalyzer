"""Журнал событий: последние записи в памяти (для интерфейса) и файл на диске."""

import threading
import time
from collections import deque
from pathlib import Path

MAX_FILE_BYTES = 2_000_000


class Logbook:
    def __init__(self, file=None, maxlen=300):
        self._items = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._file = Path(file) if file else None
        if self._file:
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._file = None

    def add(self, source, message, level="info"):
        now = time.localtime()
        item = {
            "time": time.strftime("%H:%M:%S", now),
            "level": level,
            "source": source,
            "message": str(message),
        }
        with self._lock:
            self._items.appendleft(item)
            if self._file:
                self._write(now, item)
        return item

    def _write(self, now, item):
        try:
            if self._file.exists() and self._file.stat().st_size > MAX_FILE_BYTES:
                self._file.replace(self._file.with_name(self._file.name + ".1"))
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", now)
            line = "%s [%s] %s: %s\n" % (stamp, item["level"], item["source"], item["message"])
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def recent(self, n=50):
        """Последние записи, самые новые первыми."""
        with self._lock:
            return list(self._items)[:n]
