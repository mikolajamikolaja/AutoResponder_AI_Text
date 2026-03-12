"""
responders/smierc.py
Pośmiertny autoresponder Pawła.

Konfiguracja etapów i system promptów pochodzi z pliku xlsx:
  prompts/requiem_etapy.xlsx
    zakładka 'etapy' : etap, opis, obraz, video, obrazki_ai, system_prompt
    zakładka 'style' : etap, styl, styl_odpowiedzi_tekstowej

Tryby:
  ETAP 1-max_etap  — Paweł pisze z zaświatów wg system_prompt z xlsx
                     + obrazek (statyczny PNG lub FLUX jeśli brak pliku)
                     + filmik MP4 (jeśli jest w xlsx)
  ETAP max_etap+1+ — WYSŁANNIK: odpowiedź DeepSeek po polsku
                     + obrazek FLUX z promptem wygenerowanym przez Groq
                     + załącznik _.txt z pełnym promptem wysłanym do FLUX

Podział API:
  DeepSeek → tekst emaila (call_deepseek / MODEL_TYLER)
  Groq     → kreatywny prompt FLUX
  Fallback → jeśli jeden zawodzi, używa drugiego
"""

import os
import re
import random
import base64
import requests
import pandas as pd
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
MEDIA_DIR   = os.path.join(BASE_DIR, "media")

# ── Plik xlsx z etapami ───────────────────────────────────────────────────────
XLSX_PATH = os.path.join(PROMPTS_DIR, "requiem_etapy.xlsx")

# ── Ścieżki plików promptów (Wysłannik + FLUX) ───────────────────────────────
FILE_WYSLANNIK_SYSTEM       = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_system_8_.txt")
FILE_WYSLANNIK_FLUX_GROQ_SYS = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_flux_groq_system.txt")
FILE_WYSLANNIK_IMAGE_STYLE  = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_IMAGE_STYLE.txt")
FILE_FLUX_FORBIDDEN         = os.path.join(PROMPTS_DIR, "flux_forbidden.txt")
FILE_FLUX_MUTATIONS         = os.path.join(PROMPTS_DIR, "flux_mutations.txt")

# ── Stałe FLUX ────────────────────────────────────────────────────────────────
HF_API_URL  = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS    = 5
HF_GUIDANCE = 5
TIMEOUT_SEC = 55

# ── Groq ──────────────────────────────────────────────────────────────────────
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_SYSTEM_PROMPT = (
    "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
    "Piszesz po polsku. Ton: spokojny, lekko absurdalny, z humorem. "
    "Odpowiedź maksymalnie 5 zdań. Podpisz się: — Autoresponder Pawła-zza-światów. "
    "Wspomnij że umarłeś na suchoty dnia {data_smierci_str}."
)


# ═══════════════════════════════════════════════════════════════════════════════
# ŁADOWANIE XLSX
# ═══════════════════════════════════════════════════════════════════════════════

def _load_config_xlsx() -> tuple:
    """
    Wczytuje obie zakładki z requiem_etapy.xlsx.
    Zwraca (etapy_dict, style_dict) — słowniki indeksowane numerem etapu.
    """
    etapy_data = {}
    style_data = {}

    if not os.path.exists(XLSX_PATH):
        current_app.logger.error("[smierc] Brak pliku xlsx: %s", XLSX_PATH)
        return etapy_data, style_data

    try:
        sheets = pd.read_excel(XLSX_PATH, sheet_name=None, dtype=str)

        df_etapy = sheets.get("etapy")
        if df_etapy is not None:
            df_etapy = df_etapy.where(pd.notna(df_etapy), "")
            for _, row in df_etapy.iterrows():
                try:
                    etapy_data[int(row["etap"])] = row.to_dict()
                except (ValueError, KeyError):
                    continue
        else:
            current_app.logger.warning("[smierc] Brak zakładki 'etapy' w xlsx.")

        df_style = sheets.get("style")
        if df_style is not None:
            df_style = df_style.where(pd.notna(df_style), "")
            for _, row in df_style.iterrows():
                try:
                    style_data[int(row["etap"])] = row.to_dict()
                except (ValueError, KeyError):
                    continue
        else:
            current_app.logger.warning("[smierc] Brak zakładki 'style' w xlsx.")

    except Exception as e:
        current_app.logger.error("[smierc] Błąd czytania xlsx: %s", e)

    return etapy_data, style_data


