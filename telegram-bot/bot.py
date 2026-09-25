"""Bot Telegram multiutente per CUP Piemonte: avvisa quando si libera una data prima della
prenotazione e, se l'utente tocca il pulsante (o ha attivato la conferma automatica), la sposta.

Ogni chat puo' seguire piu' "pratiche": una per ricetta (la propria, quella di un familiare...),
ciascuna con la sua area di ricerca, la sua conferma automatica e le sue offerte.

Configurazione da variabili d'ambiente (vedi .env.example):
    TELEGRAM_BOT_TOKEN   token di @BotFather                       (obbligatoria)
    CUP_BOT_KEY          chiave di cifratura: python store.py genkey (obbligatoria)
    DB_PATH              file SQLite (default: data/cup.db)
    ADMIN_CHAT_ID        chat PRIVATA di chi gestisce il bot: riceve gli errori imprevisti (facoltativa)
    CONTATTO_GESTORE     come contattare chi gestisce il bot, mostrato nell'informativa (consigliata)
    MAX_UTENTI           chat registrabili al massimo (default 30)
    MAX_PRATICHE         ricette per chat al massimo (default 0 = nessun limite)
    INTERVALLO_MIN       minuti tra due controlli della stessa ricetta (default 45, minimo 30)
    ADMIN_INTERVALLO_MIN intervallo solo per ADMIN_CHAT_ID (default come INTERVALLO_MIN, minimo 5)
    DISTANZA_PORTALE_S   secondi minimi tra due sessioni sul portale, fra tutti gli utenti (default 20)
    MODALITA_PROVA       1 = i pulsanti si fermano al riepilogo senza confermare (default 0)
    WEBAPP_URL           indirizzo HTTPS pubblico della Mini App (facoltativa; senza, niente Mini App)
    WEBAPP_PORTA         porta locale su cui ascolta la Mini App, dietro il reverse proxy (default 8095)
"""
import hmac
import json
import logging
import collections
import os
import queue
import random
import re
import secrets
import sys
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import cup_http
import store as storemod
from store import REGISTRAZIONE, Store

log = logging.getLogger("cupbot")

TTL_OFFERTA = 20 * 60  # secondi: oltre, la sessione che tiene lo slot potrebbe essere scaduta
AVVISA_ERRORI = (3, 12, 40)  # un timeout isolato del portale e' normale: avvisa solo se continua
MIN_INTERVALLO = 30  # minuti: ogni controllo tiene bloccata una data per un po'
MIN_INTERVALLO_ADMIN = 5  # solo per chi gestisce il bot: come una persona che aggiorna la pagina
PAUSA_CONTROLLA = 15 * 60  # secondi tra due /controlla della stessa ricetta
MAX_RICERCHE_FALLITE = 5  # ricerche CF+NRE fallite per chat al giorno
MAX_RICERCHE_APP = 10  # ricerche dalla Mini App per chat al giorno, riuscite o no: ognuna e' una sessione sul portale
AZIONI_PER_GIRO = 20  # azioni della Mini App eseguite prima di tornare a Telegram e ai controlli
PAUSA_MESSAGGI = 1.5  # secondi minimi tra due messaggi della stessa chat
ANTICIPI_AUTO = (1, 3, 7)  # giorni minimi da oggi per la conferma automatica, a scelta dell'utente
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
COMUNE_RE = re.compile(r"^[A-ZÀ-Ý][A-ZÀ-Ý' .-]{1,39}$")
STORICO_GIORNI = 7  # quanto indietro si tiene l'andamento delle date trovate
MAX_VISTE = 40  # date dell'ultimo controllo conservate per l'app
PANNELLO_COMPLETO = 6  # oltre, il pannello ha una riga e un pulsante per ricetta (Telegram: 4000 caratteri, 100 pulsanti)
MAX_DATE_CHAT = MAX_VISTE  # date con il pulsante Prenota in chat: tutte quelle conservate (Telegram ne regge 100)
MAX_RIGHE_DATE = MAX_VISTE  # date elencate in chat: tutte, finche' il testo sta in un messaggio
TESTO_DATE_MAX = 3500  # caratteri dell'elenco (Telegram ne accetta 4000; il resto e' per la nota finale)
MAX_PULSANTI = 90  # pulsanti in un messaggio (Telegram ne accetta 100)
MAX_LUOGHI = 80  # sedi viste nei controlli, per scegliere dove cercare dall'app
ATTESA_COMUNE = 10 * 60  # secondi: dopo "Un altro comune…" il prossimo testo vale come comune solo per poco
TZ = ZoneInfo("Europe/Rome")  # gli orari del portale sono italiani, anche se il server gira in UTC
ATTESA_MIN, ATTESA_BASE, ATTESA_MAX = 60, 90, 180  # secondi di attesa di una risposta lenta del portale
ATTESA_GIORNI = 7  # quanti giorni di controlli guardare per imparare l'attesa
MAX_RALLENTA = 60  # minuti: con il portale in difficolta' l'intervallo cresce fino a qui (o all'intervallo, se piu' lungo)


def adesso():
    return datetime.now(TZ).replace(tzinfo=None)


def orario(ts):
    return datetime.fromtimestamp(ts, TZ)


def attesa_appresa(metriche, ora):
    """Secondi da aspettare una risposta lenta del portale in quest'ora, imparati dai controlli dei giorni
    scorsi alla stessa ora (e a quelle vicine): 1,5 volte le risposte piu' lente, e il massimo se in quella
    fascia il portale va spesso in timeout. Nessuna tabella di "ore di punta": se il portale cambia
    abitudini, l'attesa cambia con lui. metriche: [(ts, durata, riuscita, lenta, timeout)], dove lenta e' la
    risposta singola piu' lenta del controllo (le righe senza, di versioni precedenti, non contano).
    Un timeout vale come una risposta lunga almeno quanto l'attesa di allora: contare solo le risposte
    arrivate abbasserebbe l'attesa proprio quando serve di piu'. Gli altri errori non dicono nulla sui tempi."""
    h = orario(ora).hour
    fascia = [(lenta, ok, scaduta) for ts, _, ok, lenta, scaduta in metriche
              if lenta is not None and (orario(ts).hour - h) % 24 in (0, 1, 23)]
    tempi = sorted(d for d, ok, scaduta in fascia if ok or scaduta)
    attesa = 1.5 * tempi[int(0.9 * (len(tempi) - 1))] if len(tempi) >= 5 else ATTESA_BASE
    if len(fascia) >= 5 and sum(1 for *_, scaduta in fascia if scaduta) >= 0.3 * len(fascia):
        attesa = ATTESA_MAX  # a quest'ora il portale non risponde in tempo spesso: tutta la pazienza possibile
    return round(min(ATTESA_MAX, max(ATTESA_MIN, attesa)))


def lento(e):
    """Il portale c'e' ma non risponde in tempo: anche a pagina iniziata, che requests chiama ConnectionError."""
    return isinstance(e, requests.ReadTimeout) or (
        isinstance(e, requests.ConnectionError) and "read timed out" in str(e).lower())

PRIVACY = (
    "🔒 Informativa, prima di iniziare\n\n"
    "Questo bot controlla per te il portale CUP Piemonte (cup.isan.csi.it) e ti avvisa se si libera "
    "una data PRIMA della tua prenotazione. Se tocchi il pulsante che ti mando, sposta la prenotazione "
    "per te. Non sposta nulla senza un tuo tocco, a meno che tu non attivi la conferma automatica "
    "(/auto). Non e' un servizio della Regione Piemonte o di CSI.\n\n"
    "Per funzionare deve conservare, per ogni ricetta che segui:\n"
    "• codice fiscale e numero della ricetta (NRE);\n"
    "• data, ora e luogo della prenotazione, e le date che ti ha gia' segnalato.\n"
    "Servono solo a questo. Sono conservati cifrati e inviati solo al portale CUP. Le date e i luoghi "
    "che ti mando viaggiano su Telegram come messaggi normali e restano nella chat finche' non la cancelli.\n\n"
    "Base giuridica: il tuo consenso esplicito (sono dati sanitari). Puoi ritirarlo in ogni momento con "
    "/cancella, che elimina tutto; poi cancella anche la chat (\"Elimina per entrambi\"). Puoi vedere i "
    "dati con /dati e cambiarli con /modifica. Vengono cancellati da soli quando la data della prenotazione "
    "e' passata, o dopo 30 giorni di pausa.\n\n"
    "Il portale chiede solo codice fiscale e NRE: chiunque li abbia puo' gestire la prenotazione, anche "
    "da qui. Usa il bot solo per le tue ricette o per quelle di un familiare che ti ha autorizzato "
    "(/aggiungi).\n\n"
    "I messaggi in cui mi scrivi codice fiscale e NRE li cancello dalla chat appena letti.")

AIUTO = (
    "📋 In alto nella chat trovi il pannello fissato con le tue ricette: prenotazione, dove cerco, "
    "conferma automatica ed esito dell'ultimo controllo. Si aggiorna da solo; i pulsanti sotto ogni "
    "ricetta cambiano dove cercare (🔎), la conferma automatica (⚡), la pausa (⏸) o controllano subito (🔄); "
    "📅 mostra le date trovate, da prenotare con un tocco.\n\n"
    "/stato – riporta il pannello in fondo alla chat\n"
    "/date – le date trovate dall'ultimo controllo, con il pulsante Prenota\n"
    "/aggiungi – segui un'altra ricetta (per esempio di un familiare)\n"
    "/dati – i dati che conservo\n"
    "/modifica – cambia codice fiscale e ricetta\n"
    "/cancella – elimina una ricetta o tutti i dati\n"
    "/privacy – come tratto i tuoi dati\n\n"
    "Funzionano anche /sede, /auto, /pausa, /riprendi e /controlla.")

COMANDI = [("stato", "Il pannello delle tue ricette"), ("date", "Le date trovate, da prenotare"),
           ("aggiungi", "Segui un'altra ricetta"),
           ("help", "Come funziona"), ("privacy", "Come tratto i tuoi dati")]
PICCOLE = {"di", "da", "del", "della", "dei", "degli", "delle", "in", "per"}  # gli articoli no: nei nomi contano

AUTO_TESTO = (
    "⚡ Conferma automatica\n\n"
    "Se la attivi, quando trovo una data PRIMA della prenotazione la prenoto subito, senza aspettare il tuo "
    "tocco: le date buone spariscono in pochi minuti.\n\n"
    "Da sapere:\n"
    "• la prenotazione attuale viene sostituita: la data vecchia si perde;\n"
    "• se poi non si puo' andare, bisogna disdire o spostare almeno 2 giorni lavorativi prima, altrimenti si "
    "paga l'intera prestazione;\n"
    "• rispetto dove cercare (/sede) e l'anticipo minimo che scegli qui sotto;\n"
    "• ti scrivo subito data, ora e luogo della nuova prenotazione.\n\n"
    "Da quando accetti una data nuova?")
AUTO_TESTO_NUOVA = (
    "⚡ Conferma automatica\n\n"
    "Questa ricetta non e' ancora prenotata. Se la attivi, la prima data libera dove cerchi la prenoto subito, "
    "senza aspettare il tuo tocco: le date buone spariscono in pochi minuti. Poi continuo a cercare date prima.\n\n"
    "Da sapere:\n"
    "• se poi non si puo' andare, bisogna disdire o spostare almeno 2 giorni lavorativi prima, altrimenti si "
    "paga l'intera prestazione;\n"
    "• rispetto dove cercare (/sede) e l'anticipo minimo che scegli qui sotto;\n"
    "• ti scrivo subito data, ora e luogo della prenotazione.\n\n"
    "Da quando accetti una data?")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def fmt(d):
    if d >= SENZA_DATA:
        return "nessuna (da prenotare)"
    return f"{GIORNI[d.weekday()]} {d:%d/%m/%Y} ore {d:%H:%M}"


UID_KEY = b""


def versione(p):
    """Cambia a ogni (ri)registrazione: i pulsanti vecchi non valgono per la ricetta nuova."""
    return str(int(p.get("creato", 0)))[-6:]


def uid(chat_id):
    """Identificativo per i log che non espone l'id Telegram (HMAC con chiave: non reversibile)."""
    return "u" + hmac.new(UID_KEY, str(chat_id).encode(), "sha256").hexdigest()[:10]


