"""Mini App: firma Telegram, permessi, schede e azioni. Senza rete verso Telegram o il portale."""
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot as botmod  # noqa: E402
import webapp  # noqa: E402
from store import Store  # noqa: E402
from test_bot import CF, NRE, aggiungi_familiare, b, pratica, registra  # noqa: E402,F401

TOKEN = "123456:TEST-token-per-i-test"


def firma(user_id, token=TOKEN, auth_date=None, **extra):
    campi = {"auth_date": str(int(auth_date or time.time())), "query_id": "AAA",
             "user": json.dumps({"id": user_id, "first_name": "Prova"}), **extra}
    stringa = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()))
    segreto = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    campi["hash"] = hmac.new(segreto, stringa.encode(), hashlib.sha256).hexdigest()
    return urlencode(campi)


@pytest.fixture
def app(b, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "PAUSA_AZIONI", 0)  # il limite di frequenza ha il suo test
    b.token = TOKEN
    b.webapp_url = "https://esempio.invalid/app"
    registra(b)
    aggiungi_familiare(b)
    return webapp.App(b, b.store)


def get(app, percorso, chat=1, **kw):
    return app.gestisci("GET", percorso, {"authorization": "tma " + firma(chat, **kw)})


def post(app, percorso, dati, chat=1, **kw):
    return app.gestisci("POST", percorso, {"authorization": "tma " + firma(chat, **kw)}, urlencode(dati).encode())


def test_firma_initdata():
    assert webapp.verifica_init_data(firma(42), TOKEN) == 42
    assert webapp.verifica_init_data(firma(42, token="altro:token"), TOKEN) is None  # firmata da un altro bot
    assert webapp.verifica_init_data(firma(42, auth_date=time.time() - 2 * 86400), TOKEN) is None  # scaduta
    manomessa = firma(42).replace("%22id%22%3A+42", "%22id%22%3A+43")
    assert webapp.verifica_init_data(manomessa, TOKEN) is None
    assert webapp.verifica_init_data("", TOKEN) is None
    assert webapp.verifica_init_data(firma(42).replace("hash=", "hash=%C3%A9"), TOKEN) is None  # niente 500


def test_senza_firma_niente_dati(app):
    stato, _, corpo = app.gestisci("GET", "/ui/ricette", {})
    assert stato == 401 and CF.encode() not in corpo


def test_schede_con_data_ora_luogo(app):
    stato, _, corpo = get(app, "/ui/ricette")
    t = corpo.decode()
    assert stato == 200 and t.count('class="scheda') == 2
    assert "Ospedale A" in t and "ore <strong>" in t and "Via Roma, 1 - Torino (TO)" in t
    assert "solo nel comune di Alba" in t and "Familiare" in t
    assert CF not in t and NRE not in t  # mai codice fiscale o ricetta nell'app


def test_ricette_di_altri_invisibili_e_intoccabili(app):
    _, _, corpo = get(app, "/ui/ricette", chat=2)
    assert b"Ospedale A" not in corpo
    pid = pratica(app.bot, 1)["id"]
    stato, _, _ = post(app, f"/ui/r/{pid}/pausa", {}, chat=2)
    assert stato == 404 and app.store.get(pid)["stato"] == "attivo"
    stato, _, _ = get(app, f"/ui/r/{pid}/dove", chat=2)
    assert stato == 404


def test_cambia_zona_e_automatica(app):
    pid = pratica(app.bot, 1)["id"]
    stato, _, corpo = post(app, f"/ui/r/{pid}/dove", {"tipo": "altro", "comune": "  alba "})
    assert stato == 200 and app.store.get(pid)["zona"] == {"tipo": "comune", "valore": "ALBA"}
    assert "cerco solo nel comune di Alba" in corpo.decode()
    post(app, f"/ui/r/{pid}/auto", {"giorni": "3"})
    assert app.store.get(pid)["auto"] == {"giorni": 3}
    post(app, f"/ui/r/{pid}/auto", {"giorni": "0"})
    assert app.store.get(pid)["auto"] is None
    stato, _, _ = post(app, f"/ui/r/{pid}/auto", {"giorni": "99"})
    assert stato == 400
    stato, _, _ = post(app, f"/ui/r/{pid}/dove", {"tipo": "altro", "comune": "<script>"})
    assert stato == 400
    assert ("pannello", 1, None) in list(app.bot.coda.queue)  # il pannello in chat si aggiorna


def test_pausa_e_riprendi(app):
    pid = pratica(app.bot, 1)["id"]
    post(app, f"/ui/r/{pid}/pausa", {})
    assert app.store.get(pid)["stato"] == "pausa"
    _, _, corpo = post(app, f"/ui/r/{pid}/riprendi", {})
    assert app.store.get(pid)["stato"] == "attivo" and "riattivati" in corpo.decode()


def test_azioni_sul_portale_vanno_in_coda_al_bot(app):
    pid = pratica(app.bot, 1)["id"]
    post(app, f"/ui/r/{pid}/controlla", {})
    assert ("controlla", 1, pid) in list(app.bot.coda.queue)
    stato, _, _ = post(app, f"/ui/r/{pid}/offerta", {"tipo": "p", "token": "zz", "indice": "0"})
    assert stato == 400  # token malformato


