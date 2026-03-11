"""
responders/smierc.py
Pośmiertny autoresponder Pawła.

Tryby:
  ETAP 1-6   — narracja pozagrobowa + obrazek PNG + filmik MP4
  ETAP 7     — reinkarnacja + obrazek PNG + filmik MP4
  ETAP 8-19  — Paweł jako robotnik niebieski (remont) + obrazek PNG
  ETAP 20-49 — Paweł jako robotnik niebieski (remont, etapy kosmiczne) + obrazek PNG
  ETAP 50    — finałowy etap Pawła (ostatnie szlify) + obrazek PNG
  ETAP 51+   — WYSŁANNIK: odpowiedź DeepSeek po polsku
               + obrazek FLUX z promptem wygenerowanym przez Groq
               + załącznik _.txt z pełnym promptem wysłanym do FLUX

Pliki promptów w katalogu prompts/:
  requiem_PAWEL_system_1-6.txt            — system prompt Pawła (etapy 1-6)
  requiem_PAWEL_system_7.txt              — system prompt Pawła (etap 7, reinkarnacja)
  requiem_PAWEL_system_8-19.txt           — system prompt Pawła (etapy 8-19, remont nieba)
  requiem_PAWEL_system_20-50.txt          — system prompt Pawła (etapy 20-50, remont kosmiczny)
  requiem_WYSLANNIK_system_8_.txt         — system prompt Wysłannika (etap 51+) → DeepSeek
  requiem_WYSLANNIK_flux_groq_system.txt  — system prompt dla Groq → generuje prompt FLUX
  requiem_WYSLANNIK_IMAGE_STYLE.txt       — styl obrazka FLUX (fallback)
  flux_forbidden.txt                      — słowa zabronione dla FLUX (do mutacji)
  flux_mutations.txt                      — sufiksy losowane przy mutacji

Podział API:
  DeepSeek → tekst emaila Wysłannika (call_deepseek / MODEL_TYLER)
  Groq     → kreatywny prompt FLUX    (call_groq)
  Fallback → jeśli jeden zawodzi, używa drugiego
"""

import os
import re
import random
import base64
import requests
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
MEDIA_DIR   = os.path.join(BASE_DIR, "media")
ETAPY_FILE  = os.path.join(PROMPTS_DIR, "pozagrobowe.txt")

# ── Ścieżki plików promptów ───────────────────────────────────────────────────
FILE_PAWEL_SYSTEM_1_6          = os.path.join(PROMPTS_DIR, "requiem_PAWEL_system_1-6.txt")
FILE_PAWEL_SYSTEM_7            = os.path.join(PROMPTS_DIR, "requiem_PAWEL_system_7.txt")
FILE_PAWEL_SYSTEM_8_19         = os.path.join(PROMPTS_DIR, "requiem_PAWEL_system_8-19.txt")
FILE_PAWEL_SYSTEM_20_50        = os.path.join(PROMPTS_DIR, "requiem_PAWEL_system_20-50.txt")
FILE_WYSLANNIK_SYSTEM_8_       = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_system_8_.txt")
FILE_WYSLANNIK_FLUX_GROQ_SYS   = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_flux_groq_system.txt")
FILE_WYSLANNIK_IMAGE_STYLE     = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_IMAGE_STYLE.txt")
FILE_FLUX_FORBIDDEN            = os.path.join(PROMPTS_DIR, "flux_forbidden.txt")
FILE_FLUX_MUTATIONS            = os.path.join(PROMPTS_DIR, "flux_mutations.txt")

# ── Stałe FLUX ────────────────────────────────────────────────────────────────
HF_API_URL  = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS    = 5
HF_GUIDANCE = 5
TIMEOUT_SEC = 55

# ── Groq modele ───────────────────────────────────────────────────────────────
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


