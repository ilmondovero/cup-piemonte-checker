"""Disdetta di una prenotazione: il dialogo del portale (pagine sintetiche, nessuna rete) e il bot."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cup_http as c  # noqa: E402
from test_bot import ATT, b, inviati, pratica, registra  # noqa: E402,F401

L = c.L
QUANDO = datetime(2026, 12, 3, 12, 40)
LUOGO = c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)")
ALTRO = c.Luogo("OSPEDALE B", "AMB 2", "Via Po, 2 - TORINO (TO)")


class ListaFinta:
    def __init__(self, risposte):
        self.risposte, self.inviati, self.vs = list(risposte), [], "vs1"

    def post(self, campi, form=None):
        self.inviati.append(campi)
        return self.risposte.pop(0)


def sessione(risposte, xml=None):
    s = c.CupSession("RSSMRA80A01H501U", "010A00000000003")
    s.righe_prenotate = [(0, QUANDO, LUOGO)]
    s.lista_xml = xml or f'<span id="{L}:j_idt61:0:disdiciButton"></span>'
    s.lista = ListaFinta(risposte)
    return s


def dialogo(*bottoni):
    corpo = "".join(bottoni)
    return f'<partial-response><changes><update id="x"><![CDATA[<div>Vuoi disdire?{corpo}</div>]]></update></changes></partial-response>'


def test_trova_il_si_del_dialogo_e_preme_disdici_sulla_riga_giusta():
    s = sessione([dialogo(f'<a id="{L}:_t289" href="#">S&igrave;</a>', f'<a id="{L}:_t290" href="#">No</a>')])
    assert s.disdici_dialogo(QUANDO) == f"{L}:_t289"
    campi = s.lista.inviati[0]
    assert campi["javax.faces.source"] == f"{L}:j_idt61:0:disdiciButton"
    assert campi["javax.faces.behavior.event"] == "activate"
    assert campi[L + ":IDSearchValueInput"] == "010A00000000003"


def test_il_si_con_input_value_e_l_id_cambia_a_ogni_apertura():
    s = sessione([dialogo(f'<input type="button" id="{L}:_t7" value="Sì"/>', f'<input type="button" id="{L}:_t8" value="No"/>')])
    assert s.disdici_dialogo(QUANDO) == f"{L}:_t7"


def test_dialogo_non_riconosciuto_non_disdice():
    s = sessione([dialogo(f'<a id="{L}:_t1">Annulla</a>')])
    try:
        s.disdici_dialogo(QUANDO)
        assert False
    except c.CupError as e:
        assert "non riconosciuto" in str(e)
    assert any("dialogo non riconosciuto" in x for x in c.DIARIO)


def test_data_non_prenotata_o_doppia_non_disdice():
    s = sessione([])
    try:
        s.disdici_dialogo(datetime(2026, 12, 4, 9, 0))
        assert False
    except c.CupError as e:
        assert "unica prenotazione" in str(e)
    assert s.lista.inviati == []  # nessuna richiesta al portale
    s.righe_prenotate = [(0, QUANDO, LUOGO), (1, QUANDO, LUOGO)]
    try:
        s.disdici_dialogo(QUANDO)
        assert False
    except c.CupError:
        pass


def test_con_due_prenotazioni_si_disdice_solo_quella_scelta():
    s = sessione([dialogo(f'<a id="{L}:_t5">Sì</a>')], f'<b id="{L}:j_idt61:0:disdiciButton"></b><b id="{L}:j_idt61:1:disdiciButton"></b>')
    s.righe_prenotate = [(0, datetime(2026, 11, 1, 9, 0), LUOGO), (1, QUANDO, LUOGO)]
    s.disdici_dialogo(QUANDO)
    assert s.lista.inviati[0]["javax.faces.source"].endswith(":1:disdiciButton")


def prova_disdici(monkeypatch, risposte, dopo=None):
    """c.disdici con la sessione finta: risposte = quelle del portale (dialogo, poi conferma)."""
    s = sessione(risposte)
    monkeypatch.setattr(c, "CupSession", lambda cf, nre: s if not s.__dict__.get("usata") else dopo(cf, nre))
    monkeypatch.setattr(s, "attuale", lambda: s.__dict__.update(usata=True) or None)
    monkeypatch.setattr(c.time, "sleep", lambda x: None)
    return s


def test_disdici_riuscita_col_messaggio_del_portale(monkeypatch):
    s = prova_disdici(monkeypatch, [dialogo(f'<a id="{L}:_t289">Sì</a>'),
                                    "<p>La prenotazione è stata disdetta con successo.</p>"])
    fasi = []
    assert c.disdici("RSSMRA80A01H501U", "010A00000000003", QUANDO, dry_run=False, fase=fasi.append) == "Prenotazione disdetta."
    assert s.lista.inviati[1]["javax.faces.source"] == f"{L}:_t289" and fasi == ["conferma", "verifica"]


def test_disdici_in_prova_si_ferma_al_dialogo(monkeypatch):
    s = prova_disdici(monkeypatch, [dialogo(f'<a id="{L}:_t289">Sì</a>')])
    assert "PROVA" in c.disdici("RSSMRA80A01H501U", "010A00000000003", QUANDO, dry_run=True)
    assert len(s.lista.inviati) == 1


def test_senza_messaggio_si_verifica_con_una_sessione_nuova(monkeypatch):
    class Nuova:
        def __init__(self, cf, nre):
            pass

        def attuale(self):
            raise c.NonAttiva("La prenotazione risulta in stato DISDETTA", ["DISDETTA"])
    prova_disdici(monkeypatch, [dialogo(f'<a id="{L}:_t289">Sì</a>'), "<p>pagina diversa</p>"], dopo=Nuova)
    assert c.disdici("RSSMRA80A01H501U", "010A00000000003", QUANDO, dry_run=False) == "Prenotazione disdetta."


def test_prenotazione_ancora_presente_dopo_la_disdetta_e_esito_incerto(monkeypatch):
    class Nuova:
        n_prenotate = 1
        righe_prenotate = [(0, QUANDO, LUOGO)]

        def __init__(self, cf, nre):
            pass

        def attuale(self):
            return None
    prova_disdici(monkeypatch, [dialogo(f'<a id="{L}:_t289">Sì</a>'), "<p>pagina diversa</p>"], dopo=Nuova)
    try:
        c.disdici("RSSMRA80A01H501U", "010A00000000003", QUANDO, dry_run=False)
        assert False
    except c.CupError as e:
        assert "Disdetta inviata, esito incerto" in str(e) and "ancora presente" in str(e)


# --- il bot -------------------------------------------------------------------------------------
def test_bot_dopo_la_disdetta_la_ricetta_torna_da_prenotare_e_in_pausa(b, monkeypatch):
    registra(b)
    p = pratica(b)
    p["auto"] = {"on": True}
    b.salva(p, "auto")
    chiamate = []
    monkeypatch.setattr(c, "disdici", lambda cf, nre, quando, **k: chiamate.append(quando) or "Prenotazione disdetta.")
    assert b.disdici(pratica(b), ATT.quando.isoformat()) == "ok"
    p = pratica(b)
    assert chiamate == [ATT.quando] and p["stato"] == "pausa" and p["da_prenotare"] and not p.get("auto")
    ultimo = inviati(b)[-1]
    assert "Prenotazione disdetta" in ultimo and "OSPEDALE A" in ultimo and f"{ATT.quando:%d/%m/%Y}" in ultimo


def test_bot_non_disdice_se_la_prenotazione_e_cambiata(b, monkeypatch):
    registra(b)
    monkeypatch.setattr(c, "disdici", lambda *a, **k: (_ for _ in ()).throw(AssertionError("non va chiamata")))
    assert b.disdici(pratica(b), "2020-01-01T09:00:00") == "fallita"
    assert pratica(b)["stato"] == "attivo" and not pratica(b).get("da_prenotare")


def test_bot_disdetta_fallita_lascia_tutto_com_e(b, monkeypatch):
    registra(b)

    def fallisce(*a, **k):
        raise c.CupError("Dialogo di conferma della disdetta non riconosciuto: non disdico")
    monkeypatch.setattr(c, "disdici", fallisce)
    assert b.disdici(pratica(b), ATT.quando.isoformat()) == "fallita"
    assert pratica(b)["stato"] == "attivo" and "La prenotazione resta com'era" in inviati(b)[-1]


def test_bot_esito_incerto_non_cambia_lo_stato_e_avvisa_l_admin(b, monkeypatch):
    registra(b)

    def incerto(*a, **k):
        raise c.CupError("Disdetta inviata, esito incerto: la prenotazione risulta non verificabile.")
    monkeypatch.setattr(c, "disdici", incerto)
    assert b.disdici(pratica(b), ATT.quando.isoformat()) == "incerta"
    p = pratica(b)
    assert any(x.startswith("🚨") for x in inviati(b)) and p["stato"] == "pausa"
    assert p["disdetta_incerta"]["quando"] == ATT.quando.isoformat() and not p.get("da_prenotare")
    assert p["attuale"]["quando"] == ATT.quando.isoformat()  # la prenotazione resta quella finche' non si verifica


# --- doppioni: due prenotazioni per la stessa ricetta --------------------------------------------
def pren(giorni, cosa="VISITA - 11.11", sede="OSPEDALE A"):
    return c.Prenotazione(datetime(2030, 12, 1, 9, 0) + __import__("datetime").timedelta(days=giorni),
                          c.Luogo(sede, "AMB 1", "Via Roma, 1 - TORINO (TO)"), cosa)


def test_doppione_tiene_la_piu_vicina_e_disdice_la_piu_lontana():
    vicina, lontana, mezzo = pren(0), pren(40), pren(10)
    assert c.doppione([lontana, vicina]) == (vicina, lontana)
    assert c.doppione([mezzo, lontana, vicina]) == (vicina, lontana)


def test_non_sono_doppioni_prestazioni_diverse_o_la_stessa_nello_stesso_appuntamento():
    assert c.doppione([pren(0, "ECO ADDOME"), pren(40, "VISITA")]) is None  # prestazioni diverse
    assert c.doppione([pren(0), pren(0)]) is None  # quantita' due, stesso appuntamento
    assert c.doppione([pren(0)]) is None and c.doppione([]) is None
    assert c.doppione([pren(0, ""), pren(40, "")]) is None  # senza descrizione non si decide


def test_stessa_data_in_due_sedi_e_un_doppione():
    a, b2 = pren(5), pren(5, sede="OSPEDALE B")
    assert c.doppione([a, b2]) is not None


def sessioni_finte(monkeypatch, letture, disdetta=None):
    """CupSession che a ogni lettura restituisce l'elenco successivo; c.disdici registrato."""
    elenchi, disdette = list(letture), []

    class S:
        def __init__(self, cf, nre):
            self.prenotate, self.n_prenotate = list(elenchi.pop(0)), 0

        def attuale(self):
            self.n_prenotate = len(self.prenotate)
            return self.prenotate[0]
    monkeypatch.setattr(c, "CupSession", S)
    monkeypatch.setattr(c.time, "sleep", lambda x: None)
    monkeypatch.setattr(c, "disdici", lambda cf, nre, quando, **k: disdette.append(quando) or "Prenotazione disdetta.")
    return disdette


