"""
Controlla periodicamente la disponibilita' di appuntamenti su CUP Piemonte
(https://cup.isan.csi.it/) per una ricetta dematerializzata (SSN) e invia
una notifica Telegram quando trova un appuntamento prenotabile.

Uso:
    python checker.py               loop continuo (rispetta interval_minutes da config.json)
    python checker.py --once        esegue un solo controllo ed esce (utile con Task Scheduler)
    python checker.py --test-telegram  invia un messaggio di prova su Telegram e esce
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "checker.log"
SCREENSHOT_DIR = BASE_DIR / "screenshots"

RICETTA_URL = "https://cup.isan.csi.it/web/guest/ricetta-dematerializzata"
NO_SLOTS_TEXT = "Nessun appuntamento disponibile"
RICETTA_ERROR_TEXT = "Impossibile recuperare"

MIN_INTERVAL_MINUTES = 5  # non scendere sotto questa soglia per non sovraccaricare il portale pubblico


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"Manca {CONFIG_PATH.name}. Copia config.example.json in config.json "
            "e compila codice_fiscale, nre e i dati Telegram."
        )
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    placeholders = ["INSERISCI_QUI"]
    for key in ("codice_fiscale", "nre", "telegram_bot_token", "telegram_chat_id"):
        value = str(config.get(key, ""))
        if not value or any(p in value for p in placeholders):
            sys.exit(f"Il campo '{key}' in config.json non e' stato compilato.")
    config["interval_minutes"] = max(MIN_INTERVAL_MINUTES, int(config.get("interval_minutes", 15)))
    config["headless"] = bool(config.get("headless", False))
    return config


def telegram_notify(config: dict, text: str, photo_path: Path | None = None) -> None:
    token = config["telegram_bot_token"]
    chat_id = config["telegram_chat_id"]
    try:
        if photo_path and photo_path.exists():
            with open(photo_path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": f},
                    timeout=30,
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text},
                timeout=30,
            )
    except requests.RequestException as e:
        log(f"Invio notifica Telegram fallito: {e}")


def new_stealthy_context(browser):
    """Contesto con impronta un po' piu' simile a un utente reale italiano
    (non garantisce di eludere l'anti-bot, ma aiuta a non essere il caso piu' ovvio)."""
    context = browser.new_context(
        locale="it-IT",
        timezone_id="Europe/Rome",
        viewport={"width": 1366, "height": 768},
    )
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return context


def _human_pause(lo_ms: int = 250, hi_ms: int = 700) -> None:
    time.sleep(random.uniform(lo_ms, hi_ms) / 1000)


def check_once(page, config: dict) -> tuple[str, str, Path | None]:
    """Ritorna (status, messaggio, screenshot) con status in
    AVAILABLE / NONE / RICETTA_ERROR / UNKNOWN"""

    page.goto(RICETTA_URL, wait_until="networkidle", timeout=45000)
    _human_pause(500, 1400)  # tempo di "lettura" della pagina prima di agire

    cf_input = page.locator('input[id$="CFInput"]')
    nre_input = page.locator('input[id$="nreInput0"]')
    submit_btn = page.locator('div[id$="nreButton"]')

    # niente digitazione carattere-per-carattere sui campi dati: con eventuali maschere/
    # validatori JS sul campo puo' corrompere il valore inserito (visto con l'NRE) - la
    # correttezza del dato conta piu' del "sembrare umano" qui, quindi si usa fill() diretto
    cf_input.wait_for(state="visible", timeout=20000)
    cf_input.click()
    _human_pause(150, 400)
    cf_input.fill(config["codice_fiscale"])
    _human_pause(150, 450)
    nre_input.click()
    _human_pause(150, 400)
    nre_input.fill(config["nre"])
    _human_pause(300, 800)

    # il bottone attiva il suo vero handler JS solo al primo "mouseover" (lazy-load
    # di ICEfaces): lo si scatena esplicitamente prima del click, poi si aspetta con
    # pazienza. NOTA dall'utente (verificato a mano): "Prosegui" va cliccato DUE volte,
    # il primo click non basta da solo - quindi si ripete se il form e' ancora li'.
    def _click_submit() -> None:
        submit_btn.dispatch_event("mouseover")
        _human_pause(400, 800)
        try:
            submit_btn.hover(timeout=8000)
        except PlaywrightTimeoutError:
            pass  # probabile overlay residuo: si tenta comunque il click sotto
        _human_pause(150, 400)
        submit_btn.click(timeout=15000)
        _wait_overlay_clear(page)
        _wait_for_next_state(page)

    _click_submit()
    if submit_btn.count() > 0 and submit_btn.is_visible():
        _human_pause(300, 700)
        _click_submit()

    # errore esplicito del portale (ricetta non trovata / gia' usata / scaduta) o
    # step intermedio "Prestazioni" con un pulsante per proseguire: si controlla
    # l'errore ad ogni iterazione per evitare falsi negativi dovuti alla postback JSF
    click_log = []
    # regex senza "prosegui": quel testo appartiene al bottone di ricerca dello step 1
    # (gia' cliccato sopra) e ri-matcharlo qui causherebbe un doppio invio della ricerca
    continue_regex = re.compile(r"\bavanti\b|\bconferma\b|\bcontinua\b", re.I)
    for step in range(6):
        if page.get_by_text(RICETTA_ERROR_TEXT, exact=False).count() > 0:
            shot = _screenshot(page, "ricetta_error")
            return "RICETTA_ERROR", page.get_by_text(RICETTA_ERROR_TEXT, exact=False).first.inner_text(), shot

        # errore di validazione (es. "Numero ricetta elettronica non valido"): ricliccare
        # "Avanti" non risolve, quindi ci si ferma subito invece di ripetere a vuoto
        invalid_msg = page.get_by_text("non valid", exact=False)
        if invalid_msg.count() > 0:
            shot = _screenshot(page, "validation_error")
            return "RICETTA_ERROR", invalid_msg.first.inner_text().strip(), shot

        if page.get_by_text("Appuntamenti Proposti", exact=False).count() > 0:
            break

        # pulsanti "Avanti"/"Conferma" ecc: solo quelli EFFETTIVAMENTE visibili
        # (la pagina puo' contenere doppioni nascosti con la stessa classe/testo)
        all_matches = page.locator(".btn").filter(has_text=continue_regex)
        visible_matches = page.locator(".btn:visible").filter(has_text=continue_regex)
        total, visible_count = all_matches.count(), visible_matches.count()

        if visible_count > 0:
            label = visible_matches.first.inner_text().strip()
            try:
                # niente force: se qualcosa intercetta il click voglio l'errore vero, non un click a vuoto
                visible_matches.first.scroll_into_view_if_needed(timeout=5000)
                visible_matches.first.hover(timeout=8000)
                _human_pause(200, 600)
                visible_matches.first.click(timeout=10000)
                click_log.append(f"#{step+1} click ok su '{label}' (url: {page.url})")
            except PlaywrightTimeoutError as e:
                first_line = str(e).splitlines()[0] if str(e) else "timeout"
                click_log.append(f"#{step+1} click fallito (matched {total}, visibili {visible_count}): {first_line}")
                break
            _wait_overlay_clear(page)
            _wait_for_next_state(page)
        else:
            click_log.append(f"#{step+1} nessun pulsante visibile trovato (matched totali {total})")
            break

    if page.get_by_text("Appuntamenti Proposti", exact=False).count() == 0:
        shot = _screenshot(page, "unknown_state")
        labels = []
        all_btns = page.locator(".btn:visible")
        for i in range(min(all_btns.count(), 12)):
            try:
                t = all_btns.nth(i).inner_text().strip().replace("\n", " ")
                if t:
                    labels.append(t)
            except Exception:
                pass
        detail = f" Pulsanti visibili: [{', '.join(labels)}]." if labels else " Nessun pulsante .btn visibile."
        detail += f" Log click: {' | '.join(click_log)}" if click_log else ""
        detail += f" Messaggi di sistema visti: {_alert_texts(page)}"
        detail += f" Righe con parole chiave: {_keyword_lines(page, config)}"
        if any("prosegui" in l.lower() for l in labels):
            try:
                cf_still_there = bool(page.locator('input[id$="CFInput"]').input_value().strip())
                detail += f" Campo CF ancora pieno dopo il click: {cf_still_there}."
            except Exception:
                pass
        return (
            "UNKNOWN",
            "Non sono arrivato alla pagina Appuntamenti: la struttura del sito potrebbe essere cambiata." + detail,
            shot,
        )

    # sezione "Appuntamenti Proposti": se non dice "nessun appuntamento", e' gia' una disponibilita'
    if page.get_by_text(NO_SLOTS_TEXT, exact=False).count() == 0:
        shot = _screenshot(page, "available_proposti")
        return "AVAILABLE", "Disponibilita' trovata negli appuntamenti proposti!", shot

    # nessuna proposta diretta: prova ad allargare con "Altre disponibilita'", come faresti a mano
    altre_btn = page.locator(".btn, a").filter(has_text=re.compile(r"altre disponibilit", re.I))
    if altre_btn.count() > 0 and altre_btn.first.is_visible():
        try:
            altre_btn.first.hover()
            _human_pause(200, 600)
            altre_btn.first.click(timeout=10000)
            _wait_overlay_clear(page)
        except PlaywrightTimeoutError:
            pass

    if page.get_by_text("Appuntamenti Disponibili", exact=False).count() > 0:
        # con entrambe le sezioni visibili, se il messaggio "nessun appuntamento" compare
        # meno di 2 volte vuol dire che una delle due sezioni mostra invece uno slot reale
        no_slots_count = page.get_by_text(NO_SLOTS_TEXT, exact=False).count()
        shot = _screenshot(page, "available" if no_slots_count < 2 else "none")
        if no_slots_count < 2:
            return "AVAILABLE", "Disponibilita' trovata nella sezione Appuntamenti Disponibili!", shot
        return "NONE", "Nessun appuntamento disponibile (controllati proposti e disponibili).", shot

    shot = _screenshot(page, "none")
    return "NONE", "Nessun appuntamento disponibile nella sezione proposti.", shot


def _alert_texts(page) -> str:
    """Testo di eventuali banner di errore/avviso generici (mai l'area con i dati del paziente)."""
    texts = []
    try:
        banner = page.locator(
            ".alert:visible, [class*='alert']:visible, [class*='warning']:visible, [class*='toast']:visible, "
            "[class*='growl']:visible, [class*='message']:visible, [class*='msg']:visible"
        )
        count = min(banner.count(), 5)
        for i in range(count):
            t = banner.nth(i).inner_text().strip().replace("\n", " ")
            if t and t not in texts:
                texts.append(t[:150])
    except Exception:
        pass
    return "[" + "; ".join(texts) + "]" if texts else "[nessuno]"


def _keyword_lines(page, config: dict) -> str:
    """Righe di testo della pagina che contengono parole chiave di sistema/errore.
    Non e' un dump della pagina: solo le righe che matchano, e comunque con CF/NRE
    oscurati per sicurezza nel caso comparissero nella stessa riga di un messaggio."""
    keywords = [
        "errore", "non disponibil", "riprova", "limite", "tentativi",
        "sessione", "scadut", "bloccat", "captcha", "robot", "sicurezza",
        "temporaneamente", "attend", "traffico", "anomal",
    ]
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        return "[non leggibile]"

    cf = config.get("codice_fiscale", "")
    nre = config.get("nre", "")
    matched = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(k in line.lower() for k in keywords):
            safe = line.replace(cf, "***CF***") if cf else line
            safe = safe.replace(nre, "***NRE***") if nre else safe
            if safe not in matched:
                matched.append(safe[:150])
        if len(matched) >= 6:
            break
    return "[" + " | ".join(matched) + "]" if matched else "[nessuna riga con parole chiave]"


def _wait_overlay_clear(page) -> None:
    """Attende che la pagina finisca di elaborare dopo un click. Si usano piu' segnali
    insieme, dal piu' affidabile al piu' specifico, perche' nessuno dei due basta da solo:
    'networkidle' (nessuna richiesta di rete attiva - il segnale piu' robusto di Playwright)
    e poi, come rete di sicurezza, gli spinner visivi noti di questo sito."""
    try:
        page.wait_for_load_state("networkidle", timeout=45000)
    except PlaywrightTimeoutError:
        pass

    overlay = page.locator(".blockUI.blockOverlay")
    wait_dialog = page.get_by_text("Attendere prego", exact=False)
    # prima si aspetta che COMPAIA (se non e' ancora comparso, "sparito" sarebbe un falso
    # positivo vacuo), poi che sparisca davvero - lo si fa per entrambi gli spinner noti
    for indicator in (overlay, wait_dialog):
        try:
            indicator.first.wait_for(state="visible", timeout=2000)
        except PlaywrightTimeoutError:
            continue  # non e' (ancora) comparso: niente da aspettare per questo
        try:
            indicator.first.wait_for(state="hidden", timeout=60000)
        except PlaywrightTimeoutError:
            pass
    page.wait_for_timeout(800)


def _wait_for_next_state(page, timeout_ms: int = 15000) -> None:
    """Aspetta un segnale concreto di risposta del server (errore, validazione o
    pagina Appuntamenti) invece di fidarsi solo dello spinner: a volte appare/sparisce
    piu' in fretta di quanto intercettato, facendo leggere la pagina troppo presto."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.get_by_text(RICETTA_ERROR_TEXT, exact=False).count() > 0:
            return
        if page.get_by_text("non valid", exact=False).count() > 0:
            return
        if page.get_by_text("Appuntamenti Proposti", exact=False).count() > 0:
            return
        time.sleep(0.4)


def _screenshot(page, label: str) -> Path:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = SCREENSHOT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def run_loop(config: dict, once: bool) -> None:
    consecutive_errors = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config["headless"])
        context = new_stealthy_context(browser)
        page = context.new_page()  # stessa scheda riusata ad ogni giro, come farebbe una persona
        try:
            while True:
                try:
                    status, message, shot = check_once(page, config)
                    log(f"{status}: {message}")

                    if status == "AVAILABLE":
                        telegram_notify(config, f"CUP Piemonte: {message}\nVai a prenotare subito su https://cup.isan.csi.it/", shot)
                    elif status == "RICETTA_ERROR":
                        telegram_notify(config, f"CUP Piemonte - errore ricetta: {message}\nControllo interrotto, verifica codice fiscale/NRE in config.json.")
                        log("Ricetta non valida: interrompo il loop.")
                        return
                    elif status == "UNKNOWN":
                        consecutive_errors += 1
                        if consecutive_errors in (1, 6, 20):
                            telegram_notify(config, f"CUP Piemonte - attenzione: {message}")
                    else:
                        consecutive_errors = 0
                except PlaywrightTimeoutError as e:
                    consecutive_errors += 1
                    log(f"Timeout durante il controllo: {e}")
                    if consecutive_errors in (1, 6, 20):
                        telegram_notify(config, "CUP Piemonte - il sito non risponde, riprovo automaticamente.")

                if once:
                    return

                jitter = random.uniform(0.9, 1.1)
                sleep_seconds = config["interval_minutes"] * 60 * jitter
                log(f"Prossimo controllo tra {sleep_seconds/60:.1f} minuti.")
                time.sleep(sleep_seconds)
        finally:
            context.close()
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Esegue un solo controllo ed esce")
    parser.add_argument("--test-telegram", action="store_true", help="Invia un messaggio di prova su Telegram")
    args = parser.parse_args()

    config = load_config()

    if args.test_telegram:
        telegram_notify(config, "Test: il bot CUP Piemonte e' configurato correttamente.")
        print("Messaggio di prova inviato (controlla Telegram).")
        return

    log("Avvio controllo CUP Piemonte.")
    try:
        run_loop(config, once=args.once)
    except KeyboardInterrupt:
        log("Interrotto dall'utente.")


if __name__ == "__main__":
    main()
