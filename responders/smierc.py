"""
responders/smierc.py [NAPRAWIONA WERSJA]
Posmiertny autoresponder Pawla.

ZMIANA: Wiadomości teraz wyliczają dni od daty śmierci i wyświetlają je w emailu!

Konfiguracja pochodzi z prompts/requiem_etapy.xlsx:
  zakladka 'etapy' — kolumny czytane po POZYCJI (niezaleznie od nazwy):
    A=etap, B=opis, C=obraz, D=video, E=kompresja_jpg, F=ilosc_obrazkow_ai
    - kompresja_jpg      -> jakosc JPG w % (0 = PNG bez kompresji)
    - ilosc_obrazkow_ai -> ile obrazkow FLUX wygenerowac (0=brak, 1/2/3/...=N)
  zakladka 'style':
    etap, styl, styl_odpowiedzi_tekstowej
    - styl                      -> nazwa pliku .txt ze stylem FLUX
    - styl_odpowiedzi_tekstowej -> nazwa pliku .txt z system promptem
      Jesli podany, NADPISUJE system_prompt z zakladki etapy.

Tryby:
  ETAP 1-max_etap  — Pawel pisze z zaswiatow
  ETAP max_etap+1+ — WYSLANNIK: DeepSeek + obrazek FLUX + _.txt debug

Podzial API:
  DeepSeek -> tekst emaila (call_deepseek / MODEL_TYLER)
  Groq     -> kreatywny prompt FLUX
  Fallback -> jesli jeden zawodzi, uzywa drugiego

Obrazki FLUX:
  Kazdy obrazek generowany z losowym seed -> rozne wariacje tego samego promptu.
  Jesli tokeny HF sie wyczerpaja przed osiagnieciem zadanej liczby,
  wysylane sa te ktore udalo sie wygenerowac.
"""

import os
import re
import random
import base64
import requests
import pandas as pd
from datetime import date, datetime
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
SUBSTITUTE_IMAGE_PATH = os.path.join(BASE_DIR, "images", "zastepczy.jpg")

XLSX_PATH = os.path.join(PROMPTS_DIR, "requiem_etapy.xlsx")

FILE_WYSLANNIK_SYSTEM = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_system_8_.txt")
FILE_WYSLANNIK_FLUX_GROQ_SYS = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_flux_groq_system.txt")
FILE_WYSLANNIK_IMAGE_STYLE = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_IMAGE_STYLE.txt")
FILE_FLUX_FORBIDDEN = os.path.join(PROMPTS_DIR, "flux_forbidden.txt")
FILE_FLUX_MUTATIONS = os.path.join(PROMPTS_DIR, "flux_mutations.txt")

HF_API_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS = 1
HF_GUIDANCE = 1
TIMEOUT_SEC = 55

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_SYSTEM_PROMPT = (
    "Byłeś człowiekiem, teraz jesteś duszą piszacą z zaswiatow. "
    "Piszesz po polsku. Ton: spokojny, lekko absurdalny, z humorem. "
    "Odpowiedz maksymalnie 50 zdan. Podpisz sie: — Autoresponder Pawla-zza-swiatow. "
    "Wspomnij ze umarles dnia {data_smierci_str}. i wymyśl absurdalną chorobę i powód śmierci gdzie powodem było własne niedopatrzenie lub pomyłka i to śmierć jak w serialu śmierć na 1000 sposóbów"
    "ZAKAZ uzywania emoji, emotikon i symboli specjalnych — tylko zwykly tekst."
)


# ═══════════════════════════════════════════════════════════════════════════════
# LICZENIE DNI OD ŚMIERCI
# ═══════════════════════════════════════════════════════════════════════════════

def _dni_w_niebie(data_smierci_str: str) -> str:
    """Oblicza ile dni minęło od daty śmierci. Zwraca gotowy tekst np. 'Jestem w niebie od 10 dni.'"""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            data_smierci = datetime.strptime(data_smierci_str.strip(), fmt).date()
            delta = (date.today() - data_smierci).days
            if delta <= 0:
                return ""
            if delta == 1:
                return "Jestem w niebie od 1 dnia."
            return f"Jestem w niebie od {delta} dni."
        except ValueError:
            continue
    return ""


def _format_dni_info(data_smierci_str: str) -> str:
    """Zwraca sformatowaną informację o dniach w niebie do wstawienia w email."""
    dni_txt = _dni_w_niebie(data_smierci_str)
    if dni_txt:
        return f"\n\n✦ {dni_txt} ✦"
    return ""


def _build_subject(etap: int, opis: str, max_etap: int) -> str:
    """Generuje temat emaila bez gwiazdek (gwiazdki dodaje app.py lub tutaj)."""
    if etap > max_etap:
        return "Wiadomość od Wysłannika z wyższych sfer"
    clean_opis = opis.strip().rstrip(".,!?") if opis.strip() else f"Etap {etap}"
    return f"Odpowiedź z zaświatów – {clean_opis}"


