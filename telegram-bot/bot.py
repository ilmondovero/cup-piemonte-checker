"""Bot Telegram multiutente per CUP Piemonte: avvisa quando si libera una data prima della
prenotazione di ciascun utente e, se l'utente tocca il pulsante, sposta la prenotazione.

Configurazione da variabili d'ambiente (vedi .env.example):
    TELEGRAM_BOT_TOKEN   token di @BotFather                       (obbligatoria)
    CUP_BOT_KEY          chiave di cifratura: python store.py genkey (obbligatoria)
    DB_PATH              file SQLite (default: data/cup.db)
    ADMIN_CHAT_ID        chat PRIVATA di chi gestisce il bot: riceve gli errori imprevisti (facoltativa)
    CONTATTO_GESTORE     come contattare chi gestisce il bot, mostrato nell'informativa (consigliata)
    MAX_UTENTI           utenti registrabili al massimo (default 30)
    INTERVALLO_MIN       minuti tra due controlli dello stesso utente (default 45, minimo 30)
    ADMIN_INTERVALLO_MIN intervallo solo per ADMIN_CHAT_ID (default come INTERVALLO_MIN, minimo 5)
    DISTANZA_PORTALE_S   secondi minimi tra due sessioni sul portale, fra tutti gli utenti (default 20)
    MODALITA_PROVA       1 = i pulsanti si fermano al riepilogo senza confermare (default 0)
"""
import hmac
import json
import logging
import os
import random
import secrets
import sys
import time
import traceback
from datetime import date, datetime, timedelta

import requests

import cup_http
import store as storemod
from store import Store

log = logging.getLogger("cupbot")

TTL_OFFERTA = 20 * 60  # secondi: oltre, la sessione che tiene lo slot potrebbe essere scaduta
AVVISA_ERRORI = (3, 12, 40)  # un timeout isolato del portale e' normale: avvisa solo se continua
MIN_INTERVALLO = 30  # minuti: ogni controllo tiene bloccata una data ~40 minuti
MIN_INTERVALLO_ADMIN = 5  # solo per chi gestisce il bot: come una persona che aggiorna la pagina
PAUSA_CONTROLLA = 15 * 60  # secondi tra due /controlla dello stesso utente
MAX_RICERCHE_FALLITE = 5  # ricerche CF+NRE fallite per chat al giorno
PAUSA_MESSAGGI = 1.5  # secondi minimi tra due messaggi della stessa chat
ANTICIPI_AUTO = (1, 3, 7)  # giorni minimi da oggi per la conferma automatica, a scelta dell'utente
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]

PRIVACY = (
    "🔒 Informativa, prima di iniziare\n\n"
    "Questo bot controlla per te il portale CUP Piemonte (cup.isan.csi.it) e ti avvisa se si libera "
    "una data PRIMA della tua prenotazione. Se tocchi il pulsante che ti mando, sposta la prenotazione "
    "per te. Non sposta nulla senza un tuo tocco, a meno che tu non attivi la conferma automatica "
    "(/auto). Non e' un servizio della Regione Piemonte o di CSI.\n\n"
    "Per funzionare deve conservare:\n"
    "• il tuo codice fiscale e il numero della ricetta (NRE);\n"
    "• data, ora e luogo della tua prenotazione, e le date che ti ha gia' segnalato.\n"
    "Servono solo a questo. Sono conservati cifrati e inviati solo al portale CUP. Le date e i luoghi "
    "che ti mando viaggiano su Telegram come messaggi normali e restano nella chat finche' non la cancelli.\n\n"
    "Base giuridica: il tuo consenso esplicito (sono dati sanitari). Puoi ritirarlo in ogni momento con "
    "/cancella, che elimina tutto; poi cancella anche la chat (\"Elimina per entrambi\"). Puoi vedere i "
    "dati con /dati e cambiarli con /modifica. Vengono cancellati da soli quando la data della prenotazione "
    "e' passata, o dopo 30 giorni di pausa.\n\n"
    "Il portale chiede solo codice fiscale e NRE: chiunque li abbia puo' gestire la prenotazione, anche "
    "da qui. Usa il bot solo per le tue ricette (o di chi ti ha autorizzato).\n\n"
    "I messaggi in cui mi scrivi codice fiscale e NRE li cancello dalla chat appena letti.")

