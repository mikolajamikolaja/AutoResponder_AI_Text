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


def generate_thumbnail_jpg(
    gra: Dict[str, Any],
    sender_name: str = "",
    thumb_width: int = 900,
) -> Optional[bytes]:
    """
    Generuje miniaturowy JPG z tego samego SVG co eryk_diagram_interaktywny.html.
    Dzieki temu obrazek inline w mailu jest wierna, czytalna miniatura drzewa.

    Strategia:
      1. Generuj SVG przez generate_svg_html_interactive (ta sama logika co zalacznik)
      2. Wyciagnij blok <svg ...> z HTML
      3. Renderuj SVG -> PNG przez cairosvg (skalowanie do thumb_width)
      4. Konwertuj PNG -> JPG przez PIL
      5. Zwroc bytes JPG

    Fallback: jezeli cairosvg niedostepny, zwraca None
    (caller uzyje wtedy starego generate_jpg_diagram lub pominie obrazek).
    """
    try:
        # Krok 1: Generuj HTML z SVG
        html_str = generate_svg_html_interactive(gra, sender_name)
        if not html_str:
            _log("generate_thumbnail_jpg: brak HTML z generate_svg_html_interactive")
            return None

        # Krok 2: Wyciagnij blok SVG z HTML
        # Szukamy od pierwszego <?xml lub <svg do zamykajacego </svg>
        svg_match = re.search(
            r"(<\?xml[^>]*\?>\s*)?(<svg\b.*?</svg>)",
            html_str,
            re.DOTALL | re.IGNORECASE,
        )
        if not svg_match:
            _log("generate_thumbnail_jpg: nie znaleziono bloku SVG w HTML")
            return None

        svg_str = svg_match.group(2)  # sam blok <svg>...</svg>

        # Upewnij sie ze ma xmlns (cairosvg wymaga)
        if "xmlns=" not in svg_str:
            svg_str = svg_str.replace(
                "<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1
            )

        # Krok 3: Odczytaj oryginalne wymiary SVG zeby zachowac proporcje
        w_match = re.search(r'<svg[^>]+width=["\'](\d+)["\']', svg_str)
        h_match = re.search(r'<svg[^>]+height=["\'](\d+)["\']', svg_str)
        orig_w = int(w_match.group(1)) if w_match else 2000
        orig_h = int(h_match.group(1)) if h_match else 1200
        ratio = orig_h / orig_w
        thumb_height = int(thumb_width * ratio)

        # Krok 4: Render SVG -> PNG przez cairosvg
        try:
            import cairosvg

            png_bytes = cairosvg.svg2png(
                bytestring=svg_str.encode("utf-8"),
                output_width=thumb_width,
                output_height=thumb_height,
            )
        except ImportError:
            _log(
                "generate_thumbnail_jpg: cairosvg niedostepny — fallback na generate_jpg_diagram"
            )
            return None

        # Krok 5: PNG -> JPG przez PIL
        from PIL import Image

        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=88, optimize=True)
        jpg_bytes = buf.getvalue()

        _log(
            f"generate_thumbnail_jpg: SVG({orig_w}x{orig_h}) -> JPG({thumb_width}x{thumb_height}) = {len(jpg_bytes)//1024}KB"
        )
        return jpg_bytes

    except Exception as e:
        _log(f"generate_thumbnail_jpg: blad — {e}")
        return None


