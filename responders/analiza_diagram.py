"""
responders/analiza_diagram.py

Generuje diagram drzewa decyzyjnego Edka Respondenta:
  - JPG (1024×1024) — widok z oddali, pokazuje całą strukturę
  - SVG/HTML interaktywny — główny interfejs do interakcji

Zależności:
  - graphviz (dot)
  - PIL (image processing)
  - networkx (graph structure)
"""

import os
import json
import subprocess
import base64
from io import BytesIO
from html import escape
from typing import Optional, Dict, Any

logger_enabled = True


def _log(msg: str):
    if logger_enabled:
        from flask import current_app

        try:
            current_app.logger.info(f"[eryk-diagram] {msg}")
        except:
            print(f"[eryk-diagram] {msg}")


def _build_graph_dot(gra: Dict[str, Any]) -> str:
    """
    Buduje DOT file dla Graphviz z drzewa decyzyjnego.
    Obsługuje starą strukturę ("kroki") i nową drzewiastą ("pytania").
    """
    # Nowa struktura drzewiasta
    pytania = gra.get("pytania", [])
    kroki = gra.get("kroki", [])

    if not pytania and not kroki:
        return ""

    dot_lines = [
        "digraph ErykTree {",
        "  rankdir=TB;",
        "  node [shape=box, style=filled, fontname=Arial];",
        "  edge [fontname=Arial, fontsize=9];",
    ]

    # Kolory dla opcji
    colors = {
        "A": "#E6F1FB",  # Niebieski
        "B": "#E1F5EE",  # Zielony
        "C": "#FAEEDA",  # Brązowy
        "Q": "#EEEDFE",  # Fiolet (pytanie główne)
        "R": "#F0E6FA",  # Jasny fiolet (runda)
    }

    # Liczniki unikalnych ID
    node_counter = 0

    def next_id():
        nonlocal node_counter
        node_counter += 1
        return f"n{node_counter}"

    if pytania:
        # Nowa struktura drzewiasta
        for p_idx, pytanie in enumerate(pytania):
            p_id = pytanie.get("id", f"P{p_idx+1}")
            tresc = pytanie.get("tresc", f"Pytanie {p_idx+1}")[:40]
            opcje = pytanie.get("opcje", {})

            # Node głównego pytania
            main_node_id = f"p{p_idx+1}"
            dot_lines.append(
                f'  {main_node_id} [label="{p_id}:\\n{tresc}", fillcolor="{colors["Q"]}", '
                f'color="#534AB7", penwidth=1.5];'
            )

            # Rekurencyjna funkcja dodająca drzewo opcji
            def add_option_tree(parent_id, options, depth=1, path=""):
                if depth > 3:  # MAX_RUNDY = 3
                    return
                if not options:
                    return

                for lit, opt in options.items():
                    tekst = opt.get("tekst", lit)[:25]
                    opt_node_id = f"{parent_id}_{lit}_{depth}"
                    dot_lines.append(
                        f'  {opt_node_id} [label="{lit}: {tekst}", fillcolor="{colors.get(lit, "#EEEEE")}", '
                        f'color="#666", penwidth=0.8, fontsize=9];'
                    )
                    dot_lines.append(
                        f'  {parent_id} -> {opt_node_id} [label="{lit}", color="#888"];'
                    )

                    # Reakcja (nie rysujemy)
                    # Sprawdź czy jest kolejna runda
                    next_round_key = f"runda{depth+1}"
                    if next_round_key in opt:
                        next_round = opt[next_round_key]
                        if "pytanie" in next_round:
                            # Node rundy
                            round_node_id = f"{opt_node_id}_r{depth+1}"
                            round_text = next_round.get("pytanie", f"Runda {depth+1}")[
                                :35
                            ]
                            dot_lines.append(
                                f'  {round_node_id} [label="R{depth+1}:\\n{round_text}", fillcolor="{colors["R"]}", '
                                f'color="#7A5FB7", penwidth=1.2];'
                            )
                            dot_lines.append(
                                f'  {opt_node_id} -> {round_node_id} [style=dashed, color="#666"];'
                            )
                            # Rekurencja dla opcji w tej rundzie
                            if "opcje" in next_round:
                                add_option_tree(
                                    round_node_id,
                                    next_round["opcje"],
                                    depth + 1,
                                    path + lit,
                                )

            # Rozpocznij drzewo od głównego pytania
            add_option_tree(main_node_id, opcje, depth=1)

            # Połączenie do następnego pytania (jeśli istnieje)
            if p_idx < len(pytania) - 1:
                next_main_id = f"p{p_idx+2}"
                dot_lines.append(f"  {main_node_id} -> {next_main_id} [style=invis];")

        # Wyrok końcowy
        dot_lines.append(
            '  wyrok [label="⚖ WYROK\\nKOŃCOWY", fillcolor="#3C3489", fontcolor="#F1EFE8", '
            "shape=ellipse, penwidth=2];"
        )
        if pytania:
            last_main_id = f"p{len(pytania)}"
            dot_lines.append(f"  {last_main_id} -> wyrok [style=dashed];")

    else:
        # Stara struktura sekwencyjna (dla kompatybilności)
        for i, krok in enumerate(kroki, 1):
            nr = krok.get("nr", i)
            pytanie = krok.get("pytanie", f"P{i}")[:40]

            node_id = f"q{i}"
            dot_lines.append(
                f'  {node_id} [label="P{i}:\\n{pytanie}", fillcolor="{colors["Q"]}", '
                f'color="#534AB7", penwidth=1.5];'
            )

            if i > 1:
                prev_id = f"q{i-1}"
                dot_lines.append(f"  {prev_id} -> {node_id} [style=dashed];")

            opcje = krok.get("opcje", {})
            for lit in ["A", "B", "C"]:
                if lit not in opcje:
                    continue

                val = opcje[lit]
                tekst = val.get("tekst", lit)[:25]
                leaf_id = f"opt_{i}_{lit}"

                dot_lines.append(
                    f'  {leaf_id} [label="{lit}: {tekst}", fillcolor="{colors.get(lit, "#EEEEE")}", '
                    f'color="#666", penwidth=0.8, fontsize=9];'
                )
                dot_lines.append(
                    f'  {node_id} -> {leaf_id} [label="{lit}", color="#888"];'
                )

        # Wyrok końcowy
        dot_lines.append(
            '  wyrok [label="⚖ WYROK\\nKOŃCOWY", fillcolor="#3C3489", fontcolor="#F1EFE8", '
            "shape=ellipse, penwidth=2];"
        )
        if kroki:
            dot_lines.append(f"  q{len(kroki)} -> wyrok [style=dashed];")

    dot_lines.append("}")

    return "\n".join(dot_lines)