AIUTO = (
    "Comandi:\n"
    "/stato – ultimo controllo e la tua prenotazione\n"
    "/controlla – controlla adesso\n"
    "/dati – i dati che conservo\n"
    "/modifica – cambia codice fiscale e ricetta\n"
    "/sede – cambia le sedi accettate\n"
    "/auto – conferma automatica delle date migliori\n"
    "/pausa, /riprendi – sospendi o riattiva i controlli\n"
    "/cancella – elimina tutti i tuoi dati\n"
    "/privacy – come tratto i tuoi dati")

COMANDI = [("stato", "Ultimo controllo e prenotazione"), ("controlla", "Controlla adesso"),
           ("dati", "I dati che conservo"), ("modifica", "Cambia codice fiscale e ricetta"),
           ("sede", "Cambia le sedi accettate"), ("auto", "Conferma automatica"),
           ("pausa", "Sospendi i controlli"),
           ("riprendi", "Riattiva i controlli"), ("cancella", "Elimina tutti i tuoi dati"),
           ("privacy", "Come tratto i tuoi dati"), ("help", "Elenco dei comandi")]


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def fmt(d):
    return f"{GIORNI[d.weekday()]} {d:%d/%m/%Y} ore {d:%H:%M}"


UID_KEY = b""


def uid(chat_id):
    """Identificativo per i log che non espone l'id Telegram (HMAC con chiave: non reversibile)."""
    return "u" + hmac.new(UID_KEY, str(chat_id).encode(), "sha256").hexdigest()[:10]


def pren_to_dict(p):
    return {"quando": p.quando.isoformat(), "sede": p.luogo.sede, "ambulatorio": p.luogo.ambulatorio,
            "indirizzo": p.luogo.indirizzo, "cosa": p.cosa}


def pren_from_dict(d):
    return cup_http.Prenotazione(datetime.fromisoformat(d["quando"]),
                                 cup_http.Luogo(d["sede"], d["ambulatorio"], d["indirizzo"]), d["cosa"])


def descrivi_prenotazione(p, titolo="La tua prenotazione"):
    return f"{titolo}:\n🩺 {p.cosa}\n📅 {fmt(p.quando)}\n📍 {p.luogo}"


def descrivi(res):
    righe = [descrivi_prenotazione(res["attuale"]), "", "Date offerte dal CUP:"]
    if not res["slots"]:
        righe.append("nessuna")
    for x in res["slots"][:10]:
        righe.append(("✅ " if x in res["migliori"] else "▫️ ") + f"{fmt(x.quando)}\n    📍 {x.luogo}")
    if len(res["slots"]) > 10:
        righe.append(f"… e altre {len(res['slots']) - 10}")
    return "\n".join(righe)


AUTO_TESTO = (
    "⚡ Conferma automatica\n\n"
    "Se la attivi, quando trovo una data PRIMA della tua la prenoto subito, senza aspettare il tuo tocco: "
    "le date buone spariscono in pochi minuti.\n\n"
    "Da sapere:\n"
    "• la prenotazione attuale viene sostituita: la data vecchia la perdi;\n"
    "• se poi non puoi andare, devi disdire o spostare almeno 2 giorni lavorativi prima, altrimenti paghi "
    "l'intera prestazione;\n"
    "• rispetto le sedi che hai scelto (/sede) e l'anticipo minimo che scegli qui sotto;\n"
    "• ti scrivo subito data, ora e luogo della nuova prenotazione.\n\n"
    "Da quando accetti una data nuova?")


def auto_descr(u):
    a = u.get("auto")
    if not a:
        return "disattivata"
    return "attiva, date da " + ("domani" if a["giorni"] == 1 else f"tra {a['giorni']} giorni") + " in poi"


def maschera(s, visibili=4):
    return s[:3] + "•" * max(0, len(s) - 3 - visibili) + s[-visibili:] if s else "-"


