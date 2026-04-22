"""
core/ai_client.py
Wywołania modelu AI (DeepSeek), sanitizacja odpowiedzi.

OPTYMALIZACJE PAMIĘCI (512 MB):
  - Jawne zamykanie response po każdym requests.post (resp.close())
  - del na dużych zmiennych pośrednich
  - Użycie stream=False (domyślne, ale jawne) + timeout agresywniejszy
  - Brak trzymania całej odpowiedzi JSON gdy nie potrzeba
"""
import os
import json
import time
import requests
from flask import current_app

from core.logging_reporter import get_logger

DEEPSEEK_API_KEY = os.getenv("API_KEY_DEEPSEEK")
MODEL_BIZ        = os.getenv("MODEL_BIZ",   "deepseek-chat")
MODEL_TYLER      = os.getenv("MODEL_TYLER", "deepseek-chat")


def sanitize_model_output(raw_text: str) -> str:
    """Usuwa JSON-wrappery, zwraca czysty tekst odpowiedzi modelu."""
    if not raw_text:
        return ""
    txt = raw_text.strip()

    if txt.startswith("{") or txt.startswith("["):
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict):
                for key in ("odpowiedz_tekstowa", "reply", "answer",
                            "text", "message", "reply_html", "content"):
                    if key in obj:
                        val = obj[key]
                        result = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
                        del obj
                        return result
                if len(obj) == 1:
                    val = next(iter(obj.values()))
                    result = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
                    del obj
                    return result
            if isinstance(obj, list):
                result = "\n".join(str(x) for x in obj)
                del obj
                return result
        except Exception:
            pass

    if txt.startswith("{") and "}" in txt:
        try:
            end = txt.index("}") + 1
            maybe_json = txt[:end]
            remainder  = txt[end:].strip()
            try:
                json.loads(maybe_json)
                if remainder:
                    return remainder
            except Exception:
                return txt[end:].strip()
        except Exception:
            pass

    return raw_text


def extract_clean_text(text: str) -> str:
    """Wyciąga pole 'odpowiedz_tekstowa' z JSON-a jeśli istnieje."""
    import re
    if not text:
        return ""
    txt   = text.strip()
    match = re.search(r'\{.*\}', txt, re.DOTALL)
    if not match:
        return txt
    try:
        obj = json.loads(match.group(0))
        if isinstance(obj, dict) and "odpowiedz_tekstowa" in obj:
            val = obj["odpowiedz_tekstowa"]
            result = val.strip() if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            del obj
            return result
        del obj
        return txt
    except Exception:
        return txt


def call_deepseek(system_prompt: str, user_msg: str, model_name: str,
                  timeout: int = 35, max_retries: int = 1,
                  retry_delay: float = 2.0, max_tokens: int = 3000):
    """
    Wywołanie modelu przez API DeepSeek.
    Zwraca czysty tekst lub None przy błędzie.

    OPTYMALIZACJA: resp.close() po każdym żądaniu, del na pośrednich zmiennych.
    """
    if not DEEPSEEK_API_KEY:
        current_app.logger.error("Brak API_KEY_DEEPSEEK")
        return None

    url     = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":    model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens":  max_tokens,
    }

    for attempt in range(1, max_retries + 1):
        resp = None
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=(5, 120), stream=False)

            if resp.status_code == 429:
                current_app.logger.warning(
                    "API rate limit (429), próba %d/%d", attempt, max_retries
                )
                _log_api(model_name, False, "RATE_LIMIT")
                resp.close()
                resp = None
                if attempt < max_retries:
                    wait = min(retry_delay * attempt, 30.0)
                    current_app.logger.warning("Czekam %.0fs przed kolejną próbą", wait)
                    time.sleep(wait)
                    continue
                current_app.logger.error("API rate limit po %d próbach — rezygnuję", max_retries)
                return None

            if resp.status_code != 200:
                current_app.logger.warning(
                    "API non-200 (%s): %s", resp.status_code, resp.text[:300]
                )
                _log_api(model_name, False, f"HTTP {resp.status_code}")
                resp.close()
                return None

            # Wyciągnij treść i od razu zamknij połączenie
            try:
                data = resp.json()
            except Exception:
                raw_text = resp.text
                resp.close()
                resp = None
                _log_api(model_name, True)
                result = sanitize_model_output(raw_text)
                del raw_text
                return result

            resp.close()
            resp = None

            # Wyciągnij content z danych i od razu usuń cały obiekt data
            content = None
            try:
                content = data["choices"][0]["message"]["content"]
            except Exception:
                if isinstance(data, dict):
                    for key in ("content", "text", "message", "reply"):
                        if key in data and isinstance(data[key], str):
                            content = data[key]
                            break
                if not content:
                    content = json.dumps(data, ensure_ascii=False)
            finally:
                del data  # zwolnij cały response JSON

            _log_api(model_name, True)
            result = sanitize_model_output(content)
            del content
            return result

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            current_app.logger.warning(
                "API timeout/connection error (próba %d/%d): %s",
                attempt, max_retries, e
            )
            _log_api(model_name, False, str(e))
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                current_app.logger.error(
                    "API niedostępne po %d próbach: %s", max_retries, e
                )
                return None

        except Exception as e:
            current_app.logger.exception("Nieoczekiwany błąd API: %s", e)
            _log_api(model_name, False, str(e))
            return None

        finally:
            # Zawsze zamknij połączenie jeśli jeszcze otwarte
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
                resp = None

    return None


def _log_api(model_name: str, success: bool, error: str = None):
    """Pomocnik — loguje wywołanie API bez powtarzania kodu."""
    try:
        logger = get_logger()
        kwargs = {"model": model_name, "success": success}
        if error:
            kwargs["error"] = error
        logger.log_api_call("deepseek", **kwargs)
    except Exception:
        pass
