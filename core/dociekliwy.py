"""
responders/analiza.py
Responder KEYWORDS3 — Eryk Responder (Mistrz Pasywno-Agresywnego Doprecyzowywania).

Render generuje JEDEN RAZ wszystkie 10 kroków (pytania + opcje + reakcje Eryka).
Dostarcza dwie rzeczy jednocześnie:

  1. reply_html  — treść maila z CSS :target "grą" (bez JS, działa w klientach pocztowych)
  2. docx_list   — [{base64, filename, content_type}] z eryk_gra.htm (pełny JS, załącznik)

Zależności z app.py / smtp_wysylka.py — BEZ ZMIAN:
  from responders.analiza import build_analiza_section
  wynik["reply_html"] — HTML maila
  wynik["docx_list"]  — lista załączników

UWAGA: sygnatura build_analiza_section rozszerzona o sender / sender_name.
W app.py znajdź wywołanie i dodaj te parametry.
"""

import os
import re
import json
import base64
import logging
import time
from typing import Optional

import requests
from flask import current_app

from .analiza_diagram import generate_svg_html_interactive
from core.logging_reporter import get_logger

logger = logging.getLogger(__name__)
execution_logger = get_logger()

# ── KLUCZE API ────────────────────────────────────────────────────────────────

_DEEPSEEK_KEY = os.getenv("API_KEY_DEEPSEEK", "").strip()

MAX_PYTANIA = 5
MAX_RUNDY = 2  # Zmniejszone z 3 → j.w.


# ── DEEPSEEK ───────────────────────────────────────────────────────────


