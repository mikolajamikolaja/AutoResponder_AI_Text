"""
flux_mutator.py
───────────────
Tester mutacji promptów FLUX z użyciem spaCy.

Wymagania (uruchom raz przed startem):
    pip install PySide6 spacy
    python -m spacy download en_core_web_sm

Pliki w tym samym katalogu co skrypt:
    test.txt            — wklej tu tekst od Groqa (prompt FLUX)
    flux_mutations.txt  — słownik mutacji (format sekcji [KATEGORIA])

Wyniki zapisywane do: HHMMSS.txt
"""

import sys
import re
import random
import os
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QScrollArea, QFrame, QSplitter
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont

# ── spaCy ─────────────────────────────────────────────────────────────────────
try:
    import spacy
    NLP = spacy.load("en_core_web_sm")
    SPACY_OK = True
except Exception as e:
    NLP = None
    SPACY_OK = False
    SPACY_ERROR = str(e)

# ── Ścieżki plików ────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TEST_FILE       = os.path.join(BASE_DIR, "test.txt")
MUTATIONS_FILE  = os.path.join(BASE_DIR, "flux_mutations.txt")

# ── Tłumaczenia kategorii spaCy na polski ────────────────────────────────────
CATEGORY_PL = {
    "PERSON":      "osoba",
    "ORG":         "organizacja",
    "GPE":         "miejsce (kraj/miasto)",
    "LOC":         "lokacja",
    "PRODUCT":     "produkt",
    "EVENT":       "wydarzenie",
    "WORK_OF_ART": "dzieło sztuki",
    "FAC":         "obiekt/budynek",
    "NORP":        "narodowość/grupa",
    "ANIMAL":      "zwierzę",
    "OBJECT":      "obiekt (domyślny)",
    "NOUN":        "rzeczownik (bez kategorii NER)",
}

# ── Wczytaj mutacje z pliku ───────────────────────────────────────────────────
def load_mutations(path: str) -> dict:
    mutations = {}
    current = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1].upper()
                    mutations[current] = []
                elif current:
                    items = [s.strip() for s in line.split(",") if s.strip()]
                    mutations[current].extend(items)
    except FileNotFoundError:
        pass
    return mutations

# ── Mapowanie kategorii NER → klucz w słowniku mutacji ───────────────────────
def ner_to_mutation_key(ent_type: str) -> str:
    mapping = {
        "PERSON":      "PERSON",
        "ORG":         "GROUP",
        "GPE":         "PLACE",
        "LOC":         "PLACE",
        "FAC":         "PLACE",
        "PRODUCT":     "OBJECT",
        "EVENT":       "OBJECT",
        "WORK_OF_ART": "OBJECT",
        "NORP":        "GROUP",
    }
    return mapping.get(ent_type, "OBJECT")

# ── Główna funkcja mutacji ────────────────────────────────────────────────────
def mutate_prompt(text: str, mutations: dict) -> tuple:
    """
    Zwraca (zmutowany_tekst, lista_zmian)
    lista_zmian = [ (oryginalne_słowo, kategoria_EN, kategoria_PL, sufiks, wynik) ]
    """
    if not SPACY_OK or NLP is None:
        return text, []

    doc = NLP(text)
    changes = []
    result = text

    # Zbierz encje nazwane (mają priorytet)
    ent_map = {ent.text: ent.label_ for ent in doc.ents}

    # Zbierz wszystkie tokeny będące rzeczownikami
    processed = set()
    tokens_to_mutate = []

    for token in doc:
        if token.text in processed:
            continue
        if token.pos_ in ("NOUN", "PROPN"):
            if token.text in ent_map:
                ner_label = ent_map[token.text]
                mut_key   = ner_to_mutation_key(ner_label)
                display   = ner_label
            else:
                mut_key = "OBJECT"
                display = "NOUN"
            tokens_to_mutate.append((token.text, display, mut_key))
            processed.add(token.text)

    # Zastosuj mutacje
    for word, display_cat, mut_key in tokens_to_mutate:
        suffixes = mutations.get(mut_key) or mutations.get("OBJECT", [])
        if not suffixes:
            continue
        sufiks = random.choice(suffixes)
        mutated = f"{word}-{sufiks}"
        # Zamień tylko całe słowo (nie w środku innych słów)
        result = re.sub(rf'\b{re.escape(word)}\b', mutated, result, count=1)
        cat_pl = CATEGORY_PL.get(display_cat, display_cat)
        changes.append((word, display_cat, cat_pl, sufiks, mutated))

    return result, changes

