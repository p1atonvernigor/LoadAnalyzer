"""Разбор журнала нагрузки: поиск скачков, виновники, короткая выжимка для модели, разбор ответа.

Считает сам скрипт, а не модель: модели уходит короткая выжимка, а не весь журнал.

Замер (sample): {"t": время (секунды, time.time), "cpu": %, "gpu": %, "ram": %, "vram": МБ,
"apps": {программа: {"cpu": %, "ram": МБ, "gpu": %, "vram": МБ}}} — любое значение может быть None.
"""

import re
import time

# метрика: (название, единица, скачок «на сколько выросло», нижняя граница, заметный рост у программы)
METRICS = {
    "cpu": ("Процессор", "%", 25.0, 40.0, 5.0),
    "gpu": ("Видеокарта", "%", 30.0, 50.0, 5.0),
    "ram": ("Оперативная память", "%", 8.0, 0.0, 200.0),
    "vram": ("Видеопамять", "МБ", 800.0, 0.0, 200.0),   # видеопамяти у владельца 8 ГБ — следим строже
}
ORDER = ("cpu", "gpu", "ram", "vram")
SHORT = {"cpu": "CPU", "gpu": "GPU", "ram": "ОЗУ", "vram": "VRAM"}
BASE_FROM, BASE_TO = 150, 30        # «как было до»: замеры от 2,5 мин до 30 с перед точкой
MERGE_GAP = 40                      # скачки ближе 40 с — один эпизод
MAX_SPIKES = 8
NOTABLE_TOP = 5
NOTABLE_MIN = {"cpu": 5.0, "gpu": 5.0, "ram": 500.0, "vram": 300.0}


def notable(apps):
    """Оставляет только заметные программы: топ-5 по каждой метрике и всё, что берёт заметно много.
    Так журнал за 30 минут занимает килобайты, а не мегабайты."""
    keep = set()
    for key in ORDER:
        ranked = sorted(((v.get(key) or 0, n) for n, v in apps.items()), reverse=True)
        keep.update(n for value, n in ranked[:NOTABLE_TOP] if value > 0)
        keep.update(n for value, n in ranked if value >= NOTABLE_MIN[key])
    out = {}
    for n in keep:
        v = apps[n]
        out[n] = {"cpu": round(v.get("cpu") or 0, 1), "ram": round(v.get("ram") or 0),
                  "gpu": round(v.get("gpu") or 0, 1), "vram": round(v.get("vram") or 0)}
    return out


def _hm(t, seconds=False):
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.localtime(t))


def fmt(key, value):
    if value is None:
        return "—"
    if key == "vram":
        return "%.1f ГБ" % (value / 1024.0)
    return "%d%%" % round(value)


def fmt_app(key, value):
    """Сколько берёт программа: проценты или мегабайты/гигабайты."""
    if key in ("cpu", "gpu"):
        return "%d%%" % round(value)
    return "%.1f ГБ" % (value / 1024.0) if value >= 1024 else "%d МБ" % round(value)


def _median(values):
    values = sorted(values)
    n = len(values)
    if not n:
        return None
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0


def _base_rows(samples, i):
    t = samples[i]["t"]
    return [s for s in samples[:i] if BASE_FROM >= t - s["t"] >= BASE_TO]


def _app_avg(rows, key):
    sums = {}
    for s in rows:
        for name, v in (s.get("apps") or {}).items():
            sums[name] = sums.get(name, 0.0) + (v.get(key) or 0)
    return {n: total / len(rows) for n, total in sums.items()} if rows else {}