def _load_config_xlsx():
    """Zwraca (etapy_dict, style_dict) indeksowane numerem etapu."""
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
                    # Czytamy po pozycji kolumny (niezaleznie od nazwy naglowka):
                    # A=0 etap, B=1 opis, C=2 obraz, D=3 video,
                    # E=4 kompresja_jpg, F=5 ilosc_obrazkow_ai
                    vals = row.tolist()
                    etap_nr = int(float(str(vals[0])))
                    etapy_data[etap_nr] = {
                        "etap": str(vals[0]),
                        "opis": str(vals[1]) if len(vals) > 1 else "",
                        "obraz": str(vals[2]) if len(vals) > 2 else "",
                        "video": str(vals[3]) if len(vals) > 3 else "",
                        "kompresja_jpg": str(vals[4]) if len(vals) > 4 else "0",
                        "ilosc_obrazkow_ai": str(vals[5]) if len(vals) > 5 else "0",
                    }
                except (ValueError, KeyError, IndexError):
                    continue
        else:
            current_app.logger.warning("[smierc] Brak zakladki 'etapy' w xlsx.")

        df_style = sheets.get("style")
        if df_style is not None:
            df_style.columns = [c.strip() for c in df_style.columns]
            df_style = df_style.where(pd.notna(df_style), "")
            for _, row in df_style.iterrows():
                try:
                    style_data[int(row["etap"])] = row.to_dict()
                except (ValueError, KeyError):
                    continue
        else:
            current_app.logger.warning("[smierc] Brak zakladki 'style' w xlsx.")

    except Exception as e:
        current_app.logger.error("[smierc] Blad czytania xlsx: %s", e)

    return etapy_data, style_data


# ═══════════════════════════════════════════════════════════════════════════════
# NARZEDZIA POMOCNICZE
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_int_col(val, default: int = 0) -> int:
    """Parsuje wartosc z komorki xlsx na int."""
    s = str(val).strip()
    try:
        return int(float(s)) if s not in ("", "nan") else default
    except (ValueError, TypeError):
        return default


def _load_txt(path: str, fallback: str = "") -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        current_app.logger.warning("Blad wczytywania pliku %s: %s", path, e)
        return fallback


def _load_style_file(filename: str) -> str:
    if not filename or not filename.strip():
        return ""
    path = os.path.join(PROMPTS_DIR, filename.strip())
    content = _load_txt(path, fallback="")
    if content:
        current_app.logger.info("[smierc] Wczytano plik stylu: %s", filename)
    else:
        current_app.logger.warning("[smierc] Brak pliku stylu: %s", path)
    return content


def _file_to_base64(path: str):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None


def _get_etap_image(etap: int, filename: str = ""):
    name = filename.strip() if filename.strip() else f"{etap}.png"
    path = os.path.join(MEDIA_DIR, "images", "niebo", name)
    b64 = _file_to_base64(path)
    if b64:
        current_app.logger.info("Obrazek etapu %d OK (%s)", etap, name)
        return {"base64": b64, "content_type": "image/png", "filename": name}
    current_app.logger.warning("Brak obrazka etapu %d: %s", etap, path)
    return None