def test_elimina_doppione_disdice_la_piu_lontana_dopo_due_letture_uguali(monkeypatch):
    vicina, lontana = pren(0), pren(40)
    disdette = sessioni_finte(monkeypatch, [[lontana, vicina], [vicina, lontana]])
    assert c.elimina_doppione("CF", "NRE", dry_run=False) == (vicina, lontana)
    assert disdette == [lontana.quando]


def test_letture_che_non_concordano_non_disdicono(monkeypatch):
    vicina, lontana = pren(0), pren(40)
    disdette = sessioni_finte(monkeypatch, [[vicina, lontana], [vicina]])
    try:
        c.elimina_doppione("CF", "NRE", dry_run=False)
        assert False
    except c.CupError as e:
        assert "non concordano" in str(e)
    assert disdette == []


def test_nessun_doppione_nessuna_disdetta(monkeypatch):
    disdette = sessioni_finte(monkeypatch, [[pren(0)]])
    assert c.elimina_doppione("CF", "NRE", dry_run=False) is None and disdette == []


def test_prima_prenotazione_con_nessun_record_falso_non_prenota_due_volte(monkeypatch):
    letture = [c.NonTrovata("Nessun record"), pren(3)]

    class S:
        def __init__(self, cf, nre):
            pass

        def attuale(self):
            x = letture.pop(0)
            if isinstance(x, Exception):
                raise x
            return x
    monkeypatch.setattr(c, "CupSession", S)
    monkeypatch.setattr(c.time, "sleep", lambda x: None)
    slot = c.Slot(datetime(2027, 1, 1, 9, 0), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)"), "id")
    try:
        c.prenota("CF", "NRE", slot, dry_run=False, nuova=True)
        assert False
    except c.GiaPrenotata as e:
        assert "risulta prenotata" in str(e)


