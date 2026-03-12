import os
import base64
import random
import pandas as pd
from flask import current_app

# --- KONFIGURACJA ---
DEFAULT_SYSTEM_PROMPT = "Jesteś Pawłem, piszesz z zaświatów. Ton absurdalny, krótko."
XLSX_PATH = os.path.join("prompts", "requiem_etapy.xlsx")


def _load_config_xlsx():
    """Wczytuje etapy i style z zakładek pliku requiem_etapy.xlsx."""
    etapy_data = {}
    style_data = {}

    if not os.path.exists(XLSX_PATH):
        current_app.logger.error(f"[smierc] Brak pliku: {XLSX_PATH}")
        return etapy_data, style_data

    try:
        sheets = pd.read_excel(XLSX_PATH, sheet_name=None, dtype=str)

        # Zakładka: etapy
        df_etapy = sheets.get("etapy")
        if df_etapy is not None:
            df_etapy = df_etapy.where(pd.notna(df_etapy), "")
            for _, row in df_etapy.iterrows():
                try:
                    e_id = int(row["etap"])
                    etapy_data[e_id] = row.to_dict()
                except (ValueError, KeyError):
                    continue
        else:
            current_app.logger.warning("[smierc] Brak zakładki 'etapy' w pliku xlsx.")

        # Zakładka: style
        df_style = sheets.get("style")
        if df_style is not None:
            df_style = df_style.where(pd.notna(df_style), "")
            for _, row in df_style.iterrows():
                try:
                    e_id = int(row["etap"])
                    style_data[e_id] = row.to_dict()
                except (ValueError, KeyError):
                    continue
        else:
            current_app.logger.warning("[smierc] Brak zakładki 'style' w pliku xlsx.")

    except Exception as e:
        current_app.logger.error(f"[smierc] Błąd czytania xlsx: {e}")

    return etapy_data, style_data


def build_smierc_section(
    sender_email,
    data=None,
    etap=1,
    data_smierci_str="nieznanego dnia",
    historia="",
    **kwargs
):
    """
    Główna funkcja budująca sekcję SMIERC.

    Obsługuje dwa sposoby wywołania:
      A) Z app.py — argumenty jako osobne kwargs:
         build_smierc_section(sender_email=..., etap=..., data_smierci_str=..., historia=..., body=...)

      B) Stary styl — argumenty spakowane w słowniku data={}:
         build_smierc_section(sender_email=..., data={'etap':..., 'data_smierci':..., 'historia':...})
    """
    if data is not None:
        etap             = int(data.get("etap",         etap))
        data_smierci_str = data.get("data_smierci",     data_smierci_str)
        historia         = data.get("historia",         historia)
    else:
        etap = int(etap)

    # 1. Ładowanie konfiguracji z xlsx
    etapy_dict, style_dict = _load_config_xlsx()

    # 2. Pobieranie danych dla konkretnego etapu
    row   = etapy_dict.get(etap, {})
    s_row = style_dict.get(etap, {})

    if not row:
        opis                   = "Błądzenie w antymaterii"
        obraz_lista            = ""
        video_lista            = ""
        obrazki_ai             = 1
        system_prompt_template = DEFAULT_SYSTEM_PROMPT
    else:
        opis        = row.get("opis",  "")
        obraz_lista = row.get("obraz", "")
        video_lista = row.get("video", "")

        # Solidne parsowanie liczby obrazków AI (obsługa float z xlsx np. "1.0")
        val_ai = str(row.get("obrazki_ai", "0")).strip()
        try:
            obrazki_ai = int(float(val_ai)) if val_ai not in ("", "nan") else 0
        except (ValueError, TypeError):
            obrazki_ai = 0

        system_prompt_template = row.get("system_prompt") or DEFAULT_SYSTEM_PROMPT

    # 3. Personalizacja system promptu
    system_prompt = system_prompt_template.replace("{data_smierci_str}", data_smierci_str)

    # 4. Pobranie stylu wizualnego dla FLUX
    styl_flux_raw = s_row.get("styl", "")
    styl_flux     = _load_style_content(styl_flux_raw)

    # 5. Generowanie odpowiedzi tekstowej (AI)
    wynik = _get_ai_reply(system_prompt, historia)

    # 6. Obrazy statyczne z dysku
    images_static = _load_images_base64(obraz_lista)

    # 7. Generowanie obrazów AI (FLUX)
    images_ai = []
    if obrazki_ai > 0:
        current_app.logger.info(f"[smierc] Generowanie {obrazki_ai} obrazów AI dla etapu {etap}")
        images_ai, _ = _generate_n_flux_images(obrazki_ai, wynik or opis, styl_flux, etap)

    # 8. Wideo
    videos = _load_videos_base64(video_lista)

    current_app.logger.info(
        f"[smierc] Etap {etap}: Wysyłam {len(images_static + images_ai)} obrazów i {len(videos)} wideo."
    )

    return {
        "reply_html": wynik,
        "nowy_etap":  etap + 1,
        "images":     images_static + images_ai,
        "videos":     videos,
    }


# --- KONIEC GŁÓWNEJ LOGIKI ---
# Upewnij się, że poniżej w pliku smierc.py masz swoje funkcje:
# _get_ai_reply, _generate_n_flux_images, _load_style_content, _load_images_base64, _load_videos_base64
