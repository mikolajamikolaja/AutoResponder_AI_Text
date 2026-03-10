"""
requiem_kalibracja.py  v3
Lokalne narzędzie do testowania promptów Requiem Autorespondera.
Umieść w katalogu: C:\\python\\...\\AutoResponder_AI_Text\\prompts\\

Podział API:
  DeepSeek (DEEPSEEK_API_KEY) → tekst emaila Wysłannika
  Groq     (GROQ_API_KEY)     → kreatywny prompt FLUX
  Fallback: jeśli jeden zawodzi → drugi
"""

import os
import re
import shutil
import threading
import datetime
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

# ── Ścieżki ───────────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
PROJECT_DIR    = SCRIPT_DIR.parent
BACKUP_DIR     = PROJECT_DIR / "backup"
RESPONDERS_DIR = PROJECT_DIR / "responders"
SMIERC_PY      = RESPONDERS_DIR / "smierc.py"

FILE_PAWEL_1_6        = SCRIPT_DIR / "requiem_PAWEL_system_1-6.txt"
FILE_PAWEL_7          = SCRIPT_DIR / "requiem_PAWEL_system_7.txt"
FILE_WYSLANNIK        = SCRIPT_DIR / "requiem_WYSLANNIK_system_8_.txt"
FILE_FLUX_GROQ_SYS    = SCRIPT_DIR / "requiem_WYSLANNIK_flux_groq_system.txt"
FILE_IMAGE_STYLE      = SCRIPT_DIR / "requiem_WYSLANNIK_IMAGE_STYLE.txt"
FILE_POZAGROBOWE      = SCRIPT_DIR / "pozagrobowe.txt"

TODAY = datetime.date.today().strftime("%d.%m.%Y")

# ── Paleta ────────────────────────────────────────────────────────────────────
BG      = "#0d0d0d"
BG2     = "#161616"
BG3     = "#1e1e1e"
ACCENT  = "#c8a96e"
ACCENT2 = "#8b5e3c"
FG      = "#e8e0d0"
FG2     = "#a09080"
FG3     = "#6a5a4a"
BTN_BG  = "#2a1f14"
SUCCESS = "#4a7c59"
ERR     = "#7c3a3a"
GREEN   = "#2d6b3a"
GREEN_H = "#3d8f4e"
BORDER  = "#3a2e22"

FONT_MONO  = ("Consolas", 10)
FONT_BTN   = ("Georgia", 9)
FONT_TITLE = ("Georgia", 12, "bold")
FONT_FILE  = ("Consolas", 8)
FONT_LBL   = ("Georgia", 9, "italic")


# ── Helpers ───────────────────────────────────────────────────────────────────
def load_txt(path: Path, fallback="") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return fallback

def load_etapy() -> dict:
    etapy = {}
    try:
        for line in FILE_POZAGROBOWE.read_text(encoding="utf-8").splitlines():
            m = re.match(r'^(\d+)\.\s+(.+)$', line.strip())
            if m:
                etapy[int(m.group(1))] = m.group(2).strip()
    except Exception:
        pass
    return etapy


# ── API calls ─────────────────────────────────────────────────────────────────
def call_deepseek(system: str, user: str) -> str | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": system},
                      {"role": "user",   "content": user}],
            max_tokens=800, temperature=0.85,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return None

def call_groq(system: str, user: str, max_tokens: int = 300) -> str | None:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key,
                        base_url="https://api.groq.com/openai/v1")
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system},
                      {"role": "user",   "content": user}],
            max_tokens=max_tokens, temperature=0.95,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return None

def call_llm_email(system: str, user: str) -> tuple[str | None, str]:
    """DeepSeek → tekst emaila. Fallback: Groq."""
    r = call_deepseek(system, user)
    if r:
        return r, "DeepSeek"
    r = call_groq(system, user, max_tokens=800)
    if r:
        return r, "Groq (fallback)"
    return None, "brak"

def call_llm_flux(system: str, user: str) -> tuple[str | None, str]:
    """Groq → prompt FLUX. Fallback: DeepSeek."""
    r = call_groq(system, user, max_tokens=300)
    if r:
        return r, "Groq"
    r = call_deepseek(system, user)
    if r:
        return r, "DeepSeek (fallback)"
    return None, "brak"