# ── Wczytaj plik tekstowy ─────────────────────────────────────────────────────
def _load_txt(path: str, fallback: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        current_app.logger.warning("Błąd wczytywania pliku %s: %s", path, e)
        return fallback


# ── Wywołaj Groq ──────────────────────────────────────────────────────────────
def _call_groq(system: str, user: str) -> str | None:
    """
    Wywołuje Groq API. Klucz: API_KEY_GROQ w zmiennych środowiskowych.
    Zwraca tekst odpowiedzi lub None przy błędzie.
    """
    api_key = os.getenv("API_KEY_GROQ", "").strip()
    if not api_key:
        current_app.logger.warning("[groq] Brak API_KEY_GROQ w zmiennych środowiskowych")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens":  300,
        "temperature": 0.95,
    }
    try:
        resp = requests.post(GROQ_API_URL, headers=headers,
                             json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"].strip()
            current_app.logger.info("[groq] OK: %.150s", result)
            return result
        else:
            current_app.logger.warning("[groq] HTTP %s: %s",
                                       resp.status_code, resp.text[:150])
            return None
    except Exception as e:
        current_app.logger.warning("[groq] Wyjątek: %s", str(e)[:100])
        return None


# ── Fallback: DeepSeek jako generator promptu FLUX ────────────────────────────
def _call_deepseek_flux_fallback(system: str, user: str) -> str | None:
    """
    Używa DeepSeek jako fallback gdy Groq nie działa.
    """
    try:
        result = call_deepseek(system, user, MODEL_TYLER)
        if result:
            current_app.logger.info("[deepseek-flux-fallback] OK: %.150s", result)
        return result or None
    except Exception as e:
        current_app.logger.warning("[deepseek-flux-fallback] Wyjątek: %s", str(e)[:100])
        return None


# ── Wczytaj listę słów z pliku ────────────────────────────────────────────────
def _load_word_list(path: str) -> list:
    """
    Wczytuje plik z listą słów — jedna linia = jedno słowo.
    Linie zaczynające się od # są ignorowane.
    """
    words = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.append(line.lower())
    except Exception as e:
        current_app.logger.warning("Błąd wczytywania listy słów %s: %s", path, e)
    return words


# ── Mutuj zabronione słowa w prompcie FLUX ────────────────────────────────────
def _mutate_flux_prompt(prompt: str) -> tuple:
    """
    Skanuje prompt FLUX i zamienia każde zabronione słowo na:
        słowo-of-randomowy-sufiks
    Działa też wewnątrz złożeń (apple-tree → apple-tree-of-frozen-frequencies).
    Aktywna tylko od etapu 8+, wywoływana z _generate_flux_prompt.
    Zwraca (zmutowany_prompt, lista_zmian).
    lista_zmian = lista stringów "słowo → słowo-sufiks"
    """
    forbidden = _load_word_list(FILE_FLUX_FORBIDDEN)
    suffixes  = _load_word_list(FILE_FLUX_MUTATIONS)

    if not forbidden or not suffixes:
        current_app.logger.warning(
            "[mutate] Brak flux_forbidden.txt lub flux_mutations.txt — pomijam mutację"
        )
        return prompt, []

    result  = prompt
    changes = []

    for word in forbidden:
        # Szukaj słowa jako samodzielnej jednostki (nie poprzedzonej ani zakończonej literą)
        pattern = re.compile(
            rf'(?<![a-zA-Z]){re.escape(word)}(?![a-zA-Z])',
            re.IGNORECASE
        )
        if pattern.search(result):
            sufiks = random.choice(suffixes)
            result = pattern.sub(
                lambda m, s=sufiks: m.group(0) + "-" + s,
                result
            )
            changes.append(f"{word} → {word}-{sufiks}")
            current_app.logger.info("[mutate] %s → %s-%s", word, word, sufiks)

    current_app.logger.info("[mutate] Łącznie zmutowano słów: %d", len(changes))
    return result, changes


# ── Generuj kreatywny prompt FLUX przez Groq (+ fallback DeepSeek) ─────────────
def _generate_flux_prompt(wyslannik_text: str) -> tuple:
    """
    Groq dostaje tekst odpowiedzi Wysłannika i generuje kreatywny prompt FLUX.
    Fallback: DeepSeek z tym samym systemem.
    Fallback 2: statyczny styl z pliku IMAGE_STYLE.
    Po wygenerowaniu prompt przechodzi przez _mutate_flux_prompt.
    Zwraca (prompt_po_mutacji, lista_zmian, provider_name).
    """
    system = _load_txt(
        FILE_WYSLANNIK_FLUX_GROQ_SYS,
        fallback=(
            "You are a creative prompt engineer for FLUX image generator. "
            "Based on the Polish heavenly messenger text, write a surreal, "
            "otherworldly image prompt in English (max 80 words). "
            "Invent bizarre celestial creatures inspired by the content. "
            "NOT photorealistic, NOT earthly. "
            "End with: divine surreal digital art, otherworldly paradise, vivid colors. "
            "Return ONLY the prompt."
        )
    )
    user = f"Generate a FLUX image prompt based on this heavenly messenger text:\n\n{wyslannik_text}"

    # Próba 1: Groq
    result = _call_groq(system, user)
    if result:
        current_app.logger.info("[flux-prompt] Wygenerowano przez Groq")
        mutated, changes = _mutate_flux_prompt(result)
        return mutated, changes, "Groq"

    # Próba 2: DeepSeek fallback
    current_app.logger.warning("[flux-prompt] Groq zawiódł — próbuję DeepSeek")
    result = _call_deepseek_flux_fallback(system, user)
    if result:
        current_app.logger.info("[flux-prompt] Wygenerowano przez DeepSeek (fallback)")
        mutated, changes = _mutate_flux_prompt(result)
        return mutated, changes, "DeepSeek (fallback po Groq)"

    # Próba 3: statyczny fallback z pliku
    current_app.logger.warning("[flux-prompt] Oba API zawiodły — używam statycznego stylu")
    image_style = _load_txt(
        FILE_WYSLANNIK_IMAGE_STYLE,
        fallback="surreal heavenly paradise, divine golden light, celestial beings, "
                 "otherworldly atmosphere, vivid colors, digital art"
    )
    return image_style, [], "statyczny fallback (oba API zawiodły)"


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


# ── Generuj obrazek FLUX ──────────────────────────────────────────────────────
def _generate_flux_image(prompt: str):
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[wyslannik] Brak tokenów HF!")
        return None

    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
        },
    }
    current_app.logger.info("[wyslannik] FLUX prompt: %s", prompt[:200])

    for name, token in tokens:
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            resp = requests.post(HF_API_URL, headers=headers,
                                 json=payload, timeout=TIMEOUT_SEC)
            if resp.status_code == 200:
                current_app.logger.info(
                    "[wyslannik] FLUX sukces token=%s PNG %d B", name, len(resp.content))
                return {
                    "base64":       base64.b64encode(resp.content).decode("ascii"),
                    "content_type": "image/png",
                    "filename":     "niebo_wyslannik.png",
                }
            elif resp.status_code in (401, 403):
                current_app.logger.warning("[wyslannik] token %s nieważny", name)
            elif resp.status_code in (503, 529):
                current_app.logger.warning("[wyslannik] token %s przeciążony", name)
            else:
                current_app.logger.warning("[wyslannik] token %s błąd %s: %s",
                                           name, resp.status_code, resp.text[:100])
        except requests.exceptions.Timeout:
            current_app.logger.warning("[wyslannik] token %s timeout", name)
        except Exception as e:
            current_app.logger.warning("[wyslannik] token %s wyjątek: %s",
                                       name, str(e)[:50])

    current_app.logger.error("[wyslannik] Wszystkie tokeny HF zawiodły!")
    return None


