"""Журнал нагрузки: сколько хранится, что пишется в файл и как читается после перезапуска."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.core.history import History  # noqa: E402

T0 = 1_700_000_000


def row(t, cpu=10):
    return {"t": t, "cpu": cpu, "apps": {"chrome": {"cpu": 5, "ram": 100, "gpu": 0, "vram": 0}}}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.jsonl"
        self.now = T0
        self.h = History(self.path, hours=1, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_only_set_hours(self):
        for i in range(800):                       # 800 замеров по 10 с — больше 2 часов
            self.h.add(row(T0 + 10 * i))
        self.assertEqual(len(self.h), 361)         # ровно час замеров
        self.assertEqual(self.h.all()[0]["t"], T0 + 10 * 439)

    def test_hours_can_grow_and_shrink(self):
        for i in range(400):
            self.h.add(row(T0 + 10 * i))
        self.h.set_hours(12)
        self.assertEqual(len(self.h), 361)         # старое уже выбросили, но новое теперь копится
        for i in range(400, 800):
            self.h.add(row(T0 + 10 * i))
        self.assertEqual(len(self.h), 761)
        self.h.set_hours(0.5)
        self.assertEqual(len(self.h), 181)

    def test_tail_by_minutes(self):
        for i in range(360):
            self.h.add(row(T0 + 10 * i))
        self.assertEqual(len(self.h.tail(30)), 181)
        self.assertEqual(len(self.h.tail(5)), 31)
        self.assertEqual(self.h.last()["t"], T0 + 3590)

    def test_file_survives_restart(self):
        for i in range(30):
            self.h.add(row(T0 + 10 * i))
        self.now = T0 + 300
        again = History(self.path, hours=1, clock=lambda: self.now)
        self.assertEqual(again.load(), 30)
        self.assertEqual(again.last()["t"], T0 + 290)
        self.assertEqual(again.all()[0]["apps"]["chrome"]["ram"], 100)

    def test_old_and_broken_lines_are_skipped(self):
        self.path.write_text("\n".join([
            json.dumps(row(T0 - 7200)),        # два часа назад — старьё
            "{битая строка",
            json.dumps(row(T0 - 60)),
            json.dumps({"нет времени": 1}),
            json.dumps(row(T0 + 99999)),       # из будущего (часы ПК съехали)
        ]) + "\n", encoding="utf-8")
        self.assertEqual(self.h.load(now=T0), 1)
        self.assertEqual(self.h.last()["t"], T0 - 60)

    def test_file_is_trimmed(self):
        self.h._last_trim = T0 - 10_000
        for i in range(500):
            self.h.add(row(T0 + 10 * i))
        self.h.trim_file()
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), len(self.h))
        self.assertEqual(json.loads(lines[-1])["t"], T0 + 4990)

    def test_missing_file_is_not_an_error(self):
        h = History(Path(self.tmp.name) / "нет" / "history.jsonl", hours=1, clock=lambda: self.now)
        self.assertEqual(h.load(), 0)
        h.add(row(T0))
        self.assertEqual(len(h), 1)


if __name__ == "__main__":
    unittest.main()