# ═══════════════════════════════════════════════════════════════════════════════
# NARZĘDZIA POMOCNICZE
# ═══════════════════════════════════════════════════════════════════════════════

def _load_txt(path: str, fallback: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        current_app.logger.warning("Błąd wczytywania pliku %s: %s", path, e)
        return fallback


def _file_to_base64(path: str):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None


def _get_etap_image(etap: int, filename: str = ""):
    """Wczytuje statyczny PNG dla etapu. filename pochodzi z xlsx (np. '1.png')."""
    name = filename or f"{etap}.png"
    path = os.path.join(MEDIA_DIR, "images", "niebo", name)
    b64  = _file_to_base64(path)
    if b64:
        current_app.logger.info("Obrazek etapu %d OK (%s)", etap, name)
        return {"base64": b64, "content_type": "image/png", "filename": name}
    current_app.logger.warning("Brak obrazka etapu %d: %s", etap, path)
    return None


def _get_etap_mp4(etap: int, filename: str = ""):
    """Wczytuje statyczny MP4 dla etapu. filename pochodzi z xlsx (np. '1.mp4')."""
    name = filename or f"{etap}.mp4"
    path = os.path.join(MEDIA_DIR, "mp4", "niebo", name)
    b64  = _file_to_base64(path)
    if b64:
        current_app.logger.info("MP4 etapu %d OK (%s)", etap, name)
        return {"base64": b64, "content_type": "video/mp4", "filename": name}
    return None


def _format_historia(historia: list) -> str:
    if not historia:
        return "(brak poprzednich wiadomości)"
    lines = []
    for h in historia[-3:]:
        lines.append(f"Osoba: {h.get('od', '')[:300]}")
        lines.append(f"Paweł: {h.get('odpowiedz', '')[:300]}")
    return "\n".join(lines)


def _parse_obrazki_ai(val) -> int:
    s = str(val).strip()
    try:
        return int(float(s)) if s not in ("", "nan") else 0
    except (ValueError, TypeError):
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# GROQ / FLUX
# ═══════════════════════════════════════════════════════════════════════════════

def _call_groq(system: str, user: str) -> str | None:
    api_key = os.getenv("API_KEY_GROQ", "").strip()
    if not api_key:
        current_app.logger.warning("[groq] Brak API_KEY_GROQ")
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user",   "content": user}],
        "max_tokens": 300, "temperature": 0.95,
    }
    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"].strip()
            current_app.logger.info("[groq] OK: %.150s", result)
            return result
        current_app.logger.warning("[groq] HTTP %s: %s", resp.status_code, resp.text[:150])
    except Exception as e:
        current_app.logger.warning("[groq] Wyjątek: %s", str(e)[:100])
    return None


def _load_word_list(path: str) -> list:
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


def _mutate_flux_prompt(prompt: str) -> tuple:
    forbidden = _load_word_list(FILE_FLUX_FORBIDDEN)
    suffixes  = _load_word_list(FILE_FLUX_MUTATIONS)
    if not forbidden or not suffixes:
        current_app.logger.warning("[mutate] Brak flux_forbidden.txt lub flux_mutations.txt")
        return prompt, []
    result, changes = prompt, []
    for word in forbidden:
        pattern = re.compile(rf'(?<![a-zA-Z]){re.escape(word)}(?![a-zA-Z])', re.IGNORECASE)
        if pattern.search(result):
            sufiks = random.choice(suffixes)
            result = pattern.sub(lambda m, s=sufiks: m.group(0) + "-" + s, result)
            changes.append(f"{word} → {word}-{sufiks}")
    current_app.logger.info("[mutate] Zmutowano słów: %d", len(changes))
    return result, changes


def _generate_flux_prompt(source_text: str) -> tuple:
    system = _load_txt(
        FILE_WYSLANNIK_FLUX_GROQ_SYS,
        fallback=(
            "You are a creative prompt engineer for FLUX image generator. "
            "Based on the Polish heavenly messenger text, write a surreal, "
            "otherworldly image prompt in English (max 80 words). "
            "NOT photorealistic, NOT earthly. "
            "End with: divine surreal digital art, otherworldly paradise, vivid colors. "
            "Return ONLY the prompt."
        )
    )
    user = f"Generate a FLUX image prompt based on this text:\n\n{source_text}"

    result = _call_groq(system, user)
    if result:
        mutated, changes = _mutate_flux_prompt(result)
        return mutated, changes, "Groq"

    current_app.logger.warning("[flux-prompt] Groq zawiódł — próbuję DeepSeek")
    result = call_deepseek(system, user, MODEL_TYLER)
    if result:
        mutated, changes = _mutate_flux_prompt(result)
        return mutated, changes, "DeepSeek (fallback)"

    current_app.logger.warning("[flux-prompt] Oba API zawiodły — statyczny fallback")
    image_style = _load_txt(
        FILE_WYSLANNIK_IMAGE_STYLE,
        fallback="surreal heavenly paradise, divine golden light, celestial beings, vivid colors, digital art"
    )
    return image_style, [], "statyczny fallback"


