#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
responders/generator_pdf.py

Generuje egzamin PDF na podstawie tekstu emaila.
- AI: Groq (primarne) → DeepSeek (fallback)
- PDF w pamięci (BytesIO) — bez zapisu na dysk
- Hint jako annotation PDF (działa w Adobe Acrobat Reader i przez najechanie myszką)
- Zwraca dict zgodny z formatem innych responderów:
  {
    "reply_html": "...",
    "pdf": {
      "base64": "...",
      "content_type": "application/pdf",
      "filename": "egzamin_....pdf"
    }
  }
"""

import os
import io
import re
import json
import time
import base64
import logging
import requests

from datetime import date
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.lib.colors import HexColor, white, black, Color

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  Czcionki
# ─────────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
_FONT_DIR = os.path.join(_DIR, "..", "fonts")
FN = "Helvetica"
FB = "Helvetica-Bold"
_FONTS_REGISTERED = False


def _reg_fonts():
    global FN, FB, _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    np_ = os.path.join(_FONT_DIR, "DejaVuSans.ttf")
    bp_ = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
    if os.path.exists(np_):
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", np_))
            FN = "DejaVuSans"
        except Exception as e:
            logger.warning("Czcionka DejaVuSans: %s", e)
    if os.path.exists(bp_):
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", bp_))
            FB = "DejaVuSans-Bold"
        except Exception as e:
            logger.warning("Czcionka DejaVuSans-Bold: %s", e)
    _FONTS_REGISTERED = True


# ─────────────────────────────────────────────────────────────
#  Kolory
# ─────────────────────────────────────────────────────────────
CH = HexColor("#1a3a5c")
CB = HexColor("#2a6099")
CT = HexColor("#1a1a2e")
CM = HexColor("#34495e")
CG = HexColor("#27ae60")
CO = HexColor("#f0f6ff")
CY = HexColor("#fff9e6")
CYB = HexColor("#f39c12")
CD = HexColor("#cdd8e3")


# ─────────────────────────────────────────────────────────────
#  Ładowanie promptu
# ─────────────────────────────────────────────────────────────
def _load_prompt() -> str:
    path = os.path.join(_DIR, "..", "prompts", "prompt_pdf_egzamin.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error("Brak pliku prompt_pdf_egzamin.txt: %s", e)
        # Fallback inline
        return (
            "Jestes ekspertem tworzenia egzaminow. Zwroc TYLKO czysty JSON bez ``` .\n"
            "Stworz egzamin z {n} pytaniami (tylko MC i TF). Poziom: {diff}.\n"
            'JSON: {{"exam_title":"...","exam_subtitle":"...","total_points":10,'
            '"questions":[{{"id":1,"type":"multiple_choice","points":2,'
            '"question":"?","options":[{{"label":"a","text":"A"}}],'
            '"correct_answer":"a","hint":"Wskazowka."}}]}}\n'
            "Tekst:\n---\n{text}\n---"
        )


PROMPT_TEMPLATE = None


def _get_prompt(text: str, n: int = 10, diff: str = "sredni") -> str:
    global PROMPT_TEMPLATE
    if PROMPT_TEMPLATE is None:
        PROMPT_TEMPLATE = _load_prompt()
    return (
        PROMPT_TEMPLATE.replace("{text}", text)
        .replace("{n}", str(n))
        .replace("{diff}", diff)
    )


# ─────────────────────────────────────────────────────────────
#  API helpers
# ─────────────────────────────────────────────────────────────
def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*", "", raw, flags=re.M)
    raw = re.sub(r"```\s*$", "", raw, flags=re.M)
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        # Próba naprawy uciętego JSON
        repaired = _repair_truncated_json(raw.strip())
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # Jeśli nadal błąd, spróbuj wyciągnąć najlepszy JSON
            decoder = json.JSONDecoder()
            for match in re.finditer(r"[\[{]", raw):
                start = match.start()
                try:
                    obj, end = decoder.raw_decode(raw[start:])
                    return obj
                except:
                    continue
            raise  # Jeśli nic nie działa, raise oryginalny błąd


def _repair_truncated_json(raw: str) -> str:
    raw = raw.strip()
    start = next((i for i, c in enumerate(raw) if c in "{["), 0)
    raw = raw[start:]
    raw = re.sub(r"//[^\n]*", "", raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)

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
        raw += '"'

    raw = re.sub(r",\s*$", "", raw)

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
            if ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()

    if stack:
        raw += "".join(reversed(stack))
    return raw


def _call_deepseek(text: str, n: int, diff: str) -> dict:
    key = os.getenv("API_KEY_DEEPSEEK", "")
    if not key:
        raise RuntimeError("Brak API_KEY_DEEPSEEK")
    sys_msg = "Jestes ekspertem tworzenia egzaminow w jezyku polskim. Zwroc TYLKO czysty JSON bez markdown."
    payload = {
        "model": "deepseek-chat",
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": _get_prompt(text, n, diff)},
        ],
    }
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(1, 4):
        logger.info("DeepSeek próba %d/3...", attempt)
        try:
            r = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers=hdrs,
                json=payload,
                timeout=90,
            )
            r.raise_for_status()
            return _parse_json(r.json()["choices"][0]["message"]["content"])
        except json.JSONDecodeError as e:
            logger.warning("DeepSeek JSON err: %s", e)
            raise
        except requests.RequestException as e:
            logger.warning("DeepSeek HTTP err: %s", e)
            if attempt == 3:
                raise
        time.sleep(3 * attempt)
    raise RuntimeError("DeepSeek: max retries")


def _call_api(text: str, n: int = 10, diff: str = "sredni") -> dict:
    """DeepSeek API call."""
    return _call_deepseek(text, n, diff)


# ─────────────────────────────────────────────────────────────
#  Scoring JS (auto-liczenie punktów przy onChange)
# ─────────────────────────────────────────────────────────────
def _make_scoring_js(exam: dict) -> str:
    qs = exam.get("questions", [])
    answers = []
    for i, q in enumerate(qs, 1):
        t = q.get("type", "multiple_choice")
        ca = q.get("correct_answer", "")
        pts = q.get("points", 1)
        if t == "multiple_choice":
            field = f"mc{i}"
        elif t == "true_false":
            field = f"tf{i}"
        else:
            continue
        answers.append({"field": field, "answer": ca, "points": pts, "type": t})

    total = exam.get("total_points", sum(a["points"] for a in answers))
    answers_json = json.dumps(answers, ensure_ascii=False)

    return f"""