def test_bot_risolve_il_doppione_e_dice_cosa_tiene_e_cosa_disdice(b, monkeypatch):
    registra(b)
    vicina, lontana = pren(0), pren(40)
    monkeypatch.setattr(c, "check", lambda *a, **k: {"attuale": lontana, "slots": [], "sessione": None, "migliori": [],
                                                    "doppione": (vicina, lontana)})
    monkeypatch.setattr(c, "elimina_doppione", lambda *a, **k: (vicina, lontana))
    b.controlla(pratica(b))
    assert c.attuale_di if False else True
    import bot as botmod
    assert botmod.attuale_di(pratica(b)).quando == vicina.quando
    t = inviati(b)[-1]
    assert "prenotata due volte" in t and f"{vicina.quando:%d/%m/%Y}" in t and f"{lontana.quando:%d/%m/%Y}" in t
    assert "Tenuta" in t and "Disdetta" in t


def test_bot_doppione_non_risolto_avvisa_una_volta_ogni_sei_ore(b, monkeypatch):
    registra(b)
    vicina, lontana = pren(0), pren(40)
    monkeypatch.setattr(c, "check", lambda *a, **k: {"attuale": lontana, "slots": [], "sessione": None, "migliori": [],
                                                    "doppione": (vicina, lontana)})

    def no(*a, **k):
        raise c.CupError("Dialogo di conferma della disdetta non riconosciuto: non disdico")
    monkeypatch.setattr(c, "elimina_doppione", no)
    b.controlla(pratica(b))
    b.controlla(pratica(b))
    assert sum("prenotata due volte" in x for x in inviati(b)) == 1


