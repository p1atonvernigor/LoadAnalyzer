"""Сбор журнала нагрузки и ответы на вопросы о нём.

Каждые poll_seconds (10 с) записываем:
- общую загрузку процессора, видеокарты, оперативной и видеопамяти;
- какие программы сколько берут (заметные: топ-5 по каждому показателю и всё крупное);
- мощность, напряжение, частоту и температуру видеокарты;
- температуру процессора (её даёт LibreHardwareMonitor, если он запущен);
- кто запустился и закрылся с прошлого замера.

Журнал хранится history_hours часов (по умолчанию 12) и переживает перезапуск (data/history.jsonl).
На вопрос («почему был скачок?») скачки и виновников считает сам скрипт, а Gemini только объясняет
готовые факты короткой выжимкой.
"""

import threading
import time

from .core.history import History
from .core.storage import read_json, write_json
from .services import gemini, gpu_power, load, procs, sensors, spikes

QUESTION_LIMIT = 300
EXAMPLES = ["Почему был скачок?", "Что сильнее всего грузит ПК?", "Куда ушла видеопамять?",
            "Не изнашивается ли видеокарта?", "Почему нагрелась видеокарта?"]
DEFAULT_QUESTION = "Почему был скачок нагрузки? Что грузило ПК больше всего?"
GAME_SKIP = ("lm studio", "lms", "llama", "llm", "ollama", "dwm", "csrss", "explorer", "system", "python", "msedge")
LHM_PAGE = "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases"
LHM_RETRY = 30               # не отвечает — спрашиваем снова раз в 30 замеров
TEMP_FRESH = 120             # температура считается свежей столько секунд


def _bands(episodes, now, minutes):
    """Полосы на графике (минуты назад): эпизоды разных показателей в одно время — одна полоса."""
    bands = []
    for e in sorted(episodes, key=lambda e: e["start"]):
        if bands and e["start"] <= bands[-1][1] + 30:
            bands[-1][1] = max(bands[-1][1], e["end"])
        else:
            bands.append([e["start"], e["end"]])
    return [(round((now - a) / 60.0, 2), round((now - b) / 60.0, 2)) for a, b in bands]


def _thin(points, limit):
    """Не больше limit точек: каждая n-я, последняя — всегда."""
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    return [points[int(i * step)] for i in range(limit - 1)] + [points[-1]]


