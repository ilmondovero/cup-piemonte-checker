"""Impronte delle pagine del portale CUP: com'e' fatta ogni pagina (form, campi, pulsanti, script, segni di
captcha o antibot), senza dati personali. Ogni pagina vista si confronta con quelle viste prima: quando il
portale cambia, l'amministratore riceve un avviso con un rapporto per correggere il bot.

Due fonti:
  - le pagine pubbliche (Elenco prenotazioni e Ricerca, vuote), lette dalla sonda ogni 30 minuti con una
    GET senza dati: non aprono sessioni di prenotazione e non bloccano date;
  - le pagine delle sessioni vere del bot (cup_http.PAGINE): di queste si tiene solo l'impronta, e solo form,
    campi, script e segni di antibot (pulsanti, cookie ed etichette cambiano da un utente all'altro).

Regole contro i falsi allarmi:
  - le prime APPRENDE osservazioni di una pagina insegnano com'e' fatta, senza avvisi;
  - un elemento nuovo si segnala quando compare per la seconda volta;
  - un elemento stabile (visto quasi sempre) si segnala sparito dopo SPARITO osservazioni di fila senza;
  - lo stesso elemento non si segnala di nuovo prima di RIPETI_GIORNI;
  - pagine d'errore e risposte non 200 non sono osservazioni (per quelle c'e' l'avviso di portale giu').

Urgente (avviso subito e prenotazioni automatiche sospese): un segno di captcha o antibot nuovo in una pagina
pubblica, o un elemento che il bot usa (CRITICI) sparito. Quando gli elementi critici tornano, la sospensione
finisce da sola. Il resto va nel riepilogo del giorno.

Nei rapporti non va mai testo libero (messaggi del portale, testo delle eccezioni): solo nomi di componenti,
conteggi e righe del diario riconosciute come strutturali (diario_sicuro). pulisci() serve per i testi che
arrivano da fuori (le segnalazioni delle app)."""
import html
import json
import re
import time
import traceback
from pathlib import Path

APPRENDE = 3  # osservazioni di una pagina prima di confrontarla
SPARITO = 3  # osservazioni di fila senza un elemento stabile prima di dirlo sparito
STABILE = 0.8  # un elemento e' stabile se c'e' in almeno questa quota delle osservazioni da quando e' comparso
STABILE_MIN = 5  # ...e almeno in tante
CAMBIATO_DI_FILA = 2  # sessioni di fila in cui il bot non riconosce lo stesso passo (o manca un critico): urgente
RIPETI_GIORNI = 7  # un cambio minore dello stesso elemento non si ripete prima
URGENTE, INFO, RIPRESA = "urgente", "info", "ripresa"
SESSIONE = ("form", "campi", "script", "antibot")  # categorie confrontate nelle pagine delle sessioni

# elementi che il codice del bot usa davvero (cup_http: L, R e i campi JSF): se spariscono il bot si ferma
CRITICI = {
    "pubblica/elenco": ("form|prescrizioniForm", "campi|javax.faces.ViewState", "campi|ice.window", "campi|ice.view"),
    "pubblica/ricerca": ("form|ePrescriptionSearchForm", "campi|CFInput", "campi|nreInput0",
                         "campi|javax.faces.ViewState", "campi|ice.window", "campi|ice.view"),
}

# segni di captcha e antibot: nella pagina e nei nomi dei cookie
ANTIBOT = (
    ("reCAPTCHA (script)", r"google\.com/recaptcha|gstatic\.com/recaptcha|recaptcha/api|recaptcha/enterprise"),
    ("reCAPTCHA (widget)", r'class="[^"]*g-recaptcha|grecaptcha\.'),
    ("reCAPTCHA (campo token)", r'name="[^"]*g-recaptcha-(?:token|response)'),
    ("hCaptcha", r"hcaptcha\.com|h-captcha"),
    ("Cloudflare Turnstile", r"challenges\.cloudflare\.com|cf-turnstile"),
    ("Cloudflare challenge", r"cf-chl|__cf_chl|cf_clearance|Just a moment\.\.\."),
    ("Friendly Captcha", r"friendlycaptcha|frc-captcha"),
    ("ALTCHA", r"altcha"),
    ("DataDome", r"datadome|captcha-delivery\.com"),
    ("PerimeterX", r"perimeterx|px-captcha|_pxhd"),
    ("Imperva / Incapsula", r"_Incapsula_Resource|incap_ses|visid_incap"),
    ("Akamai Bot Manager", r"_abck|bm_sz|ak_bmsc"),
    ("captcha generico", r'(?:id|name|class)="[^"]*captcha'),
)

