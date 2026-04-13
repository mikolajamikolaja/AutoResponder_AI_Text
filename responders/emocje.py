"""
responders/emocje.py
Responder EMOCJE — analiza emocjonalna + empatyczna odpowiedź deeskalacyjna.

Zamiast słowników słów używa DeepSeek AI który:
  1. Analizuje ton, napięcie i intencję maila
  2. Generuje empatyczną odpowiedź która:
     - trafia idealnie w ton emocjonalny nadawcy
     - łagodzi napięcie i deeskaluje sytuację
     - absolutnie nic nie obiecuje
     - sprawia wrażenie głębokiego zrozumienia

Załączniki (te same nazwy co poprzednio):
  w1_radar_{label}.png        – wykres radarowy 5 wymiarów emocjonalnych
  w3_kategorie_{label}.png    – słupki intensywności emocji
  wK_kolo_{label}.png         – kołowy: dominująca emocja
  wB_bilans_{label}.png       – bilans: napięcie vs spokój
  wE_przyklady_{label}.png    – top frazy kluczowe z maila
  wA_akapity_{label}.png      – timeline emocjonalny zdanie po zdaniu
  raport_{label}.txt          – pełna analiza tekstowa
  raport2_najwiecej_wyrazow_{label}.txt – odpowiedź AI jako tekst

Zależności:
  - matplotlib, numpy (wykresy)
  - core.ai_client (call_deepseek, MODEL_TYLER)
"""

import io
import re
import os
import gc
import json
import base64
import logging
from collections import Counter
from flask import current_app

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from core.ai_client import call_deepseek, extract_clean_text, MODEL_TYLER

logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
PROMPT_JSON = os.path.join(PROMPTS_DIR, "emocje_prompt.json")


# ── Ładowanie promptu ─────────────────────────────────────────────────────────

