"""CUP Piemonte via HTTP (niente browser): cerca date anticipate e, su richiesta, sposta la prenotazione.

Flusso del portale cup.isan.csi.it, replicato campo per campo dai POST del browser:
  1. Recupera Prenotazioni: ricerca per codice fiscale + NRE -> appuntamento attuale
  2. "Sposta appuntamento" -> pagina Appuntamenti Proposti
  3. "Altre disponibilita'" -> Appuntamenti Disponibili
  4. (solo per prenotare) "Seleziona" sullo slot -> "Avanti" -> Riepilogo -> "Conferma"

Comportamento del portale da tenere presente: aprire "Sposta" fa tenere lo slot proposto a quella
sessione per ~40 minuti, e cosi' portare uno slot fino al Riepilogo. Non c'e' modo di rilasciarlo
(ne' "Annulla" ne' il logout lo liberano): scade da solo. Per questo la prenotazione di uno slot
trovato da un controllo continua nella stessa sessione, e i controlli non vanno fatti troppo spesso.
"""
import html
import re
import time
from dataclasses import dataclass
from datetime import datetime

import requests

CUP = "https://cup.isan.csi.it"
LISTA_URL = CUP + "/web/guest/lista-prenotazioni"
RICETTA_URL = CUP + "/ricetta-dematerializzata"
CALL_CENTER = "800 000 500"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

L = "_listaprenotazioni_WAR_cupprenotazione_:prescrizioniForm"
A = "_ricettaelettronica_WAR_cupprenotazione_:appuntamentiForm"
AVANTI = "_ricettaelettronica_WAR_cupprenotazione_:appuntamenti-form-main"
RIEPILOGO = "_ricettaelettronica_WAR_cupprenotazione_:riepilogoForm"

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


class NonTrovata(CupError):
    """Il portale non trova prenotazioni per questo codice fiscale + NRE."""


