"""Сбор журнала и ответы ИИ: замер, события, окно показа, вопрос, настройки, подсказка про LHM.

Настоящих датчиков, psutil и интернета здесь нет — всё подменяется.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.core.secrets import Secrets  # noqa: E402
from analyzer.core.settings import Settings  # noqa: E402
from analyzer.monitor import Monitor  # noqa: E402

T0 = 1_700_000_000
KEY = "AIzaSyFAKEKEYFAKEKEYFAKEKEY"


class FakeApps:
    """Подмена psutil: что берут программы и какие у них pid."""

    def __init__(self, apps=None, pids=None, reason=""):
        self.apps = apps or {}
        self.pids = pids or {}
        self.reason = reason
        self._cores = 8

    def read(self):
        if self.reason:
            return None
        return ({n: dict(v) for n, v in self.apps.items()}, dict(self.pids))


class FakeGpuApps:
    def __init__(self, per_pid=None, reason=""):
        self.per_pid = per_pid or {}
        self.reason = reason

    def read(self):
        return dict(self.per_pid) if not self.reason else None

    def close(self):
        pass


class FakeModel:
    """Подмена Gemini: помнит, о чём спрашивали, и отдаёт заготовленный ответ."""

    def __init__(self, answer="Коротко: всё из-за игры.\n- game взяла 90%\nСовет: закройте игру.", error=None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, key, system, question, **kw):
        self.calls.append({"key": key, "system": system, "question": question, "kw": kw})
        if self.error:
            raise self.error
        return self.answer


def make_monitor(tmp, settings=None, secrets=None, **values):
    """Монитор с подменёнными датчиками. Время идёт шагами по poll_seconds."""
    root = Path(tmp)
    (root / "config").mkdir(exist_ok=True)
    st = settings if settings is not None else Settings(root / "config")
    for key, value in values.items():
        st.values[key] = value
    secrets = secrets if secrets is not None else Secrets(root)
    m = Monitor(st, root)
    m.secrets = secrets
    m.key = secrets.key
    m.cpu = type("C", (), {"read": staticmethod(lambda: 20.0), "reader": None})()
    m.read_gpu = lambda: {"load": 30.0, "mem_used": 2000.0, "mem_total": 8192.0, "name": "RTX 3060 Ti"}
    m.read_ram = lambda: {"percent": 55.0, "used": 9e9, "total": 1.7e10}
    m.apps = FakeApps({"chrome": {"cpu": 6.0, "ram": 1500.0, "n": 12}}, {10: "chrome"})
    m.gpu_apps = FakeGpuApps({10: {"gpu": 12.0, "vram": 900.0}})
    m.read_power = lambda: {"watts": 150.0, "power_limit": 200.0, "clock": 1800.0, "clock_max": 2100.0,
                            "fan": 50.0, "gpu_temp": 65.0, "volts": 900.0}
    m.read_lhm = lambda url: {"cpu": 54.0, "gpu": 65.0, "cpu_name": "Ryzen", "gpu_name": "RTX"}
    m.ask_model = FakeModel()
    m.sync = True
    return m


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = make_monitor(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tick_joins_gpu_to_programs_by_pid(self):
        row = self.m.tick(T0)
        self.assertEqual(row["cpu"], 20.0)
        self.assertEqual(row["gpu"], 30.0)
        self.assertEqual(row["vram"], 2000.0)
        self.assertEqual(row["apps"]["chrome"]["gpu"], 12.0)
        self.assertEqual(row["apps"]["chrome"]["vram"], 900)
        self.assertEqual(row["watts"], 150.0)

    def test_temperatures_come_from_sensors(self):
        row = self.m.tick(T0)
        self.assertEqual(row["cpu_temp"], 54.0)
        self.assertEqual(row["gpu_temp"], 65.0)     # от nvidia-smi, не из LHM
        self.assertTrue(self.m.lhm_ok)
        self.assertIsNone(self.m.lhm_hint())

    def test_hint_when_no_libre_hardware_monitor(self):
        self.m.read_lhm = lambda url: None
        row = self.m.tick(T0)
        self.assertNotIn("cpu_temp", row)
        self.assertFalse(self.m.lhm_ok)
        hint = self.m.lhm_hint()
        self.assertIn("LibreHardwareMonitor", hint["text"])
        self.assertIn("github.com/LibreHardwareMonitor", hint["url"])
        self.assertTrue(hint["first_run"])
        self.m.hint_opened()
        self.assertFalse(self.m.lhm_hint()["first_run"])    # второй раз сами не открываем
        self.m.hint_hide()
        self.assertIsNone(self.m.lhm_hint())

    def test_lhm_is_not_asked_every_tick_when_missing(self):
        asked = []
        self.m.read_lhm = lambda url: asked.append(url) or None
        for i in range(5):
            self.m.tick(T0 + 10 * i)
        self.assertEqual(len(asked), 1)                     # спросим снова через 30 замеров

    def test_history_keeps_hours_and_tracks_starts(self):
        self.m.settings.values["history_hours"] = 1
        for i in range(360):
            if i == 100:
                self.m.apps.apps["game"] = {"cpu": 20.0, "ram": 3000.0, "n": 1}
                self.m.apps.pids[20] = "game"
            if i == 300:
                self.m.apps.apps.pop("game")
                self.m.apps.pids.pop(20)
            self.m.tick(T0 + 10 * i)
        self.assertEqual(len(self.m.history), 360)
        kinds = [(k, n) for _, k, n in self.m.events]
        self.assertIn(("start", "game"), kinds)
        self.assertIn(("stop", "game"), kinds)
        for i in range(360, 800):                 # час прошёл — старые замеры и события уходят
            self.m.tick(T0 + 10 * i)
        self.assertEqual(len(self.m.history), 361)
        self.assertEqual(self.m.events, [])

    def test_window_never_exceeds_history(self):
        self.m.settings.values.update(history_hours=1, window_minutes=720)
        self.assertEqual(self.m._window(), 60)

    def test_views_have_vram_first_and_temps(self):
        for i in range(60):
            self.m.tick(T0 + 10 * i)
        self.m.clock = lambda: T0 + 600
        tiles = self.m.now_tiles()
        self.assertEqual(tiles[0]["label"], "Видеопамять")
        self.assertIn("из 8 ГБ", tiles[0]["value"])
        labels = [t["label"] for t in tiles]
        self.assertIn("Температура процессора", labels)
        chart = self.m.live_chart()
        self.assertEqual(chart["series"][0]["name"], "Видеопамять")
        self.assertEqual(chart["minutes"], 30)
        self.assertIn("30 мин", chart["title"])
        temps = self.m.temps_view()
        self.assertEqual([s["name"] for s in temps["chart"]["series"]], ["GPU", "CPU"])
        self.assertTrue(temps["chart"]["lines"])            # пунктир порогов внимания
        power = self.m.power_view()
        self.assertEqual(power["tiles"][0]["label"], "Мощность")
        self.assertTrue(any("Мощность" in c["title"] for c in power["charts"]))
        rows = self.m.now_rows()
        self.assertEqual(rows[0]["name"], "chrome")

    def test_long_window_in_titles(self):
        self.m.settings.values.update(history_hours=12, window_minutes=720)
        for i in range(60):
            self.m.tick(T0 + 10 * i)
        self.assertIn("12 ч", self.m.live_chart()["title"])
        self.assertEqual(self.m.live_chart()["minutes"], 720)

    def test_game_detection(self):
        self.m.apps.apps["game"] = {"cpu": 20.0, "ram": 3000.0, "n": 1}
        self.m.apps.pids[20] = "game"
        self.m.gpu_apps.per_pid[20] = {"gpu": 90.0, "vram": 4000.0}
        self.m.tick(T0)
        self.assertEqual(self.m.game_name(T0 + 5), "game")
        self.assertEqual(self.m.game_name(T0 + 600), "")     # замер уже не свежий

    def test_gaps_are_shown(self):
        self.m.apps = FakeApps(reason="не установлена библиотека psutil")
        self.m.tick(T0)
        self.assertIn("программы — не установлена библиотека psutil", self.m._gaps())

    def test_state_for_window(self):
        self.m.tick(T0)
        self.m.clock = lambda: T0 + 5
        s = self.m.state()
        self.assertEqual(s["samples"], 1)
        self.assertEqual(s["window_text"], "30 мин")
        self.assertEqual(s["history_hours"], 12.0)
        self.assertFalse(s["key_set"])
        self.assertEqual(s["model"], "gemini-flash-latest")


class AskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = make_monitor(self.tmp.name)
        for i in range(60):
            self.m.tick(T0 + 10 * i)
        self.m.clock = lambda: T0 + 600

    def tearDown(self):
        self.tmp.cleanup()

    def test_needs_data(self):
        empty = make_monitor(self.tmp.name)
        empty.secrets.set_key(KEY)
        with self.assertRaises(ValueError) as e:
            empty.ask("почему?")
        self.assertIn("замеров ещё нет", str(e.exception))

    def test_needs_key(self):
        with self.assertRaises(ValueError) as e:
            self.m.ask("почему?")
        self.assertIn("ключа Gemini", str(e.exception))

    def test_answer_has_facts_and_digest(self):
        self.m.secrets.set_key(KEY)
        r = self.m.ask("почему вырос GPU?")
        self.assertEqual(r["error"], "")
        self.assertEqual(r["headline"], "Всё из-за игры.")
        self.assertEqual(r["points"], ["game взяла 90%"])
        self.assertEqual(r["advice"], "закройте игру.")
        self.assertEqual(r["window_minutes"], 30)
        call = self.m.ask_model.calls[0]
        self.assertEqual(call["key"], KEY)
        self.assertIn("Журнал нагрузки ПК", call["question"])
        self.assertIn("Вопрос владельца: почему вырос GPU?", call["question"])
        self.assertIn("за последние 30 мин", call["system"])
        self.assertEqual(call["kw"]["model"], "gemini-flash-latest")
        self.assertIn("RTX 3060 Ti", call["question"])
        self.assertIn("Видеопамять", call["question"])
        self.assertTrue(r["top"])
        self.assertEqual(r["top"][0]["metric"], "vram")

    def test_default_question_when_empty(self):
        self.m.secrets.set_key(KEY)
        r = self.m.ask("   ")
        self.assertIn("Почему был скачок", r["question"])

    def test_model_error_is_shown_simply(self):
        from analyzer.services.gemini import GeminiError
        self.m.secrets.set_key(KEY)
        self.m.ask_model = FakeModel(error=GeminiError("ключ Gemini не подошёл"))
        r = self.m.ask("почему?")
        self.assertEqual(r["error"], "ключ Gemini не подошёл")
        self.assertFalse(self.m.busy)

    def test_busy_refuses_second_question(self):
        self.m.secrets.set_key(KEY)
        self.m.busy = True
        with self.assertRaises(ValueError) as e:
            self.m.ask("ещё вопрос")
        self.assertIn("ещё отвечает", str(e.exception))

    def test_clear_answer(self):
        self.m.secrets.set_key(KEY)
        self.m.ask("почему?")
        self.assertIsNotNone(self.m.last)
        self.m.clear_answer()
        self.assertIsNone(self.m.last)

    def test_fallback_model_is_shown(self):
        self.m.secrets.set_key(KEY)
        self.m.ask_model = lambda *a, **kw: {"text": "Коротко: всё спокойно.", "model": "gemini-flash-lite-latest",
                                             "tries": 4}
        r = self.m.ask("почему?")
        self.assertEqual(r["headline"], "Всё спокойно.")
        self.assertIn("gemini-flash-lite-latest", r["model_note"])
        self.assertIn("запасная", r["model_note"])

    def test_fallback_setting_goes_to_model(self):
        self.m.secrets.set_key(KEY)
        self.m.ask("почему?")
        kw = self.m.ask_model.calls[0]["kw"]
        self.assertEqual(kw["fallback"], "gemini-flash-lite-latest")
        self.assertEqual(kw["timeout"], 180)
        self.assertTrue(callable(kw["on_retry"]))

    def test_long_window_goes_to_model(self):
        self.m.secrets.set_key(KEY)
        self.m.settings.values["window_minutes"] = 720
        self.m.ask("что было за день?")
        self.assertIn("за последние 12 ч", self.m.ask_model.calls[0]["system"])


class SettingsAndSecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        self.st = Settings(self.root / "config")

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbers_are_checked(self):
        self.assertEqual(self.st.set("history_hours", "24"), 24)
        self.assertEqual(self.st.get("history_hours"), 24)
        with self.assertRaises(ValueError):
            self.st.set("history_hours", 500)
        with self.assertRaises(ValueError):
            self.st.set("poll_seconds", "быстро")
        with self.assertRaises(ValueError):
            self.st.set("чужая настройка", 1)

    def test_text_and_bool(self):
        self.assertEqual(self.st.set("gemini_model", "  gemini-3.8-flash "), "gemini-3.8-flash")
        with self.assertRaises(ValueError):
            self.st.set("gemini_model", "")
        with self.assertRaises(ValueError):
            self.st.set("lhm_url", "localhost:8085")
        self.assertEqual(self.st.set("gemini_thinking", True), True)
        self.assertEqual(self.st.set("gemini_thinking", "false"), False)

    def test_settings_are_saved_to_file(self):
        self.st.set("poll_seconds", 20)
        again = Settings(self.root / "config")
        self.assertEqual(again.get("poll_seconds"), 20)
        self.assertEqual(again.get("history_hours"), 12)     # остальное — по умолчанию

    def test_key_is_saved_and_removed(self):
        s = Secrets(self.root)
        self.assertFalse(s.has_key())
        s.set_key(KEY)
        self.assertEqual(s.key(), KEY)
        self.assertTrue((self.root / "secrets.json").exists())
        with self.assertRaises(ValueError):
            s.set_key("коротко")
        s.set_key("")
        self.assertFalse(s.has_key())


if __name__ == "__main__":
    unittest.main()