# ── Proste tłumaczenie przez słownik (bez API) ────────────────────────────────
SIMPLE_DICT = {
    "diptych": "dyptyk", "panel": "panel", "left": "lewy", "right": "prawy",
    "candid": "z ukrycia", "photograph": "zdjęcie", "leaked": "ujawnione",
    "geometry": "geometria", "proportion": "proporcja", "fractal": "fraktal",
    "plasma": "plazma", "membrane": "błona", "entity": "istota",
    "entities": "istoty", "construct": "konstrukt", "formation": "formacja",
    "compressed": "skompresowany", "crystallized": "skrystalizowany",
    "frozen": "zamrożony", "inverted": "odwrócony", "asymmetric": "asymetryczny",
    "bioluminescent": "bioluminescencyjny", "platinum": "platynowy",
    "hyper-real": "hiperrealistyczny", "surrealism": "surrealizm",
    "alien": "obcy", "dimension": "wymiar", "world": "świat",
    "sorrow": "smutek", "grief": "żałoba", "emotion": "emocja",
    "messenger": "wysłannik", "crew": "ekipa", "realm": "kraina",
    "frequency": "częstotliwość", "resonant": "rezonujący",
    "gravitational": "grawitacyjny", "tessellating": "teselujący",
    "non-humanoid": "nieludzki", "geometric": "geometryczny",
}

def simple_translate(text: str) -> str:
    result = text
    for en, pl in SIMPLE_DICT.items():
        result = re.sub(rf'\b{re.escape(en)}\b', pl, result, flags=re.IGNORECASE)
    result += "\n\n[Tłumaczenie przybliżone — słownik wbudowany]"
    return result

# ── Wątek roboczy ─────────────────────────────────────────────────────────────
class WorkerThread(QThread):
    done = Signal(str, str, list, str)  # mutated, translated, changes, error

    def __init__(self, text: str, mutations: dict):
        super().__init__()
        self.text      = text
        self.mutations = mutations

    def run(self):
        try:
            mutated, changes = mutate_prompt(self.text, self.mutations)
            translated       = simple_translate(mutated)
            self.done.emit(mutated, translated, changes, "")
        except Exception as e:
            self.done.emit("", "", [], str(e))

