#!/usr/bin/env python3
"""
tests/test_config.py
Testy konfiguracji responderów.
"""

import json
import pytest
from core.responder_manager import ResponderManager, PipelineBuilder


class TestResponderManager:
    """Testy ResponderManager."""

    def test_load_config(self):
        """Test ładowania konfiguracji."""
        manager = ResponderManager()
        assert manager.config is not None
        assert "responders" in manager.config
        assert "keyword_mappings" in manager.config

    def test_get_responder_config(self):
        """Test pobierania konfiguracji respondera."""
        manager = ResponderManager()
        config = manager.get_responder_config("zwykly")
        assert config is not None
        assert config["enabled"] is True
        assert config["requires_flux"] is True

    def test_keyword_mapping(self):
        """Test mapowania keywords."""
        manager = ResponderManager()
        responder = manager.get_keyword_mapping("KEYWORDS")
        assert responder == "zwykly"

    def test_section_order(self):
        """Test kolejności sekcji."""
        manager = ResponderManager()
        order = manager.get_section_order()
        assert "nawiazanie" in order
        assert "zwykly" in order


class TestPipelineBuilder:
    """Testy PipelineBuilder."""

    def test_build_sections_basic(self):
        """Test budowania podstawowych sekcji."""
        manager = ResponderManager()
        builder = PipelineBuilder(manager)

        data = {
            "contains_keyword": True,
            "in_history_status": "nie",
            "in_requiem_status": "nie",
        }

        sections = builder.build_sections(data)
        assert "zwykly" in sections

    def test_build_sections_joker(self):
        """Test jokera - wszystkie sekcje."""
        manager = ResponderManager()
        builder = PipelineBuilder(manager)

        data = {
            "contains_keyword_joker": True,
            "in_history_status": "nie",
            "in_requiem_status": "nie",
        }

        sections = builder.build_sections(data)
        # Joker powinien dodać wszystkie włączone respondery
        assert len(sections) > 1

    def test_build_sections_previous_body(self):
        """Test nawiazania przy poprzedniej wiadomości."""
        manager = ResponderManager()
        builder = PipelineBuilder(manager)

        data = {
            "previous_body": "poprzednia wiadomość",
            "in_history_status": "nie",
            "in_requiem_status": "nie",
        }

        sections = builder.build_sections(data)
        assert "nawiazanie" in sections


if __name__ == "__main__":
    pytest.main([__file__])
