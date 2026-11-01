"""Segnalazioni dalle app desktop: formato, limiti, niente dati personali."""
import json

import pytest
from cryptography.fernet import Fernet

import segnalazioni_app as sa
from store import Store

CF, NRE = "RSSMRA80A01L219X", "010A00000000001"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite", Fernet.generate_key().decode())


def corpo(**kw):
    d = {"v": 1, "app": "0.3.1", "sistema": "windows-x86_64", "installazione": "nsis", "id": "0123456789abcdef",
         "tipo": "avvio", "testo": "L'app non riesce a partire", "rapporto": "2026-11-01 10:00:00 avvio\nerrore"}
    d.update(kw)
    return json.dumps(d).encode()


def test_segnalazione_valida_in_coda_senza_dati(store):
    r = sa.Ricevitore()
    rapporto = f"riga con {CF} e {NRE}, visita il 16/02/2027 alle 09:00"
    assert r.ricevi(store, corpo(rapporto=rapporto)) == (200, {"ok": True})
    [s] = store.segnalazioni(da_inviare=True)
    assert s["origine"] == "app" and s["livello"] == "errore" and "non riesce a partire" in s["testo"]
    assert "0.3.1" in s["testo"] and "windows-x86_64" in s["testo"]
    assert CF not in s["rapporto"] and NRE not in s["rapporto"] and "16/02/2027" not in s["rapporto"]
    assert "0123456789abcdef" in s["rapporto"]


@pytest.mark.parametrize("modifica", [
    {"v": 2}, {"tipo": "altro"}, {"id": "xyz"}, {"app": "<script>"}, {"sistema": "Windows 11"},
    {"testo": "x" * 201}, {"rapporto": "x" * 24001}, {"rapporto": 3}])
def test_campi_non_validi_rifiutati(store, modifica):
    stato, risposta = sa.Ricevitore().ricevi(store, corpo(**modifica))
    assert stato == 400 and not risposta["ok"] and not store.segnalazioni()


def test_non_json_e_troppo_lunga(store):
    r = sa.Ricevitore()
    assert r.ricevi(store, b"ciao")[0] == 400
    assert r.ricevi(store, b"x" * (sa.MAX_CORPO + 1))[0] == 413


def test_limiti_per_installazione_e_in_tutto(store):
    r = sa.Ricevitore()
    ora = 1_800_000_000.0
    for i in range(3):
        assert r.ricevi(store, corpo(), ora + i)[0] == 200
    assert r.ricevi(store, corpo(), ora + 10)[0] == 429  # la stessa installazione: aspetta
    assert r.ricevi(store, corpo(), ora + 3700)[0] == 200  # un'ora dopo si'
    for i in range(sa.IN_TUTTO[0]):
        r.ricevi(store, corpo(id=f"{i:016x}"), ora + 4000 + i, fonte=f"10.0.0.{i}")
    assert r.ricevi(store, corpo(id="f" * 16), ora + 5000, fonte="10.9.9.9")[0] == 429  # tetto del giorno


def test_dallo_stesso_indirizzo_non_si_esaurisce_il_tetto(store):
    r = sa.Ricevitore()
    ora = 1_800_000_000.0
    esiti = [r.ricevi(store, corpo(id=f"{i:016x}"), ora + i, fonte="203.0.113.7")[0] for i in range(10)]
    assert esiti.count(200) == sa.PER_PROVENIENZA[0] and esiti[-1] == 429
    assert r.ricevi(store, corpo(id="e" * 16), ora + 20, fonte="198.51.100.1")[0] == 200  # gli altri passano
    assert "203.0.113.7" not in str(r.fonti)  # l'indirizzo non resta, nemmeno in memoria


def test_niente_link_nel_messaggio(store):
    sa.Ricevitore().ricevi(store, corpo(testo="vai su https://esempio.invalid/x per sapere"))
    assert "esempio.invalid" not in store.segnalazioni()[0]["testo"]


def test_testo_costruito_apposta_non_rallenta(store):
    import time
    t = time.time()
    for brutto in ("A" * 24000, "1-" * 11999, "0." * 11999, "a1" * 11999):
        sa.Ricevitore().ricevi(store, corpo(rapporto=brutto))
    assert time.time() - t < 1


def test_testo_su_una_riga(store):
    sa.Ricevitore().ricevi(store, corpo(testo="due\nrighe"))
    assert "due righe" in store.segnalazioni()[0]["testo"]
