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


# --- nuove funzioni: aggiungi, modifica, cancella, date viste, storico, admin ---------------
CF3, NRE3 = "BNCLRA70B41F205Z", "010A00000000033"


def cerca_e_attendi(app, chat, dati, percorso="/ui/nuova"):
    stato, _, corpo = post(app, percorso, dati, chat=chat)
    assert stato == 200, corpo
    token = __import__("re").search(r"/ui/esito/([0-9a-f]{16})", corpo.decode())
    assert token, corpo.decode()
    app.bot.esegui_coda()  # il bot fa la ricerca sul portale (finto)
    return get(app, f"/ui/esito/{token.group(1)}", chat=chat), token.group(1)


def test_nuovo_utente_serve_il_consenso(app):
    stato, _, corpo = post(app, "/ui/nuova", {"cf": CF3, "nre": NRE3}, chat=3)
    assert "consenso" in corpo.decode() and not app.store.della_chat(3)


def test_aggiungi_ricetta_dall_app(app):
    (stato, _, corpo), token = cerca_e_attendi(app, 3, {"cf": CF3.lower(), "nre": NRE3, "consenso": "1"})
    t = corpo.decode()
    assert stato == 200 and "Ho trovato la prenotazione" in t and 'name="tipo"' in t
    [p] = app.store.della_chat(3)
    assert p["stato"] == "sede" and p["cf"] == CF3 and CF3 not in t and NRE3 not in t
    post(app, f"/ui/r/{p['id']}/dove", {"tipo": "altro", "comune": "Alba"}, chat=3)
    p = app.store.get(p["id"])
    assert p["stato"] == "attivo" and p["zona"] == {"tipo": "comune", "valore": "ALBA"}
    assert get(app, f"/ui/esito/{token}", chat=3)[0] == 404  # esito gia' consegnato


def test_esito_di_un_altra_chat_non_si_vede(app):
    stato, _, corpo = post(app, "/ui/nuova", {"cf": CF3, "nre": NRE3, "consenso": "1"}, chat=3)
    token = __import__("re").search(r"/ui/esito/([0-9a-f]{16})", corpo.decode()).group(1)
    app.bot.esegui_coda()
    assert get(app, f"/ui/esito/{token}", chat=1)[0] == 404


def test_aggiungi_dati_non_validi_e_ricetta_gia_seguita(app):
    _, _, corpo = post(app, "/ui/nuova", {"cf": "ciao", "nre": NRE3}, chat=1)
    assert "codice fiscale non sembra valido" in corpo.decode() and app.bot.coda.empty()
    (_, _, corpo), _ = cerca_e_attendi(app, 3, {"cf": CF, "nre": NRE, "consenso": "1"})
    assert "già seguita" in corpo.decode()


def test_cambia_ricetta(app):
    fam = pratica(app.bot, 1, 1)
    (stato, _, corpo), _ = cerca_e_attendi(app, 1, {"cf": CF3, "nre": NRE3}, percorso=f"/ui/r/{fam['id']}/modifica")
    p = app.store.get(fam["id"])
    assert p["cf"] == CF3 and p["nre"] == NRE3 and p["nome"] == "Familiare" and p["auto"] is None


def test_rinomina(app):
    pid = pratica(app.bot, 1)["id"]
    post(app, f"/ui/r/{pid}/nome", {"nome": "Io"})
    assert app.store.get(pid)["nome"] == "Io"
    assert post(app, f"/ui/r/{pid}/nome", {"nome": CF3})[0] == 400


def test_cancella_una_e_tutte(app):
    io, fam = app.bot.store.della_chat(1)
    stato, _, _ = post(app, f"/ui/r/{fam['id']}/cancella", {}, auth_date=time.time() - 3 * 3600)
    assert stato == 401 and app.store.get(fam["id"])  # firma vecchia: niente
    post(app, f"/ui/r/{fam['id']}/cancella", {})
    assert app.store.get(fam["id"]) is None and ("dimentica", 1, fam["id"]) in list(app.bot.coda.queue)
    app.store.set_pannello(1, 777)
    _, _, corpo = post(app, "/ui/cancella-tutto", {})
    assert not app.store.della_chat(1) and ("sgancia", 1, 777) in list(app.bot.coda.queue)
    assert "Aggiungi la prima ricetta" in corpo.decode()


