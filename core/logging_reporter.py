"""
core/logging_reporter.py

Moduł do szczegółowego logowania przebiegu programu.
Tworzy log.txt z przebiegiem rozumowania i decyzji programu.
Wysyła logi na Google Drive dla programisty.
"""

import os
import json
import time
import base64
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials
    from google.api_errors import HttpError
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    GOOGLE_DRIVE_AVAILABLE = True
except ImportError:
    GOOGLE_DRIVE_AVAILABLE = False


class ExecutionLogger:
    """Rejestruje szczegółowy przebieg wykonania programu."""

    def __init__(
        self,
        output_dir: str = "logs",
        session_id: str = "",
        upload_to_drive: bool = True,
    ):
        self.output_dir = output_dir
        self.entries: List[Dict[str, Any]] = []
        self.start_time = time.time()
        self.start_datetime = datetime.now()
        self.session_id = session_id or self.start_datetime.strftime("%Y%m%d_%H%M%S")
        self.metadata: Dict[str, Any] = {}  # Metadane sesji
        self.upload_to_drive = upload_to_drive

        # Logger dla debugowania samego systemu logowania
        self.logger = logging.getLogger(__name__)

        # Utwórz katalog jeśli nie istnieje
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # Typ nazwy pliku: log_{session_id}.txt
        self.log_file = os.path.join(output_dir, f"log_{self.session_id}.txt")
        self._write_header()

        # Inicjalizuj Google Drive jeśli dostępne
        self.drive_service = None
        self.drive_folder_id = None
        if self.upload_to_drive and GOOGLE_DRIVE_AVAILABLE:
            self._init_google_drive()

    def _write_header(self):
        """Wpisz nagłówek loga."""
        with open(self.log_file, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"RAPORT WYKONANIA PROGRAMU\n")
            f.write(f"Data/Czas: {self.start_datetime.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")

    def _init_google_drive(self):
        """Inicjalizuj połączenie z Google Drive."""
        try:
            # Ścieżka do service account key (zakładamy że jest w env lub pliku)
            service_account_key = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")
            if not service_account_key:
                # Spróbuj przeczytać z pliku
                key_file = os.path.join(
                    os.path.dirname(__file__), "..", "service_account.json"
                )
                if os.path.exists(key_file):
                    with open(key_file, "r") as f:
                        service_account_key = f.read()

            if service_account_key:
                # Decode base64 jeśli potrzebne
                try:
                    service_account_key = base64.b64decode(service_account_key).decode(
                        "utf-8"
                    )
                except:
                    pass  # Może już jest plain JSON

                creds_dict = json.loads(service_account_key)
                creds = Credentials.from_service_account_info(
                    creds_dict, scopes=["https://www.googleapis.com/auth/drive.file"]
                )

                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())

                self.drive_service = build("drive", "v3", credentials=creds)

                # Znajdź lub utwórz folder "AutoResponder_Logs"
                self.drive_folder_id = self._get_or_create_logs_folder()
                self.logger.info("[LOGGER] ✓ Google Drive połączony dla uploadów logów")

        except Exception as e:
            self.logger.warning(
                f"[LOGGER] ⚠️ Nie udało się połączyć z Google Drive: {e}"
            )
            self.drive_service = None

    def _get_or_create_logs_folder(self):
        """Znajdź folder AutoResponder_Logs lub go utwórz."""
        if not self.drive_service:
            return None

        try:
            # Szukaj istniejącego folderu
            query = "name='AutoResponder_Logs' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = (
                self.drive_service.files()
                .list(q=query, spaces="drive", fields="files(id, name)")
                .execute()
            )
            items = results.get("files", [])

            if items:
                return items[0]["id"]
            else:
                # Utwórz nowy folder
                folder_metadata = {
                    "name": "AutoResponder_Logs",
                    "mimeType": "application/vnd.google-apps.folder",
                }
                folder = (
                    self.drive_service.files()
                    .create(body=folder_metadata, fields="id")
                    .execute()
                )
                return folder.get("id")

        except Exception as e:
            self.logger.error(f"[LOGGER] Błąd podczas tworzenia folderu na Drive: {e}")
            return None

    def upload_log_to_drive(self):
        """Wyślij plik loga na Google Drive."""
        if not self.drive_service or not self.drive_folder_id:
            self.logger.warning("[LOGGER] Google Drive niedostępny - pomijam upload")
            return False

        try:
            file_metadata = {
                "name": f"log_{self.session_id}.txt",
                "parents": [self.drive_folder_id],
            }

            media = MediaFileUpload(
                self.log_file, mimetype="text/plain", resumable=True
            )

            file = (
                self.drive_service.files()
                .create(body=file_metadata, media_body=media, fields="id")
                .execute()
            )

            file_id = file.get("id")
            self.logger.info(
                f"[LOGGER] ✓ Log wysłany na Google Drive: https://drive.google.com/file/d/{file_id}/view"
            )
            return True

        except Exception as e:
            self.logger.error(f"[LOGGER] ❌ Błąd podczas wysyłania na Drive: {e}")
            return False

    def log_input(self, sender: str, subject: str, body: str, sender_name: str = ""):
        """Zarejestruj oryginalne dane wejściowe."""
        self._append_log(
            "INPUT",
            {
                "sender": sender,
                "sender_name": sender_name,
                "subject": subject,
                "body_length": len(body) if body else 0,
                "body_preview": (
                    (body[:200] + "...") if body and len(body) > 200 else body
                ),
            },
        )

    def log_variables_detected(self, variables: Dict[str, Any]):
        """Zarejestruj wykryte zmienne."""
        self._append_log("VARIABLES_DETECTED", variables)

    def set_metadata(self, key: str, value: Any):
        """Ustaw metadaną sesji."""
        self.metadata[key] = value

    def log_step(
        self, step_name: str, details: Dict[str, Any] = None, status: str = "running"
    ):
        """Zarejestruj krok procesu."""
        entry = {
            "step": step_name,
            "status": status,
            "timestamp": time.time() - self.start_time,
        }
        if details:
            entry.update(details)
        self._append_log("STEP", entry)

    def log_api_call(
        self,
        api_name: str,
        model: str = "",
        tokens_used: int = 0,
        duration_sec: float = 0,
        success: bool = True,
        error: str = "",
    ):
        """Zarejestruj wywołanie API."""
        entry = {
            "api": api_name,
            "model": model,
            "tokens_used": tokens_used,
            "duration_sec": duration_sec,
            "success": success,
        }
        if error:
            entry["error"] = error
        self._append_log("API_CALL", entry)

    def log_decision(
        self, decision_name: str, condition: str, result: Any, reason: str = ""
    ):
        """Zarejestruj decyzję warunkową."""
        self._append_log(
            "DECISION",
            {
                "decision": decision_name,
                "condition": condition,
                "result": result,
                "reason": reason,
            },
        )

    def log_error(
        self,
        error_type: str,
        message: str,
        traceback_str: str = "",
        recoverable: bool = True,
    ):
        """Zarejestruj błąd."""
        self._append_log(
            "ERROR",
            {
                "error_type": error_type,
                "message": message,
                "recoverable": recoverable,
                "traceback": traceback_str[:500] if traceback_str else "",
            },
        )

    def log_section_result(
        self, section_name: str, success: bool = True, details: Dict[str, Any] = None
    ):
        """Zarejestruj wynik sekcji responderu."""
        entry = {
            "section": section_name,
            "success": success,
        }
        if details:
            entry.update(details)
        self._append_log("SECTION_RESULT", entry)

    def log_ai_response(
        self,
        ai_name: str,
        prompt: str,
        response: str,
        tokens_used: int = 0,
        duration_sec: float = 0,
    ):
        """Zarejestruj pełną odpowiedź AI dla debugowania."""
        # Skróć bardzo długie odpowiedzi dla czytelności
        response_preview = response[:2000] + "..." if len(response) > 2000 else response
        prompt_preview = prompt[:1000] + "..." if len(prompt) > 1000 else prompt

        self._append_log(
            "AI_RESPONSE",
            {
                "ai_name": ai_name,
                "prompt_length": len(prompt),
                "response_length": len(response),
                "tokens_used": tokens_used,
                "duration_sec": duration_sec,
                "prompt_preview": prompt_preview,
                "response_preview": response_preview,
                "full_response": response,  # Pełna odpowiedź dla analizy
            },
        )

    def log_config_snapshot(self, config_data: Dict[str, Any]):
        """Zarejestruj snapshot konfiguracji."""
        self._append_log("CONFIG_SNAPSHOT", config_data)

    def log_pipeline_step(
        self,
        step_name: str,
        input_data: Any = None,
        output_data: Any = None,
        metadata: Dict[str, Any] = None,
    ):
        """Zarejestruj krok pipeline'u z pełnymi danymi."""
        entry = {"step": step_name}

        if input_data is not None:
            if isinstance(input_data, (dict, list)):
                entry["input"] = input_data
            else:
                entry["input"] = str(input_data)[:500]

        if output_data is not None:
            if isinstance(output_data, (dict, list)):
                entry["output"] = output_data
            else:
                entry["output"] = str(output_data)[:500]

        if metadata:
            entry.update(metadata)

        self._append_log("PIPELINE_STEP", entry)

    def log_memory_usage(self):
        """Zarejestruj użycie pamięci."""
        try:
            import psutil

            process = psutil.Process()
            memory_info = process.memory_info()
            self._append_log(
                "MEMORY_USAGE",
                {
                    "rss_mb": memory_info.rss / 1024 / 1024,
                    "vms_mb": memory_info.vms / 1024 / 1024,
                    "percent": process.memory_percent(),
                },
            )
        except ImportError:
            self._append_log("MEMORY_USAGE", {"error": "psutil not available"})

    def log_file_operation(
        self,
        operation: str,
        file_path: str,
        success: bool,
        size_bytes: int = 0,
        error: str = "",
    ):
        """Zarejestruj operację na pliku."""
        self._append_log(
            "FILE_OPERATION",
            {
                "operation": operation,
                "file_path": file_path,
                "success": success,
                "size_bytes": size_bytes,
                "error": error,
            },
        )

    def log_debug_info(self, category: str, data: Any, level: str = "DEBUG"):
        """Ogólna metoda do logowania informacji debugowania."""
        entry = {"category": category, "level": level}

        if isinstance(data, (dict, list)):
            entry["data"] = data
        else:
            entry["data"] = str(data)

        self._append_log("DEBUG_INFO", entry)

    def log_timing(self, operation: str, duration_sec: float):
        """Zarejestruj czas trwania operacji."""
        self._append_log(
            "TIMING",
            {
                "operation": operation,
                "duration_sec": duration_sec,
            },
        )

    def _append_log(self, log_type: str, data: Dict[str, Any]):
        """Dodaj wpis do logów."""
        entry = {
            "type": log_type,
            "timestamp": time.time() - self.start_time,
            "data": data,
        }
        self.entries.append(entry)
        self._write_entry_to_file(entry)

    def _write_entry_to_file(self, entry: Dict[str, Any]):
        """Wpisz wpis do pliku."""
        timestamp_sec = entry.get("timestamp", 0)
        minutes = int(timestamp_sec) // 60
        seconds = int(timestamp_sec) % 60
        time_str = f"[{minutes:02d}:{seconds:02d}]"

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"{time_str} {entry['type']}\n")
            if entry["data"]:
                for key, value in entry["data"].items():
                    if isinstance(value, (dict, list)):
                        f.write(
                            f"  {key}: {json.dumps(value, ensure_ascii=False, indent=2)}\n"
                        )
                    else:
                        f.write(f"  {key}: {value}\n")
            f.write("\n")

    def finalize(self):
        """Zakończ i wpisz podsumowanie."""
        total_time = time.time() - self.start_time

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write("PODSUMOWANIE\n")
            f.write("=" * 80 + "\n")
            f.write(f"Całkowity czas: {total_time:.2f} sekund\n")
            f.write(f"Liczba wpisów: {len(self.entries)}\n")

            # Policz API calle
            api_calls = [e for e in self.entries if e["type"] == "API_CALL"]
            if api_calls:
                f.write(f"\nWywołania API:\n")
                for api_call in api_calls:
                    api_name = api_call["data"].get("api", "unknown")
                    success = api_call["data"].get("success", False)
                    status = "✓" if success else "✗"
                    f.write(f"  {status} {api_name}\n")

            # Policz błędy
            errors = [e for e in self.entries if e["type"] == "ERROR"]
            if errors:
                f.write(f"\nBłędy ({len(errors)}):\n")
                for error in errors:
                    error_type = error["data"].get("error_type", "unknown")
                    message = error["data"].get("message", "")
                    f.write(f"  ✗ {error_type}: {message}\n")

            f.write("\n" + "=" * 80 + "\n")

        # Wyślij log na Google Drive
        if self.upload_to_drive:
            self.upload_log_to_drive()


# Global logger instance
_global_logger: Optional[ExecutionLogger] = None


def get_logger() -> ExecutionLogger:
    """Pobierz globalny logger."""
    global _global_logger
    if _global_logger is None:
        _global_logger = ExecutionLogger()
    return _global_logger


def init_logger(
    output_dir: str = "logs", session_id: str = "", upload_to_drive: bool = True
) -> ExecutionLogger:
    """Inicjalizuj nowy logger."""
    global _global_logger
    _global_logger = ExecutionLogger(output_dir, session_id, upload_to_drive)
    return _global_logger
