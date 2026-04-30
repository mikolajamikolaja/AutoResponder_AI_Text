"""
responders/emocje.py
Responder EMOCJE — empatyczny pocieszyciel.

Zamiast analizy liczb i wykresów, ten responder:
  1. Czyta wiadomość nadawcy
  2. Dobiera jedną z 8 metod pocieszenia do kontekstu
  3. Generuje ciepłą, empatyczną odpowiedź HTML (bez rad, bez obietnic)
  4. Dołącza wizualizację SVG + miniaturę JPG + pełny HTML jako załączniki

Załączniki:
  diagram_{label}.htm   – interaktywny SVG z metodą pocieszenia
  mapa_{label}.jpg      – miniatura JPG (ciepłe kolory)
  pelna_{label}.htm     – pełny HTML gotowy do wglądu

Zależności:
  - core.ai_client (call_deepseek, MODEL_TYLER)
  - prompts/emocje.json (wytyczne i prompty)
"""

import io
import re
import os
import gc
import json
import base64
import logging
from flask import current_app

from core.ai_client import call_deepseek, extract_clean_text, MODEL_TYLER

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
PROMPT_JSON = os.path.join(PROMPTS_DIR, "emocje.json")


# ── Ładowanie promptu ─────────────────────────────────────────────────────────


