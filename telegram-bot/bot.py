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
    MAX_PRATICHE         ricette per chat al massimo (default 3)
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
MAX_LUOGHI = 80  # sedi viste nei controlli, per scegliere dove cercare dall'app
ATTESA_COMUNE = 10 * 60  # secondi: dopo "Un altro comune…" il prossimo testo vale come comune solo per poco
TZ = ZoneInfo("Europe/Rome")  # gli orari del portale sono italiani, anche se il server gira in UTC


def adesso():
    return datetime.now(TZ).replace(tzinfo=None)


def orario(ts):
    return datetime.fromtimestamp(ts, TZ)

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
    "ricetta cambiano dove cercare (🔎), la conferma automatica (⚡), la pausa (⏸) o controllano subito (🔄).\n\n"
    "/stato – riporta il pannello in fondo alla chat\n"
    "/aggiungi – segui un'altra ricetta (per esempio di un familiare)\n"
    "/dati – i dati che conservo\n"
    "/modifica – cambia codice fiscale e ricetta\n"
    "/cancella – elimina una ricetta o tutti i dati\n"
    "/privacy – come tratto i tuoi dati\n\n"
    "Funzionano anche /sede, /auto, /pausa, /riprendi e /controlla.")

COMANDI = [("stato", "Il pannello delle tue ricette"), ("aggiungi", "Segui un'altra ricetta"),
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


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def fmt(d):
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


def descrivi_prenotazione(att, titolo="Prenotazione"):
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
        return f"solo in questa sede ({titolo(v or (att.luogo.sede if att else ''))})"
    if tipo == "comune":
        return f"solo nel comune di {titolo(v or (cup_http.comune(att.luogo) if att else ''))}"
    if tipo == "provincia":
        return f"in tutta la provincia ({v or (cup_http.provincia(att.luogo) if att else '')})"
    return "ovunque proponga il CUP"


def area_breve(zona, att):
    """Per il riepilogo del controllo: "18 date viste in Piemonte, 0 a Torino"."""
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
                 admin_intervallo=None, max_pratiche=3, webapp_url=""):
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
        self.metriche = collections.deque(maxlen=5000)  # (ora, secondi, riuscita) di ogni sessione sul portale

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
    def portale(self, fn, *args, **kwargs):
        attesa = self.distanza - (time.time() - self.ultimo_portale)
        if attesa > 0:
            time.sleep(attesa)
        inizio, riuscita = time.time(), False
        try:
            risultato = fn(*args, **kwargs)
            riuscita = True
            return risultato
        except (cup_http.NonTrovata, cup_http.NonAttiva):
            riuscita = True  # il portale ha risposto: e' la ricetta che non va
            raise
        finally:
            self.ultimo_portale = time.time()
            self.metriche.append((inizio, self.ultimo_portale - inizio, riuscita))
            try:
                self.store.metrica(inizio, self.ultimo_portale - inizio, riuscita)  # sopravvive ai riavvii
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
        try:
            res = self.portale(cup_http.check, p["cf"], p["nre"], zona_di(p))
        except (cup_http.NonTrovata, cup_http.NonAttiva) as e:
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
            self.salva(p, "errori", "ultimo", "riassunto")
            self.aggiorna_pannello(chat)
            log.info("controllo %s/%s: errore %d: %s", uid(chat), p["id"], p["errori"], type(e).__name__)
            if manuale or p["errori"] in AVVISA_ERRORI:
                motivo = "il portale CUP non risponde" if isinstance(e, requests.RequestException) else str(e)
                self.dire(p, f"⚠️ {motivo[0].upper()}{motivo[1:]}" + (
                    "" if manuale else f" (da {p['errori']} controlli di fila). Continuo a riprovare da solo."))
            return None

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
        p.update(errori=0, attuale=pren_to_dict(att), ultimo={"ts": time.time(), "testo": descrivi(res)},
                 riassunto={"ts": time.time(), "viste": len(res["slots"]), "area": len(nell_area),
                            "migliori": len(res["migliori"]), "estesa": bool(cup_http.estensioni(zona)),
                            "prima_area": fmt(nell_area[0].quando) if nell_area else ""})
        log.info("controllo %s/%s: %d date, %d migliori", uid(chat), p["id"], len(res["slots"]), len(res["migliori"]))
        notificati = p.get("notificati") or {}  # data -> quando e' stata offerta l'ultima volta
        if isinstance(notificati, list):
            notificati = dict.fromkeys(notificati, 0)
        ignorati = set(p.get("ignorati", []))
        self.salva(p, "errori", "attuale", "ultimo", "riassunto", "viste", "luoghi", "storico")
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
                self.dire(p, "⚡ Conferma automatica: ho trovato una data prima.\n\n" + descrivi(res) +
                          "\n\n" + self.regola(p))
                if self.prenota(p, slot, res["sessione"], automatica=True) != "fallita":
                    return res
                p = self.store.get(p["id"])
                if not p:
                    return res
        # si ripropone una data se la sua offerta e' scaduta o persa (es. riavvio), non se l'utente l'ha ignorata
        ora = time.time()
        nuove = [x for x in res["migliori"] if x.key() not in ignorati and ora - notificati.get(x.key(), 0) > TTL_OFFERTA]
        if nuove and self.offri(p, res, ignorati):
            notificati.update({x.key(): ora for x in res["migliori"]})
            p["notificati"] = notificati
            self.salva(p, "notificati")
        elif manuale:
            self.dire(p, descrivi(res) + "\n\n" + self.regola(p))
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
        ok = self.dire(p, "🎉 C'e' una data PRIMA!\n\n" + descrivi(res) + "\n\n" + self.regola(p) +
                       f"\n\nTocca per spostare la prenotazione (valido {TTL_OFFERTA // 60} minuti).{prova}", buttons)
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
            self.dire(p, "Le date viste sono di un controllo vecchio: tocca 🔄 Ora e riprova.")
            return "fallita"
        uguali = [x for x in s["slots"] if x.key() == chiave]
        if len(uguali) != 1 or not (uguali[0].proposta or uguali[0].seleziona_id):
            self.dire(p, "Questa data non è più prenotabile dal controllo di prima: tocca 🔄 Ora e riprova.")
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
        self.dire(p, f"Sposto la prenotazione a:\n📅 {fmt(slot.quando)}\n📍 {slot.luogo}…")
        attuale_db = self.store.get(p["id"])
        if not attuale_db:
            log.info("prenotazione %s/%s annullata: dati cancellati nel frattempo", uid(chat), p["id"])
            return "fallita"
        if automatica and (not attuale_db.get("auto") or attuale_db["stato"] != "attivo"):
            self.dire(p, "Nel frattempo hai spento la conferma automatica o messo in pausa: non prenoto da solo.")
            return "fallita"
        try:
            esito = self.portale(cup_http.prenota, p["cf"], p["nre"], slot, sessione=sessione,
                                 zona=zona_di(p), dry_run=self.prova, libera=libera)
        except (cup_http.CupError, requests.RequestException) as e:
            urgente = "Conferma inviata" in str(e)
            self.dire(p, ("🚨 " if urgente else "❌ Non spostata: ") + str(e))
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
        self.salva(p, "notificati", "ignorati", "tentati_auto", "attuale", "prossimo")
        self.dire(p, f"✅ Prenotazione spostata{' (conferma automatica)' if automatica else ''}!\n"
                     f"📅 {fmt(slot.quando)}\n📍 {slot.luogo}\n\n"
                     "Arriveranno SMS/email dal CUP con il nuovo promemoria; controlla anche il codice di "
                     "pagamento del ticket. Se non si puo' andare, disdire o spostare almeno 2 giorni lavorativi "
                     "prima. Continuo a cercare date ancora prima.\n\n" + self.regola(p))
        self.aggiorna_pannello(chat)
        return "ok"

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
        p.pop("viste", None)  # le date viste erano della ricetta vecchia
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
        self.send(chat, "Cerco la prenotazione sul portale CUP…")
        try:
            att = self.portale(cup_http.cerca, p["cf"], nre)
        except cup_http.NonTrovata:
            recenti.append(time.time())
            self.send(chat, "Il portale non trova prenotazioni con questo codice fiscale e questa ricetta.\n"
                            "Il bot funziona solo per appuntamenti gia' prenotati sul CUP Piemonte: per ora non "
                            "cerca il primo appuntamento di una ricetta non ancora prenotata.")
            self.chiedi_cf(p)
            return
        except cup_http.NonAttiva as e:
            recenti.append(time.time())
            self.send(chat, f"La prenotazione di questa ricetta non e' attiva ({e}).")
            self.chiedi_cf(p)
            return
        except (cup_http.CupError, requests.RequestException) as e:
            self.send(chat, f"Il portale CUP non risponde ({e}). Rimandami il numero ricetta tra qualche minuto.")
            return
        altre = [x for x in self.store.della_chat(chat) if x["id"] != p["id"]]
        p.update(nre=nre, attuale=pren_to_dict(att), stato="nome" if altre and not p.get("nome") else "sede")
        try:
            self.store.save(p)
        except storemod.GiaRegistrata:
            self.send(chat, "Questa ricetta e' gia' seguita dal bot (in questa o in un'altra chat).")
            self.chiedi_cf(self.store.get(p["id"]))
            return
        self.send(chat, descrivi_prenotazione(att, "Ho trovato la prenotazione"))
        if p["stato"] == "nome":
            self.send(chat, "Come chiamo questa ricetta nei messaggi? Per esempio: Papà, Mamma, Nonna.")
        else:
            self.chiedi_sede(p, att)

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
        self.dire(p, f"Ok: cerco {descr_zona(zona, att)}." +
                  (f"\n\nFatto! Controllo ogni {self.intervallo_di(p['chat_id'])} minuti e ti scrivo appena esce una "
                   f"data prima del {fmt(att.quando)}.\n\n{AIUTO}" if nuova else ""))
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
        self.dire(p, AUTO_TESTO + f"\n\nStato attuale: {auto_descr(p)}.", righe)

    def privacy(self):
        return PRIVACY + (f"\n\nGestore del bot: {self.contatto}" if self.contatto else "")

    # --- azioni su una pratica (dopo la scelta, se ce n'e' piu' d'una) ------------------
    AZIONI = ("sede", "auto", "pausa", "riprendi", "modifica", "cancella", "controlla")

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
        pezzi = [f"{r['viste']} date viste" + (" in Piemonte" if r.get("estesa") else "")]
        if area_breve(zona, att):
            pezzi.append(f"{r['area']} {area_breve(zona, att)}")
        if r["migliori"]:
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
        righe = [f"👤 {self.nome(p)} — {prestazione(att.cosa)}",
                 f"📅 {fmt(att.quando)}",
                 f"📍 {titolo(att.luogo.sede)}, {indirizzo(att.luogo)}",
                 f"🔎 Cerco: {descr_zona(zona_di(p), att)}"]
        if cup_http.estensioni(zona_di(p)):
            righe.append("     (allargo la ricerca a tutto il Piemonte, poi filtro)")
        righe.append(f"⚡ Prenoto da solo: {auto_descr(p)}")
        righe.append(self.riassunto(p))
        return "\n".join(righe)

    def testo_pannello(self, chat):
        pratiche = self.store.della_chat(chat)
        if not pratiche:
            return None, None
        righe = [[{"text": "🔎 Dove", "callback_data": f"sc:sede:{p['id']}:{versione(p)}"},
                  {"text": "⚡ Auto", "callback_data": f"sc:auto:{p['id']}:{versione(p)}"},
                  {"text": "▶️ Riprendi" if p["stato"] == "pausa" else "⏸ Pausa",
                   "callback_data": f"sc:{'riprendi' if p['stato'] == 'pausa' else 'pausa'}:{p['id']}:{versione(p)}"},
                  {"text": "🔄 Ora", "callback_data": f"sc:controlla:{p['id']}:{versione(p)}"}]
                 for p in pratiche if p["stato"] in ("attivo", "pausa")]
        if len(pratiche) > 1:  # con piu' ricette, una riga col nome sopra i suoi pulsanti
            attive = [p for p in pratiche if p["stato"] in ("attivo", "pausa")]
            righe = [r for p, pulsanti in zip(attive, righe)
                     for r in ([{"text": f"👤 {self.nome(p)}", "callback_data": "pn:nome"}], pulsanti)]
        if self.webapp_url:
            righe = [[{"text": "📱 Apri l'app", "web_app": {"url": self.webapp_url}}]] + righe
        testo = (f"📋 Le tue ricette · aggiornato alle {adesso():%H:%M}\n\n" +
                 "\n\n".join(self.scheda(p) for p in pratiche))
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
        righe = [[{"text": self.nome(p), "callback_data": f"sc:{azione}:{p['id']}:{versione(p)}"}] for p in pratiche]
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
            if len(pratiche) >= self.max_pratiche:
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
        togli_pulsanti = lambda: self.tg("editMessageReplyMarkup", chat_id=chat, message_id=msg["message_id"],
                                         reply_markup={"inline_keyboard": []})
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
                self.dire(p, "⚡ Conferma automatica attiva: prenoto da solo la prima data prima di quella attuale.\n"
                             + self.regola(p))
            else:
                self.dire(p, "Conferma automatica disattivata: ti mando il pulsante e decidi tu.")
            self.aggiorna_pannello(chat)
        elif kind in ("p", "x"):
            self.on_offerta(p, parts, togli_pulsanti)

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
            if len(pratiche) >= self.max_pratiche:
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
            att = self.portale(cup_http.cerca, cf, nre)
        except cup_http.NonTrovata:
            recenti.append(ora)
            return esito(errore="Il portale non trova prenotazioni con questo codice fiscale e questa ricetta. "
                                "Il bot segue solo appuntamenti già prenotati sul CUP Piemonte.")
        except cup_http.NonAttiva as e:
            recenti.append(ora)
            return esito(errore=f"La prenotazione di questa ricetta non è attiva ({e}).")
        except (cup_http.CupError, requests.RequestException):
            return esito(errore="Il portale CUP non risponde: riprova tra qualche minuto.")
        nuovi = {"cf": cf, "nre": nre, "attuale": pren_to_dict(att), "auto": None, "notificati": {}, "ignorati": [],
                 "tentati_auto": [], "creato": time.time(), "errori": 0}
        try:
            if modo == "nuova":
                p = {"chat_id": chat, "stato": "sede", "prossimo": 0, "nome": nome, **nuovi}
                if consenso:
                    p["consenso_ts"] = ora
                self.store.save(p)  # "sede": l'app chiede subito dove cercare, poi diventa attiva
            else:
                def cambia(f):
                    f.update(nuovi)
                    for k in ("libera", "viste", "storico", "riassunto", "ultimo"):
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
              admin_intervallo=env_int("ADMIN_INTERVALLO_MIN", 0) or None, max_pratiche=env_int("MAX_PRATICHE", 3),
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
