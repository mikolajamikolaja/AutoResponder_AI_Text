"""
responders/scrabble.py
Responder Scrabble — generuje odpowiedź tekstową i renderuje ją
jako obrazek PNG na planszy Scrabble (Pillow).
"""

import os
import io
import base64
import csv
from flask import current_app

from core.ai_client import (
    call_deepseek,
    extract_clean_text,
    sanitize_model_output,
    MODEL_TYLER,
)
from core.files import load_prompt
from core.html_builder import build_html_reply

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

# ── Stałe wizualne (identyczne jak w grze Scrabble) ──────────────────────────
COLOR_BG = (10, 45, 10)
COLOR_BOARD = (34, 139, 34)
COLOR_GRID = (0, 100, 0)
COLOR_TILE = (245, 222, 179)
COLOR_TEXT = (40, 40, 40)

BOARD_DIM = 15

LETTERS_PTS = {
    "A": 1,
    "Ą": 5,
    "B": 3,
    "C": 2,
    "Ć": 6,
    "D": 2,
    "E": 1,
    "Ę": 5,
    "F": 5,
    "G": 3,
    "H": 3,
    "I": 1,
    "J": 3,
    "K": 2,
    "L": 2,
    "Ł": 3,
    "M": 2,
    "N": 1,
    "Ń": 7,
    "O": 1,
    "Ó": 5,
    "P": 2,
    "R": 1,
    "S": 1,
    "Ś": 5,
    "T": 2,
    "U": 3,
    "W": 1,
    "Y": 2,
    "Z": 1,
    "Ź": 9,
    "Ż": 5,
}

# Kandydaci na czcionkę (Linux Render + Windows lokalnie)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _load_premium_map() -> dict:
    """Wczytaj mapę premii z plansza.csv."""
    premium_map = {}
    path = os.path.join(DATA_DIR, "plansza.csv")
    if not os.path.exists(path):
        return premium_map
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for r, row in enumerate(csv.reader(f)):
                if r >= BOARD_DIM:
                    break
                for c, val in enumerate(row[:BOARD_DIM]):
                    val = val.strip().upper()
                    if not val:
                        continue
                    try:
                        if val.endswith(("S", "W")):
                            premium_map[(r, c)] = ("S", int(val[:-1]), (200, 0, 0))
                        elif val.endswith("L"):
                            premium_map[(r, c)] = ("L", int(val[:-1]), (0, 0, 180))
                    except Exception:
                        pass
    except Exception as e:
        current_app.logger.warning("_load_premium_map error: %s", e)
    return premium_map


def _tile_value(ch: str) -> int:
    return LETTERS_PTS.get(ch.upper(), ord(ch) if ch else 0)


