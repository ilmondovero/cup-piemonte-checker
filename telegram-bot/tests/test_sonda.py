import time

import pytest
from cryptography.fernet import Fernet

import sonda
from store import Store
import bot as botmod

TZ = botmod.TZ


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "s.db", Fernet.generate_key().decode())


def test_episodio_si_apre_dopo_tre_fallite_e_si_chiude_alla_prima_riuscita(store):
    t = 1_000_000.0
    store.sonda(t, 0.3, 200, "ok")
    store.sonda(t + 300, 30.0, None, "timeout")
    store.sonda(t + 600, 30.0, None, "timeout")
    assert store.episodi() == []  # due sole: un intoppo isolato non e' un episodio
    store.sonda(t + 900, 0.1, 503, "http")
    assert store.episodi() == [(t + 300, None, "timeout")]  # inizio e motivo: la prima fallita
    store.sonda(t + 1200, 30.0, None, "timeout")
    assert len(store.episodi()) == 1  # sempre lo stesso episodio
    store.sonda(t + 1500, 0.4, 200, "ok")
    assert store.episodi() == [(t + 300, t + 1500, "timeout")]


def test_niente_si_cancella(store):
    vecchio = time.time() - 400 * 86400
    store.sonda(vecchio, 0.3, 200, "ok")
    store.metrica(vecchio, 5.0, True, 2.0, False)
    store.metrica_passi([(vecchio, "elenco", 1.0, "ok", 200)])
    store.sonda(time.time(), 0.3, 200, "ok")
    store.metrica(time.time(), 5.0, True, 2.0, False)
    assert store.sonde_totali()[0] == 2 and len(store.metriche(0)[0]) == 2 and len(store.metriche_passi(0)) == 1


def test_disponibilita_e_ore_del_giorno():
    t = 1_700_000_000.0
    sonde = [(t + i * 300, 1.0 + i, 200, "ok") for i in range(8)] + [(t + 9 * 300, 30.0, None, "timeout")]
    assert sonda.disponibilita(sonde) == pytest.approx(100 * 8 / 9)
    assert sonda.disponibilita([]) is None
    per_ora = sonda.per_ora_del_giorno(sonde, TZ)
    n, ok, mediana, p95 = next(v for v in per_ora.values() if v[0])
    assert n == 9 and ok == 8 and mediana == pytest.approx(4.5)
    ore = sonda.ultime_ore(sonde, t + 3600, TZ)
    assert len(ore) == 24 and sum(o[1] for o in ore) == 9 and sum(o[2] for o in ore) == 1


def test_misura_classifica_gli_esiti(monkeypatch):
    class R:
        status_code = 200
    monkeypatch.setattr(sonda.requests, "get", lambda *a, **k: R())
    assert sonda.misura()[1:] == (200, "ok")
    R.status_code = 503
    assert sonda.misura()[1:] == (503, "http")
    R.status_code = 403  # il portale risponde: e' su
    assert sonda.misura()[2] == "ok"

    def timeout(*a, **k):
        raise sonda.requests.Timeout()
    monkeypatch.setattr(sonda.requests, "get", timeout)
    assert sonda.misura()[1:] == (None, "timeout")

    def rete(*a, **k):
        raise sonda.requests.ConnectionError()
    monkeypatch.setattr(sonda.requests, "get", rete)
    assert sonda.misura()[2] == "rete"


def test_un_buco_nelle_sonde_non_inventa_episodi(store):
    t = 1_000_000.0
    for i in range(3):
        store.sonda(t + i * 300, 30.0, None, "timeout")
    assert store.episodi() == [(t, None, "timeout")]
    store.sonda(t + 2 * 86400, 0.3, 200, "ok")  # il bot era fermo due giorni
    assert store.episodi() == [(t, t + 600, "timeout")]  # chiuso all'ultima sonda vista, non due giorni dopo
    # fallite separate da un buco non fanno un episodio
    store.sonda(t + 3 * 86400, 30.0, None, "timeout")
    store.sonda(t + 3 * 86400 + 300, 30.0, None, "timeout")
    store.sonda(t + 5 * 86400, 30.0, None, "timeout")
    assert len(store.episodi()) == 1


def test_ultime_ore_con_il_cambio_dell_ora_legale():
    import datetime as dt
    ora = dt.datetime(2026, 10, 25, 9, 30, tzinfo=TZ).timestamp()  # il giorno in cui l'ora torna indietro
    ore = sonda.ultime_ore([(ora - 3600 * 7 + 10, 1.0, 200, "ok")], ora, TZ)
    assert len(ore) == 24 and sum(o[1] for o in ore) == 1
    inizi = [o[0].timestamp() for o in ore]
    assert all(b - a == 3600 for a, b in zip(inizi, inizi[1:]))  # sempre ore vere