_ATTACHMENT_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
    ".3gp": "video/3gpp",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def _get_attachment_mime(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    return _ATTACHMENT_MIME.get(ext, "application/octet-stream")


def _get_etap_video(etap: int, filename: str = ""):
    if not filename.strip():
        return None
    name = filename.strip()
    path = os.path.join(MEDIA_DIR, "mp4", "niebo", name)
    b64 = _file_to_base64(path)
    if b64:
        mime = _get_attachment_mime(name)
        current_app.logger.info("Zalacznik video etapu %d OK (%s, %s)", etap, name, mime)
        return {"base64": b64, "content_type": mime, "filename": name}
    current_app.logger.warning("Brak pliku video etapu %d: %s", etap, path)
    return None


def _format_historia(historia: list) -> str:
    if not historia:
        return "(brak poprzednich wiadomosci)"
    lines = []
    for h in historia[-3:]:
        lines.append(f"Osoba: {h.get('od', '')[:300]}")
        lines.append(f"Pawel: {h.get('odpowiedz', '')[:300]}")
    return "\n".join(lines)


def _compress_flux_image(image_obj: dict, kompresja_jpg: int) -> dict:
    """
    Kompresuje obrazek FLUX:
      0   -> PNG bez kompresji
      1+  -> JPG w podanej jakosci % (np. 90 = JPG 90%)
    """
    if kompresja_jpg <= 0:
        return image_obj

    quality = max(1, min(100, kompresja_jpg))

    try:
        from PIL import Image
        import io

        raw = base64.b64decode(image_obj["base64"])
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        compressed_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        current_app.logger.info(
            "[flux-compress] kompresja=%d%% %dKB -> %dKB",
            quality, len(raw) // 1024, len(buf.getvalue()) // 1024
        )

        result = {
            "base64": compressed_b64,
            "content_type": "image/jpeg",
            "filename": image_obj.get("filename", "niebo.png").replace(".png", ".jpg"),
            "size_jpg": f"{len(buf.getvalue()) / 1024:.0f}KB",
        }

        # Skopiuj metadata z oryginalnego image_obj
        for key in ["seed", "token_name", "remaining_requests", "size_png"]:
            if key in image_obj:
                result[key] = image_obj[key]

        return result
    except Exception as e:
        current_app.logger.warning("[flux-compress] Blad kompresji: %s — zwracam oryginal", e)
        return image_obj


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
                     {"role": "user", "content": user}],
        "max_tokens": 300, "temperature": 0.7,  # Zmniejszone z 0.95
    }
    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)  # Zwiększony z 30
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"].strip()
            current_app.logger.info("[groq] OK: %.150s", result)
            return result
        current_app.logger.warning("[groq] HTTP %s: %s", resp.status_code, resp.text[:150])
    except requests.exceptions.Timeout:
        current_app.logger.warning("[groq] TIMEOUT (60s)")
    except Exception as e:
        current_app.logger.warning("[groq] Wyjatek: %s", str(e)[:100])
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
        current_app.logger.warning("Blad wczytywania listy slow %s: %s", path, e)
    return words


def _mutate_flux_prompt(prompt: str) -> tuple:
    forbidden = _load_word_list(FILE_FLUX_FORBIDDEN)
    suffixes = _load_word_list(FILE_FLUX_MUTATIONS)
    if not forbidden or not suffixes:
        current_app.logger.warning("[mutate] Brak flux_forbidden.txt lub flux_mutations.txt")
        return prompt, []
    result, changes = prompt, []
    for word in forbidden:
        pattern = re.compile(rf'(?<![a-zA-Z]){re.escape(word)}(?![a-zA-Z])', re.IGNORECASE)
        if pattern.search(result):
            sufiks = random.choice(suffixes)
            result = pattern.sub(lambda m, s=sufiks: m.group(0) + "-" + s, result)
            changes.append(f"{word} -> {word}-{sufiks}")
    current_app.logger.info("[mutate] Zmutowano slow: %d", len(changes))
    return result, changes


def _call_groq_flux(system: str, user: str) -> str | None:
    api_key = os.getenv("API_KEY_GROQ", "").strip()
    if not api_key:
        current_app.logger.warning("[groq-flux] Brak API_KEY_GROQ")
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 2000,
        "temperature": 0.7,  # Zmniejszone z 0.95
    }
    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)  # Zwiększony z 30
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"].strip()
            current_app.logger.info("[groq-flux] OK: %.150s", result)
            return result
        current_app.logger.warning("[groq-flux] HTTP %s: %s", resp.status_code, resp.text[:150])
    except requests.exceptions.Timeout:
        current_app.logger.warning("[groq-flux] TIMEOUT (60s)")
    except Exception as e:
        current_app.logger.warning("[groq-flux] Wyjatek: %s", str(e)[:100])
    return None


def _generate_flux_prompt(source_text: str, groq_system_override: str = "") -> tuple:
    """Generuje prompt do FLUX z podanego tekstu. Zwraca (prompt, mutation_changes, provider)."""
    groq_system = groq_system_override or _load_txt(FILE_WYSLANNIK_FLUX_GROQ_SYS, fallback=(
        "Jesteś kreatywnym promptem dla FLUX do generacji obrazów. "
        "Na podstawie tekstu generujesz wizualny opis obrazu. "
        "Wynik: jeden akapit, max 100 słów, bez nawiasów, w angielskim lub polskim."
    ))
    prompt = _call_groq_flux(groq_system, source_text)
    if not prompt:
        current_app.logger.warning("[flux] Groq zawiodl — probuje DeepSeek")
        from core.ai_client import call_deepseek, MODEL_TYLER
        deepseek_prompt = call_deepseek(groq_system, source_text, MODEL_TYLER)
        if deepseek_prompt:
            prompt = deepseek_prompt
            current_app.logger.info("[flux] DeepSeek OK dla promptu FLUX")
        else:
            current_app.logger.warning("[flux] DeepSeek tez zawiodl — uzywam tekst wprost (1000 znaków)")
            prompt = source_text[:1000]

    mutated_prompt, changes = _mutate_flux_prompt(prompt)
    if changes:
        current_app.logger.info("[flux] Mutacje: %s", ", ".join(changes))

    provider = "groq" if groq_system_override == "" else "custom"
    return mutated_prompt, changes, provider, prompt