class Monitor:
    def __init__(self, settings, data_dir, log=None):
        self.settings = settings
        self.data_dir = data_dir
        self.log = log or (lambda message, level="info": None)
        self.clock = time.time
        self.history = History(data_dir / "history.jsonl", settings.get("history_hours", 12), self.clock)
        self.events = []                 # (время, "start"|"stop", программа)
        self.marks = []                  # (время, заметка)
        self.cpu = load.CpuLoad()
        self.read_gpu = load.read_gpu
        self.read_ram = sensors.read_ram
        self.apps = procs.AppReader()
        self.gpu_apps = procs.GpuCounters()
        self.power = gpu_power.PowerReader(lhm_url=settings.get("lhm_url"))
        self.read_power = self.power.read
        self.read_lhm = sensors.read_lhm
        self.ask_model = gemini.ask_full
        self.key = lambda: ""            # ставит приложение (secrets.key)
        self._names = None
        self._info = {}
        self._lock = threading.Lock()
        self._gen = 0
        self._lhm_skip = 0
        self.lhm_ok = None               # None — ещё не проверяли
        self.busy = False
        self.stage = ""
        self.last = None                 # последний ответ
        self.sync = False                # тесты: вопрос выполняется сразу, без потока
        self.state_path = data_dir / "state.json"

    # ---------- запуск ----------

    def start(self):
        self.history.load()
        self._gen += 1
        self.cpu.read()
        threading.Thread(target=self._loop, args=(self._gen,), daemon=True).start()

    def stop(self):
        self._gen += 1
        self.gpu_apps.close()

    def _poll(self):
        return max(5, int(self.settings.get("poll_seconds", 10) or 10))

    def _window(self):
        hours = float(self.settings.get("history_hours", 12) or 12)
        return max(5, min(int(self.settings.get("window_minutes", 30) or 30), int(hours * 60)))

    def _loop(self, gen):
        while gen == self._gen:
            try:
                self.tick()
            except Exception as exc:
                self.log("замер не удался: %s" % exc, "warn")
            time.sleep(self._poll())

    # ---------- замер ----------

    def tick(self, now=None):
        now = self.clock() if now is None else now
        gpu = self.read_gpu()
        ram = self.read_ram()
        got = self.apps.read()
        per_pid = self.gpu_apps.read()
        power = self.read_power()
        apps = {}
        if got is not None:
            apps, pids = got
            apps = {n: dict(v, gpu=0.0, vram=0.0) for n, v in apps.items()}
            for pid, g in (per_pid or {}).items():
                name = pids.get(pid)
                if name in apps:
                    apps[name]["gpu"] = min(100.0, apps[name]["gpu"] + g["gpu"])
                    apps[name]["vram"] += g["vram"]
            self._track(now, set(apps))
        if gpu:
            self._info.update(gpu_name=gpu.get("name") or "", vram_total=gpu.get("mem_total"))
        if ram and ram.get("total"):
            self._info["ram_total_gb"] = ram["total"] / 1024.0 ** 3
        row = {"t": now, "cpu": self.cpu.read(), "gpu": gpu["load"] if gpu else None,
               "ram": ram["percent"] if ram else None, "vram": gpu.get("mem_used") if gpu else None,
               "vram_total": gpu.get("mem_total") if gpu else None,
               "apps": spikes.notable(apps)}
        if power:
            row.update(power)
        temps = self._temps() or {}
        if temps.get("cpu") is not None:
            row["cpu_temp"] = temps["cpu"]
        if row.get("gpu_temp") is None and temps.get("gpu") is not None:
            row["gpu_temp"] = temps["gpu"]
        self.history.set_hours(self.settings.get("history_hours", 12))
        self.history.add(row)
        self._cut_events(now)
        return row

    def _temps(self):
        """Температуры из LibreHardwareMonitor (процессор, а если нет nvidia-smi — и видеокарта)."""
        if self._lhm_skip > 0:
            self._lhm_skip -= 1
            return None
        data = self.read_lhm(self.settings.get("lhm_url"))
        if not data or data.get("cpu") is None:
            self.lhm_ok = False
            self._lhm_skip = LHM_RETRY
            return data
        self.lhm_ok = True
        return data

    def _track(self, now, names):
        """Какие программы запустились и закрылись с прошлого замера."""
        if self._names is not None:
            with self._lock:
                for n in sorted(names - self._names):
                    self.events.append((now, "start", n))
                for n in sorted(self._names - names):
                    self.events.append((now, "stop", n))
        self._names = names

    def _cut_events(self, now):
        edge = now - self.history.seconds()
        with self._lock:
            self.events = [e for e in self.events if e[0] >= edge][-800:]
            self.marks = [m for m in self.marks if m[0] >= edge][-50:]

    def _gaps(self):
        """Чего приложение не видит — показываем честно."""
        gaps = []
        if self.apps.reason:
            gaps.append("программы — " + self.apps.reason)
        if self.gpu_apps.reason and self.apps.reason == "":
            gaps.append("видеокарта по программам — " + self.gpu_apps.reason)
        if self.power.reason:
            gaps.append(self.power.reason)
        if self.history.note():
            gaps.append(self.history.note())
        return gaps

    def _temp_warn(self):
        return {"gpu_temp": float(self.settings.get("temp_gpu_warn", 75)),
                "cpu_temp": float(self.settings.get("temp_cpu_warn", 78))}

    # ---------- вопрос ИИ ----------

    def _begin(self, question):
        question = " ".join(str(question or "").split())[:QUESTION_LIMIT] or DEFAULT_QUESTION
        with self._lock:
            if self.busy:
                raise ValueError("ИИ ещё отвечает на прошлый вопрос")
            if not len(self.history):
                raise ValueError("замеров ещё нет: подождите полминуты после запуска")
            if not self.key():
                raise ValueError("нет ключа Gemini: добавьте его в настройках")
            self.busy, self.stage = True, "Готовлю выжимку…"
        return question

    def ask(self, question):
        """Вопрос уходит в фон, ответ появится в окне."""
        question = self._begin(question)
        if self.sync:
            return self._answer(question)
        threading.Thread(target=self._answer, args=(question,), daemon=True).start()
        return None

    def _answer(self, question):
        started = time.monotonic()
        minutes = self._window()
        result = {"question": question, "asked_at": time.strftime("%H:%M"), "error": "", "headline": "",
                  "points": [], "advice": "", "spikes": [], "top": [], "seconds": 0, "model_note": "",
                  "window_minutes": minutes}
        try:
            samples = self.history.tail(minutes)
            with self._lock:
                events, marks = list(self.events), list(self.marks)
            episodes = spikes.find_spikes(samples)
            result["spikes"] = spikes.spike_rows(episodes)
            result["top"] = self._top(samples)
            info = dict(self._info, poll=self._poll(), gaps=self._gaps(), temp_warn=self._temp_warn())
            info["cores"] = getattr(self.apps, "_cores", None)
            text = spikes.digest(samples, episodes, events, marks, info)
            self.stage = "Gemini разбирает журнал…"

            def retry(model, attempt, fallback):
                self.stage = ("Gemini перегружен, спрашиваю запасную модель %s…" % model if fallback and attempt == 1
                              else "Gemini перегружен, пробую ещё раз (%d из %d)…" % (attempt, gemini.RETRIES))

            reply = self.ask_model(self.key(), spikes.system_prompt(minutes),
                                   text + "\n\nВопрос владельца: " + question,
                                   model=self.settings.get("gemini_model"),
                                   fallback=self.settings.get("gemini_fallback_model", ""),
                                   max_tokens=int(self.settings.get("gemini_max_tokens", 1200)),
                                   timeout=int(self.settings.get("gemini_timeout_seconds", 120)),
                                   thinking=bool(self.settings.get("gemini_thinking", False)),
                                   on_retry=retry)
            used = self.settings.get("gemini_model")
            if isinstance(reply, dict):
                reply, used = reply.get("text", ""), reply.get("model") or used
            result.update(spikes.parse_answer(reply))
            result["model_note"] = "ответил %s" % used
            if used != self.settings.get("gemini_model"):
                result["model_note"] += " (запасная: основная была перегружена)"
        except Exception as exc:
            result["error"] = str(exc)
            self.log("ответ на вопрос не получился: %s" % exc, "warn")
        finally:
            result["seconds"] = round(time.monotonic() - started)
            self.last = result
            self.busy, self.stage = False, ""
        if not result["error"]:
            self.log("ответила на вопрос «%s» за %d с" % (question[:60], result["seconds"]))
        return result

    def clear_answer(self):
        self.last = None

    # ---------- данные для окна ----------

    def _top(self, samples):
        out = []
        for key in ("vram", "gpu", "cpu", "ram"):     # видеопамять первой: за ней владелец следит больше всего
            top = spikes.top_apps(samples, key)
            if top:
                hi = max(p for _, _, p in top) or 1
                out.append({"metric": key, "label": spikes.METRICS[key][0],
                            "rows": [{"name": n, "avg": spikes.fmt_app(key, a), "peak": spikes.fmt_app(key, p),
                                      "pct": round(100.0 * p / hi)} for n, a, p in top]})
        return out

    def _chart(self, samples, episodes, minutes):
        """Линии видеопамяти/GPU/CPU/ОЗУ в процентах и полосы скачков."""
        if not samples:
            return None
        now = samples[-1]["t"]
        series = []
        for name, key, color in (("Видеопамять", "vram", "#C084FC"), ("GPU", "gpu", "#7AA2FF"),
                                 ("CPU", "cpu", "#F5A524"), ("ОЗУ", "ram", "#4ADE80")):
            pts = []
            for s in samples:
                v = s.get(key)
                if v is None:
                    continue
                if key == "vram":
                    total = s.get("vram_total") or self._info.get("vram_total")
                    if not total:
                        continue
                    v = round(100.0 * v / total, 1)
                pts.append((round((now - s["t"]) / 60.0, 2), v))
            if pts:
                series.append({"name": name, "color": color, "points": _thin(pts, 150)})
        return {"series": series, "bands": _bands(episodes, now, minutes)}

    def live_chart(self):
        """График нагрузки с подсвеченными скачками. Пересчитываем только после нового замера."""
        minutes = self._window()
        samples = self.history.tail(minutes)
        if not samples:
            return None
        key = (len(samples), samples[-1]["t"], minutes)
        if getattr(self, "_live_key", None) != key:
            episodes = spikes.find_spikes(samples)
            chart = self._chart(samples, episodes, minutes) or {}
            chart.update(title="Нагрузка и скачки за %s" % spikes.window_text(minutes), unit="%",
                         min=0, max=100, minutes=minutes, spikes=spikes.spike_rows(episodes))
            chart["series"] = [{"name": s["name"], "color": s["color"], "points": [list(p) for p in s["points"]]}
                               for s in chart.get("series", [])]
            self._live_key, self._live = key, chart
        return self._live

    def temps_view(self):
        """Температуры: график видеокарты и процессора, полосы и список нагрева с виновниками."""
        minutes = self._window()
        samples = [s for s in self.history.tail(minutes)
                   if s.get("gpu_temp") is not None or s.get("cpu_temp") is not None]
        if not samples:
            return None
        now = samples[-1]["t"]
        warn = self._temp_warn()
        series, lines = [], []
        for name, key, color in (("GPU", "gpu_temp", "#7AA2FF"), ("CPU", "cpu_temp", "#F5A524")):
            pts = [[round((now - s["t"]) / 60.0, 2), round(s[key], 1)] for s in samples if s.get(key) is not None]
            if pts:
                series.append({"name": name, "color": color, "points": _thin(pts, 150)})
                lines.append({"value": warn[key], "color": color, "label": "%s %d°" % (name, round(warn[key]))})
        if not series:
            return None
        values = [p[1] for x in series for p in x["points"]]
        episodes = spikes.find_heat(samples)
        chart = {"title": "Температуры за %s" % spikes.window_text(minutes), "unit": "°",
                 "min": min(30, int((min(values) - 5) // 10 * 10)),
                 "max": max(90, int(-((-(max(values) + 5)) // 10) * 10)), "minutes": minutes,
                 "series": series, "lines": lines, "lines_note": "пунктир — порог внимания",
                 "bands": _bands(episodes, now, minutes), "bands_label": "нагрев"}
        note = "" if any(s.get("cpu_temp") is not None for s in samples) else \
            "температуры процессора нет: нужен LibreHardwareMonitor с Remote Web Server (порт 8085)"
        return {"chart": chart, "heat": spikes.heat_rows(episodes), "note": note}

    def power_view(self):
        """Питание видеокарты: плитки, графики мощности и напряжения, сводка износа."""
        minutes = self._window()
        samples = [s for s in self.history.tail(minutes) if s.get("watts") is not None]
        if not samples:
            return None
        now, last = samples[-1]["t"], samples[-1]
        tiles = []
        limit = last.get("power_limit")
        pct = 100.0 * last["watts"] / limit if limit else 0
        tiles.append({"label": "Мощность", "value": "%d Вт" % round(last["watts"]),
                      "sub": "из %d Вт предела" % round(limit) if limit else "", "pct": round(pct),
                      "level": "bad" if pct >= 95 else "warn" if pct >= 85 else "ok"})
        if last.get("volts") is not None:
            mv = last["volts"]
            tiles.append({"label": "Напряжение ядра", "value": "%.3f В" % (mv / 1000.0),
                          "sub": "высокое — от %.2f В" % (spikes.HIGH_MV / 1000.0),
                          "pct": round(max(0.0, min(100.0, (mv - 600) / 5.0))),
                          "level": "bad" if mv >= spikes.HIGH_MV else "warn" if mv >= 1000 else "ok"})
        if last.get("clock") is not None:
            tiles.append({"label": "Частота ядра", "value": "%d МГц" % round(last["clock"]),
                          "sub": "максимум %d МГц" % round(last["clock_max"]) if last.get("clock_max") else "",
                          "pct": round(100.0 * last["clock"] / last["clock_max"]) if last.get("clock_max") else 0,
                          "level": "ok"})
        if last.get("fan") is not None:
            tiles.append({"label": "Вентилятор", "value": "%d%%" % round(last["fan"]),
                          "sub": "%d°" % round(last["gpu_temp"]) if last.get("gpu_temp") is not None else "",
                          "pct": round(last["fan"]), "level": "warn" if last["fan"] >= 85 else "ok"})

        def pts(key, scale=1.0):
            return _thin([[round((now - s["t"]) / 60.0, 2), round(s[key] * scale, 3)] for s in samples
                          if s.get(key) is not None], 150)

        charts = []
        top_w = max([s.get("power_limit") or 0 for s in samples] + [s["watts"] for s in samples])
        charts.append({"title": "Мощность видеокарты, Вт", "unit": " Вт", "min": 0,
                       "max": int((top_w // 50 + 1) * 50), "minutes": minutes,
                       "series": [{"name": "Мощность", "color": "#F87171", "points": pts("watts")}]})
        if any(s.get("volts") is not None for s in samples):
            charts.append({"title": "Напряжение ядра, мВ", "unit": "", "min": 600, "max": 1100,
                           "minutes": minutes,
                           "series": [{"name": "мВ", "color": "#FBBF24", "points": pts("volts")}]})
        w = spikes.wear(samples, self._poll(), self._temp_warn()["gpu_temp"])
        return {"tiles": tiles, "charts": charts, "wear": spikes.wear_lines(w),
                "title": "Износ за %s" % spikes.window_text(minutes),
                "note": self.power.voltage_note or ("напряжение — из %s" % self.power.voltage_source
                                                    if self.power.voltage_source else "")}

    def now_tiles(self):
        """Плитки «сейчас»: видеопамять первой, с гигабайтами и свободным местом."""
        last = self.history.last()
        if not last:
            return []
        tiles = []
        total = last.get("vram_total") or self._info.get("vram_total")
        if last.get("vram") is not None and total:
            pct = 100.0 * last["vram"] / total
            tiles.append({"label": "Видеопамять", "value": "%.1f из %.0f ГБ" % (last["vram"] / 1024.0, total / 1024.0),
                          "sub": "свободно %.1f ГБ" % (max(0.0, total - last["vram"]) / 1024.0), "pct": round(pct),
                          "level": "bad" if pct >= 90 else "warn" if pct >= 80 else "ok"})
        for label, key in (("Видеокарта", "gpu"), ("Процессор", "cpu"), ("Оперативная память", "ram")):
            if last.get(key) is not None:
                tiles.append({"label": label, "value": "%d%%" % round(last[key]), "sub": "", "pct": round(last[key]),
                              "level": "bad" if last[key] >= 95 else "warn" if last[key] >= 85 else "ok"})
        for label, key in (("Температура видеокарты", "gpu_temp"), ("Температура процессора", "cpu_temp")):
            if last.get(key) is not None:
                warn = self._temp_warn()[key]
                tiles.append({"label": label, "value": "%d°" % round(last[key]),
                              "sub": "порог внимания %d°" % round(warn), "pct": round(last[key]),
                              "level": "bad" if last[key] >= warn + 5 else "warn" if last[key] >= warn else "ok"})
        game = self.game_name()
        if game:
            tiles.append({"label": "Идёт игра", "value": game, "sub": "видеокарту грузит она", "pct": 0,
                          "level": "ok"})
        return tiles

    def now_rows(self, limit=6):
        """Кто больше всех берёт прямо сейчас (последний замер)."""
        last = self.history.last()
        if not last:
            return []
        apps = last.get("apps") or {}

        def weight(item):
            v = item[1]
            return v["cpu"] + v["gpu"] + v["ram"] / 400.0 + v["vram"] / 200.0

        rows = []
        for name, v in sorted(apps.items(), key=weight, reverse=True)[:limit]:
            rows.append({"name": name, "cpu": "%d%%" % round(v["cpu"]), "ram": spikes.fmt_app("ram", v["ram"]),
                         "gpu": "%d%%" % round(v["gpu"]),
                         "vram": spikes.fmt_app("vram", v["vram"]) if v["vram"] else "—"})
        return rows

    def game_name(self, now=None):
        """Идёт ли игра: на свежем замере видеокарту грузит программа (не система) на game_gpu_percent
        и больше. Возвращает её имя или пустую строку."""
        now = self.clock() if now is None else now
        last = self.history.last()
        if not last or now - last["t"] > 3 * self._poll():
            return ""
        need = float(self.settings.get("game_gpu_percent", 40))
        for name, v in sorted((last.get("apps") or {}).items(), key=lambda x: -(x[1].get("gpu") or 0)):
            low = name.lower()
            if (v.get("gpu") or 0) < need:
                break
            if not any(low.startswith(x) or x in low for x in GAME_SKIP):
                return name
        return ""

    # ---------- подсказка про LibreHardwareMonitor ----------

    def hint_state(self):
        data = read_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def hint_hide(self):
        data = self.hint_state()
        data["lhm_hint_hidden"] = True
        write_json(self.state_path, data)

    def hint_opened(self):
        data = self.hint_state()
        data["lhm_page_opened"] = True
        write_json(self.state_path, data)

    def lhm_hint(self):
        """Нужна ли подсказка «поставьте LibreHardwareMonitor» (температуры процессора нет)."""
        if self.lhm_ok or self.hint_state().get("lhm_hint_hidden"):
            return None
        return {"title": "Температуры процессора пока не видно",
                "text": "Windows сама её не отдаёт. Её показывает бесплатная программа LibreHardwareMonitor: "
                        "поставьте её, включите Options → Remote Web Server (порт 8085) — и температура "
                        "процессора появится здесь сама. Всё остальное работает и без неё.",
                "url": LHM_PAGE, "first_run": not self.hint_state().get("lhm_page_opened")}

    # ---------- общее состояние для окна ----------

    def state(self):
        minutes = self._window()
        count = len(self.history)
        last = self.history.last()
        return {
            "title": "Анализ нагрузки",
            "window_minutes": minutes,
            "window_text": spikes.window_text(minutes),
            "history_hours": float(self.settings.get("history_hours", 12)),
            "poll_seconds": self._poll(),
            "samples": count,
            "apps": len(last["apps"]) if last else 0,
            "ready": count > 0,
            "busy": self.busy,
            "stage": self.stage,
            "examples": EXAMPLES,
            "limit": QUESTION_LIMIT,
            "key_set": bool(self.key()),
            "model": self.settings.get("gemini_model"),
            "gaps": self._gaps(),
            "hint": self.lhm_hint(),
            "tiles": self.now_tiles(),
            "chart": self.live_chart(),
            "temps": self.temps_view(),
            "power": self.power_view(),
            "now": self.now_rows(),
            "last": self.last,
        }