def pren_to_dict(p):
    return {"quando": p.quando.isoformat(), "sede": p.luogo.sede, "ambulatorio": p.luogo.ambulatorio,
            "indirizzo": p.luogo.indirizzo, "cosa": p.cosa}


def pren_from_dict(d):
    return cup_http.Prenotazione(datetime.fromisoformat(d["quando"]),
                                 cup_http.Luogo(d["sede"], d["ambulatorio"], d["indirizzo"]), d["cosa"])


def attuale_di(p):
    return pren_from_dict(p["attuale"]) if p.get("attuale") else None


# Ricetta mai prenotata: al posto della prenotazione c'e' una data lontanissima, cosi' ogni data trovata
# e' "prima" e tutto il resto (zone, offerte, conferma automatica, date viste) funziona uguale. Dopo la
# prima prenotazione la data diventa quella vera e il bot cerca date ancora prima, come sempre.
SENZA_DATA = datetime(2100, 1, 1)


def da_prenotare(p):
    return bool(p.get("da_prenotare"))


def senza_prenotazione(cosa):
    return {"quando": SENZA_DATA.isoformat(), "sede": "", "ambulatorio": "", "indirizzo": "", "cosa": cosa or ""}


def descrivi_prenotazione(att, titolo="Prenotazione"):
    if att.quando >= SENZA_DATA:
        return f"Ricetta da prenotare:\n🩺 {att.cosa or 'la prestazione della ricetta'}\n📅 non ancora prenotata"
    return f"{titolo}:\n🩺 {att.cosa}\n📅 {fmt(att.quando)}\n📍 {att.luogo}"


def descrivi(res):
    righe = [descrivi_prenotazione(res["attuale"]), "", "Date offerte dal CUP:"]
    if not res["slots"]:
        righe.append("nessuna")
    migliori = [x for x in res["slots"] if x in res["migliori"]]
    altre = [x for x in res["slots"] if x not in res["migliori"]]
    for x in (migliori + altre)[:10]:  # prima quelle che interessano
        righe.append(("✅ " if x in res["migliori"] else "▫️ ") + f"{fmt(x.quando)}\n    📍 {x.luogo}")
    if len(res["slots"]) > 10:
        righe.append(f"… e altre {len(res['slots']) - 10}")
    return "\n".join(righe)


def titolo(s):
    """"OSPEDALE DI ESEMPIO NORD" -> "Ospedale di Esempio Nord" (i nomi del portale sono in maiuscolo)."""
    parole = s.lower().split()
    # "a"/"e" di una sola lettera restano maiuscole: nei nomi sono piu' spesso sigle ("OSPEDALE A") che preposizioni
    return " ".join(w if i and w in PICCOLE and len(w) > 1 else w[:1].upper() + w[1:] for i, w in enumerate(parole))


PAROLINE = {"DI", "DA", "DEL", "DAL", "AL", "E", "ED", "CON", "PER", "IN", "NEL", "SU", "SUL", "TRA", "FRA",
            "DEI", "GLI", "LE", "LA", "IL", "LO", "UN", "UNA", "A", "O"}  # parole corte che non sono sigle


def indirizzo(luogo):
    """"VIA ROMA, 1 - TORINO (TO)" -> "Via Roma, 1 - Torino (TO)"."""
    m = re.match(r"^(.*?)\s*(\([A-Z]{2}\))?\s*$", luogo.indirizzo)
    return (titolo(m.group(1)) + (" " + m.group(2) if m.group(2) else "")).strip()


def prestazione(cosa, massimo=45):
    """Nome della prestazione leggibile: sigle (RM, TC, ECG) maiuscole, taglio a fine parola."""
    parole = cosa.split(" - ")[0].split()
    testo = " ".join(w if len(w) <= 3 and w.isupper() and w.isalpha() and w not in PAROLINE else w.lower()
                     for w in parole)
    testo = testo[:1].upper() + testo[1:]
    return testo if len(testo) <= massimo else testo[:massimo + 1].rsplit(" ", 1)[0].rstrip(",") + "…"


def dal_giorno(p):
    a = p.get("auto")
    return adesso().date() + timedelta(days=a["giorni"]) if a else None


def auto_descr(p):
    d = dal_giorno(p)
    if not d:
        return "no, ti chiedo prima di prenotare"
    return f"sì, date da {GIORNI[d.weekday()]} {d:%d/%m} in poi"


def zona_di(p):
    """Dove cercare. Formati precedenti: stringa ("sede", "tutte"...) o stessa_sede si'/no."""
    z = p.get("zona")
    if z is None:
        z = p.get("stessa_sede", True)
    return cup_http.zona_norm(z)


def descr_zona(zona, att):
    z = cup_http.zona_norm(zona)
    tipo, v = z["tipo"], z["valore"]
    if tipo == "sede":
        if v and att and att.luogo.sede and v != att.luogo.sede or v and not (att and att.luogo.sede):
            return f"solo nella sede {titolo(v)}"
        return f"solo in questa sede ({titolo(v or (att.luogo.sede if att else ''))})"
    if tipo == "comune":
        return f"solo nel comune di {titolo(v or (cup_http.comune(att.luogo) if att else ''))}"
    if tipo == "provincia":
        return f"in tutta la provincia ({v or (cup_http.provincia(att.luogo) if att else '')})"
    return "ovunque proponga il CUP"


def area_breve(zona, att):
    """Per il riepilogo del controllo: "18 date trovate in Piemonte, 0 a Torino"."""
    z = cup_http.zona_norm(zona)
    if z["tipo"] == "comune":
        return f"a {titolo(z['valore'] or (cup_http.comune(att.luogo) if att else ''))}"
    if z["tipo"] == "provincia":
        return f"in provincia ({z['valore'] or (cup_http.provincia(att.luogo) if att else '')})"
    return ""


def maschera(s, visibili=4):
    return s[:3] + "•" * max(0, len(s) - 3 - visibili) + s[-visibili:] if s else "-"


