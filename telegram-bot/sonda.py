"""Sonda del portale CUP: ogni pochi minuti una sola richiesta GET alla pagina iniziale, per sapere se il portale
risponde e in quanto tempo, anche quando il bot non sta controllando nessuna ricetta. Non apre sessioni di
prenotazione: non tiene occupata nessuna data. I dati non si cancellano mai e non contengono dati personali."""
import logging
import statistics
import threading
import time
from datetime import datetime

import requests

import cup_http
import impronte
from store import Store

log = logging.getLogger("cupbot.sonda")
INTERVALLO = 300  # secondi tra due sonde
ATTESA = (10, 30)  # connessione, risposta: oltre, il portale per noi e' giu'
OK, TIMEOUT, RETE, ERRORE_HTTP = "ok", "timeout", "rete", "http"
IMPRONTE_OGNI = 30 * 60  # secondi tra due letture delle pagine pubbliche per le impronte (impronte.py)
PUBBLICHE = (("pubblica/elenco", cup_http.LISTA_URL), ("pubblica/ricerca", cup_http.RICETTA_URL))


def misura():
    """(secondi, codice HTTP o None, esito). Il portale e' su se risponde con un codice sotto il 500."""
    inizio = time.monotonic()
    try:
        r = requests.get(cup_http.LISTA_URL, timeout=ATTESA, headers={"User-Agent": cup_http.UA})
        secondi = time.monotonic() - inizio
        return round(secondi, 2), r.status_code, OK if r.status_code < 500 else ERRORE_HTTP
    except requests.Timeout:
        return round(time.monotonic() - inizio, 2), None, TIMEOUT
    except requests.RequestException:
        return round(time.monotonic() - inizio, 2), None, RETE


def impronte_pubbliche(store):
    """Le pagine pubbliche (Elenco e Ricerca, vuote): una GET ciascuna, senza dati, come un browser che le apre.
    Non aprono sessioni di prenotazione. Solo le risposte 200 sono osservazioni."""
    for chiave, url in PUBBLICHE:
        try:
            r = requests.get(url, timeout=(10, 60), headers={"User-Agent": cup_http.UA})
        except requests.RequestException:
            continue
        cookie = list(r.cookies.keys())
        # un muro antibot (Cloudflare, DataDome...) risponde spesso 403/429/503: per la sonda e' "giu'", ma va visto
        muro = r.status_code in (403, 429, 503) and "antibot" in impronte.impronta(r.text, cookie)
        if r.status_code == 200 or muro:
            impronte.osserva(store, chiave, r.text, cookie, r.headers.get("Server", ""))


def avvia(db_path, key, intervallo=INTERVALLO):
    """Thread suo, con la sua connessione al database: i controlli lunghi del bot non ritardano la sonda.
    Un errore (database occupato all'avvio, scrittura fallita) si registra e il ciclo continua."""
    def ciclo():
        store, prima, ultime_impronte = None, True, 0.0
        while True:
            try:
                if store is None:
                    store = Store(db_path, key)
                if prima:  # riavvii ravvicinati non devono sondare di continuo il portale
                    prima = False
                    ultima = (store.sonde(time.time() - intervallo)[-1:] or [(0,)])[0][0]
                    time.sleep(max(0.0, ultima + intervallo - time.time()))
                secondi, codice, esito = misura()
                store.sonda(time.time(), secondi, codice, esito)
                if esito == OK and time.time() - ultime_impronte >= IMPRONTE_OGNI:
                    ultime_impronte = time.time()
                    impronte_pubbliche(store)
            except Exception as e:
                log.error("sonda: errore imprevisto %s", type(e).__name__)
                store = None  # alla prossima giro riapre la connessione
            time.sleep(intervallo)
    threading.Thread(target=ciclo, name="sonda", daemon=True).start()


# --- numeri per la dashboard ------------------------------------------------------------------
def disponibilita(sonde):
    """Percentuale di sonde riuscite, o None senza sonde. sonde: [(ts, secondi, codice, esito)]."""
    return 100 * sum(1 for s in sonde if s[3] == OK) / len(sonde) if sonde else None


def percentile(valori, p):
    valori = sorted(valori)
    return valori[min(len(valori) - 1, int(p * len(valori)))] if valori else None


def per_ora_del_giorno(sonde, tz):
    """Per ogni ora del giorno (0-23, nel fuso dato): (sonde, riuscite, mediana, 95 percentile) dei tempi
    delle sonde riuscite."""
    ore = {h: [] for h in range(24)}
    for ts, secondi, _, esito in sonde:
        ore[datetime.fromtimestamp(ts, tz).hour].append((secondi, esito == OK))
    out = {}
    for h, voci in ore.items():
        tempi = [s for s, ok in voci if ok]
        out[h] = (len(voci), len(tempi), statistics.median(tempi) if tempi else None, percentile(tempi, 0.95))
    return out


def ultime_ore(sonde, ora, tz, quante=24):
    """Le ultime `quante` ore (l'ultima e' quella in corso), dalla piu' vecchia: (inizio, sonde, fallite,
    mediana dei tempi riusciti). Le ore sono di 3600 secondi veri: nel cambio dell'ora legale non si
    sdoppiano ne' spariscono."""
    fine = (int(ora) // 3600 + 1) * 3600
    out = []
    for i in range(quante, 0, -1):
        a, b = fine - i * 3600, fine - (i - 1) * 3600
        voci = [s for s in sonde if a <= s[0] < b]
        tempi = [s[1] for s in voci if s[3] == OK]
        out.append((datetime.fromtimestamp(a, tz), len(voci), sum(1 for s in voci if s[3] != OK),
                    statistics.median(tempi) if tempi else None))
    return out
