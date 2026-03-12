"""
responders/smierc.py - Wersja POPRAWIONA z rozszerzonym logowaniem
"""

import os
import re
import random
import base64
import requests
from datetime import date, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import current_app

from core.ai_client import call_deepseek, MODEL_TYLER

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
MEDIA_DIR   = os.path.join(BASE_DIR, "media")

# ── Ścieżki plików ────────────────────────────────────────────────────────────
FILE_XLSX                    = os.path.join(PROMPTS_DIR, "requiem_etapy.xlsx")
FILE_WYSLANNIK_SYSTEM        = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_system_8_.txt")
FILE_WYSLANNIK_FLUX_GROQ_SYS = os.path.join(PROMPTS_DIR, "requiem_WYSLANNIK_flux_groq_system.txt")
FILE_FLUX_FORBIDDEN          = os.path.join(PROMPTS_DIR, "flux_forbidden.txt")
FILE_FLUX_MUTATIONS          = os.path.join(PROMPTS_DIR, "flux_mutations.txt")

HF_API_URL  = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS    = 5
HF_GUIDANCE = 5
TIMEOUT_SEC = 55

GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MAX_AI_IMAGES = 10
DEFAULT_SYSTEM_PROMPT = "Piszesz tajemniczą wiadomość z innego wymiaru. Ton: poetycki. Max 5 zdań."

# ═══════════════════════════════════════════════════════════════════════════════
# NARZĘDZIA DIAGNOSTYCZNE I POMOCNICZE
# ═══════════════════════════════════════════════════════════════════════════════

def _guess_content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "png":  "image/png", "jpg":  "image/jpeg", "jpeg": "image/jpeg",
        "gif":  "image/gif", "webp": "image/webp",
        "mp4":  "video/mp4", "mpg":  "video/mpeg", "mpeg": "video/mpeg", # DODANO MPG
        "mov":  "video/quicktime", "avi":  "video/x-msvideo",
        "pdf":  "application/pdf",
    }.get(ext, "application/octet-stream")

def _get_hf_tokens() -> list:
    """Pobiera tokeny i loguje ich dostępność."""
    names = [f"HF_TOKEN{i}" if i > 0 else "HF_TOKEN" for i in range(21)]
    found = []
    for n in names:
        val = os.getenv(n, "").strip()
        if val:
            found.append((n, val))
    
    current_app.logger.info("[flux-diag] Wykryto %d aktywnych tokenów HF w systemie.", len(found))
    if len(found) < 16:
        current_app.logger.warning("[flux-diag] Użytkownik zgłosił 20, ale system widzi tylko %d. Sprawdź ENV!", len(found))
    return found

def _oblicz_dni(data_smierci_str: str) -> str:
    s = data_smierci_str.strip()
    # Próba obsługi formatu słownego (bardzo uproszczona informacja)
    if any(word in s.lower() for word in ["stycznia", "lutego", "marca", "kwietnia"]):
        current_app.logger.error("[dni] BŁĄD: Data w Excelu jest SŁOWNA ('%s'). Zmień na RRRR-MM-DD!", s)
        return "?"

    fmt_candidates = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"]
    
    # Obsługa formatu z Google Apps Script (JS Date string)
    m = re.match(r'\w+ (\w+) (\d+) (\d{4})', s)
    if m:
        months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
        try:
            d = date(int(m.group(3)), months.get(m.group(1), 0), int(m.group(2)))
            return str((date.today() - d).days)
        except Exception: pass

    for fmt in fmt_candidates:
        try:
            d = datetime.strptime(s, fmt).date()
            return str((date.today() - d).days)
        except ValueError: continue

    current_app.logger.warning("[dni] Nie sparsowano daty: %s. Upewnij się, że to format RRRR-MM-DD.", s)
    return "?"

# ═══════════════════════════════════════════════════════════════════════════════
# ŁADOWANIE MEDIÓW Z LOGOWANIEM ŚCIEŻEK
# ═══════════════════════════════════════════════════════════════════════════════

def _load_file_list(file_list_str: str, base_dir: str, label: str) -> list:
    results = []
    if not file_list_str: return results
    
    for fname in file_list_str.split(","):
        fname = fname.strip()
        if not fname: continue
        path = os.path.normpath(os.path.join(base_dir, fname))
        
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
                results.append({
                    "base64": b64,
                    "content_type": _guess_content_type(fname),
                    "filename": fname,
                })
                current_app.logger.info("[%s] ZAŁADOWANO: %s", label, path)
        except FileNotFoundError:
            current_app.logger.error("[%s] BRAK PLIKU! Ścieżka: %s", label, path)
        except Exception as e:
            current_app.logger.error("[%s] Błąd odczytu %s: %s", label, path, e)
    return results

# ═══════════════════════════════════════════════════════════════════════════════
# GŁÓWNA LOGIKA SMIERC
# ═══════════════════════════════════════════════════════════════════════════════

def build_smierc_section(sender_email, body, etap, data_smierci_str, historia) -> dict:
    etapy = _load_xlsx() # Zakładamy że _load_xlsx jest zdefiniowane jak wcześniej
    max_etap = max(etapy.keys()) if etapy else 0

    if not etapy or etap > max_etap:
        current_app.logger.info("[smierc] Etap %d > %d -> Tryb Wysłannika", etap, max_etap)
        return _run_wyslannik(body, historia, etap)

    row = etapy.get(etap)
    if not row:
        current_app.logger.error("[smierc] Nie znaleziono danych dla etapu %d w Excelu!", etap)
        return {"reply_html": "Błąd konfiguracji etapu.", "nowy_etap": etap, "images": [], "videos": []}

    # Przygotowanie promptu
    dni = _oblicz_dni(data_smierci_str)
    system_prompt = _resolve_system_prompt(row.get("styl_odpowiedzi_tekstowej", ""), row.get("system_prompt", ""))
    system_prompt = system_prompt.replace("{data_smierci_str}", data_smierci_str).replace("{dni}", dni)

    # Generowanie tekstu
    wynik = call_deepseek(system_prompt, f"Osoba: {body}", MODEL_TYLER) or _call_groq(system_prompt, body)
    
    # Ładowanie mediów
    images_statyczne = _load_file_list(row["obraz"], os.path.join(MEDIA_DIR, "images", "niebo"), "IMAGE")
    videos = _load_file_list(row["video"], os.path.join(MEDIA_DIR, "mp4", "niebo"), "VIDEO")
    
    # AI Images (FLUX)
    images_ai = []
    debug_txt = None
    if row.get("obrazki_ai", 0) > 0:
        current_app.logger.info("[flux] Generowanie %d obrazków AI dla etapu %d", row["obrazki_ai"], etap)
        images_ai, debug_lines = _generate_n_flux_images(row["obrazki_ai"], wynik or row["opis"], row["styl_flux"], etap)
        images_ai = _compress_images_ai(images_ai, row["obrazki_ai"])
        debug_txt = _build_debug_txt(wynik or "", debug_lines, etap)

    # POŁĄCZENIE OBRAZKÓW: Statyczne + AI trafiają do jednej listy 'images'
    final_images = images_statyczne + images_ai

    current_app.logger.info("[smierc] FINISH etap %d: tekst=%s, obrazy=%d, wideo=%d", 
                            etap, bool(wynik), len(final_images), len(videos))

    return {
        "reply_html": f"<p>{wynik}</p>" if wynik else "<p>Cisza w eterze...</p>",
        "nowy_etap":  etap + 1,
        "images":     final_images, # Tutaj są teraz WSZYSTKIE obrazki
        "videos":     videos,
        "debug_txt":  debug_txt,
    }