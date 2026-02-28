"""
responders/emocje.py
Responder KEYWORDS4 — analiza emocjonalno-narracyjna tekstu.

Obsługuje:
- Treść maila (body)
- Załączniki DOCX / PDF / TXT (jako base64 z Apps Script)

Generuje 4 wykresy PNG dla każdego źródła:
- w1_radar_*        – radar makro-cech (worldbuilding/characters/dynamics/style/philosophy)
- w12_srednia_*     – linia średniej ruchomej emocji po akapitach
- w3_kategorie_*    – słupki: sumy wystąpień słów z każdej puli biblioteki
- wK_kolo_*         – wykres kołowy ze wszystkimi wskaźnikami ukierunkowania tekstu

Wyniki:
- reply_html    – HTML z krótkim raportem tekstowym
- images        – lista PNG (base64) do wysłania jako załączniki
"""

import io
import re
import os
import base64
from flask import current_app

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Opcjonalne zależności ──────────────────────────────────────────────────────
try:
    from docx import Document
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

try:
    import snowballstemmer
    _SB = snowballstemmer.stemmer("polish") \
        if "polish" in snowballstemmer.stemmer.languages() else None
except Exception:
    _SB = None

# ── Ścieżka do katalogu biblioteka/ ──────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_DIR  = os.path.join(BASE_DIR, "biblioteka")

# ── Mapowanie plików biblioteki na etykiety ───────────────────────────────────
CATEGORY_FILES = {
    "slowa_akcja.txt":               "akcja",
    "slowa_bliskosc.txt":            "bliskość",
    "slowa_dialog.txt":              "dialog",
    "slowa_dominacja.txt":           "dominacja",
    "slowa_dotyk.txt":               "dotyk",
    "slowa_duma.txt":                "duma",
    "slowa_dystans.txt":             "dystans",
    "slowa_formalne.txt":            "formalne",
    "slowa_intensywnosc_niska.txt":  "intensywność_niska",
    "slowa_intensywnosc_wysoka.txt": "intensywność_wysoka",
    "slowa_moralnosc_negatywna.txt": "moralność_negatywna",
    "slowa_moralnosc_pozytywna.txt": "moralność_pozytywna",
    "slowa_obrzydzenie.txt":         "obrzydzenie",
    "slowa_pewnosc.txt":             "pewność",
    "slowa_prawne_obrona.txt":       "prawne_obrona",
    "slowa_prawne_oskarzenie.txt":   "prawne_oskarżenie",
    "slowa_refleksja.txt":           "refleksja",
    "slowa_sluch.txt":               "słuch",
    "slowa_uleglosc.txt":            "uległość",
    "slowa_watpliwosc.txt":          "wątpliwość",
    "slowa_wspolczucie.txt":         "współczucie",
    "slowa_wzrok.txt":               "wzrok",
    "slowa_zaskoczenie.txt":         "zaskoczenie",
}

# ── Wbudowane listy fallback gdy brak plików ──────────────────────────────────
_DOMYSLNE_POZ = [
    "rado", "szczę", "uśmiech", "ciesz", "zachw", "entuzj", "miło", "koch",
    "sukces", "wygr", "świet", "doskon", "przyjem", "spokoj", "ulga", "energia",
    "radosn", "euforia", "triumf", "miłość", "przyjaź", "śmiech",
]
_DOMYSLNE_NEG = [
    "smut", "ból", "gniew", "wściek", "nienawi", "poraż", "zły", "strac",
    "przeraż", "agres", "frustrac", "depres", "lęk", "strach", "żal",
    "rozczarow", "bezsiln", "pustk", "krzyk", "oskarż",
]

# ── Polskie sufiksy do stemmera fallback ──────────────────────────────────────
_POLISH_SUFFIXES = sorted([
    "owania", "owanie", "owaniach", "owaniom", "owaniami",
    "eniech", "eniem", "eniom", "enia", "enie",
    "ach", "ami", "ego", "emu", "ej", "ie", "ią", "iąc",
    "cie", "ów", "om", "a", "e", "y", "u", "i", "o",
    "ł", "ła", "li", "ły", "sz",
], key=lambda s: -len(s))


# ── Stemmer ───────────────────────────────────────────────────────────────────
def _stem(word: str) -> str:
    w = re.sub(r"[^a-ząćęłńóśżź]", "", word.lower())
    if _SB:
        try:
            return _SB.stemWord(w)
        except Exception:
            pass
    for suf in _POLISH_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _tokenize(text: str):
    return re.findall(r"[A-Za-ząćęłńóśżźĄĆĘŁŃÓŚŻŹ\-]+", text)