def _try_font(size: int):
    """Zwraca czcionkę TrueType lub domyślną Pillow."""
    from PIL import ImageFont

    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def render_scrabble_image(text: str) -> bytes:
    """
    Renderuje tekst jako PNG na planszy Scrabble.
    Każdy znak = jeden kafelek. Zwraca PNG jako bytes.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        current_app.logger.error("Pillow nie jest zainstalowane!")
        return b""

    tile_sz = 36
    gap = 2
    cell = tile_sz + gap
    margin = 14

    # Zawijanie tekstu do wierszy planszy
    chars = list(text)
    rows_chars = []
    while chars:
        rows_chars.append(chars[:BOARD_DIM])
        chars = chars[BOARD_DIM:]
    rows_chars = rows_chars[:BOARD_DIM]
    while len(rows_chars) < BOARD_DIM:
        rows_chars.append([])

    premium_map = _load_premium_map()

    img_w = 2 * margin + BOARD_DIM * cell
    img_h = 2 * margin + BOARD_DIM * cell
    img = Image.new("RGB", (img_w, img_h), COLOR_BG)
    draw = ImageDraw.Draw(img)

    font_letter = _try_font(int(tile_sz * 0.52))
    font_pts = _try_font(int(tile_sz * 0.24))
    font_prem = _try_font(int(tile_sz * 0.26))

    for r in range(BOARD_DIM):
        for c in range(BOARD_DIM):
            x = margin + c * cell
            y = margin + r * cell
            prem = premium_map.get((r, c))

            # Tło pola
            bg_col = prem[2] if prem else COLOR_BOARD
            draw.rectangle([x, y, x + tile_sz - 1, y + tile_sz - 1], fill=bg_col)
            draw.rectangle(
                [x, y, x + tile_sz - 1, y + tile_sz - 1], outline=COLOR_GRID, width=1
            )

            row_chars = rows_chars[r]
            ch = row_chars[c] if c < len(row_chars) else None

            if ch is not None and ch != " ":
                # Kafelek z literą
                draw.rectangle(
                    [x + 1, y + 1, x + tile_sz - 2, y + tile_sz - 2], fill=COLOR_TILE
                )
                draw.rectangle(
                    [x + 1, y + 1, x + tile_sz - 2, y + tile_sz - 2],
                    outline=(0, 0, 0),
                    width=1,
                )
                # Litera
                try:
                    bbox = font_letter.getbbox(ch)
                    lw = bbox[2] - bbox[0]
                    lx = x + (tile_sz - lw) // 2 - bbox[0]
                    ly = y + tile_sz // 10
                except Exception:
                    lx, ly = x + tile_sz // 4, y + tile_sz // 10
                draw.text((lx, ly), ch, font=font_letter, fill=COLOR_TEXT)

                # Wartość w prawym dolnym rogu
                val_str = str(_tile_value(ch))
                try:
                    vbbox = font_pts.getbbox(val_str)
                    vw = vbbox[2] - vbbox[0]
                    vh = vbbox[3] - vbbox[1]
                except Exception:
                    vw, vh = 8, 8
                draw.text(
                    (x + tile_sz - vw - 3, y + tile_sz - vh - 3),
                    val_str,
                    font=font_pts,
                    fill=COLOR_TEXT,
                )

            elif prem and ch is None:
                # Etykieta premii na pustym polu
                label = f"{prem[1]}{prem[0]}"
                try:
                    pbbox = font_prem.getbbox(label)
                    pw = pbbox[2] - pbbox[0]
                    ph = pbbox[3] - pbbox[1]
                except Exception:
                    pw, ph = tile_sz // 2, tile_sz // 2
                draw.text(
                    (x + (tile_sz - pw) // 2, y + (tile_sz - ph) // 2),
                    label,
                    font=font_prem,
                    fill=(255, 255, 255),
                )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_scrabble_section(body: str) -> dict:
    """
    Buduje sekcję 'scrabble' odpowiedzi:
    - generuje tekst przez model (prompt_scrabble.txt)
    - renderuje go jako obrazek PNG na planszy Scrabble
    """
    prompt_template = load_prompt(
        "prompt_scrabble.txt",
        fallback="Odpowiedz krótko i ciekawie na poniższy tekst: {{USER_TEXT}}",
    )
    prompt_for_model = prompt_template.replace("{{USER_TEXT}}", body[:3000])

    res_raw = call_deepseek(prompt_for_model, "", MODEL_TYLER)
    res_clean = sanitize_model_output(res_raw) if res_raw else ""
    res_text = extract_clean_text(res_clean)
    if not res_text:
        res_text = "Brak odpowiedzi."

    # Twardy limit 225 znaków = 15×15 planszy
    res_text_cut = res_text[:225]

    png_bytes = render_scrabble_image(res_text_cut.upper())
    png_b64 = base64.b64encode(png_bytes).decode("ascii") if png_bytes else None
    image_dict = {
        "base64": png_b64,
        "content_type": "image/png",
        "filename": "scrabble_odpowiedz.png",
    }

    return {
        "reply_html": build_html_reply(res_text),
        "image": image_dict,
        "images": [image_dict],
    }
