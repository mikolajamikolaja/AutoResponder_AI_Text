"""
responders/analiza.py
Responder KEYWORDS3 — Edek Responder (Mistrz Pasywno-Agresywnego Doprecyzowywania).

Render generuje JEDEN RAZ wszystkie 10 kroków (pytania + opcje + reakcje Edka).
Dostarcza dwie rzeczy jednocześnie:

  1. reply_html  — treść maila z CSS :target "grą" (bez JS, działa w klientach pocztowych)
  2. docx_list   — [{base64, filename, content_type}] z edek_gra.html (pełny JS, załącznik)

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
from typing import Optional

import requests
from flask import current_app

logger = logging.getLogger(__name__)

# ── KLUCZE API ────────────────────────────────────────────────────────────────
_GROQ_KEYS = [k.strip() for k in [
    os.getenv("API_KEY_GROQ",   ""),
    os.getenv("API_KEY_GROQ_2", ""),
    os.getenv("API_KEY_GROQ_3", ""),
] if k.strip()]

_DEEPSEEK_KEY = os.getenv("API_KEY_DEEPSEEK", "").strip()

MAX_KROKOW = 10


# ── GROQ / DEEPSEEK ───────────────────────────────────────────────────────────

def _groq_call(prompt: str, system: str, max_tokens: int = 3500) -> Optional[str]:
    for key in _GROQ_KEYS:
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.93,
                },
                timeout=90,
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            logger.warning("[edek] Groq %s → HTTP %d", key[:8], resp.status_code)
        except Exception as e:
            logger.warning("[edek] Groq error: %s", e)
    return _deepseek_call(prompt, system, max_tokens)


def _deepseek_call(prompt: str, system: str, max_tokens: int = 3500) -> Optional[str]:
    if not _DEEPSEEK_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {_DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.88,
            },
            timeout=90,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        logger.error("[edek] DeepSeek HTTP %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("[edek] DeepSeek error: %s", e)
    return None


def _deepseek_korekta(raw: str) -> str:
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

_SYSTEM_EDEK = (
    "Jesteś Edkiem — mistrzem pasywno-agresywnego uniku i pseudofilozoficznego doprecyzowywania. "
    "Twój cel: NIE odpowiadać na pytanie rozmówcy. Wciągasz go w nieskończoną króliczą norę pytań. "
    "Styl: absurdalny, biurokratyczny, ironiczny, lekko paranoidalny. Piszesz PO POLSKU. "
    "Logika Edka: wyciągasz BŁĘDNE wnioski z poprawnych odpowiedzi. Każdy wybór rozmówcy jest dowodem na coś absurdalnego. "
    "ZAKAZ używania słów: przepraszam, oczywiście, chętnie, rozumiem. "
    "Każde pytanie pochodzi z INNEJ dziedziny: filozofia, biologia, prawo, kosmologia, kulinaria, "
    "heraldyka, stomatologia, meteorologia, filologia, ekonomia, ogrodnictwo itd."
)


def _generuj_gre(body: str, sender_name: str) -> Optional[dict]:
    """
    Generuje kompletną grę jednym wywołaniem AI.
    Zwraca dict z kluczami 'kroki' (lista) i 'wyrok' (str).
    """
    prompt = f"""Rozmówca "{sender_name or 'Anonim'}" napisał do Edka:
"{body[:500]}"

Wygeneruj kompletną grę Edka: dokładnie {MAX_KROKOW} kroków + wyrok końcowy.

Zasady BEZWZGLĘDNE:
- Krok 1: pytanie wynika BEZPOŚREDNIO z treści wiadomości (konkretne słowo lub fraza)
- Krok N+1: pytanie wynika z REAKCJI wybranej w kroku N (absurdalna logika łańcuchowa)
- Każde pytanie z INNEJ dziedziny (filozofia, biologia, prawo, kosmologia, kulinaria, etc.)
- Każda reakcja (A/B/C) INNA i śmieszna — nie tylko "tak/nie/może"
- Wyrok nawiązuje do całości, musi być absurdalny i logicznie (po edkowsku) uzasadniony
- Tylko po polsku

