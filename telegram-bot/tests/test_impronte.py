"""Impronte del portale: confronto delle pagine, avvisi senza falsi allarmi e senza dati personali."""
import pytest
from cryptography.fernet import Fernet

import impronte as im
from store import Store

CF, NRE = "RSSMRA80A01L219X", "010A00000000001"
R = "_ricettaelettronica_WAR_cupprenotazione_:ePrescriptionSearchForm"


def ricerca(prefisso="vk1qtdzr5r", captcha=False, cf_campo=True, extra=""):
    """Una pagina Ricerca come quella del portale: campi JSF, token reCAPTCHA vuoto, id con prefisso casuale."""
    return ('<html><head><script src="/o/js/everything.jsp?t=1700000"></script>'
            '<script src="/combo/?browserId=other&t=1700001"></script>'
            + ('<script src="https://www.google.com/recaptcha/api.js?render=6LcABCDEF123"></script>' if captcha else "")
            + f'</head><body><form id="{R}" action="/x">'
            + (f'<input type="text" name="{R}:CFInput" value="{CF}" />' if cf_campo else "")
            + f'<input type="text" name="{R}:nreInput0" value="{NRE}" />'
            f'<input type="hidden" name="g-recaptcha-token" value="" />'
            f'<input type="hidden" name="javax.faces.ViewState" value="-123:456" />'
            f'<input type="hidden" name="ice.window" value="abc" /><input type="hidden" name="ice.view" value="v1" />'
            f'<button id="{R}:nreButton">Prosegui</button>'
            f'<input id="{R}:epPrestazioniForwardNavigate" type="submit" style="display:none;" />'
            f'<div id="{prefisso}-retrieve-update"></div>{extra}'
            f'</form></body></html>')


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())


def osserva(store, pagina, volte=1, chiave="pubblica/ricerca"):
    cambi = []
    for _ in range(volte):
        cambi += im.osserva(store, chiave, pagina)
    return cambi


def test_impronta_solo_nomi_mai_valori():
    imp = im.impronta(ricerca())
    testo = str(imp)
    assert CF not in testo and NRE not in testo and "-123:456" not in testo
    assert "ePrescriptionSearchForm" in imp["form"] and "CFInput" in imp["campi"]
    assert "javax.faces.ViewState" in imp["campi"] and "epPrestazioniForwardNavigate" in imp["pulsanti"]
    assert "Prosegui" in imp["etichette"] and "reCAPTCHA (campo token)" in imp["antibot"]
    assert imp["script"] == ["combo", "everything.jsp"]
    assert im._script("https://www.google.com/recaptcha/api.js?render=x") == "www.google.com:api.js"
    # un id con un dato dentro non entra mai
    assert im.nome(f"form:{CF}") == "?" and im.nome(f"x:{NRE}") == "?"


def test_parti_che_cambiano_a_ogni_visita_non_contano(store):
    osserva(store, ricerca(), 4)
    assert osserva(store, ricerca(prefisso="v5aa21bssu")) == []
    assert osserva(store, ricerca(prefisso="viweu7d54"), 5) == []
    assert store.segnalazioni() == []


def test_le_prime_osservazioni_insegnano_senza_avvisi(store):
    assert osserva(store, ricerca()) == []
    assert osserva(store, ricerca(captcha=True), 2) == []  # ancora in apprendimento
    assert store.segnalazioni() == []


def test_captcha_nuovo_urgente_alla_seconda_volta_e_sospende_l_automatico(store):
    osserva(store, ricerca(), 4)
    assert osserva(store, ricerca(captcha=True)) == []  # una volta sola potrebbe essere un caso
    cambi = osserva(store, ricerca(captcha=True))
    assert (im.URGENTE, "comparso il segno di antibot reCAPTCHA (script)") in cambi
    assert any(t.startswith("comparso il segno di antibot chiave reCAPTCHA 6LcABCDEF123") for _, t in cambi)
    assert store.valore("auto_sospeso") and "reCAPTCHA" in store.valore("auto_sospeso")
    seg = store.segnalazioni(da_inviare=True, livello=im.URGENTE)
    assert seg and "== Schema della pagina" in seg[0]["rapporto"]
    assert any(s["testo"] == "comparso lo script www.google.com:api.js" for s in store.segnalazioni(livello=im.INFO))
    assert seg and "== Schema della pagina" in seg[0]["rapporto"]
    assert CF not in seg[0]["rapporto"] and NRE not in seg[0]["rapporto"]
    assert osserva(store, ricerca(captcha=True)) == []  # gia' segnalato: non si ripete