def _load_prompt() -> dict:
    try:
        with open(PROMPT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[emocje] Brak emocje_prompt.json: %s — używam fallbacku", e)
        return _fallback_prompt()


def _fallback_prompt() -> dict:
    return {
        "system": (
            "Jesteś ekspertem analizy emocjonalnej korespondencji biznesowej. "
            "Odpowiadasz WYŁĄCZNIE w formacie JSON bez żadnego tekstu poza klamrami {}."
        ),
        "user_template": (
            "Przeanalizuj poniższy mail i wygeneruj:\n"
            "1. Analizę emocjonalną (5 wymiarów w skali 0-100)\n"
            "2. Empatyczną odpowiedź deeskalacyjną\n\n"
            "### MAIL DO ANALIZY:\n{{MAIL}}\n\n"
            "### SCHEMAT JSON:\n"
            "{\n"
            "  \"analiza\": {\n"
            "    \"napiecie\": 0-100,\n"
            "    \"sentyment\": -100 do 100 (ujemny=negatywny),\n"
            "    \"pewnosc_nadawcy\": 0-100,\n"
            "    \"formalnosc\": 0-100,\n"
            "    \"pilnosc\": 0-100,\n"
            "    \"dominujaca_emocja\": \"złość|smutek|lęk|frustracja|rozczarowanie|neutralna|radość\",\n"
            "    \"intencja\": \"eskalacja|żądanie|skarga|prośba|podziękowanie|informacja|odmowa\",\n"
            "    \"kluczowe_frazy\": [\"fraza1\", \"fraza2\", \"fraza3\", \"fraza4\", \"fraza5\"],\n"
            "    \"zdania_scores\": [lista liczb -100 do 100, jedna per zdanie, max 15]\n"
            "  },\n"
            "  \"odpowiedz\": \"pełna empatyczna odpowiedź HTML gotowa do wysłania\"\n"
            "}\n"
        ),
        "odpowiedz_instrukcja": (
            "Odpowiedź musi: "
            "1) zaczynać się od głębokiego uznania emocji nadawcy (bez 'rozumiem że'), "
            "2) parafrazować ich sytuację własnymi słowami jakbyś był terapeutą, "
            "3) wyrazić zaangażowanie i troskę używając ciepłych metafor, "
            "4) absolutnie nic nie obiecywać — żadnych terminów, działań, zobowiązań, "
            "5) zakończyć otwartym pytaniem które zachęca do dalszego dialogu. "
            "Styl: ciepły, ludzki, empatyczny — jak najlepszy przyjaciel w korporacji. "
            "Format: HTML z tagami <p>, <strong> — gotowy do wklejenia w mail."
        )
    }


# ── Call AI ───────────────────────────────────────────────────────────────────

def _analyze_with_ai(mail_text: str, prompt_data: dict) -> dict | None:
    """Wywołuje DeepSeek i zwraca sparsowany dict lub None."""
    template = prompt_data.get("user_template", _fallback_prompt()["user_template"])
    instrukcja = prompt_data.get("odpowiedz_instrukcja", "")

    user_msg = template.replace("{{MAIL}}", mail_text[:4000])
    if instrukcja:
        user_msg += f"\n\n### INSTRUKCJA ODPOWIEDZI:\n{instrukcja}"

    system_msg = prompt_data.get("system", "Odpowiadaj WYŁĄCZNIE w JSON.")

    raw = call_deepseek(system_msg, user_msg, MODEL_TYLER)
    if not raw:
        logger.error("[emocje] DeepSeek nie odpowiedział")
        return None

    # Wyczyść i sparsuj JSON
    clean = extract_clean_text(raw) if callable(extract_clean_text) else raw
    # Usuń markdown fences jeśli są
    clean = re.sub(r"```json\s*", "", clean)
    clean = re.sub(r"```\s*", "", clean)
    clean = clean.strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Spróbuj wyciągnąć JSON z tekstu
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        logger.error("[emocje] Nie można sparsować JSON z odpowiedzi AI: %s...", clean[:200])
        return None


# ── Ekstrakcja tekstu z załączników ──────────────────────────────────────────

def _extract_text(raw_bytes: bytes, name: str) -> str:
    name_lower = (name or "").lower()
    if name_lower.endswith(".txt"):
        for enc in ("utf-8", "cp1250", "latin-1"):
            try:
                return raw_bytes.decode(enc)
            except Exception:
                pass
    if name_lower.endswith(".docx"):
        try:
            from docx import Document
            import io as _io
            doc = Document(_io.BytesIO(raw_bytes))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            pass
    if name_lower.endswith(".pdf"):
        try:
            import pdfplumber
            text = ""
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
            if text.strip():
                return text
        except Exception:
            pass
    return ""


# ── Helpers wykresów ──────────────────────────────────────────────────────────

def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", dpi=80, bbox_inches="tight")
    buf.seek(0)
    result = base64.b64encode(buf.read()).decode("ascii")
    buf.close()
    plt.close(fig)
    plt.close("all")
    gc.collect()
    return result


def _safe_label(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|,. ]', "_", text)[:40]


# ── Wykresy ───────────────────────────────────────────────────────────────────

def _plot_w1_radar(analiza: dict, title: str) -> str:
    """W1 — radar 5 wymiarów emocjonalnych."""
    labels = ["Napięcie", "Pilność", "Pewność\nnadawcy", "Formalność", "Intensywność"]
    vals = [
        analiza.get("napiecie", 0),
        analiza.get("pilnosc", 0),
        analiza.get("pewnosc_nadawcy", 0),
        analiza.get("formalnosc", 0),
        max(0, (analiza.get("sentyment", 0) * -1 + 100) / 2),  # negatywny sentyment → intensywność
    ]

    n = len(labels)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    vals_c = vals + [vals[0]]
    angles_c = angles + [angles[0]]

    fig = plt.figure(figsize=(6, 6))
    ax = plt.subplot(111, polar=True)

    color = "#E24B4A" if analiza.get("napiecie", 0) > 60 else "#378ADD"
    ax.plot(angles_c, vals_c, linewidth=2, color=color)
    ax.fill(angles_c, vals_c, alpha=0.2, color=color)
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylim(0, 100)
    ax.set_title(f"Profil emocjonalny maila\n{title}", y=1.1, fontsize=11)
    ax.grid(True, alpha=0.4)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _plot_w3_kategorie(analiza: dict, title: str) -> str:
    """W3 — słupki intensywności 5 wymiarów."""
    dims = {
        "Napięcie":    analiza.get("napiecie", 0),
        "Pilność":     analiza.get("pilnosc", 0),
        "Pewność":     analiza.get("pewnosc_nadawcy", 0),
        "Formalność":  analiza.get("formalnosc", 0),
        "Sentyment\n(abs)": abs(analiza.get("sentyment", 0)),
    }
    labels = list(dims.keys())
    vals   = list(dims.values())

    colors = []
    for v in vals:
        if v > 70:
            colors.append("#E24B4A")
        elif v > 40:
            colors.append("#EF9F27")
        else:
            colors.append("#378ADD")

    fig, ax = plt.subplots(figsize=(8, 4))
    bars = ax.bar(range(len(labels)), vals, color=colors, width=0.55)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=10)
    ax.set_ylim(0, 110)
    ax.set_ylabel("Intensywność (0–100)")
    ax.set_title(f"Intensywność wymiarów emocjonalnych\n{title}", fontsize=11)
    for bar, val in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 2,
                str(int(val)), ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _plot_wK_kolo(analiza: dict, title: str) -> str:
    """WK — kołowy: rozkład emocji (napięcie / spokój / formalność)."""
    napiecie  = analiza.get("napiecie", 0)
    pilnosc   = analiza.get("pilnosc", 0)
    formalnosc = analiza.get("formalnosc", 0)
    spokoj    = max(0, 100 - napiecie - pilnosc / 2)

    vals   = [napiecie, pilnosc, formalnosc, spokoj]
    labels = ["Napięcie", "Pilność", "Formalność", "Spokój"]
    colors = ["#E24B4A", "#EF9F27", "#7F77DD", "#1D9E75"]

    # Usuń zera
    pairs = [(l, v, c) for l, v, c in zip(labels, vals, colors) if v > 1]
    if not pairs:
        return None
    labels, vals, colors = zip(*pairs)

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, _, autotexts = ax.pie(
        vals, labels=labels, colors=colors,
        autopct="%1.0f%%", startangle=90,
        wedgeprops={"linewidth": 0.8, "edgecolor": "white"},
        textprops={"fontsize": 11},
    )
    for at in autotexts:
        at.set_fontsize(10)

    dominant = analiza.get("dominujaca_emocja", "")
    ax.text(0, 0, dominant, ha="center", va="center",
            fontsize=12, fontweight="bold", color="#2C2C2A")
    ax.set_title(f"Ukierunkowanie emocjonalne\n{title}", fontsize=11)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _plot_wb_bilans(analiza: dict, title: str) -> str:
    """WB — bilans: napięcie vs spokój vs neutralne."""
    napiecie = analiza.get("napiecie", 50)
    spokoj   = max(0, 100 - napiecie)
    sent     = analiza.get("sentyment", 0)

    pos = max(0, sent)
    neg = max(0, -sent)
    neu = max(0, 100 - pos - neg)

    vals   = [neg, pos, neu]
    labels = ["Negatywne", "Pozytywne", "Neutralne"]
    colors = ["#E24B4A", "#1D9E75", "#888780"]
    explode = (0.05, 0.05, 0.0)

    total = sum(vals)
    if total == 0:
        vals = [50, 10, 40]

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, _, autotexts = ax.pie(
        vals, labels=labels, colors=colors,
        autopct="%1.1f%%", explode=explode, startangle=90,
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
        textprops={"fontsize": 12},
    )
    bilans_txt = "NEGATYWNY" if neg > pos else ("POZYTYWNY" if pos > neg else "NEUTRALNY")
    kolor = "#E24B4A" if neg > pos else ("#1D9E75" if pos > neg else "#888780")
    ax.text(0, 0, bilans_txt, ha="center", va="center",
            fontsize=13, fontweight="bold", color=kolor)
    ax.set_title(f"Bilans emocjonalny\n{title}", fontsize=11)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _plot_we_przyklady(analiza: dict, title: str) -> str:
    """WE — kluczowe frazy z maila jako poziomy wykres."""
    frazy = analiza.get("kluczowe_frazy", [])
    if not frazy:
        return None

    # Długość frazy jako proxy "wagi" — dłuższa = ważniejsza dla AI
    vals   = [min(100, 40 + len(f) * 3) for f in frazy]
    n      = len(frazy)
    colors = ["#E24B4A", "#EF9F27", "#378ADD", "#1D9E75", "#7F77DD"][:n]

    fig, ax = plt.subplots(figsize=(9, max(3, n * 0.8 + 1)))
    bars = ax.barh(range(n), vals, color=colors, height=0.55)
    ax.set_yticks(range(n))
    ax.set_yticklabels(frazy, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, 120)
    ax.set_xlabel("Znaczenie w kontekście emocjonalnym")
    ax.set_title(f"Kluczowe frazy emocjonalne\n{title}", fontsize=11)
    for bar, val, fraza in zip(bars, vals, frazy):
        ax.text(val + 1, bar.get_y() + bar.get_height() / 2,
                str(int(val)), va="center", fontsize=9)
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


