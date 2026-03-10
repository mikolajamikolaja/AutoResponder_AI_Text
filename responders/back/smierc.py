"""
responders/smierc.py
Pośmiertny autoresponder Pawła.

Tryby:
  ETAP 1-6  — narracja pozagrobowa + obrazek PNG + filmik MP4
  ETAP 7    — reinkarnacja + obrazek PNG
  ETAP 8+   — WYSŁANNIK: odpowiedź w stylu Księgi Urantii
              + obrazek FLUX z rzeczownikami z wiadomości
              + załącznik _.txt z pełnym promptem wysłanym do FLUX
"""

import os
import re
import base64
import requests
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
MEDIA_DIR   = os.path.join(BASE_DIR, "media")
ETAPY_FILE  = os.path.join(PROMPTS_DIR, "pozagrobowe.txt")

# ── Stałe FLUX ────────────────────────────────────────────────────────────────
HF_API_URL  = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS    = 5
HF_GUIDANCE = 5
TIMEOUT_SEC = 55

WYSLANNIK_IMAGE_STYLE = (
    "heavenly paradise scene, bright golden light, clouds, magical atmosphere, "
    "colorful, joyful, vibrant colors, digital art style, beautiful and uplifting"
)


# ── Wczytaj etapy z pliku ─────────────────────────────────────────────────────
def _load_etapy() -> dict:
    etapy = {}
    try:
        with open(ETAPY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'^(\d+)\.\s+(.+)$', line)
                if m:
                    etapy[int(m.group(1))] = m.group(2).strip()
    except Exception as e:
        current_app.logger.warning("Błąd wczytywania etapów: %s", e)
    return etapy


# ── Wczytaj plik jako base64 ──────────────────────────────────────────────────
def _file_to_base64(path: str):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None


# ── Pobierz obrazek PNG dla etapu ─────────────────────────────────────────────
def _get_etap_image(etap: int):
    path = os.path.join(MEDIA_DIR, "images", "niebo", f"{etap}.png")
    b64  = _file_to_base64(path)
    if b64:
        current_app.logger.info("Obrazek etapu %d OK", etap)
        return {"base64": b64, "content_type": "image/png", "filename": f"niebo_{etap}.png"}
    current_app.logger.warning("Brak obrazka etapu %d: %s", etap, path)
    return None


# ── Pobierz MP4 dla etapu ─────────────────────────────────────────────────────
def _get_etap_mp4(etap: int):
    path = os.path.join(MEDIA_DIR, "mp4", "niebo", f"{etap}.mp4")
    b64  = _file_to_base64(path)
    if b64:
        current_app.logger.info("MP4 etapu %d OK", etap)
        return {"base64": b64, "content_type": "video/mp4", "filename": f"niebo_{etap}.mp4"}
    return None


# ── Zbierz tokeny HF ──────────────────────────────────────────────────────────
def _get_hf_tokens() -> list:
    names = [
        "HF_TOKEN",   "HF_TOKEN1",  "HF_TOKEN2",  "HF_TOKEN3",  "HF_TOKEN4",
        "HF_TOKEN5",  "HF_TOKEN6",  "HF_TOKEN7",  "HF_TOKEN8",  "HF_TOKEN9",
        "HF_TOKEN10", "HF_TOKEN11", "HF_TOKEN12", "HF_TOKEN13", "HF_TOKEN14",
        "HF_TOKEN15", "HF_TOKEN16", "HF_TOKEN17", "HF_TOKEN18", "HF_TOKEN19",
        "HF_TOKEN20",
    ]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