def test_campo_che_il_bot_usa_sparito_urgente(store):
    osserva(store, ricerca(), 5)
    assert osserva(store, ricerca(cf_campo=False)) == []
    cambi = osserva(store, ricerca(cf_campo=False))
    assert cambi == [(im.URGENTE, "sparito il campo CFInput, che il bot usa")]
    assert store.valore("auto_sospeso")
    osserva(store, ricerca(cf_campo=False), 3)
    assert osserva(store, ricerca()) == [(im.RIPRESA, "ricomparso il campo CFInput, che il bot usa")]
    assert not store.valore("auto_sospeso")  # tornato quello che mancava: la sospensione finisce da sola
    assert store.segnalazioni(livello=im.RIPRESA)


def test_cambi_minori_vanno_nel_riepilogo(store):
    altro = '<button id="altroButton">Stampa</button>'
    osserva(store, ricerca(extra=altro), 6)
    assert osserva(store, ricerca(), 2) == []
    cambi = osserva(store, ricerca())
    assert (im.INFO, "sparito il pulsante altroButton") in cambi
    assert (im.INFO, "sparito il pulsante con scritto Stampa") in cambi
    assert not store.valore("auto_sospeso")
    nuovo = '<input type="text" name="form:nuovoCampo" />'
    osserva(store, ricerca(extra=nuovo))
    assert (im.INFO, "comparso il campo nuovoCampo") in osserva(store, ricerca(extra=nuovo))


def test_nelle_sessioni_niente_etichette():
    # in una sessione un pulsante puo' portare il nome di una sede
    pagina = '<form id="f"><button id="selectButton">POLIAMBULATORIO NORD</button></form>'
    assert "etichette" not in im.impronta(pagina, pubblica=False)
    assert "POLIAMBULATORIO" not in im.scheletro(pagina, pubblica=False)
    # un id con il nome di una sede dentro non passa
    assert im.nome("form:slot_MAURIZIANO_x", rigido=True) == "?"


def test_pagina_senza_struttura_non_e_un_osservazione(store):
    assert im.osserva(store, "pubblica/ricerca", "<html><body>Servizio non disponibile</body></html>") == []
    assert store.impronta("pubblica/ricerca") is None


def test_pulisci():
    t = im.pulisci(f"errore per {CF} e {NRE} il 16/02/2027 alle 09:00, 2027-02-16T09:00, "
                   f"martedi 14 settembre 2027, mario@example.com, +39 333 1234567")
    for dato in (CF, NRE, "16/02/2027", "2027-02-16", "14 settembre", "mario@", "333 1234567"):
        assert dato not in t
    assert "KeyError" in im.pulisci("KeyError: 'cf'")


def test_rapporto_errore_con_stack():
    try:
        {}["x"]
    except KeyError as e:
        e.args = (f"per {CF} all'OSPEDALE MAURIZIANO",)  # il messaggio di un'eccezione non va mai nel rapporto
        r = im.rapporto_errore(e, "prova", [f"ricerca: {CF}", "ricerca: messaggi d'errore 1",
                                            "conferma: risposta Prenotazione conclusa con successo per MARIO ROSSI"],
                               [(0, "ricerca", 1.0, "ok", 200)], "abc123")
    assert "KeyError" in r and "== Stack ==" in r and "abc123" in r
    assert CF not in r and "MAURIZIANO" not in r and "MARIO" not in r
    assert "ricerca: (riga omessa)" in r and "ricerca: messaggi d'errore 1" in r
    assert "conferma: risposta (testo omesso), 'conclusa con successo': si" in r


def test_diario_sicuro_righe_vere():
    righe = ["elenco: righe 2, prenotate 1, con data 1, date prenotate diverse 1, descrizioni vuote 0",
             "elenco: ReadTimeout, portale sovraccarico dopo 60 s", "elenco: HTTP 503, portale sovraccarico",
             "note: 1 righe", "presa visione: registrata, ViewState nuovo",
             "tempi: elenco 0.9s, riepilogo 0.5s, conferma 12.9s, verifica 11.3s, fine 0.0s",
             "verifica: CupError: Lo slot 16/02/2027 09:00 OSPEDALE A non e' prima",
             "conferma: Lo slot del 16/02 e' sparito, verifico con una sessione nuova"]
    out = im.diario_sicuro(righe)
    assert out[:6] == righe[:6]
    assert out[6] == "verifica: CupError (messaggio omesso)" and out[7] == "conferma: (riga omessa)"


def test_pulisci_forme_meno_comuni():
    for dato in ("RSSMRA85T1LA562S", "CF_RSSMRA85T10A562S", "nre_010a01234567890", "3/11", "3 nov 2026", "ore 10:30"):
        assert dato.split("_")[-1].split()[-1] not in im.pulisci(dato), dato