def test_bot_non_prenota_una_nuova_con_una_conferma_incerta_in_sospeso(b, monkeypatch):
    registra(b)
    p = pratica(b)
    p["da_prenotare"] = True
    p["incerta"] = {"quando": datetime(2027, 1, 1, 9, 0).isoformat(), "luogo": "OSPEDALE A"}
    b.salva(p, "da_prenotare", "incerta")
    monkeypatch.setattr(c, "prenota", lambda *a, **k: (_ for _ in ()).throw(AssertionError("non va chiamata")))
    slot = c.Slot(datetime(2027, 1, 2, 9, 0), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)"), "id")
    assert b._prenota(pratica(b), slot, None) == "fallita"


# --- verifica dopo la disdetta, doppioni, esito incerto ------------------------------------------
def verifica_con(monkeypatch, letture):
    class Nuova:
        n_prenotate = 1
        righe_prenotate = [(0, QUANDO, LUOGO)]

        def __init__(self, cf, nre):
            pass

        def attuale(self):
            x = letture.pop(0)
            if isinstance(x, Exception):
                raise x
    prova_disdici(monkeypatch, [dialogo(f'<a id="{L}:_t289">Sì</a>'), "<p>pagina diversa</p>"], dopo=Nuova)


def disdici_senza_messaggio():
    return c.disdici("RSSMRA80A01H501U", "010A00000000003", QUANDO, dry_run=False)


def test_un_nessun_record_isolato_non_prova_la_disdetta(monkeypatch):
    nt, ancora = c.NonTrovata("Nessun record"), None
    verifica_con(monkeypatch, [nt, ancora, nt, ancora, nt])
    try:
        disdici_senza_messaggio()
        assert False
    except c.CupError as e:
        assert "Disdetta inviata, esito incerto" in str(e)


def test_due_nessun_record_di_fila_provano_la_disdetta(monkeypatch):
    verifica_con(monkeypatch, [c.NonTrovata("Nessun record"), c.NonTrovata("Nessun record")])
    assert disdici_senza_messaggio() == "Prenotazione disdetta."


def test_stato_erogata_non_prova_la_disdetta(monkeypatch):
    verifica_con(monkeypatch, [c.NonAttiva("La prenotazione risulta in stato EROGATA", ["EROGATA"])] * 5)
    try:
        disdici_senza_messaggio()
        assert False
    except c.CupError as e:
        assert "Disdetta inviata, esito incerto" in str(e)


def test_errore_imprevisto_nella_verifica_lascia_una_traccia(monkeypatch):
    verifica_con(monkeypatch, [KeyError("x")])
    try:
        disdici_senza_messaggio()
        assert False
    except c.CupError as e:
        assert "esito incerto" in str(e)
    assert any("errore imprevisto KeyError" in x for x in c.DIARIO)