def test_date_viste_luoghi_e_storico_dopo_un_controllo(app):
    b = app.bot
    fam = pratica(b, 1, 1)
    b.controlla(fam)
    b.offerte.clear()
    fam = b.store.get(fam["id"])
    assert fam["viste"] and fam["luoghi"] and len(fam["storico"]) == 1
    _, _, corpo = get(app, f"/ui/r/{fam['id']}/date")
    t = corpo.decode()
    assert "Prima della tua, dove cerchi" in t and "Fuori da dove cerchi" in t and 'value="ASTI"' in t
    p2 = b.store.get(fam["id"])
    p2["storico"].append({"t": time.time() + 60, "a": p2["storico"][0]["a"], "r": p2["storico"][0]["r"]})
    b.store.save(p2)
    _, _, corpo = get(app, f"/ui/r/{fam['id']}/storico")
    t = corpo.decode()
    assert "<svg" in t and "tua prenotazione" in t and "<title>" in t and "Tabella dei cambiamenti" in t


def test_sede_solo_tra_quelle_viste(app):
    b = app.bot
    fam = pratica(b, 1, 1)
    b.controlla(fam)
    assert post(app, f"/ui/r/{fam['id']}/dove", {"tipo": "sede_vista", "sede": "OSPEDALE INVENTATO"})[0] == 400
    post(app, f"/ui/r/{fam['id']}/dove", {"tipo": "sede_vista", "sede": "OSP ALBA"})
    assert app.store.get(fam["id"])["zona"] == {"tipo": "sede", "valore": "OSP ALBA"}


def test_admin_solo_per_admin_e_metriche(app, monkeypatch):
    b = app.bot
    b.admin = "1"
    b.portale(lambda: None)
    assert b.metriche and b.metriche[-1][2] is True
    stato, _, corpo = get(app, "/ui/admin", chat=1)
    assert stato == 200 and "ricette attive" in corpo.decode() and "sessioni" in corpo.decode()
    assert get(app, "/ui/admin", chat=2)[0] == 404


def test_scheda_con_prossimo_controllo_e_barra(app):
    _, _, corpo = get(app, "/ui/ricette")
    t = corpo.decode()
    assert "⏭ Prossimo" in t and "📅 Date viste" in t and "📈 Storico" in t and "🔒" in t
    assert "＋ Aggiungi" in t  # 2 ricette su 3: si puo' aggiungere
    app.bot.max_pratiche = 2
    assert "＋ Aggiungi" not in get(app, "/ui/ricette")[2].decode()


def test_date_viste_prenotabili_anche_fuori_area(app):
    b = app.bot
    fam = pratica(b, 1, 1)  # cerca solo ad Alba; Asti e' prima ma fuori area
    b.controlla(fam)
    b.offerte.clear()
    _, _, corpo = get(app, f"/ui/r/{fam['id']}/date")
    t = corpo.decode()
    assert t.count('action="/ui/r/') >= 2 and "fuori da dove cerchi" in t and "PRIMA della data attuale" in t
    from test_bot import ASTI
    att = b.store.get(fam["id"])["attuale"]["quando"]
    post(app, f"/ui/r/{fam['id']}/vista", {"slot": ASTI.key(), "att": att})
    b.esegui_coda()
    assert b.chiamate[-1][1] == ASTI.key() and b.libere[-1] is True  # scelta libera: fuori area ammessa
    assert fam["id"] not in b.sessioni  # la sessione usata non si riusa


def test_date_viste_vecchie_non_prenotabili(app):
    b = app.bot
    fam = pratica(b, 1, 1)
    b.controlla(fam)
    b.sessioni[fam["id"]]["ts"] -= 3600
    _, _, corpo = get(app, f"/ui/r/{fam['id']}/date")
    assert "Aggiorna le date" in corpo.decode() and 'action="/ui/r/' not in corpo.decode()
    from test_bot import ASTI
    post(app, f"/ui/r/{fam['id']}/vista", {"slot": ASTI.key()})
    b.esegui_coda()
    assert not b.chiamate


