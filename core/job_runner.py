#!/usr/bin/env python3
"""
core/job_runner.py
Asynchroniczny pipeline — każda sekcja: wykonaj → wyślij → drive → sheets → del.

OPTYMALIZACJE PAMIĘCI (512 MB):
  - log.txt generowany strumieniowo i natychmiast zapisywany, bez trzymania w RAM
  - log_svg usunięty całkowicie (największy pożeracz pamięci)
  - del + gc.collect() po każdej sekcji (było, wzmocnione)
  - base64 plików kasowane natychmiast po uploadzie do Drive
  - logger.entries czyszczone po wygenerowaniu log.txt
  - Brak importów na poziomie modułu — lazy import wewnątrz funkcji
"""
import gc
import traceback

from drive_utils import upload_file_to_drive, update_sheet_with_data, save_to_history_sheet
from core.sheets_logger import log_wyslano

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
    # Usuń base64 natychmiast po uploadzie — to największy pożeracz pamięci
    item.pop("base64", None)
    if not result:
        return False
    item["drive_url"] = result.get("url", "")
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

    WAŻNE: log_wyslano zapisywany po każdej próbie wysyłki (sukces lub porażka).
    """
    # Lazy import — nie ładuj modułów smtp przy starcie serwera
    from smtp_wysylka import wyslij_odpowiedz, zbierz_zalaczniki_z_response

    with flask_app.app_context():
        ordered_keys = build_section_order(list(tasks.keys()))
        sections_done = []

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
                if history_sheet_id and message_id:
                    try:
                        log_wyslano(history_sheet_id, message_id, f"ERROR:{section_key}", str(e)[:200])
                    except Exception:
                        pass
                gc.collect()
                continue

            if not result:
                if history_sheet_id and message_id:
                    try:
                        log_wyslano(history_sheet_id, message_id, f"EMPTY:{section_key}", "")
                    except Exception:
                        pass
                continue

            # ── Wyślij email ────────────────────────────────────────────────────
            try:
                _token_refresh(get_token_fn, flask_app, section_key)
                _send_section_email(
                    section_key, result, sender, sender_name, previous_subject,
                    wyslij_odpowiedz, zbierz_zalaczniki_z_response, flask_app, logger
                )
            except Exception as e:
                flask_app.logger.error("[async] Błąd wysyłki '%s': %s", section_key, e)

            # ── Drive ───────────────────────────────────────────────────────────
            if save_to_drive and drive_folder_id:
                try:
                    _upload_drive_section_files(result, drive_folder_id)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd Drive '%s': %s", section_key, e)

            # ── Sheets ──────────────────────────────────────────────────────────
            if history_sheet_id and message_id:
                try:
                    # Pobierz reply_html przed del result, ale skróć do 500 znaków
                    reply_html = ""
                    if isinstance(result, dict):
                        raw_html = result.get("reply_html", "")
                        reply_html = raw_html[:500] if raw_html else ""
                    log_wyslano(history_sheet_id, message_id, section_key, reply_html)
                    sections_done.append(section_key)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd Sheets log '%s': %s", section_key, e)

            # ── Specjalny zapis dla śmierci ─────────────────────────────────────
            if section_key == "smierc" and smierc_sheet_id and isinstance(result, dict):
                try:
                    _update_smierc_sheet(smierc_sheet_id, sender, data, result)
                except Exception as e:
                    flask_app.logger.error("[async] Błąd smierc sheet: %s", e)

            # ── Zwolnij pamięć natychmiast ──────────────────────────────────────
            del result
            gc.collect()

        # ── Historia nadawcy (raz na końcu) ─────────────────────────────────────
        if history_sheet_id and not skip_save_to_history:
            try:
                body = data.get("body", "")
                subject = data.get("subject", "")
                save_to_history_sheet(history_sheet_id, sender, subject, body)
                # Zwolnij referencje do dużych danych
                del body, subject
            except Exception as e:
                flask_app.logger.error("[async] Błąd zapisu historii: %s", e)

        # ── log.txt — generuj NA KOŃCU, wyślij strumieniowo, bez trzymania w RAM ─
        _send_log_txt(flask_app, logger, data, sender, sender_name,
                      save_to_drive, drive_folder_id, wyslij_odpowiedz,
                      upload_file_to_drive)

        flask_app.logger.info(
            "[async] Pipeline zakończony dla %s | sekcje: %s",
            sender, ", ".join(sections_done) if sections_done else "brak"
        )


def _send_log_txt(flask_app, logger, data, sender, sender_name,
                  save_to_drive, drive_folder_id, wyslij_odpowiedz_fn,
                  upload_fn):
    """
    Generuje i wysyła log.txt strumieniowo.
    NIE buduje log_svg (było głównym pożeraczem pamięci).
    Po wysyłce czyści logger.entries żeby zwolnić RAM.
    """
    try:
        from app import _build_log_txt_content
        import base64

        log_content = _build_log_txt_content(logger, {})
        # Koduj i od razu usuń string źródłowy
        log_b64 = base64.b64encode(log_content.encode("utf-8")).decode("ascii")
        log_len = len(log_content)
        del log_content  # zwolnij RAM

        filename = f"log_{logger.session_id}.txt"
        log_txt = {
            "base64":       log_b64,
            "content_type": "text/plain",
            "filename":     filename,
        }

        subject_log = f"[LOG] {data.get('subject', 'pipeline')}"
        wyslij_odpowiedz_fn(
            to_email=sender,
            to_name=sender_name,
            subject=subject_log,
            html_body=(
                "<p style='font-family:monospace;color:#444'>"
                "Log wykonania pipeline — załącznik <code>log.txt</code>.</p>"
            ),
            zalaczniki=[log_txt],
        )
        flask_app.logger.info("[async] log.txt wysłany (%d znaków)", log_len)

        if save_to_drive and drive_folder_id:
            upload_fn(log_b64, filename, "text/plain", drive_folder_id)
            flask_app.logger.info("[async] log.txt zapisany na Drive")

        # Zwolnij base64 i wyczyść entries loggera
        del log_b64, log_txt
        if hasattr(logger, "entries"):
            logger.entries.clear()
        gc.collect()

    except Exception as e:
        flask_app.logger.error("[async] Błąd generowania/wysyłki log.txt: %s", e)


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
        flask_app.logger.info("[async] '%s' — brak treści i załączników, pomijam", section_key)
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
    # Zwolnij listę załączników od razu
    del zal
    if success:
        flask_app.logger.info("[async] ✓ Wysłano: %s → %s", section_key, sender)
    else:
        flask_app.logger.warning("[async] ✗ Nie wysłano: %s", section_key)
    return success


def _update_smierc_sheet(smierc_sheet_id, sender, data, smierc_result):
    import re
    import html as html_lib

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