def test_le_righe_passate_non_fanno_disdire_quella_futura():
    passata, futura = pren(-5000), pren(40)
    assert c.doppione([passata, futura]) is None
    vicina = pren(0)
    assert c.doppione([passata, vicina, futura]) == (vicina, futura)


def test_stessa_data_in_due_sedi_si_disdice_quella_giusta():
    xml = f'<b id="{L}:j_idt61:0:disdiciButton"></b><b id="{L}:j_idt61:1:disdiciButton"></b>'
    s = sessione([dialogo(f'<a id="{L}:_t5">Sì</a>')], xml)
    s.righe_prenotate = [(0, QUANDO, LUOGO), (1, QUANDO, ALTRO)]
    try:
        s.disdici_dialogo(QUANDO)
        assert False
    except c.CupError:
        pass
    s.disdici_dialogo(QUANDO, ALTRO)
    assert s.lista.inviati[0]["javax.faces.source"].endswith(":1:disdiciButton")


def test_elimina_doppione_con_la_lettura_del_controllo_apre_una_sola_sessione(monkeypatch):
    vicina, lontana = pren(0), pren(40)
    disdette = sessioni_finte(monkeypatch, [[vicina, lontana]])
    visto, chiamate = (vicina, lontana), []
    monkeypatch.setattr(c, "disdici", lambda cf, nre, quando, **k: chiamate.append((quando, k)) or "Prenotazione disdetta.")
    assert c.elimina_doppione("CF", "NRE", dry_run=False, visto=visto) == visto
    quando, k = chiamate[0]
    assert quando == lontana.quando and k["luogo"] == lontana.luogo and k["cup"] is not None and disdette == []


def test_bot_errore_imprevisto_nel_doppione_non_esce_da_controlla(b, monkeypatch):
    registra(b)
    vicina, lontana = pren(0), pren(40)
    monkeypatch.setattr(c, "check", lambda *a, **k: {"attuale": lontana, "slots": [], "sessione": None, "migliori": [],
                                                    "doppione": (vicina, lontana)})
    monkeypatch.setattr(c, "elimina_doppione", lambda *a, **k: (_ for _ in ()).throw(TypeError("riga strana")))
    b.controlla(pratica(b))


def test_disdetta_riuscita_non_cancella_una_conferma_incerta_in_sospeso(b, monkeypatch):
    registra(b)
    p = pratica(b)
    p["incerta"] = {"quando": datetime(2027, 1, 1, 9, 0).isoformat(), "luogo": "OSPEDALE A"}
    b.salva(p, "incerta")
    monkeypatch.setattr(c, "disdici", lambda *a, **k: "Prenotazione disdetta.")
    assert b.disdici(pratica(b), ATT.quando.isoformat()) == "ok"
    assert pratica(b).get("incerta")


def segna_disdetta_incerta(b):
    p = pratica(b)
    p["disdetta_incerta"] = {"quando": ATT.quando.isoformat(), "luogo": ATT.luogo.key(), "sede": ATT.luogo.sede,
                             "ambulatorio": ATT.luogo.ambulatorio, "indirizzo": ATT.luogo.indirizzo}
    b.salva(p, "disdetta_incerta")


def test_bot_con_una_disdetta_da_verificare_non_ne_parte_un_altra(b, monkeypatch):
    registra(b)
    segna_disdetta_incerta(b)
    monkeypatch.setattr(c, "disdici", lambda *a, **k: (_ for _ in ()).throw(AssertionError("non va chiamata")))
    assert b.disdici(pratica(b), ATT.quando.isoformat()) == "fallita"


def test_bot_disdetta_incerta_ancora_presente_al_controllo_dopo(b, monkeypatch):
    registra(b)
    segna_disdetta_incerta(b)
    assert b.chiudi_disdetta_incerta(pratica(b), ATT) is True
    assert not pratica(b).get("disdetta_incerta") and "non è andata a buon fine" in inviati(b)[-1]


def test_bot_disdetta_incerta_poi_solo_disdette_vale_come_disdetta_riuscita(b, monkeypatch):
    registra(b)
    segna_disdetta_incerta(b)

    def disdetta(*a, **k):
        raise c.NonAttiva("La prenotazione risulta in stato DISDETTA", ["DISDETTA"])
    monkeypatch.setattr(c, "check", disdetta)
    b.controlla(pratica(b))
    p = pratica(b)
    assert p["da_prenotare"] and p["stato"] == "pausa" and not p.get("disdetta_incerta")
    assert "disdetta di prima è andata a buon fine" in inviati(b)[-1]


