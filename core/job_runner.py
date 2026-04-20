#!/usr/bin/env python3
"""
core/job_runner.py
Asynchroniczny pipeline — każda sekcja: wykonaj → wyślij → drive → sheets → del.
"""
import gc
import traceback

from drive_utils import upload_file_to_drive, update_sheet_with_data, save_to_history_sheet
from core.sheets_logger import log_wyslano

# Stała kolejność sekcji — niezależna od GAS
SECTION_ORDER = [
    "nawiazanie",
    "analiza",
    "zwykly",
    "smierc",
    "generator_pdf",
    "biznes",
    "scrabble",
    "emocje",
]


def build_section_order(requested: list) -> list:
    """Zwraca tylko zlecone sekcje w stałej kolejności."""
    return [s for s in SECTION_ORDER if s in requested]


def _upload_drive_item(item: dict, folder_id: str) -> bool:
    if not isinstance(item, dict) or not item.get("base64") or not item.get("filename"):
        return False
    result = upload_file_to_drive(
        item["base64"],
        item["filename"],
        item.get("content_type", "application/octet-stream"),
        folder_id,
    )
    if not result:
        return False
    item["drive_url"] = result["url"]
    item.pop("base64", None)
    return True


def _upload_drive_section_files(section_data: dict, folder_id: str) -> list:
    uploads = []
    if not isinstance(section_data, dict):
        return uploads

    single_fields = [
        "pdf", "emoticon", "cv_pdf", "log_psych", "ankieta_html", "ankieta_pdf",
        "horoskop_pdf", "karta_rpg_pdf", "raport_pdf", "debug_txt", "explanation_txt",
        "plakat_svg", "gra_html", "image", "image2", "prompt1_txt", "prompt2_txt",
    ]
    list_fields = ["triptych", "images", "videos", "docs", "docx_list"]

    for field in single_fields:
        item = section_data.get(field)
        if _upload_drive_item(item, folder_id):
            uploads.append(f"{field}/{item.get('filename')}")

    for field in list_fields:
        arr = section_data.get(field)
        if isinstance(arr, list):
            for item in arr:
                if _upload_drive_item(item, folder_id):
                    uploads.append(f"{field}/{item.get('filename')}")

    return uploads


def run_pipeline_async(flask_app, data: dict, message_id: str, tasks: dict,
                       sender: str, sender_name: str, previous_subject: str,
                       drive_folder_id: str, history_sheet_id: str,
                       smierc_sheet_id: str, save_to_drive: bool,
                       skip_save_to_history: bool, logger,
                       wyslij_fn, zbierz_zalaczniki_fn, get_token_fn):
    """
    Wykonuje sekcje sekwencyjnie w tle (daemon thread).
    Po każdej sekcji: wyślij mail → zapisz Drive → zapisz Sheets → del → gc.
    """
    import os
    from smtp_wysylka import wyslij_odpowiedz, zbierz_zalaczniki_z_response

    with flask_app.app_context():
        ordered_keys = build_section_order(list(tasks.keys()))

        for section_key in ordered_keys:
            fn = tasks.get(section_key)
            if not fn:
                continue

            result = None
            try:
                flask_app.logger.info("[async] START: %s", section_key)
                result = fn()
                flask_app.logger.info("[async] OK:    %s", section_key)
            except Exception as e:
                flask_app.logger.error(
                    "[async] BŁĄD '%s': %s\n%s", section_key, e, traceback.format_exc()
                )
                continue

            if not result:
                continue

            # ── Wyślij email ────────────────────────────────────────────────────
            try:
                _token_refresh(get_token_fn, flask_app, section_key)
                sent = _send_section_email(
                    section_key, result, sender, sender_name, previous_subject,
                    wyslij_odpowiedz, zbierz_zalaczniki_z_response, flask_app, logger
                )
            except Exception as e:
                flask_app.logger.error("[async] Błąd wysyłki '%s': %s", section_key, e)
                sent = False

            # ── Drive ───────────────────────────────────────────────────────────
            if save_to_drive and drive_folder_id:
                try:
                    _upload_drive_section_files(result, drive_folder_id)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd Drive '%s': %s", section_key, e)

            # ── Sheets ──────────────────────────────────────────────────────────
            if history_sheet_id and sent:
                try:
                    from core.sheets_logger import log_wyslano as _log_wyslano
                    reply_html = result.get("reply_html", "") if isinstance(result, dict) else ""
                    _log_wyslano(history_sheet_id, message_id, section_key, reply_html)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd Sheets log '%s': %s", section_key, e)

            # ── Specjalny zapis dla śmierci ─────────────────────────────────────
            if section_key == "smierc" and smierc_sheet_id and isinstance(result, dict):
                try:
                    _update_smierc_sheet(smierc_sheet_id, sender, data, result)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd smierc sheet: %s", e)

            # ── Zwolnij pamięć ──────────────────────────────────────────────────
            del result
            gc.collect()

        # ── Historia nadawcy (raz na końcu) ─────────────────────────────────────
        if history_sheet_id and not skip_save_to_history:
            try:
                body = data.get("body", "")
                subject = data.get("subject", "")
                save_to_history_sheet(history_sheet_id, sender, subject, body)
            except Exception as e:
                flask_app.logger.error("[async] Błąd zapisu historii: %s", e)

        flask_app.logger.info("[async] Pipeline zakończony dla %s", sender)


def _token_refresh(get_token_fn, flask_app, section_key):
    try:
        get_token_fn()
    except RuntimeError as e:
        flask_app.logger.error("[async] Brak tokenu dla '%s': %s", section_key, e)
        raise


def _send_section_email(section_key, result, sender, sender_name, previous_subject,
                        wyslij_odpowiedz_fn, zbierz_fn, flask_app, logger) -> bool:
    if not isinstance(result, dict):
        return False

    email_html = result.get("reply_html", "")
    zal = zbierz_fn({section_key: result})

    if not email_html.strip() and not zal:
        return False

    subject_line = f"Re: {previous_subject or 'Twoja wiadomość'}"
    if section_key == "smierc" and result.get("subject"):
        subject_line = result["subject"]

    success = wyslij_odpowiedz_fn(
        to_email=sender,
        to_name=sender_name,
        subject=subject_line,
        html_body=email_html or "<p>Załączniki w osobnych plikach.</p>",
        zalaczniki=zal,
    )
    if success:
        flask_app.logger.info("[async] ✓ Wysłano: %s → %s", section_key, sender)
    else:
        flask_app.logger.warning("[async] ✗ Nie wysłano: %s", section_key)
    return success


def _update_smierc_sheet(smierc_sheet_id, sender, data, smierc_result):
    import re, html as html_lib

    def strip_html(h):
        if not h:
            return ""
        text = re.sub(r"(?i)<br\s*/?>", "\n", h)
        text = re.sub(r"<[^>]+>", "", text)
        return html_lib.unescape(text).strip()

    if "nowy_etap" not in smierc_result:
        return
    range_name = (
        f"{sender.replace('@', '_').replace('.', '_')}"
        f"!A{smierc_result['nowy_etap'] + 1}"
    )
    values = [[
        smierc_result["nowy_etap"], "",
        data.get("body", "")[:2000],
        strip_html(smierc_result.get("reply_html", ""))[:2000],
        "",
    ]]
    update_sheet_with_data(smierc_sheet_id, range_name, values)
