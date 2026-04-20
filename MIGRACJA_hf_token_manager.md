# Migracja do `core/hf_token_manager.py`

## Co się zmienia

Zamiast każdy responder trzymał własny `_HF_DEAD_TOKENS` i `_get_hf_tokens()`,
teraz jest **jeden centralny obiekt** `hf_tokens` współdzielony przez cały proces.

Przy pierwszym użyciu wykonuje się **jednorazowy warm-up**: lekkie sprawdzenie
każdego tokenu przez `GET /api/whoami` na HuggingFace — **bez generowania obrazka,
bez zużywania kredytów generatywnych**. Wynik jest cache'owany przez całą sesję.

Jeśli token zwróci 402/401/403 podczas generowania — responder wywołuje `mark_dead(name)`
i singleton natychmiast usuwa token z aktywnej listy dla wszystkich responderów jednocześnie.

---

## Krok 1 — Skopiuj plik

Umieść `hf_token_manager.py` w katalogu `core/`:

```
core/
  hf_token_manager.py   ← NOWY PLIK
  ai_client.py
  config.py
  ...
```

---

## Krok 2 — Migracja `zwykly.py`

### Usuń (lub zakomentuj):

```python
# USUŃ te linie (ok. linia 1295-1310):
_HF_DEAD_TOKENS: set = HF_TOKEN_BLACKLIST.copy()

def _get_hf_tokens() -> list:
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(40)]
    all_tokens = [(n, v) for n in names if (v := os.getenv(n, "").strip())]
    active = [(n, v) for n, v in all_tokens if n not in _HF_DEAD_TOKENS]
    dead_count = len(all_tokens) - len(active)
    if dead_count:
        logger.debug("...")
    return active
```

### Dodaj import:

```python
from core.hf_token_manager import get_active_tokens, mark_dead, mark_remaining
```

### Zamień wywołania w `_generate_flux_image()`:

```python
# PRZED:
tokens = _get_hf_tokens()
...
_HF_DEAD_TOKENS.add(name)

# PO:
tokens = get_active_tokens()
...
mark_dead(name, reason="402")        # przy HTTP 402
mark_dead(name, reason="401/403")    # przy HTTP 401/403
```

### Zamień aktualizację pozostałych requestów:

```python
# W miejscu gdzie czytasz nagłówek X-Remaining-Requests:
remaining = resp.headers.get("X-Remaining-Requests")
if remaining:
    mark_remaining(name, int(remaining))  # DODAJ TĘ LINIĘ
```

### Sprawdzanie all-dead przed pętlą:

```python
# PRZED pętlą for name, token in tokens: — dodaj:
from core.hf_token_manager import hf_tokens
if hf_tokens.all_dead():
    logger.warning("[flux-tyler] Wszystkie tokeny martwe — pomijam generowanie")
    return None
```

---

## Krok 3 — Migracja `smierc.py`

Te same zmiany co w `zwykly.py`:

```python
# USUŃ:
_HF_DEAD_TOKENS: set[str] = HF_TOKEN_BLACKLIST.copy()

def _get_hf_tokens() -> list:
    names = [f"HF_TOKEN{i}" if i else "HF_TOKEN" for i in range(21)]
    return [
        (n, v)
        for n in names
        if n not in _HF_DEAD_TOKENS and (v := os.getenv(n, "").strip())
    ]

# DODAJ import:
from core.hf_token_manager import get_active_tokens, mark_dead, mark_remaining

# ZAMIEŃ wywołania:
tokens = _get_hf_tokens()           →  tokens = get_active_tokens()
_HF_DEAD_TOKENS.add(name)          →  mark_dead(name, reason="...")
```

---

## Krok 4 — Migracja `dociekliwy.py` (jeśli używa FLUX)

Identycznie jak wyżej.

---

## Krok 5 — Opcjonalny endpoint diagnostyczny w `app.py`

Dodaj do `app.py` endpoint do podglądu stanu tokenów:

```python
from core.hf_token_manager import hf_tokens

@app.route("/admin/hf-status")
def hf_status():
    """Diagnostyka stanu tokenów HF — tylko do debugowania."""
    return jsonify({
        "warmed_up": hf_tokens._warmed_up,
        "tokens":    hf_tokens.status_report(),
    })

@app.route("/admin/hf-reset", methods=["POST"])
def hf_reset():
    """Resetuje cache tokenów — ponowny warm-up przy następnym żądaniu."""
    hf_tokens.reset()
    return jsonify({"status": "ok", "message": "Warm-up zostanie powtórzony przy następnym żądaniu"})
```

---

## Krok 6 — Wyzwolenie warm-upu przy starcie (opcjonalne, zalecane)

W `app.py`, po `app = Flask(__name__)`, dodaj:

```python
# Warm-up tokenów HF przy starcie serwera (nie czekaj na pierwsze żądanie)
with app.app_context():
    from core.hf_token_manager import hf_tokens
    hf_tokens.warmup()
```

Dzięki temu serwer sprawdzi tokeny od razu przy starcie, a pierwsze żądanie
nie będzie czekać na warm-up.

---

## Podsumowanie zmian

| Przed | Po |
|---|---|
| Każdy responder: własny `_HF_DEAD_TOKENS` | Jeden singleton `hf_tokens` |
| Sprawdzanie tokenu = generowanie obrazka | Sprawdzanie = lekki GET whoami (bez kredytów) |
| Martwy token w `zwykly` nadal próbowany w `smierc` | Martwy token = martwy wszędzie |
| Warm-up przy każdym żądaniu (ukryty) | Warm-up raz przy starcie sesji |
| Brak diagnostyki | Endpoint `/admin/hf-status` |
