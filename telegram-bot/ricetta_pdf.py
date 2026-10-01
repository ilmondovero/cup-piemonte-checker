"""Legge il promemoria PDF di una ricetta elettronica (quello con testo, non una scansione) e ne ricava i
dati che servono al bot. Il PDF si legge in memoria e non si conserva: i campi vanno controllati dall'utente."""
import io
import re
import threading

MAX_BYTE = 3 * 1024 * 1024
MAX_PAGINE = 3
MAX_TESTO = 20000  # il promemoria ne ha meno di 1500: oltre, il testo si taglia (regole lente su testi enormi)
LETTERE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DISPARI = dict(zip("0123456789", [1, 0, 5, 7, 9, 13, 15, 17, 19, 21]))
_DISPARI.update(zip(LETTERE, [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 10, 16, 22, 25, 24, 23]))
_PARI = {**{c: i for i, c in enumerate("0123456789")}, **{c: i for i, c in enumerate(LETTERE)}}
CF = r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]"
_UNA_ALLA_VOLTA = threading.Semaphore(1)  # il parsing e' Python puro e tiene il GIL: una lettura per volta


class PdfNonLeggibile(Exception):
    """Il file non e' un PDF, e' troppo grande, non ha testo (una scansione) o un'altra lettura e' in corso."""


def cf_valido(cf):
    """Forma e carattere di controllo del codice fiscale."""
    if not re.fullmatch(CF, cf):
        return False
    return LETTERE[sum(_DISPARI[c] if i % 2 == 0 else _PARI[c] for i, c in enumerate(cf[:15])) % 26] == cf[15]


def testo_di(dati):
    import pdfplumber
    if len(dati) > MAX_BYTE or not dati.startswith(b"%PDF"):
        raise PdfNonLeggibile("Il file non è un PDF (o è troppo grande: al massimo 3 MB).")
    if not _UNA_ALLA_VOLTA.acquire(blocking=False):
        raise PdfNonLeggibile("Sto già leggendo un altro PDF: riprova tra qualche secondo.")
    try:
        # pages=: solo le prime pagine vengono costruite (pdf.pages le costruirebbe tutte)
        with pdfplumber.open(io.BytesIO(dati), pages=list(range(1, MAX_PAGINE + 1))) as pdf:
            testo = "\n".join((p.extract_text() or "") for p in pdf.pages)[:MAX_TESTO]
    except Exception:
        raise PdfNonLeggibile("Non riesco ad aprire questo PDF.") from None
    finally:
        _UNA_ALLA_VOLTA.release()
    if len(testo.strip()) < 50:
        raise PdfNonLeggibile("Il PDF non contiene testo (forse è una scansione): per ora inserisci i dati a mano.")
    return testo


def _prima(regola, testo):
    """Il primo gruppo della regola, spazi ripuliti. Le regole non usano il punto che attraversa le righe e
    hanno lunghezze massime: su testi strani restano veloci."""
    m = re.search(regola, testo)
    return " ".join(m.group(1).split()) if m else ""


def analizza(testo):
    """Il testo del promemoria -> {"cf", "nre", "prestazione", "priorita", "data", "quesito", "paziente",
    "problemi": [...]}. Un campo che non si legge o non torna e' vuoto e il problema e' elencato."""
    testo = testo[:MAX_TESTO]
    problemi = []
    cf = next((c for c in re.findall(r"\*(%s)\*" % CF, testo) if cf_valido(c)), "")
    if not cf:  # senza asterischi: il primo codice fiscale valido che non sia quello del medico
        senza_medico = re.sub(r"MEDICO:\s*" + CF, "", testo)
        cf = next((c for c in re.findall(CF, senza_medico) if cf_valido(c)), "")
    if not cf:
        problemi.append("codice fiscale")
    # il NRE e' stampato a pezzi: "*010A3*" (regione e ASL) e "*1234567890*" (10 cifre)
    m = re.search(r"\*([0-9A-Z]{5})\*[^*]{0,80}?\*(\d{10})\*", testo)
    nre = m.group(1) + m.group(2) if m else ""
    if not nre:
        m = re.search(r"\b(0\d{2}[A-Z0-9]\d{11})\b", testo)
        nre = m.group(1) if m else ""
    if not re.fullmatch(r"[0-9A-Z]{15}", nre):
        nre = ""
        problemi.append("numero ricetta (NRE)")
    # il codice della prestazione ("88.78 (88.78) - ...") e' su una riga sua o dopo "PRESCRIZIONE", secondo il layout
    prestazione = _prima(r"\b\d{1,3}\.\d{1,3}\s*\([^)\n]{0,20}\)\s*-\s*([^\n]{1,200}?)\s*(?:QTA'|QUESITO|\n|$)", testo)
    if not prestazione:
        problemi.append("prestazione")
    return {"cf": cf, "nre": nre, "prestazione": prestazione,
            "priorita": _prima(r"PRIORITA'[^:\n]{0,40}:\s*(\w{1,20})", testo),
            "data": _prima(r"\bDATA:\s*(\d{2}-\d{2}-\d{4})", testo),
            "quesito": _prima(r"QUESITO DIAGNOSTICO:[ \t]*([^\n]{1,200}?)\s+N\. CONFEZIONI", testo),
            "paziente": _prima(r"ASSISTITO:[ \t]*([^\n]{1,120}?)\s+INDIRIZZO:", testo), "problemi": problemi}


def leggi(dati):
    return analizza(testo_di(dati))
