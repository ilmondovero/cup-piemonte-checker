"""Prenotazione nuova via HTTP: passi Ricerca e Prestazioni con pagine sintetiche (nessuna rete).
Le pagine hanno la struttura di quelle registrate dal portale: form JSF con i campi ICEfaces, carrello
"Prestazioni Selezionate", pagina Appuntamenti come quella di "Sposta"."""
import sys
from datetime import datetime
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cup_http as c  # noqa: E402
from test_bot import blocco  # noqa: E402

CF, NRE = "GLLMRC75C12D969K", "010A00000000003"
P = "_ricettaelettronica_WAR_cupprenotazione_:"
PREST = P + "prestazioni-form-main"


def form(form_id, corpo=""):
    return (f'<form id="{form_id}" method="post"><input type="hidden" name="{form_id}" value="{form_id}" />'
            f'<input type="hidden" name="javax.faces.encodedURL" value="{c.CUP}/ricetta?azione" />'
            f'<input type="hidden" name="ice.window" value="w1" /><input type="hidden" name="ice.view" value="v1" />'
            f'{corpo}<input type="hidden" name="javax.faces.ViewState" value="vs1" /></form>')


def carrello(*nomi):
    voci = "".join(f'<span class="media-title" style="display:block;"><span id="x{i}">{n}</span> </span>'
                   f'<span class="media-title"> (<span>PRENOTABILE</span>)</span>' for i, n in enumerate(nomi))
    return form(P + "cartForm", f'<div id="prestazioni_selezionate">Prestazioni Selezionate: {len(nomi)}</div>{voci}')


RICERCA = "<h1>Ricerca Prestazioni Appuntamenti Riepilogo e conferma</h1>" + form(
    c.R, f'<input type="text" name="{c.R}:CFInput" value="" /><input type="text" name="{c.R}:nreInput0" value="" />'
         f'<div id="{c.R}:nreButton">Prosegui</div>'
         f'<input id="{c.R}:epPrestazioniForwardNavigate" type="submit" style="display:none;" />')
PRESTAZIONI = ("<h1>Ricerca Prestazioni Appuntamenti Riepilogo e conferma</h1>" + carrello("ECOGRAFIA ADDOME COMPLETO") +
               form(PREST, f'<input type="checkbox" name="{PREST}:scelta0" checked="checked" value="true" />'
                           f'<div id="{PREST}:prestazioni-nextButton-main">Avanti</div>'))
APPUNTAMENTI = ("Appuntamenti Proposti " + carrello("ECOGRAFIA ADDOME COMPLETO") +
                form(c.A, f"<div>'{c.A}:x:0:app_selector'</div>" +
                     blocco("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1")))


def xml(errore=None):
    msg = (f'<div class="messagifyMsg alert-danger"><span>***nre***: {errore}</span><br /></div>' if errore else "")
    return f'<partial-response><changes><update id="m"><![CDATA[{msg}]]></update></changes></partial-response>'


class Risposta:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class PortaleFinto:
    """Sessione requests finta: GET della pagina della ricetta secondo il passo, POST registrati."""

    def __init__(self, pagine, risposte):
        self.pagine, self.risposte, self.inviati = list(pagine), list(risposte), []
        self.headers = {}
        self.hooks = {"response": []}

    def get(self, url, timeout=None):
        return Risposta(self.pagine.pop(0))

    def post(self, url, data=None, timeout=None, headers=None):
        self.inviati.append(data)
        return Risposta(self.risposte.pop(0))


def con_portale(monkeypatch, pagine, risposte):
    finto = PortaleFinto(pagine, risposte)
    monkeypatch.setattr(c.requests, "Session", lambda: finto)
    return finto


def test_ricetta_gia_prenotata(monkeypatch):
    con_portale(monkeypatch, [RICERCA], [xml("Numero ricetta elettronica già presente")])
    with pytest.raises(c.GiaPrenotata):
        c.nuova(CF, NRE)


def test_ricetta_rifiutata_senza_prefisso_del_campo(monkeypatch):
    con_portale(monkeypatch, [RICERCA], [xml("Numero ricetta elettronica non valido")])
    with pytest.raises(c.NonTrovata) as e:
        c.nuova(CF, NRE)
    assert str(e.value) == "Numero ricetta elettronica non valido"


def test_ricerca_come_il_browser_e_prestazione_letta(monkeypatch):
    finto = con_portale(monkeypatch, [RICERCA, PRESTAZIONI], [xml()])
    assert c.nuova(CF, NRE) == "ECOGRAFIA ADDOME COMPLETO"
    [ricerca] = finto.inviati
    assert ricerca[c.R + ":CFInput"] == CF and ricerca[c.R + ":nreInput0"] == NRE
    assert ricerca["javax.faces.source"] == c.R + ":epPrestazioniForwardNavigate"
    assert ricerca["javax.faces.behavior.event"] == "action" and ricerca["g-recaptcha-token"] == ""