def generate_svg_html_interactive(
    gra: Dict[str, Any], sender_name: str = "", tytul: str = ""
) -> str:
    """
    Generuje samodzielny plik HTML z interaktywnym drzewem decyzyjnym Eryka.

    Funkcje:
    - Sekwencyjne odkrywanie pytań (P2, P3... ukryte do czasu wyboru w P1)
    - R2 (AA/AB/AC) ukryte do czasu kliknięcia opcji R1
    - Jeśli opcja ma R2 → następne pytanie odkrywa się dopiero po wyborze w R2
    - 27 (lub więcej) unikalnych wyroków końcowych zależnych od ścieżki
    - Tryb PILNE: 5 pytań zamiast 3 gdy mail zawierał słowo PILNE
    - Pasek postępu który kłamie (cofa się do 50% przy 99%)
    - Efekt maszyny do pisania dla komentarzy Eryka
    - Dźwięki z GitHub Pages (beep/plink przy R1, bounce/bop przy R2, eureka przy wyroku)
    - Przełącznik stylu: retro terminal (zielone na czarnym) / żółta kartka
    - Media query: flex-direction:column na ekranach < 600px
    - Kompatybilność wsteczna ze starą strukturą (kroki)
    """
    pytania = gra.get("pytania", [])
    kroki_legacy = gra.get("kroki", [])
    wyroki_dict = gra.get("wyroki", {})  # słownik "ABC" → tekst wyroku
    wyrok_domyslny = gra.get("wyrok", "Brak wyroku.")
    pilne = bool(gra.get("pilne", False))
    sn = escape(sender_name or "Anonim")

    # ── Stara struktura — zachowaj kompatybilność ─────────────────────────────
    if not pytania and kroki_legacy:
        _log("Używam starej struktury (kroki) dla SVG diagramu")
        return _generate_svg_legacy(kroki_legacy, wyrok_domyslny, sn)

    if not pytania:
        _log("Brak danych do diagramu SVG")
        return "<p>Brak danych do diagramu.</p>"

    n_pytan = len(pytania)
    _log(
        f"Generuję HTML (nowa struktura): {n_pytan} pytań, pilne={pilne}, wyroki={len(wyroki_dict)}"
    )

    AUDIO_BASE = "https://legionowopawel.github.io/AutoResponder_AI_Text/audio/"
    # Dźwięki per zdarzenie
    SND_R1 = ["beep.mp3", "plink.mp3", "bop.mp3"]
    SND_R2 = ["bounce.mp3", "bop.mp3", "bubbles.mp3"]
    SND_WYROK = ["eureka.mp3", "wishgranted.mp3"]
    SND_PILNE = ["nextlevel.mp3"]

    # Serializuj listy dźwięków do JSON (bezpieczne do wklejenia w JS)
    import json as _json

    snd_r1_js = _json.dumps(SND_R1)
    snd_r2_js = _json.dumps(SND_R2)
    snd_wyrok_js = _json.dumps(SND_WYROK)
    snd_pilne_js = _json.dumps(SND_PILNE)
    pilne_js = "true" if pilne else "false"
    audio_base_js = _json.dumps(AUDIO_BASE)

    # Serializuj słownik wyroków do JSON
    wyroki_js = _json.dumps(wyroki_dict, ensure_ascii=False)

    # ── CSS ───────────────────────────────────────────────────────────────────
    css = """
/* ===== RESET ===== */
*{box-sizing:border-box;margin:0;padding:0}

/* ===== STYLE BAZOWY (domyślny: klasyczny) ===== */
body{background:#FAF8F4;font-family:Arial,sans-serif;transition:background .4s,color .4s}
.dg-wrap{padding:16px;max-width:980px;margin:0 auto}

/* NAGŁÓWEK */
.dg-header{background:#2C2C2A;color:#F1EFE8;text-align:center;padding:10px 20px;
  border-radius:8px;font-size:13px;font-weight:bold;margin-bottom:8px;letter-spacing:1px}
.pilne-badge{display:inline-block;background:#CC0000;color:#fff;
  font-size:10px;padding:2px 8px;border-radius:10px;margin-left:8px;
  animation:blink 1s step-end infinite}
@keyframes blink{50%{opacity:0}}

/* PRZEŁĄCZNIK STYLU */
.style-switcher{display:flex;gap:8px;justify-content:center;margin-bottom:12px;flex-wrap:wrap}
.style-btn{border:1.5px solid #888;border-radius:20px;padding:4px 14px;
  font-size:10px;cursor:pointer;background:transparent;font-family:inherit;
  transition:all .2s}
.style-btn.active{background:#534AB7;color:#fff;border-color:#534AB7}

/* PASEK POSTĘPU */
.progress-wrap{margin:0 auto 12px;max-width:700px}
.progress-label{font-size:10px;color:#888;margin-bottom:3px;text-align:center;
  font-style:italic;min-height:16px}
.progress-bar-bg{background:#E0DEDA;border-radius:4px;height:8px;overflow:hidden}
.progress-bar-fill{background:linear-gradient(90deg,#534AB7,#8B7ED8);
  height:100%;border-radius:4px;width:0%;transition:width .4s ease}

/* TRACKER ŚCIEŻKI */
.path-tracker{font-size:11px;color:#888;text-align:center;margin:6px 0;min-height:16px}

/* PYTANIE */
.dg-question{background:#EEEDFE;border:2px solid #534AB7;border-radius:8px;
  padding:12px 18px;margin:0 auto 8px;text-align:center}
.dg-q-label{font-size:10px;color:#534AB7;font-weight:bold;letter-spacing:2px;margin-bottom:4px}
.dg-q-text{font-size:13px;color:#3C3489;font-weight:bold;line-height:1.4}

/* KOLUMNY OPCJI */
.dg-cols{display:flex;gap:10px;margin-bottom:8px}
.dg-col{flex:1;display:flex;flex-direction:column;gap:6px}

/* PRZYCISKI OPCJI R1 */
.opc-btn{border-radius:6px;padding:10px;cursor:pointer;border-width:1.5px;
  border-style:solid;text-align:left;width:100%;font-family:inherit;
  transition:filter .15s,transform .1s;background:inherit}
.opc-btn:hover{filter:brightness(.93);transform:translateY(-1px)}
.opc-btn:active{transform:translateY(0)}
.opc-btn.active{outline:2px solid;outline-offset:2px}
.opc-label{font-size:9px;font-weight:bold;letter-spacing:1px;margin-bottom:2px}
.opc-text{font-size:11px;line-height:1.4}

/* REAKCJA ERYKA — efekt maszyny do pisania */
.reakcja{border-radius:4px;padding:8px;font-size:10px;font-style:italic;
  border:1px dashed #C8A96A;background:#FFF8F0;color:#7A6040;
  line-height:1.5;display:none;min-height:20px}
.reakcja-cursor{display:inline-block;width:2px;height:11px;
  background:#7A6040;margin-left:1px;animation:blink 0.7s step-end infinite;
  vertical-align:text-bottom}

/* R2 */
.r2-block{margin-top:6px;display:none}
.r2-question{background:#F0E6FA;border:1px solid #7A5FB7;border-radius:5px;
  padding:8px;font-size:10px;color:#534AB7;font-weight:bold;
  margin-bottom:6px;text-align:center}
.r2-sub-label{font-size:9px;color:#7A5FB7;letter-spacing:1px;margin-bottom:3px}
.r2-cols{display:flex;gap:4px}
.r2-col{flex:1}
.r2-btn{border-radius:4px;padding:6px 5px;font-size:9px;border-width:1px;
  border-style:solid;text-align:center;line-height:1.4;cursor:pointer;
  width:100%;font-family:inherit;background:inherit;
  transition:filter .15s,transform .1s}
.r2-btn:hover{filter:brightness(.92);transform:translateY(-1px)}
.r2-btn.active{outline:2px solid;outline-offset:1px}
.r2-tile-label{font-weight:bold;margin-bottom:2px}
.r2-hint{font-size:9px;color:#aaa;text-align:center;margin-top:2px;font-style:italic}

/* SEKCJE UKRYTE */
.section-hidden{display:none}
.dg-arrow{text-align:center;color:#888780;font-size:20px;margin:6px 0}

/* WYROKI */
.wyrok{background:#2C2C2A;color:#E8D5B0;border-radius:8px;padding:16px;
  text-align:center;margin:0 auto;border:2px solid #8B6914;display:none}
.wyrok-label{font-size:10px;color:#8B6914;font-weight:bold;
  letter-spacing:2px;margin-bottom:8px}
.wyrok-sciezka{font-size:9px;color:#aaa;margin-bottom:8px;font-style:italic}
.wyrok-text{font-size:12px;line-height:1.6}

/* ===== STYL: RETRO TERMINAL ===== */
body.terminal{background:#0D0D0D;color:#00FF41;font-family:'Courier New',monospace}
body.terminal .dg-header{background:#001A00;color:#00FF41;border:1px solid #00FF41}
body.terminal .dg-question{background:#001A00;border-color:#00FF41;color:#00FF41}
body.terminal .dg-q-label{color:#00FF41}
body.terminal .dg-q-text{color:#00FF41}
body.terminal .opc-btn{background:#001A00 !important;border-color:#00FF41 !important;
  color:#00FF41 !important}
body.terminal .opc-label{color:#00FF41 !important}
body.terminal .reakcja{background:#001A00;border-color:#00FF41;color:#00FF41}
body.terminal .r2-question{background:#001A00;border-color:#00FF41;color:#00FF41}
body.terminal .r2-btn{background:#001A00 !important;border-color:#00FF41 !important;
  color:#00FF41 !important}
body.terminal .wyrok{background:#001A00;border-color:#00FF41;color:#00FF41}
body.terminal .wyrok-label{color:#00FF41}
body.terminal .wyrok-sciezka{color:#00AA2A}
body.terminal .progress-bar-fill{background:#00FF41}
body.terminal .style-btn{color:#00FF41;border-color:#00FF41}
body.terminal .style-btn.active{background:#00FF41;color:#000}
body.terminal .path-tracker{color:#00AA2A}
body.terminal .progress-label{color:#00AA2A}

/* ===== STYL: ŻÓŁTA KARTKA ===== */
body.kartka{background:#F5EDB0;font-family:Georgia,serif}
body.kartka .dg-wrap{background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'%3E%3Crect width='4' height='4' fill='%23F0E4A0'/%3E%3Ccircle cx='1' cy='1' r='0.5' fill='%23E8D880' opacity='0.5'/%3E%3C/svg%3E");
  box-shadow:3px 3px 12px rgba(0,0,0,.3);padding:24px;
  transform:rotate(-0.3deg);border:1px solid #C8A820}
body.kartka .dg-header{background:#8B6914;font-family:Georgia,serif}
body.kartka .dg-question{background:#FFFDE0;border-color:#8B6914}
body.kartka .dg-q-label{color:#8B6914}
body.kartka .dg-q-text{color:#5A3E00}
body.kartka .opc-btn{font-family:Georgia,serif}
body.kartka .reakcja{background:#FFFDE0;border-color:#8B6914;font-family:Georgia,serif}
body.kartka .wyrok{background:#8B6914;border-color:#5A3E00}

/* ===== MEDIA QUERY — TELEFON ===== */
@media (max-width:600px){
  .dg-cols{flex-direction:column}
  .dg-col{width:100%}
  .r2-cols{flex-direction:column}
  .r2-col{width:100%}
  .dg-wrap{padding:8px;margin:0}
  .dg-question{padding:10px 12px;margin-bottom:6px}
  .dg-q-text{font-size:14px}
  .dg-q-label{font-size:11px}
  .opc-btn{padding:12px 10px;margin-bottom:2px}
  .opc-text{font-size:13px}
  .opc-label{font-size:11px}
  .dg-header{font-size:12px;padding:10px;line-height:1.4}
  .wyrok-text{font-size:12px}
  .wyrok{padding:14px 12px}
  .r2-btn{padding:10px 6px;font-size:11px}
  .r2-question{font-size:11px;padding:10px}
  .reakcja{font-size:11px;padding:10px}
  .style-btn{font-size:11px;padding:6px 16px}
  .progress-label{font-size:11px}
  .path-tracker{font-size:11px}
}
"""

    # ── Buduj HTML treść pytań ────────────────────────────────────────────────
    def esc(s):
        return escape(str(s)) if s else ""

    body_parts = []
    # ── Nagłówek: pytanie użytkownika jako tytuł strony ──────────────────────
    tytul_safe = (
        escape(tytul.strip())
        if tytul and tytul.strip()
        else (sn or "Eryk Responder&#8482;")
    )
    # Skróć do 100 znaków dla czytelności
    if len(tytul_safe) > 100:
        tytul_safe = tytul_safe[:97] + "&#8230;"

    body_parts.append(f'<div class="dg-header">{tytul_safe}')
    if pilne:
        body_parts.append('<span class="pilne-badge">&#9888; PILNE</span>')
    body_parts.append("</div>")

    # Przełącznik stylu
    body_parts.append("""<div class="style-switcher">
  <button class="style-btn active" onclick="setStyl('klasyczny',this)">&#128196; Klasyczny</button>
  <button class="style-btn" onclick="setStyl('terminal',this)">&#9608; Terminal</button>
  <button class="style-btn" onclick="setStyl('kartka',this)">&#128195; Kartka</button>
</div>""")

    # Pasek postępu
    body_parts.append("""<div class="progress-wrap">
  <div class="progress-label" id="prog-label">Inicjalizacja systemu Eryka&hellip;</div>
  <div class="progress-bar-bg"><div class="progress-bar-fill" id="prog-bar"></div></div>
</div>""")

    # Tracker ścieżki
    body_parts.append(
        '<div class="path-tracker" id="path-tracker">Wybierz odpowied&#378; na Pytanie 1 &rarr;</div>'
    )

    # Generuj sekcje pytań
    for p_idx, pytanie in enumerate(pytania):
        tresc = esc(pytanie.get("tresc", f"Pytanie {p_idx+1}"))
        opcje = pytanie.get("opcje", {})
        p_num = p_idx + 1
        hidden_cls = ' class="section-hidden"' if p_idx > 0 else ""

        if p_idx > 0:
            body_parts.append(f'<div id="sekcja-p{p_num}" class="section-hidden">')
            body_parts.append('<div class="dg-arrow">&#8595;</div>')
        else:
            body_parts.append(f'<div id="sekcja-p{p_num}">')

        body_parts.append(f"""<div class="dg-question">
  <div class="dg-q-label">PYTANIE {p_num}</div>
  <div class="dg-q-text">{tresc}</div>
</div>""")

        body_parts.append(f'<div class="dg-cols" id="cols-p{p_num}">')

        for lit in ["A", "B", "C"]:
            opcja = opcje.get(lit, {})
            tekst_opc = esc(opcja.get("tekst", f"Opcja {lit}"))
            reakcja = opcja.get("reakcja", "")
            runda2 = opcja.get("runda2", {})
            r2_pyt = runda2.get("pytanie", "")
            r2_opcje = runda2.get("opcje", {})
            has_r2 = bool(r2_pyt and r2_opcje)
            has_r2_js = "true" if has_r2 else "false"

            COLORS = {
                "A": {"fill": "#E6F1FB", "stroke": "#185FA5", "text": "#0C447C"},
                "B": {"fill": "#E1F5EE", "stroke": "#0F6E56", "text": "#085041"},
                "C": {"fill": "#FAEEDA", "stroke": "#854F0B", "text": "#633806"},
            }
            c = COLORS[lit]
            btn_style = f'background:{c["fill"]};border-color:{c["stroke"]};color:{c["text"]};outline-color:{c["stroke"]}'

            body_parts.append('<div class="dg-col">')
            body_parts.append(
                f'<button class="opc-btn" style="{btn_style}" '
                f"onclick=\"wybierzR1({p_num},'{lit}',this,{has_r2_js})\">"
                f'<div class="opc-label" style="color:{c["stroke"]}">{lit})</div>'
                f'<div class="opc-text">{tekst_opc}</div>'
                f"</button>"
            )

            # Reakcja Eryka (typewriter)
            rea_id = f"rea-{p_num}-{lit}"
            body_parts.append(
                f'<div class="reakcja" id="{rea_id}" data-tekst="{esc(reakcja)}"></div>'
            )

            # R2
            if has_r2:
                r2_id = f"r2-{p_num}-{lit}"
                body_parts.append(f'<div class="r2-block" id="{r2_id}">')
                body_parts.append(
                    f'<div class="r2-question">'
                    f'<div class="r2-sub-label">RUNDA 2 &mdash; {lit}</div>'
                    f"{esc(r2_pyt)}</div>"
                )
                body_parts.append('<div class="r2-cols">')

                R2_COLORS = [
                    {"fill": "#E6F1FB", "stroke": "#185FA5", "text": "#0C447C"},
                    {"fill": "#E1F5EE", "stroke": "#0F6E56", "text": "#085041"},
                    {"fill": "#FAEEDA", "stroke": "#854F0B", "text": "#633806"},
                ]
                for r2_idx, r2_lit in enumerate(["A", "B", "C"]):
                    r2_opcja = r2_opcje.get(r2_lit, {})
                    r2_tekst = esc(r2_opcja.get("tekst", f"Opcja {r2_lit}"))
                    r2c = R2_COLORS[r2_idx]
                    r2_label = f"{lit}{r2_lit}"
                    r2_style = f'background:{r2c["fill"]};border-color:{r2c["stroke"]};color:{r2c["text"]};outline-color:{r2c["stroke"]}'
                    body_parts.append(
                        f'<div class="r2-col">'
                        f'<button class="r2-btn" style="{r2_style}" '
                        f"onclick=\"wybierzR2({p_num},'{lit}','{r2_lit}',this)\">"
                        f'<div class="r2-tile-label">{r2_label}</div>{r2_tekst}'
                        f"</button></div>"
                    )

                body_parts.append("</div>")  # r2-cols
                body_parts.append(
                    '<div class="r2-hint">&#8679; wybierz aby przej&#347;&#263; dalej</div>'
                )
                body_parts.append("</div>")  # r2-block

            body_parts.append("</div>")  # dg-col

        body_parts.append("</div>")  # dg-cols
        body_parts.append("</div>")  # sekcja-pN

    # Sekcja wyroku (wszystkie wyroki ukryte — JS pokaże właściwy)
    body_parts.append(
        '<div id="sekcja-wyrok" class="section-hidden"><div class="dg-arrow">&#8595;</div>'
    )

    if wyroki_dict:
        for klucz, tekst_wyroku in wyroki_dict.items():
            wyrok_id = f"wyrok-{klucz}"
            body_parts.append(
                f'<div id="{wyrok_id}" class="wyrok">'
                f'<div class="wyrok-label">&#9878; WYROK KO&#323;COWY</div>'
                f'<div class="wyrok-sciezka">&#346;cie&#380;ka: {" &rarr; ".join(list(klucz))}</div>'
                f'<div class="wyrok-text">{esc(tekst_wyroku)}</div>'
                f"</div>"
            )
    else:
        # Fallback: jeden wyrok dla wszystkich ścieżek
        body_parts.append(
            f'<div id="wyrok-fallback" class="wyrok">'
            f'<div class="wyrok-label">&#9878; WYROK KO&#323;COWY</div>'
            f'<div class="wyrok-text">{esc(wyrok_domyslny)}</div>'
            f"</div>"
        )

    body_parts.append("</div>")  # sekcja-wyrok

    content_html = "\n".join(body_parts)

    # ── JavaScript ────────────────────────────────────────────────────────────
    js = f"""
const AUDIO_BASE = {audio_base_js};
const SND_R1     = {snd_r1_js};
const SND_R2     = {snd_r2_js};
const SND_WYROK  = {snd_wyrok_js};
const SND_PILNE  = {snd_pilne_js};
const PILNE      = {pilne_js};
const WYROKI     = {wyroki_js};
const N_PYTAN    = {n_pytan};

// ── Audio ─────────────────────────────────────────────────────────────────
function graj(lista) {{
  var plik = lista[Math.floor(Math.random() * lista.length)];
  var a = new Audio(AUDIO_BASE + plik);
  a.preload = 'auto';
  a.play().catch(function(){{}});
}}

// ── Pasek postępu ─────────────────────────────────────────────────────────
var progVal = 0;
var progTimer = null;
var progFaza = 0;  // 0=wolny start, 1=przyspieszenie, 2=cofnięcie, 3=zatrzymanie
var PROG_LABELS = [
  "Inicjalizacja systemu Eryka\u2026",
  "Analiza merytoryczna odpowiedzi\u2026",
  "Weryfikacja sp\u00f3jno\u015bci logicznej\u2026",
  "Ocena intencji nadawcy\u2026",
  "\u26a0\ufe0f Wykryto nie\u015bcis\u0142o\u015b\u0107 w intencjach nadawcy",
  "Ponowna analiza od 50%\u2026",
  "Finalizacja wyroku\u2026",
  "Wyrok gotowy."
];

function startProgress() {{
  var bar = document.getElementById('prog-bar');
  var lbl = document.getElementById('prog-label');
  progVal = 0; progFaza = 0;
  bar.style.width = '0%';
  lbl.textContent = PROG_LABELS[0];
  if(progTimer) clearInterval(progTimer);
  progTimer = setInterval(function() {{
    if(progFaza === 0) {{
      progVal += 2;
      lbl.textContent = PROG_LABELS[Math.floor(progVal/30) % 2];
      if(progVal >= 70) {{ progFaza = 1; }}
    }} else if(progFaza === 1) {{
      progVal += 3;
      lbl.textContent = PROG_LABELS[2 + Math.floor((progVal-70)/10) % 2];
      if(progVal >= 99) {{ progFaza = 2; lbl.textContent = PROG_LABELS[4]; }}
    }} else if(progFaza === 2) {{
      // Cofa się dramatycznie do 50%
      progVal -= 5;
      if(progVal <= 50) {{ progFaza = 3; lbl.textContent = PROG_LABELS[5]; }}
    }} else if(progFaza === 3) {{
      progVal += 1;
      if(progVal >= 95) {{ lbl.textContent = PROG_LABELS[6]; }}
      if(progVal >= 100) {{
        progVal = 100;
        lbl.textContent = PROG_LABELS[7];
        clearInterval(progTimer);
      }}
    }}
    bar.style.width = Math.min(100, Math.max(0, progVal)) + '%';
  }}, 60);
}}

// ── Typewriter ────────────────────────────────────────────────────────────
var twTimers = {{}};
function typewriter(elId, tekst, speed) {{
  speed = speed || 28;
  var el = document.getElementById(elId);
  if(!el) return;
  el.style.display = 'block';
  el.innerHTML = '';
  if(twTimers[elId]) clearInterval(twTimers[elId]);
  var i = 0;
  var cursor = '<span class="reakcja-cursor"></span>';
  twTimers[elId] = setInterval(function() {{
    el.innerHTML = 'Eryk: \u201e' + tekst.substring(0, i) + '\u201d' + (i < tekst.length ? cursor : '');
    i++;
    if(i > tekst.length) {{
      clearInterval(twTimers[elId]);
      el.innerHTML = 'Eryk: \u201e' + tekst + '\u201d';
    }}
  }}, speed);
}}

// ── Styl (terminal / kartka / klasyczny) ──────────────────────────────────
function setStyl(styl, btn) {{
  document.body.className = styl === 'klasyczny' ? '' : styl;
  document.querySelectorAll('.style-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
}}

// ── Ścieżka użytkownika ───────────────────────────────────────────────────
var sciezka = {{}};
for(var _p=1; _p<=N_PYTAN; _p++) sciezka[_p] = null;

function updateTracker() {{
  var parts = [];
  for(var p=1; p<=N_PYTAN; p++) {{
    if(sciezka[p]) parts.push('P' + p + ':' + sciezka[p]);
    else break;
  }}
  var t = document.getElementById('path-tracker');
  if(parts.length === 0) t.textContent = 'Wybierz odpowied\u017a na Pytanie 1 \u2192';
  else if(parts.length < N_PYTAN) t.textContent = '\u015acie\u017cka: ' + parts.join(' \u2192 ') + ' \u2192 ?';
  else t.textContent = '\u015acie\u017cka: ' + Object.values(sciezka).join(' \u2192 ');
}}

// ── Reset sekcji ──────────────────────────────────────────────────────────
function resetSekcja(pyt) {{
  var cols = document.getElementById('cols-p' + pyt);
  if(!cols) return;
  cols.querySelectorAll('.opc-btn').forEach(function(b){{ b.classList.remove('active'); }});
  cols.querySelectorAll('.reakcja').forEach(function(r){{ r.style.display='none'; r.innerHTML=''; }});
  cols.querySelectorAll('.r2-block').forEach(function(r){{ r.style.display='none'; }});
  cols.querySelectorAll('.r2-btn').forEach(function(b){{ b.classList.remove('active'); }});
  sciezka[pyt] = null;
}}

function odkryjNastepne(pyt) {{
  var nastepna = pyt + 1;
  var sek = document.getElementById('sekcja-p' + nastepna);
  if(sek) {{
    sek.style.display = 'block';
    setTimeout(function(){{ sek.scrollIntoView({{behavior:'smooth',block:'nearest'}}); }}, 100);
  }} else {{
    pokazWyrok();
  }}
}}

// ── Wybór opcji R1 ────────────────────────────────────────────────────────
function wybierzR1(pyt, lit, btn, maR2) {{
  graj(PILNE ? SND_PILNE : SND_R1);
  startProgress();

  var cols = document.getElementById('cols-p' + pyt);
  cols.querySelectorAll('.opc-btn').forEach(function(b){{ b.classList.remove('active'); }});
  cols.querySelectorAll('.reakcja').forEach(function(r){{ r.style.display='none'; r.innerHTML=''; }});
  cols.querySelectorAll('.r2-block').forEach(function(r){{ r.style.display='none'; }});
  cols.querySelectorAll('.r2-btn').forEach(function(b){{ b.classList.remove('active'); }});

  btn.classList.add('active');
  sciezka[pyt] = lit;

  // Reset kolejnych pytań
  for(var p = pyt+1; p <= N_PYTAN; p++) {{
    resetSekcja(p);
    var sek = document.getElementById('sekcja-p' + p);
    if(sek) sek.style.display = 'none';
  }}
  document.getElementById('sekcja-wyrok').style.display = 'none';

  // Typewriter dla reakcji
  var reaEl = document.getElementById('rea-' + pyt + '-' + lit);
  if(reaEl) {{
    var tekst = reaEl.getAttribute('data-tekst') || '';
    if(tekst) typewriter('rea-' + pyt + '-' + lit, tekst, 25);
  }}

  if(maR2) {{
    // Pokaż R2 — czekaj na wybór przed odkryciem następnego pytania
    var r2 = document.getElementById('r2-' + pyt + '-' + lit);
    if(r2) {{ r2.style.display = 'block'; setTimeout(function(){{ graj(SND_R2); }}, 400); }}
  }} else {{
    // Brak R2 — od razu odkryj następne
    setTimeout(function(){{ odkryjNastepne(pyt); }}, 600);
  }}

  updateTracker();
}}

// ── Wybór opcji R2 ────────────────────────────────────────────────────────
function wybierzR2(pyt, litR1, litR2, btn) {{
  graj(SND_R2);
  var r2block = document.getElementById('r2-' + pyt + '-' + litR1);
  if(r2block) r2block.querySelectorAll('.r2-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
  setTimeout(function(){{ odkryjNastepne(pyt); }}, 400);
  updateTracker();
}}

// ── Pokaż wyrok końcowy ───────────────────────────────────────────────────
function pokazWyrok() {{
  graj(SND_WYROK);
  // Ukryj wszystkie wyroki
  document.querySelectorAll('.wyrok').forEach(function(w){{ w.style.display='none'; }});

  // Zbuduj klucz ze ścieżki
  var klucz = '';
  for(var p=1; p<=N_PYTAN; p++) klucz += (sciezka[p] || 'X');

  var w = document.getElementById('wyrok-' + klucz);
  if(!w) w = document.getElementById('wyrok-fallback');
  if(w) w.style.display = 'block';

  var sek = document.getElementById('sekcja-wyrok');
  sek.style.display = 'block';
  setTimeout(function(){{ sek.scrollIntoView({{behavior:'smooth',block:'nearest'}}); }}, 100);
  updateTracker();
}}

// ── Start ─────────────────────────────────────────────────────────────────
startProgress();
"""

    return f"""\ufeff<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>{tytul_safe if tytul_safe else 'Eryk Responder'}</title>
<style>
{css}
</style>
</head>
<body>
<div class="dg-wrap">
{content_html}
</div>
<div style="font-size:10px;color:#aaa;text-align:center;padding:12px 8px">
  Eryk Responder&#8482; &middot; Wygenerowano automatycznie &middot; Odpowied&#378; nast&#261;pi w odpowiednim czasie
</div>
<script>
{js}
</script>
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
    svg_lines.append(
        f'<rect x="850" y="20" width="500" height="44" rx="14" fill="#2C2C2A"/>'
    )
    svg_lines.append(
        f'<text x="1100" y="47" text-anchor="middle" font-size="14" fill="#F1EFE8" '
        f'font-weight="bold" font-family="Arial">ERYK RESPONDER™ · {sn}</text>'
    )
    y_current = 90
    for i, krok in enumerate(kroki, 1):
        pytanie = krok.get("pytanie") or krok.get("tresc", f"P{i}")
        opcje = krok.get("opcje", {})
        pyt_lines = _wrap_svg_text(pytanie, 48)
        node_h = max(60, 40 + len(pyt_lines) * 16)
        svg_lines.append(
            f'<g class="q-node"><rect x="750" y="{y_current}" width="700" height="{node_h}" rx="8"/>'
        )
        svg_lines.append(
            _svg_text_block(pyt_lines, 1100, y_current + 22, 13, "#3C3489", "bold")
        )
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
            svg_lines.append(
                f'<line x1="{1100}" y1="{y_current + node_h}" x2="{lx}" y2="{y_next}" '
                f'stroke="#888" stroke-width="1" stroke-dasharray="4,2"/>'
            )
            svg_lines.append(
                f'<g class="{classes[lit]}"><rect x="{lx-160}" y="{y_next}" width="320" height="{lh}" rx="6"/>'
            )
            svg_lines.append(
                _svg_text_block(tlines, lx, y_next + 18, 10, fills[lit], "normal")
            )
            svg_lines.append("</g>")
        y_current = y_next + 80
    wyrok_lines = _wrap_svg_text(wyrok, 50)
    wyrok_h = max(60, 30 + len(wyrok_lines) * 16)
    svg_lines.append(
        f'<rect x="850" y="{y_current}" width="500" height="{wyrok_h}" rx="8" fill="#3C3489"/>'
    )
    svg_lines.append(
        f'<text x="1100" y="{y_current + 22}" text-anchor="middle" font-size="12" fill="#F1EFE8" '
        f'font-weight="bold">⚖ WYROK KOŃCOWY</text>'
    )
    svg_lines.append(
        _svg_text_block(wyrok_lines, 1100, y_current + 40, 10, "#E8D5B0", "normal")
    )
    svg_lines.append("</svg>")
    return f'<html><body style="margin:0;background:#faf8f4">{"".join(svg_lines)}</body></html>'
