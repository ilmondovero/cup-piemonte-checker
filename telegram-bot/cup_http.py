"""CUP Piemonte via HTTP (niente browser): cerca date anticipate e, su richiesta, sposta la prenotazione.

Flusso del portale cup.isan.csi.it, replicato campo per campo dai POST del browser:
  1. Recupera Prenotazioni: ricerca per codice fiscale + NRE -> appuntamento attuale
  2. "Sposta appuntamento" -> pagina Appuntamenti Proposti
  3. "Altre disponibilita'" -> Appuntamenti Disponibili
  4. (solo per prenotare) "Seleziona" sullo slot -> "Avanti" -> Riepilogo -> "Conferma"

Ricetta mai prenotata: la procedura del portale ha quattro passi (Ricerca, Prestazioni, Appuntamenti,
Riepilogo e conferma). "Sposta" entra direttamente al terzo; qui si parte dal primo:
  a. Ricerca: codice fiscale + NRE e "Prosegui" (se la ricetta ha gia' un appuntamento: "gia' presente")
  b. Prestazioni: "Avanti" lasciando le prestazioni come le propone il portale. E' l'unico passo mai
     visto dal vivo: se la pagina non e' quella attesa ci si ferma e si descrive com'e' fatta
  c-d. Appuntamenti, Riepilogo e Conferma: gli stessi moduli di "Sposta"

Comportamento del portale da tenere presente: aprire "Sposta" fa tenere lo slot proposto a quella
sessione per ~40 minuti, e cosi' portare uno slot fino al Riepilogo. Non c'e' modo di rilasciarlo
(ne' "Annulla" ne' il logout lo liberano): scade da solo. Per questo la prenotazione di uno slot
trovato da un controllo continua nella stessa sessione, e i controlli non vanno fatti troppo spesso.
"""
import collections
import html
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

import requests

CUP = "https://cup.isan.csi.it"
LISTA_URL = CUP + "/web/guest/lista-prenotazioni"
RICETTA_URL = CUP + "/ricetta-dematerializzata"
CALL_CENTER = "800 000 500"
LENTO = 90  # secondi: la ricerca delle disponibilita' (soprattutto estendendo l'area) puo' richiedere un minuto
PIU_LENTA = 0.0  # secondi: la risposta piu' lenta dall'ultimo azzeramento (il bot la misura per imparare LENTO)
AREA_INCOMPLETA = False  # "Estendi area" scaduto dall'ultimo azzeramento: le date sono solo quelle lette fin li'
# passi della sessione in corso nei flussi ancora da osservare (prenotazione nuova, piu' prestazioni): solo
# conteggi, nomi di sezioni e id di form e pulsanti, mai dati personali. Il bot li scrive nel log e li azzera
DIARIO = collections.deque(maxlen=50)
# passo del flusso in corso (elenco, appuntamenti, estendi, ricerca, riepilogo, conferma, verifica): ogni
# richiesta vale per il suo passo, con il timeout imparato per quel passo (ATTESE, che il bot imposta a ogni
# sessione; un passo che non c'e' usa LENTO)
PASSO = "elenco"
ATTESE = {}
# (ora, passo, secondi, esito, codice HTTP) di ogni richiesta: esito "ok", "timeout", "sovraccarico" o
# "inattesa". Solo tempi e codici, mai indirizzi o dati. Il bot le salva e le azzera a ogni sessione
RICHIESTE = collections.deque(maxlen=500)
ULTIMA = 0.0  # epoch dell'ultima risposta (o errore) del portale: un ciclo lento ma vivo non sembra fermo
TEMPI = {}  # fasi dell'ultima prenotazione: {"elenco": secondi, ...} (vedi prenota)
# verifica dopo la Conferma: pause crescenti prima di ogni tentativo, e al massimo tanti secondi in tutto
VERIFICA_PAUSE = (2, 5, 10, 20, 40)
VERIFICA_MAX = 180
VERIFICA_MIN = 30  # secondi: con meno tempo rimasto un altro tentativo non avrebbe senso
SCADENZA = None  # time.monotonic() entro cui finisce la verifica in corso: ogni sua richiesta aspetta al massimo fin li'
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

L = "_listaprenotazioni_WAR_cupprenotazione_:prescrizioniForm"
A = "_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm"
AVANTI = "_ricettaelettronica_WAR_cupprenotazione_:appuntamenti-form-main"
RIEPILOGO = "_ricettaelettronica_WAR_cupprenotazione_:riepilogoForm"
R = "_ricettaelettronica_WAR_cupprenotazione_:ePrescriptionSearchForm"  # passo "Ricerca" di una prenotazione nuova
# filtri Macrozona/Zona/Sede degli "Appuntamenti Disponibili": il browser li manda sempre, "-" = nessun filtro
GEO = {A + f":localGeoSelectorsavailable:{n}SelectMenuavailable_input": "NO_VALUE" for n in ("macrozona", "zona", "sede")}

MESI = {m: i for i, m in enumerate(
    ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
     "agosto", "settembre", "ottobre", "novembre", "dicembre"], start=1)}
DATE_RE = re.compile(
    r"(?:luned|marted|mercoled|gioved|venerd|sabato|domenica)\S*\s+(\d{1,2})\s+(%s)\s+(\d{4})\s*alle ore\s*(\d{1,2}):(\d{2})"
    % "|".join(MESI), re.I)
CF_RE = re.compile(r"^[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]$")
NRE_RE = re.compile(r"^[0-9A-Z]{15}$")


class CupError(Exception):
    pass


def tipo_errore(e):
    """"timeout" (il portale non risponde in tempo), "sovraccarico" (502/503/504, connessione rifiutata o
    caduta: si riprova) o "inattesa" (altri codici 4xx/5xx, altre eccezioni)."""
    if isinstance(e, requests.ReadTimeout) or (
            isinstance(e, requests.ConnectionError) and "read timed out" in str(e).lower()):
        return "timeout"
    if isinstance(e, requests.HTTPError):
        return "sovraccarico" if _codice(e) in (502, 503, 504) else "inattesa"
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return "sovraccarico"
    return "inattesa"


def sovraccarico(e):
    return tipo_errore(e) in ("timeout", "sovraccarico")


def _codice(e):
    return getattr(getattr(e, "response", None), "status_code", None)


def descrivi(e):
    """Un errore del portale in breve e senza l'indirizzo (requests lo mette nel testo, con i parametri):
    "HTTP 504, portale sovraccarico", "ReadTimeout, portale sovraccarico", "HTTP 403, risposta inattesa"."""
    if not isinstance(e, requests.RequestException):
        return str(e) if isinstance(e, CupError) else type(e).__name__
    cosa = f"HTTP {_codice(e)}" if _codice(e) else type(e).__name__
    return f"{cosa}, {'risposta inattesa' if tipo_errore(e) == 'inattesa' else 'portale sovraccarico'}"


class NonTrovata(CupError):
    """Il portale non trova prenotazioni per questo codice fiscale + NRE."""


class NonAttiva(CupError):
    """La ricetta c'e' ma nessuna prenotazione e' in stato PRENOTATO (disdetta, gia' erogata...)."""

    def __init__(self, msg, stati=()):
        super().__init__(msg)
        self.stati = [s.upper() for s in stati]

    def solo_disdette(self):
        """Tutte le prenotazioni della ricetta sono state disdette: la ricetta si puo' prenotare di nuovo."""
        return bool(self.stati) and all(s.startswith(("DISDETT", "ANNULLAT", "CANCELLAT")) for s in self.stati)


class Separerebbe(CupError):
    """Spostare l'appuntamento separerebbe prestazioni prenotate insieme: il bot non puo' anticiparlo."""


class GiaPrenotata(CupError):
    """Prenotazione nuova: il portale dice che la ricetta ha gia' un appuntamento (si usa "Sposta")."""


# --- dati -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Luogo:
    sede: str
    ambulatorio: str
    indirizzo: str

    def __str__(self):
        return ", ".join(x for x in (self.sede, self.ambulatorio, self.indirizzo) if x)

    def key(self):
        return _norm(self.sede + self.ambulatorio)


@dataclass
class Prenotazione:
    quando: datetime
    luogo: Luogo
    cosa: str  # es. "PRIMA VISITA ... - 89.7"


class Slot:
    def __init__(self, quando, luogo, seleziona_id, proposta=False):
        self.quando, self.luogo = quando, luogo
        self.seleziona_id = seleziona_id  # pulsante "Seleziona"; la proposta non ne ha, e' gia' selezionata
        self.proposta = proposta

    def key(self):
        return f"{self.quando:%Y%m%d%H%M}|{self.luogo.key()}"

    def __repr__(self):
        return f"{self.quando:%d/%m/%Y %H:%M} {self.luogo}"


# --- parsing ----------------------------------------------------------------------------
def _text(fragment):
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S | re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _hidden(page, name):
    m = re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name), page)
    return html.unescape(m.group(1)) if m else None


def _date(text):
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mese, y, hh, mm = m.groups()
    return datetime(int(y), MESI[mese.lower()], int(d), int(hh), int(mm))


