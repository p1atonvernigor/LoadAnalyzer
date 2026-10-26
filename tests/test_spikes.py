"""Чистая логика: заметные программы, поиск скачков и нагрева, выжимка, разбор ответа, питание.

Без настоящих датчиков, psutil, счётчиков Windows и интернета.
"""

import io
import json as _json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.services import gpu_power, procs, spikes  # noqa: E402

T0 = 1_700_000_000   # условное «начало» журнала


def sample(t, cpu=10, gpu=5, ram=50, vram=1000, apps=None):
    return {"t": t, "cpu": cpu, "gpu": gpu, "ram": ram, "vram": vram, "apps": apps or {}}


def app(cpu=0, ram=0, gpu=0, vram=0):
    return {"cpu": cpu, "ram": ram, "gpu": gpu, "vram": vram}


def game_log():
    """10 минут спокойно, потом 2 минуты игра грузит видеокарту, потом снова спокойно."""
    out = []
    for i in range(90):
        t = T0 + 10 * i
        hot = 60 <= i < 72
        apps = {"chrome": app(cpu=6, ram=1500), "Telegram": app(ram=800, vram=600)}
        if hot:
            apps["game"] = app(cpu=20, ram=3000, gpu=90, vram=4500)
        out.append(sample(t, cpu=35 if hot else 10, gpu=95 if hot else 5, ram=60 if hot else 50,
                          vram=6000 if hot else 1500, apps=apps))
    return out


