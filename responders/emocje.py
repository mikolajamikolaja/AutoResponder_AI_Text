"""
responders/emocje.py
Responder KEYWORDS4 — analiza emocjonalno-narracyjna tekstu (książka/literatura).

Wykresy generowane dla każdego źródła:
  W1   – radar 23 kategorii bezpośrednio (polskie nazwy)
  W3   – słupki: sumy wystąpień słów z każdej puli biblioteki
  WK   – kołowy: ukierunkowanie tekstu (wszystkie wskaźniki)
  WB   – bilans: pozytywne vs negatywne vs neutralne (3 kawałki)
  WE   – top 10 kategorii z przykładowymi słowami które wystąpiły w tekście
  WA   – emocje per akapit: słupki pos/neg dla każdego akapitu (zastępuje W12)

Załączniki TXT:
  raport.txt              – szczegółowa analiza tekstowa
  raport2_najwiecej_wyrazow.txt – ranking wszystkich słów: słowo (N)
"""

import io
import re
import os
import gc
import base64
import tempfile
from collections import Counter
from flask import current_app

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── Opcjonalne zależności ─────────────────────────────────────────────────────
try:
    from docx import Document as DocxDocument
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

try:
    import docx2txt
    _HAS_DOCX2TXT = True
except ImportError:
    _HAS_DOCX2TXT = False

try:
    import snowballstemmer
    _SB = snowballstemmer.stemmer("polish") \
        if "polish" in snowballstemmer.stemmer.languages() else None
except Exception:
    _SB = None

# ── Ścieżki ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_DIR  = os.path.join(BASE_DIR, "biblioteka")

# ── Kategorie biblioteki ──────────────────────────────────────────────────────
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

# ── Polskie stopwords (pomijane w rankingu słów) ──────────────────────────────
_STOPWORDS = {
    "i","w","z","na","do","się","że","nie","to","jest","a","o","jak",
    "przez","po","za","ale","już","tak","co","jego","jej","ich","go",
    "mu","mi","mnie","my","wy","oni","one","ten","ta","te","tego","tej",
    "temu","tą","tym","te","być","było","była","byli","będzie","będą",
    "ze","przy","czy","też","tylko","jeszcze","bo","by","więc","oraz",
    "dla","przed","nad","pod","między","nawet","kiedy","gdy","gdzie",
    "który","która","które","którego","której","którzy","wszystko",
    "tego","sobie","sam","swój","swoją","swoich","mój","moja","moje",
    "twój","twoja","twoje","ich","nim","nich","nią","je","tu","tam",
    "pan","pani","się","była","który","więcej","może","bardzo","no",
}

# ── Fallback listy emocji ─────────────────────────────────────────────────────
_DOMYSLNE_POZ = [
    "radość","szczęście","uśmiech","cieszyć","zachwyt","entuzjazm",
    "miłość","kochać","sukces","wygrać","świetny","doskonały",
    "przyjemny","spokój","ulga","energia","triumf","przyjaźń","śmiech",
]
_DOMYSLNE_NEG = [
    "smutek","ból","gniew","wściekłość","nienawiść","porażka","zły",
    "strach","przerażenie","agresja","frustracja","depresja","lęk",
    "żal","rozczarowanie","bezsilność","pustka","krzyk","oskarżenie",
]

