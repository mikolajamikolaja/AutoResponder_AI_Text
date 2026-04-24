"""
core/logging_reporter.py

Logi trzymane są W PAMIĘCI (bez zapisu lokalnego — Render free ma read-only FS).
Na końcu sesji (finalize()) log wysyłany jest jako plik .txt do Google Drive
do folderu DRIVE_FOLDER_ID, przez ten sam OAuth co reszta projektu (drive_utils).
"""

import os
import json
import time
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

_mod_logger = logging.getLogger(__name__)


class ExecutionLogger:
    """Rejestruje szczegółowy przebieg wykonania programu."""

    def __init__(
        self,
        output_dir: str = "logs",  # ignorowany — dla kompatybilności
        session_id: str = "",
        upload_to_drive: bool = True,
    ):
        self.entries: List[Dict[str, Any]] = []
        self._log_lines: List[str] = []
        self.start_time = time.time()
        self.start_datetime = datetime.now()
        self.session_id = session_id or self.start_datetime.strftime("%Y%m%d_%H%M%S")
        self.metadata: Dict[str, Any] = {}
        self.upload_to_drive = upload_to_drive

        self._log_lines.append("=" * 80)
        self._log_lines.append("RAPORT WYKONANIA PROGRAMU")
        self._log_lines.append(
            f"Data/Czas: {self.start_datetime.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._log_lines.append("=" * 80)
        self._log_lines.append("")

    # ── Kompatybilność z logging.Logger ────────────────────────────────────────

    def info(self, msg: str):
        _mod_logger.info(msg)

    def error(self, msg: str):
        _mod_logger.error(msg)

    def warning(self, msg: str):
        _mod_logger.warning(msg)

    def debug(self, msg: str):
        _mod_logger.debug(msg)

    # ── Metody logowania domenowego ────────────────────────────────────────────

    def log_input(self, sender: str, subject: str, body: str, sender_name: str = ""):
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
        self._append_log("VARIABLES_DETECTED", variables)

    def set_metadata(self, key: str, value: Any):
        self.metadata[key] = value

    def log_step(
        self, step_name: str, details: Dict[str, Any] = None, status: str = "running"
    ):
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
        entry = {"section": section_name, "success": success}
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
        self._append_log(
            "AI_RESPONSE",
            {
                "ai_name": ai_name,
                "prompt_length": len(prompt),
                "response_length": len(response),
                "tokens_used": tokens_used,
                "duration_sec": duration_sec,
                "prompt_preview": (
                    prompt[:1000] + "..." if len(prompt) > 1000 else prompt
                ),
                "response_preview": (
                    response[:2000] + "..." if len(response) > 2000 else response
                ),
                "full_response": response,
            },
        )

    def log_config_snapshot(self, config_data: Dict[str, Any]):
        self._append_log("CONFIG_SNAPSHOT", config_data)

    def log_pipeline_step(
        self,
        step_name: str,
        input_data: Any = None,
        output_data: Any = None,
        metadata: Dict[str, Any] = None,
    ):
        entry = {"step": step_name}
        if input_data is not None:
            entry["input"] = (
                input_data
                if isinstance(input_data, (dict, list))
                else str(input_data)[:500]
            )
        if output_data is not None:
            entry["output"] = (
                output_data
                if isinstance(output_data, (dict, list))
                else str(output_data)[:500]
            )
        if metadata:
            entry.update(metadata)
        self._append_log("PIPELINE_STEP", entry)

    def log_memory_usage(self):
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

    def log_attachment_generation(
        self,
        section: str,
        attachment_name: str,
        success: bool,
        file_size: int = 0,
        content_type: str = "",
        error: str = "",
    ):
        self._append_log(
            "ATTACHMENT_GENERATION",
            {
                "section": section,
                "attachment_name": attachment_name,
                "success": success,
                "file_size": file_size,
                "content_type": content_type,
                "error": error,
            },
        )

    def log_debug_info(self, category: str, data: Any, level: str = "DEBUG"):
        entry = {"category": category, "level": level}
        entry["data"] = data if isinstance(data, (dict, list)) else str(data)
        self._append_log("DEBUG_INFO", entry)

    def log_timing(self, operation: str, duration_sec: float):
        self._append_log(
            "TIMING", {"operation": operation, "duration_sec": duration_sec}
        )

    # ── Wewnętrzne ─────────────────────────────────────────────────────────────

    def _append_log(self, log_type: str, data: Dict[str, Any]):
        entry = {
            "type": log_type,
            "timestamp": time.time() - self.start_time,
            "data": data,
        }
        self.entries.append(entry)
        self._write_entry_to_buffer(entry)

    def _write_entry_to_buffer(self, entry: Dict[str, Any]):
        ts = entry.get("timestamp", 0)
        time_str = f"[{int(ts)//60:02d}:{int(ts)%60:02d}]"
        self._log_lines.append(f"{time_str} {entry['type']}")
        data = entry.get("data", {})
        if data:
            for key, value in data.items():
                if isinstance(value, (dict, list)):
                    self._log_lines.append(
                        f"  {key}: {json.dumps(value, ensure_ascii=False, indent=2)}"
                    )
                else:
                    self._log_lines.append(f"  {key}: {value}")
        self._log_lines.append("")

    def _build_log_text(self) -> str:
        total_time = time.time() - self.start_time
        lines = list(self._log_lines)
        lines += [
            "=" * 80,
            "PODSUMOWANIE",
            "=" * 80,
            f"Całkowity czas: {total_time:.2f} sekund",
            f"Liczba wpisów: {len(self.entries)}",
        ]

        api_calls = [e for e in self.entries if e["type"] == "API_CALL"]
        if api_calls:
            lines.append("\nWywołania API:")
            for c in api_calls:
                s = "✓" if c["data"].get("success") else "✗"
                lines.append(f"  {s} {c['data'].get('api', 'unknown')}")

        errors = [e for e in self.entries if e["type"] == "ERROR"]
        if errors:
            lines.append(f"\nBłędy ({len(errors)}):")
            for e in errors:
                lines.append(
                    f"  ✗ {e['data'].get('error_type','?')}: {e['data'].get('message','')}"
                )

        lines.append("\n" + "=" * 80)
        return "\n".join(lines)

    def finalize(self):
        """Zakończ sesję i wyślij log jako .txt do DRIVE_FOLDER_ID."""
        log_text = self._build_log_text()

        if not self.upload_to_drive:
            _mod_logger.info("[LOGGER] upload_to_drive=False — pomijam wysyłkę logu")
            return

        drive_folder_id = os.getenv("DRIVE_FOLDER_ID", "").strip()
        if not drive_folder_id:
            _mod_logger.warning(
                "[LOGGER] Brak DRIVE_FOLDER_ID — nie wysyłam logu na Drive"
            )
            return

        try:
            from drive_utils import upload_file_to_drive

            result = upload_file_to_drive(
                file_data=log_text.encode("utf-8"),
                filename=f"log_{self.session_id}.txt",
                mime_type="text/plain",
                folder_id=drive_folder_id,
            )
            if result:
                _mod_logger.info(
                    f"[LOGGER] ✓ Log wysłany na Drive: {result.get('url', result.get('id'))}"
                )
            else:
                _mod_logger.warning("[LOGGER] ⚠️ upload_file_to_drive zwrócił None")
        except Exception as e:
            _mod_logger.warning(f"[LOGGER] ⚠️ Błąd wysyłki logu na Drive: {e}")

    def upload_log_to_drive(self):
        """Alias dla finalize() — kompatybilność wsteczna."""
        self.finalize()


# ── Singleton globalny ─────────────────────────────────────────────────────────

_global_logger: Optional[ExecutionLogger] = None


def get_logger() -> ExecutionLogger:
    global _global_logger
    if _global_logger is None:
        _global_logger = ExecutionLogger()
    return _global_logger


def init_logger(
    output_dir: str = "logs", session_id: str = "", upload_to_drive: bool = True
) -> ExecutionLogger:
    global _global_logger
    _global_logger = ExecutionLogger(output_dir, session_id, upload_to_drive)
    return _global_logger
