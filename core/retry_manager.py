#!/usr/bin/env python3
"""
core/retry_manager.py
Dekorator retry dla funkcji z błędami.
"""

import time
import functools
from typing import Callable, Any

from core.logging_reporter import get_logger

logger = get_logger()


def retry_on_failure(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    Dekorator retry dla funkcji.
    Retry przy wyjątkach, z wykładniczym backoff.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"Próba {attempt + 1}/{max_retries + 1} funkcji {func.__name__} "
                            f"nie powiodła się: {e}. Retry za {current_delay}s"
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(
                            f"Wszystkie {max_retries + 1} prób funkcji {func.__name__} "
                            f"nie powiodły się. Ostatni błąd: {e}"
                        )

            raise last_exception

        return wrapper

    return decorator
