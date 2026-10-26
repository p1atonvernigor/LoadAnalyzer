"""Ключ Gemini. Лежит в data/secrets.json — это личный файл, он не попадает на GitHub (.gitignore).

Ключ никогда не печатается в журнал и не отдаётся в окно: наружу уходит только «ключ есть / ключа нет».
"""

from pathlib import Path

from .storage import read_json, write_json

KEY = "gemini_api_key"


class Secrets:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "secrets.json"

    def key(self):
        """Сохранённый ключ или пустая строка."""
        data = read_json(self.path, {})
        value = data.get(KEY) if isinstance(data, dict) else ""
        return str(value or "").strip()

    def has_key(self):
        return bool(self.key())

    def set_key(self, value):
        """Сохраняет ключ (пустая строка — убрать ключ). Возвращает True, если ключ теперь есть."""
        value = str(value or "").strip()
        if value and (len(value) < 20 or any(c.isspace() for c in value)):
            raise ValueError("это не похоже на ключ Gemini: он длиннее 20 знаков и без пробелов")
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        if value:
            data[KEY] = value
        else:
            data.pop(KEY, None)
        write_json(self.path, data)
        self._protect()
        return bool(value)

    def _protect(self):
        """На всякий случай убираем у файла права для остальных пользователей (на Windows ничего не меняет)."""
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
