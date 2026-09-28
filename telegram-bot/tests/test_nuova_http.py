"""Prenotazione nuova via HTTP: passi Ricerca e Prestazioni con pagine sintetiche (nessuna rete).
Le pagine hanno la struttura di quelle registrate dal portale: form JSF con i campi ICEfaces, carrello
"Prestazioni Selezionate", pagina Appuntamenti come quella di "Sposta"."""
import re
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
        r = self.risposte.pop(0)
        if isinstance(r, Exception):
            raise r
        return Risposta(r)


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


def diario_senza_dati():
    d = " | ".join(c.DIARIO)
    for dato in (CF, NRE, "ECOGRAFIA", "VISITA", "POLIAMBULATORIO", "TORINO", "2027"):
        assert dato not in d, dato
    return d


def test_piu_prestazioni_si_cercano_insieme(monkeypatch):
    due = APPUNTAMENTI.replace(carrello("ECOGRAFIA ADDOME COMPLETO"), carrello("ECOGRAFIA ADDOME", "VISITA"))
    con_portale(monkeypatch, [RICERCA, due], [xml()])
    c.DIARIO.clear()
    res = c.check_nuova(CF, NRE)
    assert res["cosa"] == "ECOGRAFIA ADDOME + VISITA" and [s.luogo.sede for s in res["slots"]] == ["POLIAMBULATORIO NORD"]
    d = diario_senza_dati()  # nel log: com'e' fatta la pagina, mai i dati
    assert "appuntamenti (nuova): carrello 2" in d and "date 1" in d


def test_senza_prenotazione_la_zona_serve_col_suo_valore():
    s = c.Slot(None, c.Luogo("POLIAMBULATORIO NORD", "ECO", "Via Po, 5 - TORINO (TO)"), "id")
    assert c.ammesso(s, None, {"tipo": "tutte", "valore": ""})
    assert c.ammesso(s, None, {"tipo": "provincia", "valore": "TO"})
    assert not c.ammesso(s, None, {"tipo": "sede", "valore": ""})  # nessuna sede da cui ricavarla
    assert c.ammesso(s, None, {"tipo": "sedi", "valore": [{"sede": "Poliambulatorio Nord", "comune": "Torino"}]})
    assert not c.ammesso(s, None, {"tipo": "sedi", "valore": [{"sede": "POLIAMBULATORIO SUD", "comune": "TORINO"}]})
    assert not c.ammesso(s, None, {"tipo": "sedi", "valore": [{"sede": "POLIAMBULATORIO NORD", "comune": "ALBA"}]})
    assert not c.ammesso(s, None, {"tipo": "sedi", "valore": []})  # nessuna sede scelta: niente


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


def test_piu_prestazioni_passo_prestazioni_nel_diario(monkeypatch):
    due = PRESTAZIONI.replace(carrello("ECOGRAFIA ADDOME COMPLETO"), carrello("ECOGRAFIA ADDOME", "VISITA"))
    app = APPUNTAMENTI.replace(carrello("ECOGRAFIA ADDOME COMPLETO"), carrello("ECOGRAFIA ADDOME", "VISITA"))
    finto = con_portale(monkeypatch, [RICERCA, due, app], [xml(), xml()])
    c.DIARIO.clear()
    c.check_nuova(CF, NRE)
    assert len(finto.inviati) == 2  # "Avanti" con le prestazioni come le propone il portale
    d = diario_senza_dati()
    assert "passo: sezioni" in d and "carrello 2, caselle spuntate 1/1, avanti prestazioni-nextButton-main" in d


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
PROPOSTA = c.Slot(SLOT.quando, SLOT.luogo, None, proposta=True)  # con piu' prestazioni si prenota solo la proposta


def sessioni_finte(monkeypatch, attuali, riep=RIEP, n=1, cosa="ECOGRAFIA ADDOME COMPLETO", nomi=None):
    """CupSession con i passi del portale sostituiti: registra Ricerca e Conferma."""
    log = {"ricetta": 0, "conferma": 0}
    coda = list(attuali)

    def attuale(self):
        v = coda.pop(0) if len(coda) > 1 else coda[0]
        if isinstance(v, Exception):
            raise v
        # (attuale, tutte le righe PRENOTATO con data[, righe PRENOTATO in tutto])
        v, self.prenotate, *resto = v if isinstance(v, tuple) else (v, [v])
        self.n_prenotate = resto[0] if resto else len(self.prenotate)
        return v

    def ricetta(self):
        log["ricetta"] += 1
        self.modo = "nuova"
        return "passo prestazioni"

    def appuntamenti(self, page, estendi=0):
        self.cosa, self.n_prestazioni, self.slots = cosa, n, [PROPOSTA if n > 1 else SLOT]
        self.nomi = list(nomi) if nomi is not None else ([cosa] if n else [])
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
    nuova.nomi = ["ECOGRAFIA ADDOME COMPLETO"]
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