def _generate_jpg_via_graphviz(
    gra: Dict[str, Any], width: int = 1024, height: int = 1024
) -> Optional[bytes]:
    """
    Generuje JPG z DOT file za pomocą Graphviz.
    Zwraca bytes JPG lub None.
    """
    try:
        import subprocess

        dot_content = _build_graph_dot(gra)
        if not dot_content:
            return None

        # Wywołaj `dot` (z Graphviz)
        result = subprocess.run(
            ["dot", "-Tjpg", "-Gdpi=300"],
            input=dot_content.encode(),
            capture_output=True,
            timeout=10,
        )

        if result.returncode == 0:
            _log(f"JPG wygenerowany via Graphviz: {len(result.stdout)} bytes")
            return result.stdout
        else:
            _log(f"Graphviz error: {result.stderr.decode()[:100]}")
            return None
    except FileNotFoundError:
        _log("⚠ Graphviz nie zainstalowany — spróbuję fallback")
        return None
    except Exception as e:
        _log(f"Error podczas generacji JPG: {e}")
        return None


def _generate_jpg_fallback(
    gra: Dict[str, Any], width: int = 1024, height: int = 1024
) -> Optional[bytes]:
    """
    Fallback: generuje prosty JPG za pomocą PIL.
    Rysuje tekst + kwadraty reprezentujące pytania.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageColor
    except ImportError:
        _log("⚠ PIL nie zainstalowany — zwracam None")
        return None

    try:
        # Obsługuj zarówno starą strukturę (kroki) jak i nową (pytania)
        kroki = gra.get("kroki", []) or gra.get("pytania", [])
        if not kroki:
            return None

        # Utwórz białe tło
        img = Image.new("RGB", (width, height), color=(245, 240, 232))
        draw = ImageDraw.Draw(img)

        # Spróbuj załadować font
        try:
            font_small = ImageFont.truetype("arial.ttf", 9)
            font_med = ImageFont.truetype("arial.ttf", 11)
            font_big = ImageFont.truetype("arial.ttf", 14)
        except:
            try:
                font_small = ImageFont.truetype("DejaVuSans.ttf", 9)
                font_med = ImageFont.truetype("DejaVuSans.ttf", 11)
                font_big = ImageFont.truetype("DejaVuSans.ttf", 14)
            except:
                font_small = font_med = font_big = ImageFont.load_default()

        # Nagłówek
        draw.text((15, 12), "ERYK RESPONDER™", fill=(44, 44, 42), font=font_big)
        draw.text(
            (15, 32),
            f"Diagram drzewa decyzyjnego ({len(kroki)} pytań × 3 opcje = {len(kroki)*3} ścieżek)",
            fill=(100, 100, 100),
            font=font_med,
        )
        draw.line([(10, 50), (width - 10, 50)], fill=(139, 105, 20), width=2)

        # Rysuj każde pytanie jako kwadrat z liniami
        y_start = 70
        y_step = 100

        colors_q = {
            "q": (238, 237, 254),  # Fiolet (pytanie)
            "a": (230, 241, 251),  # Niebieski
            "b": (225, 245, 238),  # Zielony
            "c": (250, 238, 218),  # Brązowy
            "txt": (60, 52, 137),  # Tekst fiolet
        }

        for i, krok in enumerate(kroki, 1):
            row = (i - 1) // 2
            col = (i - 1) % 2

            x = 20 + col * (width // 2 - 20)
            y = y_start + row * y_step

            # Kwadrat dla pytania
            box_width = width // 2 - 40
            bbox = (x, y, x + box_width, y + 80)
            draw.rectangle(bbox, fill=colors_q["q"], outline=(83, 74, 183), width=1)

            # Tekst pytania (skrócony) - obsługuj zarówno 'pytanie' jak i 'tresc'
            pytanie = (krok.get("pytanie") or krok.get("tresc", f"P{i}"))[:35]
            draw.text(
                (x + 5, y + 5),
                f"P{i}: {pytanie[:30]}",
                fill=colors_q["txt"],
                font=font_small,
            )

            # Opcje A, B, C (minilettery)
            draw.text((x + 5, y + 28), "A", fill=(24, 95, 165), font=font_med)
            draw.text((x + 35, y + 28), "B", fill=(15, 110, 86), font=font_med)
            draw.text((x + 65, y + 28), "C", fill=(133, 79, 11), font=font_med)

            # Liczba ścieżek (3 do następnego pytania lub końcu)
            next_level = len(kroki) - i
            if next_level > 0:
                opcje = krok.get("opcje", {})
                num_opcje = len(opcje)
                draw.text(
                    (x + 5, y + 50),
                    f"→ {num_opcje} ścieżki",
                    fill=(150, 100, 100),
                    font=font_small,
                )
            else:
                draw.text(
                    (x + 5, y + 50), "→ WYROK", fill=(139, 105, 20), font=font_small
                )

        # Dolna informacja
        draw.text(
            (15, height - 30),
            "Mapa całej logiki gry. Aby grać aktywnie, otwórz HTML.",
            fill=(100, 100, 100),
            font=font_small,
        )

        # Zapisz do bytes
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        jpg_bytes = buf.getvalue()

        _log(f"JPG fallback wygenerowany via PIL: {len(jpg_bytes)} bytes")
        return jpg_bytes

    except Exception as e:
        _log(f"Error PIL fallback: {e}")
        return None


def _wrap_svg_text(text: str, max_chars: int) -> list[str]:
    """Dzieli tekst na wiersze o maksymalnej długości znaków."""
    if not text:
        return [""]
    words = str(text).split()
    lines = []
    current = ""
    for word in words:
        if current and len(current) + len(word) + 1 > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _svg_text_block(
    lines: list[str],
    x: int,
    y: int,
    font_size: int = 12,
    anchor: str = "middle",
    fill: str = "#3C3489",
    font_weight: str = "bold",
) -> str:
    if not lines:
        return ""
    svg = [
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="Arial" font-size="{font_size}" fill="{fill}" font-weight="{font_weight}">'
    ]
    for idx, line in enumerate(lines):
        dy = 0 if idx == 0 else font_size + 4
        svg.append(f'<tspan x="{x}" dy="{dy}">{escape(line)}</tspan>')
    svg.append("</text>")
    return "".join(svg)


def generate_jpg_diagram(gra: Dict[str, Any]) -> Optional[bytes]:
    """
    Główna funkcja — generuje JPG diagramu.
    Próbuje Graphviz, jeśli zawiedzie, używa PIL fallback.
    """
    # Najpierw spróbuj Graphviz
    jpg = _generate_jpg_via_graphviz(gra)
    if jpg:
        return jpg

    # Fallback: PIL
    jpg = _generate_jpg_fallback(gra)
    return jpg


def generate_svg_html_interactive(gra: Dict[str, Any], sender_name: str = "") -> str:
    """
    Generuje samodzielny plik HTML z SVG drzewa decyzyjnego.
    Obsługuje nową strukturę drzewiastą (pytania → opcje A/B/C → runda2)
    oraz starą sekwencyjną (kroki) dla kompatybilności wstecznej.

    Nowa struktura: każde pytanie rozgałęzia się na A/B/C,
    każda gałąź ma reakcję Eryka i opcjonalnie runda2 z kolejnymi A/B/C.
    """
    pytania = gra.get("pytania", [])
    kroki_legacy = gra.get("kroki", [])
    wyrok = gra.get("wyrok", "Brak wyroku.")
    sn = escape(sender_name or "Anonim")

    # ── Stara struktura — zachowaj kompatybilność ─────────────────────────────
    if not pytania and kroki_legacy:
        _log("Używam starej struktury (kroki) dla SVG diagramu")
        return _generate_svg_legacy(kroki_legacy, wyrok, sn)

    if not pytania:
        _log("Brak danych do diagramu SVG")
        return "<p>Brak danych do diagramu.</p>"

    _log(f"Generuję SVG dla nowej struktury drzewiastej: {len(pytania)} pytań")

    # ── Stałe layoutu ─────────────────────────────────────────────────────────
    # Szerokość SVG: 3 kolumny na opcje + marginesy
    W = 2000
    # Wysokości wierszy
    ROW_ROOT   = 70   # korzeń pytania
    ROW_GAP    = 40   # odstęp między poziomami
    ROW_OPC    = 80   # kafelek opcji 1. rundy
    ROW_REAKC  = 44   # kafelek reakcji
    ROW_R2_PYT = 36   # pytanie rundy 2
    ROW_OPC2   = 60   # kafelek opcji 2. rundy
    ROW_REAKC2 = 36   # reakcja rundy 2
    BLOCK_H = ROW_GAP + ROW_OPC + ROW_REAKC + ROW_R2_PYT + ROW_OPC2 + ROW_REAKC2 + 30

    # X środków 3 kolumn opcji
    COL_X = [340, W // 2, W - 340]
    OPC_W  = 520   # szerokość kafelka opcji rundy 1
    OPC2_W = 340   # szerokość kafelka opcji rundy 2
    ROOT_W = 900

    COLORS = {
        "A": {"fill": "#E6F1FB", "stroke": "#185FA5", "text": "#0C447C", "line": "#185FA5"},
        "B": {"fill": "#E1F5EE", "stroke": "#0F6E56", "text": "#085041", "line": "#0F6E56"},
        "C": {"fill": "#FAEEDA", "stroke": "#854F0B", "text": "#633806", "line": "#854F0B"},
    }
    ROOT_FILL   = "#EEEDFE"
    ROOT_STROKE = "#534AB7"
    ROOT_TEXT   = "#3C3489"
    REAKC_FILL  = "#FFF8F0"
    REAKC_STROKE= "#C8A96A"
    WYROK_FILL  = "#2C2C2A"

    # Całkowita wysokość SVG
    total_height = 80 + len(pytania) * (ROW_ROOT + BLOCK_H + 60) + 120
    svg = []

    # ── Nagłówek SVG ──────────────────────────────────────────────────────────
    svg.append(f'<?xml version="1.0" encoding="UTF-8"?>')
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{total_height}" '
               f'viewBox="0 0 {W} {total_height}" font-family="Arial, sans-serif">')
    svg.append(f'<rect width="{W}" height="{total_height}" fill="#FAF8F4"/>')

    # Tytuł
    svg.append(f'<rect x="{W//2 - 500}" y="14" width="1000" height="44" rx="12" fill="{WYROK_FILL}"/>')
    svg.append(f'<text x="{W//2}" y="41" text-anchor="middle" font-size="15" '
               f'fill="#F1EFE8" font-weight="bold">ERYK RESPONDER™ · Drzewo decyzyjne · {sn}</text>')

    y = 80  # bieżąca pozycja Y

    for p_idx, pytanie in enumerate(pytania):
        tresc = pytanie.get("tresc", f"Pytanie {p_idx+1}")
        opcje = pytanie.get("opcje", {})
        p_num = p_idx + 1

        # ── Korzeń pytania ────────────────────────────────────────────────────
        root_x = W // 2 - ROOT_W // 2
        root_lines = _wrap_svg_text(tresc, 70)
        root_h = max(ROW_ROOT, 28 + len(root_lines) * 18)

        svg.append(f'<rect x="{root_x}" y="{y}" width="{ROOT_W}" height="{root_h}" '
                   f'rx="8" fill="{ROOT_FILL}" stroke="{ROOT_STROKE}" stroke-width="2"/>')
        svg.append(f'<text x="{W//2}" y="{y + 16}" text-anchor="middle" font-size="11" '
                   f'fill="{ROOT_STROKE}" font-weight="bold" letter-spacing="2">PYTANIE {p_num}</text>')
        svg.append(_svg_text_block(root_lines, W//2, y + 32, font_size=13,
                                   fill=ROOT_TEXT, font_weight="bold"))

        y_root_bottom = y + root_h
        y_opc = y_root_bottom + ROW_GAP

        # ── Trzy kolumny opcji (A, B, C) ─────────────────────────────────────
        for col_idx, lit in enumerate(["A", "B", "C"]):
            cx = COL_X[col_idx]
            c = COLORS.get(lit, COLORS["A"])
            opcja = opcje.get(lit, {})
            tekst_opc = opcja.get("tekst", f"Opcja {lit}")
            reakcja   = opcja.get("reakcja", "")
            runda2    = opcja.get("runda2", {})
            r2_pytanie = runda2.get("pytanie", "")
            r2_opcje   = runda2.get("opcje", {})

            # Linia korzeń → opcja
            svg.append(f'<line x1="{W//2}" y1="{y_root_bottom}" x2="{cx}" y2="{y_opc}" '
                       f'stroke="{c["line"]}" stroke-width="1.5" stroke-dasharray="5,3"/>')
            # Etykieta litery na linii
            lbl_x = (W//2 + cx) // 2
            lbl_y = (y_root_bottom + y_opc) // 2
            svg.append(f'<circle cx="{lbl_x}" cy="{lbl_y}" r="11" fill="{c["stroke"]}"/>')
            svg.append(f'<text x="{lbl_x}" y="{lbl_y + 4}" text-anchor="middle" '
                       f'font-size="11" font-weight="bold" fill="white">{lit}</text>')

            # Kafelek opcji R1
            opc_lines = _wrap_svg_text(tekst_opc, 42)
            opc_h = max(ROW_OPC, 20 + len(opc_lines) * 18)
            svg.append(f'<rect x="{cx - OPC_W//2}" y="{y_opc}" width="{OPC_W}" height="{opc_h}" '
                       f'rx="6" fill="{c["fill"]}" stroke="{c["stroke"]}" stroke-width="1.5"/>')
            svg.append(f'<text x="{cx}" y="{y_opc + 14}" text-anchor="middle" '
                       f'font-size="10" fill="{c["stroke"]}" font-weight="bold" letter-spacing="1">{lit})</text>')
            svg.append(_svg_text_block(opc_lines, cx, y_opc + 28,
                                       font_size=11, fill=c["text"], font_weight="normal"))

            y_reakc = y_opc + opc_h + 8

            # Kafelek reakcji Eryka
            if reakcja:
                reakc_lines = _wrap_svg_text(f'Eryk: „{reakcja}"', 50)
                reakc_h = max(ROW_REAKC, 16 + len(reakc_lines) * 16)
                svg.append(f'<rect x="{cx - OPC_W//2}" y="{y_reakc}" width="{OPC_W}" height="{reakc_h}" '
                           f'rx="4" fill="{REAKC_FILL}" stroke="{REAKC_STROKE}" stroke-width="1" '
                           f'stroke-dasharray="4,2"/>')
                svg.append(_svg_text_block(reakc_lines, cx, y_reakc + 14,
                                           font_size=10, fill="#5A4A2A",
                                           font_weight="normal"))
                y_r2 = y_reakc + reakc_h + 12
            else:
                y_r2 = y_reakc + 8

            # ── Runda 2 ───────────────────────────────────────────────────────
            if r2_pytanie and r2_opcje:
                # Pytanie rundy 2
                r2_pyt_lines = _wrap_svg_text(r2_pytanie, 44)
                r2_pyt_h = max(ROW_R2_PYT, 14 + len(r2_pyt_lines) * 16)
                svg.append(f'<rect x="{cx - OPC_W//2}" y="{y_r2}" width="{OPC_W}" height="{r2_pyt_h}" '
                           f'rx="4" fill="{ROOT_FILL}" stroke="{ROOT_STROKE}" stroke-width="1"/>')
                svg.append(f'<text x="{cx}" y="{y_r2 + 12}" text-anchor="middle" '
                           f'font-size="9" fill="{ROOT_STROKE}" font-weight="bold" letter-spacing="1">'
                           f'RUNDA 2 — {lit}</text>')
                svg.append(_svg_text_block(r2_pyt_lines, cx, y_r2 + 24,
                                           font_size=10, fill=ROOT_TEXT, font_weight="normal"))

                y_r2_opc = y_r2 + r2_pyt_h + 8

                # 3 opcje rundy 2 — w wierszu obok siebie
                r2_step = OPC_W // 3
                for r2_idx, r2_lit in enumerate(["A", "B", "C"]):
                    r2_opcja = r2_opcje.get(r2_lit, {})
                    r2_tekst  = r2_opcja.get("tekst", f"Opcja {r2_lit}")
                    r2_reakc  = r2_opcja.get("reakcja", "")
                    r2_c = COLORS.get(r2_lit, COLORS["A"])

                    r2x = cx - OPC_W//2 + r2_idx * r2_step
                    r2_lines = _wrap_svg_text(r2_tekst, 22)
                    r2_h = max(ROW_OPC2, 18 + len(r2_lines) * 15)

                    # Linia r2_pytanie → r2_opcja
                    svg.append(f'<line x1="{cx}" y1="{y_r2 + r2_pyt_h}" '
                               f'x2="{r2x + OPC2_W//3}" y2="{y_r2_opc}" '
                               f'stroke="{r2_c["line"]}" stroke-width="1" stroke-dasharray="3,2" opacity="0.7"/>')

                    svg.append(f'<rect x="{r2x}" y="{y_r2_opc}" width="{r2_step - 4}" height="{r2_h}" '
                               f'rx="3" fill="{r2_c["fill"]}" stroke="{r2_c["stroke"]}" stroke-width="1"/>')
                    svg.append(f'<text x="{r2x + (r2_step-4)//2}" y="{y_r2_opc + 12}" text-anchor="middle" '
                               f'font-size="9" fill="{r2_c["stroke"]}" font-weight="bold">'
                               f'{lit}{r2_lit}</text>')
                    svg.append(_svg_text_block(r2_lines, r2x + (r2_step-4)//2,
                                               y_r2_opc + 22, font_size=9,
                                               fill=r2_c["text"], font_weight="normal"))

                    # Reakcja rundy 2 (tylko 1 linia, pod kafelkiem)
                    if r2_reakc:
                        reakc2_y = y_r2_opc + r2_h + 2
                        reakc2_short = _wrap_svg_text(r2_reakc, 22)[0] if r2_reakc else ""
                        svg.append(f'<text x="{r2x + (r2_step-4)//2}" y="{reakc2_y + 10}" '
                                   f'text-anchor="middle" font-size="8" fill="#7A6040" '
                                   f'font-style="italic">{escape(reakc2_short[:30])}…</text>')

        # Oblicz Y dla następnego pytania — max dna wszystkich 3 kolumn
        y_next_pytanie = y_opc
        for lit in ["A", "B", "C"]:
            opcja  = opcje.get(lit, {})
            opc_h  = max(ROW_OPC, 20 + len(_wrap_svg_text(opcja.get("tekst",""), 42)) * 18)
            reakc_h = max(ROW_REAKC, 16 + len(_wrap_svg_text(opcja.get("reakcja",""), 50)) * 16) if opcja.get("reakcja") else 0
            runda2 = opcja.get("runda2", {})
            if runda2.get("pytanie") and runda2.get("opcje"):
                r2_pyt_h = max(ROW_R2_PYT, 14 + len(_wrap_svg_text(runda2.get("pytanie",""), 44)) * 16)
                r2_opc_h = max(ROW_OPC2, 18 + 2 * 15)
                col_bottom = y_opc + opc_h + 8 + reakc_h + 12 + r2_pyt_h + 8 + r2_opc_h + 20
            else:
                col_bottom = y_opc + opc_h + 8 + reakc_h + 20
            y_next_pytanie = max(y_next_pytanie, col_bottom)

        # Strzałka do następnego pytania lub wyroku
        y_arrow_end = y_next_pytanie + 40
        svg.append(f'<line x1="{W//2}" y1="{y_next_pytanie}" x2="{W//2}" y2="{y_arrow_end}" '
                   f'stroke="#888780" stroke-width="2" '
                   f'marker-end="url(#arr-m)"/>')
        y = y_arrow_end + 10

    # ── Wyrok końcowy ─────────────────────────────────────────────────────────
    wyrok_lines = _wrap_svg_text(wyrok, 70)
    wyrok_h = max(80, 30 + len(wyrok_lines) * 18)
    svg.append(f'<rect x="{W//2 - 550}" y="{y}" width="1100" height="{wyrok_h}" '
               f'rx="10" fill="{WYROK_FILL}" stroke="#8B6914" stroke-width="2"/>')
    svg.append(f'<text x="{W//2}" y="{y + 22}" text-anchor="middle" font-size="13" '
               f'fill="#8B6914" font-weight="bold" letter-spacing="2">⚖ WYROK KOŃCOWY</text>')
    svg.append(_svg_text_block(wyrok_lines, W//2, y + 42,
                               font_size=11, fill="#E8D5B0", font_weight="normal"))
    y += wyrok_h

    # Markery strzałek (defs)
    svg.insert(2, '<defs>'
        '<marker id="arr-m" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        '<path d="M2 1L8 5L2 9" fill="none" stroke="#888780" stroke-width="1.5" stroke-linecap="round"/>'
        '</marker></defs>')

    svg.append('</svg>')

    # Opakuj w HTML z auto-scroll
    svg_content = "\n".join(svg)
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Eryk Responder™ — Drzewo decyzyjne</title>
<style>
  body {{ margin: 0; background: #FAF8F4; font-family: Arial, sans-serif; }}
  .wrap {{ overflow-x: auto; padding: 16px; }}
  .info {{ font-size: 11px; color: #888; text-align: center; padding: 8px; }}
</style>
</head>
<body>
<div class="wrap">
{svg_content}
</div>
<div class="info">Eryk Responder™ · Drzewo decyzyjne · Wygenerowano automatycznie</div>
</body>
</html>"""