def _plot_wa_akapity(analiza: dict, title: str) -> str:
    """WA — timeline emocjonalny zdanie po zdaniu."""
    scores = analiza.get("zdania_scores", [])
    if not scores:
        return None

    x      = list(range(1, len(scores) + 1))
    pos    = [max(0, s) for s in scores]
    neg    = [max(0, -s) for s in scores]

    fig, ax = plt.subplots(figsize=(min(max(7, len(x) * 0.5 + 2), 16), 4))
    width = 0.4
    ax.bar([i - width / 2 for i in x], pos, width=width, color="#1D9E75", label="Pozytywne", alpha=0.85)
    ax.bar([i + width / 2 for i in x], neg, width=width, color="#E24B4A", label="Negatywne", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels([f"Z.{i}" for i in x], rotation=45, fontsize=8)
    ax.set_ylabel("Intensywność emocji")
    ax.set_title(f"Emocje per zdanie — przepływ narracyjny\n{title}", fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    return _fig_to_b64(fig)


# ── Raporty TXT ───────────────────────────────────────────────────────────────

def _build_raport_txt(label: str, analiza: dict, odpowiedz: str) -> str:
    linia  = "=" * 60
    linia2 = "-" * 60
    napiecie = analiza.get("napiecie", 0)
    sent     = analiza.get("sentyment", 0)
    bilans   = "POZYTYWNY" if sent > 20 else ("NEGATYWNY" if sent < -20 else "NEUTRALNY")

    lines = [
        linia,
        "ANALIZA EMOCJONALNA KORESPONDENCJI BIZNESOWEJ (AI)",
        f"Źródło: {label}",
        linia,
        f"Napięcie:             {napiecie}/100",
        f"Sentyment:            {sent:+d}/100",
        f"Pilność:              {analiza.get('pilnosc', 0)}/100",
        f"Pewność nadawcy:      {analiza.get('pewnosc_nadawcy', 0)}/100",
        f"Formalność:           {analiza.get('formalnosc', 0)}/100",
        f"Dominująca emocja:    {analiza.get('dominujaca_emocja', '—')}",
        f"Intencja:             {analiza.get('intencja', '—')}",
        f"Bilans emocjonalny:   {bilans}",
        "",
        linia2,
        "KLUCZOWE FRAZY EMOCJONALNE:",
        linia2,
    ]
    for i, f in enumerate(analiza.get("kluczowe_frazy", []), 1):
        lines.append(f"  {i}. {f}")

    lines += [
        "",
        linia2,
        "WYGENEROWANA ODPOWIEDŹ:",
        linia2,
        odpowiedz or "(brak)",
        "",
        linia,
        "Raport wygenerowany przez DeepSeek AI — system analizy emocjonalnej.",
        linia,
    ]
    return "\n".join(lines)


def _build_ranking_txt(label: str, odpowiedz: str) -> str:
    """Drugi raport — odpowiedź AI jako czysty tekst."""
    clean = re.sub(r"<[^>]+>", " ", odpowiedz or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    lines = [
        f"# Odpowiedź AI — {label}",
        "# Wygenerowano automatycznie przez DeepSeek",
        "",
        clean,
    ]
    return "\n".join(lines)


# ── HTML dla maila ────────────────────────────────────────────────────────────

def _build_reply_html(label: str, analiza: dict, odpowiedz_html: str) -> str:
    napiecie = analiza.get("napiecie", 0)
    sent     = analiza.get("sentyment", 0)
    intencja = analiza.get("intencja", "—")
    emocja   = analiza.get("dominujaca_emocja", "—")
    bilans   = "pozytywne ✅" if sent > 20 else ("negatywne ⚠️" if sent < -20 else "neutralne")

    return (
        f"{odpowiedz_html or ''}"
        f"<hr>"
        f"<p style='font-size:12px;color:#888;'>"
        f"<em>Analiza AI: napięcie {napiecie}/100 · emocja: {emocja} · "
        f"intencja: {intencja} · bilans: {bilans}</em></p>"
    )


# ── Główna funkcja responderu ─────────────────────────────────────────────────

def build_emocje_section(body: str, attachments: list = None, test_mode: bool = False) -> dict:
    """
    Emocje responder — analiza AI + empatyczna odpowiedź deeskalacyjna.
    Zwraca dict z reply_html, images (PNG), docs (TXT).
    """
    prompt_data = _load_prompt()

    images     = []
    docs       = []
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
            txt = _extract_text(raw, att_name)
            if txt and txt.strip():
                sources.append((att_name, txt))
        except Exception as e:
            logger.warning("[emocje] Błąd załącznika %s: %s", att_name, e)

    if not sources:
        return {
            "reply_html": "<p>Brak tekstu do analizy emocjonalnej.</p>",
            "images": [],
            "docs":   [],
        }

    for label, text in sources:
        try:
            # ── Analiza AI ────────────────────────────────────────────────────
            result = _analyze_with_ai(text, prompt_data)

            if not result:
                logger.warning("[emocje] AI nie zwróciło wyniku dla '%s'", label)
                continue

            analiza     = result.get("analiza", {})
            odpowiedz   = result.get("odpowiedz", "<p>Brak odpowiedzi.</p>")
            sl          = _safe_label(label)

            # ── 6 wykresów PNG (te same nazwy plików co poprzednio) ───────────
            plot_fns = [
                (_plot_w1_radar,     f"w1_radar_{sl}.png"),
                (_plot_w3_kategorie, f"w3_kategorie_{sl}.png"),
                (_plot_wK_kolo,      f"wK_kolo_{sl}.png"),
                (_plot_wb_bilans,    f"wB_bilans_{sl}.png"),
                (_plot_we_przyklady, f"wE_przyklady_{sl}.png"),
                (_plot_wa_akapity,   f"wA_akapity_{sl}.png"),
            ]

            for fn, fname in plot_fns:
                try:
                    b64 = fn(analiza, label)
                    if b64:
                        images.append({
                            "base64":       b64,
                            "filename":     fname,
                            "content_type": "image/png",
                        })
                except Exception as e:
                    logger.warning("[emocje] Błąd wykresu %s: %s", fname, e)
                finally:
                    plt.close("all")
                    gc.collect()

            # ── Raporty TXT (te same nazwy co poprzednio) ─────────────────────
            raport = _build_raport_txt(label, analiza, odpowiedz)
            docs.append({
                "base64":       base64.b64encode(raport.encode("utf-8")).decode("ascii"),
                "filename":     f"raport_{sl}.txt",
                "content_type": "text/plain",
            })

            ranking = _build_ranking_txt(label, odpowiedz)
            docs.append({
                "base64":       base64.b64encode(ranking.encode("utf-8")).decode("ascii"),
                "filename":     f"raport2_najwiecej_wyrazow_{sl}.txt",
                "content_type": "text/plain",
            })

            reply_html += _build_reply_html(label, analiza, odpowiedz)

        except Exception as e:
            logger.exception("[emocje] Błąd analizy '%s': %s", label, e)
        finally:
            plt.close("all")
            gc.collect()

    if not reply_html:
        reply_html = "<p>Nie udało się wygenerować analizy emocjonalnej.</p>"

    logger.info("[emocje] źródeł=%d | wykresów=%d | raportów=%d",
                len(sources), len(images), len(docs))

    return {
        "reply_html": reply_html,
        "images":     images,
        "docs":       docs,
    }