def test_dal_passo_prestazioni_agli_appuntamenti(monkeypatch):
    finto = con_portale(monkeypatch, [RICERCA, PRESTAZIONI, APPUNTAMENTI], [xml(), xml()])
    res = c.check_nuova(CF, NRE, {"tipo": "comune", "valore": "TORINO"})
    avanti = finto.inviati[1]
    assert avanti["javax.faces.source"] == PREST + ":prestazioni-nextButton-main"
    assert avanti[PREST + ":scelta0"] == "true"  # le prestazioni restano come le propone il portale
    assert res["attuale"] is None and res["cosa"] == "ECOGRAFIA ADDOME COMPLETO"
    assert [s.luogo.sede for s in res["migliori"]] == ["POLIAMBULATORIO NORD"] and res["migliori"][0].proposta


def test_passo_sconosciuto_si_ferma_e_lo_descrive_senza_dati(monkeypatch):
    strana = "<h1>Ricerca Prestazioni Appuntamenti</h1>" + form(P + "quesitoForm", f'<p>{CF}</p><div id="{P}:boh">Ok</div>')
    con_portale(monkeypatch, [RICERCA, strana], [xml()])
    with pytest.raises(c.CupError) as e:
        c.check_nuova(CF, NRE)
    assert "non riconosciuto" in str(e.value) and "quesitoForm" in str(e.value) and CF not in str(e.value)


def test_piu_prestazioni_non_si_prenotano(monkeypatch):
    due = APPUNTAMENTI.replace(carrello("ECOGRAFIA ADDOME COMPLETO"), carrello("ECOGRAFIA ADDOME", "VISITA"))
    con_portale(monkeypatch, [RICERCA, due], [xml()])
    with pytest.raises(c.CupError, match="piu' prestazioni"):
        c.check_nuova(CF, NRE)


def test_senza_prenotazione_la_zona_serve_col_suo_valore():
    s = c.Slot(None, c.Luogo("POLIAMBULATORIO NORD", "ECO", "Via Po, 5 - TORINO (TO)"), "id")
    assert c.ammesso(s, None, {"tipo": "tutte", "valore": ""})
    assert c.ammesso(s, None, {"tipo": "provincia", "valore": "TO"})
    assert not c.ammesso(s, None, {"tipo": "sede", "valore": ""})  # nessuna sede da cui ricavarla


def test_non_presente_non_e_gia_presente(monkeypatch):
    con_portale(monkeypatch, [RICERCA], [xml("Ricetta non presente in archivio")])
    with pytest.raises(c.NonTrovata) as e:
        c.nuova(CF, NRE)
    assert not isinstance(e.value, c.GiaPrenotata)


def test_errore_generico_del_portale_non_rifiuta_la_ricetta(monkeypatch):
    con_portale(monkeypatch, [RICERCA], [xml("Servizio momentaneamente non disponibile")])
    with pytest.raises(c.CupError) as e:
        c.nuova(CF, NRE)
    assert not isinstance(e.value, c.NonTrovata)  # si riprova, niente pausa


def test_sposta_con_piu_prestazioni_resta_possibile(monkeypatch):
    due = APPUNTAMENTI.replace(carrello("ECOGRAFIA ADDOME COMPLETO"), carrello("ECOGRAFIA ADDOME", "VISITA"))
    con_portale(monkeypatch, [], [])
    cup = c.CupSession(CF, NRE)
    cup.modo = "sposta"  # come dopo alternative(): il limite vale solo per le prenotazioni nuove
    cup.appuntamenti(due)
    assert cup.n_prestazioni == 2 and cup.cosa == "ECOGRAFIA ADDOME + VISITA"


@pytest.mark.parametrize("msg", ["Servizio ricetta elettronica non disponibile", "Token non valido, riprovare",
                                 "Verifica captcha della ricetta non riuscita"])
def test_disservizio_che_nomina_la_ricetta_si_riprova(monkeypatch, msg):
    con_portale(monkeypatch, [RICERCA], [xml(msg)])
    with pytest.raises(c.CupError) as e:
        c.nuova(CF, NRE)
    assert not isinstance(e.value, c.NonTrovata)


def test_piu_prestazioni_si_ferma_prima_degli_appuntamenti(monkeypatch):
    due = PRESTAZIONI.replace(carrello("ECOGRAFIA ADDOME COMPLETO"), carrello("ECOGRAFIA ADDOME", "VISITA"))
    finto = con_portale(monkeypatch, [RICERCA, due], [xml()])
    with pytest.raises(c.PiuPrestazioni):
        c.check_nuova(CF, NRE)
    assert len(finto.inviati) == 1  # solo la Ricerca: "Avanti" non premuto, nessuna data bloccata


def test_mai_avanti_su_un_riepilogo(monkeypatch):
    riep = form(c.RIEPILOGO, f'<div id="{c.RIEPILOGO}:riepilogo-nextButton-bottom">Conferma</div>')
    finto = con_portale(monkeypatch, [RICERCA, riep], [xml()])
    with pytest.raises(c.CupError, match="Riepilogo"):
        c.check_nuova(CF, NRE)
    assert len(finto.inviati) == 1