def generate_flux_image(prompt: str):
    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if not hf_token:
        return None, "Brak HF_TOKEN w zmiennych środowiskowych"
    try:
        import requests
        url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {hf_token}", "Accept": "image/png"},
            json={"inputs": prompt,
                  "parameters": {"num_inference_steps": 5, "guidance_scale": 5}},
            timeout=60
        )
        if resp.status_code == 200:
            return resp.content, None
        return None, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return None, str(e)


# ── Backup ────────────────────────────────────────────────────────────────────
def save_backup(image_bytes, flux_prompt, flux_provider,
                wyslannik_text, email_provider, body_text):
    ts       = datetime.datetime.now().strftime("%H_%M_%S")
    run_dir  = BACKUP_DIR / f"Wyniki_{ts}"
    prom_dir = run_dir / "prompts"
    prom_dir.mkdir(parents=True, exist_ok=True)

    if image_bytes:
        (run_dir / "niebo_wyslannik.png").write_bytes(image_bytes)

    # tekst_zrodlowy.txt — wiadomość nadawcy użyta do testów
    if body_text:
        (run_dir / "tekst_zrodlowy.txt").write_text(body_text, encoding="utf-8")

    debug = (
        f"=== REQUIEM RESPONDER — DEBUG FLUX ===\n"
        f"Wygenerowano: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Email provider: {email_provider}\n"
        f"FLUX prompt provider: {flux_provider}\n\n"
        f"--- Odpowiedź Wysłannika (źródło promptu FLUX) ---\n"
        f"{wyslannik_text}\n\n"
        f"--- Proponowany tekst wysłany do FLUX.1-schnell ---\n"
        f"{flux_prompt}\n\n"
        f"--- Parametry FLUX ---\n"
        f"Model: FLUX.1-schnell\n"
        f"num_inference_steps: 5\n"
        f"guidance_scale: 5\n"
    )
    (run_dir / "_.txt").write_text(debug, encoding="utf-8")

    for f in SCRIPT_DIR.glob("*.txt"):
        shutil.copy2(f, prom_dir / f.name)
    if SMIERC_PY.exists():
        shutil.copy2(SMIERC_PY, prom_dir / "smierc.txt")

    return run_dir