def test_vista_chiave_non_valida_e_firma_vecchia(app):
    fam = pratica(app.bot, 1, 1)
    assert post(app, f"/ui/r/{fam['id']}/vista", {"slot": "<script>"})[0] == 400
    assert post(app, f"/ui/r/{fam['id']}/vista", {"slot": "202701010900|X"}, auth_date=time.time() - 3 * 3600)[0] == 401


def test_riga_date_sulla_scheda(app):
    fam = pratica(app.bot, 1, 1)
    app.bot.controlla(fam)
    _, _, corpo = get(app, "/ui/ricette")
    assert "date disponibili · la prima:" in corpo.decode()


def test_ricetta_da_completare_visibile_e_cancellabile(app):
    (_, _, _), _ = cerca_e_attendi(app, 3, {"cf": CF3, "nre": NRE3, "consenso": "1"})
    [p] = app.store.della_chat(3)
    _, _, corpo = get(app, "/ui/ricette", chat=3)
    assert "Da completare" in corpo.decode()
    post(app, f"/ui/r/{p['id']}/cancella", {}, chat=3)
    assert not app.store.della_chat(3)


def test_consenso_registrato_con_la_data(app):
    cerca_e_attendi(app, 3, {"cf": CF3, "nre": NRE3, "consenso": "1"})
    [p] = app.store.della_chat(3)
    assert p["consenso_ts"] > 0


def test_una_ricerca_alla_volta_e_limite_giornaliero(app):
    post(app, "/ui/nuova", {"cf": CF3, "nre": NRE3, "consenso": "1"}, chat=3)
    assert post(app, "/ui/nuova", {"cf": CF3, "nre": NRE3, "consenso": "1"}, chat=3)[0] == 429
    b = app.bot
    b.ricerche_app[1] = [time.time()] * 10
    b.cerca_da_app(1, None, "t" * 16, CF3, NRE3, "", "nuova", time.time(), False)
    assert "Troppe ricerche" in b.risultati["t" * 16]["errore"]


def test_cancella_tutto_scarta_la_ricerca_in_coda(app):
    post(app, "/ui/nuova", {"cf": CF3, "nre": NRE3, "consenso": "1"}, chat=3)
    app.bot.cancellate[3] = time.time() + 1  # cancellazione arrivata dopo la richiesta
    app.bot.esegui_coda()
    assert not app.store.della_chat(3)


def test_vista_non_prenota_se_la_prenotazione_e_cambiata(app):
    b = app.bot
    fam = pratica(b, 1, 1)
    b.controlla(fam)
    from test_bot import ASTI
    post(app, f"/ui/r/{fam['id']}/vista", {"slot": ASTI.key(), "att": "2020-01-01T09:00:00"})
    b.esegui_coda()
    assert not b.chiamate  # nel frattempo (es. conferma automatica) la prenotazione era un'altra


def test_ogni_prenotazione_consuma_la_sessione(app):
    b = app.bot
    io = pratica(b, 1)
    b.controlla(io)
    assert io["id"] in b.sessioni
    token = b.offerte[io["id"]]["token"]
    b.usa_offerta(b.store.get(io["id"]), "p", token, "0")
    assert io["id"] not in b.sessioni


def test_sessioni_scartate_con_la_ricetta(app):
    b = app.bot
    fam = pratica(b, 1, 1)
    b.controlla(fam)
    post(app, f"/ui/r/{fam['id']}/cancella", {})
    b.esegui_coda()
    assert fam["id"] not in b.sessioni and fam["id"] not in b.offerte


def test_chiave_doppia_non_si_prenota(app):
    b = app.bot
    fam = pratica(b, 1, 1)
    b.controlla(fam)
    s = b.sessioni[fam["id"]]
    s["slots"] = s["slots"] + [s["slots"][0]]  # due date con la stessa chiave
    att = b.store.get(fam["id"])["attuale"]["quando"]
    assert b.prenota_vista(b.store.get(fam["id"]), s["slots"][0].key(), att) == "fallita" and not b.chiamate
