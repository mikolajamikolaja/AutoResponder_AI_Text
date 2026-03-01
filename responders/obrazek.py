"""
responders/obrazek.py
Responder OBRAZEK — generuje obrazek AI z treści maila.

Używa Pollinations.AI (darmowe, bez klucza API, zero RAM na Render).
Zapytanie HTTP → URL obrazka → pobranie PNG → base64 → odpowiedź.

Dodanie do app.py:
    from responders.obrazek import build_obrazek_section
    if data.get("wants_obrazek"):
        response_data["obrazek"] = build_obrazek_section(body)
"""
import io
import re
import base64
import urllib.request
import urllib.parse
from flask import current_app

from core.ai_client import call_groq, MODEL_TYLER

# ── Stałe ────────────────────────────────────────────────────────────────────
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
IMG_WIDTH   = 512
IMG_HEIGHT  = 512
TIMEOUT_SEC = 25          # Pollinations bywa wolne — daj mu czas
MAX_PROMPT  = 300         # limit znaków promptu


# ── Pomocnicze ────────────────────────────────────────────────────────────────
def _shorten_to_prompt(body: str) -> str:
    """
    Zamienia treść maila na krótki prompt po angielsku (lepsze wyniki).
    Używa modelu Groq jeśli dostępny, fallback = pierwsze zdanie maila.
    """
    try:
        res = call_groq(
            "Zamień poniższy tekst na krótki prompt obrazkowy po angielsku "
            "(max 20 słów, konkretny, wizualny, bez cudzysłowów):\n\n" + body[:500],
            "",
            MODEL_TYLER,
        )
        if res and res.strip():
            # Usuń cudzysłowy i znaki specjalne
            prompt = re.sub(r'["\'\n]', ' ', res.strip())
            return prompt[:MAX_PROMPT]
    except Exception as e:
        current_app.logger.warning("Błąd generowania promptu: %s", e)

    # Fallback: pierwsze zdanie maila
    first = re.split(r'[.!?\n]', body.strip())[0].strip()
    return first[:MAX_PROMPT] if first else "abstract colorful digital art"


def _fetch_image_bytes(prompt: str) -> bytes:
    """
    Pobiera obrazek PNG z Pollinations.AI.
    Zwraca bytes lub b'' przy błędzie.
    """
    encoded = urllib.parse.quote(prompt, safe='')
    url = (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={IMG_WIDTH}&height={IMG_HEIGHT}&nologo=true&model=flux"
    )
    current_app.logger.info("Pollinations URL: %s", url)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status == 200:
                return resp.read()
            current_app.logger.warning("Pollinations status: %s", resp.status)
    except Exception as e:
        current_app.logger.error("Błąd pobierania obrazka: %s", e)

    return b""


# ── Główna funkcja responderu ──────────────────────────────────────────────────
def build_obrazek_section(body: str) -> dict:
    """
    Buduje sekcję 'obrazek':
    - tłumaczy treść maila na prompt wizualny
    - pobiera wygenerowany obrazek PNG z Pollinations.AI
    - zwraca base64 PNG + HTML z podsumowaniem

    Zwracany dict:
    {
        "reply_html": str,
        "image": {
            "base64":       str | None,
            "content_type": "image/png",
            "filename":     "obrazek_ai.png",
        },
        "prompt_used": str,
    }
    """
    if not body or not body.strip():
        return {
            "reply_html": "<p>Brak treści do wygenerowania obrazka.</p>",
            "image": {"base64": None, "content_type": "image/png", "filename": "obrazek_ai.png"},
            "prompt_used": "",
        }

    # 1. Utwórz prompt
    prompt = _shorten_to_prompt(body)
    current_app.logger.info("Prompt obrazka: %s", prompt)

    # 2. Pobierz obrazek
    png_bytes = _fetch_image_bytes(prompt)
    png_b64   = base64.b64encode(png_bytes).decode("ascii") if png_bytes else None

    # 3. Zbuduj HTML odpowiedzi
    if png_b64:
        reply_html = (
            f'<p>Wygenerowano obrazek AI na podstawie Twojej wiadomości.</p>'
            f'<p><em>Użyty prompt:</em> {prompt}</p>'
            f'<p><img src="data:image/png;base64,{png_b64}" '
            f'style="max-width:100%;border-radius:8px;" alt="obrazek AI"/></p>'
        )
    else:
        reply_html = (
            "<p>Nie udało się wygenerować obrazka (timeout lub błąd serwisu). "
            "Spróbuj ponownie za chwilę.</p>"
        )

    current_app.logger.info(
        "Obrazek AI: prompt='%s' | sukces=%s | rozmiar=%d B",
        prompt, bool(png_b64), len(png_bytes)
    )

    return {
        "reply_html": reply_html,
        "image": {
            "base64":       png_b64,
            "content_type": "image/png",
            "filename":     "obrazek_ai.png",
        },
        "prompt_used": prompt,
    }
