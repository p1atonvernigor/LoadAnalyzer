"""Вопрос к Gemini (Google AI Studio) по HTTP. Только стандартная библиотека.

Ключ берётся из data/secrets.json и уходит в заголовке x-goog-api-key — в адрес он не попадает,
поэтому не остаётся в журналах. Ни ключ, ни его части никогда не печатаются и не возвращаются наружу:
в сообщениях об ошибке ключ вырезается.

Модель по умолчанию — gemini-flash-latest (быстрая и дешёвая, см. config/settings.json).

Если у Google перегрузка (ошибка 503/500 или ответ не пришёл вовремя), повторяем запрос
несколько раз с паузой, а потом пробуем запасную модель (по умолчанию gemini-flash-lite-latest).
К Gemini идём через прокси системы (если он настроен в Windows), к окну и LibreHardwareMonitor —
напрямую (это другие файлы).
"""

import json
import re
import socket
import time
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


RETRIES = 3                 # сколько раз пробуем одну модель при перегрузке
PAUSES = (2, 5)             # пауза перед 2-й и 3-й попыткой, секунд
TRY_TIMEOUT = 45            # сколько ждать одну попытку (весь вопрос ограничен timeout)
BUSY_CODES = (500, 502, 503, 504)


class Busy(GeminiError):
    """Google перегружен или не ответил вовремя — есть смысл попробовать ещё раз."""


def _request(url, body, key, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    opener = urllib.request.build_opener()          # с прокси системы, если он есть
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
    if code in BUSY_CODES:
        return Busy("Gemini сейчас перегружен (ошибка %s у Google)" % code)
    if code in (400, 401, 403) and ("key" in detail.lower() or "credential" in detail.lower() or code != 400):
        return GeminiError("ключ Gemini не подошёл: проверьте его в настройках (%s)" % (detail or code))
    if code == 404:
        return GeminiError("такой модели нет: проверьте имя модели в настройках (%s)" % (detail or code))
    if code == 429:
        return GeminiError("Gemini пока отказывает: превышен бесплатный предел запросов, попробуйте позже")
    return GeminiError("Gemini ответил ошибкой %s%s" % (code, ": " + detail if detail else ""))


def _is_timeout(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout)) \
        or "timed out" in str(reason).lower()


def _once(key, system, question, model, max_tokens, timeout, thinking, request):
    """Одна попытка одной модели. Перегрузка и тайм-аут — Busy, остальное — GeminiError."""
    url = BASE % model
    try:
        try:
            return parse_reply(request(url, build_body(system, question, max_tokens, thinking), key, timeout))
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and not thinking:
                # некоторым моделям нельзя отключить «размышления» — пробуем ещё раз как есть
                return parse_reply(request(url, build_body(system, question, max_tokens, True), key, timeout))
            raise
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, key) from None
    except GeminiError:
        raise
    except urllib.error.URLError as exc:
        if _is_timeout(exc):
            raise Busy("Gemini не ответил за %d с" % timeout) from None
        raise GeminiError("нет связи с Gemini: проверьте интернет (%s)" % _clean(exc.reason, key)) from None
    except OSError as exc:
        if _is_timeout(exc):
            raise Busy("Gemini не ответил за %d с" % timeout) from None
        raise GeminiError("не получилось связаться с Gemini: %s" % _clean(exc, key)) from None
    except ValueError:
        raise GeminiError("Gemini ответил не-JSON") from None


def ask_full(key, system, question, model="gemini-flash-latest", max_tokens=1200, timeout=120,
             thinking=False, fallback="gemini-flash-lite-latest", request=_request, sleep=time.sleep,
             clock=time.monotonic, on_retry=None):
    """Вопрос с повторами при перегрузке и запасной моделью.
    Возвращает {"text", "model", "tries"}; ошибки — GeminiError с понятным текстом."""
    key = str(key or "").strip()
    if not key:
        raise GeminiError("нет ключа Gemini: добавьте его в настройках")
    models = []
    for name in (model, fallback):
        name = str(name or "").strip()
        if name and name not in models:
            if not MODEL_RE.match(name):
                raise GeminiError("в имени модели лишние знаки: впишите, например, gemini-flash-latest")
            models.append(name)
    if not models:
        raise GeminiError("не задано имя модели Gemini")
    deadline = clock() + max(10, int(timeout))
    tries, last = 0, None
    for m_i, name in enumerate(models):
        for attempt in range(RETRIES):
            left = deadline - clock()
            if left < 5:
                break
            if attempt or m_i:
                if on_retry:
                    on_retry(name, attempt + 1, m_i > 0)
            tries += 1
            try:
                text = _once(key, system, question, name, max_tokens, min(TRY_TIMEOUT, max(5, round(left))), thinking, request)
                return {"text": text, "model": name, "tries": tries}
            except Busy as exc:
                last = exc
                if attempt < RETRIES - 1:
                    pause = PAUSES[min(attempt, len(PAUSES) - 1)]
                    if deadline - clock() > pause + 5:
                        sleep(pause)
            except GeminiError as exc:
                if m_i == 0 and len(models) > 1 and "такой модели нет" in str(exc):
                    last = exc
                    break                         # основной модели нет — сразу к запасной
                raise
    if isinstance(last, Busy) or last is None:
        raise GeminiError("Gemini сейчас перегружен: у Google очень много запросов. Попробовал %d раз%s — "
                          "спросите ещё раз через несколько минут" % (
                              tries, " и запасную модель" if len(models) > 1 else "")) from None
    raise last


def ask(key, system, question, **kw):
    """Как ask_full, но возвращает только текст ответа."""
    return ask_full(key, system, question, **kw)["text"]
