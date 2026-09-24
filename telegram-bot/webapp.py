"""Mini App Telegram: la stessa gestione del bot in un'app che si apre dalla chat.

Gira nello stesso processo del bot, in un thread suo, e ascolta solo in locale (davanti c'e' il
reverse proxy con HTTPS). Chi apre l'app viene riconosciuto dalla firma che Telegram mette in
`initData`: la si verifica a ogni richiesta con il token del bot, niente password ne' cookie.

- Letture e impostazioni (dove cercare, automatica, pausa, nome, cancellazione) usano una connessione
  al database propria e `Store.modifica`, cosi' non sovrascrivono cio' che il bot scrive dopo un controllo.
- Le azioni che toccano il portale (cercare una ricetta, "controlla ora", "prenota") vanno nella coda
  del bot: e' lui che tiene le sessioni e le date bloccate, esattamente come per i pulsanti in chat.
"""
import hashlib
import hmac
import html
import json
import logging
import queue
import re
import secrets
import threading
import time
from datetime import datetime
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
MAX_ETA_PRENOTA = 2 * 3600  # per spostare o cancellare dati la firma dev'essere recente
MAX_CORPO = 4096
PAUSA_AZIONI = 1.5  # secondi minimi tra due azioni della stessa chat
TELEGRAM_JS = "https://telegram.org/js/telegram-web-app.js"
CSP = (f"default-src 'self'; script-src 'self' {TELEGRAM_JS}; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")
ATTIVE = ("attivo", "pausa")
AZIONI_POST = ("dove", "auto", "pausa", "riprendi", "controlla", "offerta", "vista", "nome", "cancella", "modifica")
FOGLI_GET = ("dove", "auto", "date", "storico", "altro")
AZIONI_SENSIBILI = ("offerta", "vista", "cancella", "modifica")  # vogliono una firma recente
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


def firma_da(intestazioni):
    """initData dall'intestazione standard `Authorization: tma <initData>` (i proxy la tolgono dai log)."""
    valore = intestazioni.get("authorization", "")
    return valore[4:] if valore.startswith("tma ") else ""


def nome_valido(testo):
    nome = " ".join(testo.split())[:20]
    compatto = "".join(nome.split()).upper()
    if nome.startswith("/") or cup_http.CF_RE.match(compatto) or cup_http.NRE_RE.match(compatto):
        return None
    return nome


def data_iso(testo):
    return datetime.fromisoformat(testo) if testo else None


class Richiesta(Exception):
    def __init__(self, stato, testo):
        super().__init__(testo)
        self.stato, self.testo = stato, testo


