"""
Interfaccia grafica minimale per checker.py: campi Codice Fiscale / NRE / Telegram,
pulsante Avvia/Ferma, icona nella system tray (chiudere la finestra la nasconde,
non termina il programma; per uscire davvero usa "Esci" dal menu della tray icon).

Uso:
    python gui.py
"""

import json
import queue
import random
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

import requests
from PIL import Image, ImageDraw
import pystray
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import checker

CONFIG_PATH = checker.CONFIG_PATH

ui_queue: queue.Queue = queue.Queue()
stop_event = threading.Event()
running = False
worker_thread = None


def load_saved_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- worker ----

def worker_loop(config: dict, debug_visible: bool = False) -> None:
    ui_queue.put(("status", "Avvio browser..."))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not debug_visible, slow_mo=350 if debug_visible else 0)
            context = checker.new_stealthy_context(browser)
            page = context.new_page()  # stessa scheda riusata ad ogni giro, come farebbe una persona
            try:
                while not stop_event.is_set():
                    ts = datetime.now().strftime("%H:%M:%S")
                    try:
                        status, message, shot = checker.check_once(page, config)
                        ui_queue.put(("log", f"[{ts}] {status}: {message}"))

                        if status == "AVAILABLE":
                            checker.telegram_notify(
                                config,
                                f"CUP Piemonte: {message}\nVai a prenotare su https://cup.isan.csi.it/",
                                shot,
                            )
                            ui_queue.put(("status", f"Disponibilita' trovata alle {ts}! Notifica inviata su Telegram."))
                        elif status == "RICETTA_ERROR":
                            checker.telegram_notify(config, f"CUP Piemonte - errore ricetta: {message}")
                            ui_queue.put(("status", "Ricetta non valida: controllo fermato. Correggi i dati e riavvia."))
                            ui_queue.put(("stopped", None))
                            return
                        elif status == "UNKNOWN":
                            ui_queue.put(("status", f"Pagina inattesa alle {ts} (vedi log/screenshots)."))
                        else:
                            ui_queue.put(("status", f"Ultimo controllo {ts}: nessuna disponibilita'."))
                    except PlaywrightTimeoutError as e:
                        ui_queue.put(("log", f"[{ts}] Timeout: {e}"))
                        ui_queue.put(("status", "Il sito non risponde, riprovo al prossimo giro."))

                    total_wait = config["interval_minutes"] * 60 * random.uniform(0.9, 1.1)
                    waited = 0.0
                    while waited < total_wait and not stop_event.is_set():
                        time.sleep(1)
                        waited += 1
            finally:
                context.close()
                browser.close()
    except Exception as e:  # difesa: un crash del thread non deve uccidere la GUI
        ui_queue.put(("log", f"Errore imprevisto: {e}"))
        ui_queue.put(("status", "Errore imprevisto: controllo fermato."))
    ui_queue.put(("stopped", None))


# -------------------------------------------------------------------- gui ----

root = tk.Tk()
root.title("CUP Piemonte - Controllo disponibilita'")
root.geometry("460x700")
root.minsize(420, 560)
root.resizable(True, True)

frm = ttk.Frame(root, padding=12)
frm.pack(fill="both", expand=True)


def labeled_entry(parent: ttk.Frame, label: str) -> tuple[tk.StringVar, ttk.Entry]:
    ttk.Label(parent, text=label).pack(anchor="w")
    var = tk.StringVar()
    entry = ttk.Entry(parent, textvariable=var)
    entry.pack(fill="x", pady=(0, 8))
    return var, entry


cf_var, _cf_entry = labeled_entry(frm, "Codice Fiscale")
nre_var, _nre_entry = labeled_entry(frm, "Numero ricetta elettronica (NRE)")

ttk.Separator(frm).pack(fill="x", pady=6)
ttk.Label(frm, text="Notifiche Telegram", font=("", 9, "bold")).pack(anchor="w", pady=(0, 4))
token_var, token_entry = labeled_entry(frm, "Bot Token (da @BotFather)")

bot_status_var = tk.StringVar(value="● Bot: nessun token")
bot_status_label = ttk.Label(frm, textvariable=bot_status_var, foreground="#888")
bot_status_label.pack(anchor="w", pady=(0, 8))