def _load_prompt() -> dict:
    try:
        with open(PROMPT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[emocje] Brak emocje.json: %s — używam fallbacku", e)
        return _fallback_prompt()


def _fallback_prompt() -> dict:
    return {
        "system": (
            "Jesteś empatycznym towarzyszem. Twoje jedyne zadanie to pocieszyć osobę "
            "która napisała wiadomość. NIE dajesz rad. NIE proponujesz rozwiązań. "
            "Odpowiadasz WYŁĄCZNIE w formacie JSON bez żadnego tekstu poza klamrami {}."
        ),
        "user_template": (
            "Przeczytaj poniższą wiadomość i wygeneruj ciepłą, empatyczną odpowiedź pocieszenia.\n\n"
            "### WIADOMOŚĆ:\n{{MAIL}}\n\n"
            "### IMIĘ NADAWCY:\n{{SENDER_NAME}}\n\n"
            "### WYMAGANIA ODPOWIEDZI:\n"
            "- Pole 'pocieszenie' MUSI zawierać co najmniej 4-6 akapitów HTML (<p>...</p>)\n"
            "- Każdy akapit powinien mieć 2-4 zdania\n"
            "- NIE pisz tylko jednego zdania — to jest pełna odpowiedź na email\n"
            "- Odpowiedź ma być ciepła, osobista, odwoływać się do konkretnych słów z wiadomości\n\n"
            "### SCHEMAT JSON:\n"
            "{\n"
            '  "metoda": "nazwa wybranej metody pocieszenia",\n'
            '  "pocieszenie": "<p>akapit 1...</p><p>akapit 2...</p><p>akapit 3...</p>...",\n'
            '  "nastroj": "smutek|lęk|frustracja|ból|neutralna|złość|samotność",\n'
            '  "intensywnosc": 0\n'
            "}"
        ),
        "fallback_pocieszenie": (
            "<p>Dostałem/am Twoją wiadomość i jestem tutaj.</p>"
            "<p>To co czujesz ma sens. Nie musisz teraz nic robić — wystarczy że jesteś.</p>"
            "<p>Jestem z Tobą w tym.</p>"
        ),
    }


# ── Call AI ───────────────────────────────────────────────────────────────────


def _generuj_pocieszenie(body: str, sender_name: str, prompt_data: dict) -> dict | None:
    """Wywołuje DeepSeek i zwraca sparsowany dict z 'pocieszenie' lub None."""
    template = prompt_data.get("user_template", _fallback_prompt()["user_template"])
    system_msg = prompt_data.get("system", "Odpowiadaj WYŁĄCZNIE w JSON.")

    user_msg = (
        template
        .replace("{{MAIL}}", body[:4000])
        .replace("{{SENDER_NAME}}", sender_name or "nieznane")
    )

    # Dołącz metody pocieszenia jako kontekst jeśli są w JSON
    metody = prompt_data.get("metody_pocieszenia", [])
    if metody:
        metody_txt = "\n### DOSTĘPNE METODY POCIESZENIA:\n"
        for m in metody:
            metody_txt += (
                f"- [{m.get('id', '?')}] {m.get('nazwa', '')}: {m.get('opis', '')} "
                f"(przykład: \"{m.get('przyklad', '')}\")\n"
            )
        user_msg += metody_txt

    zasady = prompt_data.get("zasady_odpowiedzi", [])
    if zasady:
        user_msg += "\n### ZASADY:\n" + "\n".join(f"- {z}" for z in zasady)

    raw = call_deepseek(system_msg, user_msg, MODEL_TYLER)
    if not raw:
        logger.error("[emocje] DeepSeek nie odpowiedział")
        return None

    clean = extract_clean_text(raw) if callable(extract_clean_text) else raw
    clean = re.sub(r"```json\s*", "", clean)
    clean = re.sub(r"```\s*", "", clean)
    clean = clean.strip()

    try:
        # Użyj raw_decode zamiast json.loads — obsługuje "Extra data"
        # (gdy AI zwróci JSON + dodatkowy tekst poza klamrami)
        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(clean)
            return obj
        except json.JSONDecodeError:
            # Fallback: szukaj największego JSON w tekście
            for match in re.finditer(r"[{\[]", clean):
                start = match.start()
                try:
                    obj, end = decoder.raw_decode(clean[start:])
                    if obj is not None:
                        return obj
                except json.JSONDecodeError:
                    continue
            raise
    except json.JSONDecodeError:
        logger.error("[emocje] Nie można sparsować JSON: %s...", clean[:200])
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_label(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|,. ]', "_", text)[:40]


def _wyciagnij_imie(sender_name: str, sender_email: str = "") -> str:
    """
    Zwraca imię do wyświetlenia.
    Jeśli sender_name jest pusty lub wygląda jak adres email — wyciąga
    lokalną część emaila i kapitalizuje ją jako imię.
    """
    name = (sender_name or "").strip()

    # Jeśli sender_name jest adresem email lub pustym — użyj emaila
    if not name or "@" in name:
        if sender_email:
            local = sender_email.split("@")[0]
            # Wyczyść cyfry i znaki specjalne, zostaw pierwsze słowo
            local = re.sub(r"[._+\-]", " ", local).strip()
            local = re.split(r"\s+", local)[0]  # tylko pierwsze słowo (imię)
            local = re.sub(r"\d+", "", local).strip()  # usuń cyfry
            if local:
                return local.capitalize()
        return ""  # brak imienia — powitanie zostanie pominięte

    return name


def _nastroj_do_koloru(nastroj: str) -> dict:
    """Mapuje nastrój na paletę kolorów."""
    palety = {
        "smutek":     {"bg": "#eeedfe", "border": "#afa9ec", "ink": "#534ab7", "accent": "#534ab7"},
        "ból":        {"bg": "#eeedfe", "border": "#afa9ec", "ink": "#534ab7", "accent": "#534ab7"},
        "lęk":        {"bg": "#faeeda", "border": "#fac775", "ink": "#854f0b", "accent": "#854f0b"},
        "frustracja": {"bg": "#fcebeb", "border": "#f09595", "ink": "#a32d2d", "accent": "#a32d2d"},
        "złość":      {"bg": "#fcebeb", "border": "#f09595", "ink": "#a32d2d", "accent": "#a32d2d"},
        "samotność":  {"bg": "#fbeaf0", "border": "#f0a8c4", "ink": "#993556", "accent": "#993556"},
        "neutralna":  {"bg": "#d4f0e8", "border": "#7ecab8", "ink": "#1d8a6e", "accent": "#1d8a6e"},
    }
    return palety.get(nastroj, palety["neutralna"])


def _metoda_do_tagu(metoda: str) -> str:
    """Zwraca czytelną nazwę metody po polsku."""
    mapy = {
        "walidacja_emocji":      "metoda 01 · walidacja emocji",
        "obecnosc":              "metoda 02 · obecność",
        "normalizacja":          "metoda 03 · normalizacja",
        "odzwierciedlenie":      "metoda 04 · odzwierciedlenie",
        "przestrzen_na_cisze":   "metoda 05 · przestrzeń na ciszę",
        "docenienie_odwagi":     "metoda 06 · docenienie odwagi",
        "bez_srebrnych_podszewek": "metoda 07 · bez srebrnych podszewek",
        "cieplo_przez_konkret":  "metoda 08 · ciepło przez konkret",
    }
    return mapy.get(metoda, f"metoda · {metoda.replace('_', ' ')}")


# ── Generowanie SVG ───────────────────────────────────────────────────────────


def _buduj_svg(pocieszenie_html: str, metoda: str, nastroj: str, sender_name: str) -> str:
    """Generuje interaktywny SVG-diagram pocieszenia."""
    kolory = _nastroj_do_koloru(nastroj)
    tag = _metoda_do_tagu(metoda)

    # Wyciągnij czysty tekst z HTML odpowiedzi
    czysty = re.sub(r"<[^>]+>", " ", pocieszenie_html)
    czysty = re.sub(r"\s+", " ", czysty).strip()
    # Przytnij do 200 znaków dla podglądu w SVG
    podglad = czysty[:200] + ("…" if len(czysty) > 200 else "")
    # Escapuj do XML
    podglad_xml = (
        podglad
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    imie = (sender_name or "Nadawca").replace("&", "&amp;")

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 520" width="360" height="520">
  <defs>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&amp;family=Fraunces:ital,wght@0,300;0,600;1,300;1,600&amp;display=swap');
      .mono {{ font-family: 'DM Mono', monospace; }}
      .serif {{ font-family: 'Fraunces', serif; }}
    </style>
  </defs>

  <!-- tło -->
  <rect width="360" height="520" fill="#fdf8f3" rx="16"/>

  <!-- header -->
  <rect x="12" y="12" width="336" height="72" fill="{kolory['bg']}" rx="14" stroke="{kolory['border']}" stroke-width="1.5"/>
  <text x="180" y="38" text-anchor="middle" font-family="Fraunces, serif" font-size="15" font-weight="600" fill="{kolory['ink']}">Pocieszenie dla {imie}</text>
  <text x="180" y="56" text-anchor="middle" font-family="DM Mono, monospace" font-size="9" fill="{kolory['ink']}" opacity="0.65">{tag}</text>
  <text x="180" y="74" text-anchor="middle" font-family="DM Mono, monospace" font-size="9" fill="{kolory['ink']}" opacity="0.5">nastrój: {nastroj}</text>

  <!-- strzałka -->
  <line x1="180" y1="84" x2="180" y2="104" stroke="#d3cfc8" stroke-width="1"/>
  <polygon points="175,104 185,104 180,112" fill="#d3cfc8"/>

  <!-- metoda card -->
  <rect x="12" y="114" width="336" height="52" fill="{kolory['bg']}" rx="10" stroke="{kolory['border']}" stroke-width="1.5"/>
  <text x="24" y="134" font-family="DM Mono, monospace" font-size="9" fill="{kolory['ink']}" opacity="0.6" font-weight="500">{tag.upper()}</text>
  <text x="24" y="152" font-family="Fraunces, serif" font-size="13" font-weight="600" fill="{kolory['ink']}">DeepSeek — jedno wywołanie AI</text>

  <!-- strzałka -->
  <line x1="180" y1="166" x2="180" y2="186" stroke="#d3cfc8" stroke-width="1"/>
  <polygon points="175,186 185,186 180,194" fill="#d3cfc8"/>

  <!-- odpowiedź box -->
  <rect x="12" y="196" width="336" height="260" fill="#ffffff" rx="12" stroke="#d3cfc8" stroke-width="1"/>
  <text x="24" y="218" font-family="DM Mono, monospace" font-size="9" fill="#8a7a6a" opacity="0.7">ODPOWIEDŹ POCIESZENIA</text>
  <foreignObject x="20" y="224" width="320" height="224">
    <body xmlns="http://www.w3.org/1999/xhtml" style="font-family:DM Mono,monospace;font-size:10px;color:#2a1f14;line-height:1.6;word-wrap:break-word;margin:0;padding:0;">
      {podglad_xml}
    </body>
  </foreignObject>

  <!-- strzałka -->
  <line x1="180" y1="456" x2="180" y2="472" stroke="#d3cfc8" stroke-width="1"/>
  <polygon points="175,472 185,472 180,480" fill="#d3cfc8"/>

  <!-- footer -->
  <rect x="12" y="482" width="336" height="26" fill="{kolory['bg']}" rx="8" stroke="{kolory['border']}" stroke-width="1"/>
  <text x="180" y="499" text-anchor="middle" font-family="DM Mono, monospace" font-size="9" fill="{kolory['ink']}" opacity="0.7">return · reply_html · pocieszenie gotowe</text>
</svg>"""
    return svg


# ── Generowanie JPG (przez PNG z matplotlib) ──────────────────────────────────


def _buduj_jpg_b64(nastroj: str, metoda: str, sender_name: str) -> str | None:
    """Generuje prostą miniaturę JPG jako base64."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np

        kolory = _nastroj_do_koloru(nastroj)
        bg_hex = kolory["bg"]
        ink_hex = kolory["ink"]
        tag = _metoda_do_tagu(metoda)

        fig, ax = plt.subplots(figsize=(4, 2.8))
        fig.patch.set_facecolor(bg_hex)
        ax.set_facecolor(bg_hex)
        ax.axis("off")

        ax.text(
            0.5, 0.78,
            f"Pocieszenie · {sender_name or 'Nadawca'}",
            ha="center", va="center",
            fontsize=9, fontweight="bold",
            color=ink_hex,
            transform=ax.transAxes,
        )
        ax.text(
            0.5, 0.52,
            tag,
            ha="center", va="center",
            fontsize=7.5,
            color=ink_hex,
            alpha=0.7,
            transform=ax.transAxes,
        )
        ax.text(
            0.5, 0.28,
            f"nastrój: {nastroj}",
            ha="center", va="center",
            fontsize=7,
            color=ink_hex,
            alpha=0.5,
            transform=ax.transAxes,
        )

        rect = mpatches.FancyBboxPatch(
            (0.04, 0.04), 0.92, 0.92,
            boxstyle="round,pad=0.02",
            linewidth=1.2,
            edgecolor=kolory["border"],
            facecolor="none",
            transform=ax.transAxes,
        )
        ax.add_patch(rect)

        buf = io.BytesIO()
        fig.savefig(buf, format="jpeg", dpi=90, bbox_inches="tight", facecolor=bg_hex)
        buf.seek(0)
        result = base64.b64encode(buf.read()).decode("ascii")
        buf.close()
        plt.close(fig)
        plt.close("all")
        gc.collect()
        return result
    except Exception as e:
        logger.warning("[emocje] Błąd generowania JPG: %s", e)
        return None


# ── Budowanie HTML maila ───────────────────────────────────────────────────────


def _buduj_html_email(
    pocieszenie_html: str,
    sender_name: str,
    metoda: str,
    nastroj: str,
    jpg_b64: str | None,
) -> str:
    """Buduje reply_html z ciepłym layoutem."""
    kolory = _nastroj_do_koloru(nastroj)
    tag = _metoda_do_tagu(metoda)
    imie = sender_name or ""

    powitanie = f"<p>Drogi/a {imie},</p>" if imie else ""

    img_tag = ""
    if jpg_b64:
        img_tag = (
            f'<div style="margin:16px 0;text-align:center;">'
            f'<img src="data:image/jpeg;base64,{jpg_b64}" '
            f'style="border-radius:12px;max-width:280px;border:1px solid {kolory["border"]};" '
            f'alt="Diagram pocieszenia"/>'
            f'</div>'
        )

    html = f"""<div style="font-family:'DM Mono',monospace;color:#2a1f14;max-width:560px;margin:0 auto;padding:20px 14px;">
  <div style="background:{kolory['bg']};border:1.5px solid {kolory['border']};border-radius:16px;padding:18px 20px;text-align:center;margin-bottom:20px;">
    <h1 style="font-family:Fraunces,serif;font-size:18px;font-weight:600;color:{kolory['ink']};margin:0 0 6px 0;">Jestem tutaj</h1>
    <p style="font-size:10px;color:{kolory['ink']};opacity:0.65;margin:0;">{tag}</p>
  </div>

  {img_tag}

  <div style="line-height:1.75;font-size:13px;color:#2a1f14;">
    {powitanie}
    {pocieszenie_html}
  </div>

  <div style="margin-top:24px;padding-top:14px;border-top:1px solid #d3cfc8;font-size:10px;color:#8a7a6a;text-align:center;">
    <em>nastrój: {nastroj}</em>
  </div>
</div>"""
    return html


# ── Budowanie pełnego HTML pliku ───────────────────────────────────────────────


def _buduj_pelny_html(
    pocieszenie_html: str,
    sender_name: str,
    metoda: str,
    nastroj: str,
) -> str:
    """Buduje pełny samodzielny plik HTML do podglądu."""
    kolory = _nastroj_do_koloru(nastroj)
    tag = _metoda_do_tagu(metoda)
    imie = sender_name or "Nadawca"

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pocieszenie · {imie}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Fraunces:ital,wght@0,300;0,600;1,300;1,600&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#fdf8f3;font-family:'DM Mono',monospace;color:#2a1f14;padding:24px 16px 60px;}}
.header{{background:{kolory['bg']};border:1.5px solid {kolory['border']};border-radius:16px;padding:20px;text-align:center;margin-bottom:20px;}}
.header h1{{font-family:Fraunces,serif;font-size:20px;font-weight:600;color:{kolory['ink']};}}
.header p{{font-size:10px;color:{kolory['ink']};opacity:0.65;margin-top:6px;}}
.body{{background:#fff;border:1px solid #d3cfc8;border-radius:14px;padding:22px 20px;line-height:1.8;font-size:13px;}}
.footer{{margin-top:20px;text-align:center;font-size:10px;color:#8a7a6a;}}
</style>
</head>
<body>
<div class="header">
  <h1>Jestem tutaj · {imie}</h1>
  <p>{tag} · nastrój: {nastroj}</p>
</div>
<div class="body">
  {pocieszenie_html}
</div>
<div class="footer">Wygenerowano przez DeepSeek AI — system pocieszenia empatycznego.</div>
</body>
</html>"""


# ── Główna funkcja responderu ─────────────────────────────────────────────────


def build_emocje_section(
    body: str,
    sender_name: str = "",
    sender_email: str = "",
    attachments: list = None,
    test_mode: bool = False,
) -> dict:
    """
    Emocje responder — empatyczny pocieszyciel.

    Zwraca dict z:
      reply_html  – HTML gotowy do wysłania jako mail
      images      – lista (pusta — brak wykresów)
      docs        – lista HTM/SVG jako załączniki
    """
    prompt_data = _load_prompt()
    docs = []

    # ── Walidacja wejścia ─────────────────────────────────────────────────────

    mail_text = (body or "").strip()
    if not mail_text:
        fallback = prompt_data.get(
            "fallback_pocieszenie",
            "<p>Dostałem/am Twoją wiadomość i jestem tutaj.</p>",
        )
        return {
            "reply_html": fallback,
            "images": [],
            "docs": [],
        }

    # ── Wyciągnij imię — z sender_name lub z adresu email ────────────────────
    imie = _wyciagnij_imie(sender_name, sender_email)

    sl = _safe_label(imie or sender_email or "mail")

    # ── Wywołanie AI ──────────────────────────────────────────────────────────

    result = _generuj_pocieszenie(mail_text, imie, prompt_data)

    if not result:
        logger.warning("[emocje] AI nie zwróciło wyniku — używam fallbacku")
        fallback = prompt_data.get(
            "fallback_pocieszenie",
            "<p>Dostałem/am Twoją wiadomość i jestem tutaj.</p>",
        )
        return {
            "reply_html": fallback,
            "images": [],
            "docs": [],
        }

    pocieszenie_html = result.get(
        "pocieszenie",
        prompt_data.get("fallback_pocieszenie", "<p>Jestem tutaj.</p>"),
    )
    metoda = result.get("metoda", "obecnosc")
    nastroj = result.get("nastroj", "neutralna")

    # ── Miniatura JPG ─────────────────────────────────────────────────────────

    jpg_b64 = _buduj_jpg_b64(nastroj, metoda, imie)

    # ── reply_html ────────────────────────────────────────────────────────────

    reply_html = _buduj_html_email(pocieszenie_html, imie, metoda, nastroj, jpg_b64)

    # ── Załączniki ────────────────────────────────────────────────────────────

    # 1. SVG diagram
    try:
        svg_content = _buduj_svg(pocieszenie_html, metoda, nastroj, imie)
        svg_htm = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Diagram pocieszenia</title>
</head><body style="margin:0;background:#fdf8f3;display:flex;justify-content:center;padding:20px;">
{svg_content}
</body></html>"""
        docs.append({
            "base64": base64.b64encode(svg_htm.encode("utf-8")).decode("ascii"),
            "filename": f"diagram_{sl}.htm",
            "content_type": "text/html",
        })
    except Exception as e:
        logger.warning("[emocje] Błąd SVG: %s", e)

    # 2. Miniatura JPG
    if jpg_b64:
        try:
            docs.append({
                "base64": jpg_b64,
                "filename": f"mapa_{sl}.jpg",
                "content_type": "image/jpeg",
            })
        except Exception as e:
            logger.warning("[emocje] Błąd JPG doc: %s", e)

    # 3. Pełny HTML
    try:
        pelny = _buduj_pelny_html(pocieszenie_html, imie, metoda, nastroj)
        docs.append({
            "base64": base64.b64encode(pelny.encode("utf-8")).decode("ascii"),
            "filename": f"pelna_{sl}.htm",
            "content_type": "text/html",
        })
    except Exception as e:
        logger.warning("[emocje] Błąd pełnego HTML: %s", e)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    try:
        import matplotlib.pyplot as plt
        plt.close("all")
    except Exception:
        pass
    gc.collect()

    logger.info(
        "[emocje] metoda=%s | nastrój=%s | załączników=%d",
        metoda,
        nastroj,
        len(docs),
    )

    return {
        "reply_html": reply_html,
        "images": [],
        "docs": docs,
    }
