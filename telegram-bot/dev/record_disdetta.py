"""Solo sviluppo: registra le richieste HTTP della disdetta, mentre la esegui tu a mano nel browser.

Uso:  py record_disdetta.py CODICE_FISCALE NRE [--no]
Si apre Chromium (non headless): cerca la ricetta, premi "Disdici Prescrizione" e, se vuoi disdire davvero,
"Si". Con --no lo script blocca la richiesta di conferma finale: cosi' registra il dialogo (id del pulsante
"Si") senza disdire niente. Finito, chiudi la finestra. Il risultato e' dev/capture/disdetta_requests.jsonl con
CF e NRE mascherati; i corpi delle risposte ajax sono in dev/capture/disdetta_risposte.jsonl."""
import json, sys
from urllib.parse import parse_qsl
from playwright.sync_api import sync_playwright
from pathlib import Path

CAPTURE = Path(__file__).parent / "capture"  # fuori da git: contiene risposte reali del portale
CAPTURE.mkdir(exist_ok=True)


def mask(text, cfg):
    for k, v in cfg.items():
        text = text.replace(v, f"***{k}***")
    return text


args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) != 2:
    sys.exit(__doc__)
cfg = {"codice_fiscale": args[0], "nre": args[1]}
solo_dialogo = "--no" in sys.argv
req_log = open(CAPTURE / "disdetta_requests.jsonl", "w", encoding="utf-8")
resp_log = open(CAPTURE / "disdetta_risposte.jsonl", "w", encoding="utf-8")


def scrivi(f, oggetto):
    f.write(mask(json.dumps(oggetto, ensure_ascii=False), cfg) + "\n")
    f.flush()


def on_request(req):
    if "cup.isan.csi.it" not in req.url or req.method != "POST":
        return
    dati = dict(parse_qsl(req.post_data or ""))
    scrivi(req_log, {"url": req.url, "headers": {k: v for k, v in req.headers.items()
                                                 if k.lower() in ("faces-request", "content-type", "x-requested-with")},
                     "data": dati})


def on_response(resp):
    if "cup.isan.csi.it" in resp.url and resp.request.method == "POST":
        try:
            scrivi(resp_log, {"url": resp.url, "status": resp.status, "body": resp.text()})
        except Exception as e:  # risposta gia' chiusa (navigazione): non e' un problema
            scrivi(resp_log, {"url": resp.url, "status": resp.status, "errore": str(e)})


def blocca_conferma(route):
    dati = dict(parse_qsl(route.request.post_data or ""))
    sorgente = (dati.get("javax.faces.source", "") + dati.get("ice.event.captured", "")).lower()
    # il dialogo si apre con "disdiciButton"; la conferma e' un altro pulsante della stessa form
    if solo_dialogo and sorgente and "disdicibutton" not in sorgente and "filterprescr" not in sorgente \
            and "listaprenotazioni" in sorgente:
        scrivi(req_log, {"BLOCCATA": True, "data": dati})
        print("Richiesta di conferma bloccata (--no): niente disdetta.")
        return route.abort()
    route.continue_()


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=False)
    ctx = b.new_context(locale="it-IT")
    page = ctx.new_page()
    page.on("request", on_request)
    page.on("response", on_response)
    page.route("**/lista-prenotazioni*", blocca_conferma)
    page.goto("https://cup.isan.csi.it/web/guest/lista-prenotazioni")
    print("Fai la procedura nel browser; chiudi la finestra quando hai finito.")
    page.wait_for_event("close", timeout=0)
    b.close()
