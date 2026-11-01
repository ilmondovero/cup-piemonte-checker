"""Segnalazioni dei problemi dalle app desktop (solo da chi ha acceso l'opzione nelle impostazioni dell'app).

L'app manda POST /segnala con un JSON (vedi app/desktop/src/segnalazioni.rs):
  {"v": 1, "app": "0.3.1", "sistema": "windows-x86_64", "installazione": "nsis", "id": "<16 hex>",
   "tipo": "panico|avvio|aggiornamento|portale|errore", "testo": "<una riga>", "rapporto": "<testo>"}
Il server la ricontrolla (campi, misure, dati personali con impronte.pulisci), la mette tra le segnalazioni e il
ciclo del bot la manda all'amministratore. Non serve un account: chiunque abbia l'app (e quindi l'indirizzo, che
dentro l'app compilata si legge) puo' scrivere. Per questo ci sono limiti per provenienza, per installazione e in
tutto. L'indirizzo IP serve solo al limite per provenienza: in memoria, come impronta con una chiave che cambia a
ogni avvio, mai salvato."""
import hashlib
import hmac
import json
import secrets
import re
import threading
import time

import impronte

VERSIONE = 1
TIPI = {"panico": "l'app si e' chiusa per un errore interno", "avvio": "l'app non riesce a partire",
        "aggiornamento": "aggiornamento non riuscito", "portale": "pagina del portale non riconosciuta",
        "errore": "esito incerto o errore"}
MAX_CORPO = 32 * 1024
MAX_TESTO, MAX_RAPPORTO = 200, 24000
PER_PROVENIENZA = (5, 86400)  # al massimo 5 segnalazioni al giorno dallo stesso indirizzo
PER_INSTALLAZIONE = (3, 3600)  # e 3 l'ora dalla stessa installazione
IN_TUTTO = (30, 86400)  # e 30 al giorno in tutto: oltre, chi scrive aspetta (429)


class Rifiutata(Exception):
    def __init__(self, stato, motivo):
        super().__init__(motivo)
        self.stato, self.motivo = stato, motivo


class Ricevitore:
    """Controlla e registra le segnalazioni. I limiti stanno in memoria (un riavvio li azzera: va bene)."""

    def __init__(self):
        self.viste = {}  # id installazione -> [epoch]
        self.fonti = {}  # impronta dell'indirizzo -> [epoch]
        self.chiave = secrets.token_bytes(32)  # solo in memoria: le impronte non valgono dopo un riavvio
        self.tutte = []  # [epoch]
        self.lock = threading.Lock()

    def ricevi(self, store, corpo, ora=None, fonte=""):
        """Ritorna (stato HTTP, risposta JSON). Una segnalazione valida finisce tra quelle da mandare.
        fonte: l'indirizzo di chi scrive (dal reverse proxy), solo per il limite per provenienza."""
        ora = ora or time.time()
        try:
            dati = self._valida(corpo)
            impronta = hmac.new(self.chiave, fonte.encode(), hashlib.sha256).hexdigest()[:16]
            self._limita(dati["id"], impronta, ora)
        except Rifiutata as r:
            return r.stato, {"ok": False, "motivo": r.motivo}
        testo = (f"{TIPI[dati['tipo']]} ({dati['app']}, {dati['sistema']}, {dati['installazione']}): "
                 f"{impronte.pulisci(dati['testo'])}")
        testo = re.sub(r"(?i)\b(?:https?://|www\.)\S+", "[indirizzo]", testo)  # niente link cliccabili in chat
        rapporto = "\n".join([
            "Segnalazione dall'app desktop", f"Tipo: {dati['tipo']}", f"Versione: {dati['app']}",
            f"Sistema: {dati['sistema']}", f"Installazione: {dati['installazione']}",
            f"Installazione (id anonimo): {dati['id']}", "", impronte.pulisci(dati["rapporto"])])
        store.cambio_portale(ora, "app", "errore", testo, rapporto)
        return 200, {"ok": True}

    def _valida(self, corpo):
        if len(corpo) > MAX_CORPO:
            raise Rifiutata(413, "troppo lunga")
        try:
            dati = json.loads(corpo.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Rifiutata(400, "non e' JSON") from None
        if not isinstance(dati, dict) or dati.get("v") != VERSIONE:
            raise Rifiutata(400, "versione del formato sconosciuta")
        if dati.get("tipo") not in TIPI:
            raise Rifiutata(400, "tipo sconosciuto")
        for campo, rx in (("app", r"\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[\w.]{1,20})?"), ("sistema", r"[a-z0-9_]{1,20}-[a-z0-9_]{1,20}"),
                          ("installazione", r"[a-z]{1,20}"), ("id", r"[0-9a-f]{16}")):
            if not isinstance(dati.get(campo), str) or not re.fullmatch(rx, dati[campo]):
                raise Rifiutata(400, f"campo {campo} non valido")
        for campo, massimo in (("testo", MAX_TESTO), ("rapporto", MAX_RAPPORTO)):
            if not isinstance(dati.get(campo), str) or len(dati[campo]) > massimo:
                raise Rifiutata(400, f"campo {campo} non valido")
        dati["testo"] = " ".join(dati["testo"].split())  # una riga
        return dati

    def _limita(self, installazione, fonte, ora):
        with self.lock:
            self.fonti = _recenti(self.fonti, PER_PROVENIENZA[1], ora)
            self.viste = _recenti(self.viste, PER_INSTALLAZIONE[1], ora)
            self.tutte = [t for t in self.tutte if ora - t < IN_TUTTO[1]]
            if (len(self.fonti.get(fonte, [])) >= PER_PROVENIENZA[0] or
                    len(self.viste.get(installazione, [])) >= PER_INSTALLAZIONE[0] or len(self.tutte) >= IN_TUTTO[0]):
                raise Rifiutata(429, "troppe segnalazioni: riprova piu' tardi")
            self.fonti.setdefault(fonte, []).append(ora)
            self.viste.setdefault(installazione, []).append(ora)
            self.tutte.append(ora)


def _recenti(per_chiave, finestra, ora):
    """Solo i tempi dentro la finestra, e solo le chiavi che ne hanno ancora: la memoria non cresce."""
    out = {k: [t for t in v if ora - t < finestra] for k, v in per_chiave.items()}
    return {k: v for k, v in out.items() if v}
