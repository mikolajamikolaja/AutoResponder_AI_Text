"""
core/logging_reporter.py

Moduł do szczegółowego logowania przebiegu programu.
Tworzy log.txt z przebiegiem rozumowania i decyzji programu.
"""

import os
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

class ExecutionLogger:
    """Rejestruje szczegółowy przebieg wykonania programu."""
    
    def __init__(self, output_dir: str = "logs"):
        self.output_dir = output_dir
        self.entries: List[Dict[str, Any]] = []
        self.start_time = time.time()
        self.start_datetime = datetime.now()
        
        # Utwórz katalog jeśli nie istnieje
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        
        self.log_file = os.path.join(output_dir, f"log_{self.start_datetime.strftime('%Y%m%d_%H%M%S')}.txt")
        self._write_header()
    
    def _write_header(self):
        """Wpisz nagłówek loga."""
        with open(self.log_file, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write(f"RAPORT WYKONANIA PROGRAMU\n")
            f.write(f"Data/Czas: {self.start_datetime.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
    
    def log_input(self, sender: str, subject: str, body: str, sender_name: str = ""):
        """Zarejestruj oryginalne dane wejściowe."""
        self._append_log("INPUT", {
            "sender": sender,
            "sender_name": sender_name,
            "subject": subject,
            "body_length": len(body) if body else 0,
            "body_preview": (body[:200] + "...") if body and len(body) > 200 else body,
        })
    
    def log_variables_detected(self, variables: Dict[str, Any]):
        """Zarejestruj wykryte zmienne."""
        self._append_log("VARIABLES_DETECTED", variables)
    
    def log_step(self, step_name: str, details: Dict[str, Any] = None, status: str = "running"):
        """Zarejestruj krok procesu."""
        entry = {
            "step": step_name,
            "status": status,
            "timestamp": time.time() - self.start_time,
        }
        if details:
            entry.update(details)
        self._append_log("STEP", entry)
    
    def log_api_call(self, api_name: str, model: str = "", tokens_used: int = 0, 
                     duration_sec: float = 0, success: bool = True, error: str = ""):
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
    
    def log_decision(self, decision_name: str, condition: str, result: Any, reason: str = ""):
        """Zarejestruj decyzję warunkową."""
        self._append_log("DECISION", {
            "decision": decision_name,
            "condition": condition,
            "result": result,
            "reason": reason,
        })
    
    def log_error(self, error_type: str, message: str, traceback_str: str = "", 
                  recoverable: bool = True):
        """Zarejestruj błąd."""
        self._append_log("ERROR", {
            "error_type": error_type,
            "message": message,
            "recoverable": recoverable,
            "traceback": traceback_str[:500] if traceback_str else "",
        })
    
    def log_section_result(self, section_name: str, success: bool = True, 
                          details: Dict[str, Any] = None):
        """Zarejestruj wynik sekcji responderu."""
        entry = {
            "section": section_name,
            "success": success,
        }
        if details:
            entry.update(details)
        self._append_log("SECTION_RESULT", entry)
    
    def log_timing(self, operation: str, duration_sec: float):
        """Zarejestruj czas trwania operacji."""
        self._append_log("TIMING", {
            "operation": operation,
            "duration_sec": duration_sec,
        })
    
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
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(f"{time_str} {entry['type']}\n")
            if entry['data']:
                for key, value in entry['data'].items():
                    if isinstance(value, (dict, list)):
                        f.write(f"  {key}: {json.dumps(value, ensure_ascii=False, indent=2)}\n")
                    else:
                        f.write(f"  {key}: {value}\n")
            f.write("\n")
    
    def finalize(self):
        """Zakończ i wpisz podsumowanie."""
        total_time = time.time() - self.start_time
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("PODSUMOWANIE\n")
            f.write("=" * 80 + "\n")
            f.write(f"Całkowity czas: {total_time:.2f} sekund\n")
            f.write(f"Liczba wpisów: {len(self.entries)}\n")
            
            # Policz API calle
            api_calls = [e for e in self.entries if e['type'] == 'API_CALL']
            if api_calls:
                f.write(f"\nWywołania API:\n")
                for api_call in api_calls:
                    api_name = api_call['data'].get('api', 'unknown')
                    success = api_call['data'].get('success', False)
                    status = "✓" if success else "✗"
                    f.write(f"  {status} {api_name}\n")
            
            # Policz błędy
            errors = [e for e in self.entries if e['type'] == 'ERROR']
            if errors:
                f.write(f"\nBłędy ({len(errors)}):\n")
                for error in errors:
                    error_type = error['data'].get('error_type', 'unknown')
                    message = error['data'].get('message', '')
                    f.write(f"  ✗ {error_type}: {message}\n")
            
            f.write("\n" + "=" * 80 + "\n")


# Global logger instance
_global_logger: Optional[ExecutionLogger] = None


def get_logger() -> ExecutionLogger:
    """Pobierz globalny logger."""
    global _global_logger
    if _global_logger is None:
        _global_logger = ExecutionLogger()
    return _global_logger


def init_logger(output_dir: str = "logs") -> ExecutionLogger:
    """Inicjalizuj nowy logger."""
    global _global_logger
    _global_logger = ExecutionLogger(output_dir)
    return _global_logger