def _deepseek_call(prompt: str, system: str, max_tokens: int = 3500) -> Optional[str]:
    if not _DEEPSEEK_KEY:
        execution_logger.log_debug_info("DEEPSEEK", "Brak klucza API", "WARNING")
        return None

    start_time = time.time()
    try:
        execution_logger.log_debug_info(
            "DEEPSEEK_REQUEST",
            {
                "prompt_length": len(prompt),
                "system_length": len(system),
                "max_tokens": max_tokens,
                "prompt_preview": prompt[:200] + "..." if len(prompt) > 200 else prompt,
            },
        )

        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.88,
            },
            timeout=90,
        )

        duration = time.time() - start_time

        if resp.status_code == 200:
            response_text = resp.json()["choices"][0]["message"]["content"].strip()

            # Loguj pełną odpowiedź AI dla debugowania
            execution_logger.log_ai_response(
                ai_name="DeepSeek",
                prompt=prompt,
                response=response_text,
                tokens_used=max_tokens,  # Przybliżone
                duration_sec=duration,
            )

            execution_logger.log_api_call(
                api_name="DeepSeek",
                model="deepseek-chat",
                tokens_used=max_tokens,
                duration_sec=duration,
                success=True,
            )

            return response_text
        else:
            execution_logger.log_api_call(
                api_name="DeepSeek",
                model="deepseek-chat",
                duration_sec=duration,
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
            logger.error(
                "[dociekliwy] DeepSeek HTTP %d: %s", resp.status_code, resp.text[:200]
            )
    except Exception as e:
        duration = time.time() - start_time
        execution_logger.log_api_call(
            api_name="DeepSeek",
            model="deepseek-chat",
            duration_sec=duration,
            success=False,
            error=str(e),
        )
        logger.error("[dociekliwy] DeepSeek error: %s", e)
    return None


def _deepseek_korekta(raw: str) -> str:
    """DeepSeek sprawdza gramatykę całego JSON-a bez zmiany struktury."""
    """DeepSeek sprawdza gramatykę całego JSON-a bez zmiany struktury."""
    if not _DEEPSEEK_KEY:
        return raw
    system = (
        "Jesteś redaktorem polskiego tekstu komediowego. "
        "Otrzymujesz JSON. Popraw TYLKO błędy gramatyczne i interpunkcję w wartościach tekstowych. "
        "NIE zmieniaj kluczy JSON, struktury, cudzysłowów JSON ani znaczenia zdań. "
        "Odpowiedz WYŁĄCZNIE poprawionym JSON-em, bez komentarzy, bez backtick-ów."
    )
    wynik = _deepseek_call(raw, system, max_tokens=4000)
    return wynik if wynik else raw


# ── GENEROWANIE CAŁEJ GRY ─────────────────────────────────────────────────────

_SYSTEM_ERYK = (
    "Jesteś Erykiem — mistrzem pasywno-agresywnego uniku i pseudofilozoficznego doprecyzowywania. "
    "Twój cel: NIE odpowiadać na pytanie rozmówcy. Wciągasz go w nieskończoną króliczą norę pytań. "
    "Styl: absurdalny, biurokratyczny, ironiczny, lekko paranoidalny. Piszesz PO POLSKU. "
    "Logika Eryka: wyciągasz BŁĘDNE wnioski z poprawnych odpowiedzi. Każdy wybór rozmówcy jest dowodem na coś absurdalnego. "
    "ZAKAZ używania słów: przepraszam, oczywiście, chętnie, rozumiem. "
    "Każde pytanie pochodzi z INNEJ dziedziny: filozofia, biologia, prawo, kosmologia, kulinaria, "
    "heraldyka, stomatologia, meteorologia, filologia, ekonomia, ogrodnictwo itd."
)


def _generuj_gre(body: str, sender_name: str, max_pytania: int = MAX_PYTANIA) -> Optional[dict]:
    """
    Generuje kompletną grę jednym wywołaniem AI.
    max_pytania: 2 normalnie, 5 gdy słowo PILNE w treści.
    """
    prompt = f"""Rozmówca "{sender_name or 'Anonim'}" napisał:
"{body[:400]}"

Wygeneruj grę Eryka: {max_pytania} pytania, każde z {MAX_RUNDY} rundami po 3 opcje (A/B/C).

Odpowiedz WYŁĄCZNIE czystym JSON bez backtick-ów, bez komentarzy:
{{
  "pytania": [
    {{
      "id": "P1",
      "tresc": "Konkretne pytanie wynikające z wiadomości rozmówcy?",
      "opcje": {{
        "A": {{
          "tekst": "Krótka odpowiedź A",
          "reakcja": "Absurdalna reakcja Eryka na A.",
          "runda2": {{
            "pytanie": "Drugie pytanie Eryka po wyborze A?",
            "opcje": {{
              "A": {{"tekst": "Odpowiedź A2", "reakcja": "Reakcja na A2."}},
              "B": {{"tekst": "Odpowiedź B2", "reakcja": "Reakcja na B2."}},
              "C": {{"tekst": "Odpowiedź C2", "reakcja": "Reakcja na C2."}}
            }}
          }}
        }},
        "B": {{
          "tekst": "Krótka odpowiedź B",
          "reakcja": "Absurdalna reakcja Eryka na B.",
          "runda2": {{
            "pytanie": "Drugie pytanie Eryka po wyborze B?",
            "opcje": {{
              "A": {{"tekst": "Odpowiedź A2", "reakcja": "Reakcja na A2."}},
              "B": {{"tekst": "Odpowiedź B2", "reakcja": "Reakcja na B2."}},
              "C": {{"tekst": "Odpowiedź C2", "reakcja": "Reakcja na C2."}}
            }}
          }}
        }},
        "C": {{
          "tekst": "Krótka odpowiedź C",
          "reakcja": "Absurdalna reakcja Eryka na C.",
          "runda2": {{
            "pytanie": "Drugie pytanie Eryka po wyborze C?",
            "opcje": {{
              "A": {{"tekst": "Odpowiedź A2", "reakcja": "Reakcja na A2."}},
              "B": {{"tekst": "Odpowiedź B2", "reakcja": "Reakcja na B2."}},
              "C": {{"tekst": "Odpowiedź C2", "reakcja": "Reakcja na C2."}}
            }}
          }}
        }}
      }}
    }},
    {{
      "id": "P2",
      "tresc": "Drugie konkretne pytanie z innej dziedziny?",
      "opcje": {{
        "A": {{"tekst": "...", "reakcja": "...", "runda2": {{"pytanie": "...", "opcje": {{"A": {{"tekst": "...", "reakcja": "..."}}, "B": {{"tekst": "...", "reakcja": "..."}}, "C": {{"tekst": "...", "reakcja": "..."}}}}}}}},
        "B": {{"tekst": "...", "reakcja": "...", "runda2": {{"pytanie": "...", "opcje": {{"A": {{"tekst": "...", "reakcja": "..."}}, "B": {{"tekst": "...", "reakcja": "..."}}, "C": {{"tekst": "...", "reakcja": "..."}}}}}}}},
        "C": {{"tekst": "...", "reakcja": "...", "runda2": {{"pytanie": "...", "opcje": {{"A": {{"tekst": "...", "reakcja": "..."}}, "B": {{"tekst": "...", "reakcja": "..."}}, "C": {{"tekst": "...", "reakcja": "..."}}}}}}}}
      }}
    }}
  ],
  "wyrok": "Absurdalny wyrok końcowy. Z pozdrowieniami, Eryk."
}}

Zasady:
- Pytania konkretne, wynikające z treści wiadomości rozmówcy
- Reakcje krótkie (1-2 zdania), absurdalne, biurokratyczne
- Każde pytanie z innej dziedziny (filozofia, biologia, prawo, kosmologia, kulinaria itp.)
- Wyrok absurdalny ale logiczny po erykowemu
- Wyrok NIE może zawierać żadnych liczb ani statystyk (zakaz: "X pytań", "Y odpowiedzi", "Z%") — tylko sam absurdalny tekst"""

    raw = _deepseek_call(prompt, _SYSTEM_ERYK, max_tokens=4000 if max_pytania <= 2 else 7000)
    if not raw:
        return None

    return _parse_json_safe(raw)


def _parse_json_safe(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"```\s*$", "", raw.strip())
    # Usuń komentarze JS-style (// ...) które AI czasem wstawia
    raw = re.sub(r"//[^\n]*", "", raw)
    # Usuń trailing commas przed } i ] — częsty błąd AI
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        data = json.loads(raw)
        if _validate_gra_structure(data):
            return data
        logger.warning(
            "[eryk] JSON sparsowany ale struktura niepoprawna — próba naprawy"
        )
    except json.JSONDecodeError:
        pass

    # Próba naprawienia uciętego JSON
    repaired = _repair_json(raw)
    if repaired:
        try:
            data = json.loads(repaired)
            if _validate_gra_structure(data):
                return data
            # Struktura niekompletna po naprawie — lepszy fallback niż zepsute dane
            logger.warning(
                "[eryk] JSON naprawiony ale struktura niekompletna — używam fallback"
            )
            return None
        except Exception:
            pass

    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            if _validate_gra_structure(data):
                return data
        except Exception:
            pass

    logger.warning("[eryk] JSON parse failed: %s", raw[:200])
    return None


def _validate_gra_structure(data: dict) -> bool:
    """Sprawdza czy JSON ma minimalną poprawną strukturę gry."""
    if not isinstance(data, dict):
        return False
    pytania = data.get("pytania")
    if not isinstance(pytania, list) or not pytania:
        return False
    # Pierwsze pytanie musi mieć tresc i opcje z co najmniej jedną opcją
    p0 = pytania[0]
    if not isinstance(p0, dict):
        return False
    if not p0.get("tresc"):
        return False
    opcje = p0.get("opcje", {})
    if not isinstance(opcje, dict) or not opcje:
        return False
    # Przynajmniej jedna opcja musi mieć tekst
    for v in opcje.values():
        if isinstance(v, dict) and v.get("tekst"):
            return True
    return False


def _repair_json(raw: str) -> Optional[str]:
    """Naprawa uciętego JSON — zamyka otwarte stringi, tablice i obiekty."""
    raw = raw.strip()
    if not raw.startswith("{"):
        return None

    # Usuń trailing commas
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

    # Jeśli JSON jest ucięty w środku stringa — zamknij string
    # Liczymy cudzysłowy niezescapowane
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string

    if in_string:
        raw += '"'  # zamknij otwarty string

    # Usuń ostatni przecinek przed uzupełnieniem nawiasów
    raw = re.sub(r",\s*$", "", raw.strip())

    # Uzupełnij brakujące nawiasy
    stack = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack and stack[-1] == ch:
                    stack.pop()

    if stack:
        raw += "".join(reversed(stack))

    return raw


# ── FALLBACK ──────────────────────────────────────────────────────────────────


def _fallback_gra() -> dict:
    """Fallback dla drzewiastej struktury (3 pytania, każde z 3 rundami)."""
    pytania = []
    for i in range(1, MAX_PYTANIA + 1):
        # Budujemy drzewo o głębokości MAX_RUNDY
        def build_tree(rundy_pozostale: int, prefix: str = ""):
            if rundy_pozostale == 0:
                return None
            opcje = {}
            for lit in ["A", "B", "C"]:
                tekst = f"Opcja {lit} w rundzie {MAX_RUNDY - rundy_pozostale + 1}"
                reakcja = f"Wybrałeś {lit}. To sugeruje, że {['nie rozumiesz pytania', 'jesteś zbyt pewny siebie', 'masz ukryte motywy'][ord(lit)-65]}."
                if rundy_pozostale > 1:
                    opcje[lit] = {
                        "tekst": tekst,
                        "reakcja": reakcja,
                        f"runda{MAX_RUNDY - rundy_pozostale + 2}": {
                            "pytanie": f"Kolejne doprecyzowanie (runda {MAX_RUNDY - rundy_pozostale + 2})",
                            "opcje": build_tree(rundy_pozostale - 1, prefix + lit),
                        },
                    }
                else:
                    opcje[lit] = {"tekst": tekst, "reakcja": reakcja}
            return opcje

        opcje_tree = build_tree(MAX_RUNDY)
        pytania.append(
            {
                "id": f"P{i}",
                "tresc": f"Pytanie fallback {i}: Co masz na myśli?",
                "opcje": opcje_tree,
            }
        )

    return {
        "pytania": pytania,
        "wyrok": (
            f"Po analizie Twoich wyborów w {MAX_PYTANIA} pytaniach stwierdzam, że Twoja pierwotna wiadomość "
            "była testem Turinga przeprowadzonym na mnie bez mojej zgody. "
            "Niestety, to Ty oblałeś test — jako człowiek. "
            "Z pozdrowieniami, Eryk."
        ),
    }


# ── HTML MAILA (CSS :target, zero JS) ────────────────────────────────────────


def _buduj_html_email_pierwsza_gra(
    gra: dict,
    sender_name: str,
    diagram_jpg_b64: str,  # zachowany dla kompatybilności sygnatury, nieużywany
    body_original: str = "",
    is_pilne: bool = False,
) -> str:
    """
    Buduje STATYCZNY HTML reply_html (bez JS) — kompatybilny z Gmail/Google.
    Na górze: link Drive ({DRIVE_LINK_PLACEHOLDER}) + info o załączniku .txt.
    Poniżej: lista pytań i opcji do wglądu (statyczna, bez interaktywności).
    """
    pytania = gra.get("pytania", [])
    if not pytania:
        return "<p>Brak pytań gry.</p>"

    sn = sender_name or "Użytkowniku"
    pilne_badge = "&#9888; PILNE &mdash; " if is_pilne else ""

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # ── Sekcja oryginalnej treści emaila ──────────────────────────────────────
    body_escaped = esc(body_original)
    body_html = f"""<div style="background:#f5f0e8;border-left:3px solid #8b6914;padding:10px 14px;
  margin:0 0 14px;border-radius:0 4px 4px 0;font-family:Arial,sans-serif">
<div style="font-size:10px;color:#8b6914;font-weight:bold;letter-spacing:1px;margin-bottom:5px">
  &#9993; Twoja wiadomość:</div>
<div style="font-size:12px;color:#444;line-height:1.6;white-space:pre-wrap;word-break:break-word">{body_escaped}</div>
</div>""" if body_escaped.strip() else ""

    # ── Statyczna lista pytań i opcji ─────────────────────────────────────────
    KOLORY_OPC = [
        ("#E6F1FB", "#185FA5", "#0C447C"),
        ("#E1F5EE", "#0F6E56", "#085041"),
        ("#FAEEDA", "#854F0B", "#633806"),
    ]
    LIT = ["A", "B", "C"]

    pytania_html = ""
    for pi, pytanie in enumerate(pytania, 1):
        tresc = esc(pytanie.get("tresc", f"Pytanie {pi}"))
        opcje = pytanie.get("opcje", {})

        opcje_html = ""
        for li, lit in enumerate(LIT):
            if lit not in opcje:
                continue
            op = opcje[lit]
            tekst_op = esc(op.get("tekst", lit))
            bg, border, col = KOLORY_OPC[li]
            opcje_html += f"""<div style="flex:1;background:{bg};border:1.5px solid {border};
  border-radius:6px;padding:10px;font-family:Arial,sans-serif">
  <div style="font-size:9px;font-weight:bold;letter-spacing:1px;color:{col};margin-bottom:4px">{lit})</div>
  <div style="font-size:11px;color:{col};line-height:1.4">{tekst_op}</div>
</div>
"""

        pytania_html += f"""<div style="margin-bottom:18px">
<div style="background:#EEEDFE;border:2px solid #534AB7;border-radius:8px;
  padding:12px 18px;margin-bottom:8px;text-align:center;font-family:Arial,sans-serif">
  <div style="font-size:10px;color:#534AB7;font-weight:bold;letter-spacing:2px;margin-bottom:4px">PYTANIE {pi}</div>
  <div style="font-size:13px;color:#3C3489;font-weight:bold;line-height:1.4">{tresc}</div>
</div>
<div style="display:flex;gap:8px;flex-wrap:wrap">
{opcje_html}
</div>
</div>
"""

    wyrok_tresc = esc(gra.get("wyrok", ""))
    wyrok_html = f"""<div style="background:#3C3489;color:#F1EFE8;border-radius:8px;
  padding:14px 18px;margin-top:8px;font-family:Arial,sans-serif;font-size:12px;line-height:1.6">
  <div style="font-weight:bold;margin-bottom:6px;font-size:11px;letter-spacing:1px">&#9878; WYROK KO&#323;COWY</div>
  {wyrok_tresc}
</div>""" if wyrok_tresc else ""

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Eryk Responder&#8482; &mdash; {esc(sn)}</title>
</head>
<body style="background:#FAF8F4;font-family:Arial,sans-serif;margin:0;padding:0">
<div style="padding:16px;max-width:900px;margin:0 auto">

  <div style="background:#2C2C2A;color:#F1EFE8;text-align:center;padding:10px 20px;
    border-radius:8px;font-size:13px;font-weight:bold;margin-bottom:12px;letter-spacing:1px">
    ERYK RESPONDER&#8482; &middot; {pilne_badge}Drzewo Docieklivo&#347;ci &middot; {esc(sn)}
  </div>

  <!-- LINK DO DRIVE — podmieniony przez zwykly.py -->
  <div style="text-align:center;margin:0 0 14px">
    <a href="{{DRIVE_LINK_PLACEHOLDER}}" target="_blank"
       style="display:inline-block;background:#1a1a2e;color:#e8d5b0;
         padding:11px 28px;font-family:'Courier New',monospace;font-size:13px;
         text-decoration:none;border:1px solid #8b6914;border-radius:4px;
         font-weight:bold;letter-spacing:1px">
      &#9654; Kliknij w link by otrzyma&#263; odpowied&#378;
    </a>
  </div>

  {body_html}

  <!-- STATYCZNY PODGLĄD PYTAŃ -->
  <div style="background:#F0EEFE;border:1px solid #534AB7;border-radius:6px;
    padding:10px 14px;margin-bottom:14px;font-size:11px;color:#534AB7;text-align:center;
    font-style:italic">
    Poni&#380;ej podgl&#261;d struktury pyta&#324; Eryka (wersja statyczna):
  </div>

  {pytania_html}
  {wyrok_html}

  <div style="font-size:10px;color:#aaa;text-align:center;padding:14px 8px">
    Eryk Responder&#8482; &middot; Wygenerowano automatycznie &middot; Odpowied&#378; nast&#261;pi w odpowiednim czasie
  </div>

</div>
</body>
</html>"""


# ── gra.html ZAŁĄCZNIK (pełny JS) ────────────────────────────────────────────


def _buduj_gra_html(gra: dict, sender_name: str) -> str:
    gra_json = json.dumps(gra, ensure_ascii=False)
    sn = sender_name or "Anonim"
    max_pytania = MAX_PYTANIA
    max_rundy = MAX_RUNDY

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Eryk Responder™</title>
<style>
:root{{--ink:#1a1a2e;--paper:#f5f0e8;--gold:#8b6914;--cream:#e8d5b0;--mid:#c8b89a;--dim:#6a5a3a;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--paper);color:var(--ink);font-family:'Courier New',monospace;
      min-height:100vh;display:flex;align-items:center;justify-content:center;padding:40px 16px;}}
body::before{{content:'';position:fixed;inset:0;
  background-image:repeating-linear-gradient(0deg,transparent,transparent 27px,rgba(0,0,0,.035) 28px);
  pointer-events:none;}}
.karta{{max-width:660px;width:100%;background:#fff;border:2px solid var(--ink);box-shadow:8px 8px 0 var(--ink);}}
header{{background:var(--ink);color:var(--cream);padding:26px 32px 18px;border-bottom:4px solid var(--gold);}}
header h1{{font-size:24px;letter-spacing:3px;margin-bottom:4px;}}
header .sub{{font-size:10px;color:var(--dim);letter-spacing:4px;text-transform:uppercase;}}
.pbar{{height:4px;background:var(--mid);}}
.pfill{{height:100%;background:var(--gold);transition:width .5s ease;width:0%;}}
.cialo{{padding:30px 32px;}}
.knr{{font-size:10px;color:var(--gold);letter-spacing:5px;text-transform:uppercase;margin-bottom:10px;}}
.intro{{font-style:italic;color:#666;font-size:13px;padding:9px 13px;
        border-left:3px solid var(--mid);margin-bottom:16px;
        opacity:0;animation:fade .45s .1s forwards;}}
.pyt{{font-size:18px;font-weight:bold;line-height:1.5;margin-bottom:22px;
      color:var(--ink);opacity:0;animation:fade .45s .22s forwards;}}
.opcje{{display:flex;flex-direction:column;gap:9px;}}
.bopc{{padding:11px 18px;background:var(--ink);color:var(--cream);border:none;cursor:pointer;
       font-family:'Courier New',monospace;font-size:13px;text-align:left;
       transition:background .15s,transform .1s;opacity:0;animation:fade .45s forwards;}}
.bopc:nth-child(1){{animation-delay:.34s;}}
.bopc:nth-child(2){{animation-delay:.44s;}}
.bopc:nth-child(3){{animation-delay:.54s;}}
.bopc:hover{{background:#2d2d4e;transform:translateX(4px);}}
.bopc:disabled{{opacity:.4;cursor:not-allowed;transform:none;}}
.lit{{color:var(--gold);font-weight:bold;margin-right:10px;}}
.rbox{{margin-top:18px;padding:14px 18px;background:var(--paper);
       border:1px solid var(--mid);border-left:4px solid var(--gold);
       font-style:italic;font-size:13px;color:#444;display:none;animation:fade .4s forwards;}}
.bdalej{{margin-top:14px;padding:9px 26px;background:var(--gold);color:var(--paper);
         border:none;cursor:pointer;font-family:'Courier New',monospace;font-size:11px;
         letter-spacing:3px;text-transform:uppercase;display:none;}}
.bdalej:hover{{opacity:.82;}}
/* Wyrok */
#ekw{{display:none;background:var(--ink);color:var(--cream);padding:32px;}}
#ekw h2{{color:var(--gold);font-size:20px;letter-spacing:2px;margin-bottom:18px;}}
.wt{{font-size:14px;line-height:1.8;color:#d4c5a0;opacity:0;animation:fade .6s .3s forwards;}}
.prot{{margin-top:20px;font-size:10px;color:var(--dim);letter-spacing:1px;
       border-top:1px solid #333;padding-top:14px;}}
footer{{padding:13px 32px;font-size:10px;color:var(--mid);letter-spacing:2px;text-align:center;border-top:1px solid var(--mid);}}
@keyframes fade{{from{{opacity:0;transform:translateY(7px);}}to{{opacity:1;transform:translateY(0);}}}}
</style>
</head>
<body>
<div class="karta">
  <header>
    <h1>ERYK RESPONDER™</h1>
    <div class="sub">System Zaawansowanego Doprecyzowywania · Sesja: {sn}</div>
  </header>
  <div class="pbar"><div class="pfill" id="pf"></div></div>
  <div id="ekg">
    <div class="cialo">
      <div class="knr"  id="knr"></div>
      <div class="intro" id="intro"></div>
      <div class="pyt"   id="pyt"></div>
      <div class="opcje" id="opcje"></div>
      <div class="rbox"  id="rbox"></div>
      <button class="bdalej" id="bdalej" onclick="dalej()">→ Dalej</button>
    </div>
  </div>
  <div id="ekw">
    <h2>⚖ WYROK KOŃCOWY</h2>
    <div class="wt"  id="wt"></div>
    <div class="prot" id="prot"></div>
  </div>
  <footer>Eryk Responder™ v3.0 (drzewiasty) &#160;·&#160; Dziękuje za cierpliwość i żałuje, że jej nie miał.</footer>
</div>
<script>
const G = {gra_json};
const MAX_PYTANIA = {max_pytania};
const MAX_RUNDY = {max_rundy};
let currentPytanieIndex = 0;
let currentRound = 1;
let currentPath = {{}}; // ścieżka: {{pytanieIndex: {{round: choice, ...}}}}
let hist = [];

function getCurrentNode() {{
    if (currentPytanieIndex >= G.pytania.length) return null;
    let node = G.pytania[currentPytanieIndex];
    // przejdź po ścieżce dla tego pytania
    let path = currentPath[currentPytanieIndex];
    if (path) {{
        for (let round = 1; round <= currentRound - 1; round++) {{
            let choice = path[round];
            if (choice && node.opcje && node.opcje[choice]) {{
                node = node.opcje[choice];
                if (node['runda' + (round + 1)]) {{
                    node = node['runda' + (round + 1)];
                }}
            }}
        }}
    }}
    return node;
}}

function render(){{
    const node = getCurrentNode();
    if (!node) {{
        wyrok();
        return;
    }}
    const pytanie = G.pytania[currentPytanieIndex];
    const pytanieNr = currentPytanieIndex + 1;
    const totalPytania = G.pytania.length;
    const totalProgress = ((currentPytanieIndex * MAX_RUNDY + (currentRound - 1)) / (totalPytania * MAX_RUNDY)) * 100;
    
    document.getElementById('knr').textContent = `Pytanie ${{pytanieNr}} z ${{totalPytania}} · Runda ${{currentRound}} z ${{MAX_RUNDY}}`;
    document.getElementById('pf').style.width = `${{totalProgress}}%`;
    ['intro','pyt'].forEach(id=>{{
        const el=document.getElementById(id);
        el.style.animation='none'; el.offsetHeight; el.style.animation='';
    }});
    document.getElementById('intro').textContent = 'Eryk potrzebuje doprecyzowania:';
    document.getElementById('pyt').textContent = node.pytanie || node.tresc || '';
    document.getElementById('rbox').style.display='none';
    document.getElementById('bdalej').style.display='none';
    const op=document.getElementById('opcje');
    op.innerHTML='';
    
    const opcje = node.opcje || {{}};
    Object.entries(opcje).forEach(([lit,val],idx)=>{{
        const b=document.createElement('button');
        b.className='bopc';
        b.style.animationDelay=`${{0.34+idx*0.1}}s`;
        b.innerHTML=`<span class="lit">${{lit}})</span>${{val.tekst}}`;
        b.onclick=()=>wybierz(lit,val.tekst,val.reakcja);
        op.appendChild(b);
    }});
}}

function wybierz(lit,tekst,reakcja){{
    if (!currentPath[currentPytanieIndex]) currentPath[currentPytanieIndex] = {{}};
    currentPath[currentPytanieIndex][currentRound] = lit;
    hist.push(`Pytanie ${{currentPytanieIndex+1}}, runda ${{currentRound}}: ${{lit}}) ${{tekst}}`);
    document.querySelectorAll('.bopc').forEach(b=>b.disabled=true);
    const rb=document.getElementById('rbox');
    rb.textContent=reakcja; rb.style.display='block';
    rb.style.animation='none'; rb.offsetHeight; rb.style.animation='';
    const bd=document.getElementById('bdalej');
    bd.style.display='inline-block';
    // Sprawdź czy to ostatnia runda w tym pytaniu
    const node = getCurrentNode();
    const opcje = node.opcje || {{}};
    const wybrana = opcje[lit];
    const maDalej = wybrana && wybrana['runda' + (currentRound + 1)];
    bd.textContent = maDalej ? '→ Następna runda' : 
                     (currentPytanieIndex + 1 < G.pytania.length) ? '→ Następne pytanie' : '⚖ Poznaj wyrok Eryka';
}}

function dalej(){{
    const node = getCurrentNode();
    const opcje = node.opcje || {{}};
    const wybranaLit = currentPath[currentPytanieIndex]?.[currentRound];
    const wybrana = opcje[wybranaLit];
    const maDalej = wybrana && wybrana['runda' + (currentRound + 1)];
    
    if (maDalej) {{
        currentRound++;
    }} else {{
        // przejdź do następnego pytania
        currentPytanieIndex++;
        currentRound = 1;
    }}
    
    if (currentPytanieIndex >= G.pytania.length) {{
        wyrok();
        return;
    }}
    render();
}}

function wyrok(){{
    document.getElementById('ekg').style.display='none';
    document.getElementById('ekw').style.display='block';
    document.getElementById('pf').style.width='100%';
    document.getElementById('wt').innerHTML = G.wyrok.replace(/\\n/g,'<br>');
    const protHist = hist.map(h=>`· ${{h}}`).join('<br>');
    document.getElementById('prot').innerHTML =
        'Protokół wyborów:<br>' + protHist +
        '<br><br>Korespondent uznany za: NIEJASNY · Sesja zamknięta.';
}}

render();
</script>
</body>
</html>"""


# ── GŁÓWNA FUNKCJA ────────────────────────────────────────────────────────────


def build_dociekliwy_section(
    body: str,
    attachments: list = None,
    sender: str = "",
    sender_email: str = "",
    sender_name: str = "",
    data: dict = None,
    test_mode: bool = False,
) -> dict:
    # Kompatybilność: app.py przekazuje sender_email= zamiast sender=
    if not sender and sender_email:
        sender = sender_email
    # Kompatybilność: app.py przekazuje data= — wyciągamy sender_name jeśli brak
    if not sender_name and data:
        sender_name = data.get("sender_name", "")
    """
    Eryk Responder - generuje grę logiczną.

    Parametr test_mode:
    - Jeśli test_mode=True (z KEYWORDS_TEST via app.py disable_flux),
      analiza.py może wy generowanie Flux jeśli to funkcjonuje w tym responderycie.

    Zwraca:
      reply_html — treść maila (CSS :target, bez JS)
      gra_html   — plik HTML do załączenia jako pojedynczy attachment
      docx_list  — [{"base64":..., "filename":"eryk_gra.htm", "content_type":"text/htm"}]

    W app.py zaktualizuj wywołanie:
      build_analiza_section(body, attachments,
                            sender=sender, sender_name=sender_name, test_mode=disable_flux)
    """
    logger.info(
        "[ERYK_START] ═══════════════════════════════════════════════════════════"
    )
    logger.info(
        "[ERYK] Rozpoczęto build_dociekliwy_section dla sender=%s", sender or "?"
    )
    logger.info(
        "[ERYK] Body length: %d | Attachments: %d",
        len(body or ""),
        len(attachments or []),
    )

    # Loguj dane wejściowe dla programisty
    execution_logger.log_input(sender, "dociekliwy_request", body, sender_name)
    execution_logger.log_pipeline_step(
        "dociekliwy_start",
        {
            "body_length": len(body or ""),
            "attachments_count": len(attachments or []),
            "sender": sender,
            "sender_name": sender_name,
            "test_mode": test_mode,
        },
    )

    # Loguj użycie pamięci na początku
    execution_logger.log_memory_usage()

    if not body or not body.strip():
        logger.warning("[ERYK] Pusta wiadomość — skipping")
        return {
            "reply_html": "<p>Edek nie odpowie na pustą wiadomość. Pojęcie 'pustości' wymaga uprzedniego doprecyzowania.</p>",
            "docx_list": [],
        }

    sn = sender_name or ""

    # ── Wykryj PILNE — jeśli słowo 'pilne' (case-insensitive) w treści ────────
    is_pilne = bool(re.search(r'\bpilne\b', body, re.IGNORECASE))
    max_pytania_sesja = 5 if is_pilne else MAX_PYTANIA
    logger.info("[ERYK] Tryb PILNE: %s → %d pytań", is_pilne, max_pytania_sesja)

    # ── Generuj całą grę jednym wywołaniem AI ─────────────────────────────────
    logger.info("[ERYK] Krok 1: Generowanie struktury gry za pomocą DeepSeek...")
    gra_data = _generuj_gre(body, sn, max_pytania=max_pytania_sesja)

    if (
        not gra_data
        or not isinstance(gra_data.get("pytania"), list)
        or not gra_data["pytania"]
    ):
        logger.warning("[ERYK] ⚠️ Brak danych z AI — użycie fallback'u")
        gra_data = _fallback_gra()
        logger.info("[ERYK] Fallback gra: %d pytań", len(gra_data.get("pytania", [])))
    else:
        logger.info(
            "[ERYK] ✓ Gra wygenerowana: %d pytań", len(gra_data.get("pytania", []))
        )

    # Log JSON struktury (dla debugowania)
    gra_json_to_log = json.dumps(gra_data, ensure_ascii=False)
    logger.debug(
        "[ERYK_JSON] Struktura gry do HTML:\n%s",
        (
            gra_json_to_log[:2000]
            if len(gra_json_to_log) < 5000
            else gra_json_to_log[:2000] + "...[UCIĘTY]..."
        ),
    )

    # ── GENERUJ SVG HTML interaktywny (diagram — załącznik + Drive) ──────────
    logger.info("[ERYK] Krok 2: Generowanie interaktywnego SVG HTML...")
    # Skróć body do max 120 znaków jako tytuł strony
    tytul_strony = (body or "").strip()
    tytul_strony = re.sub(r'\s+', ' ', tytul_strony)[:120]
    diagram_svg_html = generate_svg_html_interactive(gra_data, sn, tytul=tytul_strony)
    if diagram_svg_html:
        diagram_svg_b64 = base64.b64encode(
            b"\xef\xbb\xbf" + diagram_svg_html.encode("utf-8")
        ).decode("ascii")
        logger.info(
            "[ERYK] ✓ SVG diagram: %d bytes → base64: %d chars",
            len(diagram_svg_html.encode("utf-8")),
            len(diagram_svg_b64),
        )
    else:
        logger.warning("[ERYK] ⚠️ Nie wygenerowano SVG diagramu")
        diagram_svg_b64 = ""

    # ── Buduj dynamiczny HTML do maila ────────────────────────────────────────
    logger.info("[ERYK] Krok 3: Budowanie reply_html...")
    reply_html = _buduj_html_email_pierwsza_gra(
        gra_data, sn,
        diagram_jpg_b64="",
        body_original=body,
        is_pilne=is_pilne,
    )

    # ── Upload HTML gry do Google Drive ──────────────────────────────────────
    drive_link = ""
    if diagram_svg_b64:
        try:
            try:
                from core.drive_utils import upload_file_to_drive
            except ImportError:
                from drive_utils import upload_file_to_drive
            folder_id = os.getenv("DRIVE_FOLDER_DOCIEKLIWY", "").strip()
            import time as _time
            filename = f"eryk_gra_{int(_time.time())}.html"
            result = upload_file_to_drive(
                file_data=diagram_svg_b64,
                filename=filename,
                mime_type="text/html",
                folder_id=folder_id if folder_id else None,
            )
            if result:
                drive_link = result.get("url", "")
                logger.info("[ERYK] ✓ Upload Drive: %s", drive_link)
            else:
                logger.warning("[ERYK] ⚠️ Upload Drive zwrócił None")
        except Exception as e:
            logger.error("[ERYK] Błąd uploadu do Drive: %s", e)

    # Podmień placeholder linkiem
    reply_html = reply_html.replace("{DRIVE_LINK_PLACEHOLDER}", drive_link or "#")

    logger.info("[ERYK] ✓ reply_html: %d bytes", len(reply_html))

    logger.info(
        "[ERYK_END] ═══════════════════════════════════════════════════════════"
    )
    logger.info(
        "[ERYK_SUMMARY] Wygenerowano grę: %d pytań | PILNE=%s | sender=%s | drive=%s",
        len(gra_data.get("pytania", [])),
        is_pilne,
        sender or "?",
        drive_link or "brak",
    )

    return {
        "reply_html": reply_html,
        "htm_for_drive": {
            "base64": diagram_svg_b64,
            "filename": "eryk_diagram_interaktywny.htm",
            "content_type": "text/html",
        },
        "drive_link": drive_link,
        "docx_list": [],  # brak zalacznikow — link do Drive w body wystarczy
    }