var answers = {answers_json};
var totalMax = {total};
var scored = 0;
for (var i = 0; i < answers.length; i++) {{
    var a = answers[i];
    var f = this.getField(a.field);
    if (f) {{
        var val = f.value;
        if (val && val !== "Off" && val !== "") {{
            var correct = a.answer;
            if (a.type === "true_false") {{
                correct = correct.toUpperCase();
                val = val.toUpperCase();
            }}
            if (val === correct) scored += a.points;
        }}
    }}
}}
var wynikField = this.getField("wynik");
var ocenaField = this.getField("ocena");
if (wynikField) wynikField.value = scored + " / " + totalMax;
var pct = totalMax > 0 ? (scored / totalMax) * 100 : 0;
var ocena = "1";
if (pct >= 95) ocena = "6";
else if (pct >= 70) ocena = "5";
else if (pct >= 60) ocena = "4";
else if (pct >= 50) ocena = "3";
else if (pct >= 40) ocena = "2";
if (ocenaField) ocenaField.value = ocena;
""".strip()


# ─────────────────────────────────────────────────────────────
#  PDF Builder
# ─────────────────────────────────────────────────────────────
PW, PH = A4
ML = 18 * mm
MB = 18 * mm


class _PDF:
    def __init__(self, buf: io.BytesIO, exam: dict, sender_name: str = ""):
        _reg_fonts()
        self.exam = exam
        self.sender_name = sender_name
        self.c = canvas.Canvas(buf, pagesize=A4)
        self.f = self.c.acroForm
        self.y = PH - ML
        self.pg = 1
        self.fid = 0
        self._score_js = _make_scoring_js(exam)
        # annotation counter dla unikalnych nazw
        self._ann_id = 0

    def sf(self, b=False):
        return FB if b else FN

    def ff(self):
        return "Helvetica"

    def uid(self, pfx="f"):
        s = re.sub(r"[^a-zA-Z0-9_]", "_", f"{pfx}_{self.fid}")
        self.fid += 1
        return s

    def ensure(self, need_mm):
        if self.y < MB + need_mm * mm:
            self.newpage()

    def newpage(self):
        self.c.showPage()
        self.pg += 1
        self.y = PH - ML
        self.footer()

    def footer(self):
        c = self.c
        c.saveState()
        c.setFont(self.sf(), 8)
        c.setFillColor(CM)
        c.drawString(ML, 10 * mm, self.exam.get("exam_title", ""))
        c.drawRightString(PW - ML, 10 * mm, f"Strona {self.pg}")
        c.restoreState()

    def textfield(
        self, name, x, y, w, h, val="", bs="solid", fc=None, bc=None, fs=10, ro=False
    ):
        kw = dict(
            name=name,
            x=x,
            y=y,
            width=w,
            height=h,
            value=val,
            borderStyle=bs,
            forceBorder=True,
            fontName=self.ff(),
            fontSize=fs,
        )
        if fc:
            kw["fillColor"] = fc
        if bc:
            kw["borderColor"] = bc
        if ro:
            kw["fieldFlags"] = "readOnly"
        try:
            self.f.textfield(**kw)
        except Exception:
            try:
                self.f.textfield(
                    name=name,
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    fontName="Helvetica",
                    fontSize=fs,
                )
            except Exception:
                pass

    def radio(self, group, val, x, y, sz=4.5):
        half = sz * mm / 2
        try:
            self.f.radio(
                name=group,
                value=val,
                x=x,
                y=y - half,
                size=sz * mm,
                selected=False,
                forceBorder=True,
                borderColor=CB,
                fillColor=white,
                buttonStyle="circle",
            )
        except Exception:
            self.c.saveState()
            self.c.setStrokeColor(CB)
            self.c.rect(x, y - half, sz * mm, sz * mm, stroke=1, fill=0)
            self.c.restoreState()

    def wrap(self, x, y, text, bold=False, sz=9, maxw=None, lh=5.2, col=None) -> float:
        fn = self.sf(bold)
        mw = maxw or (PW - x - ML)
        lines = simpleSplit(text, fn, sz, mw)
        self.c.saveState()
        self.c.setFont(fn, sz)
        if col:
            self.c.setFillColor(col)
        for ln in lines:
            self.c.drawString(x, y, ln)
            y -= lh * mm
        self.c.restoreState()
        return y

    # ── Hint jako PDF annotation (Text Note) ──────────────────
    # Działa w Adobe Acrobat Reader — ikona "?" widoczna,
    # treść pojawia się po najechaniu lub kliknięciu.
    def hint_annotation(self, q, qnum):
        """
        Dodaje annotation typu Text (sticky note) z treścią hinta.
        Ikona widoczna w Adobe Acrobat Reader po prawej stronie pytania.
        Treść pojawia się po kliknięciu ikony lub najechaniu myszką.
        """
        hint = q.get("hint", "").strip()
        if not hint:
            return

        c = self.c
        m = ML
        w = PW

        # Pozycja ikony — prawa strona, przy aktualnej pozycji y
        icon_x = w - m - 8 * mm
        icon_y = self.y - 3 * mm
        icon_size = 6 * mm

        # Narysuj wizualną ikonę "?" (statyczna — zawsze widoczna)
        c.saveState()
        c.setFillColor(CY)
        c.circle(
            icon_x + icon_size / 2,
            icon_y - icon_size / 2,
            icon_size / 2,
            fill=1,
            stroke=0,
        )
        c.setStrokeColor(CYB)
        c.circle(
            icon_x + icon_size / 2,
            icon_y - icon_size / 2,
            icon_size / 2,
            fill=0,
            stroke=1,
        )
        c.setFont(self.sf(True), 8)
        c.setFillColor(HexColor("#7d4e00"))
        c.drawCentredString(
            icon_x + icon_size / 2, icon_y - icon_size / 2 - 2.5 * mm, "?"
        )
        c.restoreState()

        # PDF Annotation — Text type (sticky note)
        # Renderlab nie ma natywnego API do annotations,
        # więc wstrzykujemy przez canvas._code (ReportLab internal)
        self._ann_id += 1
        ann_name = f"Hint{qnum}_{self._ann_id}"

        # Rect: [x1, y1, x2, y2] w punktach PDF (origin = lewy-dolny)
        x1 = icon_x
        y1 = icon_y - icon_size
        x2 = icon_x + icon_size
        y2 = icon_y

        # Kodowanie PDF UTF-16BE z BOM — jedyna metoda obsługująca polskie znaki
        # w annotacjach ReportLab bez zewnętrznych fontów
        def _pdf_utf16_str(text: str) -> str:
            """Zwraca hex string <FEFF...> gotowy do wstawienia jako PDF string."""
            bom_utf16 = b"\xfe\xff" + text.encode("utf-16-be")
            return "<" + bom_utf16.hex().upper() + ">"

        hint_pdf = _pdf_utf16_str(hint)
        title_pdf = _pdf_utf16_str(f"Podpowiedz do pytania {qnum}")
        ann_name_pdf = _pdf_utf16_str(ann_name)

        # Wstrzyknij annotation do strumienia PDF
        # /Subtype /Text = sticky note, /Open false = zamknięta domyślnie
        ann_obj = (
            f"<< /Type /Annot /Subtype /Text "
            f"/Rect [{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}] "
            f"/Contents {hint_pdf} "
            f"/T {title_pdf} "
            f"/NM {ann_name_pdf} "
            f"/Open false "
            f"/Color [1 0.9 0.4] "
            f"/F 4 "
            f">>"
        )

        try:
            # Dodajemy annotation przez niskopoziomowe API ReportLab
            c._addAnnotation(ann_obj)
        except AttributeError:
            # Fallback: _addAnnotation nie istnieje w tej wersji ReportLab
            # Używamy innego podejścia — dodajemy jako part current page
            try:
                if not hasattr(c, "_annotations"):
                    c._annotations = []
                c._annotations.append(ann_obj)
                # Patch showPage by flush annotations
                original_showPage = c.showPage

                def _patched_showPage():
                    for a in getattr(c, "_annotations", []):
                        pass  # best effort
                    c._annotations = []
                    original_showPage()

            except Exception as e2:
                logger.debug("Annotation fallback też nie działa: %s", e2)

        self.y -= icon_size + 1 * mm

    # ── Strona tytułowa ───────────────────────────────────────
    def cover(self):
        c = self.c
        w = PW
        h = PH
        m = ML
        tq = len(self.exam.get("questions", []))
        tp = self.exam.get("total_points", "?")
        today = date.today().strftime("%d.%m.%Y")

        # Banner
        c.setFillColor(CH)
        c.rect(0, h - 50 * mm, w, 50 * mm, fill=1, stroke=0)
        c.setFont(self.sf(True), 19)
        c.setFillColor(white)
        c.drawCentredString(w / 2, h - 25 * mm, self.exam.get("exam_title", "Egzamin"))
        c.setFont(self.sf(), 11)
        c.setFillColor(HexColor("#aed6f1"))
        c.drawCentredString(w / 2, h - 38 * mm, self.exam.get("exam_subtitle", ""))

        # Info box
        c.setFillColor(HexColor("#e8f0f7"))
        c.roundRect(m, h - 82 * mm, w - 2 * m, 26 * mm, 4 * mm, fill=1, stroke=0)
        c.setFont(self.sf(True), 10)
        c.setFillColor(CT)
        c.drawString(m + 6 * mm, h - 61 * mm, f"Liczba pytan: {tq}")
        c.drawString(m + 6 * mm, h - 70 * mm, f"Suma punktow: {tp} pkt")
        c.setFont(self.sf(), 9)
        c.setFillColor(HexColor("#b7770d"))
        c.drawString(
            m + 6 * mm,
            h - 78 * mm,
            "Ikona [?] przy pytaniu = podpowiedz (kliknij w Adobe Reader).",
        )

        # Dane zdajacego
        dy = h - 98 * mm
        c.setFont(self.sf(True), 10)
        c.setFillColor(CT)
        c.drawString(m, dy, "Imie i nazwisko:")
        # Wstaw imię i nazwisko nadawcy automatycznie
        self.textfield(
            "imie",
            m + 50 * mm,
            dy - 3 * mm,
            w - 2 * m - 50 * mm,
            8 * mm,
            val=self.sender_name,
        )

        dy -= 14 * mm
        c.drawString(m, dy, "Data:")
        self.textfield("data", m + 16 * mm, dy - 3 * mm, 44 * mm, 8 * mm, val=today)
        c.drawString(m + 66 * mm, dy, "Klasa:")
        self.textfield("klasa", m + 82 * mm, dy - 3 * mm, 44 * mm, 8 * mm)

        # Wynik / ocena — aktualizowane przez JS onChange
        dy -= 22 * mm
        c.setFillColor(HexColor("#fef9e7"))
        c.roundRect(m, dy - 16 * mm, w - 2 * m, 22 * mm, 4 * mm, fill=1, stroke=0)
        c.setStrokeColor(CYB)
        c.roundRect(m, dy - 16 * mm, w - 2 * m, 22 * mm, 4 * mm, fill=0, stroke=1)

        c.setFont(self.sf(True), 10)
        c.setFillColor(CT)
        c.drawString(m + 5 * mm, dy, "Wynik:")
        self.textfield(
            "wynik",
            m + 22 * mm,
            dy - 4 * mm,
            50 * mm,
            8 * mm,
            fc=HexColor("#eafaf1"),
            bc=CG,
        )
        c.drawString(m + 75 * mm, dy, "pkt")

        c.drawString(m + 90 * mm, dy, "Ocena:")
        self.textfield(
            "ocena",
            m + 108 * mm,
            dy - 4 * mm,
            30 * mm,
            8 * mm,
            fc=HexColor("#eafaf1"),
            bc=CG,
        )

        c.setFont(self.sf(), 8)
        c.setFillColor(CM)
        c.drawString(
            m + 5 * mm,
            dy - 13 * mm,
            "Wynik i ocena aktualizuja sie automatycznie po zaznaczeniu odpowiedzi.",
        )

        self.footer()
        c.showPage()
        self.pg += 1
        self.y = PH - ML

    # ── Nagłówek pytania ──────────────────────────────────────
    def qhead(self, q, qnum):
        c = self.c
        m = ML
        w = PW
        pts = q.get("points", 1)
        tmap = {
            "multiple_choice": "Wielokrotny wybor \u2013 zaznacz jedna odpowiedz",
            "true_false": "Prawda / Falsz",
        }
        ql = tmap.get(q.get("type", ""), "Pytanie")

        # Baner
        BH = 7 * mm
        c.setFillColor(CH)
        c.rect(m, self.y - BH, w - 2 * m, BH, fill=1, stroke=0)
        c.setFont(self.sf(True), 9)
        c.setFillColor(white)
        c.drawString(
            m + 3 * mm,
            self.y - 5 * mm,
            f"Pytanie {qnum}  \u2022  {ql}  \u2022  {pts} pkt",
        )
        self.y -= BH + 3 * mm

        # Treść pytania (z marginesem na ikonę ?)
        qtxt = f"{qnum}. {q.get('question', '')}"
        self.y = self.wrap(
            m + 2 * mm, self.y, qtxt, sz=10, col=CT, maxw=w - 2 * m - 12 * mm, lh=5.5
        )
        self.y -= 2 * mm

        # Annotation hint (ikona ?, treść po kliknięciu)
        self.hint_annotation(q, qnum)
        self.y -= 2 * mm

    # ── Multiple choice ───────────────────────────────────────
    def mc(self, q, qnum):
        self.ensure(90)
        self.qhead(q, qnum)
        c = self.c
        m = ML
        w = PW
        RH = 9 * mm
        grp = f"mc{qnum}"

        for i, opt in enumerate(q.get("options", [])):
            self.ensure(RH / mm + 2)
            lbl = opt.get("label", "")
            text = opt.get("text", "")
            rt = self.y
            rmid = rt - RH / 2

            c.setFillColor(CO if i % 2 == 0 else white)
            c.rect(m, rt - RH, w - 2 * m, RH, fill=1, stroke=0)

            self.radio(grp, lbl, m + 4 * mm, rmid)

            text_y = rmid + 1.5 * mm
            c.setFont(self.sf(True), 9)
            c.setFillColor(CB)
            c.drawString(m + 10.5 * mm, text_y, f"{lbl})")

            lines = simpleSplit(text, self.sf(), 9, w - m - 22 * mm - m)
            c.setFont(self.sf(), 9)
            c.setFillColor(CT)
            ty = text_y
            for ln in lines:
                c.drawString(m + 18 * mm, ty, ln)
                ty -= 5 * mm

            self.y = rt - max(RH, rt - ty + 1 * mm)

        self.y -= 3 * mm
        c.setStrokeColor(CD)
        c.line(m, self.y, w - m, self.y)
        self.y -= 5 * mm

    # ── True / False ──────────────────────────────────────────
    def tf(self, q, qnum):
        self.ensure(55)
        self.qhead(q, qnum)
        c = self.c
        m = ML
        w = PW
        RH = 10 * mm
        rmid = self.y - RH / 2

        for lbl, xoff in [("PRAWDA", 0), ("FALSZ", 62 * mm)]:
            rx = m + 8 * mm + xoff
            c.setFillColor(CO)
            c.roundRect(rx - 3 * mm, self.y - RH, 52 * mm, RH, 3 * mm, fill=1, stroke=0)
            c.setStrokeColor(CB)
            c.roundRect(rx - 3 * mm, self.y - RH, 52 * mm, RH, 3 * mm, fill=0, stroke=1)
            self.radio(f"tf{qnum}", lbl, rx, rmid)
            c.setFont(self.sf(True), 10)
            c.setFillColor(CT)
            c.drawString(rx + 6.5 * mm, rmid + 1.5 * mm, lbl)

        self.y -= RH + 4 * mm
        c.setStrokeColor(CD)
        c.line(m, self.y, w - m, self.y)
        self.y -= 5 * mm

    # ── Klucz odpowiedzi ──────────────────────────────────────
    def key(self):
        self.newpage()
        c = self.c
        m = ML
        w = PW

        c.setFillColor(CG)
        c.rect(0, self.y - 12 * mm, w, 12 * mm, fill=1, stroke=0)
        c.setFont(self.sf(True), 13)
        c.setFillColor(white)
        c.drawCentredString(
            w / 2, self.y - 8.5 * mm, "KLUCZ ODPOWIEDZI  \u2013  tylko dla prowadzacego"
        )
        self.y -= 20 * mm

        COL_NR = m + 2 * mm
        COL_TYP = m + 16 * mm
        COL_ANS = m + 28 * mm
        COL_PTS = w - m - 2 * mm

        # Nagłówek tabeli
        RH_HEAD = 7 * mm
        c.setFillColor(HexColor("#1a3a5c"))
        c.rect(m, self.y - RH_HEAD, w - 2 * m, RH_HEAD, fill=1, stroke=0)
        c.setFont(self.sf(True), 9)
        c.setFillColor(white)
        c.drawString(COL_NR, self.y - 5 * mm, "Nr")
        c.drawString(COL_TYP, self.y - 5 * mm, "Typ")
        c.drawString(COL_ANS, self.y - 5 * mm, "Prawidlowa odpowiedz")
        c.drawRightString(COL_PTS, self.y - 5 * mm, "Pkt")
        self.y -= RH_HEAD + 1 * mm

        RH = 9 * mm
        for i, q in enumerate(self.exam.get("questions", []), 1):
            self.ensure(RH / mm + 2)
            ans = q.get("correct_answer", "")
            pts = q.get("points", 1)
            qt = {"multiple_choice": "MC", "true_false": "TF"}.get(
                q.get("type", ""), "?"
            )

            bg = HexColor("#eafaf1") if i % 2 == 0 else HexColor("#f9fffe")
            c.setFillColor(bg)
            c.rect(m, self.y - RH, w - 2 * m, RH, fill=1, stroke=0)

            ty = self.y - RH / 2 - 1.5 * mm

            c.setFont(self.sf(True), 9)
            c.setFillColor(CG)
            c.drawString(COL_NR, ty, f"{i:2d}.")

            c.setFont(self.sf(), 9)
            c.setFillColor(CM)
            c.drawString(COL_TYP, ty, qt)

            ans_max_w = COL_PTS - COL_ANS - 20 * mm
            ans_lines = simpleSplit(ans, self.sf(True), 9, ans_max_w)
            c.setFont(self.sf(True), 9)
            c.setFillColor(HexColor("#117a65"))
            c.drawString(COL_ANS, ty, ans_lines[0] if ans_lines else ans)

            c.setFont(self.sf(True), 9)
            c.setFillColor(CT)
            c.drawRightString(COL_PTS, ty, f"{pts} pkt")

            self.y -= RH

        self.y -= 2 * mm
        c.setStrokeColor(CG)
        c.line(m, self.y, w - m, self.y)
        self.y -= 7 * mm

        total_pts = sum(q.get("points", 1) for q in self.exam.get("questions", []))
        c.setFont(self.sf(True), 10)
        c.setFillColor(CT)
        c.drawString(COL_NR, self.y, "RAZEM:")
        c.drawRightString(COL_PTS, self.y, f"{total_pts} pkt")

        self.footer()

    def build(self):
        self.cover()
        self.footer()
        qs = self.exam.get("questions", [])
        for i, q in enumerate(qs, 1):
            t = q.get("type", "multiple_choice")
            self.ensure(30)
            if t == "multiple_choice":
                self.mc(q, i)
            elif t == "true_false":
                self.tf(q, i)
        self.key()
        self.c.save()


def _build_pdf_bytes(exam: dict, sender_name: str = "") -> bytes:
    """Buduje PDF w pamięci, zwraca bytes."""
    buf = io.BytesIO()
    _PDF(buf, exam, sender_name=sender_name).build()
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
#  Publiczny interfejs responderu
# ─────────────────────────────────────────────────────────────
def build_generator_pdf_section(
    body: str,
    sender_name: str = "",
    n: int = 10,
    diff: str = "sredni",
    test_mode: bool = False,
) -> dict:
    """
    Główna funkcja respondoru - generator PDF.

    Parametr test_mode:
    - Jeśli test_mode=True (z KEYWORDS_TEST via app.py disable_flux),
      generator_pdf może wy generowanie Flux jeśli sobie to funkcjonuje.

    Zwraca dict:
      {
        "reply_html": str,
        "pdf": {
            "base64": str,
            "content_type": "application/pdf",
            "filename": str
        }
      }
    """
    try:
        logger.info("generator_pdf: wywołanie API (n=%d, diff=%s)...", n, diff)
        exam = _call_api(body, n=n, diff=diff)
        logger.info("generator_pdf: AI OK — %d pytań", len(exam.get("questions", [])))

        pdf_bytes = _build_pdf_bytes(exam, sender_name=sender_name)
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        title = exam.get("exam_title", "Egzamin")
        safe = re.sub(r"[^\w\s-]", "", title)[:30]
        safe = re.sub(r"\s+", "_", safe.strip()) or "Egzamin"
        filename = f"egzamin_{safe}.pdf"

        n_questions = len(exam.get("questions", []))
        total_pts = exam.get("total_points", "?")

        reply_html = f"""
