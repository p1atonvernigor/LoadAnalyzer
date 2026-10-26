"""Клиент Gemini: что уходит в запросе, как читается ответ, понятны ли ошибки. Без интернета."""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyzer.services import gemini  # noqa: E402

KEY = "AIzaSyFAKEKEYFAKEKEYFAKEKEY"


def reply(text, finish="STOP"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]}


class FakeRequest:
    """Подмена похода в интернет: помнит вызовы и отдаёт заготовленные ответы."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, body, key, timeout):
        self.calls.append({"url": url, "body": body, "key": key, "timeout": timeout})
        answer = self.answers.pop(0) if self.answers else reply("Коротко: всё спокойно.")
        if isinstance(answer, Exception):
            raise answer
        return answer


def http_error(code, message=""):
    body = json.dumps({"error": {"message": message}}).encode()
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


class Clock:
    """Подмена времени: паузы не ждут по-настоящему, но время идёт."""

    def __init__(self):
        self.now, self.pauses = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class BusyTest(unittest.TestCase):
    """26.10: у Google была перегрузка Flash — 503 «high demand» и зависания до тайм-аута.
    Раньше вопрос сразу падал с «The read operation timed out»."""

    def ask(self, req, **kw):
        clock = Clock()
        kw.setdefault("timeout", 180)
        got = gemini.ask_full(KEY, "s", "q", request=req, sleep=clock.sleep, clock=clock, **kw)
        return got, clock

    def test_retries_on_503_and_succeeds(self):
        req = FakeRequest(http_error(503, "high demand"), http_error(503), reply("ответ"))
        got, clock = self.ask(req)
        self.assertEqual(got, {"text": "ответ", "model": "gemini-flash-latest", "tries": 3})
        self.assertEqual(clock.pauses, [2, 5])
        self.assertTrue(all(c["url"] == gemini.BASE % "gemini-flash-latest" for c in req.calls))

    def test_timeout_is_retried(self):
        req = FakeRequest(TimeoutError("The read operation timed out"), reply("ответ"))
        got, _ = self.ask(req)
        self.assertEqual(got["tries"], 2)
        req = FakeRequest(urllib.error.URLError(TimeoutError("timed out")), reply("ответ"))
        self.assertEqual(self.ask(req)[0]["text"], "ответ")

    def test_fallback_model_when_main_is_busy(self):
        req = FakeRequest(http_error(503), http_error(503), http_error(503), reply("ответ"))
        notes = []
        got, _ = self.ask(req, on_retry=lambda m, a, f: notes.append((m, a, f)))
        self.assertEqual(got["model"], "gemini-flash-lite-latest")
        self.assertEqual(req.calls[-1]["url"], gemini.BASE % "gemini-flash-lite-latest")
        self.assertIn(("gemini-flash-lite-latest", 1, True), notes)

    def test_fallback_when_main_model_missing(self):
        req = FakeRequest(http_error(404, "model not found"), reply("ответ"))
        got, _ = self.ask(req)
        self.assertEqual(got["model"], "gemini-flash-lite-latest")

    def test_all_busy_gives_plain_message(self):
        req = FakeRequest(*[http_error(503)] * 6)
        with self.assertRaises(gemini.GeminiError) as e:
            self.ask(req)
        self.assertIn("перегружен", str(e.exception))
        self.assertIn("6 раз и запасную модель", str(e.exception))
        self.assertEqual(len(req.calls), 6)

    def test_wrong_key_is_not_retried(self):
        req = FakeRequest(*[http_error(403, "API key not valid")] * 6)
        with self.assertRaises(gemini.GeminiError) as e:
            self.ask(req)
        self.assertIn("ключ Gemini не подошёл", str(e.exception))
        self.assertEqual(len(req.calls), 1)

    def test_total_time_is_limited(self):
        """Каждая попытка ждёт не больше TRY_TIMEOUT, а всё вместе — не дольше timeout."""
        clock = Clock()

        def slow(url, body, key, timeout):
            self.assertLessEqual(timeout, gemini.TRY_TIMEOUT)
            clock.now += timeout
            raise TimeoutError("The read operation timed out")

        with self.assertRaises(gemini.GeminiError):
            gemini.ask_full(KEY, "s", "q", timeout=100, request=slow, sleep=clock.sleep, clock=clock)
        self.assertLessEqual(clock.now, 100)

    def test_system_proxy_is_used(self):
        """С VPN или прокси Windows Gemini должен идти через них — ProxyHandler({}) их отключал."""
        import urllib.request
        made = []
        real = urllib.request.build_opener

        def fake_build(*handlers):
            made.append(handlers)
            raise OSError("стоп")

        urllib.request.build_opener = fake_build
        try:
            with self.assertRaises(OSError):
                gemini._request("http://x", {}, KEY, 5)
        finally:
            urllib.request.build_opener = real
        self.assertEqual(made, [()])


class GeminiTest(unittest.TestCase):
    def test_asks_and_returns_text(self):
        req = FakeRequest(reply("Коротко: видеопамять заняла игра."))
        got = gemini.ask(KEY, "система", "вопрос", model="gemini-flash-latest", max_tokens=900, timeout=42,
                         request=req)
        self.assertEqual(got, "Коротко: видеопамять заняла игра.")
        call = req.calls[0]
        self.assertEqual(call["url"], gemini.BASE % "gemini-flash-latest")
        self.assertEqual(call["key"], KEY)
        self.assertEqual(call["timeout"], 42)
        self.assertEqual(call["body"]["systemInstruction"]["parts"][0]["text"], "система")
        self.assertEqual(call["body"]["contents"][0]["parts"][0]["text"], "вопрос")
        self.assertEqual(call["body"]["generationConfig"]["maxOutputTokens"], 900)
        self.assertEqual(call["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)

    def test_thinking_can_be_allowed(self):
        req = FakeRequest(reply("ответ"))
        gemini.ask(KEY, "s", "q", thinking=True, request=req)
        self.assertNotIn("thinkingConfig", req.calls[0]["body"]["generationConfig"])

    def test_retries_once_when_model_demands_thinking(self):
        """Некоторым моделям нельзя отключить «размышления» — тогда пробуем ещё раз как есть."""
        req = FakeRequest(http_error(400, "thinkingBudget is not supported"), reply("ответ"))
        self.assertEqual(gemini.ask(KEY, "s", "q", request=req), "ответ")
        self.assertEqual(len(req.calls), 2)
        self.assertNotIn("thinkingConfig", req.calls[1]["body"]["generationConfig"])

    def test_no_key(self):
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask("", "s", "q", request=FakeRequest())
        self.assertIn("нет ключа", str(e.exception))

    def test_bad_model_name(self):
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", model="моя модель!", request=FakeRequest())
        self.assertIn("имени модели", str(e.exception))

    def test_wrong_key_message(self):
        req = FakeRequest(http_error(400, "API key not valid. Please pass a valid API key."),
                          http_error(400, "API key not valid. Please pass a valid API key."))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=req)
        self.assertIn("ключ Gemini не подошёл", str(e.exception))

    def test_limit_and_missing_model_messages(self):
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest(http_error(429)))
        self.assertIn("предел запросов", str(e.exception))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", fallback="", request=FakeRequest(http_error(404, "model not found")))
        self.assertIn("такой модели нет", str(e.exception))

    def test_key_is_never_shown_in_error(self):
        req = FakeRequest(http_error(403, "key %s is blocked" % KEY), http_error(403, "key %s is blocked" % KEY))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=req)
        self.assertNotIn(KEY, str(e.exception))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest(OSError("сбой с ключом %s" % KEY)))
        self.assertNotIn(KEY, str(e.exception))

    def test_no_internet(self):
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest(urllib.error.URLError("имя не разрешается")))
        self.assertIn("нет связи с Gemini", str(e.exception))

    def test_empty_and_cut_answers(self):
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest(reply("", "MAX_TOKENS")))
        self.assertIn("предел длины", str(e.exception))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest({"candidates": []}))
        self.assertIn("пустой ответ", str(e.exception))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest({"promptFeedback": {"blockReason": "SAFETY"}}))
        self.assertIn("отказался", str(e.exception))


if __name__ == "__main__":
    unittest.main()