class ProcsTest(unittest.TestCase):
    def test_short_name(self):
        self.assertEqual(procs.short_name("chrome.exe"), "chrome")
        self.assertEqual(procs.short_name("LM Studio.EXE"), "LM Studio")
        self.assertEqual(procs.short_name("python"), "python")

    def test_gpu_instances(self):
        engines = [("pid_100_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 40.0),
                   ("pid_100_luid_0x0_0x1_phys_0_eng_1_engtype_3D", 15.0),
                   ("pid_100_luid_0x0_0x1_phys_0_eng_5_engtype_Copy", 30.0),
                   ("pid_200_luid_0x0_0x1_phys_0_eng_0_engtype_VideoDecode", 300.0),
                   ("мусор", 50.0)]
        memory = [("pid_100_luid_0x0_0x1_phys_0", 2 * 1048576.0), ("pid_300_luid_0x0_0x1_phys_0", 1048576.0)]
        got = procs.parse_gpu_instances(engines, memory)
        self.assertEqual(got[100], {"gpu": 55.0, "vram": 2.0})      # самый занятый тип движка: 3D 40+15
        self.assertEqual(got[200]["gpu"], 100.0)                     # не больше 100%
        self.assertEqual(got[300], {"gpu": 0.0, "vram": 1.0})
        self.assertEqual(len(got), 3)

    def test_counters_off_outside_windows(self):
        if sys.platform == "win32":
            self.skipTest("только для других систем")
        c = procs.GpuCounters()
        self.assertIsNone(c.read())
        self.assertIn("Windows", c.reason)


class SpikesTest(unittest.TestCase):
    def test_notable_keeps_top_and_big(self):
        apps = {"p%d" % i: app(cpu=i) for i in range(10)}
        apps["fat"] = app(ram=2000)
        apps["tiny"] = app()
        got = spikes.notable(apps)
        self.assertIn("p9", got)
        self.assertIn("p5", got)            # 5% — заметно много
        self.assertNotIn("p1", got)
        self.assertIn("fat", got)
        self.assertNotIn("tiny", got)

    def test_finds_gpu_spike_and_culprit(self):
        log = game_log()
        eps = spikes.find_spikes(log)
        kinds = {e["metric"] for e in eps}
        self.assertIn("gpu", kinds)
        self.assertIn("vram", kinds)
        gpu = [e for e in eps if e["metric"] == "gpu"][0]
        self.assertEqual(gpu["start"], T0 + 600)
        self.assertEqual(gpu["end"], T0 + 710)
        self.assertEqual(gpu["peak"], 95)
        self.assertEqual(gpu["culprits"][0][0], "game")
        self.assertNotIn("chrome", [c[0] for c in gpu["culprits"]])
        rows = spikes.spike_rows(eps)
        self.assertTrue(any("game +90%" in r["who"] for r in rows))

    def test_flat_log_has_no_spikes(self):
        log = [sample(T0 + 10 * i, cpu=10 + i % 3) for i in range(60)]
        self.assertEqual(spikes.find_spikes(log), [])

    def test_digest_is_short_and_has_facts(self):
        log = game_log()
        eps = spikes.find_spikes(log)
        events = [(T0 + 600, "start", "game"), (T0 + 720, "stop", "game"), (T0 + 30, "start", "мелочь")]
        text = spikes.digest(log, eps, events, [(T0 + 100, "заметка")],
                             {"gpu_name": "RTX 3060 Ti", "vram_total": 8192})
        self.assertIn("RTX 3060 Ti", text)
        self.assertIn("Найденные скачки", text)
        self.assertIn("game (было 0%, стало 90%)", text)
        self.assertIn("Запускались:", text)
        self.assertNotIn("мелочь", text)                 # не была заметной — не засоряем выжимку
        self.assertIn("заметка", text)
        self.assertLess(len(text), 4000)

    def test_digest_for_long_window_fits(self):
        log = [sample(T0 + 10 * i, apps={"p%d" % k: app(cpu=k, ram=100 * k) for k in range(12)}) for i in range(180)]
        text = spikes.digest(log, spikes.find_spikes(log))
        self.assertIn("Резких скачков не найдено", text)
        self.assertLess(len(text), 6000)

    def test_digest_cuts_very_long_log(self):
        """12 часов замеров: выжимка всё равно должна остаться по размеру модели."""
        log = [sample(T0 + 10 * i, apps={"chrome": app(cpu=5, ram=1500)}) for i in range(4320)]
        text = spikes.digest(log, [], max_chars=12000)
        self.assertLessEqual(len(text), 12000)
        self.assertIn("Сейчас:", text)

    def test_heat_episodes_and_lines(self):
        rows = []
        for i in range(90):
            hot = i >= 60
            rows.append(dict(sample(T0 + 10 * i, apps={"game": app(gpu=90 if hot else 0)}),
                             gpu_temp=72 if hot else 50, cpu_temp=60 if hot else 45))
        eps = spikes.find_heat(rows)
        self.assertIn("gpu_temp", {e["metric"] for e in eps})
        heat = spikes.heat_rows(eps)
        self.assertTrue(any("game" in r["who"] for r in heat))
        lines = "\n".join(spikes.heat_lines(rows, eps, {"gpu_temp": 70, "cpu_temp": 78}))
        self.assertIn("Видеокарта: сейчас 72°", lines)
        self.assertIn("порог внимания 70°", lines)

    def test_parse_answer(self):
        got = spikes.parse_answer("**Коротко:** скачок из-за game.\n- 15:20 game взяла 90%\n2) chrome спокоен\n"
                                  "Совет: закройте игру.")
        self.assertEqual(got["headline"], "Скачок из-за game.")
        self.assertEqual(got["points"], ["15:20 game взяла 90%", "chrome спокоен"])
        self.assertEqual(got["advice"], "закройте игру.")
        loose = spikes.parse_answer("Всё дело в игре.\nОна грузила видеокарту.")
        self.assertEqual(loose["headline"], "Всё дело в игре.")
        self.assertEqual(loose["points"], ["Она грузила видеокарту."])
        self.assertEqual(spikes.parse_answer("")["headline"], "Модель не дала ответа")

    def test_window_text_and_prompt(self):
        self.assertEqual(spikes.window_text(30), "30 мин")
        self.assertEqual(spikes.window_text(60), "60 мин")
        self.assertEqual(spikes.window_text(720), "12 ч")
        prompt = spikes.system_prompt(720)
        self.assertIn("за последние 12 ч", prompt)
        self.assertIn("разгон", prompt)                  # никогда не советовать разгон
        self.assertNotIn("%%", prompt)

    def test_digest_warns_when_vram_almost_full(self):
        log = [sample(T0 + 10 * i, vram=7800, apps={"game": app(vram=7000)}) for i in range(60)]
        text = spikes.digest(log, [], info={"vram_total": 8192})
        self.assertIn("Почти заполнена", text)


class FakeRun:
    """Подмена subprocess.run для nvidia-smi."""

    def __init__(self, query="", voltage="", query_fails_full=False):
        self.query, self.voltage, self.fails, self.calls = query, voltage, query_fails_full, []

    def __call__(self, args, **kw):
        self.calls.append(args)
        out, code = "", 0
        if "-q" in args:
            out = self.voltage
        elif self.fails and any("enforced" in a for a in args):
            code = 6
        else:
            out = self.query
        return type("R", (), {"stdout": out, "returncode": code})()


LHM = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
    {"Text": "NVIDIA GeForce RTX 3060 Ti", "ImageURL": "images_icon/nvidia.png", "Children": [
        {"Text": "Voltages", "Children": [{"Text": "GPU Core", "Value": "0,950 V", "Children": []}]},
        {"Text": "Temperatures", "Children": [{"Text": "GPU Core", "Value": "60,0 °C", "Children": []}]}]},
    {"Text": "AMD Ryzen 7 5800X", "ImageURL": "images_icon/cpu.png", "Children": [
        {"Text": "Temperatures", "Children": [{"Text": "Core (Tctl/Tdie)", "Value": "54,0 °C", "Children": []}]}]}]}]}