def _generate_svg_legacy(kroki: list, wyrok: str, sn: str) -> str:
    """Stara logika SVG dla struktury sekwencyjnej (kroki). Zachowana dla kompatybilności."""
    W = 2200
    num_kroki = len(kroki)
    svg_height = 100 + num_kroki * 350 + 200
    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg width="{W}" viewBox="0 0 {W} {svg_height}" xmlns="http://www.w3.org/2000/svg">',
        "<defs>",
        '  <marker id="arr-m" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        '    <path d="M2 1L8 5L2 9" fill="none" stroke="#888780" stroke-width="1.5" stroke-linecap="round"/>',
        "  </marker>",
        "</defs>",
        "<style>",
        "  .q-node rect { fill: #EEEDFE; stroke: #534AB7; stroke-width: 1; }",
        "  .leaf-a rect { fill: #E6F1FB; stroke: #185FA5; stroke-width: 0.8; }",
        "  .leaf-b rect { fill: #E1F5EE; stroke: #0F6E56; stroke-width: 0.8; }",
        "  .leaf-c rect { fill: #FAEEDA; stroke: #854F0B; stroke-width: 0.8; }",
        "</style>",
    ]
    svg_lines.append(f'<rect x="850" y="20" width="500" height="44" rx="14" fill="#2C2C2A"/>')
    svg_lines.append(f'<text x="1100" y="47" text-anchor="middle" font-size="14" fill="#F1EFE8" '
                     f'font-weight="bold" font-family="Arial">ERYK RESPONDER™ · {sn}</text>')
    y_current = 90
    for i, krok in enumerate(kroki, 1):
        pytanie = krok.get("pytanie") or krok.get("tresc", f"P{i}")
        opcje = krok.get("opcje", {})
        pyt_lines = _wrap_svg_text(pytanie, 48)
        node_h = max(60, 40 + len(pyt_lines) * 16)
        svg_lines.append(f'<g class="q-node"><rect x="750" y="{y_current}" width="700" height="{node_h}" rx="8"/>')
        svg_lines.append(_svg_text_block(pyt_lines, 1100, y_current + 22, 13, "#3C3489", "bold"))
        svg_lines.append("</g>")
        y_next = y_current + node_h + 26
        positions = {"A": 640, "B": 1100, "C": 1560}
        classes = {"A": "leaf-a", "B": "leaf-b", "C": "leaf-c"}
        fills = {"A": "#0C447C", "B": "#085041", "C": "#633806"}
        for lit in ["A", "B", "C"]:
            if lit not in opcje:
                continue
            tekst = opcje[lit].get("tekst", lit)
            lx = positions[lit]
            tlines = _wrap_svg_text(tekst, 38)
            lh = max(60, 32 + len(tlines) * 18)
            svg_lines.append(f'<line x1="{1100}" y1="{y_current + node_h}" x2="{lx}" y2="{y_next}" '
                             f'stroke="#888" stroke-width="1" stroke-dasharray="4,2"/>')
            svg_lines.append(f'<g class="{classes[lit]}"><rect x="{lx-160}" y="{y_next}" width="320" height="{lh}" rx="6"/>')
            svg_lines.append(_svg_text_block(tlines, lx, y_next + 18, 10, fills[lit], "normal"))
            svg_lines.append("</g>")
        y_current = y_next + 80
    wyrok_lines = _wrap_svg_text(wyrok, 50)
    wyrok_h = max(60, 30 + len(wyrok_lines) * 16)
    svg_lines.append(f'<rect x="850" y="{y_current}" width="500" height="{wyrok_h}" rx="8" fill="#3C3489"/>')
    svg_lines.append(f'<text x="1100" y="{y_current + 22}" text-anchor="middle" font-size="12" fill="#F1EFE8" '
                     f'font-weight="bold">⚖ WYROK KOŃCOWY</text>')
    svg_lines.append(_svg_text_block(wyrok_lines, 1100, y_current + 40, 10, "#E8D5B0", "normal"))
    svg_lines.append("</svg>")
    return f'<html><body style="margin:0;background:#faf8f4">{"".join(svg_lines)}</body></html>'
