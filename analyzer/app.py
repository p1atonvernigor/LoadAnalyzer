"""Точка входа: настройки, сбор нагрузки, локальный сервер и окно интерфейса."""

import argparse
import os
import subprocess
import time
import webbrowser
from pathlib import Path

from .core.logbook import Logbook
from .core.secrets import Secrets
from .core.settings import Settings
from .monitor import Monitor
from .web.server import WebServer

ROOT = Path(__file__).resolve().parents[1]
KEY_PAGE = "https://aistudio.google.com/apikey"   # где владелец берёт ключ Gemini


class App:
    """Всё приложение в одном объекте: настройки, ключ, журнал событий и сбор нагрузки."""

    def __init__(self, root=None, data_dir=None, config_dir=None):
        self.root = Path(root) if root else ROOT
        self.config_dir = Path(config_dir) if config_dir else self.root / "config"
        self.data_dir = Path(data_dir) if data_dir else self.root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.config_dir)
        self.secrets = Secrets(self.data_dir)
        self.log = Logbook(self.data_dir / "logs" / "app.log")
        self.monitor = Monitor(self.settings, self.data_dir, self._log)
        self.monitor.key = self.secrets.key

    def _log(self, message, level="info"):
        self.log.add("анализ", message, level)

    def open_page(self, page):
        """Открывает в браузере на ПК одну из двух нужных страниц: загрузку LibreHardwareMonitor
        или получение ключа Gemini."""
        from .monitor import LHM_PAGE
        pages = {"lhm": (LHM_PAGE, "страницу загрузки LibreHardwareMonitor"),
                 "key": (KEY_PAGE, "страницу ключа Gemini")}
        if page not in pages:
            raise ValueError("неизвестная страница")
        url, what = pages[page]
        if page == "lhm":
            self.monitor.hint_opened()
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._log("не получилось открыть %s: %s" % (what, exc), "warn")
            return False
        self._log("открыл " + what)
        return True

    def state(self):
        state = self.monitor.state()
        state["log"] = self.log.recent(20)
        return state


def _find_edge():
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var)
        if base:
            path = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if path.exists():
                return str(path)
    return None


def open_ui(url):
    """Открывает интерфейс отдельным окном Edge без адресной строки, иначе в браузере по умолчанию."""
    edge = _find_edge()
    if edge:
        try:
            subprocess.Popen([edge, "--app=" + url])
            return
        except OSError:
            pass
    webbrowser.open(url)


PORT_TRIES = 10            # порт занят чужой программой или закрыт Windows — пробуем следующие


def is_ours(port, timeout=2):
    """На этом порту уже работает наше же приложение?"""
    import json
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/state" % port,
                                     headers={"Host": "localhost:%d" % port})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("title") == "Анализ нагрузки"
    except Exception:
        return False


def start_server(app, port, tries=PORT_TRIES, make=None):
    """(сервер, порт, уже_запущено). На Windows порт бывает занят чужой программой или зарезервирован
    системой (ошибка 10013) — тогда берём следующий. Если на порту уже наше приложение — не дублируем."""
    make = make or WebServer
    for p in range(port, port + max(1, tries)):
        try:
            return make(app, p), p, False
        except OSError:
            if is_ours(p):
                return None, p, True
    return None, port, False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Анализ нагрузки ПК")
    parser.add_argument("--no-window", action="store_true", help="не открывать окно интерфейса")
    parser.add_argument("--port", type=int, default=None, help="порт окна (по умолчанию из config/settings.json)")
    args = parser.parse_args(argv)

    app = App()
    port = args.port if args.port is not None else int(app.settings.get("ui_port"))
    want_window = app.settings.get("open_window_on_start") and not args.no_window

    server, port, ours = start_server(app, port, tries=1 if args.port is not None else PORT_TRIES)
    if ours:
        print("Приложение уже запущено (порт %d). Открываю окно." % port)
        if want_window:
            open_ui("http://localhost:%d" % port)
        return 0
    if server is None:
        print("Не получилось открыть окно: порты %d–%d заняты другими программами или закрыты Windows."
              % (port, port + PORT_TRIES - 1))
        print("Поменяйте ui_port в config\\settings.json (например, на 8790) и запустите снова.")
        return 1
    url = "http://localhost:%d" % port

    server.start()
    app.monitor.start()
    app.log.add("приложение", "Запущено, окно: " + url)
    print("Анализ нагрузки работает: " + url)
    print("Чтобы закрыть, закройте это окно или нажмите Ctrl+C.")
    if want_window:
        open_ui(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.monitor.stop()
        server.stop()
        app.log.add("приложение", "Остановлено")
    return 0
