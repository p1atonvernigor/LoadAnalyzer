"""Локальный веб-сервер окна.

Слушает только 127.0.0.1, то есть из сети недоступен. Дополнительно:
- проверяется заголовок Host (защита от подмены адреса со сторонних сайтов);
- POST принимается только с заголовком X-App-Request: чужая страница в браузере такой не пошлёт;
- тело запроса не больше 10 КБ, ответ страницы — с CSP.

Ключ Gemini можно только задать или убрать: обратно наружу он не отдаётся никогда.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

CSP = ("default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
       "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR позволил бы второму процессу занять уже занятый порт.
    # Выключаем, чтобы второй запуск приложения получил ошибку и просто открыл окно.
    allow_reuse_address = False


class WebServer:
    def __init__(self, app, port):
        self.app = app
        self.httpd = _Server(("127.0.0.1", port), self._make_handler())
        self.port = self.httpd.server_address[1]
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _make_handler(self):
        app = self.app
        boot_id = "%x" % time.time_ns()

        class Handler(BaseHTTPRequestHandler):
            server_version = "LoadAnalyzer"

            def log_message(self, *args):
                pass

            # ----- вспомогательное -----

            def _host_ok(self):
                port = self.server.server_address[1]
                return self.headers.get("Host", "") in ("localhost:%d" % port, "127.0.0.1:%d" % port)

            def _send(self, code, body, content_type="application/json; charset=utf-8", extra=None):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _state(self):
                state = app.state()
                state["boot"] = boot_id      # окно сверяет метку и само перезагружается после перезапуска
                state["settings"] = settings_fields(app)
                return state

            # ----- GET -----

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, {"error": "неверный адрес"})
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    try:
                        page = (STATIC_DIR / "index.html").read_bytes()
                    except OSError:
                        return self._send(500, {"error": "нет файла интерфейса"})
                    return self._send(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
                if path == "/api/state":
                    return self._send(200, self._state())
                return self._send(404, {"error": "не найдено"})

            # ----- POST -----

            def do_POST(self):
                if not self._host_ok():
                    return self._send(403, {"error": "неверный адрес"})
                if self.headers.get("X-App-Request") != "1":
                    return self._send(403, {"error": "нужен заголовок X-App-Request"})
                path = self.path.split("?")[0]
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length > 10000:
                    return self._send(413, {"error": "слишком большой запрос"})
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except ValueError:
                        return self._send(400, {"error": "тело запроса не JSON"})
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "тело запроса не JSON-объект"})
                try:
                    if path == "/api/ask":
                        app.monitor.ask(str(body.get("value", "")))
                    elif path == "/api/clear":
                        app.monitor.clear_answer()
                    elif path == "/api/setting":
                        app.settings.set(str(body.get("key", "")), body.get("value"))
                    elif path == "/api/key":
                        app.secrets.set_key(str(body.get("value", "")))
                    elif path == "/api/window":
                        app.settings.set("window_minutes", body.get("value"))
                    elif path == "/api/open":
                        app.open_page(str(body.get("page", "")))
                    elif path == "/api/hide_hint":
                        app.monitor.hint_hide()
                    else:
                        return self._send(404, {"error": "не найдено"})
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, self._state())

        return Handler


def settings_fields(app):
    """Поля окна «Настройки». Ключ Gemini наружу не отдаётся — только «есть он или нет»."""
    st = app.settings
    return {
        "key_set": app.secrets.has_key(),
        "fields": [
            {"key": "gemini_model", "label": "Модель Gemini", "type": "text",
             "sub": "по умолчанию gemini-flash-latest (быстрая). Можно вписать, например, gemini-3.8-flash",
             "value": st.get("gemini_model")},
            {"key": "gemini_fallback_model", "label": "Запасная модель", "type": "text",
             "sub": "если основная перегружена; по умолчанию gemini-flash-lite-latest (медленнее, но обычно свободна)",
             "value": st.get("gemini_fallback_model")},
            {"key": "history_hours", "label": "Хранить журнал нагрузки", "type": "int", "unit": "ч",
             "sub": "сколько часов держать замеры (файл data/history.jsonl)", "min": 1, "max": 72,
             "value": st.get("history_hours")},
            {"key": "window_minutes", "label": "Отрезок для графиков и разбора", "type": "int", "unit": "мин",
             "sub": "что показывать на графиках и отдавать Gemini", "min": 5, "max": 4320,
             "value": st.get("window_minutes")},
            {"key": "poll_seconds", "label": "Замер каждые", "type": "int", "unit": "с",
             "sub": "чаще — подробнее журнал, но больше нагрузки", "min": 5, "max": 120,
             "value": st.get("poll_seconds")},
            {"key": "gemini_max_tokens", "label": "Длина ответа", "type": "int", "unit": "токенов",
             "sub": "если ответы обрываются — увеличьте", "min": 200, "max": 8000,
             "value": st.get("gemini_max_tokens")},
            {"key": "gemini_timeout_seconds", "label": "Сколько ждать ответа", "type": "int", "unit": "с",
             "sub": "всего, вместе с повторами, если Gemini перегружен", "min": 10, "max": 600, "value": st.get("gemini_timeout_seconds")},
            {"key": "gemini_thinking", "label": "Разрешить модели «размышлять»", "type": "bool",
             "sub": "выключено — ответ быстрее", "value": bool(st.get("gemini_thinking"))},
            {"key": "lhm_url", "label": "Адрес LibreHardwareMonitor", "type": "text",
             "sub": "откуда брать температуру процессора", "value": st.get("lhm_url")},
            {"key": "temp_gpu_warn", "label": "Порог внимания: видеокарта", "type": "int", "unit": "°",
             "min": 40, "max": 100, "value": st.get("temp_gpu_warn")},
            {"key": "temp_cpu_warn", "label": "Порог внимания: процессор", "type": "int", "unit": "°",
             "min": 40, "max": 110, "value": st.get("temp_cpu_warn")},
            {"key": "game_gpu_percent", "label": "«Идёт игра» от", "type": "int", "unit": "% GPU",
             "sub": "программа грузит видеокарту на столько — считаем, что идёт игра", "min": 10, "max": 100,
             "value": st.get("game_gpu_percent")},
            {"key": "open_window_on_start", "label": "Открывать окно при запуске", "type": "bool",
             "value": bool(st.get("open_window_on_start"))},
            {"key": "ui_port", "label": "Порт окна", "type": "int", "unit": "",
             "sub": "изменится после перезапуска приложения", "min": 1024, "max": 65535,
             "value": st.get("ui_port")},
        ],
    }