def test_prenota_dall_app_usa_la_sessione_dell_offerta(app):
    b = app.bot
    p = pratica(b, 1)
    b.controlla(p)  # offerta aperta per la mia ricetta
    token = b.offerte[p["id"]]["token"]
    _, _, corpo = get(app, "/ui/ricette")
    assert "C’è una data prima" in corpo.decode() and token in corpo.decode()
    chiave = b.offerte[p["id"]]["slots"][0].key()
    assert chiave in corpo.decode()
    post(app, f"/ui/r/{p['id']}/offerta", {"tipo": "p", "token": token, "indice": "0", "slot": chiave})
    b.esegui_coda()
    assert b.chiamate and b.chiamate[-1][2] == f"S-{CF}"  # stessa sessione che tiene la data
    b.chiamate.clear()
    post(app, f"/ui/r/{p['id']}/offerta", {"tipo": "p", "token": token, "indice": "0", "slot": chiave})  # doppio invio
    b.esegui_coda()
    assert not b.chiamate


def test_la_coda_ricontrolla_il_proprietario(app):
    b = app.bot
    pid = pratica(b, 1)["id"]
    b.coda.put(("controlla", 2, pid))  # chat sbagliata
    b.esegui_coda()
    assert not b.zone_viste


def test_modifica_dall_app_non_si_perde_dopo_un_controllo(app):
    b = app.bot
    p = pratica(b, 1)  # il bot ha in mano questa copia durante un controllo lungo...
    post(app, f"/ui/r/{p['id']}/auto", {"giorni": "7"})  # ...intanto l'utente attiva l'automatica
    b.salva(p, "errori", "ultimo")
    assert b.store.get(p["id"])["auto"] == {"giorni": 7}


def test_intestazioni_di_sicurezza_e_file_statici(app):
    webapp._Gestore.app = app
    server = HTTPServer(("127.0.0.1", 0), webapp._Gestore)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/") as r:
            assert "script-src 'self' https://telegram.org" in r.headers["Content-Security-Policy"]
            assert r.headers["X-Content-Type-Options"] == "nosniff" and b'hx-get="/ui/ricette"' in r.read()
        with urllib.request.urlopen(base + "/static/htmx.min.js") as r:
            assert r.status == 200 and "javascript" in r.headers["Content-Type"]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/static/../webapp.py")
        assert err.value.code == 404
    finally:
        server.shutdown()


def test_bottone_app_nel_menu_e_nel_pannello(app):
    b = app.bot
    b.out.clear()
    b.imposta_menu()
    menu = [d for m, d in b.out if m == "setChatMenuButton"][-1]["menu_button"]
    assert menu["type"] == "web_app" and menu["web_app"]["url"] == b.webapp_url
    _, righe = b.testo_pannello(1)
    assert righe[0][0]["web_app"]["url"] == b.webapp_url


def test_prenota_solo_la_data_confermata(app):
    b = app.bot
    p = pratica(b, 1)
    b.controlla(p)
    o = b.offerte[p["id"]]
    post(app, f"/ui/r/{p['id']}/offerta", {"tipo": "p", "token": o["token"], "indice": "0", "slot": "altra-data"})
    b.esegui_coda()
    assert not b.chiamate  # l'offerta nel frattempo e' cambiata: non si prenota


def test_prenota_serve_una_firma_recente(app):
    b = app.bot
    p = pratica(b, 1)
    b.controlla(p)
    o = b.offerte[p["id"]]
    stato, _, _ = post(app, f"/ui/r/{p['id']}/offerta",
                       {"tipo": "p", "token": o["token"], "indice": "0", "slot": o["slots"][0].key()},
                       auth_date=time.time() - 3 * 3600)
    assert stato == 401 and b.coda.empty()


def test_limite_di_frequenza(app, monkeypatch):
    monkeypatch.setattr(webapp, "PAUSA_AZIONI", 60)
    pid = pratica(app.bot, 1)["id"]
    assert post(app, f"/ui/r/{pid}/pausa", {})[0] == 200
    assert post(app, f"/ui/r/{pid}/riprendi", {})[0] == 429


def test_coda_piena(app):
    pid = pratica(app.bot, 1)["id"]
    for _ in range(app.bot.coda.maxsize - app.bot.coda.qsize()):
        app.bot.coda.put_nowait(("pannello", 1, None))
    assert post(app, f"/ui/r/{pid}/controlla", {})[0] == 503


def test_niente_prenotazione_automatica_se_in_pausa(app):
    b = app.bot
    p = pratica(b, 1)
    p["auto"] = {"giorni": 1}
    b.store.save(p)
    post(app, f"/ui/r/{p['id']}/pausa", {})
    from test_bot import MEGLIO
    assert b.prenota(p, MEGLIO, "S", automatica=True) == "fallita" and not b.chiamate


def test_server_multithread_con_timeout():
    assert webapp._Gestore.timeout == 10