class App:
    def __init__(self, bot, store=None, db_path=None, key=None):
        """store: una connessione gia' pronta (test, un solo thread). In produzione db_path e key:
        ogni thread del server apre la sua connessione (sqlite non le condivide tra thread)."""
        self.bot, self._store, self._db = bot, store, (db_path, key)
        self._locale = threading.local()
        self._ultima_azione = {}  # chat -> ora dell'ultima azione (limite di frequenza)
        self._richieste = {}      # token di una ricerca in corso -> (chat, ora)
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
            raise Richiesta(503, "Il bot è molto occupato: riprova tra un minuto.")

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
            if metodo == "POST":
                self._limita(chat)
            # --- pagine della chat
            if metodo == "GET" and percorso == "/ui/ricette":
                return self._html(200, self.ricette(chat))
            if metodo == "GET" and percorso == "/ui/nuova":
                return self._html(200, self.foglio_nuova(chat))
            if metodo == "POST" and percorso == "/ui/nuova":
                return self._html(200, self.azione_nuova(chat, dati))
            m = re.fullmatch(r"/ui/esito/([0-9a-f]{16})", percorso)
            if metodo == "GET" and m:
                return self._html(200, self.esito(chat, m.group(1)))
            if metodo == "GET" and percorso == "/ui/dati":
                return self._html(200, self.foglio_dati(chat))
            if metodo == "POST" and percorso == "/ui/cancella-tutto":
                self._firma_recente(firma)
                return self._html(200, self.ricette(chat, self.azione_cancella_tutto(chat)))
            if metodo == "GET" and percorso == "/ui/admin":
                if not self.bot.admin or str(chat) != self.bot.admin:
                    raise Richiesta(404, "Pagina non trovata.")
                return self._html(200, self.foglio_admin())
            # --- pagine di una ricetta
            m = re.fullmatch(r"/ui/r/(\d+)/([a-z]+)", percorso)
            if not m or m.group(2) not in (AZIONI_POST if metodo == "POST" else FOGLI_GET):
                raise Richiesta(404, "Pagina non trovata.")
            pid, azione = int(m.group(1)), m.group(2)
            p = self.store.get(pid)
            # "dove" vale anche per la ricetta appena aggiunta dall'app, che aspetta proprio questa scelta
            stati = ATTIVE + ("sede",) if azione in ("dove", "cancella") else ATTIVE
            if not p or p["chat_id"] != chat or p["stato"] not in stati:
                raise Richiesta(404, "Ricetta non trovata.")
            if metodo == "GET":
                return self._html(200, getattr(self, "foglio_" + azione)(p))
            if azione in AZIONI_SENSIBILI:
                self._firma_recente(firma)
            risposta = getattr(self, "azione_" + azione)(chat, p, dati)
            if isinstance(risposta, tuple):  # ("foglio", html): la risposta resta nel foglio
                return self._html(200, risposta[1])
            return self._html(200, self.ricette(chat, risposta))
        except Richiesta as r:
            return self._html(r.stato, f'<p class="errore">{e(r.testo)}</p>')

    def _firma_recente(self, firma):
        if verifica_init_data(firma, self.bot.token, max_eta=MAX_ETA_PRENOTA) is None:
            raise Richiesta(401, "Per sicurezza chiudi e riapri l'app, poi riprova.")

    def _html(self, stato, testo):
        return stato, {"Content-Type": "text/html; charset=utf-8"}, testo.encode()

    def statico(self, nome):
        if nome not in FILE_STATICI:
            raise Richiesta(404, "File non trovato.")
        return 200, {"Content-Type": FILE_STATICI[nome], "Cache-Control": "public, max-age=3600"}, \
            (STATIC / nome).read_bytes()

    # --- azioni su una ricetta ---------------------------------------------------------
    def _modifica(self, chat, p, fn, stati=ATTIVE):
        def solo_se_sua(fresca):
            if fresca["chat_id"] != chat or fresca["stato"] not in stati:
                raise Richiesta(404, "Ricetta non trovata.")
            fn(fresca)
        self.store.modifica(p["id"], solo_se_sua)
        self._in_coda("pannello", chat, None)  # il pannello in chat lo aggiorna il bot

    def azione_dove(self, chat, p, dati):
        att = botmod.attuale_di(p)
        tipo = dati.get("tipo", "")
        luoghi = {l["sede"] for l in p.get("luoghi", [])} | {att.luogo.sede}
        if tipo == "sede":
            zona = {"tipo": "sede", "valore": att.luogo.sede}
        elif tipo == "sede_vista":
            sede = dati.get("sede", "")
            if sede not in luoghi:  # solo sedi che il portale ha davvero mostrato per questa ricetta
                raise Richiesta(400, "Scegli una sede dall'elenco.")
            zona = {"tipo": "sede", "valore": sede}
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

        def imposta(f):
            f.update(zona=zona)
            f.pop("stessa_sede", None)
            f.pop("attende_comune", None)
            if f["stato"] == "sede":  # ricetta appena aggiunta dall'app: da qui parte il primo controllo
                if not self._posto_libero(chat):  # connessione di questo thread, non quella del bot
                    raise Richiesta(409, "Mi dispiace, nel frattempo i posti sono finiti.")
                f.update(stato="attivo", prossimo=time.time() + 30)
        self._modifica(chat, p, imposta, stati=ATTIVE + ("sede",))
        return f"{self.bot.nome(p)}: cerco {botmod.descr_zona(zona, att)}."

    def _posto_libero(self, chat):
        gia_attiva = any(x["stato"] in ATTIVE for x in self.store.della_chat(chat))
        return gia_attiva or self.store.chat_count() < self.bot.max_utenti

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
            raise Richiesta(409, "Questa ricetta è stata registrata da un'altra chat: non posso riattivarla.")
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

    def azione_vista(self, chat, p, dati):
        chiave, attuale_vista = dati.get("slot", ""), dati.get("att", "")
        if not re.fullmatch(r"\d{12}\|[A-Z0-9]{1,300}", chiave):
            raise Richiesta(400, "Richiesta non valida.")
        try:
            datetime.fromisoformat(attuale_vista)
        except ValueError:
            raise Richiesta(400, "Richiesta non valida.")
        # la prenotazione mostrata all'utente viaggia con la richiesta: se nel frattempo e' cambiata, niente
        self._in_coda("vista", chat, p["id"], chiave, attuale_vista)
        return f"{self.bot.nome(p)}: sto spostando la prenotazione, ti scrivo nel bot l'esito."

    def azione_nome(self, chat, p, dati):
        nome = nome_valido(dati.get("nome", ""))
        if not nome:
            raise Richiesta(400, "Scrivi un nome breve, per esempio: Papà.")
        self._modifica(chat, p, lambda f: f.update(nome=nome))
        return f"Ora si chiama {nome}."

    def azione_cancella(self, chat, p, dati):
        vecchio = self.bot.nome(p)
        self.store.delete(p["id"])
        self._in_coda("dimentica", chat, p["id"])
        return f"Ho cancellato i dati di {vecchio}."

    def azione_modifica(self, chat, p, dati):
        return ("foglio", self._cerca(chat, dati, "modifica", p))

    # --- nuova ricetta o cambio di ricetta: la ricerca la fa il bot ------------------------
    def _cerca(self, chat, dati, modo, p=None):
        cf = "".join(dati.get("cf", "").split()).upper()
        nre = "".join(dati.get("nre", "").split()).upper()
        nome = ""
        if not cup_http.CF_RE.match(cf):
            return self._form_ricetta(chat, modo, p, "Il codice fiscale non sembra valido (16 caratteri).")
        if not cup_http.NRE_RE.match(nre):
            return self._form_ricetta(chat, modo, p, "Il numero ricetta deve avere 15 caratteri (es. 010A12345678901).")
        if modo == "nuova":
            pratiche = self.store.della_chat(chat)
            if not pratiche and dati.get("consenso") != "1":
                return self._form_ricetta(chat, modo, p, "Per continuare serve il consenso all'informativa.")
            nome = nome_valido(dati.get("nome", "")) if dati.get("nome", "").strip() else ""
            if nome is None:
                return self._form_ricetta(chat, modo, p, "Scegli un nome breve, per esempio: Papà.")
            if not nome and pratiche:
                nome = f"Ricetta {len(pratiche) + 1}"
        token = secrets.token_hex(8)
        with self._lock:
            ora = time.time()
            self._richieste = {k: v for k, v in self._richieste.items() if v[1] > ora - 900}
            if any(v[0] == chat and v[1] > ora - 300 for v in self._richieste.values()):
                raise Richiesta(429, "C'è già una ricerca in corso: aspetta il risultato.")
            self._richieste[token] = (chat, ora, modo, p["id"] if p else None)
        consenso = modo == "nuova" and dati.get("consenso") == "1"
        self._in_coda("cerca", chat, p["id"] if p else None, token, cf, nre, nome, modo, ora, consenso)
        return self._attesa(token)

    def azione_nuova(self, chat, dati):
        return self._cerca(chat, dati, "nuova")

    def _attesa(self, token):
        return f"""
<div class="attesa" hx-get="/ui/esito/{token}" hx-trigger="every 2s" hx-target="#foglio">
  <div class="rotella" aria-hidden="true"></div>
  <p>Cerco la prenotazione sul portale CUP…</p>
  <p class="nota">Di solito bastano pochi secondi; se il portale è lento anche un minuto.</p>
</div>"""

    def esito(self, chat, token):
        with self._lock:
            richiesta = self._richieste.get(token)
        if not richiesta or richiesta[0] != chat:
            raise Richiesta(404, "Ricerca non trovata.")
        r = self.bot.risultati.get(token)
        if not r:
            return self._attesa(token)
        with self._lock:
            self._richieste.pop(token, None)
        if r.get("errore"):
            _, _, modo, pid = richiesta
            vecchia = self.store.get(pid) if pid else None
            if modo == "modifica" and vecchia and vecchia["chat_id"] == chat:
                return self._form_ricetta(chat, "modifica", vecchia, r["errore"])
            return self._form_ricetta(chat, "nuova", None, r["errore"])
        p = self.store.get(r["pid"])
        if not p or p["chat_id"] != chat:
            raise Richiesta(404, "Ricetta non trovata.")
        att = botmod.attuale_di(p)
        return (f'<p class="avviso" role="status">Ho trovato la prenotazione di {e(self.bot.nome(p))}.</p>'
                f'<div class="trovata"><strong>{e(botmod.fmt(att.quando))}</strong><br>{e(botmod.titolo(att.luogo.sede))}'
                f'<small>{e(botmod.prestazione(att.cosa, 80))}</small></div>' + self.foglio_dove(p))

    # --- pagine ----------------------------------------------------------------------
    def pagina(self):
        return f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Le tue ricette</title>