def _luogo(fragment):
    """Blocco 'Dove:' del portale: sede e ambulatorio sono <div> con un solo <span>,
    l'indirizzo sta nel div 'unita-address' (le righe di zona hanno piu' span e vengono saltate)."""
    i = fragment.find("Dove:")
    seg = fragment[i:i + 3000] if i >= 0 else ""
    righe = [_text(x) for x in re.findall(r"<div>\s*<span[^>]*>([^<]*)</span>\s*</div>", seg)]
    righe = [x for x in righe if x]
    ind = re.search(r'class="unita-address"[^>]*>(.*?)</div>', seg, re.S)
    return Luogo(righe[0] if righe else "", righe[1] if len(righe) > 1 else "", _text(ind.group(1)) if ind else "")


def _cosa(fragment):
    m = re.search(r'captionAppointment-desc">(.*?)</div>', fragment, re.S)
    return re.sub(r"\s*\(PRENOTABILE\)\s*", "", _text(m.group(1))).strip() if m else ""


def _prestazioni(page):
    """Nomi delle prestazioni nel carrello del portale ("Prestazioni Selezionate")."""
    i = page.find('id="prestazioni_selezionate"')
    if i < 0:
        return []
    fine = page.find("</form>", i)
    nomi = re.findall(r'<span class="media-title"[^>]*>\s*<span[^>]*>([^<]+)</span>', page[i:fine if fine > 0 else None])
    return [n for n in (_text(x) for x in nomi) if n]


def _errori(xml):
    """Messaggi di errore del portale (riquadri rossi), senza il prefisso del campo ("***nre***: ...")."""
    return [re.sub(r"^\*+[^*]*\*+:\s*", "", _text(m))
            for m in re.findall(r'class="messagifyMsg alert-danger">(.*?)</div>', xml, re.S)]


def _id(s):
    """Ultimo pezzo di un id del portale, solo se ha l'aspetto del nome di un componente (mai un codice
    fiscale o una ricetta, se un giorno finissero in un id)."""
    s = s.rsplit(":", 1)[-1]
    u = s.upper()
    if not re.fullmatch(r"[A-Za-z_][\w-]{0,60}", s) or CF_RE.match(u) or (NRE_RE.match(u) and re.search(r"\d", u)):
        return "?"
    return s


def _struttura(page):
    """Com'e' fatta una pagina che non conosco, senza dati personali: sezioni, form e pulsanti (solo id)."""
    sezioni = [s for s in ("Ricerca ricetta", "Prestazioni", "Appuntamenti Proposti", "Appuntamenti Disponibili",
                           "Riepilogo") if s in page]
    forms = [_id(f) for f in re.findall(r'<form[^>]*id="([^"]+)"', page)]
    pulsanti = sorted({_id(b) for b in re.findall(r'id="([^"]*(?:Button|Navigate)[^"]*)"', page)})
    return f"sezioni {sezioni}, form {forms}, pulsanti {pulsanti[:20]}"


def _caselle(page):
    """Caselle di spunta di una pagina: (spuntate, totali). Nel passo Prestazioni sono le prestazioni scelte."""
    caselle = re.findall(r'<input[^>]*type="checkbox"[^>]*>', page)
    return sum(1 for x in caselle if re.search(r'\schecked(?![\w-])', x)), len(caselle)


def _form_html(page, form_id):
    m = re.search(r'<form[^>]*id="%s".*?</form>' % re.escape(form_id), page, re.S)
    return m.group(0) if m else None


def _form_fields(page, form_id):
    """Campi del form come li serializza il browser: input text/hidden, checkbox spuntate, select."""
    f = _form_html(page, form_id) or ""
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", f):
        name = re.search(r'name="([^"]+)"', tag)
        typ = re.search(r'type="([^"]+)"', tag)
        typ = typ.group(1).lower() if typ else "text"
        if not name or typ in ("submit", "button", "image", "reset", "file"):
            continue
        if typ in ("checkbox", "radio") and "checked" not in tag:
            continue
        val = re.search(r'value="([^"]*)"', tag)
        fields[html.unescape(name.group(1))] = html.unescape(val.group(1)) if val else ("on" if typ == "checkbox" else "")
    for name, body in re.findall(r'<select\b[^>]*name="([^"]+)"[^>]*>(.*?)</select>', f, re.S):
        opts = re.findall(r'<option\b([^>]*)value="([^"]*)"([^>]*)>', body)
        chosen = [v for a, v, b in opts if "selected" in a + b] or [v for _, v, _ in opts[:1]]
        if chosen:
            fields[html.unescape(name)] = html.unescape(chosen[0])
    for name, body in re.findall(r'<textarea\b[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', f, re.S):
        fields[html.unescape(name)] = html.unescape(body)
    return fields


# --- JSF / ICEfaces ---------------------------------------------------------------------
class _Form:
    """Stato JSF/ICEfaces di un form, per replicare i POST del browser campo per campo."""

    def __init__(self, session, page_html, form_id):
        self.s, self.form = session, form_id
        page_html = _form_html(page_html, form_id) or page_html
        self.enc = _hidden(page_html, "javax.faces.encodedURL")
        self.vs = _hidden(page_html, "javax.faces.ViewState")
        self.win = _hidden(page_html, "ice.window")
        self.view = _hidden(page_html, "ice.view")
        if not (self.enc and self.vs and self.win and self.view):
            raise CupError("Pagina CUP inattesa: campi JSF mancanti (sito cambiato o in manutenzione?)")

    def post(self, fields, form=None):
        if not self.enc.startswith(CUP + "/"):
            raise CupError("Il portale ha indicato un indirizzo esterno: non invio dati")
        form = form or self.form
        data = {form: form, "javax.faces.encodedURL": self.enc, "ice.window": self.win, "ice.view": self.view}
        data.update(fields)
        data["javax.faces.ViewState"] = self.vs
        return _chiama(self.s.post, self.enc, data=data, headers={
            "Faces-Request": "partial/ajax", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}).text


def _event(source, event=None, param=True):
    """Campi di un click su un pulsante ICEfaces ACE, come li manda il browser."""
    fields = {"ice.event.captured": source, "ice.event.type": "onclick", "ice.event.x": "200", "ice.event.y": "600"}
    fields.update({f"ice.event.{k}": "false" for k in ("alt", "ctrl", "shift", "meta", "left", "right")})
    fields.update({"javax.faces.source": source, "javax.faces.partial.execute": "@all",
                   "javax.faces.partial.render": "@all", "javax.faces.partial.ajax": "true"})
    if event:
        fields.update({"javax.faces.behavior.event": event, "javax.faces.partial.event": event})
    elif param:
        fields[source] = source
    return fields


def _attesa():
    """Timeout delle richieste: 20 s per collegarsi, TLS compreso (se il portale e' giu' non serve aspettare
    di piu'), per la risposta quello imparato per il passo in corso (LENTO se non c'e')."""
    return _limiti()[0]


def _limiti():
    """(timeout, ridotto): nella verifica dopo la Conferma nessuna richiesta va oltre SCADENZA; ridotto dice
    che il timeout e' stato accorciato per questo (un timeout cosi' non dice nulla sul portale)."""
    connessione, risposta = 20, ATTESE.get(PASSO, LENTO)
    if PASSO == "verifica" and SCADENZA is not None:
        resta = SCADENZA - time.monotonic()
        if resta < risposta or resta < connessione:
            return (min(connessione, max(resta, 0)), min(risposta, max(resta, 0))), True
    return (connessione, risposta), False


def _passo(nome):
    global PASSO
    PASSO = nome


def _chiama(metodo, url, **kw):
    """Una richiesta al portale nel passo in corso: ne registra tempo ed esito in RICHIESTE e, se fallisce,
    scrive nel DIARIO passo, codice HTTP e tipo di errore (mai l'indirizzo, che puo' avere parametri).
    Anche le pagine (GET) con un codice di errore sono errori: non contengono i moduli del portale."""
    global ULTIMA
    passo, (attesa, ridotto), inizio = PASSO, _limiti(), time.monotonic()
    try:
        if min(attesa) < 1:  # la verifica non ha piu' tempo: nessuna richiesta
            raise requests.ReadTimeout("tempo della verifica finito")
        r = metodo(url, timeout=attesa, **kw)
        r.raise_for_status()
    except requests.RequestException as e:
        tipo = tipo_errore(e)
        secondi = attesa[1] if tipo == "timeout" else time.monotonic() - inizio
        if not (ridotto and tipo == "timeout"):  # un timeout accorciato apposta non insegna nulla sul portale
            RICHIESTE.append((time.time(), passo, round(secondi, 1), tipo, _codice(e)))
        DIARIO.append(f"{passo}: {descrivi(e)}" + (f" dopo {attesa[1]:.0f} s" if tipo == "timeout" else "") +
                      (" (limite della verifica)" if ridotto and tipo == "timeout" else ""))
        raise
    finally:
        ULTIMA = time.time()
    elapsed = getattr(r, "elapsed", None)
    secondi = elapsed.total_seconds() if elapsed is not None else time.monotonic() - inizio
    RICHIESTE.append((time.time(), passo, round(secondi, 1), "ok", getattr(r, "status_code", None)))
    return r