def verify_bot_async() -> None:
    token = token_var.get().strip()
    if not token:
        ui_queue.put(("bot_status", ("● Bot: nessun token", "#888")))
        return
    ui_queue.put(("bot_status", ("● Verifica in corso...", "#888")))

    def worker() -> None:
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            data = r.json()
            if data.get("ok"):
                username = data["result"].get("username", "?")
                ui_queue.put(("bot_status", (f"● Bot collegato: @{username}", "#1b8a2f")))
            else:
                ui_queue.put(("bot_status", ("● Bot non valido (token errato)", "#c0392b")))
        except requests.RequestException:
            ui_queue.put(("bot_status", ("● Bot non raggiungibile (rete/Telegram offline)", "#c0392b")))

    threading.Thread(target=worker, daemon=True).start()


token_entry.bind("<FocusOut>", lambda _e: verify_bot_async())
token_entry.bind("<Return>", lambda _e: verify_bot_async())

chatid_var, _chatid_entry = labeled_entry(frm, "Chat ID")

telegram_btn_row = ttk.Frame(frm)
telegram_btn_row.pack(fill="x", pady=(0, 8))


def on_detect_chat_id() -> None:
    token = token_var.get().strip()
    if not token:
        messagebox.showinfo("Chat ID", "Inserisci prima il Bot Token.")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        results = r.json().get("result", [])
        if not results:
            messagebox.showinfo(
                "Chat ID",
                "Nessun messaggio trovato. Apri la chat col tuo bot su Telegram, "
                "premi Avvia (o mandagli un messaggio qualsiasi), poi premi di nuovo questo pulsante.",
            )
            return
        chat_id = results[-1]["message"]["chat"]["id"]
        chatid_var.set(str(chat_id))
        messagebox.showinfo("Chat ID", f"Trovato: {chat_id}")
    except Exception as e:
        messagebox.showerror("Chat ID", f"Errore: {e}")


def on_test_telegram() -> None:
    cfg = {"telegram_bot_token": token_var.get().strip(), "telegram_chat_id": chatid_var.get().strip()}
    if not cfg["telegram_bot_token"] or not cfg["telegram_chat_id"]:
        messagebox.showinfo("Test", "Compila prima Bot Token e Chat ID.")
        return
    checker.telegram_notify(cfg, "Test: il bot CUP Piemonte e' configurato correttamente.")
    messagebox.showinfo("Test", "Messaggio inviato: controlla Telegram.")


ttk.Button(telegram_btn_row, text="Rileva Chat ID automaticamente", command=on_detect_chat_id).pack(side="left")
ttk.Button(telegram_btn_row, text="Invia test", command=on_test_telegram).pack(side="left", padx=(8, 0))

ttk.Separator(frm).pack(fill="x", pady=6)

interval_row = ttk.Frame(frm)
interval_row.pack(fill="x", pady=(0, 8))
ttk.Label(interval_row, text="Controlla ogni").pack(side="left")
interval_var = tk.StringVar(value="15")
ttk.Spinbox(interval_row, from_=5, to=180, textvariable=interval_var, width=5).pack(side="left", padx=6)
ttk.Label(interval_row, text="minuti").pack(side="left")

debug_var = tk.BooleanVar(value=False)
ttk.Checkbutton(
    frm, text="Mostra il browser mentre controlla (debug)", variable=debug_var
).pack(anchor="w", pady=(0, 8))

status_var = tk.StringVar(value="Fermo.")
ttk.Label(frm, textvariable=status_var, foreground="#555", wraplength=400).pack(anchor="w", pady=(2, 6))

log_text = tk.Text(frm, height=6, state="disabled", wrap="word", font=("Consolas", 8))
log_text.pack(fill="both", expand=True, pady=(0, 8))


def append_log(line: str) -> None:
    log_text.config(state="normal")
    log_text.insert("end", line + "\n")
    log_text.see("end")
    log_text.config(state="disabled")


toggle_btn = ttk.Button(frm, text="Avvia ricerca")
toggle_btn.pack(fill="x")