# --- piu' prestazioni ---------------------------------------------------------------------------
NOMI = ["ECOGRAFIA ADDOME COMPLETO", "VISITA CARDIOLOGICA"]
DUE = " + ".join(NOMI)
RIEP2 = RIEP.replace("Prestazioni selezionate: 1 ECOGRAFIA ADDOME COMPLETO",
                     "Prestazioni selezionate: 2 ECOGRAFIA ADDOME COMPLETO VISITA CARDIOLOGICA")
FATTA_VISITA = c.Prenotazione(SLOT.quando, SLOT.luogo, "VISITA CARDIOLOGICA")
TUTTE_FATTE = (FATTA, [FATTA, FATTA_VISITA])


def nuova_multi(monkeypatch, dopo=TUTTE_FATTE, riep=RIEP2, nomi=NOMI, n=2):
    return sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), dopo], riep=riep, n=n,
                          cosa=" + ".join(nomi), nomi=nomi)


def test_piu_prestazioni_nello_stesso_appuntamento_si_prenotano(monkeypatch):
    log = nuova_multi(monkeypatch)
    assert prenota_nuova() == "Prenotazione fatta." and log["conferma"] == 1


def test_piu_prestazioni_una_riga_che_le_nomina_tutte(monkeypatch):
    # dopo la Conferma l'elenco potrebbe avere una sola riga con tutte le prestazioni
    una = c.Prenotazione(SLOT.quando, SLOT.luogo, "ECOGRAFIA ADDOME COMPLETO, VISITA CARDIOLOGICA")
    log = nuova_multi(monkeypatch, dopo=(una, [una]))
    assert prenota_nuova() == "Prenotazione fatta." and log["conferma"] == 1


@pytest.mark.parametrize("caso, riep, nomi, n", [
    ("una mancante", RIEP, NOMI, 2),
    ("carrello ridotto tra un passo e l'altro", RIEP, NOMI[:1], 2),
    ("nome dentro un altro", RIEP2.replace("VISITA CARDIOLOGICA", ""), ["ECOGRAFIA ADDOME", "ECOGRAFIA ADDOME COMPLETO"], 2),
    ("stessa prestazione due volte", RIEP2.replace(" VISITA CARDIOLOGICA", ""), [NOMI[0], NOMI[0]], 2),
    ("numero dichiarato diverso", RIEP2.replace("selezionate: 2", "selezionate: 3"), NOMI, 2),
    ("date diverse", RIEP2 + " Quando Mercoledì 4 Novembre 2026 alle ore 10:00 ALTRO", NOMI, 2),
    ("data in un altro formato", RIEP2 + " e il 04/11/2026 alle ore 10:00", NOMI, 2),
    ("stessa ora, altro luogo", RIEP2 + " Quando Martedì 3 Novembre 2026 alle ore 09:00 ALTRO AMBULATORIO", NOMI, 2),
    ("data in cifre senza 'alle ore'", RIEP2 + " VISITA CARDIOLOGICA Quando 04/11/2026 10:00 ALTRA SEDE", NOMI, 2),
    ("giorno senza ora", RIEP2 + " poi Mercoledì 4 Novembre 2026 ALTRA SEDE", NOMI, 2),
])
def test_piu_prestazioni_non_conferma_se_qualcosa_non_torna(monkeypatch, caso, riep, nomi, n):
    log = nuova_multi(monkeypatch, riep=riep, nomi=nomi, n=n)
    with pytest.raises(c.CupError, match="non confermo"):
        prenota_nuova()
    assert log["conferma"] == 0, caso


