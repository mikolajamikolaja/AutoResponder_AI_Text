#!/usr/bin/env python3
# app.py - webhook backend dla Google Apps Script
import os
import base64
import requests
import json
import re
import io
from flask import Flask, request, jsonify

app = Flask(__name__)


def sanitize_model_output(raw_text: str) -> str:
    """
    Jeśli model zwrócił JSON lub JSON + tekst, wyciągnij właściwy tekst.
    Zwraca czysty tekst odpowiedzi.
    """
    if not raw_text:
        return ""
    txt = raw_text.strip()
    # Jeśli cały tekst jest JSONem, spróbuj sparsować i wyciągnąć typowe pola
    if txt.startswith("{") or txt.startswith("["):
        try:
            obj = json.loads(txt)
            if isinstance(obj, dict):
                for key in ("odpowiedz_tekstowa", "reply", "answer", "text", "message", "reply_html", "content"):
                    if key in obj:
                        val = obj[key]
                        return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
                # jeśli dict z jedną wartością, zwróć ją
                if len(obj) == 1:
                    val = next(iter(obj.values()))
                    return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            if isinstance(obj, list):
                return "\n".join(str(x) for x in obj)
        except Exception:
            pass
    # Jeśli JSON jest na początku, a potem jest tekst, usuń wrapper JSON
    if txt.startswith("{") and "}" in txt:
        try:
            end = txt.index("}") + 1
            maybe_json = txt[:end]
            remainder = txt[end:].strip()
            try:
                json.loads(maybe_json)
                if remainder:
                    return remainder
            except Exception:
                # heurystyka: usuń leading JSON i zwróć resztę
                return txt[end:].strip()
        except Exception:
            pass
    return raw_text


def extract_clean_text(text: str) -> str:
    """
    Z odpowiedzi zawierającej JSON + inne treści wyciąga pole 'odpowiedz_tekstowa',
    jeśli jest dostępne. W przeciwnym razie zwraca przycięty tekst.
    """
    if not text:
        return ""
    txt = text.strip()
    match = re.search(r'\{.*\}', txt, re.DOTALL)
    if not match:
        return txt
    try:
        obj = json.loads(match.group(0))
        if isinstance(obj, dict) and "odpowiedz_tekstowa" in obj:
            val = obj["odpowiedz_tekstowa"]
            return val.strip() if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        return txt
    except Exception:
        return txt


# Konfiguracja
GROQ_API_KEY = os.getenv("API_KEY_DEEPSEEK")
MODEL_BIZ = os.getenv("MODEL_BIZ", "deepseek-chat")
MODEL_TYLER = os.getenv("MODEL_TYLER", "deepseek-chat")

EMOTKI_DIR = os.path.join(os.path.dirname(__file__), "emotki")
PDF_DIR = os.path.join(os.path.dirname(__file__), "pdf_biznes")

# ── Stałe wizualne Scrabble (identyczne jak w grze) ───────────────────────────
SCR_COLOR_BG    = (10,  45,  10)
SCR_COLOR_BOARD = (34, 139,  34)
SCR_COLOR_GRID  = (0,  100,   0)
SCR_COLOR_TILE  = (245, 222, 179)
SCR_COLOR_TEXT  = (40,   40,  40)

SCR_LETTERS_PTS = {
    'A': 1, 'Ą': 5, 'B': 3, 'C': 2, 'Ć': 6,
    'D': 2, 'E': 1, 'Ę': 5, 'F': 5, 'G': 3,
    'H': 3, 'I': 1, 'J': 3, 'K': 2, 'L': 2,
    'Ł': 3, 'M': 2, 'N': 1, 'Ń': 7, 'O': 1,
    'Ó': 5, 'P': 2, 'R': 1, 'S': 1, 'Ś': 5,
    'T': 2, 'U': 3, 'W': 1, 'Y': 2, 'Z': 1,
    'Ź': 9, 'Ż': 5,
}

SCR_BOARD_DIM = 15