# ── Generuj obrazek FLUX — zwraca dict|None ─────────────────────────────────
def _generate_flux_image(prompt: str):
    """
    Wysyła pełny prompt do HF FLUX.1-schnell.
    Dla każdego tokenu próbuje po kolei aż któryś zadziała.
    Zwraca dict z base64 PNG lub None przy błędzie.
    """
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[wyslannik] Brak HF_TOKEN w zmiennych środowiskowych!")
        return None

    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
        },
    }

    current_app.logger.info(
        "[wyslannik] FLUX — tokeny dostępne: %s | prompt: %.150s",
        [n for n, _ in tokens], prompt,
    )

    for name, token in tokens:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept":        "image/png",
        }
        try:
            resp = requests.post(
                HF_API_URL, headers=headers, json=payload, timeout=TIMEOUT_SEC
            )
            if resp.status_code == 200:
                current_app.logger.info(
                    "[wyslannik] ✓ Sukces! Token=%s | PNG %d B",
                    name, len(resp.content)
                )
                return {
                    "base64":       base64.b64encode(resp.content).decode("ascii"),
                    "content_type": "image/png",
                    "filename":     "niebo_wyslannik.png",
                }
            elif resp.status_code in (401, 403):
                current_app.logger.warning(
                    "[wyslannik] Token %s nieważny — następny token",
                    name
                )
            elif resp.status_code in (503, 529):
                current_app.logger.warning(
                    "[wyslannik] Token %s przeciążony — następny token",
                    name
                )
            else:
                current_app.logger.warning(
                    "[wyslannik] Token %s błąd %s — następny token",
                    name, resp.status_code
                )
        except requests.exceptions.Timeout:
            current_app.logger.warning(
                "[wyslannik] Token %s timeout — następny token",
                name
            )
        except Exception as e:
            current_app.logger.warning(
                "[wyslannik] Token %s błąd: %s — następny token",
                name, str(e)[:50]
            )

    current_app.logger.error("[wyslannik] Wszystkie tokeny zawiodły!")
    return None


# ── Wyciągnij rzeczowniki z wiadomości ───────────────────────────────────────
def _extract_nouns(body: str) -> list:
    """
    Prosi DeepSeek o wypisanie WSZYSTKICH rzeczowników z wiadomości —
    nie tylko materialnych, żeby nie gubić słów jak 'koza', 'koń' itp.
    """
    system = (
        "Wypisz wszystkie rzeczowniki z podanej wiadomości. "
        "Odpowiedz TYLKO rzeczownikami oddzielonymi przecinkami, po polsku. "
        "Nie dodawaj żadnych innych słów ani wyjaśnień. "
        "Jeśli nie ma żadnych rzeczowników, odpowiedz: BRAK"
    )
    wynik = call_deepseek(system, body[:500], MODEL_TYLER)
    current_app.logger.info("[wyslannik] DeepSeek rzeczowniki raw: %s", wynik)

    if not wynik or "BRAK" in wynik.upper():
        return []

    # Wyczyść odpowiedź — usuń ewentualne zdania, zostaw tylko słowa
    nouns = [n.strip().lower() for n in wynik.split(",") if n.strip()]
    # Odfiltruj zbyt długie frazy (DeepSeek czasem dodaje zdania)
    nouns = [n for n in nouns if len(n.split()) <= 3]
    current_app.logger.info("[wyslannik] Rzeczowniki po filtracji: %s", nouns)
    return nouns[:7]


# ── Przetłumacz rzeczowniki na angielski ─────────────────────────────────────
def _translate_nouns(nouns: list) -> str:
    if not nouns:
        return ""
    system = (
        "Translate these Polish words to English. "
        "Return ONLY the English words separated by commas, nothing else:"
    )
    translated = call_deepseek(system, ", ".join(nouns), MODEL_TYLER)
    current_app.logger.info("[wyslannik] Tłumaczenie raw: %s", translated)

    if not translated:
        return ", ".join(nouns)

    # Wyczyść — tylko słowa i przecinki
    translated = re.sub(r'[^a-zA-Z,\s]', '', translated).strip().lower()
    current_app.logger.info("[wyslannik] Tłumaczenie czyste: %s", translated)
    return translated