def _episodes_for(samples, key, jump, floor):
    """Эпизоды роста одного показателя: выше медианы «как было до» на jump и не ниже floor."""
    episodes, current = [], None
    for i, s in enumerate(samples):
        v = s.get(key)
        if v is None:
            continue
        base_rows = _base_rows(samples, i)
        base = _median([r[key] for r in base_rows if r.get(key) is not None])
        hot = base is not None and len(base_rows) >= 3 and v - base >= jump and v >= floor
        if current is not None and (hot or s["t"] - current["end"] <= MERGE_GAP) and \
                (hot or v - current["base"] >= jump / 2.0):
            current["end"] = s["t"]
            current["end_i"] = i
            if v > current["peak"]:
                current["peak"], current["peak_t"] = v, s["t"]
            continue
        if current is not None:
            episodes.append(current)
            current = None
        if hot:
            current = {"metric": key, "start": s["t"], "end": s["t"], "start_i": i, "end_i": i,
                       "base": base, "peak": v, "peak_t": s["t"], "base_rows": base_rows}
    if current is not None:
        episodes.append(current)
    return episodes


def _finish(samples, episodes):
    for ep in episodes:
        ep["culprits"] = culprits(samples, ep)
        ep.pop("base_rows", None)
    episodes.sort(key=lambda e: e["peak"] - e["base"], reverse=True)
    episodes = episodes[:MAX_SPIKES]
    episodes.sort(key=lambda e: e["start"])
    return episodes


def find_spikes(samples):
    """Резкие скачки: значение выросло на «скачок» относительно медианы за 2 минуты до и выше нижней границы.
    Соседние точки сливаются в один эпизод. Возвращает эпизоды по времени (не больше MAX_SPIKES)."""
    episodes = []
    for key in ORDER:
        _, _, jump, floor, _ = METRICS[key]
        episodes.extend(_episodes_for(samples, key, jump, floor))
    return _finish(samples, episodes)


# Температуры (просьба владельца 24.10): нагрев на 8° и выше относительно «как было до», от 55°.
# датчик: (название, рост, нижняя граница, по какой нагрузке программ искать виновника)
HEAT = {
    "gpu_temp": ("Видеокарта", 8.0, 55.0, "gpu"),
    "cpu_temp": ("Процессор", 8.0, 55.0, "cpu"),
}


def find_heat(samples):
    """Эпизоды нагрева видеокарты и процессора с виновниками (по росту их нагрузки)."""
    episodes = []
    for key, (_, jump, floor, _) in HEAT.items():
        episodes.extend(_episodes_for(samples, key, jump, floor))
    return _finish(samples, episodes)


def heat_rows(episodes):
    """Нагрев в виде строк для окна."""
    out = []
    for ep in episodes:
        app_key = HEAT[ep["metric"]][3]
        when = _hm(ep["start"]) if ep["end"] - ep["start"] < 60 else "%s–%s" % (_hm(ep["start"]), _hm(ep["end"]))
        who = ", ".join("%s +%s" % (n, fmt_app(app_key, max(0.0, after - before))) for n, before, after in ep["culprits"])
        out.append({"time": when, "metric": ep["metric"], "label": HEAT[ep["metric"]][0],
                    "from": "%d°" % round(ep["base"]), "to": "%d°" % round(ep["peak"]),
                    "who": who or "явного виновника нет"})
    return out