# ── GUI ───────────────────────────────────────────────────────────────────────
class RequiemApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("✦ REQUIEM KALIBRACJA v3 ✦")
        self.root.configure(bg=BG)
        self.root.geometry("880x1050")
        self.root.minsize(700, 600)

        self.etap_var = tk.IntVar(value=1)
        self.etapy    = load_etapy()
        self.max_etap = max(self.etapy.keys()) if self.etapy else 7

        self._last_img_bytes    = None
        self._last_flux         = ""
        self._last_flux_prov    = ""
        self._last_email_prov   = ""
        self._last_wyslannik    = ""

        self._build_ui()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar",
                        background=BG3, troughcolor=BG, arrowcolor=ACCENT,
                        bordercolor=BORDER, lightcolor=BG3, darkcolor=BG3)

        outer  = tk.Frame(self.root, bg=BG)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                               style="Vertical.TScrollbar")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.mf = tk.Frame(canvas, bg=BG)
        cw = canvas.create_window((0, 0), window=self.mf, anchor="nw")
        self.mf.bind("<Configure>",
                     lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(cw, width=e.width))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        f = self.mf

        # ── Nagłówek ──────────────────────────────────────────────────────────
        tk.Label(f, text="✦  REQUIEM  KALIBRACJA  ✦",
                 font=("Georgia", 16, "bold"), fg=ACCENT, bg=BG).pack(pady=(18, 2))
        tk.Label(f,
                 text="DeepSeek → email Wysłannika  |  Groq → prompt FLUX  |  fallback wzajemny",
                 font=("Georgia", 9, "italic"), fg=FG2, bg=BG).pack(pady=(0, 4))

        # Status kluczy API
        self.api_status = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.api_status.pack(pady=(0, 8))
        self._check_api_keys()
        self._sep(f)

        # ── Ustawienia ────────────────────────────────────────────────────────
        cfg = tk.Frame(f, bg=BG2, highlightbackground=BORDER, highlightthickness=1)
        cfg.pack(fill=tk.X, padx=20, pady=5)
        tk.Label(cfg, text="ETAP PAWŁA (1–6)", font=FONT_FILE,
                 fg=FG3, bg=BG2).grid(row=0, column=0, sticky="w", padx=12, pady=(10,10))
        tk.Spinbox(cfg, from_=1, to=self.max_etap, textvariable=self.etap_var,
                   width=4, font=FONT_MONO, bg=BG3, fg=ACCENT,
                   buttonbackground=BTN_BG, insertbackground=ACCENT,
                   highlightthickness=0, bd=0
                   ).grid(row=0, column=1, padx=10, pady=(10,10), sticky="w")
        tk.Label(cfg, text=f"data śmierci: {TODAY}", font=FONT_FILE,
                 fg=FG3, bg=BG2).grid(row=0, column=2, padx=10, pady=(10,10), sticky="w")
        self._sep(f)

        # ── ① Wiadomość ───────────────────────────────────────────────────────
        self._title(f, "①  WIADOMOŚĆ NADAWCY")
        self.body_text = self._textbox(f, h=6, ro=False)
        self.body_text.insert("1.0", "Wpisz tutaj przykładową wiadomość od nadawcy...")
        self.body_text.bind("<FocusIn>", self._clear_ph)
        self._sep(f)

        # ── ② Paweł 1-6 ───────────────────────────────────────────────────────
        self._title(f, "②  ETAP 1–6 — Paweł z zaświatów")
        self._badge(f, FILE_PAWEL_1_6.name)
        self.res_pawel = self._textbox(f, h=5)
        self._btn(f, f"▶  Generuj z pliku: {FILE_PAWEL_1_6.name}",
                  self._gen_pawel_1_6)
        self._sep(f)

        # ── ③ Paweł 7 ─────────────────────────────────────────────────────────
        self._title(f, "③  ETAP 7 — Reinkarnacja / pożegnanie")
        self._badge(f, FILE_PAWEL_7.name)
        self.res_pawel7 = self._textbox(f, h=5)
        self._btn(f, f"▶  Generuj z pliku: {FILE_PAWEL_7.name}",
                  self._gen_pawel_7)
        self._sep(f)

        # ── ④ Wysłannik ───────────────────────────────────────────────────────
        self._title(f, "④  ETAP 8+ — Wysłannik z wyższych sfer")
        self._badge(f, FILE_WYSLANNIK.name)
        tk.Label(f, text="  Provider: DeepSeek → email  (fallback: Groq)",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20)
        self.res_wyslannik = self._textbox(f, h=6)
        self.wyslannik_prov = tk.Label(f, text="", font=FONT_FILE, fg=FG3, bg=BG)
        self.wyslannik_prov.pack(anchor="w", padx=20, pady=(0,4))
        self._btn(f, f"▶  Generuj z pliku: {FILE_WYSLANNIK.name}",
                  self._gen_wyslannik)
        self._sep(f)

        # ── ⑤ Prompt FLUX ────────────────────────────────────────────────────
        self._title(f, "⑤  PROPONOWANY TEKST DO FLUX.1-schnell")
        self._badge(f, FILE_FLUX_GROQ_SYS.name)
        tk.Label(f, text="  Provider: Groq → prompt FLUX  (fallback: DeepSeek)",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20)
        tk.Label(f, text="Proponowany tekst do wysłania do FLUX.1-schnell:",
                 font=FONT_FILE, fg=FG3, bg=BG).pack(anchor="w", padx=20, pady=(8,0))
        self.flux_text = self._textbox(f, h=4)
        self.flux_status = tk.Label(f, text="", font=FONT_LBL, fg=FG2, bg=BG)
        self.flux_status.pack(anchor="w", padx=20, pady=2)
        self._btn(f,
            f"▶  Generuj tekst proponowany do promptu do wysłania do FLUX.1-schnell",
            self._gen_flux_tekst)
        self._sep(f)

        # ── ⑥ Obrazek ────────────────────────────────────────────────────────
        self._title(f, "⑥  GENERUJ OBRAZEK")
        self.img_status = tk.Label(f,
            text="Najpierw wygeneruj tekst proponowany (krok ⑤)",
            font=FONT_LBL, fg=FG3, bg=BG)
        self.img_status.pack(anchor="w", padx=20, pady=4)
        self.btn_obrazek = self._btn(f,
            "🟢  Generuj obrazek  (FLUX.1-schnell)",
            self._gen_obrazek, green=True, state=tk.DISABLED)
        self._sep(f)

        # ── ⑦ Backup ─────────────────────────────────────────────────────────
        self._title(f, "⑦  ZAPISZ WYNIKI  +  WYCZYŚĆ EKRAN")
        tk.Label(f,
            text="Zapisuje: niebo_wyslannik.png  •  _.txt  •  prompts/ (txt + smierc.txt)\n"
                 "Po zapisaniu czyści wszystkie wyniki — gotowe do kolejnego testu.",
            font=FONT_FILE, fg=FG2, bg=BG, justify=tk.LEFT
        ).pack(anchor="w", padx=20, pady=(0,4))
        self.btn_backup = self._btn(f,
            "💾  Zapisz do backup/Wyniki_HH_MM_SS  i wyczyść ekran",
            self._save_backup, state=tk.DISABLED)

        tk.Frame(f, bg=BG, height=40).pack()

    # ── Widget helpers ────────────────────────────────────────────────────────
    def _check_api_keys(self):
        ds = "✓ DeepSeek" if os.environ.get("DEEPSEEK_API_KEY") else "✗ DeepSeek (brak klucza)"
        gr = "✓ Groq" if os.environ.get("GROQ_API_KEY") else "✗ Groq (brak klucza)"
        hf = "✓ HF_TOKEN" if os.environ.get("HF_TOKEN") else "✗ HF_TOKEN (brak)"
        color = SUCCESS if all(os.environ.get(k) for k in
                               ["DEEPSEEK_API_KEY","GROQ_API_KEY","HF_TOKEN"]) else ERR
        self.api_status.configure(
            text=f"{ds}   {gr}   {hf}", fg=color)

    def _sep(self, p):
        tk.Frame(p, bg=BORDER, height=1).pack(fill=tk.X, padx=20, pady=8)

    def _title(self, p, text):
        tk.Label(p, text=text, font=FONT_TITLE,
                 fg=ACCENT, bg=BG).pack(anchor="w", padx=20, pady=(4, 2))

    def _badge(self, p, name):
        tk.Label(p, text=f"  📄 {name}", font=FONT_FILE,
                 fg=FG3, bg=BG).pack(anchor="w", padx=20, pady=(0, 2))

    def _textbox(self, parent, h=4, ro=True) -> tk.Text:
        frame = tk.Frame(parent, highlightbackground=BORDER,
                         highlightthickness=1, bg=BORDER)
        frame.pack(fill=tk.X, padx=20, pady=4)
        txt = tk.Text(frame, height=h, wrap=tk.WORD, font=FONT_MONO,
                      bg=BG3, fg=FG, insertbackground=ACCENT,
                      selectbackground=ACCENT2, selectforeground=FG,
                      relief=tk.FLAT, bd=0, padx=10, pady=8,
                      state=tk.DISABLED if ro else tk.NORMAL)
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview,
                           style="Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        def _resize(e=None):
            lines = int(txt.index("end-1c").split(".")[0])
            txt.configure(height=max(h, min(lines + 1, 32)))
        txt.bind("<Configure>", _resize)
        return txt

    def _btn(self, parent, label, cmd, green=False, state=tk.NORMAL):
        bg = GREEN if green else BTN_BG
        fg = "#ffffff" if green else ACCENT
        hv = GREEN_H if green else ACCENT2
        b = tk.Button(parent, text=label, command=cmd,
                      font=FONT_BTN, fg=fg, bg=bg,
                      activebackground=hv,
                      activeforeground="#ffffff" if green else BG,
                      relief=tk.FLAT, bd=0, pady=9, padx=16,
                      cursor="hand2", state=state)
        b.pack(fill=tk.X, padx=20, pady=(4, 8))
        b.bind("<Enter>", lambda e: b.configure(bg=hv))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def _clear_ph(self, e):
        if self.body_text.get("1.0", tk.END).strip() == \
                "Wpisz tutaj przykładową wiadomość od nadawcy...":
            self.body_text.delete("1.0", tk.END)

    def _set(self, w: tk.Text, text: str):
        w.configure(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        w.insert("1.0", text)
        lines = text.count("\n") + 1
        w.configure(height=max(4, min(lines + 2, 32)), state=tk.DISABLED)

    def _loading(self, w: tk.Text):
        self._set(w, "⏳ generuję...")

    def _get_body(self) -> str:
        b = self.body_text.get("1.0", tk.END).strip()
        return "" if b == "Wpisz tutaj przykładową wiadomość od nadawcy..." else b

    def _get_wyslannik(self) -> str:
        return self.res_wyslannik.get("1.0", tk.END).strip()

    # ── Generatory ────────────────────────────────────────────────────────────
    def _gen_pawel_1_6(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomości", "Wpisz wiadomość nadawcy.")
            return
        self._loading(self.res_pawel)
        etap_tresc = self.etapy.get(self.etap_var.get(), "Podróż trwa")

        def _run():
            tmpl = load_txt(FILE_PAWEL_1_6,
                "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
                "Piszesz po polsku. Ton: spokojny, lekko absurdalny, z humorem. "
                "Odpowiedź max 5 zdań. Podpisz się: — Autoresponder Pawła-zza-światów. "
                "Koniecznie wspomnij że umarłeś na suchoty dnia {data_smierci_str}. "
                "Nawiąż do wiadomości paradoksalnie chwaląc Ziemię. "
                "Opisz swój aktualny etap. Nie wspominaj Księgi Urantii.")
            system = tmpl.replace("{data_smierci_str}", TODAY)
            result, prov = call_llm_email(system,
                f"Etap w zaświatach: {etap_tresc}\nWiadomość: {body}")
            self.root.after(0, lambda: self._set(self.res_pawel,
                result or f"[Błąd — brak odpowiedzi z {prov}]"))
        threading.Thread(target=_run, daemon=True).start()

    def _gen_pawel_7(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomości", "Wpisz wiadomość nadawcy.")
            return
        self._loading(self.res_pawel7)
        etap_tresc = self.etapy.get(self.max_etap, "Reinkarnacja nadchodzi nieuchronnie")

        def _run():
            tmpl = load_txt(FILE_PAWEL_7,
                "Jesteś Pawłem — zmarłym mężczyzną piszącym z zaświatów. "
                "Ton: spokojny, wzruszający, tajemniczy. Odpowiedź max 5 zdań. "
                "Umarłem na suchoty dnia {data_smierci_str}. "
                "Poinformuj że nadchodzi reinkarnacja. Pożegnaj się ciepło.")
            system = tmpl.replace("{data_smierci_str}", TODAY)
            result, prov = call_llm_email(system,
                f"Etap: {etap_tresc}\nWiadomość: {body}")
            self.root.after(0, lambda: self._set(self.res_pawel7,
                result or f"[Błąd — brak odpowiedzi z {prov}]"))
        threading.Thread(target=_run, daemon=True).start()

    def _gen_wyslannik(self):
        body = self._get_body()
        if not body:
            messagebox.showwarning("Brak wiadomości", "Wpisz wiadomość nadawcy.")
            return
        self._loading(self.res_wyslannik)
        self.wyslannik_prov.configure(text="", fg=FG3)

        def _run():
            system = load_txt(FILE_WYSLANNIK,
                "Jesteś wysłannikiem z wyższych sfer duchowych. "
                "Przebijasz każdą rzecz wymienioną przez nadawcę — TYLKO przymiotnikami, "
                "nigdy liczbami. Ton: dostojny, poetycki, lekko absurdalny. Max 4 zdania. "
                "Podpisz się: — Wysłannik z wyższych sfer")
            result, prov = call_llm_email(system, f"Osoba pyta: {body}")
            self._last_wyslannik  = result or ""
            self._last_email_prov = prov
            self.root.after(0, lambda: (
                self._set(self.res_wyslannik, result or "[Błąd API]"),
                self.wyslannik_prov.configure(
                    text=f"  ↳ wygenerowano przez: {prov}", fg=FG3)
            ))
        threading.Thread(target=_run, daemon=True).start()

    def _gen_flux_tekst(self):
        wyslannik = self._get_wyslannik()
        if not wyslannik or wyslannik in ("⏳ generuję...", "[Błąd API]"):
            messagebox.showwarning("Brak tekstu Wysłannika",
                "Najpierw wygeneruj odpowiedź Wysłannika (krok ④).")
            return

        self.flux_status.configure(
            text="⏳ Groq generuje kreatywny prompt FLUX...", fg=FG2)
        self._set(self.flux_text, "...")
        self.btn_obrazek.configure(state=tk.DISABLED)
        self.btn_backup.configure(state=tk.DISABLED)

        def _run():
            system = load_txt(FILE_FLUX_GROQ_SYS,
                "You are a creative prompt engineer for FLUX image generator. "
                "Based on the Polish heavenly messenger text, write a surreal, "
                "otherworldly image prompt in English (max 80 words). "
                "Invent bizarre celestial creatures inspired by the content. "
                "NOT photorealistic. End with: divine surreal digital art, "
                "otherworldly paradise, vivid colors. Return ONLY the prompt.")
            user = (f"Generate a FLUX image prompt based on this heavenly "
                    f"messenger text:\n\n{wyslannik}")
            result, prov = call_llm_flux(system, user)

            if not result:
                # Ostatni fallback: statyczny styl
                result = load_txt(FILE_IMAGE_STYLE,
                    "surreal heavenly paradise, divine golden light, "
                    "celestial beings, otherworldly atmosphere, vivid colors, digital art")
                prov = "statyczny fallback (oba API zawiodły)"

            self._last_flux      = result
            self._last_flux_prov = prov
            self._last_wyslannik = wyslannik

            self.root.after(0, lambda: (
                self._set(self.flux_text, result),
                self.flux_status.configure(
                    text=f"✓ Tekst gotowy — wygenerowano przez: {prov}",
                    fg=SUCCESS),
                self.btn_obrazek.configure(state=tk.NORMAL)
            ))
        threading.Thread(target=_run, daemon=True).start()

    def _gen_obrazek(self):
        if not self._last_flux:
            messagebox.showwarning("Brak promptu",
                "Najpierw wygeneruj tekst proponowany (krok ⑤).")
            return
        self.img_status.configure(
            text="⏳ Generuję obrazek FLUX.1-schnell (może potrwać ~30s)...", fg=FG2)
        self.btn_obrazek.configure(state=tk.DISABLED)

        def _run():
            img_bytes, err = generate_flux_image(self._last_flux)
            self._last_img_bytes = img_bytes
            if img_bytes:
                self.root.after(0, lambda: (
                    self.img_status.configure(
                        text=f"✓ Obrazek gotowy ({len(img_bytes):,} B) — zapisz backup (krok ⑦)",
                        fg=SUCCESS),
                    self.btn_backup.configure(state=tk.NORMAL)
                ))
            else:
                self.root.after(0, lambda: (
                    self.img_status.configure(text=f"✗ Błąd FLUX: {err}", fg=ERR),
                    self.btn_obrazek.configure(state=tk.NORMAL)
                ))
        threading.Thread(target=_run, daemon=True).start()

    def _save_backup(self):
        try:
            body = self._get_body()
            run_dir = save_backup(
                self._last_img_bytes,
                self._last_flux,
                self._last_flux_prov,
                self._last_wyslannik,
                self._last_email_prov,
                body
            )
            messagebox.showinfo("✓ Backup zapisany",
                f"Zapisano do:\n{run_dir}\n\n"
                f"  niebo_wyslannik.png\n"
                f"  _.txt\n"
                f"  tekst_zrodlowy.txt\n"
                f"  prompts/ (wszystkie .txt + smierc.txt)")

            for w in [self.res_pawel, self.res_pawel7, self.res_wyslannik, self.flux_text]:
                self._set(w, "")
            self.flux_status.configure(text="", fg=FG2)
            self.img_status.configure(
                text="✓ Zapisano. Ekran wyczyszczony — gotowe do kolejnego testu.",
                fg=SUCCESS)
            self.wyslannik_prov.configure(text="", fg=FG3)
            self.btn_obrazek.configure(state=tk.DISABLED)
            self.btn_backup.configure(state=tk.DISABLED)
            self._last_img_bytes = None
            self._last_flux      = ""
            self._last_flux_prov = ""
            self._last_wyslannik = ""

        except Exception as e:
            messagebox.showerror("Błąd zapisu", str(e))


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = RequiemApp(root)
    root.mainloop()
