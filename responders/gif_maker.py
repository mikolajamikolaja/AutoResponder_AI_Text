"""
responders/gif_maker.py
Konwertuje obrazek PNG komiksu (2x2 grid) na animowany GIF.

Efekt: zoom in na każdy panel (20 klatek × 4 panele = 80 klatek)
Każdy panel widoczny ~2 sekundy (20 klatek × 100ms)
Wymaga tylko: Pillow (już w projekcie)
"""

import io
import base64
from PIL import Image


# ── Ustawienia animacji ───────────────────────────────────────────────────────
FRAMES_PER_PANEL = 8      # klatek na panel = 2 sekundy przy 100ms/klatkę
FRAME_DURATION   = 250     # ms na klatkę
ZOOM_START       = 1.0     # początkowy zoom (100% panelu)
ZOOM_END         = 1.25    # końcowy zoom (125% — delikatny zoom in)
OUTPUT_SIZE      = (256, 256)  # rozmiar wyjściowego GIF


def _crop_panels(img: Image.Image) -> list:
    """
    Tnie obrazek 2x2 na 4 panele.
    Zwraca listę [panel1, panel2, panel3, panel4] (PIL Image).
    """
    w, h   = img.size
    half_w = w // 2
    half_h = h // 2

    panels = [
        img.crop((0,      0,      half_w, half_h)),   # Panel 1 — lewy górny
        img.crop((half_w, 0,      w,      half_h)),   # Panel 2 — prawy górny
        img.crop((0,      half_h, half_w, h     )),   # Panel 3 — lewy dolny
        img.crop((half_w, half_h, w,      h     )),   # Panel 4 — prawy dolny
    ]
    return panels


def _zoom_frames(panel: Image.Image, n_frames: int,
                 zoom_start: float, zoom_end: float,
                 out_size: tuple) -> list:
    """
    Generuje n_frames klatek z efektem zoom in na panel.
    Każda klatka: wytnij środkowy obszar i przeskaluj do out_size.
    """
    pw, ph = panel.size
    frames = []

    for i in range(n_frames):
        t    = i / max(n_frames - 1, 1)          # 0.0 → 1.0
        zoom = zoom_start + (zoom_end - zoom_start) * t

        # Rozmiar obszaru do wycięcia (mniejszy = większy zoom)
        crop_w = int(pw / zoom)
        crop_h = int(ph / zoom)

        # Środek panelu
        cx = pw // 2
        cy = ph // 2

        left   = max(cx - crop_w // 2, 0)
        top    = max(cy - crop_h // 2, 0)
        right  = min(left + crop_w, pw)
        bottom = min(top  + crop_h, ph)

        cropped = panel.crop((left, top, right, bottom))
        resized = cropped.resize(out_size, Image.LANCZOS)
        frames.append(resized.convert("P", palette=Image.ADAPTIVE, colors=256))

    return frames


def make_gif(png_base64: str) -> str | None:
    """
    Przyjmuje PNG jako base64, zwraca animowany GIF jako base64.
    Zwraca None przy błędzie.
    """
    try:
        png_bytes = base64.b64decode(png_base64)
        img       = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as e:
        print(f"[gif_maker] Błąd dekodowania PNG: {e}")
        return None

    try:
        panels = _crop_panels(img)
    except Exception as e:
        print(f"[gif_maker] Błąd cięcia paneli: {e}")
        return None

    all_frames = []
    for panel in panels:
        frames = _zoom_frames(
            panel,
            n_frames   = FRAMES_PER_PANEL,
            zoom_start = ZOOM_START,
            zoom_end   = ZOOM_END,
            out_size   = OUTPUT_SIZE,
        )
        all_frames.extend(frames)

    if not all_frames:
        print("[gif_maker] Brak klatek — przerywam")
        return None

    try:
        buf = io.BytesIO()
        all_frames[0].save(
            buf,
            format       = "GIF",
            save_all     = True,
            append_images= all_frames[1:],
            duration     = FRAME_DURATION,
            loop         = 0,           # 0 = zapętlaj w nieskończoność
            optimize     = True,
        )
        gif_bytes = buf.getvalue()
        print(f"[gif_maker] GIF wygenerowany: {len(gif_bytes)} B, {len(all_frames)} klatek")
        return base64.b64encode(gif_bytes).decode("ascii")
    except Exception as e:
        print(f"[gif_maker] Błąd zapisu GIF: {e}")
        return None