def _get_hf_tokens() -> list:
    """Pobiera listę dostępnych tokenów HF (HF_TOKEN, HF_TOKEN1...HF_TOKEN20)."""
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(21)]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


def _load_substitute_image() -> dict | None:
    if not os.path.exists(SUBSTITUTE_IMAGE_PATH):
        current_app.logger.warning("[smierc-test] Brak pliku zastępczego: %s", SUBSTITUTE_IMAGE_PATH)
        return None
    try:
        with open(SUBSTITUTE_IMAGE_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return {
            "base64":       b64,
            "content_type": "image/jpeg",
            "filename":     "zastepczy.jpg",
        }
    except Exception as e:
        current_app.logger.warning("[smierc-test] Błąd odczytu zastepczy.jpg: %s", e)
        return None


def _generate_flux_image(prompt: str, etap: int = 0, return_token_info: bool = False, test_mode: bool = False) -> dict | None:
    """
    Generuje jeden obrazek FLUX z losowym seed.
    Próbuje każdy token HF po kolei (HF_TOKEN, HF_TOKEN1...HF_TOKEN20).
    Nikdy nie używa 2 tokeny równocześnie — jeden token = jeden request.
    
    Args:
        prompt: Tekst promptu FLUX
        etap: Numer etapu (dla logowania)
        return_token_info: Jeśli True, zwraca info o próbach tokenów
        test_mode: Jeśli True, używa obrazu zastępczego zamiast wywoływać HF
    
    Returns:
        - Sukces: dict z base64, content_type, filename
        - Porażka: dict z "token_attempts" (jeśli return_token_info=True)
        - Porażka: None (jeśli return_token_info=False)
    """
    if test_mode:
        substitute = _load_substitute_image()
        if substitute:
            substitute = dict(substitute)
            substitute["filename"] = f"smierc_etap{etap}_zastepczy.jpg"
            current_app.logger.info("[smierc-flux] test_mode — używam zastepczy.jpg dla etapu %d", etap)
            return substitute
        current_app.logger.warning("[smierc-flux] test_mode — brak zastepczy.jpg, pomijam obrazek dla etapu %d", etap)
        return None
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[flux] Brak tokenow HF!")
        return None

    current_app.logger.info(
        "[flux] Dostepne tokeny HF: %d sztuk", len(tokens)
    )

    token_attempts = []  # Śledź wszystkie próby

    seed = random.randint(0, 2 ** 32 - 1)
    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale": HF_GUIDANCE,
            "seed": seed,
        }
    }
    current_app.logger.info("[flux] prompt: %s seed: %d", prompt[:200], seed)

    # Próbuj każdy token po kolei
    for name, token in tokens:
        attempt = {
            "token_name": name,
            "status": "unknown",
            "http_code": None,
            "remaining_requests": None,
            "error": None
        }

        headers = {"Authorization": f"Bearer {token}", "Accept": "image/png"}
        try:
            current_app.logger.info("[flux-attempt] Probuje token: %s", name)
            resp = requests.post(HF_API_URL, headers=headers, json=payload, timeout=TIMEOUT_SEC)

            # Wyciągnij info z headera Hugging Face
            remaining = resp.headers.get("X-Remaining-Requests")
            if remaining:
                attempt["remaining_requests"] = int(remaining)

            attempt["http_code"] = resp.status_code

            if resp.status_code == 200:
                attempt["status"] = "SUCCESS"
                current_app.logger.info(
                    "[flux] ✓ Token %s: sukces (PNG %d B, pozostalo: %s zadan)",
                    name, len(resp.content), remaining or "?"
                )

                result = {
                    "base64": base64.b64encode(resp.content).decode("ascii"),
                    "content_type": "image/png",
                    "filename": f"niebo_etap{etap}_seed{seed}.png",
                    "seed": seed,
                    "token_name": name,
                    "remaining_requests": int(remaining) if remaining else None,
                    "size_png": f"{len(resp.content) / 1024 / 1024:.1f}MB",
                }

                # Dodaj info o tokenach jeśli jest tego wiele (dla debug)
                if return_token_info and len(token_attempts) > 0:
                    result["token_info"] = token_attempts

                return result

            elif resp.status_code in (401, 403):
                attempt["status"] = "INVALID_TOKEN"
                attempt["error"] = f"Nieautoryzowany ({resp.status_code})"
                current_app.logger.warning(
                    "[flux] ✗ Token %s: invalid/expired (HTTP %d)",
                    name, resp.status_code
                )

            elif resp.status_code in (503, 529):
                attempt["status"] = "OVERLOADED"
                attempt["error"] = f"Przeciazony ({resp.status_code})"
                current_app.logger.warning(
                    "[flux] ⚠ Token %s: serwer przeciazony (HTTP %d)",
                    name, resp.status_code
                )

            elif resp.status_code >= 500:
                attempt["status"] = "SERVER_ERROR"
                attempt["error"] = f"HTTP {resp.status_code}"
                current_app.logger.warning(
                    "[flux] ✗ Token %s: blad serwera %d: %s",
                    name, resp.status_code, resp.text[:100]
                )

            else:
                attempt["status"] = f"HTTP_{resp.status_code}"
                attempt["error"] = resp.text[:100] if resp.text else "Unknown error"
                current_app.logger.warning(
                    "[flux] ✗ Token %s: blad %d: %s",
                    name, resp.status_code, resp.text[:100]
                )

        except requests.exceptions.Timeout:
            attempt["status"] = "TIMEOUT"
            attempt["error"] = f"Timeout ({TIMEOUT_SEC}s)"
            current_app.logger.warning("[flux] ⏱ Token %s: timeout (%ds)", name, TIMEOUT_SEC)

        except requests.exceptions.ConnectionError as e:
            attempt["status"] = "CONNECTION_ERROR"
            attempt["error"] = str(e)[:50]
            current_app.logger.warning("[flux] 🔌 Token %s: connection error: %s", name, str(e)[:50])

        except Exception as e:
            attempt["status"] = "EXCEPTION"
            attempt["error"] = str(e)[:50]
            current_app.logger.warning("[flux] ❌ Token %s: exception: %s", name, str(e)[:50])

        token_attempts.append(attempt)

    current_app.logger.error(
        "[flux] ✗ Wszystkie tokeny HF zawiodly! (%d tokenow sprobowanych)",
        len(token_attempts)
    )

    # Zwróć info o tokenach nawet przy porażce
    if return_token_info:
        return {"token_attempts": token_attempts}

    return None


