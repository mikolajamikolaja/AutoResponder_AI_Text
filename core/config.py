"""
core/config.py
Centralna konfiguracja dla responders/zwykly.py.
Inne respondery (biznes.py, smierc.py itd.) mają własne stałe w swoich plikach.

Aby zmienić ile znaków emaila trafia do AI — zmień MAX_DLUGOSC_EMAIL.
Najlszepszy byłby model Groq llama-3.3-70b-versatile: limit ~128 000 tokenów (~500 000 znaków) Tymczasowo daje gorszy model : llama-3.1-8b-instant.
"""

# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNA STAŁA — limit długości emaila przekazywanego do AI
# Zmień tutaj aby sterować dla całego zwykly.py naraz.
# ─────────────────────────────────────────────────────────────────────────────
MAX_DLUGOSC_EMAIL = 7000

# ─────────────────────────────────────────────────────────────────────────────
# DEEPSEEK API
# ─────────────────────────────────────────────────────────────────────────────
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL   = "deepseek-chat"

# ─────────────────────────────────────────────────────────────────────────────
# HUGGING FACE / FLUX
# ─────────────────────────────────────────────────────────────────────────────
HF_API_URL        = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
HF_STEPS          = 1
HF_GUIDANCE       = 1
HF_TIMEOUT        = 55
TYLER_JPG_QUALITY = 95  # Kompresja JPG paneli tryptyku (95% = minimalna strata)

# Globalna blacklist wyczerpanych HF tokenów (dodawać tokeny które zwróciły 402)
HF_TOKEN_BLACKLIST = set([
    "HF_TOKEN", "HF_TOKEN1", "HF_TOKEN2", "HF_TOKEN3", "HF_TOKEN4",
    "HF_TOKEN5", "HF_TOKEN6", "HF_TOKEN7", "HF_TOKEN8", "HF_TOKEN9",
    "HF_TOKEN10", "HF_TOKEN11", "HF_TOKEN12", "HF_TOKEN13", "HF_TOKEN14",
    "HF_TOKEN15", "HF_TOKEN16", "HF_TOKEN17",
    # Dodawać tutaj wyczerpane tokeny, np. "HF_TOKEN", "HF_TOKEN1" itp.
])

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