def _get(session, url):
    return _chiama(session.get, url).text


def _misura(r, *args, **kwargs):
    """Hook di requests: il tempo fino all'inizio della risposta, quello che fa scattare il timeout."""
    global PIU_LENTA
    PIU_LENTA = max(PIU_LENTA, r.elapsed.total_seconds())


# --- sessione sul portale ---------------------------------------------------------------
class CupSession:
    def __init__(self, cf, nre):
        self.cf, self.nre, self.cosa = cf, nre, ""
        # "sposta" o "nuova": una sessione vale solo per il flusso che l'ha aperta (prenotare con la sessione
        # di una prenotazione nuova una ricetta gia' prenotata potrebbe creare un secondo appuntamento)
        self.modo = ""
        self.n_prestazioni = 0  # il massimo visto nel carrello, in qualsiasi passo
        self.nomi = []  # prestazioni dell'ultimo carrello letto
        self.prenotate = []  # tutte le righe in stato PRENOTATO dell'elenco (una per prestazione, se piu' d'una)
        self.n_prenotate = 0  # righe PRENOTATO, anche quelle di cui non si legge la data
        self.righe_prenotate = []
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.s.hooks["response"].append(_misura)
        self.search = {L + ":CFInput": cf, L + ":IDSearchTypeInput_input": "nre-label", L + ":IDSearchValueInput": nre}
        self.slots = []
        self.passo_elenco = "elenco"  # "verifica" per la sessione che controlla l'esito di una Conferma

    def attuale(self):
        """La prenotazione in stato PRENOTATO per questa ricetta."""
        _passo(self.passo_elenco)
        self.lista = _Form(self.s, _get(self.s, LISTA_URL), L)
        src = L + ":filterPrescriptionNavigate"
        self.lista_xml = self.lista.post({**self.search, "javax.faces.source": src, "javax.faces.partial.event": "click",
                                          "javax.faces.partial.execute": f"{src} {L}", "javax.faces.partial.render": "@all",
                                          "javax.faces.behavior.event": "action", "javax.faces.partial.ajax": "true"})
        x = self.lista_xml
        inizi = [m.start() for m in re.finditer(r"Stato:", x)]
        if not inizi:
            # "nessuna prenotazione" solo se il portale lo dice: una pagina diversa (manutenzione, errore,
            # sito cambiato) non deve far credere che la ricetta sia libera da prenotare
            msg = re.search(r"Non esistono[^<]*|Nessun record[^<]*", x)
            if not msg:
                raise CupError("Elenco delle prenotazioni non leggibile (portale in manutenzione o cambiato?)")
            raise NonTrovata(msg.group(0).strip())
        righe = [x[a:b] for a, b in zip(inizi, inizi[1:] + [len(x)])]
        stati =[(re.search(r"Stato:\s*(\S+)", _text(r[:400])) or [None, "?"])[1] for r in righe]
        pren = [i for i, s in enumerate(stati) if s.upper() == "PRENOTATO"]
        if not pren:
            raise NonAttiva("La prenotazione risulta in stato " + ", ".join(stati), stati)
        self.prenotate = [Prenotazione(q, _luogo(r), _cosa(r)) for r in (righe[i] for i in pren) if (q := _date(_text(r)))]
        self.n_prenotate = len(pren)
        if len(pren) > 1:
            DIARIO.append(f"elenco: righe {len(righe)}, prenotate {len(pren)}, con data {len(self.prenotate)}, "
                          f"date prenotate diverse {len({x.quando for x in self.prenotate})}, "
                          f"descrizioni vuote {sum(1 for x in self.prenotate if not x.cosa)}")
        self.riga = pren[0]
        # (indice della riga, data) di ogni riga PRENOTATO: la disdetta sceglie quella che l'utente ha confermato
        self.righe_prenotate = [(i, d) for i in pren if (d := _date(_text(righe[i])))]
        riga = righe[self.riga]
        quando = _date(_text(riga))
        if not quando:
            raise CupError("Data dell'appuntamento attuale non leggibile")
        return Prenotazione(quando, _luogo(riga), _cosa(riga))

    def disdici_dialogo(self, quando):
        """Preme "Disdici" sulla riga PRENOTATO di questa data (attuale() va chiamato prima) e ritorna l'id del
        pulsante "Sì" del dialogo di conferma. Il portale lo numera a ogni apertura (_t289 in una sessione):
        va sempre letto dal dialogo, mai fissato. CupError se il dialogo non e' riconoscibile con certezza."""
        righe = [i for i, d in self.righe_prenotate if d == quando]
        if len(righe) != 1:
            raise CupError("Non trovo un'unica prenotazione a quella data: non disdico")
        bottone = re.search(r'id="(%s:[^"]*:%d:disdiciButton)"' % (re.escape(L), righe[0]), self.lista_xml)
        if not bottone:
            raise CupError("Pulsante 'Disdici' non presente")
        _passo("disdetta")
        xml = self.lista.post({**self.search, **_event(bottone.group(1), "activate")})
        vs = re.search(r'<update id="[^"]*javax\.faces\.ViewState[^"]*"[^>]*><!\[CDATA\[(.*?)\]\]>', xml, re.S)
        if vs:
            self.lista.vs = vs.group(1)
        testo = html.unescape(xml)
        si = set()
        for m in re.finditer(r'<(?:a|button|input|span)\b[^>]*\sid="(%s:_t\d+)"[^>]*>' % re.escape(L), testo):
            valore = re.search(r'\svalue="([^"]*)"', m.group(0))
            etichetta = valore.group(1) if valore else _text(testo[m.end():m.end() + 300].split("<", 1)[0])
            if _chiave_comune(etichetta) == "SI":  # "Sì" (anche "Si'"): senza accento, come i comuni
                si.add(m.group(1))
        if len(si) != 1:
            ids = sorted({_id(b) for b in re.findall(r'id="([^"]*:_t\d+)"', testo)})[:12]
            DIARIO.append(f"disdetta: dialogo non riconosciuto, pulsanti Si' {len(si)}, componenti {ids}")
            raise CupError("Dialogo di conferma della disdetta non riconosciuto: non disdico")
        return si.pop()

    def alternative(self, estendi=0):
        """Slot offerti da "Sposta appuntamento": la proposta e gli "Appuntamenti Disponibili".
        estendi: quante volte premere "Estendi area di ricerca" (le aziende piu' lontane compaiono
        solo se hanno posti); l'elenco finale comprende le aree precedenti."""
        sposta = re.search(r'id="(%s:[^"]*:%d:spostaButton)"' % (re.escape(L), self.riga), self.lista_xml)
        if not sposta:
            raise CupError("Pulsante 'Sposta appuntamento' non presente")
        _passo("appuntamenti")
        self.lista.post({**self.search, **_event(sposta.group(1), "activate")})
        page = _get(self.s, RICETTA_URL)  # qui il portale calcola le disponibilita': puo' essere lento
        if "Appuntamenti Proposti" not in page:
            raise CupError("Il portale non ha aperto la pagina degli appuntamenti dopo 'Sposta'")
        self.modo = "sposta"
        return self.appuntamenti(page, estendi)

    # --- prenotazione nuova: passi Ricerca e Prestazioni ---------------------------------------
    def _segui(self, xml):
        """Dopo un "avanti" del portale: la pagina del passo successivo (redirect, o la stessa pagina)."""
        redirect = re.search(r'<redirect url="([^"]+)"', xml)
        url = html.unescape(redirect.group(1)) if redirect else RICETTA_URL
        if not url.startswith(CUP + "/"):
            raise CupError("Il portale ha indicato un indirizzo esterno: non proseguo")
        return _get(self.s, url)  # verso gli appuntamenti il portale puo' essere lento

    def ricetta(self):
        """Passo "Ricerca": codice fiscale + NRE e "Prosegui", come il browser (il pulsante visibile fa
        partire il comando nascosto epPrestazioniForwardNavigate). Non apre gli appuntamenti: non blocca
        date. GiaPrenotata se la ricetta ha gia' un appuntamento, NonTrovata se il portale la rifiuta."""
        _passo("ricerca")
        self.ric = _Form(self.s, _get(self.s, RICETTA_URL), R)
        src = R + ":epPrestazioniForwardNavigate"
        xml = self.ric.post({R + ":CFInput": self.cf, R + ":nreInput0": self.nre, "g-recaptcha-token": "",
                             "javax.faces.source": src, "javax.faces.partial.event": "click",
                             "javax.faces.partial.execute": f"{src} {R}", "javax.faces.partial.render": "@all",
                             "javax.faces.behavior.event": "action", "javax.faces.partial.ajax": "true"})
        errori = _errori(xml)
        DIARIO.append(f"ricerca: messaggi d'errore {len(errori)}")
        if any(re.search(r"\bgi(?:à|a'?)\s+presente", e, re.I) for e in errori):
            raise GiaPrenotata(errori[0])
        if any(re.search(r"ricett|\bnre\b|codice fiscale|scadut|inesistent", e, re.I)
               and not re.search(r"disponibil|temporane|riprov|servizio|token|captcha", e, re.I) for e in errori):
            raise NonTrovata(errori[0])  # il portale rifiuta la ricetta (un disservizio invece si riprova)
        if errori:
            raise CupError("Il portale ha risposto: " + errori[0])  # altro (anche temporaneo): si riprova
        self.modo = "nuova"
        page = self._segui(xml)
        if R + ":CFInput" in page:  # i passi sono pagine distinte: se c'e' ancora la ricerca, non e' andato avanti
            raise CupError("Il portale e' rimasto alla ricerca della ricetta: " + _struttura(page))
        self._carrello(page)
        DIARIO.append(f"dopo la ricerca: {_struttura(page)}, carrello {len(self.nomi)}")
        return page

    def _carrello(self, page):
        """Prestazioni nel carrello di una pagina: ne ricorda i nomi e il numero massimo visto. Con piu'
        prestazioni si prenotano tutte insieme, come le propone il portale (vedi prenota)."""
        nomi = _prestazioni(page)
        self.n_prestazioni = max(self.n_prestazioni, len(nomi))
        if nomi:
            self.nomi = nomi
            self.cosa = " + ".join(nomi)

    def fino_agli_appuntamenti(self, page):
        """Dal passo "Prestazioni" agli Appuntamenti: "Avanti" col form com'e', come fa una persona.
        Il passo non l'abbiamo mai visto dal vivo ("Sposta" lo salta): se non si riconosce un pulsante
        per andare avanti ci si ferma, e l'errore descrive la pagina (senza dati personali)."""
        _passo("appuntamenti")
        for _ in range(3):
            if "Appuntamenti Proposti" in page or "Appuntamenti Disponibili" in page:
                return page
            if RIEPILOGO in page:  # mai premere avanti su un Riepilogo: li' "avanti" e' la Conferma
                raise CupError("Il portale e' passato al Riepilogo prima degli appuntamenti: non proseguo")
            avanti = [b for b in re.findall(r'id="(_ricettaelettronica_WAR_cupprenotazione_:[^"]*nextButton[^"]*)"', page)
                      if "appuntamenti" not in b.lower() and "riepilogo" not in b.lower()]
            forms = set(re.findall(r'<form[^>]*id="([^"]+)"', page))
            avanti = [b for b in avanti if b.rsplit(":", 1)[0] in forms]
            if not avanti:
                raise CupError("Passo del portale non riconosciuto prima degli appuntamenti: " + _struttura(page))
            # solo nel passo Prestazioni: il pulsante e' suo o la pagina mostra il carrello
            if not any("prestazion" in x.lower() for x in avanti) and 'id="prestazioni_selezionate"' not in page:
                raise CupError("Passo del portale non riconosciuto prima degli appuntamenti: " + _struttura(page))
            # a parita', il pulsante "principale" in fondo alla pagina, come negli altri passi
            b = sorted(avanti, key=lambda x: ("main" not in x, x))[0]
            form = b.rsplit(":", 1)[0]
            spuntate, caselle = _caselle(_form_html(page, form) or "")
            DIARIO.append(f"passo: {_struttura(page)}, carrello {len(_prestazioni(page))}, "
                          f"caselle spuntate {spuntate}/{caselle}, avanti {_id(b)}")
            campi = {k: v for k, v in _form_fields(page, form).items()
                     if k not in (form, "javax.faces.encodedURL", "ice.window", "ice.view", "javax.faces.ViewState")}
            xml = _Form(self.s, page, form).post({**campi, **_event(b)})
            if _errori(xml):
                raise CupError("Il portale non va avanti: " + _errori(xml)[0])
            page = self._segui(xml)
            self._carrello(page)
        raise CupError("Troppi passi prima degli appuntamenti: " + _struttura(page))

    def appuntamenti(self, page, estendi=0):
        """Pagina Appuntamenti (la stessa per "Sposta" e per una prenotazione nuova): proposta e
        "Appuntamenti Disponibili", estendendo l'area se richiesto."""
        self._carrello(page)
        _passo("appuntamenti")
        self.app = _Form(self.s, page, A)
        prop_html = page[page.find("Appuntamenti Proposti"):]
        q = _date(_text(prop_html[:6000]))
        ha_proposta = re.search(r"'%s:[^']*:0:app_selector'" % re.escape(A), page)
        proposta = Slot(q, _luogo(prop_html), None, proposta=True) if q and ha_proposta else None

        # il click su "Altre disponibilita'" risale al pannello della proposta: il browser invia
        # prima il suo selettore (app_selector, o indisp_selector se non c'e' nessuna proposta),
        # ed e' quella risposta che contiene il pannello Appuntamenti Disponibili
        altre = re.search(r'<div class="[^"]*\bbtn\b[^"]*" id="(%s:[^"]+)"[^>]*>(?:(?!</div>).){0,800}?Altre disponibilit'
                          % re.escape(A), page, re.S)
        sel = re.search(r"'(%s:[^']*:0:(?:app|indisp)_selector)'" % re.escape(A), page)
        disp_html = ""
        if altre and sel:
            for xml in (self.app.post({**_event(A, param=False), sel.group(1): sel.group(1), "javax.faces.partial.event": "click",
                                       "ice.submit.type": "ice.s", "ice.submit.serialization": "form"}),
                        self.app.post(_event(altre.group(1)))):
                if "Appuntamenti Disponibili" in _text(xml):
                    disp_html = xml[xml.find("Appuntamenti Disponibili"):]
                    break
        _passo("estendi")
        for passo in range(estendi if disp_html else 0):
            area = re.search(r'id="(%s:nextArea)"' % re.escape(A), disp_html)
            if not area:
                break
            try:
                xml = self.app.post({**GEO, **_event(area.group(1), "activate"),
                                     "javax.faces.partial.render": f"{A} _ricettaelettronica_WAR_cupprenotazione_:allMsgs"})
            except requests.ReadTimeout:
                # il portale e' lento proprio ad allargare l'area: valgono le date gia' lette (proposta e aree
                # precedenti) invece di perdere tutto il controllo; il bot al giro dopo aspetta di piu'
                global AREA_INCOMPLETA, PIU_LENTA
                AREA_INCOMPLETA, PIU_LENTA = True, max(PIU_LENTA, _attesa()[1])
                DIARIO.append(f"estendi area: timeout al passo {passo + 1} di {estendi}, tengo le date lette")
                break
            if "Appuntamenti Disponibili" not in _text(xml):
                break
            disp_html = xml[xml.find("Appuntamenti Disponibili"):]
            pulisci = re.search(r"'(%s:notifyCleaner)'" % re.escape(A), xml)
            if pulisci:  # come il browser: chiude l'avviso "area estesa"
                self.app.post({**GEO, **_event(A, param=False), pulisci.group(1): pulisci.group(1),
                               "javax.faces.partial.event": "click", "ice.submit.type": "ice.s",
                               "ice.submit.serialization": "form"})

        slots = [proposta] if proposta else []
        # con piu' prestazioni: quante date disponibili riportano ciascuna (solo conteggi, per il diario)
        per_prestazione = collections.Counter()
        # ogni slot disponibile: data, luogo e (se non e' la proposta) il suo pulsante "Seleziona"
        for b in re.split(r'(?=<div class="captionAppointment-what")', disp_html)[1:]:
            quando = _date(_text(b))
            if not quando:
                continue
            if len(set(self.nomi)) > 1:
                conta = _conta_nomi(_text(b), self.nomi)
                per_prestazione.update(i for i, n in enumerate(dict.fromkeys(self.nomi)) if conta[n])
                per_prestazione["nessuna"] += not any(conta.values())
            btn = re.search(r'id="(%s:[^"]+)"[^>]*>(?:(?!</div>).){0,600}?>\s*Seleziona\s*<' % re.escape(A), b, re.S)
            if proposta and quando == proposta.quando and not btn:
                continue  # e' la stessa proposta ripetuta in cima all'elenco
            slots.append(Slot(quando, _luogo(b), btn.group(1) if btn else None))
        self.slots = sorted(slots, key=lambda s: s.quando)
        if self.modo == "nuova" or self.n_prestazioni > 1:
            DIARIO.append(f"appuntamenti ({self.modo}): carrello {len(self.nomi)}, "
                          f"pannelli proposta {page.count('Appuntamenti Proposti')}, "
                          f"selettori proposta {len(set(re.findall(r':(\d+):app_selector', page)))}, "
                          f"descrizioni {page.count('captionAppointment-desc')}, proposta {'si' if proposta else 'no'}, "
                          f"date {len(self.slots)}, con Seleziona {sum(1 for x in self.slots if x.seleziona_id)}, "
                          f"date uguali {len(self.slots) - len({x.key() for x in self.slots})}, "
                          f"altre disponibilita' {'pulsante' if altre else 'no pulsante'}/"
                          f"{sel.group(1).rsplit('_', 1)[0].rsplit(':', 1)[1] if sel else 'nessun selettore'}"
                          + (f", date per prestazione {[per_prestazione[i] for i in range(len(set(self.nomi)))]}"
                             f" nessuna {per_prestazione['nessuna']}" if len(set(self.nomi)) > 1 else ""))
        return self.slots

    def riepilogo(self, slot):
        """Seleziona lo slot e va al Riepilogo. Ritorna (testo, data letta, testo dopo la data, pagina html)."""
        if not slot.seleziona_id and not slot.proposta:
            raise CupError("Questa data non ha un pulsante 'Seleziona': non posso sceglierla")
        osserva = self.modo == "nuova" or self.n_prestazioni > 1
        _passo("riepilogo")
        if slot.seleziona_id:
            xml = self.app.post({**GEO, **_event(slot.seleziona_id)})
            errori = _errori(xml)
            if osserva:
                DIARIO.append(f"seleziona: messaggi d'errore {len(errori)}, rifiutata {'si' if 'alert-danger' in xml else 'no'}")
            if "alert-danger" in xml:
                raise CupError("Il portale ha rifiutato la selezione della data" + (": " + errori[0] if errori else ""))
        xml = self.app.post(_event(AVANTI + ":appuntamenti-nextButton-main"), form=AVANTI)
        redirect = re.search(r'<redirect url="([^"]+)"', xml)
        url = html.unescape(redirect.group(1)) if redirect else None
        if url and not url.startswith(CUP + "/"):
            raise CupError("Il portale ha indicato un indirizzo esterno: non proseguo")
        page = _get(self.s, url) if url else xml
        if "Riepilogo" not in page or RIEPILOGO + ":riepilogo-nextButton-bottom" not in page:
            errori = _errori(xml) or (_errori(page) if url else [])  # dopo un redirect, sulla pagina raggiunta
            if osserva:
                DIARIO.append(f"avanti: redirect {'si' if url else 'no'}, messaggi d'errore {len(errori)}, {_struttura(page)}")
            raise CupError("Non sono arrivato al Riepilogo" + (": " + errori[0] if errori else ""))
        t = _text(page)
        t = t[t.find("Prestazioni selezionate"):]
        if self.modo == "nuova" or len(self.nomi) > 1:
            date = [_date(x.group(0)) for x in DATE_RE.finditer(t)]
            dich = re.search(r"Prestazioni selezionate:?\s*(\d+)", t)
            conta = _conta_nomi(t, self.nomi)
            ore = len(re.findall(r"alle\s+ore", t, re.I))
            DIARIO.append(f"riepilogo ({self.modo}): date {len(date)}, date diverse {len(set(date))}, "
                          f"'alle ore' {ore}, "
                          f"prestazioni dichiarate {dich.group(1) if dich else '?'}, "
                          f"nomi trovati {sum(min(conta[n], 1) for n in set(self.nomi))}/{len(set(self.nomi))}")
        m = DATE_RE.search(t)
        if not m:
            return t, None, "", page
        return t, _date(m.group(0)), t[m.end():m.end() + 250], page

    def conferma(self, riepilogo_page):
        _passo("conferma")
        form = _Form(self.s, riepilogo_page, RIEPILOGO)
        campi = {k: v for k, v in _form_fields(riepilogo_page, RIEPILOGO).items()
                 if k not in (RIEPILOGO, "javax.faces.encodedURL", "ice.window", "ice.view", "javax.faces.ViewState")}
        return form.post({**campi, **_event(RIEPILOGO + ":riepilogo-nextButton-bottom")})