def _load_premium_map_for_image():
    """Wczytaj mapę premii z plansza.csv."""
    premium_map = {}
    path = os.path.join(os.path.dirname(__file__), "plansza.csv")
    if not os.path.exists(path):
        return premium_map
    try:
        import csv
        with open(path, encoding="utf-8", newline='') as f:
            for r, row in enumerate(csv.reader(f)):
                if r >= SCR_BOARD_DIM:
                    break
                for c, val in enumerate(row[:SCR_BOARD_DIM]):
                    val = val.strip().upper()
                    if not val:
                        continue
                    try:
                        if val.endswith(('S', 'W')):
                            premium_map[(r, c)] = ("S", int(val[:-1]), (200, 0, 0))
                        elif val.endswith('L'):
                            premium_map[(r, c)] = ("L", int(val[:-1]), (0, 0, 180))
                    except Exception:
                        pass
    except Exception as e:
        app.logger.warning("premium_map load error: %s", e)
    return premium_map


def _scrabble_tile_value(ch):
    return SCR_LETTERS_PTS.get(ch.upper(), ord(ch) if ch else 0)


def render_scrabble_image(text: str) -> bytes:
    """
    Renderuje tekst na planszy Scrabble — każdy znak to kafelek.
    Zwraca PNG jako bytes.
    Wymaga: Pillow (pip install Pillow)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        app.logger.error("Pillow nie jest zainstalowane — brak render_scrabble_image")
        return b""

    tile_sz   = 36          # rozmiar kafla w px
    gap       = 2           # odstęp między kaflami
    cell      = tile_sz + gap
    margin    = 14          # margines planszy
    board_dim = SCR_BOARD_DIM

    # Zawijaj tekst do wierszy planszy (max board_dim znaków/wiersz)
    chars = list(text)
    rows_chars = []
    while chars:
        rows_chars.append(chars[:board_dim])
        chars = chars[board_dim:]
    # Ogranicz do board_dim wierszy
    rows_chars = rows_chars[:board_dim]
    # Uzupełnij do board_dim wierszy (puste)
    while len(rows_chars) < board_dim:
        rows_chars.append([])

    premium_map = _load_premium_map_for_image()

    img_w = 2 * margin + board_dim * cell
    img_h = 2 * margin + board_dim * cell
    img = Image.new("RGB", (img_w, img_h), SCR_COLOR_BG)
    draw = ImageDraw.Draw(img)

    # Czcionka kafelka
    font_path_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]

    def _try_font(size):
        for fp in font_path_candidates:
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    font_letter = _try_font(int(tile_sz * 0.52))
    font_pts    = _try_font(int(tile_sz * 0.24))
    font_prem   = _try_font(int(tile_sz * 0.26))

    for r in range(board_dim):
        for c in range(board_dim):
            x = margin + c * cell
            y = margin + r * cell
            prem = premium_map.get((r, c))

            # Tło pola
            bg_col = prem[2] if prem else SCR_COLOR_BOARD
            draw.rectangle([x, y, x + tile_sz - 1, y + tile_sz - 1], fill=bg_col)
            draw.rectangle([x, y, x + tile_sz - 1, y + tile_sz - 1],
                           outline=SCR_COLOR_GRID, width=1)

            # Pobierz znak dla tej komórki
            row_chars = rows_chars[r]
            ch = row_chars[c] if c < len(row_chars) else None

            if ch is not None and ch != ' ':
                # Kafelek z literą
                tile_rect = [x + 1, y + 1, x + tile_sz - 2, y + tile_sz - 2]
                draw.rectangle(tile_rect, fill=SCR_COLOR_TILE)
                draw.rectangle(tile_rect, outline=(0, 0, 0), width=1)

                # Litera na środku
                try:
                    bbox = font_letter.getbbox(ch)
                    lw = bbox[2] - bbox[0]
                    lh = bbox[3] - bbox[1]
                except Exception:
                    lw, lh = tile_sz // 2, tile_sz // 2

                lx = x + (tile_sz - lw) // 2 - (bbox[0] if hasattr(font_letter, 'getbbox') else 0)
                ly = y + tile_sz // 10
                draw.text((lx, ly), ch, font=font_letter, fill=SCR_COLOR_TEXT)

                # Wartość w prawym dolnym rogu
                val = _scrabble_tile_value(ch)
                val_str = str(val)
                try:
                    vbbox = font_pts.getbbox(val_str)
                    vw = vbbox[2] - vbbox[0]
                    vh = vbbox[3] - vbbox[1]
                except Exception:
                    vw, vh = 8, 8
                draw.text((x + tile_sz - vw - 3, y + tile_sz - vh - 3),
                          val_str, font=font_pts, fill=SCR_COLOR_TEXT)

            elif prem and ch is None:
                # Etykieta premii na pustym polu
                label = f"{prem[1]}{prem[0]}"
                try:
                    pbbox = font_prem.getbbox(label)
                    pw = pbbox[2] - pbbox[0]
                    ph = pbbox[3] - pbbox[1]
                except Exception:
                    pw, ph = tile_sz // 2, tile_sz // 2
                draw.text((x + (tile_sz - pw) // 2, y + (tile_sz - ph) // 2),
                          label, font=font_prem, fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

EMOTIONS = [
    "twarz_lek",
    "twarz_nuda",
    "twarz_radosc",
    "twarz_smutek",
    "twarz_spokoj",
    "twarz_zaskoczenie",
    "twarz_zlosc"
]
FALLBACK_EMOT = "error"


# Pomocniczne
def read_file_base64(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
            if not data:
                app.logger.warning("Plik istnieje, ale jest pusty: %s", path)
                return None
            return base64.b64encode(data).decode("ascii")
    except Exception as e:
        app.logger.warning("read_file_base64 failed for %s: %s", path, e)
        return None


def safe_emoticon_and_pdf_for(emotion_key):
    """Zwraca tuple (png_b64, pdf_b64); jeśli brak, używa error fallback."""
    png_name = f"{emotion_key}.png"
    pdf_name = f"{emotion_key}.pdf"

    png_path = os.path.join(EMOTKI_DIR, png_name)
    pdf_path = os.path.join(PDF_DIR, pdf_name)

    png_b64 = read_file_base64(png_path)
    pdf_b64 = read_file_base64(pdf_path)

    if not png_b64:
        png_b64 = read_file_base64(os.path.join(EMOTKI_DIR, f"{FALLBACK_EMOT}.png"))
    if not pdf_b64:
        pdf_b64 = read_file_base64(os.path.join(PDF_DIR, f"{FALLBACK_EMOT}.pdf"))

    return png_b64, pdf_b64


# Wywołanie Groq (tekstowe)
def call_groq(system_prompt: str, user_msg: str, model_name: str, timeout=20):
    if not GROQ_API_KEY:
        app.logger.error("Brak  API_KEY_DEEPSEEK")
        return None

    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg}
        ],
        "temperature": 0.0,
        "max_tokens": 3000  # ograniczamy odpowiedź
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code != 200:
            app.logger.warning("GROQ non-200 (%s): %s", resp.status_code, resp.text[:500])
            return None
        try:
            data = resp.json()
        except Exception:
            # jeśli odpowiedź nie jest JSONem, zwróć surowy tekst
            return sanitize_model_output(resp.text)
        # standardowy format OpenAI-like
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            # fallback: spróbuj inne pola
            content = None
            if isinstance(data, dict):
                for key in ("content", "text", "message", "reply"):
                    if key in data and isinstance(data[key], str):
                        content = data[key]
                        break
            if not content:
                content = json.dumps(data, ensure_ascii=False)
        return sanitize_model_output(content)
    except Exception as e:
        app.logger.exception("Błąd wywołania Groq: %s", e)
        return None


# Prosty helper: poproś model o jednowyrazowe rozpoznanie emocji spośród listy
def detect_emotion_via_model(body_text: str):
    prompt = (
        "Na podstawie poniższego tekstu wybierz dokładnie jedną z następujących etykiet emocji "
        f"(bez dodatkowego tekstu): {', '.join(EMOTIONS)}; jeśli żadna nie pasuje, odpowiedz: {FALLBACK_EMOT}.\n\n"
        f"Tekst:\n{body_text}\n\nOdpowiedź:"
    )
    res = call_groq("Detektor emocji (zwróć tylko jedną etykietę)", prompt, MODEL_TYLER)
    if not res:
        return FALLBACK_EMOT
    token = res.strip().lower()
    for e in EMOTIONS:
        if e in token:
            return e
    return FALLBACK_EMOT


# Prosty helper: wykryj temat notarialny i wybierz pasujący pdf
def detect_notarial_topic_and_choose_pdf(body_text: str):
    prompt = (
        "Przeczytaj tekst klienta i rozpoznaj, który z poniższych tematów notarialnych jest najbardziej odpowiedni. "
        "Jeśli nie możesz jednoznacznie przypisać, odpowiedz: UNKNOWN.\n\n"
        "Tematy (przykładowe pliki PDF):\n"
        "- darowizna_mieszkania_lub_domu_obowiazki_podatkowe_i_formalne\n"
        "- dzial_spadku_umowny_krok_po_kroku_z_notariuszem\n"
        "- intercyza_umowa_majatkowa_malzenska_wyjasnienie_i_koszty\n"
        "- kontakt_godziny_pracy_notariusza_podstawowe_informacje\n"
        "- sprzedaz_nieruchomosci_mieszkanie_procedura_koszty_wymagane_dokumenty\n\n"
        f"Tekst:\n{body_text}\n\nOdpowiedź (jedna etykieta lub UNKNOWN):"
    )
    res = call_groq("Detektor tematu notarialnego (jedna etykieta lub UNKNOWN)", prompt, MODEL_BIZ)
    if not res:
        return "UNKNOWN"
    token = res.strip().lower()
    if "darowiz" in token:
        return "darowizna_mieszkania_lub_domu_obowiazki_podatkowe_i_formalne"
    if "spad" in token:
        return "dzial_spadku_umowny_krok_po_kroku_z_notariuszem"
    if "intercyz" in token or "intercyza" in token:
        return "intercyza_umowa_majatkowa_malzenska_wyjasnienie_i_koszty"
    if "kontakt" in token or "godzin" in token:
        return "kontakt_godziny_pracy_notariusza_podstawowe_informacje"
    if "sprzed" in token or "nieruchom" in token:
        return "sprzedaz_nieruchomosci_mieszkanie_procedura_koszty_wymagane_dokumenty"
    return "UNKNOWN"


# Formatowanie HTML zgodnie z wymaganiem (kursywa + zielona stopka)
def build_html_reply(body_text: str):
    body_text = body_text.replace("\n", "<br>")
    html = f"<p><i>{body_text}</i></p>\n"
    html += (
        "<p style=\"color:#0a8a0a; font-size:10px;\">"
        "Odpowiedź wygenerowana automatycznie przez system Script + Render.<br>"
        "Projekt dostępny na GitHub: https://github.com/legionowopawel/AutoResponder_AI_Text.git"
        "</p>"
    )
    return html


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    sender = data.get("from", "")
    subject = data.get("subject", "")
    body = data.get("body", "")

    if not body or not body.strip():
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # --- EMOCJONALNA CZESC (Tyler / prompt.txt) ---
    emotion = detect_emotion_via_model(body)

    prompt_txt_path = os.path.join(os.path.dirname(__file__), "prompt.txt")
    if os.path.exists(prompt_txt_path):
        with open(prompt_txt_path, "r", encoding="utf-8") as f:
            prompt_template = f.read()
    else:
        prompt_template = "Odpowiedz krótko i empatycznie na poniższy tekst: {{USER_TEXT}}"

    prompt_for_model = prompt_template.replace("{{USER_TEXT}}", body[:3000])

    res_tyler_raw = call_groq(prompt_for_model, body, MODEL_TYLER)
    res_tyler_clean = sanitize_model_output(res_tyler_raw) if res_tyler_raw else ""
    res_tyler = extract_clean_text(res_tyler_clean)
    if not res_tyler:
        res_tyler = "Przepraszam, wystąpił problem z generowaniem odpowiedzi."

    png_b64, pdf_b64 = safe_emoticon_and_pdf_for(emotion)

    emotional_section = {
        "reply_html": build_html_reply(res_tyler),
        "emoticon": {
            "base64": png_b64,
            "content_type": "image/png",
            "filename": f"{emotion}.png"
        },
        "pdf": {
            "base64": pdf_b64,
            "filename": f"{emotion}.pdf"
        },
        "detected_emotion": emotion
    }

    # --- BIZNESOWA CZESC (Notariusz / prompt_biznesowy.txt) ---
    prompt_biz_path = os.path.join(os.path.dirname(__file__), "prompt_biznesowy.txt")
    if os.path.exists(prompt_biz_path):
        with open(prompt_biz_path, "r", encoding="utf-8") as f:
            prompt_biz_template = f.read()
    else:
        prompt_biz_template = "Jesteś uprzejmym Notariuszem. Przygotuj profesjonalną odpowiedź: {{USER_TEXT}}"

    prompt_biz_for_model = prompt_biz_template.replace("{{USER_TEXT}}", body[:3000])

    res_biz_raw = call_groq(prompt_biz_for_model, body, MODEL_BIZ)
    res_biz_clean = sanitize_model_output(res_biz_raw) if res_biz_raw else ""
    res_biz = extract_clean_text(res_biz_clean)
    if not res_biz:
        res_biz = "Przepraszam, wystąpił problem z generowaniem odpowiedzi biznesowej."

    # wykryj temat i wybierz pdf (z logowaniem i fallbackem)
    topic_pdf_key = detect_notarial_topic_and_choose_pdf(body)
    pdf_path = os.path.join(PDF_DIR, f"{topic_pdf_key}.pdf")
    pdf_b64_biz = read_file_base64(pdf_path)
    app.logger.info("BUSINESS PDF try: %s ; base64 present? %s", pdf_path, bool(pdf_b64_biz))

    chosen_filename = f"{topic_pdf_key}.pdf"
    if not pdf_b64_biz:
        fallback_key = "kontakt_godziny_pracy_notariusza_podstawowe_informacje"
        fallback_path = os.path.join(PDF_DIR, f"{fallback_key}.pdf")
        app.logger.warning("Brak PDF dla %s, próbuję fallback: %s", topic_pdf_key, fallback_path)
        pdf_b64_biz = read_file_base64(fallback_path)
        app.logger.info("Fallback PDF present? %s", bool(pdf_b64_biz))
        chosen_filename = f"{fallback_key}.pdf" if pdf_b64_biz else chosen_filename

    biz_section = {
        "reply_html": build_html_reply(
            res_biz + ("\n\nRozpoznane zagadnienia: (zobacz załącznik)" if topic_pdf_key == "UNKNOWN" else "")
        ),
        "pdf": {
            "base64": pdf_b64_biz,
            "filename": chosen_filename
        },
        "topic": topic_pdf_key if pdf_b64_biz else "UNKNOWN"
    }
    if not pdf_b64_biz:
        biz_section["notes"] = "Brak pliku PDF na serwerze; proszę o kontakt."

    response_data = {
        "biznes": biz_section,
        "zwykly": emotional_section
    }

    # --- SCRABBLE CZESC (KEYWORDS2 / prompt_scrabble.txt) ---
    # Generowana tylko gdy backend wykryje potrzebę (flaga w żądaniu)
    if data.get("wants_scrabble"):
        prompt_scrabble_path = os.path.join(os.path.dirname(__file__), "prompt_scrabble.txt")
        if os.path.exists(prompt_scrabble_path):
            with open(prompt_scrabble_path, "r", encoding="utf-8") as f:
                prompt_scrabble_template = f.read()
        else:
            prompt_scrabble_template = "Odpowiedz krótko i ciekawie na poniższy tekst: {{USER_TEXT}}"

        prompt_scrabble_for_model = prompt_scrabble_template.replace("{{USER_TEXT}}", body[:3000])
        res_scrabble_raw = call_groq(prompt_scrabble_for_model, body, MODEL_TYLER)
        res_scrabble_clean = sanitize_model_output(res_scrabble_raw) if res_scrabble_raw else ""
        res_scrabble = extract_clean_text(res_scrabble_clean)
        if not res_scrabble:
            res_scrabble = "Brak odpowiedzi scrabble."

        # Renderuj tekst jako obrazek PNG na planszy Scrabble
        scrabble_png_bytes = render_scrabble_image(res_scrabble.upper())
        scrabble_png_b64 = base64.b64encode(scrabble_png_bytes).decode("ascii") if scrabble_png_bytes else None

        scrabble_section = {
            "reply_html": build_html_reply(res_scrabble),
            "image": {
                "base64": scrabble_png_b64,
                "content_type": "image/png",
                "filename": "scrabble_odpowiedz.png"
            }
        }
        response_data["scrabble"] = scrabble_section

    app.logger.info(
        "Response data prepared: biznes.pdf present? %s, zwykly.pdf present? %s",
        bool(response_data["biznes"].get("pdf", {}).get("base64")),
        bool(response_data["zwykly"].get("pdf", {}).get("base64"))
    )

    return jsonify(response_data), 200


if __name__ == "__main__":
    if not GROQ_API_KEY:
        app.logger.warning(" API_KEY_DEEPSEEK nie ustawiony ( API_KEY_DEEPSEEK). Backend będzie działał, ale wywołania AI zwrócą None.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
