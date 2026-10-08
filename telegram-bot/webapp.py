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
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

import bot as botmod
import cup_http
import ricetta_pdf
import sonda as sonda_mod
from store import Store

log = logging.getLogger("cupbot.web")

STATIC = Path(__file__).resolve().parent / "web" / "static"
MAX_TESTO_COMUNI = 2000  # campo nascosto "comuni": COMUNI_MAX nomi lunghi con le virgole ci stanno
MAX_TESTO_SEDI = 4000  # campo nascosto "sedi": SEDI_MAX coppie [sede, comune] in JSON (nei nomi ci sono virgole)


def coppie_sedi(testo):
    """Campo "sedi" -> [{"sede", "comune"}]; None se non e' una lista JSON di coppie di stringhe."""
    if len(testo) > MAX_TESTO_SEDI:
        return None  # e niente json.loads su testi enormi (liste annidate: RecursionError)
    try:
        v = json.loads(testo or "[]")
    except ValueError:
        return None
    if not isinstance(v, list) or not all(isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) for y in x)
                                          for x in v):
        return None
    return [{"sede": s, "comune": c} for s, c in v]


def json_sedi(valore):
    """Come app.js: JSON compatto, cosi' il valore di una casella e' uguale alla sua voce nel campo nascosto."""
    return json.dumps(valore, ensure_ascii=False, separators=(",", ":"))