# ── Ładowanie biblioteki słów ─────────────────────────────────────────────────
def _load_wordlist(path: str, fallback=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return [
                l.strip() for l in f
                if l.strip() and not l.strip().startswith("#")
            ]
    return fallback or []


def _load_categories() -> dict:
    """Zwraca dict: {etykieta: set_stemów}"""
    cats = {}

    # Pozytywne / negatywne z osobnych plików lub fallback
    for key, fname, fb in [
        ("pozytywne", "slowa_pozytywne.txt", _DOMYSLNE_POZ),
        ("negatywne", "slowa_negatywne.txt", _DOMYSLNE_NEG),
    ]:
        words = _load_wordlist(os.path.join(LIB_DIR, fname), fb)
        cats[key] = {_stem(w) for w in words if w}

    # Pozostałe kategorie z CATEGORY_FILES
    for fname, label in CATEGORY_FILES.items():
        words = _load_wordlist(os.path.join(LIB_DIR, fname), [])
        cats[label] = {_stem(w) for w in words if w}

    return cats


# ── Ekstrakcja tekstu z załączników ──────────────────────────────────────────
def _extract_text_from_bytes(raw_bytes: bytes, name: str) -> str:
    name_lower = (name or "").lower()

    # TXT
    if name_lower.endswith(".txt"):
        for enc in ("utf-8", "cp1250", "latin-1"):
            try:
                return raw_bytes.decode(enc)
            except Exception:
                pass
        return raw_bytes.decode("utf-8", errors="ignore")

    # DOCX
    if name_lower.endswith(".docx") and _HAS_DOCX:
        try:
            doc   = Document(io.BytesIO(raw_bytes))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for tbl in doc.tables:
                for row in tbl.rows:
                    cell_text = " | ".join(
                        c.text.strip() for c in row.cells if c.text.strip()
                    )
                    if cell_text:
                        parts.append(cell_text)
            return "\n\n".join(parts)
        except Exception as e:
            current_app.logger.warning("Błąd DOCX %s: %s", name, e)

    # PDF – pdfplumber, potem pypdf
    if name_lower.endswith(".pdf"):
        text = ""
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
            if text.strip():
                return text
        except Exception:
            pass
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
            return text
        except Exception as e:
            current_app.logger.warning("Błąd PDF %s: %s", name, e)

    return ""


# ── Analiza tekstu ────────────────────────────────────────────────────────────
def _analyze_paragraphs(text: str, cats: dict) -> list:
    """
    Dzieli tekst na akapity i zlicza rdzenie z każdej kategorii.
    Zwraca listę dict: {idx, pos, neg, emotion_score, kat1: n, kat2: n, ...}
    """
    # Podział na akapity (dwa newliny) lub linie
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [l.strip() for l in text.splitlines() if l.strip()]
    if not paragraphs:
        return []

    base_pos = cats.get("pozytywne", set())
    base_neg = cats.get("negatywne", set())

    results = []
    for idx, para in enumerate(paragraphs, start=1):
        tokens = _tokenize(para)
        stems  = [_stem(t) for t in tokens]
        pos    = sum(1 for s in stems if s in base_pos)
        neg    = sum(1 for s in stems if s in base_neg)
        row    = {"idx": idx, "pos": pos, "neg": neg, "emotion_score": pos - neg}
        for label, stemset in cats.items():
            row[label] = sum(1 for s in stems if s in stemset) if stemset else 0
        results.append(row)

    return results


def _aggregate(para_rows: list, cats: dict) -> dict:
    """Sumuje wystąpienia każdej kategorii ze wszystkich akapitów."""
    totals = {label: 0 for label in cats}
    for row in para_rows:
        for label in cats:
            totals[label] = totals.get(label, 0) + row.get(label, 0)
    return totals


def _percentages(totals: dict) -> dict:
    total_all = sum(totals.values())
    if total_all == 0:
        return {k: 0.0 for k in totals}
    return {k: v / total_all * 100.0 for k, v in totals.items()}


# ── Makro-cechy do radaru ─────────────────────────────────────────────────────
def _macro_dimensions(perc: dict) -> dict:
    def g(*names):
        return sum(perc.get(n, 0.0) for n in names)
    return {
        "worldbuilding": g("wzrok", "słuch", "dotyk", "intensywność_niska"),
        "characters":    g("dominacja", "uległość", "bliskość", "dystans",
                           "współczucie", "duma"),
        "dynamics":      g("akcja", "dialog", "intensywność_wysoka"),
        "style":         g("formalne", "refleksja"),
        "philosophy":    g("moralność_pozytywna", "moralność_negatywna",
                           "pewność", "wątpliwość"),
    }


# ── Helpers wykresów ──────────────────────────────────────────────────────────
def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _safe_label(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text)[:40]


# ── W1 – radar makro-cech ─────────────────────────────────────────────────────
def _plot_w1_radar(perc: dict, title: str) -> str:
    dims   = _macro_dimensions(perc)
    labels = list(dims.keys())
    vals   = [dims[k] for k in labels] + [dims[labels[0]]]   # zamknięcie
    angles = np.linspace(0, 2 * np.pi, len(labels) + 1)

    fig = plt.figure(figsize=(7, 7))
    ax  = plt.subplot(111, polar=True)
    ax.plot(angles, vals, linewidth=2, color="steelblue")
    ax.fill(angles, vals, alpha=0.25, color="steelblue")
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_title(f"Profil cech narracyjnych\n{title}", y=1.10, fontsize=11)
    ax.grid(True)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── W12 – średnia ruchoma emocji ──────────────────────────────────────────────
def _plot_w12_srednia(para_rows: list, title: str) -> str:
    if not para_rows:
        return None
    x      = [r["idx"] for r in para_rows]
    scores = [r["emotion_score"] for r in para_rows]
    window = min(5, len(scores))

    # prosta średnia ruchoma
    ma = []
    for i in range(len(scores)):
        start = max(0, i - window + 1)
        ma.append(sum(scores[start:i + 1]) / (i - start + 1))

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x, ma, color="steelblue", linewidth=2, label="Śr. ruchoma")
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.fill_between(x, ma, 0,
                    where=[v >= 0 for v in ma],
                    alpha=0.3, color="tab:green", label="Pozytywne")
    ax.fill_between(x, ma, 0,
                    where=[v < 0 for v in ma],
                    alpha=0.3, color="tab:red", label="Negatywne")
    ax.set_xlabel("Akapit")
    ax.set_ylabel(f"Śr. ruchoma emocji (okno={window})")
    ax.set_title(f"Przebieg emocji\n{title}", fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── W3 – sumy wystąpień ze wszystkich kategorii biblioteki ───────────────────
def _plot_w3_kategorie(totals: dict, title: str) -> str:
    items  = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    labels = [k for k, _ in items]
    vals   = [v for _, v in items]

    if not any(v > 0 for v in vals):
        return None

    n      = len(labels)
    colors = plt.cm.tab20(np.linspace(0, 1, min(n, 20)))
    if n > 20:
        colors = list(colors) + list(plt.cm.tab20b(np.linspace(0, 1, n - 20)))

    fig, ax = plt.subplots(figsize=(13, 6))
    bars = ax.bar(range(n), vals, color=colors[:n])
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Suma wystąpień")
    ax.set_title(f"Sumy słów z każdej puli biblioteki\n{title}", fontsize=11)

    for bar, val in zip(bars, vals):
        if val > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.1,
                str(int(val)),
                ha="center", va="bottom", fontsize=7,
            )
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── WK – kołowy wykres ukierunkowania tekstu (wszystkie wskaźniki) ────────────
def _plot_wK_kolo(perc: dict, title: str) -> str:
    items = [(k, v) for k, v in perc.items() if v > 0.01]
    if not items:
        return None
    items = sorted(items, key=lambda kv: kv[1], reverse=True)

    labels = [k for k, _ in items]
    vals   = [v for _, v in items]
    n      = len(labels)

    # Kolory – łączymy dwie palety żeby starczyło na 25 kategorii
    cmap1  = list(plt.cm.tab20(np.linspace(0, 1, min(n, 20))))
    cmap2  = list(plt.cm.tab20b(np.linspace(0, 1, max(0, n - 20))))
    colors = (cmap1 + cmap2)[:n]

    fig, ax = plt.subplots(figsize=(10, 10))
    wedges, texts, autotexts = ax.pie(
        vals,
        labels=None,
        colors=colors,
        autopct=lambda p: f"{p:.1f}%" if p > 2.5 else "",
        startangle=90,
        pctdistance=0.80,
        wedgeprops={"linewidth": 0.5, "edgecolor": "white"},
    )
    for at in autotexts:
        at.set_fontsize(7)

    # Legenda z nazwami i procentami
    legend_labels = [f"{lbl}  ({v:.1f}%)" for lbl, v in zip(labels, vals)]
    ax.legend(
        wedges, legend_labels,
        title="Wskaźniki", title_fontsize=9,
        loc="center left", bbox_to_anchor=(1.0, 0.5),
        fontsize=8,
    )

    # Dominujący kierunek w centrum
    ax.text(0, 0, f"▶ {labels[0]}",
            ha="center", va="center",
            fontsize=12, fontweight="bold", color="#222222")

    ax.set_title(f"Ukierunkowanie tekstu – wszystkie wskaźniki\n{title}",
                 fontsize=12)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── Raport HTML ───────────────────────────────────────────────────────────────