def save_current_fields() -> None:
    """Salva i campi cosi' come sono (anche incompleti), per non perderli alla chiusura."""
    try:
        interval = max(5, int(interval_var.get()))
    except ValueError:
        interval = 15
    save_config(
        {
            "codice_fiscale": cf_var.get().strip(),
            "nre": nre_var.get().strip(),
            "telegram_bot_token": token_var.get().strip(),
            "telegram_chat_id": chatid_var.get().strip(),
            "interval_minutes": interval,
        }
    )


def gather_config():
    cf = cf_var.get().strip()
    nre = nre_var.get().strip()
    token = token_var.get().strip()
    chat_id = chatid_var.get().strip()
    if not cf or not nre or not token or not chat_id:
        messagebox.showwarning("Campi mancanti", "Compila codice fiscale, NRE, bot token e chat ID prima di avviare.")
        return None
    try:
        interval = max(5, int(interval_var.get()))
    except ValueError:
        interval = 15
    return {
        "codice_fiscale": cf,
        "nre": nre,
        "telegram_bot_token": token,
        "telegram_chat_id": chat_id,
        "interval_minutes": interval,
    }


def start_checking() -> None:
    global worker_thread, running
    cfg = gather_config()
    if cfg is None:
        return
    save_config(cfg)
    stop_event.clear()
    running = True
    toggle_btn.config(text="Ferma ricerca")
    status_var.set("In esecuzione...")
    append_log("Avviato.")
    worker_thread = threading.Thread(target=worker_loop, args=(cfg, debug_var.get()), daemon=True)
    worker_thread.start()
    update_tray_menu()


def stop_checking() -> None:
    global running
    stop_event.set()
    running = False
    toggle_btn.config(text="Avvia ricerca")
    status_var.set("Fermo.")
    append_log("Fermato dall'utente.")
    update_tray_menu()


def on_toggle() -> None:
    if running:
        stop_checking()
    else:
        start_checking()


toggle_btn.config(command=on_toggle)

saved = load_saved_config()
cf_var.set(saved.get("codice_fiscale", ""))
nre_var.set(saved.get("nre", ""))
token_var.set(saved.get("telegram_bot_token", ""))
chatid_var.set(saved.get("telegram_chat_id", ""))
interval_var.set(str(saved.get("interval_minutes", 15)))

if token_var.get().strip():
    verify_bot_async()


# ------------------------------------------------------------------ tray ----

def make_tray_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(13, 71, 161, 255))
    d.text((24, 20), "C", fill="white")
    return img


def tray_toggle_text(_item) -> str:
    return "Ferma ricerca" if running else "Avvia ricerca"


tray_icon = pystray.Icon(
    "cup_checker",
    make_tray_image(),
    "CUP Piemonte Checker",
    menu=pystray.Menu(
        pystray.MenuItem("Apri finestra", lambda: ui_queue.put(("open", None)), default=True),
        pystray.MenuItem(tray_toggle_text, lambda: ui_queue.put(("toggle", None))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Esci", lambda: ui_queue.put(("quit", None))),
    ),
)


def update_tray_menu() -> None:
    tray_icon.update_menu()


def hide_window() -> None:
    save_current_fields()
    root.withdraw()


root.protocol("WM_DELETE_WINDOW", hide_window)


def shutdown() -> None:
    save_current_fields()
    stop_event.set()
    try:
        tray_icon.stop()
    except Exception:
        pass
    root.after(100, root.destroy)


def poll_queue() -> None:
    global running
    try:
        while True:
            kind, payload = ui_queue.get_nowait()
            if kind == "log":
                append_log(payload)
            elif kind == "status":
                status_var.set(payload)
            elif kind == "stopped":
                running = False
                toggle_btn.config(text="Avvia ricerca")
                update_tray_menu()
            elif kind == "open":
                root.deiconify()
                root.lift()
                root.focus_force()
            elif kind == "toggle":
                on_toggle()
            elif kind == "quit":
                shutdown()
            elif kind == "bot_status":
                text, color = payload
                bot_status_var.set(text)
                bot_status_label.config(foreground=color)
    except queue.Empty:
        pass
    root.after(150, poll_queue)


root.after(150, poll_queue)
threading.Thread(target=tray_icon.run, daemon=True).start()

root.mainloop()