def _get_hf_tokens() -> list:
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(21)]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


def _generate_flux_image(prompt: str):
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[flux] Brak tokenów HF!")
        return None
    payload = {"inputs": prompt, "parameters": {"num_inference_steps": HF_STEPS, "guidance_scale": HF_GUIDANCE}}
    current_app.logger.info("[flux] prompt: %s", prompt[:200])
    for name, token in tokens:
        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            resp = requests.post(HF_API_URL, headers=headers, json=payload, timeout=TIMEOUT_SEC)
            if resp.status_code == 200:
                current_app.logger.info("[flux] sukces token=%s PNG %d B", name, len(resp.content))
                return {"base64": base64.b64encode(resp.content).decode("ascii"),
                        "content_type": "image/png", "filename": "niebo_wyslannik.png"}
            elif resp.status_code in (401, 403):
                current_app.logger.warning("[flux] token %s nieważny", name)
            elif resp.status_code in (503, 529):
                current_app.logger.warning("[flux] token %s przeciążony", name)
            else:
                current_app.logger.warning("[flux] token %s błąd %s: %s", name, resp.status_code, resp.text[:100])
        except requests.exceptions.Timeout:
            current_app.logger.warning("[flux] token %s timeout", name)
        except Exception as e:
            current_app.logger.warning("[flux] token %s wyjątek: %s", name, str(e)[:50])
    current_app.logger.error("[flux] Wszystkie tokeny HF zawiodły!")
    return None