# --- seconda difesa: dati personali in un testo -------------------------------------------------
_OMO = "[0-9LMNPQRSTUV]"  # omocodia: le cifre del codice fiscale possono diventare lettere
CF_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{6}%s{2}[A-EHLMPR-T]%s{2}[A-Z]%s{3}[A-Z](?![A-Z0-9])" % (_OMO, _OMO, _OMO), re.I)
NRE_RE = re.compile(r"(?<![A-Z0-9])(?=[0-9A-Z]{0,14}\d)[0-9A-Z]{15}(?![A-Z0-9])", re.I)
MESI = ("gen(?:naio)?|feb(?:braio)?|mar(?:zo)?|apr(?:ile)?|mag(?:gio)?|giu(?:gno)?|lug(?:lio)?|ago(?:sto)?|"
        "set(?:tembre)?|ott(?:obre)?|nov(?:embre)?|dic(?:embre)?")
DATA_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|"
                     r"\b\d{1,2}\s+(?:%s)\b\.?(?:\s+\d{4})?|\b\d{1,2}[:.]\d{2}\b" % MESI, re.I)
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")  # limitata: mai lenta
VERSIONE_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[\w.]{1,20})?\b")
RIGA_MAX = 2000
TEL_RE = re.compile(r"(?<![\w.])\+?\d[\d \-]{7,}\d\b")


def pulisci(testo):
    """Toglie da un testo cio' che puo' essere un dato personale riconoscibile da un'espressione: codici fiscali
    (anche omocodici), ricette, date e orari, email, telefoni. Nomi e luoghi non si riconoscono: per questo nei
    rapporti non va testo libero, e questa e' solo una seconda difesa."""
    righe = []
    for riga in str(testo).split("\n"):  # riga per riga, ognuna al massimo RIGA_MAX: tempi sempre brevi
        riga = riga[:RIGA_MAX]
        versioni = VERSIONE_RE.findall(riga)  # "0.3.1" non e' una data: si rimette dopo
        riga = VERSIONE_RE.sub("\x00", riga)
        riga = CF_RE.sub("[CF]", riga)
        riga = NRE_RE.sub("[NRE]", riga)
        riga = EMAIL_RE.sub("[EMAIL]", riga)
        riga = DATA_RE.sub("[DATA]", riga)
        riga = TEL_RE.sub("[NUMERO]", riga)
        for v in versioni:
            riga = riga.replace("\x00", v, 1)
        righe.append(riga)
    return "\n".join(righe)