VICINI_KM = VICINI_MAX_KM = 25  # "Questi comuni": solo i comuni vicini al centro (di piu' e' mezza provincia)
FILE_STATICI = {"htmx.min.js": "text/javascript; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "app.css": "text/css; charset=utf-8"}
MAX_ETA_INITDATA = 24 * 3600  # secondi: oltre, Telegram deve rifirmare (basta riaprire l'app)
AUTO_SPOSTA = ("Quando trovo una data prima della prenotazione la prenoto subito, senza aspettare il tuo tocco: "
               "le date buone spariscono in pochi minuti. Prenoto solo nei giorni sì del calendario e mai per oggi. "
               "La data vecchia si perde; se poi non si può andare bisogna disdire almeno 2 giorni lavorativi prima, "
               "altrimenti si paga la prestazione.")
AUTO_NUOVA = ("La ricetta non è ancora prenotata: la prima data libera dove cerchi la prenoto subito, senza aspettare "
              "il tuo tocco, poi continuo a cercare date prima. Prenoto solo nei giorni sì del calendario e mai per "
              "oggi. Se poi non si può andare bisogna disdire almeno 2 giorni lavorativi prima, altrimenti si paga "
              "la prestazione.")
MAX_ETA_PRENOTA = 2 * 3600  # per spostare o cancellare dati la firma dev'essere recente
MAX_CORPO = 8192  # il calendario puo' mandare fino a MAX_NO_CAL date
PERCORSO_PDF = "/ui/ricetta-pdf"  # il PDF arriva come corpo grezzo: l'unico a poter superare MAX_CORPO
PAUSA_AZIONI = 1.5  # secondi minimi tra due azioni della stessa chat
TELEGRAM_JS = "https://telegram.org/js/telegram-web-app.js"
CSP = (f"default-src 'self'; script-src 'self' {TELEGRAM_JS}; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")
ATTIVE = ("attivo", "pausa")
AZIONI_POST = ("dove", "auto", "calendario", "pausa", "riprendi", "controlla", "offerta", "vista", "nome", "cancella", "modifica", "disdici")
FOGLI_GET = ("dove", "auto", "calendario", "date", "storico", "altro")
AZIONI_SENSIBILI = ("offerta", "vista", "cancella", "modifica", "disdici")  # vogliono una firma recente
MAX_NO_CAL = 400  # date segnate no al massimo nel calendario
GIORNI_CAL = 400  # il calendario arriva fino a tanti giorni da oggi
MESI_CAL = 12  # mesi dopo quello corrente sfogliabili nel calendario
PASSI = {"elenco": "Elenco prenotazioni", "ricerca": "Ricerca ricetta", "appuntamenti": "Appuntamenti",
         "estendi": "Estendi area", "riepilogo": "Riepilogo", "conferma": "Conferma",
         "verifica": "Verifica dopo la conferma"}  # passi del portale, nell'ordine del flusso
PASSI_GUIDA = {2: "Dove cercare", 3: "Giorni", 4: "Prenotazione automatica"}
ESITI = {"ok": "riuscita", "incerta": "esito incerto", "fallita": "non riuscita"}
e = html.escape


def versione_statico(nome, _cache={}):
    """Codice legato al contenuto del file: se cambia, cambia l'indirizzo e l'app di Telegram non usa
    la copia vecchia dalla cache (e' successo con app.css dopo un aggiornamento)."""
    if nome not in _cache:
        _cache[nome] = hashlib.sha256((STATIC / nome).read_bytes()).hexdigest()[:10]
    return _cache[nome]


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
        self._fermo = False  # il ciclo del bot e' fermo ed e' gia' stato segnalato

    @property
    def store(self):
        if self._store is not None:
            return self._store
        if not hasattr(self._locale, "store"):
            self._locale.store = Store(*self._db)
        return self._locale.store

    def fermo_da(self, ora=None):
        """Da quanti secondi il ciclo del bot non fa un giro ne' riceve risposte dal portale (solo lettura)."""
        return (ora or time.time()) - max(self.bot.battito, cup_http.ULTIMA)

    def controlla_battito(self, ora=None):
        """Dal thread del battito (ogni minuto): oltre BATTITO_MAX un avviso all'amministratore, e uno quando
        il ciclo riparte (uno per episodio)."""
        fermo = self.fermo_da(ora)
        with self._lock:
            avviso = None
            if fermo > botmod.BATTITO_MAX and not self._fermo:
                self._fermo = True
                avviso = (f"Il ciclo del bot e' fermo da {fermo / 60:.0f} minuti: controlli e prenotazioni non "
                          "partono. Forse serve un riavvio.")
            elif fermo <= botmod.BATTITO_MAX and self._fermo:
                self._fermo = False
                avviso = "✅ Il ciclo del bot e' ripartito."
        if avviso:
            log.warning("battito: %s", "fermo" if self._fermo else "ripartito")
            self.bot.alert_admin(avviso)
        return fermo

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
        percorso, _, query = percorso.partition("?")
        try:
            if metodo == "GET" and percorso == "/":
                return self._html(200, self.pagina())
            if metodo == "GET" and percorso.startswith("/static/"):
                return self.statico(percorso[len("/static/"):])
            firma = firma_da(intestazioni)
            chat = verifica_init_data(firma, self.bot.token)
            if chat is None:
                raise Richiesta(401, "Apri l'app dal pulsante del bot su Telegram.")
            if metodo == "POST" and percorso == PERCORSO_PDF:
                self._limita(chat)
                return self._html(200, self.azione_pdf(chat, corpo))
            dati = dict(parse_qsl(corpo.decode("utf-8", "replace"))) if corpo else {}
            if metodo == "GET":
                dati = dict(parse_qsl(query))  # solo il foglio "dove" li usa ("Centra qui")
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
            if metodo == "GET" and percorso == "/ui/portale":
                if not self.bot.admin or str(chat) != self.bot.admin:
                    raise Richiesta(404, "Pagina non trovata.")
                return self._html(200, self.foglio_portale())
            if metodo == "GET" and percorso == "/ui/admin":
                if not self.bot.admin or str(chat) != self.bot.admin:
                    raise Richiesta(404, "Pagina non trovata.")
                return self._html(200, self.foglio_admin(7 if dati.get("giorni") == "7" else 1))
            # --- pagine di una ricetta
            m = re.fullmatch(r"/ui/r/(\d+)/([a-z]+)", percorso)
            if not m or m.group(2) not in (AZIONI_POST if metodo == "POST" else FOGLI_GET):
                raise Richiesta(404, "Pagina non trovata.")
            pid, azione = int(m.group(1)), m.group(2)
            p = self.store.get(pid)
            # "dove" vale anche per la ricetta appena aggiunta dall'app, che aspetta proprio questa scelta
            guidata = azione in ("calendario", "auto") and App._guidata(p)
            stati = ATTIVE + ("sede",) if azione in ("dove", "cancella") or guidata else ATTIVE
            if not p or p["chat_id"] != chat or p["stato"] not in stati:
                raise Richiesta(404, "Ricetta non trovata.")
            if metodo == "GET":
                if azione == "dove":
                    return self._html(200, self.foglio_dove(p, dati))
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
        return 200, {"Content-Type": FILE_STATICI[nome], "Cache-Control": "public, max-age=31536000, immutable"}, \
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
        # (sede, comune) mostrate dal portale per questa ricetta: ci sono sedi omonime in comuni diversi
        luoghi = [(l["sede"], l.get("comune", "")) for l in p.get("luoghi", [])] + \
            ([(att.luogo.sede, cup_http.comune(att.luogo))] if att.luogo.sede else [])
        province = {l["prov"] for l in p.get("luoghi", []) if l.get("prov")}
        if tipo in ("sede", "comune", "provincia") and not att.luogo.sede:
            raise Richiesta(400, "Questa ricetta non è ancora prenotata: scegli un comune, una sede trovata o ovunque.")
        if tipo == "provincia_vista":
            prov = dati.get("prov", "").upper()
            if prov not in province:  # solo province che il portale ha davvero mostrato per questa ricetta
                raise Richiesta(400, "Scegli una provincia dall'elenco.")
            zona = {"tipo": "provincia", "valore": prov}
        elif tipo == "sede":
            zona = {"tipo": "sede", "valore": att.luogo.sede}
        elif tipo == "sede_vista":
            scelta = coppie_sedi(f'[{dati.get("sede", "")}]') or [{"sede": "", "comune": ""}]
            chiave = cup_http.chiave_sede(scelta[0]["sede"], scelta[0]["comune"])
            # solo sedi che il portale ha davvero mostrato per questa ricetta
            trovate = [(x, y) for x, y in luoghi if cup_http.chiave_sede(x, y) == chiave]
            if len(scelta) != 1 or not chiave[0] or not trovate:
                raise Richiesta(400, "Scegli una sede dall'elenco.")
            sede, comune = trovate[0]
            omonime = {cup_http.chiave_sede(x, y) for x, y in luoghi if cup_http._norm(x) == chiave[0]}
            # col solo nome varrebbe anche l'omonima di un altro comune: allora la zona e' la coppia
            zona = ({"tipo": "sedi", "valore": [{"sede": sede, "comune": comune}]} if len(omonime) > 1
                    else {"tipo": "sede", "valore": sede})
        elif tipo == "comune":
            zona = {"tipo": "comune", "valore": cup_http.comune(att.luogo)}
        elif tipo == "provincia":
            zona = {"tipo": "provincia", "valore": cup_http.provincia(att.luogo)}
        elif tipo == "altro":
            comune = " ".join(dati.get("comune", "").split()).upper()
            if not botmod.COMUNE_RE.match(comune):
                raise Richiesta(400, "Scrivi il nome del comune, per esempio: Torino.")
            zona = {"tipo": "comune", "valore": comune}
        elif tipo == "cintura":
            zona = {"tipo": "comuni", "valore": list(cup_http.CINTURA_TORINO)}
        elif tipo == "comuni":
            testo = dati.get("comuni", "")
            if len(testo) > MAX_TESTO_COMUNI:
                raise Richiesta(400, f"Al massimo {cup_http.COMUNI_MAX} comuni.")
            elenco, ignoti = cup_http.elenco_comuni(testo.split(","))
            if ignoti:
                raise Richiesta(400, "Non trovo tra i comuni del Piemonte: " + ", ".join(ignoti[:5]) +
                                (f" e altri {len(ignoti) - 5}" if len(ignoti) > 5 else "") +
                                ". Controlla come sono scritti e separali con una virgola.")
            if not elenco:
                raise Richiesta(400, "Scrivi i comuni separati da una virgola, per esempio: Torino, Moncalieri.")
            if len(elenco) > cup_http.COMUNI_MAX:
                raise Richiesta(400, f"Al massimo {cup_http.COMUNI_MAX} comuni: ne hai scritti {len(elenco)}.")
            zona = {"tipo": "comuni", "valore": elenco}
        elif tipo == "sedi":
            testo = dati.get("sedi", "")
            if len(testo) > MAX_TESTO_SEDI:
                raise Richiesta(400, f"L'elenco delle sedi è troppo lungo: al massimo {cup_http.SEDI_MAX} sedi.")
            elenco = coppie_sedi(testo)
            if elenco is None:
                raise Richiesta(400, "Scelta non valida.")
            coppie = {}
            for x in elenco:
                voce = {"sede": x["sede"].strip(), "comune": x["comune"].strip()}
                if cup_http._norm(voce["sede"]):
                    coppie.setdefault(cup_http.chiave_sede(voce["sede"], voce["comune"]), voce)
            elenco = list(coppie.values())
            gia = botmod.zona_di(p)
            gia = gia["valore"] if gia["tipo"] == "sedi" else []
            note = ({(cup_http._norm(x), k) for k, v in self.store.sedi_per_comune().items() for x in v}
                    | {cup_http.chiave_sede(x, y) for x, y in luoghi}
                    | {cup_http.chiave_sede(x["sede"], x["comune"]) for x in gia})
            # solo sedi che il bot ha visto nei controlli
            ignote = [x for x in elenco if cup_http.chiave_sede(x["sede"], x["comune"]) not in note]
            if ignote:
                nomi = [x["sede"] + (f" ({x['comune']})" if x["comune"] else "") for x in ignote]
                raise Richiesta(400, "Non trovo tra le sedi viste nei controlli: " + ", ".join(nomi[:3]) +
                                (f" e altre {len(nomi) - 3}" if len(nomi) > 3 else "") + ". Spuntale dall'elenco.")
            if not elenco:
                raise Richiesta(400, "Spunta almeno una sede.")
            if len(elenco) > cup_http.SEDI_MAX:
                raise Richiesta(400, f"Al massimo {cup_http.SEDI_MAX} sedi: ne hai spuntate {len(elenco)}.")
            zona = {"tipo": "sedi", "valore": elenco}
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
            if f["stato"] == "sede":  # ricetta appena aggiunta dall'app
                if not self._posto_libero(chat):  # connessione di questo thread, non quella del bot
                    raise Richiesta(409, "Mi dispiace, nel frattempo i posti sono finiti.")
                if not f.get("guida"):  # da qui parte il primo controllo (in guida solo all'ultimo passo)
                    f.update(stato="attivo", prossimo=time.time() + 30)
        self._modifica(chat, p, imposta, stati=ATTIVE + ("sede",))
        if self._guidata(p):  # il controllo parte solo all'ultimo passo
            return ("foglio", self.foglio_calendario(self.store.get(p["id"])))
        return f"{self.bot.nome(p)}: cerco {botmod.descr_zona(zona, att)}."

    def _posto_libero(self, chat):
        gia_attiva = any(x["stato"] in ATTIVE for x in self.store.della_chat(chat))
        return gia_attiva or self.store.chat_count() < self.bot.max_utenti

    def azione_auto(self, chat, p, dati):
        if dati.get("on") not in ("0", "1"):
            raise Richiesta(400, "Scelta non valida.")
        attiva = dati["on"] == "1"

        def imposta(f):
            botmod.fissa_calendario(f)  # i giorni minimi di prima restano nel calendario
            f["auto"] = {"on": True} if attiva else None
            if f.get("guida"):  # ultimo passo del percorso guidato: da qui parte il primo controllo
                if f["stato"] == "sede":
                    if not f.get("zona"):
                        raise Richiesta(400, "Prima scegli dove cercare.")
                    if not self._posto_libero(chat):
                        raise Richiesta(409, "Mi dispiace, nel frattempo i posti sono finiti.")
                    f.update(stato="attivo", prossimo=time.time() + 30)
                f.pop("guida")
        self._modifica(chat, p, imposta, stati=ATTIVE + ("sede",))
        if self._guidata(p):
            return (f"{self.bot.nome(p)}: cerco il primo appuntamento libero" +
                    (" e lo prenoto da solo." if attiva else " e ti avviso con il pulsante Prenota."))
        return f"{self.bot.nome(p)}: " + ("conferma automatica attiva." if attiva else "conferma automatica spenta.")

    def azione_calendario(self, chat, p, dati):
        """Il calendario dei giorni si'/no (vedi cup_http.giorno_si), tutto insieme dal foglio: no (date iso
        separate da virgole), no_settimana (0..6, lun=0), no_fino e si_fino (iso o vuoti). Le date passate si
        tolgono; quelle gia' no per il giorno della settimana, per no_fino o per si_fino anche."""
        oggi = botmod.adesso().date()
        ultimo = oggi + timedelta(days=GIORNI_CAL)

        def giorno(testo):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", testo):
                raise Richiesta(400, f"Data non valida: «{testo[:12]}».")
            try:
                d = date.fromisoformat(testo)
            except ValueError:
                raise Richiesta(400, f"Data non valida: «{testo[:12]}».") from None
            if d > ultimo:
                raise Richiesta(400, f"Il calendario arriva fino al {ultimo:%d/%m/%Y}.")
            return d
        no = [x for x in dati.get("no", "").split(",") if x]
        if len(no) > MAX_NO_CAL:
            raise Richiesta(400, f"Troppi giorni segnati no (al massimo {MAX_NO_CAL}): usa «No fino al» o i giorni "
                                 "della settimana.")
        no = {d for d in map(giorno, no) if d >= oggi}
        sett = [x for x in dati.get("no_settimana", "").split(",") if x]
        if any(x not in "0123456" or len(x) != 1 for x in sett):
            raise Richiesta(400, "Giorno della settimana non valido.")
        sett = sorted({int(x) for x in sett})
        no_fino = giorno(dati["no_fino"]) if dati.get("no_fino") else None
        no_fino = no_fino.isoformat() if no_fino and no_fino >= oggi else ""
        si_fino = giorno(dati["si_fino"]) if dati.get("si_fino") else None
        si_fino = max(si_fino, oggi - timedelta(days=1)).isoformat() if si_fino else ""
        cal = {"no": [], "no_settimana": sett, "no_fino": no_fino, "si_fino": si_fino}
        cal["no"] = sorted(d.isoformat() for d in no if cup_http.giorno_si(d, cal))
        cal = cal if any(cal.values()) else {}

        def imposta(f):
            botmod.fissa_calendario(f)  # toglie le regole di prima: da qui vale solo il calendario
            prima = dict(f["calendario"] or {})
            # Salva senza cambiare niente non toglie "solo anticipare" a un calendario delle regole di prima
            f["calendario"] = ({**cal, "solo_prima": True} if prima.pop("solo_prima", False) and prima == cal
                               else cal)
        self._modifica(chat, p, imposta, stati=ATTIVE + ("sede",))
        if self._guidata(p):
            return ("foglio", self.foglio_auto(self.store.get(p["id"])))
        descr = botmod.descr_calendario({"calendario": cal})
        return f"{self.bot.nome(p)}: " + (f"giorni {descr}." if descr.startswith("no:") else "tutti i giorni sì.")

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
        # le stesse regole di Bot.controlla_ora, qui subito: l'app non deve dire "avviato" a un controllo
        # che il bot poi rifiuta (il bot le riapplica comunque quando esegue la coda)
        if self.bot.offerta_valida(p["id"]):
            raise Richiesta(409, "C'è un'offerta aperta: prenotala o ignorala prima di un nuovo controllo.")
        ultimo = (p.get("ultimo") or {}).get("ts", 0)
        pausa = min(botmod.PAUSA_CONTROLLA, self.bot.intervallo_di(chat) * 60)
        if time.time() - ultimo < pausa:
            raise Richiesta(429, f"Ultimo controllo alle {botmod.orario(ultimo):%H:%M}: ogni controllo tiene "
                                 f"bloccata una data, il prossimo è possibile dalle {botmod.orario(ultimo + pausa):%H:%M}.")
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
        return (f"{self.bot.nome(p)}: {'sto prenotando' if botmod.da_prenotare(p) else 'sto spostando la prenotazione'}"
                ", ti scrivo nel bot l'esito." if tipo == "p"
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
        return (f"{self.bot.nome(p)}: {'sto prenotando' if botmod.da_prenotare(p) else 'sto spostando la prenotazione'}"
                ", ti scrivo nel bot l'esito.")

    def azione_disdici(self, chat, p, dati):
        att = botmod.attuale_di(p)
        if botmod.da_prenotare(p) or not att or dati.get("att", "") != att.quando.isoformat():
            raise Richiesta(409, "La prenotazione è cambiata: riapri la ricetta e riprova.")
        # la data che l'utente ha confermato viaggia con la richiesta: il bot disdice solo quella
        self._in_coda("disdici", chat, p["id"], att.quando.isoformat())
        return f"{self.bot.nome(p)}: sto disdicendo la prenotazione, ti scrivo nel bot l'esito."

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
        if botmod.da_prenotare(p):
            return (f'<p class="avviso" role="status">Ho trovato la ricetta di {e(self.bot.nome(p))}: non è ancora '
                    f'prenotata.</p><div class="trovata"><strong>{e(botmod.prestazione(att.cosa, 80) or "Prestazione della ricetta")}'
                    f'</strong><small>Cerco il primo appuntamento libero e ti avviso con il pulsante Prenota. Dopo la '
                    f'prenotazione continuo a cercare date ancora prima.</small></div>' + self.foglio_dove(p))
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
<script src="/static/htmx.min.js?v={versione_statico("htmx.min.js")}"></script>
<script src="/static/app.js?v={versione_statico("app.js")}" defer></script>
<link rel="stylesheet" href="/static/app.css?v={versione_statico("app.css")}">
</head>
<body>
<main id="ricette" hx-get="/ui/ricette" hx-trigger="load, every 15s, aggiorna" hx-swap="innerMorph"
  hx-sync="this:drop">
  <p class="caricamento">Carico le tue ricette…</p>
</main>
<div id="velo" hidden></div>
<section id="foglio" hidden aria-live="polite"></section>
</body>
</html>"""

    def barra(self, chat, pratiche):
        pulsanti = []
        if not self.bot.piena(len(pratiche)):
            pulsanti.append('<button type="button" hx-get="/ui/nuova" hx-target="#foglio" aria-label="Aggiungi una ricetta">＋<span class="lungo"> Aggiungi</span></button>')
        pulsanti.append('<button type="button" hx-get="/ui/dati" hx-target="#foglio" aria-label="Dati e privacy">🔒</button>')
        if self.bot.admin and str(chat) == self.bot.admin:
            pulsanti.append('<button type="button" hx-get="/ui/portale" hx-target="#foglio" aria-label="Stato del portale">📈</button>')
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
  {self.quando_dove(p, att)}
  <nav class="azioni secondarie">
    <button type="button" class="primario" hx-get="/ui/r/{p['id']}/dove" hx-target="#foglio">🔎 Scegli dove cercare</button>
    <form action="/ui/r/{p['id']}/cancella" method="post" data-conferma="{e(conferma)}"><button class="pericolo">Cancella</button></form>
  </nav>
</article>"""

    def quando_dove(self, p, att):
        """Data e luogo della prenotazione; per una ricetta mai prenotata, che non lo e' ancora."""
        if botmod.da_prenotare(p):
            return ('<div class="quando da-prenotare"><strong>Non ancora prenotata</strong></div>'
                    '<div class="dove"><small>Cerco il primo appuntamento libero dove scegli tu.</small></div>')
        giorno, data, ora = botmod.GIORNI[att.quando.weekday()], f"{att.quando:%d/%m/%Y}", f"{att.quando:%H:%M}"
        mancano = botmod.giorni_mancanti(att.quando, botmod.adesso())
        anticipo = botmod.riga_risparmio(p, att) if mancano else ""
        anticipo = f'<small>{e(anticipo)}</small>' if anticipo else ""
        mancano = f'<p class="mancano">{e(mancano.capitalize())}{anticipo}</p>' if mancano else ""
        return (f'<div class="quando"><span class="unito"><span class="giorno">{e(giorno)}</span> <strong>{e(data)}</strong>'
                f'</span> <span class="unito">· ore <strong>{e(ora)}</strong></span></div>{mancano}'
                f'<div class="dove">{e(botmod.titolo(att.luogo.sede))}<small>{e(att.luogo.ambulatorio)}<br>'
                f'{e(botmod.indirizzo(att.luogo))}</small></div>')

    def sospesa(self, p):
        """Prenotazione in corso (con data-in-corso: app.js aggiorna le schede ogni 5 s finche' c'e') o data
        nuova da verificare dopo una Conferma senza esito: data, ora e luogo, mai al posto della prenotazione."""
        s = botmod.sospesa(p)
        if not s:
            return ""
        tipo, quando, luogo, spiega = s
        dove = ", ".join(x for x in (botmod.titolo(luogo.sede), botmod.indirizzo(luogo)) if x)
        if tipo == "in_corso":
            return (f'<section class="in-corso" data-in-corso role="status"><div class="rotella" aria-hidden="true"></div>'
                    f'<div><strong>{e(quando)}</strong><small>{e(dove)}</small><small>{e(spiega)}</small></div></section>')
        return (f'<section class="da-verificare" role="status"><strong>⚠️ {e(quando)}</strong><small>{e(dove)}</small>'
                f'<small>{e(spiega)}</small></section>')

    def prossimo(self, p):
        if p["stato"] == "pausa":
            return "in pausa"
        minuti = int((p.get("prossimo", 0) - time.time()) // 60)
        return "a momenti" if minuti < 1 else f"tra {minuti} min"

    def note(self, p):
        """Le note del CUP dell'appuntamento (di solito il link alla preparazione all'esame), registrate quando il
        bot ha prenotato. I link http(s) si aprono; il resto e' testo con i caratteri speciali neutralizzati."""
        righe = botmod.righe_note(p)
        if not righe:
            return ""
        voci = [re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>',
                       e(r), flags=re.I) for r in righe]
        return ('<section class="note-cup"><strong>📝 Note del CUP</strong>'
                + "".join(f"<small>{v}</small>" for v in voci) + '</section>')

    def scheda(self, p):
        att = botmod.attuale_di(p)
        pid, nome = p["id"], self.bot.nome(p)
        zona = botmod.zona_di(p)
        pausa = p["stato"] == "pausa"
        estesa = ('<small>Allargo la ricerca a tutto il Piemonte, poi filtro.</small>'
                  if cup_http.estensioni(zona) else "")
        riassunto = self.bot.riassunto(p)
        return f"""
<article class="scheda{' in-pausa' if pausa else ''}" id="r-{pid}">
  <header>
    <h2>{e(nome)}</h2>
    <span class="stato">{'In pausa' if pausa else 'Da prenotare' if botmod.da_prenotare(p) else 'Attiva'}</span>
  </header>
  <p class="cosa">{e(botmod.prestazione(att.cosa, 80) or "Prestazione della ricetta")}</p>
  {self.quando_dove(p, att)}
  {self.sospesa(p)}
  {self.note(p)}
  {self.offerta(p)}
  {self.riga_date(p)}
  <div class="regole">
    <button type="button" class="regola" hx-get="/ui/r/{pid}/dove" hx-target="#foglio" aria-label="Cambia dove cerco">
      <span class="nome">🔎 Dove cerco</span><span class="valore">{e(botmod.descr_zona(zona, att))}{estesa}</span></button>
    <button type="button" class="regola" hx-get="/ui/r/{pid}/auto" hx-target="#foglio" aria-label="Cambia prenotazione automatica">
      <span class="nome">⚡ Prenoto da solo</span><span class="valore">{e(botmod.auto_descr(p))}</span></button>
    <button type="button" class="regola" hx-get="/ui/r/{pid}/calendario" hx-target="#foglio" aria-label="Cambia i giorni che vanno bene">
      <span class="nome">📅 Calendario</span><span class="valore">{e(botmod.descr_calendario(p))}</span></button>
    <div class="regola"><span class="nome">⏱ Ultimo controllo</span>
      <span class="valore">{e(riassunto.split(' ', 1)[1] if ' ' in riassunto else riassunto)}</span></div>
    <div class="regola"><span class="nome">⏭ Prossimo controllo</span><span class="valore">{e(self.prossimo(p))}</span></div>
  </div>
  <nav class="azioni">
    <form hx-post="/ui/r/{pid}/controlla" hx-target="#ricette" hx-swap="innerMorph">
      <button>🔄 Controlla ora</button></form>
    <form hx-post="/ui/r/{pid}/{'riprendi' if pausa else 'pausa'}" hx-target="#ricette" hx-swap="innerMorph">
      <button>{'▶️ Riprendi' if pausa else '⏸ Pausa'}</button></form>
    <button type="button" hx-get="/ui/r/{pid}/storico" hx-target="#foglio">📈 Andamento</button>
    <button type="button" hx-get="/ui/r/{pid}/altro" hx-target="#foglio">✏️ Modifica</button>
  </nav>
</article>"""

    def riga_date(self, p):
        tutte = p.get("viste") or []
        libere = [v for v in tutte if v.get("sel")]
        if not tutte:
            return ""
        if libere:
            prima = min(libere, key=lambda v: v["q"])
            comune = botmod.titolo(cup_http.comune(cup_http.Luogo("", "", prima["ind"]))) or botmod.titolo(prima["sede"])
            quante = "1 data disponibile" if len(libere) == 1 else f"{len(libere)} date disponibili"
            testo = f"📅 {quante} · la prima: {botmod.fmt(data_iso(prima['q']))} a {comune}"
        else:
            testo = f"📅 {len(tutte)} date proposte dal CUP, nessuna prenotabile"
        return (f'<button type="button" class="riga-date" hx-get="/ui/r/{p["id"]}/date" hx-target="#foglio">'
                f'{e(testo)} ›</button>')

    def offerta(self, p):
        o = self.bot.offerte.get(p["id"])
        if not o or time.time() - o["ts"] > botmod.TTL_OFFERTA:
            return ""
        scade = botmod.orario(o["ts"] + botmod.TTL_OFFERTA)
        voci, nuova = [], botmod.da_prenotare(p)
        att = botmod.attuale_di(p)
        piu_tardi = not nuova and any(x.quando > att.quando for x in o["slots"])  # prenotazione in un giorno no
        for i, x in enumerate(o["slots"]):
            conferma = (f"Prenoto {self.bot.nome(p)} il {botmod.fmt(x.quando)}, {botmod.titolo(x.luogo.sede)}? "
                        "Se poi non si può andare, va disdetta almeno 2 giorni lavorativi prima." if nuova else
                        f"Sposto la prenotazione di {self.bot.nome(p)} a {botmod.fmt(x.quando)}, "
                        f"{botmod.titolo(x.luogo.sede)}? La data attuale si perde.")
            voci.append(f"""
    <li><span class="quando">{e(botmod.fmt(x.quando))}</span><small>{e(botmod.titolo(x.luogo.sede))} · {e(botmod.indirizzo(x.luogo))}</small>
      <form action="/ui/r/{p['id']}/offerta" method="post" data-conferma="{e(conferma)}">
        <input type="hidden" name="tipo" value="p"><input type="hidden" name="token" value="{e(o['token'])}">
        <input type="hidden" name="indice" value="{i}"><input type="hidden" name="slot" value="{e(x.key())}">
        <button class="primario">Prenota</button></form></li>""")
        return f"""
  <section class="offerta">
    <h3>{'🎉 C’è una data libera' if nuova else '🎉 C’è una data nei giorni che vuoi' if piu_tardi
         else '🎉 C’è una data prima'}</h3>
    <ul>{''.join(voci)}</ul>
    <form hx-post="/ui/r/{p['id']}/offerta" hx-target="#ricette" hx-swap="innerMorph">
      <input type="hidden" name="tipo" value="x"><input type="hidden" name="token" value="{e(o['token'])}">
      <button class="secondario">Ignora queste date</button></form>
    <small>Valida fino alle {scade:%H:%M}: la data è tenuta per te fino ad allora.</small>
  </section>"""

    # --- fogli dal basso -------------------------------------------------------------
    @staticmethod
    def _guidata(p):
        """Percorso guidato in corso: ricetta nuova dall'app, ancora in "sede" (dopo l'ultimo passo e' attiva)."""
        return bool(p and p.get("guida") and p.get("stato") == "sede")

    def _passo(self, p, n):
        """Nel percorso guidato di una ricetta nuova (p["guida"]): il numero del passo."""
        return f'<p class="passo">Passo {n} di 4 · {PASSI_GUIDA[n]}</p>' if self._guidata(p) else ""

    def _dest(self, p, ultimo=False):
        """Dove va la risposta del form: nel percorso guidato resta nel foglio (il passo dopo), altrimenti
        aggiorna le schede e il foglio si chiude."""
        # l'ultimo passo risponde con le schede: il foglio si chiude
        if self._guidata(p) and not ultimo:
            return 'hx-target="#foglio" data-resta'
        return 'hx-target="#ricette" hx-swap="innerMorph"'

    def _avanti(self, p, ultimo):
        return ("Avvia la ricerca" if ultimo else "Avanti") if self._guidata(p) else "Salva"

    def foglio_dove(self, p, q=None):
        """q: i campi del foglio quando lo ricarica "Centra qui" (centro e spunte di "Questi comuni")."""
        q = q or {}
        att = botmod.attuale_di(p)
        z = botmod.zona_di(p) if p["stato"] != "sede" else {"tipo": "", "valore": ""}
        if botmod.da_prenotare(p):
            return self.foglio_dove_nuova(p, z, q)
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
        if z["tipo"] == "comuni" or "centro" in q:  # "centro": il foglio ricaricato da "Centra qui"
            scelto = "sedi" if q.get("tipo") == "sedi" else "comuni"
        voci = "".join(
            f'<label class="scelta"><input type="radio" name="tipo" value="{t}"{" checked" if t == scelto else ""}>'
            f'<span>{e(testo)}</span></label>' for t, testo in scelte)
        comuni = sorted({l["comune"] for l in p.get("luoghi", []) if l.get("comune")})
        lista_comuni = "".join(f'<option value="{e(botmod.titolo(c))}">' for c in comuni)
        altro_val = botmod.titolo(z["valore"]) if scelto == "altro" else ""
        sede_vista = ""
        if luoghi:
            opzioni = self.opzioni_sede_vista(luoghi, z, scelto)
            sede_vista = f"""
  <label class="scelta"><input type="radio" name="tipo" value="sede_vista"{" checked" if scelto == "sede_vista" else ""}>
    <span>Una sede trovata nei controlli<select name="sede">{opzioni}</select></span></label>"""
        return f"""
<h2>🔎 Dove cercare · {e(self.bot.nome(p))}</h2>
{self._passo(p, 2)}
<p class="nota">Prenotazione attuale: {e(botmod.titolo(att.luogo.sede))}, {e(botmod.indirizzo(att.luogo))}</p>
<form id="dove-{p['id']}" hx-post="/ui/r/{p['id']}/dove" {self._dest(p)} class="scelte">
  {voci}{sede_vista}
  <label class="scelta"><input type="radio" name="tipo" value="altro"{" checked" if scelto == "altro" else ""}>
    <span>Un altro comune <input type="text" name="comune" value="{e(altro_val)}" placeholder="es. Torino"
      list="comuni-{p['id']}" autocomplete="off" maxlength="40"></span></label>
  {self.voci_comuni(p, z, scelto, q)}{self.voci_sedi(p, z, scelto, q)}
  <datalist id="comuni-{p['id']}">{lista_comuni}</datalist>
  <p class="nota">Sedi scelte, comuni e provincia allargano la ricerca a tutto il Piemonte: il controllo è più lento
    ma vede anche le altre aziende sanitarie.</p>
  <button class="primario">{self._avanti(p, False)}</button>
</form>"""

    def opzioni_sede_vista(self, luoghi, z, scelto):
        """Le opzioni di "Una sede trovata nei controlli": il valore e' la coppia [sede, comune] (ci sono sedi
        omonime). Selezionata una sola: la prima col nome della zona "sede"."""
        ordinati = sorted(luoghi, key=lambda l: (l.get("comune", ""), l["sede"]))
        scelta = next((l for l in ordinati if scelto == "sede_vista" and l["sede"] == z["valore"]), None)
        return "".join(
            f'<option value="{e(json_sedi([l["sede"], l.get("comune", "")]))}"{" selected" if l is scelta else ""}>'
            f'{e(botmod.titolo(l["sede"]))}{" · " + e(botmod.titolo(l["comune"])) if l.get("comune") else ""}</option>'
            for l in ordinati)

    def foglio_dove_nuova(self, p, z, q):
        """Ricetta mai prenotata: nessuna sede di riferimento. Un comune, una provincia o una sede tra quelle
        trovate nei controlli, oppure dove propone il CUP."""
        luoghi = p.get("luoghi", [])
        province = sorted({l["prov"] for l in luoghi if l.get("prov")})
        comuni = sorted({l["comune"] for l in luoghi if l.get("comune")})
        scelto = {"comune": "altro", "sede": "sede_vista", "provincia": "provincia_vista"}.get(z["tipo"], z["tipo"])
        if z["tipo"] == "comuni" or "centro" in q:
            scelto = "sedi" if q.get("tipo") == "sedi" else "comuni"

        def voce(tipo, testo, extra=""):
            return (f'<label class="scelta"><input type="radio" name="tipo" value="{tipo}"'
                    f'{" checked" if tipo == scelto else ""}><span>{testo}{extra}</span></label>')
        parti = [voce("tutte", "Dove propone il CUP")]
        parti.append(voce("altro", "In un comune", f'<input type="text" name="comune" value="'
                          f'{e(botmod.titolo(z["valore"]) if scelto == "altro" else "")}" placeholder="es. Torino" '
                          f'list="comuni-{p["id"]}" autocomplete="off" maxlength="40">'))
        parti.append(self.voci_comuni(p, z, scelto, q))
        parti.append(self.voci_sedi(p, z, scelto, q))
        if province:
            opzioni = "".join(f'<option value="{e(v)}"{" selected" if scelto == "provincia_vista" and v == z["valore"] else ""}>'
                              f'{e(v)}</option>' for v in province)
            parti.append(voce("provincia_vista", "In una provincia trovata nei controlli", f'<select name="prov">{opzioni}</select>'))
        if luoghi:
            opzioni = self.opzioni_sede_vista(luoghi, z, scelto)
            parti.append(voce("sede_vista", "Una sede trovata nei controlli", f'<select name="sede">{opzioni}</select>'))
        lista = "".join(f'<option value="{e(botmod.titolo(c))}">' for c in comuni)
        return f"""
<h2>🔎 Dove cercare · {e(self.bot.nome(p))}</h2>
{self._passo(p, 2)}
<p class="nota">Ricetta non ancora prenotata: scegli dove cercare il primo appuntamento.{"" if luoghi else
  " Dopo il primo controllo qui compaiono anche le sedi e le province trovate."}</p>
<form id="dove-{p['id']}" hx-post="/ui/r/{p['id']}/dove" {self._dest(p)} class="scelte">
  {"".join(parti)}
  <datalist id="comuni-{p['id']}">{lista}</datalist>
  <p class="nota">Sedi scelte, comuni e provincia allargano la ricerca a tutto il Piemonte: il controllo è più lento
    ma vede anche le altre aziende sanitarie.</p>
  <button class="primario">{self._avanti(p, False)}</button>
</form>"""

    def centro_dove(self, p, q):
        """(comune scritto in "Centra qui", centro, [(comune, km)] di tutto il Piemonte dal centro). Centro:
        quello scritto, altrimenti il comune della prenotazione, altrimenti Torino."""
        att = botmod.attuale_di(p)
        scritto = " ".join(q.get("centro", "").split())
        proprio = "" if botmod.da_prenotare(p) else cup_http.comune(att.luogo)
        for nome in (scritto, proprio, "TORINO"):
            tutti = cup_http.vicini(nome, float("inf")) if nome else []
            if tutti:
                return scritto, tutti[0][0], tutti
        return scritto, "", []

    def voci_sedi(self, p, z, scelto, q):
        """La scelta "Sedi scelte": le sedi del registro (Store.sedi_per_comune) nei comuni entro VICINI_MAX_KM
        dal centro di "Questi comuni", dalla piu' vicina, piu' quelle gia' spuntate anche se lontane. Una sede e'
        la coppia sede + comune (ci sono sedi omonime). Le spunte stanno nel campo nascosto "sedi" (coppie
        [sede, comune] in JSON), che app.js aggiorna a ogni tocco."""
        _, centro, tutti = self.centro_dove(p, q)
        if "centro" in q:
            scelte = cup_http.zona_norm({"tipo": "sedi", "valore": coppie_sedi(q.get("sedi", "")) or []})["valore"]
        else:
            scelte = z["valore"] if z["tipo"] == "sedi" else []
        km = {cup_http._chiave_comune(n): d for n, d in tutti}
        righe = {}  # chiave_sede -> (km, sede, comune)
        for chiave, viste in self.store.sedi_per_comune().items():
            if km.get(chiave, float("inf")) <= VICINI_MAX_KM:
                for sede in viste:
                    righe[(cup_http._norm(sede), chiave)] = (km[chiave], sede, cup_http.NOMI_COMUNI[chiave])
        for x in scelte:
            k = cup_http.chiave_sede(x["sede"], x["comune"])
            righe[k] = (km.get(k[1], float("inf")), x["sede"], x["comune"])  # la coppia salvata, com'e'
        spuntate = {cup_http.chiave_sede(x["sede"], x["comune"]) for x in scelte}
        voci = "".join(
            f'<label class="cm"><input type="checkbox" value="{e(json_sedi([sede, comune]))}"'
            f'{" checked" if k in spuntate else ""}><span>{e(botmod.titolo(sede))}'
            f'<em class="cm-sedi">{e(botmod.titolo(comune) or "comune non indicato")}</em></span>'
            f'<small>{"" if d == float("inf") else "centro" if d == 0 else f"{d:.0f} km"}</small></label>'
            for k, (d, sede, comune) in sorted(righe.items(), key=lambda x: (x[1][0], x[1][2], x[1][1])))
        vuoto = "" if voci else (f'<p class="nota">Il bot non ha ancora visto sedi entro {VICINI_MAX_KM} km da '
                                 f'{e(botmod.titolo(centro))}: dopo i primi controlli compaiono qui.</p>')
        return f"""
  <label class="scelta"><input type="radio" name="tipo" value="sedi"{" checked" if scelto == "sedi" else ""}>
    <span>Sedi scelte<small>Spunta le sedi in cui cercare, al massimo {cup_http.SEDI_MAX}: quelle viste dai
      controlli entro {VICINI_MAX_KM} km da {e(botmod.titolo(centro))}, più quelle già scelte.</small></span></label>
  <div class="comuni-scelta">
    <input type="hidden" name="sedi" value="{e(json_sedi([[x["sede"], x["comune"]] for x in scelte]))}">{vuoto}
    <div class="cm-elenco">{voci}</div>
  </div>"""

    def voci_comuni(self, p, z, scelto, q):
        """La scelta "Questi comuni": i comuni vicini a un centro, dal piu' vicino, da spuntare (entro VICINI_KM,
        fino a VICINI_MAX_KM con "Mostra"). Le spunte stanno nel campo nascosto "comuni", che app.js aggiorna a
        ogni tocco: cosi' restano anche per i comuni nascosti o lontani. Centro: quello scelto con "Centra
        qui", altrimenti il comune della prenotazione, altrimenti Torino. Solo i comuni con sedi nel registro
        (Store.sedi_per_comune, di tutte le ricette) e quelli gia' spuntati; i comuni del preset senza sedi
        sono righe nascoste che app.js mostra al tocco di "Torino e prima cintura"."""
        scritto, centro, tutti = self.centro_dove(p, q)
        avviso = (f'<p class="errore">Non trovo «{e(scritto)}» tra i comuni del Piemonte: '
                  f'centro su {e(botmod.titolo(centro))}.</p>' if scritto and not cup_http.elenco_comuni([scritto])[0]
                  else "")
        if "centro" in q:
            scelti = cup_http.elenco_comuni(q.get("comuni", "").split(","))[0]
        else:
            scelti = cup_http.elenco_comuni(z["valore"])[0] if z["tipo"] == "comuni" else []
        sedi = self.store.sedi_per_comune()  # registro di tutte le ricette: {chiave del comune: [sedi]}
        cintura = set(cup_http.CINTURA_TORINO)
        # il preset solo se la cintura e' tutta vicina al centro
        con_preset = {n for n, d in tutti if d <= VICINI_MAX_KM} >= cintura
        nascosti = 0
        voci = []
        for n, d in tutti:
            viste = sedi.get(cup_http._chiave_comune(n), [])
            if n in scelti or (viste and d <= VICINI_MAX_KM):
                nascosto = d > VICINI_KM and n not in scelti
                preset = False
            elif con_preset and n in cintura:  # nascosta: la mostra "Torino e prima cintura" (app.js)
                nascosto = preset = True
            else:
                continue
            nascosti += nascosto and not preset
            if not viste:
                sedi_testo, titolo = "nessuna sede vista", ""
            else:
                prima = botmod.titolo(viste[0])
                sedi_testo = (f"🏥 {len(viste)} sedi" if len(viste) > 1 else
                              "🏥 " + (prima if len(prima) <= 32 else prima[:31].rstrip() + "…"))
                titolo = (' title="' + e(", ".join(botmod.titolo(s) for s in viste[:12]) +
                                         (" …" if len(viste) > 12 else "")) + '"')
            voci.append(f'<label class="cm"{" hidden" if nascosto else ""}{" data-preset" if preset else ""}'
                        f'{titolo}><input type="checkbox" value="{e(n)}"'
                        f'{" checked" if n in scelti else ""}><span>{e(botmod.titolo(n))}'
                        f'<em class="cm-sedi">{e(sedi_testo)}</em></span>'
                        f'<small>{"centro" if n == centro else f"{d:.0f} km"}</small></label>')
        preset = (f'<button type="button" class="cm-preset" data-comuni="{e(",".join(cup_http.CINTURA_TORINO))}">'
                  f'＋ Torino e prima cintura</button>' if con_preset else "")
        altri = (f'<button type="button" class="cm-altri">Mostra fino a {VICINI_MAX_KM} km</button>'
                 if nascosti else "")
        vuoto = ""
        if not any(d <= VICINI_KM and cup_http._chiave_comune(n) in sedi for n, d in tutti):
            vuoto = (f'<p class="nota">Il bot non ha ancora visto sedi entro {VICINI_KM} km da '
                     f'{e(botmod.titolo(centro))}: dopo i primi controlli compaiono qui. Intanto puoi usare '
                     f'{"«Torino e prima cintura» o " if con_preset else ""}un altro comune.</p>')
        tutti_nomi = "".join(f'<option value="{e(botmod.titolo(n))}">' for n in sorted(cup_http.NOMI_COMUNI.values()))
        return f"""
  <label class="scelta"><input type="radio" name="tipo" value="comuni"{" checked" if scelto == "comuni" else ""}>
    <span>Questi comuni<small>Spunta i comuni in cui cercare, al massimo {cup_http.COMUNI_MAX}. Qui compaiono i
      comuni entro {VICINI_KM} km in cui i controlli hanno visto sedi del CUP (🏥), più quelli già scelti.</small></span></label>
  <div class="comuni-scelta">
    <input type="hidden" name="comuni" value="{e(",".join(scelti))}">
    <div class="cm-centro"><input type="text" name="centro" value="{e(botmod.titolo(centro))}" list="tutti-{p['id']}"
      autocomplete="off" maxlength="40" aria-label="Comune al centro dell'elenco" enterkeyhint="go">
      <button type="button" hx-get="/ui/r/{p['id']}/dove" hx-include="#dove-{p['id']}" hx-target="#foglio">Centra qui</button></div>
    {avviso}{preset}{vuoto}
    <div class="cm-elenco">{"".join(voci)}</div>
    {altri}
  </div>
  <datalist id="tutti-{p['id']}">{tutti_nomi}</datalist>"""

    def foglio_auto(self, p):
        attiva = bool(p.get("auto"))
        voci = [("1", "Sì, prenoto da solo nei giorni sì del calendario"), ("0", "No, chiedimi prima di prenotare")]
        scelte = "".join(
            f'<label class="scelta"><input type="radio" name="on" value="{v}"{" checked" if (v == "1") == attiva else ""}>'
            f'<span>{e(t)}</span></label>' for v, t in voci)
        return f"""
<h2>⚡ Prenoto da solo · {e(self.bot.nome(p))}</h2>
{self._passo(p, 4)}
<p class="nota">{e(AUTO_NUOVA if botmod.da_prenotare(p) else AUTO_SPOSTA)}</p>
<form hx-post="/ui/r/{p['id']}/auto" {self._dest(p, True)} class="scelte">
  {scelte}
  <p class="nota">{"Riepilogo: cerco " + e(botmod.descr_zona(botmod.zona_di(p), botmod.attuale_di(p))) + ". " if self._guidata(p) else ""}Calendario: {e(botmod.descr_calendario(p))}.</p>
  <button class="primario">{self._avanti(p, True)}</button>
</form>"""

    def foglio_calendario(self, p):
        """Griglia dei mesi (da quello corrente a MESI_CAL dopo) con i giorni si'/no. Lo stato sta nei campi
        nascosti del form e lo cambia app.js a ogni tocco (senza una richiesta per tocco): si salva con Salva.
        Le classi dei giorni qui sono gia' giuste, app.js le ricalcola dopo ogni tocco."""
        cal = botmod.calendario_di(p) or {}
        oggi = botmod.adesso().date()
        ultimo = oggi + timedelta(days=GIORNI_CAL)
        nuova = botmod.da_prenotare(p)
        att = None if nuova else botmod.attuale_di(p)
        viste = {}
        for v in p.get("viste") or []:
            q = v["q"][:10]
            viste[q] = viste.get(q) or bool(v.get("ok"))
        # solo i giorni da oggi in poi: le date passate non servono piu'
        no = [d for d in cal.get("no") or [] if d >= oggi.isoformat()]
        sett = sorted(cal.get("no_settimana") or [])
        mesi_nomi = list(cup_http.MESI)
        mesi = []
        for i in range(MESI_CAL + 1):
            anno, mese = oggi.year + (oggi.month - 1 + i) // 12, (oggi.month - 1 + i) % 12 + 1
            primo = date(anno, mese, 1)
            dopo = date(anno + mese // 12, mese % 12 + 1, 1)
            celle = [f'<button type="button" class="cal-sett{" no" if w in sett else ""}" data-w="{w}" '
                     f'aria-pressed="{"true" if w in sett else "false"}" aria-label="Tutti i {nome}: sì o no">{nome}</button>'
                     for w, nome in enumerate(botmod.GIORNI)]
            # nel mese corrente le settimane gia' passate del tutto non si mostrano
            d = max(primo, oggi - timedelta(days=oggi.weekday())) if i == 0 else primo
            celle += ['<span class="cal-vuoto"></span>'] * d.weekday()
            while d < dopo:
                segni = ""
                if att and d == att.quando.date():
                    segni += '<i class="pin" aria-hidden="true">📌</i>'
                if d.isoformat() in viste:
                    segni += f'<i class="vista{" buona" if viste[d.isoformat()] else ""}" aria-hidden="true"></i>'
                extra = " (la tua prenotazione)" if att and d == att.quando.date() else ""
                extra += " (data trovata)" if d.isoformat() in viste else ""
                if d < oggi or d > ultimo:
                    celle.append(f'<span class="g passato" aria-label="{d:%d/%m}{extra}">{d.day}{segni}</span>')
                else:
                    si = cup_http.giorno_si(d, {**cal, "no": no})
                    fisso = si == cup_http.giorno_si(d, {**cal, "no": []})  # no per settimana, "fino al"...: non il singolo
                    classi = "g " + ("si" if si else "no") + ("" if si or not fisso else " fisso") + \
                        (" oggi" if d == oggi else "")
                    celle.append(f'<button type="button" class="{classi}" data-d="{d.isoformat()}" data-w="{d.weekday()}" '
                                 f'aria-pressed="{"false" if si else "true"}" '
                                 f'aria-label="{botmod.GIORNI[d.weekday()]} {d:%d/%m}: {"sì" if si else "no"}{extra}">'
                                 f'{d.day}{segni}</button>')
                d += timedelta(days=1)
            mesi.append(f"""
    <section class="cal-mese" data-i="{i}"{"" if i == 0 else " hidden"}>
      <header class="cal-testa">
        <button type="button" class="cal-prec" aria-label="Mese prima"{" disabled" if i == 0 else ""}>◀</button>
        <h3>{mesi_nomi[mese - 1].capitalize()} {anno}</h3>
        <button type="button" class="cal-succ" aria-label="Mese dopo"{" disabled" if i == MESI_CAL else ""}>▶</button>
      </header>
      <div class="cal-griglia">{"".join(celle)}</div>
    </section>""")
        spiega = ("Ricetta non ancora prenotata: prenoto solo in un giorno sì." if nuova else
                  "Sposto la prenotazione solo su un giorno sì, prima della tua. Se la tua è in un giorno no, "
                  "va bene anche un giorno sì più tardi.")
        return f"""
<h2>📅 Calendario · {e(self.bot.nome(p))}</h2>
{self._passo(p, 3)}
<p class="nota">{e(spiega)} La prenotazione automatica non prenota mai per oggi.</p>
<form hx-post="/ui/r/{p['id']}/calendario" {self._dest(p)} class="calendario"
  data-oggi="{oggi.isoformat()}" data-ultimo="{ultimo.isoformat()}">
  <input type="hidden" name="no" value="{e(",".join(no))}">
  <input type="hidden" name="no_settimana" value="{e(",".join(map(str, sett)))}">
  <input type="hidden" name="no_fino" value="{e(cal.get("no_fino") or "")}">
  <input type="hidden" name="si_fino" value="{e(cal.get("si_fino") or "")}">
  <div class="cal-strumenti">
    <button type="button" class="cal-tutti">Tutti sì</button>
    <button type="button" class="cal-fino" aria-pressed="false">No fino al giorno…</button>
  </div>
  <p class="nota cal-aiuto" aria-live="polite">Tocca un giorno per passarlo da sì a no e ritorno; tocca il nome
    del giorno in alto per tutti quei giorni.</p>
  {"".join(mesi)}
  <p class="nota cal-legenda"><span class="cal-campione si">sì</span> <span class="cal-campione no">no</span>
    {"<span>📌 la tua prenotazione</span>" if att else ""}
    <span><i class="vista buona" aria-hidden="true"></i> data buona trovata</span>
    <span><i class="vista" aria-hidden="true"></i> altra data trovata</span></p>
  <button class="primario">{self._avanti(p, False)}</button>
</form>"""

    def foglio_date(self, p):
        viste = p.get("viste") or []
        r = p.get("riassunto") or {}
        titolo = f"<h2>📅 Date disponibili · {e(self.bot.nome(p))}</h2>"
        if not viste:
            return titolo + '<p class="nota">Nessuna data ancora: arrivano con il prossimo controllo.</p>'
        quando = botmod.orario(r["ts"]).strftime("%H:%M") if r.get("ts") else ""
        gruppi = botmod.gruppi_date(p, viste)
        s = self.bot.sessioni.get(p["id"])
        fresche = bool(s) and time.time() - s["ts"] <= botmod.TTL_OFFERTA
        if fresche:
            nota = (f"Trovate dal controllo delle {e(quando)}: per ogni sede, la prima data che il CUP propone. "
                    f"Si possono prenotare fino alle {botmod.orario(s['ts'] + botmod.TTL_OFFERTA):%H:%M}.")
            aggiorna = ""
        else:
            nota = (f"Trovate dal controllo delle {e(quando)}: sono passati più di "
                    f"{botmod.TTL_OFFERTA // 60} minuti e il CUP potrebbe averle già date ad altri. "
                    f"Per prenotarne una serve un controllo nuovo.")
            aggiorna = (f'<form hx-post="/ui/r/{p["id"]}/controlla" hx-target="#ricette" hx-swap="innerMorph">'
                        f'<button class="primario">🔄 Controlla ora</button></form>')
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
        fuori = "" if v["area"] else ", fuori dalla zona in cui cerchi"
        if botmod.da_prenotare(p):
            conferma = (f"Prenoto {self.bot.nome(p)} il {botmod.fmt(quando)}, {botmod.titolo(v['sede'])} "
                        f"({botmod.indirizzo(luogo)}){' (fuori dalla zona in cui cerchi)' if fuori else ''}? "
                        "Se poi non si può andare, va disdetta almeno 2 giorni lavorativi prima.")
        else:
            conferma = (f"Sposto la prenotazione di {self.bot.nome(p)} a {botmod.fmt(quando)}, "
                        f"{botmod.titolo(v['sede'])} ({botmod.indirizzo(luogo)})? È {rispetto} della data attuale "
                        f"({botmod.fmt(att.quando)}){fuori}. La data attuale si perde.")
        return (f'<li class="prenotabile">{testo}<form action="/ui/r/{p["id"]}/vista" method="post" '
                f'data-conferma="{e(conferma)}"><input type="hidden" name="slot" value="{e(v["k"])}">'
                f'<input type="hidden" name="att" value="{e(att.quando.isoformat())}">'
                f'<button class="{"primario" if rispetto == "PRIMA" or v["ok"] else "secondario-pieno"}">Prenota</button></form></li>')

    def foglio_storico(self, p):
        storico = p.get("storico") or []
        att = botmod.attuale_di(p)
        titolo = f"<h2>📈 Andamento · {e(self.bot.nome(p))}</h2>"
        punti = [(v["t"], data_iso(v["a"])) for v in storico]
        con_data = [(t, a) for t, a in punti if a]
        if not con_data:
            return titolo + (f'<p class="nota">Negli ultimi {botmod.STORICO_GIORNI} giorni nessuna data dove cerchi. '
                             "Lo storico si riempie a ogni controllo.</p>")
        ultimo = con_data[-1][1]
        migliore = min(a for _, a in con_data)
        nuova = botmod.da_prenotare(p)
        tua = "Non ancora prenotata" if nuova else f"La tua prenotazione: {botmod.fmt(att.quando)}"
        testa = (f'<div class="numero"><span>Prima data dove cerchi, ora</span><strong>{e(botmod.fmt(ultimo))}</strong>'
                 f'<small>{e(tua)}. La migliore vista negli ultimi '
                 f'{botmod.STORICO_GIORNI} giorni: {e(botmod.fmt(migliore))}.</small></div>')
        grafico = self.grafico_storico(punti, None if nuova else att.quando) if len(con_data) >= 2 else \
            '<p class="nota">Il grafico compare dal secondo controllo.</p>'
        cambi, prec = [], "x"
        for t, a in punti:
            if a != prec:
                cambi.append(f'<tr><td>{e(botmod.orario(t).strftime("%d/%m %H:%M"))}</td>'
                             f'<td>{e(botmod.fmt(a)) if a else "nessuna"}</td></tr>')
                prec = a
        tabella = (f'<details><summary>Mostra i cambiamenti in tabella</summary><table><thead><tr><th>Controllo</th>'
                   f'<th>Prima data dove cerchi</th></tr></thead><tbody>{"".join(reversed(cambi[-30:]))}</tbody></table></details>')
        return titolo + testa + grafico + tabella

    def grafico_storico(self, punti, riferimento):
        """Linea a gradini della prima data utile nel tempo, con la prenotazione attuale tratteggiata.
        Tooltip nativi sui punti, tabella sotto. Scritte solo sugli assi e legenda nella didascalia: dentro
        l'area dei dati non c'e' testo, cosi' linea e punti non lo coprono mai. Il valore attuale e' gia'
        scritto in grande sopra il grafico: qui il punto dell'ultimo controllo e' solo evidenziato."""
        L, H, ml, mr, mt, mb, pad = 340, 200, 60, 10, 16, 30, 8
        t0, t1 = punti[0][0], max(punti[-1][0], punti[0][0] + 1)
        date = [a for _, a in punti if a] + ([riferimento] if riferimento else [])
        d0, d1 = min(date), max(date)
        margine = max((d1 - d0) * 0.1, botmod.timedelta(days=1))
        d0, d1 = d0 - margine, d1 + margine
        x0, x1 = ml + pad, L - mr - pad  # i punti stanno dentro, staccati dalle date dell'asse
        x = lambda t: x0 + (t - t0) / (t1 - t0) * (x1 - x0)
        # piu' in alto = prima (meglio): la linea tratteggiata della prenotazione fa da riferimento
        y = lambda d: mt + (d - d0).total_seconds() / (d1 - d0).total_seconds() * (H - mt - mb)

        tratti, corrente, marcatori = [], [], []
        for (t, a), succ in zip(punti, punti[1:] + [(t1, None)]):
            if a is None:
                if corrente:
                    tratti.append(corrente)
                corrente = []
                continue
            corrente += [(x(t), y(a)), (x(succ[0]), y(a))]
            marcatori.append((x(t), y(a), f'{botmod.orario(t):%d/%m %H:%M}: {botmod.fmt(a)}'))
        if corrente:
            tratti.append(corrente)
        linee = "".join('<polyline class="serie" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in tr) + '"/>'
                        for tr in tratti)
        # in evidenza solo se l'ultimo controllo ha trovato una data: altrimenti il punto sarebbe di prima
        evidenza = len(marcatori) - 1 if punti[-1][1] else -1
        cerchi = "".join(f'<circle class="punto{" ultimo" if i == evidenza else ""}" cx="{cx:.1f}" '
                         f'cy="{cy:.1f}" r="{6 if i == evidenza else 4}"><title>{e(testo)}</title></circle>'
                         for i, (cx, cy, testo) in enumerate(marcatori) if i >= len(marcatori) - 60)
        legenda_punto = ('· <span class="pallino" aria-hidden="true"></span>ultimo controllo' if evidenza >= 0
                         else "· l'ultimo controllo non ha trovato date")
        yr = y(riferimento) if riferimento else None

        # asse delle date: giorni "tondi" (settimane, 1 e 15 del mese, inizio mese) invece di date qualsiasi
        def tacche(passo):
            fuori, d = [], datetime(d0.year, d0.month, 1)
            while d <= d1:
                if d >= d0:
                    fuori.append(d)
                if passo >= 31:
                    mese = d.month - 1 + passo // 30
                    d = datetime(d.year + mese // 12, mese % 12 + 1, 1)
                elif passo == 14:
                    d = d.replace(day=15) if d.day == 1 else datetime(d.year + d.month // 12, d.month % 12 + 1, 1)
                else:
                    d += botmod.timedelta(days=passo)
            return fuori

        # la data della prenotazione sta sull'asse, in evidenza, accanto al suo tratteggio; le altre date
        # non le si avvicinano (ne' tra loro) per meno di 16 px
        def asse(passo):
            righe, occupate = [], [yr] if yr is not None else []
            for d in tacche(passo):
                yy = y(d)
                if all(abs(yy - o) >= 16 for o in occupate):
                    occupate.append(yy)
                    righe.append((yy, d))
            return righe

        # passo piu' largo con al massimo 4 date; se ne sopravvivono meno di 2, uno piu' fitto
        span = (d1 - d0).days
        passi = [1, 2, 7, 14, 31, 61, 92, 183, 366]
        i = next((k for k, g in enumerate(passi) if span / g <= 4), len(passi) - 1)
        righe = asse(passi[i])
        while len(righe) < 2 and i > 0:
            i -= 1
            righe = max(righe, asse(passi[i]), key=len)
        griglia = [f'<line class="griglia" x1="{ml}" x2="{L - mr}" y1="{yy:.1f}" y2="{yy:.1f}"/>' for yy, _ in righe]
        etichette = [f'<text class="asse forte" x="{ml - 8}" y="{yr + 4:.1f}" text-anchor="end">{riferimento:%d/%m/%y}</text>'] \
            if riferimento else []
        etichette += [f'<text class="asse" x="{ml - 8}" y="{yy + 4:.1f}" text-anchor="end">{d:%d/%m/%y}</text>' for yy, d in righe]

        # asse del tempo: primo e ultimo controllo, con l'ora se sono nello stesso giorno
        inizio, fine = botmod.orario(t0), botmod.orario(t1)
        fmt_t = "%d/%m %H:%M" if inizio.date() == fine.date() or t1 - t0 < 2 * 86400 else "%d/%m"
        etichette.append(f'<text class="asse" x="{x0}" y="{H - 8}">{inizio:{fmt_t}}</text>')
        etichette.append(f'<text class="asse" x="{x1}" y="{H - 8}" text-anchor="end">{fine:{fmt_t}}</text>')
        return f"""
<figure class="grafico">
  <figcaption>Prima data dove cerchi, a ogni controllo (più in alto = prima).
    <span class="legenda">{'<span class="tratto" aria-hidden="true"></span>la tua prenotazione ' if riferimento else ''}
    {legenda_punto if riferimento else legenda_punto.lstrip("· ")}</span></figcaption>
  <svg viewBox="0 0 {L} {H}" role="img" aria-label="Andamento della prima data utile rispetto alla prenotazione attuale">
    {"".join(griglia)}
    {f'<line class="riferimento" x1="{ml}" x2="{L - mr}" y1="{yr:.1f}" y2="{yr:.1f}"/>' if riferimento else ''}
    {linee}{cerchi}
    {"".join(etichette)}
  </svg>
</figure>"""

    def _disdetta(self, p):
        """Il modulo "Disdici" della prenotazione attuale, con data, ora e luogo nella conferma."""
        att = botmod.attuale_di(p)
        if botmod.da_prenotare(p) or not att or botmod.passata(p):
            return ""
        dove = f"{botmod.fmt(att.quando)}, {botmod.titolo(att.luogo.sede)}"
        conferma = f"Disdico la prenotazione di {self.bot.nome(p)}: {dove}? Il CUP la libera e non si torna indietro."
        return f"""<h3>Disdici la prenotazione</h3>
<p class="nota">Prenotata: {e(dove)}. Dopo la disdetta i controlli di questa ricetta restano in pausa: per prenotare di
  nuovo la riprendi tu.</p>
<form action="/ui/r/{p['id']}/disdici" method="post" data-conferma="{e(conferma)}">
  <input type="hidden" name="att" value="{e(att.quando.isoformat())}">
  <button class="pericolo">Disdici questa prenotazione</button>
</form>
"""

    def foglio_altro(self, p):
        pid = p["id"]
        conferma = f"Cancello i dati di {self.bot.nome(p)}? I suoi controlli si fermano."
        return f"""
<h2>✏️ Modifica · {e(self.bot.nome(p))}</h2>
<form hx-post="/ui/r/{pid}/nome" hx-target="#ricette" hx-swap="innerMorph" class="scelte">
  <label class="campo">Nome nei messaggi<input type="text" name="nome" value="{e(p.get('nome') or '')}" maxlength="20"
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
{self._disdetta(p)}<h3>Elimina</h3>
<form action="/ui/r/{pid}/cancella" method="post" data-conferma="{e(conferma)}">
  <button class="pericolo">Cancella questa ricetta</button>
</form>"""

    def _form_ricetta(self, chat, modo, p=None, errore="", valori=None, letto=None):
        """valori: cf e nre da mettere nei campi; letto: i dati letti dal PDF, mostrati da controllare."""
        if modo == "modifica" and p:
            return f'<p class="errore">{e(errore)}</p>' + self.foglio_altro(p)
        valori = valori or {}
        pratiche = self.store.della_chat(chat)
        consenso = "" if pratiche else f"""
  <details class="informativa"><summary>Informativa sui dati</summary><p>{e(self.bot.privacy()).replace(chr(10), "<br>")}</p></details>
  <label class="scelta"><input type="checkbox" name="consenso" value="1" required>
    <span>Ho letto l'informativa e do il consenso al trattamento di questi dati sanitari.</span></label>"""
        nome = "" if not pratiche else """
  <label class="campo">Nome nei messaggi<input type="text" name="nome" maxlength="20" placeholder="es. Papà"
    autocomplete="off"></label>"""
        if letto:
            righe = [("Prestazione", letto.get("prestazione")), ("Paziente", letto.get("paziente")),
                     ("Priorità", letto.get("priorita")), ("Data della ricetta", letto.get("data")),
                     ("Quesito", letto.get("quesito"))]
            dettagli = "".join(f"<li><span>{t}</span> {e(v)}</li>" for t, v in righe if v)
            mancano = letto.get("problemi") or []
            avviso = (f'<p class="errore">Non sono riuscito a leggere: {e(", ".join(mancano))}. Scrivilo tu qui sotto.</p>'
                      if mancano else '<p class="avviso" role="status">Ho letto il promemoria. Controlla che i dati siano giusti.</p>')
            carica = (f'{avviso}<ul class="elenco letta">{dettagli}</ul>'
                      '<label class="campo carica">Un altro PDF?<input type="file" accept="application/pdf" data-pdf></label>')
        else:
            carica = """
<label class="campo carica">📄 Carica il promemoria PDF della ricetta
  <input type="file" accept="application/pdf" data-pdf><small>Lo leggo e lo butto: non lo conservo. Se non hai il PDF
  scrivi i dati qui sotto.</small></label>"""
        return f"""
<h2>＋ Nuova ricetta</h2>
<p class="passo">Passo 1 di 4 · La ricetta</p>
{f'<p class="errore">{e(errore)}</p>' if errore else ''}
{carica}
<p class="nota">Tua o di un familiare che ti ha autorizzato. Codice fiscale e NRE li uso solo per il portale CUP e li
  conservo cifrati.</p>
<form hx-post="/ui/nuova" hx-target="#foglio" class="scelte" data-resta>
  <label class="campo">Codice fiscale<input type="text" name="cf" maxlength="16" autocomplete="off"
    autocapitalize="characters" spellcheck="false" value="{e(valori.get("cf", ""))}" required></label>
  <label class="campo">Numero ricetta (NRE)<input type="text" name="nre" maxlength="15" autocomplete="off"
    autocapitalize="characters" spellcheck="false" placeholder="010A…" value="{e(valori.get("nre", ""))}" required></label>{nome}{consenso}
  <button class="primario">Cerca la prenotazione</button>
</form>"""

    def azione_pdf(self, chat, corpo):
        """Il promemoria PDF caricato dall'app (corpo grezzo): il modulo dei dati, gia' compilato da controllare."""
        if self.bot.piena(len(self.store.della_chat(chat))):
            return f'<p class="errore">Puoi seguire al massimo {self.bot.max_pratiche} ricette.</p>'
        try:
            letto = ricetta_pdf.leggi(corpo)
        except ricetta_pdf.PdfNonLeggibile as ex:
            return self._form_ricetta(chat, "nuova", None, str(ex))
        except Exception:
            log.error("webapp: lettura PDF fallita")
            return self._form_ricetta(chat, "nuova", None, "Non riesco a leggere questo PDF: scrivi i dati a mano.")
        return self._form_ricetta(chat, "nuova", None, "", {"cf": letto["cf"], "nre": letto["nre"]}, letto)

    def foglio_nuova(self, chat):
        if self.bot.piena(len(self.store.della_chat(chat))):
            return f'<p class="errore">Puoi seguire al massimo {self.bot.max_pratiche} ricette.</p>'
        return self._form_ricetta(chat, "nuova")

    def foglio_dati(self, chat):
        pratiche = [p for p in self.store.della_chat(chat) if p.get("cf")]
        righe = "".join(
            f'<li><strong>{e(self.bot.nome(p))}</strong><small>Codice fiscale {e(botmod.maschera(p.get("cf", "")))} · '
            f'Ricetta {e(botmod.maschera(p.get("nre", "")))}</small></li>' for p in pratiche)
        return f"""
<h2>🔒 Dati e privacy</h2>
<p class="nota">Conservo cifrati, per ogni ricetta, codice fiscale e NRE, la prenotazione e le date trovate nei controlli.
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

    def foglio_admin(self, giorni=1):
        s = self.store
        chat_attive = s.chat_count()
        per_stato = dict(s.db.execute("SELECT stato, COUNT(*) FROM pratiche GROUP BY stato").fetchall())
        ora = time.time()
        metriche, prima = s.metriche(ora - 86400)  # dal database: i riavvii del bot non le azzerano
        attesa = botmod.attesa_appresa(s.metriche(ora - botmod.ATTESA_GIORNI * 86400)[0], ora)

        def finestra(sec):
            m = [x for x in metriche if x[0] > ora - sec]
            durate = sorted(x[1] for x in m)
            errori = sum(1 for x in m if not x[2])
            media = sum(durate) / len(durate) if durate else 0
            p95 = durate[int(len(durate) * 0.95) - 1] if len(durate) >= 20 else (durate[-1] if durate else 0)
            return len(m), errori, media, p95
        n1, e1, m1, p1 = finestra(3600)
        n24, e24, m24, p24 = finestra(86400)
        offerte = sum(1 for o in list(self.bot.offerte.values()) if ora - o["ts"] <= botmod.TTL_OFFERTA)
        # senza limite di ricette: se sono troppe per il ritmo del portale, i controlli restano indietro
        primo = s.db.execute("SELECT MIN(prossimo) FROM pratiche WHERE stato = 'attivo'").fetchone()[0]
        ritardo = max(0, ora - primo) if primo else 0
        in_ritardo = len(s.due(ora - 300))

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
  {tile(f"{ritardo // 60:.0f} min", "controlli in ritardo", f"{in_ritardo} oltre 5 minuti")}
</div>
<p class="nota">{e("Dati dal " + botmod.orario(prima).strftime("%d/%m %H:%M") if prima else "Ancora nessuna sessione sul portale registrata.")}
  {e(f"Attesa di una risposta lenta, imparata per quest'ora: {attesa} s.")}</p>
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
</div>
{self.sorveglianza(ora)}
{self.rapporto_guasti(ora, giorni)}"""

    def foglio_portale(self, ora=None):
        """Dashboard del portale CUP (solo admin): stato e velocita' dalle sonde (sonda.py), mai cancellate."""
        ora = ora or time.time()
        s = self.store
        sonde = s.sonde(ora - 30 * 86400)
        giorno = [x for x in sonde if x[0] > ora - 86400]
        settimana = [x for x in sonde if x[0] > ora - 7 * 86400]
        n_tot, ok_tot, prima = s.sonde_totali()
        episodi = s.episodi()

        def perc(v):
            return "–" if v is None else f"{v:.1f}%".replace(".", ",")

        def sec(v):
            return "–" if v is None else f"{v:.1f} s".replace(".", ",")

        def tile(valore, nome, nota=""):
            return f'<div class="tile"><strong>{e(str(valore))}</strong><span>{e(nome)}</span><small>{e(nota)}</small></div>'
        if not sonde:
            return ('<h2>📈 Portale CUP</h2><p class="nota">Ancora nessuna sonda: la prima parte pochi secondi dopo '
                    'l’avvio del bot e poi una ogni 5 minuti.</p>')
        ultima = sonde[-1]
        aperto = next((x for x in episodi if x[1] is None), None)
        if aperto:
            stato = f"⚠️ giù dal {botmod.orario(aperto[0]):%d/%m %H:%M}"
        elif ultima[3] != sonda_mod.OK:
            stato = "⚠️ ultima sonda fallita"
        else:
            stato = "✅ raggiungibile"
        dall = f"Dati dal {botmod.orario(prima):%d/%m/%Y}, {n_tot} sonde, mai cancellate." if prima else ""
        ore = sonda_mod.ultime_ore(sonde, ora, botmod.TZ)
        massimo = max([o[3] for o in ore if o[3]] or [1])
        barre = "".join(
            f'<span class="barra{" ko" if o[2] else ""}{" vuota" if not o[1] else ""}" '
            f'style="height:{max(4, min(100, (o[3] or 0) / massimo * 100)):.0f}%" '
            f'title="{o[0]:%d/%m %H}:00 · {o[1]} sonde, {o[2]} fallite, mediana {sec(o[3])}"></span>' for o in ore)
        per_ora = sonda_mod.per_ora_del_giorno(sonde, botmod.TZ)
        massimo_ora = max([v[3] for v in per_ora.values() if v[3]] or [1])
        righe_ora = "".join(
            f'<li><span class="ora">{h:02d}</span><span class="fascia"><i style="width:{(v[3] or 0) / massimo_ora * 100:.0f}%"></i></span>'
            f'<span class="val">{sec(v[2])} · picchi {sec(v[3])}'
            + (f' · {v[0] - v[1]} giù' if v[0] > v[1] else "") + "</span></li>"
            for h, v in per_ora.items() if v[0])

        def durata(inizio, fine):
            minuti = max(1, round(((fine or ora) - inizio) / 60))
            return f"{minuti // 60} h {minuti % 60} min" if minuti >= 60 else f"{minuti} min"
        righe_ep = "".join(
            f'<li><strong>{botmod.orario(i):%d/%m %H:%M}</strong> – '
            f'{"ancora giù" if f is None else f"{botmod.orario(f):%d/%m %H:%M}"} · {durata(i, f)}<small>{e(m)}</small></li>'
            for i, f, m in episodi) or "<li>Nessun episodio registrato.</li>"
        return f"""
<h2>📈 Portale CUP</h2>
<div class="tiles">
  {tile(stato, "stato ora", f"ultima sonda {botmod.orario(ultima[0]):%H:%M}, {sec(ultima[1])}")}
  {tile(perc(sonda_mod.disponibilita(giorno)), "disponibile, 24 ore", f"{len(giorno)} sonde")}
  {tile(perc(sonda_mod.disponibilita(settimana)), "disponibile, 7 giorni", f"{len(settimana)} sonde")}
  {tile(perc(sonda_mod.disponibilita(sonde)), "disponibile, 30 giorni", f"{len(sonde)} sonde")}
  {tile(perc(100 * ok_tot / n_tot if n_tot else None), "disponibile, da sempre", f"{n_tot} sonde")}
</div>
<p class="nota">{e(dall)} Una sonda è una richiesta leggera alla pagina iniziale ogni 5 minuti: non tiene occupata nessuna data.</p>
<h3>Ultime 24 ore · tempo di risposta per ora</h3>
<div class="barre" role="img" aria-label="Tempo di risposta del portale nelle ultime 24 ore">{barre}</div>
<p class="nota">Barre rosse: nell’ora c’è stata almeno una sonda fallita. L’altezza è la mediana dei tempi.</p>
<h3>Per ora del giorno · ultimi 30 giorni</h3>
<ul class="elenco per-ora">{righe_ora}</ul>
<h3>Quando è stato giù</h3>
<p class="nota">Un episodio inizia dopo {s.SONDA_SOGLIA} sonde fallite di fila e finisce alla prima riuscita.</p>
<ul class="elenco">{righe_ep}</ul>"""

    def sorveglianza(self, ora):
        """Stato del ciclo del bot (battito) e del portale (ultima sessione riuscita, guasto segnalato)."""
        b = self.bot
        fermo = self.fermo_da(ora)  # gli avvisi li manda solo il thread del battito
        ciclo = (f"⚠️ fermo da {fermo / 60:.0f} min" if fermo > botmod.BATTITO_MAX else
                 "attivo" if fermo < 120 else f"ultimo giro {fermo / 60:.0f} min fa")
        guasto = f"⚠️ guasto dalle {botmod.orario(b.guasto):%H:%M}" if b.guasto else "nessun guasto"
        return f"""
<h3>Sorveglianza</h3>
<div class="tiles">
  <div class="tile"><strong>{e(ciclo)}</strong><span>ciclo del bot</span><small>controlli, coda, Telegram</small></div>
  <div class="tile"><strong>{e(f"{botmod.orario(b.ultimo_ok):%H:%M}")}</strong><span>ultima sessione riuscita</span>
    <small>{e(guasto)}{e(f", {b.ko_di_fila} fallite di fila") if b.ko_di_fila else ""}</small></div>
</div>"""

    def rapporto_guasti(self, ora, giorni):
        """Per passo del portale: richieste, errori per tipo, tempo tipico e massimo; gli errori per ora del
        giorno; i tempi delle ultime prenotazioni. Solo tempi, codici ed esiti: nessun dato degli utenti."""
        righe = self.store.metriche_passi(ora - giorni * 86400)
        scelta = "".join(
            f'<button type="button" hx-get="/ui/admin{"?giorni=7" if g == 7 else ""}" hx-target="#foglio"'
            f' aria-pressed="{"true" if g == giorni else "false"}">{e(testo)}</button>'
            for g, testo in ((1, "Ultime 24 ore"), (7, "Ultimi 7 giorni")))
        testa = f'<h3>Rapporto guasti</h3><div class="finestra">{scelta}</div>'
        per_passo = {}
        for _, passo, secondi, esito, _ in righe:
            per_passo.setdefault(passo, []).append((secondi, esito))
        voci = []
        for passo in sorted(per_passo, key=lambda x: (list(PASSI).index(x) if x in PASSI else 99, x)):
            r = per_passo[passo]
            ok = sorted(sec for sec, esito in r if esito == "ok")
            conta = {t: sum(1 for _, esito in r if esito == t) for t in ("sovraccarico", "timeout", "inattesa")}
            nomi = {"sovraccarico": "sovraccarico", "timeout": "timeout", "inattesa": "risposta inattesa"}
            errori = ", ".join(f"{nomi[t]} {n}" for t, n in conta.items() if n)
            tempi = (f"tipico {durata(ok[len(ok) // 2])}, massimo {durata(ok[-1])}" if ok else "nessuna risposta")
            voci.append(f'<li><strong>{e(PASSI.get(passo, passo))}</strong><small>{len(r)} richieste · '
                        f'{e(errori) if errori else "nessun errore"}</small><small>{e(tempi)}</small></li>')
        passi = (f'<ul class="elenco">{"".join(voci)}</ul>' if voci else
                 '<p class="nota">Ancora nessuna richiesta registrata in questo periodo.</p>')
        return testa + passi + self.errori_per_ora(righe, giorni) + self.ultime_prenotazioni()

    def errori_per_ora(self, righe, giorni):
        """Barre degli errori per ora del giorno (una serie: niente legenda), con tooltip nativi e tabella."""
        errori, richieste = [0] * 24, [0] * 24
        for ts, _, _, esito, _ in righe:
            h = botmod.orario(ts).hour
            richieste[h] += 1
            errori[h] += esito != "ok"
        titolo = f"<h3>Errori per ora del giorno{' (7 giorni)' if giorni == 7 else ''}</h3>"
        if not any(errori):
            return titolo + '<p class="nota">Nessun errore del portale in questo periodo.</p>'
        L, H, ml, mb, mt = 340, 120, 28, 20, 8
        passo_x = (L - ml) / 24
        massimo = max(errori)
        barre = []
        for h, n in enumerate(errori):
            alto = (H - mb - mt) * n / massimo
            testo = f"ore {h:02d}: {n} errori su {richieste[h]} richieste"
            barre.append(f'<rect class="barra-hit" x="{ml + h * passo_x:.1f}" y="{mt}" width="{passo_x:.1f}" '
                         f'height="{H - mb - mt}"><title>{e(testo)}</title></rect>')
            if n:
                barre.append(f'<path class="barra" d="{barra_arrotondata(ml + h * passo_x + 1, H - mb, passo_x - 2, alto)}">'
                             f'<title>{e(testo)}</title></path>')
        assi = [f'<line class="griglia" x1="{ml}" x2="{L}" y1="{H - mb}" y2="{H - mb}"/>',
                f'<line class="griglia" x1="{ml}" x2="{L}" y1="{mt}" y2="{mt}"/>',
                f'<text class="asse" x="{ml - 6}" y="{mt + 4}" text-anchor="end">{massimo}</text>',
                f'<text class="asse" x="{ml - 6}" y="{H - mb + 4}" text-anchor="end">0</text>']
        assi += [f'<text class="asse" x="{ml + (h + 0.5) * passo_x:.1f}" y="{H - 5}" text-anchor="middle">{h}</text>'
                 for h in (0, 6, 12, 18, 23)]
        tabella = "".join(f"<tr><td>{h:02d}</td><td>{errori[h]}</td><td>{richieste[h]}</td></tr>"
                          for h in range(24) if richieste[h])
        return titolo + f"""
<figure class="grafico">
  <figcaption>Richieste al portale finite in errore (sovraccarico, timeout, risposta inattesa), per ora.</figcaption>
  <svg viewBox="0 0 {L} {H}" role="img" aria-label="Errori del portale per ora del giorno">{"".join(assi)}{"".join(barre)}</svg>
</figure>
<details><summary>Mostra in tabella</summary><table><thead><tr><th>Ora</th><th>Errori</th><th>Richieste</th></tr></thead>
<tbody>{tabella}</tbody></table></details>"""

    def ultime_prenotazioni(self):
        """Tempi delle ultime prenotazioni: dalla data trovata dal controllo all'inizio della prenotazione, poi
        le fasi (elenco, riepilogo, conferma, verifica). Niente date prenotate ne' ricette."""
        voci = []
        for ts, esito, dalla_data, fasi in self.store.tempi_prenotazioni(10):
            pezzi = [f"{nome} {durata(fasi[nome])}" for nome in ("elenco", "riepilogo", "conferma", "verifica")
                     if nome in fasi]
            totale = sum(v for v in fasi.values())
            voci.append(f'<li><strong>{e(f"{botmod.orario(ts):%d/%m %H:%M}")} · {e(ESITI.get(esito, esito))}</strong>'
                        f'<small>{e("dalla data trovata: " + durata(dalla_data) if dalla_data is not None else "")}'
                        f'{" · " if dalla_data is not None else ""}sul portale {e(durata(totale))}</small>'
                        f'<small>{e(" · ".join(pezzi) or "nessuna fase misurata")}</small></li>')
        return ('<h3>Ultime prenotazioni</h3>' +
                (f'<ul class="elenco">{"".join(voci)}</ul>' if voci else
                 '<p class="nota">Ancora nessuna prenotazione registrata.</p>'))


def durata(secondi):
    """12.3 -> "12,3 s"; 95 -> "1 min 35 s"."""
    if secondi < 60:
        return f"{secondi:.1f} s".replace(".", ",") if secondi < 10 else f"{secondi:.0f} s"
    m, s = divmod(round(secondi), 60)
    return f"{m} min {s} s" if s else f"{m} min"


def barra_arrotondata(x, base, larghezza, altezza, r=2):
    """Percorso di una barra con gli angoli in alto arrotondati, poggiata sulla base."""
    r = min(r, larghezza / 2, altezza)
    cima = base - altezza
    return (f"M{x:.1f},{base:.1f}V{cima + r:.1f}Q{x:.1f},{cima:.1f} {x + r:.1f},{cima:.1f}"
            f"H{x + larghezza - r:.1f}Q{x + larghezza:.1f},{cima:.1f} {x + larghezza:.1f},{cima + r:.1f}V{base:.1f}Z")


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
        intestazioni = {k.lower(): v for k, v in self.headers.items()}
        e_pdf = self.path.split("?")[0] == PERCORSO_PDF
        if lunghezza < 0 or lunghezza > (ricetta_pdf.MAX_BYTE if e_pdf else MAX_CORPO):
            self.send_error(413)
            return
        if e_pdf and verifica_init_data(firma_da(intestazioni), self.app.bot.token) is None:
            lunghezza = 0  # niente firma valida: i 3 MB non si leggono nemmeno
            self.close_connection = True
        try:
            corpo = self.rfile.read(lunghezza) if lunghezza else b""
            stato, h, testo = self.app.gestisci(metodo, self.path, intestazioni, corpo)
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
    """Server della Mini App in un thread suo, solo su 127.0.0.1 (davanti c'e' il reverse proxy), e un thread
    che ogni minuto guarda se il ciclo del bot gira ancora (App.controlla_battito)."""
    pronto = threading.Event()
    app = App(bot, db_path=db_path, key=key)  # ogni thread di richiesta apre la sua connessione

    def sorveglia():
        while True:
            time.sleep(60)
            try:
                app.controlla_battito()
            except Exception as ex:
                log.error("battito: errore imprevisto %s", type(ex).__name__)

    def servi():
        _Gestore.app = app
        server = ThreadingHTTPServer(("127.0.0.1", porta), _Gestore)
        server.daemon_threads = True
        pronto.set()
        log.info("Mini App in ascolto su 127.0.0.1:%d", porta)
        server.serve_forever()

    threading.Thread(target=servi, name="webapp", daemon=True).start()
    threading.Thread(target=sorveglia, name="battito", daemon=True).start()
    pronto.wait(10)