def _generate_multiple_flux_images(prompt: str, count: int, kompresja_jpg: int = 0, etap: int = 0, test_mode: bool = False) -> list:
    """Generuje N obrazków FLUX (z losowym seed dla każdego)."""
    images = []
    for i in range(count):
        img = _generate_flux_image(prompt, etap=etap, test_mode=test_mode)
        if img:
            if kompresja_jpg > 0:
                img = _compress_flux_image(img, kompresja_jpg)
            images.append(img)
            current_app.logger.info("[flux-multi] Obrazek %d/%d OK", i + 1, count)
        else:
            current_app.logger.warning("[flux-multi] Obrazek %d/%d — brak", i + 1, count)
    return images


def _build_debug_txt(
        reply_text: str,
        flux_prompt: str,
        flux_provider: str,
        etap: int,
        ilosc_zamowiona: int = 0,
        ilosc_wygenerowana: int = 0,
        kompresja_jpg: int = 0,
        mutation_changes: list = None,
        token_info=None,
        body_text: str = "",
        system_prompt: str = "",
        groq_response: str = "",
        flux_prompt_raw: str = "",
        image_details: list = None,
) -> dict:
    """
    Buduje szczegółowy debug info jako plain text — dla każdego obrazka.
    Wypisuje PEŁNĄ logikę działania programu.
    """
    if mutation_changes is None:
        mutation_changes = []
    if image_details is None:
        image_details = []

    lines = []

    # ═══ NAGŁÓWEK ═══
    lines.append("=" * 88)
    lines.append(f"=== DEBUG {datetime.now().isoformat()} ===")
    lines.append("=" * 88)
    lines.append(f"ETAP: {etap}")
    lines.append(f"OBRAZKI: {ilosc_wygenerowana}/{ilosc_zamowiona}")
    lines.append(f"KOMPRESJA JPG: {kompresja_jpg}%")
    lines.append("")

    # ═══ [1] WIADOMOŚĆ OD UŻYTKOWNIKA ═══
    lines.append("=" * 88)
    lines.append("[1] WIADOMOŚĆ OD UŻYTKOWNIKA")
    lines.append("=" * 88)
    if body_text:
        lines.append("Script otrzymał z emaila:")
        lines.append(f'"{body_text}"')
    else:
        lines.append("(brak tekstu wejściowego)")
    lines.append("")

    # ═══ [2] SYSTEM PROMPT DLA GROQ ═══
    lines.append("=" * 88)
    lines.append("[2] SYSTEM PROMPT DLA GROQ")
    lines.append("=" * 88)
    if system_prompt:
        lines.append(system_prompt)
    else:
        lines.append("(brak system promptu)")
    lines.append("")

    # ═══ [3] ODPOWIEDŹ OD GROQ ═══
    lines.append("=" * 88)
    lines.append("[3] ODPOWIEDŹ OD GROQ")
    lines.append("=" * 88)
    if groq_response:
        lines.append(f"Groq odpowiedział ({len(groq_response)} znaków):")
        lines.append(groq_response)
    else:
        lines.append("(brak odpowiedzi od Groq)")
    lines.append("")

    # ═══ [4] MUTACJA SŁÓW ZAKAZANYCH ═══
    lines.append("=" * 88)
    lines.append("[4] MUTACJA SŁÓW ZAKAZANYCH (flux_forbidden.txt)")
    lines.append("=" * 88)
    if mutation_changes:
        lines.append(f"Razem zmutowano słów: {len(mutation_changes)}")
        lines.append("")
        for i, change in enumerate(mutation_changes, 1):
            lines.append(f"  {i}. {change}")
    else:
        lines.append("Nie znaleziono słów do mutacji.")
    lines.append("")

    # ═══ [5] FINALNA ZAWARTOŚĆ WYSŁANA DO FLUX ═══
    lines.append("=" * 88)
    lines.append("[5] FINALNA ZAWARTOŚĆ WYSŁANA DO FLUX")
    lines.append("=" * 88)
    lines.append(f"FLUX Provider: {flux_provider.upper()}")
    lines.append(f"FLUX Model: FLUX.1-schnell")
    lines.append(f"Max tokens: bez limitu")
    lines.append("")
    lines.append("--- PROMPT PRZED MUTACJĄ ---")
    lines.append(flux_prompt_raw or "(brak)")
    lines.append("")
    lines.append(f"--- PROMPT PO MUTACJI ({len(flux_prompt)} znaków) — wysłany do FLUX ---")
    lines.append(flux_prompt)
    lines.append("")

    # ═══ [6] GENEROWANIE OBRAZKÓW — SZCZEGÓŁY ═══
    lines.append("=" * 88)
    lines.append("[6] GENEROWANIE OBRAZKÓW — SZCZEGÓŁY")
    lines.append("=" * 88)
    if image_details:
        for i, img in enumerate(image_details, 1):
            lines.append("")
            lines.append("-" * 88)
            lines.append(f"OBRAZEK {i}/{ilosc_zamowiona}")
            lines.append("-" * 88)
            lines.append(f"Seed: {img.get('seed', 'N/A')}")
            lines.append(f"Token HF: {img.get('token_name', 'N/A')}")
            lines.append(f"Status: {img.get('status', 'N/A')}")
            if img.get('http_code'):
                lines.append(f"HTTP Code: {img.get('http_code')}")
            if img.get('size_png'):
                lines.append(f"Rozmiar PNG: {img.get('size_png')}")
            if img.get('size_jpg'):
                lines.append(f"Rozmiar JPG ({kompresja_jpg}%): {img.get('size_jpg')}")
            lines.append(f"Filename: {img.get('filename', 'N/A')}")
            if img.get('remaining_requests') is not None:
                lines.append(f"X-Remaining-Requests: {img.get('remaining_requests')}")
            if img.get('error'):
                lines.append(f"Error: {img.get('error')}")
    else:
        lines.append("(brak szczegółów obrazków)")
    lines.append("")

    # ═══ [7] PODSUMOWANIE ═══
    lines.append("=" * 88)
    lines.append("[7] PODSUMOWANIE")
    lines.append("=" * 88)
    lines.append(f"Łącznie obrazków: {ilosc_wygenerowana}/{ilosc_zamowiona}")
    if token_info:
        lines.append(f"Tokeny HF: {token_info}")
    lines.append("")

    lines.append("=" * 88)
    lines.append("KONIEC DEBUG")
    lines.append("=" * 88)

    content = "\n".join(lines)
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    return {
        "base64": b64,
        "content_type": "text/plain",
        "filename": "_.txt"
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GŁÓWNA FUNKCJA
# ═══════════════════════════════════════════════════════════════════════════════

def build_smierc_section(
        sender_email: str,
        body: str = "",
        etap: int = 1,
        data_smierci_str: str = "nieznanego dnia",
        historia: list = None,
        data: dict = None,
        **kwargs
) -> dict:
    """
    Obsluguje dwa sposoby wywolania:
      A) Z app.py — argumenty wprost
      B) Stary styl — slownik data={}
    
    ZMIANA: Wiadomości zawierają wyliczone dni od śmierci!
    """
    if historia is None:
        historia = []

    if data is not None:
        etap = int(data.get("etap", etap))
        data_smierci_str = data.get("data_smierci", data_smierci_str)
        historia = data.get("historia", historia)
    else:
        etap = int(etap)

    test_mode = bool(kwargs.get("test_mode", False))

    etapy_dict, style_dict = _load_config_xlsx()
    max_etap = max(etapy_dict.keys()) if etapy_dict else 50
    historia_txt = _format_historia(historia)

    # ── WYSLANNIK (etap > max_etap) ───────────────────────────────────────────
    if etap > max_etap:
        s_row = style_dict.get(etap, {})
        system_file = s_row.get("styl_odpowiedzi_tekstowej", "")
        system_wyslannik = (
                _load_style_file(system_file)
                or _load_txt(FILE_WYSLANNIK_SYSTEM, fallback=(
            "Jestes wyslannikiem z wyzszych sfer duchowych piszacym po polsku. "
            "Przebijasz kazda rzecz wymieniona przez nadawce — tylko przymiotnikami, "
            "nigdy liczbami. Ton: dostojny, poetycki, absurdalny. Max 4 zdania. "
            "Podpisz sie: — Wyslannik z wyzszych sfer"
        ))
        )
        # Zakaz emoji (doklejamy na końcu system promptu)
        if "ZAKAZ" not in system_wyslannik:
            system_wyslannik += "\nZAKAZ uzywania emoji, emotikon i symboli specjalnych — tylko zwykly tekst."

        dni_txt = _dni_w_niebie(data_smierci_str)
        user_msg = (
            f"Osoba pyta: {body}\n\nHistoria:\n{historia_txt}\n\n"
            f"Data śmierci Pawła: {data_smierci_str}\n"
        )
        wynik_tekst = call_deepseek(system_wyslannik, user_msg, MODEL_TYLER)
        if not wynik_tekst:
            current_app.logger.warning("[wyslannik] DeepSeek zawiodl — probuje Groq")
            wynik_tekst = _call_groq(system_wyslannik, user_msg)

        # 🆕 ZMIANA: Dodaj dni do HTML
        reply_text = wynik_tekst or "Pawła nie ma — reinkarnował się."
        reply_text += _format_dni_info(data_smierci_str)
        reply_text += "\n\n— Wyslannik z wyższych sfer"

        from core.html_builder import build_html_reply_dark
        reply_html = build_html_reply_dark(reply_text)

        styl_file = s_row.get("styl", "")
        groq_system = _load_style_file(styl_file)
        source_with_date = f"{wynik_tekst or body}\n\n[Pawel umarl dnia: {data_smierci_str}]"
        flux_prompt, flux_changes, flux_provider, flux_prompt_raw = _generate_flux_prompt(
            source_with_date, groq_system_override=groq_system
        )
        image_result = _generate_flux_image(
            flux_prompt,
            etap=etap,
            return_token_info=True,
            test_mode=test_mode,
        )

        # Wyciągnij obrazek i token_info
        image = None
        token_info = None
        if image_result:
            if "base64" in image_result:
                image = image_result
                token_info = image_result.get("token_info")
            elif "token_attempts" in image_result:
                token_info = image_result.get("token_attempts")

        # Przygotuj szczegóły obrazka
        image_details = []
        token_info_str = "N/A"
        if image:
            image_details.append({
                "seed": image.get("seed"),
                "token_name": image.get("token_name"),
                "status": "SUCCESS",
                "size_png": image.get("size_png"),
                "size_jpg": image.get("size_jpg"),
                "filename": image.get("filename"),
                "remaining_requests": image.get("remaining_requests"),
            })
            token_info_str = image.get("token_name", "N/A")

        debug_txt = _build_debug_txt(
            wynik_tekst or "", flux_prompt, flux_provider, etap,
            ilosc_zamowiona=1,
            ilosc_wygenerowana=1 if image else 0,
            kompresja_jpg=0,
            mutation_changes=flux_changes,
            token_info=token_info_str,
            body_text=body,
            system_prompt=system_wyslannik,
            groq_response=wynik_tekst or "",
            flux_prompt_raw=flux_prompt_raw,
            image_details=image_details,
        )

        current_app.logger.info("[wyslannik] etap=%d image=%s tokens=%s",
                                etap, bool(image), bool(token_info))
        return {
            "reply_html": reply_html,
            "subject": _build_subject(etap, "", max_etap),
            "nowy_etap": etap,
            "images": [image] if image else [],
            "videos": [],
            "debug_txt": debug_txt,
        }

    # ── ETAPY 1-max_etap — Pawel ──────────────────────────────────────────────
    row = etapy_dict.get(etap, {})
    s_row = style_dict.get(etap, {})

    if not row:
        current_app.logger.warning("[smierc] Brak etapu %d w xlsx — tryb awaryjny", etap)
        opis = "Bladzenie w antymaterii"
        obraz_filename = ""
        video_filename = ""
        ilosc_obrazkow_ai = 1
        kompresja_jpg = 0
        system_prompt_tmpl = DEFAULT_SYSTEM_PROMPT
    else:
        opis = row.get("opis", "")
        obraz_filename = row.get("obraz", "")
        video_filename = row.get("video", "")
        ilosc_obrazkow_ai = _parse_int_col(row.get("ilosc_obrazkow_ai", "0"), default=0)
        kompresja_jpg = _parse_int_col(row.get("kompresja_jpg", "0"), default=0)

        system_file = s_row.get("styl_odpowiedzi_tekstowej", "")
        system_prompt_tmpl = _load_style_file(system_file) or DEFAULT_SYSTEM_PROMPT
        # Zakaz emoji (doklejamy na końcu jeśli jeszcze nie ma)
        if "ZAKAZ" not in system_prompt_tmpl:
            system_prompt_tmpl += "\nZAKAZ uzywania emoji, emotikon i symboli specjalnych — tylko zwykly tekst."

    system = system_prompt_tmpl.replace("{data_smierci_str}", data_smierci_str)
    dni_txt = _dni_w_niebie(data_smierci_str)
    user_msg = (
        f"Etap w zaswiatach: {opis}\nWiadomosc: {body}\nHistoria:\n{historia_txt}\n\n"
        f"Data śmierci: {data_smierci_str}\n"
    )
    wynik = call_deepseek(system, user_msg, MODEL_TYLER)

    # Fallback do Groq jeśli DeepSeek zawiedzie
    if not wynik:
        current_app.logger.warning("[smierc-etapy] DeepSeek zawiodl — probuje Groq")
        wynik = _call_groq(system, user_msg)

    # 🆕 ZMIANA: Dodaj dni do HTML
    reply_text = wynik or "To autoresponder. Chwilowo brak zasięgu w tej strefie kosmicznej."
    reply_text += _format_dni_info(data_smierci_str)

    from core.html_builder import build_html_reply
    reply_html = build_html_reply(reply_text)

    # Obrazek statyczny (zawsze, jesli plik istnieje)
    static_image = _get_etap_image(etap, obraz_filename)

    # Obrazki FLUX — N roznych wariacji dzieki losowemu seed
    flux_images = []
    debug_txt = None
    token_info = None
    if ilosc_obrazkow_ai > 0:
        current_app.logger.info(
            "[pawel-flux] etap=%d ilosc=%d kompresja=%d%% — generuje FLUX",
            etap, ilosc_obrazkow_ai, kompresja_jpg
        )
        styl_file = s_row.get("styl", "")
        groq_system = _load_style_file(styl_file)
        source_with_date = f"{wynik or opis}\n\n[Pawel umarl dnia: {data_smierci_str}]"
        flux_prompt, flux_changes, flux_provider, flux_prompt_raw = _generate_flux_prompt(
            source_with_date, groq_system_override=groq_system
        )
        current_app.logger.info(
            "[pawel-flux] prompt=%.120s provider=%s", flux_prompt, flux_provider
        )
        flux_images = _generate_multiple_flux_images(
            flux_prompt,
            ilosc_obrazkow_ai,
            kompresja_jpg,
            etap,
            test_mode=test_mode,
        )

        # Wyciągnij token_info z pierwszego obrazka i szczegóły wszystkich
        image_details = []
        token_summary = {}
        for i, img in enumerate(flux_images, 1):
            if isinstance(img, dict):
                img_detail = {
                    "seed": img.get("seed"),
                    "token_name": img.get("token_name"),
                    "status": "SUCCESS" if "base64" in img else "FAILED",
                    "size_png": img.get("size_png"),
                    "size_jpg": img.get("size_jpg"),
                    "filename": img.get("filename"),
                    "remaining_requests": img.get("remaining_requests"),
                }
                image_details.append(img_detail)

                # Zlicz tokeny
                token_name = img.get("token_name", "unknown")
                if token_name not in token_summary:
                    token_summary[token_name] = 0
                token_summary[token_name] += 1

        token_info_str = ", ".join(
            [f"{k}: {v} obrazków" for k, v in sorted(token_summary.items())]) if token_summary else "N/A"

        debug_txt = _build_debug_txt(
            wynik or "", flux_prompt, flux_provider, etap,
            ilosc_zamowiona=ilosc_obrazkow_ai,
            ilosc_wygenerowana=len(flux_images),
            kompresja_jpg=kompresja_jpg,
            mutation_changes=flux_changes,
            token_info=token_info_str,
            body_text=body,
            system_prompt=system,
            groq_response=wynik or "",
            flux_prompt_raw=flux_prompt_raw,
            image_details=image_details,
        )

    # Lista obrazkow: statyczny PNG pierwszy, potem FLUX
    images = [img for img in [static_image] + flux_images if img]

    mp4 = _get_etap_video(etap, video_filename)

    current_app.logger.info(
        "[smierc] Etap %d: images=%d (flux=%d/%d) mp4=%s debug_txt=%s tokens=%s",
        etap, len(images), len(flux_images), ilosc_obrazkow_ai, bool(mp4), bool(debug_txt),
        bool(token_info)
    )
    return {
        "reply_html": reply_html,
        "reply_text": wynik or "",
        "nowy_etap": etap + 1,
        "images": images,
        "videos": [mp4] if mp4 else [],
        "debug_txt": debug_txt,
    }