# ── Zbuduj prompt FLUX dla wysłannika ────────────────────────────────────────
def _build_wyslannik_flux_prompt(nouns: list) -> str:
    if not nouns:
        return f"paradise heaven scene, golden light, clouds, angels, {WYSLANNIK_IMAGE_STYLE}"

    translated = _translate_nouns(nouns)
    if not translated:
        translated = ", ".join(nouns)

    prompt = (
        f"heavenly paradise scene with flying colorful {translated}, "
        f"magical floating {translated} in paradise clouds, "
        f"golden divine light, joyful cheerful atmosphere, "
        f"{WYSLANNIK_IMAGE_STYLE}"
    )
    current_app.logger.info("[wyslannik] FLUX prompt zbudowany: %s", prompt)
    return prompt


# ── Zbuduj załącznik _.txt z debugiem promptu ────────────────────────────────
def _build_debug_txt(nouns: list, translated: str, flux_prompt: str, etap: int) -> dict:
    """
    Buduje plik _.txt z pełnymi danymi wysłanymi do FLUX.
    Załączany do każdej odpowiedzi wysłannika.
    """
    content = (
        f"=== REQUIEM RESPONDER — DEBUG FLUX ===\n"
        f"Etap: {etap}\n\n"
        f"--- Rzeczowniki wyciągnięte z wiadomości ---\n"
        f"{', '.join(nouns) if nouns else '(brak)'}\n\n"
        f"--- Tłumaczenie na angielski ---\n"
        f"{translated if translated else '(brak)'}\n\n"
        f"--- Pełny prompt wysłany do FLUX ---\n"
        f"{flux_prompt}\n\n"
        f"--- Parametry FLUX ---\n"
        f"Model: FLUX.1-schnell\n"
        f"num_inference_steps: {HF_STEPS}\n"
        f"guidance_scale: {HF_GUIDANCE}\n"
        f"API URL: {HF_API_URL}\n"
    )
    return {
        "base64":       base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "content_type": "text/plain",
        "filename":     "_.txt",
    }