# --- API usata dal bot ------------------------------------------------------------------
ZONE = ("sede", "sedi", "comune", "comuni", "provincia", "tutte")  # dove l'utente accetta una data nuova
COMUNI_MAX = 30  # comuni al massimo in una zona "comuni"
SEDI_MAX = 20  # sedi al massimo in una zona "sedi"
# Ricetta mai prenotata con piu' prestazioni: dal vivo (2026-09-25) con una data di "Altre disponibilita'"
# il bot non e' arrivato al Riepilogo, mentre accettare la proposta del portale ("Avanti") ha prenotato tutto.
SOLO_PROPOSTA = ("Con piu' prestazioni prenoto solo la data proposta dal portale per tutte insieme: "
                 "questa e' un'altra data, sceglila dal portale o al call center")
ESTENDI_MAX = 4  # "Estendi area di ricerca" premuto al massimo tante volte (le aree lontane compaiono solo se hanno posti)


def _chiave_comune(nome):
    """"Mondovì" e "MONDOVI'" -> "MONDOVI": il portale scrive gli accenti come apostrofi."""
    return _norm(unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode())


# comune -> sigla della provincia (ISTAT): per le sedi il cui indirizzo non riporta la provincia.
# Dal vivo (2026-09-28): "CORSO INGHILTERRA - ()" per PRESIDIO - SUSA, scartata da "provincia di Torino"
_RIGHE_COMUNI = [r.split(";") for r in Path(__file__).with_name("comuni_piemonte.txt").read_text(encoding="utf-8")
                 .splitlines() if r and not r.startswith("#")]
