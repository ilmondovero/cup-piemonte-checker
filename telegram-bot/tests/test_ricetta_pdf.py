import pytest

import ricetta_pdf as r

TESTO = (
    "SERVIZIO SANITARIO NAZIONALE RICETTA ELETTRONICA Regione Piemonte *010A3* *010A3* "
    "COGNOME E NOME/INIZIALI DELL'ASSISTITO: ROSSI MARIA INDIRIZZO: VIA ROMA, 1 "
    "*1234567890* *1234567890* *RSSMRA80A01H501U* *RSSMRA80A01H501U* ESENZIONE: 046 "
    "PRIORITA' PRESCRIZIONE (U, B, D, P): Programmabile PRESCRIZIONE 88.78 (88.78) - ECOGRAFIA OSTETRICA "
    "ALTRA TIPOLOGIA DI ACCESSO QTA' NOTA 1 QUESITO DIAGNOSTICO: DOLORE PELVICO N. CONFEZIONI/PRESTAZIONI: 1 "
    "DATA: 29-09-2026 CODICE FISCALE DEL MEDICO: AAABBB50A01H501Z"
)


def test_codice_fiscale_controlla_il_carattere_finale():
    assert r.cf_valido("RSSMRA80A01H501U")
    assert not r.cf_valido("RSSMRA80A01H501X") and not r.cf_valido("ciao")


def test_analizza_il_promemoria():
    d = r.analizza(TESTO)
    assert d["cf"] == "RSSMRA80A01H501U" and d["nre"] == "010A31234567890"
    assert d["prestazione"].startswith("ECOGRAFIA OSTETRICA") and d["priorita"] == "Programmabile"
    assert d["data"] == "29-09-2026" and d["quesito"] == "DOLORE PELVICO" and d["paziente"] == "ROSSI MARIA"
    assert d["problemi"] == []


def test_layout_a_righe_separate():
    testo = TESTO.replace("PRESCRIZIONE 88.78", "PRESCRIZIONE QTA' NOTA\n88.78").replace(" ALTRA TIPOLOGIA", "\n1\nALTRA TIPOLOGIA")
    assert r.analizza(testo)["prestazione"] == "ECOGRAFIA OSTETRICA"


def test_campi_che_non_tornano_restano_vuoti():
    d = r.analizza(TESTO.replace("*RSSMRA80A01H501U*", "*RSSMRA80A01H501X*").replace("*1234567890*", "*12345*"))
    assert d["cf"] == "" and d["nre"] == "" and "codice fiscale" in d["problemi"] and "numero ricetta (NRE)" in d["problemi"]


def test_file_che_non_e_un_pdf_o_troppo_grande():
    with pytest.raises(r.PdfNonLeggibile):
        r.testo_di(b"<html>")
    with pytest.raises(r.PdfNonLeggibile):
        r.testo_di(b"%PDF" + b"0" * r.MAX_BYTE)


def test_pdf_vero_con_testo():
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    pagina = doc.new_page()
    pagina.insert_textbox(fitz.Rect(30, 30, 560, 800), TESTO, fontsize=8)
    d = r.leggi(doc.tobytes())
    assert d["cf"] == "RSSMRA80A01H501U" and d["nre"] == "010A31234567890"


def test_testi_enormi_restano_veloci():
    import time
    for testo in ("QUESITO DIAGNOSTICO: a\n" * 4000, "ASSISTITO: a\n" * 4000, "PRIORITA' " * 6000):
        t0 = time.time()
        r.analizza(testo)
        assert time.time() - t0 < 1


def test_il_codice_fiscale_del_medico_non_e_quello_dell_assistito():
    testo = "ASSISTITO: ROSSI MARIA INDIRIZZO: x CODICE FISCALE DEL MEDICO: RSSMRA80A01H501U " + "x" * 50
    assert r.analizza(testo)["cf"] == ""


def test_nre_con_i_codici_lontani_nel_testo():
    # alcuni lettori di PDF mettono l'indirizzo tra i due pezzi del NRE
    testo = TESTO.replace("*1234567890* *1234567890*", "x" * 150 + " *1234567890* *1234567890*")
    assert r.analizza(testo)["nre"] == "010A31234567890"