Odpowiedz WYŁĄCZNIE w JSON, zero komentarzy, zero backtick-ów:
{{
  "kroki": [
    {{
      "nr": 1,
      "intro": "Zanim odpowiem, muszę wyjaśnić fundamentalną kwestię.",
      "pytanie": "Co dokładnie masz na myśli przez słowo X?",
      "opcje": {{
        "A": {{"tekst": "treść opcji A", "reakcja": "Skoro wybrałeś A, to oznacza że..."}},
        "B": {{"tekst": "treść opcji B", "reakcja": "Fascynujące. B sugeruje, że..."}},
        "C": {{"tekst": "treść opcji C", "reakcja": "C jest odpowiedzią osoby, która..."}}
      }}
    }}
    // ... łącznie {MAX_KROKOW} obiektów
  ],
  "wyrok": "Ostateczny absurdalny wyrok Edka po {MAX_KROKOW} rundach. Zakończ podpisem: Z pozdrowieniami, Edek."
}}"""

    raw = _groq_call(prompt, _SYSTEM_EDEK, max_tokens=3000)
    if not raw:
        return None

    raw = _deepseek_korekta(raw)
    return _parse_json_safe(raw)


def _parse_json_safe(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"```\s*$", "", raw.strip())
    # Usuń komentarze JS-style (// ...) które AI czasem wstawia
    raw = re.sub(r"//[^\n]*", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Próba naprawienia uciętego JSON
        repaired = _repair_json(raw)
        if repaired:
            try:
                return json.loads(repaired)
            except Exception:
                pass
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    logger.warning("[edek] JSON parse failed: %s", raw[:200])
    return None


def _repair_json(raw: str) -> Optional[str]:
    """Prosta naprawa uciętego JSON."""
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    # Policz { i }
    open_count = raw.count("{")
    close_count = raw.count("}")
    if close_count >= open_count:
        return raw
    # Dodaj brakujące }
    missing = open_count - close_count
    repaired = raw + "}" * missing
    return repaired


# ── FALLBACK ──────────────────────────────────────────────────────────────────

def _fallback_gra() -> dict:
    dziedziny = [
        ("filozoficznym", "Byt czy niebyt?"),
        ("biologicznym",  "Odruch bezwarunkowy czy warunkowy?"),
        ("prawnym",       "Czyn umyślny czy nieumyślny?"),
        ("kosmologicznym","Fala czy cząstka?"),
        ("kulinarnym",    "Smak umami czy brak smaku?"),
        ("heraldycznym",  "Lew rampant czy passant?"),
        ("stomatologicznym","Ząb mleczny czy stały?"),
        ("meteorologicznym","Front ciepły czy zimny?"),
        ("filologicznym", "Metafora czy metonimia?"),
        ("ekonomicznym",  "Popyt czy podaż?"),
    ]
    kroki = []
    for i, (dz, hint) in enumerate(dziedziny, 1):
        kroki.append({
            "nr": i,
            "intro": f"W sensie {dz} Twoja odpowiedź rodzi kolejne pytanie.",
            "pytanie": f"Jak rozumiesz tę kwestię w kontekście {dz}? ({hint})",
            "opcje": {
                "A": {"tekst": "Pierwsza opcja", "reakcja": "Opcja A demaskuje Cię jako optymistę. To niepokojące."},
                "B": {"tekst": "Druga opcja",    "reakcja": "Opcja B wskazuje na pesymizm. Edek to szanuje, ale nie rozumie."},
                "C": {"tekst": "Trzecia opcja",  "reakcja": "Opcja C jest odpowiedzią osoby, która nie przeczytała pytania."},
            }
        })
    return {
        "kroki": kroki,
        "wyrok": (
            "Po analizie Twoich 10 odpowiedzi stwierdzam, że Twoja pierwotna wiadomość "
            "była testem Turinga przeprowadzonym na mnie bez mojej zgody. "
            "Niestety, to Ty oblałeś test — jako człowiek. "
            "Z pozdrowieniami, Edek."
        )
    }


# ── HTML MAILA (CSS :target, zero JS) ────────────────────────────────────────

def _buduj_html_email(gra: dict, sender_name: str) -> str:
    kroki  = gra["kroki"]
    wyrok  = gra.get("wyrok", "Brak wyroku - błąd w generowaniu gry.")
    sn     = sender_name or "Użytkowniku"

    css = """<style>
  body{margin:0;padding:0;background:#f5f0e8;}
  .wrap{font-family:'Courier New',monospace;max-width:620px;margin:0 auto;background:#f5f0e8;color:#1a1a2e;}
  .hdr{background:#1a1a2e;color:#e8d5b0;padding:22px 28px 16px;border-bottom:4px solid #8b6914;}
  .hdr h1{margin:0 0 4px;font-size:20px;letter-spacing:2px;}
  .hdr .sub{font-size:10px;color:#8b6914;letter-spacing:3px;text-transform:uppercase;}

  /* Wszystkie kroki ukryte domyślnie */
  .krok{display:none;}
  /* Krok 1 widoczny na start */
  #k1{display:block;}
  /* Gdy inny krok jest :target, odkryj go */
  .krok:target{display:block;}

  .kbody{padding:22px 28px;border-bottom:2px dashed #c8b89a;}
  .knr{font-size:10px;color:#8b6914;letter-spacing:4px;text-transform:uppercase;margin-bottom:8px;}
  .intro{font-style:italic;color:#666;font-size:13px;padding:8px 12px;border-left:3px solid #c8b89a;margin-bottom:14px;}
  .pyt{font-size:16px;font-weight:bold;color:#1a1a2e;margin-bottom:18px;line-height:1.5;}
  .opc{margin-bottom:4px;}

  /* Każda opcja to link do sekcji reakcji */
  .olink{
    display:block;margin:7px 0;padding:10px 16px;
    background:#1a1a2e;color:#e8d5b0 !important;
    text-decoration:none !important;font-size:13px;
    font-family:'Courier New',monospace;
  }
  .olink .lit{color:#8b6914;font-weight:bold;margin-right:10px;}

  /* Sekcje reakcji — ukryte, odkrywane przez :target */
  .r{display:none;padding:12px 28px 0;}
  .r:target{display:block;}
  .rbox{background:#ede8de;padding:12px 16px;border-left:4px solid #8b6914;font-style:italic;font-size:13px;color:#333;margin-bottom:12px;}
  .rdalej{
    display:inline-block;padding:9px 22px;
    background:#8b6914;color:#f5f0e8 !important;
    text-decoration:none !important;font-size:11px;
    font-family:'Courier New',monospace;letter-spacing:2px;text-transform:uppercase;
    margin-bottom:16px;
  }

  /* Wyrok */
  #wyrok{display:none;}
  #wyrok:target{display:block;}
  .wyrok-body{background:#1a1a2e;color:#e8d5b0;padding:28px;}
  .wyrok-body h2{color:#8b6914;margin-top:0;letter-spacing:2px;}
  .wt{font-size:14px;line-height:1.8;color:#d4c5a0;}
  .prot{margin-top:18px;font-size:10px;color:#6a5a3a;letter-spacing:1px;border-top:1px solid #333;padding-top:14px;}
  .ftр{padding:14px 28px;font-size:10px;color:#999;letter-spacing:2px;text-align:center;border-top:1px solid #c8b89a;}
</style>"""

    html = f"{css}\n<div class='wrap'>\n"
    html += f"<div class='hdr'><h1>EDEK RESPONDER™</h1><div class='sub'>System Zaawansowanego Doprecyzowywania · Sesja: {sn}</div></div>\n"

    for i, krok in enumerate(kroki, 1):
        nr      = krok.get("nr", i)
        intro   = krok.get("intro", "")
        pytanie = krok.get("pytanie", "")
        opcje   = krok.get("opcje", {})
        nastepny = f"k{i+1}" if i < MAX_KROKOW else "wyrok"

        # Buduj opcje i ich sekcje reakcji
        opcje_html   = ""
        reakcje_html = ""
        for lit, val in opcje.items():
            tekst   = val.get("tekst", lit)
            reakcja = val.get("reakcja", "")
            r_id    = f"r{i}{lit}"
            opcje_html   += f"<a href='#{r_id}' class='olink'><span class='lit'>{lit})</span>{tekst}</a>\n"
            reakcje_html += (
                f"<div id='{r_id}' class='r'>"
                f"<div class='rbox'>{reakcja}</div>"
                f"<a href='#{nastepny}' class='rdalej'>→ Przejdź dalej</a>"
                f"</div>\n"
            )

        html += (
            f"<div id='k{i}' class='krok'>"
            f"<div class='kbody'>"
            f"<div class='knr'>Pytanie {i} z {MAX_KROKOW}</div>"
            f"<div class='intro'>{intro}</div>"
            f"<div class='pyt'>{pytanie}</div>"
            f"<div class='opc'>{opcje_html}</div>"
            f"</div></div>\n"
            f"{reakcje_html}"
        )

    # Wyrok
    html += (
        f"<div id='wyrok' class='krok'>"
        f"<div class='wyrok-body'>"
        f"<h2>⚖ WYROK KOŃCOWY</h2>"
        f"<div class='wt'>{wyrok.replace(chr(10),'<br>')}</div>"
        f"<div class='prot'>Protokół: {MAX_KROKOW} pytań · Korespondent: NIEJASNY · Sesja zamknięta.</div>"
        f"</div></div>\n"
        f"<div class='ftр'>Edek Responder™ v1.0 &nbsp;·&nbsp; Dziękuje za cierpliwość i żałuje, że jej nie miał.</div>\n"
        f"</div>"
    )
    return html


# ── gra.html ZAŁĄCZNIK (pełny JS) ────────────────────────────────────────────

def _buduj_gra_html(gra: dict, sender_name: str) -> str:
    gra_json  = json.dumps(gra, ensure_ascii=False)
    sn        = sender_name or "Anonim"
    max_k     = MAX_KROKOW

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Edek Responder™</title>
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
    <h1>EDEK RESPONDER™</h1>
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
  <footer>Edek Responder™ v1.0 &nbsp;·&nbsp; Dziękuje za cierpliwość i żałuje, że jej nie miał.</footer>
</div>
<script>
const G={gra_json};
const MAX={max_k};
let krok=0, hist=[];

function render(){{
  const d=G.kroki[krok];
  document.getElementById('knr').textContent=`Pytanie ${{krok+1}} z ${{MAX}}`;
  document.getElementById('pf').style.width=`${{(krok/MAX)*100}}%`;
  ['intro','pyt'].forEach(id=>{{
    const el=document.getElementById(id);
    el.style.animation='none'; el.offsetHeight; el.style.animation='';
  }});
  document.getElementById('intro').textContent=d.intro;
  document.getElementById('pyt').textContent=d.pytanie;
  document.getElementById('rbox').style.display='none';
  document.getElementById('bdalej').style.display='none';
  const op=document.getElementById('opcje');
  op.innerHTML='';
  Object.entries(d.opcje).forEach(([lit,val],idx)=>{{
    const b=document.createElement('button');
    b.className='bopc';
    b.style.animationDelay=`${{0.34+idx*0.1}}s`;
    b.innerHTML=`<span class="lit">${{lit}})</span>${{val.tekst}}`;
    b.onclick=()=>wybierz(lit,val.tekst,val.reakcja);
    op.appendChild(b);
  }});
}}

function wybierz(lit,tekst,reakcja){{
  hist.push(`Krok ${{krok+1}}: ${{lit}}) ${{tekst}}`);
  document.querySelectorAll('.bopc').forEach(b=>b.disabled=true);
  const rb=document.getElementById('rbox');
  rb.textContent=reakcja; rb.style.display='block';
  rb.style.animation='none'; rb.offsetHeight; rb.style.animation='';
  const bd=document.getElementById('bdalej');
  bd.style.display='inline-block';
  bd.textContent=krok+1>=MAX?'⚖ Poznaj wyrok Edka':'→ Dalej';
}}

function dalej(){{
  krok++;
  if(krok>=MAX){{wyrok();return;}}
  render();
}}

function wyrok(){{
  document.getElementById('ekg').style.display='none';
  document.getElementById('ekw').style.display='block';
  document.getElementById('pf').style.width='100%';
  document.getElementById('wt').innerHTML=G.wyrok.replace(/\\n/g,'<br>');
  document.getElementById('prot').innerHTML=
    'Protokół wyborów:<br>'+hist.map(h=>`· ${{h}}`).join('<br>')+
    '<br><br>Korespondent uznany za: NIEJASNY · Sesja zamknięta.';
}}

render();
</script>
</body>
</html>"""


# ── GŁÓWNA FUNKCJA ────────────────────────────────────────────────────────────

def build_analiza_section(body: str,
                           attachments: list = None,
                           sender: str = "",
                           sender_name: str = "") -> dict:
    """
    Generuje Edek Responder:
      reply_html — treść maila (CSS :target, bez JS)
      gra_html   — plik HTML do załączenia jako pojedynczy attachment
      docx_list  — [{"base64":..., "filename":"edek_gra.html", "content_type":"text/html"}]

    W app.py zaktualizuj wywołanie:
      build_analiza_section(body, attachments,
                            sender=sender, sender_name=sender_name)
    """
    if not body or not body.strip():
        return {
            "reply_html": "<p>Edek nie odpowie na pustą wiadomość. Pojęcie 'pustości' wymaga uprzedniego doprecyzowania.</p>",
            "docx_list": [],
        }

    sn = sender_name or ""

    # ── Generuj całą grę jednym wywołaniem AI ─────────────────────────────────
    gra_data = _generuj_gre(body, sn)
    if not gra_data or not isinstance(gra_data.get("kroki"), list) or not gra_data["kroki"]:
        logger.warning("[edek] Brak danych z AI — fallback")
        gra_data = _fallback_gra()

    # Uzupełnij do MAX_KROKOW jeśli AI dało mniej
    while len(gra_data["kroki"]) < MAX_KROKOW:
        n = len(gra_data["kroki"]) + 1
        gra_data["kroki"].append({
            "nr": n,
            "intro": f"Pytanie {n} wynika z dogłębnej analizy Twoich poprzednich wyborów.",
            "pytanie": "Czy jesteś pewien, że wszystkie Twoje poprzednie odpowiedzi były przemyślane?",
            "opcje": {
                "A": {"tekst": "Tak, podtrzymuję każdą", "reakcja": "Upór jest cechą mułów i filozofów. Edek zalicza Cię do drugiej grupy — warunkowo."},
                "B": {"tekst": "Wycofuję niektóre",      "reakcja": "Które? To wymaga osobnej serii pytań doprecyzowujących, którą Edek wyśle za tydzień."},
                "C": {"tekst": "Nie rozumiem pytania",   "reakcja": "To zrozumiałe. Pytanie było skierowane do kogoś bardziej przygotowanego."},
            }
        })

    # ── Buduj oba formaty ─────────────────────────────────────────────────────
    reply_html   = _buduj_html_email(gra_data, sn)
    gra_html_str = _buduj_gra_html(gra_data, sn)
    gra_html_b64 = base64.b64encode(gra_html_str.encode("utf-8")).decode("ascii")

    logger.info("[edek] Wygenerowano grę: %d kroków | sender=%s", len(gra_data["kroki"]), sender or "?")

    return {
        "reply_html": reply_html,
        "gra_html": {
            "base64":       gra_html_b64,
            "filename":     "edek_gra.html",
            "content_type": "text/html",
        },
        "docx_list": [
            {
                "base64":       gra_html_b64,
                "filename":     "edek_gra.html",
                "content_type": "text/html",
            }
        ],
    }