class Bot:
    def __init__(self, store, token, admin=None, max_utenti=30, intervallo=45, distanza=20, prova=False, contatto="",
                 admin_intervallo=None):
        global UID_KEY
        UID_KEY = store.hkey
        self.store, self.admin, self.token, self.contatto = store, str(admin or ""), token, contatto
        self.api = f"https://api.telegram.org/bot{token}/"
        self.max_utenti, self.intervallo = max_utenti, max(MIN_INTERVALLO, intervallo)
        # ogni controllo blocca una data: l'intervallo breve vale solo per chi gestisce il bot, non per tutti
        self.admin_intervallo = max(MIN_INTERVALLO_ADMIN, admin_intervallo or self.intervallo)
        self.distanza, self.prova = distanza, prova
        self.ricerche_fallite = {}  # chat_id -> [timestamp]
        self.ultimo_msg = {}        # chat_id -> timestamp
        self.ultima_pulizia = 0.0
        self.offerte = {}   # chat_id -> {"token", "ts", "sessione", "slots"}: in memoria, come le sessioni del portale
        self.ultimo_portale = 0.0
        self.offset = None

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
        if self.store.get(chat_id):
            self.store.delete(chat_id)
            self.offerte.pop(chat_id, None)
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

    # --- portale: una sessione alla volta, distanziate ---------------------------------
    def portale(self, fn, *args, **kwargs):
        attesa = self.distanza - (time.time() - self.ultimo_portale)
        if attesa > 0:
            time.sleep(attesa)
        try:
            return fn(*args, **kwargs)
        finally:
            self.ultimo_portale = time.time()

    # --- controllo periodico ------------------------------------------------------------
    def intervallo_di(self, chat_id):
        return self.admin_intervallo if self.admin and str(chat_id) == self.admin else self.intervallo

    def offerta_valida(self, chat_id):
        o = self.offerte.get(chat_id)
        return bool(o) and time.time() - o["ts"] <= TTL_OFFERTA

    def controlla(self, u, manuale=False):
        chat = u["chat_id"]
        try:
            res = self.portale(cup_http.check, u["cf"], u["nre"], u.get("stessa_sede", True))
        except (cup_http.NonTrovata, cup_http.NonAttiva) as e:
            u.update(stato="pausa", pausa_da=time.time())
            self.store.save(u)
            self.send(chat, f"Non trovo piu' una prenotazione attiva per la tua ricetta ({e}): forse e' stata "
                            "disdetta, spostata altrove o gia' effettuata. Ho sospeso i controlli.\n"
                            "/modifica per un'altra ricetta, /riprendi per riprovare, /cancella per eliminare i dati.")
            return None
        except Exception as e:
            if not isinstance(e, (cup_http.CupError, requests.RequestException)):
                log.error("controllo %s: errore imprevisto %s\n%s", uid(chat), type(e).__name__,
                          "".join(traceback.format_tb(e.__traceback__)))
                self.alert_admin(f"Errore imprevisto nel controllo di {uid(chat)}: {type(e).__name__}")
            u["errori"] = u.get("errori", 0) + 1
            u["ultimo"] = {"ts": time.time(), "testo": f"errore: {e}"}
            self.store.save(u)
            log.info("controllo %s: errore %d: %s", uid(chat), u["errori"], type(e).__name__)
            if manuale or u["errori"] in AVVISA_ERRORI:
                motivo = "il portale CUP non risponde" if isinstance(e, requests.RequestException) else str(e)
                self.send(chat, f"⚠️ {motivo[0].upper()}{motivo[1:]}" + (
                    "" if manuale else f" (da {u['errori']} controlli di fila). Continuo a riprovare da solo."))
            return None

        att = res["attuale"]
        if att.quando < datetime.now():
            self.store.delete(chat)
            self.offerte.pop(chat, None)
            self.send(chat, f"La data della tua prenotazione ({fmt(att.quando)}) e' passata: ho cancellato tutti "
                            "i tuoi dati. Se ti serve di nuovo, scrivi /start.")
            return None
        u.update(errori=0, attuale=pren_to_dict(att), ultimo={"ts": time.time(), "testo": descrivi(res)})
        log.info("controllo %s: %d date, %d migliori", uid(chat), len(res["slots"]), len(res["migliori"]))
        notificati = u.get("notificati") or {}  # data -> quando e' stata offerta l'ultima volta
        if isinstance(notificati, list):
            notificati = dict.fromkeys(notificati, 0)
        ignorati = set(u.get("ignorati", []))
        auto = u.get("auto")
        if auto:
            # un solo tentativo automatico per data; le date gia' offerte col pulsante valgono comunque
            tentati = set(u.get("tentati_auto", []))
            dal = date.today() + timedelta(days=auto["giorni"])
            candidati = [x for x in res["migliori"] if x.luogo.sede and x.quando.date() >= dal and x.key() not in tentati]
            if candidati:
                slot = candidati[0]  # la piu' vicina tra quelle ammesse
                u["tentati_auto"] = sorted(tentati | {slot.key()})
                self.store.save(u)
                self.send(chat, "⚡ Conferma automatica: ho trovato una data prima della tua.\n\n" + descrivi(res))
                self.prenota(u, slot, res["sessione"], automatica=True)
                return res
        # si ripropone una data se la sua offerta e' scaduta o persa (es. riavvio), non se l'utente l'ha ignorata
        ora = time.time()
        nuove = [x for x in res["migliori"] if x.key() not in ignorati and ora - notificati.get(x.key(), 0) > TTL_OFFERTA]
        if nuove and self.offri(u, res, ignorati):
            notificati.update({x.key(): ora for x in res["migliori"]})
            u["notificati"] = notificati
        elif manuale:
            self.send(chat, descrivi(res))
        self.store.save(u)
        return res

    def offri(self, u, res, ignorati=()):
        """Messaggio con un pulsante per ogni data migliore (max 3). La sessione del controllo resta
        in memoria: e' lei che tiene bloccata la data proposta."""
        token = secrets.token_hex(4)
        slots = [x for x in res["migliori"] if x.luogo.sede and x.key() not in ignorati][:3]
        if not slots:
            return False
        buttons = [[{"text": f"✅ Prenota {fmt(x.quando)}", "callback_data": f"p:{token}:{i}"}] for i, x in enumerate(slots)]
        buttons.append([{"text": "Ignora", "callback_data": f"x:{token}"}])
        prova = "\n(MODALITA' PROVA: il pulsante si ferma al riepilogo, non conferma)" if self.prova else ""
        ok = self.send(u["chat_id"], "🎉 C'e' una data PRIMA della tua!\n\n" + descrivi(res) +
                       f"\n\nTocca per spostare la prenotazione (valido {TTL_OFFERTA // 60} minuti).{prova}", buttons)
        if ok:
            self.offerte[u["chat_id"]] = {"token": token, "ts": time.time(), "sessione": res["sessione"], "slots": slots}
        return ok

    def prenota(self, u, slot, sessione, automatica=False):
        """Ritorna "ok", "fallita" o "incerta" (conferma inviata ma esito non verificato)."""
        chat = u["chat_id"]
        self.send(chat, f"Sposto la prenotazione a:\n📅 {fmt(slot.quando)}\n📍 {slot.luogo}…")
        try:
            esito = self.portale(cup_http.prenota, u["cf"], u["nre"], slot, sessione=sessione,
                                 stessa_sede=u.get("stessa_sede", True), dry_run=self.prova)
        except (cup_http.CupError, requests.RequestException) as e:
            urgente = "Conferma inviata" in str(e)
            self.send(chat, ("🚨 " if urgente else "❌ Non spostata: ") + str(e))
            if urgente:
                self.alert_admin(f"Esito incerto dopo la conferma per {uid(chat)}")
                self.sospendi_auto(u)
            log.info("prenotazione %s fallita: %s%s", uid(chat), type(e).__name__, " (esito incerto)" if urgente else "")
            return "incerta" if urgente else "fallita"
        except Exception as e:
            log.error("prenotazione %s: errore imprevisto %s\n%s", uid(chat), type(e).__name__,
                      "".join(traceback.format_tb(e.__traceback__)))
            self.send(chat, f"🚨 Errore imprevisto durante la prenotazione ({type(e).__name__}). Verifica subito su "
                            f"{cup_http.LISTA_URL} o al {cup_http.CALL_CENTER}.")
            self.alert_admin(f"Errore imprevisto nella prenotazione di {uid(chat)}: {type(e).__name__}")
            self.sospendi_auto(u)
            return "incerta"
        log.info("prenotazione %s riuscita%s%s", uid(chat), " (automatica)" if automatica else "",
                 " (prova)" if self.prova else "")
        if self.prova:
            self.send(chat, "🧪 " + esito)
            return "ok"
        # la nuova data diventa il riferimento dei prossimi controlli
        u.update(notificati={}, ignorati=[], tentati_auto=[])
        u["attuale"] = {**u.get("attuale", {}), "quando": slot.quando.isoformat(), "sede": slot.luogo.sede,
                        "ambulatorio": slot.luogo.ambulatorio, "indirizzo": slot.luogo.indirizzo}
        u["prossimo"] = time.time() + self.intervallo_di(chat) * 60
        self.store.save(u)
        self.send(chat, f"✅ Prenotazione spostata{' (conferma automatica)' if automatica else ''}!\n"
                        f"📅 {fmt(slot.quando)}\n📍 {slot.luogo}\n\n"
                        "Riceverai SMS/email dal CUP con il nuovo promemoria; controlla anche il codice di "
                        "pagamento del ticket. Se non puoi andare, disdici o sposta almeno 2 giorni lavorativi "
                        "prima. Continuo a cercare date ancora prima.")
        return "ok"

    def sospendi_auto(self, u):
        """Dopo un esito incerto niente altri tentativi automatici: decide l'utente."""
        if u.get("auto"):
            u["auto"] = None
            self.store.save(u)
            self.send(u["chat_id"], "Per sicurezza ho disattivato la conferma automatica. Verifica la prenotazione, "
                                    "poi riattivala con /auto se vuoi.")

    # --- registrazione ------------------------------------------------------------------
    def chiedi_cf(self, u):
        u.pop("nre", None)  # la coppia CF+NRE si riforma solo a ricerca riuscita
        u.update(stato="cf", creato=time.time())
        self.store.save(u)
        self.send(u["chat_id"], "Mandami il tuo codice fiscale (16 caratteri, come sul promemoria della ricetta).")

    def ricevi_cf(self, u, testo):
        cf = "".join(testo.split()).upper()
        if not cup_http.CF_RE.match(cf):
            self.send(u["chat_id"], "Non sembra un codice fiscale valido (16 caratteri, es. RSSMRA80A01L219X). Riprova.")
            return
        u.update(cf=cf, stato="nre")
        self.store.save(u)
        self.send(u["chat_id"], "Ora il numero della ricetta elettronica (NRE): 15 caratteri, di solito inizia con "
                                "010A. Lo trovi sotto il codice a barre della ricetta o sul promemoria della prenotazione.")

    def ricevi_nre(self, u, testo):
        chat = u["chat_id"]
        nre = "".join(testo.split()).upper()
        if not cup_http.NRE_RE.match(nre):
            self.send(chat, "Il numero ricetta deve avere 15 caratteri (es. 010A12345678901). Riprova.")
            return
        recenti = [t for t in self.ricerche_fallite.get(chat, []) if t > time.time() - 86400]
        self.ricerche_fallite[chat] = recenti
        if len(recenti) >= MAX_RICERCHE_FALLITE:
            self.send(chat, "Troppe ricerche non riuscite oggi. Riprova domani.")
            return
        self.send(chat, "Cerco la tua prenotazione sul portale CUP…")
        try:
            att = self.portale(cup_http.cerca, u["cf"], nre)
        except cup_http.NonTrovata:
            recenti.append(time.time())
            self.send(chat, "Il portale non trova prenotazioni con questo codice fiscale e questa ricetta.\n"
                            "Il bot funziona solo per appuntamenti gia' prenotati sul CUP Piemonte: per ora non "
                            "cerca il primo appuntamento di una ricetta non ancora prenotata.")
            self.chiedi_cf(u)
            return
        except cup_http.NonAttiva as e:
            recenti.append(time.time())
            self.send(chat, f"La prenotazione di questa ricetta non e' attiva ({e}).")
            self.chiedi_cf(u)
            return
        except (cup_http.CupError, requests.RequestException) as e:
            self.send(chat, f"Il portale CUP non risponde ({e}). Rimandami il numero ricetta tra qualche minuto.")
            return
        u.update(nre=nre, attuale=pren_to_dict(att), stato="sede", notificati=[])
        try:
            self.store.save(u)
        except storemod.GiaRegistrata:
            self.send(chat, "Questa ricetta e' gia' seguita da un altro utente del bot.")
            u = self.store.get(chat)
            self.chiedi_cf(u)
            return
        self.send(chat, descrivi_prenotazione(att, "Ho trovato la tua prenotazione"))
        self.chiedi_sede(u, att)

    def chiedi_auto(self, u):
        righe = [[{"text": "Da domani" if g == 1 else f"Da tra {g} giorni", "callback_data": f"auto:{g}"}
                  for g in ANTICIPI_AUTO]]
        righe.append([{"text": "Disattiva" if u.get("auto") else "Lascia disattivata", "callback_data": "auto:0"}])
        self.send(u["chat_id"], AUTO_TESTO + f"\n\nStato attuale: {auto_descr(u)}.", righe)

    def privacy(self):
        return PRIVACY + (f"\n\nGestore del bot: {self.contatto}" if self.contatto else "")

    def chiedi_sede(self, u, att):
        self.send(u["chat_id"], "Quali date vuoi che ti segnali?", [
            [{"text": f"Solo {att.luogo.sede}", "callback_data": "sede:1"}],
            [{"text": "Qualsiasi sede proposta dal CUP", "callback_data": "sede:0"}]])

    # --- messaggi e pulsanti ------------------------------------------------------------
    def on_message(self, msg):
        if msg.get("chat", {}).get("type") != "private" or "text" not in msg:
            return
        chat = msg["chat"]["id"]
        testo = msg["text"].strip()
        cmd = testo.split()[0].split("@")[0].lower() if testo.startswith("/") else ""
        u = self.store.get(chat)
        compatto = "".join(testo.split()).upper()
        if not cmd and (cup_http.CF_RE.match(compatto) or cup_http.NRE_RE.match(compatto)) and \
                not (u and u["stato"] in ("cf", "nre")):
            # un codice fiscale o una ricetta scritti fuori dalla registrazione non restano in chat
            self.tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        ora = time.time()
        if ora - self.ultimo_msg.get(chat, 0) < PAUSA_MESSAGGI and not (u and u["stato"] in ("cf", "nre")):
            return  # raffica: ignorata
        self.ultimo_msg[chat] = ora

        if cmd == "/start":
            if u and u["stato"] in ("attivo", "pausa"):
                self.send(chat, "Sei gia' registrato.\n\n" + AIUTO)
            elif u:
                self.chiedi_cf(u)
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
            self.send(chat, f"Utenti registrati: {self.store.count()} / {self.max_utenti}. "
                            f"Offerte aperte: {sum(self.offerta_valida(c) for c in list(self.offerte))}.")
            return
        if not u:
            self.send(chat, "Scrivi /start per iniziare.")
            return
        if cmd == "/cancella":
            self.send(chat, "Vuoi davvero cancellare tutti i tuoi dati? I controlli si fermano.",
                      [[{"text": "Si', cancella tutto", "callback_data": "del:1"}, {"text": "No", "callback_data": "del:0"}]])
            return
        if u["stato"] in ("cf", "nre") and not cmd:
            # i dati personali non restano nella chat
            self.tg("deleteMessage", chat_id=chat, message_id=msg["message_id"])
            (self.ricevi_cf if u["stato"] == "cf" else self.ricevi_nre)(u, testo)
            return
        if cmd == "/modifica":
            self.offerte.pop(chat, None)
            self.chiedi_cf(u)
            return
        if u["stato"] in ("cf", "nre", "sede"):
            self.send(chat, "Completa prima la registrazione (o /modifica per ricominciare, /cancella per annullare).")
            return
        if cmd == "/dati":
            att = pren_from_dict(u["attuale"]) if u.get("attuale") else None
            self.send(chat, f"Codice fiscale: {maschera(u.get('cf', ''))}\nRicetta (NRE): {maschera(u.get('nre', ''))}\n"
                            f"Sedi: {'solo ' + att.luogo.sede if u.get('stessa_sede', True) and att else 'qualsiasi'}\n"
                            f"Controlli: {'in pausa' if u['stato'] == 'pausa' else f'ogni {self.intervallo_di(chat)} minuti'}\n"
                            f"Conferma automatica: {auto_descr(u)}\n\n"
                            + (descrivi_prenotazione(att) if att else ""))
        elif cmd == "/sede":
            if u.get("attuale"):
                self.chiedi_sede(u, pren_from_dict(u["attuale"]))
        elif cmd == "/auto":
            self.chiedi_auto(u)
        elif cmd == "/stato":
            ult = u.get("ultimo")
            if ult:
                self.send(chat, f"Ultimo controllo {datetime.fromtimestamp(ult['ts']):%d/%m %H:%M}"
                                f"{' (IN PAUSA)' if u['stato'] == 'pausa' else ''}:\n\n{ult['testo']}")
            elif u.get("attuale"):
                self.send(chat, descrivi_prenotazione(pren_from_dict(u["attuale"])) + "\n\nNessun controllo ancora eseguito.")
        elif cmd == "/controlla":
            ultimo = (u.get("ultimo") or {}).get("ts", 0)
            if self.offerta_valida(chat):
                self.send(chat, "Hai un'offerta aperta: usa i suoi pulsanti (o Ignora) prima di un nuovo controllo.")
            elif time.time() - ultimo < min(PAUSA_CONTROLLA, self.intervallo_di(chat) * 60):
                # ogni controllo tiene bloccata una data per ~40 minuti: niente controlli a raffica
                pausa = min(PAUSA_CONTROLLA, self.intervallo_di(chat) * 60)
                self.send(chat, f"Ultimo controllo alle {datetime.fromtimestamp(ultimo):%H:%M}: il prossimo "
                                f"/controlla e' possibile dalle {datetime.fromtimestamp(ultimo + pausa):%H:%M}.")
            else:
                self.send(chat, "Controllo in corso…")
                self.controlla(u, manuale=True)
        elif cmd == "/pausa":
            u.update(stato="pausa", pausa_da=time.time())
            self.store.save(u)
            self.send(chat, "Controlli sospesi. /riprendi per ripartire.")
        elif cmd == "/riprendi":
            u.update(stato="attivo", prossimo=time.time(), errori=0)
            self.store.save(u)
            self.send(chat, "Controlli riattivati.")
        else:
            self.send(chat, AIUTO)

    def on_callback(self, cq):
        self.tg("answerCallbackQuery", callback_query_id=cq["id"])
        msg = cq.get("message") or {}
        chat = cq["from"]["id"]
        if msg.get("chat", {}).get("id") != chat:  # solo chat private
            return
        u = self.store.get(chat)
        parts = cq.get("data", "").split(":")
        togli_pulsanti = lambda: self.tg("editMessageReplyMarkup", chat_id=chat, message_id=msg["message_id"],
                                         reply_markup={"inline_keyboard": []})
        if parts[0] == "consenso":
            togli_pulsanti()
            if u:
                return
            if parts[1:] != ["1"]:
                self.send(chat, "Va bene, non ho conservato nulla. Se cambi idea scrivi /start.")
            elif self.store.count() >= self.max_utenti:
                self.send(chat, "Mi dispiace, al momento non accetto nuovi utenti.")
            else:
                self.chiedi_cf(self.store.new(chat))
            return
        if not u:
            togli_pulsanti()
            return
        kind = parts[0]
        if kind == "del":
            togli_pulsanti()
            if parts[1:] == ["1"]:
                self.store.delete(chat)
                self.offerte.pop(chat, None)
                self.send(chat, "Fatto: ho cancellato tutti i tuoi dati. Se ti serve di nuovo, scrivi /start.")
        elif kind == "sede" and u.get("attuale") and u["stato"] in ("sede", "attivo", "pausa"):
            togli_pulsanti()
            att = pren_from_dict(u["attuale"])
            nuovo = u["stato"] == "sede"
            u["stessa_sede"] = parts[1:] == ["1"]
            if nuovo:
                u.update(stato="attivo", prossimo=time.time() + 60)
            self.store.save(u)
            self.send(chat, (f"Ok: ti segnalo solo date a {att.luogo.sede}." if u["stessa_sede"]
                             else "Ok: ti segnalo date in qualsiasi sede proposta dal CUP.") +
                      (f"\n\nFatto! Controllo ogni {self.intervallo_di(chat)} minuti e ti scrivo appena esce una data prima "
                       f"del {fmt(att.quando)}.\n\n{AIUTO}" if nuovo else ""))
        elif kind == "auto" and u["stato"] in ("attivo", "pausa") and len(parts) == 2 and \
                parts[1] in {"0", *map(str, ANTICIPI_AUTO)}:
            togli_pulsanti()
            giorni = int(parts[1])
            u["auto"] = {"giorni": giorni} if giorni else None
            self.store.save(u)
            if giorni:
                self.send(chat, f"⚡ Conferma automatica attiva: prenoto da solo la prima data prima della tua, "
                                f"da {'domani' if giorni == 1 else f'tra {giorni} giorni'} in poi"
                                + (", solo a " + pren_from_dict(u["attuale"]).luogo.sede
                                   if u.get("stessa_sede", True) and u.get("attuale") else ", in qualsiasi sede")
                                + ". /auto per cambiarla o disattivarla.")
            else:
                self.send(chat, "Conferma automatica disattivata: ti mando il pulsante e decidi tu.")
        elif kind in ("p", "x"):
            self.on_offerta(u, parts, togli_pulsanti)

    def on_offerta(self, u, parts, togli_pulsanti):
        chat = u["chat_id"]
        o = self.offerte.get(chat)
        valida = o and len(parts) >= 2 and parts[1] == o["token"] and (
            parts[0] == "x" or (len(parts) == 3 and parts[2].isdigit() and int(parts[2]) < len(o["slots"])))
        togli_pulsanti()  # niente doppi tocchi
        if not valida:
            self.send(chat, "Questa offerta non e' piu' valida.")
            return
        del self.offerte[chat]
        if parts[0] == "x":
            u["ignorati"] = sorted(set(u.get("ignorati", [])) | {x.key() for x in o["slots"]})
            self.store.save(u)
            self.send(chat, "Ok, non ti ripropongo queste date.")
            return
        if time.time() - o["ts"] > TTL_OFFERTA:
            self.send(chat, "Offerta scaduta. Se la data c'e' ancora te la ripropongo al prossimo controllo.")
            return
        if u["stato"] not in ("attivo", "pausa"):
            self.send(chat, "Registrazione non completa: non posso prenotare.")
            return
        self.prenota(u, o["slots"][int(parts[2])], o["sessione"])

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
        for chat in self.store.pulizia(time.time()):
            self.offerte.pop(chat, None)
            log.info("utente %s cancellato: registrazione incompleta o pausa oltre 30 giorni", uid(chat))

    def controllo_pianificato(self):
        """Un solo controllo per giro, rispettando la distanza tra sessioni sul portale."""
        if time.time() - self.ultimo_portale < self.distanza:
            return False
        for chat in self.store.due(time.time()):
            if self.offerta_valida(chat):
                continue  # la sua sessione tiene la data offerta: un nuovo controllo non la vedrebbe
            u = self.store.get(chat)
            u["prossimo"] = time.time() + self.intervallo_di(chat) * 60 * random.uniform(0.9, 1.1)
            self.store.save(u)
            self.controlla(u)
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
                fatto = self.controllo_pianificato()
                self.poll(1 if fatto or self.store.due(time.time()) else 25)
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
              admin_intervallo=env_int("ADMIN_INTERVALLO_MIN", 0) or None)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("Fermato.")


if __name__ == "__main__":
    main()