def test_una_prestazione_ma_il_riepilogo_ne_dichiara_di_piu(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni")], riep=RIEP2)
    with pytest.raises(c.CupError, match="piu' prestazioni del carrello"):
        prenota_nuova()
    assert log["conferma"] == 0


def test_riga_prenotata_senza_data_dopo_la_conferma_e_esito_incerto(monkeypatch):
    log = nuova_multi(monkeypatch, dopo=(FATTA, [FATTA, FATTA_VISITA], 3))
    with pytest.raises(c.CupError, match="esito incerto"):
        prenota_nuova()
    assert log["conferma"] == 1


def test_piu_prestazioni_prenotata_solo_una_e_esito_incerto(monkeypatch):
    log = nuova_multi(monkeypatch, dopo=(FATTA, [FATTA]))
    with pytest.raises(c.CupError, match="esito incerto"):
        prenota_nuova()
    assert log["conferma"] == 1


def test_verifica_dopo_la_conferma_su_tutte_le_righe(monkeypatch):
    # l'elenco ha anche altre righe prenotate: contano quelle al posto scelto
    altra = c.Prenotazione(datetime(2027, 2, 1, 8, 0), c.Luogo("ALTRO", "X", ""), "ALTRO ESAME")
    log = nuova_multi(monkeypatch, dopo=(altra, [altra, FATTA, FATTA_VISITA]))
    assert prenota_nuova() == "Prenotazione fatta." and log["conferma"] == 1


INSIEME = datetime(2027, 1, 10, 9, 0)
ECO = c.Prenotazione(INSIEME, SLOT.luogo, "ECOGRAFIA ADDOME COMPLETO")
VISITA = c.Prenotazione(INSIEME, SLOT.luogo, "VISITA CARDIOLOGICA")


def sposta():
    s = c.CupSession(CF, NRE)
    s.modo, s.slots = "sposta", [SLOT]
    return c.prenota(CF, NRE, SLOT, sessione=s, zona={"tipo": "tutte", "valore": ""}, dry_run=False)


def test_sposta_non_separa_le_prestazioni_prenotate_insieme(monkeypatch):
    log = sessioni_finte(monkeypatch, [(ECO, [ECO, VISITA])], riep=RIEP)
    with pytest.raises(c.Separerebbe):
        sposta()
    assert log["conferma"] == 0


def test_sposta_insieme_le_prestazioni_prenotate_insieme(monkeypatch):
    log = sessioni_finte(monkeypatch, [(ECO, [ECO, VISITA]), TUTTE_FATTE], riep=RIEP2)
    assert sposta() == "Prenotazione spostata." and log["conferma"] == 1


def test_sposta_che_ne_lascia_una_alla_data_vecchia_e_esito_incerto(monkeypatch):
    log = sessioni_finte(monkeypatch, [(ECO, [ECO, VISITA]), (FATTA, [FATTA, VISITA])], riep=RIEP2)
    with pytest.raises(c.CupError, match="esito incerto"):
        sposta()
    assert log["conferma"] == 1


def test_sposta_in_orari_diversi_mette_in_pausa(monkeypatch):
    # il portale le sposterebbe insieme ma a orari diversi (09:00 e 09:20): non si conferma e si fa pausa
    riep = RIEP2 + " Quando Martedì 3 Novembre 2026 alle ore 09:20 POLIAMBULATORIO NORD - ECO 1"
    log = sessioni_finte(monkeypatch, [(ECO, [ECO, VISITA])], riep=riep)
    with pytest.raises(c.Separerebbe):
        sposta()
    assert log["conferma"] == 0


def test_sposta_con_una_riga_prenotata_senza_data_non_sposta(monkeypatch):
    log = sessioni_finte(monkeypatch, [(ECO, [ECO], 2)], riep=RIEP)
    with pytest.raises(c.CupError, match="non leggibile"):
        sposta()
    assert log["conferma"] == 0


def test_sposta_di_una_sola_prestazione_come_prima(monkeypatch):
    sola = c.Prenotazione(INSIEME, SLOT.luogo, "ECOGRAFIA ADDOME COMPLETO")
    log = sessioni_finte(monkeypatch, [(sola, [sola]), FATTA], riep=RIEP)
    assert sposta() == "Prenotazione spostata." and log["conferma"] == 1


def test_stesso_appuntamento_date_in_altri_formati():
    assert c._stesso_appuntamento(RIEP2, SLOT)
    assert c._stesso_appuntamento(RIEP2 + " codice 89.01.12", SLOT)  # un codice di prestazione non e' una data
    assert not c._stesso_appuntamento(RIEP2 + " anche il 04/11/2026", SLOT)
    assert not c._stesso_appuntamento(RIEP2 + " e Mercoledì 4 Novembre 2026", SLOT)


def test_id_nel_diario_mai_dati():
    assert c._id("x:y:spostaButton") == "spostaButton"
    assert c._id("x:" + CF) == "?" and c._id("x:" + NRE) == "?" and c._id("x:a b") == "?"
    assert c._id("x:prestazioniForm") == "prestazioniForm"  # 15 lettere: non e' una ricetta
    assert c._caselle('<input type="checkbox" data-checked="false" />') == (0, 1)


def test_piu_prestazioni_date_per_prestazione_e_riepilogo_mancato_nel_diario(monkeypatch):
    # come pratica 6 dal vivo: due prestazioni, ognuna con le sue date negli "Appuntamenti Disponibili"
    due = carrello("ECOGRAFIA ADDOME", "VISITA")
    pagina = ("Appuntamenti Proposti " + due + form(c.A, f"<div>'{c.A}:x:0:app_selector'</div>" +
              blocco("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1", cosa="ECOGRAFIA ADDOME") +
              f'<div class="btn btn-default" id="{c.A}:altre">Altre disponibilità</div>'))
    disp = ("<partial-response>Appuntamenti Disponibili" +
            blocco("Lunedì 6 Settembre 2027", "POLIAMBULATORIO SUD", "ECO 2", seleziona_id=c.A + ":s1", cosa="ECOGRAFIA ADDOME") +
            blocco("Lunedì 6 Settembre 2027", "POLIAMBULATORIO SUD", "ECO 2", seleziona_id=c.A + ":s2", cosa="VISITA") +
            blocco("Martedì 7 Settembre 2027", "POLIAMBULATORIO SUD", "AMB 3", seleziona_id=c.A + ":s3", cosa="VISITA") +
            "</partial-response>")
    # Seleziona accettata, ma "Avanti" non porta al Riepilogo (manca la data dell'altra prestazione)
    finto = con_portale(monkeypatch, [RICERCA, pagina], [xml(), xml(), disp, xml(), xml("Selezionare un appuntamento")])
    c.DIARIO.clear()
    res = c.check_nuova(CF, NRE)
    d = diario_senza_dati()
    assert "date 4, con Seleziona 3, date uguali 1, altre disponibilita' pulsante/app" in d
    assert "date per prestazione [1, 2] nessuna 0" in d
    cup = res["sessione"]
    s3 = [x for x in res["slots"] if x.seleziona_id == c.A + ":s3"][0]
    with pytest.raises(c.CupError, match="Non sono arrivato al Riepilogo: Selezionare un appuntamento"):
        cup.riepilogo(s3)
    d = diario_senza_dati()
    assert "seleziona: messaggi d'errore 0, rifiutata no" in d and "avanti: redirect no, messaggi d'errore 1" in d
    assert finto.inviati[-2]["javax.faces.source"] == c.A + ":s3"


def test_slot_doppio_si_dice_col_suo_motivo(monkeypatch):
    doppio = c.Slot(datetime(2027, 9, 6, 9, 0), c.Luogo("SUD", "ECO 2", "Via Roma, 1 - TORINO (TO)"), "b1")

    class Finta:
        modo, search = "nuova", {c.L + ":IDSearchValueInput": NRE}
        slots = [doppio, c.Slot(doppio.quando, doppio.luogo, "b2")]

    monkeypatch.setattr(c.CupSession, "attuale", lambda self: (_ for _ in ()).throw(c.NonTrovata("nessuna")))
    monkeypatch.setattr(c.CupSession, "__init__", lambda self, cf, nre: None)
    monkeypatch.setattr(c.CupSession, "ricetta", lambda self: "")
    monkeypatch.setattr(c.CupSession, "fino_agli_appuntamenti", lambda self, page: page)
    monkeypatch.setattr(c.CupSession, "appuntamenti", lambda self, page, estendi=0: setattr(self, "slots", Finta.slots))
    with pytest.raises(c.CupError, match="presente 2 volte"):
        c.prenota(CF, NRE, doppio, sessione=Finta(), zona={"tipo": "tutte", "valore": ""}, dry_run=True, nuova=True)


def test_piu_prestazioni_solo_la_proposta_e_tra_le_migliori(monkeypatch):
    due = carrello("ECOGRAFIA ADDOME", "VISITA")
    pagina = ("Appuntamenti Proposti " + due + form(c.A, f"<div>'{c.A}:x:0:app_selector'</div>" +
              blocco("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1", cosa="ECOGRAFIA ADDOME") +
              f'<div class="btn btn-default" id="{c.A}:altre">Altre disponibilità</div>'))
    disp = ("<partial-response>Appuntamenti Disponibili" +
            blocco("Lunedì 6 Settembre 2027", "POLIAMBULATORIO SUD", "ECO 2", seleziona_id=c.A + ":s1") + "</partial-response>")
    con_portale(monkeypatch, [RICERCA, pagina], [xml(), xml(), disp])
    res = c.check_nuova(CF, NRE)
    assert len(res["slots"]) == 2 and res["solo_proposta"]
    assert [x.proposta for x in res["migliori"]] == [True]


def test_piu_prestazioni_una_data_non_proposta_non_si_seleziona(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni")], n=2, nomi=["ECO", "VISITA"])
    vera = c.CupSession.appuntamenti

    def appuntamenti(self, page, estendi=0):
        vera(self, page, estendi)
        self.slots = [SLOT]  # la data c'e', ma e' di "Altre disponibilita'"
        return self.slots
    monkeypatch.setattr(c.CupSession, "appuntamenti", appuntamenti)
    selezionate = []
    monkeypatch.setattr(c.CupSession, "riepilogo", lambda self, slot: selezionate.append(slot))
    with pytest.raises(c.CupError, match="solo la data proposta"):
        prenota_nuova()  # senza sessione del controllo: se ne apre una, ma "Seleziona" non parte
    sessione = c.CupSession(CF, NRE)
    sessione.modo, sessione.slots, sessione.n_prestazioni = "nuova", [SLOT], 2
    sessione.search = {c.L + ":IDSearchValueInput": NRE}
    with pytest.raises(c.CupError, match="solo la data proposta"):
        prenota_nuova(sessione)  # con la sessione del controllo: nessun'altra sessione aperta
    assert not selezionate and log["conferma"] == 0 and log["ricetta"] == 1


def test_tempi_della_prenotazione_nel_diario(monkeypatch):
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    c.DIARIO.clear()
    assert prenota_nuova() == "Prenotazione fatta."
    [tempi] = [x for x in c.DIARIO if x.startswith("tempi: ")]
    assert re.fullmatch(r"tempi: elenco [\d.]+s, riepilogo [\d.]+s, conferma [\d.]+s, verifica [\d.]+s, fine [\d.]+s",
                        tempi)
    assert set(c.TEMPI) == {"elenco", "riepilogo", "conferma", "verifica", "fine"}
    c.DIARIO.clear()
    sessioni_finte(monkeypatch, [FATTA])
    with pytest.raises(c.GiaPrenotata):
        prenota_nuova()
    assert c.DIARIO[-1].startswith("tempi: fine")  # anche quando si ferma prima


def test_estendi_area_lento_tiene_le_date_gia_lette(monkeypatch):
    # dal vivo (2026-09-28): "Estendi area" in provincia di Torino scadeva sempre e il controllo perdeva tutto
    pagina = ("Appuntamenti Proposti " + carrello("ECOGRAFIA ADDOME COMPLETO") +
              form(c.A, f"<div>'{c.A}:x:0:app_selector'</div>" +
                   blocco("Martedì 14 Settembre 2027", "POLIAMBULATORIO NORD", "ECO 1") +
                   f'<div class="btn btn-default" id="{c.A}:altre">Altre disponibilità</div>'))
    disp = ("<partial-response>Appuntamenti Disponibili" +
            blocco("Lunedì 6 Settembre 2027", "POLIAMBULATORIO SUD", "ECO 2", seleziona_id=c.A + ":s1") +
            f'<div id="{c.A}:nextArea">Estendi area di ricerca</div></partial-response>')
    con_portale(monkeypatch, [RICERCA, pagina], [xml(), xml(), disp, c.requests.ReadTimeout("x")])
    c.DIARIO.clear()
    monkeypatch.setattr(c, "AREA_INCOMPLETA", False)
    monkeypatch.setattr(c, "PIU_LENTA", 0.0)
    res = c.check_nuova(CF, NRE, {"tipo": "provincia", "valore": "TO"})
    assert sorted(x.luogo.sede for x in res["slots"]) == ["POLIAMBULATORIO NORD", "POLIAMBULATORIO SUD"]
    assert c.AREA_INCOMPLETA and c.PIU_LENTA == c.LENTO
    assert any(x.startswith("estendi area: timeout al passo 1 di 4") for x in c.DIARIO)


def test_conferma_senza_risposta_si_verifica_lo_stesso(monkeypatch):
    # dal vivo (2026-09-28): la risposta alla Conferma e' scaduta, ma la prenotazione era fatta
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])

    def conferma(self, page):
        log["conferma"] += 1
        raise c.requests.ReadTimeout("x")
    monkeypatch.setattr(c.CupSession, "conferma", conferma)
    assert prenota_nuova() == "Prenotazione fatta." and log["conferma"] == 1