# ── Styl ──────────────────────────────────────────────────────────────────────
STYLE = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Consolas', 'Courier New', monospace;
}
QTextEdit {
    background-color: #16213e;
    color: #e0e0e0;
    border: 1px solid #0f3460;
    border-radius: 4px;
    padding: 6px;
    font-size: 12px;
}
QPushButton {
    background-color: #0f3460;
    color: #e0e0e0;
    border: none;
    border-radius: 4px;
    padding: 8px 20px;
    font-size: 13px;
    font-weight: bold;
}
QPushButton:hover  { background-color: #e94560; }
QPushButton:pressed { background-color: #c73652; }
QLabel#header {
    color: #e94560;
    font-size: 13px;
    font-weight: bold;
    padding: 2px 0;
}
QLabel#status {
    color: #a0a0c0;
    font-size: 11px;
}
QScrollArea { border: none; }
QSplitter::handle { background-color: #0f3460; width: 2px; }
"""

# ── Główne okno ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FLUX Mutator — spaCy + flux_mutations.txt")
        self.setMinimumSize(1200, 700)
        self.mutations = load_mutations(MUTATIONS_FILE)
        self._build_ui()
        self._load_test_file()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Pasek górny ──
        top = QHBoxLayout()
        title = QLabel("FLUX Mutator")
        title.setObjectName("header")
        title.setFont(QFont("Consolas", 15, QFont.Bold))

        self.status_lbl = QLabel("gotowy")
        self.status_lbl.setObjectName("status")

        self.btn_start  = QPushButton("▶  START")
        self.btn_reload = QPushButton("↺  Przeładuj test.txt")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_reload.clicked.connect(self._load_test_file)

        top.addWidget(title)
        top.addStretch()
        top.addWidget(self.status_lbl)
        top.addWidget(self.btn_reload)
        top.addWidget(self.btn_start)
        root.addLayout(top)

        # ── spaCy info ──
        spacy_info = QLabel(
            f"✓ spaCy: en_core_web_sm załadowany" if SPACY_OK
            else f"✗ spaCy niedostępny: {SPACY_ERROR if not SPACY_OK else ''} — uruchom: pip install spacy && python -m spacy download en_core_web_sm"
        )
        spacy_info.setObjectName("status")
        spacy_info.setStyleSheet(
            "color: #50fa7b;" if SPACY_OK else "color: #e94560;"
        )
        root.addWidget(spacy_info)

        # ── Splitter z 4 panelami ──
        splitter = QSplitter(Qt.Horizontal)

        self.panel_input    = self._make_panel("① TEKST WEJŚCIOWY (test.txt)", editable=True)
        self.panel_mutated  = self._make_panel("② PROMPT PO MUTACJI (angielski)")
        self.panel_polish   = self._make_panel("③ TŁUMACZENIE (polski)")
        self.panel_changes  = self._make_panel("④ ROZBUDOWANE SŁOWA")

        splitter.addWidget(self._wrap(self.panel_input,   "① WEJŚCIE"))
        splitter.addWidget(self._wrap(self.panel_mutated, "② ZMUTOWANY PROMPT"))
        splitter.addWidget(self._wrap(self.panel_polish,  "③ PO POLSKU"))
        splitter.addWidget(self._wrap(self.panel_changes, "④ ZMIANY spaCy"))

        splitter.setSizes([300, 300, 300, 300])
        root.addWidget(splitter, stretch=1)

    def _make_panel(self, label: str, editable: bool = False) -> QTextEdit:
        t = QTextEdit()
        t.setReadOnly(not editable)
        t.setLineWrapMode(QTextEdit.WidgetWidth)
        return t

    def _wrap(self, widget: QTextEdit, title: str) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lbl = QLabel(title)
        lbl.setObjectName("header")
        lay.addWidget(lbl)
        lay.addWidget(widget)
        return w

    def _load_test_file(self):
        try:
            with open(TEST_FILE, encoding="utf-8") as f:
                self.panel_input.setPlainText(f.read())
            self.status_lbl.setText(f"wczytano: test.txt")
        except FileNotFoundError:
            self.panel_input.setPlainText("")
            self.status_lbl.setText("BRAK test.txt — utwórz plik w katalogu skryptu")

    def _on_start(self):
        text = self.panel_input.toPlainText().strip()
        if not text:
            self.status_lbl.setText("test.txt jest pusty!")
            return
        if not self.mutations:
            self.status_lbl.setText("BRAK flux_mutations.txt lub pusty!")
            return

        self.btn_start.setEnabled(False)
        self.status_lbl.setText("przetwarzam…")
        self.panel_mutated.clear()
        self.panel_polish.clear()
        self.panel_changes.clear()

        self.worker = WorkerThread(text, self.mutations)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def _on_done(self, mutated: str, translated: str, changes: list, error: str):
        self.btn_start.setEnabled(True)

        if error:
            self.status_lbl.setText(f"błąd: {error}")
            return

        self.panel_mutated.setPlainText(mutated)
        self.panel_polish.setPlainText(translated)

        # Panel zmian
        if changes:
            lines = []
            for orig, cat_en, cat_pl, sufiks, wynik in changes:
                lines.append(
                    f"  {orig}\n"
                    f"  kategoria : {cat_en} ({cat_pl})\n"
                    f"  sufiks    : -{sufiks}\n"
                    f"  wynik     : {wynik}\n"
                    f"  {'─'*40}"
                )
            self.panel_changes.setPlainText("\n".join(lines))
        else:
            self.panel_changes.setPlainText(
                "Brak zmian — spaCy nie wykrył rzeczowników\n"
                "lub flux_mutations.txt jest pusty."
            )

        # Zapis do pliku HHMMSS.txt
        ts       = datetime.now().strftime("%H%M%S")
        out_path = os.path.join(BASE_DIR, f"{ts}.txt")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("=== TEKST WEJŚCIOWY ===\n")
                f.write(self.panel_input.toPlainText())
                f.write("\n\n=== PROMPT PO MUTACJI ===\n")
                f.write(mutated)
                f.write("\n\n=== TŁUMACZENIE ===\n")
                f.write(translated)
                f.write("\n\n=== ROZBUDOWANE SŁOWA ===\n")
                for orig, cat_en, cat_pl, sufiks, wynik in changes:
                    f.write(f"{orig} [{cat_en}] → {wynik}\n")
            self.status_lbl.setText(f"zapisano: {ts}.txt  |  zmian: {len(changes)}")
        except Exception as e:
            self.status_lbl.setText(f"błąd zapisu: {e}")


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
