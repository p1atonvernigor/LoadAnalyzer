"""Настройки приложения: значения по умолчанию плюс config/settings.json.

В файле лежат только отличия от значений ниже, поэтому его можно удалить — приложение вернётся
к настройкам по умолчанию. Ключ Gemini здесь НЕ хранится (он в data/secrets.json, см. secrets.py).
"""

from pathlib import Path

from .storage import read_json, write_json

DEFAULTS = {
    "ui_port": 8777,                  # адрес окна: http://localhost:8777
    "open_window_on_start": True,     # открывать окно при запуске
    "poll_seconds": 10,               # как часто записывать, какие программы что берут
    "history_hours": 12,              # сколько часов хранить журнал нагрузки
    "window_minutes": 30,             # какой отрезок показывать на графиках и отдавать ИИ
    # ИИ (Gemini). Ключ — в data/secrets.json, здесь только имя модели и пределы.
    "gemini_model": "gemini-flash-latest",
    "gemini_max_tokens": 1200,        # предел длины ответа
    "gemini_timeout_seconds": 120,    # сколько ждать ответа
    "gemini_thinking": False,         # False = просить модель не «размышлять» (ответ быстрее и дешевле)
    # датчики
    "lhm_url": "http://localhost:8085/data.json",  # LibreHardwareMonitor: температура процессора
    "temp_gpu_warn": 75,              # порог внимания для видеокарты, °C
    "temp_cpu_warn": 78,              # порог внимания для процессора, °C
    "game_gpu_percent": 40,           # программа грузит видеокарту на столько — считаем, что идёт игра
}

# Пределы для настроек из окна: ключ → (минимум, максимум)
LIMITS = {
    "poll_seconds": (5, 120),
    "history_hours": (1, 72),
    "window_minutes": (5, 4320),
    "gemini_max_tokens": (200, 8000),
    "gemini_timeout_seconds": (10, 600),
    "temp_gpu_warn": (40, 100),
    "temp_cpu_warn": (40, 110),
    "game_gpu_percent": (10, 100),
    "ui_port": (1024, 65535),
}

TEXT_KEYS = ("gemini_model", "lhm_url")
BOOL_KEYS = ("open_window_on_start", "gemini_thinking")


class Settings:
    def __init__(self, config_dir):
        self.path = Path(config_dir) / "settings.json"
        self.values = dict(DEFAULTS)
        stored = read_json(self.path, {})
        if isinstance(stored, dict):
            self.values.update({k: v for k, v in stored.items() if k in DEFAULTS})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        """Меняет настройку и сохраняет её в settings.json. Проверяет тип и пределы."""
        if key not in DEFAULTS:
            raise ValueError("нет такой настройки")
        if key in BOOL_KEYS:
            value = bool(value) if isinstance(value, bool) else str(value).lower() in ("1", "true", "вкл", "on")
        elif key in TEXT_KEYS:
            value = " ".join(str(value or "").split())[:200]
            if key == "lhm_url" and value and not value.startswith(("http://", "https://")):
                raise ValueError("адрес должен начинаться с http://")
            if key == "gemini_model" and not value:
                raise ValueError("имя модели не может быть пустым")
        else:
            try:
                value = int(float(value))
            except (TypeError, ValueError):
                raise ValueError("нужно число") from None
            lo, hi = LIMITS.get(key, (0, 10 ** 9))
            if not lo <= value <= hi:
                raise ValueError("нужно число от %d до %d" % (lo, hi))
        self.values[key] = value
        stored = read_json(self.path, {})
        if not isinstance(stored, dict):
            stored = {}
        stored[key] = value
        write_json(self.path, stored)
        return value