def test_provincia_dal_comune_se_l_indirizzo_non_la_riporta():
    # dal vivo (2026-09-28): Susa scartata da "provincia di Torino" perche' l'indirizzo era "CORSO INGHILTERRA - ()"
    susa = c.Luogo("PRESIDIO - SUSA", "10369-SUSA - PRIMA VISITA GASTRO PEDIATRICA", "CORSO INGHILTERRA - ()")
    assert c.provincia(c.Luogo("POLIAMBULATORIO - SALE", "AMB", "VIA X - ()")) == ""  # parola, non il comune
    assert c.provincia(c.Luogo("OSPEDALE", "10369-SUSA - VISITA", "VIA X - ()")) == ""  # solo dalla sede
    assert c.comune(susa) == "SUSA" and c.provincia(susa) == "TO"
    slot = c.Slot(datetime(2026, 12, 1, 10, 10), susa, "s1")
    assert c.ammesso(slot, None, {"tipo": "provincia", "valore": "TO"})
    assert c.ammesso(slot, None, {"tipo": "comune", "valore": "SUSA"})
    mondovi = c.Luogo("OSPEDALE", "AMB", "VIA ROMA 1 - MONDOVI' ()")
    assert c.provincia(c.Luogo("OSPEDALE - MONDOVI'", "AMB", "")) == "CN"
    assert c.provincia(mondovi) == ""  # nessun pezzo intero che sia un comune: meglio non indovinare
    assert c.provincia(c.Luogo("X", "Y", "Via Po 1 - TORINO (TO)")) == "TO"  # l'indirizzo vale per primo
    assert len(c.COMUNI_PIEMONTE) == 1180