COMUNI_PIEMONTE = {_chiave_comune(nome): sigla for nome, sigla, _, _ in _RIGHE_COMUNI}
NOMI_COMUNI = {_chiave_comune(nome): nome.upper() for nome, *_ in _RIGHE_COMUNI}  # chiave -> "MONDOVÌ"
COORD = {_chiave_comune(nome): (float(lat), float(lon)) for nome, _, lat, lon in _RIGHE_COMUNI}  # centro del comune


def vicini(nome, km):
    """[(nome ISTAT, km)] dei comuni entro km dal centro di nome (lui compreso, a 0 km), dal piu' vicino.
    Distanza in linea d'aria (haversine). Comune sconosciuto: []."""
    centro = COORD.get(_chiave_comune(nome))
    if not centro:
        return []
    lat1, lon1 = map(math.radians, centro)
    trovati = []
    for k, (lat, lon) in COORD.items():
        lat2, lon2 = math.radians(lat), math.radians(lon)
        a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        d = 2 * 6371 * math.asin(math.sqrt(a))
        if d <= km:
            trovati.append((NOMI_COMUNI[k], d))
    return sorted(trovati, key=lambda x: (x[1], x[0]))

# preset della zona "comuni": Torino e i comuni confinanti della prima cintura
CINTURA_TORINO = ("TORINO", "BEINASCO", "BORGARO TORINESE", "COLLEGNO", "GRUGLIASCO", "MONCALIERI", "NICHELINO",
                  "ORBASSANO", "RIVOLI", "SAN MAURO TORINESE", "SETTIMO TORINESE", "VENARIA REALE")


COMUNI_CORTI = {"SUSA", "BRA", "ASTI", "ALBA", "CEVA", "GAVI"}


def provincia(luogo):
    """Sigla della provincia dall'indirizzo del portale ("Via Roma, 1 - TORINO (TO)" -> "TO"); se manca,
    da quella del comune (vedi comune)."""
    m = re.search(r"\(([A-Z]{2})\)\s*$", luogo.indirizzo.upper())
    if m:
        return m.group(1)
    return COMUNI_PIEMONTE.get(_chiave_comune(comune(luogo)), "")


def comune(luogo):
    m = re.search(r"([^()]*?)\s*\([A-Z]{2}\)\s*$", luogo.indirizzo.upper())  # dopo l'ultima parentesi
    pezzi = [x.strip() for x in m.group(1).split("-")] if m else []
    if len(pezzi) >= 3 and _chiave_comune("-".join(pezzi[-2:])) in COMUNI_PIEMONTE:
        return "-".join(pezzi[-2:])  # comune col trattino nel nome: "... - BEURA-CARDEZZA (VB)"
    if len(pezzi) >= 2:
        return pezzi[-1]
    # indirizzo senza provincia: il comune scritto come pezzo intero del nome della sede ("PRESIDIO - SUSA").
    # I nomi corti sono spesso anche parole ("SALE", "ALTO", "NONE"): solo quelli con un ospedale o un presidio
    for pezzo in re.split(r"\s*-\s*", luogo.sede.upper()):
        k = _chiave_comune(pezzo)
        if k in COMUNI_PIEMONTE and (len(k) > 4 or k in COMUNI_CORTI):
            return pezzo.strip()
    return ""


def elenco_comuni(nomi):
    """Nomi scritti dall'utente -> (nomi ISTAT maiuscoli senza doppioni, nomi non riconosciuti)."""
    ok, ignoti = [], []
    for nome in nomi:
        k = _chiave_comune(nome)
        if k in NOMI_COMUNI:
            if NOMI_COMUNI[k] not in ok:
                ok.append(NOMI_COMUNI[k])
        elif k:
            ignoti.append(" ".join(nome.split()))
    return ok, ignoti