class NonAttiva(CupError):
    """La ricetta c'e' ma nessuna prenotazione e' in stato PRENOTATO (disdetta, gia' erogata...)."""


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
    def __init__(self, quando, luogo, seleziona_id):
        self.quando, self.luogo = quando, luogo
        self.seleziona_id = seleziona_id  # None = e' la proposta, gia' selezionata dal portale

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
        r = self.s.post(self.enc, data=data, timeout=60, headers={
            "Faces-Request": "partial/ajax", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        r.raise_for_status()
        return r.text


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


# --- sessione sul portale ---------------------------------------------------------------
class CupSession:
    def __init__(self, cf, nre):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.search = {L + ":CFInput": cf, L + ":IDSearchTypeInput_input": "nre-label", L + ":IDSearchValueInput": nre}
        self.slots = []

    def attuale(self):
        """La prenotazione in stato PRENOTATO per questa ricetta."""
        self.lista = _Form(self.s, self.s.get(LISTA_URL, timeout=30).text, L)
        src = L + ":filterPrescriptionNavigate"
        self.lista_xml = self.lista.post({**self.search, "javax.faces.source": src, "javax.faces.partial.event": "click",
                                          "javax.faces.partial.execute": f"{src} {L}", "javax.faces.partial.render": "@all",
                                          "javax.faces.behavior.event": "action", "javax.faces.partial.ajax": "true"})
        x = self.lista_xml
        inizi = [m.start() for m in re.finditer(r"Stato:", x)]
        if not inizi:
            msg = re.search(r"Non esistono[^<]*|Nessun record[^<]*", x)
            raise NonTrovata(msg.group(0).strip() if msg else "Prenotazione non trovata")
        righe = [x[a:b] for a, b in zip(inizi, inizi[1:] + [len(x)])]
        stati =[(re.search(r"Stato:\s*(\S+)", _text(r[:400])) or [None, "?"])[1] for r in righe]
        pren = [i for i, s in enumerate(stati) if s.upper() == "PRENOTATO"]
        if not pren:
            raise NonAttiva("La prenotazione risulta in stato " + ", ".join(stati))
        self.riga = pren[0]
        riga = righe[self.riga]
        quando = _date(_text(riga))
        if not quando:
            raise CupError("Data dell'appuntamento attuale non leggibile")
        return Prenotazione(quando, _luogo(riga), _cosa(riga))

    def alternative(self):
        """Slot offerti da "Sposta appuntamento": la proposta e gli "Appuntamenti Disponibili"."""
        sposta = re.search(r'id="(%s:[^"]*:%d:spostaButton)"' % (re.escape(L), self.riga), self.lista_xml)
        if not sposta:
            raise CupError("Pulsante 'Sposta appuntamento' non presente")
        self.lista.post({**self.search, **_event(sposta.group(1), "activate")})
        page = self.s.get(RICETTA_URL, timeout=30).text
        if "Appuntamenti Proposti" not in page:
            raise CupError("Il portale non ha aperto la pagina degli appuntamenti dopo 'Sposta'")
        self.app = _Form(self.s, page, A)
        prop_html = page[page.find("Appuntamenti Proposti"):]
        q = _date(_text(prop_html[:6000]))
        proposta = Slot(q, _luogo(prop_html), None) if q else None

        # il click su "Altre disponibilita'" risale al pannello della proposta: il browser invia
        # prima il suo app_selector (evidenzia la proposta), ed e' quella risposta che contiene
        # il pannello Appuntamenti Disponibili
        altre = re.search(r'<div class="[^"]*\bbtn\b[^"]*" id="(%s:[^"]+)"[^>]*>(?:(?!</div>).){0,800}?Altre disponibilit'
                          % re.escape(A), page, re.S)
        sel = re.search(r"'(%s:[^']*:0:app_selector)'" % re.escape(A), page)
        disp_html = ""
        if altre and sel:
            for xml in (self.app.post({**_event(A, param=False), sel.group(1): sel.group(1), "javax.faces.partial.event": "click",
                                       "ice.submit.type": "ice.s", "ice.submit.serialization": "form"}),
                        self.app.post(_event(altre.group(1)))):
                if "Appuntamenti Disponibili" in _text(xml):
                    disp_html = xml[xml.find("Appuntamenti Disponibili"):]
                    break

        slots = [proposta] if proposta else []
        # ogni slot disponibile: data, luogo e (se non e' la proposta) il suo pulsante "Seleziona"
        for b in re.split(r'(?=<div class="captionAppointment-what")', disp_html)[1:]:
            quando = _date(_text(b))
            if not quando:
                continue
            btn = re.search(r'id="(%s:[^"]+)"[^>]*>(?:(?!</div>).){0,600}?>\s*Seleziona\s*<' % re.escape(A), b, re.S)
            if proposta and quando == proposta.quando and not btn:
                continue  # e' la stessa proposta ripetuta in cima all'elenco
            slots.append(Slot(quando, _luogo(b), btn.group(1) if btn else None))
        self.slots = sorted(slots, key=lambda s: s.quando)
        return self.slots

    def riepilogo(self, slot):
        """Seleziona lo slot e va al Riepilogo. Ritorna (testo, data letta, testo dopo la data, pagina html)."""
        if slot.seleziona_id:
            xml = self.app.post({A + ":localGeoSelectorsavailable:macrozonaSelectMenuavailable_input": "NO_VALUE",
                                 A + ":localGeoSelectorsavailable:zonaSelectMenuavailable_input": "NO_VALUE",
                                 A + ":localGeoSelectorsavailable:sedeSelectMenuavailable_input": "NO_VALUE",
                                 **_event(slot.seleziona_id)})
            if "alert-danger" in xml:
                raise CupError("Il portale ha rifiutato la selezione della data")
        xml = self.app.post(_event(AVANTI + ":appuntamenti-nextButton-main"), form=AVANTI)
        redirect = re.search(r'<redirect url="([^"]+)"', xml)
        url = html.unescape(redirect.group(1)) if redirect else None
        if url and not url.startswith(CUP + "/"):
            raise CupError("Il portale ha indicato un indirizzo esterno: non proseguo")
        page = self.s.get(url, timeout=30).text if url else xml
        if "Riepilogo" not in page or RIEPILOGO + ":riepilogo-nextButton-bottom" not in page:
            raise CupError("Non sono arrivato al Riepilogo")
        t = _text(page)
        t = t[t.find("Prestazioni selezionate"):]
        m = DATE_RE.search(t)
        if not m:
            return t, None, "", page
        return t, _date(m.group(0)), t[m.end():m.end() + 250], page

    def conferma(self, riepilogo_page):
        form = _Form(self.s, riepilogo_page, RIEPILOGO)
        campi = {k: v for k, v in _form_fields(riepilogo_page, RIEPILOGO).items()
                 if k not in (RIEPILOGO, "javax.faces.encodedURL", "ice.window", "ice.view", "javax.faces.ViewState")}
        return form.post({**campi, **_event(RIEPILOGO + ":riepilogo-nextButton-bottom")})


# --- API usata dal bot ------------------------------------------------------------------
def ammesso(slot, attuale, stessa_sede):
    if not slot.luogo.sede:
        return False  # luogo non leggibile: mai proporlo
    return not stessa_sede or (bool(attuale.luogo.sede) and _norm(slot.luogo.sede) == _norm(attuale.luogo.sede))


def cerca(cf, nre):
    """Solo la prenotazione attuale (usato alla registrazione): non apre "Sposta", non blocca slot."""
    return CupSession(cf, nre).attuale()


def check(cf, nre, stessa_sede=True):
    """{"attuale": Prenotazione, "slots": [Slot], "migliori": [Slot], "sessione": CupSession}.
    La sessione tiene lo slot proposto: se c'e' una data migliore la prenotazione deve continuare li'."""
    cup = CupSession(cf, nre)
    att = cup.attuale()
    slots = cup.alternative()
    return {"attuale": att, "slots": slots, "sessione": cup,
            "migliori": [x for x in slots if x.quando < att.quando and ammesso(x, att, stessa_sede)]}


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


def prenota(cf, nre, slot, sessione=None, stessa_sede=True, dry_run=True):
    """Sposta la prenotazione sullo slot. sessione: quella del controllo che ha trovato lo slot
    (lo tiene bloccato per noi); se manca o fallisce si riparte da una sessione nuova.
    Con dry_run si ferma al Riepilogo. Ritorna un messaggio; CupError se un controllo fallisce."""
    att = CupSession(cf, nre).attuale()  # sessione a parte: non tocca lo stato di quella del controllo
    if not dry_run and slot.quando >= att.quando:
        raise CupError(f"Lo slot {slot.quando:%d/%m/%Y %H:%M} non e' prima dell'appuntamento attuale "
                       f"({att.quando:%d/%m/%Y %H:%M})")
    if not ammesso(slot, att, stessa_sede):
        raise CupError(f"Sede non ammessa dalle tue preferenze: {slot.luogo.sede}")

    def arriva_al_riepilogo(cup):
        s = next((x for x in cup.slots if x.key() == slot.key()), None)
        if not s:
            raise CupError(f"Slot {slot.quando:%d/%m/%Y %H:%M} non piu' disponibile")
        return (s,) + cup.riepilogo(s)

    try:
        if sessione is None:
            raise CupError("nessuna sessione del controllo")
        cup = sessione
        s, testo, data_riep, dopo, page = arriva_al_riepilogo(cup)
    except (CupError, requests.RequestException) as e:
        primo_errore = str(e)
        cup = CupSession(cf, nre)
        cup.attuale()
        cup.alternative()
        try:
            s, testo, data_riep, dopo, page = arriva_al_riepilogo(cup)
        except CupError as e2:
            raise CupError(f"{e2} (tentativo nella sessione originale: {primo_errore})")
    _verifica_riepilogo(testo, data_riep, dopo, s, att.cosa)
    if dry_run:
        return f"PROVA: arrivato al Riepilogo di {s!r}, non confermo."

    # da qui la Conferma e' partita: qualunque problema e' "esito incerto", mai "non spostata"
    nuova = None
    try:
        cup.conferma(page)
        for _ in range(3):
            try:
                nuova = CupSession(cf, nre).attuale()
                if nuova.quando == s.quando:
                    return "Prenotazione spostata."
            except (CupError, requests.RequestException):
                pass
            time.sleep(10)
    except Exception:
        pass  # qualunque errore dopo la Conferma: esito incerto, sotto
    stato = f"al {nuova.quando:%d/%m/%Y %H:%M}" if nuova else "non verificabile"
    raise CupError(f"Conferma inviata, esito incerto: la prenotazione risulta {stato}. "
                   f"Controlla subito su {LISTA_URL} o al {CALL_CENTER}.")