def _build_report_html(label: str, totals: dict, perc: dict,
                       para_count: int) -> str:
    total_words = sum(totals.values())
    top3 = sorted(perc.items(), key=lambda kv: kv[1], reverse=True)[:3]
    top3_str = ", ".join(f"<b>{k}</b> ({v:.1f}%)" for k, v in top3)

    pos_p  = perc.get("pozytywne", 0.0)
    neg_p  = perc.get("negatywne", 0.0)
    bilans = ("pozytywne" if pos_p > neg_p
              else "negatywne" if neg_p > pos_p
              else "neutralne")

    return (
        f"<p><strong>Analiza emocjonalna: {label}</strong></p>"
        f"<p>Akapitów: {para_count} | Wykrytych rdzeni słownych: {total_words}</p>"
        f"<p>Bilans emocjonalny: <b>{bilans}</b> "
        f"(pozytywne {pos_p:.1f}% / negatywne {neg_p:.1f}%)</p>"
        f"<p>Dominujące kategorie: {top3_str}</p>"
        f"<p><em>Wykresy w załącznikach PNG:</em><br>"
        f"&bull; W1 – Profil radarowy cech narracyjnych<br>"
        f"&bull; W12 – Przebieg emocji (średnia ruchoma)<br>"
        f"&bull; W3 – Sumy słów z każdej puli biblioteki<br>"
        f"&bull; WK – Kołowy wykres ukierunkowania tekstu</p>"
        f"<hr>"
    )


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_emocje_section(body: str, attachments: list = None) -> dict:
    """
    Buduje sekcję 'emocje':
    - analizuje treść maila i każdy załącznik osobno
    - generuje 4 wykresy PNG dla każdego źródła
    - zwraca HTML z raportem + listę obrazków PNG (base64)

    Parametry:
        body        – treść maila (str)
        attachments – lista dict [{base64: ..., name: ...}]

    Zwraca:
        {
          "reply_html": str,
          "images": [{"base64": str, "filename": str, "content_type": str}, ...]
        }
    """
    cats = _load_categories()

    images     = []   # lista {base64, filename, content_type}
    reply_html = ""

    # ── Zbierz źródła tekstowe ────────────────────────────────────────────────
    sources = []

    if body and body.strip():
        sources.append(("Treść_maila", body))

    for att in (attachments or []):
        att_b64  = att.get("base64")
        att_name = att.get("name", "dokument")
        if not att_b64:
            continue
        try:
            raw = base64.b64decode(att_b64)
            txt = _extract_text_from_bytes(raw, att_name)
            if txt and txt.strip():
                sources.append((att_name, txt))
            else:
                current_app.logger.warning(
                    "Emocje: brak tekstu w załączniku %s", att_name)
        except Exception as e:
            current_app.logger.warning(
                "Emocje: błąd załącznika %s: %s", att_name, e)

    if not sources:
        return {
            "reply_html": "<p>Brak tekstu do analizy emocjonalnej.</p>",
            "images":     [],
        }

    # ── Analizuj każde źródło ─────────────────────────────────────────────────
    for label, text in sources:
        try:
            para_rows = _analyze_paragraphs(text, cats)
            if not para_rows:
                current_app.logger.warning(
                    "Emocje: brak akapitów w źródle '%s'", label)
                continue

            totals = _aggregate(para_rows, cats)
            perc   = _percentages(totals)
            sl     = _safe_label(label)

            # W1 – radar
            b64 = _plot_w1_radar(perc, label)
            if b64:
                images.append({
                    "base64":       b64,
                    "filename":     f"w1_radar_{sl}.png",
                    "content_type": "image/png",
                })

            # W12 – średnia ruchoma
            b64 = _plot_w12_srednia(para_rows, label)
            if b64:
                images.append({
                    "base64":       b64,
                    "filename":     f"w12_srednia_{sl}.png",
                    "content_type": "image/png",
                })

            # W3 – kategorie sumy
            b64 = _plot_w3_kategorie(totals, label)
            if b64:
                images.append({
                    "base64":       b64,
                    "filename":     f"w3_kategorie_{sl}.png",
                    "content_type": "image/png",
                })

            # WK – kołowy
            b64 = _plot_wK_kolo(perc, label)
            if b64:
                images.append({
                    "base64":       b64,
                    "filename":     f"wK_kolo_{sl}.png",
                    "content_type": "image/png",
                })

            reply_html += _build_report_html(label, totals, perc, len(para_rows))

        except Exception as e:
            current_app.logger.exception(
                "Emocje: błąd analizy źródła '%s': %s", label, e)

    if not reply_html:
        reply_html = "<p>Nie udało się wygenerować analizy emocjonalnej.</p>"

    current_app.logger.info(
        "Emocje: źródeł=%d | wykresów=%d", len(sources), len(images))

    return {
        "reply_html": reply_html,
        "images":     images,
    }
