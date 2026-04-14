"""
responders/smierc.py
Posmiertny autoresponder Pawla.

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
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
MEDIA_DIR   = os.path.join(BASE_DIR, "media")

XLSX_PATH = os.path.join(PROMPTS_DIR, "requiem_etapy.xlsx")

FILE_WYSLANNIK_SYSTEM        = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_system_8_.txt")
FILE_WYSLANNIK_FLUX_GROQ_SYS = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_flux_groq_system.txt")
FILE_WYSLANNIK_IMAGE_STYLE   = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_IMAGE_STYLE.txt")
FILE_FLUX_FORBIDDEN          = os.path.join(PROMPTS_DIR, "flux_forbidden.txt")
FILE_FLUX_MUTATIONS          = os.path.join(PROMPTS_DIR, "flux_mutations.txt")

HF_API_URL  = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS    = 5
HF_GUIDANCE = 5
TIMEOUT_SEC = 55


DEFAULT_SYSTEM_PROMPT = (
    "Jestes Pawlem — zmarlym mezczyzna piszacym z zaswiatow. "
    "Piszesz po polsku. Ton: spokojny, lekko absurdalny, z humorem. "
    "Odpowiedz maksymalnie 5 zdan. Podpisz sie: — Autoresponder Pawla-zza-swiatow. "
    "Wspomnij ze umarles na suchoty dnia {data_smierci_str}."
)


# ═══════════════════════════════════════════════════════════════════════════════
# LADOWANIE XLSX
# ═══════════════════════════════════════════════════════════════════════════════

def _load_config_xlsx() -> tuple:
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
                        "etap":              str(vals[0]),
                        "opis":              str(vals[1]) if len(vals) > 1 else "",
                        "obraz":             str(vals[2]) if len(vals) > 2 else "",
                        "video":             str(vals[3]) if len(vals) > 3 else "",
                        "kompresja_jpg":     str(vals[4]) if len(vals) > 4 else "0",
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
    b64  = _file_to_base64(path)
    if b64:
        current_app.logger.info("Obrazek etapu %d OK (%s)", etap, name)
        return {"base64": b64, "content_type": "image/png", "filename": name}
    current_app.logger.warning("Brak obrazka etapu %d: %s", etap, path)
    return None


_ATTACHMENT_MIME = {
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".avi":  "video/x-msvideo",
    ".mov":  "video/quicktime",
    ".mkv":  "video/x-matroska",
    ".ogv":  "video/ogg",
    ".3gp":  "video/3gpp",
    ".flv":  "video/x-flv",
    ".wmv":  "video/x-ms-wmv",
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt":  "text/plain",
    ".csv":  "text/csv",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".mp3":  "audio/mpeg",
    ".ogg":  "audio/ogg",
    ".wav":  "audio/wav",
}

def _get_attachment_mime(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    return _ATTACHMENT_MIME.get(ext, "application/octet-stream")


def _get_etap_video(etap: int, filename: str = ""):
    if not filename.strip():
        return None
    name = filename.strip()
    path = os.path.join(MEDIA_DIR, "mp4", "niebo", name)
    b64  = _file_to_base64(path)
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
        return {
            "base64":       compressed_b64,
            "content_type": "image/jpeg",
            "filename":     image_obj.get("filename", "niebo.png").replace(".png", ".jpg"),
        }
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
            changes.append(f"{word} -> {word}-{sufiks}")
    current_app.logger.info("[mutate] Zmutowano slow: %d", len(changes))
    return result, changes


def _generate_flux_prompt(source_text: str, groq_system_override: str = "") -> tuple:
    system = groq_system_override or _load_txt(
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

    current_app.logger.warning("[flux-prompt] Groq zawiodl — probuje DeepSeek")
    result = call_deepseek(system, user, MODEL_TYLER)
    if result:
        mutated, changes = _mutate_flux_prompt(result)
        return mutated, changes, "DeepSeek (fallback)"

    current_app.logger.warning("[flux-prompt] Oba API zawiodly — statyczny fallback")
    image_style = _load_txt(
        FILE_WYSLANNIK_IMAGE_STYLE,
        fallback="surreal heavenly paradise, divine golden light, celestial beings, vivid colors, digital art"
    )
    return image_style, [], "statyczny fallback"


def _get_hf_tokens() -> list:
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(21)]
    return [(n, v) for n in names if (v := os.getenv(n, "").strip())]


def _generate_flux_image(prompt: str, etap: int = 0, return_token_info: bool = True):
    """
    Generuje jeden obrazek FLUX z losowym seed.
    
    Args:
        prompt: Tekst promptu FLUX
        etap: Numer etapu (dla logowania)
        return_token_info: Jeśli True, zwraca info o próbach tokenów
    
    Returns:
        - Sukces: dict z base64, content_type, filename
        - Porazka: dict z "token_attempts" (jeśli return_token_info=True)
        - Porazka: None (jeśli return_token_info=False)
    """
    tokens = _get_hf_tokens()
    if not tokens:
        current_app.logger.error("[flux] Brak tokenow HF!")
        return None
    
    current_app.logger.info(
        "[flux] Dostepne tokeny HF: %d sztuk", len(tokens)
    )
    
    token_attempts = []  # Śledź wszystkie próby
    
    payload = {
        "inputs": prompt,
        "parameters": {
            "num_inference_steps": HF_STEPS,
            "guidance_scale":      HF_GUIDANCE,
            "seed":                random.randint(0, 2**32 - 1),
        }
    }
    current_app.logger.info("[flux] prompt: %s", prompt[:200])
    
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
                    "base64":       base64.b64encode(resp.content).decode("ascii"),
                    "content_type": "image/png",
                    "filename":     "niebo.png",
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
                attempt["status"] = f"SERVER_ERROR"
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


def _generate_multiple_flux_images(prompt: str, ilosc: int, kompresja_jpg: int, etap: int) -> list:
    """
    Generuje do `ilosc` obrazkow FLUX z losowym seed dla kazdego.
    Zwraca listę obrazków (każdy może mieć token_info z pierwszej próby).
    """
    wyniki = []
    first_attempt = None  # Śledź info z pierwszego obrazka
    
    for i in range(ilosc):
        current_app.logger.info("[pawel-flux] etap=%d obrazek %d/%d", etap, i + 1, ilosc)
        
        # Dla pierwszego obrazka, zbierz info o tokenach
        return_token_info = (i == 0)
        raw = _generate_flux_image(prompt, etap=etap, return_token_info=return_token_info)
        
        if raw is None:
            current_app.logger.warning(
                "[pawel-flux] etap=%d obrazek %d/%d nieudany — przerywam", etap, i + 1, ilosc
            )
            break
        
        # Wyciągnij token_info z pierwszego wyniku
        if i == 0 and isinstance(raw, dict) and "token_attempts" in raw:
            first_attempt = raw.get("token_attempts")
            # Sprawdź czy jest też base64 (sukces)
            if "base64" in raw:
                compressed = _compress_flux_image(raw, kompresja_jpg)
            else:
                # Porażka — pomiń ten obrazek
                continue
        else:
            compressed = _compress_flux_image(raw, kompresja_jpg)
        
        ext = ".jpg" if kompresja_jpg > 0 else ".png"
        compressed["filename"] = f"niebo_{etap}_{i + 1}{ext}"
        
        # Dodaj token_info do pierwszego obrazka dla debug
        if i == 0 and first_attempt:
            compressed["token_info"] = first_attempt
        
        wyniki.append(compressed)

    current_app.logger.info(
        "[pawel-flux] etap=%d wygenerowano %d/%d obrazkow", etap, len(wyniki), ilosc
    )
    return wyniki


def _build_debug_txt(source_text: str, flux_prompt: str,
                     flux_provider: str, etap: int,
                     ilosc_zamowiona: int = 1,
                     ilosc_wygenerowana: int = 1,
                     mutation_changes: list = None,
                     token_info: list = None) -> dict:
    """
    Generuje debug TXT z informacjami o FLUX i próbach tokenów.
    
    Args:
        token_info: Lista slownikow z info o próbach tokenów HF:
                    [{"token_name": "HF_TOKEN", "status": "SUCCESS", 
                      "remaining_requests": 100, ...}, ...]
    """
    
    changes_str = "\n".join(mutation_changes) if mutation_changes else "(brak mutacji)"
    
    # Formatuj info o tokenach
    tokens_str = ""
    if token_info:
        tokens_str = "\n=== TOKENY HUGGING FACE ===\n"
        tokens_str += f"Razem prob: {len(token_info)}\n\n"
        
        for attempt in token_info:
            token_name = attempt.get("token_name", "?")
            status = attempt.get("status", "unknown")
            http_code = attempt.get("http_code")
            remaining = attempt.get("remaining_requests")
            error = attempt.get("error")
            
            tokens_str += f"• {token_name}:\n"
            tokens_str += f"  Status: {status}\n"
            
            if http_code:
                tokens_str += f"  HTTP: {http_code}\n"
            
            if remaining is not None:
                tokens_str += f"  Pozostalo zadan: {remaining}\n"
            
            if error:
                tokens_str += f"  Blad: {error}\n"
            
            tokens_str += "\n"
    else:
        tokens_str = "\n--- Tokeny HF ---\nBrak danych (generacja nie byla wysylana)\n\n"
    
    content = (
        f"=== REQUIEM RESPONDER — DEBUG FLUX ===\n\n"
        f"Etap: {etap}\n"
        f"Timestamp: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"=== PROMPT FLUX ===\n{flux_prompt}\n\n"
        f"=== OBRAZKI ===\n"
        f"Zamowione: {ilosc_zamowiona}\n"
        f"Wygenerowane: {ilosc_wygenerowana}\n\n"
        f"=== ZRODLO PROMPTU ===\n{source_text[:1000]}\n\n"
        f"=== GENERACJA ===\n"
        f"Provider: {flux_provider}\n"
        f"Model: FLUX.1-schnell\n"
        f"Kroki: {HF_STEPS}\n"
        f"Guidance: {HF_GUIDANCE}\n"
        f"Seed: losowy per obrazek\n"
        f"API: {HF_API_URL}\n\n"
        f"=== MUTACJE ===\n{changes_str}\n"
        f"{tokens_str}"
    )
    
    return {
        "base64":       base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "content_type": "text/plain",
        "filename":     "_.txt",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GLOWNA FUNKCJA
# ═══════════════════════════════════════════════════════════════════════════════

def build_smierc_section(
    sender_email:     str,
    body:             str  = "",
    etap:             int  = 1,
    data_smierci_str: str  = "nieznanego dnia",
    historia:         list = None,
    data:             dict = None,
    **kwargs
) -> dict:
    """
    Obsluguje dwa sposoby wywolania:
      A) Z app.py — argumenty wprost
      B) Stary styl — slownik data={}
    """
    if historia is None:
        historia = []

    if data is not None:
        etap             = int(data.get("etap",         etap))
        data_smierci_str = data.get("data_smierci",     data_smierci_str)
        historia         = data.get("historia",         historia)
    else:
        etap = int(etap)

    etapy_dict, style_dict = _load_config_xlsx()
    max_etap     = max(etapy_dict.keys()) if etapy_dict else 50
    historia_txt = _format_historia(historia)

    # ── WYSLANNIK (etap > max_etap) ───────────────────────────────────────────
    if etap > max_etap:
        s_row = style_dict.get(etap, {})
        system_file      = s_row.get("styl_odpowiedzi_tekstowej", "")
        system_wyslannik = (
            _load_style_file(system_file)
            or _load_txt(FILE_WYSLANNIK_SYSTEM, fallback=(
                "Jestes wyslannikiem z wyzszych sfer duchowych piszacym po polsku. "
                "Przebijasz kazda rzecz wymieniona przez nadawce — tylko przymiotnikami, "
                "nigdy liczbami. Ton: dostojny, poetycki, absurdalny. Max 4 zdania. "
                "Podpisz sie: — Wyslannik z wyzszych sfer"
            ))
        )
        user_msg    = f"Osoba pyta: {body}\n\nHistoria:\n{historia_txt}"
        wynik_tekst = call_deepseek(system_wyslannik, user_msg, MODEL_TYLER)
        if not wynik_tekst:
            current_app.logger.warning("[wyslannik] DeepSeek zawiodl — probuje Groq")
            wynik_tekst = _call_groq(system_wyslannik, user_msg)

        reply_html = (
            f"<p>{wynik_tekst}</p><p><i>— Wyslannik z wyzszych sfer</i></p>"
            if wynik_tekst
            else "<p>Pawla nie ma — reinkarnlowal sie.<br><i>— Wyslannik z wyzszych sfer</i></p>"
        )

        styl_file   = s_row.get("styl", "")
        groq_system = _load_style_file(styl_file)
        flux_prompt, flux_changes, flux_provider = _generate_flux_prompt(
            wynik_tekst or body, groq_system_override=groq_system
        )
        image_result = _generate_flux_image(flux_prompt, etap=etap, return_token_info=True)
        
        # Wyciągnij obrazek i token_info
        image = None
        token_info = None
        if image_result:
            if "base64" in image_result:
                image = image_result
                token_info = image_result.get("token_info")
            elif "token_attempts" in image_result:
                token_info = image_result.get("token_attempts")
        
        debug_txt = _build_debug_txt(
            wynik_tekst or "", flux_prompt, flux_provider, etap,
            ilosc_zamowiona=1,
            ilosc_wygenerowana=1 if image else 0,
            mutation_changes=flux_changes,
            token_info=token_info,
        )

        current_app.logger.info("[wyslannik] etap=%d image=%s tokens=%s", 
                               etap, bool(image), bool(token_info))
        return {
            "reply_html": reply_html,
            "nowy_etap":  etap,
            "images":     [image] if image else [],
            "videos":     [],
            "debug_txt":  debug_txt,
        }

    # ── ETAPY 1-max_etap — Pawel ──────────────────────────────────────────────
    row   = etapy_dict.get(etap, {})
    s_row = style_dict.get(etap, {})

    if not row:
        current_app.logger.warning("[smierc] Brak etapu %d w xlsx — tryb awaryjny", etap)
        opis              = "Bladzenie w antymaterii"
        obraz_filename    = ""
        video_filename    = ""
        ilosc_obrazkow_ai = 1
        kompresja_jpg     = 0
        system_prompt_tmpl = DEFAULT_SYSTEM_PROMPT
    else:
        opis              = row.get("opis",  "")
        obraz_filename    = row.get("obraz", "")
        video_filename    = row.get("video", "")
        ilosc_obrazkow_ai = _parse_int_col(row.get("ilosc_obrazkow_ai", "0"), default=0)
        kompresja_jpg     = _parse_int_col(row.get("kompresja_jpg",     "0"), default=0)

        system_file        = s_row.get("styl_odpowiedzi_tekstowej", "")
        system_prompt_tmpl = _load_style_file(system_file) or DEFAULT_SYSTEM_PROMPT

    system   = system_prompt_tmpl.replace("{data_smierci_str}", data_smierci_str)
    user_msg = f"Etap w zaswiatach: {opis}\nWiadomosc: {body}\nHistoria:\n{historia_txt}"
    wynik    = call_deepseek(system, user_msg, MODEL_TYLER)
    reply_html = (
        f"<p>{wynik}</p>" if wynik
        else "<p>To autoresponder. Chwilowo brak zasiegu w tej strefie kosmicznej.</p>"
    )

    # Obrazek statyczny (zawsze, jesli plik istnieje)
    static_image = _get_etap_image(etap, obraz_filename)

    # Obrazki FLUX — N roznych wariacji dzieki losowemu seed
    flux_images = []
    debug_txt   = None
    token_info  = None
    if ilosc_obrazkow_ai > 0:
        current_app.logger.info(
            "[pawel-flux] etap=%d ilosc=%d kompresja=%d%% — generuje FLUX",
            etap, ilosc_obrazkow_ai, kompresja_jpg
        )
        styl_file    = s_row.get("styl", "")
        styl_content = _load_style_file(styl_file)
        flux_prompt, flux_changes, flux_provider = _generate_flux_prompt(
            styl_content or wynik or opis
        )
        current_app.logger.info(
            "[pawel-flux] prompt=%.120s provider=%s", flux_prompt, flux_provider
        )
        flux_images = _generate_multiple_flux_images(
            flux_prompt, ilosc_obrazkow_ai, kompresja_jpg, etap
        )
        
        # Wyciągnij token_info z pierwszego obrazka
        if flux_images and isinstance(flux_images[0], dict):
            token_info = flux_images[0].get("token_info")
        
        debug_txt = _build_debug_txt(
            wynik or "", flux_prompt, flux_provider, etap,
            ilosc_zamowiona=ilosc_obrazkow_ai,
            ilosc_wygenerowana=len(flux_images),
            mutation_changes=flux_changes,
            token_info=token_info,
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
        "nowy_etap":  etap + 1,
        "images":     images,
        "videos":     [mp4] if mp4 else [],
        "debug_txt":  debug_txt,
    }