def test_comuni_col_trattino_e_accenti():
    beura = c.Luogo("OSPEDALE", "AMB", "VIA ROMA 1 - BEURA-CARDEZZA (VB)")
    assert c.comune(beura) == "BEURA-CARDEZZA" and c.provincia(beura) == "VB"
    assert c.comune(c.Luogo("X", "Y", "VIA A-B - MONCALIERI (TO)")) == "MONCALIERI"
    assert c.comune(c.Luogo("X", "Y", "Largo Turati 62 - TORINO (TO)")) == "TORINO"
    assert c.comune(c.Luogo("X", "Y", "- MONCALIERI (TO)")) == "MONCALIERI"
    assert c.comune(c.Luogo("OSP - RIVOLI", "Y", "VIA X (ANGOLO Y) - TORINO (TO)")) == "TORINO"  # parentesi nella via
    mondovi = c.Slot(datetime(2027, 1, 5, 9, 0), c.Luogo("OSP", "AMB", "VIA X 1 - MONDOVI' (CN)"), "s")
    assert c.ammesso(mondovi, None, {"tipo": "comune", "valore": "MONDOVÌ"})
    assert c.ammesso(mondovi, None, {"tipo": "comuni", "valore": ["Mondovì"]})
    assert c.zona_norm({"tipo": "boh"}) == {"tipo": "sede", "valore": ""}
    assert c.zona_norm([1]) == {"tipo": "sede", "valore": ""}
    samone = c.COORD[c._chiave_comune("Samone")]
    assert 45 < samone[0] < 46 and 7 < samone[1] < 8  # Samone (TO), non quello del Trentino