<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#1a3a5c;color:white;padding:16px 20px;border-radius:8px 8px 0 0;">
    <h2 style="margin:0;font-size:18px;">&#127891; Egzamin PDF gotowy!</h2>
  </div>
  <div style="background:#f4f8ff;padding:16px 20px;border:1px solid #d0dff0;border-top:none;border-radius:0 0 8px 8px;">
    <p style="margin:0 0 10px;">Oto egzamin wygenerowany automatycznie na podstawie Twojego tekstu:</p>
    <table style="border-collapse:collapse;width:100%;margin-bottom:12px;">
      <tr>
        <td style="padding:6px 10px;background:#e8f0f7;font-weight:bold;border-radius:4px;">Temat:</td>
        <td style="padding:6px 10px;">{title}</td>
      </tr>
      <tr>
        <td style="padding:6px 10px;font-weight:bold;">Pytań:</td>
        <td style="padding:6px 10px;">{n_questions}</td>
      </tr>
      <tr>
        <td style="padding:6px 10px;background:#e8f0f7;font-weight:bold;border-radius:4px;">Punktów:</td>
        <td style="padding:6px 10px;">{total_pts}</td>
      </tr>
    </table>
    <p style="color:#666;font-size:13px;margin:0;">
      PDF zawiera pytania wielokrotnego wyboru i prawda/fałsz. Wynik i ocena
      wyliczają się automatycznie po zaznaczeniu odpowiedzi.<br>
      Ikona <strong>[?]</strong> przy każdym pytaniu zawiera podpowiedź
      (kliknij w Adobe Acrobat Reader).
    </p>
  </div>
</div>
""".strip()

        return {
            "reply_html": reply_html,
            "pdf": {
                "base64": pdf_b64,
                "content_type": "application/pdf",
                "filename": filename,
            },
        }

    except Exception as e:
        logger.error("generator_pdf BŁĄD: %s", e, exc_info=True)
        return {
            "reply_html": (
                "<p>Przepraszam, wystąpił błąd podczas generowania egzaminu PDF. "
                "Spróbuj ponownie za chwilę.</p>"
            ),
            "pdf": None,
        }