_ID = r"'[A-Za-z_?][\w?*#.-]{0,60}'"  # un id di componente tra apici, come lo scrive cup_http._struttura
_LISTA_ID = r"\[(?:%s(?:, %s)*)?\]" % (_ID, _ID)
_SEZIONI = r"\[(?:'(?:Ricerca ricetta|Prestazioni|Appuntamenti Proposti|Appuntamenti Disponibili|Riepilogo)'(?:, )?)*\]"
_STRUTTURA = r"sezioni %s, form %s, pulsanti %s" % (_SEZIONI, _LISTA_ID, _LISTA_ID)
_ERRORE_RETE = r"(?:HTTP \d{3}|[A-Z][a-z]\w*), (?:portale sovraccarico|risposta inattesa)(?: dopo \d+ s)?(?: \(limite della verifica\))?"
_MODO = r"\((?:sposta|nuova|)\)"
DIARIO_SICURO = [re.compile(x) for x in (
    r"^[a-z]+: " + _ERRORE_RETE + r"$",
    r"^elenco: righe \d+, prenotate \d+, con data \d+, date prenotate diverse \d+, descrizioni vuote \d+$",
    r"^ricerca: messaggi d'errore \d+$",
    r"^dopo la ricerca: " + _STRUTTURA + r", carrello \d+$",
    r"^passo: " + _STRUTTURA + r", carrello \d+, caselle spuntate \d+/\d+, avanti [A-Za-z_?][\w?*#.-]{0,60}$",
    r"^avanti: redirect (?:si|no), messaggi d'errore \d+, " + _STRUTTURA + r"$",
    r"^disdetta: dialogo non riconosciuto, pulsanti Si' \d+, componenti " + _LISTA_ID + r"$",
    r"^estendi area: timeout al passo \d+ di \d+, tengo le date lette$",
    r"^appuntamenti " + _MODO + r": carrello \d+, pannelli proposta \d+, selettori proposta \d+, descrizioni \d+, "
    r"proposta (?:si|no), date \d+, con Seleziona \d+, date uguali \d+, altre disponibilita' (?:no )?pulsante/"
    r"(?:[A-Za-z_][\w-]{0,40}|nessun selettore)(?:, date per prestazione \[\d+(?:, \d+)*\] nessuna \d+)?$",
    r"^seleziona: messaggi d'errore \d+, rifiutata (?:si|no)$",
    r"^riepilogo " + _MODO + r": date \d+, date diverse \d+, 'alle ore' \d+, prestazioni dichiarate (?:\d+|\?), "
    r"nomi trovati \d+/\d+$",
    r"^presa visione: (?:nessun pulsante|registrata, ViewState (?:nuovo|invariato))$",
    r"^fase [a-z]+: [A-Z]\w*$",
    r"^tempi: [a-z]+ \d+\.\d+s(?:, [a-z]+ \d+\.\d+s)*$",
    r"^note: \d+ righe$",
    r"^(?:conferma|disdetta): " + _ERRORE_RETE + r", verifico con una sessione nuova$",
    r"^verifica: (?:pagina non letta|nessun record, ricontrollo|errore imprevisto [A-Z]\w* \(riga [\d?]+\))$",
)]


def diario_sicuro(righe):
    """Le righe del diario di una sessione che si possono mandare: solo quelle riconosciute come strutturali
    (conteggi, nomi di componenti, codici HTTP). Le altre diventano "(riga omessa)"; della risposta alla Conferma
    resta solo se diceva "conclusa con successo"."""
    out = []
    for r in righe:
        r = str(r)
        if any(x.match(r) for x in DIARIO_SICURO) and not re.search(r"(?<![A-Za-z])(?!HTTP)[A-Z]{4,}", r):
            out.append(r)  # le parole tutte maiuscole (tranne HTTP) possono essere sedi: la riga non passa
        elif r.startswith("conferma: risposta"):
            out.append("conferma: risposta (testo omesso), 'conclusa con successo': " +
                       ("si" if "conclusa con successo" in r.lower() else "no"))
        elif re.match(r"^verifica: [A-Z]\w*: ", r):
            out.append("verifica: " + r.split(": ")[1] + " (messaggio omesso)")
        else:
            out.append((re.match(r"^[a-z ]{1,20}(?: \(\w*\))?(?=:)", r) or re.match("", r)).group(0) + ": (riga omessa)")
    return out