def test_conferma_incerta_non_fatta_riattiva_la_conferma_automatica(b, monkeypatch):
    registra(b)
    p = pratica(b)
    p["auto"] = {"on": True}
    p["incerta"] = {"quando": datetime(2027, 1, 1, 9, 0).isoformat(), "luogo": "OSPEDALE X"}
    b.salva(p, "auto", "incerta")
    b.sospendi_auto(pratica(b))
    assert not pratica(b).get("auto") and pratica(b)["auto_sospesa"] == {"on": True}
    assert b.chiudi_incerta(pratica(b), ATT) is False
    assert pratica(b)["auto"] == {"on": True} and not pratica(b).get("auto_sospesa")
    assert "riattivato la conferma automatica" in inviati(b)[-1]


def test_conferma_incerta_fatta_lascia_spenta_la_conferma_automatica(b, monkeypatch):
    registra(b)
    p = pratica(b)
    p["auto"] = {"on": True}
    p["incerta"] = {"quando": ATT.quando.isoformat(), "luogo": ATT.luogo.key()}
    b.salva(p, "auto", "incerta")
    b.sospendi_auto(pratica(b))
    assert b.chiudi_incerta(pratica(b), ATT) is True
    assert not pratica(b).get("auto") and not pratica(b).get("auto_sospesa")


def test_data_sparita_fa_partire_subito_un_altro_controllo(b, monkeypatch):
    registra(b)
    p = pratica(b)
    p["prossimo"] = __import__("time").time() + 3600
    b.salva(p, "prossimo")

    def sparita(*a, **k):
        raise c.CupError("Slot 27/10/2026 12:00 non piu' disponibile (tentativo nella sessione originale: rifiutata)")
    monkeypatch.setattr(c, "prenota", sparita)
    slot = c.Slot(datetime(2026, 10, 27, 12, 0), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)"), "id")
    assert b._prenota(pratica(b), slot, None) == "fallita"
    assert pratica(b)["prossimo"] <= __import__("time").time()
    assert "Cerco subito un'altra data" in inviati(b)[-1]


def test_un_altro_controllo_dello_stesso_cf_non_libera_la_data_offerta(b, monkeypatch):
    import time as _t
    registra(b)
    p = pratica(b)
    p["prossimo"] = 0
    b.salva(p, "prossimo")
    b.distanza = 0
    altra = {"id": 999, "cf": p["cf"], "chat_id": p["chat_id"]}
    orig = b.store.get
    monkeypatch.setattr(b.store, "get", lambda pid: altra if pid == 999 else orig(pid))
    controlli = []
    monkeypatch.setattr(b, "controlla", lambda q, manuale=False: controlli.append(q["id"]))

    b.offerte[999] = {"ts": _t.time(), "token": "t", "sessione": None, "slots": []}
    assert b.sessione_viva_stesso_cf(pratica(b)) and not b.controllo_pianificato() and controlli == []

    altra["cf"] = "ALTRO"  # un altro paziente: nessun legame
    assert not b.sessione_viva_stesso_cf(pratica(b))
    altra["cf"] = p["cf"]
    b.offerte[999]["ts"] = _t.time() - botmod_ttl() - 1  # offerta scaduta
    assert not b.sessione_viva_stesso_cf(pratica(b)) and b.controllo_pianificato() and controlli == [p["id"]]


def botmod_ttl():
    import bot as botmod
    return botmod.TTL_OFFERTA


# --- "Conferma presa visione" delle note, prima della Conferma ----------------------------------
PAGINA_NOTE = f'<div id="{L}:_t5">x</div><div id="{c.RIEPILOGO}:_t217" onmouseover="y"><span aria-describedby="Conferma presa visione"></span></div>'
RISPOSTA_OK = ('<partial-response><changes><update id="j_id1:javax.faces.ViewState:0"><![CDATA[vs-nuovo]]></update>'
               '<update id="x"><![CDATA[<i class="icon-check" style="m"></i><span id="a">Presa visione delle note</span>]]></update>'
               '</changes></partial-response>')


class FormFinto:
    inviati, risposta = [], RISPOSTA_OK

    def __init__(self, s, page, form):
        self.vs = "vs-vecchio"

    def post(self, campi, form=None):
        FormFinto.inviati.append((campi, self.vs))
        return FormFinto.risposta