def _build_debug_txt(source_text: str, flux_prompt: str,
                     flux_provider: str, etap: int,
                     mutation_changes: list = None) -> dict:
    changes_str = "\n".join(mutation_changes) if mutation_changes else "(brak mutacji)"
    content = (
        f"Etap: {etap}\n\n"
        f"{flux_prompt}\n\n\n"
        f"=== REQUIEM RESPONDER — DEBUG FLUX ===\n\n"
        f"--- Źródło promptu FLUX ---\n{source_text}\n\n"
        f"--- Provider ---\n{flux_provider}\n\n"
        f"--- Zmutowane słowa ---\n{changes_str}\n\n"
        f"--- Parametry FLUX ---\n"
        f"Model: FLUX.1-schnell\n"
        f"num_inference_steps: {HF_STEPS}\n"
        f"guidance_scale: {HF_GUIDANCE}\n"
        f"API URL: {HF_API_URL}\n"
    )
    return {"base64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "content_type": "text/plain", "filename": "_.txt"}


# ═══════════════════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJA
# ═══════════════════════════════════════════════════════════════════════════════

def build_smierc_section(
    sender_email:     str,
    body:             str       = "",
    etap:             int       = 1,
    data_smierci_str: str       = "nieznanego dnia",
    historia:         list      = None,
    data:             dict      = None,
    **kwargs
) -> dict:
    """
    Obsługuje dwa sposoby wywołania:
      A) Z app.py — argumenty wprost:
         build_smierc_section(sender_email=..., body=..., etap=...,
                              data_smierci_str=..., historia=...)
      B) Stary styl — słownik data={}:
         build_smierc_section(sender_email=..., data={etap, data_smierci, historia})
    """
    if historia is None:
        historia = []

    # Stary styl z data={}
    if data is not None:
        etap             = int(data.get("etap",         etap))
        data_smierci_str = data.get("data_smierci",     data_smierci_str)
        historia         = data.get("historia",         historia)
    else:
        etap = int(etap)

    # 1. Załaduj konfigurację z xlsx
    etapy_dict, style_dict = _load_config_xlsx()
    max_etap = max(etapy_dict.keys()) if etapy_dict else 50

    historia_txt = _format_historia(historia)

    # ── WYSŁANNIK (etap > max_etap) ───────────────────────────────────────────
    if etap > max_etap:
        system_wyslannik = _load_txt(
            FILE_WYSLANNIK_SYSTEM,
            fallback=(
                "Jesteś wysłannikiem z wyższych sfer duchowych piszącym po polsku. "
                "Przebijasz każdą rzecz wymienioną przez nadawcę — tylko przymiotnikami, "
                "nigdy liczbami. Ton: dostojny, poetycki, absurdalny. Max 4 zdania. "
                "Podpisz się: — Wysłannik z wyższych sfer"
            )
        )
        user_msg    = f"Osoba pyta: {body}\n\nHistoria:\n{historia_txt}"
        wynik_tekst = call_deepseek(system_wyslannik, user_msg, MODEL_TYLER)
        if not wynik_tekst:
            current_app.logger.warning("[wyslannik] DeepSeek zawiódł — próbuję Groq")
            wynik_tekst = _call_groq(system_wyslannik, user_msg)

        reply_html = (
            f"<p>{wynik_tekst}</p><p><i>— Wysłannik z wyższych sfer</i></p>"
            if wynik_tekst
            else "<p>Pawła nie ma — reinkarnował się. Jesteśmy tu do dyspozycji."
                 "<br><i>— Wysłannik z wyższych sfer</i></p>"
        )
        flux_prompt, flux_changes, flux_provider = _generate_flux_prompt(wynik_tekst or body)
        image     = _generate_flux_image(flux_prompt)
        debug_txt = _build_debug_txt(wynik_tekst or "", flux_prompt, flux_provider, etap, flux_changes)

        current_app.logger.info("[wyslannik] etap=%d image=%s", etap, bool(image))
        return {"reply_html": reply_html, "nowy_etap": etap,
                "image": image, "mp4": None, "debug_txt": debug_txt}

    # ── ETAPY 1-max_etap — Paweł ──────────────────────────────────────────────
    row   = etapy_dict.get(etap, {})
    s_row = style_dict.get(etap, {})

    if not row:
        # Etap nie istnieje w xlsx — tryb awaryjny
        current_app.logger.warning("[smierc] Brak etapu %d w xlsx — tryb awaryjny", etap)
        opis               = "Błądzenie w antymaterii"
        obraz_filename     = ""
        video_filename     = ""
        obrazki_ai         = 1
        system_prompt_tmpl = DEFAULT_SYSTEM_PROMPT
    else:
        opis               = row.get("opis",  "")
        obraz_filename     = row.get("obraz", "")
        video_filename     = row.get("video", "")
        obrazki_ai         = _parse_obrazki_ai(row.get("obrazki_ai", "0"))
        system_prompt_tmpl = row.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    # Podstaw datę śmierci w prompcie
    system = system_prompt_tmpl.replace("{data_smierci_str}", data_smierci_str)
    user_msg = f"Etap w zaświatach: {opis}\nWiadomość: {body}\nHistoria:\n{historia_txt}"
    wynik    = call_deepseek(system, user_msg, MODEL_TYLER)
    reply_html = (
        f"<p>{wynik}</p>" if wynik
        else "<p>To autoresponder. Chwilowo brak zasięgu w tej strefie kosmicznej.</p>"
    )

    # Obrazek: najpierw statyczny PNG z dysku, fallback FLUX jeśli xlsx mówi obrazki_ai>0
    static_image = _get_etap_image(etap, obraz_filename)
    if static_image:
        image     = static_image
        debug_txt = None
    elif obrazki_ai > 0:
        current_app.logger.info("[pawel-flux] etap=%d — brak PNG, generuję FLUX", etap)
        # Użyj stylu z zakładki 'style' jeśli jest
        styl_flux = s_row.get("styl", "") or (wynik or opis)
        flux_prompt, flux_changes, flux_provider = _generate_flux_prompt(styl_flux)
        current_app.logger.info("[pawel-flux] prompt=%.120s provider=%s", flux_prompt, flux_provider)
        image     = _generate_flux_image(flux_prompt)
        debug_txt = _build_debug_txt(wynik or "", flux_prompt, flux_provider, etap, flux_changes)
        current_app.logger.info("[pawel-flux] etap=%d image=%s", etap, bool(image))
    else:
        image     = None
        debug_txt = None

    mp4 = _get_etap_mp4(etap, video_filename)

    current_app.logger.info(
        "[smierc] Etap %d: image=%s mp4=%s debug_txt=%s",
        etap, bool(image), bool(mp4), bool(debug_txt)
    )
    return {
        "reply_html": reply_html,
        "nowy_etap":  etap + 1,
        "image":      image,
        "mp4":        mp4,
        "debug_txt":  debug_txt,
    }
