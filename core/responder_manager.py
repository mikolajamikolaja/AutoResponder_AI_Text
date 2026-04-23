#!/usr/bin/env python3
"""
core/responder_manager.py
Centralne zarządzanie responderami i budowaniem pipeline'u.
Ładuje konfigurację z config_responders.json.
"""

import json
import os
from typing import Dict, List, Optional, Any

from core.logging_reporter import get_logger


class ResponderManager:
    """Zarządza konfiguracją responderów."""

    def __init__(self, config_path: str = "config_responders.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.logger = get_logger()

    def _load_config(self) -> Dict[str, Any]:
        """Ładuje konfigurację z JSON."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Błąd ładowania konfiguracji: {e}")
            return {}

    def get_responder_config(self, responder_name: str) -> Optional[Dict[str, Any]]:
        """Zwraca konfigurację konkretnego respondera."""
        return self.config.get("responders", {}).get(responder_name)

    def get_keyword_mapping(self, keyword: str) -> Optional[str]:
        """Mapuje keyword na responder."""
        return self.config.get("keyword_mappings", {}).get(keyword)

    def get_section_order(self) -> List[str]:
        """Zwraca kolejność sekcji."""
        return self.config.get("section_order", [])

    def get_condition_mapping(self, condition: str) -> Optional[str]:
        """Mapuje warunek na responder."""
        return self.config.get("conditions", {}).get(condition)

    def is_responder_enabled(self, responder_name: str) -> bool:
        """Sprawdza czy responder jest włączony."""
        config = self.get_responder_config(responder_name)
        return config.get("enabled", False) if config else False

    def requires_flux(self, responder_name: str) -> bool:
        """Sprawdza czy responder wymaga FLUX."""
        config = self.get_responder_config(responder_name)
        return config.get("requires_flux", False) if config else False

    def get_wave(self, responder_name: str) -> int:
        """Zwraca falę wykonania respondera."""
        config = self.get_responder_config(responder_name)
        return config.get("wave", 2) if config else 2


class PipelineBuilder:
    """Buduje listę sekcji do wykonania na podstawie warunków."""

    def __init__(self, responder_manager: ResponderManager):
        self.manager = responder_manager
        self.logger = get_logger()

    def build_sections(self, data: Dict[str, Any]) -> List[str]:
        """
        Buduje listę sekcji na podstawie danych z webhooka.
        data zawiera flagi jak contains_keyword, wants_smierc, etc.
        """
        requested = set()

        # Mapowanie keywords
        keyword_flags = {
            "contains_keyword": "KEYWORDS",
            "contains_keyword1": "KEYWORDS1",
            "contains_keyword2": "KEYWORDS2",
            "contains_keyword3": "KEYWORDS3",
            "contains_keyword4": "KEYWORDS4",
            "contains_keyword_joker": "KEYWORDS_JOKER",
        }

        for flag, keyword in keyword_flags.items():
            if data.get(flag):
                mapped = self.manager.get_keyword_mapping(keyword)
                if mapped == "all":
                    # Joker - wszystkie respondery
                    for resp in self.manager.config.get("responders", {}):
                        if self.manager.is_responder_enabled(resp):
                            requested.add(resp)
                elif mapped and self.manager.is_responder_enabled(mapped):
                    requested.add(mapped)

        # Specjalne warunki
        if data.get("wants_smierc") and self.manager.is_responder_enabled("smierc"):
            requested.add("smierc")

        if data.get("wants_analiza") and self.manager.is_responder_enabled("analiza"):
            requested.add("analiza")

        if data.get("wants_biznes") and self.manager.is_responder_enabled("biznes"):
            requested.add("biznes")

        if data.get("wants_scrabble") and self.manager.is_responder_enabled("scrabble"):
            requested.add("scrabble")

        if data.get("wants_emocje") and self.manager.is_responder_enabled("emocje"):
            requested.add("emocje")

        if data.get("wants_generator_pdf") and self.manager.is_responder_enabled(
            "generator_pdf"
        ):
            requested.add("generator_pdf")

        if data.get("previous_body") and self.manager.is_responder_enabled(
            "nawiazanie"
        ):
            requested.add("nawiazanie")

        # Warunki specjalne
        if data.get("in_history_status") == "tak" and not requested:
            # Znany użytkownik bez keywords - zwykly
            if self.manager.is_responder_enabled("zwykly"):
                requested.add("zwykly")

        if data.get("in_requiem_status") == "tak":
            # Na liście śmierci - smierc zamiast zwykly
            requested.discard("zwykly")
            if self.manager.is_responder_enabled("smierc"):
                requested.add("smierc")

        # Kolejność
        order = self.manager.get_section_order()
        return [s for s in order if s in requested]