def sessione_note(monkeypatch, risposta=RISPOSTA_OK):
    FormFinto.inviati, FormFinto.risposta = [], risposta
    monkeypatch.setattr(c, "_Form", FormFinto)
    return c.CupSession("RSSMRA80A01H501U", "010A00000000003")


def test_presa_visione_clicca_il_pulsante_letto_dalla_pagina_e_tiene_il_viewstate_nuovo(monkeypatch):
    s = sessione_note(monkeypatch)
    s.presa_visione(PAGINA_NOTE)
    assert FormFinto.inviati[0][0]["javax.faces.source"] == f"{c.RIEPILOGO}:_t217" and s.vs_riepilogo == "vs-nuovo"
    s.conferma(PAGINA_NOTE)
    assert FormFinto.inviati[1][1] == "vs-nuovo"  # la Conferma parte con il ViewState dopo la presa visione


def test_presa_visione_senza_note_non_fa_nulla(monkeypatch):
    s = sessione_note(monkeypatch)
    s.presa_visione("<div>Riepilogo senza note</div>")
    assert FormFinto.inviati == [] and s.vs_riepilogo is None


def test_presa_visione_non_registrata_non_conferma(monkeypatch):
    s = sessione_note(monkeypatch, '<partial-response><changes><update id="x"><![CDATA[<i class="icon-check-empty"></i>'
                                   '<span>Presa visione delle note</span>]]></update></changes></partial-response>')
    try:
        s.presa_visione(PAGINA_NOTE)
        assert False
    except c.CupError as e:
        assert "presa visione" in str(e)


# --- note del CUP: registrate alla prenotazione, mostrate nel bot e nella Mini App ----------------
def test_note_riepilogo_estrae_il_link_senza_intestazioni():
    pagina = ('<div class="row-fluid" id="noteDialog" style="display: none;"><h4>Note</h4><h5>Note Paziente</h5>'
              '<span id="x" style="white-space: pre-wrap;">HTTPS://WWW.ESEMPIO.IT/PREP.PDF</span>'
              '<div><span aria-describedby="Conferma presa visione" role="button"></span></div></div>')
    assert c.note_riepilogo(pagina) == ["HTTPS://WWW.ESEMPIO.IT/PREP.PDF"]
    assert c.note_riepilogo("<div>niente note</div>") == []


def test_bot_registra_e_mostra_le_note_dopo_la_prenotazione(b, monkeypatch):
    registra(b)
    slot = c.Slot(datetime(2026, 10, 12, 9, 0), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)"), "id")

    def prenota(*a, **k):
        c.NOTE[:] = ["HTTPS://WWW.ESEMPIO.IT/PREP.PDF"]
        return "Prenotazione spostata."
    monkeypatch.setattr(c, "prenota", prenota)
    assert b._prenota(pratica(b), slot, None) == "ok"
    p = pratica(b)
    assert p["note"] == {"quando": slot.quando.isoformat(), "righe": ["HTTPS://WWW.ESEMPIO.IT/PREP.PDF"]}
    assert "Note del CUP" in inviati(b)[-1] and "PREP.PDF" in inviati(b)[-1]
    import bot as botmod
    assert botmod.righe_note(p) == ["HTTPS://WWW.ESEMPIO.IT/PREP.PDF"]
    p["note"]["quando"] = "2020-01-01T09:00:00"  # note di un'altra data: non valgono
    assert botmod.righe_note(p) == []


# --- un controllo alla volta per codice fiscale ---------------------------------------------------
def due_ricette_stesso_cf(b, monkeypatch):
    """Due ricette dello stesso CF, entrambe da controllare; controlla() finto che apre la sessione come quello vero."""
    import time as _t
    registra(b)
    p1 = pratica(b)
    q = b.store.new(p1["chat_id"], cf=p1["cf"], nre="010A30000000999", stato="attivo", prossimo=0)
    p1["prossimo"] = 0
    b.salva(p1, "prossimo")
    b.distanza = 0
    controlli = []

    def finto(pp, manuale=False):
        controlli.append(pp["id"])
        b.sessioni[pp["id"]] = {"ts": _t.time(), "sessione": None, "slots": []}
    monkeypatch.setattr(b, "controlla", finto)
    return p1["id"], q["id"], controlli