# --- errori del portale, passi, verifica dopo la Conferma --------------------------------------
def errore_http(codice):
    r = c.requests.Response()
    r.status_code, r.url = codice, f"https://cup.isan.csi.it/web?cf={CF}&nre={NRE}"
    return c.requests.HTTPError(f"{codice} Server Error for url: {r.url}", response=r)


def test_errori_classificati():
    for codice in (502, 503, 504):
        assert c.tipo_errore(errore_http(codice)) == "sovraccarico" and c.sovraccarico(errore_http(codice))
    for codice in (400, 403, 404, 500):
        assert c.tipo_errore(errore_http(codice)) == "inattesa" and not c.sovraccarico(errore_http(codice))
    assert c.tipo_errore(c.requests.ReadTimeout("x")) == "timeout"
    assert c.tipo_errore(c.requests.ConnectionError("HTTPSConnectionPool: Read timed out.")) == "timeout"
    assert c.tipo_errore(c.requests.ConnectionError("Connection refused")) == "sovraccarico"
    assert c.tipo_errore(c.requests.ConnectTimeout("x")) == "sovraccarico"
    assert c.descrivi(errore_http(504)) == "HTTP 504, portale sovraccarico"
    assert c.descrivi(errore_http(403)) == "HTTP 403, risposta inattesa"
    assert c.descrivi(c.requests.ReadTimeout("x")) == "ReadTimeout, portale sovraccarico"


class RispostaErrata:
    def __init__(self, codice):
        self.status_code, self.text = codice, "<html>Gateway Time-out</html>"

    def raise_for_status(self):
        raise errore_http(self.status_code)


