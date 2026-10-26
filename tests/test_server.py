"""Окно и сервер: что отдаётся в /api/state, какие POST принимаются, и что ключ Gemini не утекает."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.app import App  # noqa: E402
from analyzer.web.server import WebServer  # noqa: E402

from test_monitor import FakeApps, FakeGpuApps, FakeModel, KEY, T0, make_monitor  # noqa: E402


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir(exist_ok=True)
        self.app = App(root=ROOT, data_dir=root, config_dir=root / "config")
        self.app.monitor = make_monitor(self.tmp.name, settings=self.app.settings, secrets=self.app.secrets)
        self.opened = []
        self.app.open_page = lambda page: self.opened.append(page)
        for i in range(10):
            self.app.monitor.tick(T0 + 10 * i)
        self.server = WebServer(self.app, 0)
        self.server.start()
        self.base = "http://127.0.0.1:%d" % self.server.port

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read()

    def post(self, path, body=None, headers=None):
        head = {"Content-Type": "application/json", "X-App-Request": "1"}
        head.update(headers or {})
        req = urllib.request.Request(self.base + path, method="POST", headers=head,
                                     data=json.dumps(body or {}).encode())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # ----- GET -----

    def test_page_and_state(self):
        code, page = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>", page)
        code, raw = self.get("/api/state")
        state = json.loads(raw)
        self.assertEqual(state["samples"], 10)
        self.assertTrue(state["boot"])
        self.assertEqual(state["window_minutes"], 30)
        self.assertTrue(state["settings"]["fields"])
        self.assertFalse(state["settings"]["key_set"])

    def test_wrong_host_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/state", {"Host": "evil.example.com"})
        self.assertEqual(e.exception.code, 403)

    def test_post_needs_header(self):
        code, body = self.post("/api/clear", headers={"X-App-Request": "0"})
        self.assertEqual(code, 403)
        self.assertIn("X-App-Request", body["error"])

    def test_unknown_path(self):
        self.assertEqual(self.post("/api/nope")[0], 404)
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/nope")

    # ----- ключ -----

    def test_key_is_saved_but_never_returned(self):
        code, state = self.post("/api/key", {"value": KEY})
        self.assertEqual(code, 200)
        self.assertTrue(state["key_set"])
        self.assertTrue(state["settings"]["key_set"])
        self.assertNotIn(KEY, json.dumps(state, ensure_ascii=False))
        code, raw = self.get("/api/state")
        self.assertNotIn(KEY.encode(), raw)
        self.assertEqual(self.app.secrets.key(), KEY)
        code, state = self.post("/api/key", {"value": ""})
        self.assertFalse(state["key_set"])

    def test_bad_key_is_refused_with_plain_words(self):
        code, body = self.post("/api/key", {"value": "коротко"})
        self.assertEqual(code, 400)
        self.assertIn("не похоже на ключ", body["error"])

    def test_key_is_not_in_log(self):
        self.post("/api/key", {"value": KEY})
        self.app.monitor.ask_model = FakeModel()
        self.post("/api/ask", {"value": "почему?"})
        text = json.dumps(self.app.log.recent(50), ensure_ascii=False)
        self.assertNotIn(KEY, text)

    # ----- вопрос и настройки -----

    def test_ask_runs_and_answer_appears(self):
        self.post("/api/key", {"value": KEY})
        self.app.monitor.ask_model = FakeModel()
        code, state = self.post("/api/ask", {"value": "почему вырос GPU?"})
        self.assertEqual(code, 200)
        self.assertEqual(state["last"]["headline"], "Всё из-за игры.")
        code, state = self.post("/api/clear")
        self.assertIsNone(state["last"])

    def test_ask_without_key_says_why(self):
        code, body = self.post("/api/ask", {"value": "почему?"})
        self.assertEqual(code, 400)
        self.assertIn("ключа Gemini", body["error"])

    def test_settings_change_and_are_checked(self):
        code, state = self.post("/api/setting", {"key": "history_hours", "value": 6})
        self.assertEqual(state["history_hours"], 6.0)
        code, body = self.post("/api/setting", {"key": "history_hours", "value": 999})
        self.assertEqual(code, 400)
        self.assertIn("от 1 до 72", body["error"])
        code, body = self.post("/api/setting", {"key": "ui_port", "value": "не порт"})
        self.assertEqual(code, 400)

    def test_window_switch(self):
        self.post("/api/setting", {"key": "history_hours", "value": 12})
        code, state = self.post("/api/window", {"value": 720})
        self.assertEqual(state["window_minutes"], 720)
        self.assertEqual(state["window_text"], "12 ч")

    def test_hint_buttons(self):
        self.app.monitor.read_lhm = lambda url: None
        self.app.monitor._lhm_skip = 0
        self.app.monitor.tick(T0 + 200)
        code, state = self.get("/api/state")
        self.assertIn("LibreHardwareMonitor", json.loads(state)["hint"]["text"])
        self.post("/api/open", {"page": "lhm"})
        self.assertEqual(self.opened, ["lhm"])
        code, state = self.post("/api/hide_hint")
        self.assertIsNone(state["hint"])

    def test_big_body_is_refused(self):
        code, body = self.post("/api/ask", {"value": "х" * 20000})
        self.assertEqual(code, 413)

    def test_broken_json(self):
        req = urllib.request.Request(self.base + "/api/ask", method="POST",
                                     headers={"Content-Type": "application/json", "X-App-Request": "1"},
                                     data="{не json".encode("utf-8"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            opener.open(req, timeout=5)
            self.fail("должен был отказать")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


class PortTest(unittest.TestCase):
    """26.10: на Windows порт бывает занят чужой программой или закрыт системой — берём следующий."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config").mkdir()
        self.app = App(root=ROOT, data_dir=root, config_dir=root / "config")

    def tearDown(self):
        self.tmp.cleanup()

    def test_busy_port_moves_to_next(self):
        import analyzer.app as module
        busy = {9000, 9001}

        def make(app, port):
            if port in busy:
                raise PermissionError(10013, "закрыт Windows")
            return "сервер на %d" % port

        server, port, ours = module.start_server(self.app, 9000, make=make)
        self.assertEqual((server, port, ours), ("сервер на 9002", 9002, False))
        server, port, ours = module.start_server(self.app, 9000, tries=2, make=make)
        self.assertIsNone(server)
        self.assertFalse(ours)

    def test_our_app_already_running_is_detected(self):
        import analyzer.app as module
        first = WebServer(self.app, 0)
        first.start()
        try:
            self.assertTrue(module.is_ours(first.port))

            def make(app, port):
                raise OSError("занят")

            server, port, ours = module.start_server(self.app, first.port, tries=1, make=make)
            self.assertTrue(ours)
            self.assertEqual(port, first.port)
        finally:
            first.stop()


class OpenPageTest(unittest.TestCase):
    def test_only_two_pages_can_be_opened(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "config").mkdir()
        app = App(root=ROOT, data_dir=root, config_dir=root / "config")
        opened = []
        import analyzer.app as module
        module.webbrowser = type("W", (), {"open": staticmethod(lambda url: opened.append(url))})
        app.open_page("key")
        app.open_page("lhm")
        self.assertEqual(opened, ["https://aistudio.google.com/apikey",
                                  "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases"])
        with self.assertRaises(ValueError):
            app.open_page("http://evil.example.com")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