def test_due_ricette_dello_stesso_cf_si_controllano_una_alla_volta(b, monkeypatch):
    p1, q, controlli = due_ricette_stesso_cf(b, monkeypatch)
    assert b.controllo_pianificato() and len(controlli) == 1
    prima = controlli[0]
    altra = q if prima == p1 else p1
    # l'altra e' scaduta ma la sessione della prima tiene ancora le sue date: aspetta
    b.store.modifica(altra, lambda f: f.update(prossimo=0))
    assert not b.controllo_pianificato() and controlli == [prima]
    b.sessioni[prima]["ts"] -= botmod_ttl() + 1  # la sessione della prima e' scaduta
    assert b.controllo_pianificato() and controlli == [prima, altra]


def test_la_sessione_scartata_libera_l_altra_ricetta(b, monkeypatch):
    p1, q, controlli = due_ricette_stesso_cf(b, monkeypatch)
    assert b.controllo_pianificato()
    prima = controlli[0]
    altra = q if prima == p1 else p1
    b.store.modifica(altra, lambda f: f.update(prossimo=0))
    assert not b.controllo_pianificato()
    b.scarta(prima)  # presa la data, o ricetta cancellata: la sessione non tiene piu' niente
    assert b.controllo_pianificato() and controlli == [prima, altra]


def test_la_propria_sessione_non_blocca_il_proprio_controllo(b, monkeypatch):
    import time as _t
    registra(b)
    p = pratica(b)
    p["prossimo"] = 0
    b.salva(p, "prossimo")
    b.distanza = 0
    b.sessioni[p["id"]] = {"ts": _t.time(), "sessione": None, "slots": []}
    controlli = []
    monkeypatch.setattr(b, "controlla", lambda pp, manuale=False: controlli.append(pp["id"]))
    assert b.controllo_pianificato() and controlli == [p["id"]]


def test_ricette_di_codici_fiscali_diversi_non_si_aspettano(b, monkeypatch):
    p1, q, controlli = due_ricette_stesso_cf(b, monkeypatch)
    b.store.modifica(q, lambda f: f.update(cf="ALTROCF"))  # un altro paziente
    assert b.controllo_pianificato() and b.controllo_pianificato() and sorted(controlli) == sorted([p1, q])


# --- note lunghe: mai troncate ---------------------------------------------------------------------
def test_note_riepilogo_lunghe_restano_intere():
    lunga = "Preparazione: " + "digiuno e idratazione prima dell'esame, " * 60  # ~2400 caratteri
    pagina = (f'<div id="noteDialog"><h4>Note</h4><h5>Note Paziente</h5><span style="white-space: pre-wrap;">{lunga}</span>'
              + "".join(f"<span>Riga {i}</span>" for i in range(30))
              + '<div><span aria-describedby="Conferma presa visione"></span></div></div>')
    righe = c.note_riepilogo(pagina)
    assert righe[0] == " ".join(lunga.split()) and len(righe) == 31 and righe[-1] == "Riga 29"


def test_pezzi_note_non_perde_niente_e_non_supera_il_limite():
    import bot as botmod
    righe = ["a " * 2000, "riga corta", "b" * 5000, "ultima"]  # una riga con spazi, una senza, una corta
    pezzi = botmod.pezzi_note(righe, massimo=3500)
    assert all(len(x) <= 3500 for x in pezzi)
    assert "".join("".join(pezzi).split()) == "".join("".join(righe).split())  # stesso contenuto, spazi a parte
    assert botmod.pezzi_note([]) == [] and botmod.pezzi_note(["x"]) == ["x"]


def test_bot_note_lunghe_in_piu_messaggi_numerati(b, monkeypatch):
    registra(b)
    slot = c.Slot(datetime(2026, 10, 12, 9, 0), c.Luogo("OSPEDALE A", "AMB 1", "Via Roma, 1 - TORINO (TO)"), "id")
    lunga = ["parola " * 700]  # ~4900 caratteri: due messaggi

    def prenota(*a, **k):
        c.NOTE[:] = lunga
        return "Prenotazione spostata."
    monkeypatch.setattr(c, "prenota", prenota)
    assert b._prenota(pratica(b), slot, None) == "ok"
    messaggi = [x for x in inviati(b) if "Note del CUP" in x]
    assert len(messaggi) == 2 and "[1/2]" in messaggi[0] and "[2/2]" in messaggi[1]
    assert all(len(x) < 4000 for x in messaggi)
    assert pratica(b)["note"]["righe"] == lunga  # salvate intere
