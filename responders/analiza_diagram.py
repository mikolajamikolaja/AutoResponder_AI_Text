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
from typing import Optional, Dict, Any

logger_enabled = True

def _log(msg: str):
    if logger_enabled:
        from flask import current_app
        try:
            current_app.logger.info(f"[edek-diagram] {msg}")
        except:
            print(f"[edek-diagram] {msg}")


def _build_graph_dot(gra: Dict[str, Any]) -> str:
    """
    Buduje DOT file dla Graphviz z drzewa decyzyjnego.
    Każde pytanie ma 3 dzieci (A, B, C).
    """
    kroki = gra.get("kroki", [])
    if not kroki:
        return ""
    
    dot_lines = [
        "digraph EdekTree {",
        "  rankdir=TB;",
        "  node [shape=box, style=filled, fontname=Arial];",
        "  edge [fontname=Arial, fontsize=9];",
    ]
    
    # Kolory dla opcji
    colors = {
        "A": "#E6F1FB",  # Niebieski
        "B": "#E1F5EE",  # Zielony
        "C": "#FAEEDA",  # Brązowy
        "Q": "#EEEDFE",  # Fiolet
    }
    
    # Każde pytanie
    for i, krok in enumerate(kroki, 1):
        nr = krok.get("nr", i)
        pytanie = krok.get("pytanie", f"P{i}")[:40]  # Skróć
        
        # Node pytania
        node_id = f"q{i}"
        dot_lines.append(
            f'  {node_id} [label="P{i}:\\n{pytanie}", fillcolor="{colors["Q"]}", '
            f'color="#534AB7", penwidth=1.5];'
        )
        
        # Połączenia do poprzedniego (jeśli i > 1)
        if i > 1:
            prev_id = f"q{i-1}"
            dot_lines.append(f"  {prev_id} -> {node_id} [style=dashed];")
        
        # Opcje A, B, C
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
        'shape=ellipse, penwidth=2];'
    )
    dot_lines.append(f"  q{len(kroki)} -> wyrok [style=dashed];")
    
    dot_lines.append("}")
    
    return "\n".join(dot_lines)


def _generate_jpg_via_graphviz(gra: Dict[str, Any], width: int = 1024, height: int = 1024) -> Optional[bytes]:
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


