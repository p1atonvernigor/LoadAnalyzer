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
            gemini.ask(KEY, "s", "q", request=FakeRequest(http_error(404, "model not found")))
        self.assertIn("такой модели нет", str(e.exception))
        with self.assertRaises(gemini.GeminiError) as e:
            gemini.ask(KEY, "s", "q", request=FakeRequest(http_error(503)))
        self.assertIn("не отвечает", str(e.exception))

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