<script src="{TELEGRAM_JS}"></script>
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

    def barra(self, chat, pratiche):
        pulsanti = []
        if len(pratiche) < self.bot.max_pratiche:
            pulsanti.append('<button type="button" hx-get="/ui/nuova" hx-target="#foglio">＋ Aggiungi</button>')
        pulsanti.append('<button type="button" hx-get="/ui/dati" hx-target="#foglio" aria-label="Dati e privacy">🔒</button>')
        if self.bot.admin and str(chat) == self.bot.admin:
            pulsanti.append('<button type="button" hx-get="/ui/admin" hx-target="#foglio" aria-label="Admin">⚙️</button>')
        return f'<header class="barra"><h1>Le tue ricette</h1><nav>{"".join(pulsanti)}</nav></header>'

    def ricette(self, chat, avviso=""):
        pratiche = self.store.della_chat(chat)
        visibili = [p for p in pratiche if p["stato"] in ATTIVE]
        in_attesa = [p for p in pratiche if p["stato"] == "sede" and p.get("attuale")]
        testa = self.barra(chat, pratiche) + (f'<p class="avviso" role="status">{e(avviso)}</p>' if avviso else "")
        testa += "".join(self.scheda_in_attesa(p) for p in in_attesa)
        if not visibili and in_attesa:
            return testa
        if not visibili:
            return testa + ('<div class="vuoto"><p>Non segui ancora nessuna ricetta.</p>'
                            '<button class="primario" type="button" hx-get="/ui/nuova" hx-target="#foglio">'
                            '＋ Aggiungi la prima ricetta</button></div>')
        return testa + "".join(self.scheda(p) for p in visibili)

    def scheda_in_attesa(self, p):
        att = botmod.attuale_di(p)
        conferma = f"Cancello i dati di {self.bot.nome(p)}?"
        return f"""
<article class="scheda in-attesa" id="r-{p['id']}">
  <header><h2>{e(self.bot.nome(p))}</h2><span class="stato">Da completare</span></header>
  <p class="cosa">{e(botmod.prestazione(att.cosa, 80))}</p>
  <div class="quando"><strong>{e(botmod.fmt(att.quando))}</strong></div>
  <div class="dove">{e(botmod.titolo(att.luogo.sede))}</div>
  <nav class="azioni secondarie">
    <button type="button" class="primario" hx-get="/ui/r/{p['id']}/dove" hx-target="#foglio">🔎 Scegli dove cercare</button>
    <form action="/ui/r/{p['id']}/cancella" method="post" data-conferma="{e(conferma)}"><button class="pericolo">Cancella</button></form>
  </nav>
</article>"""

    def prossimo(self, p):
        if p["stato"] == "pausa":
            return "in pausa"
        minuti = int((p.get("prossimo", 0) - time.time()) // 60)
        return "a momenti" if minuti < 1 else f"tra {minuti} min"

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
  {self.riga_date(p)}
  <dl class="regole">
    <div><dt>🔎 Dove cerco</dt><dd>{e(botmod.descr_zona(zona, att))}{estesa}</dd></div>
    <div><dt>⚡ Prenoto da solo</dt><dd>{e(botmod.auto_descr(p))}</dd></div>
    <div><dt>⏱ Ultimo controllo</dt><dd>{e(riassunto.split(' ', 1)[1] if ' ' in riassunto else riassunto)}</dd></div>
    <div><dt>⏭ Prossimo</dt><dd>{e(self.prossimo(p))}</dd></div>
  </dl>
  <nav class="azioni">
    <button type="button" hx-get="/ui/r/{pid}/dove" hx-target="#foglio">🔎 Dove</button>
    <button type="button" hx-get="/ui/r/{pid}/auto" hx-target="#foglio">⚡ Auto</button>
    <form hx-post="/ui/r/{pid}/{'riprendi' if pausa else 'pausa'}" hx-target="#ricette" hx-swap="innerMorph">
      <button>{'▶️ Riprendi' if pausa else '⏸ Pausa'}</button></form>
    <form hx-post="/ui/r/{pid}/controlla" hx-target="#ricette" hx-swap="innerMorph">
      <button>🔄 Ora</button></form>
  </nav>
  <nav class="azioni secondarie">
    <button type="button" hx-get="/ui/r/{pid}/date" hx-target="#foglio">📅 Date viste</button>
    <button type="button" hx-get="/ui/r/{pid}/storico" hx-target="#foglio">📈 Storico</button>
    <button type="button" hx-get="/ui/r/{pid}/altro" hx-target="#foglio">⋯ Altro</button>
  </nav>
</article>"""

    def riga_date(self, p):
        viste = [v for v in (p.get("viste") or []) if v.get("sel")]
        if not viste:
            return ""
        prima = min(viste, key=lambda v: v["q"])
        comune = botmod.titolo(cup_http.comune(cup_http.Luogo("", "", prima["ind"]))) or botmod.titolo(prima["sede"])
        return (f'<button type="button" class="riga-date" hx-get="/ui/r/{p["id"]}/date" hx-target="#foglio">'
                f'📅 {len(viste)} date disponibili · la prima: {e(botmod.fmt(data_iso(prima["q"])))} a {e(comune)} ›</button>')

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

    # --- fogli dal basso -------------------------------------------------------------
    def foglio_dove(self, p):
        att = botmod.attuale_di(p)
        z = botmod.zona_di(p) if p["stato"] != "sede" else {"tipo": "", "valore": ""}
        comune, prov = cup_http.comune(att.luogo), cup_http.provincia(att.luogo)
        luoghi = [l for l in p.get("luoghi", []) if l["sede"] != att.luogo.sede]
        scelte = [("sede", f"Solo in questa sede ({botmod.titolo(att.luogo.sede)})")]
        if comune:
            scelte.append(("comune", f"Solo nel comune di {botmod.titolo(comune)}"))
        if prov:
            scelte.append(("provincia", f"In tutta la provincia ({prov})"))
        scelte.append(("tutte", "Ovunque proponga il CUP"))
        scelto = z["tipo"]
        if z["tipo"] == "comune" and z["valore"] and z["valore"] != comune:
            scelto = "altro"
        if z["tipo"] == "sede" and z["valore"] and z["valore"] != att.luogo.sede:
            scelto = "sede_vista"
        voci = "".join(
            f'<label class="scelta"><input type="radio" name="tipo" value="{t}"{" checked" if t == scelto else ""}>'
            f'<span>{e(testo)}</span></label>' for t, testo in scelte)
        comuni = sorted({l["comune"] for l in p.get("luoghi", []) if l.get("comune")})
        lista_comuni = "".join(f'<option value="{e(botmod.titolo(c))}">' for c in comuni)
        altro_val = botmod.titolo(z["valore"]) if scelto == "altro" else ""
        sede_vista = ""
        if luoghi:
            opzioni = "".join(
                f'<option value="{e(l["sede"])}"{" selected" if scelto == "sede_vista" and l["sede"] == z["valore"] else ""}>'
                f'{e(botmod.titolo(l["sede"]))}{" · " + e(botmod.titolo(l["comune"])) if l.get("comune") else ""}</option>'
                for l in sorted(luoghi, key=lambda l: (l.get("comune", ""), l["sede"])))
            sede_vista = f"""
  <label class="scelta"><input type="radio" name="tipo" value="sede_vista"{" checked" if scelto == "sede_vista" else ""}>
    <span>Una sede vista nei controlli<select name="sede">{opzioni}</select></span></label>"""
        return f"""
<h2>🔎 Dove cerco · {e(self.bot.nome(p))}</h2>
<p class="nota">Prenotazione attuale: {e(botmod.titolo(att.luogo.sede))}, {e(botmod.indirizzo(att.luogo))}</p>
<form hx-post="/ui/r/{p['id']}/dove" hx-target="#ricette" hx-swap="innerMorph" class="scelte">
  {voci}{sede_vista}
  <label class="scelta"><input type="radio" name="tipo" value="altro"{" checked" if scelto == "altro" else ""}>
    <span>Un altro comune <input type="text" name="comune" value="{e(altro_val)}" placeholder="es. Torino"
      list="comuni-{p['id']}" autocomplete="off" maxlength="40"></span></label>
  <datalist id="comuni-{p['id']}">{lista_comuni}</datalist>
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

    def foglio_date(self, p):
        viste = p.get("viste") or []
        r = p.get("riassunto") or {}
        titolo = f"<h2>📅 Date viste · {e(self.bot.nome(p))}</h2>"
        if not viste:
            return titolo + '<p class="nota">Nessuna data ancora: arrivano con il prossimo controllo.</p>'
        quando = botmod.orario(r["ts"]).strftime("%H:%M") if r.get("ts") else ""
        gruppi = [("✅ Prima della tua, dove cerchi", [v for v in viste if v["ok"]]),
                  ("Dove cerchi, ma dopo la tua", [v for v in viste if v["area"] and not v["ok"]]),
                  ("Fuori da dove cerchi", [v for v in viste if not v["area"]])]
        s = self.bot.sessioni.get(p["id"])
        fresche = bool(s) and time.time() - s["ts"] <= botmod.TTL_OFFERTA
        if fresche:
            nota = (f"Ultimo controllo {e(quando)}: la prima data per ogni sede che il CUP propone. "
                    f"Puoi prenotarle fino alle {botmod.orario(s['ts'] + botmod.TTL_OFFERTA):%H:%M}.")
            aggiorna = ""
        else:
            nota = f"Ultimo controllo {e(quando)}: per prenotare una di queste date servono date fresche."
            aggiorna = (f'<form hx-post="/ui/r/{p["id"]}/controlla" hx-target="#ricette" hx-swap="innerMorph">'
                        f'<button class="primario">🔄 Aggiorna le date</button></form>')
        att = botmod.attuale_di(p)
        parti = [titolo, f'<p class="nota">{nota}</p>', aggiorna]
        for nome_gruppo, voci in gruppi:
            if not voci:
                continue
            righe = "".join(self.voce_vista(p, v, att, fresche) for v in voci)
            parti.append(f'<h3>{e(nome_gruppo)}</h3><ul class="elenco">{righe}</ul>')
        comuni = sorted({cup_http.comune(cup_http.Luogo("", "", v["ind"])) for v in viste} - {""})
        if comuni:
            bottoni = "".join(
                f'<form hx-post="/ui/r/{p["id"]}/dove" hx-target="#ricette" hx-swap="innerMorph">'
                f'<input type="hidden" name="tipo" value="altro"><input type="hidden" name="comune" value="{e(c)}">'
                f'<button class="chip">{e(botmod.titolo(c))}</button></form>' for c in comuni)
            parti.append(f'<h3>Cerca solo in uno di questi comuni</h3><div class="chips">{bottoni}</div>')
        return "".join(parti)

    def voce_vista(self, p, v, att, fresche):
        quando = data_iso(v["q"])
        luogo = cup_http.Luogo(v["sede"], v["amb"], v["ind"])
        testo = (f'<strong>{e(botmod.fmt(quando))}</strong>'
                 f'<small>{e(botmod.titolo(v["sede"]))} · {e(botmod.indirizzo(luogo))}</small>')
        if not (fresche and v.get("sel") and v.get("k")):
            return f"<li>{testo}</li>"
        if quando == att.quando:
            return f'<li>{testo}<small>Alla stessa ora della prenotazione attuale.</small></li>'
        rispetto = "PRIMA" if quando < att.quando else "DOPO"
        fuori = "" if v["area"] else ", fuori da dove cerchi"
        conferma = (f"Sposto la prenotazione di {self.bot.nome(p)} a {botmod.fmt(quando)}, "
                    f"{botmod.titolo(v['sede'])} ({botmod.indirizzo(luogo)})? È {rispetto} della data attuale "
                    f"({botmod.fmt(att.quando)}){fuori}. La data attuale si perde.")
        return (f'<li class="prenotabile">{testo}<form action="/ui/r/{p["id"]}/vista" method="post" '
                f'data-conferma="{e(conferma)}"><input type="hidden" name="slot" value="{e(v["k"])}">'
                f'<input type="hidden" name="att" value="{e(att.quando.isoformat())}">'
                f'<button class="{"primario" if rispetto == "PRIMA" else "secondario-pieno"}">Prenota</button></form></li>')

    def foglio_storico(self, p):
        storico = p.get("storico") or []
        att = botmod.attuale_di(p)
        titolo = f"<h2>📈 Storico · {e(self.bot.nome(p))}</h2>"
        punti = [(v["t"], data_iso(v["a"])) for v in storico]
        con_data = [(t, a) for t, a in punti if a]
        if not con_data:
            return titolo + (f'<p class="nota">Negli ultimi {botmod.STORICO_GIORNI} giorni nessuna data dove cerchi. '
                             "Lo storico si riempie a ogni controllo.</p>")
        ultimo = con_data[-1][1]
        migliore = min(a for _, a in con_data)
        testa = (f'<div class="numero"><span>Prima data dove cerchi, ora</span><strong>{e(botmod.fmt(ultimo))}</strong>'
                 f'<small>La tua prenotazione: {e(botmod.fmt(att.quando))}. La migliore vista negli ultimi '
                 f'{botmod.STORICO_GIORNI} giorni: {e(botmod.fmt(migliore))}.</small></div>')
        grafico = self.grafico_storico(punti, att.quando) if len(con_data) >= 2 else \
            '<p class="nota">Il grafico compare dal secondo controllo.</p>'
        cambi, prec = [], "x"
        for t, a in punti:
            if a != prec:
                cambi.append(f'<tr><td>{e(botmod.orario(t).strftime("%d/%m %H:%M"))}</td>'
                             f'<td>{e(botmod.fmt(a)) if a else "nessuna"}</td></tr>')
                prec = a
        tabella = (f'<details><summary>Tabella dei cambiamenti</summary><table><thead><tr><th>Controllo</th>'
                   f'<th>Prima data dove cerchi</th></tr></thead><tbody>{"".join(reversed(cambi[-30:]))}</tbody></table></details>')
        return titolo + testa + grafico + tabella

    def grafico_storico(self, punti, riferimento):
        """Linea a gradini della prima data utile nel tempo, con la prenotazione attuale tratteggiata.
        Una serie sola: niente legenda (il titolo la nomina); tooltip nativi sui punti; tabella sotto."""
        L, H, ml, mr, mt, mb = 340, 190, 58, 12, 14, 26
        t0, t1 = punti[0][0], max(punti[-1][0], punti[0][0] + 1)
        date = [a for _, a in punti if a] + [riferimento]
        d0, d1 = min(date), max(date)
        margine = max((d1 - d0) * 0.08, botmod.timedelta(days=1))
        d0, d1 = d0 - margine, d1 + margine
        x = lambda t: ml + (t - t0) / (t1 - t0) * (L - ml - mr)
        # piu' in alto = prima (meglio): la linea tratteggiata della prenotazione fa da riferimento
        y = lambda d: mt + (d - d0).total_seconds() / (d1 - d0).total_seconds() * (H - mt - mb)
        griglia, etichette = [], []
        for i in range(3):  # tre date sull'asse, dalla piu' vicina alla piu' lontana
            d = d0 + (d1 - d0) * (i + 0.5) / 3
            yy = y(d)
            griglia.append(f'<line class="griglia" x1="{ml}" x2="{L - mr}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
            etichette.append(f'<text class="asse" x="{ml - 6}" y="{yy + 4:.1f}" text-anchor="end">{d:%d/%m/%y}</text>')
        etichette.append(f'<text class="asse" x="{ml}" y="{H - 6}">{botmod.orario(t0):%d/%m}</text>')
        etichette.append(f'<text class="asse" x="{L - mr}" y="{H - 6}" text-anchor="end">{botmod.orario(t1):%d/%m %H:%M}</text>')
        tratti, corrente, marcatori = [], [], []
        for (t, a), succ in zip(punti, punti[1:] + [(t1, None)]):
            if a is None:
                if corrente:
                    tratti.append(corrente)
                corrente = []
                continue
            corrente += [(x(t), y(a)), (x(succ[0]), y(a))]
            marcatori.append(f'<circle class="punto" cx="{x(t):.1f}" cy="{y(a):.1f}" r="4">'
                             f'<title>{e(botmod.orario(t).strftime("%d/%m %H:%M"))}: {e(botmod.fmt(a))}</title></circle>')
        if corrente:
            tratti.append(corrente)
        linee = "".join('<polyline class="serie" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in tr) + '"/>'
                        for tr in tratti)
        yr = y(riferimento)
        t_ultimo, ultimo = [(t, a) for t, a in punti if a][-1]
        return f"""
<figure class="grafico">
  <figcaption>Prima data dove cerchi, a ogni controllo (più in alto = prima)</figcaption>
  <svg viewBox="0 0 {L} {H}" role="img" aria-label="Andamento della prima data utile rispetto alla prenotazione attuale">
    {"".join(griglia)}
    <line class="riferimento" x1="{ml}" x2="{L - mr}" y1="{yr:.1f}" y2="{yr:.1f}"/>
    <text class="etichetta" x="{L - mr}" y="{yr - 5:.1f}" text-anchor="end">tua prenotazione</text>
    {linee}{"".join(marcatori[-60:])}
    <text class="etichetta forte" x="{x(t_ultimo) - 8:.1f}" y="{y(ultimo) - 8:.1f}" text-anchor="end">{ultimo:%d/%m/%y}</text>
    {"".join(etichette)}
  </svg>
</figure>"""

    def foglio_altro(self, p):
        pid = p["id"]
        conferma = f"Cancello i dati di {self.bot.nome(p)}? I suoi controlli si fermano."
        return f"""
<h2>⋯ {e(self.bot.nome(p))}</h2>
<form hx-post="/ui/r/{pid}/nome" hx-target="#ricette" hx-swap="innerMorph" class="scelte">
  <label class="campo">Nome<input type="text" name="nome" value="{e(p.get('nome') or '')}" maxlength="20"
    placeholder="es. Papà" autocomplete="off" required></label>
  <button class="primario">Rinomina</button>
</form>
<h3>Cambia ricetta</h3>
<p class="nota">Per una nuova impegnativa della stessa persona (o di un'altra): cerco la prenotazione e la seguo al
  posto di quella attuale. La conferma automatica si spegne.</p>
<form hx-post="/ui/r/{pid}/modifica" hx-target="#foglio" class="scelte" data-resta>
  <label class="campo">Codice fiscale<input type="text" name="cf" maxlength="16" autocomplete="off"
    autocapitalize="characters" spellcheck="false" required></label>
  <label class="campo">Numero ricetta (NRE)<input type="text" name="nre" maxlength="15" autocomplete="off"
    autocapitalize="characters" spellcheck="false" required></label>
  <button class="primario">Cerca e sostituisci</button>
</form>
<h3>Elimina</h3>
<form action="/ui/r/{pid}/cancella" method="post" data-conferma="{e(conferma)}">
  <button class="pericolo">Cancella questa ricetta</button>
</form>"""

    def _form_ricetta(self, chat, modo, p=None, errore=""):
        if modo == "modifica" and p:
            return f'<p class="errore">{e(errore)}</p>' + self.foglio_altro(p)
        pratiche = self.store.della_chat(chat)
        consenso = "" if pratiche else f"""
  <details class="informativa"><summary>Informativa sui dati</summary><p>{e(self.bot.privacy()).replace(chr(10), "<br>")}</p></details>
  <label class="scelta"><input type="checkbox" name="consenso" value="1" required>
    <span>Ho letto l'informativa e do il consenso al trattamento di questi dati sanitari.</span></label>"""
        nome = "" if not pratiche else """
  <label class="campo">Nome nei messaggi<input type="text" name="nome" maxlength="20" placeholder="es. Papà"
    autocomplete="off"></label>"""
        return f"""
<h2>＋ Nuova ricetta</h2>
{f'<p class="errore">{e(errore)}</p>' if errore else ''}
<p class="nota">Tua o di un familiare che ti ha autorizzato. Codice fiscale e NRE li trovi sul promemoria della
  prenotazione; li uso solo per il portale CUP e li conservo cifrati.</p>
<form hx-post="/ui/nuova" hx-target="#foglio" class="scelte" data-resta>
  <label class="campo">Codice fiscale<input type="text" name="cf" maxlength="16" autocomplete="off"
    autocapitalize="characters" spellcheck="false" required></label>
  <label class="campo">Numero ricetta (NRE)<input type="text" name="nre" maxlength="15" autocomplete="off"
    autocapitalize="characters" spellcheck="false" placeholder="010A…" required></label>{nome}{consenso}
  <button class="primario">Cerca la prenotazione</button>
</form>"""

    def foglio_nuova(self, chat):
        if len(self.store.della_chat(chat)) >= self.bot.max_pratiche:
            return f'<p class="errore">Puoi seguire al massimo {self.bot.max_pratiche} ricette.</p>'
        return self._form_ricetta(chat, "nuova")

    def foglio_dati(self, chat):
        pratiche = [p for p in self.store.della_chat(chat) if p.get("cf")]
        righe = "".join(
            f'<li><strong>{e(self.bot.nome(p))}</strong><small>Codice fiscale {e(botmod.maschera(p.get("cf", "")))} · '
            f'Ricetta {e(botmod.maschera(p.get("nre", "")))}</small></li>' for p in pratiche)
        return f"""
<h2>🔒 Dati e privacy</h2>
<p class="nota">Conservo cifrati, per ogni ricetta, codice fiscale e NRE, la prenotazione e le date viste.
  Si cancellano da soli quando la data della prenotazione è passata.</p>
<ul class="elenco">{righe or '<li>Nessun dato.</li>'}</ul>
<details class="informativa"><summary>Informativa completa</summary><p>{e(self.bot.privacy()).replace(chr(10), "<br>")}</p></details>
<form action="/ui/cancella-tutto" method="post" data-conferma="Cancello tutte le tue ricette e tutti i tuoi dati? I controlli si fermano.">
  <button class="pericolo">Cancella tutti i miei dati</button>
</form>"""

    def azione_cancella_tutto(self, chat):
        # una ricerca ancora in coda non deve ricreare i dati appena cancellati
        self.bot.cancellate[chat] = time.time()
        with self._lock:
            self._richieste = {k: v for k, v in self._richieste.items() if v[0] != chat}
        pratiche = self.store.della_chat(chat)
        mid = self.store.pannello(chat)
        self.store.delete_chat(chat)
        for p in pratiche:
            self._in_coda("dimentica", chat, p["id"])
        if mid:
            self._in_coda("sgancia", chat, mid)
        return "Fatto: ho cancellato tutti i tuoi dati."

    def foglio_admin(self):
        s = self.store
        chat_attive = s.chat_count()
        per_stato = dict(s.db.execute("SELECT stato, COUNT(*) FROM pratiche GROUP BY stato").fetchall())
        ora = time.time()
        metriche = list(self.bot.metriche)

        def finestra(sec):
            m = [x for x in metriche if x[0] > ora - sec]
            durate = sorted(d for _, d, _ in m)
            errori = sum(1 for *_, ok in m if not ok)
            media = sum(durate) / len(durate) if durate else 0
            p95 = durate[int(len(durate) * 0.95) - 1] if len(durate) >= 20 else (durate[-1] if durate else 0)
            return len(m), errori, media, p95
        n1, e1, m1, p1 = finestra(3600)
        n24, e24, m24, p24 = finestra(86400)
        offerte = sum(1 for o in list(self.bot.offerte.values()) if ora - o["ts"] <= botmod.TTL_OFFERTA)

        def tile(valore, nome, nota=""):
            return f'<div class="tile"><strong>{e(str(valore))}</strong><span>{e(nome)}</span><small>{e(nota)}</small></div>'
        return f"""
<h2>⚙️ Admin</h2>
<div class="tiles">
  {tile(chat_attive, "chat attive", f"su {self.bot.max_utenti}")}
  {tile(per_stato.get("attivo", 0), "ricette attive", f"{per_stato.get('pausa', 0)} in pausa, "
        f"{sum(v for k, v in per_stato.items() if k not in ATTIVE)} in registrazione")}
  {tile(offerte, "offerte aperte")}
  {tile(self.bot.coda.qsize(), "azioni in coda")}
</div>
<h3>Portale, ultima ora</h3>
<div class="tiles">
  {tile(n1, "sessioni")}
  {tile(e1, "errori", f"{(e1 / n1 * 100 if n1 else 0):.0f}%")}
  {tile(f"{m1:.0f}s", "durata media")}
  {tile(f"{p1:.0f}s", "durata p95")}
</div>
<h3>Portale, ultime 24 ore</h3>
<div class="tiles">
  {tile(n24, "sessioni")}
  {tile(e24, "errori", f"{(e24 / n24 * 100 if n24 else 0):.0f}%")}
  {tile(f"{m24:.0f}s", "durata media")}
  {tile(f"{p24:.0f}s", "durata p95")}
</div>"""


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