# --- impronta di una pagina -------------------------------------------------------------------
def nome(s, rigido=False):
    """Nome di un componente senza la parte che cambia a ogni visita: ultimo pezzo dopo ':', prefissi casuali
    ("vk1qtdzr5r-retrieve-update" -> "v*-retrieve-update"), id automatici di JSF e ICEfaces (j_idt12, _t34),
    numeri lunghi. "?" se non ha l'aspetto di un nome di componente (mai un dato finito in un id). rigido (pagine
    delle sessioni): anche niente parole tutte maiuscole, che possono essere il nome di una sede."""
    s = html.unescape(s).rsplit(":", 1)[-1]
    if CF_RE.search(s) or NRE_RE.search(s):  # prima di accorciare i numeri, che li renderebbero irriconoscibili
        return "?"
    s = re.sub(r"^v(?=[a-z0-9]*\d)[a-z0-9]{6,12}-", "v*-", s)
    s = re.sub(r"\bj_id(t?)\d+", r"j_id\1#", s)
    s = re.sub(r"^_t\d+", "_t#", s)
    s = re.sub(r"\d{3,}", "#", s)
    if not re.fullmatch(r"[A-Za-z_*][\w.*#-]{0,60}", s) or CF_RE.search(s) or NRE_RE.search(s):
        return "?"
    if rigido and re.search(r"[A-Z]{4,}", s):
        return "?"
    if re.search(r"\d{1,2}[./-]\d{1,2}", s):  # giorno e mese di una data
        return "?"
    return s


def _etichetta(t):
    t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t)).split())
    if not t or len(t) > 40 or re.search(r"\d|@", t) or CF_RE.search(t):
        return None  # date, orari, numeri o testi lunghi: possono essere dati, non etichette
    return t


def _script(src):
    """Nome di uno script senza parametri (versioni, cache): "/o/js/everything.jsp?t=1" -> "everything.jsp";
    un indirizzo esterno tiene il dominio, che dice chi lo fornisce."""
    m = re.match(r"(?:https?:)?//([^/]+)", src)
    pezzi = [x for x in re.sub(r"[?#;].*", "", src).split("/") if x]  # ;jsessionid=... e' la sessione dell'utente
    return (m.group(1) + ":" if m else "") + nome(pezzi[-1] if pezzi else "?", rigido=True)