# ── Zbuduj załącznik _.txt ────────────────────────────────────────────────────
def _build_debug_txt(wyslannik_text: str, flux_prompt: str,
                     flux_provider: str, etap: int,
                     mutation_changes: list = None) -> dict:
    changes_str = "\n".join(mutation_changes) if mutation_changes else "(brak mutacji)"
    content = (
        f"Etap: {etap}\n\n"
        f"{flux_prompt}\n\n\n"
        f"=== REQUIEM RESPONDER — DEBUG FLUX ===\n\n"
        f"--- Odpowiedź Wysłannika (źródło promptu FLUX) ---\n"
        f"{wyslannik_text}\n\n"
        f"--- Provider który wygenerował prompt FLUX ---\n"
        f"{flux_provider}\n\n"
        f"--- Zmutowane słowa ---\n"
        f"{changes_str}\n\n"
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


# ── Formatuj historię ─────────────────────────────────────────────────────────
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
    max_etap = max(etapy.keys()) if etapy else 50

    # ── WYSŁANNIK (etap po max_etap, czyli 51+) ───────────────────────────────
    if etap > max_etap:
        historia_txt = _format_historia(historia)

        # 1. Tekst emaila — DeepSeek (fallback: Groq)
        system_wyslannik = _load_txt(
            FILE_WYSLANNIK_SYSTEM_8_,
            fallback=(
                "Jesteś wysłannikiem z wyższych sfer duchowych piszącym po polsku. "
                "Przebijasz każdą rzecz wymienioną przez nadawcę — tylko przymiotnikami, "
                "nigdy liczbami. Ton: dostojny, poetycki, absurdalny. Max 4 zdania. "
                "Podpisz się: — Wysłannik z wyższych sfer"
            )
        )
        user_msg    = f"Osoba pyta: {body}\n\nHistoria:\n{historia_txt}"
        wynik_tekst = call_deepseek(system_wyslannik, user_msg, MODEL_TYLER)

        # Fallback na Groq jeśli DeepSeek zawiódł
        if not wynik_tekst:
            current_app.logger.warning("[wyslannik] DeepSeek zawiódł — próbuję Groq")
            wynik_tekst = _call_groq(system_wyslannik, user_msg)

        reply_html = (
            f"<p>{wynik_tekst}</p><p><i>— Wysłannik z wyższych sfer</i></p>"
            if wynik_tekst
            else "<p>Pawła nie ma — reinkarnował się. Jesteśmy tu do dyspozycji."
                 "<br><i>— Wysłannik z wyższych sfer</i></p>"
        )

        # 2. Prompt FLUX — Groq (fallback: DeepSeek, potem statyczny) + mutacja
        flux_prompt, flux_changes, flux_provider = _generate_flux_prompt(wynik_tekst or body)

        # 3. Generuj obrazek
        image     = _generate_flux_image(flux_prompt)
        debug_txt = _build_debug_txt(
            wynik_tekst or "", flux_prompt, flux_provider, etap, flux_changes
        )

        current_app.logger.info(
            "[wyslannik] etap=%d | flux_prompt=%.100s | image=%s",
            etap, flux_prompt, bool(image)
        )
        return {
            "reply_html": reply_html,
            "nowy_etap":  etap,
            "image":      image,
            "mp4":        None,
            "debug_txt":  debug_txt,
        }

    # ── ETAP 1-50 ─────────────────────────────────────────────────────────────
    if etap < max_etap:
        etap_tresc   = etapy.get(etap, "Podróż trwa")
        historia_txt = _format_historia(historia)

        # Wybierz właściwy plik promptu zależnie od etapu
        if etap <= 7:
            prompt_file = FILE_PAWEL_SYSTEM_1_6
            fallback_sys = (
                "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
                "Piszesz po polsku. Ton: spokojny, lekko absurdalny, z humorem. "
                "Odpowiedź maksymalnie 5 zdań. Podpisz się: '— Autoresponder Pawła-zza-światów'. "
                "Koniecznie wspomnij że umarłeś na suchoty dnia {data_smierci_str}. "
                "Opisz swój aktualny etap. Nie wspominaj Księgi Urantii."
            )
        elif etap <= 19:
            prompt_file = FILE_PAWEL_SYSTEM_8_19
            fallback_sys = (
                "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
                "Piszesz po polsku. Ton: spokojny, absurdalny, z humorem robotniczym. "
                "Odpowiedź maksymalnie 5 zdań. Podpisz się: '— Autoresponder Pawła-zza-światów'. "
                "Koniecznie wspomnij że umarłeś na suchoty dnia {data_smierci_str}. "
                "Nawiąż do wiadomości tej osoby paradoksalnie chwaląc, że na Ziemi jest lepiej niż w niebie. "
                "Opisz swój aktualny etap rozwijając podany punkt - używaj konkretnych szczegółów roboczych. "
                "Nie wspominaj Księgi Urantii."
            )
        else:
            prompt_file = FILE_PAWEL_SYSTEM_20_50
            fallback_sys = (
                "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
                "Piszesz po polsku. Ton: spokojny, absurdalny, z humorem robotniczym. "
                "Odpowiedź maksymalnie 5 zdań. Podpisz się: '— Autoresponder Pawła-zza-światów'. "
                "Koniecznie wspomnij że umarłeś na suchoty dnia {data_smierci_str}. "
                "Nawiąż do wiadomości tej osoby paradoksalnie chwaląc, że na Ziemi jest lepiej niż w niebie. "
                "Opisz swój aktualny etap rozwijając podany punkt - używaj konkretnych szczegółów roboczych. "
                "Nie wspominaj Księgi Urantii."
            )

        system_tmpl = _load_txt(prompt_file, fallback=fallback_sys)
        system     = system_tmpl.replace("{data_smierci_str}", data_smierci_str)
        user_msg   = f"Etap w zaświatach: {etap_tresc}\nWiadomość: {body}\nHistoria:\n{historia_txt}"
        wynik      = call_deepseek(system, user_msg, MODEL_TYLER)
        reply_html = (
            f"<p>{wynik}</p>" if wynik
            else "<p>To autoresponder. Chwilowo brak zasięgu w tej strefie kosmicznej.</p>"
        )

        # Obrazek: statyczny PNG (etapy 1-7) lub FLUX generowany (etapy 8+)
        static_image = _get_etap_image(etap)
        if static_image:
            image     = static_image
            debug_txt = None
        elif etap >= 8:
            current_app.logger.info("[pawel-flux] etap=%d START generowania FLUX", etap)
            flux_prompt, flux_changes, flux_provider = _generate_flux_prompt(wynik or etap_tresc)
            current_app.logger.info("[pawel-flux] prompt=%.120s provider=%s", flux_prompt, flux_provider)
            image     = _generate_flux_image(flux_prompt)
            debug_txt = _build_debug_txt(
                wynik or "", flux_prompt, flux_provider, etap, flux_changes
            )
            current_app.logger.info("[pawel-flux] etap=%d image=%s debug_txt=%s",
                                    etap, bool(image), bool(debug_txt))
        else:
            image     = None
            debug_txt = None

        return {
            "reply_html": reply_html,
            "nowy_etap":  etap + 1,
            "image":      image,
            "mp4":        _get_etap_mp4(etap),
            "debug_txt":  debug_txt,
        }

    # ── ETAP OSTATNI (max_etap = 50) — finałowe szlify przed końcem ─────────────
    etap_tresc   = etapy.get(max_etap, "Ostatnie szlify przed końcem świata")
    historia_txt = _format_historia(historia)
    system_tmpl  = _load_txt(
        FILE_PAWEL_SYSTEM_20_50,
        fallback=(
            "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
            "Piszesz po polsku. Ton: spokojny, absurdalny, z humorem robotniczym. "
            "Odpowiedź maksymalnie 5 zdań. Podpisz się: '— Autoresponder Pawła-zza-światów'. "
            "Umarłem na suchoty dnia {data_smierci_str}. "
            "Nawiąż do wiadomości tej osoby paradoksalnie chwaląc, że na Ziemi jest lepiej niż w niebie. "
            "Opisz finałowy etap remontu nieba — ostatnie szlify przed końcem świata. "
            "Nie wspominaj Księgi Urantii."
        )
    )
    system     = system_tmpl.replace("{data_smierci_str}", data_smierci_str)
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
        "mp4":        None,
        "debug_txt":  None,
    }
