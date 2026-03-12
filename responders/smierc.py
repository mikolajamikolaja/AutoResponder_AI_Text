import os
import re
import random
import base64
import requests
import csv
from flask import current_app

# --- KONFIGURACJA ---
DEFAULT_SYSTEM_PROMPT = "Jesteś Pawłem, piszesz z zaświatów. Ton absurdalny, krótko."
ETAPY_CSV_PATH = os.path.join("prompts", "etapy.csv")
STYLE_CSV_PATH = os.path.join("prompts", "style.csv")

def _load_config_csv():
    """Wczytuje etapy i style z plików CSV."""
    etapy_data = {}
    style_data = {}

    # Wczytywanie etapy.csv
    if os.path.exists(ETAPY_CSV_PATH):
        try:
            with open(ETAPY_CSV_PATH, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        e_id = int(row['etap'])
                        etapy_data[e_id] = row
                    except: continue
            current_app.logger.info(f"[smierc] Wczytano {len(etapy_data)} etapów z CSV.")
        except Exception as e:
            current_app.logger.error(f"[smierc] Błąd czytania etapy.csv: {e}")
    else:
        current_app.logger.error(f"[smierc] Brak pliku: {ETAPY_CSV_PATH}")

    # Wczytywanie style.csv
    if os.path.exists(STYLE_CSV_PATH):
        try:
            with open(STYLE_CSV_PATH, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        e_id = int(row['etap'])
                        style_data[e_id] = row
                    except: continue
        except Exception as e:
            current_app.logger.error(f"[smierc] Błąd czytania style.csv: {e}")
    
    return etapy_data, style_data

def build_smierc_section(data, sender_email, **kwargs):
    """Główna funkcja budująca sekcję SMIERC dla webhooka."""
    etap = int(data.get('etap', 1))
    data_smierci_str = data.get('data_smierci', "nieznanego dnia")
    historia = data.get('historia', "")

    # 1. Ładowanie danych
    etapy_dict, style_dict = _load_config_csv()

    # 2. Pobieranie danych dla konkretnego etapu
    row = etapy_dict.get(etap, {})
    s_row = style_dict.get(etap, {})

    if not row:
        # Tryb awaryjny / Wysłannik (jeśli etapu nie ma w CSV)
        opis = "Błądzenie w antymaterii"
        obraz_lista = ""
        video_lista = ""
        obrazki_ai = 1
        system_prompt_template = DEFAULT_SYSTEM_PROMPT
    else:
        opis = row.get('opis', "")
        obraz_lista = row.get('obraz', "")
        video_lista = row.get('video', "")
        
        # Bezpieczne czytanie obrazki_ai (rozwiązuje problem etapu 30)
        val_ai = row.get('obrazki_ai', '0').strip()
        try:
            obrazki_ai = int(val_ai) if val_ai else 0
        except:
            obrazki_ai = 0
            
        system_prompt_template = row.get('system_prompt') or DEFAULT_SYSTEM_PROMPT

    # 3. Personalizacja system promptu (wstawienie daty)
    system_prompt = system_prompt_template.replace("{data_smierci_str}", data_smierci_str)
    
    # 4. Styl wizualny FLUX
    styl_flux_raw = s_row.get('styl', "")
    styl_flux = _load_style_if_file(styl_flux_raw)

    # 5. Generowanie tekstu przez AI (Groq/DeepSeek)
    # Zakładamy, że masz funkcję _get_ai_reply zdefiniowaną w tym pliku lub importowaną
    wynik = _get_ai_reply(system_prompt, historia)

    # 6. Przygotowanie załączników
    # Obrazy statyczne
    images = _load_images_from_list(obraz_lista)
    
    # Obrazy AI (FLUX)
    images_ai = []
    if obrazki_ai > 0:
        # Generujemy prompty i obrazy
        images_ai, _ = _generate_n_flux_images(obrazki_ai, wynik or opis, styl_flux, etap)
        # Kompresja
        images_ai = _compress_images_ai(images_ai)

    # Video
    videos = _load_videos_from_list(video_lista)

    # 7. Składanie finalnej listy obrazów (AI + statyczne)
    final_images = images + images_ai

    return {
        "reply_html": wynik,
        "nowy_etap": etap + 1,
        "images": final_images,
        "videos": videos
    }

# --- FUNKCJE POMOCNICZE (Wymagane do działania powyższego) ---

def _load_style_if_file(styl_raw):
    if not styl_raw: return ""
    if styl_raw.endswith(".txt"):
        path = os.path.join("prompts", styl_raw)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip()
    return styl_raw

def _load_images_from_list(lista_str):
    if not lista_str: return []
    imgs = []
    pliki = [p.strip() for p in lista_str.split(",") if p.strip()]
    for p in pliki:
        path = os.path.join("media", "images", "niebo", p)
        if os.path.exists(path):
            with open(path, "rb") as f:
                imgs.append({
                    "base64": base64.b64encode(f.read()).decode('utf-8'),
                    "filename": p,
                    "content_type": "image/png"
                })
    return imgs

def _load_videos_from_list(lista_str):
    if not lista_str: return []
    vids = []
    pliki = [p.strip() for p in lista_str.split(",") if p.strip()]
    for p in pliki:
        path = os.path.join("media", "mp4", "niebo", p)
        if os.path.exists(path):
            with open(path, "rb") as f:
                vids.append({
                    "base64": base64.b64encode(f.read()).decode('utf-8'),
                    "filename": p,
                    "content_type": "video/mp4"
                })
    return vids

# Tutaj powinny znajdować się Twoje funkcje: 
# _get_ai_reply, _generate_n_flux_images, _compress_images_ai itd.
# (zostawiam je w Twojej oryginalnej wersji, powyżej poprawiłem serce logiki CSV)