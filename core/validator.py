#!/usr/bin/env python3
"""
core/validator.py
Walidacja wejścia (emaile, załączniki, prompty).
"""

import os
from typing import List, Dict, Any, Optional

from core.logging_reporter import get_logger


class Validator:
    """Waliduje dane wejściowe."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.logger = get_logger()

    def validate_email(self, body: str, subject: str, sender: str) -> bool:
        """Waliduje email."""
        if not body or not body.strip():
            self.logger.warning("Pusty body emaila")
            return False

        if len(body) > self.config.get("max_email_length", 10000):
            self.logger.warning(f"Email zbyt długi: {len(body)} znaków")
            return False

        if not sender or "@" not in sender:
            self.logger.warning(f"Nieprawidłowy sender: {sender}")
            return False

        return True

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