# ── Formatuj historię dla DeepSeeka ──────────────────────────────────────────
def _format_historia(historia: list) -> str:
    if not historia:
        return "(brak poprzednich wiadomości)"
    lines = []
    for h in historia[-3:]:
        lines.append(f"Osoba: {h.get('od', '')[:300]}")
        lines.append(f"Paweł: {h.get('odpowiedz', '')[:300]}")
    return "\n".join(lines)


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_smierc_section(
    sender_email:      str,
    body:              str,
    etap:              int,
    data_smierci_str:  str,
    historia:          list,
) -> dict:
    """
    Zwraca:
      {
        "reply_html": str,
        "nowy_etap":  int,
        "image":      { base64, content_type, filename } | None,
        "mp4":        { base64, content_type, filename } | None,
        "debug_txt":  { base64, content_type, filename } | None,
      }
    """
    etapy    = _load_etapy()
    max_etap = max(etapy.keys()) if etapy else 7

    # ── WYSŁANNIK (etap 8+) ───────────────────────────────────────────────────
    if etap > max_etap:
        historia_txt = _format_historia(historia)
        system = (
            "Jesteś wysłannikiem z wyższych sfer duchowych. "
            "Odpowiadasz z głęboką mądrością kosmiczną. "
            "Nigdy nie ujawniasz źródła swojej wiedzy. "
            "Piszesz po polsku, spokojnie, z dostojeństwem i ciepłem. "
            "Reklamujesz niebo jako miejsce niesamowitej radości i wolności. "
            "Odpowiedź maksymalnie 4 zdania."
        )
        user_msg    = f"Osoba pyta: {body}\n\nHistoria:\n{historia_txt}"
        wynik_tekst = call_deepseek(system, user_msg, MODEL_TYLER)

        reply_html = (
            f"<p>{wynik_tekst}</p><p><i>— Wysłannik z wyższych sfer</i></p>"
            if wynik_tekst
            else "<p>Pawła nie ma — reinkarnował się. Jesteśmy tu do dyspozycji."
                 "<br><i>— Wysłannik z wyższych sfer</i></p>"
        )

        # Wyciągnij rzeczowniki → przetłumacz → zbuduj prompt → generuj
        nouns       = _extract_nouns(body)
        translated  = _translate_nouns(nouns) if nouns else ""
        flux_prompt = _build_wyslannik_flux_prompt(nouns)
        image       = _generate_flux_image(flux_prompt)
        debug_txt   = _build_debug_txt(nouns, translated, flux_prompt, etap)

        current_app.logger.info(
            "[wyslannik] etap=%d | rzeczowniki=%s | image=%s",
            etap, nouns, bool(image)
        )
        return {
            "reply_html": reply_html,
            "nowy_etap":  etap,
            "image":      image,
            "mp4":        None,
            "debug_txt":  debug_txt,
        }

    # ── ETAP 1-6 ──────────────────────────────────────────────────────────────
    if etap < max_etap:
        etap_tresc   = etapy.get(etap, "Podróż trwa")
        historia_txt = _format_historia(historia)
        system = (
            "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
            "Piszesz po polsku. Ton: spokojny, lekko absurdalny, z humorem. "
            "Odpowiedź maksymalnie 5 zdań. Na końcu podpisz się: '— Autoresponder Pawła-zza-światów' "
            f"Koniecznie wspomnij że umarłeś na suchoty dnia {data_smierci_str}. "
            "Nawiąż do wiadomości tej osoby paradoksalnie chwaląc, to że na Ziemi jest lepiej niż w niebie pomimo, że ta osoba będzie narzekać  "
            "Opisz swój aktualny etap rozwijając podany punkt. "
            "Nie wspominaj Księgi Urantii."
        )
        user_msg   = f"Etap w zaświatach: {etap_tresc}\nWiadomość: {body}\nHistoria:\n{historia_txt}"
        wynik      = call_deepseek(system, user_msg, MODEL_TYLER)
        reply_html = (
            f"<p>{wynik}</p>" if wynik
            else "<p>To autoresponder. Chwilowo brak zasięgu w tej strefie kosmicznej.</p>"
        )
        return {
            "reply_html": reply_html,
            "nowy_etap":  etap + 1,
            "image":      _get_etap_image(etap),
            "mp4":        _get_etap_mp4(etap),
            "debug_txt":  None,
        }

    # ── ETAP 7 — reinkarnacja ─────────────────────────────────────────────────
    etap_tresc   = etapy.get(max_etap, "Reinkarnacja nadchodzi nieuchronnie")
    historia_txt = _format_historia(historia)
    system = (
        "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
        "Piszesz po polsku. Ton: spokojny, wzruszający, tajemniczy. "
        "Odpowiedź maksymalnie 5 zdań. "
        f"Umarłem na suchoty dnia {data_smierci_str}. "
        "Poinformuj że właśnie nadchodzi moment reinkarnacji. "
        "Nie możesz powiedzieć gdzie ani kim się urodzisz. "
        "Pożegnaj się ciepło. Nie wspominaj Księgi Urantii."
    )
    user_msg   = f"Etap: {etap_tresc}\nWiadomość: {body}\nHistoria:\n{historia_txt}"
    wynik      = call_deepseek(system, user_msg, MODEL_TYLER)
    reply_html = (
        f"<p>{wynik}</p>" if wynik
        else "<p>Nadszedł czas. Reinkarnuję się. Do zobaczenia po drugiej stronie.</p>"
    )
    return {
        "reply_html": reply_html,
        "nowy_etap":  etap + 1,
        "image":      _get_etap_image(max_etap),
        "mp4":        _get_etap_mp4(max_etap),
        "debug_txt":  None,
    }
