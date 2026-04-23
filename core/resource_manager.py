#!/usr/bin/env python3
"""
core/resource_manager.py
Monitorowanie zasobów systemowych (pamięć, CPU).
"""

import psutil
import gc
import threading
from typing import Dict, Any

from core.logging_reporter import get_logger


class ResourceManager:
    """Monitoruje i zarządza zasobami systemowymi."""

    def __init__(self, memory_threshold_mb: int = 400, max_concurrent: int = 5):
        self.memory_threshold_mb = memory_threshold_mb
        self.max_concurrent = max_concurrent
        self.logger = get_logger()
        self._active_pipelines = 0
        self._lock = threading.Lock()

    def get_memory_usage(self) -> Dict[str, float]:
        """Zwraca użycie pamięci w MB."""
        process = psutil.Process()
        mem_info = process.memory_info()
        return {
            "rss_mb": mem_info.rss / 1024 / 1024,
            "vms_mb": mem_info.vms / 1024 / 1024,
            "percent": process.memory_percent(),
        }

    def is_memory_high(self) -> bool:
        """Sprawdza czy użycie pamięci jest wysokie."""
        mem = self.get_memory_usage()
        return mem["rss_mb"] > self.memory_threshold_mb

    def force_gc(self):
        """Wymusza garbage collection."""
        collected = gc.collect()
        self.logger.info(f"GC: zebrano {collected} obiektów")

    def can_start_pipeline(self) -> bool:
        """Sprawdza czy można uruchomić nowy pipeline."""
        with self._lock:
            return self._active_pipelines < self.max_concurrent

    def pipeline_start(self):
        """Rejestruje rozpoczęcie pipeline'u."""
        with self._lock:
            self._active_pipelines += 1
            self.logger.info(f"Pipeline start: active={self._active_pipelines}")

    def pipeline_end(self):
        """Rejestruje zakończenie pipeline'u."""
        with self._lock:
            self._active_pipelines = max(0, self._active_pipelines - 1)
            self.logger.info(f"Pipeline end: active={self._active_pipelines}")

    def monitor_resources(self):
        """Loguje aktualne użycie zasobów."""
        mem = self.get_memory_usage()
        self.logger.info(
            f"Resources: Memory RSS={mem['rss_mb']:.1f}MB, "
            f"Active pipelines={self._active_pipelines}"
        )

        if self.is_memory_high():
            self.logger.warning("Wysokie użycie pamięci - wymuszam GC")
            self.force_gc()