def heat_lines(samples, episodes, warn=None):
    """Строки «Температуры» для выжимки. warn: {"gpu_temp": 75, "cpu_temp": 78}."""
    warn = warn or {}
    lines = []
    for key, (name, _, _, app_key) in HEAT.items():
        rows = [s for s in samples if s.get(key) is not None]
        if not rows:
            if key == "cpu_temp" and lines:
                lines.append("Процессор: температура не видна (нужен LibreHardwareMonitor).")
            continue
        peak = max(rows, key=lambda s: s[key])
        avg = sum(s[key] for s in rows) / len(rows)
        leader = top_apps([s for s in samples if int(s["t"] // 60) == int(peak["t"] // 60)], app_key, 1)
        text = "%s: сейчас %d°, в среднем %d°, пик %d° в %s" % (
            name, round(rows[-1][key]), round(avg), round(peak[key]), _hm(peak["t"]))
        if leader:
            text += " (больше всех грузил %s)" % leader[0][0]
        if warn.get(key):
            over = [s for s in rows if s[key] >= warn[key]]
            text += ", порог внимания %d°" % round(warn[key])
            if over:
                text += ", выше него %d мин" % max(1, round(len(over) * _poll_of(samples) / 60.0))
        lines.append(text + ".")
    for ep in episodes:
        who = ", ".join("%s (было %s, стало %s)" % (n, fmt_app(HEAT[ep["metric"]][3], b), fmt_app(HEAT[ep["metric"]][3], a))
                        for n, b, a in ep["culprits"]) or "явного виновника нет"
        lines.append("Нагрев %s–%s: %s с %d° до %d°. Выросла нагрузка: %s." % (
            _hm(ep["start"], True), _hm(ep["end"], True), HEAT[ep["metric"]][0].lower(),
            round(ep["base"]), round(ep["peak"]), who))
    return lines


def _poll_of(samples):
    """Шаг замеров в секундах по самим замерам (медиана), по умолчанию 10."""
    gaps = [b["t"] - a["t"] for a, b in zip(samples, samples[1:]) if b["t"] > a["t"]]
    return _median(gaps) or 10


def culprits(samples, ep, limit=3):
    """Чья доля сильнее всего выросла во время скачка: [(программа, было, стало)]."""
    key = ep["metric"]
    app_key = HEAT[key][3] if key in HEAT else key
    before = _app_avg(ep.get("base_rows") or [], app_key)
    peak = {}
    for s in samples[ep["start_i"]:ep["end_i"] + 1]:
        for name, v in (s.get("apps") or {}).items():
            peak[name] = max(peak.get(name, 0.0), v.get(app_key) or 0)
    need = METRICS[app_key][4]
    grown = [(peak[n] - before.get(n, 0.0), n) for n in peak]
    grown = [(d, n) for d, n in grown if d >= need]
    grown.sort(reverse=True)
    return [(n, round(before.get(n, 0.0), 1), round(peak[n], 1)) for _, n in grown[:limit]]


def top_apps(samples, key, limit=3):
    """Кто больше всех брал за всё время: [(программа, в среднем, пик)]."""
    if not samples:
        return []
    sums, peaks = {}, {}
    for s in samples:
        for name, v in (s.get("apps") or {}).items():
            value = v.get(key) or 0
            sums[name] = sums.get(name, 0.0) + value
            peaks[name] = max(peaks.get(name, 0.0), value)
    ranked = sorted(((total / len(samples), n) for n, total in sums.items()), reverse=True)
    return [(n, round(avg, 1), round(peaks[n], 1)) for avg, n in ranked[:limit] if peaks[n] > 0]


def minutes(samples):
    """Поминутно: [(время минуты, {метрика: (среднее, пик)}, {метрика: главная программа})]."""
    groups = []
    for s in samples:
        m = int(s["t"] // 60)
        if not groups or groups[-1][0] != m:
            groups.append((m, []))
        groups[-1][1].append(s)
    out = []
    for m, rows in groups:
        stats, leaders = {}, {}
        for key in ORDER + tuple(HEAT):
            vals = [r[key] for r in rows if r.get(key) is not None]
            if vals:
                stats[key] = (sum(vals) / len(vals), max(vals))
            if key in HEAT:
                continue
            best = top_apps(rows, key, 1)
            if best:
                leaders[key] = best[0][0]
        out.append((m * 60, stats, leaders))
    return out


def spike_rows(episodes):
    """Скачки в виде строк для окна и картинки."""
    out = []
    for ep in episodes:
        key = ep["metric"]
        label = METRICS[key][0]
        when = _hm(ep["start"]) if ep["end"] - ep["start"] < 60 else "%s–%s" % (_hm(ep["start"]), _hm(ep["end"]))
        who = ", ".join("%s +%s" % (n, fmt_app(key, max(0.0, after - before))) for n, before, after in ep["culprits"])
        out.append({"time": when, "metric": key, "label": label, "from": fmt(key, ep["base"]),
                    "to": fmt(key, ep["peak"]), "who": who or "явного виновника нет"})
    return out


def digest(samples, episodes, events=(), marks=(), info=None, max_chars=12000):
    """Короткий текст для модели. events: [(время, "start"|"stop", программа)], marks: [(время, текст)]."""
    info = info or {}
    if not samples:
        return "Замеров пока нет."
    first, last = samples[0]["t"], samples[-1]["t"]
    lines = ["Журнал нагрузки ПК с %s до %s (%d мин), замер каждые %s с." % (
        _hm(first), _hm(last), max(1, round((last - first) / 60.0)), info.get("poll", 10))]
    hw = []
    if info.get("gpu_name"):
        hw.append("видеокарта %s" % info["gpu_name"])
    if info.get("vram_total"):
        hw.append("видеопамяти всего %.0f ГБ" % (info["vram_total"] / 1024.0))
    if info.get("ram_total_gb"):
        hw.append("оперативной памяти всего %.0f ГБ" % info["ram_total_gb"])
    if info.get("cores"):
        hw.append("потоков процессора %s" % info["cores"])
    if hw:
        lines.append("ПК: " + ", ".join(hw) + ".")
    if info.get("gaps"):
        lines.append("Нет данных: " + "; ".join(info["gaps"]) + ".")

    lines.append("")
    lines.append("Поминутно (среднее/пик, в скобках главная программа):")
    for t, stats, leaders in minutes(samples):
        parts = []
        for key in ORDER:
            if key not in stats:
                continue
            avg, peak = stats[key]
            if key == "vram":
                total = info.get("vram_total")
                text = ("VRAM %.1f/%.0f ГБ" % (peak / 1024.0, total / 1024.0)) if total else "VRAM %.1f ГБ" % (peak / 1024.0)
            elif key == "ram":
                text = "ОЗУ %d%%" % round(peak)
            else:
                text = "%s %d/%d%%" % (SHORT[key], round(avg), round(peak))
            if leaders.get(key):
                text += " (%s)" % leaders[key]
            parts.append(text)
        temps = ["%s %d°" % (SHORT[k[:3]], round(stats[k][1])) for k in HEAT if k in stats]
        if temps:
            parts.append("темп. " + ", ".join(temps))
        lines.append("%s %s" % (_hm(t), " · ".join(parts)))

    lines.append("")
    if episodes:
        lines.append("Найденные скачки:")
        for i, ep in enumerate(episodes, 1):
            key = ep["metric"]
            when = "%s–%s" % (_hm(ep["start"], True), _hm(ep["end"], True))
            who = ", ".join("%s (было %s, стало %s)" % (n, fmt_app(key, b), fmt_app(key, a))
                            for n, b, a in ep["culprits"]) or "явного виновника нет"
            lines.append("%d. %s %s: с %s до %s. Выросли: %s." % (
                i, when, METRICS[key][0], fmt(key, ep["base"]), fmt(key, ep["peak"]), who))
    else:
        lines.append("Резких скачков не найдено.")

    lines.append("")
    lines.append("Больше всего за всё время (в среднем / пик):")
    for key in ORDER:
        top = top_apps(samples, key)
        if top:
            lines.append("%s: %s." % (METRICS[key][0], ", ".join(
                "%s %s / %s" % (n, fmt_app(key, avg), fmt_app(key, peak)) for n, avg, peak in top)))

    seen = set()
    for s in samples:
        seen.update((s.get("apps") or {}).keys())
    started = ["%s %s" % (_hm(t), n) for t, kind, n in events if kind == "start" and n in seen][-12:]
    stopped = ["%s %s" % (_hm(t), n) for t, kind, n in events if kind == "stop" and n in seen][-12:]
    if started:
        lines.append("Запускались: " + ", ".join(started) + ".")
    if stopped:
        lines.append("Закрывались: " + ", ".join(stopped) + ".")
    if marks:
        lines.append("Заметки: " + "; ".join("%s %s" % (_hm(t), text) for t, text in marks[-6:]) + ".")
    total = info.get("vram_total")
    vram_vals = [s["vram"] for s in samples if s.get("vram") is not None]
    if total and vram_vals:
        peak = max(vram_vals)
        full = [s for s in samples if s.get("vram") is not None and s["vram"] >= 0.9 * total]
        line = "Видеопамять: сейчас занято %.1f из %.0f ГБ (свободно %.1f ГБ), пик %.1f ГБ." % (
            vram_vals[-1] / 1024.0, total / 1024.0, max(0.0, total - vram_vals[-1]) / 1024.0, peak / 1024.0)
        if full:
            line += " Почти заполнена (90%% и больше) %d мин, впервые в %s." % (
                max(1, round(len(full) * info.get("poll", 10) / 60.0)), _hm(full[0]["t"]))
        lines.append(line)
    heat = heat_lines(samples, find_heat(samples), info.get("temp_warn"))
    if heat:
        lines.append("")
        lines.append("Температуры:")
        lines.extend(heat)
    w = wear(samples, info.get("poll", 10), (info.get("temp_warn") or {}).get("gpu_temp", 75))
    if w:
        lines.append("")
        lines.append("Питание и износ видеокарты:")
        lines.extend(wear_lines(w))
    now = samples[-1]
    lines.append("Сейчас: " + ", ".join("%s %s" % (SHORT[k], fmt(k, now.get(k))) for k in ORDER
                                        if now.get(k) is not None) + ".")
    text = "\n".join(lines)
    if len(text) > max_chars:                       # на всякий случай: режем поминутную часть с начала
        head, _, rest = text.partition("Поминутно")
        text = head + "Поминутно" + rest[len(text) - max_chars:]
    return text


HIGH_MV = 1050          # напряжение ядра «на пределе»: у RTX 30xx потолок около 1080 мВ
LIMIT_SHARE = 0.9       # мощность от 90% предела — карта упирается в предел


def wear(samples, poll=10, hot=75):
    """Сводка «износа» видеокарты: мощность, напряжение, температура, вентилятор. None — нет данных."""
    rows = [s for s in samples if s.get("watts") is not None]
    if not rows:
        return None
    n = len(rows)
    watts = [s["watts"] for s in rows]
    limit = max((s.get("power_limit") or 0) for s in rows) or None
    out = {"avg_w": sum(watts) / n, "peak_w": max(watts), "limit_w": limit,
           "wh": sum(watts) * poll / 3600.0, "minutes": n * poll / 60.0}
    out["at_limit"] = (sum(1 for s in rows if limit and s["watts"] >= LIMIT_SHARE * limit) / n) if limit else None
    mv = [s["volts"] for s in rows if s.get("volts") is not None]
    out["avg_mv"] = sum(mv) / len(mv) if mv else None
    out["peak_mv"] = max(mv) if mv else None
    out["high_mv"] = (sum(1 for v in mv if v >= HIGH_MV) / len(mv)) if mv else None
    temps = [s["gpu_temp"] for s in rows if s.get("gpu_temp") is not None]
    out["peak_temp"] = max(temps) if temps else None
    out["hot"] = (sum(1 for t in temps if t >= hot) / len(temps)) if temps else None
    fans = [s["fan"] for s in rows if s.get("fan") is not None]
    out["peak_fan"] = max(fans) if fans else None
    heavy = [s for s in rows if limit and s["watts"] >= 0.7 * limit] or \
        sorted(rows, key=lambda s: -s["watts"])[:max(1, n // 10)]
    top = top_apps(heavy, "gpu", 2)
    out["who"] = [name for name, _, _ in top]
    return out


def wear_lines(w):
    """Сводка износа простыми строками (для выжимки и окна)."""
    if not w:
        return []
    lines = []
    power = "Мощность в среднем %d Вт, пик %d Вт" % (round(w["avg_w"]), round(w["peak_w"]))
    if w.get("limit_w"):
        power += " из %d Вт предела" % round(w["limit_w"])
    if w.get("at_limit") is not None:
        power += "; у предела (90%%+) %d%% времени" % round(100 * w["at_limit"])
    lines.append(power + ".")
    if w.get("avg_mv") is not None:
        lines.append("Напряжение ядра в среднем %.2f В, пик %.2f В; на высоком (от %.2f В) %d%% времени." % (
            w["avg_mv"] / 1000.0, w["peak_mv"] / 1000.0, HIGH_MV / 1000.0, round(100 * w["high_mv"])))
    if w.get("peak_temp") is not None:
        t = "Температура видеокарты до %d°" % round(w["peak_temp"])
        if w.get("hot"):
            t += ", горячо (75°+) %d%% времени" % round(100 * w["hot"])
        if w.get("peak_fan") is not None:
            t += ", вентилятор до %d%%" % round(w["peak_fan"])
        lines.append(t + ".")
    lines.append("Потрачено энергии за %d мин: %.0f Вт·ч." % (round(w["minutes"]), w["wh"]))
    if w.get("who"):
        lines.append("Сильнее всего нагружали видеокарту: %s." % ", ".join(w["who"]))
    return lines


def window_text(minutes):
    """«30 минут» / «2 часа» — для заголовков и промпта."""
    minutes = int(minutes or 30)
    if minutes < 90:
        return "%d мин" % minutes
    hours = minutes / 60.0
    return "%g ч" % round(hours, 1)


def system_prompt(minutes=30):
    """Что объясняем модели: формат ответа и на что обращать внимание."""
    return (
        "Ты помощник, который разбирает нагрузку домашнего ПК владельца. Тебе дают журнал замеров за последние "
        "%s: поминутные значения, найденные скачки с виновниками и самые прожорливые программы. "
        "Отвечай по-русски, простыми словами, без технического жаргона и коротко. Опирайся только на эти данные; "
        "если их не хватает, так и скажи. Названия программ пиши как в данных, время — как в данных. "
        "Особое внимание — видеопамяти: всегда скажи, сколько её занято и какая программа заняла больше всего; "
        "если её почти не осталось (90%% и больше), обязательно предупреди — тогда модели и игры начинают тормозить. "
        "Владелец бережёт ПК от износа: если видеокарта долго работает у предела мощности, на высоком напряжении или "
        "горячей, скажи об этом и чем это вызвано; если спрашивают про температуру или был нагрев — назови, когда и из-за какой "
        "программы нагрелись видеокарта или процессор, и сравни с порогом внимания; совет давай простой и безопасный (закрыть программу, ограничить "
        "частоту кадров, снизить предел мощности до 80–90%% в MSI Afterburner) и никогда не советуй разгон.\n"
        "Формат ответа строго такой, без заголовков и без Markdown:\n"
        "Коротко: одно предложение — главный ответ на вопрос.\n"
        "- от двух до пяти пунктов с фактами (время, программа, на сколько выросло)\n"
        "Совет: одно предложение, что можно сделать, или «ничего делать не нужно»." % window_text(minutes))


SYSTEM = system_prompt(30)      # для тестов и значения по умолчанию

_BULLET = re.compile(r"^\s*(?:[-•*–—]|\d+[.)])\s*")
_MD = re.compile(r"\*\*|__|`|^#+\s*")


def parse_answer(text):
    """Ответ модели → {"headline", "points", "advice"}. Если модель нарушила формат — берём как получится."""
    headline, advice, points, other = "", "", [], []
    for raw in (text or "").splitlines():
        line = _MD.sub("", raw).strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("коротко:") and not headline:
            headline = line.split(":", 1)[1].strip()
        elif low.startswith("совет:") and not advice:
            advice = line.split(":", 1)[1].strip()
        elif _BULLET.match(line):
            item = _BULLET.sub("", line).strip()
            if item:
                points.append(item)
        else:
            other.append(line)
    if not headline and other:
        headline = other.pop(0)
    if not points:
        points = other[:5]
    if headline:
        headline = headline[0].upper() + headline[1:]
    return {"headline": headline or "Модель не дала ответа", "points": points[:6], "advice": advice}