def test_codice_e_passo_nel_diario_senza_dati_personali(monkeypatch):
    c.DIARIO.clear()
    c.RICHIESTE.clear()
    c._passo("riepilogo")
    with pytest.raises(c.requests.HTTPError):
        c._chiama(lambda url, timeout: RispostaErrata(504), f"https://cup.isan.csi.it/x?cf={CF}")
    c._passo("elenco")
    with pytest.raises(c.requests.HTTPError):
        c._get(type("S", (), {"get": lambda self, url, timeout: RispostaErrata(403)})(), c.LISTA_URL)
    c._passo("estendi")

    def lenta(url, timeout):
        raise c.requests.ReadTimeout(f"HTTPSConnectionPool: Read timed out. (read timeout={timeout[1]}) {url}")
    monkeypatch.setattr(c, "ATTESE", {"estendi": 77})
    with pytest.raises(c.requests.ReadTimeout):
        c._chiama(lenta, f"https://cup.isan.csi.it/x?nre={NRE}")
    assert list(c.DIARIO) == ["riepilogo: HTTP 504, portale sovraccarico", "elenco: HTTP 403, risposta inattesa",
                              "estendi: ReadTimeout, portale sovraccarico dopo 77 s"]
    assert [(r[1], r[3], r[4]) for r in c.RICHIESTE] == [("riepilogo", "sovraccarico", 504),
                                                         ("elenco", "inattesa", 403), ("estendi", "timeout", None)]
    assert c.RICHIESTE[-1][2] == 77  # un timeout conta come una risposta lunga quanto l'attesa
    assert CF not in " ".join(c.DIARIO) and NRE not in " ".join(c.DIARIO) and "http" not in " ".join(c.DIARIO)


def test_timeout_del_passo_in_corso(monkeypatch):
    monkeypatch.setattr(c, "ATTESE", {"elenco": 33, "estendi": 150})
    monkeypatch.setattr(c, "LENTO", 90)
    attese = []

    class R:
        status_code, text = 200, "ok"

        def raise_for_status(self):
            pass

    def get(url, timeout):
        attese.append((c.PASSO, timeout))
        return R()
    c.RICHIESTE.clear()
    for passo in ("elenco", "estendi", "riepilogo"):
        c._passo(passo)
        c._chiama(get, "u")
    assert attese == [("elenco", (20, 33)), ("estendi", (20, 150)), ("riepilogo", (20, 90))]  # senza dati: LENTO
    assert [(r[1], r[3], r[4]) for r in c.RICHIESTE] == [("elenco", "ok", 200), ("estendi", "ok", 200),
                                                         ("riepilogo", "ok", 200)]


def test_pagina_con_errore_http_e_un_errore_del_portale(monkeypatch):
    # prima un 504 sulla pagina dell'elenco diventava "campi JSF mancanti"; ora e' il sovraccarico che e'
    finto = con_portale(monkeypatch, [], [])
    finto.get = lambda url, timeout=None: RispostaErrata(504)
    c.DIARIO.clear()
    with pytest.raises(c.requests.HTTPError):
        c.cerca(CF, NRE)
    assert list(c.DIARIO) == ["elenco: HTTP 504, portale sovraccarico"]


def orologio_finto(monkeypatch, tentativo_s=0.0):
    """time.monotonic e time.sleep finti: ogni pausa e ogni tentativo di verifica fanno avanzare l'orologio."""
    ora, pause = [0.0], []

    def dormi(s):
        pause.append(s)
        ora[0] += s
    monkeypatch.setattr(c.time, "sleep", dormi)
    monkeypatch.setattr(c.time, "monotonic", lambda: ora[0])
    return ora, pause


def test_verifica_con_attese_crescenti(monkeypatch):
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), c.CupError("x"), c.CupError("x"), FATTA])
    ora, pause = orologio_finto(monkeypatch)
    assert prenota_nuova() == "Prenotazione fatta."
    assert pause == [2, 5, 10]  # riuscita al terzo tentativo


def test_verifica_senza_esito_resta_incerta_in_tempo_limitato(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), c.CupError("x")])
    ora, pause = orologio_finto(monkeypatch)
    with pytest.raises(c.CupError, match="esito incerto"):
        prenota_nuova()
    assert pause == list(c.VERIFICA_PAUSE) and log["conferma"] == 1


