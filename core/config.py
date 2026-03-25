"""
core/config.py
Centralna konfiguracja dla responders/zwykly.py.
Inne respondery (biznes.py, smierc.py itd.) mają własne stałe w swoich plikach.

Aby zmienić ile znaków emaila trafia do AI — zmień MAX_DLUGOSC_EMAIL.
Groq llama-3.3-70b-versatile: limit ~128 000 tokenów (~500 000 znaków).
"""

# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA STAŁA — limit długości emaila przekazywanego do AI
# Zmień tutaj aby sterować dla całego zwykly.py naraz.
# ─────────────────────────────────────────────────────────────────────────────
MAX_DLUGOSC_EMAIL = 7000

# ─────────────────────────────────────────────────────────────────────────────
# GROQ API
# ─────────────────────────────────────────────────────────────────────────────
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"

# ─────────────────────────────────────────────────────────────────────────────
# HUGGING FACE / FLUX
# ─────────────────────────────────────────────────────────────────────────────
HF_API_URL        = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS          = 3
HF_GUIDANCE       = 3
HF_TIMEOUT        = 55
TYLER_JPG_QUALITY = 95  # Kompresja JPG paneli tryptyku (95% = minimalna strata)

# ─────────────────────────────────────────────────────────────────────────────
# MAPOWANIE EMOCJI → NAZWY PLIKÓW
# ─────────────────────────────────────────────────────────────────────────────
EMOCJA_MAP = {
    "radosc": "twarz_radosc",
    "smutek": "twarz_smutek",
    "zlosc":  "twarz_zlosc",
    "lek":    "twarz_lek",
    "nuda":   "twarz_nuda",
    "spokoj": "twarz_spokoj",
}
FALLBACK_EMOT = "error"