_POLISH_SUFFIXES = sorted([
    "owania","owanie","owaniach","owaniom","owaniami",
    "eniech","eniem","eniom","enia","enie",
    "ach","ami","ego","emu","ej","ie","ią","iąc",
    "cie","ów","om","a","e","y","u","i","o",
    "ł","ła","li","ły","sz",
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
    return re.findall(r"[A-Za-ząćęłńóśżźĄĆĘŁŃÓŚŻŹ]+", text)


# ── Ładowanie biblioteki ──────────────────────────────────────────────────────
def _load_wordlist(path: str, fallback=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return [
                ln.strip() for ln in f
                if ln.strip() and not ln.strip().startswith("#")
            ]
    return fallback or []


def _load_categories() -> dict:
    """Zwraca {etykieta: {'stems': set, 'words': list}}"""
    cats = {}

    for key, fname, fb in [
        ("pozytywne", "slowa_pozytywne.txt", _DOMYSLNE_POZ),
        ("negatywne", "slowa_negatywne.txt", _DOMYSLNE_NEG),
    ]:
        words = _load_wordlist(os.path.join(LIB_DIR, fname), fb)
        cats[key] = {
            "stems": {_stem(w) for w in words if w},
            "words": words,
        }

    for fname, label in CATEGORY_FILES.items():
        words = _load_wordlist(os.path.join(LIB_DIR, fname), [])
        cats[label] = {
            "stems": {_stem(w) for w in words if w},
            "words": words,
        }

    return cats


# ── Ekstrakcja tekstu ─────────────────────────────────────────────────────────
def _extract_text_from_bytes(raw_bytes: bytes, name: str) -> str:
    name_lower = (name or "").lower()

    if name_lower.endswith(".txt"):
        for enc in ("utf-8", "cp1250", "latin-1"):
            try:
                return raw_bytes.decode(enc)
            except Exception:
                pass
        return raw_bytes.decode("utf-8", errors="ignore")

    if name_lower.endswith(".docx") and _HAS_DOCX:
        try:
            doc   = DocxDocument(io.BytesIO(raw_bytes))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for tbl in doc.tables:
                for row in tbl.rows:
                    ct = " | ".join(
                        c.text.strip() for c in row.cells if c.text.strip()
                    )
                    if ct:
                        parts.append(ct)
            return "\n\n".join(parts)
        except Exception as e:
            current_app.logger.warning("Błąd DOCX %s: %s", name, e)

    if name_lower.endswith(".doc"):
        if _HAS_DOCX2TXT:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
                    tmp.write(raw_bytes)
                    tmp_path = tmp.name
                text = docx2txt.process(tmp_path)
                if text and text.strip():
                    return text
            except Exception as e:
                current_app.logger.warning("Błąd DOC %s: %s", name, e)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        if _HAS_DOCX:
            try:
                doc   = DocxDocument(io.BytesIO(raw_bytes))
                parts = [p.text for p in doc.paragraphs if p.text.strip()]
                if parts:
                    return "\n\n".join(parts)
            except Exception:
                pass
        current_app.logger.warning(
            "Plik .doc '%s' – brak docx2txt. Wyślij jako .docx.", name)

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
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        paragraphs = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not paragraphs:
        return []

    pos_stems = cats.get("pozytywne", {}).get("stems", set())
    neg_stems = cats.get("negatywne", {}).get("stems", set())

    results = []
    for idx, para in enumerate(paragraphs, start=1):
        tokens = _tokenize(para)
        stems  = [_stem(t) for t in tokens]
        pos    = sum(1 for s in stems if s in pos_stems)
        neg    = sum(1 for s in stems if s in neg_stems)
        row    = {
            "idx": idx,
            "text": para[:120],
            "pos": pos,
            "neg": neg,
            "emotion_score": pos - neg,
        }
        for label, cdata in cats.items():
            stemset = cdata.get("stems", set())
            # zbierz też konkretne słowa które wystąpiły
            hit_words = [t for t, s in zip(tokens, stems) if s in stemset]
            row[label]              = len(hit_words)
            row[label + "__hits"]   = hit_words[:8]   # max 8 przykładów
        results.append(row)

    return results


def _aggregate(para_rows: list, cats: dict) -> dict:
    totals = {label: 0 for label in cats}
    for row in para_rows:
        for label in cats:
            totals[label] = totals.get(label, 0) + row.get(label, 0)
    return totals


def _aggregate_hits(para_rows: list, cats: dict) -> dict:
    """Zbiera unikalne słowa-przykłady dla każdej kategorii."""
    hits = {label: [] for label in cats}
    for row in para_rows:
        for label in cats:
            hits[label] += row.get(label + "__hits", [])
    # deduplikacja, zachowaj kolejność
    unique = {}
    for label in cats:
        seen = set()
        uniq = []
        for w in hits[label]:
            wl = w.lower()
            if wl not in seen:
                seen.add(wl)
                uniq.append(w)
        unique[label] = uniq[:10]
    return unique


def _percentages(totals: dict) -> dict:
    total_all = sum(totals.values())
    if total_all == 0:
        return {k: 0.0 for k in totals}
    return {k: v / total_all * 100.0 for k, v in totals.items()}


def _word_freq(text: str) -> list:
    """Zwraca listę (słowo, count) posortowaną malejąco, bez stopwords."""
    tokens = _tokenize(text)
    filtered = [
        t.lower() for t in tokens
        if len(t) > 2 and t.lower() not in _STOPWORDS
    ]
    return Counter(filtered).most_common()


# ── Helpers wykresów ──────────────────────────────────────────────────────────
def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", dpi=60, bbox_inches="tight")
    buf.seek(0)
    result = base64.b64encode(buf.read()).decode("ascii")
    buf.close()
    plt.close(fig)
    plt.close("all")
    gc.collect()
    return result


def _safe_label(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|,. ]', "_", text)[:40]


def _cat_colors(n: int):
    c1 = list(plt.cm.tab20(np.linspace(0, 1, min(n, 20))))
    c2 = list(plt.cm.tab20b(np.linspace(0, 1, max(1, n - 20))))
    return (c1 + c2)[:n]


# ── W1 – radar bezpośrednio wszystkich 23 kategorii ──────────────────────────
def _plot_w1_radar(totals: dict, title: str) -> str:
    # Tylko kategorie z CATEGORY_FILES (bez pozytywne/negatywne)
    labels = [label for label in CATEGORY_FILES.values() if label in totals]
    vals   = [totals.get(l, 0) for l in labels]

    if not any(v > 0 for v in vals):
        return None

    n      = len(labels)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    vals_c = vals + [vals[0]]
    angles_c = angles + [angles[0]]

    fig = plt.figure(figsize=(7, 7))
    ax  = plt.subplot(111, polar=True)
    ax.plot(angles_c, vals_c, linewidth=2, color="steelblue")
    ax.fill(angles_c, vals_c, alpha=0.25, color="steelblue")
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=8)
    ax.set_title(f"Radar 23 kategorii narracyjnych\n{title}", y=1.08, fontsize=11)
    ax.grid(True)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── W3 – słupki kategorii ────────────────────────────────────────────────────
def _plot_w3_kategorie(totals: dict, title: str) -> str:
    items  = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    labels = [k for k, _ in items]
    vals   = [v for _, v in items]

    if not any(v > 0 for v in vals):
        return None

    n      = len(labels)
    colors = _cat_colors(n)

    fig, ax = plt.subplots(figsize=(10, 4))
    bars = ax.bar(range(n), vals, color=colors)
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Suma wystąpień")
    ax.set_title(f"Sumy słów z każdej puli biblioteki\n{title}", fontsize=11)

    for bar, val in zip(bars, vals):
        if val > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.05,
                str(int(val)),
                ha="center", va="bottom", fontsize=7,
            )
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── WK – kołowy wszystkich wskaźników ────────────────────────────────────────
def _plot_wK_kolo(perc: dict, title: str) -> str:
    items = [(k, v) for k, v in perc.items() if v > 0.01]
    if not items:
        return None
    items = sorted(items, key=lambda kv: kv[1], reverse=True)

    labels = [k for k, _ in items]
    vals   = [v for _, v in items]
    n      = len(labels)
    colors = _cat_colors(n)

    fig, ax = plt.subplots(figsize=(7, 7))
    wedges, _, autotexts = ax.pie(
        vals, labels=None, colors=colors,
        autopct=lambda p: f"{p:.1f}%" if p > 2.5 else "",
        startangle=90, pctdistance=0.80,
        wedgeprops={"linewidth": 0.5, "edgecolor": "white"},
    )
    for at in autotexts:
        at.set_fontsize(7)

    legend_labels = [f"{lbl}  ({v:.1f}%)" for lbl, v in zip(labels, vals)]
    ax.legend(
        wedges, legend_labels,
        title="Wskaźniki", title_fontsize=9,
        loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8,
    )
    ax.text(0, 0, f"▶ {labels[0]}",
            ha="center", va="center",
            fontsize=12, fontweight="bold", color="#222222")
    ax.set_title(f"Ukierunkowanie tekstu – wszystkie wskaźniki\n{title}",
                 fontsize=12)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── WB – bilans emocjonalny (3 kawałki) ──────────────────────────────────────
def _plot_wb_bilans(totals: dict, title: str) -> str:
    pos = totals.get("pozytywne", 0)
    neg = totals.get("negatywne", 0)
    # neutralne = wszystkie inne kategorie
    other = sum(v for k, v in totals.items()
                if k not in ("pozytywne", "negatywne"))

    total = pos + neg + other
    if total == 0:
        return None

    labels_b = ["Pozytywne", "Negatywne", "Neutralne/inne"]
    vals_b   = [pos, neg, other]
    colors_b = ["#4CAF50", "#F44336", "#90CAF9"]
    explode  = (0.05, 0.05, 0.0)

    fig, ax = plt.subplots(figsize=(7, 7))
    wedges, _, autotexts = ax.pie(
        vals_b, labels=labels_b, colors=colors_b,
        autopct="%1.1f%%", explode=explode, startangle=90,
        textprops={"fontsize": 13},
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
    )
    for at in autotexts:
        at.set_fontsize(12)

    bilans = "POZYTYWNY" if pos > neg else ("NEGATYWNY" if neg > pos else "NEUTRALNY")
    kolor  = "#4CAF50" if pos > neg else ("#F44336" if neg > pos else "#90CAF9")
    ax.text(0, 0, bilans,
            ha="center", va="center",
            fontsize=14, fontweight="bold", color=kolor)

    ax.set_title(f"Bilans emocjonalny tekstu\n{title}", fontsize=12)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── WE – top 10 kategorii z przykładowymi słowami ────────────────────────────
def _plot_we_przyklady(totals: dict, hits: dict, title: str) -> str:
    top10 = sorted(
        [(k, v) for k, v in totals.items() if v > 0],
        key=lambda kv: kv[1], reverse=True
    )[:10]

    if not top10:
        return None

    labels = [k for k, _ in top10]
    vals   = [v for _, v in top10]
    n      = len(labels)
    colors = _cat_colors(n)

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(range(n), vals, color=colors, height=0.6)
    ax.set_yticks(range(n))
    ax.set_yticklabels(labels, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("Liczba wystąpień")
    ax.set_title(f"Top 10 kategorii – przykładowe słowa z tekstu\n{title}",
                 fontsize=11)

    for i, (bar, lbl, val) in enumerate(zip(bars, labels, vals)):
        # liczba po prawej
        ax.text(val + 0.05, bar.get_y() + bar.get_height() / 2,
                str(int(val)), va="center", fontsize=9)
        # przykładowe słowa pod słupkiem
        przykl = hits.get(lbl, [])[:6]
        if przykl:
            ax.text(0.3, bar.get_y() + bar.get_height() / 2,
                    "  »  " + ", ".join(przykl),
                    va="center", fontsize=8, color="#555555",
                    style="italic")

    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── WA – emocje per akapit (zastępuje W12) ────────────────────────────────────
def _plot_wa_akapity(para_rows: list, title: str) -> str:
    if not para_rows:
        return None

    rows = para_rows[:50]

    x   = [r["idx"] for r in rows]
    pos = [r["pos"] for r in rows]
    neg = [r["neg"] for r in rows]

    fig, ax = plt.subplots(figsize=(min(max(8, len(x) * 0.3 + 2), 20), 4))
    width = 0.4
    width = 0.4
    bars_p = ax.bar(
        [i - width/2 for i in range(len(x))], pos,
        width=width, color="#4CAF50", label="Pozytywne", alpha=0.85
    )
    bars_n = ax.bar(
        [i + width/2 for i in range(len(x))], neg,
        width=width, color="#F44336", label="Negatywne", alpha=0.85
    )

    ax.set_xticks(range(len(x)))
    ax.set_xticklabels([f"Ak.{i}" for i in x], rotation=45, fontsize=7)
    ax.set_ylabel("Liczba słów emocjonalnych")
    ax.set_title(f"Emocje pozytywne vs negatywne per akapit\n{title}",
                 fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    # Dodaj etykiety wartości
    for bar in bars_p:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h + 0.05,
                    str(int(h)), ha="center", va="bottom", fontsize=7)
    for bar in bars_n:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h + 0.05,
                    str(int(h)), ha="center", va="bottom", fontsize=7,
                    color="#C62828")

    fig.tight_layout()
    return _fig_to_b64(fig)


# ── Raport TXT ────────────────────────────────────────────────────────────────
def _build_raport_txt(label: str, text: str, totals: dict, perc: dict,
                      hits: dict, para_rows: list) -> str:
    linia = "=" * 60
    linia2 = "-" * 60

    pos_p = perc.get("pozytywne", 0.0)
    neg_p = perc.get("negatywne", 0.0)
    bilans = ("POZYTYWNY" if pos_p > neg_p
              else "NEGATYWNY" if neg_p > pos_p else "NEUTRALNY")

    total_hits  = sum(totals.values())
    total_words = sum(len(_tokenize(r["text"])) for r in para_rows)

    lines = [
        linia,
        f"ANALIZA EMOCJONALNO-NARRACYJNA TEKSTU",
        f"Źródło: {label}",
        linia,
        f"Akapitów:            {len(para_rows)}",
        f"Słów w tekście:      ~{total_words}",
        f"Wykrytych wskaźników:{total_hits}",
        f"Bilans emocjonalny:  {bilans}",
        f"  pozytywne: {pos_p:.1f}%  |  negatywne: {neg_p:.1f}%",
        "",
        linia2,
        "RANKING KATEGORII (od najczęstszej):",
        linia2,
    ]

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    for rank, (cat, cnt) in enumerate(ranked, start=1):
        if cnt == 0:
            continue
        pct    = perc.get(cat, 0.0)
        bar    = "█" * min(int(pct * 2), 40)
        przykl = ", ".join(hits.get(cat, [])[:6]) or "—"
        lines.append(f"{rank:>3}. {cat:<28} {cnt:>4}x  ({pct:5.1f}%)  {bar}")
        lines.append(f"     przykłady: {przykl}")
        lines.append("")

    lines += [
        linia2,
        "ANALIZA PER AKAPIT (pierwsze 20):",
        linia2,
    ]
    for row in para_rows[:20]:
        score = row["emotion_score"]
        znak  = "+" if score > 0 else ("" if score == 0 else "")
        lines.append(
            f"[Ak.{row['idx']:>3}]  pos={row['pos']}  neg={row['neg']}  "
            f"score={znak}{score:+d}  |  {row['text'][:80]}..."
        )

    lines += [
        "",
        linia,
        "Raport wygenerowany automatycznie przez system analizy emocjonalnej.",
        linia,
    ]

    return "\n".join(lines)


def _build_ranking_txt(text: str, label: str) -> str:
    """Ranking najczęstszych słów: słowo (N)"""
    freq = _word_freq(text)
    lines = [
        f"# Ranking słów — {label}",
        f"# Wygenerowano automatycznie",
        "",
    ]
    for word, cnt in freq:
        lines.append(f"{word} ({cnt})")
    return "\n".join(lines)


# ── HTML dla maila ────────────────────────────────────────────────────────────
def _build_reply_html(label: str, totals: dict, perc: dict,
                      para_count: int) -> str:
    total = sum(totals.values())
    top5  = sorted(perc.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top5_str = ", ".join(
        f"<b>{k}</b> ({v:.1f}%)" for k, v in top5 if v > 0
    )
    pos_p  = perc.get("pozytywne", 0.0)
    neg_p  = perc.get("negatywne", 0.0)
    bilans = ("pozytywne ✅" if pos_p > neg_p
              else "negatywne ⚠️" if neg_p > pos_p else "neutralne")

    return (
        f"<p><strong>📊 Analiza emocjonalno-narracyjna: {label}</strong></p>"
        f"<p>Akapitów: <b>{para_count}</b> | "
        f"Wykrytych wskaźników: <b>{total}</b></p>"
        f"<p>Bilans emocjonalny: <b>{bilans}</b> "
        f"(+{pos_p:.1f}% / -{neg_p:.1f}%)</p>"
        f"<p>Dominujące kategorie: {top5_str}</p>"
        f"<p><em>Załączniki:</em><br>"
        f"&bull; <b>W1</b> – Radar 23 kategorii narracyjnych<br>"
        f"&bull; <b>W3</b> – Słupki: sumy wystąpień ze wszystkich puli<br>"
        f"&bull; <b>WK</b> – Kołowy: ukierunkowanie tekstu<br>"
        f"&bull; <b>WB</b> – Bilans: pozytywne / negatywne / inne<br>"
        f"&bull; <b>WE</b> – Top 10 kategorii z przykładowymi słowami<br>"
        f"&bull; <b>WA</b> – Emocje pozytywne vs negatywne per akapit<br>"
        f"&bull; <b>raport.txt</b> – Szczegółowa analiza tekstowa<br>"
        f"&bull; <b>raport2_najwiecej_wyrazow.txt</b> – Ranking słów</p>"
        f"<hr>"
    )


# ── Główna funkcja responderu ─────────────────────────────────────────────────
def build_emocje_section(body: str, attachments: list = None, test_mode: bool = False) -> dict:
    """
    Emocje responder - generuje analizę emocji w mailu.
    
    Parametr test_mode:
    - Jeśli test_mode=True (z KEYWORDS_TEST via app.py disable_flux),
      można wy generowanie Flux jeśli emocje sobie funkcjonuje ten obraz.
    """
    cats = _load_categories()

    images     = []
    docs       = []   # załączniki TXT (raporty)
    reply_html = ""
    sources    = []

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
            "reply_html": (
                "<p>Brak tekstu do analizy emocjonalnej.</p>"
                "<p><em>Pliki .doc wyślij jako .docx.</em></p>"
            ),
            "images": [],
            "docs":   [],
        }

    for label, text in sources:
        try:
            para_rows = _analyze_paragraphs(text, cats)
            if not para_rows:
                current_app.logger.warning(
                    "Emocje: brak akapitów w '%s'", label)
                continue

            totals = _aggregate(para_rows, cats)
            hits   = _aggregate_hits(para_rows, cats)
            perc   = _percentages(totals)
            sl     = _safe_label(label)

            # ── 6 wykresów ────────────────────────────────────────────────────
            for fn, fname_prefix in [
                (_plot_w1_radar,      "w1_radar"),
                (lambda p, t: _plot_w3_kategorie(totals, t), "w3_kategorie"),
                (lambda p, t: _plot_wK_kolo(perc, t),        "wK_kolo"),
                (lambda p, t: _plot_wb_bilans(totals, t),    "wB_bilans"),
                (lambda p, t: _plot_we_przyklady(totals, hits, t), "wE_przyklady"),
                (lambda p, t: _plot_wa_akapity(para_rows, t),"wA_akapity"),
            ]:
                try:
                    # w1_radar wymaga totals a nie perc
                    if fname_prefix == "w1_radar":
                        b64 = _plot_w1_radar(totals, label)
                    else:
                        b64 = fn(perc, label)
                    if b64:
                        images.append({
                            "base64":       b64,
                            "filename":     f"{fname_prefix}_{sl}.png",
                            "content_type": "image/png",
                        })
                except Exception as e:
                    current_app.logger.warning(
                        "Emocje: błąd wykresu %s: %s", fname_prefix, e)
                finally:
                    plt.close("all")
                    gc.collect()

            # ── Raporty TXT ───────────────────────────────────────────────────
            raport_txt = _build_raport_txt(
                label, text, totals, perc, hits, para_rows
            )
            docs.append({
                "base64":       base64.b64encode(
                    raport_txt.encode("utf-8")).decode("ascii"),
                "filename":     f"raport_{sl}.txt",
                "content_type": "text/plain",
            })

            ranking_txt = _build_ranking_txt(text, label)
            docs.append({
                "base64":       base64.b64encode(
                    ranking_txt.encode("utf-8")).decode("ascii"),
                "filename":     f"raport2_najwiecej_wyrazow_{sl}.txt",
                "content_type": "text/plain",
            })

            reply_html += _build_reply_html(label, totals, perc, len(para_rows))

        except Exception as e:
            current_app.logger.exception(
                "Emocje: błąd analizy źródła '%s': %s", label, e)
        finally:
            # Zwolnij pamięć po każdym źródle
            plt.close("all")
            gc.collect()

    if not reply_html:
        reply_html = "<p>Nie udało się wygenerować analizy emocjonalnej.</p>"

    current_app.logger.info(
        "Emocje: źródeł=%d | wykresów=%d | raportów=%d",
        len(sources), len(images), len(docs),
    )

    return {
        "reply_html": reply_html,
        "images":     images,
        "docs":       docs,
    }