def test_verifica_lenta_si_ferma_entro_il_massimo(monkeypatch):
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni")])
    ora, pause = orologio_finto(monkeypatch)
    passi, attese = [], []

    def attuale(self):
        passi.append(self.passo_elenco)
        c._passo(self.passo_elenco)
        attese.append(c._attesa()[1] if passi[-1] == "verifica" else None)
        if passi[-1] == "verifica":
            ora[0] += 80  # ogni tentativo il portale ci mette 80 s
            raise c.requests.ReadTimeout("x")
        raise c.NonTrovata("Non esistono prenotazioni")
    monkeypatch.setattr(c.CupSession, "attuale", attuale)
    monkeypatch.setattr(c, "ATTESE", {"verifica": 150})
    with pytest.raises(c.CupError, match="esito incerto"):
        prenota_nuova()
    assert passi[0] == "elenco" and set(passi[1:]) == {"verifica"}
    # due tentativi; al terzo (dopo 2 + 80 + 5 + 80 s) resterebbero 13 s meno la pausa: meno di VERIFICA_MIN
    assert pause == [2, 5] and ora[0] == 2 + 80 + 5 + 80 <= c.VERIFICA_MAX
    assert attese[1:] == [150, 180 - 87]  # ogni richiesta aspetta al massimo il tempo che resta
    assert c.ATTESE == {"verifica": 150} and c.SCADENZA is None


def test_fasi_della_prenotazione(monkeypatch):
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    fasi = []
    assert c.prenota(CF, NRE, SLOT, zona={"tipo": "tutte", "valore": ""}, dry_run=False, nuova=True,
                     fase=fasi.append) == "Prenotazione fatta."
    assert fasi == ["riepilogo", "conferma", "verifica"]
    fasi.clear()
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    c.prenota(CF, NRE, SLOT, zona={"tipo": "tutte", "valore": ""}, dry_run=True, nuova=True, fase=fasi.append)
    assert fasi == ["riepilogo"]  # in prova la Conferma non parte

    def rotta(nome):
        raise RuntimeError("callback")
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    with pytest.raises(c.CupError, match="non confermo"):  # senza lo stato salvato la Conferma non parte
        c.prenota(CF, NRE, SLOT, zona={"tipo": "tutte", "valore": ""}, dry_run=False, nuova=True, fase=rotta)
    assert log["conferma"] == 0

    def rotta_dopo(nome):
        if nome != "conferma":
            raise RuntimeError("callback")
    sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])
    assert c.prenota(CF, NRE, SLOT, zona={"tipo": "tutte", "valore": ""}, dry_run=False, nuova=True,
                     fase=rotta_dopo) == "Prenotazione fatta."  # le altre fasi non fermano la prenotazione


def test_conferma_con_504_si_verifica_e_lo_scrive_senza_indirizzo(monkeypatch):
    log = sessioni_finte(monkeypatch, [c.NonTrovata("Non esistono prenotazioni"), FATTA])

    def conferma(self, page):
        log["conferma"] += 1
        raise errore_http(504)
    monkeypatch.setattr(c.CupSession, "conferma", conferma)
    c.DIARIO.clear()
    assert prenota_nuova() == "Prenotazione fatta."
    assert "conferma: HTTP 504, portale sovraccarico, verifico con una sessione nuova" in c.DIARIO
    assert CF not in " ".join(c.DIARIO)


def test_timeout_accorciati_della_verifica_non_insegnano(monkeypatch):
    ora, _ = orologio_finto(monkeypatch)
    monkeypatch.setattr(c, "ATTESE", {"verifica": 60})
    monkeypatch.setattr(c, "SCADENZA", 40.0)  # restano 40 s
    c.RICHIESTE.clear()
    c.DIARIO.clear()
    visti = []

    def lenta(url, timeout):
        visti.append(timeout)
        raise c.requests.ReadTimeout("x")
    c._passo("verifica")
    with pytest.raises(c.requests.ReadTimeout):
        c._chiama(lenta, "u")
    assert visti == [(20, 40)] and not c.RICHIESTE  # accorciato: non va nelle metriche
    assert c.DIARIO[-1] == "verifica: ReadTimeout, portale sovraccarico dopo 40 s (limite della verifica)"
    ora[0] = 40.5  # tempo finito: nessuna richiesta
    with pytest.raises(c.requests.ReadTimeout):
        c._chiama(lenta, "u")
    assert len(visti) == 1 and not c.RICHIESTE
    monkeypatch.setattr(c, "SCADENZA", None)  # fuori dalla verifica il timeout pieno si registra
    with pytest.raises(c.requests.ReadTimeout):
        c._chiama(lenta, "u")
    assert visti[-1] == (20, 60) and [(r[1], r[3]) for r in c.RICHIESTE] == [("verifica", "timeout")]