def test_elenco_prenotazioni_non_leggibile_non_e_nessuna_prenotazione(monkeypatch):
    lista = form(c.L)
    con_portale(monkeypatch, [lista], ["<partial-response>manutenzione</partial-response>"])
    with pytest.raises(c.CupError) as e:
        c.cerca(CF, NRE)
    assert not isinstance(e.value, c.NonTrovata)


# --- prenota(nuova=True): controlli prima e dopo la Conferma -------------------------------------
SLOT = c.Slot(datetime(2026, 11, 3, 9, 0), c.Luogo("POLIAMBULATORIO NORD", "ECO 1", "Via Po, 5 - TORINO (TO)"), "sel1")
RIEP = ("Prestazioni selezionate: 1 ECOGRAFIA ADDOME COMPLETO Quando Martedì 3 Novembre 2026 alle ore 09:00 "
        "POLIAMBULATORIO NORD - ECO 1 - Via Po, 5 - TORINO (TO)")
FATTA = c.Prenotazione(SLOT.quando, SLOT.luogo, "ECOGRAFIA ADDOME COMPLETO")


def sessioni_finte(monkeypatch, attuali, riep=RIEP, n=1, cosa="ECOGRAFIA ADDOME COMPLETO"):
    """CupSession con i passi del portale sostituiti: registra Ricerca e Conferma."""
    log = {"ricetta": 0, "conferma": 0}
    coda = list(attuali)

    def attuale(self):
        v = coda.pop(0) if len(coda) > 1 else coda[0]
        if isinstance(v, Exception):
            raise v
        return v

    def ricetta(self):
        log["ricetta"] += 1
        self.modo = "nuova"
        return "passo prestazioni"

    def appuntamenti(self, page, estendi=0):
        self.cosa, self.n_prestazioni, self.slots = cosa, n, [SLOT]
        return self.slots

    def riepilogo(self, slot):
        m = c.DATE_RE.search(riep)
        return riep, c._date(m.group(0)), riep[m.end():m.end() + 250], "pagina"

    def conferma(self, page):
        log["conferma"] += 1
    for nome, fn in (("attuale", attuale), ("ricetta", ricetta), ("appuntamenti", appuntamenti),
                     ("riepilogo", riepilogo), ("conferma", conferma)):
        monkeypatch.setattr(c.CupSession, nome, fn)
    monkeypatch.setattr(c.CupSession, "fino_agli_appuntamenti", lambda self, page: page)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    return log


def prenota_nuova(sessione=None):
    return c.prenota(CF, NRE, SLOT, sessione=sessione, zona={"tipo": "tutte", "valore": ""}, dry_run=False, nuova=True)


def test_prima_prenotazione_verificata(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    assert prenota_nuova() == "Prenotazione fatta." and log["conferma"] == 1


def test_gia_prenotata_non_si_prenota(monkeypatch):
    log = sessioni_finte(monkeypatch, [FATTA])
    with pytest.raises(c.GiaPrenotata):
        prenota_nuova()
    assert log["conferma"] == 0


def test_prenotazione_erogata_non_si_aggiunge_un_appuntamento(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonAttiva("La prenotazione risulta in stato EROGATO", ["EROGATO"])])
    with pytest.raises(c.CupError, match="non prenoto"):
        prenota_nuova()
    assert log["conferma"] == 0


def test_disdetta_si_puo_prenotare_di_nuovo(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonAttiva("La prenotazione risulta in stato DISDETTO", ["DISDETTO"]), FATTA])
    assert prenota_nuova() == "Prenotazione fatta." and log["conferma"] == 1


def test_sessione_di_sposta_mai_usata_per_una_prenotazione_nuova(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    sposta = c.CupSession(CF, NRE)
    sposta.modo, sposta.slots = "sposta", [SLOT]
    assert prenota_nuova(sposta) == "Prenotazione fatta."
    assert log["ricetta"] == 1  # ha aperto una sessione nuova col flusso giusto


def test_sessione_del_controllo_riusata_se_e_dello_stesso_flusso(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    nuova = c.CupSession(CF, NRE)
    nuova.modo, nuova.slots, nuova.cosa, nuova.n_prestazioni = "nuova", [SLOT], "ECOGRAFIA ADDOME COMPLETO", 1
    assert prenota_nuova(nuova) == "Prenotazione fatta." and log["ricetta"] == 0


def test_prestazione_non_riconosciuta_non_conferma(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni")], n=0)
    with pytest.raises(c.CupError, match="non confermo"):
        prenota_nuova()
    assert log["conferma"] == 0


def test_riepilogo_con_due_appuntamenti_non_conferma(monkeypatch):
    due = RIEP + " Quando Mercoledì 4 Novembre 2026 alle ore 10:00 ALTRO"
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni")], riep=due)
    with pytest.raises(c.CupError, match="piu' appuntamenti"):
        prenota_nuova()
    assert log["conferma"] == 0