def impronta(pagina, cookie=(), server="", pubblica=True):
    """{categoria: [elementi]} di una pagina HTML o di una risposta parziale JSF. Solo nomi e strutture.
    pubblica=False (pagina di una sessione): solo le categorie SESSIONE, con i nomi controllati in modo rigido."""
    p = re.sub(r"<!\[CDATA\[|\]\]>", "", pagina)
    r = not pubblica
    imp = {
        "form": sorted({nome(f, r) for f in re.findall(r'<form\b[^>]*\bid="([^"]+)"', p)}),
        "campi": sorted({nome(n, r) for n in re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="([^"]+)"', p)}),
        "pulsanti": sorted({nome(b) for b in re.findall(r'\bid="([^"]*(?:Button|Navigate|button|btn|Btn)[^"]*)"', p)}),
        "etichette": sorted({e for e in (_etichetta(t) for t in re.findall(r"<button\b[^>]*>(.*?)</button>", p, re.S))
                             if e} | {e for e in (_etichetta(v) for v in re.findall(
                                 r'<input\b[^>]*type="(?:submit|button)"[^>]*value="([^"]*)"', p)) if e}),
        "script": sorted({_script(s) for s in re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', p)}),
        "antibot": sorted({n for n, rx in ANTIBOT if re.search(rx, p, re.I) or any(
            re.search(rx, c, re.I) for c in cookie)}),
        "cookie": sorted({re.sub(r"\d+", "#", c) for c in cookie if re.fullmatch(r"[\w.-]{1,40}", c)}),
    }
    chiavi = re.findall(r'data-sitekey="([\w-]+)"|recaptcha/api\.js\?[^"]*render=([\w-]+)', p)
    if chiavi:  # la chiave di reCAPTCHA e' pubblica: se cambia, il captcha e' stato rifatto
        imp["antibot"] = sorted(set(imp["antibot"]) | {"chiave reCAPTCHA " + (a or b)[:12] for a, b in chiavi})
    if server:
        imp["server"] = [server[:80]]
    if not pubblica:
        imp = {k: v for k, v in imp.items() if k in SESSIONE}
    return {k: v for k, v in imp.items() if v}


def scheletro(pagina, righe_max=150, pubblica=True):
    """Com'e' fatta la pagina, form per form: campi (tipo e nome), pulsanti (id e, nelle pagine pubbliche,
    etichetta), menu a tendina (nome e quante voci). Mai i valori. Serve a chi corregge il bot."""
    p = re.sub(r"<!\[CDATA\[|\]\]>", "", pagina)
    r = not pubblica
    out = []
    forms = list(re.finditer(r'<form\b[^>]*\bid="([^"]+)"(.*?)</form>', p, re.S)) or [None]
    for f in forms:
        corpo = f.group(2) if f else p
        out.append(f"form {nome(f.group(1), r) if f else '(nessun form)'}")
        for tag in re.findall(r"<input\b[^>]*>", corpo):
            n = re.search(r'\bname="([^"]+)"', tag)
            t = re.search(r'\btype="(\w+)"', tag)
            if n:
                out.append(f"  campo {(t.group(1) if t else 'text').lower()} {nome(n.group(1), r)}")
        for n, corpo_sel in re.findall(r'<select\b[^>]*\bname="([^"]+)"[^>]*>(.*?)</select>', corpo, re.S):
            out.append(f"  menu {nome(n, r)} ({len(re.findall(r'<option', corpo_sel))} voci)")
        for attr, testo in re.findall(r"<button\b([^>]*)>(.*?)</button>", corpo, re.S):
            i = re.search(r'\bid="([^"]+)"', attr)
            out.append(f"  pulsante {nome(i.group(1), r) if i else '(senza id)'}" +
                       (f" \"{_etichetta(testo) or '?'}\"" if pubblica else ""))
        for b in sorted({nome(b, r) for b in re.findall(r'\bid="([^"]*(?:Button|Navigate)[^"]*)"', corpo)}):
            out.append(f"  comando {b}")
    # le righe uguali di seguito (es. una per data) diventano una con il conto
    compatto = []
    for riga in out:
        if compatto and compatto[-1][0] == riga:
            compatto[-1][1] += 1
        else:
            compatto.append([riga, 1])
    out = [f"{x} (x{n})" if n > 1 else x for x, n in compatto]
    if len(out) > righe_max:
        out = out[:righe_max] + [f"... (altre {len(out) - righe_max} righe)"]
    return "\n".join(out)


# --- confronto con le osservazioni di prima ---------------------------------------------------
def confronta(stato, imp, chiave, ora=None):
    """Aggiorna lo stato di una pagina con una nuova osservazione e ritorna i cambi da segnalare:
    [(livello, testo)]. stato: {"n": osservazioni, "el": {"cat|elem": [viste, prima, ultima, stato, segnalato]}}
    (prima e ultima: numero dell'osservazione; segnalato: epoch dell'ultimo cambio minore detto)."""
    ora = ora or time.time()
    stato.setdefault("n", 0)
    el = stato.setdefault("el", {})
    stato["n"] += 1
    n = stato["n"]
    presenti = {f"{cat}|{e}" for cat, v in imp.items() for e in v}
    cambi = []
    critici = set(CRITICI.get(chiave, ()))
    pubblica = chiave.startswith("pubblica/")

    def minore(v, testo):  # un cambio minore dello stesso elemento al massimo una volta ogni RIPETI_GIORNI
        if ora - (v[4] if len(v) > 4 else 0) >= RIPETI_GIORNI * 86400:
            cambi.append((INFO, testo))
            v[4:] = [ora]

    for k in sorted(presenti):
        v = el.setdefault(k, [0, n, n, "", 0])
        assente = n - v[2] - 1  # osservazioni di fila senza, prima di questa
        v[0] += 1
        v[2] = n
        cat, e = k.split("|", 1)
        if v[3] == "sparito":
            v[3] = ""
            if k in critici:
                cambi.append((RIPRESA, f"ricomparso {_cat(cat)} {e}, che il bot usa"))
            else:
                minore(v, f"ricomparso {_cat(cat)} {e} (mancava da {assente} osservazioni)")
        elif v[0] == 2 and v[1] > APPRENDE and v[3] != "nuovo":
            v[3] = "nuovo"
            if cat == "antibot" and pubblica:  # nelle sessioni conta solo insieme a un errore (cup_http.Antibot)
                cambi.append((URGENTE, f"comparso {_cat(cat)} {e}"))
            else:
                minore(v, f"comparso {_cat(cat)} {e}")
    for k, v in sorted(el.items()):
        if k in presenti or n <= APPRENDE or v[3] == "sparito":
            continue
        mancanti = n - v[2]
        cat, e = k.split("|", 1)
        if k in critici and mancanti >= CAMBIATO_DI_FILA:
            v[3] = "sparito"
            cambi.append((URGENTE, f"sparito {_cat(cat)} {e}, che il bot usa"))
            continue
        quota = v[0] / max(1, v[2] - v[1] + 1)
        if mancanti >= SPARITO and v[0] >= STABILE_MIN and quota >= STABILE:
            v[3] = "sparito"
            minore(v, f"sparito {_cat(cat)} {e}")
    if n == APPRENDE + 1:  # i critici mai visti: il bot li cerca ma la pagina non li ha mai avuti
        for k in sorted(critici - set(el)):
            cat, e = k.split("|", 1)
            cambi.append((INFO, f"il bot usa {_cat(cat)} {e}, ma non l'ho mai visto in questa pagina"))
    return cambi


def _cat(cat):
    return {"form": "il form", "campi": "il campo", "pulsanti": "il pulsante", "etichette": "il pulsante con scritto",
            "script": "lo script", "antibot": "il segno di antibot", "cookie": "il cookie",
            "server": "il server"}.get(cat, cat)


def osserva(store, chiave, pagina, cookie=(), server="", ora=None):
    """Una pagina vista: aggiorna lo stato salvato e registra i cambi. Un cambio urgente sospende le prenotazioni
    automatiche; quando gli elementi critici di una pagina tornano tutti, la sospensione che avevano causato finisce.
    Ritorna i cambi [(livello, testo)]."""
    ora = ora or time.time()
    pubblica = chiave.startswith("pubblica/")
    imp = impronta(pagina, cookie, server, pubblica=pubblica)
    if not imp.get("form") and not imp.get("campi") and not imp.get("antibot"):
        return []  # una risposta senza struttura (solo testo, un frammento vuoto): niente da confrontare
    stato = store.impronta(chiave) or {}
    cambi = confronta(stato, imp, chiave, ora)
    stato["ultima"] = imp
    stato["ts"] = ora
    if cambi:
        stato["scheletro"] = scheletro(pagina, pubblica=pubblica)
    store.salva_impronta(chiave, stato)
    for livello, testo in cambi:
        store.cambio_portale(ora, chiave, livello, testo, rapporto_cambio(chiave, livello, testo, stato))
        if livello == URGENTE:
            # un elemento sparito torna da solo; un captcha comparso aspetta l'amministratore (per elemento)
            sospendi(store, ora, chiave if testo.startswith("sparito") else f"{chiave} {testo}", testo,
                     riprende=testo.startswith("sparito"))
    critici = CRITICI.get(chiave, ())
    if critici and all(stato["el"].get(k, [0, 0, 0, ""])[3] != "sparito" for k in critici):
        riprendi(store, chiave, ora, "gli elementi che il bot usa sono tornati")
    return cambi


def cause(store):
    """{origine: [epoch, testo, riprende]}: perche' le prenotazioni automatiche sono sospese."""
    try:
        return json.loads(store.valore("auto_sospeso_cause") or "{}")
    except ValueError:
        return {}


def _salva_cause(store, c):
    store.imposta("auto_sospeso_cause", json.dumps(c) if c else None)
    if not c:
        store.imposta("auto_sospeso", None)
        return
    ts, testo, _ = next(iter(c.values()))
    origine = next(iter(c))
    store.imposta("auto_sospeso", f"{ts:.0f} {origine}: {testo}" + (f" (e altre {len(c) - 1} cause)" if len(c) > 1 else ""))


def sospendi(store, ora, origine, testo, riprende=True):
    """Sospende le prenotazioni automatiche per una causa in piu'. riprende: la causa puo' finire da sola quando
    sparisce (riprendi con la stessa origine); un captcha nuovo invece aspetta l'amministratore. Le prenotazioni
    ripartono solo quando non resta nessuna causa."""
    c = cause(store)
    if origine not in c or (c[origine][2] and not riprende):
        c[origine] = [ora, testo, riprende]
        _salva_cause(store, c)


def riprendi(store, origine, ora, perche):
    """La causa `origine` e' finita (se poteva finire da sola). Se era l'ultima, le prenotazioni automatiche
    ripartono e l'amministratore riceve una segnalazione."""
    c = cause(store)
    if origine not in c or not c[origine][2]:
        return
    del c[origine]
    _salva_cause(store, c)
    if not c:
        store.cambio_portale(ora, origine, RIPRESA, f"prenotazioni automatiche riattivate: {perche}", "")


def riattiva_tutto(store):
    _salva_cause(store, {})


def rapporto_cambio(chiave, livello, testo, stato):
    righe = [f"Cambio del portale CUP ({livello})", f"Pagina: {chiave}", f"Cambio: {testo}",
             f"Osservazioni di questa pagina: {stato.get('n')}", "",
             "== Impronta attuale ==", json.dumps(stato.get("ultima", {}), indent=1, ensure_ascii=False), "",
             "== Schema della pagina (solo nomi, mai valori) ==", stato.get("scheletro", ""), "",
             "== Elementi visti in passato ([viste, prima osservazione, ultima, stato, segnalato]) ==",
             "\n".join(f"{k}: {v}" for k, v in sorted(stato.get("el", {}).items()))]
    return "\n".join(righe)


# --- errori ------------------------------------------------------------------------------------
def stack(e):
    """Lo stack di un'eccezione: file, riga, funzione e codice di ogni chiamata. Mai il messaggio."""
    if not e.__traceback__:
        return "(nessuno stack)"
    return "".join(traceback.format_list(traceback.extract_tb(e.__traceback__))).rstrip()


def rapporto_errore(e, contesto, diario=(), richieste=(), versione=""):
    """Rapporto di un errore imprevisto: tipo (mai il messaggio, che puo' contenere dati), stack, diario della
    sessione ridotto alle righe strutturali e tempi delle richieste."""
    righe = [f"Errore imprevisto: {type(e).__name__}", f"Dove: {contesto}", f"Versione del bot: {versione or '?'}",
             "", "== Stack ==", stack(e), ""]
    if diario:
        righe += ["== Diario della sessione (solo righe strutturali) ==", *diario_sicuro(diario), ""]
    if richieste:
        righe += ["== Richieste al portale (passo, secondi, esito, codice) ==", *righe_richieste(richieste), ""]
    return "\n".join(righe)


def righe_richieste(richieste):
    return [f"{time.strftime('%H:%M:%S', time.localtime(r[0]))} {r[1]} {r[2]} {r[3]} {r[4]}" for r in richieste]


def versione_bot(cartella):
    """Commit del repo da cui gira il bot (letto da .git, senza lanciare git)."""
    for d in [Path(cartella), *Path(cartella).parents]:
        g = d / ".git"
        if g.is_dir():
            try:
                head = (g / "HEAD").read_text().strip()
                if not head.startswith("ref: "):
                    return head[:10]
                ref = head[5:]
                if (g / ref).exists():
                    return (g / ref).read_text().strip()[:10]
                for riga in (g / "packed-refs").read_text().splitlines():
                    if riga.endswith(" " + ref):
                        return riga[:10]
            except OSError:
                return ""
            return ""
    return ""
