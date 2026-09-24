"""Mini App Telegram: la stessa gestione del bot in un'app che si apre dalla chat.

Gira nello stesso processo del bot, in un thread suo, e ascolta solo in locale (davanti c'e' il
reverse proxy con HTTPS). Chi apre l'app viene riconosciuto dalla firma che Telegram mette in
`initData`: la si verifica a ogni richiesta con il token del bot, niente password ne' cookie.

- Letture e impostazioni (dove cercare, automatica, pausa) usano una connessione al database propria
  e `Store.modifica`, cosi' non sovrascrivono cio' che il bot scrive dopo un controllo.
- Le azioni che toccano il portale ("controlla ora", "prenota") vanno nella coda del bot: e' lui che
  tiene le sessioni e le date bloccate, esattamente come per i pulsanti in chat.
"""
import hashlib
import hmac
import html
import json
import logging
import re
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

import bot as botmod
import cup_http
from store import Store

log = logging.getLogger("cupbot.web")

STATIC = Path(__file__).resolve().parent / "web" / "static"
FILE_STATICI = {"htmx.min.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "app.css": "text/css; charset=utf-8"}
MAX_ETA_INITDATA = 24 * 3600  # secondi: oltre, Telegram deve rifirmare (basta riaprire l'app)
MAX_ETA_PRENOTA = 2 * 3600  # per spostare una prenotazione la firma dev'essere recente
MAX_CORPO = 4096
PAUSA_AZIONI = 1.5  # secondi minimi tra due azioni della stessa chat
TELEGRAM_JS = "https://telegram.org/js/telegram-web-app.js"
CSP = (f"default-src 'self'; script-src 'self' {TELEGRAM_JS}; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")
e = html.escape


def verifica_init_data(init_data, token, ora=None, max_eta=MAX_ETA_INITDATA):
    """Id dell'utente Telegram se initData e' firmato dal bot e recente, altrimenti None.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app"""
    if not init_data or len(init_data) > 4096:
        return None
    campi = dict(parse_qsl(init_data, keep_blank_values=True))
    firma = campi.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", firma):
        return None
    stringa = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()))
    segreto = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    atteso = hmac.new(segreto, stringa.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(atteso, firma):
        return None
    try:
        if (ora or time.time()) - int(campi.get("auth_date", 0)) > max_eta:
            return None
        return int(json.loads(campi.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        return None


class Richiesta(Exception):
    def __init__(self, stato, testo):
        super().__init__(testo)
        self.stato, self.testo = stato, testo


def firma_da(intestazioni):
    """initData dall'intestazione standard `Authorization: tma <initData>` (i proxy la tolgono dai log)."""
    valore = intestazioni.get("authorization", "")
    return valore[4:] if valore.startswith("tma ") else ""


class App:
    def __init__(self, bot, store=None, db_path=None, key=None):
        """store: una connessione gia' pronta (test, un solo thread). In produzione db_path e key:
        ogni thread del server apre la sua connessione (sqlite non le condivide tra thread)."""
        self.bot, self._store, self._db = bot, store, (db_path, key)
        self._locale = threading.local()
        self._ultima_azione = {}  # chat -> ora dell'ultima azione (limite di frequenza)
        self._lock = threading.Lock()

    @property
    def store(self):
        if self._store is not None:
            return self._store
        if not hasattr(self._locale, "store"):
            self._locale.store = Store(*self._db)
        return self._locale.store

    def _limita(self, chat):
        with self._lock:
            ora = time.time()
            if ora - self._ultima_azione.get(chat, 0) < PAUSA_AZIONI:
                raise Richiesta(429, "Un momento: una cosa alla volta.")
            self._ultima_azione[chat] = ora

    def _in_coda(self, *voce):
        try:
            self.bot.coda.put_nowait(voce)
        except queue.Full:
            raise Richiesta(503, "Il bot e' molto occupato: riprova tra un minuto.")

    # --- instradamento ---------------------------------------------------------------
    def gestisci(self, metodo, percorso, intestazioni, corpo=b""):
        """(stato, intestazioni, corpo). Tutto passa da qui: comodo da testare senza rete."""
        try:
            if metodo == "GET" and percorso == "/":
                return self._html(200, self.pagina())
            if metodo == "GET" and percorso.startswith("/static/"):
                return self.statico(percorso[len("/static/"):])
            firma = firma_da(intestazioni)
            chat = verifica_init_data(firma, self.bot.token)
            if chat is None:
                raise Richiesta(401, "Apri l'app dal pulsante del bot su Telegram.")
            dati = dict(parse_qsl(corpo.decode("utf-8", "replace"))) if corpo else {}
            if metodo == "GET" and percorso == "/ui/ricette":
                return self._html(200, self.ricette(chat))
            m = re.fullmatch(r"/ui/r/(\d+)/(dove|auto|pausa|riprendi|controlla|offerta)", percorso)
            if not m:
                raise Richiesta(404, "Pagina non trovata.")
            pid, azione = int(m.group(1)), m.group(2)
            p = self.store.get(pid)
            if not p or p["chat_id"] != chat or p["stato"] not in ("attivo", "pausa"):
                raise Richiesta(404, "Ricetta non trovata.")
            if metodo == "GET" and azione in ("dove", "auto"):
                return self._html(200, (self.foglio_dove if azione == "dove" else self.foglio_auto)(p))
            if metodo == "POST":
                self._limita(chat)
                if azione == "offerta" and verifica_init_data(firma, self.bot.token, max_eta=MAX_ETA_PRENOTA) is None:
                    raise Richiesta(401, "Per sicurezza chiudi e riapri l'app, poi prenota di nuovo.")
                avviso = getattr(self, "azione_" + azione)(chat, p, dati)
                return self._html(200, self.ricette(chat, avviso))
            raise Richiesta(405, "Metodo non permesso.")
        except Richiesta as r:
            return self._html(r.stato, f'<p class="errore">{e(r.testo)}</p>')

    def _html(self, stato, testo):
        return stato, {"Content-Type": "text/html; charset=utf-8"}, testo.encode()

    def statico(self, nome):
        if nome not in FILE_STATICI:
            raise Richiesta(404, "File non trovato.")
        return 200, {"Content-Type": FILE_STATICI[nome], "Cache-Control": "public, max-age=3600"}, \
            (STATIC / nome).read_bytes()

    # --- azioni ----------------------------------------------------------------------
    def _modifica(self, chat, p, fn):
        def solo_se_sua(fresca):
            if fresca["chat_id"] != chat or fresca["stato"] not in ("attivo", "pausa"):
                raise Richiesta(404, "Ricetta non trovata.")
            fn(fresca)
        self.store.modifica(p["id"], solo_se_sua)
        self._in_coda("pannello", chat, None)  # il pannello in chat lo aggiorna il bot

    def azione_dove(self, chat, p, dati):
        att = botmod.attuale_di(p)
        tipo = dati.get("tipo", "")
        if tipo == "sede":
            zona = {"tipo": "sede", "valore": att.luogo.sede}
        elif tipo == "comune":
            zona = {"tipo": "comune", "valore": cup_http.comune(att.luogo)}
        elif tipo == "provincia":
            zona = {"tipo": "provincia", "valore": cup_http.provincia(att.luogo)}
        elif tipo == "altro":
            comune = " ".join(dati.get("comune", "").split()).upper()
            if not botmod.COMUNE_RE.match(comune):
                raise Richiesta(400, "Scrivi il nome del comune, per esempio: Torino.")
            zona = {"tipo": "comune", "valore": comune}
        elif tipo == "tutte":
            zona = {"tipo": "tutte", "valore": ""}
        else:
            raise Richiesta(400, "Scelta non valida.")
        if zona["tipo"] != "tutte" and not zona["valore"]:
            raise Richiesta(400, "Non riesco a leggere questo dato dall'indirizzo della prenotazione.")
        self._modifica(chat, p, lambda f: (f.update(zona=zona), f.pop("stessa_sede", None), f.pop("attende_comune", None)))
        return f"{self.bot.nome(p)}: cerco {botmod.descr_zona(zona, att)}."

    def azione_auto(self, chat, p, dati):
        giorni = dati.get("giorni", "")
        if giorni not in {"0", *map(str, botmod.ANTICIPI_AUTO)}:
            raise Richiesta(400, "Scelta non valida.")
        self._modifica(chat, p, lambda f: f.update(auto={"giorni": int(giorni)} if int(giorni) else None))
        return f"{self.bot.nome(p)}: " + ("conferma automatica attiva." if int(giorni) else "conferma automatica spenta.")

    def azione_pausa(self, chat, p, dati):
        self._modifica(chat, p, lambda f: f.update(stato="pausa", pausa_da=time.time()))
        return f"{self.bot.nome(p)}: controlli in pausa."

    def azione_riprendi(self, chat, p, dati):
        def riprendi(f):
            f.update(stato="attivo", prossimo=time.time(), errori=0)
            f.pop("libera", None)
        try:
            self._modifica(chat, p, riprendi)
        except botmod.storemod.GiaRegistrata:
            raise Richiesta(409, "Questa ricetta e' stata registrata da un'altra chat: non posso riattivarla.")
        return f"{self.bot.nome(p)}: controlli riattivati."

    def azione_controlla(self, chat, p, dati):
        self._in_coda("controlla", chat, p["id"])
        return f"{self.bot.nome(p)}: controllo avviato. L'esito arriva qui e nel bot fra poco."

    def azione_offerta(self, chat, p, dati):
        tipo, token, indice, chiave = (dati.get("tipo", ""), dati.get("token", ""), dati.get("indice", ""),
                                       dati.get("slot", ""))
        if tipo not in ("p", "x") or not re.fullmatch(r"[0-9a-f]{8}", token) or \
                (tipo == "p" and (not indice.isdecimal() or not chiave)):
            raise Richiesta(400, "Richiesta non valida.")
        # con la chiave il bot prenota solo se la data e' ancora quella che l'utente ha confermato
        self._in_coda("offerta", chat, p["id"], tipo, token, indice, chiave if tipo == "p" else None)
        return (f"{self.bot.nome(p)}: sto spostando la prenotazione, ti scrivo nel bot l'esito." if tipo == "p"
                else f"{self.bot.nome(p)}: ok, non ti ripropongo queste date.")

    # --- pagine ----------------------------------------------------------------------
    def pagina(self):
        return """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Le tue ricette</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="/static/htmx.min.js"></script>
<script src="/static/app.js" defer></script>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<main id="ricette" hx-get="/ui/ricette" hx-trigger="load, every 15s, aggiorna" hx-swap="innerMorph">
  <p class="caricamento">Carico le tue ricette…</p>
</main>
<div id="velo" hidden></div>
<section id="foglio" hidden aria-live="polite"></section>
</body>
</html>"""

    def ricette(self, chat, avviso=""):
        pratiche = [p for p in self.store.della_chat(chat) if p["stato"] in ("attivo", "pausa")]
        testa = f'<p class="avviso" role="status">{e(avviso)}</p>' if avviso else ""
        if not pratiche:
            return testa + ('<div class="vuoto"><p>Non segui ancora nessuna ricetta.</p>'
                            '<p>Torna nel bot e scrivi <b>/start</b> (o <b>/aggiungi</b>).</p></div>')
        return testa + "".join(self.scheda(p) for p in pratiche)

    def scheda(self, p):
        att = botmod.attuale_di(p)
        pid, nome = p["id"], self.bot.nome(p)
        zona = botmod.zona_di(p)
        pausa = p["stato"] == "pausa"
        giorno, data, ora = botmod.GIORNI[att.quando.weekday()], f"{att.quando:%d/%m/%Y}", f"{att.quando:%H:%M}"
        estesa = ('<small>Allargo la ricerca a tutto il Piemonte, poi filtro.</small>'
                  if cup_http.estensioni(zona) else "")
        riassunto = self.bot.riassunto(p)
        return f"""
<article class="scheda{' in-pausa' if pausa else ''}" id="r-{pid}">
  <header>
    <h2>{e(nome)}</h2>
    <span class="stato">{'In pausa' if pausa else 'Attiva'}</span>
  </header>
  <p class="cosa">{e(botmod.prestazione(att.cosa, 80))}</p>
  <div class="quando"><span class="giorno">{e(giorno)}</span> <strong>{e(data)}</strong> · ore <strong>{e(ora)}</strong></div>
  <div class="dove">{e(botmod.titolo(att.luogo.sede))}<small>{e(att.luogo.ambulatorio)}<br>{e(botmod.indirizzo(att.luogo))}</small></div>
  {self.offerta(p)}
  <dl class="regole">
    <div><dt>🔎 Dove cerco</dt><dd>{e(botmod.descr_zona(zona, att))}{estesa}</dd></div>
    <div><dt>⚡ Prenoto da solo</dt><dd>{e(botmod.auto_descr(p))}</dd></div>
    <div><dt>⏱ Ultimo controllo</dt><dd>{e(riassunto.split(' ', 1)[1] if ' ' in riassunto else riassunto)}</dd></div>
  </dl>
  <nav class="azioni">
    <button type="button" hx-get="/ui/r/{pid}/dove" hx-target="#foglio">🔎 Dove</button>
    <button type="button" hx-get="/ui/r/{pid}/auto" hx-target="#foglio">⚡ Auto</button>
    <form hx-post="/ui/r/{pid}/{'riprendi' if pausa else 'pausa'}" hx-target="#ricette" hx-swap="innerMorph">
      <button>{'▶️ Riprendi' if pausa else '⏸ Pausa'}</button></form>
    <form hx-post="/ui/r/{pid}/controlla" hx-target="#ricette" hx-swap="innerMorph">
      <button>🔄 Ora</button></form>
  </nav>
</article>"""

    def offerta(self, p):
        o = self.bot.offerte.get(p["id"])
        if not o or time.time() - o["ts"] > botmod.TTL_OFFERTA:
            return ""
        scade = botmod.orario(o["ts"] + botmod.TTL_OFFERTA)
        voci = []
        for i, x in enumerate(o["slots"]):
            conferma = (f"Sposto la prenotazione di {self.bot.nome(p)} a {botmod.fmt(x.quando)}, "
                        f"{botmod.titolo(x.luogo.sede)}? La data attuale si perde.")
            voci.append(f"""
    <li><span class="quando">{e(botmod.fmt(x.quando))}</span><small>{e(botmod.titolo(x.luogo.sede))} · {e(botmod.indirizzo(x.luogo))}</small>
      <form action="/ui/r/{p['id']}/offerta" method="post" data-conferma="{e(conferma)}">
        <input type="hidden" name="tipo" value="p"><input type="hidden" name="token" value="{e(o['token'])}">
        <input type="hidden" name="indice" value="{i}"><input type="hidden" name="slot" value="{e(x.key())}">
        <button class="primario">Prenota</button></form></li>""")
        return f"""
  <section class="offerta">
    <h3>🎉 C’è una data prima</h3>
    <ul>{''.join(voci)}</ul>
    <form hx-post="/ui/r/{p['id']}/offerta" hx-target="#ricette" hx-swap="innerMorph">
      <input type="hidden" name="tipo" value="x"><input type="hidden" name="token" value="{e(o['token'])}">
      <button class="secondario">Ignora queste date</button></form>
    <small>Valida fino alle {scade:%H:%M}: la data è tenuta per te fino ad allora.</small>
  </section>"""

    def foglio_dove(self, p):
        att = botmod.attuale_di(p)
        z = botmod.zona_di(p)
        comune, prov = cup_http.comune(att.luogo), cup_http.provincia(att.luogo)
        scelte = [("sede", f"Solo in questa sede ({botmod.titolo(att.luogo.sede)})", True)]
        if comune:
            scelte.append(("comune", f"Solo nel comune di {botmod.titolo(comune)}", True))
        if prov:
            scelte.append(("provincia", f"In tutta la provincia ({prov})", True))
        scelte.append(("tutte", "Ovunque proponga il CUP", True))
        attuale_tipo = z["tipo"]
        if z["tipo"] == "comune" and z["valore"] and z["valore"] != comune:
            attuale_tipo = "altro"
        voci = "".join(
            f'<label class="scelta"><input type="radio" name="tipo" value="{t}"{" checked" if t == attuale_tipo else ""}>'
            f'<span>{e(testo)}</span></label>' for t, testo, _ in scelte)
        altro_val = botmod.titolo(z["valore"]) if attuale_tipo == "altro" else ""
        return f"""
<h2>🔎 Dove cerco · {e(self.bot.nome(p))}</h2>
<p class="nota">Prenotazione attuale: {e(botmod.titolo(att.luogo.sede))}, {e(botmod.indirizzo(att.luogo))}</p>
<form hx-post="/ui/r/{p['id']}/dove" hx-target="#ricette" hx-swap="innerMorph" class="scelte">
  {voci}
  <label class="scelta"><input type="radio" name="tipo" value="altro"{" checked" if attuale_tipo == "altro" else ""}>
    <span>Un altro comune <input type="text" name="comune" value="{e(altro_val)}" placeholder="es. Torino"
      autocomplete="off" maxlength="40"></span></label>
  <p class="nota">Comune e provincia allargano la ricerca a tutto il Piemonte: il controllo è più lento
    ma vede anche le altre aziende sanitarie.</p>
  <button class="primario">Salva</button>
</form>"""

    def foglio_auto(self, p):
        attivo = (p.get("auto") or {}).get("giorni", 0)
        oggi = botmod.adesso().date()
        voci = [("0", "No, chiedimi prima di prenotare")] + [
            (str(g), f"Sì, date da {botmod.GIORNI[(oggi + botmod.timedelta(days=g)).weekday()]} "
                     f"{oggi + botmod.timedelta(days=g):%d/%m} in poi") for g in botmod.ANTICIPI_AUTO]
        scelte = "".join(
            f'<label class="scelta"><input type="radio" name="giorni" value="{v}"{" checked" if int(v) == attivo else ""}>'
            f'<span>{e(t)}</span></label>' for v, t in voci)
        return f"""
<h2>⚡ Prenoto da solo · {e(self.bot.nome(p))}</h2>
<p class="nota">Quando trovo una data prima della prenotazione la prenoto subito, senza aspettare il tuo tocco:
  le date buone spariscono in pochi minuti. La data vecchia si perde; se poi non si può andare bisogna
  disdire almeno 2 giorni lavorativi prima, altrimenti si paga la prestazione.</p>
<form hx-post="/ui/r/{p['id']}/auto" hx-target="#ricette" hx-swap="innerMorph" class="scelte">
  {scelte}
  <button class="primario">Salva</button>
</form>"""


class _Gestore(BaseHTTPRequestHandler):
    app = None
    server_version = "cup"
    sys_version = ""
    timeout = 10  # secondi per ogni lettura/scrittura: un client lento non tiene occupato il server

    def _rispondi(self, metodo):
        try:
            lunghezza = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            lunghezza = -1
        if lunghezza < 0 or lunghezza > MAX_CORPO:
            self.send_error(413)
            return
        intestazioni = {k.lower(): v for k, v in self.headers.items()}
        try:
            corpo = self.rfile.read(lunghezza) if lunghezza else b""
            stato, h, testo = self.app.gestisci(metodo, self.path.split("?")[0], intestazioni, corpo)
        except Exception as ex:
            log.error("webapp: errore imprevisto %s", type(ex).__name__)
            stato, h, testo = 500, {"Content-Type": "text/plain; charset=utf-8"}, b"Errore interno"
        self.send_response(stato)
        for k, v in {**h, "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                     "Referrer-Policy": "no-referrer", "Content-Length": str(len(testo))}.items():
            self.send_header(k, v)
        if not h.get("Cache-Control"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(testo)

    def do_GET(self):
        self._rispondi("GET")

    def do_POST(self):
        self._rispondi("POST")

    def log_message(self, *args):
        pass  # niente log di accesso: gli indirizzi contengono id di ricette


def avvia(bot, db_path, key, porta=8095):
    """Server della Mini App in un thread suo, solo su 127.0.0.1 (davanti c'e' il reverse proxy)."""
    pronto = threading.Event()

    def servi():
        _Gestore.app = App(bot, db_path=db_path, key=key)  # ogni thread di richiesta apre la sua connessione
        server = ThreadingHTTPServer(("127.0.0.1", porta), _Gestore)
        server.daemon_threads = True
        pronto.set()
        log.info("Mini App in ascolto su 127.0.0.1:%d", porta)
        server.serve_forever()

    threading.Thread(target=servi, name="webapp", daemon=True).start()
    pronto.wait(10)
