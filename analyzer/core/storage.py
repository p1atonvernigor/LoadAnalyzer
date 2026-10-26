"""Чтение и запись JSON-файлов."""

import json
import os
from pathlib import Path


def read_json(path, default):
    """Читает JSON. Если файла нет или он повреждён, возвращает default."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path, data):
    """Пишет JSON через временный файл, чтобы сбой не оставил половину файла."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
