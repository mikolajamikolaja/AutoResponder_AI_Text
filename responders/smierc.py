import os
import csv
import base64
import random
from flask import current_app

# --- KONFIGURACJA ---
DEFAULT_SYSTEM_PROMPT = "Jesteś Pawłem, piszesz z zaświatów. Ton absurdalny, krótko."
ETAPY_CSV_PATH = os.path.join("prompts", "etapy.csv")
STYLE_CSV_PATH = os.path.join("prompts", "style.csv")

def _load_config_csv():
    """Wczytuje etapy i style z plików CSV, które masz w folderze prompts."""
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
        except Exception as e:
            current_app.logger.error(f"[smierc] Błąd czytania etapy.csv: {e}")

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

def build_smierc_section(sender_email, data=None, **kwargs):
    """
    Główna funkcja. 
    **kwargs jest KLUCZOWE - pozwala przyjąć 'body', który wysyła app.py i nie wywalać błędu.
    """
    etap = int(data.get('etap', 1))
    data_smierci_str = data.get('data_smierci', "nieznanego dnia")
    historia = data.get('historia', "")

    # 1. Ładowanie Twoich plików CSV
    etapy_dict, style_dict = _load_config_csv()

    # 2. Pobieranie danych dla etapu
    row = etapy_dict.get(etap, {})
    s_row = style_dict.get(etap, {})

    if not row:
        # Jeśli etapu nie ma w CSV (np. etap 1000)
        opis = "Błądzenie w antymaterii"
        obraz_lista = ""
        video_lista = ""
        obrazki_ai = 1
        system_prompt_template = DEFAULT_SYSTEM_PROMPT
    else:
        opis = row.get('opis', "")
        obraz_lista = row.get('obraz', "")
        video_lista = row.get('video', "")
        
        # Solidne sprawdzanie liczby obrazków AI (rozwiązuje problem z etapem 30)
        val_ai = str(row.get('obrazki_ai', '0')).strip()
        try:
            obrazki_ai = int(val_ai) if val_ai.isdigit() else 0
        except:
            obrazki_ai = 0
            
        system_prompt_template = row.get('system_prompt') or DEFAULT_SYSTEM_PROMPT

    # 3. Personalizacja (data śmierci)
    system_prompt = system_prompt_template.replace("{data_smierci_str}", data_smierci_str)
    
    # 4. Styl wizualny
    styl_flux_raw = s_row.get('styl', "")
    # Zakładamy że masz funkcję pomocniczą do czytania stylu z pliku .txt
    styl_flux = _load_style_content(styl_flux_raw)

    # 5. Generowanie odpowiedzi (używając Twoich istniejących funkcji AI)
    # Wywołujemy Twoją funkcję _get_ai_reply (upewnij się, że jest w tym pliku lub zaimportowana)
    wynik = _get_ai_reply(system_prompt, historia)

    # 6. Przygotowanie mediów
    images_static = _load_images_base64(obraz_lista)
    
    images_ai = []
    if obrazki_ai > 0:
        # Wywołanie Twojego generatora FLUX
        images_ai, _ = _generate_n_flux_images(obrazki_ai, wynik or opis, styl_flux, etap)

    videos = _load_videos_base64(video_lista)

    return {
        "reply_html": wynik,
        "nowy_etap": etap + 1,
        "images": images_static + images_ai,
        "videos": videos
    }

# --- KONIEC GŁÓWNEJ LOGIKI ---
# Upewnij się, że poniżej w pliku smierc.py masz swoje funkcje:
# _get_ai_reply, _generate_n_flux_images, _load_style_content, _load_images_base64 itd.