class GpuPowerTest(unittest.TestCase):
    def test_reads_power_and_voltage_from_nvidia_smi(self):
        run = FakeRun("187.50, 200.00, 1890, 2100, 63, 71\n", "Voltage\n    Graphics : 1043.750 mV\n")
        r = gpu_power.PowerReader(smi="nvidia-smi", run=run).read()
        self.assertEqual(r, {"watts": 187.5, "power_limit": 200.0, "clock": 1890.0, "clock_max": 2100.0,
                             "fan": 63.0, "gpu_temp": 71.0, "volts": 1043.75})

    def test_old_driver_and_not_available_fields(self):
        run = FakeRun("[N/A], 200.00, 1890, 2100, [N/A], 71", "", query_fails_full=True)
        reader = gpu_power.PowerReader(smi="nvidia-smi", run=run)
        r = reader.read()
        self.assertIsNone(r["watts"])
        self.assertIsNone(r["fan"])
        self.assertEqual(reader.query, gpu_power.SHORT)
        self.assertIsNone(r["volts"])
        self.assertIn("LibreHardwareMonitor", reader.voltage_note)
        self.assertEqual(reader.reason, "")                          # без напряжения — не ошибка
        n = len(run.calls)
        reader.read()
        self.assertEqual(len(run.calls), n + 1)                      # напряжение не спрашиваем каждый раз

    def test_voltage_from_libre_hardware_monitor(self):
        self.assertEqual(gpu_power.parse_lhm_voltage(LHM), 950.0)
        opener = lambda url, timeout=3: io.BytesIO(_json.dumps(LHM).encode())   # noqa: E731
        run = FakeRun("150, 200, 1800, 2100, 50, 65", "")
        r = gpu_power.PowerReader(smi="nvidia-smi", run=run, lhm_url="http://x", opener=opener).read()
        self.assertEqual(r["volts"], 950.0)

    def test_no_nvidia(self):
        reader = gpu_power.PowerReader(smi="nvidia-smi", run=FakeRun("", ""))
        self.assertIsNone(reader.read())
        self.assertIn("мощность", reader.reason)

    def test_wear_summary(self):
        rows = []
        for i in range(60):
            busy = i >= 30
            rows.append(dict(sample(T0 + 10 * i, apps={"game": app(gpu=90 if busy else 0)}),
                             watts=195.0 if busy else 40.0, power_limit=200.0, volts=1062.0 if busy else 750.0,
                             gpu_temp=78 if busy else 50, fan=80 if busy else 30))
        w = spikes.wear(rows, poll=10)
        self.assertEqual(round(w["at_limit"], 2), 0.5)
        self.assertEqual(round(w["high_mv"], 2), 0.5)
        self.assertEqual(w["who"], ["game"])
        self.assertAlmostEqual(w["wh"], (30 * 195 + 30 * 40) * 10 / 3600.0)
        text = "\n".join(spikes.wear_lines(w))
        self.assertIn("у предела (90%+) 50% времени", text)
        self.assertIn("пик 1.06 В", text)
        self.assertIn("горячо (75°+) 50% времени", text)
        self.assertIsNone(spikes.wear([sample(T0)]))


class SensorsTest(unittest.TestCase):
    def test_lhm_gives_cpu_and_gpu_temperature(self):
        from analyzer.services import sensors
        got = sensors.parse_lhm(LHM)
        self.assertEqual(got["cpu"], 54.0)
        self.assertEqual(got["gpu"], 60.0)
        self.assertIn("Ryzen", got["cpu_name"])

    def test_lhm_not_running(self):
        from analyzer.services import sensors

        def broken(url, timeout=3):
            raise OSError("нет связи")

        self.assertIsNone(sensors.read_lhm("http://localhost:8085/data.json", opener=broken))
        self.assertIsNone(sensors.read_lhm(""))


class RunBatTest(unittest.TestCase):
    def test_run_bat_checks_every_requirement(self):
        bat = (ROOT / "run.bat").read_text(encoding="utf-8")
        line = [x for x in bat.splitlines() if "import " in x][0]
        for req in (ROOT / "requirements.txt").read_text(encoding="utf-8").split():
            pkg = req.split(">")[0].split("=")[0].strip()
            self.assertIn(pkg, line, "run.bat не проверяет пакет %s" % pkg)
        self.assertIn("py -3 -m analyzer", bat)

    def test_run_bat_has_windows_line_endings(self):
        """26.10: run.bat был с переводами строк Linux (LF) — на Windows нужны CRLF."""
        raw = (ROOT / "run.bat").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "в run.bat есть строки без CR")


if __name__ == "__main__":
    unittest.main()
