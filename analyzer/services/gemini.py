"""Вопрос к Gemini (Google AI Studio) по HTTP. Только стандартная библиотека.

Ключ берётся из data/secrets.json и уходит в заголовке x-goog-api-key — в адрес он не попадает,
поэтому не остаётся в журналах. Ни ключ, ни его части никогда не печатаются и не возвращаются наружу:
в сообщениях об ошибке ключ вырезается.

Модель по умолчанию — gemini-flash-latest (быстрая и дешёвая, см. config/settings.json).
"""

import json
import re
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class GeminiError(Exception):
    pass


def _clean(text, key):
    """Убирает ключ из текста ошибки на всякий случай."""
    text = str(text or "")
    if key:
        text = text.replace(key, "***")
    return " ".join(text.split())[:300]


def build_body(system, question, max_tokens=1200, thinking=False):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": int(max_tokens),
                             "topP": 0.95},
    }
    if not thinking:
        # просим модель не «размышлять»: ответ быстрее, и весь предел длины уходит на сам ответ
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    return body


def parse_reply(data):
    """Ответ Gemini → текст. Если текста нет, объясняет почему."""
    if not isinstance(data, dict):
        raise GeminiError("Gemini ответил непонятным сообщением")
    blocked = (data.get("promptFeedback") or {}).get("blockReason")
    if blocked:
        raise GeminiError("Gemini отказался отвечать (%s)" % blocked)
    for cand in data.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        text = "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
        if text:
            return text
        if cand.get("finishReason") == "MAX_TOKENS":
            raise GeminiError("ответ не поместился в предел длины: увеличьте «длину ответа» в настройках")
    raise GeminiError("Gemini вернул пустой ответ")


def _request(url, body, key, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _http_error(exc, key):
    """Понятное объяснение кодов ошибок Gemini."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        detail = _clean((body.get("error") or {}).get("message"), key)
    except Exception:
        detail = ""
    code = getattr(exc, "code", 0)
    if code in (400, 401, 403) and ("key" in detail.lower() or "credential" in detail.lower() or code != 400):
        return GeminiError("ключ Gemini не подошёл: проверьте его в настройках (%s)" % (detail or code))
    if code == 404:
        return GeminiError("такой модели нет: проверьте имя модели в настройках (%s)" % (detail or code))
    if code == 429:
        return GeminiError("Gemini пока отказывает: превышен бесплатный предел запросов, попробуйте позже")
    if code in (500, 503):
        return GeminiError("Gemini сейчас не отвечает (ошибка на их стороне), попробуйте ещё раз")
    return GeminiError("Gemini ответил ошибкой %s%s" % (code, ": " + detail if detail else ""))


def ask(key, system, question, model="gemini-flash-latest", max_tokens=1200, timeout=120,
        thinking=False, request=_request):
    """Задаёт вопрос модели и возвращает текст ответа. Ошибки — GeminiError с понятным текстом."""
    key = str(key or "").strip()
    if not key:
        raise GeminiError("нет ключа Gemini: добавьте его в настройках")
    model = str(model or "").strip()
    if not MODEL_RE.match(model):
        raise GeminiError("в имени модели лишние знаки: впишите, например, gemini-flash-latest")
    url = BASE % model
    body = build_body(system, question, max_tokens, thinking)
    try:
        data = request(url, body, key, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and not thinking:
            # некоторым моделям нельзя отключить «размышления» — пробуем ещё раз как есть
            try:
                data = request(url, build_body(system, question, max_tokens, True), key, timeout)
            except urllib.error.HTTPError as exc2:
                raise _http_error(exc2, key) from None
            except OSError as exc2:
                raise GeminiError("не получилось связаться с Gemini: %s" % _clean(exc2, key)) from None
        else:
            raise _http_error(exc, key) from None
    except urllib.error.URLError as exc:
        raise GeminiError("нет связи с Gemini: проверьте интернет (%s)" % _clean(exc.reason, key)) from None
    except OSError as exc:
        raise GeminiError("не получилось связаться с Gemini: %s" % _clean(exc, key)) from None
    except ValueError:
        raise GeminiError("Gemini ответил не-JSON") from None
    return parse_reply(data)