def e_cintura(elenco):
    return {_chiave_comune(x) for x in elenco} == {_chiave_comune(x) for x in CINTURA_TORINO}


def chiave_sede(sede, nome_comune):
    """Una sede della zona "sedi" e' la coppia sede + comune (come lo legge comune()): ci sono sedi omonime
    in comuni diversi ("POLIAMBULATORIO")."""
    return _norm(sede), _chiave_comune(nome_comune)


def zona_norm(zona):
    """{"tipo": sede|sedi|comune|comuni|provincia|tutte, "valore": ...}: per "comuni" una lista di nomi, per
    "sedi" una lista di {"sede", "comune"} (senza doppioni, al massimo SEDI_MAX), per gli altri una stringa.
    Accetta anche i formati precedenti (True/False = stessa sede si'/no, oppure solo il tipo come stringa)."""
    if isinstance(zona, dict) and zona.get("tipo") in ZONE:
        v = zona.get("valore") or ""
        if zona["tipo"] == "comuni":
            v = [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []
        elif zona["tipo"] == "sedi":
            coppie = {}
            for x in (v if isinstance(v, list) else []):
                if (isinstance(x, dict) and isinstance(x.get("sede"), str) and isinstance(x.get("comune", ""), str)
                        and _norm(x["sede"])):
                    voce = {"sede": x["sede"].strip(), "comune": x.get("comune", "").strip()}
                    coppie.setdefault(chiave_sede(voce["sede"], voce["comune"]), voce)
            v = list(coppie.values())[:SEDI_MAX]
        elif not isinstance(v, str):
            v = ""
        return {"tipo": zona["tipo"], "valore": v}
    if isinstance(zona, dict):
        zona = zona.get("tipo")
    zona = {True: "sede", False: "tutte"}.get(zona, zona) if isinstance(zona, (bool, str, type(None))) else None
    return {"tipo": zona if zona in ZONE else "sede", "valore": ""}


def estensioni(zona):
    """Per sedi, comune, comuni e provincia serve estendere l'area: le sedi fuori dall'azienda della prenotazione
    compaiono solo cosi'. "sede" e "tutte" restano nell'area proposta dal CUP."""
    return ESTENDI_MAX if zona_norm(zona)["tipo"] in ("sedi", "comune", "comuni", "provincia") else 0


def ammesso(slot, attuale, zona="sede"):
    """Se il dato che serve al confronto non si legge, la data non e' ammessa. Senza prenotazione
    (ricetta mai prenotata) la zona deve avere il suo valore: non c'e' una sede da cui ricavarlo."""
    z = zona_norm(zona)
    tipo, rif = z["tipo"], z["valore"]
    if not slot.luogo.sede:
        return False  # luogo non leggibile: mai proporlo
    if tipo == "sede":
        rif = rif or (attuale.luogo.sede if attuale else "")
        return bool(rif) and _norm(slot.luogo.sede) == _norm(rif)
    if tipo == "sedi":
        scelte = {chiave_sede(x["sede"], x["comune"]) for x in rif}
        return chiave_sede(slot.luogo.sede, comune(slot.luogo)) in scelte
    if tipo == "comune":
        rif = rif or (comune(attuale.luogo) if attuale else "")
        return bool(rif) and _chiave_comune(comune(slot.luogo)) == _chiave_comune(rif)
    if tipo == "comuni":
        k = _chiave_comune(comune(slot.luogo))
        return bool(k) and k in {_chiave_comune(x) for x in rif}
    if tipo == "provincia":
        rif = rif or (provincia(attuale.luogo) if attuale else "")
        return bool(rif) and provincia(slot.luogo) == rif.upper()
    return tipo == "tutte"


def giorno_si(quando, cal):
    """Il giorno (date o datetime) e' un giorno si' del calendario della ricetta (vuoto o None: tutti si').
    cal: {"no": [iso], "no_settimana": [0..6, lun=0], "no_fino": iso|"" (fino a quel giorno compreso tutti no),
    "si_fino": iso|"" (dopo quel giorno tutti no)}."""
    if not cal:
        return True
    d = quando.date() if isinstance(quando, datetime) else quando
    iso = d.isoformat()
    if cal.get("no_fino") and iso <= cal["no_fino"] or cal.get("si_fino") and iso > cal["si_fino"]:
        return False
    return d.weekday() not in (cal.get("no_settimana") or []) and iso not in (cal.get("no") or [])


def piu_tardi_ok(attuale, cal):
    """Si accetta una data piu' tardi della prenotazione solo se questa e' in un giorno no. Non per i
    calendari ricavati dalle regole di prima (cal["solo_prima"]): con quelle il bot anticipava soltanto."""
    return not giorno_si(attuale.quando, cal) and not (cal or {}).get("solo_prima")


def candidata(slot, attuale, zona, cal=None, solo_proposta=False):
    """La regola unica delle date da proporre (e da prenotare da soli): prenotabile, nella zona, in un giorno
    si' e prima della prenotazione. Se la prenotazione e' in un giorno no va bene anche una data piu' tardi
    (dopo lo spostamento e' in un giorno si': niente spostamenti a catena). attuale None: ricetta mai
    prenotata, ogni giorno si' va bene (con piu' prestazioni solo la proposta del portale, vedi SOLO_PROPOSTA)."""
    if not (slot.proposta or (slot.seleziona_id and not solo_proposta)):
        return False
    if not ammesso(slot, attuale, zona) or not giorno_si(slot.quando, cal):
        return False
    return attuale is None or slot.quando < attuale.quando or piu_tardi_ok(attuale, cal)


def cerca(cf, nre):
    """Solo la prenotazione attuale (usato alla registrazione): non apre "Sposta", non blocca slot."""
    return CupSession(cf, nre).attuale()


def check(cf, nre, zona="sede"):
    """{"attuale": Prenotazione, "slots": [Slot], "migliori": [Slot], "sessione": CupSession}.
    La sessione tiene lo slot proposto: se c'e' una data migliore la prenotazione deve continuare li'."""
    cup = CupSession(cf, nre)
    att = cup.attuale()
    doppio = doppione(cup.prenotate)
    if doppio and cup.n_prenotate == len(cup.prenotate):  # due prenotazioni: prima si sistema, poi si cerca
        return {"attuale": att, "slots": [], "sessione": cup, "migliori": [], "doppione": doppio}
    slots = cup.alternative(estendi=estensioni(zona))
    return {"attuale": att, "slots": slots, "sessione": cup,
            "migliori": [x for x in slots if candidata(x, att, zona)]}


def doppione(prenotate):
    """(da tenere, da disdire) se la stessa prestazione e' prenotata in due appuntamenti diversi (data o luogo): si
    tiene il piu' vicino e si disdice il piu' lontano. None se non c'e' un doppione. Righe con prestazioni
    diverse nello stesso appuntamento, o la stessa prestazione ripetuta nello stesso appuntamento (quantita'
    maggiore di uno), non sono doppioni."""
    gruppi = collections.defaultdict(list)
    for x in prenotate:
        if _norm(x.cosa):
            gruppi[_norm(x.cosa)].append(x)
    trovati = []
    for righe in gruppi.values():
        diversi = {(x.quando, x.luogo.key()) for x in righe}
        if len(diversi) > 1:
            ordinati = sorted(righe, key=lambda x: x.quando)
            trovati.append((ordinati[0], ordinati[-1]))
    return max(trovati, key=lambda t: t[1].quando) if trovati else None


def elimina_doppione(cf, nre, dry_run=True, fase=None):
    """Se la ricetta ha due prenotazioni disdice la piu' lontana. Il doppione deve risultare uguale in due
    letture con sessioni diverse (il portale a volte sbaglia): altrimenti non si disdice nulla. Ritorna
    (prenotazione tenuta, prenotazione disdetta), o None se non c'e' un doppione. CupError se qualcosa non torna;
    "Disdetta inviata" nel messaggio: esito incerto."""
    prima = CupSession(cf, nre)
    prima.attuale()
    d1 = doppione(prima.prenotate)
    if not d1 or prima.n_prenotate != len(prima.prenotate):
        return None
    time.sleep(3)
    seconda = CupSession(cf, nre)
    seconda.attuale()
    d2 = doppione(seconda.prenotate)
    chiave = lambda d: [(x.quando, x.luogo.key(), _norm(x.cosa)) for x in d] if d else None  # noqa: E731
    if chiave(d1) != chiave(d2):
        raise CupError("Le due letture dell'elenco non concordano: non disdico")
    disdici(cf, nre, d1[1].quando, dry_run=dry_run, fase=fase)
    return d1


def nuova(cf, nre):
    """Registrazione di una ricetta mai prenotata: solo il passo "Ricerca" (non apre gli appuntamenti,
    non blocca date). Ritorna la prestazione se la pagina la mostra, altrimenti "".
    GiaPrenotata / NonTrovata come CupSession.ricetta."""
    cup = CupSession(cf, nre)
    cup.ricetta()
    return cup.cosa


def check_nuova(cf, nre, zona="tutte"):
    """Come check, per una ricetta mai prenotata: "attuale" e' None e ogni data e' buona se e' nella zona.
    Con piu' prestazioni vale solo la proposta del portale (vedi SOLO_PROPOSTA)."""
    cup = CupSession(cf, nre)
    page = cup.fino_agli_appuntamenti(cup.ricetta())
    slots = cup.appuntamenti(page, estendi=estensioni(zona))
    solo_proposta = cup.n_prestazioni > 1
    return {"attuale": None, "cosa": cup.cosa, "slots": slots, "sessione": cup, "solo_proposta": solo_proposta,
            "migliori": [x for x in slots if candidata(x, None, zona, solo_proposta=solo_proposta)]}


def _conta_nomi(testo, nomi):
    """Quante volte il testo riporta ciascuna prestazione, come parole intere: i nomi piu' lunghi per primi e
    tolti dal testo, cosi' "ECOGRAFIA ADDOME" non si ritrova dentro "ECOGRAFIA ADDOME COMPLETO"."""
    t = " " + re.sub(r"\s+", " ", testo.upper()) + " "
    conta = collections.Counter()
    for n in sorted(set(nomi), key=len, reverse=True):
        pat = r"(?<![A-Z0-9])" + re.escape(re.sub(r"\s+", " ", n.upper().strip())) + r"(?![A-Z0-9])"
        conta[n] = len(re.findall(pat, t))
        t = re.sub(pat, " ", t)
    return conta


def _stesso_appuntamento(testo, slot):
    """Ogni appuntamento del Riepilogo e' quello scelto: stessa data e ora e, subito dopo, lo stesso luogo.
    Nessuna data in un altro formato: tanti "alle ore", "Quando" e mesi con l'anno quante date riconosciute,
    e nessuna data in cifre."""
    date = list(DATE_RE.finditer(testo))
    mesi_anno = re.findall(r"\b(?:%s)\s+\d{4}\b" % "|".join(MESI), testo, re.I)
    quando = re.findall(r"\bQuando\b", testo)
    if (not date or len(re.findall(r"alle\s+ore", testo, re.I)) != len(date) or len(mesi_anno) != len(date)
            or (quando and len(quando) != len(date)) or re.search(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{4}\b", testo)):
        return False
    return all(
        _date(m.group(0)) == slot.quando and _norm(testo[m.end():m.end() + 250]).startswith(slot.luogo.key())
        for m in date)


def _tutte_al_posto(righe, slot, nomi, vecchia=None):
    """Dopo la Conferma: le righe PRENOTATO al posto scelto coprono tutte le prestazioni (una riga per
    prestazione, o righe che le nominano tutte) e, spostando, nessuna e' rimasta alla data vecchia."""
    al_posto = [x for x in righe if x.quando == slot.quando and x.luogo.key() == slot.luogo.key()]
    if vecchia and any(x.quando == vecchia for x in righe):
        return False
    if len(nomi) <= 1:
        return bool(al_posto)
    conta = _conta_nomi(" | ".join(x.cosa for x in al_posto), nomi)
    return len(al_posto) >= len(nomi) or all(conta[n] >= k for n, k in collections.Counter(nomi).items())


def _verifica_riepilogo(testo, data_riep, dopo_data, slot, cosa):
    if not slot.luogo.sede or not cosa:
        raise CupError("Luogo o prestazione non leggibili: non confermo")
    if data_riep != slot.quando:
        raise CupError(f"Il riepilogo riporta una data diversa ({data_riep}) da quella scelta ({slot.quando})")
    if _norm(cosa) not in _norm(testo):
        raise CupError("Il riepilogo non riporta la stessa prestazione della prenotazione attuale")
    # sede e ambulatorio sono scritti subito dopo la data e devono essere quelli dello slot scelto
    if not _norm(dopo_data).startswith(slot.luogo.key()):
        raise CupError("Il riepilogo riporta un luogo diverso da quello scelto")


def prenota(cf, nre, slot, sessione=None, zona="sede", dry_run=True, libera=False, nuova=False, calendario=None,
            fase=None):
    """Come _prenota, e nel diario (e in TEMPI) quanto e' durata ogni fase: per capire dove va il tempo di una
    prenotazione. fase: funzione chiamata con "riepilogo", "conferma" (prima di inviarla: da li' l'esito puo'
    essere incerto) e "verifica"; un suo errore non ferma la prenotazione."""
    tempi, inizio = [], [time.time()]
    TEMPI.clear()

    def tappa(nome):
        ora = time.time()
        tempi.append(f"{nome} {ora - inizio[0]:.1f}s")
        TEMPI[nome] = round(ora - inizio[0], 1)
        inizio[0] = ora

    def avvisa(nome):
        try:
            if fase:
                fase(nome)
        except Exception as e:
            DIARIO.append(f"fase {nome}: {type(e).__name__}")
            if nome == "conferma":  # la Conferma non e' ancora partita: senza lo stato salvato non si invia
                raise CupError("Non riesco a salvare lo stato della prenotazione: non confermo") from None
    try:
        return _prenota(cf, nre, slot, sessione, zona, dry_run, libera, nuova, tappa, calendario, avvisa)
    finally:
        tappa("fine")
        DIARIO.append("tempi: " + ", ".join(tempi))


def _prenota(cf, nre, slot, sessione, zona, dry_run, libera, nuova, tappa, calendario=None, fase=lambda nome: None):
    """Sposta la prenotazione sullo slot. sessione: quella del controllo che ha trovato lo slot
    (lo tiene bloccato per noi); se manca o fallisce si riparte da una sessione nuova.
    Con dry_run si ferma al Riepilogo. Ritorna un messaggio; CupError se un controllo fallisce.
    libera: scelta esplicita dell'utente di una data vista (anche fuori area o piu' tardi): niente filtro
    su zona e anticipo, ma restano tutte le verifiche sul Riepilogo e dopo la conferma.
    nuova: ricetta mai prenotata (prima prenotazione). Se nel frattempo risulta prenotata: GiaPrenotata.
    calendario: i giorni si'/no (vedi giorno_si), riletti al momento: lo slot deve cadere in un giorno si' e
    puo' essere piu' tardi dell'attuale solo se la prenotazione appena letta dal portale e' in un giorno no."""
    insieme = []  # prestazioni prenotate nello stesso appuntamento di quello da spostare
    if nuova:
        try:
            att = CupSession(cf, nre).attuale()
        except NonTrovata:
            att = None  # il portale dice che non ci sono prenotazioni: si puo' prenotare
            # ma a volte sbaglia ("Nessun record" per ricette prenotate): una seconda lettura, con una sessione nuova
            time.sleep(3)
            try:
                att2 = CupSession(cf, nre).attuale()
            except (NonTrovata, NonAttiva):
                att2 = None
            if att2:
                raise GiaPrenotata(f"Nel frattempo la ricetta risulta prenotata al {att2.quando:%d/%m/%Y %H:%M}")
        except NonAttiva as e:
            if not e.solo_disdette():  # erogata, in corso...: meglio non aggiungere un appuntamento
                raise CupError(f"{e}: non prenoto, verifica sul portale")
            att = None
        else:
            raise GiaPrenotata(f"Nel frattempo la ricetta risulta prenotata al {att.quando:%d/%m/%Y %H:%M}")
    else:
        lista = CupSession(cf, nre)  # sessione a parte: non tocca lo stato di quella del controllo
        att = lista.attuale()
        if lista.n_prenotate > len(lista.prenotate):  # una riga prenotata senza data leggibile
            raise CupError("Elenco delle prenotazioni non leggibile del tutto: non sposto")
        # piu' prestazioni prenotate nello stesso appuntamento: si spostano insieme o niente
        insieme = [x.cosa for x in lista.prenotate if x.quando == att.quando]
    tappa("elenco")
    if not libera and not giorno_si(slot.quando, calendario):
        raise CupError(f"Lo slot {slot.quando:%d/%m/%Y %H:%M} e' in un giorno segnato no nel calendario")
    if not dry_run and not libera and att and slot.quando >= att.quando and not piu_tardi_ok(att, calendario):
        raise CupError(f"Lo slot {slot.quando:%d/%m/%Y %H:%M} non e' prima dell'appuntamento attuale "
                       f"({att.quando:%d/%m/%Y %H:%M})")
    if not libera and not ammesso(slot, att, zona):
        raise CupError(f"Sede non ammessa dalle tue preferenze: {slot.luogo}")
    if not (slot.proposta or slot.seleziona_id):
        raise CupError("Questa data non ha un pulsante 'Seleziona': non posso sceglierla")
    if sessione is not None and (sessione.search.get(L + ":IDSearchValueInput") != nre
                                 or getattr(sessione, "modo", "") != ("nuova" if nuova else "sposta")):
        sessione = None  # sessione di un'altra ricetta o dell'altro flusso: mai usarla

    if nuova and not slot.proposta and getattr(sessione, "n_prestazioni", 0) > 1:
        raise CupError(SOLO_PROPOSTA)  # prima di aprire altre sessioni: non bloccare date per niente

    def arriva_al_riepilogo(cup):
        uguali = [x for x in cup.slots if x.key() == slot.key()]
        if len(uguali) > 1:  # due date con la stessa chiave: meglio non scegliere a caso
            raise CupError(f"Slot {slot.quando:%d/%m/%Y %H:%M} presente {len(uguali)} volte: non scelgo a caso")
        if not uguali:
            raise CupError(f"Slot {slot.quando:%d/%m/%Y %H:%M} non piu' disponibile")
        s = uguali[0]
        if nuova and cup.n_prestazioni > 1 and not s.proposta:
            raise CupError(SOLO_PROPOSTA)  # prima di "Seleziona", che bloccherebbe la data
        return (s,) + cup.riepilogo(s)

    fase("riepilogo")
    try:
        if sessione is None:
            raise CupError("nessuna sessione del controllo")
        cup = sessione
        s, testo, data_riep, dopo, page = arriva_al_riepilogo(cup)
    except (CupError, requests.RequestException) as e:
        primo_errore = descrivi(e)
        cup = CupSession(cf, nre)
        if nuova:
            cup.appuntamenti(cup.fino_agli_appuntamenti(cup.ricetta()), estendi=estensioni(zona))
        else:
            cup.attuale()
            cup.alternative(estendi=estensioni(zona))
        try:
            s, testo, data_riep, dopo, page = arriva_al_riepilogo(cup)
        except CupError as e2:
            raise CupError(f"{e2} (tentativo nella sessione originale: {primo_errore})")
    tappa("riepilogo")
    # la prestazione del Riepilogo deve essere quella della prenotazione (o, per una nuova, quella
    # che il portale ha messo nel carrello): se non si legge, _verifica_riepilogo non conferma
    _verifica_riepilogo(testo, data_riep, dopo, s, att.cosa if att else (cup.nomi[:1] or [cup.cosa])[0])
    # le prestazioni che devono finire tutte nell'appuntamento scelto: quelle del carrello per una prima
    # prenotazione, quelle prenotate insieme per uno spostamento
    if nuova:
        nomi = list(cup.nomi)
        # il carrello di un passo precedente ne aveva di piu': il portale ne prenoterebbe solo una parte
        if not nomi or len(nomi) != cup.n_prestazioni or not all(re.search(r"[A-Za-z]{4}", n) for n in nomi):
            raise CupError("Prestazioni della ricetta non riconosciute con certezza: non confermo")
    else:
        nomi = insieme if len(insieme) > 1 else []
        if nomi and not all(nomi):
            raise Separerebbe("Prestazioni prenotate insieme non leggibili dall'elenco: non sposto")
    if len(nomi) > 1:
        separa = Separerebbe if not nuova else CupError
        dich = re.search(r"Prestazioni selezionate:?\s*(\d+)", testo)
        conta = _conta_nomi(testo, nomi)
        if not dich or int(dich.group(1)) != len(nomi) or any(
                conta[n] < k for n, k in collections.Counter(nomi).items()):
            raise separa("Il Riepilogo non riporta tutte le prestazioni della ricetta: non confermo" if nuova else
                         "Spostare questo appuntamento lo separerebbe dalle altre prestazioni prenotate insieme: "
                         "non confermo")
        if not _stesso_appuntamento(testo, s):
            raise separa("Il portale mette le prestazioni in appuntamenti diversi (o il Riepilogo non si legge "
                         "con certezza): non confermo, prenota dal portale o al call center")
    elif nuova:
        # C: una sola prestazione nel carrello, ma il Riepilogo ne dichiara altre: il portale ne ha nascoste
        dich = re.search(r"Prestazioni selezionate:?\s*(\d+)", testo)
        if dich and int(dich.group(1)) != 1:
            raise CupError("Il Riepilogo riporta piu' prestazioni del carrello: non confermo")
        if {_date(x.group(0)) for x in DATE_RE.finditer(testo)} != {s.quando}:
            raise CupError("Il Riepilogo contiene piu' appuntamenti: non confermo")
    if dry_run:
        return f"PROVA: arrivato al Riepilogo di {s!r}, non confermo."

    # da qui la Conferma e' partita: qualunque problema e' "esito incerto", mai "non spostata". La fase si
    # segna PRIMA di inviarla: se il bot si ferma durante l'invio, al riavvio l'esito e' incerto, non "non fatta"
    fase("conferma")
    nuova_att = None
    try:
        cup.conferma(page)
    except Exception as e:
        # nessuna risposta (timeout, 504...): la Conferma puo' essere arrivata lo stesso, si verifica comunque.
        # Dal vivo (2026-09-28) la risposta e' scaduta dopo 180 s ma la prenotazione era fatta
        DIARIO.append(f"conferma: {descrivi(e)}, verifico con una sessione nuova")
    tappa("conferma")
    fase("verifica")
    # verifica con pause crescenti (la prima dopo pochi secondi), in tutto al massimo VERIFICA_MAX: ogni
    # richiesta aspetta la risposta quanto il passo "verifica", mai oltre SCADENZA (vedi _limiti)
    global SCADENZA
    SCADENZA = time.monotonic() + VERIFICA_MAX
    try:
        for pausa in VERIFICA_PAUSE:
            if SCADENZA - time.monotonic() - pausa < VERIFICA_MIN:
                break  # non c'e' piu' tempo per un tentativo sensato
            time.sleep(pausa)
            try:
                verifica = CupSession(cf, nre)
                verifica.passo_elenco = "verifica"
                nuova_att = verifica.attuale()
                # una riga prenotata senza data leggibile potrebbe essere rimasta alla data vecchia
                if verifica.n_prenotate <= len(verifica.prenotate) and _tutte_al_posto(
                        verifica.prenotate or [nuova_att], s, nomi, att.quando if len(insieme) > 1 else None):
                    tappa("verifica")
                    return "Prenotazione fatta." if nuova else "Prenotazione spostata."
            except (CupError, requests.RequestException) as e:
                DIARIO.append(f"verifica: {descrivi(e) if isinstance(e, requests.RequestException) else 'pagina non letta'}")
    except Exception:
        pass  # qualunque errore dopo la Conferma: esito incerto, sotto
    finally:
        SCADENZA = None
    tappa("verifica")
    stato = f"al {nuova_att.quando:%d/%m/%Y %H:%M}" if nuova_att else "non verificabile"
    raise CupError(f"Conferma inviata, esito incerto: la prenotazione risulta {stato}. "
                   f"Controlla subito su {LISTA_URL} o al {CALL_CENTER}.")


def disdici(cf, nre, quando, dry_run=True, fase=None):
    """Disdice la prenotazione di questa data (datetime): elenco, "Disdici", dialogo, "Sì", messaggio del portale
    e verifica con una sessione nuova. Con dry_run si ferma al dialogo, senza il "Sì". Ritorna un messaggio;
    CupError se un controllo fallisce. fase: come in prenota ("conferma" prima del "Sì", poi "verifica").
    Dopo il "Sì" qualunque problema e' "esito incerto": la disdetta puo' essere partita lo stesso."""
    cup = CupSession(cf, nre)
    try:
        cup.attuale()
    except NonTrovata:
        raise CupError("Il portale non mostra prenotazioni per questa ricetta: non c'e' niente da disdire")
    si = cup.disdici_dialogo(quando)
    if dry_run:
        return "PROVA: arrivato al dialogo di conferma della disdetta, non confermo."
    if fase:
        fase("conferma")
    esito = ""
    try:
        esito = _text(html.unescape(cup.lista.post({**cup.search, **_event(si, "activate")})))
    except Exception as e:
        DIARIO.append(f"disdetta: {descrivi(e)}, verifico con una sessione nuova")
    if fase:
        fase("verifica")
    if re.search(r"disdett\w* con successo", esito, re.I):
        return "Prenotazione disdetta."
    # nessun messaggio (timeout, pagina diversa): si guarda il portale con sessioni nuove, pause crescenti
    global SCADENZA
    SCADENZA = time.monotonic() + VERIFICA_MAX
    ancora = None
    try:
        for pausa in VERIFICA_PAUSE:
            if SCADENZA - time.monotonic() - pausa < VERIFICA_MIN:
                break
            time.sleep(pausa)
            try:
                verifica = CupSession(cf, nre)
                verifica.passo_elenco = "verifica"
                verifica.attuale()
                ancora = quando in [d for _, d in verifica.righe_prenotate]
                if not ancora and verifica.n_prenotate <= len(verifica.righe_prenotate):
                    return "Prenotazione disdetta."
            except (NonTrovata, NonAttiva):
                return "Prenotazione disdetta."
            except (CupError, requests.RequestException):
                DIARIO.append("verifica: pagina non letta")
    except Exception:
        pass
    finally:
        SCADENZA = None
    stato = "ancora presente" if ancora else "non verificabile"
    raise CupError(f"Disdetta inviata, esito incerto: la prenotazione risulta {stato}. "
                   f"Controlla subito su {LISTA_URL} o al {CALL_CENTER}.")