class Bot:
    def __init__(self, store, token, admin=None, max_utenti=30, intervallo=45, distanza=20, prova=False, contatto="",
                 admin_intervallo=None, max_pratiche=0, webapp_url=""):
        global UID_KEY
        UID_KEY = store.hkey
        self.store, self.admin, self.token, self.contatto = store, str(admin or ""), token, contatto
        self.api = f"https://api.telegram.org/bot{token}/"
        self.max_utenti, self.max_pratiche = max_utenti, max_pratiche
        self.intervallo = max(MIN_INTERVALLO, intervallo)
        # ogni controllo blocca una data: l'intervallo breve vale solo per chi gestisce il bot, non per tutti
        self.admin_intervallo = max(MIN_INTERVALLO_ADMIN, admin_intervallo or self.intervallo)
        self.distanza, self.prova = distanza, prova
        self.ricerche_fallite = {}  # chat_id -> [timestamp]
        self.ricerche_app = {}      # chat_id -> [timestamp]: tutte le ricerche chieste dalla Mini App
        self.cancellate = {}        # chat_id -> ora in cui ha cancellato tutti i dati (ricerche piu' vecchie: scartate)
        self.ultimo_msg = {}        # chat_id -> timestamp
        self.ultima_pulizia = 0.0
        self.offerte = {}   # id pratica -> {"token", "ts", "sessione", "slots"}: in memoria, come le sessioni del portale
        self.ultimo_portale = 0.0
        self.offset = None
        self.webapp_url = webapp_url
        self.coda = queue.Queue(maxsize=200)  # azioni chieste dalla Mini App: le esegue questo thread
        self.risultati = {}  # esiti delle ricerche chieste dalla Mini App: token -> {"chat", "ts", "pid" | "errore"}
        self.sessioni = {}   # id pratica -> {"ts", "sessione", "slots"}: l'ultimo controllo, per prenotare una data vista
        self.metriche = collections.deque(maxlen=5000)  # (ora, secondi, riuscita, lenta, timeout) di ogni sessione
        self.pazienza = {}  # id pratica -> secondi: dopo un timeout la sua ricerca aspetta di piu', fino al successo
        self.date_mostrate = {}  # id pratica -> {"token", "ts", "chiavi", "att"}: le date con Prenota in chat
        self._attesa = (0.0, ATTESA_BASE)  # (quando e' stata calcolata, secondi): ricalcolata ogni 10 minuti

    def piena(self, n):
        """Una chat con n ricette puo' aggiungerne un'altra? Con max_pratiche 0 sempre."""
        return bool(self.max_pratiche) and n >= self.max_pratiche

    # --- Telegram -----------------------------------------------------------------------
    def redact(self, e):
        return str(e).replace(self.token, "***")

    def tg(self, method, **data):
        try:
            r = requests.post(self.api + method, json=data, timeout=40).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Telegram %s fallito: %s", method, self.redact(e))
            return {}
        # l'utente ha bloccato il bot o cancellato l'account: i suoi dati non servono piu'
        if r.get("error_code") == 403 and data.get("chat_id") and str(data["chat_id"]) != self.admin:
            self.dimentica(data["chat_id"], "bot bloccato")
        return r

    def dimentica(self, chat_id, motivo):
        pratiche = self.store.della_chat(chat_id)
        if pratiche:
            for p in pratiche:
                self.scarta(p["id"])
            self.store.delete_chat(chat_id)
            log.info("utente %s cancellato: %s", uid(chat_id), motivo)

    def send(self, chat_id, text, buttons=None):
        data = {"chat_id": chat_id, "text": text[:4000]}
        if buttons:
            data["reply_markup"] = {"inline_keyboard": buttons}
        return bool(self.tg("sendMessage", **data).get("ok"))

    def alert_admin(self, text):
        """Solo testo generico: mai dati degli utenti ne' testo di eccezioni."""
        if self.admin:
            self.send(self.admin, "⚙️ " + text)

    # --- pratiche -----------------------------------------------------------------------
    def nome(self, p):
        return p.get("nome") or "La mia ricetta"

    def dire(self, p, testo, buttons=None):
        """Messaggio su una pratica: con piu' ricette nella chat porta il nome davanti."""
        if len(self.store.della_chat(p["chat_id"])) > 1:
            testo = f"[{self.nome(p)}] {testo}"
        return self.send(p["chat_id"], testo, buttons)

    def in_registrazione(self, chat):
        return next((p for p in self.store.della_chat(chat) if p["stato"] in REGISTRAZIONE), None)

    def della_chat(self, chat, pid):
        """La pratica solo se appartiene davvero a questa chat."""
        try:
            p = self.store.get(int(pid))
        except (TypeError, ValueError):
            return None
        return p if p and p["chat_id"] == chat else None

    # --- portale: una sessione alla volta, distanziate ---------------------------------
    def attesa(self, pid=None):
        """Secondi di pazienza con il portale ora: imparati dagli ultimi ATTESA_GIORNI giorni, di piu' per
        una ricetta la cui ricerca e' andata in timeout di recente. Solo dal thread del bot."""
        ora = time.time()
        if ora - self._attesa[0] > 600:
            try:
                metriche = self.store.metriche(ora - ATTESA_GIORNI * 86400)[0]
            except Exception as e:
                log.warning("metriche non lette: %s", type(e).__name__)
                metriche = list(self.metriche)
            self._attesa = (ora, attesa_appresa(metriche, ora))
        return max(self._attesa[1], self.pazienza.get(pid, 0))

    def meno_paziente(self, pid):
        """Dopo un successo la pazienza extra di una ricetta cala piano, e mai sotto 1,5 volte la risposta piu'
        lenta appena misurata (una ricerca sempre lenta non torna a scadere), fino al valore imparato."""
        if pid in self.pazienza:
            meno = min(self.pazienza[pid], max(round(self.pazienza[pid] / 1.25), round(1.5 * cup_http.PIU_LENTA)))
            if meno <= self._attesa[1]:
                self.pazienza.pop(pid)
            else:
                self.pazienza[pid] = meno

    def portale(self, fn, *args, pid=None, paziente=False, **kwargs):
        """Una sessione sul portale. paziente: per le prenotazioni, rare e preziose, tutta l'attesa possibile."""
        attesa = self.distanza - (time.time() - self.ultimo_portale)
        if attesa > 0:
            time.sleep(attesa)
        # un solo thread parla col portale: l'attesa vale per questa sessione
        cup_http.LENTO = pazienza = ATTESA_MAX if paziente else self.attesa(pid)
        cup_http.PIU_LENTA = 0.0
        cup_http.DIARIO.clear()
        inizio, riuscita, lenta, scaduta = time.time(), False, 0.0, False
        try:
            risultato = fn(*args, **kwargs)
            riuscita = True
            self.meno_paziente(pid)
            return risultato
        except (cup_http.NonTrovata, cup_http.NonAttiva):
            riuscita = True  # il portale ha risposto: e' la ricetta che non va
            self.meno_paziente(pid)
            raise
        except requests.RequestException as e:
            # il portale c'e' ma e' lento: la prossima volta un po' piu' di pazienza. Se invece non risponde
            # proprio (connessione rifiutata o assente), aspettare di piu' non servirebbe
            if lento(e):
                scaduta, lenta = True, pazienza  # quella risposta ci avrebbe messo almeno tanto
                if pid is not None and not paziente:  # una prenotazione aspetta gia' il massimo
                    self.pazienza[pid] = min(ATTESA_MAX, round(pazienza * 1.5))
            raise
        finally:
            self.ultimo_portale = time.time()
            lenta = round(max(lenta, cup_http.PIU_LENTA), 1)
            if cup_http.DIARIO:  # solo conteggi e id di form e pulsanti: per capire i flussi ancora da osservare
                log.info("portale %s %s: %s", f"pratica {pid}" if pid is not None else "ricerca", fn.__name__,
                         " | ".join(cup_http.DIARIO))
            self.metriche.append((inizio, self.ultimo_portale - inizio, riuscita, lenta, scaduta))
            try:  # sopravvive ai riavvii
                self.store.metrica(inizio, self.ultimo_portale - inizio, riuscita, lenta, scaduta)
            except Exception as e:
                log.warning("metrica non salvata: %s", type(e).__name__)

    # --- controllo periodico ------------------------------------------------------------
    def intervallo_di(self, chat_id):
        return self.admin_intervallo if self.admin and str(chat_id) == self.admin else self.intervallo

    def scarta(self, pid):
        """Offerta aperta e sessione dell'ultimo controllo di una ricetta: via insieme (contengono i dati
        della ricetta e tengono una sessione sul portale)."""
        self.offerte.pop(pid, None)
        self.sessioni.pop(pid, None)
        self.pazienza.pop(pid, None)
        self.date_mostrate.pop(pid, None)

    def salva(self, p, *campi):
        """Scrive solo i campi indicati sopra la versione attuale nel database: un controllo lungo non
        cancella le modifiche fatte nel frattempo dalla Mini App (zona, automatica, pausa...)."""
        nuovi = {k: p[k] for k in campi if k in p}
        tolti = [k for k in campi if k not in p]

        def applica(fresca):
            fresca.update(nuovi)
            for k in tolti:
                fresca.pop(k, None)
        aggiornata = self.store.modifica(p["id"], applica)
        if aggiornata:
            p.clear()
            p.update(aggiornata)
        return aggiornata

    def offerta_valida(self, pid):
        o = self.offerte.get(pid)
        return bool(o) and time.time() - o["ts"] <= TTL_OFFERTA

    def controlla(self, p, manuale=False):
        chat = p["chat_id"]
        nuova = da_prenotare(p)
        try:
            if nuova:
                try:
                    res = self.portale(cup_http.check_nuova, p["cf"], p["nre"], zona_di(p), pid=p["id"])
                except cup_http.GiaPrenotata:
                    # prenotata fuori dal bot (a mano, al telefono...): da qui si anticipa quella. Se non si
                    # riesce a leggerla, l'errore segue la strada degli errori normali (contati, avvisi radi)
                    self.diventa_prenotata(p)
                    return None
            else:
                res = self.portale(cup_http.check, p["cf"], p["nre"], zona_di(p), pid=p["id"])
        except (cup_http.NonTrovata, cup_http.NonAttiva) as e:
            if nuova:
                p.update(stato="pausa", pausa_da=time.time(), libera=True)
                self.salva(p, "stato", "pausa_da", "libera")
                self.aggiorna_pannello(chat)
                motivo = f"Il portale non accetta piu' questa ricetta ({e}): forse e' scaduta o e' gia' stata usata."
                self.dire(p, f"{motivo} Ho sospeso i controlli.\n/modifica per un'altra ricetta, /riprendi per "
                             "riprovare, /cancella per eliminarla.")
                return None
            p.update(stato="pausa", pausa_da=time.time(), libera=True)
            self.salva(p, "stato", "pausa_da", "libera")
            self.aggiorna_pannello(chat)
            self.dire(p, f"Non trovo piu' una prenotazione attiva per questa ricetta ({e}): forse e' stata "
                         "disdetta, spostata altrove o gia' effettuata. Ho sospeso i controlli.\n"
                         "/modifica per un'altra ricetta, /riprendi per riprovare, /cancella per eliminarla.")
            return None
        except Exception as e:
            if not isinstance(e, (cup_http.CupError, requests.RequestException)):
                log.error("controllo %s: errore imprevisto %s\n%s", uid(chat), type(e).__name__,
                          "".join(traceback.format_tb(e.__traceback__)))
                self.alert_admin(f"Errore imprevisto nel controllo di {uid(chat)}: {type(e).__name__}")
            p["errori"] = p.get("errori", 0) + 1
            p["ultimo"] = {"ts": time.time(), "testo": f"errore: {e}"}
            p["riassunto"] = {**(p.get("riassunto") or {}), "ts": time.time(), "errore": True}
            # errori di fila: controlli sempre piu' radi (fino a MAX_RALLENTA), per non insistere su un portale
            # in difficolta' e riprovare quando e' piu' probabile che risponda. Al primo successo, ritmo normale
            iv = self.intervallo_di(chat)
            rallenta = min(max(MAX_RALLENTA, iv), iv * 2 ** min(p["errori"] - 1, 10))
            dopo = time.time() + rallenta * 60 * random.uniform(0.9, 1.1)
            def rallenta_(f):
                if f.get("errori", 0) == 0 and (f.get("prossimo") or 0) <= time.time():
                    return  # ripresa dalla Mini App durante il controllo: vale la sua scelta
                f["prossimo"] = max(f.get("prossimo") or 0, dopo)
            self.store.modifica(p["id"], rallenta_)
            self.salva(p, "errori", "ultimo", "riassunto")  # rilegge anche prossimo
            self.aggiorna_pannello(chat)
            log.info("controllo %s/%s: errore %d: %s (prossimo tra %d min)", uid(chat), p["id"],
                     p["errori"], type(e).__name__, (p.get("prossimo", dopo) - time.time()) / 60)
            if manuale or p["errori"] in AVVISA_ERRORI:
                if lento(e):
                    motivo = "il portale CUP e' lento e non risponde in tempo"
                elif isinstance(e, requests.RequestException):
                    motivo = "il portale CUP non risponde"
                else:
                    motivo = str(e)
                self.dire(p, f"⚠️ {motivo[0].upper()}{motivo[1:]}" + (
                    "" if manuale else f" (da {p['errori']} controlli di fila). Riprovo da solo, piu' di rado "
                                       f"finche' non si riprende: il prossimo controllo verso le "
                                       f"{orario(p.get('prossimo', dopo)):%H:%M}."))
            return None

        if nuova:  # nessuna prenotazione: il riferimento resta la data lontanissima, con la prestazione letta
            p["attuale"] = {**senza_prenotazione(""), **(p.get("attuale") or {}), "quando": SENZA_DATA.isoformat()}
            if res.get("cosa"):
                p["attuale"]["cosa"] = res["cosa"]
            res["attuale"] = attuale_di(p)
        att = res["attuale"]
        self.sessioni[p["id"]] = {"ts": time.time(), "sessione": res["sessione"], "slots": res["slots"]}
        if att.quando < adesso():
            self.scarta(p["id"])
            self.store.delete(p["id"])
            self.dire(p, f"La data della prenotazione ({fmt(att.quando)}) e' passata: ho cancellato i dati di "
                         "questa ricetta. Per seguirne un'altra: /aggiungi.")
            self.aggiorna_pannello(chat)
            return None
        zona = zona_di(p)
        nell_area = [x for x in res["slots"] if cup_http.ammesso(x, att, zona)]
        self.registra_viste(p, res, nell_area)
        if p.get("errori", 0) >= AVVISA_ERRORI[0]:  # aveva avvisato dei problemi: ora che passano, lo dice
            self.dire(p, f"✅ Il portale CUP risponde di nuovo: torno a controllare ogni "
                         f"{self.intervallo_di(chat)} minuti.")
        ripresa = bool(p.get("errori"))
        if ripresa:  # dopo errori di fila i controlli si erano diradati: di nuovo al ritmo normale
            p["prossimo"] = min(p.get("prossimo") or float("inf"), time.time() + self.intervallo_di(chat) * 60)
        p.update(errori=0, attuale=pren_to_dict(att), ultimo={"ts": time.time(), "testo": descrivi(res)},
                 riassunto={"ts": time.time(), "viste": len(res["slots"]), "area": len(nell_area),
                            "migliori": len(res["migliori"]), "estesa": bool(cup_http.estensioni(zona)),
                            "prima_area": fmt(nell_area[0].quando) if nell_area else ""})
        log.info("controllo %s/%s: %d date, %d migliori", uid(chat), p["id"], len(res["slots"]), len(res["migliori"]))
        notificati = p.get("notificati") or {}  # data -> quando e' stata offerta l'ultima volta
        if isinstance(notificati, list):
            notificati = dict.fromkeys(notificati, 0)
        ignorati = set(p.get("ignorati", []))
        self.salva(p, "errori", "attuale", "ultimo", "riassunto", "viste", "luoghi", "storico",
                   *(("prossimo",) if ripresa else ()))
        auto = p.get("auto")  # appena riletta: se nel frattempo l'hanno spenta dall'app, niente prenotazione da solo
        if auto:
            # un solo tentativo automatico per data; le date gia' offerte col pulsante valgono comunque
            tentati = set(p.get("tentati_auto", []))
            dal = adesso().date() + timedelta(days=auto["giorni"])
            candidati = [x for x in res["migliori"] if x.luogo.sede and x.quando.date() >= dal and x.key() not in tentati]
            if candidati:
                slot = candidati[0]  # la piu' vicina tra quelle ammesse
                p["tentati_auto"] = sorted(tentati | {slot.key()})
                self.salva(p, "tentati_auto")
                self.dire(p, f"⚡ Conferma automatica: ho trovato una data{'' if nuova else ' prima'}.\n\n" + descrivi(res) +
                          "\n\n" + self.regola(p))
                if self.prenota(p, slot, res["sessione"], automatica=True) != "fallita":
                    return res
                p = self.store.get(p["id"])
                if not p or p["stato"] != "attivo" or da_prenotare(p) != nuova:
                    # nel frattempo e' risultata prenotata: le date (e la sessione) di questo controllo erano
                    # della prenotazione nuova e non valgono per spostare quella attuale
                    return res
        # si ripropone una data se la sua offerta e' scaduta o persa (es. riavvio), non se l'utente l'ha ignorata
        # (con "Controlla ora" anche se l'ultima offerta e' di pochi minuti fa: l'utente le sta chiedendo)
        ora = time.time()
        nuove = [x for x in res["migliori"] if x.key() not in ignorati
                 and (manuale or ora - notificati.get(x.key(), 0) > TTL_OFFERTA)]
        if nuove and self.offri(p, res, ignorati):
            notificati.update({x.key(): ora for x in res["migliori"]})
            p["notificati"] = notificati
            self.salva(p, "notificati")
        elif manuale:
            vedi = [[{"text": "📅 Vedi e prenota le date trovate", "callback_data": f"sc:date:{p['id']}:{versione(p)}"}]]
            self.dire(p, descrivi(res) + "\n\n" + self.regola(p), vedi if res["slots"] else None)
        self.aggiorna_pannello(chat)
        return res

    def registra_viste(self, p, res, nell_area):
        """Per la Mini App: date dell'ultimo controllo, sedi viste finora e andamento della prima data utile."""
        migliori = {x.key() for x in res["migliori"]}
        area = {x.key() for x in nell_area}
        p["viste"] = [{"q": x.quando.isoformat(), "sede": x.luogo.sede, "amb": x.luogo.ambulatorio,
                       "ind": x.luogo.indirizzo, "area": x.key() in area, "ok": x.key() in migliori,
                       "k": x.key(), "sel": bool(x.proposta or x.seleziona_id)}
                      for x in res["slots"][:MAX_VISTE]]
        luoghi = {l["sede"]: l for l in p.get("luoghi", [])}
        for x in res["slots"]:
            luoghi[x.luogo.sede] = {"sede": x.luogo.sede, "comune": cup_http.comune(x.luogo),
                                    "prov": cup_http.provincia(x.luogo)}
        p["luoghi"] = list(luoghi.values())[-MAX_LUOGHI:]
        ora = time.time()
        voce = {"t": ora, "a": nell_area[0].quando.isoformat() if nell_area else None,
                "r": res["attuale"].quando.isoformat()}
        storico = [v for v in p.get("storico", []) if v["t"] > ora - STORICO_GIORNI * 86400]
        # si aggiunge un punto quando qualcosa cambia, o almeno una volta l'ora
        if not storico or (storico[-1]["a"], storico[-1]["r"]) != (voce["a"], voce["r"]) or ora - storico[-1]["t"] > 3600:
            storico.append(voce)
        p["storico"] = storico[-400:]

    def offri(self, p, res, ignorati=()):
        """Messaggio con un pulsante per ogni data migliore (max 3). La sessione del controllo resta
        in memoria: e' lei che tiene bloccata la data proposta."""
        token = secrets.token_hex(4)
        slots = [x for x in res["migliori"] if x.luogo.sede and x.key() not in ignorati][:3]
        if not slots:
            return False
        pid = p["id"]
        buttons = [[{"text": f"✅ Prenota {fmt(x.quando)}", "callback_data": f"p:{pid}:{token}:{i}"}]
                   for i, x in enumerate(slots)]
        buttons.append([{"text": "Ignora", "callback_data": f"x:{pid}:{token}"}])
        prova = "\n(MODALITA' PROVA: il pulsante si ferma al riepilogo, non conferma)" if self.prova else ""
        nuova = da_prenotare(p)
        insieme = ("\n\nLa ricetta ha piu' prestazioni: le prenoto tutte nello stesso appuntamento. Se il portale le "
                   "mette in date diverse non confermo e te lo dico." if nuova and " + " in (res.get("cosa") or "") else "")
        ok = self.dire(p, ("🎉 C'e' una data libera!" if nuova else "🎉 C'e' una data PRIMA!") + "\n\n" + descrivi(res) +
                       "\n\n" + self.regola(p) + insieme + f"\n\nTocca per {'prenotare' if nuova else 'spostare la prenotazione'} "
                       f"(valido {TTL_OFFERTA // 60} minuti).{prova}", buttons)
        if ok:
            self.offerte[pid] = {"token": token, "ts": time.time(), "sessione": res["sessione"], "slots": slots}
        return ok

    def prenota_vista(self, p, chiave, attuale_vista=""):
        """Una data scelta dall'utente tra quelle viste (Mini App), anche fuori da dove cerca o piu' tardi.
        attuale_vista: la prenotazione che l'utente aveva sullo schermo quando ha confermato."""
        if (p.get("attuale") or {}).get("quando") != attuale_vista:
            self.dire(p, "La prenotazione è cambiata nel frattempo: riapri le date e scegli di nuovo.")
            return "fallita"
        s = self.sessioni.get(p["id"])
        if not s or time.time() - s["ts"] > TTL_OFFERTA:
            self.dire(p, "Quelle date sono di un controllo di più di 20 minuti fa: tocca 🔄 Controlla ora e riprova.")
            return "fallita"
        uguali = [x for x in s["slots"] if x.key() == chiave]
        if len(uguali) != 1 or not (uguali[0].proposta or uguali[0].seleziona_id):
            self.dire(p, "Questa data non è più prenotabile dal controllo di prima: tocca 🔄 Controlla ora e riprova.")
            return "fallita"
        slot = uguali[0]
        if slot.quando == attuale_di(p).quando:
            self.dire(p, "Questa data è alla stessa ora della prenotazione attuale: non la sposto.")
            return "fallita"
        self.scarta(p["id"])
        return self.prenota(p, slot, s["sessione"], libera=True)

    def prenota(self, p, slot, sessione, automatica=False, libera=False):
        """Ritorna "ok", "fallita" o "incerta" (conferma inviata ma esito non verificato)."""
        chat = p["chat_id"]
        self.sessioni.pop(p["id"], None)  # la sessione va al Riepilogo: nessun'altra data la riusa
        nuova = da_prenotare(p)
        self.dire(p, f"{'Prenoto' if nuova else 'Sposto la prenotazione a'}:\n📅 {fmt(slot.quando)}\n📍 {slot.luogo}…")
        attuale_db = self.store.get(p["id"])
        if not attuale_db:
            log.info("prenotazione %s/%s annullata: dati cancellati nel frattempo", uid(chat), p["id"])
            return "fallita"
        if automatica and (not attuale_db.get("auto") or attuale_db["stato"] != "attivo"):
            self.dire(p, "Nel frattempo hai spento la conferma automatica o messo in pausa: non prenoto da solo.")
            return "fallita"
        try:
            esito = self.portale(cup_http.prenota, p["cf"], p["nre"], slot, sessione=sessione,
                                 zona=zona_di(p), dry_run=self.prova, libera=libera, nuova=nuova,
                                 pid=p["id"], paziente=True)
        except cup_http.GiaPrenotata as e:
            self.dire(p, f"❌ Non prenotata: {e}.")
            try:
                self.diventa_prenotata(p)
            except (cup_http.CupError, requests.RequestException):
                pass  # ci riprova il prossimo controllo
            return "fallita"
        except cup_http.Separerebbe as e:
            # il portale sposterebbe una sola delle prestazioni prenotate insieme: ogni controllo terrebbe
            # bloccata una data per niente, quindi pausa finche' l'utente non decide
            p.update(stato="pausa", pausa_da=time.time(), libera=True)
            self.salva(p, "stato", "pausa_da", "libera")
            self.dire(p, f"❌ Non spostata: {e}.\n\nQuesta prenotazione ha piu' prestazioni nello stesso "
                         "appuntamento e il portale non le sposterebbe tutte insieme: da qui non posso anticiparla "
                         "senza rischiare di separarle. Ho messo in pausa i controlli per non tenere occupate date.\n"
                         f"Per spostarla: {cup_http.LISTA_URL} o il {cup_http.CALL_CENTER}. /riprendi per riprovare.")
            self.aggiorna_pannello(chat)
            log.info("prenotazione %s/%s fallita: Separerebbe", uid(chat), p["id"])
            return "fallita"
        except (cup_http.CupError, requests.RequestException) as e:
            urgente = "Conferma inviata" in str(e)
            self.dire(p, ("🚨 " if urgente else ("❌ Non prenotata: " if nuova else "❌ Non spostata: ")) + str(e))
            if urgente:
                self.alert_admin(f"Esito incerto dopo la conferma per {uid(chat)}")
                self.sospendi_auto(p)
            log.info("prenotazione %s/%s fallita: %s%s", uid(chat), p["id"], type(e).__name__,
                     " (esito incerto)" if urgente else "")
            return "incerta" if urgente else "fallita"
        except Exception as e:
            log.error("prenotazione %s: errore imprevisto %s\n%s", uid(chat), type(e).__name__,
                      "".join(traceback.format_tb(e.__traceback__)))
            self.dire(p, f"🚨 Errore imprevisto durante la prenotazione ({type(e).__name__}). Verifica subito su "
                         f"{cup_http.LISTA_URL} o al {cup_http.CALL_CENTER}.")
            self.alert_admin(f"Errore imprevisto nella prenotazione di {uid(chat)}: {type(e).__name__}")
            self.sospendi_auto(p)
            return "incerta"
        log.info("prenotazione %s/%s riuscita%s%s", uid(chat), p["id"], " (automatica)" if automatica else "",
                 " (prova)" if self.prova else "")
        if self.prova:
            self.dire(p, "🧪 " + esito)
            return "ok"
        # la nuova data diventa il riferimento dei prossimi controlli
        p.update(notificati={}, ignorati=[], tentati_auto=[])
        p["attuale"] = {**p.get("attuale", {}), "quando": slot.quando.isoformat(), "sede": slot.luogo.sede,
                        "ambulatorio": slot.luogo.ambulatorio, "indirizzo": slot.luogo.indirizzo}
        p["prossimo"] = time.time() + self.intervallo_di(chat) * 60
        p.pop("da_prenotare", None)  # da qui e' una prenotazione come le altre: si cercano date prima
        self.salva(p, "notificati", "ignorati", "tentati_auto", "attuale", "prossimo", "da_prenotare")
        self.dire(p, f"✅ Prenotazione {'fatta' if nuova else 'spostata'}{' (conferma automatica)' if automatica else ''}!\n"
                     f"📅 {fmt(slot.quando)}\n📍 {slot.luogo}\n\n"
                     "Arriveranno SMS/email dal CUP con il nuovo promemoria; controlla anche il codice di "
                     "pagamento del ticket. Se non si puo' andare, disdire o spostare almeno 2 giorni lavorativi "
                     "prima. Continuo a cercare date ancora prima.\n\n" + self.regola(p))
        self.aggiorna_pannello(chat)
        return "ok"

    def diventa_prenotata(self, p):
        """Una ricetta da prenotare risulta prenotata fuori dal bot: da qui si anticipa quella prenotazione.
        CupError se la prenotazione non si legge (il chiamante lo tratta come un errore del portale)."""
        try:
            att = self.portale(cup_http.cerca, p["cf"], p["nre"])
        except (cup_http.NonTrovata, cup_http.NonAttiva) as e:
            raise cup_http.CupError(f"la ricetta risulta gia' prenotata, ma non trovo la prenotazione attiva ({e})")
        auto = bool(p.get("auto"))
        p["attuale"] = pren_to_dict(att)
        p.pop("da_prenotare", None)
        # una prenotazione fatta a mano non si sposta da sola: la conferma automatica la riaccende l'utente
        p.update(notificati={}, ignorati=[], tentati_auto=[], auto=None)
        self.scarta(p["id"])
        self.salva(p, "attuale", "da_prenotare", "notificati", "ignorati", "tentati_auto", "auto")
        self.dire(p, descrivi_prenotazione(att, "La ricetta risulta prenotata") +
                  "\n\nDa ora cerco date prima di questa." +
                  ("\nHo spento la conferma automatica: se vuoi, riattivala con /auto." if auto else "") +
                  "\n\n" + self.regola(p))
        self.aggiorna_pannello(p["chat_id"])

    def sospendi_auto(self, p):
        """Dopo un esito incerto niente altri tentativi automatici: decide l'utente."""
        if p.get("auto"):
            p["auto"] = None
            self.salva(p, "auto")
            self.dire(p, "Per sicurezza ho disattivato la conferma automatica. Verifica la prenotazione, "
                         "poi riattivala con /auto se vuoi.")

    # --- registrazione di una pratica ---------------------------------------------------
    def chiedi_cf(self, p):
        p.pop("nre", None)  # la coppia CF+NRE si riforma solo a ricerca riuscita
        p.pop("attende_comune", None)
        p.pop("viste", None)  # le date e le sedi trovate erano della ricetta vecchia
        p.pop("luoghi", None)
        self.scarta(p["id"])
        p.pop("libera", None)
        p.update(stato="cf", creato=time.time(), auto=None, notificati={}, ignorati=[], tentati_auto=[])
        self.store.save(p)
        self.send(p["chat_id"], "Mandami il codice fiscale della persona a cui e' intestata la ricetta "
                                "(16 caratteri, come sul promemoria).")

    def ricevi_cf(self, p, testo):
        cf = "".join(testo.split()).upper()
        if not cup_http.CF_RE.match(cf):
            self.send(p["chat_id"], "Non sembra un codice fiscale valido (16 caratteri, es. RSSMRA80A01L219X). Riprova.")
            return
        p.update(cf=cf, stato="nre")
        self.store.save(p)
        self.send(p["chat_id"], "Ora il numero della ricetta elettronica (NRE): 15 caratteri, di solito inizia con "
                                "010A. Lo trovi sotto il codice a barre della ricetta o sul promemoria della prenotazione.")

    def ricevi_nre(self, p, testo):
        chat = p["chat_id"]
        nre = "".join(testo.split()).upper()
        if not cup_http.NRE_RE.match(nre):
            self.send(chat, "Il numero ricetta deve avere 15 caratteri (es. 010A12345678901). Riprova.")
            return
        recenti = [t for t in self.ricerche_fallite.get(chat, []) if t > time.time() - 86400]
        self.ricerche_fallite[chat] = recenti
        if len(recenti) >= MAX_RICERCHE_FALLITE:
            self.send(chat, "Troppe ricerche non riuscite oggi. Riprova domani.")
            return
        self.send(chat, "Cerco la ricetta sul portale CUP…")
        try:
            att, cosa = self.cerca_ricetta(p["cf"], nre)
        except cup_http.NonAttiva as e:
            recenti.append(time.time())
            self.send(chat, f"La prenotazione di questa ricetta non e' attiva ({e}).")
            self.chiedi_cf(p)
            return
        except cup_http.NonTrovata as e:
            recenti.append(time.time())
            self.send(chat, f"Il portale non accetta questa ricetta con questo codice fiscale ({e}).")
            self.chiedi_cf(p)
            return
        except (cup_http.CupError, requests.RequestException) as e:
            self.send(chat, f"Il portale CUP non risponde ({e}). Rimandami il numero ricetta tra qualche minuto.")
            return
        altre = [x for x in self.store.della_chat(chat) if x["id"] != p["id"]]
        p.update(nre=nre, attuale=pren_to_dict(att) if att else senza_prenotazione(cosa),
                 stato="nome" if altre and not p.get("nome") else "sede")
        if att:
            p.pop("da_prenotare", None)
        else:
            p["da_prenotare"] = True
        try:
            self.store.save(p)
        except storemod.GiaRegistrata:
            self.send(chat, "Questa ricetta e' gia' seguita dal bot (in questa o in un'altra chat).")
            self.chiedi_cf(self.store.get(p["id"]))
            return
        if att:
            self.send(chat, descrivi_prenotazione(att, "Ho trovato la prenotazione"))
        else:
            self.send(chat, f"Ho trovato la ricetta: non e' ancora prenotata.\n🩺 {cosa or 'la prestazione della ricetta'}\n\n"
                            "Cerco il primo appuntamento libero e ti avviso con il pulsante Prenota. Dopo la "
                            "prenotazione continuo a cercare date ancora prima.")
        if p["stato"] == "nome":
            self.send(chat, "Come chiamo questa ricetta nei messaggi? Per esempio: Papà, Mamma, Nonna.")
        else:
            self.chiedi_sede(p, attuale_di(p))

    def cerca_ricetta(self, cf, nre):
        """(prenotazione, None) se la ricetta ha un appuntamento attivo; (None, prestazione) se e' da prenotare.
        NonTrovata se il portale non accetta la ricetta. Per una ricetta da prenotare si fa solo il passo
        "Ricerca" della prenotazione nuova: non apre gli appuntamenti, non blocca date."""
        try:
            return self.portale(cup_http.cerca, cf, nre), None
        except cup_http.NonAttiva as e:
            if not e.solo_disdette():
                raise  # erogata o in un altro stato: non e' una ricetta da prenotare
            nessuna = e
        except cup_http.NonTrovata as e:
            nessuna = e
        try:
            return None, self.portale(cup_http.nuova, cf, nre)
        except cup_http.GiaPrenotata:
            # il portale la considera gia' prenotata ma non c'e' una prenotazione attiva da spostare
            raise cup_http.NonTrovata(f"la ricetta risulta gia' usata, ma senza una prenotazione attiva: {nessuna}")

    def ricevi_nome(self, p, testo):
        nome = " ".join(testo.split())[:20]
        compatto = "".join(nome.split()).upper()
        if not nome or nome.startswith("/") or cup_http.CF_RE.match(compatto) or cup_http.NRE_RE.match(compatto):
            self.send(p["chat_id"], "Scrivi un nome breve, per esempio: Papà.")
            return
        p.update(nome=nome, stato="sede")
        self.store.save(p)
        self.chiedi_sede(p, attuale_di(p))

    def chiedi_sede(self, p, att):
        pv = f"{p['id']}:{versione(p)}"
        if da_prenotare(p):  # nessuna sede di riferimento: un comune scelto o dove propone il CUP
            self.dire(p, "Dove cerco il primo appuntamento?\n(Con un comune allargo la ricerca con \"Estendi area\" "
                         "del portale: il controllo e' piu' lento ma vede anche le altre aziende sanitarie. \"Dove "
                         "propone il CUP\" guarda le sedi che il portale propone per questa ricetta.)",
                      [[{"text": "Un comune…", "callback_data": f"sede:{pv}:altro"}],
                       [{"text": "Dove propone il CUP", "callback_data": f"sede:{pv}:tutte"}]])
            return
        righe = [[{"text": f"Solo {att.luogo.sede}", "callback_data": f"sede:{pv}:sede"}]]
        if cup_http.comune(att.luogo):
            righe.append([{"text": f"Solo il comune di {cup_http.comune(att.luogo).title()}", "callback_data": f"sede:{pv}:comune"}])
        if cup_http.provincia(att.luogo):
            righe.append([{"text": f"Solo la provincia ({cup_http.provincia(att.luogo)})", "callback_data": f"sede:{pv}:provincia"}])
        righe.append([{"text": "Un altro comune…", "callback_data": f"sede:{pv}:altro"}])
        righe.append([{"text": "Qualsiasi sede proposta dal CUP", "callback_data": f"sede:{pv}:tutte"}])
        self.dire(p, "Dove cerco le date?\n(Comune e provincia allargano la ricerca con \"Estendi area\" del "
                     "portale: il controllo e' piu' lento ma vede anche le altre aziende sanitarie.)", righe)

    def posto_libero(self, chat):
        """Una chat nuova si attiva solo se c'e' posto (il limite si ricontrolla all'attivazione)."""
        gia_attiva = any(x["stato"] in ("attivo", "pausa") for x in self.store.della_chat(chat))
        return gia_attiva or self.store.chat_count() < self.max_utenti

    def imposta_zona(self, p, zona):
        att = attuale_di(p)
        nuova = p["stato"] in REGISTRAZIONE
        if nuova and not self.posto_libero(p["chat_id"]):
            self.dire(p, "Mi dispiace, nel frattempo i posti sono finiti: al momento non accetto nuovi utenti.")
            return
        p["zona"] = zona
        p.pop("stessa_sede", None)
        p.pop("attende_comune", None)
        if nuova:
            p.update(stato="attivo", prossimo=time.time() + 60)
        self.salva(p, "zona", "stessa_sede", "attende_comune", "stato", "prossimo")
        quale = "data libera" if da_prenotare(p) else f"data prima del {fmt(att.quando)}"
        self.dire(p, f"Ok: cerco {descr_zona(zona, att)}." +
                  (f"\n\nFatto! Controllo ogni {self.intervallo_di(p['chat_id'])} minuti e ti scrivo appena esce una "
                   f"{quale}.\n\n{AIUTO}" if nuova else ""))
        self.aggiorna_pannello(p["chat_id"], nuovo=nuova)

    def ricevi_comune(self, p, testo):
        comune = " ".join(testo.split()).upper()
        if not COMUNE_RE.match(comune):
            self.send(p["chat_id"], "Scrivi solo il nome del comune, per esempio: Torino.")
            return
        self.imposta_zona(p, {"tipo": "comune", "valore": comune})

    def chiedi_auto(self, p):
        pv = f"{p['id']}:{versione(p)}"
        righe = [[{"text": "Da domani" if g == 1 else f"Da tra {g} giorni", "callback_data": f"auto:{pv}:{g}"}
                  for g in ANTICIPI_AUTO]]
        righe.append([{"text": "Disattiva" if p.get("auto") else "Lascia disattivata", "callback_data": f"auto:{pv}:0"}])
        self.dire(p, (AUTO_TESTO_NUOVA if da_prenotare(p) else AUTO_TESTO) + f"\n\nStato attuale: {auto_descr(p)}.", righe)

    def privacy(self):
        return PRIVACY + (f"\n\nGestore del bot: {self.contatto}" if self.contatto else "")

    # --- azioni su una pratica (dopo la scelta, se ce n'e' piu' d'una) ------------------
    AZIONI = ("sede", "auto", "pausa", "riprendi", "modifica", "cancella", "controlla", "date", "menu")

    def esegui(self, azione, p):
        if azione == "sede":
            if p.get("attuale"):
                self.chiedi_sede(p, attuale_di(p))
        elif azione == "auto":
            self.chiedi_auto(p)
        elif azione == "pausa":
            p.update(stato="pausa", pausa_da=time.time())
            self.salva(p, "stato", "pausa_da")
            self.aggiorna_pannello(p["chat_id"])
        elif azione == "controlla":
            self.controlla_ora(p)
        elif azione == "date":
            self.mostra_date(p)
        elif azione == "menu":  # dal pannello compatto: la scheda di una ricetta con i suoi pulsanti
            self.dire(p, self.scheda(p), self.pulsanti_ricetta(p))
        elif azione == "riprendi":
            p.update(stato="attivo", prossimo=time.time(), errori=0)
            p.pop("libera", None)
            try:
                self.salva(p, "stato", "prossimo", "errori", "libera")
            except storemod.GiaRegistrata:
                self.dire(p, "Nel frattempo questa ricetta e' stata registrata da un'altra chat: non posso riattivarla.")
                return
            self.aggiorna_pannello(p["chat_id"])
        elif azione == "modifica":
            self.scarta(p["id"])
            self.chiedi_cf(p)
        elif azione == "cancella":
            self.dire(p, f"Cancello i dati di questa ricetta ({self.nome(p)})? I suoi controlli si fermano.",
                      [[{"text": "Si', cancella", "callback_data": f"del:{p['id']}:{versione(p)}:1"},
                        {"text": "No", "callback_data": f"del:{p['id']}:{versione(p)}:0"}]])

    def mostra_date(self, p):
        """Le date dell'ultimo controllo in chat, ognuna con Prenota: come "Date disponibili" della Mini App,
        per chi non la usa. Si prenota solo dopo una conferma con data, ora e luogo (e se e' fuori zona o
        dopo la prenotazione attuale, lo dice)."""
        viste = p.get("viste") or []
        if not viste:
            self.dire(p, "Nessuna data trovata finora: arrivano con il prossimo controllo.")
            return
        r = p.get("riassunto") or {}
        s = self.sessioni.get(p["id"])
        fresche = bool(s) and time.time() - s["ts"] <= TTL_OFFERTA
        att, nuova, token = attuale_di(p), da_prenotare(p), secrets.token_hex(4)
        gruppi = [("✅ Dove cerchi" if nuova else "✅ Prima della tua prenotazione, dove cerchi", [v for v in viste if v["ok"]]),
                  ("Dove cerchi, ma dopo la tua prenotazione", [v for v in viste if v["area"] and not v["ok"]]),
                  ("In altre zone", [v for v in viste if not v["area"]])]
        testo, bottoni, chiavi, elencate = [f"📅 Date trovate · {self.nome(p)}"], [], [], 0
        pieno = False  # il testo non ci sta piu': le altre date restano solo nei pulsanti
        for nome_gruppo, voci in gruppi:
            if not voci:
                continue
            if not pieno:
                testo += ["", nome_gruppo]
            for v in voci:
                q = datetime.fromisoformat(v["q"])
                luogo = cup_http.Luogo(v["sede"], v["amb"], v["ind"])
                riga = f"• {fmt(q)} · {titolo(v['sede'])}, {indirizzo(luogo)}"
                pieno = pieno or elencate >= MAX_RIGHE_DATE or sum(len(x) + 1 for x in testo) + len(riga) > TESTO_DATE_MAX
                if not pieno:
                    testo.append(riga)
                    elencate += 1
                if fresche and v.get("sel") and v.get("k") and q != att.quando and len(chiavi) < MAX_DATE_CHAT:
                    bottoni.append([{"text": f"Prenota {fmt(q)} · {titolo(v['sede'])}"[:60],
                                     "callback_data": f"vd:{p['id']}:{token}:{len(chiavi)}"}])
                    chiavi.append(v["k"])
        if len(viste) > elencate:
            testo.append(f"…e altre {len(viste) - elencate}" + (", nei pulsanti qui sotto." if fresche else "."))
        quando = f" delle {orario(r['ts']):%H:%M}" if r.get("ts") else ""
        if fresche:
            testo += ["", f"Dal controllo{quando}: per ogni sede la prima data che il CUP propone. Si possono "
                          f"prenotare fino alle {orario(s['ts'] + TTL_OFFERTA):%H:%M}: tocca una data, ti chiedo conferma."]
            self.date_mostrate[p["id"]] = {"token": token, "ts": s["ts"], "chiavi": chiavi,
                                           "att": (p.get("attuale") or {}).get("quando")}
        else:
            testo += ["", f"Dal controllo{quando}: sono passati più di {TTL_OFFERTA // 60} minuti e il CUP potrebbe "
                          "averle già date ad altri. Per prenotarne una serve un controllo nuovo."]
            bottoni.append([{"text": "🔄 Controlla ora", "callback_data": f"sc:controlla:{p['id']}:{versione(p)}"}])
        self.dire(p, "\n".join(testo), bottoni or None)

    def data_mostrata(self, p, token, indice):
        """La data numero `indice` dell'ultimo elenco in chat, se l'elenco e' ancora quello e ancora valido."""
        d = self.date_mostrate.get(p["id"])
        if not d or d["token"] != token or not indice.isdecimal() or int(indice) >= len(d["chiavi"]):
            self.dire(p, "Questo elenco non e' piu' valido: /date per quello nuovo.")
            return None, None
        s = self.sessioni.get(p["id"])
        if not s or s["ts"] != d["ts"]:
            self.dire(p, "Nel frattempo c'e' stato un controllo nuovo: /date per le date aggiornate.")
            return None, None
        if time.time() - d["ts"] > TTL_OFFERTA:
            self.dire(p, "Queste date sono di un controllo di più di 20 minuti fa: tocca 🔄 Controlla ora e riprova.")
            return None, None
        chiave = d["chiavi"][int(indice)]
        v = next((x for x in p.get("viste") or [] if x.get("k") == chiave), None)
        if not v:
            self.dire(p, "Questa data non c'e' piu' tra quelle trovate: /date per l'elenco nuovo.")
            return None, None
        return d, v

    def chiedi_conferma_data(self, p, token, indice):
        d, v = self.data_mostrata(p, token, indice)
        if not v:
            return
        q, att = datetime.fromisoformat(v["q"]), attuale_di(p)
        luogo = cup_http.Luogo(v["sede"], v["amb"], v["ind"])
        dove = f"📅 {fmt(q)}\n📍 {titolo(v['sede'])}, {indirizzo(luogo)}"
        fuori = "\n\n⚠️ È fuori dalla zona in cui cerchi." if not v["area"] else ""
        if da_prenotare(p):
            testo = (f"Prenoto {self.nome(p)} qui?\n\n{dove}{fuori}\n\nSe poi non si può andare, va disdetta almeno 2 "
                     "giorni lavorativi prima.")
        else:
            rispetto = "PRIMA" if q < att.quando else "⚠️ DOPO"
            testo = (f"Sposto la prenotazione di {self.nome(p)} qui?\n\n{dove}\n\nÈ {rispetto} della data attuale "
                     f"({fmt(att.quando)}), che si perde.{fuori}")
        self.dire(p, testo, [[{"text": "✅ Sì, prenota" if da_prenotare(p) else "✅ Sì, sposta",
                                "callback_data": f"vs:{p['id']}:{token}:{indice}"},
                               {"text": "No", "callback_data": f"vn:{p['id']}"}]])

    def conferma_data(self, p, token, indice):
        d, v = self.data_mostrata(p, token, indice)
        if not v:
            return
        if p["stato"] not in ("attivo", "pausa"):
            self.dire(p, "Registrazione non completa: non posso prenotare.")
            return
        self.date_mostrate.pop(p["id"], None)  # niente doppie prenotazioni dallo stesso elenco
        self.prenota_vista(p, v["k"], d["att"])

    def controlla_ora(self, p):
        ultimo = (p.get("ultimo") or {}).get("ts", 0)
        pausa = min(PAUSA_CONTROLLA, self.intervallo_di(p["chat_id"]) * 60)
        if self.offerta_valida(p["id"]):
            self.dire(p, "C'e' un'offerta aperta: usa i suoi pulsanti (o Ignora) prima di un nuovo controllo.")
        elif time.time() - ultimo < pausa:
            # ogni controllo tiene bloccata una data: niente controlli a raffica
            self.dire(p, f"Ultimo controllo alle {orario(ultimo):%H:%M}: il prossimo e' possibile dalle "
                         f"{orario(ultimo + pausa):%H:%M}.")
        else:
            self.dire(p, "Controllo in corso…")
            self.controlla(p, manuale=True)

    # --- pannello fissato: una sola vista, aggiornata sul posto -------------------------
    def regola(self, p):
        """La regola con cui il bot sta cercando, ripetuta in ogni avviso."""
        att = attuale_di(p)
        z = descr_zona(zona_di(p), att)
        d = dal_giorno(p)
        return (f"🔎 {z[0].upper()}{z[1:]} · ⚡ " +
                (f"prenoto da solo date da {GIORNI[d.weekday()]} {d:%d/%m} in poi" if d else "decidi tu"))

    def riassunto(self, p):
        r = p.get("riassunto")
        if p["stato"] == "pausa":
            return "⏸ Controlli in pausa"
        if not r:
            return "⏱ Primo controllo tra poco"
        ora = orario(r["ts"]).strftime("%H:%M")
        if r.get("errore"):
            return f"⏱ {ora} · il portale non ha risposto, riprovo da solo"
        att, zona = attuale_di(p), zona_di(p)
        pezzi = [("1 data trovata" if r['viste'] == 1 else f"{r['viste']} date trovate") + (" in Piemonte" if r.get("estesa") else "")]
        if area_breve(zona, att):
            pezzi.append(f"{r['area']} {area_breve(zona, att)}")
        if da_prenotare(p):
            pezzi.append(f"✅ {r['migliori']} prenotabili dove cerchi" if r["migliori"] else "nessuna dove cerchi")
        elif r["migliori"]:
            pezzi.append(f"✅ {r['migliori']} prima della tua")
        elif r.get("prima_area") and area_breve(zona, att):
            pezzi.append(f"la prima {area_breve(zona, att)} e' {r['prima_area']}, dopo la tua")
        else:
            pezzi.append("nessuna prima della tua")
        return f"⏱ {ora} · " + ", ".join(pezzi)

    def scheda(self, p):
        if p["stato"] in REGISTRAZIONE or not p.get("attuale"):
            return f"👤 {self.nome(p)}\n📝 Registrazione in corso"
        att = attuale_di(p)
        if da_prenotare(p):
            righe = [f"👤 {self.nome(p)} — {prestazione(att.cosa) or 'ricetta'}",
                     "📅 da prenotare: cerco il primo appuntamento libero",
                     f"🔎 Cerco: {descr_zona(zona_di(p), att)}"]
        else:
            righe = [f"👤 {self.nome(p)} — {prestazione(att.cosa)}",
                     f"📅 {fmt(att.quando)}",
                     f"📍 {titolo(att.luogo.sede)}, {indirizzo(att.luogo)}",
                     f"🔎 Cerco: {descr_zona(zona_di(p), att)}"]
        if cup_http.estensioni(zona_di(p)):
            righe.append("     (allargo la ricerca a tutto il Piemonte, poi filtro)")
        righe.append(f"⚡ Prenoto da solo: {auto_descr(p)}")
        righe.append(self.riassunto(p))
        return "\n".join(righe)

    def pulsanti_ricetta(self, p):
        """I pulsanti di una ricetta: due righe di due (con nomi interi quattro in fila non ci stanno sul
        telefono) e, se l'ultimo controllo ha trovato date, quello per vederle e prenotarle."""
        v = versione(p)
        righe = [[{"text": "🔎 Dove cerco", "callback_data": f"sc:sede:{p['id']}:{v}"},
                  {"text": "⚡ Prenoto da solo", "callback_data": f"sc:auto:{p['id']}:{v}"}],
                 [{"text": "▶️ Riprendi" if p["stato"] == "pausa" else "⏸ Pausa",
                   "callback_data": f"sc:{'riprendi' if p['stato'] == 'pausa' else 'pausa'}:{p['id']}:{v}"},
                  {"text": "🔄 Controlla ora", "callback_data": f"sc:controlla:{p['id']}:{v}"}]]
        if p.get("viste"):
            n = len(p["viste"])
            righe.append([{"text": f"📅 {n} {'data trovata' if n == 1 else 'date trovate'}: vedi e prenota",
                           "callback_data": f"sc:date:{p['id']}:{v}"}])
        return righe

    def testo_pannello(self, chat):
        pratiche = self.store.della_chat(chat)
        if not pratiche:
            return None, None
        attive = [p for p in pratiche if p["stato"] in ("attivo", "pausa")]
        righe = []
        if len(pratiche) > PANNELLO_COMPLETO:
            # tante ricette: una riga e un pulsante ciascuna, il pulsante apre la scheda con tutti i suoi pulsanti
            for p in attive:
                righe.append([{"text": f"👤 {self.nome(p)}", "callback_data": f"sc:menu:{p['id']}:{versione(p)}"}])
            schede = [f"👤 {self.nome(p)} · " + (self.riassunto(p) if p.get("attuale") and p["stato"] not in REGISTRAZIONE
                                                  else "📝 Registrazione in corso") for p in pratiche]
            sotto = "\n\nTocca una ricetta per i suoi pulsanti."
        else:
            for p in attive:
                if len(pratiche) > 1:  # con piu' ricette, una riga col nome sopra i suoi pulsanti
                    righe.append([{"text": f"👤 {self.nome(p)}", "callback_data": "pn:nome"}])
                righe += self.pulsanti_ricetta(p)
            schede, sotto = [self.scheda(p) for p in pratiche], ""
        righe = righe[:MAX_PULSANTI]
        if self.webapp_url:
            righe = [[{"text": "📱 Apri l'app", "web_app": {"url": self.webapp_url}}]] + righe
        testo = f"📋 Le tue ricette · aggiornato alle {adesso():%H:%M}\n\n" + "\n\n".join(schede)
        if len(testo) + len(sotto) > 4000:
            testo = testo[:3900 - len(sotto)].rsplit("\n", 1)[0] + "\n…"
        testo += sotto
        return testo, righe

    def aggiorna_pannello(self, chat, nuovo=False):
        """Aggiorna il messaggio fissato; con nuovo=True lo rimanda in fondo alla chat e lo fissa di nuovo."""
        testo, righe = self.testo_pannello(chat)
        mid = self.store.pannello(chat)
        if not testo:
            if mid:
                self.tg("unpinChatMessage", chat_id=chat, message_id=mid)
                self.store.set_pannello(chat, None)
            return
        markup = {"inline_keyboard": righe or []}
        if mid and not nuovo:
            r = self.tg("editMessageText", chat_id=chat, message_id=mid, text=testo[:4000], reply_markup=markup)
            if r.get("ok") or "not modified" in (r.get("description") or ""):
                return
        if mid:
            self.tg("deleteMessage", chat_id=chat, message_id=mid)
        r = self.tg("sendMessage", chat_id=chat, text=testo[:4000], reply_markup=markup)
        nuovo_id = (r.get("result") or {}).get("message_id")
        if nuovo_id:
            self.tg("pinChatMessage", chat_id=chat, message_id=nuovo_id, disable_notification=True)
        self.store.set_pannello(chat, nuovo_id)

    def scegli(self, chat, azione, pratiche):
        righe = [[{"text": self.nome(p), "callback_data": f"sc:{azione}:{p['id']}:{versione(p)}"}]
                 for p in pratiche][:MAX_PULSANTI]
        if azione == "cancella":
            righe.append([{"text": "Tutte, e tutti i miei dati", "callback_data": "del:tutte:1"}])
        self.send(chat, "Per quale ricetta?", righe)

    # --- messaggi e pulsanti ------------------------------------------------------------
    def on_message(self, msg):
        if msg.get("chat", {}).get("type") != "private" or "text" not in msg:
            return
        chat = msg["chat"]["id"]
        testo = msg["text"].strip()
        cmd = testo.split()[0].split("@")[0].lower() if testo.startswith("/") else ""
        pratiche = self.store.della_chat(chat)
        reg = next((p for p in pratiche if p["stato"] in REGISTRAZIONE), None)
        compatto = "".join(testo.split()).upper()
        if not cmd and (cup_http.CF_RE.match(compatto) or cup_http.NRE_RE.match(compatto)) and \
                not (reg and reg["stato"] in ("cf", "nre")):
            # un codice fiscale o una ricetta scritti fuori dalla registrazione non restano in chat
            self.tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        ora = time.time()
        if ora - self.ultimo_msg.get(chat, 0) < PAUSA_MESSAGGI and not reg:
            return  # raffica: ignorata
        self.ultimo_msg[chat] = ora

        if cmd == "/start":
            if reg:
                self.send(chat, "Riprendiamo la registrazione.")
                self.riprendi_registrazione(reg)
            elif pratiche:
                self.send(chat, "Sei gia' registrato.\n\n" + AIUTO)
            else:
                self.send(chat, self.privacy(), [[{"text": "Accetto", "callback_data": "consenso:1"},
                                                  {"text": "Non accetto", "callback_data": "consenso:0"}]])
            return
        if cmd == "/privacy":
            self.send(chat, self.privacy())
            return
        if cmd == "/help":
            self.send(chat, AIUTO)
            return
        if cmd == "/admin" and self.admin and str(chat) == self.admin:
            self.send(chat, f"Chat registrate: {self.store.chat_count()} / {self.max_utenti}. Ricette seguite: "
                            f"{self.store.count()}. Offerte aperte: {sum(self.offerta_valida(k) for k in list(self.offerte))}.")
            return
        if not pratiche:
            self.send(chat, "Scrivi /start per iniziare.")
            return
        attende = next((p for p in pratiche if p.get("attende_comune")), None)
        if attende and (cmd or time.time() - attende["attende_comune"] > ATTESA_COMUNE):
            attende.pop("attende_comune")  # un comando o troppo tempo: la richiesta del comune decade
            self.salva(attende, "attende_comune")
            attende = None
        if attende and not reg and not cmd:
            self.ricevi_comune(attende, testo)
            return
        if reg and not cmd:
            if reg["stato"] in ("cf", "nre"):
                # i dati personali non restano nella chat
                self.tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
            {"cf": self.ricevi_cf, "nre": self.ricevi_nre, "nome": self.ricevi_nome,
             "comune": self.ricevi_comune}.get(reg["stato"], lambda p, t: self.riprendi_registrazione(p))(reg, testo)
            return
        if cmd == "/cancella":
            if reg:
                self.esegui("cancella", reg)
            elif len(pratiche) == 1:
                self.send(chat, "Vuoi davvero cancellare tutti i tuoi dati? I controlli si fermano.",
                          [[{"text": "Si', cancella tutto", "callback_data": "del:tutte:1"},
                            {"text": "No", "callback_data": "del:no"}]])
            else:
                self.scegli(chat, "cancella", pratiche)
            return
        if reg and cmd not in ("/stato", "/dati"):
            self.send(chat, "Completa prima la registrazione della ricetta (o /cancella per annullarla).")
            self.riprendi_registrazione(reg)
            return
        attive = [p for p in pratiche if p["stato"] not in REGISTRAZIONE]
        if cmd == "/aggiungi":
            if self.piena(len(pratiche)):
                self.send(chat, f"Puoi seguire al massimo {self.max_pratiche} ricette. /cancella per toglierne una.")
            else:
                self.send(chat, "Nuova ricetta da seguire: tua o di un familiare che ti ha autorizzato.")
                self.chiedi_cf(self.store.new(chat))
        elif cmd == "/dati":
            for p in pratiche:
                att = attuale_di(p)
                self.dire(p, f"Codice fiscale: {maschera(p.get('cf', ''))}\nRicetta (NRE): {maschera(p.get('nre', ''))}\n"
                             f"Dove cerco: {descr_zona(zona_di(p), att)}\n"
                             f"Controlli: {'in pausa' if p['stato'] == 'pausa' else f'ogni {self.intervallo_di(chat)} minuti'}\n"
                             f"Conferma automatica: {auto_descr(p)}\n\n"
                             + (descrivi_prenotazione(att) if att else "Registrazione non completata."))
        elif cmd == "/stato":
            self.aggiorna_pannello(chat, nuovo=True)
        elif cmd == "/controlla":
            for p in attive:
                self.controlla_ora(p)
        elif cmd[1:] in self.AZIONI:
            if len(attive) == 1:
                self.esegui(cmd[1:], attive[0])
            else:
                self.scegli(chat, cmd[1:], attive)
        else:
            self.send(chat, AIUTO)

    def riprendi_registrazione(self, p):
        if p["stato"] == "cf":
            self.send(p["chat_id"], "Mandami il codice fiscale della persona a cui e' intestata la ricetta.")
        elif p["stato"] == "nre":
            self.send(p["chat_id"], "Mandami il numero della ricetta elettronica (NRE), 15 caratteri.")
        elif p["stato"] == "nome":
            self.send(p["chat_id"], "Come chiamo questa ricetta nei messaggi? Per esempio: Papà.")
        elif p["stato"] == "comune":
            self.send(p["chat_id"], "Scrivi il comune in cui cercare, per esempio: Torino.")
        elif p.get("attuale"):
            self.chiedi_sede(p, attuale_di(p))

    def on_callback(self, cq):
        self.tg("answerCallbackQuery", callback_query_id=cq["id"])
        msg = cq.get("message") or {}
        chat = cq["from"]["id"]
        if msg.get("chat", {}).get("id") != chat:  # solo chat private
            return
        parts = cq.get("data", "").split(":")
        pannello = msg.get("message_id") == self.store.pannello(chat)
        # i pulsanti del pannello fissato restano: si tolgono solo quelli dei messaggi a un solo uso
        togli_pulsanti = lambda: None if pannello else self.tg(
            "editMessageReplyMarkup", chat_id=chat, message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
        kind = parts[0]
        if kind == "consenso":
            togli_pulsanti()
            if self.store.della_chat(chat):
                return
            if parts[1:] != ["1"]:
                self.send(chat, "Va bene, non ho conservato nulla. Se cambi idea scrivi /start.")
            elif self.store.chat_count() >= self.max_utenti:
                self.send(chat, "Mi dispiace, al momento non accetto nuovi utenti.")
            else:
                self.chiedi_cf(self.store.new(chat, consenso_ts=time.time()))
            return
        if parts[:3] == ["del", "tutte", "1"]:
            togli_pulsanti()
            if self.store.della_chat(chat):
                for p in self.store.della_chat(chat):
                    self.scarta(p["id"])
                mid = self.store.pannello(chat)
                if mid:
                    self.tg("unpinChatMessage", chat_id=chat, message_id=mid)
                self.store.delete_chat(chat)
                self.send(chat, "Fatto: ho cancellato tutti i tuoi dati. Se ti serve di nuovo, scrivi /start.")
            return
        if parts == ["del", "no"]:
            togli_pulsanti()
            return
        p = self.della_chat(chat, parts[2] if kind == "sc" and len(parts) > 2 else parts[1] if len(parts) > 1 else None)
        if not p:
            togli_pulsanti()
            return
        if kind in ("sc", "sede", "auto", "del"):
            # questi pulsanti portano la versione della ricetta: dopo /modifica i vecchi non valgono
            v = parts[3] if kind == "sc" and len(parts) > 3 else parts[2] if kind != "sc" and len(parts) > 2 else None
            if v != versione(p):
                togli_pulsanti()
                self.dire(p, "Questo pulsante e' di un messaggio vecchio: usa il comando di nuovo.")
                return
            parts = parts[:3] if kind == "sc" else [parts[0], parts[1]] + parts[3:]
        if kind == "sc" and len(parts) == 3 and parts[1] in self.AZIONI:
            togli_pulsanti()
            if p["stato"] not in REGISTRAZIONE or parts[1] == "cancella":
                self.esegui(parts[1], p)
        elif kind == "del" and len(parts) == 3:
            togli_pulsanti()
            if parts[2] == "1":
                self.scarta(p["id"])
                self.store.delete(p["id"])
                resto = self.store.della_chat(chat)
                self.send(chat, f"Fatto: ho cancellato i dati di \"{self.nome(p)}\"." +
                          ("" if resto else " Non seguo piu' nessuna ricetta: per ricominciare scrivi /start."))
                self.aggiorna_pannello(chat)
        elif kind == "sede" and len(parts) == 3 and p.get("attuale") and \
                p["stato"] in ("sede", "attivo", "pausa") and parts[2] in ("sede", "comune", "provincia", "tutte", "altro"):
            togli_pulsanti()
            att = attuale_di(p)
            if parts[2] == "altro":
                if p["stato"] == "sede":
                    p["stato"] = "comune"  # ancora in registrazione
                else:
                    p["attende_comune"] = time.time()  # ricetta gia' attiva: cambia solo l'area, lo stato resta
                self.salva(p, "stato", "attende_comune")
                self.dire(p, "Scrivi il comune in cui cercare, per esempio: Torino.")
                return
            valore = {"sede": att.luogo.sede, "comune": cup_http.comune(att.luogo),
                      "provincia": cup_http.provincia(att.luogo), "tutte": ""}[parts[2]]
            if parts[2] != "tutte" and not valore:
                self.dire(p, "Non riesco a leggere questo dato dall'indirizzo della prenotazione: scegli un'altra opzione.")
                self.chiedi_sede(p, att)
                return
            self.imposta_zona(p, {"tipo": parts[2], "valore": valore})
        elif kind == "auto" and len(parts) == 3 and p["stato"] in ("attivo", "pausa") and \
                parts[2] in {"0", *map(str, ANTICIPI_AUTO)}:
            togli_pulsanti()
            giorni = int(parts[2])
            p["auto"] = {"giorni": giorni} if giorni else None
            self.salva(p, "auto")
            if giorni:
                self.dire(p, ("⚡ Conferma automatica attiva: prenoto da solo la prima data libera dove cerchi.\n"
                              if da_prenotare(p) else
                              "⚡ Conferma automatica attiva: prenoto da solo la prima data prima di quella attuale.\n")
                             + self.regola(p))
            else:
                self.dire(p, "Conferma automatica disattivata: ti mando il pulsante e decidi tu.")
            self.aggiorna_pannello(chat)
        elif kind in ("p", "x"):
            self.on_offerta(p, parts, togli_pulsanti)
        elif kind == "vd" and len(parts) == 4:  # una data dell'elenco /date: prima la conferma
            self.chiedi_conferma_data(p, parts[2], parts[3])
        elif kind == "vs" and len(parts) == 4:
            togli_pulsanti()  # niente doppi tocchi
            self.conferma_data(p, parts[2], parts[3])
        elif kind == "vn":
            togli_pulsanti()
            self.dire(p, "Va bene, non prenoto.")

    def on_offerta(self, p, parts, togli_pulsanti):
        togli_pulsanti()  # niente doppi tocchi
        self.usa_offerta(p, parts[0], parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")

    def usa_offerta(self, p, tipo, token, indice, chiave=None):
        """tipo "p" = prenota la data numero `indice`, "x" = ignora. Stessa strada per chat e Mini App.
        chiave: se data (Mini App), la data a quell'indice dev'essere proprio quella mostrata all'utente."""
        o = self.offerte.get(p["id"])
        valida = o and token == o["token"] and (
            tipo == "x" or (tipo == "p" and str(indice).isdecimal() and int(indice) < len(o["slots"])
                            and (chiave is None or o["slots"][int(indice)].key() == chiave)))
        if not valida:
            self.dire(p, "Questa offerta non e' piu' valida.")
            return
        del self.offerte[p["id"]]
        if tipo == "x":
            p["ignorati"] = sorted(set(p.get("ignorati", [])) | {x.key() for x in o["slots"]})
            self.salva(p, "ignorati")
            self.dire(p, "Ok, non ti ripropongo queste date.")
            return
        if time.time() - o["ts"] > TTL_OFFERTA:
            self.dire(p, "Offerta scaduta. Se la data c'e' ancora te la ripropongo al prossimo controllo.")
            return
        if p["stato"] not in ("attivo", "pausa"):
            self.dire(p, "Registrazione non completa: non posso prenotare.")
            return
        self.prenota(p, o["slots"][int(indice)], o["sessione"])

    def cerca_da_app(self, chat, pid, token, cf, nre, nome, modo, chiesta=0.0, consenso=False):
        """Nuova ricetta (modo "nuova") o cambio di ricetta (modo "modifica") chiesti dalla Mini App.
        Codice fiscale e NRE arrivano solo in memoria; l'esito per l'app non li contiene."""
        def esito(**kw):
            self.risultati[token] = {"chat": chat, "ts": time.time(), **kw}
        ora = time.time()
        for k in [k for k, v in self.risultati.items() if v["ts"] < ora - 900]:
            self.risultati.pop(k, None)
        if self.cancellate.get(chat, 0) >= chiesta:
            return  # l'utente ha cancellato tutti i dati dopo aver chiesto la ricerca: cf e nre si buttano
        tutte = [t for t in self.ricerche_app.get(chat, []) if t > ora - 86400]
        if len(tutte) >= MAX_RICERCHE_APP:
            return esito(errore="Troppe ricerche oggi. Riprova domani.")
        self.ricerche_app[chat] = tutte + [ora]
        pratiche = self.store.della_chat(chat)
        if modo == "nuova":
            if not pratiche and self.store.chat_count() >= self.max_utenti:
                return esito(errore="Mi dispiace, al momento non accetto nuovi utenti.")
            if self.piena(len(pratiche)):
                return esito(errore=f"Puoi seguire al massimo {self.max_pratiche} ricette.")
        else:
            p = self.della_chat(chat, pid)
            if not p or p["stato"] not in ("attivo", "pausa"):
                return esito(errore="Ricetta non trovata.")
        recenti = [t for t in self.ricerche_fallite.get(chat, []) if t > ora - 86400]
        self.ricerche_fallite[chat] = recenti
        if len(recenti) >= MAX_RICERCHE_FALLITE:
            return esito(errore="Troppe ricerche non riuscite oggi. Riprova domani.")
        try:
            att, cosa = self.cerca_ricetta(cf, nre)
        except cup_http.NonAttiva as e:
            recenti.append(ora)
            return esito(errore=f"La prenotazione di questa ricetta non è attiva ({e}).")
        except cup_http.NonTrovata as e:
            recenti.append(ora)
            return esito(errore=f"Il portale non accetta questa ricetta con questo codice fiscale ({e}).")
        except (cup_http.CupError, requests.RequestException):
            return esito(errore="Il portale CUP non risponde: riprova tra qualche minuto.")
        nuovi = {"cf": cf, "nre": nre, "attuale": pren_to_dict(att) if att else senza_prenotazione(cosa),
                 "da_prenotare": not att, "auto": None, "notificati": {}, "ignorati": [],
                 "tentati_auto": [], "creato": time.time(), "errori": 0}
        try:
            if modo == "nuova":
                p = {"chat_id": chat, "stato": "sede", "prossimo": 0, "nome": nome, **nuovi}
                if consenso:
                    p["consenso_ts"] = ora
                self.store.save(p)  # "sede": l'app chiede subito dove cercare, poi diventa attiva
            else:
                def cambia(f):
                    z, vecchia = zona_di(f), attuale_di(f)
                    if z["tipo"] in ("sede", "comune", "provincia") and not z["valore"] and vecchia:
                        # la zona era "quella della prenotazione": con la ricetta nuova vale quella di prima
                        z["valore"] = {"sede": vecchia.luogo.sede, "comune": cup_http.comune(vecchia.luogo),
                                       "provincia": cup_http.provincia(vecchia.luogo)}[z["tipo"]]
                    if not att and z["tipo"] != "tutte" and not z["valore"]:
                        z = {"tipo": "tutte", "valore": ""}  # senza prenotazione non c'e' una sede da cui ricavarla
                    f.update(nuovi, zona=z)
                    for k in ("libera", "viste", "storico", "riassunto", "ultimo", "luoghi"):
                        f.pop(k, None)
                p = self.store.modifica(pid, cambia)
                self.scarta(pid)
        except storemod.GiaRegistrata:
            return esito(errore="Questa ricetta è già seguita dal bot (in questa o in un'altra chat).")
        log.info("ricetta %s/%s %s dalla Mini App", uid(chat), p["id"], "aggiunta" if modo == "nuova" else "cambiata")
        esito(pid=p["id"])
        self.aggiorna_pannello(chat)

    def esegui_coda(self):
        """Azioni arrivate dalla Mini App. Ognuna porta la chat che l'ha chiesta: si ricontrolla che la
        ricetta sia sua anche qui, non solo nella Mini App. Al massimo AZIONI_PER_GIRO per volta, poi si
        torna a Telegram e ai controlli."""
        ora = time.time()
        for diz in (self.ricerche_fallite, self.ricerche_app):  # niente crescita senza fine
            for k in [k for k, v in diz.items() if not v or v[-1] < ora - 86400]:
                diz.pop(k, None)
        for k in [k for k, t in self.cancellate.items() if t < ora - 3600]:
            self.cancellate.pop(k, None)
        for k in [k for k, v in self.sessioni.items() if ora - v["ts"] > TTL_OFFERTA and not self.offerta_valida(k)]:
            self.sessioni.pop(k, None)
        for _ in range(AZIONI_PER_GIRO):
            try:
                azione, chat, pid, *altro = self.coda.get_nowait()
            except queue.Empty:
                return
            if azione == "pannello":  # impostazioni cambiate dalla Mini App
                self.aggiorna_pannello(chat)
                continue
            if azione == "dimentica":  # ricetta cancellata dalla Mini App
                self.scarta(pid)
                self.aggiorna_pannello(chat)
                continue
            if azione == "sgancia":  # tutti i dati cancellati dalla Mini App: via anche il pannello fissato
                self.tg("unpinChatMessage", chat_id=chat, message_id=pid)
                self.tg("deleteMessage", chat_id=chat, message_id=pid)
                continue
            if azione == "cerca":
                try:
                    self.cerca_da_app(chat, pid, *altro)
                except Exception as e:
                    log.error("coda: errore imprevisto %s\n%s", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)))
                    self.risultati.setdefault(altro[0], {"chat": chat, "ts": time.time(), "errore": "Errore imprevisto: riprova."})
                continue
            p = self.della_chat(chat, pid)
            if not p or p["stato"] not in ("attivo", "pausa"):
                continue
            try:
                if azione == "controlla":
                    self.controlla_ora(p)
                elif azione == "offerta":
                    self.usa_offerta(p, *altro)
                elif azione == "vista":
                    self.prenota_vista(p, *altro)
            except Exception as e:
                log.error("coda: errore imprevisto %s\n%s", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)))
                self.alert_admin(f"Errore imprevisto in un'azione dalla Mini App: {type(e).__name__}")

    # --- ciclo principale ---------------------------------------------------------------
    def poll(self, timeout):
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query", "my_chat_member"])}
        if self.offset:
            params["offset"] = self.offset
        try:
            r = requests.get(self.api + "getUpdates", params=params, timeout=timeout + 15).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("getUpdates: %s", self.redact(e))
            time.sleep(5)
            return
        if not r.get("ok"):
            log.warning("getUpdates fallito: %s", r.get("description"))
            time.sleep(15)
            return
        for upd in r.get("result", []):
            self.offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    self.on_message(upd["message"])
                elif "callback_query" in upd:
                    self.on_callback(upd["callback_query"])
                elif "my_chat_member" in upd:
                    m = upd["my_chat_member"]
                    if m.get("new_chat_member", {}).get("status") == "kicked":
                        self.dimentica(m["chat"]["id"], "bot bloccato")
            except Exception as e:
                log.error("update: errore imprevisto %s\n%s", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)))
                self.alert_admin(f"Errore imprevisto nella gestione di un messaggio: {type(e).__name__}")

    def pulizia(self):
        if time.time() - self.ultima_pulizia < 3600:
            return
        self.ultima_pulizia = time.time()
        for pid, chat in self.store.pulizia(time.time()):
            self.scarta(pid)
            log.info("pratica %s/%s cancellata: registrazione incompleta o pausa oltre 30 giorni", uid(chat), pid)
            self.send(chat, "Ho cancellato una ricetta lasciata a meta' (o in pausa da oltre 30 giorni). "
                            "Per seguirla di nuovo: /aggiungi.")

    def controllo_pianificato(self):
        """Un solo controllo per giro, rispettando la distanza tra sessioni sul portale."""
        if time.time() - self.ultimo_portale < self.distanza:
            return False
        for pid in self.store.due(time.time()):
            if self.offerta_valida(pid):
                continue  # la sua sessione tiene la data offerta: un nuovo controllo non la vedrebbe
            p = self.store.get(pid)
            p["prossimo"] = time.time() + self.intervallo_di(p["chat_id"]) * 60 * random.uniform(0.9, 1.1)
            self.salva(p, "prossimo")
            self.controlla(p)
            return True
        return False

    def imposta_menu(self):
        """Menu dei comandi: per tutti, per le chat private e (con /admin) solo per chi gestisce il bot.
        Le app Telegram lo aggiornano quando si riapre la chat."""
        comandi = [{"command": c, "description": d} for c, d in COMANDI]
        for scope in ({"type": "default"}, {"type": "all_private_chats"}):
            self.tg("setMyCommands", commands=comandi, scope=scope)
        if self.admin:
            self.tg("setMyCommands", scope={"type": "chat", "chat_id": int(self.admin)},
                    commands=comandi + [{"command": "admin", "description": "Statistiche del bot"}])
        if self.webapp_url:
            self.tg("setChatMenuButton", menu_button={"type": "web_app", "text": "📱 App",
                                                       "web_app": {"url": self.webapp_url}})
        else:
            self.tg("setChatMenuButton", menu_button={"type": "commands"})

    def run(self):
        me = self.tg("getMe")
        if not me.get("ok"):
            raise SystemExit("Token Telegram non valido.")
        self.imposta_menu()
        log.info("Bot @%s avviato%s", me["result"]["username"], " in MODALITA' PROVA" if self.prova else "")
        while True:
            try:
                self.pulizia()
                self.esegui_coda()
                fatto = self.controllo_pianificato()
                # con la Mini App il giro e' piu' corto: le sue azioni aspettano al massimo qualche secondo
                self.poll(1 if fatto or self.store.due(time.time()) or not self.coda.empty()
                          else 3 if self.webapp_url else 25)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error("ciclo: errore imprevisto %s\n%s", type(e).__name__, "".join(traceback.format_tb(e.__traceback__)))
                time.sleep(10)


def main():
    os.umask(0o077)  # database e file creati dal bot leggibili solo dal suo utente
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Manca TELEGRAM_BOT_TOKEN")
    store = Store(os.environ.get("DB_PATH", "data/cup.db"), os.environ.get("CUP_BOT_KEY"))
    bot = Bot(store, token, admin=os.environ.get("ADMIN_CHAT_ID"), max_utenti=env_int("MAX_UTENTI", 30),
              intervallo=env_int("INTERVALLO_MIN", 45), distanza=env_int("DISTANZA_PORTALE_S", 20),
              prova=os.environ.get("MODALITA_PROVA", "0") == "1", contatto=os.environ.get("CONTATTO_GESTORE", ""),
              admin_intervallo=env_int("ADMIN_INTERVALLO_MIN", 0) or None, max_pratiche=env_int("MAX_PRATICHE", 0),
              webapp_url=os.environ.get("WEBAPP_URL", "").strip())
    if bot.webapp_url:
        import webapp
        webapp.avvia(bot, os.environ.get("DB_PATH", "data/cup.db"), os.environ.get("CUP_BOT_KEY"),
                     porta=env_int("WEBAPP_PORTA", 8095))
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Fermato.")


if __name__ == "__main__":
    main()