def _generate_jpg_fallback(gra: Dict[str, Any], width: int = 1024, height: int = 1024) -> Optional[bytes]:
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
        kroki = gra.get("kroki", [])
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
        draw.text((15, 12), "EDEK RESPONDER™", fill=(44, 44, 42), font=font_big)
        draw.text((15, 32), f"Diagram drzewa decyzyjnego ({len(kroki)} pytań × 3 opcje = {len(kroki)*3} ścieżek)", fill=(100, 100, 100), font=font_med)
        draw.line([(10, 50), (width - 10, 50)], fill=(139, 105, 20), width=2)
        
        # Rysuj każde pytanie jako kwadrat z liniami
        y_start = 70
        y_step = 100
        
        colors_q = {
            "q":  (238, 237, 254),  # Fiolet (pytanie)
            "a":  (230, 241, 251),  # Niebieski
            "b":  (225, 245, 238),  # Zielony
            "c":  (250, 238, 218),  # Brązowy
            "txt": (60, 52, 137),   # Tekst fiolet
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
            
            # Tekst pytania (skrócony)
            pytanie = krok.get("pytanie", f"P{i}")[:35]
            draw.text((x + 5, y + 5), f"P{i}: {pytanie[:30]}", fill=colors_q["txt"], font=font_small)
            
            # Opcje A, B, C (minilettery)
            draw.text((x + 5, y + 28), "A", fill=(24, 95, 165), font=font_med)
            draw.text((x + 35, y + 28), "B", fill=(15, 110, 86), font=font_med)
            draw.text((x + 65, y + 28), "C", fill=(133, 79, 11), font=font_med)
            
            # Liczba ścieżek (3 do następnego pytania lub końcu)
            next_level = len(kroki) - i
            if next_level > 0:
                opcje = krok.get("opcje", {})
                num_opcje = len(opcje)
                draw.text((x + 5, y + 50), f"→ {num_opcje} ścieżki", fill=(150, 100, 100), font=font_small)
            else:
                draw.text((x + 5, y + 50), "→ WYROK", fill=(139, 105, 20), font=font_small)
        
        # Dolna informacja
        draw.text((15, height - 30), "Mapa całej logiki gry. Aby grać aktywnie, otwórz HTML.", fill=(100, 100, 100), font=font_small)
        
        # Zapisz do bytes
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        jpg_bytes = buf.getvalue()
        
        _log(f"JPG fallback wygenerowany via PIL: {len(jpg_bytes)} bytes")
        return jpg_bytes
    
    except Exception as e:
        _log(f"Error PIL fallback: {e}")
        return None


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
    Generuje HTML z interaktywnym SVG diagramem drzewa decyzyjnego.
    Na bazie struktury z backup/edek_responder_flowchart.html
    """
    kroki = gra.get("kroki", [])
    wyrok = gra.get("wyrok", "Brak wyroku.")
    sn = sender_name or "Anonim"
    
    if not kroki:
        return "<p>Brak danych do diagramu.</p>"
    
    # Oblicz wymiary SVG
    num_kroki = len(kroki)
    svg_height = 100 + num_kroki * 300 + 200  # Przybliżenie
    svg_width = 2200
    
    # Buduj SVG
    svg_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg width="2200" viewBox="0 0 {svg_width} {svg_height}" xmlns="http://www.w3.org/2000/svg">',
        '<defs>',
        '  <marker id="arr-a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        '    <path d="M2 1L8 5L2 9" fill="none" stroke="#185FA5" stroke-width="1.5" stroke-linecap="round"/>',
        '  </marker>',
        '  <marker id="arr-b" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        '    <path d="M2 1L8 5L2 9" fill="none" stroke="#0F6E56" stroke-width="1.5" stroke-linecap="round"/>',
        '  </marker>',
        '  <marker id="arr-c" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        '    <path d="M2 1L8 5L2 9" fill="none" stroke="#854F0B" stroke-width="1.5" stroke-linecap="round"/>',
        '  </marker>',
        '  <marker id="arr-m" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        '    <path d="M2 1L8 5L2 9" fill="none" stroke="#888780" stroke-width="1.5" stroke-linecap="round"/>',
        '  </marker>',
        '</defs>',
        '<style>',
        '  .q-node rect { fill: #EEEDFE; stroke: #534AB7; stroke-width: 1; }',
        '  .q-node text { fill: #3C3489; font-size: 12px; font-weight: bold; }',
        '  .leaf-a rect { fill: #E6F1FB; stroke: #185FA5; stroke-width: 0.8; }',
        '  .leaf-a text { fill: #0C447C; font-size: 10px; }',
        '  .leaf-b rect { fill: #E1F5EE; stroke: #0F6E56; stroke-width: 0.8; }',
        '  .leaf-b text { fill: #085041; font-size: 10px; }',
        '  .leaf-c rect { fill: #FAEEDA; stroke: #854F0B; stroke-width: 0.8; }',
        '  .leaf-c text { fill: #633806; font-size: 10px; }',
        '  .arr-a { stroke: #185FA5; stroke-width: 1; fill: none; marker-end: url(#arr-a); }',
        '  .arr-b { stroke: #0F6E56; stroke-width: 1; fill: none; marker-end: url(#arr-b); }',
        '  .arr-c { stroke: #854F0B; stroke-width: 1; fill: none; marker-end: url(#arr-c); }',
        '  .arr-main { stroke: #888780; stroke-width: 1.2; fill: none; marker-end: url(#arr-m); }',
        '  .lbl { font-size: 10px; font-weight: bold; }',
        '  .lbl-a { fill: #185FA5; }',
        '  .lbl-b { fill: #0F6E56; }',
        '  .lbl-c { fill: #854F0B; }',
        '</style>',
    ]
    
    # Header
    svg_lines.append(
        f'<rect x="850" y="20" width="500" height="44" rx="14" fill="#2C2C2A"/>'
    )
    svg_lines.append(
        f'<text x="1100" y="47" text-anchor="middle" font-size="14" fill="#F1EFE8" '
        f'font-weight="bold" font-family="Arial">EDEK RESPONDER™ · Drzewo decyzyjne · {sn}</text>'
    )
    svg_lines.append('<line x1="1100" y1="64" x2="1100" y2="90" class="arr-main"/>')
    
    # Rysuj każde pytanie
    y_current = 90
    for i, krok in enumerate(kroki, 1):
        nr = krok.get("nr", i)
        pytanie = krok.get("pytanie", f"P{i}")[:50]
        intro = krok.get("intro", "")[:40]
        opcje = krok.get("opcje", {})
        
        # Node pytania
        svg_lines.append(f'<g class="q-node">')
        svg_lines.append(
            f'  <rect x="750" y="{y_current}" width="700" height="52" rx="8"/>'
        )
        svg_lines.append(
            f'  <text x="1100" y="{y_current + 22}" text-anchor="middle" font-family="Arial">{pytanie}</text>'
        )
        svg_lines.append(
            f'  <text x="1100" y="{y_current + 40}" text-anchor="middle" font-size="10" '
            f'fill="#7F77DD" font-family="Arial">{intro}</text>'
        )
        svg_lines.append('</g>')
        
        # Połączenia do opcji
        y_next = y_current + 118
        
        # Opcja A
        if "A" in opcje:
            tekst_a = opcje["A"].get("tekst", "A")[:20]
            svg_lines.append(f'<path d="M900 {y_current + 52} L900 {y_current + 85} L640 {y_current + 85} L640 {y_next}" class="arr-a"/>')
            svg_lines.append(
                f'<text x="760" y="{y_current + 80}" class="lbl lbl-a" text-anchor="middle" '
                f'font-family="Arial">A: {tekst_a}</text>'
            )
            svg_lines.append(
                f'<g class="leaf-a"><rect x="480" y="{y_next}" width="320" height="52" rx="6"/>'
                f'<text x="640" y="{y_next + 20}" text-anchor="middle" font-family="Arial">{tekst_a}</text>'
                f'</g>'
            )
        
        # Opcja B
        if "B" in opcje:
            tekst_b = opcje["B"].get("tekst", "B")[:20]
            svg_lines.append(f'<path d="M1100 {y_current + 52} L1100 {y_next}" class="arr-b"/>')
            svg_lines.append(
                f'<text x="1145" y="{y_current + 78}" class="lbl lbl-b" font-family="Arial">B: {tekst_b}</text>'
            )
            svg_lines.append(
                f'<g class="leaf-b"><rect x="940" y="{y_next}" width="320" height="52" rx="6"/>'
                f'<text x="1100" y="{y_next + 20}" text-anchor="middle" font-family="Arial">{tekst_b}</text>'
                f'</g>'
            )
        
        # Opcja C
        if "C" in opcje:
            tekst_c = opcje["C"].get("tekst", "C")[:20]
            svg_lines.append(f'<path d="M1300 {y_current + 52} L1300 {y_current + 85} L1560 {y_current + 85} L1560 {y_next}" class="arr-c"/>')
            svg_lines.append(
                f'<text x="1430" y="{y_current + 80}" class="lbl lbl-c" text-anchor="middle" '
                f'font-family="Arial">C: {tekst_c}</text>'
            )
            svg_lines.append(
                f'<g class="leaf-c"><rect x="1400" y="{y_next}" width="320" height="52" rx="6"/>'
                f'<text x="1560" y="{y_next + 20}" text-anchor="middle" font-family="Arial">{tekst_c}</text>'
                f'</g>'
            )
        
        # Połączenie do następnego pytania
        y_connect = y_next + 52
        svg_lines.append(
            f'<path d="M640 {y_connect} L640 {y_connect + 30} L1100 {y_connect + 30} L1100 {y_connect + 50}" class="arr-main"/>'
        )
        svg_lines.append(
            f'<path d="M1100 {y_connect} L1100 {y_connect + 50}" class="arr-main"/>'
        )
        svg_lines.append(
            f'<path d="M1560 {y_connect} L1560 {y_connect + 30} L1100 {y_connect + 30}" class="arr-main"/>'
        )
        
        y_current = y_connect + 60
    
    # Wyrok końcowy
    svg_lines.append(
        f'<g><rect x="850" y="{y_current}" width="500" height="60" rx="8" fill="#3C3489"/>'
        f'<text x="1100" y="{y_current + 22}" text-anchor="middle" font-size="12" fill="#F1EFE8" '
        f'font-weight="bold" font-family="Arial">⚖ WYROK KOŃCOWY</text>'
        f'<text x="1100" y="{y_current + 40}" text-anchor="middle" font-size="10" fill="#E8D5B0" '
        f'font-family="Arial">{wyrok[:100]}</text></g>'
    )
    
    svg_lines.append('</svg>')
    
    return "\n".join(svg_lines)
