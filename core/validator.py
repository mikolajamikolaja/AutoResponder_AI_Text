#!/usr/bin/env python3
"""
core/validator.py
Walidacja wejścia (emaile, załączniki, prompty).
"""

import os
from typing import List, Dict, Any, Optional, Tuple

from core.logging_reporter import get_logger


class Validator:
    """Waliduje dane wejściowe."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.logger = get_logger()

    def validate_email(self, sender: str, subject: str, body: str) -> Tuple[bool, str]:
        """
        Waliduje email.
        Zwraca: (is_valid, error_message)
        """
        if not sender or "@" not in sender:
            msg = f"Nieprawidłowy sender: {sender}"
            self.logger.warning(msg)
            return False, msg

        if not body or not body.strip():
            msg = "Pusty body emaila"
            self.logger.warning(msg)
            return False, msg

        max_len = self.config.get("max_email_length", 10000)
        if len(body) > max_len:
            msg = f"Email zbyt długi: {len(body)} znaków (limit: {max_len})"
            self.logger.warning(msg)
            return False, msg

        return True, ""

    def validate_attachments(
        self, attachments: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Waliduje i filtruje załączniki."""
        valid = []
        allowed_types = self.config.get("allowed_attachment_types", [])
        max_size = self.config.get("max_attachment_size", 10485760)  # 10MB

        for att in attachments:
            if att.get("size", 0) > max_size:
                self.logger.warning(
                    f"Załącznik zbyt duży: {att.get('filename')} ({att['size']} bytes)"
                )
                continue

            content_type = att.get("content_type", "").lower()
            if allowed_types and content_type not in allowed_types:
                self.logger.warning(f"Niedozwolony typ załącznika: {content_type}")
                continue

            valid.append(att)

        return valid

    def validate_prompt(self, prompt_data: Dict[str, Any]) -> bool:
        """Waliduje dane promptu."""
        required_keys = ["system", "output_schema"]
        for key in required_keys:
            if key not in prompt_data:
                self.logger.error(f"Brak wymaganej klucza w prompcie: {key}")
                return False
        return True

    def sanitize_input(self, text: str) -> str:
        """Sanityzuje tekst wejściowy."""
        # Usuwa potencjalnie niebezpieczne znaki
        return text.replace("\x00", "").strip()
